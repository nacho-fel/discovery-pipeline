"""Acquisition safety tests. `is_safe_url`'s DNS resolution is always faked
via an injected `resolver` -- these tests never touch the real network,
matching the repo-wide "no live calls during tests" rule.
"""

import os
from pathlib import Path

import httpx
import pytest

from discovery.acquirer import (
    _WINDOWS_MAX_SAFE_PATH_LENGTH,
    RequestBudgetExhaustedError,
    SharedRequestBudget,
    acquire,
    check_output_path_safety,
    is_safe_url,
)

_PDF_BYTES = b"%PDF-1.4\n%fake pdf content for testing\n"


def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def _private_resolver(host, port):
    return [(0, 0, 0, "", ("10.0.0.5", 0))]


def _loopback_resolver(host, port):
    return [(0, 0, 0, "", ("127.0.0.1", 0))]


def test_is_safe_url_allows_public_address():
    safe, reason = is_safe_url("https://example.com/report.pdf", resolver=_public_resolver)
    assert safe is True
    assert reason is None


def test_is_safe_url_blocks_private_address():
    safe, reason = is_safe_url("https://internal.example/report.pdf", resolver=_private_resolver)
    assert safe is False
    assert "disallowed address" in reason


def test_is_safe_url_blocks_loopback():
    safe, _reason = is_safe_url("http://localhost/report.pdf", resolver=_loopback_resolver)
    assert safe is False


def test_is_safe_url_blocks_non_http_scheme():
    safe, reason = is_safe_url("file:///etc/passwd", resolver=_public_resolver)
    assert safe is False
    assert "scheme" in reason


def test_acquire_blocked_by_safety_policy_for_ssrf_target(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never issue a request for an unsafe URL")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "http://169.254.169.254/latest/meta-data/",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=lambda host, port: [(0, 0, 0, "", ("169.254.169.254", 0))],
    )
    assert outcome.status == "blocked_by_safety_policy"
    assert outcome.error_type == "unsafe_url"


def test_acquire_succeeds_for_valid_pdf(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"
    assert outcome.sha256 is not None
    assert outcome.local_path is not None
    from pathlib import Path

    assert Path(outcome.local_path).read_bytes() == _PDF_BYTES


def test_acquire_rejects_content_length_over_limit(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "10000000"},
            content=_PDF_BYTES,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/huge.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "content_too_large"


def test_acquire_rejects_when_stream_exceeds_limit_despite_missing_content_length(tmp_path):
    big_body = b"%PDF-1.4\n" + (b"x" * 5000)

    def handler(request: httpx.Request) -> httpx.Response:
        # No content-length header -- server "lied" / omitted it; the
        # mid-stream byte counter must still catch this.
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=big_body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/huge2.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=100,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "content_too_large"
    assert not any(tmp_path.glob(".tmp-*"))  # temp file cleaned up


def test_acquire_rejects_magic_byte_mismatch(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"NOT A REAL PDF FILE"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/fake.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "magic_byte_mismatch"


def test_acquire_rejects_unsupported_content_type(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/file.zip",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "unsupported_content_type"


def test_acquire_treats_403_as_paywalled(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/paywalled.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "paywalled_or_forbidden"


def test_acquire_follows_safe_redirect(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == "https://example.com/landing":
            return httpx.Response(302, headers={"location": "https://example.com/final.pdf"})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/landing",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"
    assert len(calls) == 2


def test_shared_request_budget_consume_raises_once_exhausted():
    budget = SharedRequestBudget(2)
    budget.consume()
    budget.consume()
    with pytest.raises(RequestBudgetExhaustedError):
        budget.consume()
    assert budget.used == 2  # the failed 3rd consume() never incremented it


# --- allowed_extensions (per-candidate declared-format enforcement) --------


def test_acquire_rejects_html_when_only_pdf_is_declared(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>not a pdf</html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/landing.html",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"pdf"}),
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "declared_format_mismatch"
    assert outcome.retryable is False
    assert outcome.local_path is None
    assert list(tmp_path.iterdir()) == []  # no file, not even a temp file, was ever written


def test_acquire_rejects_pdf_when_only_html_is_declared(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"html"}),
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "declared_format_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_acquire_accepts_pdf_when_html_or_pdf_is_declared(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"html", "pdf"}),
    )
    assert outcome.status == "succeeded"


def test_acquire_accepts_html_when_html_or_pdf_is_declared(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>real article</html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/article",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"html", "pdf"}),
    )
    assert outcome.status == "succeeded"


def test_acquire_without_allowed_extensions_keeps_the_original_global_allowlist_behavior(tmp_path):
    """Regression guard: allowed_extensions=None (the default) must behave
    exactly like acquire() did before this parameter existed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html>ok</html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/whatever",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"  # html is in the global allowlist regardless


def test_acquire_still_checks_magic_bytes_after_a_declared_format_matches(tmp_path):
    """A server that claims content-type: application/pdf (a type this
    candidate declared as acceptable) but whose body isn't really a PDF must
    still be rejected by the existing magic-byte check -- the new declared-
    format gate must not short-circuit it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"NOT A REAL PDF"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/fake.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"pdf"}),
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "magic_byte_mismatch"


# --- cross-signal format agreement: URL extension / Content-Disposition ----


def test_acquire_rejects_when_url_extension_contradicts_resolved_mime(tmp_path):
    """The URL claims .pdf but the server's content-type resolves to html --
    a present, contradictory extension must fail closed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html><body>ok</body></html>"
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "url_extension_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_acquire_rejects_when_content_disposition_extension_contradicts_resolved_mime(tmp_path):
    """Content-Disposition claims a .xlsx filename but the body is a PDF --
    a present, contradictory extension must fail closed.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="data.xlsx"',
            },
            content=_PDF_BYTES,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/download",  # no extension -- isolates the CD check
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "content_disposition_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_acquire_accepts_a_valid_pdf_when_the_url_has_no_recognized_extension(tmp_path):
    """Absence of a URL/Content-Disposition extension is accepted, not
    treated as a contradiction -- many real download endpoints have no file
    extension in their path at all (query-string-driven, .php, .aspx, ...).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/download?id=123",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"


# --- HTML body structural validation ----------------------------------------


def test_acquire_rejects_a_fake_body_mislabeled_text_html(tmp_path):
    """A response claiming text/html whose body has no plausible HTML
    structure must fail without leaving a file -- the old decode-only check
    would have accepted any decodable text as 'html'.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"this is just plain text, not a real HTML document at all",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/article",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "html_signature_mismatch"
    assert outcome.local_path is None
    assert list(tmp_path.iterdir()) == []


def test_acquire_accepts_a_genuine_html_body(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<!DOCTYPE html><html><head><title>Real</title></head><body>content</body></html>",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/article",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"


def test_acquire_stops_mid_redirect_chain_when_shared_budget_runs_out_before_the_per_candidate_cap(
    tmp_path,
):
    """A tight shared budget (3) cuts a single candidate's redirect chain off
    partway through -- well before acquirer's own per-candidate cap of 5
    redirects (6 requests) would have. This is the general mechanism a
    two-candidate pilot's shared ceiling relies on, isolated from any
    pilot-specific constant.
    """
    calls = []

    def always_redirect(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": str(request.url) + "x"})

    client = httpx.Client(transport=httpx.MockTransport(always_redirect))
    budget = SharedRequestBudget(3)
    outcome = acquire(
        "https://example.com/start",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        request_budget=budget,
    )

    assert len(calls) == 3  # not 6 -- the shared budget, not the redirect cap, stopped it
    assert budget.used == 3
    assert outcome.status == "blocked_by_safety_policy"
    assert outcome.error_type == "request_budget_exhausted"


def test_acquire_without_a_budget_is_unaffected(tmp_path):
    """Existing callers that don't pass `request_budget` see no behavior
    change -- the parameter is purely additive.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "succeeded"


def test_acquire_blocks_redirect_to_unsafe_target(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/landing":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})
        raise AssertionError("must never follow the unsafe redirect target")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def resolver(host, port):
        if host == "169.254.169.254":
            return [(0, 0, 0, "", ("169.254.169.254", 0))]
        return [(0, 0, 0, "", ("93.184.216.34", 0))]

    outcome = acquire(
        "https://example.com/landing",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=resolver,
    )
    assert outcome.status == "blocked_by_safety_policy"


# --- 2026-08-22 crash-resilience correction: pre-network output-path safety
# and structured finalization-failure handling ------------------------------

_WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="check_output_path_safety only enforces on Windows (os.name == 'nt')"
)


def _dest_dir_of_length(total_length: int) -> Path:
    """A synthetic Path whose str() is exactly `total_length` characters --
    check_output_path_safety only measures string length, never touches the
    filesystem, so this doesn't need to be a real, creatable directory.
    """
    prefix = "C:\\"
    filler = "a" * (total_length - len(prefix))
    return Path(prefix + filler)


@_WINDOWS_ONLY
def test_check_output_path_safety_rejects_an_overlong_worst_case_path():
    # worst case with allowed_extensions={"pdf"}: 64 hex + "." + "pdf" = 68,
    # so final_path_length = len(dest_dir) + 1 + 68. One character past the
    # boundary that keeps that at _WINDOWS_MAX_SAFE_PATH_LENGTH.
    dest_dir = _dest_dir_of_length(_WINDOWS_MAX_SAFE_PATH_LENGTH - 69 + 2)
    outcome = check_output_path_safety(dest_dir, allowed_extensions=frozenset({"pdf"}))
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.error_type == "output_path_too_long"
    assert outcome.retryable is False
    assert f"safe_limit={_WINDOWS_MAX_SAFE_PATH_LENGTH}" in outcome.error_message
    assert str(dest_dir) in outcome.error_message
    assert "calculated_path_length=" in outcome.error_message


@_WINDOWS_ONLY
def test_check_output_path_safety_accepts_a_boundary_length_safe_path():
    # Exactly at the safe limit -- must be accepted (<=, not <).
    dest_dir = _dest_dir_of_length(_WINDOWS_MAX_SAFE_PATH_LENGTH - 69)
    outcome = check_output_path_safety(dest_dir, allowed_extensions=frozenset({"pdf"}))
    assert outcome is None


@_WINDOWS_ONLY
def test_check_output_path_safety_uses_the_global_allowlists_longest_extension_when_unconstrained():
    # allowed_extensions=None -- must use the longest extension across the
    # *global* content-type allowlist (4, for "html"/"xlsx"), not assume "pdf".
    dest_dir = _dest_dir_of_length(_WINDOWS_MAX_SAFE_PATH_LENGTH - 70 + 2)
    outcome = check_output_path_safety(dest_dir, allowed_extensions=None)
    assert outcome is not None
    assert outcome.error_type == "output_path_too_long"


@_WINDOWS_ONLY
def test_check_output_path_safety_resolves_a_relative_dest_dir_before_measuring():
    """A *relative* dest_dir (exactly how a CLI --download-dir argument is
    typically passed, e.g. "data/pilots/...") must be measured by its
    resolved absolute length, not its raw string length -- a relative
    string can look comfortably short while resolving (against the
    process's CWD, the same resolution `open()`/`os.replace()` perform)
    to something well over the limit. This is the exact gap that would
    have silently under-counted and let an unsafe path through.
    """
    cwd_length = len(str(Path.cwd()))
    # Sized so the raw relative string alone looks safe (only ~150 chars),
    # but cwd_length + 1 + this + 1 + 68 (worst-case pdf filename) exceeds
    # the safe limit once actually resolved.
    relative_component_length = max(
        150, _WINDOWS_MAX_SAFE_PATH_LENGTH - cwd_length - 1 - 68 + 10
    )
    relative_dest_dir = Path("relative_test_dir_" + ("y" * relative_component_length))
    assert not relative_dest_dir.is_absolute()

    outcome = check_output_path_safety(relative_dest_dir, allowed_extensions=frozenset({"pdf"}))
    assert outcome is not None
    assert outcome.error_type == "output_path_too_long"
    # The reported diagnostic must reflect the *resolved* (absolute) form.
    assert str(Path.cwd()) in outcome.error_message
    assert not relative_dest_dir.is_absolute()  # the original caller-supplied Path is untouched


def test_acquire_rejects_overlong_output_path_before_any_http_request(tmp_path):
    """The path-safety check must run before is_safe_url, before budget
    consumption, before any HTTP call -- this test's handler raises if
    called at all, and (on Windows) the overlong dest_dir must still be
    rejected without ever reaching it.
    """
    if os.name != "nt":
        pytest.skip("output_path_too_long only enforces on Windows")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never issue a request when the output path is unsafe")

    overlong_dir = tmp_path / ("x" * 250)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = SharedRequestBudget(10)
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=overlong_dir,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        request_budget=budget,
        allowed_extensions=frozenset({"pdf"}),
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "output_path_too_long"
    assert budget.used == 0  # no request was ever consumed from the budget


def test_acquire_short_dest_dir_is_unaffected_by_the_path_safety_check(tmp_path):
    """Regression guard: pytest's tmp_path (always short) must never trip
    the new pre-flight check -- every existing successful-acquisition test
    in this file already proves this implicitly; this test makes it explicit
    and also confirms the resulting path is comfortably short.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        allowed_extensions=frozenset({"pdf"}),
    )
    assert outcome.status == "succeeded"
    assert len(outcome.local_path) < _WINDOWS_MAX_SAFE_PATH_LENGTH


def test_acquire_converts_finalization_oserror_to_structured_failure(tmp_path, monkeypatch):
    """A filesystem-level failure at the final tmp-to-final rename step (a
    full disk, a permission error, a file lock -- any OSError cause other
    than the path-length case check_output_path_safety already screens out
    ahead of time) must become a structured failed AcquisitionOutcome, never
    an unhandled exception -- preserving the already-computed SHA-256, byte
    count, and content-type even though finalization itself failed. The tmp
    file is intentionally left in place (quarantined), not deleted -- its
    content already passed every validation check.
    """

    def _raise_replace(self, target):
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "replace", _raise_replace)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = acquire(
        "https://example.com/report.pdf",
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )
    assert outcome.status == "failed"
    assert outcome.error_type == "file_finalization_failed"
    assert outcome.retryable is False
    assert outcome.sha256 is not None  # computed before the failed rename
    assert outcome.bytes_downloaded == len(_PDF_BYTES)
    assert outcome.content_type == "application/pdf"
    assert "simulated disk full" in outcome.error_message
    # Quarantined, not deleted: the tmp file still exists on disk.
    quarantined = list(tmp_path.glob(".tmp-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == _PDF_BYTES
    # And no file was ever written under the (never-reached) final name.
    assert not any(f.suffix == ".pdf" for f in tmp_path.iterdir())
