"""SerpApi Google Search adapter (`engine=google`).

Used for long-tail reports, PDFs, spreadsheets, theses, and project
disclosures via `site:`/`filetype:`/`intitle:` and named-project queries --
see `query_planner.py`'s Google-targeted templates.
"""

from discovery.adapters.serpapi_common import SerpApiClient
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import PlannedQuery


class SerpApiGoogleAdapter:
    """Adapter over `SerpApiClient` for `engine=google`."""

    name = "serpapi_google"

    def __init__(self, client: SerpApiClient, results_per_page: int = 10):
        self._client = client
        self._results_per_page = results_per_page

    def search(self, query: PlannedQuery, *, page_cursor: str | None) -> AdapterSearchResponse:
        start = int(page_cursor) if page_cursor else 0
        params = {
            "engine": "google",
            "q": query.rendered_query,
            "num": self._results_per_page,
            "start": start,
            "hl": query.language,
        }
        payload = self._client.raw_search(params)

        hits: list[RawSearchHit] = []
        for item in payload.get("organic_results", []):
            hits.append(
                RawSearchHit(
                    title=item.get("title"),
                    url=item.get("link"),
                    snippet=item.get("snippet"),
                    displayed_source=item.get("displayed_link"),
                    provider_result_id=str(item.get("position"))
                    if item.get("position") is not None
                    else None,
                    rank=item.get("position"),
                    raw=item,
                )
            )

        has_next = "next" in payload.get("serpapi_pagination", {})
        next_cursor = str(start + self._results_per_page) if has_next else None

        return AdapterSearchResponse(
            provider_request_id=payload.get("search_metadata", {}).get("id"),
            result_count=len(hits),
            hits=hits,
            next_page_cursor=next_cursor,
            raw_response=payload,
        )
