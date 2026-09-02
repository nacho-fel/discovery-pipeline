from pathlib import Path

from discovery.access_policy import (
    ACCESS_STATES,
    infer_access_status,
    is_automatable_access,
    is_restricted_access,
    load_access_policy,
)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "access_policy.yaml"


def test_load_real_access_policy_parses():
    policy = load_access_policy(_CONFIG_PATH)
    assert policy["onepetro.org"] == "licensed_mit_access"
    assert policy["nrel.gov"] == "open_access"


def test_real_access_policy_values_are_all_known_states():
    policy = load_access_policy(_CONFIG_PATH)
    assert set(policy.values()) <= ACCESS_STATES


def test_is_restricted_access():
    assert is_restricted_access("licensed_mit_access") is True
    assert is_restricted_access("authentication_required") is True
    assert is_restricted_access("manual_acquisition_required") is True
    assert is_restricted_access("unavailable") is True
    assert is_restricted_access("metadata_only") is True
    assert is_restricted_access("open_access") is False
    assert is_restricted_access(None) is False


def test_is_automatable_access_allows_only_open_access_and_unclassified():
    assert is_automatable_access("open_access") is True
    assert is_automatable_access(None) is True
    assert is_automatable_access("licensed_mit_access") is False
    assert is_automatable_access("authentication_required") is False
    assert is_automatable_access("manual_acquisition_required") is False
    assert is_automatable_access("metadata_only") is False
    assert is_automatable_access("unavailable") is False


def test_every_access_state_is_either_automatable_or_restricted_never_both_never_neither():
    for state in ACCESS_STATES:
        assert is_automatable_access(state) != is_restricted_access(state)


def test_infer_access_status_paywalled_domain():
    policy = {"onepetro.org": "paywalled"}
    assert infer_access_status("https://onepetro.org/SPE/12345", policy=policy) == "paywalled"


def test_infer_access_status_open_domain():
    policy = {"nrel.gov": "open"}
    assert infer_access_status("https://nrel.gov/reports/x.pdf", policy=policy) == "open"


def test_infer_access_status_unknown_domain_returns_none():
    policy = {"nrel.gov": "open"}
    assert infer_access_status("https://example.com/report.pdf", policy=policy) is None


def test_infer_access_status_matches_subdomain():
    policy = {"onepetro.org": "paywalled"}
    assert infer_access_status("https://www.onepetro.org/x", policy=policy) == "paywalled"
    assert infer_access_status("https://library.onepetro.org/x", policy=policy) == "paywalled"


def test_infer_access_status_matches_explicit_port():
    policy = {"onepetro.org": "paywalled"}
    assert infer_access_status("https://onepetro.org:443/x", policy=policy) == "paywalled"


def test_infer_access_status_none_url():
    assert infer_access_status(None, policy={"nrel.gov": "open"}) is None


def test_infer_access_status_never_substring_matches():
    # "notonepetro.org" must not match "onepetro.org" via substring.
    policy = {"onepetro.org": "paywalled"}
    assert infer_access_status("https://notonepetro.org/x", policy=policy) is None
