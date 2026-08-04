"""Explodes transactions into signed per-account movements.

Two rules, both taken from the workbook rather than assumed.

*Which column a transaction lands in*, from New_entry: a Credit posts to the account's
Credit column and a Debit to its Debit column (Input!D4 matches the type against
Monthly_Template!I3:J3). A Transfer posts twice -- xlDevTransfColOffset1 = 2 puts the
'from' side in the Debit column and xlDevTransfColOffset2 = 1 puts the 'to' side in Credit.

*What sign that column carries*, from the month-tab row-4 formulas:

    bank         = <start> + <credit> - <debit>
    credit card  = <start> - <credit> + <debit>

A credit-card balance is therefore positive debt: spending on the card increases it and
paying it off reduces it. Getting this backwards silently inverts three of the twenty-two
accounts, which is why it is derived from the formula on import rather than from the name.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

CREDIT = "credit"
DEBIT = "debit"


@dataclass(frozen=True)
class Posting:
    account: str
    column: str  # CREDIT | DEBIT
    amount: Decimal

    def signed(self, account_type: str) -> Decimal:
        """Movement applied to the account's running balance."""
        if account_type == "credit_card":
            return -self.amount if self.column == CREDIT else self.amount
        return self.amount if self.column == CREDIT else -self.amount


def postings_for(txn_type: str, account_from: str, account_to: str | None, amount: Decimal
                 ) -> list[Posting]:
    if txn_type == "Transfer":
        if not account_to:
            raise ValueError("transfer without a destination account")
        return [
            Posting(account_from, DEBIT, amount),
            Posting(account_to, CREDIT, amount),
        ]
    if txn_type == "Credit":
        return [Posting(account_from, CREDIT, amount)]
    if txn_type == "Debit":
        return [Posting(account_from, DEBIT, amount)]
    raise ValueError(f"unknown transaction type {txn_type!r}")
