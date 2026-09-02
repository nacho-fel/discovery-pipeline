# Architecture

This pipeline discovers and acquires research resources likely to generate
large numbers of evidence-backed cost and technical observations for the
existing geocost database. Source count is an intermediate metric;
observation yield, evidence quality and coverage are the final objectives.

## Pipeline stages

```text
Coverage definition (config/coverage_matrix.yaml, config/query_templates.yaml)
    -> query_planner.py           deterministic, curated queries + fingerprints
    -> adapters/*                 SerpApi Google/Scholar, OpenAlex, Crossref, seed import
    -> result_registry.py         immutable QueryPlan/SearchExecution/SearchResult rows
    -> normalizer.py              DOI / URL / title normalization
    -> deduplicator.py            candidate identity matching + alias preservation
    -> screener.py                deterministic, high-recall accept/review/reject
    -> acquirer.py                safe, streamed, bounded, SSRF-guarded download
    -> handoff.py                 file validation + self-hashed manifest
    -> geocost (separate repo)    consumes the manifest, unaffected by this repo
```

`service.py` orchestrates plan -> execute -> (optional) frontier expansion,
with per-request commits, retry/backoff, request/query budgets, and resume.
`coverage_analyzer.py` tracks per-cell saturation across all runs.
`frontier.py` expands from accepted candidates (title/DOI/author/organization
lookups), budget- and depth-limited, recording `SourceLineageEdge`s.

## Database

Thirteen tables (`alembic/versions/0001_initial_schema.py` through
`0004_query_plan_kind.py`), mirroring geocost's own conventions:
`String(36)` UUID4 primary keys, status columns as `String(N)` with an
inline comment enumerating states (validated in code via
`state_machine.py`, not a DB `Enum`/`CHECK`), and `uq_*`/`idx_*` naming for
constraints/indexes.

| Table | Purpose |
|---|---|
| `discovery_run` | One bounded, resumable run (budgets, counters, status) |
| `query_plan` | One fingerprinted query, scoped to a run |
| `search_execution` | One HTTP attempt at a query_plan |
| `search_result` | One raw, unmodified provider result row |
| `source_candidate` | The deduplicated, canonical candidate |
| `candidate_alias` | Every raw occurrence of a candidate (never deleted) |
| `screening_decision` | One versioned, explainable screening decision |
| `acquisition_attempt` | One download attempt |
| `source_lineage_edge` | Directed provenance edges (`seeded_by`, `cites`, ...) |
| `coverage_cell` | Cumulative coverage-matrix cell state and saturation |
| `resource_asset` | One child downloadable asset of a multi-asset resource landing page |
| `quota_ledger_state` | One row per campaign: the durable, atomically-updated physical-request counter |
| `quota_ledger_entry` | One row per physical SerpApi HTTP attempt (initial or retry), the audit trail |

## State machine

```text
discovered -> normalized -> deduplicated
    -> screened_accept | screened_review | screened_reject
screened_accept -> acquisition_pending -> downloaded -> validated
    -> ingestion_ready -> handed_off

Failure/review states: download_failed, paywalled, metadata_only,
unsupported_format, corrupt_file, metadata_incomplete, validation_failed,
manual_review_required -- each has an explicit, limited set of allowed next
transitions (see state_machine.py); nothing skips back onto the happy path
implicitly.
```

In practice, a persisted `SourceCandidate` row is created directly at
`deduplicated` (normalization + dedup matching already happened by
construction before the row exists) rather than passing through `discovered`/
`normalized` as separate persisted states.

## Safety boundaries

- **Discovery never extracts.** No search snippet, title, or metadata is
  treated as cost evidence; `screener.py` only ever looks at title/snippet/
  URL/metadata to decide accept/review/reject, never full document text.
- **Acquisition never trusts a URL.** `acquirer.py` scheme-allowlists,
  SSRF-guards (rejects private/loopback/link-local/reserved/multicast
  resolved addresses) every hop including redirects, streams with a hard
  byte ceiling enforced mid-stream (not just from a spoofable
  `Content-Length` header), and validates magic bytes rather than trusting
  the URL extension or `Content-Type` header.
- **Secrets never leave the process boundary.** SerpApi's `api_key` is
  stripped from every cached/persisted response (`serpapi_common.redact_secrets`)
  and from every persisted request-parameters blob
  (`result_registry.strip_secrets`); see `tests/unit/test_secret_redaction.py`.
- **No network call without `--execute-network`.** Every CLI command that
  would make a real HTTP request defaults to dry-run/plan-only.
- **No credentials, ever, for restricted resources.** This pipeline never
  stores or requests MIT Kerberos credentials, passwords, MFA tokens, or
  browser cookies, and never automates systematic downloading from licensed
  publishers -- a restricted-access candidate is routed to `paywalled` and
  can only reach `handed_off` via a human manually retrieving the file (see
  `docs/mit_assisted_acquisition.md`), never an automated fetch.

## Concurrency

`discovery_concurrency` is accepted as configuration and stored on the run
for the record, but every request in this implementation executes
sequentially -- a SQLAlchemy `Session` isn't thread-safe, so real parallel
execution needs a session-per-worker pattern. This is a documented,
intentional scope reduction, not an oversight; everything else at this scale
(budget, batching, per-request commits, retry/backoff, resume, idempotent
reruns) is real and tested (see `tests/integration/test_budget_resume.py`'s
~280-request simulation -- a historical acceptance-test scale, not the
production target; see "Adaptive production discovery" below).

## Adaptive production discovery

Production discovery volume is not a fixed request count. The campaign has
a hard, durable ceiling -- **5,000 physical SerpApi requests, total**,
enforced by `quota_ledger.py`'s atomically-reserved counter (a database
row, correct across process restarts and concurrent invocations, never an
in-memory approximation). `adaptive_controller.py` runs bounded batches
against it, measuring deduplication and coverage after every batch (both
overall and per query *family*) and generating follow-up queries from
promising authors/citations/projects/datasets/repositories (`frontier.py`).
A run stops when several consecutive batches yield no new accepted
candidates and no new coverage cells, or when a query family specifically
goes low-yield (excluded from future batches), not when the 5,000-request
ceiling is exhausted -- the ceiling is a hard stop, not a target; the
preferred outcome is to stop substantially earlier. See
docs/adaptive_discovery.md for the full design: quota ledger, retry policy,
pagination, staged allocation, and stopping criteria.

## Handoff to geocost

See [handoff_contract.md](handoff_contract.md).
