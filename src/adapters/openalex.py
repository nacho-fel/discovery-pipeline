"""OpenAlex adapter -- keyless, documented JSON REST API (`api.openalex.org/works`).

`contact_email` joins OpenAlex's "polite pool" (higher, documented rate
limits) per their own etiquette convention; it is never a secret and is safe
to log/cache.
"""

import httpx

from discovery.adapters.base import RetryableAdapterError, raise_for_httpx_response
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import PlannedQuery

OPENALEX_BASE_URL = "https://api.openalex.org/works"
_PER_PAGE = 25


class OpenAlexAdapter:
    """Adapter over the OpenAlex Works API."""

    name = "openalex"

    def __init__(self, client: httpx.Client, contact_email: str = ""):
        self._client = client
        self._contact_email = contact_email

    def search(self, query: PlannedQuery, *, page_cursor: str | None) -> AdapterSearchResponse:
        page = int(page_cursor) if page_cursor else 1
        params: dict[str, str | int] = {
            "search": query.rendered_query,
            "per-page": _PER_PAGE,
            "page": page,
        }
        if self._contact_email:
            params["mailto"] = self._contact_email

        try:
            response = self._client.get(OPENALEX_BASE_URL, params=params)
        except httpx.TimeoutException as exc:
            raise RetryableAdapterError(f"OpenAlex timeout: {exc}", error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableAdapterError(
                f"OpenAlex transport error: {exc}", error_type="transport_error"
            ) from exc

        raise_for_httpx_response(response, provider="openalex")
        payload = response.json()

        hits: list[RawSearchHit] = []
        for index, item in enumerate(payload.get("results", [])):
            authors = [
                a.get("author", {}).get("display_name")
                for a in item.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]
            primary_location = item.get("primary_location") or {}
            open_access = item.get("open_access") or {}
            url = open_access.get("oa_url") or (primary_location.get("landing_page_url"))

            hits.append(
                RawSearchHit(
                    title=item.get("title"),
                    url=url,
                    doi=item.get("doi"),
                    authors=authors,
                    publication_year=item.get("publication_year"),
                    provider_result_id=item.get("id"),
                    rank=(page - 1) * _PER_PAGE + index + 1,
                    raw=item,
                )
            )

        meta = payload.get("meta", {})
        total_pages = -(-meta.get("count", 0) // _PER_PAGE) if meta.get("count") else 0
        next_cursor = str(page + 1) if page < total_pages else None

        return AdapterSearchResponse(
            provider_request_id=None,
            result_count=len(hits),
            hits=hits,
            next_page_cursor=next_cursor,
            raw_response=payload,
        )
