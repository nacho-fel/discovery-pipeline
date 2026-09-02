import httpx

from discovery.adapters.crossref import CrossrefAdapter
from discovery.adapters.openalex import OpenAlexAdapter
from discovery.models.query import CoverageDimensions, PlannedQuery


def _query() -> PlannedQuery:
    return PlannedQuery(
        query_fingerprint="fp",
        adapter="openalex",
        kind="broad_domain",
        canonical_intent="egs cost",
        rendered_query="egs cost",
        coverage_dimensions=CoverageDimensions(),
    )


def test_openalex_parses_results_and_joins_polite_pool():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["mailto"] == "researcher@example.edu"
        return httpx.Response(
            200,
            json={
                "meta": {"count": 1, "page": 1, "per_page": 25},
                "results": [
                    {
                        "id": "https://openalex.org/W123",
                        "doi": "https://doi.org/10.1/x",
                        "title": "EGS Cost Drivers",
                        "publication_year": 2022,
                        "authorships": [{"author": {"display_name": "Jane Doe"}}],
                        "open_access": {"is_oa": True, "oa_url": "https://example.com/w123.pdf"},
                        "primary_location": {"landing_page_url": "https://example.com/landing"},
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OpenAlexAdapter(client, contact_email="researcher@example.edu")

    response = adapter.search(_query(), page_cursor=None)

    assert response.result_count == 1
    hit = response.hits[0]
    assert hit.title == "EGS Cost Drivers"
    assert hit.doi == "https://doi.org/10.1/x"
    assert hit.authors == ["Jane Doe"]
    assert hit.url == "https://example.com/w123.pdf"
    assert response.next_page_cursor is None  # count=1 <= per_page


def test_crossref_parses_results_and_extracts_year():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "total-results": 30,
                    "items": [
                        {
                            "DOI": "10.2/y",
                            "title": ["Geothermal Well Cost Correlations"],
                            "author": [{"given": "M.", "family": "Lukawski"}],
                            "published-print": {"date-parts": [[2014, 3]]},
                            "URL": "https://example.com/crossref-item",
                        }
                    ],
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = CrossrefAdapter(client)

    response = adapter.search(_query(), page_cursor=None)

    hit = response.hits[0]
    assert hit.doi == "10.2/y"
    assert hit.publication_year == 2014
    assert hit.authors == ["M. Lukawski"]
    # total-results (30) > offset(0) + rows(25) -> another page exists.
    assert response.next_page_cursor == "25"


def test_crossref_handles_incomplete_date_parts_without_crashing():
    """Regression test: Crossref returns `"date-parts": [[None]]` for some
    incomplete records (a non-empty inner list whose first element is still
    None) -- caught live during the 2026-08-23 500-request campaign, where
    `_extract_year` crashed with TypeError instead of treating it as an
    unknown year.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "message": {
                    "total-results": 1,
                    "items": [
                        {
                            "DOI": "10.2/incomplete",
                            "title": ["Untitled Preprint"],
                            "author": [],
                            "published": {"date-parts": [[None]]},
                            "URL": "https://example.com/crossref-incomplete",
                        }
                    ],
                }
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = CrossrefAdapter(client)

    response = adapter.search(_query(), page_cursor=None)

    hit = response.hits[0]
    assert hit.doi == "10.2/incomplete"
    assert hit.publication_year is None
