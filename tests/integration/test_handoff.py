import json

from discovery.db.models import SourceCandidate
from discovery.handoff import build_handoff_entry, build_manifest, validate_and_prepare_candidate
from discovery.models.candidate import HandoffManifest


def _make_downloaded_candidate(test_db, tmp_path, *, filename="report.pdf", content=b"%PDF-1.4\nfake"):
    import hashlib

    path = tmp_path / filename
    path.write_bytes(content)
    sha256 = hashlib.sha256(content).hexdigest()

    candidate = SourceCandidate(
        canonical_url="https://example.com/report.pdf",
        normalized_title="egs drilling cost report",
        doi="10.1/x",
        publication_year=2022,
        screening_status="downloaded",
        local_acquired_path=str(path),
        sha256=sha256,
        discovery_occurrence_count=1,
    )
    test_db.add(candidate)
    test_db.flush()
    return candidate


def test_validate_and_prepare_promotes_to_ingestion_ready(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    ok = validate_and_prepare_candidate(test_db, candidate)
    assert ok is True
    assert candidate.screening_status == "ingestion_ready"


def test_validate_and_prepare_detects_hash_mismatch(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    # Corrupt the file after acquisition -- hash no longer matches.
    from pathlib import Path

    Path(candidate.local_acquired_path).write_bytes(b"corrupted content")

    ok = validate_and_prepare_candidate(test_db, candidate)
    assert ok is False
    assert candidate.screening_status == "corrupt_file"


def test_validate_and_prepare_detects_missing_file(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    from pathlib import Path

    Path(candidate.local_acquired_path).unlink()

    ok = validate_and_prepare_candidate(test_db, candidate)
    assert ok is False
    assert candidate.screening_status == "corrupt_file"


def test_validate_requires_title_or_doi(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    candidate.normalized_title = None
    candidate.doi = None
    test_db.flush()

    ok = validate_and_prepare_candidate(test_db, candidate)
    assert ok is False
    assert candidate.screening_status == "metadata_incomplete"


def test_build_handoff_entry_maps_fields():
    candidate = SourceCandidate(
        id="cand-1",
        normalized_title="egs drilling cost report",
        doi="10.1/x",
        canonical_url="https://example.com/report.pdf",
        publication_year=2022,
        source_type="government_technical_report",
        evidence_tier="government_technical",
        technology_domains_json=json.dumps(["egs"]),
        expected_evidence_categories_json=json.dumps(["drilling_cost"]),
        local_acquired_path="/tmp/x.pdf",
        sha256="abc123",
    )
    entry = build_handoff_entry(candidate, discovery_run_id="run-1")
    assert entry.candidate_id == "cand-1"
    assert entry.title == "egs drilling cost report"
    assert entry.technology_domains == ["egs"]
    assert entry.expected_evidence == ["drilling_cost"]
    assert entry.discovery_run_id == "run-1"
    assert entry.ready_for_ingestion is True


def test_build_manifest_self_hash_is_verifiable(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    validate_and_prepare_candidate(test_db, candidate)
    test_db.commit()

    manifest, manifest_path = build_manifest(
        test_db, discovery_run_id="run-1", handoff_dir=tmp_path / "handoff"
    )
    assert manifest.entry_count == 1
    assert candidate.screening_status == "handed_off"

    loaded = HandoffManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    from discovery.fingerprint import manifest_fingerprint

    recomputed = manifest_fingerprint([e.model_dump() for e in loaded.entries])
    assert recomputed == loaded.manifest_sha256


def test_build_manifest_is_idempotent_across_calls(test_db, tmp_path):
    candidate = _make_downloaded_candidate(test_db, tmp_path)
    validate_and_prepare_candidate(test_db, candidate)
    test_db.commit()

    manifest1, _ = build_manifest(test_db, discovery_run_id="run-1", handoff_dir=tmp_path / "handoff")
    test_db.commit()
    # Candidate is now handed_off; a second manifest build picks up nothing new.
    manifest2, _ = build_manifest(test_db, discovery_run_id="run-1", handoff_dir=tmp_path / "handoff")

    assert manifest1.entry_count == 1
    assert manifest2.entry_count == 0
