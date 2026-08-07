"""The rollover engine (DESIGN.md 6a).

The workbook welded two rules into one: the branch it called "Negative" carried *everything*
and applied retention to a surplus, despite `MIN(0, ...)` never appearing in it. Splitting
them means `rollover` says which balances carry and `excess_retention` says how much of a
credit balance survives. These tests pin that separation, because collapsing it back is the
easy mistake.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import repo

HALF = Decimal("0.5")


class TestCarriedForward:
    @pytest.mark.parametrize("closing", [Decimal("500"), Decimal("-500"), Decimal("0")])
    def test_none_carries_nothing(self, closing):
        assert repo.carried_forward(closing, "none") == Decimal("0")

    def test_all_carries_a_debit_balance_in_full(self):
        assert repo.carried_forward(Decimal("500"), "all") == Decimal("500")

    def test_all_carries_a_credit_balance_in_full_at_100_percent(self):
        assert repo.carried_forward(Decimal("-500"), "all") == Decimal("-500")

    def test_retention_applies_to_a_credit_balance(self):
        assert repo.carried_forward(Decimal("-500"), "all", HALF) == Decimal("-250")

    def test_retention_does_not_touch_a_debit_balance(self):
        """An overspend always carries in full: retention is about not banking a surplus
        twice, not about forgiving debt."""
        assert repo.carried_forward(Decimal("500"), "all", HALF) == Decimal("500")

    def test_debit_keeps_only_debit_balances(self):
        assert repo.carried_forward(Decimal("500"), "debit") == Decimal("500")
        assert repo.carried_forward(Decimal("-500"), "debit") == Decimal("0")

    def test_credit_keeps_only_credit_balances(self):
        assert repo.carried_forward(Decimal("-500"), "credit") == Decimal("-500")
        assert repo.carried_forward(Decimal("500"), "credit") == Decimal("0")

    def test_credit_applies_retention(self):
        assert repo.carried_forward(Decimal("-500"), "credit", HALF) == Decimal("-250")


CLASSES = pd.DataFrame(
    [
        {"name": "Bills", "direction": 1, "rollover": "none"},
        {"name": "Excess", "direction": -1, "rollover": "all"},
    ]
)
EMPTY_POSTINGS = pd.DataFrame(
    columns=["txn_id", "date", "period", "account", "account_type", "column", "amount",
             "signed", "type", "category", "classification", "direction", "comment",
             "deleted"]
)
NO_PROJECTIONS = pd.DataFrame(columns=["date", "classification", "amount", "comment"])
NO_ALLOWANCE = pd.DataFrame(columns=["period", "classification", "daily_amount"])


def allowance(period, name, amount):
    return pd.DataFrame(
        [{"period": period, "classification": name, "daily_amount": Decimal(amount)}]
    )


class TestDailyAllowance:
    """Month-tab 'Spend per day' -- Running Excess gains it every day of the month."""

    def test_allowance_accumulates_daily(self):
        frame = repo.running_classification(
            EMPTY_POSTINGS, NO_PROJECTIONS, allowance("2026-08", "Excess", "30"),
            CLASSES, "2026-08", today=dt.date(2026, 8, 31),
        )
        excess = frame[frame["classification"] == "Excess"].sort_values("date")
        assert excess.iloc[0]["running"] == Decimal("30")   # day 1
        assert excess.iloc[30]["running"] == Decimal("930")  # 31 days x 30

    def test_a_classification_without_an_allowance_is_unaffected(self):
        frame = repo.running_classification(
            EMPTY_POSTINGS, NO_PROJECTIONS, allowance("2026-08", "Excess", "30"),
            CLASSES, "2026-08", today=dt.date(2026, 8, 31),
        )
        bills = frame[frame["classification"] == "Bills"]
        assert set(bills["running"]) == {Decimal("0")}


class TestProjections:
    """Days after today take their value from the projection table, not from actuals."""

    def test_future_days_use_projections(self):
        projections = pd.DataFrame(
            [
                {"date": pd.Timestamp("2026-08-20"), "classification": "Bills",
                 "amount": Decimal("100"), "comment": None},
            ]
        )
        frame = repo.running_classification(
            EMPTY_POSTINGS, projections, NO_ALLOWANCE, CLASSES, "2026-08",
            today=dt.date(2026, 8, 10),
        )
        bills = frame[frame["classification"] == "Bills"].sort_values("date")
        assert bills.iloc[-1]["running"] == Decimal("100")

    def test_past_days_ignore_projections(self):
        projections = pd.DataFrame(
            [
                {"date": pd.Timestamp("2026-08-05"), "classification": "Bills",
                 "amount": Decimal("100"), "comment": None},
            ]
        )
        frame = repo.running_classification(
            EMPTY_POSTINGS, projections, NO_ALLOWANCE, CLASSES, "2026-08",
            today=dt.date(2026, 8, 31),
        )
        bills = frame[frame["classification"] == "Bills"]
        assert set(bills["running"]) == {Decimal("0")}


class TestChaining:
    def test_a_month_starts_from_the_previous_closing_when_rollover_is_all(self):
        _, closes = repo.running_by_period(
            EMPTY_POSTINGS, NO_PROJECTIONS, allowance("2026-08", "Excess", "30"),
            CLASSES, ["2026-08", "2026-09"], today=dt.date(2026, 9, 30),
        )
        august = closes[(closes["period"] == "2026-08")
                        & (closes["classification"] == "Excess")].iloc[0]["closing"]
        september = closes[(closes["period"] == "2026-09")
                           & (closes["classification"] == "Excess")].iloc[0]["closing"]
        assert august == Decimal("930")
        # No allowance in September, so it simply carries August forward.
        assert september == Decimal("930")

    def test_rollover_none_restarts_each_month(self):
        classes = pd.DataFrame([{"name": "Bills", "direction": 1, "rollover": "none"}])
        _, closes = repo.running_by_period(
            EMPTY_POSTINGS, NO_PROJECTIONS, allowance("2026-08", "Bills", "10"),
            classes, ["2026-08", "2026-09"], today=dt.date(2026, 9, 30),
        )
        assert closes.iloc[0]["closing"] == Decimal("310")  # 31 x 10
        assert closes.iloc[1]["closing"] == Decimal("0")    # nothing carried

    def test_a_stated_opening_starts_the_year(self):
        """April has no prior month; the workbook typed the brought-forward figure straight
        into the formula (a bare '-2632.45')."""
        openings = pd.DataFrame(
            [{"period": "2026-04", "classification": "Excess", "amount": Decimal("-2632.45")}]
        )
        _, closes = repo.running_by_period(
            EMPTY_POSTINGS, NO_PROJECTIONS, NO_ALLOWANCE, CLASSES, ["2026-04"],
            today=dt.date(2026, 4, 30), openings=openings,
        )
        excess = closes[closes["classification"] == "Excess"].iloc[0]["closing"]
        assert excess == Decimal("-2632.45")

    def test_a_stated_opening_replaces_what_was_carried_forward(self):
        """Backfilling put a real March underneath April 2026. The stated opening is the
        year-end brought forward, so applying it *and* the carry counts the same year end
        twice -- which opened April at -5,255.13 instead of -2,603.34.

        The two are not identical here on purpose: the derived close and the stated opening
        differ by the 25-26 differences the reconciliation accepts, and the explicit figure
        is the one that wins."""
        openings = pd.DataFrame(
            [{"period": "2026-04", "classification": "Excess", "amount": Decimal("-2632.45")}]
        )
        _, closes = repo.running_by_period(
            EMPTY_POSTINGS, NO_PROJECTIONS, NO_ALLOWANCE, CLASSES,
            ["2026-03", "2026-04"], today=dt.date(2026, 4, 30),
            openings=pd.concat([
                pd.DataFrame([{"period": "2026-03", "classification": "Excess",
                               "amount": Decimal("-2651.79")}]),
                openings,
            ], ignore_index=True),
        )
        april = closes[(closes["period"] == "2026-04")
                       & (closes["classification"] == "Excess")].iloc[0]["closing"]

        assert april == Decimal("-2632.45")  # not -5,284.24

    def test_a_month_with_no_stated_opening_still_carries_forward(self):
        """Replacing must not become 'reset to zero' for the months in between."""
        openings = pd.DataFrame(
            [{"period": "2026-04", "classification": "Excess", "amount": Decimal("-100")}]
        )
        _, closes = repo.running_by_period(
            EMPTY_POSTINGS, NO_PROJECTIONS, NO_ALLOWANCE, CLASSES,
            ["2026-04", "2026-05"], today=dt.date(2026, 5, 31), openings=openings,
        )
        may = closes[(closes["period"] == "2026-05")
                     & (closes["classification"] == "Excess")].iloc[0]["closing"]

        assert may == Decimal("-100")
