# Live smoke test (bounded, up to 5 paid SerpApi requests)

**Not executed as part of this build.** This document and the
`discovery smoke-test` command exist so the operator can run a small,
strictly-bounded live check once `SERPAPI_API_KEY` is configured, before
committing to a larger run -- either the historical ~280-request acceptance
scenario or, for actual production discovery, `discovery adaptive-run` (see
docs/adaptive_discovery.md), which is quota- and coverage-driven rather than
a fixed request count.

## What it does

- Plans **up to 5** queries, hard-capped in code
  (`discovery.service.SMOKE_TEST_REQUEST_CEILING = 5` -- not a CLI flag, not
  a `.env` setting, cannot be raised without editing source).
- Uses **only** `serpapi_google`/`serpapi_scholar` -- OpenAlex, Crossref, and
  seed import are excluded, since they're free/keyless and this command
  exists specifically to bound *paid* request volume.
- Sets both the run's `query_budget` and `request_budget` to the hard-coded
   ceiling of `5`, so even a misconfigured `.env`
   (`DISCOVERY_REQUEST_BUDGET=10000`) cannot push this specific command past 5
   real HTTP calls. The plan can contain fewer queries when fewer eligible rows
   are available.
- Every request still goes through the same caching layer as a normal run
  (`data/serpapi_cache/`) -- an identical repeated smoke test after this one
  would hit the cache, not the API, and cost nothing further.

## Cost estimate

SerpApi's pricing is per-search-request regardless of engine
(`engine=google` or `engine=google_scholar`); 5 requests is a small fraction
of a typical monthly plan's included quota. Check the account's actual plan
tier before running if cost sensitivity matters beyond "a handful of
requests."

## Procedure

1. Confirm `SERPAPI_API_KEY` is set in `.env` (a real key, not the empty
   placeholder from `.env.example`).
2. **Dry run first** (zero network calls, shows which queries would execute,
   up to 5):
   ```bash
   uv run discovery smoke-test
   ```
   Review the printed query list. If any of them look wrong (e.g. a
   template rendering error), stop and fix the config before proceeding.
3. **Live run** (spends up to 5 real requests):
   ```bash
   uv run discovery smoke-test --execute-network
   ```
4. Inspect the result:
   ```bash
   uv run discovery status <run_id>   # run_id printed by the smoke-test command
   ```
   Expect `api_requests_attempted <= 5`, some `unique_candidates`, and
   (depending on the 5 specific queries drawn) possibly some
   `accepted_count`/`manual_review_count`.
5. Inspect the raw cached responses if anything looks off:
   ```bash
   ls data/serpapi_cache/
   ```
   (secrets already stripped from every cached file -- see
   `tests/unit/test_secret_redaction.py`).

## Rollback / cleanup

Nothing here is destructive. The smoke-test run is a normal `discovery_run`
row like any other; if its results aren't wanted, they can simply be
ignored (candidates it created remain in the corpus, same as any other
run's -- deleting them is a manual operator decision, this pipeline never
auto-deletes discovered candidates).

## Explicit authorization required

Per this build's operating constraints, running step 3 above (the
`--execute-network` invocation) requires the user's explicit go-ahead --
it is a paid API call. Do not run it as part of an automated/unattended
task.
