"""Persist planned queries, executions, and raw results -- the immutable registry.

Raw provider responses are written to disk **before** any parsing is
attempted (same discipline as geocost's extraction artifacts: a parse
failure must never lose the actual response), and only a path reference is
stored in `SearchExecution.raw_response_path`, keeping DB rows small.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from discovery.adapters.base import AdapterError
from discovery.db.models import DiscoveryRun, QueryPlan, SearchExecution, SearchResult
from discovery.models.candidate import AdapterSearchResponse
from discovery.models.query import PlannedQuery
from discovery.timeutils import utcnow

logger = logging.getLogger(__name__)

_SECRET_PARAM_NAMES = {"api_key", "authorization"}


def strip_secrets(params: dict) -> dict:
    """Remove any known-secret key before a params dict is persisted or logged."""
    return {k: v for k, v in params.items() if k.lower() not in _SECRET_PARAM_NAMES}


def get_or_create_discovery_run(
    db: Session,
    *,
    query_budget: int,
    request_budget: int,
    configuration: dict,
    created_by: str | None = None,
) -> DiscoveryRun:
    """Start a new `DiscoveryRun`. Callers resuming an existing run should load
    it by id directly instead (see `discovery.cli`'s `--resume`), not call this.
    """
    run = DiscoveryRun(
        status="running",
        configuration_json=json.dumps(configuration, default=str),
        query_budget=query_budget,
        request_budget=request_budget,
        requests_attempted=0,
        requests_succeeded=0,
        requests_failed=0,
        created_by=created_by,
    )
    db.add(run)
    db.flush()
    return run


def get_or_create_query_plan(
    db: Session, discovery_run_id: str, planned: PlannedQuery
) -> tuple[QueryPlan, bool]:
    """Idempotent within one run: re-planning never duplicates a `(run,
    fingerprint)` pair, so a resumed/rerun plan phase is a no-op for queries
    already planned.
    """
    existing = (
        db.query(QueryPlan)
        .filter(
            QueryPlan.discovery_run_id == discovery_run_id,
            QueryPlan.query_fingerprint == planned.query_fingerprint,
        )
        .first()
    )
    if existing is not None:
        return existing, False

    row = QueryPlan(
        discovery_run_id=discovery_run_id,
        query_fingerprint=planned.query_fingerprint,
        adapter=planned.adapter,
        kind=planned.kind,
        canonical_intent=planned.canonical_intent,
        rendered_query=planned.rendered_query,
        coverage_dimensions_json=planned.coverage_dimensions.model_dump_json(),
        language=planned.language,
        priority=planned.priority,
        status="planned",
    )
    db.add(row)
    db.flush()
    return row, True


def start_search_execution(
    db: Session, query_plan: QueryPlan, *, attempt_number: int, request_params: dict
) -> SearchExecution:
    """Record the start of one execution attempt, params already secret-stripped."""
    execution = SearchExecution(
        query_plan_id=query_plan.id,
        attempt_number=attempt_number,
        request_parameters_json=json.dumps(strip_secrets(request_params), default=str),
        result_count=0,
    )
    db.add(execution)
    query_plan.status = "executing"
    db.flush()
    return execution


def complete_search_execution_success(
    db: Session,
    execution: SearchExecution,
    query_plan: QueryPlan,
    response: AdapterSearchResponse,
    *,
    raw_response_dir: Path,
) -> list[SearchResult]:
    """Persist a successful response: raw payload to disk first, then one
    `SearchResult` row per hit, then mark the execution/plan complete.
    """
    raw_response_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_response_dir / f"{execution.id}.json"
    raw_path.write_text(json.dumps(response.raw_response, default=str), encoding="utf-8")

    execution.response_status = "success"
    execution.provider_request_id = response.provider_request_id
    execution.result_count = response.result_count
    execution.raw_response_path = str(raw_path)
    execution.completed_at = utcnow()
    execution.retryable = False

    results: list[SearchResult] = []
    for hit in response.hits:
        result = SearchResult(
            search_execution_id=execution.id,
            rank=hit.rank,
            title_raw=hit.title,
            url_raw=hit.url,
            snippet_raw=hit.snippet,
            displayed_source=hit.displayed_source,
            publication_info_raw=hit.publication_info,
            provider_result_id=hit.provider_result_id,
            cited_by_id=hit.cited_by_id,
            cluster_version_id=hit.cluster_version_id,
            raw_result_json=json.dumps(hit.raw, default=str),
        )
        db.add(result)
        results.append(result)

    query_plan.status = "completed"
    query_plan.pagination_cursor = response.next_page_cursor
    query_plan.executed_at = utcnow()
    db.flush()
    return results


def complete_search_execution_failure(
    db: Session, execution: SearchExecution, query_plan: QueryPlan, error: AdapterError
) -> None:
    """Persist a failed attempt without ever claiming success."""
    execution.response_status = error.error_type
    execution.error_type = error.error_type
    execution.retryable = error.retryable
    execution.completed_at = utcnow()
    query_plan.status = "failed" if not error.retryable else "planned"
    db.flush()
