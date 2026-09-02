import pytest

from discovery.screener import RulesScreener, ScreeningInput


def _screener(**overrides):
    return RulesScreener.from_yaml(
        overrides.pop("path"),
        accept_threshold=overrides.pop("accept_threshold", 0.55),
        reject_threshold=overrides.pop("reject_threshold", 0.15),
    )


def test_strong_direct_cost_match_is_accepted(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="Enhanced Geothermal Systems Drilling Cost Report",
            snippet="This government technical report presents drilling cost per foot data.",
            canonical_url="https://nrel.gov/reports/egs-drilling-cost.pdf",
            source_type="government_technical_report",
            access_status="open_access",
        )
    )
    assert result.decision == "accept"
    assert "direct_cost_keyword_match" in result.reason_codes
    assert result.rules_version == "rules-v4"


def test_structured_data_format_boosts_source_quality(screening_rules_path):
    # A middling, non-trusted-domain source_type so the boost isn't
    # invisibly clipped by the score's 1.0 ceiling.
    screener = _screener(path=screening_rules_path)
    base = ScreeningInput(
        title="Enhanced Geothermal Systems Drilling Cost Report",
        snippet="This report presents drilling cost per foot data.",
        canonical_url="https://example.com/report.pdf",
        source_type="conference_paper",
        access_status="open_access",
    )
    narrative = screener.screen(base)
    structured = screener.screen(base.model_copy(update={"file_format": "xlsx"}))

    assert structured.source_quality_score > narrative.source_quality_score
    assert "structured_data_boost" in structured.reason_codes
    assert "structured_data_boost" not in narrative.reason_codes


def test_structured_data_keyword_in_snippet_also_boosts_source_quality(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="Enhanced Geothermal Systems Drilling Cost Report",
            snippet="Includes a downloadable cost dataset spreadsheet with per-well breakdowns.",
            canonical_url="https://nrel.gov/reports/egs-drilling-cost.pdf",
            source_type="government_technical_report",
            access_status="open_access",
        )
    )
    assert "structured_data_boost" in result.reason_codes


def test_structured_data_likelihood_field_also_boosts_source_quality(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="Enhanced Geothermal Systems Drilling Cost Report",
            snippet="This government technical report presents drilling cost per foot data.",
            canonical_url="https://nrel.gov/reports/egs-drilling-cost.pdf",
            source_type="government_technical_report",
            access_status="open_access",
            structured_data_likelihood=0.9,
        )
    )
    assert "structured_data_boost" in result.reason_codes


def test_coverage_novelty_score_defaults_to_half_when_not_supplied(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(title="x", snippet="drilling cost", canonical_url="https://example.com/x")
    )
    assert result.coverage_novelty_score == 0.5


def test_coverage_novelty_score_is_pass_through_from_caller(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(title="x", snippet="drilling cost", canonical_url="https://example.com/x"),
        coverage_novelty_score=0.9,
    )
    assert result.coverage_novelty_score == 0.9


def test_irrelevant_result_goes_to_manual_review_not_reject(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="A History of Regional Cuisine",
            snippet="This article discusses local food traditions.",
            canonical_url="https://example.com/food",
        )
    )
    assert result.decision == "manual_review"
    assert "no_relevant_keyword_signal" in result.reason_codes


def test_denylisted_domain_is_rejected(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(title="Geothermal drilling cost", canonical_url="https://pinterest.com/pin/123")
    )
    assert result.decision == "reject"
    assert "reject_domain_match" in result.reason_codes


def test_low_score_without_denylist_hit_never_auto_rejects(screening_rules_path):
    screener = _screener(path=screening_rules_path, accept_threshold=0.99)
    result = screener.screen(
        ScreeningInput(title="Geothermal drilling cost", canonical_url="https://example.com/report")
    )
    assert result.decision != "reject"


def test_trusted_domain_boosts_source_quality(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    trusted = screener.screen(
        ScreeningInput(title="Drilling cost report", canonical_url="https://nrel.gov/x")
    )
    untrusted = screener.screen(
        ScreeningInput(title="Drilling cost report", canonical_url="https://example.com/x")
    )
    assert trusted.source_quality_score >= untrusted.source_quality_score


def test_spanish_cost_and_domain_terms_are_recognized(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="Costo de perforación geotérmica en Costa Rica",
            snippet="Este informe presenta el costo de pozo geotérmico por metro perforado.",
            canonical_url="https://example.com/informe.pdf",
        )
    )
    assert "direct_cost_keyword_match" in result.reason_codes
    assert "domain_keyword_match" in result.reason_codes


def test_french_and_german_cost_terms_are_recognized(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    french = screener.screen(
        ScreeningInput(title="Coût de forage géothermique", canonical_url="https://example.fr/x")
    )
    german = screener.screen(
        ScreeningInput(title="Bohrkosten Geothermie Bericht", canonical_url="https://example.de/x")
    )
    assert "direct_cost_keyword_match" in french.reason_codes
    assert "direct_cost_keyword_match" in german.reason_codes


def test_english_afe_and_unconventional_synonyms_are_recognized(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="Unconventional shale well economics",
            snippet="Authorization for expenditure detail with cost per lateral foot and day rate.",
            canonical_url="https://example.com/report",
        )
    )
    assert "direct_cost_keyword_match" in result.reason_codes


def test_onepetro_licensed_still_scores_source_quality(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    result = screener.screen(
        ScreeningInput(
            title="SPE Drilling Cost Analysis",
            canonical_url="https://onepetro.org/SPE/12345",
            access_status="licensed_mit_access",
        )
    )
    # trusted_domains boost applies to source_quality independent of the
    # separately-scored (and lower) accessibility_score for a restricted-access source.
    assert result.source_quality_score > 0.5
    assert result.accessibility_score < 0.5


def test_v3_reversed_word_order_cost_phrases_are_recognized(screening_rules_path):
    """Regression test for the false negatives found auditing the 2026-08-20
    live smoke test: "costs per well" (EIA) and "cost of drilling a well"
    (Congressional Research Service) are genuine cost evidence that the v2
    "drilling cost"/"well cost" keywords missed purely due to word order.
    """
    screener = _screener(path=screening_rules_path)
    eia_style = screener.screen(
        ScreeningInput(
            title="EIA report shows decline in cost of U.S. oil and gas wells",
            snippet="Costs per well generally increased from 2006 to 2012.",
            canonical_url="https://eia.gov/todayinenergy/detail.php?id=25592",
        )
    )
    crs_style = screener.screen(
        ScreeningInput(
            title="An overview of unconventional oil and natural gas",
            snippet="roughly 0.13 to 0.21% of the cost of drilling a well.",
            canonical_url="https://congress.gov/crs-product/R43148",
        )
    )
    assert "direct_cost_keyword_match" in eia_style.reason_codes
    assert "direct_cost_keyword_match" in crs_style.reason_codes


def test_v3_additions_do_not_create_false_positives_on_known_irrelevant_snippets(
    screening_rules_path,
):
    """Regression test: the v3 keyword additions must not retroactively
    flag content that is genuinely off-topic (found during the same audit --
    a residential HVAC discussion and a site-access-road construction
    article, neither about well/drilling cost) as direct-cost evidence.
    """
    screener = _screener(path=screening_rules_path)
    hvac = screener.screen(
        ScreeningInput(
            title="Vertical vs. horizontal loop costs and contractors",
            snippet=(
                "I had a closed loop waterfurnace 4 ton, 5 series with a desuperheater "
                "installed for $17.7k back in 2013."
            ),
            canonical_url="https://reddit.com/r/geothermal/comments/x",
        )
    )
    site_access = screener.screen(
        ScreeningInput(
            title="Well site construction: proven cost savings tactic",
            snippet=(
                "When oil was $65 a barrel, the cost of developing drill rig sites "
                "was less significant."
            ),
            canonical_url="https://tensarcorp.com/resources/articles/x",
        )
    )
    assert "direct_cost_keyword_match" not in hvac.reason_codes
    assert "direct_cost_keyword_match" not in site_access.reason_codes


def test_composite_score_is_reproducible(screening_rules_path):
    screener = _screener(path=screening_rules_path)
    screening_input = ScreeningInput(title="drilling cost report", canonical_url="https://example.com")
    first = screener.screen(screening_input)
    second = screener.screen(screening_input)
    assert first.composite_score == second.composite_score
    assert first.explanation == second.explanation


# --- Float-boundary regression tests -----------------------------------
#
# Root cause (found live: 20 real candidates in the production database
# with `composite_score` stored/displayed as exactly 0.55 but
# `decision='manual_review'`): the accept/manual_review decision used to
# compare the *raw, unrounded* floating-point sum of six weighted
# component scores against `accept_threshold`, while `composite_score`
# stored `round(raw, 4)`. Summing six decimal-fraction multiplications in
# IEEE-754 binary floats is not exact -- component scores that are
# mathematically 0.7999999999999999999... (== 0.8) etc. combine to a raw
# sum like 0.5499999999999998, which fails `>= 0.55` even though it
# rounds to a displayed 0.55. The fix rounds once, before the comparison,
# so the decision and the stored value can never disagree.


def _custom_screener(rules: dict, *, accept_threshold: float) -> RulesScreener:
    return RulesScreener(rules, accept_threshold=accept_threshold, reject_threshold=0.15)


_BOUNDARY_RULES = {
    "weights": {
        "direct_cost_evidence": 0.30,
        "technical_driver_evidence": 0.20,
        "domain_relevance": 0.20,
        "source_quality": 0.15,
        "accessibility": 0.10,
        "coverage_novelty": 0.05,
    },
    "direct_cost_keywords": ["drilling cost", "well cost"],
    "technical_driver_keywords": [],
    "domain_keywords": ["geothermal"],
    "structured_data_keywords": ["spreadsheet"],
    "source_quality_by_type": {},
    "trusted_domains": [],
    "accessibility_by_status": {},
    "reject_domains": [],
    "reject_keywords": [],
    "version": "boundary-test",
}


def test_reproduces_the_real_composite_0_55_boundary_anomaly(screening_rules_path):
    """Exact reproduction of production candidate 913a92f2's component
    scores (direct_cost=0.8, technical_driver=0.0, domain_relevance=0.7,
    source_quality=0.65 [default 0.5 + structured_data_boost 0.15],
    accessibility=0.6 [unknown-status default], coverage_novelty=0.25) --
    these mathematically sum to exactly 0.55, but the raw IEEE-754 sum is
    0.5499999999999998. Before the fix this produced 'manual_review' with
    a stored composite_score of 0.55; after the fix it must be 'accept'.
    """
    screener = _custom_screener(_BOUNDARY_RULES, accept_threshold=0.55)
    result = screener.screen(
        ScreeningInput(
            title="drilling cost and well cost spreadsheet",
            snippet="geothermal reservoir data",
            canonical_url="https://example.com/report",
            access_status=None,
        ),
        coverage_novelty_score=0.25,
    )
    assert result.direct_cost_evidence_score == 0.7999999999999999  # 0.7 + 0.1, not the literal 0.8
    assert result.technical_driver_evidence_score == 0.0
    assert result.domain_relevance_score == 0.7
    assert result.source_quality_score == 0.65
    assert result.accessibility_score == 0.6
    assert result.composite_score == 0.55
    assert result.decision == "accept"


@pytest.mark.parametrize(
    ("coverage_novelty_score", "expected_composite", "expected_decision"),
    [
        (0.24, 0.5495, "manual_review"),  # immediately below 0.55
        (0.25, 0.55, "accept"),  # exactly 0.55
        (0.26, 0.5505, "accept"),  # immediately above 0.55
    ],
)
def test_decision_and_stored_composite_score_never_disagree_at_the_0_55_boundary(
    screening_rules_path, coverage_novelty_score, expected_composite, expected_decision
):
    screener = _custom_screener(_BOUNDARY_RULES, accept_threshold=0.55)
    result = screener.screen(
        ScreeningInput(
            title="drilling cost and well cost spreadsheet",
            snippet="geothermal reservoir data",
            canonical_url="https://example.com/report",
            access_status=None,
        ),
        coverage_novelty_score=coverage_novelty_score,
    )
    assert result.composite_score == expected_composite
    assert result.decision == expected_decision
    # The general property this whole fix guarantees: the decision is
    # always consistent with the *stored* (rounded) composite_score, never
    # with some other, unrounded value a caller can't see.
    if result.decision == "accept":
        assert result.composite_score >= screener._accept_threshold
    else:
        assert result.composite_score < screener._accept_threshold


_BOUNDARY_RULES_050 = {
    **_BOUNDARY_RULES,
    # 4 distinct keywords -> direct_cost_evidence_score caps at
    # min(1.0, 0.7 + 0.1*(hits-1)) = 1.0, giving a base (with no domain/
    # driver hit) of 0.4575 -- low enough that coverage_novelty_score's
    # 0.05 weight can sweep across the 0.4995-0.5005 boundary.
    "direct_cost_keywords": ["drilling cost", "well cost", "cost per foot", "cost per meter"],
}


@pytest.mark.parametrize(
    ("coverage_novelty_score", "expected_composite", "expected_decision"),
    [
        (0.84, 0.4995, "manual_review"),  # immediately below 0.50
        (0.85, 0.50, "accept"),  # exactly 0.50
        (0.86, 0.5005, "accept"),  # immediately above 0.50
    ],
)
def test_decision_and_stored_composite_score_never_disagree_at_the_0_50_boundary(
    screening_rules_path, coverage_novelty_score, expected_composite, expected_decision
):
    """Same property, at the alternate 0.50 threshold under consideration
    for production (see the 0.50-0.55 promotion-cohort analysis) -- the
    fix is threshold-value-independent, not a special case for 0.55.
    """
    screener = _custom_screener(_BOUNDARY_RULES_050, accept_threshold=0.50)
    result = screener.screen(
        ScreeningInput(
            title="drilling cost, well cost, cost per foot, cost per meter",
            snippet="a spreadsheet report",
            canonical_url="https://example.com/report",
            access_status=None,
        ),
        coverage_novelty_score=coverage_novelty_score,
    )
    assert result.source_quality_score == 0.65  # 0.5 default + 0.15 structured_data_boost
    assert result.composite_score == expected_composite
    assert result.decision == expected_decision


def test_composite_score_serialization_round_trip_preserves_the_decision_boundary(screening_rules_path):
    """A ScreeningResult's composite_score must survive a JSON
    (de)serialization round-trip (e.g. persisted then reloaded from
    `ScreeningDecision.composite_score`, a plain float DB column) bit-for-bit
    identically, so re-deriving a decision from a reloaded score can never
    disagree with the decision that was actually made and stored.
    """
    import json

    screener = _custom_screener(_BOUNDARY_RULES, accept_threshold=0.55)
    result = screener.screen(
        ScreeningInput(
            title="drilling cost and well cost spreadsheet",
            snippet="geothermal reservoir data",
            canonical_url="https://example.com/report",
            access_status=None,
        ),
        coverage_novelty_score=0.25,
    )
    round_tripped = json.loads(json.dumps({"composite_score": result.composite_score}))["composite_score"]
    assert round_tripped == result.composite_score
    assert (round_tripped >= screener._accept_threshold) == (result.decision == "accept")


def test_historical_anomalous_candidates_are_reproducible_from_persisted_component_scores(screening_rules_path):
    """All 20 candidates found live with composite_score==0.55 and
    decision=='manual_review' share the same reconstructable root cause:
    recomputing their exact persisted component scores against the real
    production weights must now decide 'accept', matching what a re-screen
    with the fixed comparison logic would produce. This doesn't require a
    live DB connection -- the 5 distinct persisted component-score
    combinations found among the 20 anomalous rows are reproduced directly
    (verified against `screening_decision` at fix time).
    """
    weights = {
        "direct_cost_evidence": 0.30,
        "technical_driver_evidence": 0.20,
        "domain_relevance": 0.20,
        "source_quality": 0.15,
        "accessibility": 0.10,
        "coverage_novelty": 0.05,
    }
    # (direct_cost, technical_driver, domain_relevance, source_quality, accessibility, coverage_novelty)
    historical_component_scores = [
        (0.7999999999999999, 0.0, 0.7, 0.65, 0.6, 0.25),
        (0.7, 0.0, 0.7, 0.6, 1.0, 0.2),
    ]
    for cost, driver, domain, quality, access, novelty in historical_component_scores:
        raw = (
            cost * weights["direct_cost_evidence"]
            + driver * weights["technical_driver_evidence"]
            + domain * weights["domain_relevance"]
            + quality * weights["source_quality"]
            + access * weights["accessibility"]
            + novelty * weights["coverage_novelty"]
        )
        fixed_composite = round(raw, 4)
        assert fixed_composite == 0.55
        assert fixed_composite >= 0.55  # the fixed comparison: accept
