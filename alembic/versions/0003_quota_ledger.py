# Alembic migration: persistent SerpApi quota ledger
"""Add quota_ledger_state and quota_ledger_entry tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-21

Replaces approximate, in-process-only cross-run quota accounting with a
durable, atomically-reservable physical-request ledger (see
`src/discovery/quota_ledger.py`) -- the campaign-level hard ceiling (default
5,000 physical SerpApi requests) is enforced by a single atomic
`UPDATE ... WHERE reserved_count < max_requests` against
`quota_ledger_state`, correct across process restarts and concurrent
processes because it never depends on in-memory state.

Purely additive and idempotent, same convention as 0001/0002: guarded by
`_has_table` so this is safe to run against a database that already has
either table (e.g. one built fresh via `Base.metadata.create_all()`). Never
touches any existing column, row, or other table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table_name)


def upgrade() -> None:
    """Add quota_ledger_state and quota_ledger_entry if not already present."""
    if not _has_table("quota_ledger_state"):
        op.create_table(
            "quota_ledger_state",
            sa.Column("campaign_id", sa.String(64), primary_key=True),
            sa.Column("max_requests", sa.Integer(), nullable=False),
            sa.Column("reserved_count", sa.Integer(), nullable=False),
            sa.Column("completed_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    if not _has_table("quota_ledger_entry"):
        op.create_table(
            "quota_ledger_entry",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("campaign_id", sa.String(64), nullable=False),
            sa.Column("batch_number", sa.Integer()),
            sa.Column("query_family", sa.String(64)),
            sa.Column("query_fingerprint", sa.String(64)),
            sa.Column(
                "discovery_run_id", sa.String(36), sa.ForeignKey("discovery_run.id")
            ),
            sa.Column("attempt_kind", sa.String(16), nullable=False),
            sa.Column("retry_reason", sa.String(64)),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("response_status_code", sa.Integer()),
            sa.Column("quota_before", sa.Integer(), nullable=False),
            sa.Column("quota_after", sa.Integer(), nullable=False),
            sa.Column("candidates_produced", sa.Integer()),
            sa.Column("unique_relevant_produced", sa.Integer()),
            sa.Column("reserved_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
        )
        op.create_index(
            "idx_quota_ledger_entry_campaign", "quota_ledger_entry", ["campaign_id"]
        )
        op.create_index(
            "idx_quota_ledger_entry_batch", "quota_ledger_entry", ["campaign_id", "batch_number"]
        )
        op.create_index(
            "idx_quota_ledger_entry_family", "quota_ledger_entry", ["campaign_id", "query_family"]
        )


def downgrade() -> None:
    """Drop quota_ledger_entry and quota_ledger_state, if present."""
    if _has_table("quota_ledger_entry"):
        op.drop_table("quota_ledger_entry")
    if _has_table("quota_ledger_state"):
        op.drop_table("quota_ledger_state")
