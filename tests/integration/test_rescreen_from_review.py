"""Proves `persist_screening_decision` is safe to call as a re-screen path
against a candidate already sitting at `screened_review` -- exactly the
mechanism the float-boundary fix (commit e7aeee1) needs to promote the 20
production candidates whose raw composite crossed 0.55 only after rounding.
`persist_screening_decision`'s own docstring already claims this is a valid
use ("a second screening pass (re-screen) is only valid from
`screened_review`"); nothing previously exercised that claim end-to-end
against a real DB session and the real state machine, so this closes that
gap before the mechanism is used against production data. Offline only, no
network calls.
"""

from discovery.db.models import ScreeningDecision, SourceCandidate
from discovery.screener import ScreeningResult, persist_screening_decision

_BOUNDARY_RESULT_KWARGS = dict(
    decision="accept",
    direct_cost_evidence_score=0.7999999999999999,
    technical_driver_evidence_score=0.0,
    domain_relevance_score=0.7,
    source_quality_score=0.65,
    accessibility_score=0.6,
    coverage_novelty_score=0.25,
    composite_score=0.55,
    reason_codes=["direct_cost_keyword_match"],
    explanation="re-screen: float-boundary fix promotes this candidate from manual_review to accept",
    rules_version="v3-fixed",
)


def _screened_review_candidate(db, **overrides) -> SourceCandidate:
    defaults = dict(
        canonical_url="https://example.com/geothermal-cost-report.pdf",
        normalized_title="Geothermal well cost drilling spreadsheet",
        screening_status="screened_review",
        access_status="open_access",
    )
    defaults.update(overrides)
    candidate = SourceCandidate(**defaults)
    db.add(candidate)
    db.flush()
    return candidate


def test_rescreen_from_screened_review_promotes_to_accept_and_records_new_audit_row(test_db):
    candidate = _screened_review_candidate(test_db)

    # An earlier, real screening pass already put this candidate at
    # screened_review with the pre-fix (manual_review) decision -- exactly
    # what production looks like for the 20 anomalous candidates today.
    original_decision = ScreeningDecision(
        source_candidate_id=candidate.id,
        decision="manual_review",
        direct_cost_evidence_score=0.7999999999999999,
        technical_driver_evidence_score=0.0,
        domain_relevance_score=0.7,
        source_quality_score=0.65,
        accessibility_score=0.6,
        coverage_novelty_score=0.25,
        composite_score=0.55,
        reason_codes_json="[]",
        explanation="original pre-fix screening pass",
        rules_version="v3",
    )
    test_db.add(original_decision)
    test_db.flush()

    result = ScreeningResult(**_BOUNDARY_RESULT_KWARGS)
    new_decision_row = persist_screening_decision(test_db, candidate, result, reviewer="float-fix-rescreen")
    test_db.flush()

    assert candidate.screening_status == "acquisition_pending"
    assert new_decision_row.decision == "accept"
    assert new_decision_row.composite_score == 0.55

    all_decisions = (
        test_db.query(ScreeningDecision)
        .filter(ScreeningDecision.source_candidate_id == candidate.id)
        .order_by(ScreeningDecision.decided_at)
        .all()
    )
    assert len(all_decisions) == 2, "the original manual_review audit row must be preserved, not overwritten"
    assert all_decisions[0].decision == "manual_review"
    assert all_decisions[1].decision == "accept"
    assert all_decisions[1].reviewer == "float-fix-rescreen"


def test_rescreen_from_screened_review_sends_restricted_access_to_paywalled_not_acquisition_pending(test_db):
    candidate = _screened_review_candidate(test_db, access_status="licensed_mit_access")

    result = ScreeningResult(**_BOUNDARY_RESULT_KWARGS)
    persist_screening_decision(test_db, candidate, result)
    test_db.flush()

    assert candidate.screening_status == "paywalled"


def test_rescreen_refuses_to_touch_a_candidate_not_at_screened_review(test_db):
    candidate = _screened_review_candidate(test_db, screening_status="handed_off")

    result = ScreeningResult(**_BOUNDARY_RESULT_KWARGS)
    try:
        persist_screening_decision(test_db, candidate, result)
        raised = False
    except Exception:
        raised = True

    assert raised, "the state machine must reject re-screening a candidate already past screened_review"
    assert candidate.screening_status == "handed_off"
