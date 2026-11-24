"""Phase F — financial correctness, verified on the rendered workbook and on the
parsed text.

The checks are deliberately arithmetic, not "the cell looks plausible": ownership must
sum to 100 %, the principal must equal the disbursements, the outstanding balance must
be principal minus repayments, and interest must equal principal × rate × days/365.
Every one is recomputed here from the *typed* cell values, then again from what the
parser extracted.
"""
from __future__ import annotations

import datetime as dt

import pytest
from openpyxl import load_workbook

from synthetic_data.tests.v3_xlsx import (feature_ingested, feature_rendered,
                                          pilot_workbook, workbook_feature, xlsx_config,
                                          xlsx_ingested, xlsx_rendered, xlsx_store)


def values(rendered):
    return load_workbook(rendered.path, data_only=True)


# -- ownership --------------------------------------------------------------
def test_ownership_percentages_reconcile_to_one_hundred(feature_rendered):
    sheet = values(feature_rendered)["Shareholder register"]
    holdings = [sheet[f"D{row}"].value for row in range(4, 8)]
    assert all(isinstance(value, float) for value in holdings)
    assert sum(holdings) == pytest.approx(1.0)
    assert sheet["D8"].value == pytest.approx(1.0)          # the SUM's cached result
    assert sheet["C8"].value == 6_250_000                    # shares total


def test_pilot_register_reconciles_to_one_hundred(xlsx_rendered):
    workbook = values(xlsx_rendered)
    holdings = [workbook["Share register"][f"E{row}"].value for row in range(4, 8)]
    assert sum(holdings) == pytest.approx(1.0)
    assert workbook["Share register"]["E8"].value == pytest.approx(1.0)


# -- principal and disbursements -------------------------------------------
def test_principal_equals_the_tranche_disbursements(feature_rendered):
    workbook = values(feature_rendered)
    ledger = workbook["Transaction ledger"]
    drawdowns = [ledger[f"D{row}"].value for row in range(4, 6)]
    assert sum(drawdowns) == pytest.approx(4_000_000)
    summary = workbook["Financial summary"]
    assert summary["B4"].value == pytest.approx(4_000_000)   # cross-sheet formula
    recon = workbook["Reconciliation"]
    assert recon["B4"].value == pytest.approx(4_000_000)     # cross-sheet formula
    assert recon["C4"].value == pytest.approx(4_000_000)


def test_pilot_principal_equals_its_drawdowns(xlsx_rendered):
    workbook = values(xlsx_rendered)
    ledger = workbook["Transaction ledger"]
    draw = 0
    for row in range(4, ledger.max_row + 1):
        kind = ledger[f"D{row}"].value
        amount = ledger[f"E{row}"].value
        if kind == "Drawdown" and isinstance(amount, (int, float)):
            draw += amount
    assert draw == pytest.approx(30_000_000)


# -- repayments and outstanding --------------------------------------------
def test_repayment_totals_reconcile(feature_rendered):
    workbook = values(feature_rendered)
    ledger = workbook["Transaction ledger"]
    repayments = [ledger[f"D{row}"].value for row in range(4, 7)
                  if isinstance(ledger[f"D{row}"].value, (int, float))
                  and ledger[f"D{row}"].value < 0]
    assert repayments == [pytest.approx(-1_000_000)]
    assert ledger["D7"].value == pytest.approx(1_000_000)    # SUM cached result


def test_outstanding_balance_is_principal_minus_repayments(feature_rendered):
    workbook = values(feature_rendered)
    ledger = workbook["Transaction ledger"]
    running = [ledger[f"E{row}"].value for row in range(4, 7)]
    assert running == [pytest.approx(2_500_000), pytest.approx(4_000_000),
                       pytest.approx(3_000_000)]
    summary = workbook["Financial summary"]
    assert summary["B6"].value == pytest.approx(4_000_000 - 1_000_000)
    assert summary["B6"].value == pytest.approx(running[-1])


def test_the_running_balance_chain_closes(feature_rendered):
    workbook = values(feature_rendered)
    ledger = workbook["Transaction ledger"]
    balance = 0.0
    for row in range(4, 7):
        amount = ledger[f"D{row}"].value
        balance += amount
        assert ledger[f"E{row}"].value == pytest.approx(balance)


def test_the_pilot_ledger_balances_to_zero(xlsx_rendered):
    """Four drawdowns of 30,000,000 and eight repayments that clear it completely."""
    workbook = values(xlsx_rendered)
    ledger = workbook["Transaction ledger"]
    kinds = [ledger[f"D{row}"].value for row in range(4, 16)]
    amounts = [ledger[f"E{row}"].value for row in range(4, 16)]
    running = [ledger[f"F{row}"].value for row in range(4, 16)]
    assert kinds.count("Drawdown") == 4 and kinds.count("Repayment") == 8
    drawdown = sum(amount for kind, amount in zip(kinds, amounts) if kind == "Drawdown")
    repaid = sum(amount for kind, amount in zip(kinds, amounts) if kind == "Repayment")
    assert drawdown == pytest.approx(30_000_000)
    assert repaid == pytest.approx(30_000_000)
    balance = 0.0
    for kind, amount, statement in zip(kinds, amounts, running):
        # the amount is stored positive; its direction lives in the Type column
        balance += amount if kind == "Drawdown" else -amount
        assert statement == pytest.approx(balance)
    assert balance == pytest.approx(0.0)
    assert ledger["E16"].value == pytest.approx(30_000_000)      # total repaid


# -- interest ---------------------------------------------------------------
def test_interest_is_computed_correctly(feature_rendered):
    expected = round(4_000_000 * 0.065 * 90 / 365, 2)
    workbook = values(feature_rendered)
    recon = workbook["Reconciliation"]
    assert recon["B5"].value == pytest.approx(expected)
    assert recon["C5"].value == pytest.approx(expected)
    assert recon["D5"].value == pytest.approx(0.0)           # difference is zero


def test_the_interest_formula_is_the_one_that_was_written(feature_rendered):
    formulas = load_workbook(feature_rendered.path)["Reconciliation"]
    assert formulas["B5"].value == "=B4*0.065*90/365"


# -- dates and references ---------------------------------------------------
def test_dates_and_references_agree_across_records(feature_rendered):
    workbook = values(feature_rendered)
    ledger = workbook["Transaction ledger"]
    dates = [ledger[f"A{row}"].value for row in range(4, 7)]
    assert [date.date() for date in dates] == [dt.date(2024, 1, 15),
                                               dt.date(2024, 2, 15),
                                               dt.date(2024, 4, 15)]
    references = [ledger[f"B{row}"].value for row in range(4, 7)]
    assert references == ["XW-01", "XW-02", "XW-03"]
    assert len(set(references)) == len(references)
    # the reconciliation restates the same principal, not a different one
    assert workbook["Reconciliation"]["B4"].value == pytest.approx(sum(
        [ledger["D4"].value, ledger["D5"].value]))


# -- the same checks on what the parser saw --------------------------------
def _numbers(row: str) -> list[float]:
    """Every numeric cell in a parsed row, whatever the empty-cell spacing."""
    import re
    return [float(token) for token in (part.strip() for part in row.split("|"))
            if re.fullmatch(r"-?\d+(\.\d+)?", token)]


def _rows(ingested, page: int | None = None):
    return [" ".join(ingested.view.sentence_text(row).split())
            for row in ingested.view.sentences
            if page is None or row["page"] == page]


def test_the_parsed_workbook_reconciles_too(feature_ingested):
    """The parser's page is the sheet: page 2 is the transaction ledger."""
    rows = _rows(feature_ingested, page=2)
    ledger = [row for row in rows if row.startswith("15 ")]
    assert len(ledger) == 3, rows
    amounts = [_numbers(row)[0] for row in ledger]      # [amount, running balance]
    assert sum(amounts) == pytest.approx(4_000_000 - 1_000_000)
    totals = [row for row in rows if row.startswith("Total repaid")]
    assert totals and _numbers(totals[0]) == [pytest.approx(1_000_000)]


def test_parsed_holdings_sum_to_one_hundred(feature_ingested):
    rows = _rows(feature_ingested, page=1)
    holdings = []
    for row in rows:
        cells = row.split(" | ")
        if len(cells) == 4 and cells[3].endswith("%") and cells[0] != "Total":
            holdings.append(float(cells[3].rstrip("%")) / 100)
    assert len(holdings) == 4, rows
    assert sum(holdings) == pytest.approx(1.0)
    assert any(row.startswith("Total |") and "100.00%" in row for row in rows)


def test_parsed_reconciliation_rows_are_zero(feature_ingested):
    rows = [row for row in _rows(feature_ingested, page=5)
            if row.startswith("Interest for the period")]
    assert rows, "the reconciliation sheet was not extracted"
    value, expected, difference = _numbers(rows[0])[:3]
    assert value == pytest.approx(expected)
    assert difference == pytest.approx(0.0)
