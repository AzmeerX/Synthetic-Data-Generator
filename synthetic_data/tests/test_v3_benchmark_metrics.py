"""Metric primitives (Phase I) — expectations computed by hand, not by the code."""
from __future__ import annotations

import pytest

from synthetic_data.benchmark import metrics as M


def test_precision_recall_f1_from_counts():
    tally = M.Tally(tp=3, fp=1, fn=1)
    assert tally.precision == pytest.approx(0.75)
    assert tally.recall == pytest.approx(0.75)
    assert tally.f1 == pytest.approx(0.75)


def test_an_empty_denominator_is_not_a_perfect_score():
    tally = M.Tally()
    assert tally.precision is None
    assert tally.recall is None
    assert tally.f1 is None
    assert tally.as_dict()["precision"] is None


def test_a_missing_everything_tally_has_zero_recall():
    tally = M.Tally(fn=4)
    assert tally.precision is None
    assert tally.recall == 0.0
    assert tally.f1 is None


def test_tallies_merge():
    left, right = M.Tally(tp=1, fp=1), M.Tally(tp=2, fn=1)
    left.merge(right)
    assert (left.tp, left.fp, left.fn) == (3, 1, 1)


def test_normalisation_ignores_case_space_and_edge_punctuation():
    assert M.normalise("  Dungate Industries GmbH. ") == "dungate industries gmbh"
    assert M.normalized_match("Dungate  Industries GmbH", "dungate industries gmbh.")


def test_exact_match_is_stricter_than_normalised_match():
    assert not M.exact_match("Dungate  Industries", "Dungate Industries")
    assert M.normalized_match("Dungate  Industries", "Dungate Industries")


def test_token_f1_counts_partial_overlap():
    assert M.token_f1("the facility was approved", "the facility was approved") == 1.0
    # overlap 2, precision 1.0, recall 0.5
    assert M.token_f1("facility approved", "the facility was approved") == pytest.approx(
        2 / 3)
    assert M.token_f1("nothing", "the facility") == 0.0


def test_money_parsing_keeps_the_currency():
    assert M.parse_money("GBP 12,000,000.00") == (1200000000, "GBP")
    assert M.parse_money("342900173.99") == (34290017399, None)
    assert M.parse_money("twelve million") is None


def test_money_comparison_distinguishes_a_currency_error():
    equal, reason = M.money_equal(1200000000, "GBP", 1200000000, "GBP")
    assert equal and reason is None
    equal, reason = M.money_equal(1200000000, "USD", 1200000000, "GBP")
    assert not equal and reason == "currency"
    equal, reason = M.money_equal(1200000001, "GBP", 1200000000, "GBP")
    assert not equal and reason == "amount"


def test_percent_comparison_is_tolerance_based():
    assert M.percent_equal("40.00%", "40%")
    assert M.percent_equal("100.00%", "100.0 %")
    assert not M.percent_equal("40.01%", "40.00%")


def test_dates_are_parsed_in_the_formats_the_corpus_uses():
    assert M.date_equal("2023-01-23", "2023-01-23")
    assert M.date_equal("23 January 2023", "2023-01-23")
    assert not M.date_equal("2022-01-23", "2023-01-23")


def test_derived_reconciliation_needs_the_inputs_to_add_up():
    result = M.reconcile_derived([100, 200, 300], total=600, predicted_total=600)
    assert result["ok"] is True
    result = M.reconcile_derived([100, 200, 300], total=700, predicted_total=700)
    assert result["agrees_with_inputs"] is False
    assert result["ok"] is False


def test_mean_ignores_not_applicable_metrics():
    assert M.mean([1.0, None, 0.0]) == pytest.approx(0.5)
    assert M.mean([None, None]) is None
