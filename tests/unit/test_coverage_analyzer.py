"""Offline tests for coverage_analyzer.py, focused on `coverage_novelty_score`
-- the real (no-longer-hardcoded) novelty input `screener.screen()` now
receives, per the production ranking objective's coverage-novelty dimensions.
"""

from discovery.coverage_analyzer import (
    coverage_novelty_score,
    get_or_create_cell,
    record_query_executed,
    update_cell_after_round,
)

_DIMS = {"technology_domain": "enhanced_geothermal_systems", "cost_component": "drilling_and_well_construction"}


def test_coverage_novelty_score_is_maximal_for_a_never_queried_cell(test_db):
    assert coverage_novelty_score(test_db, _DIMS) == 1.0


def test_coverage_novelty_score_decays_as_a_cell_is_queried_more(test_db):
    cell = get_or_create_cell(test_db, _DIMS)
    record_query_executed(test_db, cell)
    first = coverage_novelty_score(test_db, _DIMS)

    record_query_executed(test_db, cell)
    record_query_executed(test_db, cell)
    second = coverage_novelty_score(test_db, _DIMS)

    assert first > second
    assert 0.2 <= second < first <= 1.0


def test_coverage_novelty_score_is_low_for_a_saturated_cell(test_db):
    cell = get_or_create_cell(test_db, _DIMS)
    record_query_executed(test_db, cell)
    update_cell_after_round(test_db, cell, accepted_this_round=0, zero_yield_rounds_threshold=1)

    assert cell.saturation_status == "saturated"
    assert coverage_novelty_score(test_db, _DIMS) == 0.1


def test_coverage_novelty_score_treats_different_dimensions_as_different_cells(test_db):
    cell = get_or_create_cell(test_db, _DIMS)
    record_query_executed(test_db, cell)
    update_cell_after_round(test_db, cell, accepted_this_round=0, zero_yield_rounds_threshold=1)

    other_dims = {"technology_domain": "oil_and_gas_well_construction"}
    assert coverage_novelty_score(test_db, other_dims) == 1.0  # untouched, still novel
