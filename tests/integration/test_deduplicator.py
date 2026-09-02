from discovery.db.models import CandidateAlias, SourceCandidate
from discovery.deduplicator import get_or_create_candidate, match_by_sha256, record_alias
from discovery.models.candidate import AggregatedCandidate


def test_exact_doi_match_reuses_existing_candidate(test_db):
    first = AggregatedCandidate(doi="10.1/x", normalized_title="a report")
    second = AggregatedCandidate(doi="10.1/x", normalized_title="a different title entirely")

    candidate1, created1 = get_or_create_candidate(test_db, first)
    candidate2, created2 = get_or_create_candidate(test_db, second)

    assert created1 is True
    assert created2 is False
    assert candidate1.id == candidate2.id
    assert test_db.query(SourceCandidate).count() == 1


def test_exact_url_match_reuses_existing_candidate(test_db):
    first = AggregatedCandidate(canonical_url="https://example.com/report")
    second = AggregatedCandidate(canonical_url="https://example.com/report")

    candidate1, _created1 = get_or_create_candidate(test_db, first)
    candidate2, created2 = get_or_create_candidate(test_db, second)

    assert created2 is False
    assert candidate1.id == candidate2.id


def test_direct_download_url_is_preserved_separately_from_canonical_url(test_db):
    """Regression test for the URL-fetch defect found during the 2026-08-24
    acquisition (2 nrel.gov failures): `canonical_url` is dedup-normalized
    (`normalize_url()` strips `www.` among other rewrites) and must never be
    the only URL retained -- `direct_download_url` carries the raw,
    as-discovered URL through untouched, so acquisition can fetch a URL that
    still resolves for hosts whose DNS requires the stripped prefix.
    """
    candidate = AggregatedCandidate(
        canonical_url="https://nrel.gov/docs/fy07osti/41156.pdf",
        direct_download_url="https://www.nrel.gov/docs/fy07osti/41156.pdf",
    )
    row, created = get_or_create_candidate(test_db, candidate)

    assert created is True
    assert row.canonical_url == "https://nrel.gov/docs/fy07osti/41156.pdf"
    assert row.direct_download_url == "https://www.nrel.gov/docs/fy07osti/41156.pdf"


def test_www_and_non_www_forms_still_dedup_to_one_candidate(test_db):
    """The fix must not weaken dedup: two occurrences whose raw URLs differ
    only by `www.` (and so carry different `direct_download_url` values)
    still normalize to the identical `canonical_url` and must still collapse
    to one `SourceCandidate` -- exactly the pre-existing behavior, unchanged.
    """
    first = AggregatedCandidate(
        canonical_url="https://nrel.gov/docs/report.pdf",
        direct_download_url="https://nrel.gov/docs/report.pdf",
    )
    second = AggregatedCandidate(
        canonical_url="https://nrel.gov/docs/report.pdf",
        direct_download_url="https://www.nrel.gov/docs/report.pdf",
    )

    candidate1, created1 = get_or_create_candidate(test_db, first)
    candidate2, created2 = get_or_create_candidate(test_db, second)

    assert created1 is True
    assert created2 is False
    assert candidate1.id == candidate2.id
    assert test_db.query(SourceCandidate).count() == 1
    # First-discovered raw URL wins -- a later occurrence's direct_download_url
    # is never used to overwrite it (record_alias never touches this field).
    assert candidate1.direct_download_url == "https://nrel.gov/docs/report.pdf"


def test_direct_download_url_defaults_to_none_when_not_given(test_db):
    """A candidate built without a raw URL (e.g. DOI-only, or constructed
    before this field existed) must not error -- callers fall back to
    canonical_url themselves.
    """
    candidate = AggregatedCandidate(doi="10.1/no-url-case", normalized_title="a report")
    row, created = get_or_create_candidate(test_db, candidate)

    assert created is True
    assert row.direct_download_url is None


def test_title_and_year_match_reuses_existing_candidate(test_db):
    first = AggregatedCandidate(normalized_title="egs drilling cost report", publication_year=2022)
    second = AggregatedCandidate(normalized_title="egs drilling cost report", publication_year=2022)

    candidate1, _created1 = get_or_create_candidate(test_db, first)
    candidate2, created2 = get_or_create_candidate(test_db, second)

    assert created2 is False
    assert candidate1.id == candidate2.id


def test_different_years_are_not_merged(test_db):
    first = AggregatedCandidate(normalized_title="egs drilling cost report", publication_year=2020)
    second = AggregatedCandidate(normalized_title="egs drilling cost report", publication_year=2023)

    candidate1, _ = get_or_create_candidate(test_db, first)
    candidate2, _ = get_or_create_candidate(test_db, second)

    assert candidate1.id != candidate2.id


def test_fuzzy_title_match_requires_author_overlap(test_db):
    first = AggregatedCandidate(
        normalized_title="cost analysis of oil gas and geothermal well drilling",
        authors=["Maciej Lukawski"],
        publication_year=2014,
    )
    # Near-identical title, same year, but a completely different, non-overlapping author.
    second = AggregatedCandidate(
        normalized_title="cost analysis of oil gas and geothermal well drillng",
        authors=["Someone Else"],
        publication_year=2014,
    )

    candidate1, _ = get_or_create_candidate(test_db, first)
    candidate2, _ = get_or_create_candidate(test_db, second)

    assert candidate1.id != candidate2.id


def test_fuzzy_title_match_with_matching_author_merges(test_db):
    first = AggregatedCandidate(
        normalized_title="cost analysis of oil gas and geothermal well drilling",
        authors=["Maciej Lukawski"],
        publication_year=2014,
    )
    second = AggregatedCandidate(
        normalized_title="cost analysis of oil gas and geothermal well drillng",
        authors=["Maciej Lukawski"],
        publication_year=2014,
    )

    candidate1, _ = get_or_create_candidate(test_db, first)
    candidate2, _ = get_or_create_candidate(test_db, second)

    assert candidate1.id == candidate2.id


def test_aliases_are_never_deleted_even_on_duplicate(test_db):
    candidate, _ = get_or_create_candidate(test_db, AggregatedCandidate(doi="10.1/x"))
    record_alias(test_db, candidate, occurrence_kind="search_result", provider="serpapi_google", rank=1)
    record_alias(test_db, candidate, occurrence_kind="search_result", provider="openalex", rank=3)

    assert test_db.query(CandidateAlias).count() == 2
    assert candidate.discovery_occurrence_count == 2
    assert candidate.best_result_rank == 1  # min() across occurrences


def test_match_by_sha256_finds_post_download_duplicate(test_db):
    candidate, _ = get_or_create_candidate(test_db, AggregatedCandidate(canonical_url="https://a.com/x"))
    candidate.sha256 = "deadbeef" * 8
    test_db.flush()

    found = match_by_sha256(test_db, "deadbeef" * 8)
    assert found is not None
    assert found.id == candidate.id

    assert match_by_sha256(test_db, "0" * 64) is None
