# Current Status

**Implementation baseline:** see `git log -1` / `git status` for the
authoritative current commit and push state — this file is updated
same-session and may momentarily lag a push. As of this update: two
commits ahead of the original `e93ff0b` baseline (the 500-request campaign
commit, plus a URL-fetch-fix follow-up commit), pushed to `origin/main`
per this task's explicit authorization.
**Last updated:** 2026-08-24 (URL-fetch fix, NREL reattempts, corrected
content-quality tiers, isolated geocost preflight)

---

**NOTE (2026-08-25, isolated branch `fix/threshold-boundary-anomaly`,
worktree `C:/dpwt_f27edd3`, not merged into `main`):** this copy of the
file, in the isolated worktree only, additionally records the
float-boundary threshold fix (commit `e7aeee1` on top of `f27edd3`) and a
full offline re-score of all 4,209 candidates. **The `main`-worktree copy
of this file was deliberately left untouched** (a concurrent session was
active there at the time) — merge this section in when the branch is
eventually merged.

## Float-boundary threshold fix and full re-score (2026-08-25)

Root cause of the 20-candidate 0.50–0.55-boundary anomaly (composite_score
stored as exactly 0.55 but decision=`manual_review`, flagged unresolved by
the prior backlog-conversion task): `RulesScreener.screen()` compared the
*unrounded* six-term weighted sum against `accept_threshold`, then
separately rounded only the *stored* `composite_score`. IEEE-754 float
summation of six two-decimal weights isn't exact — empirically confirmed
one real case sums to `0.5499999999999998`. Fixed by rounding once, before
the comparison, so decision and stored value can never disagree (not an
epsilon/tolerance hack — 4 decimal places is the score's own already-
persisted precision). 9 new regression tests; full suite 341 passed
(332 baseline + 9), ruff/mypy clean.

Applying the fix to all 4,209 candidates' real, already-persisted
component scores (no network calls, no re-fetching): **exactly 20 promote
from `manual_review` to `accept`** (116 → 136 accepted), matching the
anomaly count exactly. Full outputs — re-scored pool, evaluation dataset
(37 real content-assessed documents via gcpf1/gcpf2, 87.9% high-or-medium
precision among acquired/content-verified candidates; explicit
selection-bias caveat, not a random sample), 6 action queues, and a
versioned manual-access-queue replacement (73 candidates, reconciling the
prior 60, not overwriting it) — are all in
`data/pilots/rescoring_20260825T1630Z/`.

**Independent cross-check**: re-deriving the "0.50–0.55 promotion cohort"
size from the fixed comparison gives **120**, matching the concurrent
session's independently-built cohort exactly.

**Threshold recommendation**: real content-verified precision by score
band (0.60+: 90.0%, 0.55–0.60: 84.2%, 0.50–0.55: 100.0% but n=4) supports
lowering `accept_threshold` to 0.50, corroborating the concurrent
session's same conclusion from a different angle — but the 0.50–0.55
band's sample size is small; treat as directionally strong, not
conclusive. Not applied to production config — a recommendation only.

## URL-fetch defect fix and geocost preflight (2026-08-24, second commit)

Root cause confirmed in production code: `SourceCandidate.canonical_url`
(dedup-normalized — `normalize_url()` unconditionally strips `www.`) was
also the URL every acquisition entry point (`cli.py acquire`,
`acquisition_pilot.py`, `multi_format_pilot.py`, `open_resource_pilot.py`)
fetched directly. Fixed by adding `direct_download_url` (raw,
as-discovered URL, already an unused DB column) alongside
`canonical_url`; every acquisition call site now fetches
`direct_download_url or canonical_url`. `canonical_url`'s dedup behavior
is completely unchanged — 6 new regression tests confirm both the fetch
fix and no dedup regression. 330 tests passed, ruff/mypy clean.

**Important correction**: reattempting the 2 failed NREL candidates with
their corrected `www.`-prefixed URL **still failed identically** — direct
DNS testing confirmed `nrel.gov` (in any form) does not resolve from this
environment at all, while general internet access and other government
domains (osti.gov) resolve fine. The original "www-stripping caused the
NREL failures" diagnosis was **wrong**; the fix itself is still correct
and independently justified, but it did not explain those two specific
failures, which remain an external, unresolved network/DNS issue.

**Content-quality corrections**: manual re-verification (Step 3 of this
task) found one of the original "reject" (zero-extractable-text)
documents was actually a real, substantial 23-page document — the first
scan's `pdfplumber` call had a parsing bug on that one file; PyMuPDF
extracted it cleanly. Corrected totals: **16 high, 20 medium, 3 low, 1
`ocr_required_pending_relevance_assessment`** (only one document is
genuinely scanned; visually confirmed to be clearly legible, likely high
value once OCR'd — geocost has a native, automatic OCR path via Docling
`do_ocr=True`, no new component needed, not invoked here).

Of the 16 high-value documents, **3 are HTML** (landing/database pages) —
geocost's parser does not support HTML at all, so only **13 are
genuinely ingestible**. An isolated geocost preflight
(`data/pilots/gcpf1/`: isolated DB, corpus, and source manifest — geocost's
production DB and manifest untouched) ran the real Docling ingest +
deterministic candidate-detection scanner against these 13 plus the 3
comparison PDFs (Rickard/Lowry/Baumgartner): **exact** totals — 1,422
native chunks, 331 candidate-bearing (selected for OpenAI) chunks across
all 16 documents (243 for the 13 new, 88 for the 3 comparison). See
`data/pilots/gcpf1/PREFLIGHT_REPORT.md` for the full per-document table
and the designed (not executed) 3-stage extraction plan.

**No OpenAI or SerpApi call occurred. No paid OCR was run.**

## Bounded acquisition of the open-access shortlist (2026-08-24)

Following the campaign below, its 47 open-access candidates were ranked,
5 excluded (1 duplicate-by-URL-form, 2 generic navigation pages, 1 likely
duplicate of an existing geocost source, 1 malformed URL), and the
remaining 42 acquired in two stages using only the native
`discovery.acquirer` functions (no bespoke acquisition logic) — see
`data/pilots/acq47_20260824T0000Z/FINAL_REPORT.md`.

- **40 of 42 succeeded** (2 failed: both `nrel.gov` — see the DNS finding
  above; not actually a `www.`-stripping artifact as first believed).
  Zero retries, zero SerpApi/OpenAI calls, zero hash duplicates, zero
  overlap with geocost's existing corpus or the prior 3-PDF pilot.
- All 40 validated and an isolated handoff manifest built (real
  `validate_and_prepare_candidate`/`build_manifest` functions, isolated
  `handoff_dir` — not added to `data/handoff/` or geocost's manifest).
- **Content-quality assessment, corrected above: 16 high, 20 medium, 3
  low, 1 OCR-required-pending-assessment** (originally misreported as 15
  high / 2 reject).
- **No extraction, no SerpApi/OpenAI call, no geocost modification.**

## 500-request discovery campaign (2026-08-23, committed as `4771a9f`)

Authorized: analyze the prior 100-request trial offline, improve the native
pipeline where evidence justified it, then run up to 500 additional live
SerpApi requests in staged batches with adaptive stopping.

- **Code/config changes (uncommitted):** `include_families` allow-list
  added to `execute_run`/`run_adaptive_discovery`/CLI (`--include-families`)
  for reliable per-family staged execution through the real orchestrator
  (no more bespoke scripts); two previously-unused coverage dimensions
  (`cost_representation`, `evidence_type`) wired into new query templates;
  `cost_driver`'s query wording fixed (was missing any domain-topic word,
  the audit-identified cause of its zero-strict-accept result) plus a new
  structured-data variant; a real Crossref `TypeError` bug fixed
  (`"date-parts": [[None]]`). 6 new tests added. `pytest`: 324 passed.
  `ruff`/`mypy`: clean.
- **Requests:** ledger **107 → 595** (488 of 500 authorized used, 12
  unspent by deliberate choice, not a forced stop). **0 retries** against
  SerpApi itself for the entire campaign (one retry burst was OpenAlex
  rate-limiting, a free non-metered adapter, confirmed via `search_execution`
  rows — SerpApi had zero errors throughout).
- **Candidates:** 588 → 4,209 total (+3,621 new). Accepted (open path):
  31 → 103. Accepted (restricted/paywalled): 3 → 13. Rejected: 22 → 72.
  Manual review: 532 → 4,021.
- **Best next-step candidates:** 47 `acquisition_pending` + `open_access`
  candidates ranked in the final report — not acquired or verified in this
  task (that remains a separate, explicitly-approvable step, same pattern
  as the 3-PDF pilot).
- **No OpenAI call. No geocost modification. No git push.**

## What's built

- Discovery/normalize/dedup/screen/acquire pipeline (`service.py`,
  `screener.py`, `acquirer.py`) with SSRF-guarded, magic-byte-validated
  downloads, pre-network Windows path-safety validation, and structured
  (never-unhandled) finalization-failure handling.
- Adaptive production controller (`adaptive_controller.py`) and durable
  campaign quota ledger (`quota_ledger.py`, 5,000-request default ceiling) —
  see "SerpApi discovery trial" below for the one bounded trial run to date.
- Research-resource model, MIT-assisted manual acquisition workflow for
  restricted resources, self-hashed versioned handoff manifest to geocost
  (geocost itself untouched throughout).
- Three acquisition pilots: a frozen 2-PDF regression pilot
  (`acquisition_pilot.py`) and a multi-format pilot (`multi_format_pilot.py`)
  remain designed and offline-tested only, **never executed**. The
  **three-open-resource pilot** (`open_resource_pilot.py`, CLI: `discovery
  open-resource-pilot`) has now been executed twice — see "Acquisition
  canaries" below.

## SerpApi discovery trial (`serpapi_learning_trial_200_20260821T223105Z`)

- **100 of the 200 authorized requests executed** (40 initial + 60
  continuation). Durable production ledger (campaign `production`):
  **107 / 5,000** reserved — the 100 trial requests plus 7 pre-existing from
  earlier bounded work. **The remaining 100 of the 200-request trial
  authorization are unused**, and no decision to spend them has been made.
- Produced **588 unique candidates**: 34 accepted, 532 sent to manual
  review, 22 rejected.

## Offline manual-review audit

- Of the 532 manual-review candidates, **98** were flagged high-value by the
  trial's own broader classification. A full offline metadata-only audit
  (no URLs opened) judged **69 of the 98 clearly or probably relevant**.
- **Access-status correction**: of the 50 relevant, non-restricted
  candidates from that audit, only **11 carried affirmative open-access
  evidence** (explicit `access_status="open_access"`, not merely
  unclassified/`None`) — target selection for both acquisition canaries
  below drew only from this set of 11.

## Acquisition canaries

**First canary (2026-08-22) — failed at finalization, not acquisition.**
One direct HTTP request was made (Target A, Rickard/FORGE MSE); the file
downloaded and validated successfully (magic bytes, correct content-type),
but finalizing it to disk failed because the destination path exceeded
Windows' 260-character `MAX_PATH`. Targets B and C were never attempted.
The failed run's directory, its orphan temporary PDF, its `pilot_run.db`,
and its empty log are **preserved unchanged** as forensic evidence at
`data/serpapi_learning_trials/serpapi_learning_trial_200_20260821T223105Z/open_resource_canary_live_20260822T230846Z/`.

**Crash-resilience correction — committed `e93ff0b`.** Pre-network
Windows path-safety validation (computed against the resolved absolute
path); structured `file_finalization_failed`/`output_path_too_long`
outcomes instead of unhandled exceptions; the pilot never raises for a
candidate-level failure, so mandatory artifacts are always produced after
seeding succeeds; a configurable, bounded direct-request budget
(`--max-direct-requests`, 1–10).

**Replacement canary (2026-08-23) — succeeded, all three legs.** Run
directory: `data/pilots/pdf3_r2_20260823T202843Z/`. Exact three documents
acquired and finalized:

1. Rickard — *Mechanical Specific Energy Analysis of the FORGE Utah Well*
2. Lowry — *Economic Valuation of Directional Wells for EGS Heat Extraction*
3. Baumgartner — *Soultz-sous-Forêts: Main Technical Aspects of Deepening*

All three: PDF signature validated, SHA-256 recorded, `screening_status`
`handed_off`, and **all three handoff-manifest entries are
`ready_for_ingestion: true`**.

**Request accounting**: replacement run used **3** direct requests (0
redirects, 0 restricted-publisher requests). **Cumulative across both
canaries: 4.** Automated retries: **0**. Target A's request in the
replacement run is an **explicitly authorized reattempt** of the
first canary's unfinalized download, not an automatic retry — always
reported as such, never folded silently into "zero retries."

**This validated open-PDF acquisition only.** No extraction has been run
and no observation yield has been measured — the `predicted_*_observation_yield`
figures anywhere in this pipeline remain metadata-derived estimates, not
confirmed observations. XLSX support remains offline-tested only (never
live-acquired). HTML ingestion remains unsupported on geocost's side (see
[docs/geocost_format_compatibility.md](geocost_format_compatibility.md)).

## Verification (as of `e93ff0b`, committed)

- `pytest`: **318 passed**, 0 failed
- `ruff check .`: clean
- `mypy src`: clean

## Verification (URL-fetch fix commit)

- `pytest`: **330 passed**, 0 failed
- `ruff check .`: clean
- `mypy src`: clean

## Requires explicit approval before proceeding

- Extraction (OpenAI/API calls) on any acquired document (the original 3
  PDFs, the 40 from the 2026-08-24 acquisition, or the isolated geocost
  preflight's 331 selected chunks — see `data/pilots/gcpf1/PREFLIGHT_REPORT.md`
  for the designed-but-not-executed 3-stage plan).
- Any ingestion into geocost's *production* database or manifest (the
  isolated `data/pilots/gcpf1/` preflight is separate and was never
  connected to production).
- Spending a further SerpApi tranche beyond this campaign's 595/5000, or
  starting a full production `adaptive-run` campaign — neither is approved.
- Paid OCR, or adding any new OCR component, for the one
  `ocr_required_pending_relevance_assessment` document — geocost's
  existing native OCR path (Docling `do_ocr=True`) was identified but not
  invoked.
- Further reattempts on the 2 NREL candidates — both explicitly-authorized
  reattempts failed identically (a real, external DNS issue unrelated to
  this repo's code); stopped per instruction, not retried further.
- Any further live network call, live pilot run, or acquisition beyond what
  has already run.

## Next task

Four independent preflights/pilots are now done, none extended further
than described:

1. The three original acquired PDFs' offline preflight (source identity,
   hashes, duplicate status, schema compatibility, expected OpenAI/API
   cost, output isolation, extraction acceptance conditions) is **done** —
   no paid extraction has been run.
2. The 500-request campaign's 47-candidate open-access shortlist has been
   **ranked, acquired (40 of 42), verified, and quality-assessed** (16
   high / 20 medium / 3 low / 1 OCR-required, corrected) — see
   "Bounded acquisition of the open-access shortlist" above and its
   `FINAL_REPORT.md`.
3. The URL-fetch defect is fixed and tested; the 2 failed NREL candidates
   were reattempted once each per explicit authorization and both failed
   identically (external DNS issue, not this repo's code) — stopped, not
   retried further.
4. An isolated geocost preflight (13 new ingestible high-value + 3
   comparison documents) computed **exact** native/selected chunk counts
   (1,422 / 331) via geocost's real ingest + candidate-detection pipeline,
   and designed a 3-stage extraction plan (76 / 167 / 88 requests) with
   acceptance and stopping conditions — see
   `data/pilots/gcpf1/PREFLIGHT_REPORT.md`. **Not executed.** The next
   decision is whether to authorize Stage A (76 OpenAI requests, ≈$1–$2)
   as a bounded paid-extraction canary.
