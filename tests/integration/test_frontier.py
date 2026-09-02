import json

from discovery.db.models import SourceCandidate
from discovery.frontier import (
    build_expansion_queries,
    count_children,
    get_expansion_depth,
    record_lineage_edge,
)


def _candidate(test_db, **kwargs):
    defaults = dict(screening_status="acquisition_pending")
    defaults.update(kwargs)
    candidate = SourceCandidate(**defaults)
    test_db.add(candidate)
    test_db.flush()
    return candidate


def test_expansion_depth_zero_for_root_candidate(test_db):
    root = _candidate(test_db, doi="10.1/root")
    assert get_expansion_depth(test_db, root.id) == 0


def test_expansion_depth_increments_across_lineage_chain(test_db):
    root = _candidate(test_db, doi="10.1/root")
    child = _candidate(test_db, doi="10.1/child")
    grandchild = _candidate(test_db, doi="10.1/grandchild")

    record_lineage_edge(test_db, parent_candidate_id=root.id, child_candidate_id=child.id, relation_type="seeded_by")
    record_lineage_edge(
        test_db, parent_candidate_id=child.id, child_candidate_id=grandchild.id, relation_type="seeded_by"
    )

    assert get_expansion_depth(test_db, child.id) == 1
    assert get_expansion_depth(test_db, grandchild.id) == 2


def test_record_lineage_edge_is_idempotent(test_db):
    root = _candidate(test_db, doi="10.1/root")
    child = _candidate(test_db, doi="10.1/child")

    first = record_lineage_edge(test_db, parent_candidate_id=root.id, child_candidate_id=child.id, relation_type="seeded_by")
    second = record_lineage_edge(test_db, parent_candidate_id=root.id, child_candidate_id=child.id, relation_type="seeded_by")

    assert first is not None
    assert second is None  # already existed, no duplicate row
    assert count_children(test_db, root.id) == 1


def test_build_expansion_queries_respects_depth_budget(test_db):
    root = _candidate(test_db, doi="10.1/root", normalized_title="root title")
    queries = build_expansion_queries(test_db, root, max_expansion_depth=0, max_children_per_source=25)
    assert queries == []


def test_build_expansion_queries_respects_child_budget(test_db):
    root = _candidate(test_db, doi="10.1/root", normalized_title="root title")
    for i in range(3):
        child = _candidate(test_db, doi=f"10.1/child{i}")
        record_lineage_edge(
            test_db, parent_candidate_id=root.id, child_candidate_id=child.id, relation_type="seeded_by"
        )

    queries = build_expansion_queries(test_db, root, max_expansion_depth=5, max_children_per_source=3)
    assert queries == []


def test_build_expansion_queries_uses_doi_authors_and_organization(test_db):
    candidate = _candidate(
        test_db,
        doi="10.1/root",
        authors_json=json.dumps(["Maciej Lukawski"]),
        organization="NREL",
    )
    queries = build_expansion_queries(test_db, candidate, max_expansion_depth=5, max_children_per_source=25)
    kinds = [q.kind for q in queries]
    assert "exact_title_doi" in kinds
    assert "author_organization" in kinds
    assert any("NREL" in q.rendered_query for q in queries)
