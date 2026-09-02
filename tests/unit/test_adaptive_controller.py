"""Offline tests for adaptive_controller.py: quota accounting, batching,
saturation-based stopping, and resumability. No network calls -- every
adapter here is a scripted fake, matching the pattern in
tests/integration/test_budget_resume.py.
"""

from datetime import timedelta
from pathlib import Path

from discovery import quota_ledger
from discovery.adaptive_controller import QuotaTracker, estimate_dry_run, run_adaptive_discovery
from discovery.config import Settings
from discovery.db.models import DiscoveryRun, QueryPlan, QuotaLedgerEntry, SourceCandidate
from discovery.models.candidate import AdapterSearchResponse, RawSearchHit
from discovery.models.query import CoverageDimensions, PlannedQuery
from discovery.result_registry import get_or_create_discovery_run, get_or_create_query_plan
from discovery.timeutils import utcnow

_CONFIG = Path(__file__).resolve().parents[2] / "config"


class UniqueDocAdapter:
    """A fresh, novel document per call -- every batch finds new candidates
    and (via the real coverage matrix) new coverage cells, so it never
    saturates on its own; used to exercise quota accounting in isolation.
    """

    def __init__(self):
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        doc_id = f"doc-{query.query_fingerprint[:12]}-{self.calls}"
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title=f"Drilling Cost Report {doc_id}",
                    url=f"https://nrel.gov/reports/{doc_id}.pdf",
                    snippet="This government technical report presents drilling cost per foot data.",
                    rank=1,
                )
            ],
            raw_response={},
        )


class SaturatingAdapter:
    """Always returns the exact same document -- every call after the first
    is a duplicate, so this adapter can never contribute a new candidate.
    """

    def search(self, query, *, page_cursor):
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title="Enhanced Geothermal Systems Drilling Cost Report",
                    url="https://nrel.gov/reports/egs-drilling-cost.pdf",
                    snippet="This government technical report presents drilling cost per foot data.",
                    rank=1,
                )
            ],
            raw_response={},
        )


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        database_url="sqlite:///:memory:",
        discovery_enabled_adapters="serpapi_google,openalex,crossref",
        screening_rules_path=_CONFIG / "screening_rules.yaml",
        coverage_matrix_path=_CONFIG / "coverage_matrix.yaml",
        query_templates_path=_CONFIG / "query_templates.yaml",
        multilingual_terms_path=_CONFIG / "multilingual_terms.yaml",
        access_policy_path=_CONFIG / "access_policy.yaml",
        discovery_max_retries=0,
        discovery_retry_base_delay_seconds=0.001,
        discovery_retry_max_delay_seconds=0.002,
        adaptive_monthly_request_limit=1000,
        adaptive_hourly_request_limit=1000,
        adaptive_batch_size=3,
        adaptive_reserved_quota_fraction=0.0,
        adaptive_low_yield_max_new_accepted=0,
        adaptive_consecutive_low_yield_batches_to_stop=2,
    )
    base.update(overrides)
    return Settings(**base)


def _new_run(db, query_budget: int = 0) -> DiscoveryRun:
    run = get_or_create_discovery_run(db, query_budget=query_budget, request_budget=0, configuration={})
    db.commit()
    return run


def test_estimate_dry_run_reports_quota_without_spending_it(test_db, tmp_path):
    settings = _settings(
        tmp_path, adaptive_monthly_request_limit=100, adaptive_hourly_request_limit=50, campaign_max_requests=200
    )
    run = _new_run(test_db)

    estimate = estimate_dry_run(test_db, run, settings)

    assert estimate["unique_queries_planned"] > 0
    assert estimate["monthly_limit"] == 100
    assert estimate["monthly_remaining"] == 100  # untouched -- no requests made
    assert estimate["hourly_remaining"] == 50
    assert estimate["campaign_max_requests"] == 200
    assert estimate["campaign_remaining"] == 200  # untouched -- nothing reserved yet
    assert estimate["batch_size"] == settings.adaptive_batch_size
    assert estimate["worst_case_requests_including_retries"] == estimate["unique_queries_planned"] * (
        settings.serpapi_max_retries + 1
    )
    assert len(estimate["expected_stopping_checks"]) > 0
    assert test_db.query(SourceCandidate).count() == 0  # purely offline planning, nothing discovered


def test_quota_tracker_sums_only_within_the_hour_window(test_db):
    quota_ledger.reserve(test_db, "camp-a", max_requests=100)
    quota_ledger.reserve(test_db, "camp-a", max_requests=100)
    old_entry = QuotaLedgerEntry(
        campaign_id="camp-a", attempt_kind="initial", status="reserved", quota_before=2, quota_after=3,
        reserved_at=utcnow() - timedelta(hours=2),
    )
    test_db.add(old_entry)
    test_db.commit()

    tracker = QuotaTracker(test_db, campaign_id="camp-a", monthly_limit=1000, hourly_limit=1000)

    assert tracker.used_this_hour() == 2  # the 2 recent reserve() calls, not the manually-backdated one
    assert tracker.used_this_month() == 3  # all 3 fall inside the current calendar month


def test_run_adaptive_discovery_stops_at_quota_exhausted(test_db, tmp_path):
    """A campaign whose ledger is already fully spent (e.g. by a prior
    process/invocation) must refuse to run even a single batch.
    """
    settings = _settings(tmp_path, campaign_max_requests=5)
    run = _new_run(test_db)
    for _ in range(5):
        quota_ledger.reserve(test_db, settings.campaign_id, max_requests=5)
    adapters = {
        "serpapi_google": UniqueDocAdapter(),
        "openalex": UniqueDocAdapter(),
        "crossref": UniqueDocAdapter(),
    }

    result = run_adaptive_discovery(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw"
    )

    assert result.status == "quota_exhausted"
    assert len(result.batches) == 0  # the ledger was already exhausted before this call started
    assert run.status == "running"  # not a terminal state -- resumable once quota replenishes


def test_run_adaptive_discovery_respects_max_batches(test_db, tmp_path):
    settings = _settings(tmp_path, adaptive_monthly_request_limit=1000)
    run = _new_run(test_db)
    adapters = {
        "serpapi_google": UniqueDocAdapter(),
        "openalex": UniqueDocAdapter(),
        "crossref": UniqueDocAdapter(),
    }

    result = run_adaptive_discovery(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw", max_batches=1
    )

    assert result.status == "max_batches_reached"
    assert len(result.batches) == 1
    assert run.status == "running"  # a safety cap, not saturation -- still resumable


def test_run_adaptive_discovery_include_families_restricts_every_batch(test_db, tmp_path):
    """`include_families`, threaded through from the CLI's --include-families,
    must restrict EVERY batch this invocation runs (not just the first), and
    must combine with (not replace) the controller's own low-yield exclusion.
    Added for the 2026-08-23 500-request campaign's staged execution.
    """
    settings = _settings(tmp_path, adaptive_batch_size=3, adaptive_monthly_request_limit=1000)
    run = _new_run(test_db)
    adapters = {
        "serpapi_google": UniqueDocAdapter(),
        "openalex": UniqueDocAdapter(),
        "crossref": UniqueDocAdapter(),
    }

    result = run_adaptive_discovery(
        test_db,
        run,
        settings=settings,
        adapters=adapters,
        raw_response_dir=tmp_path / "raw",
        max_batches=3,
        include_families={"cost_representation"},
    )

    # These fake in-process adapters never call quota_ledger.reserve() (only
    # the real SerpApiClient wrapper does), so total_physical_requests_used
    # stays 0 by construction here -- queries_executed is the right signal.
    assert sum(b.queries_executed for b in result.batches) > 0
    for batch in result.batches:
        assert set(batch.new_candidates_by_family) <= {"cost_representation"}

    other_kind_completed = (
        test_db.query(QueryPlan)
        .filter(
            QueryPlan.discovery_run_id == run.id,
            QueryPlan.status == "completed",
            QueryPlan.kind != "cost_representation",
        )
        .count()
    )
    assert other_kind_completed == 0


def test_run_adaptive_discovery_reaches_saturated_status_after_low_yield_batches(test_db, tmp_path):
    settings = _settings(
        tmp_path,
        discovery_enabled_adapters="scripted",  # not a real template adapter -> plan_run() adds 0 rows
        adaptive_batch_size=1,
        adaptive_consecutive_low_yield_batches_to_stop=2,
    )
    run = _new_run(test_db)

    # Seed exactly 3 queries by hand, all sharing one (empty) coverage cell,
    # bypassing plan_run's real coverage-matrix/template machinery entirely.
    dims = CoverageDimensions()
    for i in range(3):
        planned = PlannedQuery(
            query_fingerprint=f"manual-saturation-{i}",
            adapter="scripted",
            kind="broad_domain",
            canonical_intent="manual saturation probe",
            rendered_query="egs drilling cost",
            coverage_dimensions=dims,
            priority=i,
        )
        get_or_create_query_plan(test_db, run.id, planned)
    test_db.commit()

    result = run_adaptive_discovery(
        test_db, run, settings=settings, adapters={"scripted": SaturatingAdapter()}, raw_response_dir=tmp_path / "raw"
    )

    assert result.status == "saturated"
    assert len(result.batches) == 3
    assert result.batches[0].new_coverage_cells == 1  # the shared cell opens once, in batch 1
    assert result.batches[0].newly_accepted == 1  # the one genuinely new (accepted) candidate
    assert result.batches[1].newly_accepted == 0  # every later hit is the same duplicate document
    assert result.batches[2].newly_accepted == 0
    assert test_db.query(SourceCandidate).count() == 1  # deduplicated, never re-inserted
    assert run.status == "completed"  # saturation is a genuine terminal state


_DISTINCT_WORDS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot",
    "Golf", "Hotel", "India", "Juliet", "Kilo", "Lima",
]


class FamilyAwareAdapter:
    """Behavior depends on the query's own `kind` (family): the
    "trusted_domain" family always returns the exact same duplicate
    document (never yields anything new after its first hit); the
    "named_project" family returns a fresh, unique document every call.

    Titles use a rotating distinct word (not just a trailing digit) so two
    different named_project hits never look similar enough to trip
    deduplicator.py's fuzzy-title tier (ratio >= 0.92) -- "Report doc-1" vs
    "Report doc-2" are near-identical strings and would otherwise collapse
    into the same candidate despite having different URLs.
    """

    def __init__(self):
        self.calls = 0

    def search(self, query, *, page_cursor):
        self.calls += 1
        if query.kind == "trusted_domain":
            return AdapterSearchResponse(
                result_count=1,
                hits=[
                    RawSearchHit(
                        title="Duplicate EGS Drilling Cost Report",
                        url="https://nrel.gov/reports/dup-egs-drilling-cost.pdf",
                        snippet="This government technical report presents drilling cost per foot data.",
                        rank=1,
                    )
                ],
                raw_response={},
            )
        word = _DISTINCT_WORDS[self.calls % len(_DISTINCT_WORDS)]
        doc_id = f"{word}-{self.calls}"
        return AdapterSearchResponse(
            result_count=1,
            hits=[
                RawSearchHit(
                    title=f"{doc_id} Drilling Cost Well Construction Report",
                    url=f"https://nrel.gov/reports/{doc_id}.pdf",
                    snippet="This government technical report presents drilling cost per foot data.",
                    rank=1,
                )
            ],
            raw_response={},
        )


def test_low_yield_query_family_is_excluded_from_future_batches(test_db, tmp_path):
    settings = _settings(
        tmp_path,
        discovery_enabled_adapters="scripted",
        adaptive_batch_size=2,
        campaign_batch_size=2,
        campaign_family_consecutive_low_yield_batches_to_stop=2,
        # Never trip the whole-campaign saturation stop -- isolate the
        # per-family mechanism from the whole-run one.
        adaptive_consecutive_low_yield_batches_to_stop=100,
    )
    run = _new_run(test_db)

    dims = CoverageDimensions()
    # 6 queries per family, interleaved by priority so each 2-query batch
    # picks up exactly one from each family.
    for i in range(6):
        for family, offset in (("trusted_domain", 0), ("named_project", 1)):
            planned = PlannedQuery(
                query_fingerprint=f"{family}-{i}",
                adapter="scripted",
                kind=family,
                canonical_intent=f"{family} probe {i}",
                rendered_query="egs drilling cost",
                coverage_dimensions=dims,
                priority=i * 2 + offset,
            )
            get_or_create_query_plan(test_db, run.id, planned)
    test_db.commit()

    result = run_adaptive_discovery(
        test_db,
        run,
        settings=settings,
        adapters={"scripted": FamilyAwareAdapter()},
        raw_response_dir=tmp_path / "raw",
        max_batches=6,
    )

    assert any("trusted_domain" in b.excluded_families for b in result.batches)
    # Once excluded, trusted_domain is never executed again -- it never
    # contributes to new_candidates_by_family in any subsequent batch.
    first_excluded_batch = next(b.batch_number for b in result.batches if "trusted_domain" in b.excluded_families)
    assert all(
        "trusted_domain" not in b.new_candidates_by_family
        for b in result.batches
        if b.batch_number > first_excluded_batch
    )
    # named_project was never excluded and keeps yielding new candidates
    # in the later batches trusted_domain lost.
    later_named_project_yield = sum(
        b.new_candidates_by_family.get("named_project", 0)
        for b in result.batches
        if b.batch_number > first_excluded_batch
    )
    assert later_named_project_yield > 0


def test_stage_checkpoint_is_emitted_when_crossing_a_stage_boundary(test_db, tmp_path):
    settings = _settings(
        tmp_path,
        discovery_enabled_adapters="scripted",
        adaptive_batch_size=1,
        campaign_batch_size=1,
        campaign_stage_a_max_requests=1,
        campaign_stage_b_max_requests=5,
        campaign_stage_c_max_requests=10,
        campaign_max_requests=10,
        adaptive_consecutive_low_yield_batches_to_stop=100,
    )
    run = _new_run(test_db)
    # Pre-reserve the ledger to just below stage A's 1-request boundary so
    # the very first batch's own bookkeeping crosses into stage A itself
    # (campaign_used_after=1 after that one batch's ledger delta -- here 0,
    # since the fake adapter never touches the ledger; the checkpoint logic
    # is exercised purely via quota_ledger.used()'s starting value).
    quota_ledger.get_or_create_campaign(test_db, settings.campaign_id, max_requests=10)

    dims = CoverageDimensions()
    for i in range(3):
        planned = PlannedQuery(
            query_fingerprint=f"stage-probe-{i}",
            adapter="scripted",
            kind="broad_domain",
            canonical_intent=f"stage probe {i}",
            rendered_query="egs drilling cost",
            coverage_dimensions=dims,
            priority=i,
        )
        get_or_create_query_plan(test_db, run.id, planned)
    test_db.commit()

    result = run_adaptive_discovery(
        test_db,
        run,
        settings=settings,
        adapters={"scripted": UniqueDocAdapter()},
        raw_response_dir=tmp_path / "raw",
        max_batches=3,
    )

    assert len(result.checkpoints) >= 1
    assert result.checkpoints[0].stage in ("A", "B", "C", "D")
    assert result.checkpoints[0].requests_used_at_checkpoint >= 0


def test_run_adaptive_discovery_is_resumable_across_invocations(test_db, tmp_path):
    """Simulates a real restart: the campaign ledger is a database row, so a
    campaign that ran out of quota, got its ceiling raised (e.g. next
    month), and is invoked again picks up exactly where it left off --
    already-completed query plans are never re-executed.
    """
    settings = _settings(tmp_path, campaign_max_requests=1)
    run = _new_run(test_db)
    # One unit already spent (by "some prior activity") leaves zero for
    # this first invocation to work with.
    quota_ledger.reserve(test_db, settings.campaign_id, max_requests=1)
    adapters = {
        "serpapi_google": UniqueDocAdapter(),
        "openalex": UniqueDocAdapter(),
        "crossref": UniqueDocAdapter(),
    }

    first = run_adaptive_discovery(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw1"
    )
    assert first.status == "quota_exhausted"
    completed_after_first = (
        test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id, QueryPlan.status == "completed").count()
    )
    assert completed_after_first == 0

    # Simulate the ceiling being raised (e.g. a new billing period) and
    # resume the SAME run.
    quota_ledger.set_campaign_ceiling(test_db, settings.campaign_id, max_requests=1000)
    settings.campaign_max_requests = 1000
    second = run_adaptive_discovery(
        test_db, run, settings=settings, adapters=adapters, raw_response_dir=tmp_path / "raw2", max_batches=2
    )

    completed_after_second = (
        test_db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id, QueryPlan.status == "completed").count()
    )
    assert completed_after_second > completed_after_first
    assert second.status in ("max_batches_reached", "quota_exhausted", "saturated", "no_more_queries")
