"""Shared pytest fixtures, mirroring geocost's `conftest.py` pattern."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from discovery.db.models import Base


@pytest.fixture
def test_db():
    """In-memory SQLite session; schema built directly via `Base.metadata.create_all`."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    yield db
    db.close()


@pytest.fixture
def screening_rules_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "config" / "screening_rules.yaml"
