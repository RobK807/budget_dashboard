"""Aggregation rules that the reconciliation gate covers end-to-end, pinned here as small
examples so a failure says *which* rule broke rather than 'April is out by £3.40'.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import repo, tax


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


class TestCumulativeTax:
    """Assembling the year-to-date position from the Salary page's frame.

    The arithmetic is pinned in test_tax; what matters here is the plumbing -- which months
    are included, which figure stands in for a month with no payslip, and that two tax years
    are never accumulated into one another.
    """

    from tests.test_tax import BANDS

    def bands_for(self, period):
        return self.BANDS

    def frame(self, rows):
        return pd.DataFrame(
            rows, columns=["period", "taxable", "actual_paye", "expected_paye"]
        )

    def test_actual_paye_is_used_where_a_payslip_exists(self):
        got = repo.cumulative_tax(
            self.frame([("2026-04", Decimal("9342.47"), Decimal("3130.86"),
                         Decimal("3130.65"))]),
            self.bands_for,
        )
        assert got.iloc[0]["deducted"] == Decimal("3130.86")
        assert bool(got.iloc[0]["actual"]) is True

    def test_the_model_stands_in_where_no_payslip_has_been_entered(self):
        """Otherwise the closing row would report the year as massively overpaid simply
        because the months still to come had deducted nothing."""
        got = repo.cumulative_tax(
            self.frame([("2026-04", Decimal("9342.47"), None, Decimal("3130.65"))]),
            self.bands_for,
        )
        assert got.iloc[0]["deducted"] == Decimal("3130.65")
        assert bool(got.iloc[0]["actual"]) is False

    def test_months_the_model_cannot_build_are_dropped(self):
        got = repo.cumulative_tax(
            self.frame([
                ("2026-04", Decimal("9342.47"), None, Decimal("3130.65")),
                ("2026-05", None, None, None),
            ]),
            self.bands_for,
        )
        assert list(got["period"]) == ["2026-04"]

    def test_tax_years_accumulate_separately(self):
        """March and the April after it are different years; running them together would
        carry a whole year's bands into the first month of the next."""
        got = repo.cumulative_tax(
            self.frame([
                ("2027-03", Decimal("9517.97"), None, Decimal("3270.82")),
                ("2027-04", Decimal("9517.97"), None, Decimal("3270.82")),
            ]),
            self.bands_for,
        )
        assert list(got["tax_year"]) == [2026, 2027]
        # Each year restarts: the April row is month 1 of its own year, not month 13.
        assert got.iloc[1]["taxable_to_date"] == Decimal("9517.97")

    def test_a_single_year_can_be_asked_for(self):
        got = repo.cumulative_tax(
            self.frame([
                ("2027-03", Decimal("9517.97"), None, Decimal("3270.82")),
                ("2027-04", Decimal("9517.97"), None, Decimal("3270.82")),
            ]),
            self.bands_for,
            tax_year=2027,
        )
        assert list(got["period"]) == ["2027-04"]

    def test_the_bonus_year_reaches_the_known_closing_position(self):
        """End to end, against the same figures test_tax pins the arithmetic on."""
        months = [("2026-04", Decimal("9342.47")), ("2026-05", Decimal("38546.45"))] + [
            (p, Decimal("9517.97"))
            for p in ["2026-%02d" % m for m in range(6, 13)]
            + ["2027-%02d" % m for m in (1, 2, 3)]
        ]
        rows = [
            (period, taxable, None,
             tax.income_tax(taxable, self.BANDS, repo.period_start(period)))
            for period, taxable in months
        ]
        closing = repo.cumulative_tax(self.frame(rows), self.bands_for).iloc[-1]
        assert closing["taxable_to_date"] == Decimal("143068.62")
        assert closing["due_to_date"] == Decimal("50583.88")
        assert closing["difference"] == Decimal("1464.32")

    def test_an_empty_frame_gives_an_empty_result_with_the_right_columns(self):
        got = repo.cumulative_tax(pd.DataFrame(), self.bands_for)
        assert got.empty
        assert "difference" in got.columns


class TestSplitAtZero:
    """Splitting a line so the part below the axis can be drawn in another colour.

    Plotly colours a line per trace, not per segment, so the series has to be handed over as
    two. The property that matters is that they meet *on* the axis -- without interpolating
    the crossing there is a visible break, one sample wide, every time a running total passes
    through nothing, which on a daily series is often.
    """

    def split(self, y, x=None):
        from budget import ui

        return ui.split_at_zero(x or list(range(len(y))), y)

    def test_a_crossing_is_interpolated_onto_the_axis(self):
        xs, above, below = self.split([10.0, -10.0])
        assert xs == [0, 0.5, 1]
        assert above == [10.0, 0.0, None]
        assert below == [None, 0.0, -10.0]

    def test_the_two_halves_share_one_x_axis(self):
        xs, above, below = self.split([5.0, -5.0, 5.0])
        assert len(xs) == len(above) == len(below)

    def test_every_point_belongs_to_exactly_one_half_unless_it_is_zero(self):
        _, above, below = self.split([3.0, -3.0, 4.0])
        for a, b in zip(above, below):
            assert (a is None) != (b is None) or a == b == 0.0

    def test_a_series_that_never_goes_negative_has_an_empty_lower_half(self):
        _, above, below = self.split([1.0, 2.0, 3.0])
        assert above == [1.0, 2.0, 3.0]
        assert below == [None, None, None]

    def test_touching_zero_without_crossing_does_not_break_the_line(self):
        """Zero belongs to both halves, so a line that grazes the axis stays joined."""
        xs, above, below = self.split([2.0, 0.0, 2.0])
        assert xs == [0, 1, 2]
        assert above == [2.0, 0.0, 2.0]

    def test_dates_interpolate_too(self):
        xs, _, _ = self.split(
            [4.0, -4.0],
            [pd.Timestamp("2026-08-01"), pd.Timestamp("2026-08-03")],
        )
        assert xs[1] == pd.Timestamp("2026-08-02")

    def test_an_empty_series_is_not_an_error(self):
        assert self.split([]) == ([], [], [])


class TestRolledForwardOpenings:
    """A month's opening is its stated opening plus everything posted since.

    The stored opening_balance rows came from the workbook's row 60, which there is a
    *formula* -- the previous month's End. Copied across as values they stopped following
    the data, so every stored opening after the first described the day the migration ran.
    Adding GBP 2.50 to July left August 2.50 behind, September frozen at August's stale
    figure, and so on to March: 107 reconciliation differences from one cause, all silent.
    """

    def openings(self, rows):
        return pd.DataFrame(rows, columns=["account", "period", "opening"])

    def postings(self, rows):
        return pd.DataFrame(rows, columns=["account", "period", "signed"])

    def test_the_first_month_is_the_stated_opening(self):
        got = repo.rolled_forward_openings(
            self.postings([]),
            self.openings([("HSBC", "2026-04", Decimal("100"))]),
            "2026-04",
        )
        assert got["HSBC"] == Decimal("100")

    def test_a_later_month_carries_what_happened_in_between(self):
        got = repo.rolled_forward_openings(
            self.postings([("HSBC", "2026-04", Decimal("25"))]),
            self.openings([
                ("HSBC", "2026-04", Decimal("100")),
                ("HSBC", "2026-05", Decimal("100")),  # stale: never updated
            ]),
            "2026-05",
        )
        assert got["HSBC"] == Decimal("125")

    def test_the_stored_value_no_longer_wins(self):
        """The regression itself. A stale stored row must not override the roll-forward."""
        got = repo.rolled_forward_openings(
            self.postings([("HSBC", "2026-04", Decimal("2.50"))]),
            self.openings([
                ("HSBC", "2026-04", Decimal("1000")),
                ("HSBC", "2026-05", Decimal("1000")),
            ]),
            "2026-05",
        )
        assert got["HSBC"] == Decimal("1002.50")

    def test_it_accumulates_across_several_months(self):
        got = repo.rolled_forward_openings(
            self.postings([
                ("HSBC", "2026-04", Decimal("10")),
                ("HSBC", "2026-05", Decimal("20")),
                ("HSBC", "2026-06", Decimal("40")),
            ]),
            self.openings([("HSBC", "2026-04", Decimal("100"))]),
            "2026-07",
        )
        assert got["HSBC"] == Decimal("170")

    def test_a_month_with_nothing_in_it_still_carries_forward(self):
        """Where the old behaviour repeated itself to March: no postings, so the stale
        stored opening was returned unchanged twelve times over."""
        got = repo.rolled_forward_openings(
            self.postings([("HSBC", "2026-04", Decimal("30"))]),
            self.openings([("HSBC", "2026-04", Decimal("100"))]),
            "2026-12",
        )
        assert got["HSBC"] == Decimal("130")

    def test_an_account_opened_mid_year_starts_at_its_own_first_month(self):
        """Its stated opening is a real balance, not a carry-forward, and nothing before
        that month belongs to it. Anchoring on the year's first month would either lose it
        or double-count anything dated earlier."""
        got = repo.rolled_forward_openings(
            self.postings([("Tembo", "2026-06", Decimal("50"))]),
            self.openings([
                ("Tembo", "2026-06", Decimal("1000")),
                ("Tembo", "2026-07", Decimal("1000")),
            ]),
            "2026-07",
        )
        assert got["Tembo"] == Decimal("1050")

    def test_postings_before_an_account_opened_are_not_counted_twice(self):
        got = repo.rolled_forward_openings(
            self.postings([
                ("Tembo", "2026-04", Decimal("999")),  # before its stated opening
                ("Tembo", "2026-06", Decimal("50")),
            ]),
            self.openings([("Tembo", "2026-06", Decimal("1000"))]),
            "2026-07",
        )
        assert got["Tembo"] == Decimal("1050")

    def test_accounts_are_kept_apart(self):
        got = repo.rolled_forward_openings(
            self.postings([
                ("HSBC", "2026-04", Decimal("10")),
                ("Savings", "2026-04", Decimal("500")),
            ]),
            self.openings([
                ("HSBC", "2026-04", Decimal("100")),
                ("Savings", "2026-04", Decimal("0")),
            ]),
            "2026-05",
        )
        assert got["HSBC"] == Decimal("110")
        assert got["Savings"] == Decimal("500")

    def test_no_openings_at_all_is_not_an_error(self):
        got = repo.rolled_forward_openings(
            self.postings([("HSBC", "2026-04", Decimal("10"))]),
            self.openings([]),
            "2026-05",
        )
        assert got.empty or got.get("HSBC") == Decimal("10")


class TestCompletedThrough:
    """Which month a summary has actually measured to.

    Kept beside investment_return_summary and using the same test, so a heading cannot drift
    from the figures under it. Naming the month with series['period'].max() described the end
    of the *data*, which runs to the fiscal year end: on 6 August it announced 'to end of
    August 2026' over July's numbers, August having been excluded for not having finished.
    """

    def series(self, periods):
        return pd.DataFrame(
            {
                "period": periods,
                "date": [repo.month_end(p) for p in periods],
                "account": ["ISA"] * len(periods),
            }
        )

    def test_a_month_still_running_is_not_counted(self):
        got = repo.completed_through(
            self.series(["2026-06", "2026-07", "2026-08"]), dt.date(2026, 8, 6)
        )
        assert got == "2026-07"

    def test_the_last_day_of_a_month_completes_it(self):
        got = repo.completed_through(
            self.series(["2026-07", "2026-08"]), dt.date(2026, 8, 31)
        )
        assert got == "2026-08"

    def test_months_beyond_today_are_ignored(self):
        """The series runs to the fiscal year end whether or not those months have happened."""
        got = repo.completed_through(
            self.series(["2026-%02d" % m for m in range(4, 13)]), dt.date(2026, 8, 6)
        )
        assert got == "2026-07"

    def test_before_any_month_has_closed_it_falls_back_to_the_first(self):
        """Matching the summary's own fallback, so the two still agree."""
        got = repo.completed_through(self.series(["2026-04"]), dt.date(2026, 4, 15))
        assert got == "2026-04"

    def test_an_empty_series_has_no_answer(self):
        assert repo.completed_through(pd.DataFrame(columns=["period", "date"])) is None

    def test_it_agrees_with_what_the_summary_counts(self):
        """The property that matters: heading and figures derived from one rule."""
        periods = ["2026-%02d" % m for m in range(4, 10)]
        series = pd.DataFrame(
            {
                "period": periods,
                "date": [repo.month_end(p) for p in periods],
                "account": ["ISA"] * len(periods),
                "opening": [Decimal("100")] * len(periods),
                "contributions": [Decimal("0")] * len(periods),
                "gain": [Decimal("1")] * len(periods),
                "closing": [Decimal("101")] * len(periods),
                "monthly_return": [Decimal("0.01")] * len(periods),
            }
        )
        today = dt.date(2026, 8, 6)
        through = repo.completed_through(series, today)
        counted = int(repo.investment_return_summary(series, today)["months"].max())
        assert periods.index(through) + 1 == counted


class TestAccountsOutsideTheirValidityWindow:
    """Backfilling 25-26 made this visible. Every account used to run the whole of the only
    year there was, so an account that had closed, or had not opened, never came up. With a
    second year underneath, five accounts closed in 2025 would otherwise have sat at 0.00 in
    every month of 2026-27, and the ones opened later did the same in reverse."""

    def accounts(self, **kw):
        base = {
            "name": "Old", "type": "bank", "is_savings": False,
            "is_investment": False, "is_isa": False,
            "valid_from": dt.date(2025, 4, 1), "valid_to": dt.date(2025, 8, 31),
        }
        return frame({**base, **kw})

    def names(self, accounts, period, postings=None, openings=None):
        # Empty *with the right columns*, which is what the loaders return for a year with
        # nothing in it. A bare DataFrame has no columns at all and fails on the first
        # lookup rather than on anything this is testing.
        if postings is None:
            postings = frame(posting()).iloc[0:0]
        if openings is None:
            openings = frame(
                {"account": "Old", "period": "2025-04", "opening": Decimal("0")}
            ).iloc[0:0]
        out = repo.account_balances(postings, openings, period, accounts)
        return [] if out.empty else list(out["account"])

    def test_a_closed_and_empty_account_is_omitted(self):
        assert self.names(self.accounts(), "2026-04") == []

    def test_an_account_that_has_not_opened_is_omitted(self):
        future = self.accounts(valid_from=dt.date(2026, 6, 1), valid_to=None)
        assert self.names(future, "2026-04") == []

    def test_the_month_it_closes_is_still_its_month(self):
        """Both ends inclusive: an account closed on the 31st was live all August."""
        assert self.names(self.accounts(), "2025-08") == ["Old"]

    def test_the_month_it_opens_is_too(self):
        late = self.accounts(valid_from=dt.date(2025, 4, 20), valid_to=None)
        assert self.names(late, "2025-04") == ["Old"]

    def test_an_account_still_holding_money_keeps_its_row(self):
        """Emptiness is required as well as the dates, so a balance cannot vanish because a
        valid_to was typed a month early. It stays visible, looking like the mistake it is."""
        openings = frame({"account": "Old", "period": "2025-04", "opening": Decimal("500")})
        assert self.names(self.accounts(), "2026-04", openings=openings) == ["Old"]

    def test_an_account_with_postings_keeps_its_row(self):
        postings = frame(
            posting(account="Old", period="2026-04", date=pd.Timestamp("2026-04-05"))
        )
        assert self.names(self.accounts(), "2026-04", postings=postings) == ["Old"]

    def test_an_open_account_is_always_kept_even_at_zero(self):
        open_now = self.accounts(valid_to=None)
        assert self.names(open_now, "2026-04") == ["Old"]
