# MIT-assisted acquisition for restricted-access resources

## The constraint this workflow exists to satisfy

This pipeline **never stores or requests MIT Kerberos credentials,
passwords, MFA tokens, or browser cookies, and never automates systematic
downloading from licensed publishers.** Any candidate whose `access_status`
is restricted (`licensed_mit_access`, `authentication_required`,
`manual_acquisition_required`, `unavailable` -- see `access_policy.py`'s
`is_restricted_access`) is routed to `paywalled` at screening time and is
never automatically fetched, full stop. `mit_assisted_acquisition.py` is
the only supported path such a candidate can still reach `handed_off`
through, and every step of it keeps a human in the loop for the actual
retrieval.

## The three-step workflow

**1. Export the queue.**

```
discovery acquisition-queue --format csv --output queue.csv
discovery acquisition-queue --format markdown
discovery acquisition-queue --format html --output queue.html
```

Each row: candidate id, title, DOI, publisher, landing URL, a lookup link,
the candidate's `access_status`, and its predicted cost+technical
observation yield (see `screener.py`'s yield-aware ranking) -- highest-yield
restricted candidates sort first, so a researcher's limited manual-retrieval
time goes to the resources most likely to matter.

The lookup link is deliberately conservative: a DOI resolver
(`https://doi.org/{doi}`) when a DOI is known -- MIT's link resolver
intercepts this correctly for MIT users on VPN/Touchstone -- or a
title-based Google Scholar search as a "find this" fallback when there's no
DOI. This pipeline never fabricates an MIT-specific deep link it can't
verify is currently correct.

**2. Retrieve the file, entirely outside this pipeline.**

The researcher uses whatever access route MIT provides them --
[MIT VPN](https://ist.mit.edu/vpn), Touchstone single sign-on, or the
[LibKey Nomad](https://libkey.io/libkey-nomad/) browser extension -- to
open the lookup link or landing URL and download the file themselves. This
pipeline is not involved in that step and has no visibility into how the
researcher authenticated.

The researcher then drops the downloaded file into the manual-acquisition
inbox (`Settings.discovery_manual_inbox_dir`, default
`./data/manual_inbox/`), named `<candidate_id>.<ext>` -- the candidate id is
right there in the exported queue.

**3. Scan the inbox.**

```
discovery scan-inbox
```

`mit_assisted_acquisition.scan_inbox`:
- Matches each inbox file to a candidate by filename (`<candidate_id>.<ext>`
  convention), or via an explicit `mapping` (filename -> candidate_id) for
  filenames that can't carry the id.
- Validates the file extension against the same downloadable-format
  allowlist the rest of the pipeline understands (PDF, XLSX/XLS, CSV/TSV,
  ZIP, DOCX/PPTX, JSON/XML).
- Copies it into `Settings.discovery_acquired_root`, computes its SHA-256,
  and advances the candidate through the *same* state machine an automated
  download uses (`paywalled -> manual_review_required ->
  acquisition_pending -> downloaded`), then runs it through
  `handoff.validate_and_prepare_candidate` -- the identical hash-check and
  dedup-by-content-hash logic an automated download gets, ending at
  `ingestion_ready` and, on the next `discovery handoff`, `handed_off`.
- Records provenance as `access_route="manual_acquisition"` -- traceable,
  but never a credential.

A file that doesn't match any candidate, uses an unrecognized format, or
belongs to a candidate not currently in an inbox-eligible state is reported
back (not silently dropped) and the candidate is left untouched.

## What this workflow deliberately does not do

- It does not open a browser, submit a login form, or hold a session
  cookie.
- It does not attempt EZproxy/Touchstone SSO programmatically.
- It does not guess at an MIT Libraries deep-link URL syntax it can't
  verify.
- It does not "help" by silently matching a plausible-looking filename to
  the wrong candidate -- matching is always either the explicit
  `<candidate_id>.<ext>` convention or an explicit mapping a human wrote,
  never fuzzy title similarity.
