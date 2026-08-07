"""The Phase 5 calculations: expected gross, dated rates, card outstanding, savings.

Each of these replaces something the workbook held as a constant inside a formula, so the
tests are mostly about the derivation working again once the constant became data.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import reference, repo

APRIL = dt.date(2026, 4, 1)


def profiles(*rows) -> pd.DataFrame:
    """Salary records, keyed by *base* salary. The car allowance is derived from it."""
    return pd.DataFrame(
        [
            {
                "id": i,
                "effective_from": d,
                "base_salary": Decimal(s),
                "annual_salary": Decimal(s) + repo.car_allowance(Decimal(s)),
                "note": None,
            }
            for i, (d, s) in enumerate(rows, start=1)
        ],
        columns=["id", "effective_from", "base_salary", "annual_salary", "note"],
    )


def bonuses(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"period": p, "amount": Decimal(a), "note": None} for p, a in rows],
        columns=["period", "amount", "note"],
    )


# ------------------------------------------------------------------- expected gross


class TestExpectedGross:
    """Salary tracker column P, which was `ROUND(O/12, 2)` in eleven rows and
    `ROUND(O5/12,2)+29028.48` in the twelfth."""

    # Base salaries. The totals they imply -- 126,022.40 and 128,350.25 -- are what the
    # workbook stored as 'annual salary', which is why that figure never appeared on a
    # payslip: it already had the car allowance folded into it.
    PROFILES = profiles((APRIL, "116688"), (dt.date(2026, 5, 1), "118905"))
    BONUSES = bonuses(("2026-05", "29028.48"))

    def test_the_base_in_force_is_the_last_change_on_or_before_the_month(self):
        assert repo.base_in_force(self.PROFILES, APRIL) == Decimal("116688")
        assert repo.base_in_force(
            self.PROFILES, dt.date(2026, 4, 30)
        ) == Decimal("116688")
        assert repo.base_in_force(
            self.PROFILES, dt.date(2026, 5, 1)
        ) == Decimal("118905")

    def test_salary_in_force_adds_the_derived_car_allowance(self):
        """The two figures the old model stored as one."""
        assert repo.salary_in_force(self.PROFILES, APRIL) == Decimal("126022.40")
        assert repo.salary_in_force(
            self.PROFILES, dt.date(2026, 5, 1)
        ) == Decimal("128350.25")

    def test_before_the_first_record_there_is_no_salary(self):
        assert repo.salary_in_force(self.PROFILES, dt.date(2026, 3, 31)) is None
        assert repo.expected_gross("2026-03", self.PROFILES, self.BONUSES) is None

    def test_an_ordinary_month_is_a_twelfth(self):
        assert repo.expected_gross(
            "2026-04", self.PROFILES, self.BONUSES
        ) == Decimal("10501.87")

    def test_a_bonus_month_carries_its_own_figure(self):
        """The workbook could not derive this: the bonus lived inside the formula."""
        assert repo.expected_gross(
            "2026-05", self.PROFILES, self.BONUSES
        ) == Decimal("39724.33")

    def test_the_bonus_applies_only_to_its_own_month(self):
        assert repo.expected_gross(
            "2026-06", self.PROFILES, self.BONUSES
        ) == Decimal("10695.85")

    def test_no_bonuses_at_all_is_fine(self):
        empty = bonuses()
        assert repo.expected_gross("2026-04", self.PROFILES, empty) == Decimal("10501.87")


# ----------------------------------------------------------------------- dated rates


class TestCyclingRates:
    RATES = pd.DataFrame(
        [
            {"kind": "commute", "effective_from": APRIL, "amount": Decimal("10.50")},
            {"kind": "commute", "effective_from": dt.date(2026, 9, 1),
             "amount": Decimal("11.20")},
            {"kind": "band", "effective_from": APRIL, "amount": Decimal("8.90")},
            {"kind": "gym", "effective_from": APRIL, "amount": Decimal("4.60")},
        ]
    )

    def test_the_rate_in_force_is_the_last_change_on_or_before_the_day(self):
        assert repo.rate_in_force(
            self.RATES, "commute", dt.date(2026, 8, 31)
        ) == Decimal("10.50")
        assert repo.rate_in_force(
            self.RATES, "commute", dt.date(2026, 9, 1)
        ) == Decimal("11.20")

    def test_before_any_rate_the_day_scores_nothing(self):
        assert repo.rate_in_force(self.RATES, "commute", dt.date(2026, 3, 1)) == 0

    def test_an_unknown_kind_scores_nothing(self):
        assert repo.rate_in_force(self.RATES, "swim", APRIL) == 0

    def test_a_raised_fare_does_not_rewrite_earlier_rides(self):
        """The whole point of dating the rate: the workbook's nested IF applied today's
        fare to every historic row."""
        days = pd.DataFrame(
            [
                {"date": pd.Timestamp("2026-08-03"), "commute": True, "band": False,
                 "gym": False},
                {"date": pd.Timestamp("2026-09-03"), "commute": True, "band": False,
                 "gym": False},
            ]
        )
        valued = repo.cycling_savings_dated(days, self.RATES)
        assert list(valued["saving"]) == [Decimal("10.50"), Decimal("11.20")]

    def test_priority_order_is_commute_then_band_then_gym(self):
        days = pd.DataFrame(
            [{"date": pd.Timestamp("2026-05-01"), "commute": True, "band": True,
              "gym": True}]
        )
        valued = repo.cycling_savings_dated(days, self.RATES)
        assert valued["saving"].iloc[0] == Decimal("10.50")
        assert valued["kind"].iloc[0] == "Commute"

    def test_a_day_with_no_flag_scores_nothing(self):
        days = pd.DataFrame(
            [{"date": pd.Timestamp("2026-05-01"), "commute": False, "band": False,
              "gym": False}]
        )
        valued = repo.cycling_savings_dated(days, self.RATES)
        assert valued["saving"].iloc[0] == Decimal("0")
        assert valued["kind"].iloc[0] == "None"


# ------------------------------------------------------------------ card outstanding


class TestCardOutstanding:
    """What is owed on a card on top of the bill it is about to pay.

    Three states, decided by the card's own two dates: the whole balance before a statement
    is issued, the balance less that bill while it is awaiting collection, and the whole
    balance again once it has been paid.
    """

    # BA Amex bills on the 26th and is collected on the 9th of the *following* month, so the
    # payment day being the earlier number is what says the cycle crosses a month boundary.
    ACCOUNTS = pd.DataFrame(
        [
            {"id": 1, "name": "BA Amex", "type": "credit_card", "statement_day": 26,
             "payment_day": 9},
            {"id": 2, "name": "HSBC", "type": "bank", "statement_day": None,
             "payment_day": None},
        ]
    )
    BALANCES = pd.DataFrame(
        [
            {"account": "BA Amex", "closing": Decimal("1653.27")},
            {"account": "HSBC", "closing": Decimal("500.00")},
        ]
    )
    STATEMENTS = pd.DataFrame(
        [
            {"period": "2026-06", "account_id": 1, "bill_eom": Decimal("299.66")},
            {"period": "2026-07", "account_id": 1, "bill_eom": Decimal("1577.54")},
        ]
    )

    def outstanding(self, period, today):
        rows = repo.card_outstanding(
            self.BALANCES, self.STATEMENTS, self.ACCOUNTS, period, today=today
        )
        return rows.set_index("account").loc["BA Amex"]

    def test_only_credit_cards_appear(self):
        rows = repo.card_outstanding(
            self.BALANCES, self.STATEMENTS, self.ACCOUNTS, "2026-07",
            today=dt.date(2026, 8, 4),
        )
        assert list(rows["account"]) == ["BA Amex"]

    def test_a_past_month_is_read_as_at_its_month_end(self):
        """31 July: the statement went out on the 26th and is not collected until 9 August,
        so it is still standing. Reproduces the workbook's D52 for the same month."""
        row = self.outstanding("2026-07", dt.date(2026, 8, 4))
        assert row["as_of"] == dt.date(2026, 7, 31)
        assert row["awaiting"] == Decimal("1577.54")
        assert row["outstanding"] == Decimal("75.73")

    def test_before_the_payment_day_the_previous_bill_is_still_standing(self):
        """5 July: June's bill was issued on 26 June and is not collected until 9 July."""
        row = self.outstanding("2026-07", dt.date(2026, 7, 5))
        assert row["awaiting"] == Decimal("299.66")
        assert row["outstanding"] == Decimal("1353.61")

    def test_once_paid_the_whole_balance_is_outstanding_again(self):
        """9 July to 26 July: June's bill has gone out and July's has not been issued, so
        nothing on the card has reached a statement."""
        row = self.outstanding("2026-07", dt.date(2026, 7, 15))
        assert row["awaiting"] == Decimal("0")
        assert row["outstanding"] == Decimal("1653.27")

    def test_on_the_payment_day_itself_the_bill_counts_as_paid(self):
        row = self.outstanding("2026-07", dt.date(2026, 7, 9))
        assert row["awaiting"] == Decimal("0")

    def test_after_the_statement_day_the_new_bill_stands(self):
        row = self.outstanding("2026-07", dt.date(2026, 7, 27))
        assert row["awaiting"] == Decimal("1577.54")

    def test_the_bill_standing_may_not_be_this_months(self):
        """August, before the 26th: what is awaiting collection is July's bill, not the one
        in August's own column. That is the distinction the two columns carry."""
        row = self.outstanding("2026-08", dt.date(2026, 8, 4))
        assert row["awaiting"] == Decimal("1577.54")
        assert row["statement"] == Decimal("0")  # nothing stored for August yet

    def test_a_month_with_no_stored_bill_treats_it_as_zero(self):
        row = self.outstanding("2026-05", dt.date(2026, 8, 4))
        assert row["statement"] == Decimal("0")
        assert row["outstanding"] == Decimal("1653.27")

    def test_a_card_with_no_dates_set_cannot_say_what_is_billed(self):
        accounts = self.ACCOUNTS.copy()
        accounts.loc[accounts["name"] == "BA Amex", "statement_day"] = None
        row = repo.card_outstanding(
            self.BALANCES, self.STATEMENTS, accounts, "2026-07",
            today=dt.date(2026, 8, 4),
        ).set_index("account").loc["BA Amex"]
        assert row["awaiting"] == Decimal("0")
        assert "No statement or payment day" in row["position"]

    def test_no_credit_cards_gives_an_empty_frame_with_the_right_columns(self):
        banks = self.ACCOUNTS[self.ACCOUNTS["type"] == "bank"]
        rows = repo.card_outstanding(self.BALANCES, self.STATEMENTS, banks, "2026-07")
        assert rows.empty
        assert "outstanding" in rows.columns


class TestBillingCycle:
    """Which statement stands on a date, and when it falls due."""

    def test_a_card_paid_the_following_month(self):
        """BA Amex: issued on the 26th, collected on the 9th. The payment day being the
        smaller number is what says the cycle runs into the next month."""
        period, issued, due = repo.billing_cycle(26, 9, dt.date(2026, 7, 30))
        assert (period, issued, due) == (
            "2026-07", dt.date(2026, 7, 26), dt.date(2026, 8, 9)
        )

    def test_a_card_paid_within_the_same_month(self):
        """Platinum Amex: issued on the 16th, collected on the 30th."""
        period, issued, due = repo.billing_cycle(16, 30, dt.date(2026, 7, 20))
        assert (period, issued, due) == (
            "2026-07", dt.date(2026, 7, 16), dt.date(2026, 7, 30)
        )

    def test_before_this_months_statement_the_previous_one_is_current(self):
        period, issued, _ = repo.billing_cycle(26, 9, dt.date(2026, 7, 5))
        assert period == "2026-06"
        assert issued == dt.date(2026, 6, 26)

    def test_a_day_that_does_not_exist_falls_on_the_month_end(self):
        """A 31st-of-the-month cycle has nowhere to land in April."""
        _, issued, _ = repo.billing_cycle(31, 15, dt.date(2026, 4, 30))
        assert issued == dt.date(2026, 4, 30)

    def test_it_crosses_the_year_boundary(self):
        period, issued, due = repo.billing_cycle(26, 9, dt.date(2026, 12, 28))
        assert (period, issued, due) == (
            "2026-12", dt.date(2026, 12, 26), dt.date(2027, 1, 9)
        )

    def test_without_both_days_there_is_no_answer(self):
        assert repo.billing_cycle(None, 9, dt.date(2026, 7, 1)) is None
        assert repo.billing_cycle(26, None, dt.date(2026, 7, 1)) is None


def test_previous_period_crosses_the_year_boundary():
    assert repo.previous_period("2027-01") == "2026-12"
    assert repo.previous_period("2026-07") == "2026-06"


def test_month_end_knows_february():
    assert repo.month_end("2026-02") == dt.date(2026, 2, 28)
    assert repo.month_end("2026-04") == dt.date(2026, 4, 30)


# ------------------------------------------------------------------ account targets


def test_account_targets_pair_a_target_with_the_closing_balance():
    accounts = pd.DataFrame([{"id": 1, "name": "HSBC"}, {"id": 2, "name": "Halifax"}])
    balances = pd.DataFrame(
        [
            {"account": "HSBC", "closing": Decimal("1530.00")},
            {"account": "Halifax", "closing": Decimal("132.50")},
        ]
    )
    targets = pd.DataFrame(
        [
            {"period": "2026-07", "account_id": 1, "amount": Decimal("2800.00")},
            {"period": "2026-07", "account_id": 2, "amount": Decimal("132.50")},
        ]
    )
    table = repo.account_target_table(balances, targets, accounts, "2026-07")
    by_account = table.set_index("account")
    assert by_account.loc["HSBC", "required"] == Decimal("1270.00")
    assert by_account.loc["Halifax", "required"] == Decimal("0.00")


def test_account_targets_for_a_month_with_none_set_is_empty():
    accounts = pd.DataFrame([{"id": 1, "name": "HSBC"}])
    balances = pd.DataFrame([{"account": "HSBC", "closing": Decimal("10")}])
    targets = pd.DataFrame(columns=["period", "account_id", "amount"])
    assert repo.account_target_table(balances, targets, accounts, "2026-07").empty


# ---------------------------------------------------------------- spending calculation


class TestSpendingCalculation:
    """Salary tracker H17:I28, verified against the workbook's own July figures."""

    CATEGORIES = pd.DataFrame(
        [
            {"name": "Mortgage", "grouping": "Household bills"},
            {"name": "Water", "grouping": "Household bills"},
            {"name": "Netflix", "grouping": "Regular outgoings"},
            {"name": "Credit cards", "grouping": "Regular outgoings"},
            {"name": "Food", "grouping": "Regular outgoings"},
            {"name": "Job", "grouping": "Income"},
        ]
    )
    BUDGETS = pd.DataFrame(
        [
            {"period": "2026-07", "category": "Mortgage", "expected": Decimal("1000")},
            {"period": "2026-07", "category": "Water", "expected": Decimal("50")},
            {"period": "2026-07", "category": "Netflix", "expected": Decimal("15")},
            {"period": "2026-07", "category": "Credit cards", "expected": Decimal("550")},
            {"period": "2026-07", "category": "Food", "expected": Decimal("500")},
            {"period": "2026-07", "category": "Job", "expected": Decimal("0")},
        ]
    )

    def calculate(self, **kwargs):
        return repo.spending_calculation(
            self.BUDGETS, self.CATEGORIES, Decimal("5000"), "2026-07", **kwargs
        )

    def test_bills_are_the_household_grouping(self):
        assert self.calculate()["bills"] == Decimal("1050")

    def test_other_costs_exclude_food_and_credit_cards(self):
        """Both are counted elsewhere -- food is its own input, credit cards go to savings.
        Leaving them in would double-count."""
        assert self.calculate()["other"] == Decimal("15")

    def test_savings_is_the_input_plus_the_budgeted_card_repayment(self):
        result = self.calculate(savings=Decimal("1000"))
        assert result["card_repayment"] == Decimal("550")
        assert result["savings"] == Decimal("1550")

    def test_the_daily_figure_divides_by_thirty_whatever_the_month(self):
        """A spending allowance, not an apportionment -- so April and July give the same
        divisor, as the workbook's `=I27/30` did."""
        result = self.calculate(
            savings=Decimal("1000"), food=Decimal("500"), essentials=Decimal("50")
        )
        assert result["card_limit"] == Decimal("2385")
        assert result["monthly"] == Decimal("1835")
        assert result["daily"] == Decimal("61.17")

    def test_a_month_with_no_budget_yields_no_costs(self):
        result = repo.spending_calculation(
            self.BUDGETS, self.CATEGORIES, Decimal("5000"), "2026-11"
        )
        assert result["bills"] == Decimal("0")
        assert result["monthly"] == Decimal("5000")


# ---------------------------------------------------------------------- savings series


class TestSavingsSeries:
    """One row per month, opening and closing side by side."""

    ACCOUNTS = pd.DataFrame(
        [
            {"id": 1, "name": "Marcus", "type": "bank", "is_savings": True,
             "is_investment": False, "is_isa": False, "exclude_from_savings": False},
            {"id": 2, "name": "Wedding", "type": "bank", "is_savings": True,
             "is_investment": False, "is_isa": False, "exclude_from_savings": True},
            {"id": 3, "name": "Stocks", "type": "bank", "is_savings": False,
             "is_investment": True, "is_isa": False, "exclude_from_savings": False},
        ]
    )
    OPENINGS = pd.DataFrame(
        [
            {"account": "Marcus", "period": "2026-04", "opening": Decimal("1000")},
            {"account": "Wedding", "period": "2026-04", "opening": Decimal("500")},
            {"account": "Stocks", "period": "2026-04", "opening": Decimal("2000")},
            # May opens where April closed, as a real ledger does.
            {"account": "Marcus", "period": "2026-05", "opening": Decimal("1200")},
            {"account": "Wedding", "period": "2026-05", "opening": Decimal("500")},
            {"account": "Stocks", "period": "2026-05", "opening": Decimal("2000")},
        ]
    )
    POSTINGS = pd.DataFrame(
        [
            {"period": "2026-04", "account": "Marcus", "type": "Credit",
             "column": "credit", "amount": Decimal("200"), "signed": Decimal("200")},
            {"period": "2026-05", "account": "Stocks", "type": "Credit",
             "column": "credit", "amount": Decimal("150"), "signed": Decimal("150")},
        ]
    )
    TARGETS = pd.DataFrame(
        [
            {"period": "2026-04", "savings": Decimal("300"),
             "investments": Decimal("100")},
            {"period": "2026-05", "savings": Decimal("300"),
             "investments": Decimal("100")},
        ]
    )

    def series(self):
        return repo.savings_series(
            self.POSTINGS, self.OPENINGS, self.ACCOUNTS, self.TARGETS,
            ["2026-04", "2026-05"], today=dt.date(2026, 6, 1),
        )

    def test_one_row_per_month(self):
        rows = self.series()
        assert list(rows["period"]) == ["2026-04", "2026-05"]

    def test_a_month_carries_its_own_opening_and_closing(self):
        april = self.series().iloc[0]
        assert april["savings_bom"] == Decimal("1500")
        assert april["savings_eom"] == Decimal("1700")

    def test_earmarked_pots_are_excluded_from_available(self):
        april = self.series().iloc[0]
        assert april["savings_bom"] == Decimal("1500")
        assert april["available_bom"] == Decimal("1000")
        assert april["available_eom"] == Decimal("1200")

    def test_added_is_the_change_in_the_total_balance(self):
        april = self.series().iloc[0]
        assert april["savings_added"] == Decimal("200")
        assert april["savings_bom"] + april["savings_added"] == april["savings_eom"]

    def test_added_splits_into_available_and_reserved(self):
        """Adding to the wedding pot and adding to the general one are not the same event,
        which a single 'Added' column could not say."""
        april = self.series().iloc[0]
        assert april["available_added"] == Decimal("200")   # Marcus
        assert april["reserved_added"] == Decimal("0")      # Wedding untouched
        assert april["available_added"] + april["reserved_added"] == april["savings_added"]

    def test_money_into_an_earmarked_pot_counts_as_reserved(self):
        postings = pd.concat(
            [
                self.POSTINGS,
                pd.DataFrame(
                    [{"period": "2026-04", "account": "Wedding", "type": "Credit",
                      "column": "credit", "amount": Decimal("50"),
                      "signed": Decimal("50")}]
                ),
            ]
        )
        april = repo.savings_series(
            postings, self.OPENINGS, self.ACCOUNTS, self.TARGETS,
            ["2026-04"], today=dt.date(2026, 6, 1),
        ).iloc[0]
        assert april["available_added"] == Decimal("200")
        assert april["reserved_added"] == Decimal("50")
        assert april["savings_added"] == Decimal("250")
        # 'Available' is unmoved by it, which is the whole point of earmarking.
        assert april["available_eom"] == Decimal("1200")

    def test_the_target_column_accumulates(self):
        rows = self.series()
        assert rows.iloc[0]["savings_target_eom"] == Decimal("300")
        assert rows.iloc[1]["savings_target_eom"] == Decimal("600")

    def test_required_is_the_cumulative_target_less_what_is_available(self):
        """Positive means money still to find. Here the balance is comfortably ahead of the
        cumulative target, so it comes out negative throughout."""
        rows = self.series()
        assert rows.iloc[0]["savings_required"] == Decimal("300") - Decimal("1200")
        assert rows.iloc[1]["savings_required"] == Decimal("600") - Decimal("1200")

    def test_required_is_offered_on_each_of_the_three_bases(self):
        """One cumulative target, three balances measured against it: what the basis
        dropdown switches is which pot is being asked to meet it."""
        april = self.series().iloc[0]
        assert april["total_required"] == Decimal("300") - Decimal("1700")
        assert april["available_required"] == Decimal("300") - Decimal("1200")
        assert april["reserved_required"] == Decimal("300") - Decimal("500")
        # The original column is the available basis, which is what it always measured.
        assert april["savings_required"] == april["available_required"]

    def test_combined_available_leaves_out_the_earmarked_pots(self):
        april = self.series().iloc[0]
        assert april["combined"] == Decimal("1700") + Decimal("2000")
        assert april["combined_available"] == Decimal("1200") + Decimal("2000")

    def test_investments_are_tracked_separately(self):
        may = self.series().iloc[1]
        assert may["investments_eom"] == Decimal("2150")
        assert may["investments_added"] == Decimal("150")
        assert may["investments_required"] == Decimal("200") - Decimal("2150")

    def test_a_month_with_no_target_contributes_nothing_to_the_running_total(self):
        rows = repo.savings_series(
            self.POSTINGS, self.OPENINGS, self.ACCOUNTS,
            pd.DataFrame(columns=["period", "savings", "investments"]),
            ["2026-04"], today=dt.date(2026, 6, 1),
        )
        assert rows.iloc[0]["savings_target"] == Decimal("0")
        assert rows.iloc[0]["savings_target_eom"] == Decimal("0")


# ------------------------------------------------------------------------- band storage


class TestBandsAsPercentages:
    """Rates are stored as percentages so a two-decimal column can express 8.5%. As a
    fraction it could only ever have held whole percentage points."""

    def test_salary_bands_converts_rates_back_to_fractions(self, session):
        with session.begin():
            for key, value in (
                ("ni_lower_rate", Decimal("8")),
                ("basic_rate", Decimal("20")),
                ("personal_allowance", Decimal("1047.50")),
                ("basic_rate_threshold", Decimal("4189.17")),
            ):
                reference.set_assumption(session, 2026, key, value)

        bands = repo.salary_bands(session, 2026)
        assert bands.ni_lower_rate == Decimal("0.08")
        assert bands.basic_rate == Decimal("0.2")

    def test_a_fractional_rate_survives_the_round_trip(self):
        assert Decimal("8.50") / repo.HUNDRED == Decimal("0.085")

    def test_the_basic_band_is_derived_from_its_two_inputs(self, session):
        """The workbook's D36 is `=D28-D22`. Storing the result as well as the inputs would
        let the three drift apart the moment one was edited."""
        with session.begin():
            reference.set_assumption(
                session, 2026, "personal_allowance", Decimal("1047.50")
            )
            reference.set_assumption(
                session, 2026, "basic_rate_threshold", Decimal("4189.17")
            )
        assert repo.salary_bands(session, 2026).basic_band == Decimal("3141.67")

    def test_without_the_inputs_it_falls_back_to_the_stored_band(self, session):
        with session.begin():
            reference.set_assumption(session, 2026, "basic_band", Decimal("3141.67"))
        assert repo.salary_bands(session, 2026).basic_band == Decimal("3141.67")


# ------------------------------------------------------------------------------- writes


class TestCyclingRecord:
    def test_a_day_takes_at_most_one_entry(self, session):
        with session.begin():
            reference.record_cycling_day(session, APRIL, "commute")
        with session.begin():
            outcome = reference.record_cycling_day(session, APRIL, "gym")
        assert outcome.ok
        assert "Replaced" in outcome.message

        rates = pd.DataFrame(
            [{"kind": "gym", "effective_from": APRIL, "amount": Decimal("4.60")}]
        )
        _, days = repo.load_cycling(session)
        assert len(days) == 1
        assert repo.cycling_savings_dated(days, rates)["kind"].iloc[0] == "Gym"

    def test_an_unknown_kind_is_refused(self, session):
        with session.begin():
            outcome = reference.record_cycling_day(session, APRIL, "swim")
        assert not outcome.ok

    def test_a_day_can_be_removed(self, session):
        with session.begin():
            reference.record_cycling_day(session, APRIL, "commute")
        with session.begin():
            outcome = reference.record_cycling_day(session, APRIL, None)
        assert outcome.ok
        _, days = repo.load_cycling(session)
        assert days.empty

    def test_an_outgoing_needs_a_description_and_an_amount(self, session):
        with session.begin():
            assert not reference.add_cycling_outgoing(
                session, APRIL, "", Decimal("10"), "General"
            ).ok
        with session.begin():
            assert not reference.add_cycling_outgoing(
                session, APRIL, "Service", Decimal("0"), "General"
            ).ok


class TestCards:
    def test_a_card_is_added_and_amended(self, session):
        with session.begin():
            card, outcome = reference.add_card(
                session, "Barclaycard", Decimal("12538.84"), APRIL, 21, Decimal("0.01")
            )
        assert outcome.ok
        with session.begin():
            assert reference.update_card(
                session, card.id, term_months=24, credit_limit=Decimal("15000")
            ).ok
        cards = repo.load_cards(session)
        assert int(cards.iloc[0]["term_months"]) == 24
        assert cards.iloc[0]["credit_limit"] == Decimal("15000")

    def test_two_cards_cannot_share_a_name(self, session):
        with session.begin():
            reference.add_card(session, "MBNA", Decimal("100"), APRIL, 12, Decimal("0.025"))
        with session.begin():
            _, outcome = reference.add_card(
                session, "mbna", Decimal("200"), APRIL, 12, Decimal("0.025")
            )
        assert not outcome.ok

    def test_a_term_must_be_at_least_a_month(self, session):
        with session.begin():
            _, outcome = reference.add_card(
                session, "Tesco", Decimal("100"), APRIL, 0, Decimal("0.01")
            )
        assert not outcome.ok

    def test_a_card_can_be_deleted_since_it_carries_no_transactions(self, session):
        with session.begin():
            card, _ = reference.add_card(
                session, "Halifax", Decimal("100"), APRIL, 12, Decimal("0.025")
            )
        with session.begin():
            assert reference.delete_card(session, card.id).ok
        assert repo.load_cards(session).empty


class TestSalaryInputs:
    def test_a_salary_change_replaces_the_same_date(self, session):
        with session.begin():
            reference.set_salary_profile(session, APRIL, Decimal("100000"))
        with session.begin():
            reference.set_salary_profile(session, APRIL, Decimal("110000"))
        rows = repo.load_salary_profiles(session)
        assert len(rows) == 1
        assert rows.iloc[0]["base_salary"] == Decimal("110000")
        # Written alongside as base + car allowance: 6,000 + 5% of 60,000.
        assert rows.iloc[0]["annual_salary"] == Decimal("119000")

    def test_a_zero_bonus_removes_the_row(self, session):
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("5000"))
            assert len(repo.load_bonuses(session)) == 1
        with session.begin():
            reference.set_bonus(session, "2026-05", Decimal("0"))
        assert repo.load_bonuses(session).empty

    def test_a_payslip_is_filled_in_field_by_field(self, session):
        """The workbook had no way to enter a future month's actuals at all."""
        with session.begin():
            reference.set_payslip(session, "2026-08", gross=Decimal("10000"))
        with session.begin():
            reference.set_payslip(session, "2026-08", net=Decimal("6000"))
        row = repo.load_payslips(session).iloc[0]
        assert row["gross"] == Decimal("10000")
        assert row["net"] == Decimal("6000")


class TestPeriodicParameters:
    def test_an_account_target_of_none_clears_the_row(self, session):
        with session.begin():
            reference.set_account_target(session, "2026-07", 1, Decimal("500"))
            assert len(repo.load_account_targets(session)) == 1
        with session.begin():
            reference.set_account_target(session, "2026-07", 1, None)
        assert repo.load_account_targets(session).empty

    def test_clearing_projections_removes_only_that_month(self, session):
        with session.begin():
            reference.set_projection(session, dt.date(2026, 4, 15), 1, Decimal("50"))
            reference.set_projection(session, dt.date(2026, 5, 15), 1, Decimal("60"))
        with session.begin():
            removed = reference.clear_projections(session, "2026-04")
        assert removed == 1
        remaining = repo.load_projections(session)
        assert len(remaining) == 1
        assert remaining.iloc[0]["date"].month == 5

    def test_clearing_projections_handles_december(self, session):
        with session.begin():
            reference.set_projection(session, dt.date(2026, 12, 31), 1, Decimal("50"))
            reference.set_projection(session, dt.date(2027, 1, 1), 1, Decimal("60"))
        with session.begin():
            removed = reference.clear_projections(session, "2026-12")
        assert removed == 1
        assert len(repo.load_projections(session)) == 1


class TestMonthlyAnnualEntry:
    """A band can be entered either way round, and the other side follows."""

    def reconcile(self, monthly, annual, was_monthly=1000.0, was_annual=12000.0):
        return repo.reconcile_monthly_annual(monthly, annual, was_monthly, was_annual)

    def test_an_unchanged_row_is_not_rewritten(self):
        assert self.reconcile(1000.0, 12000.0) is None

    def test_a_monthly_edit_is_taken_as_given(self):
        assert self.reconcile(1100.0, 12000.0) == Decimal("1100")

    def test_an_annual_edit_is_divided_by_twelve(self):
        assert self.reconcile(1000.0, 13200.0) == Decimal("1100")

    def test_when_both_move_the_annual_value_wins(self):
        """The figure bands are published as, and the one likelier to have been copied from
        a payslip -- so in the ambiguous case it is the one to trust."""
        assert self.reconcile(999.0, 13200.0) == Decimal("1100")

    def test_clearing_the_annual_value_gives_zero(self):
        assert self.reconcile(1000.0, None) == Decimal("0")

    def test_a_row_that_was_empty_and_stays_empty_is_left_alone(self):
        assert repo.reconcile_monthly_annual(None, None, None, None) is None

    def test_filling_in_a_previously_empty_row(self):
        assert repo.reconcile_monthly_annual(None, 12000.0, None, None) == Decimal("1000")

    def test_rounding_noise_does_not_count_as_a_change(self):
        """A data_editor round-trips through float, so an untouched 1047.50 can come back a
        hair off. Treating that as an edit would rewrite every row on every save."""
        assert self.reconcile(1000.001, 12000.0) is None


class TestDatabaseExists:
    """The launch check must not confuse 'busy' with 'gone'.

    Path.exists() swallows OSError and returns False, so a database briefly locked by
    another process looks exactly like one that was never created -- and the app then told
    the user to rebuild from the workbook, which would have discarded everything entered
    since. Being wrong in that direction is much worse than being wrong in the other.
    """

    def test_a_real_database_is_found(self, tmp_path):
        from unittest import mock
        from budget import config, ui

        path = tmp_path / "budget.db"
        path.write_bytes(b"x" * 100)
        with mock.patch.object(config, "DB_PATH", path):
            assert ui.database_exists()

    def test_a_locked_database_is_not_reported_as_missing(self, tmp_path):
        from pathlib import Path
        from unittest import mock
        from budget import config, ui

        path = tmp_path / "budget.db"
        path.write_bytes(b"x" * 100)
        (tmp_path / "budget.db-wal").write_bytes(b"")

        with mock.patch.object(config, "DB_PATH", path), mock.patch.object(
            Path, "stat", side_effect=PermissionError("in use")
        ):
            assert ui.database_exists()

    def test_a_genuinely_absent_database_is_reported(self, tmp_path):
        from unittest import mock
        from budget import config, ui

        with mock.patch.object(config, "DB_PATH", tmp_path / "nothing.db"):
            assert not ui.database_exists()

    def test_an_empty_file_does_not_count(self, tmp_path):
        """A zero-byte budget.db is a half-finished copy, not a database."""
        from unittest import mock
        from budget import config, ui

        path = tmp_path / "budget.db"
        path.touch()
        with mock.patch.object(config, "DB_PATH", path):
            assert not ui.database_exists()


class TestSavingsTargetSeed:
    """The cumulative target counts up from each account's seed, not from zero.

    It is measured against a *balance*, and these pots had years of contributions in them
    before any of this was recorded. Starting the total at nothing makes every month read
    as thousands ahead of target -- against the real data, April 2025 came out 15,659.21
    ahead, where the workbook says 5,340.79 behind.
    """

    def accounts(self, marcus=None, stocks=None):
        rows = []
        for row in TestSavingsSeries.ACCOUNTS.to_dict("records"):
            seed = {"Marcus": marcus, "Stocks": stocks}.get(row["name"])
            rows.append({**row, "savings_seed": seed})
        return pd.DataFrame(rows)

    def series(self, **seeds):
        return repo.savings_series(
            TestSavingsSeries.POSTINGS, TestSavingsSeries.OPENINGS,
            self.accounts(**seeds), TestSavingsSeries.TARGETS,
            ["2026-04", "2026-05"], today=dt.date(2026, 6, 1),
        )

    def test_no_seed_starts_the_cumulative_target_at_the_first_month(self):
        april = self.series().iloc[0]
        assert april["savings_target_eom"] == Decimal("300")

    def test_a_seed_starts_it_higher(self):
        april = self.series(marcus=Decimal("21000")).iloc[0]
        assert april["savings_target_eom"] == Decimal("21300")

    def test_the_seed_carries_into_every_later_month(self):
        may = self.series(marcus=Decimal("21000")).iloc[1]
        assert may["savings_target_eom"] == Decimal("21600")  # 21,000 + 300 + 300

    def test_required_is_measured_from_the_seeded_total(self):
        """The whole point: 'required' is the cumulative target less the balance, so both
        sides have to start from the same place."""
        april = self.series(marcus=Decimal("21000")).iloc[0]
        assert april["available_required"] == Decimal("20100")  # 21,300 - 1,200

    def test_savings_and_investment_seeds_are_kept_apart(self):
        april = self.series(marcus=Decimal("1000"), stocks=Decimal("5000")).iloc[0]
        assert april["savings_target_eom"] == Decimal("1300")
        assert april["investments_target_eom"] == Decimal("5100")

    def test_a_missing_column_is_not_an_error(self):
        """A database that predates the column still loads -- the frame simply has no
        savings_seed, and the totals start where they always did."""
        without = TestSavingsSeries.ACCOUNTS
        rows = repo.savings_series(
            TestSavingsSeries.POSTINGS, TestSavingsSeries.OPENINGS, without,
            TestSavingsSeries.TARGETS, ["2026-04"], today=dt.date(2026, 6, 1),
        )
        assert rows.iloc[0]["savings_target_eom"] == Decimal("300")
