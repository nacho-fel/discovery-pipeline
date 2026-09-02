# Alembic migration: persist query family on query_plan
"""Add query_plan.kind (the query "family" campaign-level yield-aware
allocation groups by).

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-21

Previously `PlannedQuery.kind` was computed at plan time but discarded --
`execute_run` even hardcoded a literal "broad_domain" placeholder when
re-hydrating a `QueryPlan` row into a `PlannedQuery` for execution ("kind is
not persisted on QueryPlan; only used for logging here"). Without this
column, `quota_ledger.py`'s per-family audit trail and
`adaptive_controller.py`'s per-family low-yield allocation have no real
family to group by at execution time.

Purely additive, nullable, idempotent -- same convention as 0001-0003.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    """Add query_plan.kind if not already present."""
    if not _has_column("query_plan", "kind"):
        with op.batch_alter_table("query_plan") as batch_op:
            batch_op.add_column(sa.Column("kind", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop query_plan.kind, if present."""
    if _has_column("query_plan", "kind"):
        with op.batch_alter_table("query_plan") as batch_op:
            batch_op.drop_column("kind")
