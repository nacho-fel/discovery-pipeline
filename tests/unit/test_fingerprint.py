from discovery.fingerprint import (
    candidate_key_fingerprint,
    coverage_cell_fingerprint,
    manifest_fingerprint,
    query_fingerprint,
)


def test_query_fingerprint_is_deterministic():
    kwargs = dict(
        adapter="serpapi_google",
        canonical_intent="egs drilling cost",
        rendered_query="egs drilling cost report",
        language="en",
    )
    assert query_fingerprint(**kwargs) == query_fingerprint(**kwargs)


def test_query_fingerprint_differs_by_adapter():
    base = dict(canonical_intent="x", rendered_query="y", language="en")
    a = query_fingerprint(adapter="serpapi_google", **base)
    b = query_fingerprint(adapter="openalex", **base)
    assert a != b


def test_query_fingerprint_differs_by_pagination_offset():
    base = dict(adapter="serpapi_google", canonical_intent="x", rendered_query="y", language="en")
    assert query_fingerprint(**base, pagination_offset=0) != query_fingerprint(**base, pagination_offset=10)


def test_candidate_key_fingerprint_deterministic_and_order_independent_fields():
    a = candidate_key_fingerprint(doi="10.1/x", canonical_url=None, normalized_title=None, publication_year=None)
    b = candidate_key_fingerprint(doi="10.1/x", canonical_url=None, normalized_title=None, publication_year=None)
    assert a == b


def test_candidate_key_fingerprint_differs_by_doi():
    a = candidate_key_fingerprint(doi="10.1/x", canonical_url=None, normalized_title=None, publication_year=None)
    b = candidate_key_fingerprint(doi="10.1/y", canonical_url=None, normalized_title=None, publication_year=None)
    assert a != b


def test_coverage_cell_fingerprint_ignores_null_entries():
    a = coverage_cell_fingerprint({"technology_domain": "egs", "cost_component": None})
    b = coverage_cell_fingerprint({"technology_domain": "egs"})
    assert a == b


def test_coverage_cell_fingerprint_key_order_independent():
    a = coverage_cell_fingerprint({"a": "1", "b": "2"})
    b = coverage_cell_fingerprint({"b": "2", "a": "1"})
    assert a == b


def test_manifest_fingerprint_deterministic():
    entries = [{"candidate_id": "1"}, {"candidate_id": "2"}]
    assert manifest_fingerprint(entries) == manifest_fingerprint(entries)


def test_manifest_fingerprint_changes_with_content():
    a = manifest_fingerprint([{"candidate_id": "1"}])
    b = manifest_fingerprint([{"candidate_id": "2"}])
    assert a != b
