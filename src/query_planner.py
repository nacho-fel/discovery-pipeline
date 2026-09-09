"""Deterministic, inspectable query planning from curated templates.

Loads `config/coverage_matrix.yaml` (controlled-vocabulary dimension values)
and `config/query_templates.yaml` (curated templates, each referencing 1-2
dimensions by `{placeholder}` name), and produces one `PlannedQuery` per
(template, adapter, binding) combination -- never the full Cartesian product
of every dimension, only the specific ones each template actually names.

`exact_title_doi`, `author_organization`, `citation_expansion`, and
`coverage_gap` queries are NOT produced here; those are generated at runtime
from already-discovered candidates by `frontier.py` / `coverage_analyzer.py`,
using `build_programmatic_query()` below so they share the same fingerprint
shape as templated queries.
"""

import string
from itertools import product
from pathlib import Path

import yaml

from discovery.fingerprint import query_fingerprint
from discovery.models.query import CoverageDimensions, PlannedQuery, QueryKind

# Template placeholder name -> coverage_matrix.yaml list key.
_FIELD_TO_MATRIX_KEY = {
    "technology_domain": "technology_domains",
    "cost_component": "cost_components",
    "cost_representation": "cost_representations",
    "technical_driver": "technical_drivers",
    "evidence_type": "evidence_types",
    "geography": "geographies",
    "language": "languages",
    "trusted_domain": "trusted_domains",
    "named_project": "named_projects",
    "named_dataset": "named_datasets",
    "repository_domain": "repository_domains",
}

# Fields whose raw value is a literal (domain name, proper noun) and must
# never be humanized (underscore -> space) when rendered into a query string.
_LITERAL_FIELDS = {"trusted_domain", "named_project", "named_dataset", "repository_domain"}


def _humanize(value: str) -> str:
    return value.replace("_", " ")


def load_coverage_matrix(path: Path) -> dict:
    """Load the coverage-matrix YAML as a plain dict of dimension -> value list."""
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_query_templates(path: Path) -> list[dict]:
    """Load the query-templates YAML as a list of template dicts."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    templates: list[dict] = data.get("templates", [])
    return templates


def load_multilingual_terms(path: Path) -> list[dict]:
    """Load config/multilingual_terms.yaml's paired (language, phrase) list."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    terms: list[dict] = data.get("terms", [])
    return terms


def plan_multilingual_queries(
    *, terms: list[dict], enabled_adapters: set[str]
) -> list[PlannedQuery]:
    """Build one `PlannedQuery` per paired (language, phrase) term.

    Unlike `plan_queries`, this never cross-products fields -- each term is
    already a complete, coupled (language, phrase) pair, built directly via
    `build_programmatic_query` so it shares the same fingerprint shape as
    every other query. Runs against `serpapi_google` and `serpapi_scholar`
    (both handle arbitrary non-English text natively); `openalex`/`crossref`
    are skipped here since their `search=`/`query=` params are metadata-field
    search, not general free-text search, so a non-English natural-language
    phrase is a poor fit for them.
    """
    target_adapters = {"serpapi_google", "serpapi_scholar"} & enabled_adapters
    planned: list[PlannedQuery] = []
    for term in terms:
        language = term["language"]
        phrase = term["phrase"]
        kind = term.get("kind", "broad_domain")
        for adapter in sorted(target_adapters):
            planned.append(
                build_programmatic_query(
                    adapter=adapter,
                    kind=kind,
                    canonical_intent=f"multilingual [{language}]: {phrase}",
                    rendered_query=phrase,
                    coverage_dimensions=CoverageDimensions(language=language),
                    priority=18,
                )
            )
    return planned


def _template_field_names(template: dict) -> list[str]:
    formatter = string.Formatter()
    fields = []
    for source in (template["intent"], template["query"]):
        for _, field_name, _, _ in formatter.parse(source):
            if field_name and field_name not in fields:
                fields.append(field_name)
    return fields


def _bindings_for_template(template: dict, coverage_matrix: dict) -> list[dict]:
    fields = _template_field_names(template)
    value_lists = []
    for field in fields:
        matrix_key = _FIELD_TO_MATRIX_KEY.get(field)
        values = coverage_matrix.get(matrix_key, []) if matrix_key else []
        value_lists.append(values)
    if not fields or any(not values for values in value_lists):
        return []
    combos = product(*value_lists)
    return [dict(zip(fields, combo, strict=True)) for combo in combos]


def build_programmatic_query(
    *,
    adapter: str,
    kind: QueryKind,
    canonical_intent: str,
    rendered_query: str,
    coverage_dimensions: CoverageDimensions | None = None,
    priority: int = 40,
) -> PlannedQuery:
    """Build one `PlannedQuery` for a runtime-generated (non-templated) query
    -- citation expansion, exact-title/DOI lookups, author/organization
    searches, coverage-gap re-queries -- using the same fingerprint shape as
    every templated query, so idempotency/dedup treats them identically.
    """
    dims = coverage_dimensions or CoverageDimensions()
    fingerprint = query_fingerprint(
        adapter=adapter,
        canonical_intent=canonical_intent,
        rendered_query=rendered_query,
        language=dims.language,
    )
    return PlannedQuery(
        query_fingerprint=fingerprint,
        adapter=adapter,
        kind=kind,
        canonical_intent=canonical_intent,
        rendered_query=rendered_query,
        coverage_dimensions=dims,
        language=dims.language,
        priority=priority,
    )


def plan_queries(
    *,
    coverage_matrix: dict,
    templates: list[dict],
    enabled_adapters: set[str],
) -> list[PlannedQuery]:
    """Expand every template against `coverage_matrix` for each of its
    applicable, currently-enabled adapters. Deterministic: iterates templates
    and coverage-matrix values in their declared YAML order, so re-running
    planning against the same config files always yields the same query
    fingerprints in the same order.
    """
    planned: list[PlannedQuery] = []

    for template in templates:
        applicable_adapters = [a for a in template.get("adapters", []) if a in enabled_adapters]
        if not applicable_adapters:
            continue

        bindings = _bindings_for_template(template, coverage_matrix)
        if not bindings:
            continue

        for binding in bindings:
            render_binding = {
                field: value if field in _LITERAL_FIELDS else _humanize(value)
                for field, value in binding.items()
            }
            canonical_intent = template["intent"].format(**render_binding)
            rendered_query = template["query"].format(**render_binding)

            dims_kwargs = {
                field: value
                for field, value in binding.items()
                if field in CoverageDimensions.model_fields
            }
            dims = CoverageDimensions(**dims_kwargs)

            for adapter in applicable_adapters:
                fingerprint = query_fingerprint(
                    adapter=adapter,
                    canonical_intent=canonical_intent,
                    rendered_query=rendered_query,
                    language=dims.language,
                )
                planned.append(
                    PlannedQuery(
                        query_fingerprint=fingerprint,
                        adapter=adapter,
                        kind=template["kind"],
                        canonical_intent=canonical_intent,
                        rendered_query=rendered_query,
                        coverage_dimensions=dims,
                        language=dims.language,
                        priority=template.get("priority", 0),
                    )
                )

    return planned
