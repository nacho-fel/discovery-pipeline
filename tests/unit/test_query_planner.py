from pathlib import Path

from discovery.query_planner import (
    build_programmatic_query,
    load_coverage_matrix,
    load_multilingual_terms,
    load_query_templates,
    plan_multilingual_queries,
    plan_queries,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_load_real_config_files_parse():
    matrix = load_coverage_matrix(_CONFIG_DIR / "coverage_matrix.yaml")
    templates = load_query_templates(_CONFIG_DIR / "query_templates.yaml")
    assert "technology_domains" in matrix
    assert "repository_domains" in matrix
    assert len(templates) > 0


def test_real_config_includes_repository_scoped_templates():
    matrix = load_coverage_matrix(_CONFIG_DIR / "coverage_matrix.yaml")
    templates = load_query_templates(_CONFIG_DIR / "query_templates.yaml")
    planned = plan_queries(
        coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"}
    )
    repo_queries = [q for q in planned if "site:gdr.openei.org" in q.rendered_query]
    onepetro_queries = [q for q in planned if "site:onepetro.org" in q.rendered_query]
    assert repo_queries
    assert onepetro_queries


def test_real_config_includes_structured_data_and_dataset_repository_templates():
    """Redefining the discovery unit from "document" to "research resource":
    the real templates must include filetype:xlsx/csv/zip searches and
    dataset-landing-page queries against the newly-added licensed/commercial
    repository domains (sciencedirect.com, spglobal.com, ihsmarkit.com), not
    just PDFs.
    """
    matrix = load_coverage_matrix(_CONFIG_DIR / "coverage_matrix.yaml")
    templates = load_query_templates(_CONFIG_DIR / "query_templates.yaml")
    planned = plan_queries(
        coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"}
    )
    spreadsheet_queries = [q for q in planned if "filetype:xlsx" in q.rendered_query]
    dataset_queries = [q for q in planned if "dataset" in q.rendered_query.lower()]
    sciencedirect_queries = [q for q in planned if "site:sciencedirect.com" in q.rendered_query]
    spglobal_queries = [q for q in planned if "site:spglobal.com" in q.rendered_query]
    well_cost_db_queries = [q for q in planned if "well cost database" in q.rendered_query]
    assert spreadsheet_queries
    assert dataset_queries
    assert sciencedirect_queries
    assert spglobal_queries
    assert well_cost_db_queries


def test_real_config_wires_previously_unused_cost_representation_and_evidence_type_dimensions():
    """`cost_representations`/`evidence_types` were defined in
    coverage_matrix.yaml (and already handled by _FIELD_TO_MATRIX_KEY) since
    before this test existed, but zero templates ever referenced them -- a
    structural gap found while planning the 2026-08-23 500-request campaign.
    Regression test for the templates added to close it.
    """
    matrix = load_coverage_matrix(_CONFIG_DIR / "coverage_matrix.yaml")
    templates = load_query_templates(_CONFIG_DIR / "query_templates.yaml")
    planned = plan_queries(
        coverage_matrix=matrix,
        templates=templates,
        enabled_adapters={"serpapi_google", "serpapi_scholar", "openalex", "crossref"},
    )
    cost_representation_queries = [q for q in planned if q.kind == "cost_representation"]
    evidence_type_queries = [q for q in planned if q.kind == "evidence_type"]
    assert cost_representation_queries
    assert evidence_type_queries
    # Every cost_representations/evidence_types value from the matrix must
    # actually reach a rendered query, not just a subset.
    rendered_cr = " ".join(q.rendered_query.lower() for q in cost_representation_queries)
    rendered_et = " ".join(q.rendered_query.lower() for q in evidence_type_queries)
    for value in matrix["cost_representations"]:
        assert value.replace("_", " ") in rendered_cr, value
    for value in matrix["evidence_types"]:
        assert value.replace("_", " ") in rendered_et, value


def test_real_config_cost_driver_template_carries_a_domain_topic_word():
    """Prior-trial audit finding: cost_driver's only template rendered with no
    domain-topic word at all (e.g. "drilling"), which the audit identified as
    the dominant cause of its zero-strict-accept outcome across 17 requests
    (domain-relevance scoring gap, not a lack of real content). Regression
    test for the query-wording fix, not a scoring-rule change.
    """
    matrix = load_coverage_matrix(_CONFIG_DIR / "coverage_matrix.yaml")
    templates = load_query_templates(_CONFIG_DIR / "query_templates.yaml")
    planned = plan_queries(
        coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"}
    )
    cost_driver_queries = [q for q in planned if q.kind == "cost_driver"]
    assert cost_driver_queries
    assert all("drilling" in q.rendered_query.lower() for q in cost_driver_queries)


def test_load_real_multilingual_terms_parse():
    terms = load_multilingual_terms(_CONFIG_DIR / "multilingual_terms.yaml")
    assert len(terms) > 0
    languages = {t["language"] for t in terms}
    assert {"es", "fr", "de", "is", "tr", "id", "it", "pt", "en"} <= languages


def test_plan_multilingual_queries_keeps_language_phrase_paired():
    terms = [
        {"language": "es", "kind": "broad_domain", "phrase": "costo de perforación geotérmica"},
        {"language": "fr", "kind": "broad_domain", "phrase": "coût de forage géothermique"},
    ]
    planned = plan_multilingual_queries(
        terms=terms, enabled_adapters={"serpapi_google", "serpapi_scholar"}
    )
    # 2 terms x 2 adapters = 4 queries, each still carrying its own language.
    assert len(planned) == 4
    for q in planned:
        if q.rendered_query == "costo de perforación geotérmica":
            assert q.coverage_dimensions.language == "es"
        elif q.rendered_query == "coût de forage géothermique":
            assert q.coverage_dimensions.language == "fr"


def test_plan_multilingual_queries_skips_openalex_crossref():
    terms = [{"language": "es", "kind": "broad_domain", "phrase": "costo de perforación"}]
    planned = plan_multilingual_queries(
        terms=terms, enabled_adapters={"openalex", "crossref", "serpapi_google"}
    )
    adapters_used = {q.adapter for q in planned}
    assert adapters_used == {"serpapi_google"}


def test_plan_multilingual_queries_respects_enabled_adapters():
    terms = [{"language": "es", "kind": "broad_domain", "phrase": "costo de perforación"}]
    planned = plan_multilingual_queries(terms=terms, enabled_adapters={"openalex"})
    assert planned == []


def test_plan_multilingual_queries_fingerprints_are_deterministic():
    terms = [{"language": "es", "kind": "broad_domain", "phrase": "costo de perforación"}]
    first = plan_multilingual_queries(terms=terms, enabled_adapters={"serpapi_google"})
    second = plan_multilingual_queries(terms=terms, enabled_adapters={"serpapi_google"})
    assert first[0].query_fingerprint == second[0].query_fingerprint


def test_plan_queries_is_curated_not_full_cartesian():
    matrix = {
        "technology_domains": ["egs", "oil_and_gas"],
        "cost_components": ["drilling", "completion"],
        "trusted_domains": ["nrel.gov"],
    }
    templates = [
        {
            "kind": "cost_component",
            "adapters": ["serpapi_google"],
            "priority": 0,
            "intent": "{technology_domain} {cost_component} cost",
            "query": "{technology_domain} {cost_component} cost",
        }
    ]
    planned = plan_queries(coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"})
    # Only technology_domain x cost_component (2x2=4), never also x trusted_domains.
    assert len(planned) == 4


def test_plan_queries_deterministic_ordering_and_fingerprints():
    matrix = {"technology_domains": ["egs"], "cost_components": ["drilling"]}
    templates = [
        {
            "kind": "cost_component",
            "adapters": ["serpapi_google"],
            "priority": 0,
            "intent": "{technology_domain} {cost_component} cost",
            "query": "{technology_domain} {cost_component} cost",
        }
    ]
    first = plan_queries(coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"})
    second = plan_queries(coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"})
    assert [q.query_fingerprint for q in first] == [q.query_fingerprint for q in second]


def test_plan_queries_skips_template_with_disabled_adapter():
    matrix = {"technology_domains": ["egs"]}
    templates = [
        {
            "kind": "broad_domain",
            "adapters": ["serpapi_google"],
            "priority": 0,
            "intent": "{technology_domain}",
            "query": "{technology_domain}",
        }
    ]
    planned = plan_queries(coverage_matrix=matrix, templates=templates, enabled_adapters={"openalex"})
    assert planned == []


def test_plan_queries_humanizes_slug_values_but_not_literals():
    matrix = {"technology_domains": ["enhanced_geothermal_systems"], "trusted_domains": ["nrel.gov"]}
    templates = [
        {
            "kind": "trusted_domain",
            "adapters": ["serpapi_google"],
            "priority": 0,
            "intent": "{technology_domain} on {trusted_domain}",
            "query": "{technology_domain} site:{trusted_domain}",
        }
    ]
    planned = plan_queries(coverage_matrix=matrix, templates=templates, enabled_adapters={"serpapi_google"})
    assert "enhanced geothermal systems" in planned[0].rendered_query
    assert "site:nrel.gov" in planned[0].rendered_query


def test_build_programmatic_query_matches_fingerprint_shape():
    q = build_programmatic_query(
        adapter="serpapi_scholar",
        kind="exact_title_doi",
        canonical_intent="exact DOI lookup: 10.1/x",
        rendered_query="10.1/x",
    )
    assert q.query_fingerprint
    assert q.kind == "exact_title_doi"
