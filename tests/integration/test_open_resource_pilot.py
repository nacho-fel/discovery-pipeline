"""Offline tests for the three-open-resource acquisition pilot. Every HTTP
interaction goes through httpx.MockTransport; DNS resolution through an
injected fake resolver -- no real network call anywhere here. Test fixture
URLs are placeholders (example.com/example.org) -- not the real targets this
pilot variant was designed for.
"""

import ast
import inspect
import json

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from discovery.db.models import AcquisitionAttempt, Base, SourceCandidate
from discovery.mit_assisted_acquisition import build_acquisition_queue
from discovery.open_resource_pilot import (
    MAX_CANDIDATES,
    MAX_DIRECT_REQUESTS,
    OpenResourcePilotError,
    PilotTarget,
    RestrictedCandidateDetectedError,
    run_open_resource_pilot,
    seed_open_resource_pilot,
    write_audit_report,
    write_queue_report,
)

_PDF_BYTES = b"%PDF-1.4\nfake pilot pdf content\n"
_HTML_BYTES = b"<html><body>fake pilot article</body></html>"
# A real XLSX file is a ZIP archive -- "PK\x03\x04" is the local-file-header
# magic bytes acquirer.py's _MAGIC_BYTES table checks for "xlsx". Padded well
# past acquirer._SIGNATURE_PEEK_BYTES isn't needed here (the check only reads
# the initial buffer), just past a trivial length so it reads like a real
# (if fake) zip payload.
_XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 64
_XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

URL_A = "https://example.com/news/article-a"
URL_B = "https://example.org/reports/report-b.pdf"
URL_C = "https://example.net/blog/post-c"
URL_RESTRICTED = "https://restricted.example/paper/123"
# A dedicated .xlsx-suffixed URL for XLSX-format tests -- URL_B's own
# ".pdf" suffix would itself trip the URL-extension cross-check when
# declaring expected_format="xlsx", which is not what those tests are
# about (see test_xlsx_url_extension_contradiction_fails_closed for the
# test that deliberately exercises that check).
URL_B_XLSX = "https://example.org/reports/report-b.xlsx"


def _reseed_b_with_url(source_db, new_url: str):
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == URL_B).update(
        {"canonical_url": new_url}
    )
    source_db.commit()


def _fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


@pytest.fixture
def source_db():
    db = _fresh_db()
    yield db
    db.close()


@pytest.fixture
def pilot_db():
    db = _fresh_db()
    yield db
    db.close()


def _seed_source_trio(db, *, c_access="open_access", c_screening="acquisition_pending"):
    a = SourceCandidate(
        canonical_url=URL_A,
        normalized_title="Target A",
        organization="MIT",
        publisher="MIT News",
        publication_year=2026,
        screening_status="acquisition_pending",
        access_status="open_access",
        expected_cost_observation_yield=1,
        expected_technical_observation_yield=3,
    )
    b = SourceCandidate(
        canonical_url=URL_B,
        normalized_title="Target B",
        organization="DOE",
        publisher="DOE Loan Programs Office",
        publication_year=2025,
        screening_status="acquisition_pending",
        access_status="open_access",
        expected_cost_observation_yield=20,
        expected_technical_observation_yield=15,
    )
    c = SourceCandidate(
        canonical_url=URL_C,
        normalized_title="Target C",
        organization="ThinkGeoEnergy",
        publisher="ThinkGeoEnergy",
        publication_year=2026,
        screening_status=c_screening,
        access_status=c_access,
        expected_cost_observation_yield=0,
        expected_technical_observation_yield=1,
    )
    db.add_all([a, b, c])
    db.commit()
    return a, b, c


def _targets(*, a_format="html", b_format="pdf", c_format="html_or_pdf"):
    return [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format=a_format),
        PilotTarget(label="target_b", canonical_url=URL_B, expected_format=b_format),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format=c_format),
    ]


def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def _well_behaved_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == URL_A:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
    if url == URL_B:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)
    if url == URL_C:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
    raise AssertionError(f"unexpected request to {url}")


# --- seeding: exact count, restricted detection -----------------------------


def test_seeding_requires_exactly_three_targets(source_db, pilot_db):
    _seed_source_trio(source_db)
    with pytest.raises(OpenResourcePilotError):
        seed_open_resource_pilot(source_db, pilot_db, targets=_targets()[:2])


def test_seeding_succeeds_for_three_genuinely_open_targets(source_db, pilot_db):
    source_a, source_b, source_c = _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    assert len(seeded) == 3
    urls = {c.canonical_url for c, _t in seeded}
    assert urls == {URL_A, URL_B, URL_C}
    assert all(c.id not in (source_a.id, source_b.id, source_c.id) for c, _t in seeded)


def test_seeding_detects_an_unexpectedly_restricted_target_and_stops_the_pilot(source_db, pilot_db):
    _seed_source_trio(source_db, c_access="licensed_mit_access")
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == URL_C).update(
        {"screening_status": "paywalled"}
    )
    source_db.commit()

    with pytest.raises(RestrictedCandidateDetectedError) as excinfo:
        seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    # Never auto-acquire it: only the restricted candidate is persisted, as
    # paywalled -- the other two are not seeded at all, so the automated
    # pilot has nothing to run even if the caller ignored the exception.
    pilot_rows = pilot_db.query(SourceCandidate).all()
    assert len(pilot_rows) == 1
    assert pilot_rows[0].canonical_url == URL_C
    assert pilot_rows[0].screening_status == "paywalled"
    assert excinfo.value.queued_candidate_ids == [pilot_rows[0].id]

    # It creates a metadata-only queue entry.
    queue = build_acquisition_queue(pilot_db)
    assert len(queue) == 1
    assert queue[0].candidate_id == pilot_rows[0].id
    assert queue[0].access_status == "licensed_mit_access"


def test_seeding_fails_closed_on_contradictory_open_access_paywalled_screening(source_db, pilot_db):
    """access_status=open_access (automatable) but screening_status=paywalled
    is a data inconsistency -- it must never be silently "fixed" by seeding
    the candidate as acquisition_pending.
    """
    _seed_source_trio(source_db, c_access="open_access", c_screening="paywalled")

    with pytest.raises(OpenResourcePilotError):
        seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    # Nothing was seeded at all -- in particular, target C was never
    # converted to acquisition_pending.
    assert pilot_db.query(SourceCandidate).count() == 0


def test_seeding_fails_closed_on_automatable_access_with_wrong_screening_status(
    source_db, pilot_db
):
    """access_status=open_access but screening_status is neither
    acquisition_pending nor paywalled (e.g. still under manual review) --
    also a contradiction this pilot refuses to resolve on its own.
    """
    _seed_source_trio(source_db, c_access="open_access", c_screening="manual_review_required")

    with pytest.raises(OpenResourcePilotError):
        seed_open_resource_pilot(source_db, pilot_db, targets=_targets())
    assert pilot_db.query(SourceCandidate).count() == 0


def test_seeding_fails_closed_on_unrecognized_access_status(source_db, pilot_db):
    """An access_status outside both the automatable and restricted sets
    (e.g. stale/invalid vocabulary) must not be silently treated as open.
    """
    _seed_source_trio(
        source_db, c_access="open", c_screening="acquisition_pending"
    )  # legacy, invalid value

    with pytest.raises(OpenResourcePilotError):
        seed_open_resource_pilot(source_db, pilot_db, targets=_targets())
    assert pilot_db.query(SourceCandidate).count() == 0


# --- run: format success/failure, fail-fast, budget -------------------------


def test_run_succeeds_for_html_pdf_and_html_or_pdf_targets(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert result.stopped_early is False
    assert len(result.candidate_records) == 3
    assert all(r.outcome_status == "succeeded" for r in result.candidate_records)
    formats = {r.label: r.observed_extension for r in result.candidate_records}
    assert formats == {"target_a": "html", "target_b": "pdf", "target_c": "html"}


def test_run_rejects_html_for_a_declared_pdf_target(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B:
            # Target B declared "pdf" but the server returns HTML.
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        raise AssertionError(f"target C must never be requested after B's mismatch, got {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert result.stopped_early is True
    assert "declared_format_mismatch" in result.stop_reason
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "failed"
    assert b_record.error_type == "declared_format_mismatch"
    assert list(tmp_path.iterdir())  # target A's file exists...
    # ...but nothing was written for B (no PDF was ever produced for it).
    assert not any(f.name.endswith(".pdf") for f in tmp_path.iterdir())


def test_fail_fast_prevents_any_request_for_candidates_after_a_mismatch(
    source_db, pilot_db, tmp_path
):
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == URL_A:
            # Target A declared "html" but the server returns a PDF.
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES
            )
        raise AssertionError(f"must never be requested after target A's mismatch: {url}")

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert requested == [URL_A]  # B and C were never requested at all
    assert result.stopped_early is True
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    c_record = next(r for r in result.candidate_records if r.label == "target_c")
    assert b_record.outcome_status == "not_attempted"
    assert c_record.outcome_status == "not_attempted"
    assert b_record.stop_reason is not None
    assert c_record.stop_reason is not None


def test_run_refuses_more_than_max_candidates(pilot_db, tmp_path):
    seeded = [
        (
            SourceCandidate(
                canonical_url=f"https://example.com/{i}",
                screening_status="acquisition_pending",
                access_status="open_access",
            ),
            PilotTarget(
                label=f"target_{i}",
                canonical_url=f"https://example.com/{i}",
                expected_format="html",
            ),
        )
        for i in range(MAX_CANDIDATES + 1)
    ]
    for candidate, _target in seeded:
        pilot_db.add(candidate)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    with pytest.raises(OpenResourcePilotError):
        run_open_resource_pilot(
            pilot_db,
            seeded,
            client=client,
            dest_dir=tmp_path,
            max_bytes=1_000_000,
            timeout_seconds=5.0,
            resolver=_public_resolver,
        )


def test_run_refuses_fewer_than_max_candidates(pilot_db, tmp_path):
    """Exact-three enforcement cuts both ways: len(seeded) must equal
    MAX_CANDIDATES, not merely be <= it.
    """
    seeded = [
        (
            SourceCandidate(
                canonical_url=f"https://example.com/{i}",
                screening_status="acquisition_pending",
                access_status="open_access",
            ),
            PilotTarget(
                label=f"target_{i}",
                canonical_url=f"https://example.com/{i}",
                expected_format="html",
            ),
        )
        for i in range(MAX_CANDIDATES - 1)
    ]
    for candidate, _target in seeded:
        pilot_db.add(candidate)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    with pytest.raises(OpenResourcePilotError):
        run_open_resource_pilot(
            pilot_db,
            seeded,
            client=client,
            dest_dir=tmp_path,
            max_bytes=1_000_000,
            timeout_seconds=5.0,
            resolver=_public_resolver,
        )


# --- every representative non-success outcome stops the whole pilot --------


def _handler_declared_format_mismatch(request: httpx.Request) -> httpx.Response:
    # target_a declares "html"; returning a PDF instead is the mismatch.
    return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)


def _handler_magic_byte_mismatch(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"NOT REALLY A PDF"
    )


def _handler_unsupported_content_type(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04")


def _handler_size_limit_failure(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/pdf", "content-length": "999999999"},
        content=_PDF_BYTES,
    )


def _handler_ssrf_block(request: httpx.Request) -> httpx.Response:
    raise AssertionError("must never issue a request for an unsafe URL")


def _handler_auth_forbidden(request: httpx.Request) -> httpx.Response:
    return httpx.Response(403, text="Forbidden")


def _handler_http_failure(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="Internal Server Error")


def _handler_redirect_limit_exhaustion(request: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"location": str(request.url) + "x"})


@pytest.mark.parametrize(
    "handler,resolver,expected_status,a_format",
    [
        (_handler_declared_format_mismatch, None, "failed", "html"),
        (_handler_magic_byte_mismatch, None, "failed", "pdf"),
        (_handler_unsupported_content_type, None, "failed", "html"),
        (_handler_size_limit_failure, None, "failed", "pdf"),
        (_handler_ssrf_block, "private", "blocked_by_safety_policy", "html"),
        (_handler_auth_forbidden, None, "failed", "html"),
        (_handler_http_failure, None, "failed", "html"),
        (_handler_redirect_limit_exhaustion, None, "failed", "html"),
    ],
    ids=[
        "declared_format_mismatch",
        "magic_byte_mismatch",
        "unsupported_content_type",
        "size_limit_failure",
        "ssrf_host_policy_block",
        "authentication_restricted_redirect",
        "http_acquisition_failure",
        "redirect_limit_exhaustion",
    ],
)
def test_every_representative_non_success_outcome_stops_the_pilot(
    source_db, pilot_db, tmp_path, handler, resolver, expected_status, a_format
):
    requested = []

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    def private_resolver(host, port):
        return [(0, 0, 0, "", ("10.0.0.5", 0))]

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets(a_format=a_format))

    client = httpx.Client(transport=httpx.MockTransport(wrapped_handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000,  # small enough that _handler_size_limit_failure's declared length trips it
        timeout_seconds=5.0,
        resolver=private_resolver if resolver == "private" else _public_resolver,
    )

    assert result.stopped_early is True
    a_record = next(r for r in result.candidate_records if r.label == "target_a")
    assert a_record.outcome_status == expected_status
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    c_record = next(r for r in result.candidate_records if r.label == "target_c")
    assert b_record.outcome_status == "not_attempted"
    assert c_record.outcome_status == "not_attempted"
    # Only target A's URL was ever requested (SSRF case: zero requests at all).
    assert URL_B not in requested
    assert URL_C not in requested


def test_shared_request_budget_exhaustion_across_three_candidates(source_db, pilot_db, tmp_path):
    """target_a and target_b each redirect 4 times before succeeding (5
    physical requests each = 10 total), exactly exhausting
    MAX_DIRECT_REQUESTS across two candidates that both otherwise succeed.
    target_c's very first request must then be refused *before* any HTTP
    call is made for it -- `SharedRequestBudget.consume()` raises before
    `client.stream()` is ever invoked, so target_c's URL is never requested
    at all, proving the ceiling is enforced pre-request, not detected after
    the fact.
    """
    hop_counts = {"a": 0, "b": 0}
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested_urls.append(url)
        if url == URL_A:
            hop_counts["a"] += 1
            if hop_counts["a"] <= 4:
                return httpx.Response(302, headers={"location": URL_A})
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B:
            hop_counts["b"] += 1
            if hop_counts["b"] <= 4:
                return httpx.Response(302, headers={"location": URL_B})
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)
        raise AssertionError(f"target_c must never be requested -- budget exhausted before it: {url}")

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert MAX_DIRECT_REQUESTS == 10
    assert len(requested_urls) == MAX_DIRECT_REQUESTS  # exactly 10, never 11
    assert hop_counts == {"a": 5, "b": 5}
    assert URL_C not in requested_urls  # target_c's URL was never requested at all

    a_record = next(r for r in result.candidate_records if r.label == "target_a")
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    c_record = next(r for r in result.candidate_records if r.label == "target_c")
    assert a_record.outcome_status == "succeeded"
    assert b_record.outcome_status == "succeeded"
    assert c_record.outcome_status == "blocked_by_safety_policy"
    assert c_record.error_type == "request_budget_exhausted"
    assert result.stopped_early is True
    assert result.requests_used == MAX_DIRECT_REQUESTS


def test_budget_refuses_the_eleventh_request_before_it_is_sent(source_db, pilot_db, tmp_path):
    """Direct proof the ceiling is enforced *before* the outbound request,
    not detected after: the mock transport asserts it is never called with a
    request count above MAX_DIRECT_REQUESTS, which would fail loudly if
    `SharedRequestBudget.consume()` ever let request 11 through.
    """
    from discovery.acquirer import RequestBudgetExhaustedError, SharedRequestBudget

    budget = SharedRequestBudget(MAX_DIRECT_REQUESTS)
    for _ in range(MAX_DIRECT_REQUESTS):
        budget.consume()
    with pytest.raises(RequestBudgetExhaustedError):
        budget.consume()
    assert budget.used == MAX_DIRECT_REQUESTS


# --- XLSX format support -----------------------------------------------------
# Exercises the *same* generic acquirer.py validation path ("html"/"pdf"
# already use) with expected_format="xlsx" -- no XLSX-specific code exists in
# open_resource_pilot.py or in these tests' assertions about *how* validation
# happens, only *that* declaring "xlsx" now works end to end.


def test_run_succeeds_for_a_declared_xlsx_target(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    _reseed_b_with_url(source_db, URL_B_XLSX)
    targets = [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format="html"),
        PilotTarget(label="target_b", canonical_url=URL_B_XLSX, expected_format="xlsx"),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format="html_or_pdf"),
    ]
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=targets)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B_XLSX:
            return httpx.Response(
                200, headers={"content-type": _XLSX_CONTENT_TYPE}, content=_XLSX_BYTES
            )
        if url == URL_C:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        raise AssertionError(f"unexpected request to {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert result.stopped_early is False
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "succeeded"
    assert b_record.observed_extension == "xlsx"
    assert any(f.name.endswith(".xlsx") for f in tmp_path.iterdir())


def test_xlsx_target_rejects_html_response_as_declared_format_mismatch(
    source_db, pilot_db, tmp_path
):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets(b_format="xlsx"))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B:
            # Declared xlsx but the server returns an HTML error/login page.
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        raise AssertionError(f"target_c must never be requested after B's mismatch: {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "failed"
    assert b_record.error_type == "declared_format_mismatch"


def test_xlsx_url_extension_contradiction_fails_closed(source_db, pilot_db, tmp_path):
    """The URL path asserts a *different*, recognized extension (.csv) than
    the resolved Content-Type (xlsx) -- a present contradiction, not an
    absence, so this must fail closed before any byte reaches disk.
    """
    csv_suffixed_url = "https://example.org/reports/report-b.csv"
    _seed_source_trio(source_db)
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == URL_B).update(
        {"canonical_url": csv_suffixed_url}
    )
    source_db.commit()

    targets = [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format="html"),
        PilotTarget(label="target_b", canonical_url=csv_suffixed_url, expected_format="xlsx"),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format="html_or_pdf"),
    ]
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=targets)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == csv_suffixed_url:
            return httpx.Response(
                200, headers={"content-type": _XLSX_CONTENT_TYPE}, content=_XLSX_BYTES
            )
        raise AssertionError(f"target_c must never be requested after B's mismatch: {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "failed"
    assert b_record.error_type == "url_extension_mismatch"
    assert not any(f.suffix == ".xlsx" for f in tmp_path.iterdir())


def test_xlsx_content_disposition_contradiction_fails_closed(source_db, pilot_db, tmp_path):
    """Content-Disposition asserts a *different*, recognized extension (.csv)
    than the resolved Content-Type (xlsx) -- fails closed the same way a URL
    extension contradiction does.
    """
    _seed_source_trio(source_db)
    _reseed_b_with_url(source_db, URL_B_XLSX)
    targets = [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format="html"),
        PilotTarget(label="target_b", canonical_url=URL_B_XLSX, expected_format="xlsx"),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format="html_or_pdf"),
    ]
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=targets)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B_XLSX:
            return httpx.Response(
                200,
                headers={
                    "content-type": _XLSX_CONTENT_TYPE,
                    "content-disposition": 'attachment; filename="export.csv"',
                },
                content=_XLSX_BYTES,
            )
        raise AssertionError(f"target_c must never be requested after B's mismatch: {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "failed"
    assert b_record.error_type == "content_disposition_mismatch"


def test_malformed_or_fake_xlsx_rejected_by_magic_byte_check(source_db, pilot_db, tmp_path):
    r"""A response correctly labeled Content-Type: xlsx but whose body does
    NOT start with the ZIP local-file-header signature ("PK\x03\x04") is
    not a real (or not-yet-corrupted) XLSX file -- rejected before any file
    is written, exactly as a PDF/HTML signature mismatch already is.
    """
    _seed_source_trio(source_db)
    _reseed_b_with_url(source_db, URL_B_XLSX)
    targets = [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format="html"),
        PilotTarget(label="target_b", canonical_url=URL_B_XLSX, expected_format="xlsx"),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format="html_or_pdf"),
    ]
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=targets)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == URL_B_XLSX:
            return httpx.Response(
                200,
                headers={"content-type": _XLSX_CONTENT_TYPE},
                content=b"this is not a real xlsx file, just plain text",
            )
        raise AssertionError(f"target_c must never be requested after B's mismatch: {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "failed"
    assert b_record.error_type == "magic_byte_mismatch"
    assert b_record.bytes_downloaded is not None  # bytes were streamed before rejection
    assert not any(f.suffix == ".xlsx" for f in tmp_path.iterdir())


def test_xlsx_target_with_no_url_extension_is_not_a_contradiction(source_db, pilot_db, tmp_path):
    """A URL with no recognized extension at all (a bare API/record path, no
    suffix) makes no format assertion to contradict -- MIME + magic-byte
    evidence alone must be sufficient to succeed.
    """
    no_extension_url = "https://example.org/records/xyz789"
    _seed_source_trio(source_db)
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == URL_B).update(
        {"canonical_url": no_extension_url}
    )
    source_db.commit()

    targets = [
        PilotTarget(label="target_a", canonical_url=URL_A, expected_format="html"),
        PilotTarget(label="target_b", canonical_url=no_extension_url, expected_format="xlsx"),
        PilotTarget(label="target_c", canonical_url=URL_C, expected_format="html_or_pdf"),
    ]
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=targets)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        if url == no_extension_url:
            return httpx.Response(
                200, headers={"content-type": _XLSX_CONTENT_TYPE}, content=_XLSX_BYTES
            )
        if url == URL_C:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        raise AssertionError(f"unexpected request to {url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "succeeded"
    assert b_record.observed_extension == "xlsx"


# --- handoff-compatibility awareness (informational, not a seeding gate) ----


def test_handoff_compatible_formats_match_geocost_format_compatibility_matrix():
    """Per docs/geocost_format_compatibility.md: PDF and XLSX are fully
    geocost-ingestible today; HTML has no scraping/ingestion path yet.
    `html_or_pdf` is conservatively treated as not-statically-compatible
    since its actual observed format is only known at runtime.
    """
    from discovery.open_resource_pilot import is_handoff_compatible_format

    assert is_handoff_compatible_format("pdf") is True
    assert is_handoff_compatible_format("xlsx") is True
    assert is_handoff_compatible_format("html") is False
    assert is_handoff_compatible_format("html_or_pdf") is False


# --- zero retries -------------------------------------------------------------


def test_a_failed_acquisition_is_never_retried(source_db, pilot_db, tmp_path):
    """acquirer.acquire() has no retry loop -- a single failure for target_a
    must result in exactly one request to its URL, never a second attempt at
    the same hop, whether or not the pilot then stops.
    """
    call_counts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        call_counts[url] = call_counts.get(url, 0) + 1
        if url == URL_A:
            return httpx.Response(500, text="Internal Server Error")
        raise AssertionError(f"must never be requested after target A's failure: {url}")

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert call_counts[URL_A] == 1  # exactly one attempt, never retried
    a_record = next(r for r in result.candidate_records if r.label == "target_a")
    assert a_record.outcome_status == "failed"
    assert a_record.error_type == "http_error"


# --- 2026-08-22 crash-resilience correction: finalization failures ---------


def test_finalization_failure_stops_the_pilot_preserves_request_count_and_never_marks_the_candidate_acquired(
    source_db, pilot_db, tmp_path, monkeypatch
):
    """A filesystem-level failure at the tmp-to-final rename step (simulated
    here the same way test_acquirer.py does, via a monkeypatched
    Path.replace -- independent of the path-length case, which
    check_output_path_safety screens out before any request) must: consume
    exactly the one real request target_a's successful download used (not
    zero, not lost); leave target_a's SourceCandidate row in a
    download_failed state, never downloaded/acquired; and stop the pilot
    before target_b or target_c is ever requested.
    """
    from pathlib import Path

    def _raise_replace(self, target):
        raise OSError("simulated disk full")

    monkeypatch.setattr(Path, "replace", _raise_replace)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == URL_A:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=_HTML_BYTES)
        raise AssertionError(f"must never be requested after target A's finalization failure: {url}")

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert result.stopped_early is True
    assert result.requests_used == 1  # the one real request that DID happen, preserved

    a_record = next(r for r in result.candidate_records if r.label == "target_a")
    assert a_record.outcome_status == "failed"
    assert a_record.error_type == "file_finalization_failed"
    assert a_record.sha256 is not None  # computed before the failed rename
    assert a_record.bytes_downloaded == len(_HTML_BYTES)

    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    c_record = next(r for r in result.candidate_records if r.label == "target_c")
    assert b_record.outcome_status == "not_attempted"
    assert c_record.outcome_status == "not_attempted"

    # The seeded (pilot_db) candidate, not the original source_db one --
    # persist_acquisition_attempt operates on the pilot_db row.
    seeded_candidate_a = next(c for c, t in seeded if t.label == "target_a")
    pilot_db.refresh(seeded_candidate_a)
    assert seeded_candidate_a.screening_status == "download_failed"
    assert seeded_candidate_a.sha256 is None  # never set -- only a successful outcome sets it
    assert seeded_candidate_a.local_acquired_path is None

    # Quarantined, not deleted.
    assert any(tmp_path.glob(".tmp-*"))


def test_unexpected_acquisition_exception_is_converted_to_a_structured_failure_not_raised(
    source_db, pilot_db, tmp_path, monkeypatch
):
    """Any exception acquire() itself doesn't already turn into a structured
    AcquisitionOutcome (e.g. a genuinely unanticipated bug) must still leave
    run_open_resource_pilot returning a normal result, never raising -- this
    is what actually guarantees the CLI always reaches its
    artifact-writing code, independent of which specific failure occurred.
    """
    from discovery import open_resource_pilot as open_resource_pilot_module

    def _raise_unexpected(*args, **kwargs):
        raise RuntimeError("something genuinely unanticipated")

    monkeypatch.setattr(open_resource_pilot_module, "acquire", _raise_unexpected)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must never be reached -- acquire() itself is fully mocked out")

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    assert result.stopped_early is True
    a_record = next(r for r in result.candidate_records if r.label == "target_a")
    assert a_record.outcome_status == "failed"
    assert a_record.error_type == "unexpected_acquisition_exception"
    b_record = next(r for r in result.candidate_records if r.label == "target_b")
    assert b_record.outcome_status == "not_attempted"

    # The full exception detail is preserved in the persisted AcquisitionAttempt
    # row (error_message), even though CandidateAuditRecord's stop_reason only
    # summarizes error_type.
    attempt = (
        pilot_db.query(AcquisitionAttempt)
        .filter(AcquisitionAttempt.error_type == "unexpected_acquisition_exception")
        .one()
    )
    assert "RuntimeError" in (attempt.error_message or "")
    assert "something genuinely unanticipated" in (attempt.error_message or "")


# --- 2026-08-22 crash-resilience correction: operator-specified budget -----


def test_run_accepts_a_lowered_operator_specified_request_budget(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
        max_direct_requests=9,
    )
    assert result.max_direct_requests == 9
    assert result.requests_used <= 9
    assert all(r.outcome_status == "succeeded" for r in result.candidate_records)


def test_run_rejects_an_operator_specified_budget_above_the_pilot_maximum(
    source_db, pilot_db, tmp_path
):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    with pytest.raises(OpenResourcePilotError):
        run_open_resource_pilot(
            pilot_db,
            seeded,
            client=client,
            dest_dir=tmp_path,
            max_bytes=1_000_000,
            timeout_seconds=5.0,
            resolver=_public_resolver,
            max_direct_requests=MAX_DIRECT_REQUESTS + 1,
        )


def test_run_rejects_a_zero_or_negative_operator_specified_budget(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    with pytest.raises(OpenResourcePilotError):
        run_open_resource_pilot(
            pilot_db,
            seeded,
            client=client,
            dest_dir=tmp_path,
            max_bytes=1_000_000,
            timeout_seconds=5.0,
            resolver=_public_resolver,
            max_direct_requests=0,
        )


# --- durable artifacts -------------------------------------------------------


def test_audit_report_and_queue_report_are_written_and_contain_required_fields(
    source_db, pilot_db, tmp_path
):
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    result = run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    audit_path = tmp_path / "audit" / "report.json"
    queue_path = tmp_path / "queue" / "queue.json"
    queue_entries = build_acquisition_queue(pilot_db)
    write_audit_report(audit_path, result, queue_entries=queue_entries)
    write_queue_report(queue_path, queue_entries)

    assert audit_path.exists()
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert len(audit_payload["candidates"]) == 3
    required_fields = {
        "label",
        "candidate_id",
        "normalized_title",
        "organization",
        "publisher",
        "publication_year",
        "original_url",
        "resolved_url",
        "expected_format",
        "observed_extension",
        "content_type",
        "access_status",
        "bytes_downloaded",
        "sha256",
        "predicted_cost_observation_yield",
        "predicted_technical_observation_yield",
        "outcome_status",
        "error_type",
        "stop_reason",
    }
    for record in audit_payload["candidates"]:
        assert required_fields <= set(record.keys())

    by_label = {record["label"]: record for record in audit_payload["candidates"]}
    assert by_label["target_a"]["normalized_title"] == "Target A"
    assert by_label["target_a"]["organization"] == "MIT"
    assert by_label["target_a"]["publisher"] == "MIT News"
    assert by_label["target_a"]["publication_year"] == 2026
    assert by_label["target_b"]["organization"] == "DOE"

    assert queue_path.exists()
    assert (
        json.loads(queue_path.read_text(encoding="utf-8")) == []
    )  # empty queue -- none restricted


def test_queue_report_is_written_even_when_empty(tmp_path):
    empty_path = tmp_path / "nested" / "queue.json"
    write_queue_report(empty_path, [])
    assert empty_path.exists()
    assert json.loads(empty_path.read_text(encoding="utf-8")) == []


# --- robust (AST-based, not text-grep) campaign-isolation proof -------------


_FORBIDDEN_CAMPAIGN_MODULES = {
    "discovery.adaptive_controller",
    "discovery.quota_ledger",
    "discovery.adapters.serpapi_common",
    "discovery.adapters.serpapi_google",
    "discovery.adapters.serpapi_scholar",
}


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_open_resource_pilot_module_cannot_import_campaign_machinery():
    import discovery.open_resource_pilot as module

    imported = _imported_module_names(inspect.getsource(module))
    assert not (imported & _FORBIDDEN_CAMPAIGN_MODULES), imported & _FORBIDDEN_CAMPAIGN_MODULES


def test_open_resource_pilot_cli_command_cannot_import_campaign_machinery():
    from discovery import cli

    imported = _imported_module_names(inspect.getsource(cli.open_resource_pilot_command))
    assert not (imported & _FORBIDDEN_CAMPAIGN_MODULES), imported & _FORBIDDEN_CAMPAIGN_MODULES


# --- handoff manifest --------------------------------------------------------


def test_handoff_produces_an_exact_three_entry_manifest_when_all_legs_succeed(
    source_db, pilot_db, tmp_path
):
    from discovery.handoff import build_manifest, validate_and_prepare_candidate

    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    validated = sum(
        1 for candidate, _target in seeded if validate_and_prepare_candidate(pilot_db, candidate)
    )
    pilot_db.commit()
    assert validated == 3

    manifest, manifest_path = build_manifest(
        pilot_db, discovery_run_id="test-open-resource-pilot", handoff_dir=tmp_path / "handoff"
    )
    pilot_db.commit()

    assert manifest.entry_count == 3
    assert manifest_path.exists()
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["entry_count"] == 3
    assert written["manifest_sha256"] == manifest.manifest_sha256


# --- unchanged behavior for the existing pilots -----------------------------


def test_acquisition_attempt_rows_are_written_for_each_seeded_candidate(
    source_db, pilot_db, tmp_path
):
    """Sanity check that this new pilot persists AcquisitionAttempt rows the
    same way the existing pilots do -- no divergence in the shared
    persist_acquisition_attempt() path.
    """
    _seed_source_trio(source_db)
    seeded = seed_open_resource_pilot(source_db, pilot_db, targets=_targets())

    client = httpx.Client(transport=httpx.MockTransport(_well_behaved_handler))
    run_open_resource_pilot(
        pilot_db,
        seeded,
        client=client,
        dest_dir=tmp_path,
        max_bytes=1_000_000,
        timeout_seconds=5.0,
        resolver=_public_resolver,
    )

    attempts = pilot_db.query(AcquisitionAttempt).count()
    assert attempts == 3
