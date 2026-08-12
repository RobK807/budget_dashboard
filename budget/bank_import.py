"""Turning parsed bank rows into transactions worth reviewing.

`bank_formats` reads a file. This decides what the rows mean, in four passes:

1. **Direction becomes a type.** Money out of an account is a Debit, money in is a Credit --
   including on a credit card, where a purchase is money out of the card and paying the bill
   is money in. That is the same convention the ledger already uses (see budget/postings.py),
   so no card needs special handling here.

2. **What is already recorded is dropped.** The ledger is exploded into one entry per
   account movement and matched on account, direction, amount and date. Dates are allowed to
   differ by a few days, because a card posts on a different day from the one you wrote down
   -- the Hyrox entry sits at 24 July in the ledger and 25 July on the statement. Matching
   consumes: two identical purchases on one day need two ledger entries to be dropped, and
   one of them survives if only one is recorded.

3. **Transfers are paired.** A movement between two of your own accounts appears in both
   banks' files, as a debit in one and a credit in the other. Matched on amount and date,
   the pair collapses into the single Transfer the ledger wants. This is the part that needs
   no configuration and cannot really be fooled -- the alternative, reading the description,
   cannot tell which card 'AMERICAN EXPRESS DD' paid, and both of them appear.

4. **What is left is offered to the rules, then recorded as it stands.** A pattern can name
   the account on the other side, for a transfer whose counterpart is not in the batch.
   Anything still unmatched becomes a plain debit or credit -- the direction the bank gave
   it. Wording is not evidence of a transfer: money leaving a joint account for the other
   holder's own account is described exactly like an internal one, so 'looks like a transfer'
   would query the same payments every month without ever being able to settle them.

Nothing here writes. The result is a frame for the import grid, where every row can still be
corrected -- which is the point, because passes 3 and 4 are inferences and inferences are
sometimes wrong.

So is every exclusion, which is why each one is itemised with its reason and can be reversed:
`reinstate` takes the keys of rows judged to have been dropped wrongly and re-runs the whole
decision with them spared. Reversing an exclusion has consequences beyond the row itself --
sparing half of a paired transfer has to un-pair the other half -- so it is a re-run rather
than a patch applied to the result.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

import pandas as pd

from budget import bank_formats as bf
from budget.validation import CREDIT, DEBIT, TRANSFER

# How far a statement date may sit from the recorded one and still be the same transaction.
DATE_TOLERANCE_DAYS = 3

# How far *before* an account's last recorded movement a row may still be, and be treated as
# something that simply had not been entered yet rather than as history you have moved past.
#
# The age test exists to stop an export's six-month tail backfilling a quiet account. But an
# account is rarely written up in date order -- a transfer recorded on the day it was made
# sits after a card purchase from the same week that was entered later -- so a bare 'older
# than the last entry' line takes genuine gaps with it. A week's grace keeps those, and the
# duplicate check runs on them first, so anything already recorded is still dropped as such.
PREDATES_GRACE_DAYS = 7

# Reasons a row did not make it through, in the order they are applied.
ALREADY_RECORDED = "already in the ledger"
PAIRED_AWAY = "paired into a transfer"
PREDATES = "well before this account's last recorded movement"


@dataclass
class SourceRow:
    """One line of one file, with the account it was imported against."""

    account: str
    date: dt.date
    description: str
    amount: Decimal
    direction: str  # bf.OUT | bf.IN
    source: str  # the file it came from
    row: int  # its line number in that file

    @property
    def out(self) -> bool:
        return self.direction == bf.OUT

    @property
    def key(self) -> str:
        """Stable identity for one line of one file.

        A row that has been left out is offered back for review, and ticking it re-runs the
        whole decision with that row spared. The tick therefore has to survive a rerun and
        name the same line afterwards, which rules out a position in a list that changes
        length as rows are reinstated.
        """
        return f"{self.source}#{self.row}"


@dataclass
class Prepared:
    """What an import would bring in, and what it would leave behind."""

    rows: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    excluded: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())
    paired: int = 0
    ruled: int = 0
    reinstated: int = 0

    @property
    def count(self) -> int:
        return len(self.rows)


def rows_from(frame: pd.DataFrame, account: str, source: str) -> list[SourceRow]:
    """A parsed file plus the account it belongs to."""
    return [
        SourceRow(
            account=account,
            date=_as_date(r.date),
            description=r.description,
            amount=Decimal(str(r.amount)),
            direction=r.direction,
            source=source,
            row=int(r.row),
        )
        for r in frame.itertuples()
        if r.date is not None and r.amount is not None
    ]


# ------------------------------------------------------------------ what is already there


def ledger_index(postings: pd.DataFrame) -> dict:
    """Existing account movements, as a consumable multiset.

    Keyed on (account, out?, amount) with the dates as a list, because the date is the part
    allowed to be approximate. Everything else has to agree exactly: an amount that is out by
    a penny is a different transaction, not a near miss.

    Built from postings rather than from transactions so a stored Transfer contributes to
    *both* of its accounts. Without that, importing the two files either side of a transfer
    would exclude the half that happens to be the 'from' account and let the other half
    through as a new transaction.
    """
    index: dict = defaultdict(list)
    if postings is None or postings.empty:
        return index
    for row in postings.itertuples():
        key = (row.account, row.column == "debit", Decimal(str(row.amount)))
        # A date column read back through pandas arrives as a Timestamp, which will not
        # subtract from a datetime.date. Normalised here rather than at each comparison.
        index[key].append(_as_date(row.date))
    for dates in index.values():
        dates.sort()
    return index


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.to_datetime(value).date()


def last_recorded(postings: pd.DataFrame) -> dict[str, dt.date]:
    """The most recent movement already recorded against each account.

    A bank will happily export six months whatever you ask it for, and these files reach
    back further than the ledger does for the quieter accounts -- Halifax and the Coventry
    saver both start in February, months before either was last written down. Those rows are
    not duplicates, so nothing above catches them, and importing them would silently backfill
    half a year on the strength of having clicked 'upload'.

    A row well before the account's last recorded movement is one you have already moved
    past. That is the line this draws, and it is per account, because being up to date on
    HSBC says nothing about Halifax. `prepare` allows a week either side of it -- accounts
    are not written up in date order, so the last entry is a rough edge rather than a
    watermark, and anything the grace lets through still faces the duplicate check.
    """
    if postings is None or postings.empty:
        return {}
    latest: dict[str, dt.date] = {}
    for row in postings.itertuples():
        when = _as_date(row.date)
        if row.account not in latest or when > latest[row.account]:
            latest[row.account] = when
    return latest


def _take(index: dict, row: SourceRow, tolerance: int) -> dt.date | None:
    """Consume a matching ledger entry, returning the date it was recorded under."""
    dates = index.get((row.account, row.out, row.amount))
    if not dates:
        return None
    nearest = min(dates, key=lambda d: abs((d - row.date).days))
    if abs((nearest - row.date).days) > tolerance:
        return None
    dates.remove(nearest)
    return nearest


# --------------------------------------------------------------------------- transfer pairs


def _pair_transfers(
    rows: list[SourceRow], tolerance: int, spared: frozenset[str] = frozenset()
) -> tuple[list, list[SourceRow]]:
    """Match opposite movements of the same amount across two different accounts.

    Returns (pairs, unpaired). A pair is (out_row, in_row) and becomes one Transfer.

    Only across *different* accounts, and only one pairing per row. Two candidates equally
    close in date are resolved by taking the nearest, then the earliest -- arbitrary, but
    stable, and both readings describe the same movement anyway.

    A row the user has reinstated is never paired. That is what breaks a pairing they judged
    wrong: sparing either half leaves both as plain movements, rather than reinstating one
    while a Transfer still accounts for the same money.
    """
    by_key: dict = defaultdict(list)
    for row in rows:
        if row.key not in spared:
            by_key[(row.amount, row.out)].append(row)

    used: set[int] = set()
    pairs = []
    for row in rows:
        if id(row) in used or not row.out or row.key in spared:
            continue
        options = [
            other
            for other in by_key.get((row.amount, False), [])
            if id(other) not in used
            and other.account != row.account
            and abs((other.date - row.date).days) <= tolerance
        ]
        if not options:
            continue
        partner = min(options, key=lambda o: (abs((o.date - row.date).days), o.date))
        used.add(id(row))
        used.add(id(partner))
        pairs.append((row, partner))

    unpaired = [row for row in rows if id(row) not in used]
    return pairs, unpaired


# --------------------------------------------------------------------------------- the rules


def _rule_for(description: str, rules: pd.DataFrame) -> str | None:
    """The account a description names, where a rule says so."""
    if rules is None or rules.empty or not description:
        return None
    haystack = description.lower()
    for rule in rules.itertuples():
        pattern = (rule.pattern or "").strip().lower()
        if pattern and pattern in haystack:
            account = rule.account
            # A frame built with a missing account gives NaN rather than None, and NaN is
            # truthy -- so an unguarded return proposes a transfer to an account called 'nan'.
            return None if account is None or pd.isna(account) else account
    return None


# ------------------------------------------------------------------------------- the result


COLUMNS = [
    "Date", "Type", "Amount", "Account From", "Account To", "Category",
    "Purchase type", "Comment", "Category comment", "Donation",
]


def prepare(
    rows: list[SourceRow],
    postings: pd.DataFrame,
    rules: pd.DataFrame | None = None,
    tolerance: int = DATE_TOLERANCE_DAYS,
    skip_older: bool = True,
    grace_days: int = PREDATES_GRACE_DAYS,
    reinstate: frozenset[str] | set[str] = frozenset(),
) -> Prepared:
    """Everything above, in order. See the module docstring.

    `reinstate` holds the keys of rows the user looked at in the left-out list and judged to
    have been dropped wrongly. A spared row bypasses **every** test -- duplicate, age and
    pairing alike -- and arrives as a plain movement. Re-running the whole decision with the
    row spared, rather than patching the result afterwards, is what keeps the two consistent:
    sparing half of a pair has to un-pair the other half, and a rule applied to a row that is
    no longer there has to stop applying.
    """
    spared = frozenset(reinstate)
    index = ledger_index(postings)
    latest = last_recorded(postings) if skip_older else {}

    kept: list[SourceRow] = []
    excluded: list[dict] = []

    def drop(row: SourceRow, why: str) -> None:
        excluded.append(
            {
                "key": row.key,
                "Date": row.date,
                "Account": row.account,
                "Amount": row.amount,
                "Direction": "out" if row.out else "in",
                "Comment": row.description,
                "Why": why,
                "From": row.source,
            }
        )

    for row in sorted(rows, key=lambda r: (r.date, r.source, r.row)):
        if row.key in spared:
            kept.append(row)
            continue
        recorded = _take(index, row, tolerance)
        if recorded is not None:
            drop(
                row,
                ALREADY_RECORDED
                if recorded == row.date
                else f"{ALREADY_RECORDED}, dated {recorded:%d %b}",
            )
            continue
        # After the duplicate check, not before: a row that *is* already recorded should be
        # reported as such, which is the more useful thing to be told -- and it is the answer
        # that stays true whatever the age window is set to.
        cutoff = latest.get(row.account)
        if cutoff is not None and row.date < cutoff - dt.timedelta(days=grace_days):
            drop(row, f"{PREDATES} ({cutoff:%d %b %Y})")
            continue
        kept.append(row)

    pairs, unpaired = _pair_transfers(kept, tolerance, spared)

    records: list[dict] = []
    for out_row, in_row in pairs:
        records.append(
            {
                "Date": min(out_row.date, in_row.date),
                "Type": TRANSFER,
                "Amount": float(out_row.amount),
                "Account From": out_row.account,
                "Account To": in_row.account,
                "Category": None,
                "Purchase type": None,
                # A transfer carries no category, so the description is the only trace of
                # where it came from. Both sides, since the two banks word it differently.
                "Comment": _joined(out_row.description, in_row.description),
                "Category comment": None,
                "Donation": False,
            }
        )
        drop(in_row, f"{PAIRED_AWAY} from {out_row.account}")

    ruled = 0
    for row in unpaired:
        other = _rule_for(row.description, rules)
        # A rule naming the account the row is already on says nothing -- 'HSBC' matches half
        # of HSBC's own descriptions -- so it is ignored rather than made into a transfer to
        # itself, which would not validate anyway.
        if other and other != row.account:
            ruled += 1
            records.append(
                {
                    "Date": row.date,
                    "Type": TRANSFER,
                    "Amount": float(row.amount),
                    "Account From": row.account if row.out else other,
                    "Account To": other if row.out else row.account,
                    "Category": None,
                    "Purchase type": None,
                    "Comment": row.description,
                    "Category comment": None,
                    "Donation": False,
                }
            )
            continue

        # No counterpart in the batch and no rule: a plain movement, with the bank's own
        # wording left alone.
        #
        # There is no third state here on purpose. A description that reads like a transfer
        # -- 'withdrawal to', 'payment received' -- says nothing reliable about whether the
        # far side is yours: money leaving a joint account for the other holder's own account
        # is worded identically to an internal transfer and is an ordinary debit. Marking
        # those as suspect meant the same handful of payments were queried every month, so
        # the marker became something to scroll past. The evidence that a transfer happened
        # is the matching movement on the other account, and where that is absent the honest
        # answer is a debit or a credit.
        records.append(
            {
                "Date": row.date,
                "Type": DEBIT if row.out else CREDIT,
                "Amount": float(row.amount),
                "Account From": row.account,
                "Account To": None,
                "Category": None,
                "Purchase type": None,
                "Comment": row.description,
                "Category comment": None,
                "Donation": False,
            }
        )

    frame = pd.DataFrame(records, columns=COLUMNS)
    if not frame.empty:
        frame = frame.sort_values("Date", ascending=False).reset_index(drop=True)
        frame["Date"] = pd.to_datetime(frame["Date"])
    frame["Donation"] = frame["Donation"].astype(bool) if not frame.empty else frame.get(
        "Donation"
    )

    return Prepared(
        rows=frame,
        excluded=pd.DataFrame(
            excluded, columns=["key", "Date", "Account", "Amount", "Direction", "Comment",
                               "Why", "From"]
        ),
        paired=len(pairs),
        ruled=ruled,
        reinstated=sum(1 for row in rows if row.key in spared),
    )


def guess_account(filename: str, identifier: str, fmt, accounts: list[str]) -> str | None:
    """Which account a file probably belongs to. Never the last word -- the dropdown is.

    Three signals, weakest last. A format used by one account answers it outright. Otherwise
    the filename and whatever the file says about itself are scored against the configured
    names, and a tie returns nothing rather than a coin toss: picking the wrong account files
    a month of someone's spending against the wrong balance, which is worse than a dropdown
    that starts empty.
    """
    known = [a for a in accounts if a in (fmt.accounts if fmt else ())]
    pool = known or list(accounts)
    if len(pool) == 1:
        return pool[0]

    haystack = f"{filename} {identifier}".lower()
    haystack = "".join(c if c.isalnum() else " " for c in haystack)
    words = set(haystack.split())

    scores: list[tuple[int, str]] = []
    for account in pool:
        tokens = [
            t for t in "".join(
                c if c.isalnum() else " " for c in account.lower()
            ).split() if len(t) > 2
        ]
        if not tokens:
            continue
        hit = sum(len(t) for t in tokens if t in words)
        if hit:
            scores.append((hit, account))
    if not scores:
        return None
    scores.sort(reverse=True)
    if len(scores) > 1 and scores[0][0] == scores[1][0]:
        return None
    return scores[0][1]


INCLUDE = "Include"


def offer_back(excluded: pd.DataFrame) -> pd.DataFrame:
    """The left-out rows with a tick box in front, ready for an editor."""
    offered = excluded.copy()
    offered.insert(0, INCLUDE, False)
    return offered


def spared_keys(edited: pd.DataFrame) -> set[str]:
    """Which rows were ticked, from whatever the editor hands back.

    Not as simple as `edited[edited[INCLUDE]]`. A checkbox column that has never been touched
    can come back as object rather than bool, and an untouched cell as None or NA rather than
    False -- and a null is not falsey to pandas' boolean indexing, it raises. Both spellings
    have to mean 'not ticked', because the alternative is a page that works until the first
    time nobody ticks anything.
    """
    if edited is None or edited.empty or INCLUDE not in edited.columns:
        return set()
    ticked = edited[INCLUDE].map(lambda v: v is True or v == 1).fillna(False).astype(bool)
    return set(edited.loc[ticked, "key"])


def as_grid(frame: pd.DataFrame) -> pd.DataFrame:
    """The prepared rows in the shape the import grid expects.

    The grid's column types are what give it a date picker and a numeric field rather than
    free text, so an empty result still has to carry them -- otherwise clearing a file turns
    the editor into a grid of strings.
    """
    from budget import importer

    template = importer.template()
    if frame is None or frame.empty:
        return template
    out = frame.reindex(columns=list(template.columns))
    for column, dtype in template.dtypes.items():
        try:
            out[column] = out[column].astype(dtype)
        except (TypeError, ValueError):
            # A column of Nones cannot become float64 in one step; through object it can.
            out[column] = pd.Series(out[column].tolist(), dtype="object")
    return out.reset_index(drop=True)


def _joined(first: str, second: str) -> str:
    """Both banks' wording for one movement, without repeating an identical description."""
    left, right = (first or "").strip(), (second or "").strip()
    if not right or left.lower() == right.lower():
        return left
    if not left:
        return right
    return f"{left} / {right}"
