"""Validation rules for a transaction.

Mostly a port of the checks the workbook showed in Input!E4:E10 -- "Include account to",
"Update to credit", "Delete comment" and so on -- which were advisory text in a cell that
nothing enforced. Here they are enforced before a row reaches the database.

One rule is new. The workbook asked for the month separately from the date, so a row could
be filed under May while dated 2029; that is how three bad dates survived in the ledger.
Here the period is *derived* from the date, so the class of error cannot occur.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

TRANSFER = "Transfer"
CREDIT = "Credit"
DEBIT = "Debit"
TYPES = (CREDIT, DEBIT, TRANSFER)


def period_for(date: dt.date) -> str:
    """The period a transaction belongs to, derived rather than chosen."""
    return f"{date.year:04d}-{date.month:02d}"


@dataclass
class Candidate:
    """A proposed transaction, before it is known to be valid."""

    txn_date: dt.date | None = None
    type: str | None = None
    amount: Decimal | None = None
    account_from: str | None = None
    account_to: str | None = None
    category: str | None = None
    classification: str | None = None
    comment: str | None = None
    category_comment: str | None = None
    source_row: int | None = None

    @property
    def period(self) -> str | None:
        return period_for(self.txn_date) if self.txn_date else None


@dataclass
class Result:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok


@dataclass
class Reference:
    """What the validator needs to know about the configured accounts and lists."""

    accounts: dict[str, dict]  # name -> {valid_from, valid_to, ...}
    categories: dict[str, dict]  # name -> {spend_type, valid_from, valid_to}
    classifications: set[str]

    def active(self, table: dict[str, dict], name: str, on: dt.date) -> bool:
        row = table.get(name)
        if row is None:
            return False
        if row.get("valid_from") and on < row["valid_from"]:
            return False
        if row.get("valid_to") and on > row["valid_to"]:
            return False
        return True


def to_decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("£", "").strip())
    except (InvalidOperation, ValueError):
        return None


def validate(c: Candidate, ref: Reference) -> Result:
    r = Result()

    # --- the basics -------------------------------------------------------------------
    if c.txn_date is None:
        r.errors.append("Date is required")
    if c.type not in TYPES:
        r.errors.append(f"Type must be one of {', '.join(TYPES)}")
    if c.amount is None:
        r.errors.append("Amount is required")
    elif c.amount <= 0:
        # Direction is carried by the type, so a negative amount is ambiguous rather than
        # merely odd -- a -£10 Debit could plausibly mean either direction.
        r.errors.append("Amount must be positive; use Credit or Debit to set direction")
    if not c.account_from:
        r.errors.append("Account From is required")

    if r.errors:
        return r  # later rules assume these hold

    # --- accounts ---------------------------------------------------------------------
    if c.account_from not in ref.accounts:
        r.errors.append(f"Unknown account {c.account_from!r}")
    elif not ref.active(ref.accounts, c.account_from, c.txn_date):
        r.errors.append(f"{c.account_from} was not open on {c.txn_date}")

    # --- transfer vs everything else (Input!E4 and E6) ---------------------------------
    if c.type == TRANSFER:
        if not c.account_to:
            r.errors.append("A transfer needs an Account To")
        elif c.account_to not in ref.accounts:
            r.errors.append(f"Unknown account {c.account_to!r}")
        elif not ref.active(ref.accounts, c.account_to, c.txn_date):
            r.errors.append(f"{c.account_to} was not open on {c.txn_date}")
        elif c.account_to == c.account_from:
            r.errors.append("A transfer needs two different accounts")
        if c.category:
            r.errors.append("A transfer cannot have a category")
        if c.comment:
            r.warnings.append("Comments on transfers are not recorded against a category")
    elif c.account_to:
        r.errors.append("Only transfers may have an Account To")

    # --- category and classification ---------------------------------------------------
    if c.category:
        if c.category not in ref.categories:
            r.errors.append(f"Unknown category {c.category!r}")
        elif not ref.active(ref.categories, c.category, c.txn_date):
            r.errors.append(f"Category {c.category!r} was not in use on {c.txn_date}")
        else:
            # Input!E4: a Credit-only category cannot take a Debit, and vice versa.
            spend_type = ref.categories[c.category].get("spend_type")
            if spend_type == CREDIT and c.type == DEBIT:
                r.errors.append(f"{c.category!r} only takes Credits — change the type")
            elif spend_type == DEBIT and c.type == CREDIT:
                r.errors.append(f"{c.category!r} only takes Debits — change the type")
    elif c.category_comment:
        r.errors.append("Category comment given without a category")  # Input!E10
    elif c.type != TRANSFER:
        r.warnings.append("No category: this will not appear in the budget breakdown")

    if c.classification:
        if c.classification not in ref.classifications:
            r.errors.append(f"Unknown classification {c.classification!r}")
    elif c.type != TRANSFER:
        r.warnings.append("No purchase type: this will not appear in classification totals")

    # --- sanity -----------------------------------------------------------------------
    if c.txn_date > dt.date.today():
        r.warnings.append(f"{c.txn_date} is in the future")
    elif (dt.date.today() - c.txn_date).days > 365:
        r.warnings.append(f"{c.txn_date} is more than a year ago")

    return r
