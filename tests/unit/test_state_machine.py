import pytest

from discovery.state_machine import InvalidStateTransitionError, validate_transition


def test_happy_path_transitions_allowed():
    validate_transition("discovered", "normalized")
    validate_transition("normalized", "deduplicated")
    validate_transition("deduplicated", "screened_accept")
    validate_transition("screened_accept", "acquisition_pending")
    validate_transition("acquisition_pending", "downloaded")
    validate_transition("downloaded", "validated")
    validate_transition("validated", "ingestion_ready")
    validate_transition("ingestion_ready", "handed_off")


def test_illegal_transition_raises():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("discovered", "handed_off")


def test_terminal_states_have_no_outgoing_transitions():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("handed_off", "discovered")
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("screened_reject", "screened_accept")


def test_screened_accept_can_go_straight_to_paywalled():
    # A candidate already known-paywalled at normalization time (see
    # access_policy.py) skips acquisition entirely.
    validate_transition("screened_accept", "paywalled")


def test_failed_download_can_retry_or_escalate():
    validate_transition("download_failed", "acquisition_pending")
    validate_transition("download_failed", "manual_review_required")


def test_manual_review_can_resolve_either_way():
    validate_transition("manual_review_required", "screened_accept")
    validate_transition("manual_review_required", "screened_reject")


def test_unknown_state_raises():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("not_a_real_state", "discovered")
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("discovered", "not_a_real_state")


def test_acquisition_pending_can_be_excluded_pre_acquisition():
    # A validly-accepted candidate found to be a duplicate/navigation-page/
    # malformed-URL during offline acquisition ranking, before any network
    # attempt -- distinct from `download_failed` (a real attempt occurred).
    validate_transition("acquisition_pending", "excluded_pre_acquisition")


def test_excluded_pre_acquisition_is_terminal():
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("excluded_pre_acquisition", "acquisition_pending")
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("excluded_pre_acquisition", "downloaded")


def test_never_silently_skips_failure_back_onto_happy_path():
    # A rejected candidate can never transition directly into acquisition.
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("screened_reject", "acquisition_pending")
    # Paywalled must be reviewed, not silently marked downloaded.
    with pytest.raises(InvalidStateTransitionError):
        validate_transition("paywalled", "downloaded")
