"""Parsing a pasted or uploaded table into candidates."""

import datetime as dt
import io
import warnings
from decimal import Decimal

import pandas as pd
import pytest

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


class TestDateOrder:
    """Two conventions arrive in this column and need opposite treatment.

    `03/08/2026` is ambiguous and, this being a UK budget, means 3 August. `2026-08-01` is
    not ambiguous and means 1 August anywhere. Applying dayfirst to both read the second as
    year-day-month and returned 8 January -- and pandas warns about that only when the day is
    too big to be a month, so `2026-07-31` came back right with a warning while `2026-08-01`
    came back wrong in silence. Every date in the first twelve days of a month was transposed
    and the survivors were the only ones that would have been noticed.
    """

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("2026-08-01", dt.date(2026, 8, 1)),   # the silent failure
            ("2026-07-31", dt.date(2026, 7, 31)),  # the one that warned and worked
            ("2026-1-5", dt.date(2026, 1, 5)),     # unpadded is still ISO
            ("2026-08-01 00:00:00", dt.date(2026, 8, 1)),
            ("01/08/2026", dt.date(2026, 8, 1)),   # UK, ambiguous, day first
            ("31/07/2026", dt.date(2026, 7, 31)),
            ("1/8/2026", dt.date(2026, 8, 1)),
            ("01-08-2026", dt.date(2026, 8, 1)),   # dashes but not ISO: still day first
        ],
    )
    def test_each_form_is_read_as_written(self, text, expected):
        df = pd.DataFrame(
            {"Date": [text], "Type": ["Debit"], "Amount": [1], "Account From": ["HSBC"]}
        )
        candidates, _ = importer.parse(df)
        assert candidates[0].txn_date == expected

    def test_no_pandas_date_warning_is_provoked(self):
        """The warning was the only signal this was going wrong, and it fired on the case
        that still worked. Treating it as an error keeps the quiet path honest."""
        df = pd.DataFrame(
            {
                "Date": ["2026-08-01", "2026-07-31", "01/08/2026"],
                "Type": ["Debit"] * 3, "Amount": [1] * 3, "Account From": ["HSBC"] * 3,
            }
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            importer.parse(df)

    def test_the_conflict_export_survives_its_own_round_trip(self):
        """The path this broke on. sync.local_only_frame emits datetime.date, which to_csv
        renders as ISO, so export -> re-import was the one route guaranteed to hit it -- and
        it is the route that exists to rescue a machine's transactions after a conflict."""
        exported = pd.DataFrame(
            {
                "Date": [dt.date(2026, 8, 1), dt.date(2026, 7, 31), dt.date(2026, 12, 3)],
                "Type": ["Debit"] * 3,
                "Amount": [10.0, 20.0, 30.0],
                "Account From": ["HSBC"] * 3,
                "Category": ["Food"] * 3,
                "Purchase type": ["Food"] * 3,
            }
        )
        round_tripped = pd.read_csv(io.StringIO(exported.to_csv(index=False)))
        candidates, problems = importer.parse(round_tripped)

        assert problems == []
        assert [c.txn_date for c in candidates] == list(exported["Date"])


class TestBulkBlankRows:
    """Adding a known number of rows at once. The editor's own + button adds one per click,
    which is fine for three and tedious for forty."""

    def test_rows_are_appended(self):
        grown = importer.with_blank_rows(importer.template(), 5)
        assert len(grown) == 5

    def test_dtypes_survive(self):
        """Concatenating an all-NA frame widens every column to object, which costs the date
        picker and the numeric field in the editor and turns them into free text."""
        template = importer.template()
        grown = importer.with_blank_rows(template, 3)
        assert dict(grown.dtypes) == dict(template.dtypes)

    def test_existing_content_is_kept(self):
        started = importer.with_blank_rows(importer.template(), 2)
        started.loc[0, "Amount"] = 12.5
        grown = importer.with_blank_rows(started, 3)

        assert len(grown) == 5
        assert grown.loc[0, "Amount"] == 12.5
        assert dict(grown.dtypes) == dict(importer.template().dtypes)

    def test_asking_for_none_changes_nothing(self):
        started = importer.with_blank_rows(importer.template(), 2)
        assert len(importer.with_blank_rows(started, 0)) == 2


class TestCategoryCommentDefault:
    """'Other' says nothing about what a payment was, so the description would otherwise be
    typed twice. Every other category already describes it."""

    def frame(self, category, comment, category_comment=None):
        return pd.DataFrame(
            {
                "Date": ["2026-08-01"], "Type": ["Debit"], "Amount": [1],
                "Account From": ["HSBC"], "Category": [category],
                "Comment": [comment], "Category comment": [category_comment],
            }
        )

    def test_other_inherits_the_comment(self):
        candidates, _ = importer.parse(self.frame("Other", "Vet bill"))
        assert candidates[0].category_comment == "Vet bill"

    def test_a_named_category_does_not(self):
        candidates, _ = importer.parse(self.frame("Food", "Lunch"))
        assert candidates[0].category_comment is None

    def test_anything_entered_wins(self):
        """A default, not an override -- otherwise it could not be corrected on the row."""
        candidates, _ = importer.parse(self.frame("Other", "Gift", "Birthday"))
        assert candidates[0].category_comment == "Birthday"

    def test_case_and_spacing_do_not_matter(self):
        candidates, _ = importer.parse(self.frame("  other ", "Vet bill"))
        assert candidates[0].category_comment == "Vet bill"

    def test_no_comment_to_inherit_leaves_it_blank(self):
        candidates, _ = importer.parse(self.frame("Other", None))
        assert candidates[0].category_comment is None
