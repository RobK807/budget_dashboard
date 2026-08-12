"""Reading a bank's own CSV export.

Every bank exports a different shape and none of them is the shape this application wants.
The differences are not cosmetic:

* **Where the data starts.** Nationwide, Virgin Money and Coventry all print a block of
  account details first. Virgin then prints an address and six paragraphs of small print
  *after* the transactions, so the end has to be found as carefully as the beginning.
* **How the amount is carried.** Some give one signed column; Halifax, Nationwide, Virgin
  and Coventry give a pair. Virgin's pair is (out, in) and Coventry's is (in, out) -- the
  same two headings in the opposite order, which is the sort of thing that reads correctly,
  imports silently, and puts every payment on the wrong side of the ledger.
* **What a positive number means.** On the HSBC family a purchase is negative, because the
  account is losing money. On Amex a purchase is *positive*, because the statement is
  counting what you owe. Reversing that inverts every card transaction, so the sign
  convention is stated per format rather than inferred from the numbers.
* **How the date is written.** `03/08/2026`, `15 Jul 2026` and `20260805` all appear.

So each format is a small declaration -- where the header is, which column is which, and
which way is out -- and one reader walks them all. Everything ends up in the same normalised
frame: a date, a description, a positive amount, and a direction of `out` or `in`.

Nothing here knows about accounts, duplicates or transfers. This module turns a file into
rows; `budget/bank_import.py` decides what they mean.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pandas as pd

OUT = "out"
IN = "in"

# Columns every reader produces, whatever it started from.
NORMALISED = ("row", "date", "description", "amount", "direction")


# --------------------------------------------------------------------------- parsing bits


def _clean(cell: str | None) -> str:
    """A cell as text: unquoted, unpadded, and without the pound sign banks like to include."""
    if cell is None:
        return ""
    return str(cell).replace("﻿", "").strip().strip('"').strip()


def money(cell: str | None) -> Decimal | None:
    """A figure from a cell, or None where the cell is empty.

    Empty is the normal state of half a debit/credit pair, so it is a value rather than a
    fault. Thousands separators and pound signs are stripped; parentheses are read as
    negative, which is how some exports mark a credit.
    """
    text = _clean(cell).replace("£", "").replace(",", "").replace("\xa0", "")
    if not text or text in ("-", "--"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value.is_nan():
        return None
    return -value if negative else value


# `15 Jul 2026` and `20260805` are unambiguous and have to be read on their own terms;
# everything else here is UK day-first.
_COMPACT = re.compile(r"^\d{8}$")


def read_date(cell: str | None) -> dt.date | None:
    text = _clean(cell)
    if not text:
        return None
    if _COMPACT.match(text):
        try:
            return dt.datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None
    parsed = pd.to_datetime(text, dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _normalise_heading(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean(name).lower())


# ------------------------------------------------------------------------- the declaration


@dataclass(frozen=True)
class BankFormat:
    """One bank's export, described rather than coded.

    `header` names the columns that must all be present for the row to be the header row --
    which is also what identifies the format, since no two of these share a full set.
    `headerless` formats have no such row and are recognised by their shape instead.
    """

    key: str
    label: str
    # Which accounts export in this shape. Used to guess, and to say so on screen.
    accounts: tuple[str, ...] = ()
    header: tuple[str, ...] = ()
    date: str = "date"
    description: str = "description"
    # One signed column ...
    amount: str | None = None
    # ... or a pair. `out_column` is money leaving the account.
    out_column: str | None = None
    in_column: str | None = None
    # For a single signed column: which sign means money out. Amex states what you owe, so
    # a purchase is positive there and negative everywhere else.
    out_is_negative: bool = True
    headerless: bool = False
    # Positional layout, for the formats that have no header at all.
    positions: tuple[int, ...] = ()
    notes: str = ""
    # Where in the file the account identifies itself, if it does: a heading whose adjacent
    # cell names or numbers the account. Only used to guess which account a file belongs to.
    identifiers: tuple[str, ...] = field(default=())

    @property
    def paired_amounts(self) -> bool:
        return self.out_column is not None


FORMATS: tuple[BankFormat, ...] = (
    BankFormat(
        key="amex",
        label="American Express",
        accounts=("BA Amex", "Platinum Amex"),
        header=("date", "description", "amount"),
        amount="amount",
        # The one format where a purchase is positive: a card statement counts what is owed,
        # not what the account holds.
        out_is_negative=False,
        notes="Purchases positive, payments and refunds negative.",
    ),
    BankFormat(
        key="first_direct",
        label="First Direct",
        accounts=("First Direct", "Savings - First Direct"),
        header=("date", "description", "amount", "balance"),
        amount="amount",
        notes="One signed amount, with a running balance beside it.",
    ),
    BankFormat(
        key="halifax",
        label="Halifax",
        accounts=("Halifax",),
        header=(
            "transactiondate", "transactiontype", "transactiondescription",
            "debitamount", "creditamount",
        ),
        date="transactiondate",
        description="transactiondescription",
        out_column="debitamount",
        in_column="creditamount",
        notes="Separate debit and credit columns, with the sort code and account number.",
    ),
    BankFormat(
        key="nationwide",
        label="Nationwide",
        accounts=("Nationwide", "Savings - Nationwide", "Savings - Nationwide Reg"),
        header=("date", "transactiontype", "description", "paidout", "paidin"),
        out_column="paidout",
        in_column="paidin",
        notes="Account details above the data, pound signs in the figures, dates as 15 Jul 2026.",
        identifiers=("accountname",),
    ),
    BankFormat(
        key="marcus",
        label="Marcus",
        accounts=("Savings - Marcus", "Savings - Wedding"),
        header=("transactiondate", "description", "value", "accountbalance", "accountname"),
        date="transactiondate",
        amount="value",
        notes="Dates as 20260805, one signed value.",
        identifiers=("accountname", "accountnumber"),
    ),
    BankFormat(
        key="virgin",
        label="Virgin Money",
        accounts=("Savings - Spending",),
        header=("date", "details", "moneyout", "moneyin", "balance"),
        description="details",
        out_column="moneyout",
        in_column="moneyin",
        notes="Account details above the data and small print below it.",
        identifiers=("accountnumber", "sortcode"),
    ),
    BankFormat(
        key="coventry",
        label="Coventry Building Society",
        accounts=("Savings - Service Charge",),
        # Same two headings as Virgin Money in the opposite order. Read by name, never by
        # position: taking these as (out, in) would reverse every row in the file.
        header=("date", "description", "moneyin", "moneyout", "balance"),
        out_column="moneyout",
        in_column="moneyin",
        notes="Money in before money out — the reverse of Virgin Money's column order.",
    ),
    BankFormat(
        key="hsbc",
        label="HSBC",
        accounts=("HSBC", "Mastercard", "ISA"),
        headerless=True,
        positions=(0, 1, 2),
        notes="No header row: date, description, signed amount.",
    ),
)

BY_KEY = {f.key: f for f in FORMATS}


def format_for(key: str) -> BankFormat | None:
    return BY_KEY.get(key)


# ------------------------------------------------------------------------------- detection


def _rows(text: str) -> list[list[str]]:
    """The file as a list of cells per line, quoting and embedded commas handled."""
    return list(csv.reader(io.StringIO(text.replace("﻿", ""))))


def _header_at(row: list[str], fmt: BankFormat) -> bool:
    headings = {_normalise_heading(cell) for cell in row if _clean(cell)}
    return bool(fmt.header) and set(fmt.header).issubset(headings)


def _looks_like_data(row: list[str]) -> bool:
    """A headerless row: a date, then something, then a figure."""
    return (
        len(row) >= 3
        and read_date(row[0]) is not None
        and money(row[-1]) is not None
    )


def detect(text: str) -> tuple[BankFormat | None, list[BankFormat]]:
    """Which format this file is, and any others it could plausibly be.

    Header-bearing formats are identified by their full set of headings, which is unique
    across these eight. The headerless one is the fallback, and it is a genuine fallback
    rather than a guess of equal standing: three accounts export in that shape, so a file
    matching it says nothing about which account it came from.
    """
    rows = _rows(text)
    matches = [fmt for fmt in FORMATS if any(_header_at(row, fmt) for row in rows[:40])]
    if matches:
        # Longest header wins where one format's headings are a subset of another's -- Amex's
        # three are contained in First Direct's four.
        best = max(matches, key=lambda f: len(f.header))
        return best, [m for m in matches if m is not best]

    data_rows = [row for row in rows if _looks_like_data(row)]
    if data_rows and len(data_rows) >= max(1, len(rows) // 2):
        return BY_KEY["hsbc"], []
    return None, []


def header_index(rows: list[list[str]], fmt: BankFormat) -> int | None:
    for index, row in enumerate(rows):
        if _header_at(row, fmt):
            return index
    return None


def identify(text: str, fmt: BankFormat) -> str:
    """Whatever the file says about which account it is, as one string.

    Nationwide names the product, Marcus names the pot, Virgin gives the account number.
    None of it is a key -- it is a hint for the account dropdown, matched loosely against
    the configured names, and wrong often enough that the dropdown stays changeable.
    """
    if not fmt.identifiers:
        return ""
    found = []
    for row in _rows(text)[:12]:
        if len(row) < 2:
            continue
        if _normalise_heading(row[0]) in fmt.identifiers:
            found.append(_clean(row[1]))
    # Marcus carries its identifiers as columns rather than as a preamble.
    rows = _rows(text)
    index = header_index(rows, fmt)
    if index is not None and not found:
        headings = [_normalise_heading(c) for c in rows[index]]
        for wanted in fmt.identifiers:
            if wanted in headings and len(rows) > index + 1:
                position = headings.index(wanted)
                if position < len(rows[index + 1]):
                    found.append(_clean(rows[index + 1][position]))
    return " ".join(part for part in found if part)


# --------------------------------------------------------------------------------- reading


class UnreadableFile(Exception):
    """The file is not in the shape the chosen format describes."""


def decode(raw: bytes) -> str:
    """Bytes to text, trying the encodings a UK bank actually emits.

    Most are UTF-8, several carry a byte-order mark, and the ones exported from a Windows
    back office are cp1252 -- which differs from UTF-8 only where a pound sign or a smart
    quote appears, so it decodes cleanly until the one row that does not.
    """
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read(text: str, fmt: BankFormat) -> pd.DataFrame:
    """One file, as `row, date, description, amount, direction`.

    `amount` is always positive and `direction` carries the sign, which is the form the rest
    of the application uses -- a transaction's direction lives in its type, never in a minus
    sign (see budget/validation.py).

    Rows that are not transactions are dropped rather than reported: a preamble, a blank
    separator and six paragraphs of small print are all normal parts of these files, and
    complaining about them would mean complaining on every import.
    """
    rows = _rows(text)
    if fmt.headerless:
        indexed = [(n, row) for n, row in enumerate(rows, start=1) if _looks_like_data(row)]
        if not indexed:
            raise UnreadableFile(
                "No rows of the form date, description, amount were found."
            )
        return _frame(
            [
                (
                    n,
                    read_date(row[fmt.positions[0]]),
                    _clean(row[fmt.positions[1]]),
                    money(row[fmt.positions[2]]),
                    None,
                )
                for n, row in indexed
            ],
            fmt,
        )

    start = header_index(rows, fmt)
    if start is None:
        raise UnreadableFile(
            f"No {fmt.label} header row found. Expected columns: "
            + ", ".join(fmt.header)
        )

    headings = [_normalise_heading(cell) for cell in rows[start]]

    def column(name: str) -> int:
        try:
            return headings.index(name)
        except ValueError as exc:  # pragma: no cover -- the header check has already passed
            raise UnreadableFile(f"{fmt.label}: no {name!r} column") from exc

    date_at = column(fmt.date)
    description_at = column(fmt.description)
    signed_at = column(fmt.amount) if fmt.amount else None
    out_at = column(fmt.out_column) if fmt.out_column else None
    in_at = column(fmt.in_column) if fmt.in_column else None

    collected = []
    for offset, row in enumerate(rows[start + 1:], start=start + 2):
        if not any(_clean(cell) for cell in row):
            # A blank line ends the data. Virgin Money's small print follows one, and reading
            # on turns 'MR ROBERT KANER' and an address into transactions.
            break
        when = read_date(row[date_at]) if date_at < len(row) else None
        if when is None:
            continue
        description = _clean(row[description_at]) if description_at < len(row) else ""
        if signed_at is not None:
            value = money(row[signed_at]) if signed_at < len(row) else None
            collected.append((offset, when, description, value, None))
        else:
            paid_out = money(row[out_at]) if out_at < len(row) else None
            paid_in = money(row[in_at]) if in_at < len(row) else None
            collected.append((offset, when, description, paid_out, paid_in))

    if not collected:
        raise UnreadableFile(f"{fmt.label}: the header was found but no dated rows follow it.")
    return _frame(collected, fmt)


def _frame(collected, fmt: BankFormat) -> pd.DataFrame:
    records = []
    for offset, when, description, first, second in collected:
        if fmt.paired_amounts:
            out_value, in_value = first, second
            # Both columns filled and one of them zero is common -- Coventry writes 0.00
            # rather than leaving the other side empty.
            if out_value and out_value != 0:
                amount, direction = abs(out_value), OUT
            elif in_value and in_value != 0:
                amount, direction = abs(in_value), IN
            else:
                continue
        else:
            if first is None or first == 0:
                continue
            negative = first < 0
            out = negative if fmt.out_is_negative else not negative
            amount, direction = abs(first), OUT if out else IN
        records.append(
            {
                "row": offset,
                "date": when,
                "description": description,
                "amount": amount,
                "direction": direction,
            }
        )
    return pd.DataFrame(records, columns=list(NORMALISED))
