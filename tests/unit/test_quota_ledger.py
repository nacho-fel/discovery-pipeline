"""Offline tests for quota_ledger.py: the durable, atomically-reserved
physical-request ledger the campaign's hard 5,000-request ceiling depends
on. Restart and concurrency tests use a real file-based SQLite database
(never `:memory:`, which isn't shared across connections) so they actually
exercise cross-connection/cross-process-shaped behavior.
"""

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from discovery.db.models import Base
from discovery.quota_ledger import (
    QuotaExhaustedError,
    annotate_batch_yield,
    campaign_summary,
    complete,
    remaining,
    reserve,
    set_campaign_ceiling,
    used,
)


def _session_factory(db_path):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def test_reserve_increments_and_is_reflected_in_remaining(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()

    handle = reserve(db, "test-campaign", max_requests=10)

    assert handle.quota_before == 0
    assert handle.quota_after == 1
    assert used(db, "test-campaign") == 1
    assert remaining(db, "test-campaign") == 9


def test_reservations_beyond_the_ceiling_are_refused(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()

    for _ in range(5):
        reserve(db, "small-campaign", max_requests=5)

    assert remaining(db, "small-campaign") == 0
    with pytest.raises(QuotaExhaustedError):
        reserve(db, "small-campaign", max_requests=5)
    assert used(db, "small-campaign") == 5  # the refused attempt reserved nothing


def test_the_n_plus_1th_reservation_is_impossible_at_any_ceiling(tmp_path):
    """The literal safety property behind "5,001 requests are impossible":
    at max_requests=N, exactly N reservations succeed and the (N+1)th is
    always refused. Exercised at a small N here -- `reserve()`'s atomic
    UPDATE...WHERE is the same single code path regardless of N's
    magnitude, so this is not a smaller claim than proving it at N=5000,
    just a faster one; the config-level default is checked separately in
    test_config.py.
    """
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()

    ceiling = 25
    for _ in range(ceiling):
        reserve(db, "campaign", max_requests=ceiling)

    assert used(db, "campaign") == ceiling
    assert remaining(db, "campaign") == 0
    with pytest.raises(QuotaExhaustedError):
        reserve(db, "campaign", max_requests=ceiling)
    assert used(db, "campaign") == ceiling  # still exactly the ceiling -- never one more


def test_retries_consume_their_own_ledger_unit(tmp_path):
    """A query that retries twice before succeeding spends 3 physical units
    (1 initial + 2 retries), not 1 -- retries count against the ceiling.
    """
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()

    reserve(db, "campaign", max_requests=10, attempt_kind="initial")
    reserve(db, "campaign", max_requests=10, attempt_kind="retry", retry_reason="timeout")
    reserve(db, "campaign", max_requests=10, attempt_kind="retry", retry_reason="http_error")

    assert used(db, "campaign") == 3
    summary = campaign_summary(db, "campaign")
    assert summary["retry_count"] == 2


def test_a_ceiling_of_zero_permits_no_requests_at_all(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()

    with pytest.raises(QuotaExhaustedError):
        reserve(db, "campaign", max_requests=0)
    assert used(db, "campaign") == 0


def test_restart_does_not_reset_the_counter(tmp_path):
    """Simulates a process restart: a fresh engine/session against the SAME
    database file must see the already-spent quota, not start over.
    """
    db_path = tmp_path / "ledger.db"
    session_factory1 = _session_factory(db_path)
    db1 = session_factory1()
    for _ in range(3):
        reserve(db1, "campaign", max_requests=10)
    db1.close()

    # A brand new engine/session, as a restarted process would create.
    engine2 = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    session_factory2 = sessionmaker(bind=engine2)
    db2 = session_factory2()

    assert used(db2, "campaign") == 3
    assert remaining(db2, "campaign") == 7

    reserve(db2, "campaign", max_requests=10)
    assert used(db2, "campaign") == 4


def test_concurrent_reservations_never_exceed_the_ceiling(tmp_path):
    """Many threads, each with its OWN engine/session against the same
    database file (the same shape as separate processes sharing one sqlite
    file), race to reserve against a small ceiling. The atomic
    UPDATE...WHERE must ensure the total that succeed is exactly the
    ceiling, never more, regardless of interleaving.
    """
    db_path = tmp_path / "ledger.db"
    # Create the schema once up front.
    _session_factory(db_path)

    ceiling = 8
    attempts = 24
    successes = []
    lock = threading.Lock()

    def worker():
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, pool_size=1)
        worker_session_factory = sessionmaker(bind=engine)
        db = worker_session_factory()
        try:
            reserve(db, "race-campaign", max_requests=ceiling)
            with lock:
                successes.append(1)
        except QuotaExhaustedError:
            pass
        finally:
            db.close()
            engine.dispose()

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == ceiling

    verify_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    verify_db = sessionmaker(bind=verify_engine)()
    assert used(verify_db, "race-campaign") == ceiling
    assert remaining(verify_db, "race-campaign") == 0


def test_concurrent_first_reservations_racing_to_create_the_same_new_campaign_never_error(tmp_path):
    """The very first reserve() for a brand-new campaign_id also creates its
    QuotaLedgerState row -- concurrent callers racing to be the one that
    creates it must never raise an IntegrityError or lose a reservation.
    """
    db_path = tmp_path / "ledger.db"
    _session_factory(db_path)

    attempts = 12
    errors = []
    successes = []
    lock = threading.Lock()

    def worker():
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, pool_size=1)
        worker_session_factory = sessionmaker(bind=engine)
        db = worker_session_factory()
        try:
            reserve(db, "brand-new-campaign", max_requests=100)
            with lock:
                successes.append(1)
        except Exception as exc:
            with lock:
                errors.append(exc)
        finally:
            db.close()
            engine.dispose()

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(successes) == attempts

    verify_db = sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))()
    assert used(verify_db, "brand-new-campaign") == attempts


def test_complete_never_changes_reserved_count(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()
    handle = reserve(db, "campaign", max_requests=10)

    complete(db, handle.entry_id, failed=True, response_status_code=500)

    assert used(db, "campaign") == 1  # a failed physical attempt still spent its unit
    summary = campaign_summary(db, "campaign")
    assert summary["failed_count"] == 1
    assert summary["completed_count"] == 0


def test_set_campaign_ceiling_refuses_to_go_below_already_reserved(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()
    for _ in range(5):
        reserve(db, "campaign", max_requests=10)

    with pytest.raises(ValueError):
        set_campaign_ceiling(db, "campaign", max_requests=3)

    set_campaign_ceiling(db, "campaign", max_requests=8)
    assert remaining(db, "campaign") == 3


def test_annotate_batch_yield_only_touches_matching_unannotated_entries(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()
    h1 = reserve(db, "campaign", max_requests=10, batch_number=1)
    h2 = reserve(db, "campaign", max_requests=10, batch_number=1)
    h3 = reserve(db, "campaign", max_requests=10, batch_number=2)

    updated = annotate_batch_yield(db, "campaign", 1, candidates_produced=4, unique_relevant_produced=2)

    assert updated == 2
    from discovery.db.models import QuotaLedgerEntry

    assert db.query(QuotaLedgerEntry).filter_by(id=h1.entry_id).first().candidates_produced == 4
    assert db.query(QuotaLedgerEntry).filter_by(id=h2.entry_id).first().unique_relevant_produced == 2
    assert db.query(QuotaLedgerEntry).filter_by(id=h3.entry_id).first().candidates_produced is None


def test_campaign_summary_reports_exists_false_for_unknown_campaign(tmp_path):
    session_factory = _session_factory(tmp_path / "ledger.db")
    db = session_factory()
    assert campaign_summary(db, "never-created")["exists"] is False
    assert used(db, "never-created") == 0
    assert remaining(db, "never-created") == 0
