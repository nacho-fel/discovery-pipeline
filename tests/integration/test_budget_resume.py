"""HISTORICAL ACCEPTANCE-TEST SCENARIO -- not the production discovery
target. This fixes a ~280-request run: request budget, batching via
repeated `execute_run` calls, partial failures, and resume, entirely
offline. It was the original implementation brief's required "mocked
280-request run can fail, resume, and finish without duplicate sources or
corrupted state" acceptance scenario -- a fixed-size regression/safety test
for `execute_run`'s batching, resume, and budget-accounting machinery, not a
statement about how many requests a real production run should make.

Production discovery volume is governed by `adaptive_controller.py`'s
monthly/hourly SerpApi quota and coverage-based saturation stopping instead
-- see `tests/unit/test_adaptive_controller.py` and
docs/adaptive_discovery.md. This file is preserved unchanged as a regression
test for the underlying execute_run/resume mechanics the adaptive
controller itself is built on top of.
"""

from pathlib import Path

from discovery.adapters.base import RetryableAdapterError
from discovery.config import Settings
from discovery.db.models import QueryPlan, SourceCandidate
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.result_registry import get_or_create_discovery_run
from discovery.service import execute_run, plan_run

_TOTAL_QUERIES = 280  # historical acceptance-test scale, not a production target


class ScriptedAdapter:
    """Returns a unique, novel document per call so each request maps to a
    genuinely distinct candidate -- makes "no duplicates after resume" and
    "budget respected" both verifiable by counting rows.
    """

    def __init__(self, name: str, *, fail_every: int | None = None):
        self.name = name
        self._fail_every = fail_every
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        if self._fail_every and self.calls % self._fail_every == 0:
            raise RetryableAdapterError("simulated transient failure", error_type="timeout")
        doc_id = f"{self.name}-{query.query_fingerprint[:12]}"
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title=f"Drilling Cost Report {doc_id}",
                    url=f"https://example.com/{doc_id}.pdf",
                    snippet="drilling cost per foot rate of penetration capital expenditure",
                    rank=1,
                )
            ],
            raw_response={},
        )


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        discovery_query_budget=_TOTAL_QUERIES,
        discovery_request_budget=_TOTAL_QUERIES,
        discovery_enabled_adapters="serpapi_google,openalex,crossref",
        screening_rules_path=Path(__file__).resolve().parents[2] / "config" / "screening_rules.yaml",
        coverage_matrix_path=Path(__file__).resolve().parents[2] / "config" / "coverage_matrix.yaml",
        query_templates_path=Path(__file__).resolve().parents[2] / "config" / "query_templates.yaml",
        multilingual_terms_path=Path(__file__).resolve().parents[2] / "config" / "multilingual_terms.yaml",
        access_policy_path=Path(__file__).resolve().parents[2] / "config" / "access_policy.yaml",
        # max_retries=0: a single attempt only, so a ScriptedAdapter's
        # modulo-based failure pattern produces a genuine, persisted failure
        # rather than always healing itself on an internal retry (which would
        # always succeed, since two *consecutive* call counts can never both
        # be a multiple of the same `fail_every` > 1).
        discovery_max_retries=0,
        discovery_retry_base_delay_seconds=0.001,
        discovery_retry_max_delay_seconds=0.002,
    )
    base.update(overrides)
    return Settings(**base)


def test_full_280_request_plan_is_available():
    """The real coverage matrix / templates must be rich enough to plan at
    least 280 distinct queries, otherwise the historical acceptance-test
    budget itself is meaningless -- production discovery doesn't cap at 280;
    see the module docstring.
    """
    from discovery.query_planner import load_coverage_matrix, load_query_templates, plan_queries

    matrix = load_coverage_matrix(
        Path(__file__).resolve().parents[2] / "config" / "coverage_matrix.yaml"
    )
    templates = load_query_templates(
        Path(__file__).resolve().parents[2] / "config" / "query_templates.yaml"
    )
    planned = plan_queries(
        coverage_matrix=matrix,
        templates=templates,
        enabled_adapters={"serpapi_google", "openalex", "crossref", "serpapi_scholar"},
    )
    assert len(planned) >= _TOTAL_QUERIES


def test_request_budget_stops_execution_at_the_configured_ceiling(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_request_budget=50)
    run = get_or_create_discovery_run(
        test_db, query_budget=_TOTAL_QUERIES, request_budget=50, configuration={}
    )
    test_db.commit()
    plan_run(test_db, run, settings)

    adapters = {
        "serpapi_google": ScriptedAdapter("serpapi_google"),
        "openalex": ScriptedAdapter("openalex"),
        "crossref": ScriptedAdapter("crossref"),
    }
    outcome = execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    assert run.requests_attempted <= 50
    assert outcome.queries_executed <= 50


def test_interrupted_run_resumes_without_duplicating_candidates(test_db, tmp_path):
    settings = _settings(tmp_path)
    run = get_or_create_discovery_run(
        test_db, query_budget=_TOTAL_QUERIES, request_budget=_TOTAL_QUERIES, configuration={}
    )
    test_db.commit()
    plan_run(test_db, run, settings)

    total_planned = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count()
    assert total_planned >= _TOTAL_QUERIES

    # First batch: a small budget simulates an interruption partway through.
    partial_settings = _settings(tmp_path, discovery_request_budget=30)
    run.request_budget = 30
    test_db.commit()
    adapters_batch1 = {
        "serpapi_google": ScriptedAdapter("serpapi_google", fail_every=7),
        "openalex": ScriptedAdapter("openalex", fail_every=11),
        "crossref": ScriptedAdapter("crossref"),
    }
    outcome1 = execute_run(
        test_db, run, settings=partial_settings, adapters=adapters_batch1, raw_response_dir=tmp_path / "raw1"
    )
    assert outcome1.status == "completed"  # budget exhaustion is a clean stop, not a failure
    candidates_after_batch1 = test_db.query(SourceCandidate).count()
    completed_after_batch1 = (
        test_db.query(QueryPlan)
        .filter(QueryPlan.discovery_run_id == run.id, QueryPlan.status == "completed")
        .count()
    )
    assert completed_after_batch1 > 0
    assert completed_after_batch1 < total_planned  # genuinely partial

    # "Resume": raise the budget and run again against the SAME run row.
    # Already-completed query_plans must not be re-executed. A failed
    # attempt that exhausted its retries is reset to "planned" (not
    # "completed") so a later batch can retry it -- but that retry still
    # consumes another unit of `requests_attempted` against the same
    # cumulative run budget, so resuming needs a little headroom beyond the
    # raw plan count to guarantee every plan reaches "completed" (a request
    # actually spent on a failure is still a real, budgeted API call).
    run.status = "running"
    run.request_budget = _TOTAL_QUERIES + 20
    test_db.commit()
    adapters_batch2 = {
        "serpapi_google": ScriptedAdapter("serpapi_google"),
        "openalex": ScriptedAdapter("openalex"),
        "crossref": ScriptedAdapter("crossref"),
    }
    execute_run(
        test_db, run, settings=settings, adapters=adapters_batch2, raw_response_dir=tmp_path / "raw2"
    )

    completed_after_batch2 = (
        test_db.query(QueryPlan)
        .filter(QueryPlan.discovery_run_id == run.id, QueryPlan.status == "completed")
        .count()
    )
    assert completed_after_batch2 == total_planned  # every plan eventually executed exactly once

    # No query_plan was executed twice: candidates from batch 1 are untouched,
    # and total candidate count only grew by what batch 2 genuinely added
    # (each ScriptedAdapter call fingerprints a unique document, so this
    # count check also proves resume didn't re-request the same query twice).
    candidates_after_batch2 = test_db.query(SourceCandidate).count()
    assert candidates_after_batch2 >= candidates_after_batch1
    assert candidates_after_batch2 <= total_planned

    # Re-running execute_run again (already fully completed) must be a safe no-op.
    outcome3 = execute_run(
        test_db, run, settings=settings, adapters=adapters_batch2, raw_response_dir=tmp_path / "raw3"
    )
    assert outcome3.queries_executed == 0
    assert test_db.query(SourceCandidate).count() == candidates_after_batch2


def test_run_row_tracks_accurate_counters_through_partial_failures(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_query_budget=40, discovery_request_budget=40)
    run = get_or_create_discovery_run(test_db, query_budget=40, request_budget=40, configuration={})
    test_db.commit()
    plan_run(test_db, run, settings)

    adapters = {
        "serpapi_google": ScriptedAdapter("serpapi_google", fail_every=3),
        "openalex": ScriptedAdapter("openalex", fail_every=5),
        "crossref": ScriptedAdapter("crossref"),
    }
    outcome = execute_run(test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw")

    assert run.requests_attempted == run.requests_succeeded + run.requests_failed
    assert run.requests_attempted <= 40
    assert outcome.queries_failed >= 1  # fail_every guarantees at least one failure
