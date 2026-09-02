# Alembic migration: initial discovery-pipeline schema
"""Create the initial discovery-pipeline schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-20

Creates all ten discovery-pipeline tables (discovery_run, query_plan,
search_execution, search_result, source_candidate, candidate_alias,
screening_decision, acquisition_attempt, source_lineage_edge, coverage_cell)
in FK-dependency order.

Idempotent per-table: each `create_table` call is guarded by a `_has_table`
check first and no-ops if the table already exists (e.g. a database built
fresh via `Base.metadata.create_all()`), matching geocost's idempotent-
migration convention for column additions, extended here to whole tables
since this is the first migration in the chain.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table_name)


def upgrade() -> None:
    """Create every discovery-pipeline table, skipping any that already exist."""
    if not _has_table("discovery_run"):
        op.create_table(
            "discovery_run",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("configuration_json", sa.Text()),
            sa.Column("query_budget", sa.Integer(), nullable=False),
            sa.Column("request_budget", sa.Integer(), nullable=False),
            sa.Column("requests_attempted", sa.Integer(), nullable=False),
            sa.Column("requests_succeeded", sa.Integer(), nullable=False),
            sa.Column("requests_failed", sa.Integer(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
            sa.Column("failure_reason", sa.Text()),
            sa.Column("created_by", sa.String(128)),
            sa.Column("version_metadata_json", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_discovery_run_status", "discovery_run", ["status"])

    if not _has_table("query_plan"):
        op.create_table(
            "query_plan",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "discovery_run_id", sa.String(36), sa.ForeignKey("discovery_run.id"), nullable=False
            ),
            sa.Column("query_fingerprint", sa.String(64), nullable=False),
            sa.Column("adapter", sa.String(64), nullable=False),
            sa.Column("canonical_intent", sa.String(256), nullable=False),
            sa.Column("rendered_query", sa.Text(), nullable=False),
            sa.Column("coverage_dimensions_json", sa.Text()),
            sa.Column("language", sa.String(16), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("pagination_cursor", sa.String(64)),
            sa.Column("planned_at", sa.DateTime(), nullable=False),
            sa.Column("executed_at", sa.DateTime()),
            sa.UniqueConstraint(
                "discovery_run_id", "query_fingerprint", name="uq_query_plan_run_fingerprint"
            ),
        )
        op.create_index("idx_query_plan_status", "query_plan", ["status"])
        op.create_index("idx_query_plan_run", "query_plan", ["discovery_run_id"])

    if not _has_table("search_execution"):
        op.create_table(
            "search_execution",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "query_plan_id", sa.String(36), sa.ForeignKey("query_plan.id"), nullable=False
            ),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("request_parameters_json", sa.Text()),
            sa.Column("response_status", sa.String(32)),
            sa.Column("provider_request_id", sa.String(128)),
            sa.Column("result_count", sa.Integer(), nullable=False),
            sa.Column("raw_response_path", sa.String(512)),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
            sa.Column("error_type", sa.String(64)),
            sa.Column("retryable", sa.Boolean()),
        )
        op.create_index("idx_search_execution_query_plan", "search_execution", ["query_plan_id"])
        op.create_index(
            "idx_search_execution_status", "search_execution", ["response_status"]
        )

    if not _has_table("search_result"):
        op.create_table(
            "search_result",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "search_execution_id",
                sa.String(36),
                sa.ForeignKey("search_execution.id"),
                nullable=False,
            ),
            sa.Column("rank", sa.Integer()),
            sa.Column("title_raw", sa.Text()),
            sa.Column("url_raw", sa.Text()),
            sa.Column("snippet_raw", sa.Text()),
            sa.Column("displayed_source", sa.String(256)),
            sa.Column("publication_info_raw", sa.Text()),
            sa.Column("provider_result_id", sa.String(256)),
            sa.Column("cited_by_id", sa.String(256)),
            sa.Column("cluster_version_id", sa.String(256)),
            sa.Column("raw_result_json", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_search_result_execution", "search_result", ["search_execution_id"])

    if not _has_table("source_candidate"):
        op.create_table(
            "source_candidate",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("canonical_url", sa.String(1024)),
            sa.Column("doi", sa.String(255)),
            sa.Column("normalized_title", sa.Text()),
            sa.Column("authors_json", sa.Text()),
            sa.Column("organization", sa.String(256)),
            sa.Column("publication_year", sa.Integer()),
            sa.Column("source_type", sa.String(64)),
            sa.Column("evidence_tier", sa.String(64)),
            sa.Column("technology_domains_json", sa.Text()),
            sa.Column("jurisdiction", sa.String(64)),
            sa.Column("language", sa.String(16)),
            sa.Column("access_status", sa.String(32)),
            sa.Column("screening_status", sa.String(32), nullable=False),
            sa.Column("relevance_score", sa.Float()),
            sa.Column("expected_evidence_categories_json", sa.Text()),
            sa.Column("discovery_occurrence_count", sa.Integer(), nullable=False),
            sa.Column("best_result_rank", sa.Integer()),
            sa.Column("candidate_key_fingerprint", sa.String(64)),
            sa.Column("canonical_source_document_id", sa.String(36)),
            sa.Column("local_acquired_path", sa.String(1024)),
            sa.Column("sha256", sa.String(64)),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("doi", name="uq_source_candidate_doi"),
            sa.UniqueConstraint("canonical_url", name="uq_source_candidate_canonical_url"),
        )
        op.create_index("idx_source_candidate_status", "source_candidate", ["screening_status"])
        op.create_index("idx_source_candidate_sha256", "source_candidate", ["sha256"])
        op.create_index(
            "idx_source_candidate_fingerprint", "source_candidate", ["candidate_key_fingerprint"]
        )

    if not _has_table("candidate_alias"):
        op.create_table(
            "candidate_alias",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "source_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column("search_result_id", sa.String(36), sa.ForeignKey("search_result.id")),
            sa.Column(
                "search_execution_id", sa.String(36), sa.ForeignKey("search_execution.id")
            ),
            sa.Column("matched_query_plan_id", sa.String(36), sa.ForeignKey("query_plan.id")),
            sa.Column("provider", sa.String(64)),
            sa.Column("rank", sa.Integer()),
            sa.Column("occurrence_kind", sa.String(32)),
            sa.Column("title_raw", sa.Text()),
            sa.Column("url_raw", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("idx_candidate_alias_candidate", "candidate_alias", ["source_candidate_id"])

    if not _has_table("screening_decision"):
        op.create_table(
            "screening_decision",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "source_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("direct_cost_evidence_score", sa.Float()),
            sa.Column("technical_driver_evidence_score", sa.Float()),
            sa.Column("domain_relevance_score", sa.Float()),
            sa.Column("source_quality_score", sa.Float()),
            sa.Column("accessibility_score", sa.Float()),
            sa.Column("coverage_novelty_score", sa.Float()),
            sa.Column("composite_score", sa.Float()),
            sa.Column("reason_codes_json", sa.Text()),
            sa.Column("explanation", sa.Text()),
            sa.Column("rules_version", sa.String(32), nullable=False),
            sa.Column("model_version", sa.String(64)),
            sa.Column("reviewer", sa.String(128)),
            sa.Column("decided_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "idx_screening_decision_candidate", "screening_decision", ["source_candidate_id"]
        )
        op.create_index("idx_screening_decision_decision", "screening_decision", ["decision"])

    if not _has_table("acquisition_attempt"):
        op.create_table(
            "acquisition_attempt",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "source_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("resolved_url", sa.Text()),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("http_status_code", sa.Integer()),
            sa.Column("content_type", sa.String(128)),
            sa.Column("bytes_downloaded", sa.Integer()),
            sa.Column("sha256", sa.String(64)),
            sa.Column("retryable", sa.Boolean()),
            sa.Column("error_type", sa.String(64)),
            sa.Column("error_message", sa.Text()),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
        )
        op.create_index(
            "idx_acquisition_attempt_candidate", "acquisition_attempt", ["source_candidate_id"]
        )
        op.create_index("idx_acquisition_attempt_status", "acquisition_attempt", ["status"])

    if not _has_table("source_lineage_edge"):
        op.create_table(
            "source_lineage_edge",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "parent_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column(
                "child_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column("relation_type", sa.String(32), nullable=False),
            sa.Column("discovery_run_id", sa.String(36), sa.ForeignKey("discovery_run.id")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "parent_candidate_id",
                "child_candidate_id",
                "relation_type",
                name="uq_source_lineage_edge",
            ),
        )
        op.create_index(
            "idx_lineage_edge_parent", "source_lineage_edge", ["parent_candidate_id"]
        )
        op.create_index("idx_lineage_edge_child", "source_lineage_edge", ["child_candidate_id"])

    if not _has_table("coverage_cell"):
        op.create_table(
            "coverage_cell",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("dimensions_json", sa.Text(), nullable=False),
            sa.Column("dimensions_fingerprint", sa.String(64), nullable=False),
            sa.Column("query_count", sa.Integer(), nullable=False),
            sa.Column("unique_result_count", sa.Integer(), nullable=False),
            sa.Column("accepted_count", sa.Integer(), nullable=False),
            sa.Column("last_marginal_yield", sa.Integer(), nullable=False),
            sa.Column("consecutive_zero_yield_rounds", sa.Integer(), nullable=False),
            sa.Column("saturation_status", sa.String(32), nullable=False),
            sa.Column("last_discovery_run_id", sa.String(36), sa.ForeignKey("discovery_run.id")),
            sa.Column("last_updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("dimensions_fingerprint", name="uq_coverage_cell_fingerprint"),
        )
        op.create_index("idx_coverage_cell_status", "coverage_cell", ["saturation_status"])


def downgrade() -> None:
    """Drop every discovery-pipeline table, skipping any already absent, in reverse FK order."""
    for table_name in (
        "coverage_cell",
        "source_lineage_edge",
        "acquisition_attempt",
        "screening_decision",
        "candidate_alias",
        "source_candidate",
        "search_result",
        "search_execution",
        "query_plan",
        "discovery_run",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
