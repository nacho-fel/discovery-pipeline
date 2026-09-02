"""End-to-end offline pipeline tests: query plan -> execute -> screen ->
(mocked) acquire -> validate -> handoff manifest, entirely without network
calls. Adapters are simple in-process fakes implementing the `SearchAdapter`
protocol directly (the service-level boundary), complementing the
httpx.MockTransport tests that exercise each adapter's own HTTP parsing.
"""

from pathlib import Path

from discovery.adapters.base import RetryableAdapterError
from discovery.config import Settings
from discovery.handoff import build_manifest, validate_and_prepare_candidate
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.result_registry import get_or_create_discovery_run
from discovery.service import (
    SMOKE_TEST_REQUEST_CEILING,
    execute_run,
    execute_smoke_test_run,
    expand_frontier,
    plan_run,
    plan_smoke_test_run,
)


class FakeAdapter:
    def __init__(self, name: str, responses: list):
        self.name = name
        self._responses = list(responses)
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        if not self._responses:
            return AdapterSearchResponse(result_count=0, hits=[])
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        discovery_query_budget=50,
        discovery_request_budget=50,
        discovery_enabled_adapters="serpapi_google,openalex",
        screening_rules_path=Path(__file__).resolve().parents[2] / "config" / "screening_rules.yaml",
        coverage_matrix_path=Path(__file__).resolve().parents[2] / "config" / "coverage_matrix.yaml",
        query_templates_path=Path(__file__).resolve().parents[2] / "config" / "query_templates.yaml",
        multilingual_terms_path=Path(__file__).resolve().parents[2] / "config" / "multilingual_terms.yaml",
        access_policy_path=Path(__file__).resolve().parents[2] / "config" / "access_policy.yaml",
        discovery_max_retries=1,
        discovery_retry_base_delay_seconds=0.001,
        discovery_retry_max_delay_seconds=0.002,
    )
    base.update(overrides)
    return Settings(**base)


def _cost_response() -> AdapterSearchResponse:
    return AdapterSearchResponse(
        result_count=1,
        hits=[
            RawSearchHit(
                title="Enhanced Geothermal Systems Drilling Cost and Rate of Penetration Report",
                url="https://nrel.gov/reports/egs-drilling-cost",
                snippet=(
                    "This government technical report presents drilling cost per foot, "
                    "capital expenditure, and rate of penetration data for geothermal wells."
                ),
                rank=1,
            )
        ],
        raw_response={},
    )


def test_plan_run_is_idempotent(test_db, tmp_path):
    settings = _settings(tmp_path)
    run = get_or_create_discovery_run(
        test_db, query_budget=5, request_budget=5, configuration={}
    )
    test_db.commit()

    first = plan_run(test_db, run, settings)
    second = plan_run(test_db, run, settings)

    assert len(first) == len(second)
    from discovery.db.models import QueryPlan

    assert test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count() == len(first)


def test_end_to_end_offline_run_through_handoff_manifest(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_query_budget=1, discovery_request_budget=5)
    run = get_or_create_discovery_run(
        test_db, query_budget=1, request_budget=5, configuration={}
    )
    test_db.commit()

    rows = plan_run(test_db, run, settings)
    assert len(rows) >= 1

    google_adapter = FakeAdapter("serpapi_google", [_cost_response()] * 10)
    openalex_adapter = FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10)
    adapters = {"serpapi_google": google_adapter, "openalex": openalex_adapter}

    outcome = execute_run(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw"
    )
    assert outcome.status == "completed"
    assert outcome.new_candidates >= 1

    from discovery.db.models import SourceCandidate

    accepted = (
        test_db.query(SourceCandidate)
        .filter(SourceCandidate.screening_status == "acquisition_pending")
        .all()
    )
    assert len(accepted) >= 1
    candidate = accepted[0]

    # Simulate a successful acquisition (acquirer.py itself is unit-tested
    # separately against httpx.MockTransport) by writing the file directly and
    # advancing the state machine the same way `persist_acquisition_attempt` would.
    acquired_path = tmp_path / "acquired.pdf"
    acquired_path.write_bytes(b"%PDF-1.4\nfake content")
    import hashlib

    from discovery import state_machine

    candidate.sha256 = hashlib.sha256(acquired_path.read_bytes()).hexdigest()
    candidate.local_acquired_path = str(acquired_path)
    state_machine.apply_transition(candidate, "downloaded")
    test_db.commit()

    assert validate_and_prepare_candidate(test_db, candidate) is True
    test_db.commit()

    manifest, manifest_path = build_manifest(
        test_db, discovery_run_id=run.id, handoff_dir=tmp_path / "handoff"
    )
    assert manifest.entry_count == 1
    assert manifest_path.exists()
    assert candidate.screening_status == "handed_off"


def _paywalled_response() -> AdapterSearchResponse:
    return AdapterSearchResponse(
        result_count=1,
        hits=[
            RawSearchHit(
                title="SPE Drilling Cost Analysis for Unconventional Wells",
                url="https://onepetro.org/SPE/999999",
                snippet=(
                    "Authorization for expenditure and cost per lateral foot "
                    "for unconventional shale wells, day rate benchmark."
                ),
                rank=1,
            )
        ],
        raw_response={},
    )


def test_screening_uses_the_hit_snippet_not_just_the_title(test_db, tmp_path):
    """Regression test: `_process_hits` must pass a hit's actual snippet to
    the screener. All the cost/driver signal below lives in the snippet, not
    the (deliberately generic) title -- if the snippet were dropped (as it
    was before this fix), this candidate would score too low to reach
    manual_review's accept threshold and this test would fail.
    """
    settings = _settings(tmp_path, discovery_query_budget=1, discovery_request_budget=5)
    run = get_or_create_discovery_run(test_db, query_budget=1, request_budget=5, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    response = AdapterSearchResponse(
        result_count=1,
        hits=[
            RawSearchHit(
                title="Annual Technical Report",
                url="https://nrel.gov/reports/annual-2024",
                snippet=(
                    "Drilling cost per foot, rate of penetration, and capital "
                    "expenditure for enhanced geothermal wells."
                ),
                rank=1,
            )
        ],
        raw_response={},
    )
    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", [response] * 10),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }
    execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    from discovery.db.models import SourceCandidate

    candidate = test_db.query(SourceCandidate).one()
    assert candidate.screening_status == "acquisition_pending"


def test_known_paywalled_source_skips_acquisition_entirely(test_db, tmp_path):
    """A candidate from a domain listed `licensed_mit_access` in
    access_policy.yaml must be preserved as metadata-only: accepted by
    screening, routed straight to the `paywalled` screening state, and
    never given an AcquisitionAttempt -- no unauthorized download is ever
    attempted.
    """
    settings = _settings(tmp_path, discovery_query_budget=1, discovery_request_budget=5)
    run = get_or_create_discovery_run(test_db, query_budget=1, request_budget=5, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", [_paywalled_response()] * 10),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }
    execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    from discovery.db.models import AcquisitionAttempt, SourceCandidate

    candidates = test_db.query(SourceCandidate).all()
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.access_status == "licensed_mit_access"
    assert candidate.screening_status == "paywalled"
    assert test_db.query(AcquisitionAttempt).filter(
        AcquisitionAttempt.source_candidate_id == candidate.id
    ).count() == 0


def test_multilingual_queries_are_included_in_the_plan(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_query_budget=1000, discovery_request_budget=5)
    run = get_or_create_discovery_run(test_db, query_budget=1000, request_budget=5, configuration={})
    test_db.commit()
    rows = plan_run(test_db, run, settings)

    spanish_queries = [r for r in rows if "geotérmica" in r.rendered_query or "perforación" in r.rendered_query]
    assert spanish_queries
    assert any(r.language == "es" for r in spanish_queries)


def test_smoke_test_plan_is_hard_capped_at_five(test_db, tmp_path):
    settings = _settings(
        tmp_path,
        discovery_enabled_adapters="serpapi_google,serpapi_scholar,openalex,crossref",
        # Even a wildly over-budget request/query budget must not raise the
        # smoke test's own hard-coded ceiling.
        discovery_query_budget=10_000,
        discovery_request_budget=10_000,
    )
    run = get_or_create_discovery_run(
        test_db, query_budget=10_000, request_budget=10_000, configuration={}
    )
    test_db.commit()

    rows = plan_smoke_test_run(test_db, run, settings)

    assert len(rows) <= SMOKE_TEST_REQUEST_CEILING == 5
    assert run.request_budget <= 5
    assert all(row.adapter in {"serpapi_google", "serpapi_scholar"} for row in rows)


def test_smoke_test_plan_excludes_free_adapters(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_enabled_adapters="openalex,crossref")
    run = get_or_create_discovery_run(test_db, query_budget=100, request_budget=100, configuration={})
    test_db.commit()

    rows = plan_smoke_test_run(test_db, run, settings)
    assert rows == []  # no serpapi adapters enabled -> nothing to plan


class AlwaysFailingAdapter:
    """Every call raises a retryable error -- simulates a persistent SerpApi
    timeout/5xx/429 so the physical-request ceiling can be checked under the
    worst case, not just the happy path.
    """

    name = "serpapi_google"

    def __init__(self):
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        raise RetryableAdapterError("simulated persistent failure", error_type="timeout")


def test_smoke_test_execution_never_exceeds_physical_request_ceiling_even_with_retries_configured(
    test_db, tmp_path
):
    """Regression test for the exact gap found before the first live smoke
    test: `execute_run`'s retry policy otherwise comes from
    `settings.discovery_max_retries`, so with the realistic default (5) a
    persistently-failing SerpApi call would fan a single planned query out
    into up to 6 physical attempts -- up to 30 for a "5-request" smoke test.
    `execute_smoke_test_run` must force zero retries regardless of what
    `discovery_max_retries` says.
    """
    settings = _settings(
        tmp_path,
        discovery_enabled_adapters="serpapi_google,serpapi_scholar",
        discovery_query_budget=5,
        discovery_request_budget=5,
        # The realistic, dangerous default -- proves the override, not just
        # a test fixture that happened to already disable retries.
        discovery_max_retries=5,
    )
    run = get_or_create_discovery_run(test_db, query_budget=5, request_budget=5, configuration={})
    test_db.commit()
    rows = plan_smoke_test_run(test_db, run, settings)
    assert len(rows) == SMOKE_TEST_REQUEST_CEILING == 5

    failing_adapter = AlwaysFailingAdapter()
    adapters = {"serpapi_google": failing_adapter, "serpapi_scholar": failing_adapter}

    outcome = execute_smoke_test_run(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw"
    )

    # One physical attempt per planned query, zero retries -- never 6x that.
    assert failing_adapter.calls == 5
    assert run.requests_attempted == 5
    assert outcome.queries_failed == 5
    assert outcome.queries_executed == 0


def test_run_rerun_does_not_duplicate_candidates(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_query_budget=1, discovery_request_budget=5)
    run = get_or_create_discovery_run(test_db, query_budget=1, request_budget=5, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", [_cost_response()] * 10),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }
    execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    from discovery.db.models import SourceCandidate

    count_after_first = test_db.query(SourceCandidate).count()

    # Re-running the same plan (resume-style) against fresh responses of the
    # SAME underlying document must not create a second candidate.
    adapters2 = {
        "serpapi_google": FakeAdapter("serpapi_google", [_cost_response()] * 10),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }
    # Reset query_plan status to re-execute (simulating a resumed/rerun batch).
    from discovery.db.models import QueryPlan

    for qp in test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id):
        qp.status = "planned"
    test_db.commit()

    execute_run(test_db, run, settings=settings, adapters=adapters2, raw_response_dir=tmp_path / "raw2")
    count_after_second = test_db.query(SourceCandidate).count()

    assert count_after_second == count_after_first


def test_operational_failure_does_not_roll_back_prior_progress(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_query_budget=3, discovery_request_budget=10)
    run = get_or_create_discovery_run(test_db, query_budget=3, request_budget=10, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    from discovery.db.models import QueryPlan

    plan_count = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count()
    assert plan_count >= 2

    # First call succeeds, second raises an unexpected (non-AdapterError) bug,
    # remaining calls succeed too.
    responses = [_cost_response(), RuntimeError("unexpected bug")] + [_cost_response()] * 10
    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", responses),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }

    outcome = execute_run(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw"
    )

    # The run must finish (not crash), and at least one candidate from the
    # successful call(s) must have survived the failed iteration.
    assert outcome.status in ("completed", "failed")
    from discovery.db.models import SourceCandidate

    assert test_db.query(SourceCandidate).count() >= 1

    # The session must be usable afterwards -- no dangling failed transaction.
    test_db.query(QueryPlan).count()


def test_frontier_expansion_generates_new_queries_from_accepted_candidate(test_db, tmp_path):
    settings = _settings(
        tmp_path,
        discovery_query_budget=1,
        discovery_request_budget=5,
        discovery_max_expansion_depth=3,
        discovery_max_children_per_source=10,
        # Expansion queries target serpapi_scholar (exact-title/DOI, author
        # lookups); it must be enabled for expand_frontier to keep them.
        discovery_enabled_adapters="serpapi_google,openalex,serpapi_scholar",
    )
    run = get_or_create_discovery_run(test_db, query_budget=1, request_budget=5, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", [_cost_response()] * 10),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10),
    }
    execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    parents = expand_frontier(test_db, run, settings)
    from discovery.db.models import QueryPlan

    total_plans = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count()
    assert total_plans > 1  # expansion added at least one new query
    assert isinstance(parents, dict)


def test_execute_run_preserves_raw_url_as_direct_download_url(test_db, tmp_path):
    """Regression test for the URL-fetch defect found during the 2026-08-24
    acquisition: a hit's raw URL (here, with `www.`) must survive on the
    persisted candidate as `direct_download_url`, even though `canonical_url`
    is deliberately normalized (www stripped) for dedup purposes.
    """
    settings = _settings(tmp_path)
    run = get_or_create_discovery_run(test_db, query_budget=1, request_budget=5, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    raw_hit = AdapterSearchResponse(
        result_count=1,
        hits=[
            RawSearchHit(
                title="Preliminary Technical Risk Analysis for the Geothermal Technologies Program",
                url="https://www.nrel.gov/docs/fy07osti/41156.pdf",
                snippet="This government technical report presents drilling cost per foot data.",
                rank=1,
            )
        ],
        raw_response={},
    )
    google_adapter = FakeAdapter("serpapi_google", [raw_hit] * 10)
    openalex_adapter = FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 10)
    adapters = {"serpapi_google": google_adapter, "openalex": openalex_adapter}

    execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    from discovery.db.models import SourceCandidate

    candidate = (
        test_db.query(SourceCandidate)
        .filter(SourceCandidate.direct_download_url == "https://www.nrel.gov/docs/fy07osti/41156.pdf")
        .first()
    )
    assert candidate is not None
    assert candidate.canonical_url == "https://nrel.gov/docs/fy07osti/41156.pdf"  # www stripped, dedup key


def test_include_families_restricts_execution_to_the_named_kinds(test_db, tmp_path):
    """`include_families` is an allow-list: only pending QueryPlan rows whose
    `kind` is named execute in this call; everything else stays `planned` for
    a future batch. Added for the 2026-08-23 500-request campaign's staged
    execution -- the prior 60-request continuation had no such filter and had
    to fall back to a bespoke script outside the real orchestrator instead.
    """
    settings = _settings(tmp_path, discovery_query_budget=0, discovery_request_budget=10_000)
    run = get_or_create_discovery_run(test_db, query_budget=0, request_budget=10_000, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    from discovery.db.models import QueryPlan

    kinds_planned = {
        row[0] for row in test_db.query(QueryPlan.kind).filter(QueryPlan.discovery_run_id == run.id).distinct()
    }
    # Sanity check the new cost_representation/evidence_type templates are
    # actually reachable through the real planner, not just present in YAML.
    assert "cost_representation" in kinds_planned
    assert "evidence_type" in kinds_planned
    assert "cost_driver" in kinds_planned

    adapters = {
        "serpapi_google": FakeAdapter("serpapi_google", [_cost_response()] * 1000),
        "serpapi_scholar": FakeAdapter("serpapi_scholar", [_cost_response()] * 1000),
        "openalex": FakeAdapter("openalex", [AdapterSearchResponse(result_count=0, hits=[])] * 1000),
        "crossref": FakeAdapter("crossref", [AdapterSearchResponse(result_count=0, hits=[])] * 1000),
    }

    outcome = execute_run(
        test_db,
        run,
        settings=settings,
        adapters=adapters,
        raw_response_dir=tmp_path / "raw",
        include_families={"cost_representation"},
    )
    assert outcome.queries_executed > 0
    assert set(outcome.new_candidates_by_family) <= {"cost_representation"}

    completed_other_kinds = (
        test_db.query(QueryPlan)
        .filter(
            QueryPlan.discovery_run_id == run.id,
            QueryPlan.status == "completed",
            QueryPlan.kind != "cost_representation",
        )
        .count()
    )
    assert completed_other_kinds == 0

    completed_cost_representation = (
        test_db.query(QueryPlan)
        .filter(
            QueryPlan.discovery_run_id == run.id,
            QueryPlan.status == "completed",
            QueryPlan.kind == "cost_representation",
        )
        .count()
    )
    assert completed_cost_representation == outcome.queries_executed
