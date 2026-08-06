"""The Savings interest tracker, folded into the dashboard.

Four sheets' worth, none of it new data: interest and donations are groupings of the ledger,
the plan is the targets it already had split back into the accounts they came from, and the
investment return is derived from transactions rather than typed in month by month.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import importer, repo
from budget.validation import Candidate

# --------------------------------------------------------------------- the tax year


class TestTaxYearFromADate:
    """The UK tax year runs 6 April to 5 April. Month granularity cannot say that, which is
    why the tracker split April into two hand-labelled rows and filed them separately."""

    @pytest.mark.parametrize(
        "when,expected",
        [
            (dt.date(2026, 4, 1), 2025),   # 'Apr (before 6th)'
            (dt.date(2026, 4, 5), 2025),   # the last day of the old year
            (dt.date(2026, 4, 6), 2026),   # 'Apr (after 6th)'
            (dt.date(2026, 4, 30), 2026),
            (dt.date(2027, 3, 31), 2026),  # still the same tax year
            (dt.date(2027, 4, 6), 2027),
            (dt.date(2026, 12, 25), 2026),
        ],
    )
    def test_the_boundary_is_the_sixth(self, when, expected):
        assert repo.tax_year_of_date(when) == expected

    def test_a_timestamp_works_too(self):
        assert repo.tax_year_of_date(pd.Timestamp("2026-04-03")) == 2025

    def test_it_differs_from_the_period_version(self):
        """`tax_year_of` is right to the month and cannot be right to the day. Both exist
        because a payslip belongs to a month but interest belongs to a date."""
        assert repo.tax_year_of("2026-04") == 2026
        assert repo.tax_year_of_date(dt.date(2026, 4, 1)) == 2025

    def test_the_label_reads_as_the_tracker_wrote_it(self):
        assert repo.tax_year_label(2026) == "26-27"
        assert repo.tax_year_label(1999) == "99-00"


# ------------------------------------------------------------------------- interest


ACCOUNTS = pd.DataFrame(
    [
        {"id": 1, "name": "Halifax", "type": "bank", "is_savings": False,
         "is_investment": False, "is_isa": False, "exclude_from_savings": False,
         "interest_net": True},
        {"id": 2, "name": "Marcus", "type": "bank", "is_savings": True,
         "is_investment": False, "is_isa": False, "exclude_from_savings": False,
         "interest_net": False},
        {"id": 3, "name": "Stocks", "type": "bank", "is_savings": False,
         "is_investment": True, "is_isa": False, "exclude_from_savings": False,
         "interest_net": False},
    ]
)


def txn(id, date, amount, account="Marcus", category="Interest", kind="Credit",
        comment=None, donation=False, deleted=False):
    return {
        "id": id, "date": pd.Timestamp(date), "period": date[:7], "type": kind,
        "amount": Decimal(str(amount)), "account_from": account, "account_to": None,
        "category": category, "classification": "Excess", "comment": comment,
        "category_comment": None, "identifier": None, "is_donation": donation,
        "deleted": deleted, "deleted_reason": None,
    }


class TestInterestByTaxYear:
    TXNS = pd.DataFrame(
        [
            # 1 April is the *previous* tax year.
            txn(1, "2026-04-01", "2.35", account="Halifax"),
            txn(2, "2026-04-30", "11.50"),
            txn(3, "2026-07-30", "18.35"),
            txn(4, "2026-05-31", "0.50", account="Halifax"),
            # Not interest, so not counted.
            txn(5, "2026-06-01", "500.00", category="Food"),
            # Removed, so not counted either.
            txn(6, "2026-06-02", "99.00", deleted=True),
        ]
    )

    def rows(self):
        return repo.interest_by_tax_year(self.TXNS, ACCOUNTS)

    def test_only_the_interest_category_counts(self):
        assert Decimal("500") not in list(self.rows()["amount"])
        assert self.rows()["amount"].sum() == Decimal("32.70")

    def test_a_removed_transaction_is_ignored(self):
        assert Decimal("99.00") not in list(self.rows()["amount"])

    def test_the_first_days_of_april_fall_in_the_previous_year(self):
        rows = self.rows()
        early = rows[rows["tax_year"] == 2025]
        assert list(early["account"]) == ["Halifax"]
        assert early["amount"].iloc[0] == Decimal("2.35")

    def test_the_account_flag_decides_gross_or_net(self):
        rows = self.rows().set_index(["tax_year", "account"])
        assert rows.loc[(2026, "Halifax"), "basis"] == "Net"
        assert rows.loc[(2026, "Marcus"), "basis"] == "Gross"

    def test_interest_charged_subtracts(self):
        """A debit under the same category is interest going the other way."""
        frame = pd.concat(
            [self.TXNS, pd.DataFrame([txn(7, "2026-08-01", "5.00", kind="Debit")])]
        )
        rows = repo.interest_by_tax_year(frame, ACCOUNTS)
        marcus = rows[(rows["tax_year"] == 2026) & (rows["account"] == "Marcus")]
        assert marcus["amount"].iloc[0] == Decimal("11.50") + Decimal("18.35") - Decimal("5")

    def test_totals_keep_gross_and_net_apart(self):
        """They are not interchangeable at tax time, which is the reason for the flag."""
        totals = repo.interest_totals(self.rows()).set_index("tax_year")
        assert totals.loc[2026, "gross"] == Decimal("29.85")
        assert totals.loc[2026, "net"] == Decimal("0.50")
        assert totals.loc[2026, "total"] == Decimal("30.35")

    def test_no_interest_gives_an_empty_frame_with_the_right_columns(self):
        empty = repo.interest_by_tax_year(
            pd.DataFrame(columns=self.TXNS.columns), ACCOUNTS
        )
        assert list(empty.columns) == ["tax_year", "year", "account", "basis", "amount"]
        assert repo.interest_totals(empty).empty


# ------------------------------------------------------------------------ donations


class TestDonations:
    TXNS = pd.DataFrame(
        [
            txn(582, "2026-07-01", "30.00", category="Other", kind="Debit",
                comment="Charity", donation=True),
            # The fee that came with it: same day, same account, same category, not a gift.
            txn(739, "2026-07-01", "5.70", category="Other", kind="Debit",
                comment="transaction fee", donation=False),
            txn(200, "2026-04-02", "10.00", category="Other", kind="Debit",
                comment="Charity", donation=True),
            txn(300, "2027-06-01", "25.00", category="Other", kind="Debit",
                comment="Charity", donation=True, deleted=True),
        ]
    )

    def test_only_flagged_payments_count(self):
        given = repo.donations(self.TXNS)
        assert set(given["id"]) == {582, 200}

    def test_the_fee_is_not_a_donation(self):
        """The whole reason the flag is on the transaction rather than on the category."""
        assert 739 not in set(repo.donations(self.TXNS)["id"])

    def test_a_removed_donation_is_ignored(self):
        assert 300 not in set(repo.donations(self.TXNS)["id"])

    def test_the_second_of_april_falls_in_the_previous_tax_year(self):
        given = repo.donations(self.TXNS).set_index("id")
        assert given.loc[200, "tax_year"] == 2025
        assert given.loc[582, "tax_year"] == 2026

    def test_totals_by_tax_year(self):
        by_year = repo.donations_by_tax_year(self.TXNS).set_index("tax_year")
        assert by_year.loc[2026, "amount"] == Decimal("30.00")
        assert by_year.loc[2026, "count"] == 1
        assert by_year.loc[2025, "amount"] == Decimal("10.00")

    def test_the_split_adds_back_to_what_was_recorded(self):
        """30.00 + 5.70 = 35.70, which is why the month's totals did not move."""
        same_day = self.TXNS[self.TXNS["id"].isin([582, 739])]
        assert same_day["amount"].sum() == Decimal("35.70")

    def test_a_frame_without_the_column_is_handled(self):
        """An old cached frame, or one built by a test that predates the flag."""
        assert repo.donations(self.TXNS.drop(columns="is_donation")).empty


class TestImportingADonation:
    """The flag survives a paste or a CSV, however the column spells 'yes'."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (True, True), ("Y", True), ("y", True), ("yes", True), ("TRUE", True),
            (1, True), ("1", True),
            (False, False), ("N", False), ("", False), (None, False), ("maybe", False),
        ],
    )
    def test_yes_in_its_various_spellings(self, value, expected):
        frame = pd.DataFrame(
            [
                {"Date": "01/07/2026", "Type": "Debit", "Amount": 30.0,
                 "Account From": "HSBC", "Comment": "Charity", "Donation": value}
            ]
        )
        candidates, _ = importer.parse(frame)
        assert candidates[0].is_donation is expected

    def test_the_column_is_optional(self):
        frame = pd.DataFrame(
            [{"Date": "01/07/2026", "Type": "Debit", "Amount": 30.0,
              "Account From": "HSBC"}]
        )
        candidates, problems = importer.parse(frame)
        assert candidates[0].is_donation is False
        assert not problems

    def test_it_is_not_reported_as_an_unrecognised_column(self):
        frame = pd.DataFrame(
            [{"Date": "01/07/2026", "Type": "Debit", "Amount": 30.0,
              "Account From": "HSBC", "Donation": "Y"}]
        )
        _, problems = importer.parse(frame)
        assert not problems

    def test_it_round_trips_through_the_preview(self):
        frame = importer.to_frame([Candidate(comment="Charity", is_donation=True)])
        assert bool(frame["Donation"].iloc[0]) is True


# ---------------------------------------------------------------- the savings plan


class TestSavingsPlan:
    """Effective-dated per account, the way the tracker held it as one column per revision."""

    PLAN = pd.DataFrame(
        [
            {"id": 1, "account_id": 2, "account": "Marcus",
             "effective_from": dt.date(2025, 10, 1), "amount": Decimal("300")},
            {"id": 2, "account_id": 3, "account": "Stocks",
             "effective_from": dt.date(2025, 10, 1), "amount": Decimal("250")},
            {"id": 3, "account_id": 2, "account": "Marcus",
             "effective_from": dt.date(2026, 11, 1), "amount": Decimal("700")},
            {"id": 4, "account_id": 1, "account": "Halifax",
             "effective_from": dt.date(2026, 11, 1), "amount": Decimal("0")},
        ]
    )

    def test_the_dates_are_the_revisions(self):
        assert repo.plan_dates(self.PLAN) == [dt.date(2025, 10, 1), dt.date(2026, 11, 1)]

    def test_nothing_applies_before_the_first_revision(self):
        assert repo.plan_in_force(self.PLAN, dt.date(2025, 1, 1)).empty

    def test_the_latest_set_that_has_started_applies(self):
        in_force = repo.plan_in_force(self.PLAN, dt.date(2026, 6, 1)).set_index("account")
        assert in_force.loc["Marcus", "amount"] == Decimal("300")
        assert in_force.loc["Stocks", "amount"] == Decimal("250")

    def test_a_later_revision_replaces_only_what_it_names(self):
        """Stocks is not restated in November, so its October figure carries on."""
        in_force = repo.plan_in_force(self.PLAN, dt.date(2026, 12, 1)).set_index("account")
        assert in_force.loc["Marcus", "amount"] == Decimal("700")
        assert in_force.loc["Stocks", "amount"] == Decimal("250")

    def test_a_zero_is_stored_rather_than_omitted(self):
        """A pot being wound down needs an explicit nothing: leaving the row out would carry
        the previous figure forward instead."""
        in_force = repo.plan_in_force(self.PLAN, dt.date(2026, 12, 1)).set_index("account")
        assert in_force.loc["Halifax", "amount"] == Decimal("0")

    def test_the_overview_is_the_sum_of_the_breakdown(self):
        periods = ["2026-06", "2026-12"]
        derived = repo.targets_from_plan(self.PLAN, ACCOUNTS, periods).set_index("period")
        # Marcus is savings, Stocks is an investment, Halifax is neither.
        assert derived.loc["2026-06", "savings"] == Decimal("300")
        assert derived.loc["2026-06", "investments"] == Decimal("250")
        assert derived.loc["2026-12", "savings"] == Decimal("700")
        assert derived.loc["2026-12", "investments"] == Decimal("250")

    def test_an_account_that_is_neither_lands_in_neither_total(self):
        detail = repo.plan_by_period(self.PLAN, ACCOUNTS, ["2026-12"])
        assert detail[detail["account"] == "Halifax"]["kind"].iloc[0] == "Other"

    def test_an_empty_plan_gives_no_targets(self):
        empty = pd.DataFrame(
            columns=["id", "account_id", "account", "effective_from", "amount"]
        )
        assert repo.targets_from_plan(empty, ACCOUNTS, ["2026-06"]).empty


# ------------------------------------------------------------- the investment return


class TestInvestmentReturn:
    """`closing = opening + contributions + gain`, where a contribution is a transfer in and
    the gain is the valuation moving."""

    OPENINGS = pd.DataFrame(
        [
            {"account": "Stocks", "period": "2026-04", "opening": Decimal("1000")},
            {"account": "Stocks", "period": "2026-05", "opening": Decimal("1150")},
        ]
    )
    POSTINGS = pd.DataFrame(
        [
            # April: 100 paid in, 50 of growth.
            {"period": "2026-04", "account": "Stocks", "type": "Transfer",
             "column": "credit", "amount": Decimal("100"), "signed": Decimal("100")},
            {"period": "2026-04", "account": "Stocks", "type": "Credit",
             "column": "credit", "amount": Decimal("50"), "signed": Decimal("50")},
            # May: 100 paid in, 20 lost.
            {"period": "2026-05", "account": "Stocks", "type": "Transfer",
             "column": "credit", "amount": Decimal("100"), "signed": Decimal("100")},
            {"period": "2026-05", "account": "Stocks", "type": "Debit",
             "column": "debit", "amount": Decimal("20"), "signed": Decimal("-20")},
        ]
    )
    PERIODS = ["2026-04", "2026-05"]

    def series(self):
        return repo.investment_return_series(
            self.POSTINGS, self.OPENINGS, ACCOUNTS, self.PERIODS
        )

    def test_only_investment_accounts_appear(self):
        assert set(self.series()["account"]) == {"Stocks"}

    def test_contributions_are_the_transfers(self):
        april = self.series().iloc[0]
        assert april["contributions"] == Decimal("100")

    def test_the_gain_is_everything_that_is_not_a_transfer(self):
        rows = self.series()
        assert rows.iloc[0]["gain"] == Decimal("50")
        assert rows.iloc[1]["gain"] == Decimal("-20")

    def test_the_identity_holds(self):
        for _, row in self.series().iterrows():
            assert row["opening"] + row["contributions"] + row["gain"] == row["closing"]

    def test_the_monthly_return_excludes_the_contribution(self):
        """A standing order is not growth. 50 on an opening 1,000 is 5%, not 15%."""
        assert self.series().iloc[0]["monthly_return"] == Decimal("50") / Decimal("1000")

    def test_the_summary_nets_off_what_was_paid_in(self):
        summary = repo.investment_return_summary(
            self.series(), today=dt.date(2026, 6, 30)
        ).set_index("account")
        row = summary.loc["Stocks"]
        assert row["start"] == Decimal("1000")
        assert row["current"] == Decimal("1230")     # 1150 + 100 - 20
        assert row["contributions"] == Decimal("200")
        assert row["net"] == Decimal("1030")         # 1230 - 200
        assert row["total_return"] == Decimal("30") / Decimal("1000")
        assert row["months"] == 2

    def test_the_summary_stops_at_today(self):
        """Measured to now, so a plan that runs ahead of itself does not report returns on
        months that have not happened."""
        summary = repo.investment_return_summary(
            self.series(), today=dt.date(2026, 4, 30)
        ).set_index("account")
        assert summary.loc["Stocks", "months"] == 1
        assert summary.loc["Stocks", "current"] == Decimal("1150")

    def test_annualising_compounds(self):
        summary = repo.investment_return_summary(
            self.series(), today=dt.date(2026, 6, 30)
        ).set_index("account")
        row = summary.loc["Stocks"]
        # Two months of 3% scaled to a year: (1.03)^6 - 1, not 3% x 6.
        assert float(row["annualised"]) == pytest.approx(1.03**6 - 1, rel=1e-9)

    def test_an_empty_series_summarises_to_nothing(self):
        empty = repo.investment_return_series(
            self.POSTINGS.iloc[:0], self.OPENINGS, ACCOUNTS, []
        )
        assert repo.investment_return_summary(empty).empty


class TestMonthlyRate:
    def test_it_compounds_rather_than_divides(self):
        """The plan's `=(1+L4)^(1/12)-1`. Dividing by twelve overstates the month and
        compounds past the rate it started from."""
        monthly = repo.monthly_rate(Decimal("0.06"))
        assert monthly < Decimal("0.06") / 12
        assert float((1 + monthly) ** 12) == pytest.approx(1.06, rel=1e-12)

    def test_it_matches_the_spreadsheet(self):
        assert float(repo.monthly_rate(Decimal("0.06"))) == pytest.approx(
            0.004867550565343048, rel=1e-12
        )

    def test_zero_stays_zero(self):
        assert repo.monthly_rate(Decimal("0")) == Decimal("0")


class TestNegativeAndOneOffTargets:
    """Two things the plan could not previously say: money moving *out* of a pot, and a lump
    sum that happens once rather than every month."""

    PLAN = pd.DataFrame(
        [
            {"id": 1, "account_id": 2, "account": "Marcus",
             "effective_from": dt.date(2026, 3, 1), "amount": Decimal("300")},
            # Moving money out of the wedding pot and into Marcus: the pair nets to nothing.
            {"id": 2, "account_id": 4, "account": "Wedding",
             "effective_from": dt.date(2026, 3, 1), "amount": Decimal("-300")},
            {"id": 3, "account_id": 3, "account": "Stocks",
             "effective_from": dt.date(2026, 3, 1), "amount": Decimal("250")},
        ]
    )
    ACCOUNTS = pd.DataFrame(
        [
            {"id": 2, "name": "Marcus", "type": "bank", "is_savings": True,
             "is_investment": False, "is_isa": False, "exclude_from_savings": False},
            {"id": 3, "name": "Stocks", "type": "bank", "is_savings": False,
             "is_investment": True, "is_isa": False, "exclude_from_savings": False},
            {"id": 4, "name": "Wedding", "type": "bank", "is_savings": True,
             "is_investment": False, "is_isa": False, "exclude_from_savings": True},
        ]
    )

    def test_a_negative_target_is_kept(self):
        in_force = repo.plan_in_force(self.PLAN, dt.date(2026, 6, 1)).set_index("account")
        assert in_force.loc["Wedding", "amount"] == Decimal("-300")

    def test_a_transfer_between_pots_nets_to_nothing(self):
        """Which is right: the month has saved no more than it started with."""
        derived = repo.targets_from_plan(self.PLAN, self.ACCOUNTS, ["2026-06"])
        assert derived.iloc[0]["savings"] == Decimal("0")

    def test_the_buckets_still_show_the_two_halves(self):
        """Netting to zero overall must not hide that one pot is up 300 and another down."""
        buckets = repo.targets_by_bucket(self.PLAN, self.ACCOUNTS, ["2026-06"]).iloc[0]
        assert buckets["available"] == Decimal("300")    # Marcus
        assert buckets["reserved"] == Decimal("-300")    # Wedding, earmarked
        assert buckets["investments"] == Decimal("250")

    def test_buckets_sum_to_the_overview(self):
        periods = ["2026-06"]
        overview = repo.targets_from_plan(self.PLAN, self.ACCOUNTS, periods).iloc[0]
        buckets = repo.targets_by_bucket(self.PLAN, self.ACCOUNTS, periods).iloc[0]
        assert buckets["available"] + buckets["reserved"] == overview["savings"]
        assert buckets["investments"] == overview["investments"]

    # ---- one-offs ----------------------------------------------------------------

    ONE_OFFS = pd.DataFrame(
        [
            {"id": 1, "period": "2026-07", "account_id": 2, "account": "Marcus",
             "amount": Decimal("5000"), "note": "bonus"},
            {"id": 2, "period": "2026-07", "account_id": 4, "account": "Wedding",
             "amount": Decimal("-2000"), "note": "deposit"},
            {"id": 3, "period": "2026-09", "account_id": 3, "account": "Stocks",
             "amount": Decimal("1000"), "note": "top up"},
        ]
    )

    def test_a_one_off_only_moves_its_own_month(self):
        periods = ["2026-06", "2026-07", "2026-08"]
        derived = repo.targets_from_plan(
            self.PLAN, self.ACCOUNTS, periods, self.ONE_OFFS
        ).set_index("period")
        assert derived.loc["2026-06", "savings"] == Decimal("0")
        assert derived.loc["2026-07", "savings"] == Decimal("3000")   # 0 + 5000 - 2000
        assert derived.loc["2026-08", "savings"] == Decimal("0")

    def test_a_one_off_outside_the_periods_is_ignored(self):
        derived = repo.targets_from_plan(
            self.PLAN, self.ACCOUNTS, ["2026-07"], self.ONE_OFFS
        )
        assert derived.iloc[0]["investments"] == Decimal("250")  # September's 1000 excluded

    def test_the_breakdown_says_which_is_which(self):
        """A month whose target looks wrong has to be readable back to the thing that
        moved it, so a one-off is its own row rather than added into the standing figure."""
        detail = repo.plan_by_period(
            self.PLAN, self.ACCOUNTS, ["2026-07"], self.ONE_OFFS
        )
        marcus = detail[detail["account"] == "Marcus"].set_index("source")
        assert marcus.loc["Plan", "amount"] == Decimal("300")
        assert marcus.loc["One-off", "amount"] == Decimal("5000")

    def test_no_adjustments_behaves_as_before(self):
        with_none = repo.targets_from_plan(self.PLAN, self.ACCOUNTS, ["2026-07"], None)
        empty = repo.targets_from_plan(
            self.PLAN, self.ACCOUNTS, ["2026-07"],
            pd.DataFrame(columns=["id", "period", "account_id", "account", "amount", "note"]),
        )
        assert with_none.iloc[0]["savings"] == empty.iloc[0]["savings"] == Decimal("0")


class TestIdleWatchdog:
    """Closing the last tab stops the server, so the console window closes with it."""

    def test_it_is_off_unless_asked_for(self, monkeypatch):
        """`streamlit run` from a terminal must still behave like a server -- and a headless
        test run must not be killed by its own watchdog."""
        from budget import watchdog

        monkeypatch.delenv(watchdog.ENV_FLAG, raising=False)
        assert watchdog.enabled() is False
        assert watchdog.start() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_flag_is_read_generously(self, monkeypatch, value):
        from budget import watchdog

        monkeypatch.setenv(watchdog.ENV_FLAG, value)
        assert watchdog.enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
    def test_anything_else_is_off(self, monkeypatch, value):
        from budget import watchdog

        monkeypatch.setenv(watchdog.ENV_FLAG, value)
        assert watchdog.enabled() is False

    def test_shutting_down_closes_the_database_first(self, monkeypatch):
        """The whole reason this is not a bare os._exit. SQLite runs in WAL mode, so
        committed data sits in budget.db-wal until a checkpoint folds it back in, and that
        happens when the last connection closes. Exiting without closing leaves a WAL that
        outlives its database -- twice the cause of 'disk image is malformed' here."""
        from budget import ui, watchdog

        order = []
        monkeypatch.setattr(ui, "close_connections", lambda: order.append("closed"))
        monkeypatch.setattr(watchdog.os, "_exit", lambda code: order.append(f"exit {code}"))

        watchdog._shut_down()
        assert order == ["closed", "exit 0"]

    def test_it_exits_even_if_closing_fails(self, monkeypatch):
        """A broken engine must not leave the process wedged with nobody watching it."""
        from budget import ui, watchdog

        exited = []

        def boom():
            raise RuntimeError("engine already gone")

        monkeypatch.setattr(ui, "close_connections", boom)
        monkeypatch.setattr(watchdog.os, "_exit", lambda code: exited.append(code))

        watchdog._shut_down()
        assert exited == [0]

    def test_an_unavailable_runtime_reads_as_unknown(self):
        """Outside a Streamlit process there is no runtime to ask. None means 'do not know',
        which the loop treats as somebody being there -- the safe way to be wrong."""
        from budget import watchdog

        assert watchdog._session_count() is None


class TestStaleBuildGuard:
    """A process that started before the code changed runs a new page against an old module,
    and the page dies with a KeyError naming a column but not the cause."""

    def loaded(self, **overrides):
        data = {
            "savings_plan": pd.DataFrame(),
            "plan_detail": pd.DataFrame(),
            "savings_targets": pd.DataFrame(),
            "accounts": pd.DataFrame(columns=["exclude_from_savings", "interest_net"]),
            "transactions": pd.DataFrame(columns=["is_donation"]),
        }
        data.update(overrides)
        return data

    def test_a_current_build_reports_nothing(self):
        from budget import ui

        assert ui._stale_build(self.loaded()) == []

    def test_a_missing_column_is_named(self):
        from budget import ui

        stale = ui._stale_build(
            self.loaded(accounts=pd.DataFrame(columns=["exclude_from_savings"]))
        )
        assert "accounts.interest_net" in stale

    def test_a_missing_key_is_named(self):
        from budget import ui

        data = self.loaded()
        del data["savings_plan"]
        assert "data['savings_plan']" in ui._stale_build(data)

    def test_every_expected_column_is_one_the_loader_actually_returns(self):
        """Guards the guard: a stale entry here would fire on a perfectly current build and
        lock the dashboard out of every page."""
        from budget import repo, ui

        loaders = {
            "accounts": repo.load_reference,
            "transactions": repo.load_transactions,
        }
        assert set(ui.EXPECTED_COLUMNS) <= set(loaders)


class TestInvestmentReturnSetting:
    def test_it_reads_back_as_a_fraction(self):
        assert repo.investment_return_rate({"investment_return_annual": "6.00"}) == (
            Decimal("0.06")
        )

    def test_it_falls_back_to_the_default(self):
        assert repo.investment_return_rate({}) == Decimal("0.06")

    def test_nonsense_falls_back_rather_than_raising(self):
        assert repo.investment_return_rate(
            {"investment_return_annual": "six percent"}
        ) == Decimal("0.06")
