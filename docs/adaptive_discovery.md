# Adaptive production discovery

## Why this exists

Earlier implementation phases used a fixed request-count target (~280
requests) as the acceptance-test scale for `execute_run`'s batching, resume,
and budget-accounting machinery. That number was never a production target
-- it was a convenient, bounded scenario for offline tests
(`tests/integration/test_budget_resume.py`, preserved unchanged as a
regression test).

**The production campaign ceiling is 5,000 physical SerpApi requests,
total, hard.** This is a project-level spending cap, not the SerpApi
Production subscription's own published limits (15,000/month, 3,000/hour,
still tracked and still binding when tighter -- see `QuotaTracker` below).
5,000 is a ceiling, not a target: the preferred outcome is to stop
substantially earlier once marginal discovery yield declines.
`adaptive_controller.py` runs bounded batches, measures what each batch
(and each query *family* within it) actually yielded, and stops once
further searching stops paying off.

## The campaign quota ledger (`quota_ledger.py`)

The hard ceiling is enforced by a **durable, atomically-reserved counter**,
not an in-memory or approximated one:

- `QuotaLedgerState` is one row per campaign (`Settings.campaign_id`,
  default `"production"`), holding `max_requests` and `reserved_count`.
- `quota_ledger.reserve()` performs exactly one atomic
  `UPDATE quota_ledger_state SET reserved_count = reserved_count + 1
  WHERE campaign_id = ? AND reserved_count < max_requests`, committed
  immediately, *before* the physical HTTP request it's reserving for. Two
  concurrent callers -- two threads, two processes, or two independent
  `discovery adaptive-run` invocations sharing the same database -- can
  never both observe room under the ceiling and both succeed past it,
  because SQL engines (including SQLite) serialize conflicting writes to
  the same row.
- The counter is a database row, so a process restart reads the same value
  back -- quota spent before a crash or interruption is never "found again."
- Every reservation also writes a `QuotaLedgerEntry` audit row in the same
  commit: query fingerprint, campaign + batch number, query family,
  whether it was an initial attempt or a retry (and why, if a retry),
  response status and yield (backfilled per-batch -- see below), and the
  quota counter's value immediately before and after.

**Where reservation actually happens:** `SerpApiClient.raw_search` (see
`adapters/serpapi_common.py`), immediately before the real `httpx` GET, and
strictly *after* its own on-disk cache-hit check. A cache hit never
reserves a unit -- "never pay twice for the same normalized query" means a
repeated query costs zero campaign quota, not just zero latency. Pass
`force_refresh=True` to `raw_search` to deliberately bypass the cache (and
spend a unit) when a genuinely fresh result is needed.

Per-call metadata (which batch, which query family, initial vs. retry, and
why) reaches `SerpApiClient` without changing every adapter's `search()`
signature: `call_with_retry` (see `adapters/base.py`) publishes an
`AttemptContext` via a `contextvars.ContextVar` immediately before each
physical attempt (tenacity retry included), and `SerpApiClient.raw_search`
reads it at reservation time.

## Retry policy

SerpApi retries are capped at `Settings.serpapi_max_retries` (default
**1**), far stricter than the general `discovery_max_retries` (5, used by
OpenAlex/Crossref, which aren't subject to the same hard campaign quota).
Only genuinely transient failures are retryable at all -- classified once,
centrally, in `adapters/base.py`'s `raise_for_httpx_response` and
`SerpApiClient.raw_search`:

| Condition | Retryable? |
|---|---|
| HTTP 429 (rate limited) | Yes -- honors `Retry-After` |
| HTTP 5xx | Yes |
| Timeout / connection error | Yes |
| HTTP 401/403 (auth failure) | No |
| Other HTTP 4xx | No |
| `search_metadata.status == "Error"` in a 200 response (malformed query) | No -- retrying a malformed query just re-spends quota reproducing the same failure |
| A valid, well-formed empty result set | Not an error at all -- never retried, never should be |

Retries still count against the campaign ceiling (`reserve()` is called
once per physical attempt, including retries) and against the existing
whole-run circuit breaker (`service._MAX_CONSECUTIVE_OPERATIONAL_FAILURES`,
10 consecutive operational failures pauses the run).

## Search-results pagination

Continuing to the next page of a query's results is not automatic.
`service._maybe_queue_next_page` queues the next page only if the provider
offered one (`response.next_page_cursor`) *and* the page just processed
produced at least `Settings.min_new_candidates_to_continue_pagination`
(default 1) genuinely new candidates *and* the query's page depth hasn't
reached `Settings.discovery_max_pages_per_query` (default 5). Page depth is
never confused with research coverage: a query whose results have gone
stale stops paginating immediately, rather than continuing on inertia.

## How the batch loop works

`run_adaptive_discovery(db, run, settings=..., adapters=..., raw_response_dir=...)`
loops:

1. **Quota check.** `QuotaTracker.remaining()` is the tightest of three
   constraints: the campaign ledger's `remaining()`, and the subscription's
   own hourly/monthly limits (now also read from the ledger's per-request
   timestamps -- exact, not approximated), minus a **reserved fraction**
   (`ADAPTIVE_RESERVED_QUOTA_FRACTION`, default 15%) held back so retries
   and frontier follow-up queries always have quota available. If what's
   left (after reserve) is exhausted, the run stops with status
   `quota_exhausted` -- not terminal, just paused; it resumes cleanly once
   quota replenishes (a raised ceiling, a new billing period, or simply
   time passing for the hourly window).
2. **Batch execution.** Up to `min(Settings.campaign_batch_size,
   Settings.adaptive_batch_size)` (defaults 75 and 20) pending queries are
   executed via `service.execute_run`, reusing its resume-safe
   stop-at-budget behavior verbatim. Batches excluding any low-yield query
   families (see below) via `execute_run`'s `exclude_families`.
3. **Yield measurement**, both whole-batch and **per query family**
   (`QueryPlan.kind`): before/after snapshots of accepted `SourceCandidate`
   count and queried `CoverageCell` count for the whole batch, plus
   `RunOutcome.new_candidates_by_family` computed directly inside
   `execute_run`'s own loop (not reconstructed after the fact -- alias rows
   use random UUIDs, not sequential ones, so "which alias came first"
   can't be recovered by sorting).
4. **Follow-up queries.** Once the planned query pool (minus excluded
   families) is exhausted, `service.expand_frontier` generates follow-up
   queries from every accepted candidate's own metadata. If expansion
   queues nothing new, the run stops with status `no_more_queries`
   (terminal, `run.status` becomes `completed`).
5. **Per-family allocation.** A family that goes
   `Settings.campaign_family_consecutive_low_yield_batches_to_stop`
   (default 3) consecutive batches without a single new accepted candidate
   is excluded from future batches -- not deleted, just deprioritized; any
   batch that *does* yield resets its streak, and a family can still be
   reached later via frontier expansion.
6. **Whole-campaign saturation.** A batch is "low-yield" if it added at
   most `ADAPTIVE_LOW_YIELD_MAX_NEW_ACCEPTED` (default 0) new accepted
   candidates *and* opened no new coverage cells. Once
   `ADAPTIVE_CONSECUTIVE_LOW_YIELD_BATCHES_TO_STOP` (default 4) consecutive
   batches are low-yield, the run stops with status `saturated` (terminal).
7. **Staged checkpoints.** `Settings.campaign_stage_a/b/c_max_requests`
   (defaults 50 / 1,000 / 3,500) and `campaign_max_requests` itself (stage
   D) are **checkpoints for reporting, not quotas that must be exhausted**.
   The first batch whose cumulative ledger usage crosses a stage boundary
   emits a `StageCheckpoint` (`AdaptiveRunResult.checkpoints`) summarizing
   yield and excluded families so far. Whether to keep spending past a
   stage is still governed entirely by the saturation criteria above, never
   automatic advancement.
8. **Resumability.** Calling `run_adaptive_discovery` again on the same
   `DiscoveryRun` row picks up exactly where it left off -- idempotent
   planning, already-`completed` `QueryPlan` rows never re-executed, and
   the campaign ledger is a database row that survives the restart
   entirely.

`max_batches` is an optional safety cap (used by tests and bounded demos);
production invocations leave it unset and rely on quota/saturation alone.

## Dry-run decision report

`estimate_dry_run(db, run, settings)` (CLI: `discovery adaptive-dry-run`)
reports, entirely offline (no network calls, no quota spent, safe to call
repeatedly): unique queries planned, how many were eliminated as
duplicates of an already-planned query, the allocation by query family,
the stage/campaign ceilings, the worst-case physical request count
including every possible retry, the remaining safety margin below the
ceiling, and every stopping check that will apply.

## CLI

```
discovery adaptive-dry-run                  # offline estimate, no network calls
discovery adaptive-run --execute-network     # start a new adaptive run
discovery adaptive-run --run-id ID --execute-network  # resume an existing one
discovery adaptive-run --max-batches N --execute-network  # bounded demo/test run
```

## Known, deliberate scope limits

- **Per-request yield precision.** `QuotaLedgerEntry.candidates_produced`/
  `unique_relevant_produced` are backfilled at *batch* granularity
  (`quota_ledger.annotate_batch_yield`), not per physical request --
  precise enough to drive family/batch-level allocation decisions, without
  threading a result-count callback through every layer between
  `service.execute_run` and the SerpApi client.
- **Acquisition-side retries.** `acquirer.acquire()` still makes exactly
  one attempt per redirect hop, by design (this is what
  `acquisition_pilot.py`'s/`multi_format_pilot.py`'s documented request
  ceilings assume) -- a single opt-in retry for a landing-page timeout/5xx
  is a reasonable future addition but was not made here, to avoid silently
  changing the worst-case request math those frozen, safety-critical pilot
  modules depend on.
- **Inaccessible-but-high-value routing.** A candidate that repeatedly
  fails automated acquisition today lands in `download_failed` (retryable
  via the existing state machine) rather than being automatically routed
  to the MIT-assisted manual-acquisition queue after N failures -- an
  operator can still route it there manually via
  `mit_assisted_acquisition.py`; automatic routing by predicted yield is a
  scoped-out follow-on.
