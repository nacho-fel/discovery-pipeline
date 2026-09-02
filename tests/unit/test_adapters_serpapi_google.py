import httpx

from discovery.adapters.serpapi_common import SerpApiClient
from discovery.adapters.serpapi_google import SerpApiGoogleAdapter
from discovery.models.query import CoverageDimensions, PlannedQuery

_FAKE_RESPONSE = {
    "search_metadata": {"id": "req123", "status": "Success", "json_endpoint": "https://serpapi.com/x?api_key=SECRET"},
    "search_information": {"total_results": 2},
    "organic_results": [
        {
            "position": 1,
            "title": "EGS Drilling Cost Report",
            "link": "https://example.com/report.pdf",
            "snippet": "Drilling cost per foot data.",
            "displayed_link": "example.com",
        },
        {
            "position": 2,
            "title": "Another Report",
            "link": "https://example.org/report2.pdf",
        },
    ],
    "serpapi_pagination": {"next": "https://serpapi.com/search?start=10"},
}


def _query() -> PlannedQuery:
    return PlannedQuery(
        query_fingerprint="fp",
        adapter="serpapi_google",
        kind="broad_domain",
        canonical_intent="egs drilling cost",
        rendered_query="egs drilling cost",
        coverage_dimensions=CoverageDimensions(),
    )


def _client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_serpapi_google_parses_hits_and_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google"
        assert request.url.params["q"] == "egs drilling cost"
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = SerpApiClient("fake-key", _client_with_handler(handler))
    adapter = SerpApiGoogleAdapter(client)

    response = adapter.search(_query(), page_cursor=None)

    assert response.result_count == 2
    assert response.hits[0].title == "EGS Drilling Cost Report"
    assert response.hits[0].url == "https://example.com/report.pdf"
    assert response.hits[0].rank == 1
    assert response.next_page_cursor == "10"


def test_serpapi_google_redacts_api_key_from_raw_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = SerpApiClient("fake-key", _client_with_handler(handler))
    adapter = SerpApiGoogleAdapter(client)

    response = adapter.search(_query(), page_cursor=None)

    assert "api_key" not in response.raw_response["search_metadata"]["json_endpoint"]
    assert "SECRET" not in response.raw_response["search_metadata"]["json_endpoint"]


def test_serpapi_google_pagination_offset():
    seen_starts = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_starts.append(request.url.params["start"])
        return httpx.Response(200, json={**_FAKE_RESPONSE, "serpapi_pagination": {}})

    client = SerpApiClient("fake-key", _client_with_handler(handler))
    adapter = SerpApiGoogleAdapter(client, results_per_page=10)

    adapter.search(_query(), page_cursor="10")
    assert seen_starts == ["10"]
