"""The balance check from BulkImport!CK3:CQ32.

The workbook wrote it as SUMIFS pairs and negated them for the three credit-card rows,
because a card balance is positive debt. Getting that backwards would make the check pass
on exactly the accounts it most needs to catch.
"""

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

from budget import repo
from budget.validation import Candidate

ACCOUNTS = pd.DataFrame(
    [
        {"name": "HSBC", "type": "bank", "is_savings": False, "is_investment": False,
         "is_isa": False},
        {"name": "Savings", "type": "bank", "is_savings": True, "is_investment": False,
         "is_isa": False},
        {"name": "BA Amex", "type": "credit_card", "is_savings": False,
         "is_investment": False, "is_isa": False},
    ]
)

JUNE = dt.date(2026, 6, 15)


def candidate(**kw):
    base = dict(txn_date=JUNE, type="Debit", amount=Decimal("10.00"), account_from="HSBC")
    return Candidate(**{**base, **kw})


def impact_for(*candidates):
    return repo.candidate_impact(list(candidates), ACCOUNTS).set_index("account")


class TestBankAccounts:
    def test_a_debit_reduces_the_balance(self):
        assert impact_for(candidate()).loc["HSBC", "net"] == Decimal("-10.00")

    def test_a_credit_increases_the_balance(self):
        row = impact_for(candidate(type="Credit")).loc["HSBC"]
        assert row["net"] == Decimal("10.00")
        assert row["in"] == Decimal("10.00")


class TestCreditCards:
    """A card balance is debt owed: spending increases it, paying it off reduces it."""

    def test_spending_on_a_card_increases_the_balance_owed(self):
        row = impact_for(candidate(account_from="BA Amex")).loc["BA Amex"]
        assert row["net"] == Decimal("10.00")
        # Money left the user, so it reads as 'out' even though the balance rose.
        assert row["out"] == Decimal("10.00")

    def test_paying_a_card_off_reduces_the_balance_owed(self):
        row = impact_for(candidate(account_from="BA Amex", type="Credit")).loc["BA Amex"]
        assert row["net"] == Decimal("-10.00")

    def test_a_transfer_from_bank_to_card_moves_both_down(self):
        impact = impact_for(
            candidate(type="Transfer", account_from="HSBC", account_to="BA Amex")
        )
        assert impact.loc["HSBC", "net"] == Decimal("-10.00")
        assert impact.loc["BA Amex", "net"] == Decimal("-10.00")


class TestTransfers:
    def test_a_transfer_nets_to_zero_across_two_bank_accounts(self):
        impact = impact_for(
            candidate(type="Transfer", account_from="HSBC", account_to="Savings")
        )
        assert impact.loc["HSBC", "net"] == Decimal("-10.00")
        assert impact.loc["Savings", "net"] == Decimal("10.00")
        assert impact["net"].sum() == Decimal("0")


class TestRobustness:
    def test_incomplete_rows_are_skipped_rather_than_raising(self):
        # Validation reports these separately; the check must still render.
        impact = impact_for(
            candidate(type="Transfer", account_to=None),  # no destination
            candidate(amount=None),
            candidate(account_from=None),
            candidate(),
        )
        assert impact.loc["HSBC", "net"] == Decimal("-10.00")

    def test_unknown_accounts_are_ignored(self):
        impact = impact_for(candidate(account_from="Barclays"))
        assert impact["net"].sum() == Decimal("0")


class TestTargetPersistence:
    """st.data_editor keys edits by row position, so filtering the balance-check table
    would otherwise wipe what was typed. Targets live per account instead."""

    def test_a_value_survives_the_table_being_filtered_and_restored(self):
        from budget import importer

        stored: dict[str, float] = {}
        everything = ["BA Amex", "HSBC", "Savings"]

        # Full table: a target is entered for Savings, which this import does not touch.
        importer.capture_targets(
            zip(everything, [pd.NA, pd.NA, 900.0]), stored
        )
        assert stored == {"Savings": 900.0}

        # Filtered to affected accounts only -- Savings is off screen and untouched.
        affected = ["BA Amex", "HSBC"]
        importer.capture_targets(zip(affected, [1578.27, pd.NA]), stored)
        assert stored == {"Savings": 900.0, "BA Amex": 1578.27}

        # Back to the full table: both reappear.
        assert importer.seed_targets(everything, stored) == [1578.27, pd.NA, 900.0]

    def test_a_value_entered_while_filtered_survives_expanding(self):
        from budget import importer

        stored: dict[str, float] = {}
        importer.capture_targets(zip(["HSBC"], [1930.0]), stored)
        assert importer.seed_targets(["BA Amex", "HSBC", "Savings"], stored) == [
            pd.NA, 1930.0, pd.NA
        ]

    def test_clearing_a_cell_removes_the_target(self):
        from budget import importer

        stored = {"HSBC": 100.0}
        importer.capture_targets(zip(["HSBC"], [pd.NA]), stored)
        assert stored == {}

    def test_none_is_treated_as_cleared(self):
        from budget import importer

        stored = {"HSBC": 100.0}
        importer.capture_targets(zip(["HSBC"], [None]), stored)
        assert stored == {}


class TestProjection:
    def test_projected_is_current_plus_net(self):
        openings = pd.DataFrame(
            [{"account": "HSBC", "period": "2026-06", "opening": Decimal("500")}]
        )
        postings = pd.DataFrame(
            columns=["txn_id", "date", "period", "account", "account_type", "column",
                     "amount", "signed", "type", "category", "classification", "direction",
                     "comment", "deleted"]
        )
        out = repo.import_verification(
            [candidate()], postings, openings, ACCOUNTS, "2026-06"
        ).set_index("account")

        assert out.loc["HSBC", "current"] == Decimal("500")
        assert out.loc["HSBC", "projected"] == Decimal("490")
        assert out.loc["HSBC", "affected"]
        assert not out.loc["Savings", "affected"]
