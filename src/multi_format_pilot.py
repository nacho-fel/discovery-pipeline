"""The multi-format acquisition pilot: production-readiness demonstration
that supersedes the PDF-only `acquisition_pilot.py` (preserved unchanged as
a regression/safety test -- see its own module docstring).

Three legs, exercising every acquisition path this pipeline actually has,
not just the PDF one:

  1. **Open PDF** -- an automated download through the same
     `acquirer.acquire()` path `acquisition_pilot.py` uses.
  2. **Open structured-data resource** (an XLSX/CSV/ZIP dataset, typically
     from a GDR-style repository) -- also an automated download through
     `acquirer.acquire()`, proving the acquisition path isn't PDF-specific.
  3. **A restricted (licensed/authentication-required) resource** -- proves
     the *opposite* property: this pipeline must never attempt an automated
     download of it. This leg does not download anything; it verifies the
     candidate is routed to `paywalled` and appears in
     `mit_assisted_acquisition.build_acquisition_queue`, exactly the
     behavior the MIT-assisted manual workflow depends on.

Deliberately generic over its actual targets -- unlike
`acquisition_pilot.py`'s two specific, already-discovered candidates from a
named historical run, this pilot's targets are supplied by the caller
(3 canonical URLs known ahead of time to satisfy each leg's role), because
selecting concrete, currently-live URLs for legs 2 and 3 is an operational
decision for whoever runs this pilot, not a decision this offline
implementation phase can make. See docs/multi_format_pilot.md.

Same numeric safety ceilings as `acquisition_pilot.py`, for the same
reason: `MAX_AUTOMATED_CANDIDATES` (2, legs 1-2 only -- the licensed leg
never calls `acquire()`) x (1 initial request + `MAX_REDIRECTS_PER_CANDIDATE`
5 redirects) = `MAX_TOTAL_REQUESTS` (12), enforced via one shared
`SharedRequestBudget`.
"""

import logging
import socket
from dataclasses import dataclass

from sqlalchemy.orm import Session

from discovery.access_policy import is_automatable_access, is_restricted_access
from discovery.acquirer import (
    AcquisitionOutcome,
    SharedRequestBudget,
    acquire,
    persist_acquisition_attempt,
)
from discovery.db.models import SourceCandidate
from discovery.mit_assisted_acquisition import build_acquisition_queue

logger = logging.getLogger(__name__)

MAX_AUTOMATED_CANDIDATES = 2
MAX_TOTAL_REQUESTS = 12
MAX_REDIRECTS_PER_CANDIDATE = 5


class MultiFormatPilotError(ValueError):
    """Raised when the pilot's strict candidate-role constraints aren't met."""


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


def seed_multi_format_pilot(
    source_db: Session,
    pilot_db: Session,
    *,
    open_pdf_url: str,
    open_dataset_url: str,
    licensed_url: str,
) -> tuple[list[SourceCandidate], SourceCandidate]:
    """Copy exactly three named candidates from `source_db` into a fresh,
    isolated `pilot_db`: two open/automated-acquisition targets and one
    restricted target for the negative (never-auto-downloaded) leg.

    Refuses (raises `MultiFormatPilotError`, seeds nothing) if either open
    candidate isn't found as a non-restricted `acquisition_pending`
    candidate, or if the licensed candidate isn't found as a restricted,
    `paywalled` candidate -- each leg's precondition is enforced at the
    seeding boundary, the same discipline `acquisition_pilot.py` uses.
    """
    open_candidates = []
    for url in (open_pdf_url, open_dataset_url):
        source_candidate = (
            source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == url).first()
        )
        if source_candidate is None:
            raise MultiFormatPilotError(f"No candidate found in source database for {url!r}")
        if not is_automatable_access(source_candidate.access_status):
            raise MultiFormatPilotError(
                f"Refusing to seed a non-automatable-access candidate as an open-acquisition leg: {url!r}"
            )
        if source_candidate.screening_status != "acquisition_pending":
            raise MultiFormatPilotError(
                f"Candidate {url!r} is not acquisition_pending "
                f"(status={source_candidate.screening_status!r})"
            )
        open_candidates.append(_copy_candidate(source_candidate, screening_status="acquisition_pending"))

    licensed_source = (
        source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == licensed_url).first()
    )
    if licensed_source is None:
        raise MultiFormatPilotError(f"No candidate found in source database for {licensed_url!r}")
    if not is_restricted_access(licensed_source.access_status):
        raise MultiFormatPilotError(
            f"Licensed-leg candidate must have a restricted access_status, got "
            f"{licensed_source.access_status!r}: {licensed_url!r}"
        )
    if licensed_source.screening_status != "paywalled":
        raise MultiFormatPilotError(
            f"Licensed-leg candidate must already be routed to 'paywalled' "
            f"(status={licensed_source.screening_status!r}): {licensed_url!r}"
        )
    licensed_candidate = _copy_candidate(licensed_source, screening_status="paywalled")

    for candidate in (*open_candidates, licensed_candidate):
        pilot_db.add(candidate)
    pilot_db.commit()
    return open_candidates, licensed_candidate


@dataclass
class MultiFormatPilotResult:
    automated_outcomes: list[AcquisitionOutcome]
    licensed_candidate_id: str
    licensed_candidate_correctly_queued: bool
    licensed_candidate_download_attempted: bool  # must always be False


def run_multi_format_pilot(
    pilot_db: Session,
    open_candidates: list[SourceCandidate],
    licensed_candidate: SourceCandidate,
    *,
    client,
    dest_dir,
    max_bytes: int,
    timeout_seconds: float,
    resolver=socket.getaddrinfo,
) -> MultiFormatPilotResult:
    """Run legs 1-2 (automated download, shared-budget-capped, identical
    mechanics to `acquisition_pilot.run_acquisition_pilot`) and verify leg 3
    (the licensed candidate is never downloaded and is present in the
    MIT-assisted acquisition queue instead).
    """
    if len(open_candidates) > MAX_AUTOMATED_CANDIDATES:
        raise MultiFormatPilotError(
            f"Pilot cannot automate-acquire more than {MAX_AUTOMATED_CANDIDATES} candidates, "
            f"got {len(open_candidates)}"
        )
    for candidate in open_candidates:
        if not is_automatable_access(candidate.access_status):
            raise MultiFormatPilotError(
                f"Refusing to automate-acquire a non-automatable-access candidate: {candidate.canonical_url!r}"
            )
    if licensed_candidate.screening_status != "paywalled" or not is_restricted_access(
        licensed_candidate.access_status
    ):
        raise MultiFormatPilotError(
            "Licensed-leg candidate must be restricted-access and paywalled -- refusing to run pilot"
        )

    budget = SharedRequestBudget(MAX_TOTAL_REQUESTS)
    outcomes: list[AcquisitionOutcome] = []
    for candidate in open_candidates:
        # Fetch the URL as actually discovered, not the dedup-normalized
        # canonical_url -- see cli.py's `acquire` command for why.
        fetch_url = candidate.direct_download_url or candidate.canonical_url
        if not fetch_url:
            outcome = AcquisitionOutcome(status="failed", retryable=False, error_type="missing_url")
        else:
            outcome = acquire(
                fetch_url,
                client=client,
                dest_dir=dest_dir,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
                resolver=resolver,
                request_budget=budget,
            )
        persist_acquisition_attempt(
            pilot_db, candidate, outcome, attempt_number=1, url=fetch_url or ""
        )
        pilot_db.commit()
        outcomes.append(outcome)
        logger.info(
            "multi_format_pilot.open_leg_done",
            extra={"candidate_id": candidate.id, "status": outcome.status, "requests_used_so_far": budget.used},
        )

    # Leg 3: never call acquire() on the licensed candidate. Verify instead
    # that it's exactly where the MIT-assisted workflow expects it.
    queue = build_acquisition_queue(pilot_db)
    correctly_queued = any(entry.candidate_id == licensed_candidate.id for entry in queue)
    logger.info(
        "multi_format_pilot.licensed_leg_verified",
        extra={"candidate_id": licensed_candidate.id, "correctly_queued": correctly_queued},
    )

    return MultiFormatPilotResult(
        automated_outcomes=outcomes,
        licensed_candidate_id=licensed_candidate.id,
        licensed_candidate_correctly_queued=correctly_queued,
        licensed_candidate_download_attempted=False,
    )
