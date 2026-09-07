"""Session/engine management and database integrity helpers.

Mirrors geocost's `db/__init__.py`: module-level engine/session singletons for
normal use, plus a set of functions that each re-resolve `get_settings()` /
`create_engine()` fresh so they follow a changed `DATABASE_URL` at test time
instead of trusting the module-level singleton.
"""

import hashlib
import json
import logging
from collections.abc import Generator
from pathlib import Path

import alembic.command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from discovery.config import get_settings
from discovery.db.models import Base

logger = logging.getLogger(__name__)

_settings = get_settings()
engine = create_engine(_settings.database_url, **_settings.get_db_engine_kwargs())
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI-style DB session dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_session_local() -> sessionmaker:
    """Build a fresh sessionmaker bound to a fresh engine from current settings.

    Unlike the module-level `SessionLocal` (bound once, at import time, to
    whatever `DATABASE_URL` was in effect then), this re-resolves
    `get_settings()` on every call -- the CLI uses this, not `SessionLocal`
    directly, so a `DATABASE_URL` change is honored correctly even across
    multiple command invocations within one process (real usage: scripted
    multi-database operator sessions; tests: `CliRunner` invoking several
    commands against different temp databases in one pytest process).
    """
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    return sessionmaker(autocommit=False, autoflush=False, bind=fresh_engine)


def _alembic_config() -> AlembicConfig:
    repo_root = Path(__file__).resolve().parents[3]
    config = AlembicConfig(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "alembic"))
    return config


def upgrade_db(revision: str = "head") -> None:
    """Apply pending Alembic migrations up to `revision`."""
    alembic.command.upgrade(_alembic_config(), revision)


def stamp_db(revision: str = "head") -> None:
    """Mark the database as being at `revision` without running migration bodies."""
    alembic.command.stamp(_alembic_config(), revision)


def get_current_migration_revision() -> str | None:
    """Return the Alembic revision the live database is stamped at, if any."""
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    with fresh_engine.connect() as conn:
        context = MigrationContext.configure(conn)
        return context.get_current_revision()


def init_db() -> None:
    """Create all tables directly from the ORM metadata and stamp as head.

    Used for tests and fresh local databases; production databases should be
    created empty and brought up via `upgrade_db()` so the Alembic history is
    exercised for real.
    """
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    if fresh_engine.dialect.name == "sqlite":
        with fresh_engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(bind=fresh_engine)
    stamp_db()


def drop_all_tables() -> None:
    """Drop every table. Test-only; never call this against a real database."""
    logger.warning("Dropping all discovery-pipeline tables")
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    Base.metadata.drop_all(bind=fresh_engine)


def verify_database_content() -> dict:
    """Return a SQLite integrity check plus row counts per table."""
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    row_counts: dict[str, int] = {}
    with fresh_engine.connect() as conn:
        integrity_check = None
        if fresh_engine.dialect.name == "sqlite":
            integrity_check = conn.execute(text("PRAGMA integrity_check")).scalar()
        inspector = inspect(fresh_engine)
        for table_name in inspector.get_table_names():
            count = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
            row_counts[table_name] = int(count or 0)
    return {"integrity_check": integrity_check, "row_counts": row_counts}


def file_sha256(path: Path) -> str:
    """Raw byte-level file hash, for verifying a straight file copy."""
    from discovery.hashing import file_sha256 as _file_sha256

    return _file_sha256(path)


def logical_database_hash() -> dict:
    """Content-level, insertion-order-independent hash of every table's rows."""
    settings = get_settings()
    fresh_engine = create_engine(settings.database_url, **settings.get_db_engine_kwargs())
    per_table_hashes: dict[str, str] = {}
    with fresh_engine.connect() as conn:
        inspector = inspect(fresh_engine)
        for table_name in sorted(inspector.get_table_names()):
            if table_name == "alembic_version":
                continue
            columns = sorted(col["name"] for col in inspector.get_columns(table_name))
            column_list = ", ".join(f'"{c}"' for c in columns)
            rows = conn.execute(text(f'SELECT {column_list} FROM "{table_name}"')).fetchall()
            row_digests = sorted(
                hashlib.sha256(json.dumps(list(row), default=str).encode("utf-8")).hexdigest()
                for row in rows
            )
            per_table_hashes[table_name] = hashlib.sha256(
                "".join(row_digests).encode("utf-8")
            ).hexdigest()
    whole_lines = [f"{table}:{digest}" for table, digest in sorted(per_table_hashes.items())]
    whole_database_hash = hashlib.sha256("".join(whole_lines).encode("utf-8")).hexdigest()
    return {"per_table_hashes": per_table_hashes, "whole_database_hash": whole_database_hash}
