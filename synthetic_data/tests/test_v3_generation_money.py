"""Phase C closure — monetary representation.

Three presentation styles exist on purpose (§10.2 realisation), but a presentation
is never a different value:

* ``Money`` stays typed — nothing re-parses a display string into an amount;
* one style is chosen per document (deterministic, from the corpus key), so a
  reader never sees two styles in one instrument, while the corpus still varies;
* ``money_key`` / ``money_surface_key`` normalise presentation to ``CCY:minor``, and
  every comparison (`surfaces_equivalent`, alignment grounding) goes through them.
"""
from __future__ import annotations

import pytest

from synthetic_data.generation import CorpusConfig
from synthetic_data.generation.surface import fmt_money
from synthetic_data.model import Money
from synthetic_data.surfaces import (money_key, money_surface_key, money_surfaces,
                                     surfaces_equivalent)
from synthetic_data.tests.v3_generation import PILOT, pilot_plan


def test_money_stays_typed_and_formatting_is_pure():
    money = Money(amount_minor=400_000_000, currency="GBP")
    assert money.amount_minor == 400_000_000
    assert money.currency == "GBP"
    assert money.display() == "GBP 4,000,000.00"
    assert fmt_money(money, 1) == money.display()
    assert fmt_money(money, 0) == "£4,000,000"
    assert fmt_money(money, 2) == "4,000,000.00 pounds sterling"


def test_all_styles_denote_one_value():
    money = Money(amount_minor=400_000_000, currency="GBP")
    keys = {money_surface_key(fmt_money(money, style)) for style in range(3)}
    assert keys == {money_key(money)}


def test_a_formatting_variation_is_not_a_different_amount():
    assert surfaces_equivalent("£4,000,000", "GBP 4,000,000.00")
    assert surfaces_equivalent("£4,000,000", "4,000,000.00 pounds sterling")
    assert not surfaces_equivalent("£4,000,000", "£4,500,000")
    assert not surfaces_equivalent("GBP 4,000,000.00", "GBP 4,000,000.01")


def test_currency_is_part_of_the_identity():
    pounds = Money(amount_minor=400_000_000, currency="GBP")
    dollars = Money(amount_minor=400_000_000, currency="USD")
    assert money_key(pounds) != money_key(dollars)
    assert not surfaces_equivalent("£4,000,000", "$4,000,000")
    assert surfaces_equivalent("$4,000,000", "USD 4,000,000.00")
    assert surfaces_equivalent("$4,000,000", "4,000,000.00 US dollars")


def test_decimal_amounts_are_preserved_exactly():
    assert money_surface_key("£0.99") == "GBP:99"
    assert money_surface_key("GBP 1,234.56") == "GBP:123456"
    assert money_surface_key("1,234.56 pounds sterling") == "GBP:123456"
    assert money_surface_key("£0.99") != money_surface_key("£0.90")
    assert fmt_money(Money(amount_minor=99, currency="USD"), 0) == "$0.99"


def test_non_money_text_is_never_coerced_into_an_amount():
    assert money_surface_key("4,000,000") is None
    assert money_surface_key("the facility") is None
    assert not surfaces_equivalent("£4,000,000", "4,000,000")


def test_money_surfaces_lists_every_amount_in_text():
    text = "The Lender lent £30,000,000 and was repaid 750,000.00 pounds sterling."
    assert money_surfaces(text) == ["£30,000,000", "750,000.00 pounds sterling"]


def test_one_style_per_document(pilot=None):
    plan = pilot_plan()
    shapes = {
        0: lambda text: text.startswith(("£", "$", "€", "¥")),
        1: lambda text: text[:1].isalpha() and text[:3] in {"GBP", "USD", "EUR"},
        2: lambda text: "pounds sterling" in text or "US dollars" in text
        or "euros" in text,
    }
    observed = 0
    for doc_id in plan.prose_ids:
        surfaces = [slot for statement in plan.documents[doc_id].statements
                    for key, slot in statement.value_slots.items() if key == "amount"]
        if not surfaces:
            continue
        observed += 1
        matching = {style for style in shapes
                    if all(shapes[style](slot) for slot in surfaces)}
        assert len(matching) == 1, (doc_id, surfaces[:3])
    assert observed >= 5


def test_money_styles_vary_across_the_corpus():
    plan = pilot_plan()
    styles = set()
    for doc_id in plan.prose_ids:
        for statement in plan.documents[doc_id].statements:
            slot = statement.value_slots.get("amount")
            if slot:
                styles.add(money_surface_key(slot).split(":")[0] + ":" +
                           ("sym" if slot[0] in "£$€¥" else
                            "name" if slot[0].isdigit() else "code"))
                break
    assert len(styles) >= 2


def test_table_money_cells_use_the_normalised_display():
    plan = pilot_plan()
    checked = 0
    for doc_id in plan.prose_ids:
        for section in plan.documents[doc_id].sections:
            for block in section.blocks:
                if block.table is None:
                    continue
                for row in [*block.table.header_rows, *block.table.body_rows,
                            *block.table.total_rows]:
                    for cell in row.cells:
                        if isinstance(cell.value, Money):
                            checked += 1
                            assert cell.display == cell.value.display()
                            assert money_surface_key(cell.display) == money_key(cell.value)
    assert checked > 5


def test_value_slots_only_record_values_that_were_rendered():
    plan = pilot_plan()
    for doc_id in plan.prose_ids:
        document = plan.documents[doc_id]
        for statement in document.statements:
            for slot, value in statement.value_slots.items():
                assert value in statement.surface, (doc_id, slot, value)


def test_grounding_accepts_an_equivalent_style_in_the_quote():
    from synthetic_data.alignment import surface_in_quote

    assert surface_in_quote("£4,000,000", "The Lender lent GBP 4,000,000.00 on 3 March 2024.")
    assert surface_in_quote("4,000,000.00 pounds sterling", "The Lender lent £4,000,000 today.")
    assert not surface_in_quote("£4,500,000", "The Lender lent £4,000,000 today.")
    assert surface_in_quote("23 January 2023", "advanced on 2023-01-23 under the facility")


def test_date_and_percentage_variations_compare_by_value():
    assert surfaces_equivalent("23 January 2023", "2023-01-23")
    assert surfaces_equivalent("23rd January 2023", "23 Jan 2023")
    assert surfaces_equivalent("January 23, 2023", "23 January 2023")
    assert not surfaces_equivalent("23 January 2023", "24 January 2023")
    assert surfaces_equivalent("40.00%", "40.00 per cent")
    assert surfaces_equivalent("40.00%", "4000 basis points")
    assert not surfaces_equivalent("40.00%", "41.00%")


def test_generation_with_money_is_deterministic():
    from synthetic_data.generation import build_corpus

    first = build_corpus(CorpusConfig(**PILOT))
    second = build_corpus(CorpusConfig(**PILOT))
    for doc_id in first.prose_ids:
        assert first.documents[doc_id].model_dump_json() == \
            second.documents[doc_id].model_dump_json()
