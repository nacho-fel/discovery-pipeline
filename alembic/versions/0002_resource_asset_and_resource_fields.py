# Alembic migration: resource/asset model expansion
"""Add resource-level fields to source_candidate and the new resource_asset table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-21

Redefines the discovery unit from "document" to "research resource": a
resource can be a single file (the common case) or a landing page/dataset
with several distinct downloadable assets (resource_asset, new table).
Also adds the fields needed to represent format/MIME type, structured-data
likelihood, expected observation yield by family, cost scopes, and
access-route/publisher/license metadata directly on source_candidate.

Purely additive and idempotent, same convention as 0001: every new column
is nullable (or has a Python-side default only, no server default, matching
what `Base.metadata.create_all()` would produce), guarded by `_has_column`/
`_has_table` so this is safe to run against a database that already has
some or all of these (e.g. one built fresh via `Base.metadata.create_all()`).
Never touches any existing column, row, or other table.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SOURCE_CANDIDATE_COLUMNS = [
    ("direct_download_url", sa.String(1024)),
    ("file_format", sa.String(32)),
    ("mime_type", sa.String(128)),
    ("structured_data_likelihood", sa.Float()),
    ("expected_observation_families_json", sa.Text()),
    ("expected_cost_observation_yield", sa.Integer()),
    ("expected_technical_observation_yield", sa.Integer()),
    ("cost_scopes_json", sa.Text()),
    ("access_route", sa.String(64)),
    ("publisher", sa.String(256)),
    ("license_info", sa.String(256)),
]


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def _has_table(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return inspector.has_table(table_name)


def upgrade() -> None:
    """Add the new source_candidate columns and the resource_asset table."""
    missing = [
        (name, col_type)
        for name, col_type in _SOURCE_CANDIDATE_COLUMNS
        if not _has_column("source_candidate", name)
    ]
    if missing:
        with op.batch_alter_table("source_candidate") as batch_op:
            for column_name, column_type in missing:
                batch_op.add_column(sa.Column(column_name, column_type, nullable=True))

    if not _has_table("resource_asset"):
        op.create_table(
            "resource_asset",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "parent_candidate_id",
                sa.String(36),
                sa.ForeignKey("source_candidate.id"),
                nullable=False,
            ),
            sa.Column("asset_url", sa.Text(), nullable=False),
            sa.Column("direct_download_url", sa.Text()),
            sa.Column("file_format", sa.String(32)),
            sa.Column("mime_type", sa.String(128)),
            sa.Column("label", sa.String(512)),
            sa.Column("structured_data_likelihood", sa.Float()),
            sa.Column("access_status", sa.String(32)),
            sa.Column("sha256", sa.String(64)),
            sa.Column("local_acquired_path", sa.String(1024)),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "parent_candidate_id", "asset_url", name="uq_resource_asset_parent_url"
            ),
        )
        op.create_index("idx_resource_asset_parent", "resource_asset", ["parent_candidate_id"])
        op.create_index("idx_resource_asset_sha256", "resource_asset", ["sha256"])


def downgrade() -> None:
    """Drop resource_asset and the new source_candidate columns, if present."""
    if _has_table("resource_asset"):
        op.drop_table("resource_asset")

    present = [
        name for name, _col_type in _SOURCE_CANDIDATE_COLUMNS if _has_column("source_candidate", name)
    ]
    if present:
        with op.batch_alter_table("source_candidate") as batch_op:
            for column_name in present:
                batch_op.drop_column(column_name)
