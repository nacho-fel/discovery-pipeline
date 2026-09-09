"""Crossref adapter -- keyless, documented JSON REST API (`api.crossref.org/works`)."""

import httpx

from discovery.adapters.base import RetryableAdapterError, raise_for_httpx_response
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import PlannedQuery

CROSSREF_BASE_URL = "https://api.crossref.org/works"
_ROWS = 25


def _extract_year(item: dict) -> int | None:
    for key in ("published-print", "published-online", "published", "issued"):
        date_parts = (item.get(key) or {}).get("date-parts")
        # Crossref sometimes returns incomplete records as `[[None]]` (an
        # inner list that is non-empty but whose first element is still
        # None) -- caught live during the 2026-08-23 500-request campaign,
        # where it crashed with TypeError and burned a full retry budget
        # against the real API for a query that could never succeed.
        if date_parts and date_parts[0] and date_parts[0][0] is not None:
            return int(date_parts[0][0])
    return None


class CrossrefAdapter:
    """Adapter over the Crossref Works API."""

    name = "crossref"

    def __init__(self, client: httpx.Client, contact_email: str = ""):
        self._client = client
        self._contact_email = contact_email

    def search(self, query: PlannedQuery, *, page_cursor: str | None) -> AdapterSearchResponse:
        offset = int(page_cursor) if page_cursor else 0
        params: dict[str, str | int] = {
            "query": query.rendered_query,
            "rows": _ROWS,
            "offset": offset,
        }
        if self._contact_email:
            params["mailto"] = self._contact_email

        try:
            response = self._client.get(CROSSREF_BASE_URL, params=params)
        except httpx.TimeoutException as exc:
            raise RetryableAdapterError(f"Crossref timeout: {exc}", error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableAdapterError(
                f"Crossref transport error: {exc}", error_type="transport_error"
            ) from exc

        raise_for_httpx_response(response, provider="crossref")
        payload = response.json()
        message = payload.get("message", {})

        hits: list[RawSearchHit] = []
        for index, item in enumerate(message.get("items", [])):
            titles = item.get("title") or []
            authors = [
                " ".join(part for part in (a.get("given"), a.get("family")) if part)
                for a in item.get("author", [])
            ]
            hits.append(
                RawSearchHit(
                    title=titles[0] if titles else None,
                    url=item.get("URL"),
                    doi=item.get("DOI"),
                    authors=[a for a in authors if a],
                    publication_year=_extract_year(item),
                    provider_result_id=item.get("DOI"),
                    rank=offset + index + 1,
                    raw=item,
                )
            )

        total_results = message.get("total-results", 0)
        next_offset = offset + _ROWS
        next_cursor = str(next_offset) if next_offset < total_results else None

        return AdapterSearchResponse(
            provider_request_id=None,
            result_count=len(hits),
            hits=hits,
            next_page_cursor=next_cursor,
            raw_response=payload,
        )
