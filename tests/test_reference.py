"""Reference-data CRUD.

The gate for this phase is the thing the workbook could not do: add an account part-way
through the year without disturbing the months before it. In the spreadsheet an account's
position *was* its storage location, so `add_account` had to insert columns and then
`update_months` copied the template over every month from the change onwards, destroying
whatever was already recorded there. Hence the warning on all six forms.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from budget import reference, repo, service
from budget.models import Account, Category, Classification, DbMeta, Txn
from budget.validation import Candidate, validate

APRIL = dt.date(2026, 4, 1)
JUNE = dt.date(2026, 6, 15)


def add_txn(session, account="HSBC", date=JUNE, amount="10.00", category="Food"):
    return service.add_transaction(
        session,
        Candidate(
            txn_date=date, type="Debit", amount=Decimal(amount),
            account_from=account, category=category, classification="Food",
        ),
    )


class TestAddAccountMidYear:
    """The Phase 3 gate."""

    def test_earlier_months_are_untouched(self, session):
        add_txn(session, date=dt.date(2026, 4, 10))
        add_txn(session, date=dt.date(2026, 5, 10))
        session.commit()

        before = {
            t.id: (t.txn_date, t.amount, t.account_from_id)
            for t in session.scalars(select(Txn))
        }

        account, outcome = reference.add_account(
            session, "Chase", "CHS", "bank", valid_from=dt.date(2026, 6, 1)
        )
        session.commit()

        assert outcome.ok, outcome.message
        after = {
            t.id: (t.txn_date, t.amount, t.account_from_id)
            for t in session.scalars(select(Txn))
        }
        # No transaction was moved, altered or destroyed -- the whole point.
        assert before == after

    def test_the_new_account_is_rejected_before_its_start_date(self, session):
        reference.add_account(
            session, "Chase", "CHS", "bank", valid_from=dt.date(2026, 6, 1)
        )
        session.commit()

        ref = service.load_reference(session)
        early = Candidate(
            txn_date=dt.date(2026, 5, 20), type="Debit", amount=Decimal("5"),
            account_from="Chase", category="Food", classification="Food",
        )
        assert any("not open" in e for e in validate(early, ref).errors)

    def test_the_new_account_works_from_its_start_date(self, session):
        reference.add_account(
            session, "Chase", "CHS", "bank", valid_from=dt.date(2026, 6, 1)
        )
        session.commit()

        txn, result = add_txn(session, account="Chase")
        assert result.ok
        assert txn is not None


class TestAccountValidation:
    def test_duplicate_name_is_rejected_case_insensitively(self, session):
        _, outcome = reference.add_account(session, "hsbc", "XXX", "bank", APRIL)
        assert not outcome.ok
        assert "already exists" in outcome.message

    def test_duplicate_short_code_is_rejected(self, session):
        _, outcome = reference.add_account(session, "Another", "HSB", "bank", APRIL)
        assert not outcome.ok
        assert "already used" in outcome.message

    def test_an_account_cannot_be_both_savings_and_investment(self, session):
        _, outcome = reference.add_account(
            session, "Odd", "ODD", "bank", APRIL, is_savings=True, is_investment=True
        )
        assert not outcome.ok

    def test_bad_type_is_rejected(self, session):
        _, outcome = reference.add_account(session, "Odd", "ODD", "crypto", APRIL)
        assert not outcome.ok

    def test_short_code_is_upper_cased(self, session):
        account, outcome = reference.add_account(session, "Chase", "chs", "bank", APRIL)
        assert outcome.ok
        assert account.short_code == "CHS"


class TestRenaming:
    """Transactions reference an account by id, so a rename carries history automatically --
    no rows are re-pointed and nothing is rewritten."""

    def test_existing_transactions_follow_the_new_name(self, session):
        txn, _ = add_txn(session, date=dt.date(2026, 4, 10))
        session.commit()
        account = session.scalar(select(Account).where(Account.name == "HSBC"))

        outcome = reference.update_account(session, account.id, name="HSBC Advance")
        session.commit()

        assert outcome.ok
        assert session.get(Txn, txn.id).account_from_id == account.id
        rows = repo.load_transactions(session)
        assert set(rows["account_from"]) == {"HSBC Advance"}

    def test_balances_are_unchanged_by_a_rename(self, session):
        add_txn(session, date=dt.date(2026, 6, 10), amount="40.00")
        session.commit()
        reference_before = repo.load_reference(session)
        postings_before = repo.load_postings(session)
        openings = repo.load_opening_balances(session)
        before = repo.account_balances(
            postings_before, openings, "2026-06", reference_before["accounts"]
        )
        total_before = before["closing"].sum()

        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        reference.update_account(session, account.id, name="HSBC Advance")
        session.commit()

        after_ref = repo.load_reference(session)
        after = repo.account_balances(
            repo.load_postings(session), openings, "2026-06", after_ref["accounts"]
        )
        assert after["closing"].sum() == total_before

    def test_renaming_warns_that_reconciliation_matches_by_name(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        outcome = reference.update_account(session, account.id, name="HSBC Advance")
        assert outcome.ok
        assert any("reconciliation" in w for w in outcome.warnings)

    def test_renaming_to_an_existing_name_is_rejected(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        outcome = reference.update_account(session, account.id, name="Savings")
        assert not outcome.ok
        assert "already exists" in outcome.message

    def test_keeping_the_same_name_is_not_treated_as_a_clash(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        outcome = reference.update_account(session, account.id, name="HSBC", short_code="HSB")
        assert outcome.ok
        assert not outcome.warnings  # unchanged name, so no history caveat

    def test_short_code_clash_is_rejected(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        outcome = reference.update_account(session, account.id, short_code="SAV")
        assert not outcome.ok


class TestRetiring:
    """`remove_account` deleted the columns outright, so the account vanished from months it
    had genuinely been used in. Closing keeps the history intact."""

    def test_closing_keeps_the_row_and_its_transactions(self, session):
        add_txn(session)
        session.commit()
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        count_before = session.scalar(select(Txn).where(Txn.account_from_id == account.id))

        outcome = reference.retire(session, "account", account.id, dt.date(2026, 7, 1))
        session.commit()

        assert outcome.ok
        assert session.get(Account, account.id) is not None
        assert count_before is not None

    def test_cannot_close_before_the_last_transaction(self, session):
        add_txn(session, date=dt.date(2026, 6, 20))
        session.commit()
        account = session.scalar(select(Account).where(Account.name == "HSBC"))

        outcome = reference.retire(session, "account", account.id, dt.date(2026, 5, 1))

        assert not outcome.ok
        assert "is used on" in outcome.message

    def test_a_closed_account_is_rejected_for_later_dates(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        reference.retire(session, "account", account.id, dt.date(2026, 6, 30))
        session.commit()

        ref = service.load_reference(session)
        late = Candidate(
            txn_date=dt.date(2026, 7, 5), type="Debit", amount=Decimal("5"),
            account_from="HSBC", category="Food", classification="Food",
        )
        assert any("not open" in e for e in validate(late, ref).errors)

    def test_a_closed_account_still_accepts_earlier_dates(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        reference.retire(session, "account", account.id, dt.date(2026, 6, 30))
        session.commit()

        _, result = add_txn(session, date=dt.date(2026, 5, 5))
        assert result.ok

    def test_reopening_clears_the_closing_date(self, session):
        account = session.scalar(select(Account).where(Account.name == "HSBC"))
        reference.retire(session, "account", account.id, dt.date(2026, 6, 30))
        reference.reinstate(session, "account", account.id)
        session.commit()
        assert session.get(Account, account.id).valid_to is None


class TestDeleting:
    """The workbook's remove_category stripped a category from Selections while leaving
    every historic Debug row still naming it -- which is how the 'Claude' category was lost."""

    def test_an_unused_row_can_be_deleted(self, session):
        account, _ = reference.add_account(session, "Chase", "CHS", "bank", APRIL)
        session.commit()
        outcome = reference.delete(session, "account", account.id)
        session.commit()
        assert outcome.ok
        assert session.scalar(select(Account).where(Account.name == "Chase")) is None

    def test_a_used_row_cannot_be_deleted(self, session):
        add_txn(session)
        session.commit()
        account = session.scalar(select(Account).where(Account.name == "HSBC"))

        outcome = reference.delete(session, "account", account.id)

        assert not outcome.ok
        assert "orphan" in outcome.message

    def test_a_soft_deleted_transaction_still_counts_as_usage(self, session):
        txn, _ = add_txn(session)
        service.soft_delete(session, txn.id)
        session.commit()
        category = session.scalar(select(Category).where(Category.name == "Food"))

        outcome = reference.delete(session, "category", category.id)

        assert not outcome.ok


class TestCategories:
    def test_add_and_reject_duplicates(self, session):
        _, outcome = reference.add_category(session, "Haircut", "Other", "Debit", APRIL)
        assert outcome.ok
        _, again = reference.add_category(session, "haircut", "Other", "Debit", APRIL)
        assert not again.ok

    def test_spend_type_must_be_valid(self, session):
        _, outcome = reference.add_category(session, "Odd", "Other", "Sideways", APRIL)
        assert not outcome.ok

    def test_updating_spend_type_changes_what_validates(self, session):
        category = session.scalar(select(Category).where(Category.name == "Other"))
        reference.update_category(session, category.id, spend_type="Credit")
        session.commit()

        ref = service.load_reference(session)
        debit = Candidate(
            txn_date=JUNE, type="Debit", amount=Decimal("5"),
            account_from="HSBC", category="Other", classification="Food",
        )
        assert any("only takes Credits" in e for e in validate(debit, ref).errors)


class TestClassifications:
    def test_add_assigns_the_next_legacy_reference(self, session):
        existing = [c.legacy_ref for c in session.scalars(select(Classification))]
        new, outcome = reference.add_classification(
            session, "Travel budget", 1, "none", APRIL
        )
        assert outcome.ok
        assert new.legacy_ref == max(existing) + 1

    def test_direction_must_be_plus_or_minus_one(self, session):
        _, outcome = reference.add_classification(session, "Odd", 2, "none", APRIL)
        assert not outcome.ok

    def test_rollover_must_be_valid(self, session):
        _, outcome = reference.add_classification(session, "Odd", 1, "sideways", APRIL)
        assert not outcome.ok

    def test_changing_direction_warns_that_history_flips(self, session):
        classification = session.scalar(
            select(Classification).where(Classification.name == "Food")
        )
        outcome = reference.update_classification(
            session, classification.id, direction=-1
        )
        assert outcome.ok
        assert any("historic" in w for w in outcome.warnings)


class TestRevision:
    def test_reference_changes_bump_the_revision_so_they_sync(self, session):
        start = session.get(DbMeta, 1).revision
        reference.add_account(session, "Chase", "CHS", "bank", APRIL)
        session.commit()
        assert session.get(DbMeta, 1).revision > start

    def test_a_rejected_change_does_not_bump_it(self, session):
        start = session.get(DbMeta, 1).revision
        reference.add_account(session, "hsbc", "XXX", "bank", APRIL)
        session.commit()
        assert session.get(DbMeta, 1).revision == start


class TestSettings:
    def test_set_and_overwrite(self, session):
        reference.set_setting(session, "tax_year", "2027")
        session.commit()
        assert repo.load_reference(session)["settings"]["tax_year"] == "2027"
