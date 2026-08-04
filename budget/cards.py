"""Balance-transfer card amortisation.

A port of the Balance Transfer Cards schedule, which was 400 rows of stored formulas:

    C5 = C4 - D4                                    balance carries down
    D5 = IF(A5 = term, C5, ROUND(C5 * min_pct, 2))  minimum, or the lot in the final month

Generated from the card's parameters here, so changing a term or a rate is one edit rather
than a re-fill.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PENCE = Decimal("0.01")


@dataclass(frozen=True)
class Instalment:
    month: int
    date: dt.date
    opening: Decimal
    payment: Decimal
    closing: Decimal


def _month_end(start: dt.date, offset: int) -> dt.date:
    import calendar

    month = start.month - 1 + offset
    year = start.year + month // 12
    month = month % 12 + 1
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def schedule(
    opening_balance: Decimal,
    opening_date: dt.date,
    term_months: int,
    min_payment_pct: Decimal,
) -> list[Instalment]:
    """Month-by-month balance and payment until the card clears.

    The final instalment settles whatever is left, which is what the `IF(A5 = term, C5, …)`
    branch does -- the promotional period ends and the balance is due.
    """
    rows: list[Instalment] = []
    balance = Decimal(opening_balance)

    for month in range(term_months + 1):
        if balance <= 0:
            break
        if month == term_months:
            payment = balance
        else:
            payment = (balance * Decimal(min_payment_pct)).quantize(
                PENCE, rounding=ROUND_HALF_UP
            )
            payment = min(payment, balance)

        closing = balance - payment
        rows.append(
            Instalment(
                month=month,
                # The first row carries the opening date itself; from then on, month ends.
                # The workbook does the same -- a payment made during a month shows up in
                # the *following* row's balance, so C5 is the balance after D4 was paid.
                date=opening_date if month == 0 else _month_end(opening_date, month),
                opening=balance,
                payment=payment,
                closing=closing,
            )
        )
        balance = closing

    return rows


def balance_on(rows: list[Instalment], on: dt.date) -> Decimal:
    """Outstanding balance at a date -- the balance *before* that date's payment.

    Matches the workbook's balance column, where each row shows what is owed at that point
    and the payment beside it is then deducted to give the next row.

    Cards start at different times -- Halifax and MBNA 2 open in June while Barclaycard runs
    from April -- so summing opening balances would total figures from different months.
    """
    if not rows:
        return Decimal("0")
    if on < rows[0].date:
        return rows[0].opening
    if on > rows[-1].date:
        return Decimal("0")  # the final instalment has settled it
    latest = [r for r in rows if r.date <= on]
    return latest[-1].opening


def total_payable(rows: list[Instalment]) -> Decimal:
    return sum((r.payment for r in rows), Decimal("0"))


def payoff_date(rows: list[Instalment]) -> dt.date | None:
    return rows[-1].date if rows else None
