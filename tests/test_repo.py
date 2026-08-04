"""Aggregation rules that the reconciliation gate covers end-to-end, pinned here as small
examples so a failure says *which* rule broke rather than 'April is out by £3.40'.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import repo


def posting(**kw):
    base = {
        "txn_id": 1, "date": pd.Timestamp("2026-04-05"), "period": "2026-04",
        "account": "HSBC", "account_type": "bank", "column": "debit",
        "amount": Decimal("10.00"), "signed": Decimal("-10.00"), "type": "Debit",
        "category": "Food", "classification": "Food", "direction": 1,
        "comment": None, "deleted": False,
    }
    return {**base, **kw}


def frame(*rows):
    return pd.DataFrame(list(rows))


class TestCategoryActuals:
    """Income and spend are accumulated separately by New_entry -- a credit goes to column
    C and a debit to column E -- so a category can carry both without them netting."""

    def test_debits_and_credits_are_not_netted(self):
        df = repo.category_actuals(
            frame(
                posting(txn_id=1, type="Debit", amount=Decimal("100")),
                posting(txn_id=2, type="Credit", amount=Decimal("30")),
            ),
            "2026-04",
        ).set_index("category")
        assert df.loc["Food", "spent"] == Decimal("100")
        assert df.loc["Food", "income"] == Decimal("30")

    def test_transfers_are_excluded(self):
        df = repo.category_actuals(
            frame(posting(type="Transfer", category=None)), "2026-04"
        )
        assert df.empty

    def test_other_periods_are_excluded(self):
        df = repo.category_actuals(frame(posting(period="2026-05")), "2026-04")
        assert df.empty


class TestDailyClassification:
    """Workbook rule: direction * (debits - credits), transfers excluded."""

    def test_debit_is_positive_for_a_positive_direction(self):
        df = repo.daily_classification(frame(posting(type="Debit")), "2026-04")
        assert df.iloc[0]["total"] == Decimal("10.00")

    def test_credit_is_negative_for_a_positive_direction(self):
        df = repo.daily_classification(frame(posting(type="Credit")), "2026-04")
        assert df.iloc[0]["total"] == Decimal("-10.00")

    def test_negative_direction_inverts(self):
        # Excess is the only classification with direction -1, and missing it inverted
        # every Excess figure on the first reconciliation run.
        df = repo.daily_classification(
            frame(posting(classification="Excess", direction=-1, type="Debit")), "2026-04"
        )
        assert df.iloc[0]["total"] == Decimal("-10.00")

    def test_transfers_carry_no_classification_and_are_excluded(self):
        df = repo.daily_classification(
            frame(posting(type="Transfer", classification=None)), "2026-04"
        )
        assert df.empty


class TestAccountBalances:
    ACCOUNTS = frame(
        {"name": "HSBC", "type": "bank", "is_savings": False,
         "is_investment": False, "is_isa": False}
    )
    OPENINGS = frame({"account": "HSBC", "period": "2026-04", "opening": Decimal("500")})

    def test_closing_is_opening_plus_signed_movement(self):
        postings = frame(
            posting(column="debit", signed=Decimal("-10")),
            posting(txn_id=2, column="credit", signed=Decimal("25"), amount=Decimal("25"),
                    type="Credit"),
        )
        out = repo.account_balances(postings, self.OPENINGS, "2026-04", self.ACCOUNTS).iloc[0]
        assert out["paid_in"] == Decimal("25")
        assert out["paid_out"] == Decimal("10")
        assert out["closing"] == Decimal("515")

    def test_transfers_are_split_out_of_paid_in_and_paid_out(self):
        """The workbook summed the whole Credit and Debit columns, so moving money between
        your own accounts inflated both sides."""
        postings = frame(
            posting(type="Debit", column="debit", amount=Decimal("10"),
                    signed=Decimal("-10")),
            posting(txn_id=2, type="Transfer", column="debit", amount=Decimal("200"),
                    signed=Decimal("-200"), category=None, classification=None),
            posting(txn_id=3, type="Transfer", column="credit", amount=Decimal("50"),
                    signed=Decimal("50"), category=None, classification=None),
        )
        out = repo.account_balances(postings, self.OPENINGS, "2026-04", self.ACCOUNTS).iloc[0]

        assert out["paid_out"] == Decimal("10")
        assert out["transfer_out"] == Decimal("200")
        assert out["transfer_in"] == Decimal("50")
        # The workbook's combined definition is retained for comparison.
        assert out["total_out"] == Decimal("210")
        assert out["total_in"] == Decimal("50")
        # Splitting must not disturb the balance itself.
        assert out["closing"] == Decimal("340")


class TestSavingsPosition:
    def test_flags_drive_the_headline_totals(self):
        balances = frame(
            {"account": "Current", "type": "bank", "closing": Decimal("100"),
             "is_savings": False, "is_investment": False, "is_isa": False},
            {"account": "Pot", "type": "bank", "closing": Decimal("900"),
             "is_savings": True, "is_investment": False, "is_isa": False},
            {"account": "Amex", "type": "credit_card", "closing": Decimal("250"),
             "is_savings": False, "is_investment": False, "is_isa": False},
        )
        position = repo.savings_position(balances)
        assert position["current"] == Decimal("100")
        assert position["savings"] == Decimal("900")
        assert position["cards"] == Decimal("250")


class TestFiscalPeriods:
    def test_runs_april_to_march_across_the_year_boundary(self):
        periods = repo.fiscal_periods(2026)
        assert periods[0] == "2026-04"
        assert periods[-1] == "2027-03"
        assert len(periods) == 12

    @pytest.mark.parametrize(
        "period,label", [("2026-04", "April 2026"), ("2027-03", "March 2027")]
    )
    def test_labels(self, period, label):
        assert repo.period_label(period) == label


class TestAlphabetical:
    def test_is_case_insensitive(self):
        from budget import ui

        # A plain sorted() gives HSBC, Halifax, ISA, Investments -- ASCII order.
        assert ui.alphabetical(["HSBC", "Halifax", "ISA", "Investments", "NS&I", "Nationwide"]) == [
            "Halifax", "HSBC", "Investments", "ISA", "Nationwide", "NS&I"
        ]

    def test_drops_nulls_and_deduplicates(self):
        from budget import ui

        assert ui.alphabetical(["b", None, "a", "b"]) == ["a", "b"]


class TestSortHuman:
    """Project convention: names order case-insensitively everywhere. pandas' sort_values
    is ASCII like everything else, so it needs the same treatment as SQL and sorted()."""

    NAMES = pd.DataFrame(
        {
            "account": ["HSBC", "Halifax", "ISA", "Investments", "NS&I", "Nationwide"],
            "affected": [False, True, False, True, False, True],
        }
    )

    def test_orders_text_case_insensitively(self):
        out = repo.sort_human(self.NAMES, by="account")
        assert list(out["account"]) == [
            "Halifax", "HSBC", "Investments", "ISA", "Nationwide", "NS&I"
        ]

    def test_mixed_bool_and_text_keys(self):
        # The balance check sorts affected accounts first, then alphabetically.
        out = repo.sort_human(self.NAMES, by=["affected", "account"], ascending=[False, True])
        assert list(out["account"]) == [
            "Halifax", "Investments", "Nationwide", "HSBC", "ISA", "NS&I"
        ]

    def test_numeric_columns_are_untouched(self):
        df = pd.DataFrame({"amount": [3, 1, 2]})
        assert list(repo.sort_human(df, by="amount")["amount"]) == [1, 2, 3]


class TestPeriodsToDate:
    YEAR = repo.fiscal_periods(2026)

    def test_stops_after_the_current_month(self):
        trimmed = repo.periods_to_date(self.YEAR, dt.date(2026, 8, 3))
        assert trimmed[-1] == "2026-08"
        assert "2026-09" not in trimmed

    def test_current_month_is_included_because_it_is_in_progress(self):
        assert "2026-08" in repo.periods_to_date(self.YEAR, dt.date(2026, 8, 1))
        assert "2026-08" in repo.periods_to_date(self.YEAR, dt.date(2026, 8, 31))

    def test_handles_the_rollover_into_the_next_calendar_year(self):
        # Fiscal months Jan-Mar carry a later calendar year, so a naive sort would drop them.
        trimmed = repo.periods_to_date(self.YEAR, dt.date(2027, 2, 15))
        assert trimmed[-1] == "2027-02"
        assert "2026-12" in trimmed
        assert "2027-03" not in trimmed

    def test_a_finished_year_keeps_every_month(self):
        assert repo.periods_to_date(self.YEAR, dt.date(2028, 1, 1)) == self.YEAR

    def test_a_year_that_has_not_begun_falls_back_to_the_full_list(self):
        # Better to show a full empty year than a blank page with no months to select.
        assert repo.periods_to_date(self.YEAR, dt.date(2020, 1, 1)) == self.YEAR
