"""SQLAlchemy ORM models for the discovery pipeline.

Conventions deliberately mirror geocost's `db/models.py` (audited directly):
every primary key is a plain `String(36)` populated with `str(uuid.uuid4())`
(no dialect-specific UUID type, so SQLite and Postgres behave identically),
every status/state field is a `String(N)` with an inline comment enumerating
its known states (no Python or SQLAlchemy `Enum` type -- state transitions are
validated in code, see `discovery.state_machine`), timestamps default to
`timeutils.utcnow` (a naive-UTC value computed via the non-deprecated
`datetime.now(UTC)` API -- see `discovery/timeutils.py`), and unique/index
declarations live in each class's `__table_args__` tuple using the `uq_*` /
`idx_*` naming convention.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from discovery.timeutils import utcnow as _utcnow


class Base(DeclarativeBase):
    """Base class for all discovery-pipeline models."""


def _uuid() -> str:
    return str(uuid.uuid4())


class DiscoveryRun(Base):
    """One bounded, resumable discovery run: a budgeted batch of adapter queries."""

    __tablename__ = "discovery_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # running, completed, failed, cancelled
    status: Mapped[str] = mapped_column(String(32), default="running")
    configuration_json: Mapped[str | None] = mapped_column(Text)
    query_budget: Mapped[int] = mapped_column(Integer, default=0)
    request_budget: Mapped[int] = mapped_column(Integer, default=0)
    requests_attempted: Mapped[int] = mapped_column(Integer, default=0)
    requests_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    requests_failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(128))
    version_metadata_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    query_plans: Mapped[list["QueryPlan"]] = relationship(
        "QueryPlan", back_populates="discovery_run"
    )

    __table_args__ = (Index("idx_discovery_run_status", "status"),)


class QueryPlan(Base):
    """One planned, fingerprinted query against one adapter, scoped to a run.

    Uniqueness is scoped to `(discovery_run_id, query_fingerprint)`, not global:
    re-planning the same run is idempotent (no duplicate rows), while a fresh
    run intentionally gets its own fresh `QueryPlan` rows even for an identical
    rendered query, so per-run counters/budgets stay accurate.
    """

    __tablename__ = "query_plan"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    discovery_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("discovery_run.id"), nullable=False
    )
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    # broad_domain, cost_component, cost_driver, named_project, trusted_domain,
    # exact_title_doi, author_organization, citation_expansion,
    # dataset_model_name, coverage_gap -- the "query family" campaign-level
    # yield-aware allocation (adaptive_controller.py) groups by. Added in the
    # 2026-08-21 quota-ledger audit; previously this was computed at plan
    # time (PlannedQuery.kind) but never persisted, so it was unavailable at
    # execution time for per-family accounting.
    kind: Mapped[str | None] = mapped_column(String(32))
    canonical_intent: Mapped[str] = mapped_column(String(256), nullable=False)
    rendered_query: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_dimensions_json: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="en")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # planned, executing, completed, failed, skipped
    status: Mapped[str] = mapped_column(String(32), default="planned")
    pagination_cursor: Mapped[str | None] = mapped_column(String(64))
    planned_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)

    discovery_run: Mapped["DiscoveryRun"] = relationship(
        "DiscoveryRun", back_populates="query_plans"
    )
    search_executions: Mapped[list["SearchExecution"]] = relationship(
        "SearchExecution", back_populates="query_plan"
    )

    __table_args__ = (
        UniqueConstraint(
            "discovery_run_id", "query_fingerprint", name="uq_query_plan_run_fingerprint"
        ),
        Index("idx_query_plan_status", "status"),
        Index("idx_query_plan_run", "discovery_run_id"),
    )


class SearchExecution(Base):
    """One HTTP attempt (possibly a retry) at executing a `QueryPlan`."""

    __tablename__ = "search_execution"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    query_plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("query_plan.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    request_parameters_json: Mapped[str | None] = mapped_column(Text)
    # success, http_error, timeout, rate_limited, auth_error, parse_error
    response_status: Mapped[str | None] = mapped_column(String(32))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    raw_response_path: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_type: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None] = mapped_column()

    query_plan: Mapped["QueryPlan"] = relationship(
        "QueryPlan", back_populates="search_executions"
    )
    search_results: Mapped[list["SearchResult"]] = relationship(
        "SearchResult", back_populates="search_execution"
    )

    __table_args__ = (
        Index("idx_search_execution_query_plan", "query_plan_id"),
        Index("idx_search_execution_status", "response_status"),
    )


class SearchResult(Base):
    """One raw, unmodified result row exactly as a provider returned it."""

    __tablename__ = "search_result"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    search_execution_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("search_execution.id"), nullable=False
    )
    rank: Mapped[int | None] = mapped_column(Integer)
    title_raw: Mapped[str | None] = mapped_column(Text)
    url_raw: Mapped[str | None] = mapped_column(Text)
    snippet_raw: Mapped[str | None] = mapped_column(Text)
    displayed_source: Mapped[str | None] = mapped_column(String(256))
    publication_info_raw: Mapped[str | None] = mapped_column(Text)
    provider_result_id: Mapped[str | None] = mapped_column(String(256))
    cited_by_id: Mapped[str | None] = mapped_column(String(256))
    cluster_version_id: Mapped[str | None] = mapped_column(String(256))
    raw_result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    search_execution: Mapped["SearchExecution"] = relationship(
        "SearchExecution", back_populates="search_results"
    )
    aliases: Mapped[list["CandidateAlias"]] = relationship(
        "CandidateAlias", back_populates="search_result"
    )

    __table_args__ = (Index("idx_search_result_execution", "search_execution_id"),)


class SourceCandidate(Base):
    """One deduplicated, canonical candidate document/dataset.

    Exact-identity uniqueness (DOI, canonical URL) is enforced here at the DB
    level; the weaker title+year / fuzzy tiers are handled in
    `discovery.deduplicator` and only ever merge into an *existing* row found
    via one of these exact keys or via explicit review, never silently create
    a second exact-DOI or exact-URL row.
    """

    __tablename__ = "source_candidate"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    canonical_url: Mapped[str | None] = mapped_column(String(1024))
    doi: Mapped[str | None] = mapped_column(String(255))
    normalized_title: Mapped[str | None] = mapped_column(Text)
    authors_json: Mapped[str | None] = mapped_column(Text)
    organization: Mapped[str | None] = mapped_column(String(256))
    publication_year: Mapped[int | None] = mapped_column(Integer)
    source_type: Mapped[str | None] = mapped_column(String(64))
    evidence_tier: Mapped[str | None] = mapped_column(String(64))
    technology_domains_json: Mapped[str | None] = mapped_column(Text)
    jurisdiction: Mapped[str | None] = mapped_column(String(64))
    language: Mapped[str | None] = mapped_column(String(16))
    # open_access, licensed_mit_access, authentication_required,
    # manual_acquisition_required, metadata_only, unavailable -- see
    # access_policy.py's ACCESS_STATES for the definition of each.
    access_status: Mapped[str | None] = mapped_column(String(32))
    # discovered, normalized, deduplicated, screened_accept, screened_review,
    # screened_reject, acquisition_pending, downloaded, validated,
    # ingestion_ready, handed_off, download_failed, paywalled, metadata_only,
    # unsupported_format, corrupt_file, metadata_incomplete, validation_failed,
    # manual_review_required
    screening_status: Mapped[str] = mapped_column(String(32), default="discovered")
    relevance_score: Mapped[float | None] = mapped_column()
    expected_evidence_categories_json: Mapped[str | None] = mapped_column(Text)
    discovery_occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    best_result_rank: Mapped[int | None] = mapped_column(Integer)
    candidate_key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    canonical_source_document_id: Mapped[str | None] = mapped_column(String(36))
    local_acquired_path: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64))

    # --- "research resource" fields (not just "document") ---
    # The URL a file can actually be streamed from, when known and distinct
    # from `canonical_url` (which may be a landing page listing several
    # assets -- see ResourceAsset for the one-to-many case).
    direct_download_url: Mapped[str | None] = mapped_column(String(1024))
    # pdf, xlsx, xls, csv, tsv, zip, html_table, json, xml, docx, pptx,
    # dataset_landing_page, metadata_only
    file_format: Mapped[str | None] = mapped_column(String(32))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    # 0.0-1.0 heuristic: how likely this resource contains structured/
    # tabular data (a spreadsheet, a CSV, an HTML data table) rather than
    # narrative prose -- see screener.py's structured-data ranking boost.
    structured_data_likelihood: Mapped[float | None] = mapped_column()
    # JSON list drawn from {"cost", "technical", "geology", "resource_potential"}
    # -- which geocost observation families this resource is expected to
    # yield, used to prioritize cost/technical (primary) over geology/
    # resource-potential (supporting) per the production ranking objective.
    expected_observation_families_json: Mapped[str | None] = mapped_column(Text)
    expected_cost_observation_yield: Mapped[int | None] = mapped_column(Integer)
    expected_technical_observation_yield: Mapped[int | None] = mapped_column(Integer)
    # JSON list of cost-scope tags this resource likely covers (e.g.
    # "drilling", "completion", "stimulation") -- see coverage_analyzer.py's
    # coverage-novelty dimensions.
    cost_scopes_json: Mapped[str | None] = mapped_column(Text)
    # direct_download, mit_library_lookup, manual_request, repository_browse
    access_route: Mapped[str | None] = mapped_column(String(64))
    publisher: Mapped[str | None] = mapped_column(String(256))
    license_info: Mapped[str | None] = mapped_column(String(256))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    aliases: Mapped[list["CandidateAlias"]] = relationship(
        "CandidateAlias", back_populates="source_candidate"
    )
    screening_decisions: Mapped[list["ScreeningDecision"]] = relationship(
        "ScreeningDecision", back_populates="source_candidate"
    )
    acquisition_attempts: Mapped[list["AcquisitionAttempt"]] = relationship(
        "AcquisitionAttempt", back_populates="source_candidate"
    )
    assets: Mapped[list["ResourceAsset"]] = relationship(
        "ResourceAsset", back_populates="parent_candidate"
    )

    __table_args__ = (
        UniqueConstraint("doi", name="uq_source_candidate_doi"),
        UniqueConstraint("canonical_url", name="uq_source_candidate_canonical_url"),
        Index("idx_source_candidate_status", "screening_status"),
        Index("idx_source_candidate_sha256", "sha256"),
        Index("idx_source_candidate_fingerprint", "candidate_key_fingerprint"),
    )


class ResourceAsset(Base):
    """One individual downloadable asset belonging to a parent resource.

    A `SourceCandidate` is a "research resource" (per the redefinition from
    "document" to resource), which can be a single file (the common case,
    where this table stays empty for it) or a landing page/dataset that
    contains several distinct downloadable assets -- a GDR submission with a
    main report PDF plus a data workbook, a ScienceDirect article with
    supplementary spreadsheets, a repository page listing multiple file
    formats of the same dataset. The parent `SourceCandidate` row is always
    preserved (never deleted) even when it has zero assets that turned out
    downloadable -- see `frontier.py`'s repository-resource expansion.
    """

    __tablename__ = "resource_asset"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    parent_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    asset_url: Mapped[str] = mapped_column(Text, nullable=False)
    direct_download_url: Mapped[str | None] = mapped_column(Text)
    # pdf, xlsx, xls, csv, tsv, zip, html_table, json, xml, docx, pptx
    file_format: Mapped[str | None] = mapped_column(String(32))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(512))
    structured_data_likelihood: Mapped[float | None] = mapped_column()
    # Same six-state vocabulary as SourceCandidate.access_status -- an asset
    # can have a different access status than its parent landing page (e.g.
    # an open abstract page linking to a licensed full-text PDF).
    access_status: Mapped[str | None] = mapped_column(String(32))
    sha256: Mapped[str | None] = mapped_column(String(64))
    local_acquired_path: Mapped[str | None] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    parent_candidate: Mapped["SourceCandidate"] = relationship(
        "SourceCandidate", back_populates="assets"
    )

    __table_args__ = (
        Index("idx_resource_asset_parent", "parent_candidate_id"),
        Index("idx_resource_asset_sha256", "sha256"),
        UniqueConstraint(
            "parent_candidate_id", "asset_url", name="uq_resource_asset_parent_url"
        ),
    )


class CandidateAlias(Base):
    """Every raw occurrence (search hit, seed import, citation mention) of a candidate.

    Never deleted, even after merging into an existing `SourceCandidate` --
    this is the audit trail that lets coverage/discoverability reporting count
    "how many times did we find this" independent of the dedup outcome.
    """

    __tablename__ = "candidate_alias"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    search_result_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("search_result.id")
    )
    search_execution_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("search_execution.id")
    )
    matched_query_plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("query_plan.id")
    )
    provider: Mapped[str | None] = mapped_column(String(64))
    rank: Mapped[int | None] = mapped_column(Integer)
    # search_result, seed_import, citation_expansion, manual_url
    occurrence_kind: Mapped[str | None] = mapped_column(String(32))
    title_raw: Mapped[str | None] = mapped_column(Text)
    url_raw: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    source_candidate: Mapped["SourceCandidate"] = relationship(
        "SourceCandidate", back_populates="aliases"
    )
    search_result: Mapped["SearchResult | None"] = relationship(
        "SearchResult", back_populates="aliases"
    )

    __table_args__ = (Index("idx_candidate_alias_candidate", "source_candidate_id"),)


class ScreeningDecision(Base):
    """One relevance-screening decision for a candidate, fully versioned/reproducible."""

    __tablename__ = "screening_decision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    # accept, manual_review, reject
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    direct_cost_evidence_score: Mapped[float | None] = mapped_column()
    technical_driver_evidence_score: Mapped[float | None] = mapped_column()
    domain_relevance_score: Mapped[float | None] = mapped_column()
    source_quality_score: Mapped[float | None] = mapped_column()
    accessibility_score: Mapped[float | None] = mapped_column()
    coverage_novelty_score: Mapped[float | None] = mapped_column()
    composite_score: Mapped[float | None] = mapped_column()
    reason_codes_json: Mapped[str | None] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    reviewer: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    source_candidate: Mapped["SourceCandidate"] = relationship(
        "SourceCandidate", back_populates="screening_decisions"
    )

    __table_args__ = (
        Index("idx_screening_decision_candidate", "source_candidate_id"),
        Index("idx_screening_decision_decision", "decision"),
    )


class AcquisitionAttempt(Base):
    """One download attempt for an accepted candidate."""

    __tablename__ = "acquisition_attempt"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_url: Mapped[str | None] = mapped_column(Text)
    # pending, downloading, succeeded, failed, paywalled, blocked_by_safety_policy
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status_code: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(128))
    bytes_downloaded: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    retryable: Mapped[bool | None] = mapped_column()
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    source_candidate: Mapped["SourceCandidate"] = relationship(
        "SourceCandidate", back_populates="acquisition_attempts"
    )

    __table_args__ = (
        Index("idx_acquisition_attempt_candidate", "source_candidate_id"),
        Index("idx_acquisition_attempt_status", "status"),
    )


class SourceLineageEdge(Base):
    """A directed provenance/relation edge between two candidates."""

    __tablename__ = "source_lineage_edge"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    parent_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    child_candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_candidate.id"), nullable=False
    )
    # seeded_by, cites, cited_by, same_work_version, same_project, same_dataset,
    # same_author, mentioned_in
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    discovery_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("discovery_run.id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "parent_candidate_id",
            "child_candidate_id",
            "relation_type",
            name="uq_source_lineage_edge",
        ),
        Index("idx_lineage_edge_parent", "parent_candidate_id"),
        Index("idx_lineage_edge_child", "child_candidate_id"),
    )


class CoverageCell(Base):
    """One cell of the coverage matrix, tracked cumulatively across all runs.

    Not scoped to a single `discovery_run` (coverage/saturation is a
    whole-corpus concept) -- `last_discovery_run_id` records only which run
    most recently updated the cell, for provenance.
    """

    __tablename__ = "coverage_cell"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dimensions_json: Mapped[str] = mapped_column(Text, nullable=False)
    dimensions_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    query_count: Mapped[int] = mapped_column(Integer, default=0)
    unique_result_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    last_marginal_yield: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_zero_yield_rounds: Mapped[int] = mapped_column(Integer, default=0)
    # active, saturated, not_yet_queried
    saturation_status: Mapped[str] = mapped_column(String(32), default="not_yet_queried")
    last_discovery_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("discovery_run.id")
    )
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("dimensions_fingerprint", name="uq_coverage_cell_fingerprint"),
        Index("idx_coverage_cell_status", "saturation_status"),
    )


class QuotaLedgerState(Base):
    """One row per campaign: the durable, atomically-updated physical-request
    counter a hard ceiling is enforced against. See `quota_ledger.py`.

    Deliberately a single mutable counter row per campaign (not derived by
    summing ledger entries on every check) so `reserve()` can enforce the
    ceiling with one atomic `UPDATE ... WHERE reserved_count < max_requests`
    statement -- correct under concurrent writers because SQLite (and any
    other SQL engine) serializes conflicting writes to the same row, so two
    concurrent reservations can never both observe room under the ceiling
    and both succeed.
    """

    __tablename__ = "quota_ledger_state"

    campaign_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    max_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class QuotaLedgerEntry(Base):
    """One row per physical SerpApi HTTP attempt (initial or retry),
    written atomically with its reservation -- the audit trail requirement
    9's ledger fields (query fingerprint, campaign/batch, family,
    attempt kind, retry reason, response status, quota before/after,
    yield) map directly onto these columns.
    """

    __tablename__ = "quota_ledger_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_number: Mapped[int | None] = mapped_column(Integer)
    query_family: Mapped[str | None] = mapped_column(String(64))
    query_fingerprint: Mapped[str | None] = mapped_column(String(64))
    discovery_run_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("discovery_run.id"))
    # initial, retry
    attempt_kind: Mapped[str] = mapped_column(String(16), default="initial")
    retry_reason: Mapped[str | None] = mapped_column(String(64))
    # reserved, completed, failed
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    response_status_code: Mapped[int | None] = mapped_column(Integer)
    quota_before: Mapped[int] = mapped_column(Integer, nullable=False)
    quota_after: Mapped[int] = mapped_column(Integer, nullable=False)
    candidates_produced: Mapped[int | None] = mapped_column(Integer)
    unique_relevant_produced: Mapped[int | None] = mapped_column(Integer)
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_quota_ledger_entry_campaign", "campaign_id"),
        Index("idx_quota_ledger_entry_batch", "campaign_id", "batch_number"),
        Index("idx_quota_ledger_entry_family", "campaign_id", "query_family"),
    )
