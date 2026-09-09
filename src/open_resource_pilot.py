"""A three-open-resource acquisition pilot: proves the acquisition path for
independently-declared-format resources (HTML news/report pages, PDFs, XLSX
spreadsheets, or a resource whose exact format is only known after a live
response), without the multi-format pilot's mandatory third restricted leg.

Not a generalization of `multi_format_pilot.py` -- a distinct, smaller pilot
for a distinct shape of target set: three resources this pipeline expects to
all be open, each with its own declared expected format, none of them
required to be paywalled. `acquisition_pilot.py` and `multi_format_pilot.py`
are both preserved unchanged; this module is additive, following the same
precedent that added `multi_format_pilot.py` alongside `acquisition_pilot.py`
rather than editing it.

Safety properties, enforced here (not just documented):

  - exactly `MAX_CANDIDATES` (3) targets, each identified by canonical URL,
    never a generic "whatever is acquisition_pending" query;
  - each target carries a declared `expected_format` ("html", "pdf",
    "html_or_pdf", or "xlsx") that `acquirer.acquire()`'s `allowed_extensions`
    parameter enforces -- a response in an unexpected format is rejected
    before any byte reaches disk, never silently accepted as if it were the
    intended file (see acquirer.py's `declared_format_mismatch`). XLSX reuses
    `acquirer.py`'s existing, format-agnostic content-type/magic-byte/URL-
    extension/Content-Disposition validation unchanged -- no second XLSX
    validator exists in this module;
  - any outcome other than `succeeded` -- a declared-format mismatch, a
    magic-byte/HTML-signature mismatch, an unsupported content type, a
    size-limit failure, an SSRF/host-policy block, redirect-limit or
    shared-budget exhaustion, an authentication/restricted redirect, or any
    other HTTP/acquisition failure -- stops the pilot immediately: no
    `acquire()` call is ever made for a later candidate once one has
    occurred (see `run_open_resource_pilot`'s fail-fast loop);
  - if seeding discovers any target actually resolves to a restricted
    access_status (contrary to the "all three open" expectation), the
    automated pilot never starts at all: the restricted candidate(s) are
    persisted as `paywalled` (so `mit_assisted_acquisition.build_acquisition_
    queue` picks them up) and `seed_open_resource_pilot` raises
    `RestrictedCandidateDetectedError` before any acquisition code runs;
  - a single `SharedRequestBudget(MAX_DIRECT_REQUESTS)` (10) shared across
    all three candidates' acquisitions caps total physical outbound requests
    (including every redirect hop) -- a hard, mechanically-enforced stop
    before the 11th request, not an operator-monitored figure;
  - every run (whether it completes, stops on a format mismatch, or never
    starts because of a restricted candidate) produces a full, durable audit
    record -- not just an in-memory result -- via `write_audit_report`.
"""

import json
import logging
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from discovery.access_policy import is_automatable_access, is_restricted_access
from discovery.acquirer import (
    AcquisitionOutcome,
    SharedRequestBudget,
    acquire,
    persist_acquisition_attempt,
)
from discovery.db.models import SourceCandidate
from discovery.mit_assisted_acquisition import QueueEntry

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 3
MAX_REDIRECTS_PER_CANDIDATE = 5
# The structural upper bound if every candidate used its full redirect
# allowance (3 candidates x (1 initial + 5 redirects) = 18) -- kept only as a
# documented reference figure. It was previously (incorrectly) used as the
# actual `SharedRequestBudget` ceiling passed to `run_open_resource_pilot`,
# which meant the pilot could legitimately spend up to 18 physical requests
# even when an authorization for a specific run capped it lower (e.g. 10) --
# operator monitoring of `requests_used` was the only enforcement of that
# lower number, which is not a hard stop. `MAX_DIRECT_REQUESTS` below is now
# the actual enforced ceiling; this figure is no longer used to construct the
# budget.
_STRUCTURAL_MAX_REQUESTS_IF_UNBOUNDED = MAX_CANDIDATES * (1 + MAX_REDIRECTS_PER_CANDIDATE)  # 18

# The pilot's actual hard ceiling on total physical outbound HTTP requests --
# the initial request plus every redirect hop plus any other network
# operation made for any candidate, summed across all `MAX_CANDIDATES`
# candidates. Enforced by `SharedRequestBudget.consume()`, which raises
# *before* a request is issued once `used >= max_requests` (see budget.py) --
# so the 11th physical request is refused before it is ever sent, not
# detected after the fact. This is deliberately lower than the structural
# maximum above: it is a chosen authorization ceiling for this pilot, not a
# theoretical bound, and it is mechanical (a code-enforced budget object),
# not a documentation/logging/operator-monitoring convention.
MAX_DIRECT_REQUESTS = 10

ExpectedFormat = Literal["html", "pdf", "html_or_pdf", "xlsx"]

_FORMAT_TO_EXTENSIONS: dict[ExpectedFormat, frozenset[str]] = {
    "html": frozenset({"html"}),
    "pdf": frozenset({"pdf"}),
    "html_or_pdf": frozenset({"html", "pdf"}),
    # Reuses acquirer.acquire()'s existing, already-generic XLSX support
    # unchanged: content-type allowlist (application/vnd.openxmlformats-
    # officedocument.spreadsheetml.sheet -> "xlsx"), the "PK\x03\x04" zip
    # magic-byte signature check, the URL-extension cross-check, and the
    # Content-Disposition-filename cross-check are all format-agnostic code
    # in acquirer.py already exercised by "pdf"/"html" -- nothing new is
    # added here or there. See docs/geocost_format_compatibility.md: XLSX is
    # fully geocost-compatible today (openpyxl importer), unlike HTML.
    "xlsx": frozenset({"xlsx"}),
}

# Formats geocost can actually ingest today, per
# docs/geocost_format_compatibility.md's compatibility matrix: PDF (Docling)
# and XLSX (openpyxl) are fully compatible; HTML has no scraping/ingestion
# path on geocost's side yet. This is informational only -- it does not gate
# seeding or acquisition (this pilot still supports declaring "html", proving
# the acquisition path for a format geocost can't ingest *yet* is still a
# legitimate use of this module, per its own docstring). A caller assembling
# a target set for a pilot that specifically requires handoff-readiness
# should filter targets with this function before selecting them.
_HANDOFF_COMPATIBLE_FORMATS: frozenset[str] = frozenset({"pdf", "xlsx"})


def is_handoff_compatible_format(expected_format: ExpectedFormat) -> bool:
    """True if a resource declared as `expected_format` has a working
    geocost ingestion path today. `html_or_pdf` is deliberately treated as
    not-statically-compatible -- its actual observed format is only known at
    runtime (check the resulting `CandidateAuditRecord.observed_extension`
    for that case instead of this function).
    """
    return expected_format in _HANDOFF_COMPATIBLE_FORMATS


class OpenResourcePilotError(ValueError):
    """Raised when the pilot's strict candidate/target constraints aren't met."""


class RestrictedCandidateDetectedError(OpenResourcePilotError):
    """Raised by `seed_open_resource_pilot` when a target expected to be open
    actually resolves to a restricted access_status. Carries the ids of the
    candidate(s) that were queued for manual acquisition instead -- the
    caller (the CLI command) uses these to confirm the queue entry and to
    record `stop_reason="restricted_candidate_detected"` in the audit report.
    """

    def __init__(self, message: str, *, queued_candidate_ids: list[str]):
        super().__init__(message)
        self.queued_candidate_ids = queued_candidate_ids


@dataclass(frozen=True)
class PilotTarget:
    """One of the three targets, as declared by the caller."""

    label: str
    canonical_url: str
    expected_format: ExpectedFormat


@dataclass
class CandidateAuditRecord:
    """Everything the handoff/audit trail must record for one target,
    whether it was fully acquired, rejected for a format mismatch, skipped
    because an earlier candidate stopped the pilot, or never attempted
    because it was restricted.
    """

    label: str
    candidate_id: str
    normalized_title: str | None
    organization: str | None
    publisher: str | None
    publication_year: int | None
    original_url: str
    resolved_url: str | None
    expected_format: str
    observed_extension: str | None
    content_type: str | None
    access_status: str | None
    screening_status_before: str
    bytes_downloaded: int | None
    sha256: str | None
    predicted_cost_observation_yield: int | None
    predicted_technical_observation_yield: int | None
    outcome_status: str  # succeeded | failed | blocked_by_safety_policy | not_attempted
    error_type: str | None
    stop_reason: str | None


@dataclass
class OpenResourcePilotResult:
    candidate_records: list[CandidateAuditRecord]
    stopped_early: bool
    stop_reason: str | None
    restricted_detected: bool
    restricted_candidate_ids: list[str]
    requests_used: int
    # The actual ceiling this specific run enforced -- MAX_DIRECT_REQUESTS
    # by default, or a caller-lowered value (see run_open_resource_pilot's
    # max_direct_requests parameter) for a continuation/replacement run
    # after a prior run already consumed part of the same conceptual
    # cross-run budget. write_audit_report reports this, not the constant,
    # so the durable audit record always reflects what was actually
    # authorized for this run, not the pilot's absolute maximum.
    max_direct_requests: int = MAX_DIRECT_REQUESTS


def _copy_candidate(source: SourceCandidate, *, screening_status: str) -> SourceCandidate:
    return SourceCandidate(
        canonical_url=source.canonical_url,
        doi=source.doi,
        normalized_title=source.normalized_title,
        authors_json=source.authors_json,
        organization=source.organization,
        publication_year=source.publication_year,
        source_type=source.source_type,
        evidence_tier=source.evidence_tier,
        technology_domains_json=source.technology_domains_json,
        jurisdiction=source.jurisdiction,
        language=source.language,
        access_status=source.access_status,
        screening_status=screening_status,
        expected_evidence_categories_json=source.expected_evidence_categories_json,
        discovery_occurrence_count=source.discovery_occurrence_count,
        best_result_rank=source.best_result_rank,
        candidate_key_fingerprint=source.candidate_key_fingerprint,
        file_format=source.file_format,
        direct_download_url=source.direct_download_url,
        publisher=source.publisher,
        expected_cost_observation_yield=source.expected_cost_observation_yield,
        expected_technical_observation_yield=source.expected_technical_observation_yield,
    )


def seed_open_resource_pilot(
    source_db: Session,
    pilot_db: Session,
    *,
    targets: list[PilotTarget],
) -> list[tuple[SourceCandidate, PilotTarget]]:
    """Copy exactly `MAX_CANDIDATES` named targets from `source_db` into a
    fresh, isolated `pilot_db`.

    Refuses (raises, seeding as little as possible) if: the wrong number of
    targets is given, any target's URL isn't found in `source_db`, or a
    target's `access_status`/`screening_status` combination is anything
    other than one of the two recognized, self-consistent shapes:

      - **automatable** (`access_status` is `open_access` or unclassified)
        *and* `screening_status == "acquisition_pending"` exactly -- seeded
        as an open leg;
      - **restricted** (`access_status` is one of `access_policy.py`'s five
        restricted states) -- queued as `paywalled` regardless of whatever
        `screening_status` it already had, never auto-acquired.

    Any other combination -- e.g. a restricted-looking `screening_status`
    ("paywalled") paired with a non-restricted `access_status` ("open_access"),
    an unrecognized/invalid `access_status` string, or an automatable
    `access_status` paired with any `screening_status` other than
    `acquisition_pending` -- is a data inconsistency this function refuses to
    silently resolve. It is never "fixed" by converting the candidate to
    `acquisition_pending`; `seed_open_resource_pilot` raises instead.

    In the restricted case, the candidate(s) *are* persisted (as `paywalled`,
    so the manual-acquisition queue sees them) but nothing else is seeded and
    `RestrictedCandidateDetectedError` is raised before any acquisition code
    can run -- "never auto-acquire; queue it; stop the automated pilot".
    """
    if len(targets) != MAX_CANDIDATES:
        raise OpenResourcePilotError(
            f"Exactly {MAX_CANDIDATES} targets are required, got {len(targets)}"
        )

    open_candidates: list[tuple[SourceCandidate, PilotTarget]] = []
    restricted_candidates: list[tuple[SourceCandidate, PilotTarget]] = []

    for target in targets:
        source_candidate = (
            source_db.query(SourceCandidate)
            .filter(SourceCandidate.canonical_url == target.canonical_url)
            .first()
        )
        if source_candidate is None:
            raise OpenResourcePilotError(
                f"No candidate found in source database for target {target.label!r} "
                f"({target.canonical_url!r})"
            )

        restricted_access = is_restricted_access(source_candidate.access_status)
        automatable_access = is_automatable_access(source_candidate.access_status)

        if restricted_access:
            restricted_candidates.append((source_candidate, target))
        elif automatable_access:
            if source_candidate.screening_status != "acquisition_pending":
                raise OpenResourcePilotError(
                    f"Target {target.label!r} ({target.canonical_url!r}) has a "
                    f"contradictory state: access_status={source_candidate.access_status!r} "
                    f"is automatable but screening_status="
                    f"{source_candidate.screening_status!r} is not 'acquisition_pending' "
                    f"-- refusing to silently convert it"
                )
            open_candidates.append((source_candidate, target))
        else:
            raise OpenResourcePilotError(
                f"Target {target.label!r} ({target.canonical_url!r}) has an "
                f"unrecognized access_status {source_candidate.access_status!r} -- "
                f"refusing to classify it as either automatable or restricted"
            )

    if restricted_candidates:
        queued_ids: list[str] = []
        for sc, _target in restricted_candidates:
            queued = _copy_candidate(sc, screening_status="paywalled")
            pilot_db.add(queued)
            pilot_db.flush()
            queued_ids.append(queued.id)
        pilot_db.commit()
        labels = ", ".join(target.label for _sc, target in restricted_candidates)
        raise RestrictedCandidateDetectedError(
            f"Target(s) {labels} resolved to a restricted access_status -- "
            f"refusing to seed the automated pilot. Queued for manual "
            f"acquisition instead; the automated pilot did not start.",
            queued_candidate_ids=queued_ids,
        )

    seeded: list[tuple[SourceCandidate, PilotTarget]] = []
    for sc, target in open_candidates:
        copied = _copy_candidate(sc, screening_status="acquisition_pending")
        pilot_db.add(copied)
        seeded.append((copied, target))
    pilot_db.commit()

    if len(seeded) != MAX_CANDIDATES:
        raise OpenResourcePilotError(
            f"Expected exactly {MAX_CANDIDATES} seeded candidates, got {len(seeded)}"
        )
    return seeded


def run_open_resource_pilot(
    pilot_db: Session,
    seeded: list[tuple[SourceCandidate, PilotTarget]],
    *,
    client,
    dest_dir: Path,
    max_bytes: int,
    timeout_seconds: float,
    resolver=socket.getaddrinfo,
    max_direct_requests: int = MAX_DIRECT_REQUESTS,
) -> OpenResourcePilotResult:
    """Acquire each seeded candidate in order, under one shared
    `SharedRequestBudget(max_direct_requests)` -- a single fresh budget
    object constructed here, before any candidate is touched, so every
    physical request this call makes (across all candidates) is accounted
    for from zero. `SharedRequestBudget.consume()` raises before the request
    that would be the `(max_direct_requests + 1)`th is ever issued (see
    `budget.py`) -- accurate accounting is guaranteed structurally (the
    budget is consumed immediately before each physical hop, inside
    `acquirer.acquire()`, never after), so there is no code path in this
    function that can send a network request the budget hasn't already
    accounted for.

    `max_direct_requests` defaults to the pilot's own ceiling
    (`MAX_DIRECT_REQUESTS`, 10) and can be *lowered* for a specific
    invocation -- e.g. a continuation/replacement run after a prior run
    already consumed part of the same conceptual budget across separate
    processes (this function itself has no cross-process memory; a caller
    reducing this value is how that gets reflected) -- but never raised
    above it: raising `OpenResourcePilotError` for any value outside
    `1..MAX_DIRECT_REQUESTS` is a fail-closed guard against a caller
    accidentally (or otherwise) authorizing more direct requests than this
    pilot is ever allowed to make.

    Any outcome other than `succeeded` -- a declared-format mismatch, a
    magic-byte/HTML-signature mismatch, an unsupported content type, a
    size-limit failure, an SSRF/host-policy block, redirect-limit or
    shared-budget exhaustion, an authentication/restricted redirect (401/403),
    a file-finalization failure, an unexpected exception during acquisition,
    or any other HTTP/acquisition failure -- stops the loop immediately: no
    `acquire()` call -- and so no physical HTTP request -- is ever made for a
    later candidate. Every candidate, attempted or not, gets a
    `CandidateAuditRecord`; a skipped candidate is recorded with
    `outcome_status="not_attempted"` so the audit trail accounts for all
    `MAX_CANDIDATES` targets regardless of where the pilot stopped.

    Never raises for a candidate-level or acquisition-level failure -- any
    exception `acquire()` itself doesn't already turn into a structured
    `AcquisitionOutcome` is caught here, logged, and converted into one
    (`error_type="unexpected_acquisition_exception"`) so this function
    always returns a normal `OpenResourcePilotResult`, never an unhandled
    traceback -- the caller (the CLI command) can then unconditionally reach
    its own artifact-writing code (audit report, queue report, handoff
    manifest) regardless of how a candidate failed.
    """
    if len(seeded) != MAX_CANDIDATES:
        raise OpenResourcePilotError(
            f"Pilot requires exactly {MAX_CANDIDATES} candidates, got {len(seeded)}"
        )
    if not (1 <= max_direct_requests <= MAX_DIRECT_REQUESTS):
        raise OpenResourcePilotError(
            f"max_direct_requests must be between 1 and {MAX_DIRECT_REQUESTS} "
            f"(the pilot's own ceiling), got {max_direct_requests}"
        )
    for candidate, target in seeded:
        if not is_automatable_access(candidate.access_status):
            raise OpenResourcePilotError(
                f"Refusing to auto-acquire a non-automatable-access candidate: "
                f"{target.label!r} ({candidate.canonical_url!r})"
            )
        if candidate.screening_status != "acquisition_pending":
            raise OpenResourcePilotError(
                f"Refusing to auto-acquire a candidate not in acquisition_pending: "
                f"{target.label!r} ({candidate.canonical_url!r}), "
                f"screening_status={candidate.screening_status!r}"
            )

    budget = SharedRequestBudget(max_direct_requests)
    records: list[CandidateAuditRecord] = []
    stop_reason: str | None = None

    for index, (candidate, target) in enumerate(seeded):
        if stop_reason is not None:
            records.append(
                CandidateAuditRecord(
                    label=target.label,
                    candidate_id=candidate.id,
                    normalized_title=candidate.normalized_title,
                    organization=candidate.organization,
                    publisher=candidate.publisher,
                    publication_year=candidate.publication_year,
                    original_url=candidate.direct_download_url or candidate.canonical_url or "",
                    resolved_url=None,
                    expected_format=target.expected_format,
                    observed_extension=None,
                    content_type=None,
                    access_status=candidate.access_status,
                    screening_status_before=candidate.screening_status,
                    bytes_downloaded=None,
                    sha256=None,
                    predicted_cost_observation_yield=candidate.expected_cost_observation_yield,
                    predicted_technical_observation_yield=candidate.expected_technical_observation_yield,
                    outcome_status="not_attempted",
                    error_type=None,
                    stop_reason=f"pilot stopped before this candidate: {stop_reason}",
                )
            )
            continue

        # Fetch the URL as actually discovered, not the dedup-normalized
        # canonical_url -- see cli.py's `acquire` command for why.
        fetch_url = candidate.direct_download_url or candidate.canonical_url
        if not fetch_url:
            outcome = AcquisitionOutcome(status="failed", retryable=False, error_type="missing_url")
        else:
            try:
                outcome = acquire(
                    fetch_url,
                    client=client,
                    dest_dir=dest_dir,
                    max_bytes=max_bytes,
                    timeout_seconds=timeout_seconds,
                    resolver=resolver,
                    request_budget=budget,
                    allowed_extensions=_FORMAT_TO_EXTENSIONS[target.expected_format],
                )
            except Exception as exc:
                # Deliberate broad catch: any exception acquire() itself doesn't
                # already turn into a structured AcquisitionOutcome must still
                # become one here, logged, so this function never raises and the
                # caller's mandatory-artifact-writing code always runs (see docstring).
                logger.exception(
                    "open_resource_pilot.unexpected_acquisition_exception",
                    extra={
                        "candidate_id": candidate.id,
                        "label": target.label,
                        "requests_used_so_far": budget.used,
                    },
                )
                outcome = AcquisitionOutcome(
                    status="failed",
                    retryable=False,
                    error_type="unexpected_acquisition_exception",
                    error_message=f"{type(exc).__name__}: {exc}",
                )
        persist_acquisition_attempt(
            pilot_db, candidate, outcome, attempt_number=1, url=fetch_url or ""
        )
        pilot_db.commit()

        observed_extension = None
        if outcome.local_path:
            observed_extension = Path(outcome.local_path).suffix.lstrip(".") or None

        this_stop_reason = None
        if outcome.status != "succeeded":
            this_stop_reason = (
                f"{outcome.status}"
                f"{f':{outcome.error_type}' if outcome.error_type else ''}"
                f" -- expected format {target.expected_format!r}, "
                f"got content-type {outcome.content_type!r}"
            )
            stop_reason = this_stop_reason

        records.append(
            CandidateAuditRecord(
                label=target.label,
                candidate_id=candidate.id,
                normalized_title=candidate.normalized_title,
                organization=candidate.organization,
                publisher=candidate.publisher,
                publication_year=candidate.publication_year,
                original_url=fetch_url or "",
                resolved_url=outcome.resolved_url,
                expected_format=target.expected_format,
                observed_extension=observed_extension,
                content_type=outcome.content_type,
                access_status=candidate.access_status,
                screening_status_before=candidate.screening_status,
                bytes_downloaded=outcome.bytes_downloaded,
                sha256=outcome.sha256,
                predicted_cost_observation_yield=candidate.expected_cost_observation_yield,
                predicted_technical_observation_yield=candidate.expected_technical_observation_yield,
                outcome_status=outcome.status,
                error_type=outcome.error_type,
                stop_reason=this_stop_reason,
            )
        )
        logger.info(
            "open_resource_pilot.candidate_done",
            extra={
                "candidate_id": candidate.id,
                "label": target.label,
                "status": outcome.status,
                "error_type": outcome.error_type,
                "requests_used_so_far": budget.used,
                "index": index,
            },
        )

    return OpenResourcePilotResult(
        candidate_records=records,
        stopped_early=stop_reason is not None,
        stop_reason=stop_reason,
        restricted_detected=False,
        restricted_candidate_ids=[],
        requests_used=budget.used,
        max_direct_requests=max_direct_requests,
    )


def write_audit_report(
    path: Path, result: OpenResourcePilotResult, *, queue_entries: list[QueueEntry]
) -> None:
    """Write the durable JSON audit artifact -- the record required by this
    pilot's handoff/audit requirements (target identity, expected/observed
    format, original/resolved URL, access status, MIME type, byte size,
    SHA-256, predicted yields, acquisition outcome, and stop reason). Written
    unconditionally by the CLI command, whatever path the run took.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stopped_early": result.stopped_early,
        "stop_reason": result.stop_reason,
        "restricted_detected": result.restricted_detected,
        "restricted_candidate_ids": result.restricted_candidate_ids,
        "requests_used": result.requests_used,
        "max_direct_requests": result.max_direct_requests,
        "candidates": [asdict(record) for record in result.candidate_records],
        "queue_entries": [asdict(entry) for entry in queue_entries],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def write_queue_report(path: Path, entries: list[QueueEntry]) -> None:
    """Write the mandatory queue JSON artifact -- always written, even when
    `entries` is empty, so its presence/absence is never itself a signal of
    whether the pilot ran.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(entry) for entry in entries], indent=2, default=str), encoding="utf-8"
    )
