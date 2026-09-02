"""Offline tests for result-page pagination continuation
(service._maybe_queue_next_page): continues only while a page is actually
worth its cost, respects the configured max-pages ceiling, and never
confuses page depth with coverage. No network calls -- adapters are simple
in-process fakes.
"""

from pathlib import Path

from discovery.config import Settings
from discovery.db.models import QueryPlan, SourceCandidate
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import CoverageDimensions, PlannedQuery
from discovery.result_registry import get_or_create_discovery_run, get_or_create_query_plan
from discovery.service import execute_run

_CONFIG = Path(__file__).resolve().parents[2] / "config"

# Titles differing only by a trailing digit ("Page 0 Report" vs "Page 1
# Report") are similar enough to trip deduplicator.py's fuzzy-title tier
# (ratio >= 0.92), silently collapsing genuinely distinct pages' candidates
# into one -- see the equivalent fix in test_adaptive_controller.py's
# FamilyAwareAdapter. Rotating a distinct word avoids that.
_DISTINCT_WORDS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
    "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima",
]


class PagingAdapter:
    """Returns one fresh, unique document per page for up to `pages_with_yield`
    pages, then an empty (but still "has a next page") response forever --
    simulating a query whose results genuinely run out.
    """

    def __init__(self, pages_with_yield: int):
        self.pages_with_yield = pages_with_yield
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        page = int(page_cursor) if page_cursor else 0
        if page >= self.pages_with_yield:
            return AdapterSearchResponse(result_count=0, hits=[], next_page_cursor=str(page + 1))
        word = _DISTINCT_WORDS[page % len(_DISTINCT_WORDS)]
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title=f"{word} Drilling Cost Well Construction Report",
                    url=f"https://nrel.gov/reports/page-{page}.pdf",
                    snippet="This government technical report presents drilling cost per foot data.",
                    rank=1,
                )
            ],
            next_page_cursor=str(page + 1),
        )


class NoNextPageAdapter:
    def search(self, query, *, page_cursor):
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title="Single Page Drilling Cost Report",
                    url="https://nrel.gov/reports/single.pdf",
                    snippet="This government technical report presents drilling cost per foot data.",
                    rank=1,
                )
            ],
            next_page_cursor=None,
        )


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        discovery_enabled_adapters="scripted",
        screening_rules_path=_CONFIG / "screening_rules.yaml",
        coverage_matrix_path=_CONFIG / "coverage_matrix.yaml",
        query_templates_path=_CONFIG / "query_templates.yaml",
        multilingual_terms_path=_CONFIG / "multilingual_terms.yaml",
        access_policy_path=_CONFIG / "access_policy.yaml",
        discovery_max_retries=0,
        min_new_candidates_to_continue_pagination=1,
        discovery_max_pages_per_query=5,
    )
    base.update(overrides)
    return Settings(**base)


def _seed_run_with_one_query(db):
    run = get_or_create_discovery_run(db, query_budget=0, request_budget=100, configuration={})
    db.commit()
    planned = PlannedQuery(
        query_fingerprint="root-query",
        adapter="scripted",
        kind="broad_domain",
        canonical_intent="pagination probe",
        rendered_query="egs drilling cost",
        coverage_dimensions=CoverageDimensions(),
        priority=0,
    )
    get_or_create_query_plan(db, run.id, planned)
    db.commit()
    return run


def test_pagination_stops_immediately_when_there_is_no_next_page(test_db, tmp_path):
    settings = _settings(tmp_path)
    run = _seed_run_with_one_query(test_db)

    execute_run(
        test_db, run, settings=settings, adapters={"scripted": NoNextPageAdapter()}, raw_response_dir=tmp_path / "raw"
    )

    all_plans = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).all()
    assert len(all_plans) == 1  # no continuation queued at all


def test_pagination_continues_while_yield_holds_and_stops_when_it_drops(test_db, tmp_path):
    """3 pages of genuine yield, then a 4th empty page -- pagination must
    continue through pages 2-3 (each produced a new candidate) and stop
    right after the first page that produces nothing new, never blindly
    continuing to discovery_max_pages_per_query on inertia.
    """
    settings = _settings(tmp_path, discovery_max_pages_per_query=10)
    run = _seed_run_with_one_query(test_db)
    adapter = PagingAdapter(pages_with_yield=3)

    # Run repeatedly: each execute_run call only picks up currently-"planned"
    # rows, and a freshly-queued next-page row needs its own pass.
    for _ in range(6):
        execute_run(test_db, run, settings=settings, adapters={"scripted": adapter}, raw_response_dir=tmp_path / "raw")

    all_plans = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).all()
    # Page 0 (root) + pages 1, 2, 3 with yield = 4 executed rows that each
    # queued a next page; page 3's response (page index 3 >= pages_with_yield)
    # yields nothing, so no 5th row is ever queued.
    assert len(all_plans) == 4
    assert all(p.status == "completed" for p in all_plans)
    assert test_db.query(SourceCandidate).count() == 3  # exactly the 3 genuinely-yielding pages


def test_pagination_respects_max_pages_per_query_even_with_unlimited_yield(test_db, tmp_path):
    settings = _settings(tmp_path, discovery_max_pages_per_query=2)
    run = _seed_run_with_one_query(test_db)
    # Always yields -- without the page ceiling this would paginate forever.
    adapter = PagingAdapter(pages_with_yield=1000)

    for _ in range(6):
        execute_run(test_db, run, settings=settings, adapters={"scripted": adapter}, raw_response_dir=tmp_path / "raw")

    all_plans = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).all()
    assert len(all_plans) == 2  # capped at discovery_max_pages_per_query, despite endless yield


def test_pagination_never_causes_a_repeated_search_for_a_failed_landing_page(test_db, tmp_path):
    """Proves the "failed landing pages do not generate repeated searches"
    property structurally: nothing in execute_run/frontier.py triggers a
    new QueryPlan in response to an AcquisitionAttempt failure -- expansion
    only ever originates from accepted candidates' own metadata
    (frontier.build_expansion_queries), never from a download outcome.
    """
    from discovery.acquirer import AcquisitionOutcome, persist_acquisition_attempt

    settings = _settings(tmp_path)
    run = _seed_run_with_one_query(test_db)
    execute_run(
        test_db, run, settings=settings, adapters={"scripted": NoNextPageAdapter()}, raw_response_dir=tmp_path / "raw"
    )
    plans_before = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count()

    candidate = test_db.query(SourceCandidate).first()
    assert candidate is not None
    candidate.screening_status = "acquisition_pending"
    test_db.commit()
    failure = AcquisitionOutcome(status="failed", retryable=False, error_type="http_error", http_status_code=404)
    persist_acquisition_attempt(test_db, candidate, failure, attempt_number=1, url=candidate.canonical_url)
    test_db.commit()

    plans_after = test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id).count()
    assert plans_after == plans_before  # the download failure queued no new search whatsoever
