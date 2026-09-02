"""SerpApiClient's quota/caching/retry-classification integration --
verified separately from test_secret_redaction.py's secret-safety focus and
test_adapters_serpapi_google/scholar.py's hit-parsing focus.

Every HTTP interaction goes through httpx.MockTransport; no real network
call anywhere in this file.
"""

import httpx
import pytest

from discovery.adapters.base import (
    AttemptContext,
    NonRetryableAdapterError,
    current_attempt_context,
)
from discovery.adapters.serpapi_common import SerpApiClient


def _success_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"search_metadata": {"status": "Success", "id": "abc"}, "organic_results": []},
    )


def test_cache_hit_never_calls_quota_reserve(tmp_path):
    calls = {"http": 0, "reserve": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["http"] += 1
        return _success_response()

    def quota_reserve(**kwargs):
        calls["reserve"] += 1

    client = SerpApiClient(
        "key", httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path, quota_reserve=quota_reserve
    )
    params = {"engine": "google", "q": "geothermal drilling cost"}

    client.raw_search(params)  # cache miss: 1 HTTP call, 1 reservation
    client.raw_search(params)  # cache hit: 0 more HTTP calls, 0 more reservations

    assert calls["http"] == 1
    assert calls["reserve"] == 1


def test_force_refresh_bypasses_cache_and_reserves_again(tmp_path):
    calls = {"http": 0, "reserve": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["http"] += 1
        return _success_response()

    def quota_reserve(**kwargs):
        calls["reserve"] += 1

    client = SerpApiClient(
        "key", httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path, quota_reserve=quota_reserve
    )
    params = {"engine": "google", "q": "geothermal drilling cost"}

    client.raw_search(params)
    client.raw_search(params, force_refresh=True)

    assert calls["http"] == 2
    assert calls["reserve"] == 2


def test_quota_reserve_raising_is_translated_to_nonretryable_and_no_request_is_made(tmp_path):
    calls = {"http": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["http"] += 1
        return _success_response()

    def quota_reserve(**kwargs):
        from discovery.quota_ledger import QuotaExhaustedError

        raise QuotaExhaustedError("campaign exhausted")

    client = SerpApiClient(
        "key", httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path, quota_reserve=quota_reserve
    )

    with pytest.raises(NonRetryableAdapterError) as exc_info:
        client.raw_search({"engine": "google", "q": "x"})

    assert exc_info.value.error_type == "quota_exhausted"
    assert calls["http"] == 0  # the request was never made


def test_quota_reserve_receives_attempt_context_fields(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response()

    def quota_reserve(**kwargs):
        captured.update(kwargs)

    client = SerpApiClient(
        "key", httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path, quota_reserve=quota_reserve
    )

    token = current_attempt_context.set(
        AttemptContext(
            attempt_kind="retry",
            retry_reason="timeout",
            batch_number=3,
            query_family="broad_domain",
            query_fingerprint="fp-123",
            discovery_run_id="run-1",
        )
    )
    try:
        client.raw_search({"engine": "google", "q": "x"})
    finally:
        current_attempt_context.reset(token)

    assert captured["attempt_kind"] == "retry"
    assert captured["retry_reason"] == "timeout"
    assert captured["batch_number"] == 3
    assert captured["query_family"] == "broad_domain"
    assert captured["query_fingerprint"] == "fp-123"
    assert captured["discovery_run_id"] == "run-1"


def test_malformed_query_error_is_nonretryable(tmp_path):
    """A 200 whose body reports search_metadata.status == "Error" is treated
    as a malformed-query/parameter problem, not a transient one -- genuinely
    transient failures (429, 5xx, timeout) are classified separately by
    HTTP status/transport exception, so this body-level error must not be
    retried (retrying a malformed query just re-spends quota reproducing
    the same failure).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search_metadata": {"status": "Error"}, "error": "Invalid query"})

    client = SerpApiClient("key", httpx.Client(transport=httpx.MockTransport(handler)))

    with pytest.raises(NonRetryableAdapterError) as exc_info:
        client.raw_search({"engine": "google", "q": "x"})
    assert exc_info.value.error_type == "provider_error"


def test_valid_empty_result_is_not_an_error_at_all(tmp_path):
    """An empty, well-formed result set must never be treated as a failure
    -- it's a legitimate outcome, not something to retry.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"search_metadata": {"status": "Success"}, "organic_results": []})

    client = SerpApiClient("key", httpx.Client(transport=httpx.MockTransport(handler)))

    payload = client.raw_search({"engine": "google", "q": "x"})
    assert payload["organic_results"] == []
