"""Parses a table of rows into transaction candidates.

Replaces the BulkImport tab. Column names are matched loosely so that a sheet copied
straight out of the old workbook still works, and so does a bank export with slightly
different headings.

The workbook's Month column is accepted but ignored: the period is derived from the date
(see validation.period_for), which removes the mismatch that let three rows sit under the
wrong month for years.
"""

from __future__ import annotations

import datetime as dt
import re

import pandas as pd

from budget.validation import Candidate, to_decimal

# Canonical field -> accepted headings, compared case- and punctuation-insensitively.
ALIASES: dict[str, tuple[str, ...]] = {
    "txn_date": ("date", "txndate", "transactiondate", "when"),
    "type": ("type", "transactiontype", "transaction"),
    "amount": ("amount", "value", "sum"),
    "account_from": ("accountfrom", "from", "account", "fromaccount"),
    "account_to": ("accountto", "to", "toaccount"),
    "category": ("category", "cat"),
    "classification": ("purchasetype", "classification", "class", "purchase"),
    "comment": ("comment", "description", "notes", "note", "reference"),
    "category_comment": ("categorycomment", "catcomment"),
    "is_donation": ("donation", "isdonation", "charitabledonation", "charity"),
}

IGNORED = ("month", "item", "id", "errormessages")

TEMPLATE_COLUMNS = [
    "Date", "Type", "Amount", "Account From", "Account To",
    "Category", "Purchase type", "Comment", "Category comment", "Donation",
]

# What counts as a yes in a pasted column. The workbook's own donation sheet used 'Y', and a
# spreadsheet round-trip turns a tick box into TRUE, 1 or 'True' depending on the route.
TRUTHY = ("y", "yes", "true", "1", "t", "donation")


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def map_columns(columns) -> tuple[dict[str, str], list[str]]:
    """Returns (field -> actual column name, unrecognised columns)."""
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for col in columns:
        key = _normalise(col)
        if key in IGNORED:
            continue
        for field, aliases in ALIASES.items():
            if key in aliases and field not in mapping:
                mapping[field] = col
                break
        else:
            unknown.append(str(col))
    return mapping, unknown


def _to_date(value) -> dt.date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    # dayfirst: this is a UK budget, so 03/08/2026 is 3 August.
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _text(value) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    # The workbook writes 0 into optional text fields rather than leaving them empty.
    return None if text in ("", "0", "nan", "None") else text


def _flag(value) -> bool:
    """A yes/no column, however it arrives.

    A pasted tick box comes through as a real bool; a CSV out of a spreadsheet gives TRUE,
    1 or 'Y' depending on the route. Anything unrecognised is a no, so a stray word in the
    column cannot silently flag a payment as a gift.
    """
    if isinstance(value, bool):
        return value
    text = _text(value)
    return text is not None and text.strip().lower() in TRUTHY


def _title(value) -> str | None:
    text = _text(value)
    if text is None:
        return None
    return {"credit": "Credit", "debit": "Debit", "transfer": "Transfer"}.get(
        text.lower(), text
    )


def parse(df: pd.DataFrame) -> tuple[list[Candidate], list[str]]:
    """Table -> candidates. Returns (candidates, problems with the table itself)."""
    mapping, unknown = map_columns(df.columns)

    missing = [
        f.replace("_", " ")
        for f in ("txn_date", "type", "amount", "account_from")
        if f not in mapping
    ]
    if missing:
        return [], [f"No column found for: {', '.join(missing)}"]

    problems = []
    if unknown:
        problems.append("Ignoring unrecognised column(s): " + ", ".join(unknown))

    def get(row, field):
        col = mapping.get(field)
        return row[col] if col else None

    candidates = []
    for offset, (_, row) in enumerate(df.iterrows(), start=2):
        # Skip rows that are entirely blank -- common when pasting from a spreadsheet.
        if all(_text(get(row, f)) is None for f in mapping):
            continue
        candidates.append(
            Candidate(
                txn_date=_to_date(get(row, "txn_date")),
                type=_title(get(row, "type")),
                amount=to_decimal(get(row, "amount")),
                account_from=_text(get(row, "account_from")),
                account_to=_text(get(row, "account_to")),
                category=_text(get(row, "category")),
                classification=_text(get(row, "classification")),
                comment=_text(get(row, "comment")),
                category_comment=_text(get(row, "category_comment")),
                is_donation=_flag(get(row, "is_donation")),
                source_row=offset,
            )
        )
    return candidates, problems


def template() -> pd.DataFrame:
    """Empty frame with real dtypes, so st.data_editor can offer a date picker and a
    numeric field rather than free text."""
    return pd.DataFrame(
        {
            "Date": pd.Series(dtype="datetime64[ns]"),
            "Type": pd.Series(dtype="object"),
            "Amount": pd.Series(dtype="float64"),
            "Account From": pd.Series(dtype="object"),
            "Account To": pd.Series(dtype="object"),
            "Category": pd.Series(dtype="object"),
            "Purchase type": pd.Series(dtype="object"),
            "Comment": pd.Series(dtype="object"),
            "Category comment": pd.Series(dtype="object"),
            "Donation": pd.Series(dtype="bool"),
        }
    )


# --------------------------------------------------------------- balance-check targets
#
# st.data_editor keys its edits by row position, so filtering the balance-check table would
# otherwise discard what was typed, or worse, replay it onto a different set of accounts.
# Targets are therefore held per account and re-seeded on every render. Kept here as pure
# functions so the round-trip is testable without a Streamlit runtime.


def seed_targets(accounts, stored: dict[str, float]) -> list:
    """Values to display for the accounts currently on screen."""
    return [stored.get(a, pd.NA) for a in accounts]


def capture_targets(visible_rows, stored: dict[str, float]) -> dict[str, float]:
    """Fold the visible rows back into the store.

    Only accounts present in `visible_rows` are touched, so a target entered in the full
    table survives being filtered out and reappears when the filter is removed. Clearing a
    cell removes that account rather than storing a null.
    """
    for account, value in visible_rows:
        if value is None or pd.isna(value):
            stored.pop(account, None)
        else:
            stored[account] = float(value)
    return stored


def to_frame(candidates: list[Candidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Row": c.source_row,
                "Date": c.txn_date,
                "Type": c.type,
                "Amount": c.amount,
                "Account From": c.account_from,
                "Account To": c.account_to,
                "Category": c.category,
                "Purchase type": c.classification,
                "Comment": c.comment,
                "Category comment": c.category_comment,
                "Donation": c.is_donation,
            }
            for c in candidates
        ]
    )
