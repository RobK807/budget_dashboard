"""Parsing a pasted or uploaded table into candidates."""

import datetime as dt
from decimal import Decimal

import pandas as pd

from budget import importer

WORKBOOK_HEADERS = [
    "Item", "Month", "Amount", "Type", "Account From", "Account To",
    "Date", "Comment", "Category", "Category comment", "Purchase type",
]


def test_accepts_the_old_bulkimport_layout_verbatim():
    """Pasting straight out of the workbook has to work, including its Item and Month
    columns -- Month is ignored because the period comes from the date."""
    df = pd.DataFrame(
        [[1, "June", 12.5, "Debit", "HSBC", None, "15/06/2026", "Lunch", "Food", None, "Food"]],
        columns=WORKBOOK_HEADERS,
    )
    candidates, problems = importer.parse(df)

    assert problems == []
    assert len(candidates) == 1
    c = candidates[0]
    assert c.txn_date == dt.date(2026, 6, 15)
    assert c.amount == Decimal("12.5")
    assert c.account_from == "HSBC"
    assert c.classification == "Food"


def test_dates_are_read_day_first():
    df = pd.DataFrame(
        {"Date": ["03/08/2026"], "Type": ["Debit"], "Amount": [1], "Account From": ["HSBC"]}
    )
    candidates, _ = importer.parse(df)
    assert candidates[0].txn_date == dt.date(2026, 8, 3)


def test_zero_is_treated_as_empty_in_text_fields():
    # The workbook writes 0 rather than blank into unused optional fields.
    df = pd.DataFrame(
        {
            "Date": ["15/06/2026"], "Type": ["Debit"], "Amount": [1],
            "Account From": ["HSBC"], "Account To": [0], "Comment": [0],
        }
    )
    candidates, _ = importer.parse(df)
    assert candidates[0].account_to is None
    assert candidates[0].comment is None


def test_headings_are_matched_loosely():
    df = pd.DataFrame(
        {"date": ["15/06/2026"], "TYPE": ["debit"], "value": [5], "from": ["HSBC"]}
    )
    candidates, problems = importer.parse(df)
    assert problems == []
    assert candidates[0].type == "Debit"
    assert candidates[0].amount == Decimal("5")


def test_missing_required_columns_are_reported():
    df = pd.DataFrame({"Date": ["15/06/2026"], "Comment": ["x"]})
    candidates, problems = importer.parse(df)
    assert candidates == []
    assert any("No column found" in p for p in problems)


def test_unrecognised_columns_warn_but_do_not_block():
    df = pd.DataFrame(
        {
            "Date": ["15/06/2026"], "Type": ["Debit"], "Amount": [1],
            "Account From": ["HSBC"], "Sparkle": ["?"],
        }
    )
    candidates, problems = importer.parse(df)
    assert len(candidates) == 1
    assert any("Sparkle" in p for p in problems)


def test_blank_rows_are_skipped():
    df = pd.DataFrame(
        {
            "Date": ["15/06/2026", None], "Type": ["Debit", None],
            "Amount": [1, None], "Account From": ["HSBC", None],
        }
    )
    candidates, _ = importer.parse(df)
    assert len(candidates) == 1


def test_amounts_tolerate_currency_formatting():
    df = pd.DataFrame(
        {
            "Date": ["15/06/2026"], "Type": ["Debit"],
            "Amount": ["£1,234.56"], "Account From": ["HSBC"],
        }
    )
    candidates, _ = importer.parse(df)
    assert candidates[0].amount == Decimal("1234.56")


def test_source_rows_are_numbered_from_the_header():
    df = pd.DataFrame(
        {
            "Date": ["15/06/2026", "16/06/2026"], "Type": ["Debit", "Debit"],
            "Amount": [1, 2], "Account From": ["HSBC", "HSBC"],
        }
    )
    candidates, _ = importer.parse(df)
    assert [c.source_row for c in candidates] == [2, 3]
