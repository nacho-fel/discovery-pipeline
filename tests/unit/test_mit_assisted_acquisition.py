"""Offline tests for mit_assisted_acquisition.py: queue building/export,
inbox matching, and state-machine progression -- entirely offline, no
network calls, and critically, nothing here ever touches a credential.
"""

from pathlib import Path

from discovery.db.models import SourceCandidate
from discovery.mit_assisted_acquisition import (
    build_acquisition_queue,
    export_queue_csv,
    export_queue_html,
    export_queue_markdown,
    scan_inbox,
)

_PDF_BYTES = b"%PDF-1.4\n%fake pdf content for testing\n"


def _restricted_candidate(
    db, *, screening_status="paywalled", access_status="licensed_mit_access", **overrides
) -> SourceCandidate:
    defaults = dict(
        canonical_url="https://onepetro.org/SPE/12345",
        normalized_title="SPE Drilling Cost Analysis",
        doi="10.1000/spe.12345",
        publisher="Society of Petroleum Engineers",
        screening_status=screening_status,
        access_status=access_status,
        expected_cost_observation_yield=5,
        expected_technical_observation_yield=2,
    )
    defaults.update(overrides)
    candidate = SourceCandidate(**defaults)
    db.add(candidate)
    db.flush()
    return candidate


def test_build_acquisition_queue_includes_only_restricted_candidates(test_db):
    restricted = _restricted_candidate(test_db)
    open_candidate = SourceCandidate(
        canonical_url="https://nrel.gov/reports/x.pdf",
        screening_status="acquisition_pending",
        access_status="open_access",
    )
    test_db.add(open_candidate)
    test_db.flush()

    queue = build_acquisition_queue(test_db)

    ids = {e.candidate_id for e in queue}
    assert restricted.id in ids
    assert open_candidate.id not in ids


def test_build_acquisition_queue_prioritizes_by_predicted_yield(test_db):
    low = _restricted_candidate(
        test_db,
        canonical_url="https://onepetro.org/SPE/low",
        doi="10.1000/spe.low",
        expected_cost_observation_yield=1,
        expected_technical_observation_yield=0,
    )
    high = _restricted_candidate(
        test_db,
        canonical_url="https://onepetro.org/SPE/high",
        doi="10.1000/spe.high",
        expected_cost_observation_yield=10,
        expected_technical_observation_yield=5,
    )

    queue = build_acquisition_queue(test_db)

    assert [e.candidate_id for e in queue] == [high.id, low.id]


def test_queue_entry_lookup_url_prefers_doi_resolver(test_db):
    with_doi = _restricted_candidate(test_db, doi="10.1000/xyz")
    queue = build_acquisition_queue(test_db)
    entry = next(e for e in queue if e.candidate_id == with_doi.id)
    assert entry.lookup_url == "https://doi.org/10.1000/xyz"


def test_queue_entry_lookup_url_falls_back_to_scholar_search_without_doi(test_db):
    no_doi = _restricted_candidate(test_db, doi=None, normalized_title="Some Cost Report")
    queue = build_acquisition_queue(test_db)
    entry = next(e for e in queue if e.candidate_id == no_doi.id)
    assert entry.lookup_url is not None
    assert "scholar.google.com" in entry.lookup_url


def test_export_queue_csv_and_markdown_and_html_include_every_entry(test_db):
    _restricted_candidate(test_db)
    queue = build_acquisition_queue(test_db)

    csv_text = export_queue_csv(queue)
    md_text = export_queue_markdown(queue)
    html_text = export_queue_html(queue)

    assert "candidate_id" in csv_text
    assert queue[0].candidate_id in csv_text
    assert queue[0].candidate_id[:8] in md_text
    assert queue[0].candidate_id in html_text
    assert "<table>" in html_text


def test_scan_inbox_matches_by_candidate_id_filename_and_reaches_ingestion_ready(test_db, tmp_path):
    candidate = _restricted_candidate(test_db)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / f"{candidate.id}.pdf").write_bytes(_PDF_BYTES)

    results = scan_inbox(test_db, inbox, acquired_root=tmp_path / "acquired")

    assert len(results) == 1
    assert results[0].matched is True
    assert results[0].validated is True
    test_db.refresh(candidate)
    assert candidate.screening_status == "ingestion_ready"
    assert candidate.access_route == "manual_acquisition"
    assert candidate.sha256 is not None
    assert Path(candidate.local_acquired_path).exists()


def test_scan_inbox_matches_via_explicit_mapping_when_filename_is_not_the_candidate_id(test_db, tmp_path):
    candidate = _restricted_candidate(test_db)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "spe_paper_downloaded_from_library.pdf").write_bytes(_PDF_BYTES)

    results = scan_inbox(
        test_db,
        inbox,
        acquired_root=tmp_path / "acquired",
        mapping={"spe_paper_downloaded_from_library.pdf": candidate.id},
    )

    assert results[0].matched is True
    assert results[0].candidate_id == candidate.id


def test_scan_inbox_reports_unmatched_files_without_raising(test_db, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "unknown-candidate.pdf").write_bytes(_PDF_BYTES)

    results = scan_inbox(test_db, inbox, acquired_root=tmp_path / "acquired")

    assert results[0].matched is False
    assert results[0].reason == "no matching candidate_id"


def test_scan_inbox_rejects_unrecognized_file_formats(test_db, tmp_path):
    candidate = _restricted_candidate(test_db)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / f"{candidate.id}.exe").write_bytes(b"not a real document")

    results = scan_inbox(test_db, inbox, acquired_root=tmp_path / "acquired")

    assert results[0].validated is False
    assert "unrecognized format" in results[0].reason
    test_db.refresh(candidate)
    assert candidate.screening_status == "paywalled"  # untouched -- no unsafe format was accepted


def test_scan_inbox_never_touches_screening_reject_candidates(test_db, tmp_path):
    candidate = _restricted_candidate(test_db, screening_status="screened_reject", access_status=None)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / f"{candidate.id}.pdf").write_bytes(_PDF_BYTES)

    results = scan_inbox(test_db, inbox, acquired_root=tmp_path / "acquired")

    assert results[0].validated is False
    test_db.refresh(candidate)
    assert candidate.screening_status == "screened_reject"  # never resurrected via the inbox


def test_scan_inbox_on_missing_directory_returns_empty(test_db, tmp_path):
    results = scan_inbox(test_db, tmp_path / "does_not_exist", acquired_root=tmp_path / "acquired")
    assert results == []
