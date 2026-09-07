"""Typed artifacts for search results, aggregated candidates, and the handoff contract."""

from pydantic import BaseModel, Field


class RawSearchHit(BaseModel):
    """One result exactly as an adapter parsed it from a provider response.

    Deliberately provider-agnostic (every adapter normalizes into this shape)
    so `result_registry`/`normalizer` never need to know which adapter a hit
    came from.
    """

    title: str | None = None
    url: str | None = None
    snippet: str | None = None
    displayed_source: str | None = None
    publication_info: str | None = None
    provider_result_id: str | None = None
    cited_by_id: str | None = None
    cluster_version_id: str | None = None
    rank: int | None = None
    doi: str | None = None
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    raw: dict = Field(default_factory=dict)


class AdapterSearchResponse(BaseModel):
    """Everything an adapter returns for one request, before persistence."""

    provider_request_id: str | None = None
    result_count: int
    hits: list[RawSearchHit] = Field(default_factory=list)
    next_page_cursor: str | None = None
    raw_response: dict = Field(default_factory=dict)


class AggregatedCandidate(BaseModel):
    """The normalized, deduplicated view of a candidate, independent of storage.

    This is the in-memory shape `normalizer.py`/`deduplicator.py` build and
    compare before writing/updating a `SourceCandidate` row; it never carries
    a DB id, so building one is side-effect-free and testable without a
    session.
    """

    canonical_url: str | None = None
    # The URL as actually discovered, before `normalize_url()` strips `www.`,
    # forces https, or otherwise rewrites it for dedup purposes -- kept
    # separately so acquisition can fetch a URL that still resolves for
    # hosts whose DNS genuinely requires the stripped prefix (e.g. nrel.gov
    # vs www.nrel.gov), without weakening `canonical_url`'s role as the
    # dedup identity key. `None` for anything constructed before this field
    # existed, or for a caller with no raw URL to give (e.g. a DOI-only
    # candidate) -- callers must fall back to `canonical_url` in that case.
    direct_download_url: str | None = None
    doi: str | None = None
    normalized_title: str | None = None
    authors: list[str] = Field(default_factory=list)
    organization: str | None = None
    publication_year: int | None = None
    source_type: str | None = None
    evidence_tier: str | None = None
    technology_domains: list[str] = Field(default_factory=list)
    jurisdiction: str | None = None
    language: str | None = None
    access_status: str | None = None
    expected_evidence_categories: list[str] = Field(default_factory=list)


class HandoffEntry(BaseModel):
    """One entry in the handoff manifest -- the contract with geocost.

    Field names are chosen to map 1:1 onto geocost's `SourceDocument` columns
    (`title`, `authors`, `publication_year`, `doi_or_url`, `source_type`,
    `evidence_tier`, `technology_domain`, `filename`/local path, `sha256`) so
    a future importer is a thin field-by-field copy, not a translation layer.
    See docs/handoff_contract.md.
    """

    candidate_id: str
    source_document_id: str | None = None
    local_path: str
    sha256: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    publication_year: int | None = None
    doi: str | None = None
    canonical_url: str | None = None
    organization: str | None = None
    jurisdiction: str | None = None
    source_type: str | None = None
    evidence_tier: str | None = None
    technology_domains: list[str] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    discovery_run_id: str
    ready_for_ingestion: bool = True


class HandoffManifest(BaseModel):
    """The self-hashed, immutable JSON artifact produced by `discovery handoff`.

    Same shape as geocost's own `extraction/selection_manifest.py` pattern: a
    `manifest_sha256` computed over the entries themselves, so a consumer can
    verify the file wasn't hand-edited after the fact.
    """

    schema_version: str = "1.0"
    discovery_run_id: str
    generated_at: str
    entry_count: int
    entries: list[HandoffEntry]
    manifest_sha256: str
