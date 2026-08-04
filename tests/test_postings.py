"""The posting sign rules are the easiest thing in this codebase to get silently wrong:
inverting credit cards moves three of twenty-two accounts in the wrong direction, and
nothing else in the application would notice.
"""

from decimal import Decimal

import pytest

from budget.postings import CREDIT, DEBIT, Posting, postings_for

TEN = Decimal("10.00")


def test_credit_posts_to_the_credit_column_of_the_source_account():
    assert postings_for("Credit", "Halifax", None, TEN) == [Posting("Halifax", CREDIT, TEN)]


def test_debit_posts_to_the_debit_column_of_the_source_account():
    assert postings_for("Debit", "Halifax", None, TEN) == [Posting("Halifax", DEBIT, TEN)]


def test_transfer_debits_the_source_and_credits_the_destination():
    # xlDevTransfColOffset1 = 2 (Debit) for the 'from' side, offset2 = 1 (Credit) for 'to'.
    assert postings_for("Transfer", "Halifax", "HSBC", TEN) == [
        Posting("Halifax", DEBIT, TEN),
        Posting("HSBC", CREDIT, TEN),
    ]


def test_transfer_without_destination_is_rejected():
    with pytest.raises(ValueError, match="destination"):
        postings_for("Transfer", "Halifax", None, TEN)


def test_unknown_type_is_rejected():
    with pytest.raises(ValueError, match="unknown transaction type"):
        postings_for("Refund", "Halifax", None, TEN)


class TestSigns:
    """bank        = start + credit - debit
    credit card = start - credit + debit   (balance is positive debt)"""

    def test_bank_credit_increases_balance(self):
        assert Posting("Halifax", CREDIT, TEN).signed("bank") == TEN

    def test_bank_debit_decreases_balance(self):
        assert Posting("Halifax", DEBIT, TEN).signed("bank") == -TEN

    def test_card_debit_increases_debt(self):
        assert Posting("BA Amex", DEBIT, TEN).signed("credit_card") == TEN

    def test_card_credit_reduces_debt(self):
        assert Posting("BA Amex", CREDIT, TEN).signed("credit_card") == -TEN


def test_transfer_from_bank_to_card_moves_both_balances_down():
    """Paying a card off from a current account: the bank falls and the debt falls too."""
    from_p, to_p = postings_for("Transfer", "Halifax", "BA Amex", TEN)
    assert from_p.signed("bank") == -TEN
    assert to_p.signed("credit_card") == -TEN
