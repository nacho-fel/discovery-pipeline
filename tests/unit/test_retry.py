import pytest

from discovery.adapters.base import (
    NonRetryableAdapterError,
    RetryableAdapterError,
    RetryPolicy,
    call_with_retry,
    compute_backoff_delay,
    current_attempt_context,
)
from discovery.budget import SharedRequestBudget


def test_call_with_retry_succeeds_after_transient_failures():
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RetryableAdapterError("transient", error_type="timeout")
        return "ok"

    sleeps = []
    result = call_with_retry(
        flaky, policy=RetryPolicy(max_retries=5, base_delay_seconds=0.01, max_delay_seconds=0.02), sleep_func=sleeps.append
    )
    assert result == "ok"
    assert attempts["count"] == 3
    assert len(sleeps) == 2  # slept before attempt 2 and 3


def test_call_with_retry_gives_up_after_max_retries():
    def always_fails():
        raise RetryableAdapterError("still failing", error_type="timeout")

    with pytest.raises(RetryableAdapterError):
        call_with_retry(
            always_fails,
            policy=RetryPolicy(max_retries=2, base_delay_seconds=0.01, max_delay_seconds=0.02),
            sleep_func=lambda _seconds: None,
        )


def test_call_with_retry_does_not_retry_nonretryable():
    calls = {"count": 0}

    def auth_fails():
        calls["count"] += 1
        raise NonRetryableAdapterError("auth", error_type="auth_error")

    with pytest.raises(NonRetryableAdapterError):
        call_with_retry(auth_fails, policy=RetryPolicy(max_retries=5), sleep_func=lambda _s: None)
    assert calls["count"] == 1


def test_call_with_retry_budget_counts_every_physical_attempt_including_retries():
    """Regression test: quota accounting must count the initial attempt AND
    every retry as a real physical request -- a query that retries twice
    before succeeding is 3 real outbound requests against SerpApi's actual
    quota, not the 1 a flat per-query counter would record.
    """
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RetryableAdapterError("transient", error_type="timeout")
        return "ok"

    budget = SharedRequestBudget(10)
    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_retries=5, base_delay_seconds=0.01, max_delay_seconds=0.02),
        sleep_func=lambda _s: None,
        request_budget=budget,
    )
    assert result == "ok"
    assert budget.used == 3  # 2 failures + 1 success, all physical requests


def test_call_with_retry_stops_immediately_when_budget_exhausted_mid_retry():
    calls = {"count": 0}

    def always_transient_failure():
        calls["count"] += 1
        raise RetryableAdapterError("transient", error_type="timeout")

    budget = SharedRequestBudget(2)
    with pytest.raises(NonRetryableAdapterError) as exc_info:
        call_with_retry(
            always_transient_failure,
            policy=RetryPolicy(max_retries=5, base_delay_seconds=0.01, max_delay_seconds=0.02),
            sleep_func=lambda _s: None,
            request_budget=budget,
        )
    assert exc_info.value.error_type == "quota_exhausted"
    assert calls["count"] == 2  # the 3rd physical attempt never happened
    assert budget.used == 2


def test_call_with_retry_publishes_attempt_context_per_physical_attempt():
    """`current_attempt_context` must read "initial" on the first physical
    attempt and "retry" (with the prior error's type as retry_reason) on
    every attempt after -- this is what lets SerpApiClient.raw_search
    attribute each ledger reservation to the right batch/family/attempt
    kind without changing every adapter's search() signature.
    """
    observed = []

    def flaky():
        observed.append(current_attempt_context.get())
        if len(observed) < 3:
            raise RetryableAdapterError("transient", error_type="timeout")
        return "ok"

    result = call_with_retry(
        flaky,
        policy=RetryPolicy(max_retries=5, base_delay_seconds=0.01, max_delay_seconds=0.02),
        sleep_func=lambda _s: None,
        batch_number=7,
        query_family="broad_domain",
        query_fingerprint="fp-xyz",
        discovery_run_id="run-1",
    )

    assert result == "ok"
    assert len(observed) == 3
    assert observed[0].attempt_kind == "initial"
    assert observed[0].retry_reason is None
    assert observed[1].attempt_kind == "retry"
    assert observed[1].retry_reason == "timeout"
    assert observed[2].attempt_kind == "retry"
    assert all(ctx.batch_number == 7 for ctx in observed)
    assert all(ctx.query_family == "broad_domain" for ctx in observed)
    assert all(ctx.query_fingerprint == "fp-xyz" for ctx in observed)
    # The context must not leak outside call_with_retry once it returns.
    assert current_attempt_context.get() is None


def test_compute_backoff_delay_honors_retry_after():
    policy = RetryPolicy(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=60.0)
    delay = compute_backoff_delay(policy, attempt=1, retry_after_seconds=30.0)
    assert delay == 30.0


def test_compute_backoff_delay_caps_at_max_delay():
    policy = RetryPolicy(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=5.0)
    delay = compute_backoff_delay(policy, attempt=10, retry_after_seconds=None)
    assert delay <= 5.0


def test_compute_backoff_delay_is_jittered_within_bounds():
    policy = RetryPolicy(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=60.0)
    delay = compute_backoff_delay(policy, attempt=3, retry_after_seconds=None)
    assert 0 <= delay <= 4.0  # base * 2^(3-1) = 4.0 upper bound
