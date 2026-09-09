"""Run orchestration: planning, budgeted/batched/resumable execution, and the
discovery/normalize/dedup/screen pipeline glue.

Concurrency is accepted as a config value and stored on the run for the
record, but every request in this implementation is executed sequentially --
a SQLAlchemy `Session` is not thread-safe, so real parallel execution would
need a session-per-worker pattern; that's a documented extension point, not
implemented here. Everything else the brief asks for at this scale (budget,
batching, per-request commits, retry/backoff, resume, idempotent reruns) is
real.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from discovery import access_policy as access_policy_module
from discovery import coverage_analyzer, deduplicator, frontier
from discovery import screener as screener_module
from discovery.adapters.base import (
    AdapterError,
    NonRetryableAdapterError,
    RetryPolicy,
    SearchAdapter,
    call_with_retry,
)
from discovery.budget import SharedRequestBudget
from discovery.config import Settings
from discovery.db.models import (
    AcquisitionAttempt,
    CandidateAlias,
    DiscoveryRun,
    QueryPlan,
    ScreeningDecision,
    SourceCandidate,
)
from discovery.fingerprint import query_fingerprint
from discovery.models.candidate import AdapterSearchResponse, AggregatedCandidate
from discovery.models.coverage import RunSummary
from discovery.models.query import CoverageDimensions, PlannedQuery
from discovery.normalizer import normalize_doi, normalize_title, normalize_url
from discovery.query_planner import (
    load_coverage_matrix,
    load_multilingual_terms,
    load_query_templates,
    plan_multilingual_queries,
    plan_queries,
)
from discovery.result_registry import (
    complete_search_execution_failure,
    complete_search_execution_success,
    get_or_create_query_plan,
    start_search_execution,
)
from discovery.timeutils import utcnow

logger = logging.getLogger(__name__)

_MAX_CONSECUTIVE_OPERATIONAL_FAILURES = 10


@dataclass
class RunOutcome:
    discovery_run_id: str
    status: str
    queries_executed: int
    queries_failed: int
    new_candidates: int
    aliases_recorded: int
    # QueryPlan.kind -> new candidates it produced, this call only --
    # adaptive_controller.py's per-family yield-aware allocation reads this
    # directly rather than reconstructing it after the fact (CandidateAlias
    # rows use random UUID ids, not sequential ones, so "which alias was
    # this candidate's first" can't be recovered by sorting alias ids).
    new_candidates_by_family: dict[str, int] = field(default_factory=dict)


def plan_run(db: Session, run: DiscoveryRun, settings: Settings) -> list[QueryPlan]:
    """Build and persist the query plan for `run`, respecting `query_budget`.

    Idempotent: calling this twice for the same run never creates duplicate
    `QueryPlan` rows (see `result_registry.get_or_create_query_plan`).
    """
    coverage_matrix = load_coverage_matrix(settings.coverage_matrix_path)
    templates = load_query_templates(settings.query_templates_path)
    multilingual_terms = load_multilingual_terms(settings.multilingual_terms_path)
    enabled = set(settings.enabled_adapter_names())

    planned = plan_queries(coverage_matrix=coverage_matrix, templates=templates, enabled_adapters=enabled)
    planned += plan_multilingual_queries(terms=multilingual_terms, enabled_adapters=enabled)
    planned.sort(key=lambda q: q.priority)
    if run.query_budget:
        planned = planned[: run.query_budget]

    rows = []
    for candidate_query in planned:
        row, _created = get_or_create_query_plan(db, run.id, candidate_query)
        rows.append(row)
    db.commit()
    return rows


SMOKE_TEST_REQUEST_CEILING = 5
_SMOKE_TEST_ADAPTERS = {"serpapi_google", "serpapi_scholar"}


def plan_smoke_test_run(db: Session, run: DiscoveryRun, settings: Settings) -> list[QueryPlan]:
    """Build a *hard-capped* (<= 5 requests, never more) SerpApi-only plan.

    Distinct from `plan_run`/`run.request_budget` (which is operator-
    configurable and can be raised arbitrarily): this ceiling is a literal
    Python constant, not read from config or a CLI flag, so a live-smoke-test
    invocation can never accidentally exceed a handful of paid requests no
    matter what `.env`/CLI arguments are in effect. OpenAlex/Crossref/seed
    import are excluded entirely -- they're free and keyless, so "paid
    request" budgeting doesn't apply to them; this command exists
    specifically to bound SerpApi cost. See docs/live_smoke_test.md.
    """
    coverage_matrix = load_coverage_matrix(settings.coverage_matrix_path)
    templates = load_query_templates(settings.query_templates_path)
    multilingual_terms = load_multilingual_terms(settings.multilingual_terms_path)
    enabled = set(settings.enabled_adapter_names()) & _SMOKE_TEST_ADAPTERS

    planned = plan_queries(coverage_matrix=coverage_matrix, templates=templates, enabled_adapters=enabled)
    planned += plan_multilingual_queries(terms=multilingual_terms, enabled_adapters=enabled)
    planned.sort(key=lambda q: q.priority)
    planned = planned[:SMOKE_TEST_REQUEST_CEILING]

    run.query_budget = SMOKE_TEST_REQUEST_CEILING
    run.request_budget = SMOKE_TEST_REQUEST_CEILING

    rows = []
    for candidate_query in planned:
        row, _created = get_or_create_query_plan(db, run.id, candidate_query)
        rows.append(row)
    db.commit()
    return rows


def execute_smoke_test_run(
    db: Session,
    run: DiscoveryRun,
    *,
    settings: Settings,
    adapters: dict[str, SearchAdapter],
    raw_response_dir: Path,
) -> RunOutcome:
    """Execute a smoke-test run with retries forced off (`max_retries=0`).

    This is the actual physical-request guarantee, not just
    `plan_smoke_test_run`'s query-count cap: `execute_run`'s retry policy
    otherwise comes from `settings.discovery_max_retries` (default 5), which
    would let each of up to `SMOKE_TEST_REQUEST_CEILING` queries retry up to
    5 additional times on a transient failure -- up to 30 actual outbound
    HTTP requests for a "5-request" smoke test. Living here (not just as a
    CLI-side flag the caller has to remember to pass) means any caller of
    this specific function gets the guarantee regardless.

    A query that fails is simply recorded as failed after its single
    attempt -- never retried -- so `run.requests_attempted` (incremented once
    per query, before the call, same as `execute_run`) is the exact count of
    real outbound requests this can ever make, bounded by the query plan's
    own <= `SMOKE_TEST_REQUEST_CEILING` size.
    """
    zero_retry_policy = RetryPolicy(
        max_retries=0,
        base_delay_seconds=settings.discovery_retry_base_delay_seconds,
        max_delay_seconds=settings.discovery_retry_max_delay_seconds,
    )
    return execute_run(
        db,
        run,
        settings=settings,
        adapters=adapters,
        raw_response_dir=raw_response_dir,
        retry_policy=zero_retry_policy,
    )


def _to_aggregated(
    hit, *, adapter_name: str, access_policy: dict[str, str]
) -> AggregatedCandidate:
    canonical_url = normalize_url(hit.url)
    return AggregatedCandidate(
        canonical_url=canonical_url,
        # The raw, un-normalized URL as returned by the adapter -- preserved
        # so acquisition can fetch a URL that still resolves for hosts whose
        # DNS requires the `www.` prefix normalize_url() strips for dedup.
        # See AggregatedCandidate.direct_download_url's docstring.
        direct_download_url=hit.url,
        doi=normalize_doi(hit.doi),
        normalized_title=normalize_title(hit.title),
        authors=hit.authors,
        publication_year=hit.publication_year,
        source_type=None,
        evidence_tier=None,
        technology_domains=[],
        jurisdiction=None,
        language=None,
        access_status=access_policy_module.infer_access_status(
            canonical_url, policy=access_policy
        ),
        expected_evidence_categories=[],
    )


def _process_hits(
    db: Session,
    execution,
    results,
    query_plan: QueryPlan,
    screener: screener_module.RulesScreener,
    *,
    run_id: str,
    access_policy: dict[str, str],
    parent_candidate_id: str | None = None,
):
    """Normalize -> dedup -> record alias -> screen every result from one
    execution. Screening runs once per newly-touched candidate per call.

    If `parent_candidate_id` is set (this query_plan came from frontier
    expansion), every genuinely new candidate gets a `seeded_by` lineage edge
    back to the candidate whose metadata generated the expansion query.
    """
    new_candidates = 0
    aliases_recorded = 0
    touched: dict[str, SourceCandidate] = {}
    # A candidate touched by more than one hit in this batch keeps the last
    # snippet seen -- any one occurrence's snippet is strictly better signal
    # for screening than the `None` this loop used to hand the screener.
    latest_snippet_by_candidate_id: dict[str, str | None] = {}

    for result, hit in results:
        aggregated = _to_aggregated(
            hit, adapter_name=query_plan.adapter, access_policy=access_policy
        )
        if not aggregated.canonical_url and not aggregated.doi and not aggregated.normalized_title:
            continue

        candidate, created = deduplicator.get_or_create_candidate(db, aggregated)
        if created:
            new_candidates += 1
            if parent_candidate_id and parent_candidate_id != candidate.id:
                frontier.record_lineage_edge(
                    db,
                    parent_candidate_id=parent_candidate_id,
                    child_candidate_id=candidate.id,
                    relation_type="seeded_by",
                    discovery_run_id=run_id,
                )
        deduplicator.record_alias(
            db,
            candidate,
            occurrence_kind="search_result",
            search_result_id=result.id,
            search_execution_id=execution.id,
            matched_query_plan_id=query_plan.id,
            provider=query_plan.adapter,
            rank=hit.rank,
            title_raw=hit.title,
            url_raw=hit.url,
        )
        aliases_recorded += 1
        touched[candidate.id] = candidate
        latest_snippet_by_candidate_id[candidate.id] = hit.snippet

    # Same coverage cell for every candidate this query_plan touches -- one
    # lookup per call, not per candidate. Falls back to the screener's own
    # 0.5 default (via `screen()`'s keyword-arg default) when this query
    # plan carries no coverage dimensions at all (e.g. a frontier-expansion
    # query), rather than fabricating a fingerprint for an empty cell.
    novelty_kwargs = {}
    if query_plan.coverage_dimensions_json:
        dims = json.loads(query_plan.coverage_dimensions_json)
        novelty_kwargs["coverage_novelty_score"] = coverage_analyzer.coverage_novelty_score(db, dims)

    for candidate in touched.values():
        if candidate.screening_status != "deduplicated":
            continue
        screening_input = screener_module.ScreeningInput(
            title=candidate.normalized_title,
            snippet=latest_snippet_by_candidate_id.get(candidate.id),
            canonical_url=candidate.canonical_url,
            source_type=candidate.source_type,
            access_status=candidate.access_status,
            file_format=candidate.file_format,
            structured_data_likelihood=candidate.structured_data_likelihood,
        )
        result = screener.screen(screening_input, **novelty_kwargs)
        screener_module.persist_screening_decision(db, candidate, result)

    return new_candidates, aliases_recorded


def execute_run(
    db: Session,
    run: DiscoveryRun,
    *,
    settings: Settings,
    adapters: dict[str, SearchAdapter],
    raw_response_dir: Path,
    parent_by_query_plan_id: dict[str, str] | None = None,
    retry_policy: RetryPolicy | None = None,
    request_budget: SharedRequestBudget | None = None,
    batch_number: int | None = None,
    exclude_families: set[str] | None = None,
    include_families: set[str] | None = None,
) -> RunOutcome:
    """Execute every still-pending `QueryPlan` for `run`, batched and
    checkpointed: each `SearchExecution` (one request) commits independently,
    so an interruption or a single failed request never rolls back prior
    progress. Safe to call again on the same `run` (resume): completed plans
    are skipped.

    `retry_policy`, if given, overrides the policy `settings.discovery_max_retries`
    would otherwise build -- used by `execute_smoke_test_run` to guarantee a
    hard physical request ceiling that doesn't depend on `.env`/`Settings`
    (a query that fails with a retryable error is retried up to
    `policy.max_retries` additional times *per query*, so leaving this to
    `settings.discovery_max_retries` -- default 5 -- would let a handful of
    planned queries fan out into many times that many actual outbound HTTP
    requests).

    `request_budget`, if given, is passed through to `call_with_retry` so
    `budget.used` accumulates the true *physical* outbound-request count
    (initial attempt + every retry), independent of `run.requests_attempted`
    (which still counts one unit per query_plan, unchanged, for the existing
    budget-exhaustion check below) -- an in-process, best-effort ceiling;
    `quota_ledger.py`'s persisted, cross-process campaign ledger (wired in
    via `SerpApiClient`'s `quota_reserve`, bound when `adapters` was built --
    see `cli.py`'s `_adapters_for`) is the authoritative one.

    `batch_number`, if given, is threaded into `call_with_retry` (and from
    there, via `AttemptContext`, into every `quota_ledger.reserve()` call
    this batch of queries makes) purely for audit/reporting -- it plays no
    role in budget/stopping logic here.

    `exclude_families`, if given, skips any pending `QueryPlan` whose `kind`
    is in the set entirely (never even counted against `run.request_budget`)
    -- `adaptive_controller.py` uses this to stop spending batches on query
    families that have gone consistently low-yield, without needing to
    mutate or delete those `QueryPlan` rows (they simply wait for a future
    batch that doesn't exclude their family, or are picked up by `resume`).

    `include_families`, if given, is an allow-list: only pending `QueryPlan`
    rows whose `kind` is in the set are eligible at all (everything else is
    left untouched for a future batch, exactly like `exclude_families`).
    This is the counterpart operation -- added for the 2026-08-23 500-request
    campaign, whose staged plan needs to run a deliberately chosen family mix
    per stage rather than the `pending_plans` query's plain global-priority
    order, without resorting to a one-off script outside this function (the
    prior 60-request continuation had to do exactly that, and its bespoke
    per-query loop left `DiscoveryRun.status`/`requests_succeeded` out of
    sync with the real state -- see that run's provenance note). Both filters
    may be combined; `include_families` is applied first, `exclude_families`
    second, so an excluded family stays excluded even if also named in
    `include_families`.
    """
    screener = screener_module.RulesScreener.from_yaml(
        settings.screening_rules_path,
        accept_threshold=settings.discovery_screen_accept_threshold,
        reject_threshold=settings.discovery_screen_reject_threshold,
    )
    access_policy = access_policy_module.load_access_policy(settings.access_policy_path)
    explicit_retry_policy = retry_policy
    default_serpapi_retry_policy = RetryPolicy(
        max_retries=settings.serpapi_max_retries,
        base_delay_seconds=settings.discovery_retry_base_delay_seconds,
        max_delay_seconds=settings.discovery_retry_max_delay_seconds,
    )
    default_other_retry_policy = RetryPolicy(
        max_retries=settings.discovery_max_retries,
        base_delay_seconds=settings.discovery_retry_base_delay_seconds,
        max_delay_seconds=settings.discovery_retry_max_delay_seconds,
    )

    pending_plans = (
        db.query(QueryPlan)
        .filter(QueryPlan.discovery_run_id == run.id, QueryPlan.status.in_(["planned"]))
        .order_by(QueryPlan.priority)
        .all()
    )
    if include_families:
        pending_plans = [p for p in pending_plans if p.kind in include_families]
    if exclude_families:
        pending_plans = [p for p in pending_plans if p.kind not in exclude_families]

    queries_executed = 0
    queries_failed = 0
    total_new_candidates = 0
    total_aliases = 0
    new_candidates_by_family: dict[str, int] = {}
    consecutive_operational_failures = 0

    for query_plan in pending_plans:
        if run.requests_attempted >= run.request_budget:
            logger.info("service.request_budget_exhausted", extra={"discovery_run_id": run.id})
            break

        adapter = adapters.get(query_plan.adapter)
        if adapter is None:
            logger.warning("service.adapter_not_enabled", extra={"adapter": query_plan.adapter})
            query_plan.status = "skipped"
            db.commit()
            continue

        planned = PlannedQuery(
            query_fingerprint=query_plan.query_fingerprint,
            adapter=query_plan.adapter,
            kind=query_plan.kind or "broad_domain",
            canonical_intent=query_plan.canonical_intent,
            rendered_query=query_plan.rendered_query,
            coverage_dimensions=_load_dims(query_plan),
            language=query_plan.language,
            priority=query_plan.priority,
        )

        request_params = {"adapter": query_plan.adapter, "q": query_plan.rendered_query}
        execution = start_search_execution(
            db, query_plan, attempt_number=1, request_params=request_params
        )
        run.requests_attempted += 1
        # Commit before the network call so no transaction is held open across
        # it, and so this attempt's record survives even if processing the
        # response later fails and rolls back.
        db.commit()

        def _call_adapter(
            adapter=adapter, planned=planned, page_cursor=query_plan.pagination_cursor
        ):
            # Default-argument binding, not a bare closure: `adapter`/`planned`/
            # `page_cursor` are captured by value at definition time, so this
            # stays correct even though it's defined fresh inside a loop body.
            return adapter.search(planned, page_cursor=page_cursor)

        if explicit_retry_policy is not None:
            effective_retry_policy = explicit_retry_policy
        elif query_plan.adapter in ("serpapi_google", "serpapi_scholar"):
            effective_retry_policy = default_serpapi_retry_policy
        else:
            effective_retry_policy = default_other_retry_policy

        try:
            response = call_with_retry(
                _call_adapter,
                policy=effective_retry_policy,
                request_budget=request_budget,
                batch_number=batch_number,
                query_family=query_plan.kind,
                query_fingerprint=query_plan.query_fingerprint,
                discovery_run_id=run.id,
            )
        except NonRetryableAdapterError as exc:
            complete_search_execution_failure(db, execution, query_plan, exc)
            run.requests_failed += 1
            queries_failed += 1
            db.commit()
            if exc.error_type == "auth_error":
                run.status = "failed"
                run.failure_reason = f"auth failure on adapter {query_plan.adapter}: {exc}"
                db.commit()
                break
            consecutive_operational_failures += 1
            if consecutive_operational_failures >= _MAX_CONSECUTIVE_OPERATIONAL_FAILURES:
                run.status = "failed"
                run.failure_reason = "too many consecutive operational failures"
                db.commit()
                break
            continue
        except AdapterError as exc:
            complete_search_execution_failure(db, execution, query_plan, exc)
            run.requests_failed += 1
            queries_failed += 1
            consecutive_operational_failures += 1
            db.commit()
            if consecutive_operational_failures >= _MAX_CONSECUTIVE_OPERATIONAL_FAILURES:
                run.status = "failed"
                run.failure_reason = "too many consecutive operational failures"
                db.commit()
                break
            continue
        except Exception as exc:
            # must never crash the whole run or leave a dangling transaction;
            # see the module docstring's "raises RetryableAdapterError or
            # NonRetryableAdapterError" contract -- this is the safety net for
            # when a real (buggy) adapter doesn't honor it.
            db.rollback()
            logger.exception(
                "service.unexpected_adapter_failure",
                extra={"discovery_run_id": run.id, "query_plan_id": query_plan.id},
            )
            synthetic = NonRetryableAdapterError(str(exc), error_type="unexpected_error")
            complete_search_execution_failure(db, execution, query_plan, synthetic)
            run.requests_failed += 1
            queries_failed += 1
            consecutive_operational_failures += 1
            db.commit()
            if consecutive_operational_failures >= _MAX_CONSECUTIVE_OPERATIONAL_FAILURES:
                run.status = "failed"
                run.failure_reason = "too many consecutive operational failures"
                db.commit()
                break
            continue

        consecutive_operational_failures = 0

        # Everything from here on is response *processing*, not the network
        # call itself -- an unexpected bug here (not just a modeled
        # AdapterError) must still never crash the whole run or leave a
        # dangling uncommitted transaction behind. On any exception, roll
        # back just this iteration's partial writes; the previous iteration's
        # `db.commit()` already made its progress durable, so nothing earlier
        # is lost (see the brief's "a failed small batch must not corrupt
        # prior committed progress").
        try:
            results = complete_search_execution_success(
                db, execution, query_plan, response, raw_response_dir=raw_response_dir
            )
            run.requests_succeeded += 1
            queries_executed += 1

            new_candidates, aliases_recorded = _process_hits(
                db,
                execution,
                list(zip(results, response.hits, strict=True)),
                query_plan,
                screener,
                run_id=run.id,
                access_policy=access_policy,
                parent_candidate_id=(parent_by_query_plan_id or {}).get(query_plan.id),
            )
            total_new_candidates += new_candidates
            total_aliases += aliases_recorded
            family = query_plan.kind or "unknown"
            new_candidates_by_family[family] = new_candidates_by_family.get(family, 0) + new_candidates

            cell_dims = query_plan.coverage_dimensions_json
            if cell_dims:
                cell = coverage_analyzer.get_or_create_cell(db, json.loads(cell_dims))
                coverage_analyzer.record_query_executed(db, cell, discovery_run_id=run.id)
                coverage_analyzer.record_unique_results(db, cell, response.result_count)

            _maybe_queue_next_page(
                db, run, query_plan, response, settings=settings, new_candidates=new_candidates
            )

            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "service.unexpected_processing_failure",
                extra={"discovery_run_id": run.id, "query_plan_id": query_plan.id},
            )
            queries_failed += 1
            run.requests_failed += 1
            query_plan.status = "failed"
            db.commit()
            continue

    if run.status == "running":
        run.status = "completed"
    run.completed_at = utcnow()
    db.commit()

    return RunOutcome(
        discovery_run_id=run.id,
        status=run.status,
        queries_executed=queries_executed,
        queries_failed=queries_failed,
        new_candidates=total_new_candidates,
        aliases_recorded=total_aliases,
        new_candidates_by_family=new_candidates_by_family,
    )


def _load_dims(query_plan: QueryPlan) -> CoverageDimensions:
    raw = json.loads(query_plan.coverage_dimensions_json or "{}")
    return CoverageDimensions(**raw)


def _page_depth(db: Session, run_id: str, query_plan: QueryPlan) -> int:
    """How many QueryPlan rows already exist for this exact underlying query
    (same adapter/canonical_intent/rendered_query/language) -- i.e. how many
    pages of it have already been planned, page 1 included. Grouping by
    these fields rather than a dedicated page-number column avoids another
    migration; `QueryPlan` rows for different pages of the same query share
    everything except their fingerprint (which encodes `pagination_offset`,
    see `fingerprint.query_fingerprint`) and `pagination_cursor`.
    """
    return (
        db.query(QueryPlan)
        .filter(
            QueryPlan.discovery_run_id == run_id,
            QueryPlan.adapter == query_plan.adapter,
            QueryPlan.canonical_intent == query_plan.canonical_intent,
            QueryPlan.rendered_query == query_plan.rendered_query,
            QueryPlan.language == query_plan.language,
        )
        .count()
    )


def _maybe_queue_next_page(
    db: Session,
    run: DiscoveryRun,
    query_plan: QueryPlan,
    response: AdapterSearchResponse,
    *,
    settings: Settings,
    new_candidates: int,
) -> QueryPlan | None:
    """Queue the next page of `query_plan` only if the provider actually
    offered one *and* this page was worth the request: fewer than
    `settings.min_new_candidates_to_continue_pagination` new (non-duplicate)
    candidates means the well has run dry for this query, and result-page
    depth is a cost, not a coverage goal in itself, so pagination stops
    right there rather than continuing on inertia up to
    `discovery_max_pages_per_query`. Never queues a page number that would
    put this query's page depth at or beyond `discovery_max_pages_per_query`.

    Returns the newly-queued `QueryPlan` (status "planned", picked up by a
    later call to `execute_run`/an adaptive-controller batch), or `None` if
    no next page was queued.
    """
    if not response.next_page_cursor:
        return None
    if new_candidates < settings.min_new_candidates_to_continue_pagination:
        return None
    page_depth = _page_depth(db, run.id, query_plan)
    if page_depth >= settings.discovery_max_pages_per_query:
        return None

    dims = _load_dims(query_plan)
    planned = PlannedQuery(
        query_fingerprint=query_fingerprint(
            adapter=query_plan.adapter,
            canonical_intent=query_plan.canonical_intent,
            rendered_query=query_plan.rendered_query,
            language=query_plan.language,
            pagination_offset=page_depth,
        ),
        adapter=query_plan.adapter,
        kind=query_plan.kind or "broad_domain",
        canonical_intent=query_plan.canonical_intent,
        rendered_query=query_plan.rendered_query,
        coverage_dimensions=dims,
        language=query_plan.language,
        priority=query_plan.priority,
    )
    row, created = get_or_create_query_plan(db, run.id, planned)
    if created:
        row.pagination_cursor = response.next_page_cursor
        db.flush()
        logger.info(
            "service.pagination_continued",
            extra={
                "discovery_run_id": run.id,
                "query_fingerprint": query_plan.query_fingerprint,
                "page_depth": page_depth,
                "new_candidates_this_page": new_candidates,
            },
        )
        return row
    return None


_EXPANSION_ELIGIBLE_STATUSES = (
    "acquisition_pending",
    "downloaded",
    "validated",
    "ingestion_ready",
    "handed_off",
)


def expand_frontier(db: Session, run: DiscoveryRun, settings: Settings) -> dict[str, str]:
    """Generate and persist expansion `QueryPlan`s from every accepted candidate.

    Respects `frontier.py`'s depth/child budgets. Returns
    `{query_plan_id: parent_candidate_id}` so a subsequent `execute_run` call
    can record lineage edges once those queries actually produce results.
    """
    parent_by_plan: dict[str, str] = {}
    candidates = (
        db.query(SourceCandidate)
        .filter(SourceCandidate.screening_status.in_(_EXPANSION_ELIGIBLE_STATUSES))
        .all()
    )
    for candidate in candidates:
        queries = frontier.build_expansion_queries(
            db,
            candidate,
            max_expansion_depth=settings.discovery_max_expansion_depth,
            max_children_per_source=settings.discovery_max_children_per_source,
        )
        for planned_query in queries:
            if planned_query.adapter not in settings.enabled_adapter_names():
                continue
            row, created = get_or_create_query_plan(db, run.id, planned_query)
            if created:
                parent_by_plan[row.id] = candidate.id
    db.commit()
    return parent_by_plan


def build_run_summary(db: Session, run: DiscoveryRun) -> RunSummary:
    """Build the `discovery status`/`discovery report` summary for `run`.

    Query/candidate counts are scoped to plans/executions that belong to
    `run`; candidate-lifecycle counts (accepted/rejected/handed-off etc.) are
    reported cumulatively across the whole database, since a candidate's
    final disposition can be reached via a later run than the one that first
    discovered it.
    """
    query_plans = db.query(QueryPlan).filter(QueryPlan.discovery_run_id == run.id)
    queries_planned = query_plans.count()
    queries_executed = query_plans.filter(QueryPlan.status == "completed").count()
    queries_failed = query_plans.filter(QueryPlan.status == "failed").count()

    raw_results = (
        db.query(CandidateAlias)
        .filter(CandidateAlias.occurrence_kind == "search_result")
        .count()
    )
    unique_candidates = db.query(SourceCandidate).count()
    duplicate_aliases = db.query(CandidateAlias).count() - unique_candidates

    accepted = db.query(ScreeningDecision).filter(ScreeningDecision.decision == "accept").count()
    manual_review = (
        db.query(ScreeningDecision).filter(ScreeningDecision.decision == "manual_review").count()
    )
    rejected = db.query(ScreeningDecision).filter(ScreeningDecision.decision == "reject").count()

    downloads_attempted = db.query(AcquisitionAttempt).count()
    downloads_succeeded = (
        db.query(AcquisitionAttempt).filter(AcquisitionAttempt.status == "succeeded").count()
    )
    downloads_failed = (
        db.query(AcquisitionAttempt).filter(AcquisitionAttempt.status == "failed").count()
    )
    downloads_paywalled = (
        db.query(AcquisitionAttempt)
        .filter(AcquisitionAttempt.error_type == "paywalled_or_forbidden")
        .count()
    )
    handed_off = (
        db.query(SourceCandidate).filter(SourceCandidate.screening_status == "handed_off").count()
    )
    deduped_by_sha = (
        db.query(SourceCandidate.sha256)
        .filter(SourceCandidate.sha256.isnot(None))
        .distinct()
        .count()
    )

    coverage_summary = coverage_analyzer.summarize_coverage(db)

    return RunSummary(
        discovery_run_id=run.id,
        status=run.status,
        queries_planned=queries_planned,
        queries_executed=queries_executed,
        queries_cached=0,
        queries_failed=queries_failed,
        api_requests_attempted=run.requests_attempted,
        api_requests_remaining_budget=max(0, run.request_budget - run.requests_attempted),
        raw_results=raw_results,
        unique_candidates=unique_candidates,
        duplicate_aliases=max(0, duplicate_aliases),
        accepted_count=accepted,
        manual_review_count=manual_review,
        rejected_count=rejected,
        downloads_attempted=downloads_attempted,
        downloads_succeeded=downloads_succeeded,
        downloads_failed=downloads_failed,
        downloads_paywalled=downloads_paywalled,
        files_deduplicated_by_sha256=deduped_by_sha,
        candidates_handed_off=handed_off,
        active_cells=coverage_summary.get("active", 0),
        saturated_cells=coverage_summary.get("saturated", 0),
    )
