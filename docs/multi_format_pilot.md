# Multi-format acquisition pilot (design; not executed)

Supersedes `acquisition_pilot.py`'s two-PDF pilot as the
production-readiness demonstration. That pilot is **preserved unchanged**
as a regression/safety test for the underlying redirect/budget/SSRF
mechanics (see `docs/acquisition_pilot.md`) -- it is not deleted, reverted,
or redesigned. This pilot is additional, not a replacement of that code.

**Not executed as part of this build.** This document describes the design
and the infrastructure already built for it
(`src/discovery/multi_format_pilot.py`, offline-tested in
`tests/integration/test_multi_format_pilot.py`); it does not name concrete,
currently-live target URLs, because selecting them is an operational
decision for whoever runs the pilot, not something an offline
implementation phase can respons­ibly guess at.

## Why one PDF pilot wasn't enough

The two-PDF pilot proved the acquisition -> validation -> handoff path for
exactly one format and one access route (open, automated download). This
pipeline now discovers a much wider range of research resources -- see
`docs/geocost_format_compatibility.md` -- and has a whole separate
acquisition route (`mit_assisted_acquisition.py`) for restricted resources
that must *never* be automated. A production-readiness demonstration needs
to exercise all of that, not just the one path the original pilot covered.

## The three legs

1. **Open PDF** -- an automated download through `acquirer.acquire()`,
   identical mechanics to the existing pilot.
2. **Open structured-data resource** (XLSX, CSV, or ZIP -- typically from a
   GDR-style dataset repository) -- also an automated download through
   `acquirer.acquire()`. This proves the acquisition path handles the
   "research resource, not just document" formats this pipeline now
   discovers, not just PDFs.
3. **A restricted resource** (licensed_mit_access, authentication_required,
   or manual_acquisition_required) -- the *negative* leg. This pilot never
   calls `acquire()` on it. Instead it verifies the candidate is routed to
   `paywalled` and appears in `mit_assisted_acquisition.build_acquisition_queue`
   -- proving the "never automate a licensed download" constraint holds at
   the exact moment a real, would-be-restricted resource reaches the
   acquisition stage, not just in isolated unit tests of the access-policy
   logic alone.

## Selection criteria for real targets, when this pilot is actually run

- **Open PDF**: any already-discovered, `acquisition_pending`,
  `open_access` candidate -- the existing pilot's Stanford/OSTI candidates
  would work unchanged for this leg if still current.
- **Open dataset**: an `acquisition_pending`, `open_access` candidate whose
  `file_format` is `xlsx`, `csv`, or `zip` -- the structured-data query
  templates added in this phase (`config/query_templates.yaml`'s
  "Structured/tabular data discovery" section) are designed to surface
  exactly this kind of candidate from GDR/OSTI/NREL.
- **Licensed resource**: any candidate already routed to `paywalled` with a
  restricted `access_status` -- an OnePetro/SPE paper is the natural
  choice, matching the domain this pipeline's `access_policy.yaml` already
  flags as `licensed_mit_access`.

## Safety properties (enforced in code, `multi_format_pilot.py`)

| | |
|---|---|
| Max automated candidates | 2 (`MAX_AUTOMATED_CANDIDATES`) -- the licensed leg is never counted here since it's never downloaded |
| Max total outbound requests (automated legs only) | 12 (`MAX_TOTAL_REQUESTS`) = 2 x (1 initial + 5 redirects), same worst-case formula as the PDF pilot |
| Retries | 0 -- `acquirer.acquire()` has no retry loop, same as the PDF pilot |
| Licensed leg | `acquire()` is never called on it -- `seed_multi_format_pilot`/`run_multi_format_pilot` both raise `MultiFormatPilotError` before any acquisition code runs if the licensed candidate isn't already `paywalled` and restricted-access |

## Procedure, when actually run

```bash
# Dry run first -- zero network calls, prints the three targets and ceilings.
uv run discovery multi-format-pilot \
  --source-database-url "sqlite:///..." \
  --pilot-database-url "sqlite:///data/multi_format_pilot_runs/pilot_<timestamp>.db" \
  --open-pdf-url "<open PDF candidate canonical_url>" \
  --open-dataset-url "<open dataset candidate canonical_url>" \
  --licensed-url "<licensed candidate canonical_url>" \
  --download-dir "data/multi_format_pilot_runs/pilot_<timestamp>_downloads"

# Live run (spends up to 12 real requests, for the 2 automated legs only).
uv run discovery multi-format-pilot ... --execute-network
```

The command reports, per leg: the two automated legs' download/hash
outcomes, and the licensed leg's `download_attempted` (must be `False`) and
`correctly_queued` (must be `True`) flags.

## Explicit authorization required

Per this build's operating constraints, the live (`--execute-network`) run
requires the user's explicit go-ahead, same as the original pilot. Do not
run it as part of an automated/unattended task.
