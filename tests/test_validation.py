"""The rules the Input tab displayed as advisory text but never enforced."""

import datetime as dt
from decimal import Decimal

import pytest

from budget import service
from budget.validation import Candidate, period_for, validate

JUNE = dt.date(2026, 6, 15)
APRIL = dt.date(2026, 4, 15)


@pytest.fixture
def ref(session):
    return service.load_reference(session)


def candidate(**kw):
    base = dict(
        txn_date=JUNE, type="Debit", amount=Decimal("10.00"),
        account_from="HSBC", category="Food", classification="Food",
    )
    return Candidate(**{**base, **kw})


def test_a_valid_transaction_passes(ref):
    assert validate(candidate(), ref).ok


class TestPeriodIsDerived:
    """The workbook took the month as a separate input, which let a row dated 2029 sit under
    May for years. Deriving it removes the possibility."""

    @pytest.mark.parametrize(
        "date,expected",
        [
            (dt.date(2026, 4, 1), "2026-04"),
            (dt.date(2026, 12, 31), "2026-12"),
            (dt.date(2027, 3, 1), "2027-03"),
        ],
    )
    def test_period_follows_the_date(self, date, expected):
        assert period_for(date) == expected


class TestBasics:
    def test_amount_must_be_positive(self, ref):
        errors = validate(candidate(amount=Decimal("-5")), ref).errors
        assert any("positive" in e for e in errors)

    def test_zero_is_rejected(self, ref):
        assert not validate(candidate(amount=Decimal("0")), ref).ok

    def test_unknown_account_is_rejected(self, ref):
        assert not validate(candidate(account_from="Barclays"), ref).ok

    def test_unknown_type_is_rejected(self, ref):
        assert not validate(candidate(type="Refund"), ref).ok


class TestEffectiveDating:
    """Replaces update_months: adding an account no longer rewrites earlier months, so the
    validator is what keeps a transaction out of a month the account did not exist in."""

    def test_account_cannot_be_used_before_it_opened(self, ref):
        errors = validate(candidate(txn_date=APRIL, account_from="Tembo"), ref).errors
        assert any("not open" in e for e in errors)

    def test_account_can_be_used_after_it_opened(self, ref):
        assert validate(candidate(txn_date=JUNE, account_from="Tembo"), ref).ok

    def test_retired_category_cannot_be_used_afterwards(self, ref):
        c = candidate(txn_date=dt.date(2026, 7, 5), category="Claude")
        assert any("not in use" in e for e in validate(c, ref).errors)

    def test_retired_category_still_works_within_its_span(self, ref):
        assert validate(candidate(category="Claude"), ref).ok


class TestTransfers:
    def test_transfer_needs_a_destination(self, ref):  # Input!E4
        c = candidate(type="Transfer", category=None, classification=None)
        assert any("Account To" in e for e in validate(c, ref).errors)

    def test_transfer_needs_two_different_accounts(self, ref):
        c = candidate(type="Transfer", account_to="HSBC", category=None, classification=None)
        assert any("different" in e for e in validate(c, ref).errors)

    def test_transfer_cannot_have_a_category(self, ref):
        c = candidate(type="Transfer", account_to="Savings", category="Food")
        assert any("category" in e for e in validate(c, ref).errors)

    def test_only_transfers_may_have_a_destination(self, ref):  # Input!E6
        c = candidate(account_to="Savings")
        assert any("Only transfers" in e for e in validate(c, ref).errors)

    def test_a_valid_transfer_passes(self, ref):
        c = candidate(type="Transfer", account_to="Savings", category=None, classification=None)
        assert validate(c, ref).ok


class TestSpendTypeConsistency:
    """Input!E4's 'Update to credit' / 'Update to debit' messages."""

    def test_credit_only_category_rejects_a_debit(self, ref):
        c = candidate(category="Job", type="Debit")
        assert any("only takes Credits" in e for e in validate(c, ref).errors)

    def test_debit_only_category_rejects_a_credit(self, ref):
        c = candidate(category="Food", type="Credit")
        assert any("only takes Debits" in e for e in validate(c, ref).errors)

    def test_an_all_category_takes_either(self, ref):
        assert validate(candidate(category="Other", type="Credit"), ref).ok
        assert validate(candidate(category="Other", type="Debit"), ref).ok


class TestComments:
    def test_category_comment_without_a_category_is_rejected(self, ref):  # Input!E10
        c = candidate(category=None, category_comment="something")
        assert any("without a category" in e for e in validate(c, ref).errors)


class TestWarnings:
    def test_missing_category_warns_but_does_not_block(self, ref):
        result = validate(candidate(category=None), ref)
        assert result.ok
        assert result.warnings

    def test_a_future_date_warns(self, ref):
        future = dt.date.today() + dt.timedelta(days=30)
        result = validate(candidate(txn_date=future), ref)
        assert result.ok
        assert any("future" in w for w in result.warnings)
