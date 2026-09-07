"""Typed artifacts for query planning.

`QueryKind` and `CoverageDimensions` describe *why* a query exists;
`PlannedQuery` is what the planner actually hands to an adapter and what gets
persisted as a `QueryPlan` row.
"""

from typing import Literal

from pydantic import BaseModel

QueryKind = Literal[
    "broad_domain",
    "cost_component",
    "cost_driver",
    "named_project",
    "trusted_domain",
    "exact_title_doi",
    "author_organization",
    "citation_expansion",
    "dataset_model_name",
    "coverage_gap",
    # Added for the 500-request campaign authorized 2026-08-23: previously
    # `cost_representations`/`evidence_types` were defined coverage-matrix
    # dimensions with zero query templates ever referencing them.
    "cost_representation",
    "evidence_type",
]


class CoverageDimensions(BaseModel):
    """The coverage-matrix cell a query is intended to populate.

    Every field is optional because most real queries only pin down a few
    dimensions at once (e.g. a cost-component query pins `cost_component` and
    leaves `geography`/`technical_driver` open) -- see brief's "Coverage
    matrix and query planning": the full Cartesian product is never generated
    blindly.
    """

    # Coverage-novelty dimensions per the production ranking objective
    # (technology, cost scope, engineering metric, geography, year, source
    # class, language, format): `cost_component`/`cost_representation`
    # together are "cost scope", `technical_driver` is "engineering
    # metric", `publication_period` is "year", `evidence_type` is "source
    # class" -- kept under their original names rather than renamed, to
    # avoid an unnecessary, disruptive rename across every template/test
    # that already references them.
    technology_domain: str | None = None
    cost_component: str | None = None
    cost_representation: str | None = None
    technical_driver: str | None = None
    evidence_type: str | None = None
    geography: str | None = None
    language: str = "en"
    publication_period: str | None = None
    # pdf, xlsx, xls, csv, tsv, zip, html_table, json, xml, docx, pptx --
    # the "format" coverage-novelty dimension. Optional and unset by most
    # existing templates (a query itself doesn't usually pin one format),
    # populated by the structured/tabular-data query templates that do.
    format: str | None = None


class PlannedQuery(BaseModel):
    """One query the planner intends to run, before execution."""

    query_fingerprint: str
    adapter: str
    kind: QueryKind
    canonical_intent: str
    rendered_query: str
    coverage_dimensions: CoverageDimensions
    language: str = "en"
    priority: int = 0
