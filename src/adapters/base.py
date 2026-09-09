"""Shared adapter protocol, HTTP client injection, and retry/backoff.

`httpx.Client` (real or built on `httpx.MockTransport`) is injected into every
adapter constructor -- this is the one HTTP boundary every offline test fakes,
continuing the legacy prototype's "inject a fake transport" test strategy but
via `httpx`'s own supported mechanism instead of a hand-rolled `Protocol`.
"""

import contextvars
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx
import tenacity

from discovery.models.candidate import AdapterSearchResponse
from discovery.models.query import PlannedQuery

if TYPE_CHECKING:
    from discovery.budget import SharedRequestBudget

logger = logging.getLogger(__name__)


@dataclass
class AttemptContext:
    """Metadata describing *this* physical attempt -- set by `call_with_retry`
    immediately before each call to the wrapped function, read by
    `SerpApiClient.raw_search` (see `adapters/serpapi_common.py`) at the
    exact point it's about to make a real outbound HTTP request, so the
    persistent quota ledger's per-request audit fields (query family,
    batch, attempt kind, retry reason) can be populated without changing
    the `SearchAdapter.search()` Protocol signature that every adapter
    (including OpenAlex/Crossref/seed_import, which don't care about any of
    this) implements. A `contextvars.ContextVar` rather than a plain
    module-global so this stays correct if this code is ever driven from
    multiple threads/async tasks concurrently.
    """

    attempt_kind: str = "initial"  # "initial" | "retry"
    retry_reason: str | None = None
    batch_number: int | None = None
    query_family: str | None = None
    query_fingerprint: str | None = None
    discovery_run_id: str | None = None


current_attempt_context: contextvars.ContextVar["AttemptContext | None"] = contextvars.ContextVar(
    "current_attempt_context", default=None
)


class AdapterError(Exception):
    """Base class for adapter failures."""

    def __init__(self, message: str, *, retryable: bool, error_type: str):
        super().__init__(message)
        self.retryable = retryable
        self.error_type = error_type


class RetryableAdapterError(AdapterError):
    """A transient failure (timeout, 5xx, rate-limit) worth retrying."""

    def __init__(self, message: str, *, error_type: str, retry_after_seconds: float | None = None):
        super().__init__(message, retryable=True, error_type=error_type)
        self.retry_after_seconds = retry_after_seconds


class NonRetryableAdapterError(AdapterError):
    """A permanent failure (auth, malformed request, parse error) -- do not retry."""

    def __init__(self, message: str, *, error_type: str):
        super().__init__(message, retryable=False, error_type=error_type)


class SearchAdapter(Protocol):
    """One discovery-channel adapter. Every concrete adapter implements this."""

    name: str

    def search(self, query: PlannedQuery, *, page_cursor: str | None) -> AdapterSearchResponse:
        """Execute one request for `query`, starting at `page_cursor` (None = first page).

        Raises `RetryableAdapterError` or `NonRetryableAdapterError` on
        failure; never raises a bare/unstructured exception for a network or
        parse failure, so the orchestrator's circuit breaker can distinguish
        operational failures correctly.
        """
        ...


@dataclass
class RetryPolicy:
    """Exponential backoff with jitter, capped, and `Retry-After`-aware."""

    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0


def compute_backoff_delay(policy: RetryPolicy, attempt: int, retry_after_seconds: float | None) -> float:
    """Delay before `attempt` (1-indexed retry attempt), honoring `Retry-After` if given."""
    if retry_after_seconds is not None:
        return min(retry_after_seconds, policy.max_delay_seconds)
    exponential = policy.base_delay_seconds * (2 ** (attempt - 1))
    capped = min(exponential, policy.max_delay_seconds)
    # Full jitter (0..capped), per the brief's "exponential backoff with jitter".
    return random.uniform(0, capped)


def call_with_retry(
    fn: Callable[[], AdapterSearchResponse],
    *,
    policy: RetryPolicy,
    sleep_func: Callable[[float], None] | None = None,
    request_budget: "SharedRequestBudget | None" = None,
    batch_number: int | None = None,
    query_family: str | None = None,
    query_fingerprint: str | None = None,
    discovery_run_id: str | None = None,
) -> AdapterSearchResponse:
    """Call `fn()` (zero-arg, returns AdapterSearchResponse or raises AdapterError),
    retrying `RetryableAdapterError`s up to `policy.max_retries` times with
    backoff via `tenacity`. `NonRetryableAdapterError` propagates immediately.
    Once attempts are exhausted, the last `RetryableAdapterError` propagates
    (tenacity's `reraise=True`), so the caller's circuit breaker sees the true
    final outcome rather than tenacity's own `RetryError` wrapper.

    `request_budget`, if given, is consumed once per *physical* attempt --
    the initial call and every retry, since each is a real outbound SerpApi
    request against actual hourly/monthly quota. A flat "1 request per
    query" counter undercounts this: a query that retries 3 times before
    succeeding is 4 real requests, not 1. If the budget is exhausted
    mid-retry, this raises `NonRetryableAdapterError` (error_type
    "quota_exhausted") immediately -- never retried further, since retrying
    a request the budget forbids would defeat the whole point. This is an
    in-process, best-effort ceiling; `quota_ledger.py`'s persisted,
    cross-process campaign ledger is the authoritative one (see below).

    `batch_number`/`query_family`/`query_fingerprint`/`discovery_run_id`, if
    given, are published via `current_attempt_context` before each physical
    attempt (initial or retry) so `SerpApiClient.raw_search` can pass them
    to `quota_ledger.reserve()` at the exact point it's about to make a real
    HTTP request -- *after* its own cache-hit check, so a cache hit never
    reserves ledger quota. This is deliberately not threaded through `fn`'s
    signature (which every `SearchAdapter.search()` implementation would
    then need to accept) -- see `AttemptContext`'s docstring.
    """
    attempt_count = [0]
    last_error_type: list[str | None] = [None]

    def _budgeted_fn() -> AdapterSearchResponse:
        attempt_count[0] += 1
        kind = "initial" if attempt_count[0] == 1 else "retry"
        ctx = AttemptContext(
            attempt_kind=kind,
            retry_reason=last_error_type[0] if kind == "retry" else None,
            batch_number=batch_number,
            query_family=query_family,
            query_fingerprint=query_fingerprint,
            discovery_run_id=discovery_run_id,
        )
        token = current_attempt_context.set(ctx)
        try:
            if request_budget is not None:
                from discovery.budget import RequestBudgetExhaustedError

                try:
                    request_budget.consume()
                except RequestBudgetExhaustedError as exc:
                    raise NonRetryableAdapterError(str(exc), error_type="quota_exhausted") from exc
            try:
                return fn()
            except AdapterError as exc:
                last_error_type[0] = exc.error_type
                raise
        finally:
            current_attempt_context.reset(token)

    def _wait(retry_state: tenacity.RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        retry_after = getattr(exc, "retry_after_seconds", None)
        delay = compute_backoff_delay(policy, retry_state.attempt_number, retry_after)
        logger.warning(
            "adapter.retry",
            extra={
                "attempt": retry_state.attempt_number,
                "delay_seconds": round(delay, 2),
                "error_type": getattr(exc, "error_type", None),
            },
        )
        return delay

    retryer = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(policy.max_retries + 1),
        wait=_wait,
        retry=tenacity.retry_if_exception_type(RetryableAdapterError),
        sleep=sleep_func or time.sleep,
        reraise=True,
    )
    result: AdapterSearchResponse = retryer(_budgeted_fn)
    return result


def raise_for_httpx_response(response: httpx.Response, *, provider: str) -> None:
    """Translate an httpx response's status into the adapter error hierarchy.

    Nothing about the request (headers, params, API key) is included in any
    raised message -- only status code and provider name.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RetryableAdapterError(
            f"{provider} rate-limited (429)",
            error_type="rate_limited",
            retry_after_seconds=float(retry_after) if retry_after else None,
        )
    if response.status_code in (401, 403):
        raise NonRetryableAdapterError(f"{provider} auth failure ({response.status_code})", error_type="auth_error")
    if 500 <= response.status_code < 600:
        raise RetryableAdapterError(f"{provider} server error ({response.status_code})", error_type="http_error")
    if response.status_code >= 400:
        raise NonRetryableAdapterError(f"{provider} client error ({response.status_code})", error_type="http_error")
