"""High-recall relevance screening.

Deterministic rules run first and are sufficient for the pipeline to operate
end-to-end with zero network/LLM calls (required for tests and for
`--dry-run`). An optional LLM-backed classifier can be plugged in behind the
`ScreeningClassifier` protocol for a future run, but nothing in this module
or its tests depends on one existing.

Reject is deliberately hard to reach: only an explicit denylist hit
(`reject_domains`/`reject_keywords`) triggers it. Everything else that scores
low goes to `manual_review`, never a silent drop -- per the brief, "missing a
rare cost report is worse than sending a moderate number of candidates to
manual review."
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from discovery import state_machine

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from discovery.db.models import ScreeningDecision, SourceCandidate

_DECISION_TO_STATUS = {
    "accept": "screened_accept",
    "manual_review": "screened_review",
    "reject": "screened_reject",
}


class ScreeningInput(BaseModel):
    """Everything the screener is allowed to look at: title/snippet/URL/
    metadata only -- never the document's full text (that would violate the
    discovery/extraction boundary; see CLAUDE.md-equivalent guidance in the
    brief's "Prohibited coupling").
    """

    title: str | None = None
    snippet: str | None = None
    canonical_url: str | None = None
    source_type: str | None = None
    access_status: str | None = None
    organization: str | None = None
    # research-resource format metadata (still metadata, not full text) --
    # drives the structured/tabular-data source-quality boost below.
    file_format: str | None = None
    structured_data_likelihood: float | None = None


class ScreeningResult(BaseModel):
    """A fully explainable, versioned screening decision."""

    decision: str  # accept, manual_review, reject
    direct_cost_evidence_score: float
    technical_driver_evidence_score: float
    domain_relevance_score: float
    source_quality_score: float
    accessibility_score: float
    coverage_novelty_score: float
    composite_score: float
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str
    rules_version: str
    model_version: str | None = None


class ScreeningClassifier(Protocol):
    """Extension point for an optional, structured-output LLM classifier.

    Not required for basic pipeline operation (see module docstring); if
    implemented, `classify()` must return the same component scores as the
    deterministic scorer so results are directly comparable/versioned
    alongside rules-only decisions.
    """

    def classify(self, screening_input: ScreeningInput) -> ScreeningResult: ...


class RulesScreener:
    """Deterministic keyword/metadata-based screener."""

    def __init__(self, rules: dict, *, accept_threshold: float, reject_threshold: float):
        self._rules = rules
        self._accept_threshold = accept_threshold
        self._reject_threshold = reject_threshold

    @classmethod
    def from_yaml(cls, path: Path, *, accept_threshold: float, reject_threshold: float) -> "RulesScreener":
        rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(rules, accept_threshold=accept_threshold, reject_threshold=reject_threshold)

    def _text_blob(self, screening_input: ScreeningInput) -> str:
        return " ".join(
            filter(None, [screening_input.title, screening_input.snippet])
        ).lower()

    def _keyword_score(self, blob: str, keywords: list[str]) -> tuple[float, list[str]]:
        hits = [kw for kw in keywords if kw.lower() in blob]
        if not hits:
            return 0.0, []
        # First hit already carries most of the signal; additional hits add a
        # capped bonus rather than scaling linearly (avoids keyword-stuffed
        # snippets scoring implausibly high).
        score = min(1.0, 0.7 + 0.1 * (len(hits) - 1))
        return score, hits

    def _domain(self, url: str | None) -> str | None:
        if not url:
            return None
        netloc = urlparse(url).netloc.lower().removeprefix("www.")
        return netloc or None

    _STRUCTURED_FORMATS: ClassVar[set[str]] = {"xlsx", "xls", "csv", "tsv", "zip", "json", "xml", "html_table"}

    def _is_structured_data(self, blob: str, screening_input: ScreeningInput) -> tuple[bool, list[str]]:
        """True if this resource is likely structured/tabular data (a
        spreadsheet, CSV, dataset, or an appendix/supplementary table)
        rather than narrative prose -- these yield cost/technical
        observations at much higher density per source, so they earn a
        source-quality boost (see `screen()`).
        """
        if screening_input.file_format in self._STRUCTURED_FORMATS:
            return True, []
        if (screening_input.structured_data_likelihood or 0.0) >= 0.6:
            return True, []
        hits = [
            kw for kw in self._rules.get("structured_data_keywords", []) if kw.lower() in blob
        ]
        return bool(hits), hits

    def screen(self, screening_input: ScreeningInput, *, coverage_novelty_score: float = 0.5) -> ScreeningResult:
        blob = self._text_blob(screening_input)
        domain = self._domain(screening_input.canonical_url)
        reason_codes: list[str] = []

        reject_domains = set(self._rules.get("reject_domains", []))
        reject_keywords = self._rules.get("reject_keywords", [])
        if domain and domain in reject_domains:
            reason_codes.append("reject_domain_match")
            return self._reject_result(reason_codes, f"Domain '{domain}' is on the reject denylist.")
        matched_reject_kw = [kw for kw in reject_keywords if kw.lower() in blob]
        if matched_reject_kw:
            reason_codes.append("reject_keyword_match")
            return self._reject_result(
                reason_codes, f"Matched reject keyword(s): {matched_reject_kw}."
            )

        direct_cost_score, cost_hits = self._keyword_score(
            blob, self._rules.get("direct_cost_keywords", [])
        )
        if cost_hits:
            reason_codes.append("direct_cost_keyword_match")

        driver_score, driver_hits = self._keyword_score(
            blob, self._rules.get("technical_driver_keywords", [])
        )
        if driver_hits:
            reason_codes.append("technical_driver_keyword_match")

        domain_score, domain_hits = self._keyword_score(blob, self._rules.get("domain_keywords", []))
        if domain_hits:
            reason_codes.append("domain_keyword_match")

        source_quality_map = self._rules.get("source_quality_by_type", {})
        source_quality_score = source_quality_map.get(screening_input.source_type or "", 0.5)
        trusted_domains = set(self._rules.get("trusted_domains", []))
        if domain and domain in trusted_domains:
            source_quality_score = min(1.0, source_quality_score + 0.1)
            reason_codes.append("trusted_source_domain")

        is_structured, structured_hits = self._is_structured_data(blob, screening_input)
        if is_structured:
            source_quality_score = min(1.0, source_quality_score + 0.15)
            reason_codes.append("structured_data_boost")
            if structured_hits:
                reason_codes.append(f"structured_data_keyword_match:{structured_hits[0]}")

        accessibility_map = self._rules.get("accessibility_by_status", {})
        accessibility_score = accessibility_map.get(screening_input.access_status or "unknown", 0.6)

        weights = self._rules.get("weights", {})
        raw_composite = (
            direct_cost_score * weights.get("direct_cost_evidence", 0.3)
            + driver_score * weights.get("technical_driver_evidence", 0.2)
            + domain_score * weights.get("domain_relevance", 0.2)
            + source_quality_score * weights.get("source_quality", 0.15)
            + accessibility_score * weights.get("accessibility", 0.1)
            + coverage_novelty_score * weights.get("coverage_novelty", 0.05)
        )
        # Round once, to the same 4-decimal precision `composite_score` is
        # always persisted/displayed at, and use THIS value for the decision
        # -- never the unrounded sum. IEEE-754 binary floats can't represent
        # most two-decimal weights (0.30, 0.15, ...) exactly, so summing six
        # of them can land a fraction below a threshold that is mathematically
        # exact (observed live: component scores summing to a "true" 0.55
        # produced a raw composite of 0.5499999999999998, which failed
        # `>= 0.55` while still *rounding* to a displayed 0.55 -- 20 real
        # candidates were stuck in manual_review despite a stored
        # composite_score exactly equal to the accept threshold). Comparing
        # and storing the same rounded value eliminates that class of
        # decision/display mismatch entirely -- not by tolerance/epsilon
        # (which would silently promote genuinely-below-threshold candidates
        # too), but because 4 decimal places is already the composite
        # score's own declared precision: no information beyond
        # floating-point representation noise is discarded.
        composite = round(raw_composite, 4)

        if not (cost_hits or driver_hits or domain_hits):
            reason_codes.append("no_relevant_keyword_signal")

        decision = "accept" if composite >= self._accept_threshold else "manual_review"
        if decision == "accept":
            reason_codes.append("composite_score_above_accept_threshold")
        else:
            reason_codes.append("composite_score_below_accept_threshold")

        return ScreeningResult(
            decision=decision,
            direct_cost_evidence_score=direct_cost_score,
            technical_driver_evidence_score=driver_score,
            domain_relevance_score=domain_score,
            source_quality_score=source_quality_score,
            accessibility_score=accessibility_score,
            coverage_novelty_score=coverage_novelty_score,
            composite_score=composite,  # already rounded above, before the decision was made
            reason_codes=reason_codes,
            explanation=(
                f"composite={composite:.3f} "
                f"(cost={direct_cost_score:.2f}, driver={driver_score:.2f}, "
                f"domain={domain_score:.2f}, quality={source_quality_score:.2f}, "
                f"access={accessibility_score:.2f}, novelty={coverage_novelty_score:.2f}); "
                f"decision={decision}"
            ),
            rules_version=self._rules.get("version", "unknown"),
            model_version=None,
        )

    def _reject_result(self, reason_codes: list[str], explanation: str) -> ScreeningResult:
        return ScreeningResult(
            decision="reject",
            direct_cost_evidence_score=0.0,
            technical_driver_evidence_score=0.0,
            domain_relevance_score=0.0,
            source_quality_score=0.0,
            accessibility_score=0.0,
            coverage_novelty_score=0.0,
            composite_score=0.0,
            reason_codes=reason_codes,
            explanation=explanation,
            rules_version=self._rules.get("version", "unknown"),
            model_version=None,
        )


def persist_screening_decision(
    db: "Session", candidate: "SourceCandidate", result: ScreeningResult, *, reviewer: str | None = None
) -> "ScreeningDecision":
    """Persist `result` as a `ScreeningDecision` row and advance the
    candidate's state machine accordingly. Never mutates a candidate already
    past `deduplicated` in a way the state machine disallows -- a second
    screening pass (re-screen) is only valid from `screened_review`.
    """
    from discovery.db.models import ScreeningDecision

    decision_row = ScreeningDecision(
        source_candidate_id=candidate.id,
        decision=result.decision,
        direct_cost_evidence_score=result.direct_cost_evidence_score,
        technical_driver_evidence_score=result.technical_driver_evidence_score,
        domain_relevance_score=result.domain_relevance_score,
        source_quality_score=result.source_quality_score,
        accessibility_score=result.accessibility_score,
        coverage_novelty_score=result.coverage_novelty_score,
        composite_score=result.composite_score,
        reason_codes_json=json.dumps(result.reason_codes),
        explanation=result.explanation,
        rules_version=result.rules_version,
        model_version=result.model_version,
        reviewer=reviewer,
    )
    db.add(decision_row)

    new_status = _DECISION_TO_STATUS[result.decision]
    state_machine.apply_transition(candidate, new_status)
    if result.decision == "accept":
        from discovery.access_policy import is_restricted_access

        if is_restricted_access(candidate.access_status):
            # Known-restricted (licensed_mit_access, authentication_required,
            # manual_acquisition_required, or unavailable) at candidate-
            # normalization time (see access_policy.py) -- go straight to
            # the `paywalled` state (this pipeline's general "metadata-only,
            # never automatically downloaded" terminal) instead of
            # `acquisition_pending`, regardless of which specific restricted
            # status applies. Per the brief: "Preserve paywalled sources as
            # metadata-only candidates rather than attempting unauthorized
            # downloads." `manual_acquisition.py` is the only path a
            # restricted candidate can still reach `downloaded` through --
            # a human-supplied file, matched back by hash/title, never an
            # automated fetch.
            state_machine.apply_transition(candidate, "paywalled")
        else:
            state_machine.apply_transition(candidate, "acquisition_pending")

    db.flush()
    return decision_row
