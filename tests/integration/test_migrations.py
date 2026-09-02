"""Alembic migration tests: upgrade/downgrade against a real (temp) sqlite
file, exercising the actual migration bodies -- not `Base.metadata.create_all`.
"""

import sqlalchemy as sa

_ALL_TABLES = {
    "discovery_run",
    "query_plan",
    "search_execution",
    "search_result",
    "source_candidate",
    "candidate_alias",
    "screening_decision",
    "acquisition_attempt",
    "source_lineage_edge",
    "coverage_cell",
    "resource_asset",
    "quota_ledger_state",
    "quota_ledger_entry",
}

_RESOURCE_FIELDS_0002 = [
    "direct_download_url",
    "file_format",
    "mime_type",
    "structured_data_likelihood",
    "expected_observation_families_json",
    "expected_cost_observation_yield",
    "expected_technical_observation_yield",
    "cost_scopes_json",
    "access_route",
    "publisher",
    "license_info",
]


def _reload_db_module(monkeypatch, db_path):
    """discovery.db module-level engine/SessionLocal are built once at import
    time from Settings(); the migration helper functions (`upgrade_db`, etc.)
    each re-resolve settings fresh, so setting DATABASE_URL before calling
    them is sufficient without needing to reimport the module.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")


def test_upgrade_creates_all_tables(tmp_path, monkeypatch):
    from discovery.db import get_current_migration_revision, upgrade_db

    db_path = tmp_path / "migration_test.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    table_names = set(inspector.get_table_names())
    assert _ALL_TABLES <= table_names
    assert get_current_migration_revision() == "0004"


def test_migration_0002_adds_resource_fields_and_asset_table(tmp_path, monkeypatch):
    from discovery.db import upgrade_db

    db_path = tmp_path / "migration_0002.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    source_candidate_columns = {c["name"] for c in inspector.get_columns("source_candidate")}
    assert set(_RESOURCE_FIELDS_0002) <= source_candidate_columns

    asset_columns = {c["name"] for c in inspector.get_columns("resource_asset")}
    assert {
        "id", "parent_candidate_id", "asset_url", "direct_download_url",
        "file_format", "mime_type", "label", "structured_data_likelihood",
        "access_status", "sha256", "local_acquired_path", "created_at",
    } <= asset_columns


def test_migration_0002_applies_cleanly_on_top_of_0001_alone(tmp_path, monkeypatch):
    """Upgrading incrementally (stop at 0001, then continue to head) must
    behave identically to upgrading straight to head -- proves 0002 doesn't
    assume anything beyond what 0001 actually left behind.
    """
    import alembic.command

    from discovery.db import _alembic_config, get_current_migration_revision, upgrade_db

    db_path = tmp_path / "migration_0002_incremental.db"
    _reload_db_module(monkeypatch, db_path)

    alembic.command.upgrade(_alembic_config(), "0001")
    assert get_current_migration_revision() == "0001"

    upgrade_db()  # continue to head (0002)
    assert get_current_migration_revision() == "0004"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "resource_asset" in set(inspector.get_table_names())


def test_migration_0003_adds_quota_ledger_tables(tmp_path, monkeypatch):
    from discovery.db import upgrade_db

    db_path = tmp_path / "migration_0003.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    state_columns = {c["name"] for c in inspector.get_columns("quota_ledger_state")}
    assert {"campaign_id", "max_requests", "reserved_count", "completed_count", "failed_count"} <= state_columns

    entry_columns = {c["name"] for c in inspector.get_columns("quota_ledger_entry")}
    assert {
        "id", "campaign_id", "batch_number", "query_family", "query_fingerprint",
        "attempt_kind", "retry_reason", "status", "response_status_code",
        "quota_before", "quota_after", "reserved_at", "completed_at",
    } <= entry_columns


def test_migration_0003_applies_cleanly_on_top_of_0002_alone(tmp_path, monkeypatch):
    """Same incremental-upgrade proof as 0002's: stopping at 0002 then
    continuing to head must behave identically to upgrading straight to
    head -- 0003 doesn't assume anything beyond what 0002 actually left
    behind.
    """
    import alembic.command

    from discovery.db import _alembic_config, get_current_migration_revision, upgrade_db

    db_path = tmp_path / "migration_0003_incremental.db"
    _reload_db_module(monkeypatch, db_path)

    alembic.command.upgrade(_alembic_config(), "0002")
    assert get_current_migration_revision() == "0002"

    upgrade_db()  # continue to head (0003)
    assert get_current_migration_revision() == "0004"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert {"quota_ledger_state", "quota_ledger_entry"} <= set(inspector.get_table_names())


def test_migration_0004_adds_query_plan_kind(tmp_path, monkeypatch):
    from discovery.db import upgrade_db

    db_path = tmp_path / "migration_0004.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("query_plan")}
    assert "kind" in columns


def test_migration_0004_applies_cleanly_on_top_of_0003_alone(tmp_path, monkeypatch):
    import alembic.command

    from discovery.db import _alembic_config, get_current_migration_revision, upgrade_db

    db_path = tmp_path / "migration_0004_incremental.db"
    _reload_db_module(monkeypatch, db_path)

    alembic.command.upgrade(_alembic_config(), "0003")
    assert get_current_migration_revision() == "0003"

    upgrade_db()  # continue to head (0004)
    assert get_current_migration_revision() == "0004"

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert "kind" in {c["name"] for c in inspector.get_columns("query_plan")}


def test_upgrade_is_idempotent(tmp_path, monkeypatch):
    from discovery.db import upgrade_db

    db_path = tmp_path / "migration_idempotent.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()
    upgrade_db()  # must not raise on a database that already has every table

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert _ALL_TABLES <= set(inspector.get_table_names())


def test_downgrade_removes_all_tables(tmp_path, monkeypatch):
    import alembic.command

    from discovery.db import _alembic_config, upgrade_db

    db_path = tmp_path / "migration_downgrade.db"
    _reload_db_module(monkeypatch, db_path)

    upgrade_db()
    alembic.command.downgrade(_alembic_config(), "base")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    table_names = set(inspector.get_table_names())
    assert not (_ALL_TABLES & table_names)


def test_upgrade_no_ops_when_tables_already_exist_unstamped(tmp_path, monkeypatch):
    """The `_has_table`/`_has_column` guards in every migration's `upgrade()`:
    a database whose tables were created directly via
    `Base.metadata.create_all()` (no Alembic stamp at all) must still
    upgrade cleanly to head instead of erroring on `CREATE TABLE`/
    `ADD COLUMN` against something that already exists.
    """
    from discovery.db import get_current_migration_revision, upgrade_db
    from discovery.db.models import Base

    db_path = tmp_path / "migration_unstamped.db"
    _reload_db_module(monkeypatch, db_path)

    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)  # tables exist, but no alembic_version row

    upgrade_db()  # must not raise

    assert get_current_migration_revision() == "0004"
    inspector = sa.inspect(engine)
    assert _ALL_TABLES <= set(inspector.get_table_names())


def test_upgrade_preserves_seeded_rows(tmp_path, monkeypatch):
    """Upgrading against a DB that already has the tables (e.g. one created via
    `init_db()`/`Base.metadata.create_all`) must be a no-op, never touching
    existing rows.
    """
    from discovery.db import init_db, upgrade_db
    from discovery.db.models import DiscoveryRun

    db_path = tmp_path / "migration_preserve.db"
    _reload_db_module(monkeypatch, db_path)

    init_db()
    engine = sa.create_engine(f"sqlite:///{db_path}")
    session = sa.orm.sessionmaker(bind=engine)()
    session.add(DiscoveryRun(status="completed", query_budget=1, request_budget=1))
    session.commit()
    session.close()

    upgrade_db()

    session = sa.orm.sessionmaker(bind=engine)()
    assert session.query(DiscoveryRun).count() == 1
    session.close()
