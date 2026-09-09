"""A durable, atomically-reserved ledger of physical SerpApi HTTP attempts.

Replaces the earlier approximate, in-process/query-count-based cross-run
quota tracking (`adaptive_controller.QuotaTracker`, which summed
`DiscoveryRun.requests_attempted` -- an honest but approximate figure) with
an exact, persisted counter: `QuotaLedgerState.reserved_count`, updated via
one atomic SQL statement per physical attempt, before that attempt is made.

Why this is safe under restarts and concurrent processes:

  - The counter is a database row, not a Python object -- a process restart
    reads the same row back, no state is lost.
  - `reserve()` performs exactly one `UPDATE quota_ledger_state SET
    reserved_count = reserved_count + 1 WHERE campaign_id = ? AND
    reserved_count < max_requests`, committed immediately. SQL engines
    (including SQLite, which this pipeline uses) serialize conflicting
    writers to the same row: two concurrent `reserve()` calls -- from two
    threads, two processes, or two independent `discovery adaptive-run`
    invocations -- can never both observe `reserved_count < max_requests`
    and both succeed past the ceiling, because the second writer's UPDATE
    necessarily executes after the first's commit, against the
    already-incremented value. The `rowcount` of that single UPDATE is the
    only signal `reserve()` trusts (0 = exhausted, no room; 1 = a unit was
    genuinely reserved) -- it never reads-then-writes as two separate
    steps, which is exactly the pattern that would be racy.
  - Every reservation is also written as its own `QuotaLedgerEntry` row
    (in the *same* commit as the counter increment) before the physical
    request is made, so a process that crashes mid-request still leaves an
    accurate, auditable "this unit was spent" record -- the reservation,
    not the eventual HTTP outcome, is what the ceiling protects.

Where this is called from: `SerpApiClient.raw_search` (see
`adapters/serpapi_common.py`), immediately before the real `httpx` GET and
strictly *after* the on-disk cache-hit check -- a cache hit never reserves
a unit, since no physical request occurs. This is a deliberate change from
`call_with_retry`'s old `SharedRequestBudget.consume()` placement (called
once per *logical* attempt regardless of cache), which would have charged
the ledger even for a cache hit.
"""

import logging
from dataclasses import dataclass
from typing import cast

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from discovery.db.models import QuotaLedgerEntry, QuotaLedgerState
from discovery.timeutils import utcnow

logger = logging.getLogger(__name__)

DEFAULT_CAMPAIGN_ID = "production"


class QuotaExhaustedError(Exception):
    """Raised when a campaign's `reserved_count` has reached `max_requests`."""


@dataclass
class ReservationHandle:
    entry_id: str
    campaign_id: str
    quota_before: int
    quota_after: int
    max_requests: int


def get_or_create_campaign(db: Session, campaign_id: str, *, max_requests: int) -> QuotaLedgerState:
    """Idempotent -- including under concurrent callers racing to create the
    *same* campaign for the first time. An existing campaign's
    `max_requests` is never silently changed by re-calling this; lowering
    or raising a campaign's ceiling is a deliberate, explicit operation
    (`set_campaign_ceiling`), not a side effect of resuming it.
    """
    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
    if state is not None:
        return state
    state = QuotaLedgerState(
        campaign_id=campaign_id,
        max_requests=max_requests,
        reserved_count=0,
        completed_count=0,
        failed_count=0,
    )
    db.add(state)
    try:
        db.commit()
    except IntegrityError:
        # Another concurrent caller (thread or process) won the race to
        # create this exact campaign row between our SELECT and our INSERT
        # -- not an error, just lost a benign race. Roll back our own
        # attempted insert and read back whichever row actually landed.
        db.rollback()
        state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
        if state is None:
            raise
    return state


def set_campaign_ceiling(db: Session, campaign_id: str, *, max_requests: int) -> QuotaLedgerState:
    """Explicitly change an existing (or newly-created) campaign's ceiling.

    Refuses to lower it below `reserved_count` already spent -- a ceiling
    can never retroactively invalidate reservations already made.
    """
    state = get_or_create_campaign(db, campaign_id, max_requests=max_requests)
    if max_requests < state.reserved_count:
        raise ValueError(
            f"Cannot set campaign {campaign_id!r} ceiling to {max_requests} -- "
            f"{state.reserved_count} requests are already reserved"
        )
    state.max_requests = max_requests
    db.commit()
    return state


def remaining(db: Session, campaign_id: str) -> int:
    """Remaining quota for `campaign_id`, or 0 if it doesn't exist yet.

    A never-created campaign has spent nothing, but this deliberately
    returns 0 rather than an unbounded/unknown figure -- callers that need
    "the full ceiling, nothing spent yet" should call
    `get_or_create_campaign` first (as `run_adaptive_discovery` and
    `estimate_dry_run` both do), not infer it from this function alone.
    """
    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
    if state is None:
        return 0
    return max(0, state.max_requests - state.reserved_count)


def used(db: Session, campaign_id: str) -> int:
    """Total reserved (spent) quota for `campaign_id`, or 0 if it doesn't exist yet."""
    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
    return state.reserved_count if state is not None else 0


def reserve(
    db: Session,
    campaign_id: str,
    *,
    max_requests: int,
    batch_number: int | None = None,
    query_family: str | None = None,
    query_fingerprint: str | None = None,
    discovery_run_id: str | None = None,
    attempt_kind: str = "initial",
    retry_reason: str | None = None,
) -> ReservationHandle:
    """Atomically reserve one unit of campaign quota for a physical SerpApi
    attempt, writing its audit-ledger row in the same commit. Raises
    `QuotaExhaustedError` (reserving nothing) if the campaign has no room
    left -- the caller must not make the physical HTTP request in that case.

    `max_requests` is only consulted the first time a campaign is seen
    (`get_or_create_campaign`); to change an existing campaign's ceiling,
    call `set_campaign_ceiling` explicitly.
    """
    get_or_create_campaign(db, campaign_id, max_requests=max_requests)

    result = db.execute(
        sa.update(QuotaLedgerState)
        .where(
            QuotaLedgerState.campaign_id == campaign_id,
            QuotaLedgerState.reserved_count < QuotaLedgerState.max_requests,
        )
        .values(reserved_count=QuotaLedgerState.reserved_count + 1)
    )
    db.commit()
    rowcount = cast(int, result.rowcount)  # type: ignore[attr-defined]

    if rowcount == 0:
        state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
        known_max = state.max_requests if state is not None else max_requests
        logger.warning(
            "quota_ledger.exhausted", extra={"campaign_id": campaign_id, "max_requests": known_max}
        )
        raise QuotaExhaustedError(
            f"Campaign {campaign_id!r} has no remaining quota (max_requests={known_max})"
        )

    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
    assert state is not None  # the UPDATE above just succeeded against this exact row
    quota_after = state.reserved_count
    quota_before = quota_after - 1

    entry = QuotaLedgerEntry(
        campaign_id=campaign_id,
        batch_number=batch_number,
        query_family=query_family,
        query_fingerprint=query_fingerprint,
        discovery_run_id=discovery_run_id,
        attempt_kind=attempt_kind,
        retry_reason=retry_reason,
        status="reserved",
        quota_before=quota_before,
        quota_after=quota_after,
        reserved_at=utcnow(),
    )
    db.add(entry)
    db.commit()

    logger.info(
        "quota_ledger.reserved",
        extra={
            "campaign_id": campaign_id,
            "entry_id": entry.id,
            "quota_after": quota_after,
            "max_requests": state.max_requests,
            "attempt_kind": attempt_kind,
        },
    )
    return ReservationHandle(
        entry_id=entry.id,
        campaign_id=campaign_id,
        quota_before=quota_before,
        quota_after=quota_after,
        max_requests=state.max_requests,
    )


def complete(
    db: Session,
    entry_id: str,
    *,
    failed: bool = False,
    response_status_code: int | None = None,
    candidates_produced: int | None = None,
    unique_relevant_produced: int | None = None,
) -> None:
    """Mark a reserved entry's physical outcome. Never changes
    `reserved_count` (the reservation already happened and is permanent --
    a failed physical attempt still genuinely spent one unit of quota
    against the real SerpApi account), only the entry's own audit fields
    and the campaign's `completed_count`/`failed_count` tallies.
    """
    entry = db.query(QuotaLedgerEntry).filter(QuotaLedgerEntry.id == entry_id).first()
    if entry is None:
        raise ValueError(f"No quota_ledger_entry with id {entry_id!r}")

    entry.status = "failed" if failed else "completed"
    entry.response_status_code = response_status_code
    entry.candidates_produced = candidates_produced
    entry.unique_relevant_produced = unique_relevant_produced
    entry.completed_at = utcnow()

    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == entry.campaign_id).first()
    if state is not None:
        if failed:
            state.failed_count += 1
        else:
            state.completed_count += 1
    db.commit()


def annotate_batch_yield(
    db: Session,
    campaign_id: str,
    batch_number: int,
    *,
    candidates_produced: int,
    unique_relevant_produced: int,
) -> int:
    """Backfill `candidates_produced`/`unique_relevant_produced` on every
    still-unannotated ledger entry for one batch, in bulk.

    Per-request response detail (status code) is captured at `complete()`
    time by the SerpApi client itself; per-request *yield* (how many
    candidates a specific physical request produced) isn't observable at
    that layer -- yield is only known once `_process_hits` has run for the
    whole batch. Annotating at batch granularity is an explicit, documented
    scope decision: precise enough to drive family/batch-level allocation
    decisions, without threading a result-count callback through every
    layer between `service.execute_run` and the SerpApi client. Returns the
    number of entries annotated.
    """
    result = db.execute(
        sa.update(QuotaLedgerEntry)
        .where(
            QuotaLedgerEntry.campaign_id == campaign_id,
            QuotaLedgerEntry.batch_number == batch_number,
            QuotaLedgerEntry.candidates_produced.is_(None),
        )
        .values(
            candidates_produced=candidates_produced,
            unique_relevant_produced=unique_relevant_produced,
        )
    )
    db.commit()
    return cast(int, result.rowcount)  # type: ignore[attr-defined]


def campaign_summary(db: Session, campaign_id: str) -> dict:
    """A read-only snapshot for reporting/dry-run output."""
    state = db.query(QuotaLedgerState).filter(QuotaLedgerState.campaign_id == campaign_id).first()
    if state is None:
        return {"campaign_id": campaign_id, "exists": False}
    retry_count = (
        db.query(QuotaLedgerEntry)
        .filter(QuotaLedgerEntry.campaign_id == campaign_id, QuotaLedgerEntry.attempt_kind == "retry")
        .count()
    )
    return {
        "campaign_id": campaign_id,
        "exists": True,
        "max_requests": state.max_requests,
        "reserved_count": state.reserved_count,
        "completed_count": state.completed_count,
        "failed_count": state.failed_count,
        "retry_count": retry_count,
        "remaining": max(0, state.max_requests - state.reserved_count),
    }
