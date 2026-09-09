"""Shared SerpApi request/response handling for the Google and Scholar adapters.

SerpApi echoes the caller's `api_key` back inside several `search_metadata.*`
URL fields (`json_endpoint`, `raw_html_file`, `prettify_html_file`, ...). This
module strips it from every response before it is cached to disk, persisted
as `raw_response_json`, or returned to a caller -- structural redaction at the
one place every SerpApi response passes through, not a scattered post-hoc
scrub. See `discovery.adapters.serpapi_google`/`serpapi_scholar` for the
engine-specific query shape.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from discovery.adapters.base import (
    NonRetryableAdapterError,
    RetryableAdapterError,
    current_attempt_context,
    raise_for_httpx_response,
)

logger = logging.getLogger(__name__)

SERPAPI_BASE_URL = "https://serpapi.com/search.json"


def _redact_api_key_from_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "api_key"]
    return urlunparse(parsed._replace(query=urlencode(kept)))


def redact_secrets(value: object) -> object:
    """Recursively strip `api_key=...` from any URL-shaped string in a JSON value."""
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str) and "api_key=" in value:
        return _redact_api_key_from_url(value)
    return value


def cache_key(params: dict) -> str:
    """Deterministic cache key over request params, excluding the secret itself."""
    safe_params = {k: v for k, v in sorted(params.items()) if k != "api_key"}
    payload = json.dumps(safe_params, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SerpApiClient:
    """Thin, cache-aware SerpApi HTTP client.

    `client` is an injected `httpx.Client` (production: a real client;
    tests: one built on `httpx.MockTransport`) -- this is the sole HTTP
    boundary this class ever crosses.
    """

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        cache_dir: Path | None = None,
        *,
        quota_reserve: Callable[..., object] | None = None,
    ):
        self._api_key = api_key
        self._client = client
        self._cache_dir = cache_dir
        # Called with (query_fingerprint=, query_family=, batch_number=,
        # discovery_run_id=, attempt_kind=, retry_reason=) immediately
        # before the real HTTP GET below -- i.e. strictly *after* the
        # cache-hit check, so a cache hit never spends campaign quota. See
        # `adapters/base.py`'s `current_attempt_context` for how the
        # per-call metadata reaches this point. Raising `QuotaExhaustedError`
        # here is translated into `NonRetryableAdapterError` so the request
        # is never made and never retried.
        self._quota_reserve = quota_reserve

    def raw_search(self, params: dict, *, force_refresh: bool = False) -> dict:
        """GET `search.json` with `params` (api_key attached here, never by the
        caller), consulting/populating the on-disk cache first unless
        `force_refresh` is set. Never passes `no_cache=true` unless the
        caller explicitly set it -- exact requests are cached by default.

        A cache hit returns immediately, before any quota reservation --
        "never pay twice for the same normalized query" means a repeated
        query must cost zero campaign quota, not just zero HTTP latency.
        """
        full_params = {**params, "api_key": self._api_key}
        key = cache_key(full_params)

        if not force_refresh and self._cache_dir is not None:
            cache_path = self._cache_dir / f"{key}.json"
            if cache_path.exists():
                logger.info("serpapi.cache_hit", extra={"cache_key": key})
                cached: dict = json.loads(cache_path.read_text(encoding="utf-8"))
                return cached

        if self._quota_reserve is not None:
            from discovery.quota_ledger import QuotaExhaustedError

            ctx = current_attempt_context.get()
            try:
                self._quota_reserve(
                    query_fingerprint=(ctx.query_fingerprint if ctx else None) or key,
                    query_family=ctx.query_family if ctx else None,
                    batch_number=ctx.batch_number if ctx else None,
                    discovery_run_id=ctx.discovery_run_id if ctx else None,
                    attempt_kind=ctx.attempt_kind if ctx else "initial",
                    retry_reason=ctx.retry_reason if ctx else None,
                )
            except QuotaExhaustedError as exc:
                raise NonRetryableAdapterError(str(exc), error_type="quota_exhausted") from exc

        try:
            response = self._client.get(SERPAPI_BASE_URL, params=full_params)
        except httpx.TimeoutException as exc:
            raise RetryableAdapterError(f"SerpApi timeout: {exc}", error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableAdapterError(
                f"SerpApi transport error: {exc}", error_type="transport_error"
            ) from exc

        raise_for_httpx_response(response, provider="serpapi")
        payload: dict = response.json()
        if payload.get("search_metadata", {}).get("status") == "Error":
            # A 200 response whose *body* reports failure is far more often a
            # malformed query/parameter problem than a transient one --
            # genuinely transient provider trouble (rate limits, 5xx) is
            # already classified separately above via HTTP status. Retrying
            # a malformed query blindly just spends quota reproducing the
            # same failure, so this is deliberately NonRetryable, not
            # RetryableAdapterError as it was before this audit.
            raise NonRetryableAdapterError(
                f"SerpApi reported an error: {payload.get('error', 'unknown')}",
                error_type="provider_error",
            )

        # A top-level SerpApi response is always a JSON object; redact_secrets
        # preserves the dict shape at the top level, only recursing into it.
        payload = cast(dict, redact_secrets(payload))

        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = self._cache_dir / f"{key}.json"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")

        return payload
