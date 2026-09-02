# Two-document acquisition pilot (bounded, not yet executed)

**Not executed as part of this build.** Proves the acquisition → validation
→ handoff path end-to-end against two real, already-discovered, open-access
candidates from the 2026-08-20 live smoke test, before trusting the same
path at 280-request scale (see the offline audit report,
`data/smoke_test_runs/smoketest_20260820T194944Z_audit/AUDIT_REPORT.md`,
§5-6, for why this is the recommended next step).

## Targets

Exactly two candidates, identified by canonical URL (never a generic
"whatever is `acquisition_pending`" query):

- `28586945` — "2025 Geothermal Drilling Cost Curves Update" —
  `pangea.stanford.edu` (Stanford Geothermal Workshop) — open access.
- `dccc984c` — "2022 GETEM Geothermal Drilling Cost Curve Update" —
  `osti.gov` — open access.

The third accepted candidate from that run (a ScienceDirect paper) is
`paywalled` and is deliberately excluded — `discovery.acquisition_pilot`
refuses to seed or acquire a paywalled candidate at all, checked before any
network code runs.

## Ceilings (enforced in code, `discovery/acquisition_pilot.py`)

| | |
|---|---|
| Max candidates | 2 (`MAX_PILOT_CANDIDATES`) |
| Max total outbound requests, including every redirect | 12 (`MAX_PILOT_TOTAL_REQUESTS`) |
| Max redirects per candidate | 5 (documents `acquirer.py`'s own `_MAX_REDIRECTS`) |
| Retries | 0 — `acquirer.acquire()` has no retry loop at all, with or without a budget |

12 = 2 candidates × (1 initial request + 5 redirects), the exact worst case.
A single `SharedRequestBudget` (`acquirer.py`) is shared across both
candidates' acquisitions; it's consumed once per physical HTTP attempt,
immediately before that attempt, so the 13th request can never occur no
matter how either candidate's redirect chain behaves.

## Isolation

Every invocation is pointed at explicit, isolated paths — never the default
`data/discovery.db`/`data/acquired`/`data/handoff`:

```bash
uv run discovery acquisition-pilot \
  --source-database-url "sqlite:///data/smoke_test_runs/smoketest_20260820T194944Z.db" \
  --pilot-database-url "sqlite:///data/acquisition_pilot_runs/pilot_<timestamp>.db" \
  --download-dir "data/acquisition_pilot_runs/pilot_<timestamp>_downloads" \
  --handoff-dir "data/acquisition_pilot_runs/pilot_<timestamp>_handoff"
```

The source database is only ever read (`seed_pilot_candidates` never writes
to it); the pilot database is fresh and disposable.

## Procedure

1. **Dry run first** (zero network calls, zero filesystem writes — prints
   the exact 2 candidates and all ceilings):
   ```bash
   uv run discovery acquisition-pilot --source-database-url ... --pilot-database-url ... --download-dir ... --handoff-dir ...
   ```
2. **Live run** (spends up to 12 real requests):
   ```bash
   uv run discovery acquisition-pilot ... --execute-network
   ```
3. Inspect the result: the command prints download/validation counts and
   the handoff manifest path; confirm the manifest's `entry_count` and
   self-hash (see `docs/handoff_contract.md` for how to verify
   `manifest_sha256`).

## Explicit authorization required

Per this build's operating constraints, the live (`--execute-network`) run
requires the user's explicit go-ahead. Do not run it as part of an
automated/unattended task.
