"""CLI smoke tests via Typer's CliRunner. Every command here stays offline --
either genuinely no `--execute-network` (dry-run only), or, for the handful
of `--execute-network` invocations near the end of this file, `httpx.Client`
itself is monkeypatched to an `httpx.MockTransport`-backed client, so no real
HTTP call is ever made even though the command's live-execution code path
runs. This exercises argument parsing, DB session plumbing, dry-run
messaging, and (for the mocked-execute cases) real artifact creation -- never
a live provider call.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from discovery.cli import app
from discovery.db.models import Base, SourceCandidate

runner = CliRunner()


@pytest.fixture
def isolated_discovery_logging():
    """discovery.logging.configure_logging() only has an effect on its
    *first* call per process (by design, so a real CLI invocation is safe to
    call it multiple times). That makes a test asserting a specific log file
    was written order-dependent unless it resets the module's state first --
    this fixture does that, and restores the prior state afterward so it
    never leaks into other tests.
    """
    import logging as stdlib_logging

    from discovery import logging as discovery_logging

    logger = stdlib_logging.getLogger("discovery")
    original_handlers = list(logger.handlers)
    original_configured = discovery_logging._CONFIGURED
    logger.handlers.clear()
    discovery_logging._CONFIGURED = False
    try:
        yield
    finally:
        logger.handlers.clear()
        logger.handlers.extend(original_handlers)
        discovery_logging._CONFIGURED = original_configured


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db_path = tmp_path / "cli_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("DISCOVERY_ENABLED_ADAPTERS", "openalex,crossref")
    result = runner.invoke(app, ["init-database"])
    assert result.exit_code == 0, result.output
    return db_path


def test_init_database_creates_schema(tmp_path, monkeypatch):
    db_path = tmp_path / "init_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    result = runner.invoke(app, ["init-database"])
    assert result.exit_code == 0
    assert db_path.exists()


def test_plan_command_is_offline_and_creates_query_plans(cli_db):
    result = runner.invoke(app, ["plan", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["queries_planned"] > 0
    assert "openalex" in payload["by_adapter"]


def test_run_without_execute_network_is_dry_run(cli_db):
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()


def test_adaptive_run_include_families_flag_parses_in_dry_run(cli_db):
    """`--include-families` is accepted and doesn't break dry-run parsing --
    the actual filtering behavior is covered at the service/controller layer
    (tests/integration/test_service_run.py, tests/unit/test_adaptive_controller.py).
    """
    result = runner.invoke(app, ["adaptive-run", "--include-families", "cost_component,cost_driver"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()


def test_smoke_test_without_execute_network_is_dry_run(cli_db, monkeypatch):
    # Even with a wildly over-budget adapter set enabled, the dry-run must
    # print (not execute) at most 5 queries and never touch the network.
    monkeypatch.setenv("DISCOVERY_ENABLED_ADAPTERS", "serpapi_google,serpapi_scholar")
    result = runner.invoke(app, ["smoke-test"])
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert "hard cap: 5" in result.output


def test_smoke_test_live_builds_serpapi_adapters_when_only_free_adapters_enabled(
    cli_db, monkeypatch, tmp_path
):
    monkeypatch.setenv("DISCOVERY_ENABLED_ADAPTERS", "openalex,crossref")
    monkeypatch.setenv("SERPAPI_API_KEY", "test-key")
    monkeypatch.setenv("DISCOVERY_SERPAPI_CACHE_DIR", str(tmp_path / "cache"))

    created = []

    class FakeGoogleAdapter:
        def __init__(self, client, *, results_per_page):
            created.append(("google", client, results_per_page))

    class FakeScholarAdapter:
        def __init__(self, client, *, results_per_page):
            created.append(("scholar", client, results_per_page))

    monkeypatch.setattr(
        "discovery.adapters.serpapi_google.SerpApiGoogleAdapter", FakeGoogleAdapter
    )
    monkeypatch.setattr(
        "discovery.adapters.serpapi_scholar.SerpApiScholarAdapter", FakeScholarAdapter
    )
    monkeypatch.setattr(
        "discovery.service.execute_smoke_test_run",
        lambda *args, **kwargs: SimpleNamespace(
            status="completed",
            queries_executed=0,
            queries_failed=0,
            new_candidates=0,
        ),
    )

    result = runner.invoke(app, ["smoke-test", "--execute-network"])

    assert result.exit_code == 0, result.output
    assert [name for name, _client, _page_size in created] == ["google", "scholar"], result.output
    assert created[0][1] is created[1][1]


def test_status_reports_zero_activity_for_dry_run(cli_db):
    plan_result = runner.invoke(app, ["plan", "--json"])
    run_id = json.loads(plan_result.output)["discovery_run_id"]

    status_result = runner.invoke(app, ["status", run_id])
    assert status_result.exit_code == 0, status_result.output
    summary = json.loads(status_result.output)
    assert summary["discovery_run_id"] == run_id
    assert summary["api_requests_attempted"] == 0


def test_status_unknown_run_id_errors(cli_db):
    result = runner.invoke(app, ["status", "not-a-real-run-id"])
    assert result.exit_code == 1


def test_review_lists_nothing_when_empty(cli_db):
    result = runner.invoke(app, ["review"])
    assert result.exit_code == 0
    assert "(0 shown)" in result.output


def test_review_default_status_matches_real_manual_review_candidates_sorted_by_score(cli_db):
    """Regression test: the old default `--status manual_review_required`
    never matched a candidate a fresh screening pass actually produces
    (`screened_review` -- see state_machine._DECISION_TO_STATUS), so
    `discovery review` with no arguments always showed "(0 shown)" even with
    real manual-review candidates present. Also verifies score-descending
    ordering, found necessary while auditing a live run where 39/42
    candidates landed in manual review.
    """
    from discovery.db import get_session_local
    from discovery.db.models import ScreeningDecision, SourceCandidate

    db = get_session_local()()
    try:
        low = SourceCandidate(
            normalized_title="low score candidate",
            screening_status="screened_review",
        )
        high = SourceCandidate(
            normalized_title="high score candidate",
            screening_status="screened_review",
        )
        db.add_all([low, high])
        db.flush()
        db.add(
            ScreeningDecision(
                source_candidate_id=low.id,
                decision="manual_review",
                composite_score=0.2,
                rules_version="rules-v3",
            )
        )
        db.add(
            ScreeningDecision(
                source_candidate_id=high.id,
                decision="manual_review",
                composite_score=0.5,
                rules_version="rules-v3",
            )
        )
        db.commit()
    finally:
        db.close()

    result = runner.invoke(app, ["review"])
    assert result.exit_code == 0, result.output
    assert "(2 shown)" in result.output
    high_pos = result.output.find("high score candidate")
    low_pos = result.output.find("low score candidate")
    assert high_pos != -1 and low_pos != -1
    assert high_pos < low_pos  # higher composite_score listed first


def test_acquire_without_execute_network_is_dry_run(cli_db):
    result = runner.invoke(app, ["acquire"])
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()


def test_acquire_fetches_direct_download_url_not_canonical_url(cli_db, monkeypatch):
    """Regression test for the URL-fetch defect found during the 2026-08-24
    acquisition: `canonical_url` is dedup-normalized (`www.` stripped) and
    must never be the URL actually requested when `direct_download_url` (the
    raw, as-discovered URL) is available. `discovery.acquirer.acquire` is
    monkeypatched to capture its own first argument rather than mocking
    DNS/HTTP -- this isolates the test to the URL-selection logic this fix
    changed, not acquirer.py's own already-tested network behavior.
    """
    from discovery.acquirer import AcquisitionOutcome
    from discovery.db import get_session_local
    from discovery.db.models import SourceCandidate

    db = get_session_local()()
    try:
        candidate = SourceCandidate(
            normalized_title="a report requiring www",
            canonical_url="https://nrel.gov/docs/fy07osti/41156.pdf",
            direct_download_url="https://www.nrel.gov/docs/fy07osti/41156.pdf",
            access_status="open_access",
            screening_status="acquisition_pending",
        )
        db.add(candidate)
        db.commit()
    finally:
        db.close()

    captured_urls = []

    def fake_acquire(url, **kwargs):
        captured_urls.append(url)
        return AcquisitionOutcome(status="succeeded", retryable=False, sha256="a" * 64, local_path="x.pdf")

    monkeypatch.setattr("discovery.acquirer.acquire", fake_acquire)

    result = runner.invoke(app, ["acquire", "--execute-network"])
    assert result.exit_code == 0, result.output
    assert captured_urls == ["https://www.nrel.gov/docs/fy07osti/41156.pdf"]


def test_acquire_falls_back_to_canonical_url_when_direct_download_url_is_none(cli_db, monkeypatch):
    """A candidate with no direct_download_url (created before this field
    existed, or with no raw URL to give) must fall back to canonical_url
    exactly as before this fix -- no regression for existing data.
    """
    from discovery.acquirer import AcquisitionOutcome
    from discovery.db import get_session_local
    from discovery.db.models import SourceCandidate

    db = get_session_local()()
    try:
        candidate = SourceCandidate(
            normalized_title="a report with no raw url on record",
            canonical_url="https://example.com/report.pdf",
            direct_download_url=None,
            access_status="open_access",
            screening_status="acquisition_pending",
        )
        db.add(candidate)
        db.commit()
    finally:
        db.close()

    captured_urls = []

    def fake_acquire(url, **kwargs):
        captured_urls.append(url)
        return AcquisitionOutcome(status="succeeded", retryable=False, sha256="b" * 64, local_path="y.pdf")

    monkeypatch.setattr("discovery.acquirer.acquire", fake_acquire)

    result = runner.invoke(app, ["acquire", "--execute-network"])
    assert result.exit_code == 0, result.output
    assert captured_urls == ["https://example.com/report.pdf"]


def test_import_url(cli_db):
    result = runner.invoke(app, ["import", "url", "https://example.com/report.pdf", "--title", "Test Report"])
    assert result.exit_code == 0, result.output
    assert "1 new candidate" in result.output


def test_import_csv(cli_db, tmp_path):
    csv_path = tmp_path / "seeds.csv"
    csv_path.write_text("url,title\nhttps://example.com/a.pdf,Report A\n", encoding="utf-8")
    result = runner.invoke(app, ["import", "csv", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert "Imported 1 row" in result.output


def test_handoff_with_no_downloaded_candidates_produces_empty_manifest(cli_db):
    result = runner.invoke(app, ["handoff", "some-run-id"])
    assert result.exit_code == 0, result.output
    assert "Validated 0 file" in result.output


def test_open_resource_pilot_dry_run_makes_zero_network_calls_and_creates_zero_outputs(tmp_path):
    source_db_path = tmp_path / "source_placeholder.db"
    pilot_db_path = tmp_path / "pilot.db"
    download_dir = tmp_path / "downloads"
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    result = runner.invoke(
        app,
        [
            "open-resource-pilot",
            "--source-database-url",
            f"sqlite:///{source_db_path}",
            "--pilot-database-url",
            f"sqlite:///{pilot_db_path}",
            "--target-a-url",
            "https://example.com/news/article-a",
            "--target-a-format",
            "html",
            "--target-b-url",
            "https://example.org/reports/report-b.pdf",
            "--target-b-format",
            "pdf",
            "--target-c-url",
            "https://example.net/blog/post-c",
            "--target-c-format",
            "html_or_pdf",
            "--download-dir",
            str(download_dir),
            "--handoff-dir",
            str(handoff_dir),
            "--queue-path",
            str(queue_path),
            "--audit-path",
            str(audit_path),
            "--log-path",
            str(log_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    assert "target_a" in result.output
    assert "target_b" in result.output
    assert "target_c" in result.output
    assert "max_direct_requests=10" in result.output

    # Zero generated execution outputs -- nothing beyond the source-db
    # placeholder path (never created since it's never queried) should exist.
    for path in (pilot_db_path, download_dir, handoff_dir, queue_path, audit_path, log_path):
        assert not path.exists(), f"dry run must not create {path}"


def test_open_resource_pilot_rejects_an_invalid_declared_format(tmp_path):
    result = runner.invoke(
        app,
        [
            "open-resource-pilot",
            "--source-database-url",
            f"sqlite:///{tmp_path / 'source.db'}",
            "--pilot-database-url",
            f"sqlite:///{tmp_path / 'pilot.db'}",
            "--target-a-url",
            "https://example.com/a",
            "--target-a-format",
            "docx",  # not html/pdf/html_or_pdf
            "--target-b-url",
            "https://example.com/b",
            "--target-b-format",
            "pdf",
            "--target-c-url",
            "https://example.com/c",
            "--target-c-format",
            "html",
            "--download-dir",
            str(tmp_path / "downloads"),
            "--handoff-dir",
            str(tmp_path / "handoff"),
            "--queue-path",
            str(tmp_path / "queue.json"),
            "--audit-path",
            str(tmp_path / "audit.json"),
            "--log-path",
            str(tmp_path / "pilot.log"),
        ],
    )
    assert result.exit_code == 1
    assert "Invalid" in result.output


_OPEN_RESOURCE_PILOT_URL_A = "https://example.com/news/article-a"
_OPEN_RESOURCE_PILOT_URL_B = "https://example.org/reports/report-b.pdf"
_OPEN_RESOURCE_PILOT_URL_C = "https://example.net/blog/post-c"


def _seed_open_resource_pilot_source_db(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    session.add_all(
        [
            SourceCandidate(
                canonical_url=_OPEN_RESOURCE_PILOT_URL_A,
                normalized_title="Target A",
                organization="MIT",
                screening_status="acquisition_pending",
                access_status="open_access",
                expected_cost_observation_yield=1,
                expected_technical_observation_yield=3,
            ),
            SourceCandidate(
                canonical_url=_OPEN_RESOURCE_PILOT_URL_B,
                normalized_title="Target B",
                organization="DOE",
                screening_status="acquisition_pending",
                access_status="open_access",
                expected_cost_observation_yield=20,
                expected_technical_observation_yield=15,
            ),
            SourceCandidate(
                canonical_url=_OPEN_RESOURCE_PILOT_URL_C,
                normalized_title="Target C",
                organization="ThinkGeoEnergy",
                screening_status="acquisition_pending",
                access_status="open_access",
                expected_cost_observation_yield=0,
                expected_technical_observation_yield=1,
            ),
        ]
    )
    session.commit()
    session.close()
    engine.dispose()


def _open_resource_pilot_mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == _OPEN_RESOURCE_PILOT_URL_A:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html><body>a</body></html>"
        )
    if url == _OPEN_RESOURCE_PILOT_URL_B:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.4\nfake\n"
        )
    if url == _OPEN_RESOURCE_PILOT_URL_C:
        return httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<html><body>c</body></html>"
        )
    raise AssertionError(f"unexpected request to {url}")


def _open_resource_pilot_args(
    *,
    source_db_path,
    pilot_db_path,
    download_dir,
    handoff_dir,
    queue_path,
    audit_path,
    log_path,
    max_direct_requests=None,
    execute_network=True,
):
    args = [
        "open-resource-pilot",
        "--source-database-url",
        f"sqlite:///{source_db_path}",
        "--pilot-database-url",
        f"sqlite:///{pilot_db_path}",
        "--target-a-url",
        _OPEN_RESOURCE_PILOT_URL_A,
        "--target-a-format",
        "html",
        "--target-b-url",
        _OPEN_RESOURCE_PILOT_URL_B,
        "--target-b-format",
        "pdf",
        "--target-c-url",
        _OPEN_RESOURCE_PILOT_URL_C,
        "--target-c-format",
        "html_or_pdf",
        "--download-dir",
        str(download_dir),
        "--handoff-dir",
        str(handoff_dir),
        "--queue-path",
        str(queue_path),
        "--audit-path",
        str(audit_path),
        "--log-path",
        str(log_path),
    ]
    if max_direct_requests is not None:
        args += ["--max-direct-requests", str(max_direct_requests)]
    if execute_network:
        args.append("--execute-network")
    return args


def test_open_resource_pilot_execute_path_creates_every_mandatory_artifact(
    tmp_path, monkeypatch, isolated_discovery_logging
):
    """The full --execute-network path, driven end-to-end through the CLI
    with httpx.Client monkeypatched to a MockTransport -- proves every
    mandatory artifact (pilot DB, downloads dir, manifest, queue JSON,
    execution log, audit report) actually gets created, and that the log
    file is genuinely non-empty (discovery/logging.py's 0% unit coverage
    doesn't prove that on its own).
    """
    source_db_path = tmp_path / "source.db"
    pilot_db_path = tmp_path / "pilot.db"
    download_dir = tmp_path / "downloads"
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    _seed_open_resource_pilot_source_db(source_db_path)

    real_httpx_client = httpx.Client

    def fake_client(*args, **kwargs):
        return real_httpx_client(transport=httpx.MockTransport(_open_resource_pilot_mock_handler))

    monkeypatch.setattr("httpx.Client", fake_client)

    result = runner.invoke(
        app,
        _open_resource_pilot_args(
            source_db_path=source_db_path,
            pilot_db_path=pilot_db_path,
            download_dir=download_dir,
            handoff_dir=handoff_dir,
            queue_path=queue_path,
            audit_path=audit_path,
            log_path=log_path,
        ),
    )

    assert result.exit_code == 0, result.output

    assert pilot_db_path.exists()
    assert download_dir.exists()
    assert any(download_dir.iterdir()), "downloads directory has no acquired files"
    assert handoff_dir.exists()
    manifest_files = list(handoff_dir.glob("manifest_*.json"))
    assert len(manifest_files) == 1
    manifest_payload = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    assert manifest_payload["entry_count"] == 3

    assert queue_path.exists()
    assert json.loads(queue_path.read_text(encoding="utf-8")) == []

    assert audit_path.exists()
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert len(audit_payload["candidates"]) == 3

    assert log_path.exists()
    assert log_path.stat().st_size > 0, "execution log file must be non-empty"
    log_content = log_path.read_text(encoding="utf-8")
    assert "open_resource_pilot" in log_content


def test_open_resource_pilot_execute_path_never_touches_campaign_machinery(
    tmp_path, monkeypatch, isolated_discovery_logging
):
    """Runtime campaign-isolation proof: install sentinels on the actual
    adaptive-discovery and quota-ledger entry points that fail the test if
    called, run the mocked --execute-network path end-to-end, and confirm
    neither sentinel fired.
    """
    source_db_path = tmp_path / "source.db"
    pilot_db_path = tmp_path / "pilot.db"
    download_dir = tmp_path / "downloads"
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    _seed_open_resource_pilot_source_db(source_db_path)

    real_httpx_client = httpx.Client

    def fake_client(*args, **kwargs):
        return real_httpx_client(transport=httpx.MockTransport(_open_resource_pilot_mock_handler))

    monkeypatch.setattr("httpx.Client", fake_client)

    sentinel_calls = []

    def _adaptive_sentinel(*args, **kwargs):
        sentinel_calls.append("run_adaptive_discovery")
        raise AssertionError(
            "adaptive_controller.run_adaptive_discovery must never be invoked by the pilot"
        )

    def _dry_run_sentinel(*args, **kwargs):
        sentinel_calls.append("estimate_dry_run")
        raise AssertionError(
            "adaptive_controller.estimate_dry_run must never be invoked by the pilot"
        )

    def _quota_reserve_sentinel(*args, **kwargs):
        sentinel_calls.append("quota_ledger.reserve")
        raise AssertionError("quota_ledger.reserve must never be invoked by the pilot")

    monkeypatch.setattr("discovery.adaptive_controller.run_adaptive_discovery", _adaptive_sentinel)
    monkeypatch.setattr("discovery.adaptive_controller.estimate_dry_run", _dry_run_sentinel)
    monkeypatch.setattr("discovery.quota_ledger.reserve", _quota_reserve_sentinel)

    result = runner.invoke(
        app,
        _open_resource_pilot_args(
            source_db_path=source_db_path,
            pilot_db_path=pilot_db_path,
            download_dir=download_dir,
            handoff_dir=handoff_dir,
            queue_path=queue_path,
            audit_path=audit_path,
            log_path=log_path,
        ),
    )

    assert result.exit_code == 0, result.output
    assert sentinel_calls == [], f"forbidden campaign machinery was invoked: {sentinel_calls}"


# --- 2026-08-22 crash-resilience correction: path safety, structured
# finalization failures, guaranteed artifacts, operator-specified budget ---


def test_open_resource_pilot_overlong_download_dir_stops_before_any_network_call_but_still_produces_every_mandatory_artifact(
    tmp_path, monkeypatch, isolated_discovery_logging
):
    """A caller-supplied --download-dir long enough that the worst-case
    final path (64-hex-char sha256 + "." + longest declared extension)
    would exceed Windows' safe path length must fail target_a via
    check_output_path_safety's pre-network check -- zero HTTP requests are
    ever made (the mock handler raises if called at all), the pilot stops
    after target_a (target_b/target_c are never attempted), the command
    exits non-zero, and every mandatory artifact is still produced with a
    non-empty log -- never leaving only a Python traceback as the
    explanation (this is exactly the 2026-08-22 three-PDF-canary incident,
    reproduced deterministically offline).
    """
    source_db_path = tmp_path / "source.db"
    pilot_db_path = tmp_path / "pilot.db"
    # Shaped like the real 2026-08-22 incident: download_dir itself is a
    # perfectly creatable ~200-character path (well under Windows' 260-char
    # mkdir limit) -- it's only the *final* sha256-named file
    # (200 + 1 + 68 = 269 > 259) that would be unsafe, exactly as it was
    # for the real canary. A directory long enough to fail mkdir() itself
    # would be a different (also-real, but not this) bug.
    target_download_dir_length = 200
    filler_length = max(1, target_download_dir_length - len(str(tmp_path)) - 1)
    download_dir = tmp_path / ("x" * filler_length)
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    _seed_open_resource_pilot_source_db(source_db_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must never issue a request: {request.url}")

    real_httpx_client = httpx.Client

    def fake_client(*args, **kwargs):
        return real_httpx_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("httpx.Client", fake_client)

    result = runner.invoke(
        app,
        _open_resource_pilot_args(
            source_db_path=source_db_path,
            pilot_db_path=pilot_db_path,
            download_dir=download_dir,
            handoff_dir=handoff_dir,
            queue_path=queue_path,
            audit_path=audit_path,
            log_path=log_path,
        ),
    )

    # Non-zero exit, but not because of an unhandled exception escaping to
    # the terminal -- the command itself controls this via typer.Exit.
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)

    assert pilot_db_path.exists()
    assert audit_path.exists()
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert len(audit_payload["candidates"]) == 3
    a_record = next(c for c in audit_payload["candidates"] if c["label"] == "target_a")
    assert a_record["outcome_status"] == "failed"
    assert a_record["error_type"] == "output_path_too_long"
    b_record = next(c for c in audit_payload["candidates"] if c["label"] == "target_b")
    c_record = next(c for c in audit_payload["candidates"] if c["label"] == "target_c")
    assert b_record["outcome_status"] == "not_attempted"
    assert c_record["outcome_status"] == "not_attempted"

    assert queue_path.exists()
    assert handoff_dir.exists()
    manifest_files = list(handoff_dir.glob("manifest_*.json"))
    assert len(manifest_files) == 1
    manifest_payload = json.loads(manifest_files[0].read_text(encoding="utf-8"))
    assert manifest_payload["entry_count"] == 0  # no successful handoff occurred -- recorded explicitly, not omitted

    assert log_path.exists()
    assert log_path.stat().st_size > 0, "execution log file must be non-empty even on failure"


def test_open_resource_pilot_accepts_operator_max_direct_requests_of_nine(
    tmp_path, monkeypatch, isolated_discovery_logging
):
    """A caller-specified --max-direct-requests below the pilot maximum (the
    value a real continuation/replacement run of the failed three-PDF
    canary would use, since 1 of the original 10 was already consumed) is
    accepted and reflected in the audit report -- not silently ignored.
    """
    source_db_path = tmp_path / "source.db"
    pilot_db_path = tmp_path / "pilot.db"
    download_dir = tmp_path / "downloads"
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    _seed_open_resource_pilot_source_db(source_db_path)

    real_httpx_client = httpx.Client

    def fake_client(*args, **kwargs):
        return real_httpx_client(transport=httpx.MockTransport(_open_resource_pilot_mock_handler))

    monkeypatch.setattr("httpx.Client", fake_client)

    result = runner.invoke(
        app,
        _open_resource_pilot_args(
            source_db_path=source_db_path,
            pilot_db_path=pilot_db_path,
            download_dir=download_dir,
            handoff_dir=handoff_dir,
            queue_path=queue_path,
            audit_path=audit_path,
            log_path=log_path,
            max_direct_requests=9,
        ),
    )

    assert result.exit_code == 0, result.output
    assert "max_direct_requests=9" in result.output
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit_payload["max_direct_requests"] == 9
    assert audit_payload["requests_used"] <= 9


def test_open_resource_pilot_rejects_max_direct_requests_above_ten(tmp_path):
    """A caller may never authorize more direct requests than the pilot's
    own ceiling, even explicitly -- rejected before any DB is opened or any
    network call is attempted (this is a dry-run-only invocation).
    """
    source_db_path = tmp_path / "source.db"
    pilot_db_path = tmp_path / "pilot.db"
    download_dir = tmp_path / "downloads"
    handoff_dir = tmp_path / "handoff"
    queue_path = tmp_path / "queue" / "queue.json"
    audit_path = tmp_path / "audit" / "report.json"
    log_path = tmp_path / "pilot.log"

    result = runner.invoke(
        app,
        _open_resource_pilot_args(
            source_db_path=source_db_path,
            pilot_db_path=pilot_db_path,
            download_dir=download_dir,
            handoff_dir=handoff_dir,
            queue_path=queue_path,
            audit_path=audit_path,
            log_path=log_path,
            max_direct_requests=11,
            execute_network=False,
        ),
    )

    assert result.exit_code == 1
    assert "--max-direct-requests must be between 1 and 10" in result.output
    assert not pilot_db_path.exists()
