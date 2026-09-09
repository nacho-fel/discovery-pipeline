"""Explicit, validated state transitions for `SourceCandidate.screening_status`.

Transitions are validated in application code, not a DB CHECK constraint --
matching geocost's approach to its own status-like fields (plain `String(N)`
columns, validation logic living in the layer that mutates them).
"""

HAPPY_PATH: list[str] = [
    "discovered",
    "normalized",
    "deduplicated",
    "screened_accept",
    "acquisition_pending",
    "downloaded",
    "validated",
    "ingestion_ready",
    "handed_off",
]

REVIEW_STATES: set[str] = {"screened_review", "manual_review_required"}

REJECT_STATES: set[str] = {"screened_reject"}

FAILURE_STATES: set[str] = {
    "download_failed",
    "paywalled",
    "metadata_only",
    "unsupported_format",
    "corrupt_file",
    "metadata_incomplete",
    "validation_failed",
    # A candidate that was validly accepted but is deliberately never
    # attempted -- duplicate-by-URL-form, generic navigation page, likely
    # duplicate of an existing geocost source, malformed URL, or similar --
    # found during offline acquisition-ranking review, not a network
    # attempt outcome (that's `download_failed`) and not a relevance
    # reversal (the original `screened_accept` decision is untouched; see
    # the accompanying `ScreeningDecision` audit row for the reason).
    "excluded_pre_acquisition",
}

ALL_STATES: set[str] = set(HAPPY_PATH) | REVIEW_STATES | REJECT_STATES | FAILURE_STATES

# A failed/rejected/reviewed candidate is never silently marked as though it
# succeeded -- see CLAUDE.md-equivalent guidance in the brief's "Definition of
# done": retained states can only move forward again via an explicit re-attempt
# or explicit reviewer action, never skip back onto the happy path implicitly.
_TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"normalized"},
    "normalized": {"deduplicated"},
    "deduplicated": {"screened_accept", "screened_review", "screened_reject"},
    # `paywalled` here (not just via acquisition_pending) covers a candidate
    # already known-paywalled at normalization time (access_policy.py):
    # skip straight past acquisition_pending, no download is ever attempted.
    "screened_accept": {"acquisition_pending", "paywalled"},
    "screened_review": {"screened_accept", "screened_reject", "manual_review_required"},
    "screened_reject": set(),
    "acquisition_pending": {
        "downloaded",
        "download_failed",
        "paywalled",
        "metadata_only",
        "unsupported_format",
        "excluded_pre_acquisition",
    },
    "excluded_pre_acquisition": set(),
    "downloaded": {"validated", "corrupt_file"},
    "validated": {"ingestion_ready", "validation_failed", "metadata_incomplete"},
    "ingestion_ready": {"handed_off"},
    "handed_off": set(),
    # Failure/review states: retryable ones can loop back to acquisition_pending
    # for a fresh attempt, or be escalated to manual_review_required.
    "download_failed": {"acquisition_pending", "manual_review_required"},
    "paywalled": {"manual_review_required"},
    "metadata_only": {"manual_review_required"},
    "unsupported_format": {"manual_review_required"},
    "corrupt_file": {"acquisition_pending", "manual_review_required"},
    "validation_failed": {"manual_review_required"},
    "metadata_incomplete": {"manual_review_required", "acquisition_pending"},
    "manual_review_required": {"screened_accept", "screened_reject", "acquisition_pending"},
}


class InvalidStateTransitionError(ValueError):
    """Raised when a state transition is not permitted by the state machine."""


def validate_transition(current: str, new: str) -> None:
    """Raise `InvalidStateTransitionError` unless `current -> new` is a permitted edge."""
    if current not in ALL_STATES:
        raise InvalidStateTransitionError(f"Unknown current state: {current!r}")
    if new not in ALL_STATES:
        raise InvalidStateTransitionError(f"Unknown target state: {new!r}")
    allowed = _TRANSITIONS.get(current, set())
    if new not in allowed:
        raise InvalidStateTransitionError(
            f"Illegal transition {current!r} -> {new!r}; allowed: {sorted(allowed)}"
        )


def apply_transition(candidate, new_status: str) -> None:
    """Validate and apply `new_status` onto a `SourceCandidate` ORM instance."""
    validate_transition(candidate.screening_status, new_status)
    candidate.screening_status = new_status
