"""Balance-transfer card amortisation, replacing 400 rows of stored formulas."""

import datetime as dt
from decimal import Decimal

from budget import cards

APRIL = dt.date(2026, 4, 1)


def test_minimum_payment_is_a_percentage_of_the_balance():
    rows = cards.schedule(Decimal("1000"), APRIL, term_months=12,
                          min_payment_pct=Decimal("2.5"))
    assert rows[0].opening == Decimal("1000")
    assert rows[0].payment == Decimal("25.00")
    assert rows[0].closing == Decimal("975.00")


def test_the_final_instalment_settles_the_balance():
    """`IF(month = term, balance, min%)` -- the promotional term ends and the rest is due."""
    rows = cards.schedule(Decimal("1000"), APRIL, term_months=3,
                          min_payment_pct=Decimal("2.5"))
    assert len(rows) == 4
    assert rows[-1].payment == rows[-1].opening
    assert rows[-1].closing == Decimal("0")


def test_the_balance_reaches_zero():
    rows = cards.schedule(Decimal("12538.84"), APRIL, term_months=21,
                          min_payment_pct=Decimal("1"))
    assert rows[-1].closing == Decimal("0")


def test_payments_sum_to_the_opening_balance():
    """Nothing is charged beyond the balance: these cards are interest-free for the term,
    which is the point of a balance transfer."""
    opening = Decimal("4468.02")
    rows = cards.schedule(opening, APRIL, term_months=28, min_payment_pct=Decimal("2.5"))
    assert cards.total_payable(rows) == opening


def test_the_first_row_carries_the_opening_date_then_month_ends():
    """The workbook does the same: a payment made during a month shows in the *following*
    row's balance, so its first row is the opening date and the rest are month ends."""
    rows = cards.schedule(Decimal("500"), dt.date(2026, 1, 15), term_months=3,
                          min_payment_pct=Decimal("10"))
    assert [r.date for r in rows] == [
        dt.date(2026, 1, 15), dt.date(2026, 2, 28),
        dt.date(2026, 3, 31), dt.date(2026, 4, 30),
    ]


def test_a_cleared_card_produces_no_schedule():
    assert cards.schedule(Decimal("0"), APRIL, 12, Decimal("2.5")) == []


def test_payoff_date_is_the_last_instalment():
    rows = cards.schedule(Decimal("500"), APRIL, term_months=2, min_payment_pct=Decimal("10"))
    assert cards.payoff_date(rows) == rows[-1].date


class TestBalanceOn:
    """Cards start in different months, so summing opening balances would total figures
    from different points in the year."""

    ROWS = cards.schedule(Decimal("1000"), APRIL, term_months=12,
                          min_payment_pct=Decimal("10"))

    def test_before_the_card_starts_it_is_the_opening_balance(self):
        assert cards.balance_on(self.ROWS, dt.date(2026, 1, 1)) == Decimal("1000")

    def test_on_the_opening_date_nothing_has_been_paid_yet(self):
        assert cards.balance_on(self.ROWS, APRIL) == Decimal("1000")

    def test_the_next_scheduled_date_reflects_one_payment(self):
        # Row 0 is the opening date (1 April); row 1 is the following month end.
        assert cards.balance_on(self.ROWS, dt.date(2026, 5, 31)) == Decimal("900.00")

    def test_between_scheduled_dates_the_balance_holds(self):
        assert cards.balance_on(self.ROWS, dt.date(2026, 4, 20)) == Decimal("1000")

    def test_after_the_term_it_is_zero(self):
        assert cards.balance_on(self.ROWS, dt.date(2030, 1, 1)) == Decimal("0")

    def test_an_empty_schedule_is_zero(self):
        assert cards.balance_on([], dt.date(2026, 4, 1)) == Decimal("0")


def test_a_payment_never_exceeds_the_balance():
    rows = cards.schedule(Decimal("10"), APRIL, term_months=24, min_payment_pct=Decimal("90"))
    assert all(r.payment <= r.opening for r in rows)
    assert rows[-1].closing == Decimal("0")
