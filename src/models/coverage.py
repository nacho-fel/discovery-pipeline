"""Typed artifacts for coverage/saturation reporting and run summaries."""

from pydantic import BaseModel, Field


class CoverageCellSnapshot(BaseModel):
    """A point-in-time view of one coverage-matrix cell."""

    dimensions_fingerprint: str
    dimensions: dict
    query_count: int
    unique_result_count: int
    accepted_count: int
    last_marginal_yield: int
    consecutive_zero_yield_rounds: int
    saturation_status: str


class RunSummary(BaseModel):
    """The `discovery status`/`discovery report` summary for one run.

    Field set matches the brief's "Expose a run summary containing" list
    directly.
    """

    discovery_run_id: str
    status: str
    queries_planned: int
    queries_executed: int
    queries_cached: int
    queries_failed: int
    api_requests_attempted: int
    api_requests_remaining_budget: int
    raw_results: int
    unique_candidates: int
    duplicate_aliases: int
    accepted_count: int
    manual_review_count: int
    rejected_count: int
    downloads_attempted: int
    downloads_succeeded: int
    downloads_failed: int
    downloads_paywalled: int
    files_deduplicated_by_sha256: int
    candidates_handed_off: int
    coverage_gaps: list[str] = Field(default_factory=list)
    saturated_cells: int = 0
    active_cells: int = 0
