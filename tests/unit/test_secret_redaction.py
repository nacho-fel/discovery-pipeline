"""Secret-safety tests, mirroring geocost's `test_secret_safety.py` approach:
assert a fake secret literal never survives into anything persisted or logged,
rather than testing a generic "redactor" in isolation.
"""

import json

import httpx

from discovery.adapters.serpapi_common import SerpApiClient, cache_key, redact_secrets
from discovery.result_registry import strip_secrets

FAKE_SECRET = "sk-THIS_IS_A_FAKE_SECRET_MUST_NEVER_APPEAR_ANYWHERE_abc123"


def test_redact_secrets_strips_api_key_from_url_string():
    payload = {
        "search_metadata": {
            "json_endpoint": f"https://serpapi.com/searches/x.json?api_key={FAKE_SECRET}",
            "raw_html_file": f"https://serpapi.com/searches/x.html?api_key={FAKE_SECRET}&foo=bar",
        },
        "nested": {"list": [f"https://serpapi.com/x?api_key={FAKE_SECRET}"]},
    }
    redacted = redact_secrets(payload)
    dumped = json.dumps(redacted)
    assert FAKE_SECRET not in dumped
    assert "foo=bar" in dumped  # other params survive


def test_cache_key_excludes_api_key():
    key_with_secret_a = cache_key({"q": "x", "api_key": "secret-a"})
    key_with_secret_b = cache_key({"q": "x", "api_key": "secret-b"})
    assert key_with_secret_a == key_with_secret_b


def test_serpapi_client_never_caches_the_raw_api_key(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == FAKE_SECRET
        return httpx.Response(
            200,
            json={
                "search_metadata": {
                    "status": "Success",
                    "json_endpoint": f"https://serpapi.com/x?api_key={FAKE_SECRET}",
                },
                "organic_results": [],
            },
        )

    client = SerpApiClient(
        FAKE_SECRET, httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path
    )
    client.raw_search({"engine": "google", "q": "x"})

    cache_files = list(tmp_path.glob("*.json"))
    assert len(cache_files) == 1
    assert FAKE_SECRET not in cache_files[0].read_text(encoding="utf-8")


def test_strip_secrets_removes_known_secret_keys():
    stripped = strip_secrets({"q": "x", "api_key": FAKE_SECRET, "Authorization": f"Bearer {FAKE_SECRET}"})
    assert "api_key" not in stripped
    assert "Authorization" not in stripped
    assert stripped == {"q": "x"}
