"""SerpApi Google Scholar adapter (`engine=google_scholar`).

Captures citation/cited-by/related-work/alternate-version metadata where
Scholar exposes it (`inline_links.cited_by`, `inline_links.versions`), so
`frontier.py` can expand from a strong candidate without a second API call
just to discover its cluster/version id.
"""

from discovery.adapters.serpapi_common import SerpApiClient
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import PlannedQuery


class SerpApiScholarAdapter:
    """Adapter over `SerpApiClient` for `engine=google_scholar`."""

    name = "serpapi_scholar"

    def __init__(self, client: SerpApiClient, results_per_page: int = 10):
        self._client = client
        self._results_per_page = results_per_page

    def search(self, query: PlannedQuery, *, page_cursor: str | None) -> AdapterSearchResponse:
        start = int(page_cursor) if page_cursor else 0
        params = {
            "engine": "google_scholar",
            "q": query.rendered_query,
            "start": start,
            "hl": query.language,
        }
        payload = self._client.raw_search(params)

        hits: list[RawSearchHit] = []
        for item in payload.get("organic_results", []):
            publication_info = item.get("publication_info", {}) or {}
            inline_links = item.get("inline_links", {}) or {}
            cited_by = inline_links.get("cited_by", {}) or {}
            versions = inline_links.get("versions", {}) or {}

            hits.append(
                RawSearchHit(
                    title=item.get("title"),
                    url=item.get("link"),
                    snippet=item.get("snippet"),
                    publication_info=publication_info.get("summary"),
                    provider_result_id=item.get("result_id"),
                    cited_by_id=cited_by.get("cites_id"),
                    cluster_version_id=versions.get("cluster_id"),
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
