"""Offline tests for the multi-format acquisition pilot (superseding the
PDF-only acquisition_pilot.py as the production-readiness demonstration).

Every HTTP interaction goes through httpx.MockTransport; DNS resolution
through an injected fake resolver -- no real network call anywhere here.
"""

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from discovery.db.models import AcquisitionAttempt, Base, SourceCandidate
from discovery.multi_format_pilot import (
    MAX_AUTOMATED_CANDIDATES,
    MAX_TOTAL_REQUESTS,
    MultiFormatPilotError,
    run_multi_format_pilot,
    seed_multi_format_pilot,
)

_PDF_BYTES = b"%PDF-1.4\nfake pilot pdf content\n"
_XLSX_BYTES = b"PK\x03\x04fake xlsx content"

PDF_URL = "https://pangea.stanford.edu/ERE/db/record/37947"
DATASET_URL = "https://gdr.openei.org/records/123/cost_data.xlsx"
LICENSED_URL = "https://onepetro.org/SPE/12345"


def _fresh_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


@pytest.fixture
def source_db():
    db = _fresh_db()
    yield db
    db.close()


@pytest.fixture
def pilot_db():
    db = _fresh_db()
    yield db
    db.close()


def _seed_source_trio(db):
    pdf = SourceCandidate(
        canonical_url=PDF_URL,
        normalized_title="Open PDF Report",
        screening_status="acquisition_pending",
        access_status="open_access",
        file_format="pdf",
    )
    dataset = SourceCandidate(
        canonical_url=DATASET_URL,
        normalized_title="Open Cost Dataset",
        screening_status="acquisition_pending",
        access_status="open_access",
        file_format="xlsx",
    )
    licensed = SourceCandidate(
        canonical_url=LICENSED_URL,
        normalized_title="Licensed SPE Paper",
        screening_status="paywalled",
        access_status="licensed_mit_access",
        expected_cost_observation_yield=5,
    )
    db.add_all([pdf, dataset, licensed])
    db.commit()
    return pdf, dataset, licensed


def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def _mixed_format_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url).endswith(".xlsx"):
        return httpx.Response(
            200,
            headers={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            content=_XLSX_BYTES,
        )
    return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)


# --- seeding constraints -------------------------------------------------


def test_seeding_requires_open_candidates_for_the_automated_legs(source_db, pilot_db):
    _seed_source_trio(source_db)
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == PDF_URL).update(
        {"access_status": "licensed_mit_access"}
    )
    source_db.commit()

    with pytest.raises(MultiFormatPilotError):
        seed_multi_format_pilot(
            source_db, pilot_db, open_pdf_url=PDF_URL, open_dataset_url=DATASET_URL, licensed_url=LICENSED_URL
        )
    assert pilot_db.query(SourceCandidate).count() == 0


def test_seeding_requires_the_licensed_leg_to_actually_be_restricted(source_db, pilot_db):
    _seed_source_trio(source_db)
    source_db.query(SourceCandidate).filter(SourceCandidate.canonical_url == LICENSED_URL).update(
        {"access_status": "open_access"}
    )
    source_db.commit()

    with pytest.raises(MultiFormatPilotError):
        seed_multi_format_pilot(
            source_db, pilot_db, open_pdf_url=PDF_URL, open_dataset_url=DATASET_URL, licensed_url=LICENSED_URL
        )


def test_seeding_creates_fresh_isolated_rows_for_all_three_legs(source_db, pilot_db):
    source_pdf, source_dataset, source_licensed = _seed_source_trio(source_db)

    open_candidates, licensed_candidate = seed_multi_format_pilot(
        source_db, pilot_db, open_pdf_url=PDF_URL, open_dataset_url=DATASET_URL, licensed_url=LICENSED_URL
    )

    assert len(open_candidates) == 2
    assert {c.canonical_url for c in open_candidates} == {PDF_URL, DATASET_URL}
    assert licensed_candidate.canonical_url == LICENSED_URL
    assert licensed_candidate.id != source_licensed.id
    assert all(c.id not in (source_pdf.id, source_dataset.id) for c in open_candidates)


# --- run constraints -------------------------------------------------------


def test_run_refuses_a_restricted_candidate_in_the_open_legs(pilot_db, tmp_path):
    bad_open = SourceCandidate(
        canonical_url="https://onepetro.org/SPE/other",
        screening_status="acquisition_pending",
        access_status="licensed_mit_access",
    )
    licensed = SourceCandidate(canonical_url=LICENSED_URL, screening_status="paywalled", access_status="licensed_mit_access")
    pilot_db.add_all([bad_open, licensed])
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_mixed_format_handler))
    with pytest.raises(MultiFormatPilotError):
        run_multi_format_pilot(
            pilot_db, [bad_open], licensed, client=client, dest_dir=tmp_path,
            max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
        )


def test_run_refuses_more_than_max_automated_candidates(pilot_db, tmp_path):
    opens = [
        SourceCandidate(canonical_url=f"https://x{i}.example.com", screening_status="acquisition_pending", access_status="open_access")
        for i in range(3)
    ]
    licensed = SourceCandidate(canonical_url=LICENSED_URL, screening_status="paywalled", access_status="licensed_mit_access")
    pilot_db.add_all([*opens, licensed])
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_mixed_format_handler))
    with pytest.raises(MultiFormatPilotError):
        run_multi_format_pilot(
            pilot_db, opens, licensed, client=client, dest_dir=tmp_path,
            max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
        )


# --- the actual multi-format demonstration ---------------------------------


def test_pilot_acquires_both_open_legs_in_their_distinct_formats(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    open_candidates, licensed_candidate = seed_multi_format_pilot(
        source_db, pilot_db, open_pdf_url=PDF_URL, open_dataset_url=DATASET_URL, licensed_url=LICENSED_URL
    )

    client = httpx.Client(transport=httpx.MockTransport(_mixed_format_handler))
    result = run_multi_format_pilot(
        pilot_db, open_candidates, licensed_candidate, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert len(result.automated_outcomes) == 2
    assert all(o.status == "succeeded" for o in result.automated_outcomes)
    assert {c.file_format for c in open_candidates} == {"pdf", "xlsx"}  # genuinely two distinct formats


def test_pilot_never_downloads_the_licensed_leg_and_queues_it_instead(source_db, pilot_db, tmp_path):
    _seed_source_trio(source_db)
    open_candidates, licensed_candidate = seed_multi_format_pilot(
        source_db, pilot_db, open_pdf_url=PDF_URL, open_dataset_url=DATASET_URL, licensed_url=LICENSED_URL
    )

    client = httpx.Client(transport=httpx.MockTransport(_mixed_format_handler))
    result = run_multi_format_pilot(
        pilot_db, open_candidates, licensed_candidate, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert result.licensed_candidate_download_attempted is False
    assert result.licensed_candidate_correctly_queued is True
    # No AcquisitionAttempt row exists for the licensed candidate -- it was
    # never even attempted, not just "attempted and blocked".
    licensed_attempts = (
        pilot_db.query(AcquisitionAttempt)
        .filter(AcquisitionAttempt.source_candidate_id == licensed_candidate.id)
        .count()
    )
    assert licensed_attempts == 0
    pilot_db.refresh(licensed_candidate)
    assert licensed_candidate.screening_status == "paywalled"  # untouched


def test_physical_request_ceiling_still_applies_to_the_two_open_legs(pilot_db, tmp_path):
    call_count = {"n": 0}

    def always_redirect(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        assert call_count["n"] <= MAX_TOTAL_REQUESTS, "a request beyond the shared ceiling was attempted"
        return httpx.Response(302, headers={"location": str(request.url) + "x"})

    open_candidates = [
        SourceCandidate(canonical_url=PDF_URL, screening_status="acquisition_pending", access_status="open_access"),
        SourceCandidate(canonical_url=DATASET_URL, screening_status="acquisition_pending", access_status="open_access"),
    ]
    licensed = SourceCandidate(canonical_url=LICENSED_URL, screening_status="paywalled", access_status="licensed_mit_access")
    pilot_db.add_all([*open_candidates, licensed])
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(always_redirect))
    result = run_multi_format_pilot(
        pilot_db, open_candidates, licensed, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert call_count["n"] <= MAX_TOTAL_REQUESTS
    assert len(result.automated_outcomes) == MAX_AUTOMATED_CANDIDATES
