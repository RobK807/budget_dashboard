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


# A leading YYYY-M-D. Anything starting this way states its own order and must be taken at
# its word -- see _to_date.
_ISO_LEADING = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(\D|$)")


def _to_date(value) -> dt.date | None:
    """A date from whatever the column holds, without guessing at an order it has stated.

    Two conventions arrive here and they need opposite treatment:

      * `03/08/2026` is ambiguous, and this is a UK budget, so it is 3 August.
      * `2026-08-01` is not ambiguous. It is 1 August, in every locale.

    Applying dayfirst to both -- which is what this used to do -- reads the second as
    year-day-month and returns 8 January. Pandas warns about it only when the day is too
    large to be a month, so `2026-07-31` came back correct with a warning while `2026-08-01`
    came back wrong in silence. Every date in the first twelve days of a month was quietly
    transposed, and the ones that would have been noticed were the ones that still worked.

    It mattered because ISO is what this application itself writes: the conflict export in
    sync.local_only_frame emits `datetime.date`, which `to_csv` renders as ISO. So the round
    trip that exists to rescue a machine's transactions -- export, pull, re-import -- was the
    one path guaranteed to hit it.
    """
    if value is None or value == "":
        return None
    # Before the datetime check, not after: pd.NaT *is* an instance of datetime, and
    # NaT.date() is NaT again -- so an empty date cell came back as a Candidate whose
    # txn_date was neither a date nor None, and `txn_date is None` never reported it missing.
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    text = str(value).strip()
    if not text:
        return None

    parsed = pd.to_datetime(
        text, dayfirst=not _ISO_LEADING.match(text), errors="coerce"
    )
    return None if pd.isna(parsed) else parsed.date()


def _text(value) -> str | None:
    if value is None:
        return None
    # Not just float NaN: an empty date cell is NaT and an empty nullable cell is pd.NA,
    # neither of which is a float. Both used to fall through to str(), which turned them
    # into the text 'NaT' -- so a row the user had never touched read as having content.
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass  # not a scalar; fall through and stringify
    text = str(value).strip()
    # The workbook writes 0 into optional text fields rather than leaving them empty.
    return None if text in ("", "0", "nan", "None", "NaT", "<NA>") else text


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


# Categories whose comment column is a restatement of the transaction's own comment. 'Other'
# is the catch-all: the category says nothing about what the payment was, so the category
# comment is where the description would otherwise have to be typed a second time.
INHERIT_COMMENT_FOR = ("other",)


def _inherited_category_comment(
    entered: str | None, category: str | None, comment: str | None
) -> str | None:
    """A default, not an override: only fills a blank, and only for the catch-all category.

    Anything typed into the column wins, so this can be corrected on the row rather than
    fought with -- and a category that does describe the spending is left alone, where
    copying the comment across would just be the same words twice.
    """
    if entered is not None:
        return entered
    if category is None or category.strip().lower() not in INHERIT_COMMENT_FOR:
        return None
    return comment


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

    def is_blank(row) -> bool:
        """Nothing in the row at all.

        Common when pasting from a spreadsheet, and now the normal state of every row added
        by the 'add blank rows' button. An unticked checkbox counts as nothing: a bool
        column cannot hold a null, so st.data_editor returns a real False for a box the user
        never saw, and testing it as text made every blank row look occupied.
        """
        for field in mapping:
            value = get(row, field)
            if field == "is_donation":
                if _flag(value):
                    return False
            elif _text(value) is not None:
                return False
        return True

    candidates = []
    for offset, (_, row) in enumerate(df.iterrows(), start=2):
        if is_blank(row):
            continue
        category = _text(get(row, "category"))
        comment = _text(get(row, "comment"))
        candidates.append(
            Candidate(
                txn_date=_to_date(get(row, "txn_date")),
                type=_title(get(row, "type")),
                amount=to_decimal(get(row, "amount")),
                account_from=_text(get(row, "account_from")),
                account_to=_text(get(row, "account_to")),
                category=category,
                classification=_text(get(row, "classification")),
                comment=comment,
                category_comment=_inherited_category_comment(
                    _text(get(row, "category_comment")), category, comment
                ),
                is_donation=_flag(get(row, "is_donation")),
                source_row=offset,
            )
        )
    return candidates, problems


def with_blank_rows(frame: pd.DataFrame, count: int) -> pd.DataFrame:
    """`frame` with `count` empty rows appended, keeping its dtypes.

    For entering a known number of transactions at once. The editor's own + button adds one
    row per click, which is fine for three and tedious for forty.

    Concatenating an all-NA frame would widen the dtypes to object and cost the date picker
    and the numeric field, so the blanks are taken from the template and the column types
    restored afterwards.
    """
    if count <= 0:
        return frame
    blanks = pd.DataFrame(
        {column: [None] * count for column in frame.columns}, index=range(count)
    )
    grown = pd.concat([frame, blanks], ignore_index=True)
    for column, dtype in template().dtypes.items():
        if column in grown.columns:
            grown[column] = grown[column].astype(dtype)
    return grown


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
