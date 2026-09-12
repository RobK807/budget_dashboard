"""Standing targets, the payslip identity, and the balance check's account list.

Three things that were each nearly right. Account targets were stored per month and read per
month, so a month nobody had visited had none. The payslip consistency line compared four of
nine figures. And the balance check listed every account that had ever existed.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import repo

APRIL = dt.date(2026, 4, 1)


def targets(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"period": period, "account_id": account, "amount": Decimal(amount)}
            for period, account, amount in rows
        ],
        columns=["period", "account_id", "amount"],
    )


def accounts(*names) -> pd.DataFrame:
    """Plain bank accounts. A tuple gives (name, opened, closed)."""
    rows = []
    for i, entry in enumerate(names, start=1):
        name, opened, closed = (entry if isinstance(entry, tuple) else (entry, APRIL, None))
        rows.append(
            {
                "id": i, "name": name, "type": "bank", "valid_from": opened,
                "valid_to": closed, "is_savings": False, "is_investment": False,
                "is_isa": False, "exclude_from_savings": False, "savings_seed": None,
            }
        )
    return pd.DataFrame(
        rows,
        columns=["id", "name", "type", "valid_from", "valid_to", "is_savings",
                 "is_investment", "is_isa", "exclude_from_savings", "savings_seed"],
    )


class TestTargetsInForce:
    STORED = targets(
        ("2026-04", 1, "3000"), ("2026-04", 2, "100"),
        ("2026-08", 1, "3950"), ("2026-08", 2, "132.50"),
    )

    def test_a_month_with_its_own_set_uses_it(self):
        mine, source = repo.targets_in_force(self.STORED, "2026-08")
        assert source == "2026-08"
        assert set(mine["amount"]) == {Decimal("3950"), Decimal("132.50")}

    def test_a_later_month_carries_the_last_set_forward(self):
        # The point of the change: September had four blanks on a set nobody had altered.
        mine, source = repo.targets_in_force(self.STORED, "2026-09")
        assert source == "2026-08"
        assert set(mine["amount"]) == {Decimal("3950"), Decimal("132.50")}

    def test_a_month_between_two_sets_uses_the_earlier(self):
        mine, source = repo.targets_in_force(self.STORED, "2026-06")
        assert source == "2026-04"
        assert set(mine["amount"]) == {Decimal("3000"), Decimal("100")}

    def test_a_month_before_the_first_set_has_none(self):
        # Carrying backwards would invent a target for months already closed.
        mine, source = repo.targets_in_force(self.STORED, "2026-03")
        assert mine.empty
        assert source is None

    def test_a_months_set_replaces_rather_than_merges(self):
        """An account dropped from a later set is dropped, not inherited from further back."""
        stored = targets(("2026-04", 1, "10"), ("2026-04", 2, "20"), ("2026-08", 1, "30"))
        mine, _ = repo.targets_in_force(stored, "2026-09")
        assert list(mine["account_id"]) == [1]

    def test_nothing_stored_at_all(self):
        mine, source = repo.targets_in_force(targets(), "2026-09")
        assert mine.empty
        assert source is None

    def test_the_summary_table_shows_a_carried_target(self):
        balances = pd.DataFrame(
            [{"account": "HSBC", "closing": Decimal("500")}],
            columns=["account", "closing"],
        )
        table = repo.account_target_table(
            balances, self.STORED, accounts("HSBC", "First Direct"), "2026-09"
        )
        hsbc = table.set_index("account").loc["HSBC"]
        assert hsbc["target"] == Decimal("3950")
        assert hsbc["required"] == Decimal("3450")


class TestThePayslipAddsUp:
    """gross + car + home working - NI - PAYE - pension - holiday - cycle to work = net.

    Holiday pay is the term that is not obvious: it is holiday *bought*, so it belongs with
    the deductions. Sixteen of the seventeen recorded payslips balance on this to the penny.
    """

    def payslip(self, **kw):
        base = {
            "period": "2026-08", "gross": Decimal("9908.75"),
            "car_allowance": Decimal("787.10"), "additional": Decimal("24"),
            "ni": Decimal("357.86"), "paye": Decimal("3270.46"),
            "benefits": Decimal("990.88"), "holiday_pay": Decimal("187"),
            "cycle_to_work": Decimal("0"), "net": Decimal("5913.65"),
        }
        return pd.Series({**base, **kw})

    def test_a_real_payslip_balances(self):
        assert repo.payslip_balance(self.payslip()) == 0

    def test_holiday_pay_is_a_deduction_not_earnings(self):
        """Treating it as earnings is the mistake that made the old check fire on everything."""
        assert repo.payslip_balance(self.payslip(holiday_pay=Decimal("0"))) == Decimal("-187")

    def test_a_gross_typed_short_is_caught(self):
        # February 2026: gross entered 152.55 low against an unchanged net.
        assert repo.payslip_balance(
            self.payslip(gross=Decimal("9756.20"))
        ) == Decimal("152.55")

    def test_a_missing_figure_counts_as_zero_rather_than_raising(self):
        assert repo.payslip_balance(self.payslip(cycle_to_work=None)) == 0

    def test_the_car_allowance_and_home_working_are_earnings(self):
        assert repo.payslip_balance(
            self.payslip(car_allowance=Decimal("0"))
        ) == Decimal("787.10")
        assert repo.payslip_balance(
            self.payslip(additional=Decimal("0"))
        ) == Decimal("24")

    def test_only_the_rows_that_do_not_balance_are_reported(self):
        frame = pd.DataFrame([self.payslip(), self.payslip(
            period="2026-02", gross=Decimal("9756.20")
        )])
        out = repo.payslips_out_of_balance(frame)
        assert list(out["period"]) == ["2026-02"]
        assert out.iloc[0]["difference"] == Decimal("152.55")
        assert out.iloc[0]["stated"] == Decimal("5913.65")

    def test_a_month_with_no_payslip_yet_is_not_an_error(self):
        """Pension and home working are set ahead of payday, so the row exists before the
        figures do -- reporting it would say the whole of its net was missing."""
        frame = pd.DataFrame([self.payslip(gross=None, net=None)])
        assert repo.payslips_out_of_balance(frame).empty

    def test_nothing_recorded(self):
        out = repo.payslips_out_of_balance(pd.DataFrame())
        assert out.empty
        assert list(out.columns) == ["period", "stated", "computed", "difference"]


class TestTheBalanceCheckAccountList:
    OPEN = accounts(
        "HSBC", ("Old Card", APRIL, dt.date(2026, 5, 31)),
    )

    COLUMNS = ["account", "date", "period", "column", "amount", "signed", "type",
               "account_type"]

    def postings(self, *rows) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "account": a, "date": d, "period": d.strftime("%Y-%m"),
                    "column": "debit", "amount": Decimal(v), "signed": -Decimal(v),
                    "type": "Debit", "account_type": "bank",
                }
                for a, d, v in rows
            ],
            columns=self.COLUMNS,
        )

    OPENINGS = pd.DataFrame(columns=["account", "period", "opening"])

    def check(self, candidates=(), postings=None):
        return repo.import_verification(
            list(candidates),
            self.postings(("HSBC", dt.date(2026, 7, 4), "10"))
            if postings is None else postings,
            self.OPENINGS,
            self.OPEN,
            "2026-09",
        )

    def test_a_closed_account_is_left_off(self):
        assert list(self.check()["account"]) == ["HSBC"]

    def test_the_last_recorded_date_is_the_newest_movement(self):
        postings = self.postings(
            ("HSBC", dt.date(2026, 7, 4), "10"),
            ("HSBC", dt.date(2026, 9, 1), "20"),
            ("HSBC", dt.date(2026, 8, 2), "30"),
        )
        row = self.check(postings=postings).set_index("account").loc["HSBC"]
        assert row["last_seen"] == dt.date(2026, 9, 1)

    def test_an_account_with_no_movements_shows_no_date(self):
        row = self.check(postings=self.postings()).set_index("account").loc["HSBC"]
        assert row["last_seen"] is None

    def test_the_columns_are_stable_when_nothing_qualifies(self):
        empty = repo.import_verification(
            [], self.postings(), self.OPENINGS,
            accounts(("Shut", APRIL, dt.date(2026, 5, 31))), "2026-09",
        )
        assert empty.empty
        assert list(empty.columns) == [
            "account", "current", "in", "out", "projected", "last_seen", "affected"
        ]
