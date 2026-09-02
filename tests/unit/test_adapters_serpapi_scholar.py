import httpx

from discovery.adapters.serpapi_common import SerpApiClient
from discovery.adapters.serpapi_scholar import SerpApiScholarAdapter
from discovery.models.query import CoverageDimensions, PlannedQuery

_FAKE_RESPONSE = {
    "search_metadata": {"id": "req456", "status": "Success"},
    "organic_results": [
        {
            "position": 1,
            "title": "Cost analysis of oil, gas, and geothermal well drilling",
            "link": "https://example.com/lukawski.pdf",
            "snippet": "This paper presents...",
            "result_id": "abc123",
            "publication_info": {"summary": "M Lukawski - 2014"},
            "inline_links": {
                "cited_by": {"total": 120, "cites_id": "cites_xyz"},
                "versions": {"cluster_id": "cluster_789"},
            },
        }
    ],
    "serpapi_pagination": {},
}


def _query() -> PlannedQuery:
    return PlannedQuery(
        query_fingerprint="fp",
        adapter="serpapi_scholar",
        kind="named_project",
        canonical_intent="lukawski drilling cost",
        rendered_query="lukawski drilling cost",
        coverage_dimensions=CoverageDimensions(),
    )


def test_serpapi_scholar_captures_citation_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["engine"] == "google_scholar"
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = SerpApiClient("fake-key", httpx.Client(transport=httpx.MockTransport(handler)))
    adapter = SerpApiScholarAdapter(client)

    response = adapter.search(_query(), page_cursor=None)

    hit = response.hits[0]
    assert hit.cited_by_id == "cites_xyz"
    assert hit.cluster_version_id == "cluster_789"
    assert hit.provider_result_id == "abc123"
    assert hit.publication_info == "M Lukawski - 2014"


def test_serpapi_scholar_no_next_page_when_absent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_FAKE_RESPONSE)

    client = SerpApiClient("fake-key", httpx.Client(transport=httpx.MockTransport(handler)))
    adapter = SerpApiScholarAdapter(client)

    response = adapter.search(_query(), page_cursor=None)
    assert response.next_page_cursor is None
