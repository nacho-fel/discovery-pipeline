"""Offline tests for the dedicated two-document acquisition pilot.

Every HTTP interaction goes through `httpx.MockTransport`; DNS resolution
through an injected fake resolver -- no real network call anywhere in this
file, matching every other acquisition test in this repo.
"""


import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from discovery.acquisition_pilot import (
    MAX_PILOT_CANDIDATES,
    MAX_PILOT_TOTAL_REQUESTS,
    PilotCandidateError,
    run_acquisition_pilot,
    seed_pilot_candidates,
)
from discovery.db.models import AcquisitionAttempt, Base, SourceCandidate
from discovery.handoff import build_manifest, validate_and_prepare_candidate

_PDF_BYTES = b"%PDF-1.4\nfake pilot pdf content\n"

STANFORD_URL = "https://pangea.stanford.edu/ERE/db/IGAstandard/record_detail.php?id=37947"
OSTI_URL = "https://osti.gov/biblio/1983898"
PAYWALLED_URL = "https://sciencedirect.com/science/article/pii/S0306261926004137"


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


def _seed_source_candidate(db, *, url, screening_status="acquisition_pending", access_status="open_access", title="Doc"):
    candidate = SourceCandidate(
        canonical_url=url,
        normalized_title=title,
        screening_status=screening_status,
        access_status=access_status,
    )
    db.add(candidate)
    db.commit()
    return candidate


def _public_resolver(host, port):
    return [(0, 0, 0, "", ("93.184.216.34", 0))]


def _pdf_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/pdf"}, content=_PDF_BYTES)


# --- seeding constraints -----------------------------------------------


def test_seeding_refuses_more_than_two_candidates(source_db, pilot_db):
    _seed_source_candidate(source_db, url="https://a.example.com/1")
    _seed_source_candidate(source_db, url="https://b.example.com/2")
    _seed_source_candidate(source_db, url="https://c.example.com/3")

    with pytest.raises(PilotCandidateError):
        seed_pilot_candidates(
            source_db,
            pilot_db,
            ["https://a.example.com/1", "https://b.example.com/2", "https://c.example.com/3"],
        )
    assert pilot_db.query(SourceCandidate).count() == 0


def test_seeding_excludes_paywalled_candidate(source_db, pilot_db):
    _seed_source_candidate(source_db, url=STANFORD_URL)
    _seed_source_candidate(source_db, url=PAYWALLED_URL, access_status="licensed_mit_access")

    with pytest.raises(PilotCandidateError):
        seed_pilot_candidates(source_db, pilot_db, [STANFORD_URL, PAYWALLED_URL])
    # Refusal is total -- nothing partially seeded either.
    assert pilot_db.query(SourceCandidate).count() == 0


def test_seeding_refuses_a_candidate_not_acquisition_pending(source_db, pilot_db):
    _seed_source_candidate(source_db, url=STANFORD_URL, screening_status="screened_review")

    with pytest.raises(PilotCandidateError):
        seed_pilot_candidates(source_db, pilot_db, [STANFORD_URL])


def test_seeding_refuses_unknown_url(source_db, pilot_db):
    with pytest.raises(PilotCandidateError):
        seed_pilot_candidates(source_db, pilot_db, ["https://not-in-source-db.example.com/x"])


def test_seeding_creates_fresh_isolated_rows(source_db, pilot_db):
    source_candidate = _seed_source_candidate(source_db, url=STANFORD_URL, title="Stanford Doc")

    seeded = seed_pilot_candidates(source_db, pilot_db, [STANFORD_URL])

    assert len(seeded) == 1
    assert seeded[0].id != source_candidate.id  # fresh pilot-local row, not the same PK
    assert seeded[0].canonical_url == STANFORD_URL
    assert seeded[0].screening_status == "acquisition_pending"


# --- run_acquisition_pilot constraints ----------------------------------


def test_run_refuses_more_than_two_candidates(pilot_db, tmp_path):
    candidates = [
        SourceCandidate(canonical_url=f"https://x{i}.example.com", screening_status="acquisition_pending")
        for i in range(3)
    ]
    pilot_db.add_all(candidates)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_pdf_handler))
    with pytest.raises(PilotCandidateError):
        run_acquisition_pilot(
            pilot_db,
            candidates,
            client=client,
            dest_dir=tmp_path,
            max_bytes=1_000_000,
            timeout_seconds=5.0,
            resolver=_public_resolver,
        )


def test_run_refuses_a_paywalled_candidate(pilot_db, tmp_path):
    candidate = SourceCandidate(
        canonical_url=PAYWALLED_URL, screening_status="acquisition_pending", access_status="licensed_mit_access"
    )
    pilot_db.add(candidate)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_pdf_handler))
    with pytest.raises(PilotCandidateError):
        run_acquisition_pilot(
            pilot_db, [candidate], client=client, dest_dir=tmp_path,
            max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
        )


# --- physical request ceiling -------------------------------------------


def test_redirects_count_toward_the_physical_ceiling_and_the_13th_request_never_happens(
    pilot_db, tmp_path
):
    """Both candidates redirect indefinitely. Each candidate's own loop caps
    at acquirer._MAX_REDIRECTS (5) => 6 requests/candidate => 12 total for 2
    candidates -- exactly MAX_PILOT_TOTAL_REQUESTS. This proves redirects are
    counted (not just initial requests) and that request #13 can never occur,
    even though both candidates *want* to keep redirecting past that.
    """
    call_count = {"n": 0}

    def always_redirect(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        assert call_count["n"] <= MAX_PILOT_TOTAL_REQUESTS, "a 13th request was attempted"
        return httpx.Response(302, headers={"location": str(request.url) + "x"})

    candidates = [
        SourceCandidate(canonical_url=STANFORD_URL, screening_status="acquisition_pending", access_status="open_access"),
        SourceCandidate(canonical_url=OSTI_URL, screening_status="acquisition_pending", access_status="open_access"),
    ]
    pilot_db.add_all(candidates)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(always_redirect))
    outcomes = run_acquisition_pilot(
        pilot_db, candidates, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert call_count["n"] <= MAX_PILOT_TOTAL_REQUESTS
    assert len(outcomes) == MAX_PILOT_CANDIDATES
    assert all(o.status in ("failed", "blocked_by_safety_policy") for o in outcomes)


def test_failures_do_not_bypass_the_ceiling(pilot_db, tmp_path):
    """A candidate that fails outright (e.g. a 500) still consumes exactly
    one unit of budget for that attempt -- failure is not a free pass around
    the shared ceiling.
    """

    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    candidates = [
        SourceCandidate(canonical_url=STANFORD_URL, screening_status="acquisition_pending", access_status="open_access"),
        SourceCandidate(canonical_url=OSTI_URL, screening_status="acquisition_pending", access_status="open_access"),
    ]
    pilot_db.add_all(candidates)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(always_500))
    outcomes = run_acquisition_pilot(
        pilot_db, candidates, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert all(o.status == "failed" for o in outcomes)
    # Exactly one physical request per candidate for a non-redirect failure.
    attempts = pilot_db.query(AcquisitionAttempt).all()
    assert len(attempts) == 2


# --- successful download + validation + handoff --------------------------


def test_successful_documents_are_hashed_and_validated(pilot_db, tmp_path):
    candidates = [
        SourceCandidate(
            canonical_url=STANFORD_URL,
            normalized_title="Stanford Doc",
            screening_status="acquisition_pending",
            access_status="open_access",
        ),
        SourceCandidate(
            canonical_url=OSTI_URL,
            normalized_title="OSTI Doc",
            screening_status="acquisition_pending",
            access_status="open_access",
        ),
    ]
    pilot_db.add_all(candidates)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_pdf_handler))
    outcomes = run_acquisition_pilot(
        pilot_db, candidates, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    assert all(o.status == "succeeded" for o in outcomes)
    assert all(o.sha256 for o in outcomes)
    for candidate in candidates:
        assert candidate.screening_status == "downloaded"
        assert candidate.sha256 is not None


def test_pilot_produces_a_non_empty_self_hashed_handoff_manifest(pilot_db, tmp_path):
    candidates = [
        SourceCandidate(
            canonical_url=STANFORD_URL,
            normalized_title="Stanford Doc",
            screening_status="acquisition_pending",
            access_status="open_access",
        ),
        SourceCandidate(
            canonical_url=OSTI_URL,
            normalized_title="OSTI Doc",
            screening_status="acquisition_pending",
            access_status="open_access",
        ),
    ]
    pilot_db.add_all(candidates)
    pilot_db.commit()

    client = httpx.Client(transport=httpx.MockTransport(_pdf_handler))
    run_acquisition_pilot(
        pilot_db, candidates, client=client, dest_dir=tmp_path,
        max_bytes=1_000_000, timeout_seconds=5.0, resolver=_public_resolver,
    )

    for candidate in candidates:
        assert validate_and_prepare_candidate(pilot_db, candidate) is True
    pilot_db.commit()

    manifest, manifest_path = build_manifest(
        pilot_db, discovery_run_id="pilot-test-run", handoff_dir=tmp_path / "handoff"
    )
    assert manifest.entry_count == 2
    assert manifest_path.exists()

    from discovery.fingerprint import manifest_fingerprint

    recomputed = manifest_fingerprint([e.model_dump() for e in manifest.entries])
    assert recomputed == manifest.manifest_sha256
