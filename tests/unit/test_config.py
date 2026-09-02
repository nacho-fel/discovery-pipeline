from discovery.config import Settings


def test_campaign_max_requests_defaults_to_5000_or_less():
    assert Settings().campaign_max_requests <= 5000


def test_campaign_max_requests_is_configurable_downward(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_MAX_REQUESTS", "500")
    assert Settings().campaign_max_requests == 500


def test_serpapi_max_retries_defaults_to_at_most_one():
    assert Settings().serpapi_max_retries <= 1


def test_staged_allocation_bounds_are_monotonically_increasing_and_within_the_campaign_ceiling():
    settings = Settings()
    assert (
        settings.campaign_stage_a_max_requests
        < settings.campaign_stage_b_max_requests
        < settings.campaign_stage_c_max_requests
        <= settings.campaign_max_requests
    )


def test_campaign_batch_size_is_within_the_recommended_50_to_100_range():
    settings = Settings()
    assert 50 <= settings.campaign_batch_size <= 100
