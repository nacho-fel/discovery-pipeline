# CLAUDE.md — discovery-pipeline

Durable project instructions for Claude Code sessions working in this
repo. For current state (commit, test results, next steps), see
[docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md).

## What this project is

An upstream internet-discovery system for the MIT EGS/geothermal
cost-evidence research project. It discovers, screens, and acquires
research resources — not just documents, but PDFs, datasets, spreadsheets,
and multi-asset repository landing pages — likely to contain
extraction-cost evidence, then hands validated resources to the existing
**geocost** document-to-database pipeline via a versioned, self-hashed
handoff manifest.

**geocost is a separate, read-only reference repository.** Never modify
it, never write into its database, never add code to it. The two systems
are coupled only through the handoff manifest — see
[docs/handoff_contract.md](docs/handoff_contract.md) and
[docs/geocost_format_compatibility.md](docs/geocost_format_compatibility.md)
for which formats geocost can currently ingest.

## The actual objective

**Source count is an intermediate metric. Observation yield, evidence
quality, and coverage are the final objectives.** Priority order:
1. Cost observations — primary
2. Technical/engineering observations connected to cost — primary
3. Geology — supporting
4. Resource-potential — supporting

Every observation must remain traceable to exact evidence. Structured/
tabular resources (spreadsheets, datasets, cost tables) rank higher than
narrative prose because they yield far more observations per source — see
`screener.py`'s yield-aware ranking.

## Production discovery budget

**5,000 physical SerpApi requests is a hard campaign ceiling, not a
target.** It's enforced by a durable, atomically-reserved database counter
(`quota_ledger.py`), correct across process restarts and concurrent
invocations. The *preferred* outcome is to stop substantially earlier once
marginal yield declines — batches are evaluated for yield (overall and per
query family), low-yield families lose future allocation, and the whole
campaign stops on sustained saturation. Never raise the default ceiling
above 5,000 in config; lowering it is fine.

A repeated (normalized) SerpApi query must never spend campaign quota
twice — caching is checked before any quota is reserved. See
[docs/adaptive_discovery.md](docs/adaptive_discovery.md) for the full
design (retry policy, pagination, staged allocation, stopping criteria).

## Access and credentials — hard constraints

- **Never store or request MIT Kerberos credentials, passwords, MFA
  tokens, or browser cookies.**
- **Never automate a login or a systematic download from a licensed
  publisher.** A restricted resource (`licensed_mit_access`,
  `authentication_required`, `manual_acquisition_required`,
  `metadata_only`, `unavailable`) must never enter automated acquisition —
  only `open_access` (or an unclassified URL, validated at request time by
  `acquirer.py`'s own runtime checks) may be fetched automatically.
- Restricted resources are queued for a human via
  [docs/mit_assisted_acquisition.md](docs/mit_assisted_acquisition.md): the
  queue is exported, a researcher retrieves the file themselves (VPN,
  Touchstone, LibKey Nomad), then `scan-inbox` validates and matches it
  back. This pipeline is never in the authentication loop.

## Generated-data and secret hygiene

- `data/` (databases, downloads, caches, manual inbox, audit artifacts,
  logs) and `.env` are gitignored — never commit anything under them.
- Never display, log, or commit an API key.
- Before staging or committing, check `git status` output for anything
  that could carry a secret, even under an innocuous filename.

## Before making any change

1. Run `git status` and check `git rev-parse HEAD` / `origin/main` —
   never assume a clean or synced state.
2. If a command could discard uncommitted work, stash or commit it first.
3. Check [docs/CURRENT_STATUS.md](docs/CURRENT_STATUS.md) for what's
   already done and what's explicitly pending approval.

## Requires explicit user approval, every time

- Any live network call (SerpApi, GDR, a publisher, or a document
  download) — everything defaults to `--execute-network`-gated dry-run.
- Any destructive git operation (force-push, reset --hard, history rewrite).
- Any modification to the geocost repository, however small.
- `git push`, unless the active prompt explicitly authorizes it.
- Running a live pilot (`acquisition-pilot`, `multi-format-pilot`, or
  `open-resource-pilot` with `--execute-network`) or a production
  `adaptive-run` campaign.

## Testing and code quality

```bash
uv run pytest              # full test suite -- offline only, no live calls
uv run ruff check .        # lint
uv run mypy src            # type check
uv run discovery migrate   # apply Alembic migrations
```

Every test is offline: `httpx.MockTransport` for HTTP, injected fake DNS
resolvers for SSRF checks, real (temp) SQLite files for migration and
concurrency tests. No test may make a real network call.

## Key documents

- [docs/architecture.md](docs/architecture.md) — pipeline stages, schema,
  state machine, safety boundaries
- [docs/adaptive_discovery.md](docs/adaptive_discovery.md) — quota ledger,
  retry policy, pagination, staged allocation
- [docs/mit_assisted_acquisition.md](docs/mit_assisted_acquisition.md) —
  restricted-resource manual workflow
- [docs/geocost_format_compatibility.md](docs/geocost_format_compatibility.md)
  — which formats geocost can ingest today
- [docs/multi_format_pilot.md](docs/multi_format_pilot.md) /
  [docs/acquisition_pilot.md](docs/acquisition_pilot.md) — designed but
  not-yet-executed live pilots
- [docs/handoff_contract.md](docs/handoff_contract.md) — the manifest
  schema shared with geocost
