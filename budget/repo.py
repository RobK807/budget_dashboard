"""Query layer: everything the dashboard displays.

The month tabs, Summary and Cumulative Analysis were all reports masquerading as storage.
Here they are what they always were -- aggregations over the ledger.

Postings are exploded in Python rather than in a SQL view, deliberately. The sign rule is
subtle (see postings.py) and already covered by the reconciliation gate and unit tests;
expressing it a second time in SQL would create two implementations that could drift. At
738 transactions a year this costs nothing, and the tidy frame produced here is what every
aggregation below groups over.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget.models import (
    Account,
    AccountTarget,
    Bonus,
    Budget,
    Card,
    CardStatement,
    Category,
    Classification,
    ClassificationAllowance,
    ClassificationOpening,
    CyclingDay,
    CyclingOutgoing,
    CyclingRate,
    OpeningBalance,
    Payslip,
    Projection,
    SalaryAssumption,
    SalaryProfile,
    SavingsAdjustment,
    SavingsPlan,
    SavingsTarget,
    Setting,
    Txn,
)
from budget import tax
from budget.postings import postings_for

FISCAL_START_MONTH = 4


# ------------------------------------------------------------------------------ loading


def load_reference(session: Session) -> dict[str, pd.DataFrame]:
    def frame(rows, cols):
        return pd.DataFrame([{c: getattr(r, c) for c in cols} for r in rows])

    # Alphabetical rather than the workbook's column order: an account's position in the
    # month tab was a storage detail (offset = position * 4) and means nothing here.
    # lower() because a plain ORDER BY is ASCII, which puts HSBC before Halifax and ISA
    # before Investments. func.lower is portable if this ever moves to Postgres.
    accounts = frame(
        session.scalars(select(Account).order_by(func.lower(Account.name))),
        ["id", "name", "short_code", "type", "is_savings", "is_investment", "is_isa",
         "exclude_from_savings", "interest_net", "statement_day", "payment_day",
         "display_order", "valid_from", "valid_to"],
    )
    # Grouping then name: the workbook's display_order was roughly grouped already, but only
    # by convention -- a category added later landed wherever the row was inserted.
    categories = frame(
        session.scalars(
            select(Category).order_by(func.lower(Category.grouping), func.lower(Category.name))
        ),
        ["id", "name", "grouping", "spend_type", "display_order", "valid_from", "valid_to"],
    )
    classifications = frame(
        session.scalars(select(Classification).order_by(Classification.display_order)),
        ["id", "name", "direction", "rollover", "counts_as_spend", "display_order",
         "valid_from", "valid_to"],
    )
    settings = {s.key: s.value for s in session.scalars(select(Setting))}
    return {
        "accounts": accounts,
        "categories": categories,
        "classifications": classifications,
        "settings": settings,
    }


def load_postings(session: Session, include_deleted: bool = False) -> pd.DataFrame:
    """One row per account movement. Transfers produce two rows, everything else one."""
    accounts = {a.id: a for a in session.scalars(select(Account))}
    categories = {c.id: c.name for c in session.scalars(select(Category))}
    classifications = {c.id: c for c in session.scalars(select(Classification))}

    stmt = select(Txn)
    if not include_deleted:
        stmt = stmt.where(Txn.deleted_at.is_(None))

    records = []
    for t in session.scalars(stmt):
        to_name = accounts[t.account_to_id].name if t.account_to_id else None
        cls = classifications.get(t.classification_id)
        for p in postings_for(t.type, accounts[t.account_from_id].name, to_name, t.amount):
            acct = next(a for a in accounts.values() if a.name == p.account)
            records.append(
                {
                    "txn_id": t.id,
                    "date": t.txn_date,
                    "period": t.period,
                    "account": p.account,
                    "account_type": acct.type,
                    "column": p.column,
                    "amount": t.amount,
                    "signed": p.signed(acct.type),
                    "type": t.type,
                    "category": categories.get(t.category_id),
                    "classification": cls.name if cls else None,
                    "direction": cls.direction if cls else 0,
                    "comment": t.comment,
                    "deleted": t.deleted_at is not None,
                }
            )

    df = pd.DataFrame.from_records(
        records,
        columns=["txn_id", "date", "period", "account", "account_type", "column", "amount",
                 "signed", "type", "category", "classification", "direction", "comment",
                 "deleted"],
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_transactions(session: Session, include_deleted: bool = False) -> pd.DataFrame:
    """One row per transaction, for the Transactions page."""
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    categories = {c.id: c.name for c in session.scalars(select(Category))}
    classifications = {c.id: c.name for c in session.scalars(select(Classification))}

    stmt = select(Txn).order_by(Txn.txn_date.desc(), Txn.id.desc())
    if not include_deleted:
        stmt = stmt.where(Txn.deleted_at.is_(None))

    rows = [
        {
            "id": t.id,
            "date": t.txn_date,
            "period": t.period,
            "type": t.type,
            "amount": t.amount,
            "account_from": accounts.get(t.account_from_id),
            "account_to": accounts.get(t.account_to_id),
            "category": categories.get(t.category_id),
            "classification": classifications.get(t.classification_id),
            "comment": t.comment,
            "category_comment": t.category_comment,
            "identifier": t.legacy_identifier,
            "is_donation": bool(t.is_donation),
            "deleted": t.deleted_at is not None,
            "deleted_reason": t.deleted_reason,
        }
        for t in session.scalars(stmt)
    ]
    df = pd.DataFrame(
        rows,
        columns=["id", "date", "period", "type", "amount", "account_from", "account_to",
                 "category", "classification", "comment", "category_comment", "identifier",
                 "is_donation", "deleted", "deleted_reason"],
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_opening_balances(session: Session) -> pd.DataFrame:
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    rows = [
        {"account": accounts[ob.account_id], "period": ob.period, "opening": ob.amount}
        for ob in session.scalars(select(OpeningBalance))
    ]
    return pd.DataFrame(rows, columns=["account", "period", "opening"])


def load_budgets(session: Session) -> pd.DataFrame:
    categories = {c.id: c.name for c in session.scalars(select(Category))}
    rows = [
        {
            "period": b.period,
            "category": categories[b.category_id],
            "income": b.income or Decimal("0"),
            "expected": b.expected or Decimal("0"),
        }
        for b in session.scalars(select(Budget))
    ]
    return pd.DataFrame(rows, columns=["period", "category", "income", "expected"])


# ------------------------------------------------------------------------- period helpers


# --------------------------------------------------------------------------- sorting
#
# Project convention: names are ordered case-insensitively, everywhere.
#
# Every default ordering in the stack is ASCII -- SQL's ORDER BY, Python's sorted(), and
# pandas' sort_values() all put uppercase before lowercase, giving "HSBC, Halifax" and
# "ISA, Investments". Correct by codepoint, wrong to a reader.
#
#   SQL          order_by(func.lower(Column))
#   iterables    ui.alphabetical(values)
#   DataFrames   repo.sort_human(df, by=...)


def casefold_key(series: pd.Series) -> pd.Series:
    """Sort key for pandas: case-insensitive for text, untouched for anything else.

    Tested with is_string_dtype rather than `dtype == object`: pandas 3 gives string columns
    a dedicated `str` dtype, so an object check silently stops matching them and the sort
    quietly reverts to ASCII. Both are accepted, since object still appears in mixed frames.
    """
    if pd.api.types.is_string_dtype(series) or series.dtype == object:
        return series.astype("string").str.casefold()
    return series


def sort_human(df: pd.DataFrame, by, ascending=True) -> pd.DataFrame:
    """sort_values with case-insensitive ordering on text columns."""
    return df.sort_values(by, ascending=ascending, key=casefold_key)


def fiscal_periods(tax_year: int) -> list[str]:
    """April of tax_year through March of the next, in fiscal order."""
    out = [f"{tax_year:04d}-{m:02d}" for m in range(4, 13)]
    out += [f"{tax_year + 1:04d}-{m:02d}" for m in range(1, 4)]
    return out


def tax_year_of(period: str) -> int:
    """The tax year a month falls in -- the April it started from."""
    year, month = (int(p) for p in period.split("-"))
    return year if month >= FISCAL_START_MONTH else year - 1


# The UK tax year runs 6 April to 5 April, so the first five days of April belong to the
# year before. Month granularity cannot express that, which is why the interest tracker had
# to split its April rows into 'Apr (before 6th)' and 'Apr (after 6th)' and file them under
# different years by hand.
TAX_YEAR_START_DAY = 6


def tax_year_of_date(when) -> int:
    """The tax year a *date* falls in, on the real 6 April boundary.

    Distinct from `tax_year_of`, which takes a period and can only ever be right to the
    month. Both are needed: a payslip belongs to a month, but interest belongs to the day it
    was paid, and 1 April is a different tax year from 30 April.
    """
    when = pd.Timestamp(when).date() if not isinstance(when, dt.date) else when
    if (when.month, when.day) < (FISCAL_START_MONTH, TAX_YEAR_START_DAY):
        return when.year - 1
    return when.year


def tax_year_label(year: int) -> str:
    """'26-27', the form the interest tracker used."""
    return f"{str(year)[-2:]}-{str(year + 1)[-2:]}"


def period_label(period: str) -> str:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1).strftime("%B %Y")


def periods_to_date(periods: list[str], today: dt.date | None = None) -> list[str]:
    """Trim a fiscal year to months that have actually started.

    There is nothing to say about September before September, and showing eight empty
    months pushes the months that matter off the screen. The current month is included --
    it is in progress, not future.

    'YYYY-MM' strings are zero-padded and fixed width, so a plain string comparison orders
    them correctly and handles the January-to-March rollover into the next calendar year.
    """
    today = today or dt.date.today()
    current = f"{today.year:04d}-{today.month:02d}"
    trimmed = [p for p in periods if p <= current]
    # Guard against a year that has not begun: better to show it all than nothing.
    return trimmed or list(periods)


# --------------------------------------------------------------- periods from the data
#
# The month dropdowns were built from `fiscal_periods(tax_year)`, so every one of them ran
# April 2026 to March 2027 and no further. That is a property of the workbook -- one file per
# year -- not of a database that will hold several. These build the range from what is
# actually stored plus where today falls, so the lists extend on their own.


def period_of(value) -> str:
    """The period a date belongs to."""
    date = pd.to_datetime(value).date() if not isinstance(value, dt.date) else value
    return f"{date.year:04d}-{date.month:02d}"


def month_add(period: str, months: int) -> str:
    """Shift a period by a number of months, either direction."""
    year, month = (int(p) for p in period.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def months_between(start: str, end: str) -> int:
    sy, sm = (int(p) for p in start.split("-"))
    ey, em = (int(p) for p in end.split("-"))
    return (ey * 12 + em) - (sy * 12 + sm)


def period_range(start: str, end: str) -> list[str]:
    """Every month from start to end inclusive, in order. Empty if end precedes start."""
    count = months_between(start, end)
    if count < 0:
        return []
    return [month_add(start, i) for i in range(count + 1)]


def earliest_period(*sources, default: str) -> str:
    """The first month anything is recorded against, across however many frames.

    Falls back to `default` when the database is empty -- a fresh install still needs a
    month to select.
    """
    seen: set[str] = set()
    for source in sources:
        if source is None:
            continue
        values = source.dropna() if hasattr(source, "dropna") else source
        seen.update(str(v) for v in values if v)
    return min(seen) if seen else default


def span(
    earliest: str, look_forward: int = 0, today: dt.date | None = None
) -> list[str]:
    """The months a dropdown should offer: earliest recorded through to today, plus a
    look-forward for the ones that plan ahead.

    A month that has not begun is still worth selecting where a figure is being *set* for it
    -- next month's savings target, or a projection -- which is why the look-forward exists
    rather than every list stopping dead at the current month.
    """
    today = today or dt.date.today()
    current = f"{today.year:04d}-{today.month:02d}"
    start = min(earliest, current)
    return period_range(start, month_add(max(current, earliest), look_forward))


# --------------------------------------------------------------------------- aggregations


def rolled_forward_openings(
    postings: pd.DataFrame, openings: pd.DataFrame, period: str
) -> pd.Series:
    """Each account's opening balance for a month: its stated opening plus everything
    posted since.

    The `opening_balance` table holds one row per account per month, copied from the
    workbook's row 60 at migration. In the workbook that row is a *formula* -- the previous
    month's End -- so it follows the data. Copied across as values it stopped following
    anything, and every stored opening after the first was a fact about the day the
    migration ran.

    That went unnoticed for as long as the database only ever reproduced the workbook. The
    moment a transaction was entered, every later month was wrong: adding GBP 2.50 to July
    left August's stored opening 2.50 behind, September's opening frozen at August's stale
    figure, and so on to March -- 107 differences from one cause, all of them silent.

    So the stored value is used for the month it anchors and derived from there on. The
    anchor is the *earliest* month stored for that account rather than the earliest month
    overall, because an account opened part-way through the year has its real opening
    balance there and nothing before it.
    """
    if openings.empty:
        anchors = pd.DataFrame(columns=["account", "period", "opening"])
    else:
        anchors = (
            openings.sort_values("period").groupby("account", as_index=False).first()
        )

    stated = anchors.set_index("account")["opening"] if not anchors.empty else pd.Series(
        dtype="object"
    )
    anchor_period = anchors.set_index("account")["period"] if not anchors.empty else (
        pd.Series(dtype="object")
    )

    movement: dict[str, Decimal] = {}
    if not postings.empty:
        earlier = postings[postings["period"] < period]
        if not earlier.empty:
            # Only what falls on or after the account's own anchor. Anything before it is
            # already inside the stated opening, and counting it again would double it.
            # An account with no stated opening anchors at the empty string, which sorts
            # before any period, so it accumulates from the start rather than being dropped.
            starts = earlier["account"].map(anchor_period).astype("object").fillna("")
            since = earlier[earlier["period"].astype("object") >= starts]
            if not since.empty:
                movement = since.groupby("account")["signed"].sum().to_dict()

    return pd.Series(
        {
            account: stated.get(account, Decimal("0")) + movement.get(account, Decimal("0"))
            for account in set(stated.index) | set(movement)
        },
        dtype="object",
    )


def account_balances(
    postings: pd.DataFrame, openings: pd.DataFrame, period: str, accounts: pd.DataFrame
) -> pd.DataFrame:
    """Reproduces month-tab rows 60-63 (Start, End, Total paid in, Total paid out), with
    transfers split out.

    The workbook's rows 62/63 are `=SUM(I4:I59)` and `=SUM(J4:J59)` -- the whole Credit and
    Debit columns, so transfers between your own accounts are mixed in with real income and
    spending. Moving GBP 5,000 from HSBC to savings shows as GBP 5,000 'paid out' there and
    GBP 5,000 'paid in' here, which flatters both figures. Splitting them makes 'paid in'
    and 'paid out' mean money genuinely entering or leaving; `total_in`/`total_out` retain
    the workbook's combined definition.
    """
    period_postings = postings[postings["period"] == period]
    # Derived, not read straight from the table: see rolled_forward_openings.
    opening = rolled_forward_openings(postings, openings, period)

    rows = []
    for _, acct in accounts.iterrows():
        mine = period_postings[period_postings["account"] == acct["name"]]
        is_transfer = mine["type"] == "Transfer"
        credit = mine["column"] == "credit"
        debit = mine["column"] == "debit"

        def total(mask) -> Decimal:
            return mine.loc[mask, "amount"].sum() or Decimal("0")

        paid_in = total(credit & ~is_transfer)
        paid_out = total(debit & ~is_transfer)
        transfer_in = total(credit & is_transfer)
        transfer_out = total(debit & is_transfer)
        movement = mine["signed"].sum() or Decimal("0")
        start = opening.get(acct["name"], Decimal("0"))

        rows.append(
            {
                "account": acct["name"],
                "type": acct["type"],
                "opening": start,
                "paid_in": paid_in,
                "paid_out": paid_out,
                "transfer_in": transfer_in,
                "transfer_out": transfer_out,
                "total_in": paid_in + transfer_in,
                "total_out": paid_out + transfer_out,
                "movement": movement,
                "closing": start + movement,
                "is_savings": bool(acct["is_savings"]),
                "is_investment": bool(acct["is_investment"]),
                "is_isa": bool(acct["is_isa"]),
                # Carried here rather than joined back on by name at each call site, so
                # 'available' means the same thing on the Summary chart as in the savings
                # tables. NaN is truthy, so a bare bool() would earmark every unflagged pot.
                "earmarked": _flag(acct.get("exclude_from_savings")),
            }
        )
    return pd.DataFrame(rows)


def _flag(value) -> bool:
    return False if value is None or pd.isna(value) else bool(value)


def category_actuals(postings: pd.DataFrame, period: str) -> pd.DataFrame:
    """Reproduces month-tab columns C ('Income') and E ('Total Spent').

    These are *not* netted. New_entry sends a credit to column C and a debit to column E:

        If k = 1 Then Selection.Offset(0, 1)             ' C, Income
        Else          Selection.Offset(0, intColReduced) ' E, Total Spent

    so a category with spend_type 'All' -- 'Other', 'Going Out', 'Band' -- accumulates both
    independently. Netting them understates spend by the value of the credits, which for
    'Other' in June is over GBP 20,000.
    """
    mine = postings[
        (postings["period"] == period)
        & (postings["type"] != "Transfer")
        & (postings["category"].notna())
    ]
    if mine.empty:
        return pd.DataFrame(columns=["category", "spent", "income"])

    spent = (
        mine[mine["type"] == "Debit"].groupby("category")["amount"].sum().rename("spent")
    )
    income = (
        mine[mine["type"] == "Credit"].groupby("category")["amount"].sum().rename("income")
    )
    out = pd.concat([spent, income], axis=1).fillna(Decimal("0")).reset_index()
    return out


def budget_vs_actual(
    postings: pd.DataFrame, budgets: pd.DataFrame, period: str, categories: pd.DataFrame
) -> pd.DataFrame:
    """Month-tab columns B-F: Item, Income, Expected Costs, Total Spent, Total Left.

    Only 'Expected Costs' is a budget. Income and spend are both actuals accumulated by the
    macro in the workbook, and are derived from the ledger here -- which is why
    Budget.income is redundant and should be dropped (see PHASE1_NOTES.md).
    """
    actuals = category_actuals(postings, period).set_index("category")
    period_budgets = budgets[budgets["period"] == period].set_index("category")

    rows = []
    for _, cat in categories.iterrows():
        name = cat["name"]
        if name not in period_budgets.index:
            continue
        expected = period_budgets.loc[name, "expected"] or Decimal("0")
        spent = actuals["spent"].get(name, Decimal("0")) if not actuals.empty else Decimal("0")
        income = (
            actuals["income"].get(name, Decimal("0")) if not actuals.empty else Decimal("0")
        )
        rows.append(
            {
                "category": name,
                "grouping": cat["grouping"],
                "income": income,
                "expected": expected,
                "spent": spent,
                "left": expected - spent,
            }
        )
    return pd.DataFrame(rows)


def daily_classification(postings: pd.DataFrame, period: str) -> pd.DataFrame:
    """Reproduces the month-tab per-day, per-classification totals (columns DB:DI).

    Workbook rule: direction * (debits - credits), transfers excluded because New_entry
    writes no classification reference for them.
    """
    mine = postings[
        (postings["period"] == period)
        & (postings["type"] != "Transfer")
        & (postings["classification"].notna())
    ].copy()
    if mine.empty:
        return pd.DataFrame(columns=["date", "classification", "total"])
    mine["total"] = mine.apply(
        lambda r: r["direction"] * (r["amount"] if r["type"] == "Debit" else -r["amount"]),
        axis=1,
    )
    return mine.groupby(["date", "classification"], as_index=False)["total"].sum()


def load_projections(session: Session) -> pd.DataFrame:
    classifications = {c.id: c.name for c in session.scalars(select(Classification))}
    rows = [
        {
            "date": p.proj_date,
            "classification": classifications.get(p.classification_id),
            "amount": p.amount,
            "comment": p.comment,
        }
        for p in session.scalars(select(Projection))
    ]
    df = pd.DataFrame(rows, columns=["date", "classification", "amount", "comment"])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_allowances(session: Session) -> pd.DataFrame:
    classifications = {c.id: c.name for c in session.scalars(select(Classification))}
    rows = [
        {
            "period": a.period,
            "classification": classifications.get(a.classification_id),
            "daily_amount": a.daily_amount,
        }
        for a in session.scalars(select(ClassificationAllowance))
    ]
    return pd.DataFrame(rows, columns=["period", "classification", "daily_amount"])


def load_class_openings(session: Session) -> pd.DataFrame:
    classifications = {c.id: c.name for c in session.scalars(select(Classification))}
    rows = [
        {
            "period": o.period,
            "classification": classifications.get(o.classification_id),
            "amount": o.amount,
        }
        for o in session.scalars(select(ClassificationOpening))
    ]
    return pd.DataFrame(rows, columns=["period", "classification", "amount"])


def load_payslips(session: Session) -> pd.DataFrame:
    columns = ["period", "payday", "gross", "ni", "holiday_pay", "cycle_to_work", "paye",
               "net", "salary", "expected_gross", "benefits", "additional"]
    rows = [
        {c: getattr(p, c) for c in columns} for p in session.scalars(select(Payslip))
    ]
    return pd.DataFrame(rows, columns=columns)


# Stored as percentages (models.SalaryAssumption); the arithmetic in tax.py wants fractions.
RATE_KEYS = frozenset(
    {"ni_lower_rate", "ni_higher_rate", "basic_rate", "higher_rate", "additional_rate"}
)
HUNDRED = Decimal("100")


def load_salary_assumptions(session: Session, tax_year: int) -> pd.DataFrame:
    """The raw band rows, for the editable table on the Salary page."""
    rows = [
        {
            "key": r.key,
            "effective_from": r.effective_from,
            "value": r.value,
            "tax_year": r.tax_year,
        }
        for r in session.scalars(
            select(SalaryAssumption).where(SalaryAssumption.tax_year == tax_year)
        )
    ]
    return pd.DataFrame(rows, columns=["key", "effective_from", "value", "tax_year"])


def assumption_tax_years(session: Session) -> list[int]:
    return sorted(
        {y for (y,) in session.execute(select(SalaryAssumption.tax_year).distinct())}
    )


ADJUSTMENT_KEY = "personal_allowance_adjustment"


def assumption_dates(assumptions: pd.DataFrame) -> list[dt.date]:
    """Every date on which a threshold or rate changed, most recent last.

    The allowance steps are excluded: those are a taper within a year, not a revision of the
    bands, and they are already handled inside tax.Bands.allowance_for.
    """
    if assumptions.empty:
        return []
    rows = assumptions[assumptions["key"] != ADJUSTMENT_KEY]
    return sorted({pd.to_datetime(d).date() for d in rows["effective_from"]})


def bands_from(assumptions: pd.DataFrame, on: dt.date | None = None) -> tax.Bands:
    """Assemble the PAYE/NI bands from stored assumptions, as they stood on a date.

    Every threshold and rate is effective-dated, so a rate change part-way through a year is
    a new row rather than an edit that quietly rewrites what earlier months were taxed at.
    For each key the row in force is the last one starting on or before `on`; `on` of None
    takes the most recent of all, which is what an editor showing 'current' wants.

    `basic_band` is derived rather than read: the workbook's D36 is `=D28 - D22`, the basic
    rate threshold less the personal allowance. Storing it as well as its two inputs would
    let the three drift apart the moment one was edited, so the stored value is only a
    fallback for a database that predates the inputs being kept.
    """
    simple: dict[str, Decimal] = {}
    steps: tuple = ()

    if not assumptions.empty:
        frame = assumptions.copy()
        frame["effective_from"] = pd.to_datetime(frame["effective_from"]).dt.date
        for key, group in frame.groupby("key"):
            group = group.sort_values("effective_from")
            if key == ADJUSTMENT_KEY:
                steps = tuple(
                    (row["effective_from"], row["value"]) for _, row in group.iterrows()
                )
                continue
            applicable = group if on is None else group[group["effective_from"] <= on]
            # Before the first stored set there is nothing in force. Reaching forward to the
            # earliest one is a better answer than zero, which would silently model a 0% tax
            # rate rather than admitting the year has no bands yet.
            row = applicable.iloc[-1] if not applicable.empty else group.iloc[0]
            simple[key] = row["value"] / HUNDRED if key in RATE_KEYS else row["value"]

    threshold = simple.get("basic_rate_threshold")
    allowance = simple.get("personal_allowance")
    if threshold is not None and allowance is not None:
        basic_band = threshold - allowance
    else:
        basic_band = simple.get("basic_band", Decimal("0"))

    return tax.Bands(
        ni_lower_earnings_limit=simple.get("ni_lower_earnings_limit", Decimal("0")),
        ni_upper_earnings_limit=simple.get("ni_upper_earnings_limit", Decimal("0")),
        ni_lower_rate=simple.get("ni_lower_rate", Decimal("0")),
        ni_higher_rate=simple.get("ni_higher_rate", Decimal("0")),
        personal_allowance=allowance or Decimal("0"),
        basic_rate_threshold=threshold or Decimal("0"),
        basic_band=basic_band,
        higher_threshold=simple.get("higher_threshold", Decimal("0")),
        basic_rate=simple.get("basic_rate", Decimal("0")),
        higher_rate=simple.get("higher_rate", Decimal("0")),
        additional_rate=simple.get("additional_rate", Decimal("0")),
        allowance_steps=steps,
    )


def salary_bands(
    session: Session, tax_year: int, on: dt.date | None = None
) -> tax.Bands:
    """The bands for a tax year, as they stood on a date. See bands_from."""
    return bands_from(load_salary_assumptions(session, tax_year), on)


def load_cards(session: Session) -> pd.DataFrame:
    columns = ["id", "name", "opening_balance", "opening_date", "payment_day",
               "term_months", "min_payment_pct", "credit_limit", "display_order"]
    rows = [{c: getattr(k, c) for c in columns} for k in session.scalars(select(Card))]
    return pd.DataFrame(rows, columns=columns)


def load_cycling(session: Session) -> tuple[pd.DataFrame, pd.DataFrame]:
    outgoings = pd.DataFrame(
        [
            {"date": o.date, "item": o.item, "amount": o.amount, "flag": o.flag}
            for o in session.scalars(select(CyclingOutgoing))
        ],
        columns=["date", "item", "amount", "flag"],
    )
    days = pd.DataFrame(
        [
            {"date": d.date, "commute": d.commute, "band": d.band, "gym": d.gym}
            for d in session.scalars(select(CyclingDay))
        ],
        columns=["date", "commute", "band", "gym"],
    )
    for frame in (outgoings, days):
        if not frame.empty:
            frame["date"] = pd.to_datetime(frame["date"])
    return outgoings, days


def cycling_savings(days: pd.DataFrame, rates: dict[str, Decimal]) -> pd.DataFrame:
    """Saving per day ridden.

    The workbook used a nested IF in priority order -- commute, then band, then gym -- so a
    day flagged for two counts once, at the higher rate.
    """
    if days.empty:
        return days.assign(saving=[])

    def saving(row):
        for key in ("commute", "band", "gym"):
            if row[key]:
                return rates.get(key, Decimal("0"))
        return Decimal("0")

    out = days.copy()
    out["saving"] = out.apply(saving, axis=1)
    out["kind"] = out.apply(
        lambda r: "Commute" if r["commute"] else ("Band" if r["band"] else
                                                  ("Gym" if r["gym"] else "None")),
        axis=1,
    )
    return out


# ------------------------------------------------------------------- rollover engine


def carried_forward(
    closing: Decimal, rollover: str, retention: Decimal = Decimal("1")
) -> Decimal:
    """How much of last month's closing balance starts this month.

    DESIGN.md 6a. Two independent rules, which the workbook welded into one:

      rollover  -- which balances carry: none / credit / debit / all
      retention -- the proportion of a *credit* balance that carries

    Sign convention: a running total is direction x (debits - credits), so a positive total
    is a debit balance and a negative one a credit balance. Excess has direction -1, which
    is why a surplus there shows as a positive figure in everyday terms but arrives here
    already inverted.

    Retention applies to credit balances only -- carrying a surplus forward at less than
    100% is the point of it; an overspend always carries in full.
    """
    if rollover == "none" or closing == 0:
        return Decimal("0")

    is_credit = closing < 0
    if rollover == "credit" and not is_credit:
        return Decimal("0")
    if rollover == "debit" and is_credit:
        return Decimal("0")

    return closing * retention if is_credit else closing


def running_classification(
    postings: pd.DataFrame,
    projections: pd.DataFrame,
    allowances: pd.DataFrame,
    classifications: pd.DataFrame,
    period: str,
    opening: dict[str, Decimal] | None = None,
    today: dt.date | None = None,
) -> pd.DataFrame:
    """Daily running totals per classification -- the month tab's CT:DA columns.

    Each day adds the actual total, or the projection when the day is in the future, plus
    any daily allowance. The workbook applied the allowance by testing whether the column
    header ended in 'Excess'; here it is keyed data, so any classification can carry one.
    """
    today = today or dt.date.today()
    opening = opening or {}

    year, month = (int(p) for p in period.split("-"))
    days = pd.date_range(
        dt.date(year, month, 1),
        dt.date(year, month, calendar.monthrange(year, month)[1]),
        freq="D",
    )

    daily = daily_classification(postings, period)
    actual = (
        daily.set_index(["classification", "date"])["total"] if not daily.empty else None
    )
    projected = (
        projections.set_index(["classification", "date"])["amount"]
        if not projections.empty
        else None
    )
    period_allowance = allowances[allowances["period"] == period].set_index(
        "classification"
    )["daily_amount"] if not allowances.empty else pd.Series(dtype=object)

    rows = []
    for name in classifications["name"]:
        balance = opening.get(name, Decimal("0"))
        allowance = period_allowance.get(name, Decimal("0"))
        for day in days:
            movement = Decimal("0")
            if day.date() <= today:
                if actual is not None and (name, day) in actual.index:
                    movement = actual.loc[(name, day)]
            elif projected is not None and (name, day) in projected.index:
                movement = projected.loc[(name, day)]
            balance += movement + allowance
            rows.append(
                {
                    "date": day,
                    "classification": name,
                    "movement": movement,
                    "allowance": allowance,
                    "running": balance,
                }
            )

    return pd.DataFrame(rows, columns=["date", "classification", "movement", "allowance",
                                       "running"])


def running_by_period(
    postings: pd.DataFrame,
    projections: pd.DataFrame,
    allowances: pd.DataFrame,
    classifications: pd.DataFrame,
    periods: list[str],
    retention: Decimal = Decimal("1"),
    today: dt.date | None = None,
    openings: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chains the running totals across a year, applying each classification's rollover.

    Returns (daily running totals, month-end closing values). The closing values are what
    Summary!Q19:Y31 reads through xlCloseValue.

    `openings` supplies stated opening balances -- the first month of a year has no prior
    month to roll forward from, and the workbook typed that figure into the formula.
    """
    rollovers = dict(zip(classifications["name"], classifications["rollover"]))
    stated = (
        openings.set_index(["period", "classification"])["amount"]
        if openings is not None and not openings.empty
        else None
    )

    opening: dict[str, Decimal] = {}
    all_days, closes = [], []
    for period in periods:
        if stated is not None:
            for name in classifications["name"]:
                if (period, name) in stated.index:
                    opening[name] = opening.get(name, Decimal("0")) + stated.loc[
                        (period, name)
                    ]

        frame = running_classification(
            postings, projections, allowances, classifications, period, opening, today
        )
        all_days.append(frame)

        closing = (
            frame.sort_values("date").groupby("classification")["running"].last()
            if not frame.empty
            else pd.Series(dtype=object)
        )
        for name, value in closing.items():
            closes.append({"period": period, "classification": name, "closing": value})

        opening = {
            name: carried_forward(value, rollovers.get(name, "none"), retention)
            for name, value in closing.items()
        }

    daily = pd.concat(all_days, ignore_index=True) if all_days else pd.DataFrame()
    return daily, pd.DataFrame(closes, columns=["period", "classification", "closing"])


def classification_by_month(postings: pd.DataFrame) -> pd.DataFrame:
    """Reproduces Summary Q19:Y31 -- classification totals per month."""
    mine = postings[
        (postings["type"] != "Transfer") & (postings["classification"].notna())
    ].copy()
    if mine.empty:
        return pd.DataFrame()
    mine["total"] = mine.apply(
        lambda r: r["direction"] * (r["amount"] if r["type"] == "Debit" else -r["amount"]),
        axis=1,
    )
    matrix = mine.pivot_table(
        index="period", columns="classification", values="total", aggfunc="sum", fill_value=0
    )
    # pivot_table orders columns by codepoint; keep the project's case-insensitive rule.
    return matrix[sorted(matrix.columns, key=lambda c: str(c).casefold())]


def savings_position(balances: pd.DataFrame) -> dict[str, Decimal]:
    """Headline totals from the Summary tab, driven by the account flags.

    'available' is savings less the earmarked pots -- the workbook's 'Less SC & Wed', whose
    label had stopped describing what it excluded. Derived from the flag rather than a fixed
    list, so it follows when a pot is added.
    """
    earmarked = (
        balances["earmarked"] if "earmarked" in balances else pd.Series(False, balances.index)
    )
    return {
        "savings": balances.loc[balances["is_savings"], "closing"].sum() or Decimal("0"),
        "available": balances.loc[balances["is_savings"] & ~earmarked, "closing"].sum()
        or Decimal("0"),
        "investments": balances.loc[balances["is_investment"], "closing"].sum() or Decimal("0"),
        "isa": balances.loc[balances["is_isa"], "closing"].sum() or Decimal("0"),
        "cards": balances.loc[balances["type"] == "credit_card", "closing"].sum()
        or Decimal("0"),
        "current": balances.loc[
            (balances["type"] == "bank")
            & ~balances["is_savings"]
            & ~balances["is_investment"],
            "closing",
        ].sum()
        or Decimal("0"),
    }


def account_history(
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    account_name: str,
    periods: list[str],
) -> pd.DataFrame:
    """One account across the year, with the same columns as the month-end account table."""
    one = accounts[accounts["name"] == account_name]
    if one.empty:
        return pd.DataFrame()

    rows = []
    for period in periods:
        balances = account_balances(postings, openings, period, one)
        if balances.empty:
            continue
        b = balances.iloc[0]
        rows.append(
            {
                "month": period_label(period),
                "opening": b["opening"],
                "paid_in": b["paid_in"],
                "paid_out": b["paid_out"],
                "transfer_in": b["transfer_in"],
                "transfer_out": b["transfer_out"],
                "closing": b["closing"],
            }
        )
    return pd.DataFrame(rows)


def candidate_impact(candidates, accounts: pd.DataFrame) -> pd.DataFrame:
    """What a pending import would do to each account balance.

    Reproduces BulkImport!CK3:CQ32, the check where you type in each account's real balance
    and confirm that opening position plus the import's net effect lands on it. It is the
    thing that catches a transaction typed twice, or one left out altogether.

    The workbook wrote this as SUMIFS pairs, negated for the three credit-card rows because
    a card balance is positive debt. Here the sign comes from Posting.signed(), which is the
    same rule the balances and the reconciliation gate already use.

    'In' and 'Out' follow the direction money actually moved, so they read naturally for a
    current account. 'Net' applies the signed rule, so a card's spending correctly increases
    the balance owed even though it appears under 'Out'.
    """
    types = dict(zip(accounts["name"], accounts["type"]))

    money_in: dict[str, Decimal] = defaultdict(Decimal)
    money_out: dict[str, Decimal] = defaultdict(Decimal)
    net: dict[str, Decimal] = defaultdict(Decimal)

    for c in candidates:
        if not c.account_from or c.amount is None or c.type not in ("Credit", "Debit", "Transfer"):
            continue
        try:
            postings = postings_for(c.type, c.account_from, c.account_to, c.amount)
        except ValueError:
            continue  # incomplete row; validation reports it separately
        for p in postings:
            account_type = types.get(p.account)
            if account_type is None:
                continue
            if p.column == "credit":
                money_in[p.account] += p.amount
            else:
                money_out[p.account] += p.amount
            net[p.account] += p.signed(account_type)

    return pd.DataFrame(
        [
            {
                "account": name,
                "in": money_in.get(name, Decimal("0")),
                "out": money_out.get(name, Decimal("0")),
                "net": net.get(name, Decimal("0")),
            }
            for name in accounts["name"]
        ]
    )


def import_verification(
    candidates,
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    period: str,
) -> pd.DataFrame:
    """Current balance, the import's effect, and the resulting projection per account."""
    balances = account_balances(postings, openings, period, accounts).set_index("account")
    impact = candidate_impact(candidates, accounts).set_index("account")

    rows = []
    for name in accounts["name"]:
        current = balances["closing"].get(name, Decimal("0"))
        change = impact["net"].get(name, Decimal("0"))
        rows.append(
            {
                "account": name,
                "current": current,
                "in": impact["in"].get(name, Decimal("0")),
                "out": impact["out"].get(name, Decimal("0")),
                "projected": current + change,
                "affected": impact["net"].get(name, Decimal("0")) != 0
                or impact["in"].get(name, Decimal("0")) != 0
                or impact["out"].get(name, Decimal("0")) != 0,
            }
        )
    return pd.DataFrame(rows)


def monthly_series(
    postings: pd.DataFrame, openings: pd.DataFrame, accounts: pd.DataFrame, periods: list[str]
) -> pd.DataFrame:
    """Closing savings / investment / card position for each month of the year."""
    rows = []
    for period in periods:
        balances = account_balances(postings, openings, period, accounts)
        position = savings_position(balances)
        net = postings[postings["period"] == period]["signed"].sum() or Decimal("0")
        rows.append({"period": period, "net_cashflow": net, **position})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------- phase 5 loading


def load_salary_profiles(session: Session) -> pd.DataFrame:
    columns = ["id", "effective_from", "base_salary", "annual_salary", "note"]
    rows = [
        {c: getattr(p, c) for c in columns}
        for p in session.scalars(select(SalaryProfile).order_by(SalaryProfile.effective_from))
    ]
    return pd.DataFrame(rows, columns=columns)


def load_bonuses(session: Session) -> pd.DataFrame:
    columns = ["period", "amount", "note", "payday", "gross", "ni", "paye", "net"]
    rows = [
        {c: getattr(b, c) for c in columns}
        for b in session.scalars(select(Bonus).order_by(Bonus.period))
    ]
    return pd.DataFrame(rows, columns=columns)


def load_cycling_rates(session: Session) -> pd.DataFrame:
    rows = [
        {"kind": r.kind, "effective_from": r.effective_from, "amount": r.amount}
        for r in session.scalars(
            select(CyclingRate).order_by(CyclingRate.kind, CyclingRate.effective_from)
        )
    ]
    return pd.DataFrame(rows, columns=["kind", "effective_from", "amount"])


def load_card_statements(session: Session) -> pd.DataFrame:
    rows = [
        {"period": s.period, "account_id": s.account_id, "bill_eom": s.bill_eom}
        for s in session.scalars(select(CardStatement))
    ]
    return pd.DataFrame(rows, columns=["period", "account_id", "bill_eom"])


def load_account_targets(session: Session) -> pd.DataFrame:
    rows = [
        {"period": t.period, "account_id": t.account_id, "amount": t.amount}
        for t in session.scalars(select(AccountTarget))
    ]
    return pd.DataFrame(rows, columns=["period", "account_id", "amount"])


def load_savings_targets(session: Session) -> pd.DataFrame:
    """The superseded per-period overview. Retained as the pre-split record; the live
    figures come from `load_savings_plan` via `targets_from_plan`."""
    rows = [
        {"period": t.period, "savings": t.savings, "investments": t.investments}
        for t in session.scalars(select(SavingsTarget).order_by(SavingsTarget.period))
    ]
    return pd.DataFrame(rows, columns=["period", "savings", "investments"])


def load_savings_plan(session: Session) -> pd.DataFrame:
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    rows = [
        {
            "id": p.id,
            "account_id": p.account_id,
            "account": accounts.get(p.account_id),
            "effective_from": p.effective_from,
            "amount": p.amount,
        }
        for p in session.scalars(
            select(SavingsPlan).order_by(SavingsPlan.effective_from, SavingsPlan.account_id)
        )
    ]
    return pd.DataFrame(
        rows, columns=["id", "account_id", "account", "effective_from", "amount"]
    )


def plan_dates(plan: pd.DataFrame) -> list[dt.date]:
    """The distinct dates the plan was revised on, earliest first."""
    if plan.empty:
        return []
    return sorted({pd.Timestamp(d).date() for d in plan["effective_from"]})


def plan_in_force(plan: pd.DataFrame, on: dt.date) -> pd.DataFrame:
    """Each account's target as at a date -- the latest set that has started.

    Per account rather than per set, so an account added to the plan later keeps its own
    start date instead of being backdated to whenever the last wholesale revision was.
    """
    columns = ["account", "amount"]
    if plan.empty:
        return pd.DataFrame(columns=columns)
    dated = plan.copy()
    dated["effective_from"] = pd.to_datetime(dated["effective_from"]).dt.date
    applicable = dated[dated["effective_from"] <= on]
    if applicable.empty:
        return pd.DataFrame(columns=columns)
    latest = (
        applicable.sort_values("effective_from")
        .groupby("account", as_index=False)
        .last()
    )
    return latest[columns]


def load_savings_adjustments(session: Session) -> pd.DataFrame:
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    rows = [
        {
            "id": a.id,
            "period": a.period,
            "account_id": a.account_id,
            "account": accounts.get(a.account_id),
            "amount": a.amount,
            "note": a.note,
        }
        for a in session.scalars(
            select(SavingsAdjustment).order_by(
                SavingsAdjustment.period, SavingsAdjustment.account_id
            )
        )
    ]
    return pd.DataFrame(
        rows, columns=["id", "period", "account_id", "account", "amount", "note"]
    )


def account_kinds(accounts: pd.DataFrame) -> dict[str, str]:
    """Which side of the page an account counts towards.

    'Savings' splits again into available and reserved for the charts, since an earmarked pot
    is money saved but already spoken for -- the same distinction the balances make.
    """
    kinds = {}
    for _, acct in accounts.iterrows():
        if acct.get("is_investment"):
            kinds[acct["name"]] = "Investments"
        elif acct.get("is_savings"):
            kinds[acct["name"]] = "Savings"
    return kinds


def plan_by_period(
    plan: pd.DataFrame,
    accounts: pd.DataFrame,
    periods: list[str],
    adjustments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The monthly target for every account, month by month.

    Columns: period, month, account, kind ('Savings' or 'Investments'), source and amount.
    Accounts with no target in force in a month are omitted rather than shown as zero -- a
    pot that has not started yet and a pot with a target of nothing are different things.

    `adjustments` are the one-offs: a lump sum planned into or out of a pot in one named
    month. They arrive as their own rows rather than being added to the standing figure, so
    a month whose target looks wrong can be read back to the thing that moved it.
    """
    columns = ["period", "month", "account", "kind", "source", "amount"]
    kinds = account_kinds(accounts)
    rows = []

    if not plan.empty:
        for period in periods:
            for _, row in plan_in_force(plan, period_start(period)).iterrows():
                rows.append(
                    {
                        "period": period,
                        "month": period_label(period),
                        "account": row["account"],
                        "kind": kinds.get(row["account"], "Other"),
                        "source": "Plan",
                        "amount": row["amount"],
                    }
                )

    if adjustments is not None and not adjustments.empty:
        wanted = set(periods)
        for _, row in adjustments.iterrows():
            if row["period"] not in wanted:
                continue
            rows.append(
                {
                    "period": row["period"],
                    "month": period_label(row["period"]),
                    "account": row["account"],
                    "kind": kinds.get(row["account"], "Other"),
                    "source": "One-off",
                    "amount": row["amount"],
                }
            )

    return pd.DataFrame(rows, columns=columns)


def targets_from_plan(
    plan: pd.DataFrame,
    accounts: pd.DataFrame,
    periods: list[str],
    adjustments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The savings/investments overview, summed from the per-account plan and its one-offs.

    Derived rather than stored, so the headline and the breakdown cannot disagree. The two
    used to be typed separately, which is how the dashboard came to hold 900 and 350 while
    the plan behind them said 250 + 350 + 300 and 250 + 100 -- the same figures, but only by
    coincidence of nobody having changed one without the other.

    Targets are signed. Moving money between two pots is a negative target in one and a
    positive one in the other, and they net to nothing overall, which is exactly right: the
    month has saved no more than it started with.
    """
    columns = ["period", "savings", "investments"]
    detail = plan_by_period(plan, accounts, periods, adjustments)
    if detail.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for period in periods:
        mine = detail[detail["period"] == period]
        rows.append(
            {
                "period": period,
                "savings": mine.loc[mine["kind"] == "Savings", "amount"].sum()
                or Decimal("0"),
                "investments": mine.loc[mine["kind"] == "Investments", "amount"].sum()
                or Decimal("0"),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def targets_by_bucket(
    plan: pd.DataFrame,
    accounts: pd.DataFrame,
    periods: list[str],
    adjustments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The same targets, split the way the *added* figures are split.

    Savings divides into available and reserved on the earmarked flag, so every bar on the
    chart has a target of its own to be measured against rather than a single savings figure
    covering two columns.
    """
    columns = ["period", "available", "reserved", "investments"]
    detail = plan_by_period(plan, accounts, periods, adjustments)
    if detail.empty:
        return pd.DataFrame(columns=columns)

    earmarked = set()
    if "exclude_from_savings" in accounts.columns:
        flag = accounts["exclude_from_savings"].fillna(False).astype(bool)
        earmarked = set(accounts.loc[flag, "name"])

    rows = []
    for period in periods:
        mine = detail[detail["period"] == period]
        savings = mine[mine["kind"] == "Savings"]
        rows.append(
            {
                "period": period,
                "available": savings.loc[
                    ~savings["account"].isin(earmarked), "amount"
                ].sum() or Decimal("0"),
                "reserved": savings.loc[
                    savings["account"].isin(earmarked), "amount"
                ].sum() or Decimal("0"),
                "investments": mine.loc[mine["kind"] == "Investments", "amount"].sum()
                or Decimal("0"),
            }
        )
    return pd.DataFrame(rows, columns=columns)


# ------------------------------------------------------------------ expected salary


def period_start(period: str) -> dt.date:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1)


# ---------------------------------------------------------------------- salary parameters
#
# The figures behind a payslip that are policy rather than pay: what proportion of base goes
# into the pension, what the car allowance is worth, what the home working allowance is.
# Held as plain settings because they are not year-scoped the way the tax bands are -- and
# deliberately apart from `salary_assumption`, so editing them cannot disturb a threshold.

SALARY_PARAMETERS = {
    "pension_rate": Decimal("10"),               # % of base salary
    "home_working_allowance": Decimal("24"),     # per month, not taxable
    "holiday_pay_monthly": Decimal("187"),       # per month, deducted before tax
    "car_allowance_threshold": Decimal("50000"),
    "car_allowance_lower_rate": Decimal("12"),   # % of base up to the threshold
    "car_allowance_upper_rate": Decimal("5"),    # % of base above it
}


def salary_parameters(settings: dict) -> dict[str, Decimal]:
    """The stored salary parameters, falling back to the defaults for anything unset."""
    out = {}
    for key, default in SALARY_PARAMETERS.items():
        value = settings.get(key)
        try:
            out[key] = Decimal(str(value)) if value not in (None, "") else default
        except (ArithmeticError, ValueError):
            out[key] = default
    return out


# The plan's L4, 'Investment return (annual)'. Stored as a percentage like every other rate
# since v3, and read back as a fraction because that is what the compounding wants.
INVESTMENT_RETURN_KEY = "investment_return_annual"
DEFAULT_INVESTMENT_RETURN = Decimal("6")


def investment_return_rate(settings: dict) -> Decimal:
    """The expected annual investment return, as a fraction."""
    value = settings.get(INVESTMENT_RETURN_KEY)
    try:
        percent = (
            Decimal(str(value)) if value not in (None, "") else DEFAULT_INVESTMENT_RETURN
        )
    except (ArithmeticError, ValueError):
        percent = DEFAULT_INVESTMENT_RETURN
    return percent / 100


def car_allowance(base: Decimal, params: dict[str, Decimal] | None = None) -> Decimal:
    """The annual car allowance: a percentage of base up to a threshold, less above it.

    12% of the first 50,000 plus 5% of anything over, so a base of 118,905 gives
    6,000 + 3,445.25 = 9,445.25 -- the Tax Calculator's B20, 'Annual (non-pen)'. Non-pensionable,
    which is the whole reason it has to be told apart from base rather than added to it.
    """
    params = params or dict(SALARY_PARAMETERS)
    base = Decimal(base)
    threshold = params["car_allowance_threshold"]
    lower = params["car_allowance_lower_rate"] / HUNDRED
    upper = params["car_allowance_upper_rate"] / HUNDRED
    if base <= threshold:
        return base * lower
    return threshold * lower + (base - threshold) * upper


def base_in_force(profiles: pd.DataFrame, on: dt.date) -> Decimal | None:
    """The annual *base* salary applying on a date -- the last change on or before it.

    The workbook repeated the figure down all twelve rows of column O, so a pay rise meant
    editing every month from that point and hoping none were missed.
    """
    if profiles.empty:
        return None
    applicable = profiles[pd.to_datetime(profiles["effective_from"]).dt.date <= on]
    if applicable.empty:
        return None
    latest = applicable.sort_values("effective_from").iloc[-1]
    base = latest["base_salary"]
    if base is None or pd.isna(base):
        return None
    return Decimal(base)


def salary_in_force(
    profiles: pd.DataFrame, on: dt.date, params: dict[str, Decimal] | None = None
) -> Decimal | None:
    """Base plus car allowance -- what used to be stored as the annual salary."""
    base = base_in_force(profiles, on)
    if base is None:
        return None
    return base + car_allowance(base, params)


def bonus_for(period: str, bonuses: pd.DataFrame) -> Decimal:
    if bonuses.empty:
        return Decimal("0")
    match = bonuses[bonuses["period"] == period]
    return Decimal(match["amount"].iloc[0]) if not match.empty else Decimal("0")


def expected_gross(
    period: str,
    profiles: pd.DataFrame,
    bonuses: pd.DataFrame,
    params: dict[str, Decimal] | None = None,
) -> Decimal | None:
    """Salary tracker column P: the annual salary in force / 12, plus any bonus that month.

    May's cell was `=ROUND(O5/12,2)+29028.48` -- the bonus typed into the formula. With the
    bonus held as data the derivation works for every month, so expected gross no longer has
    to be stored alongside the inputs that produce it.

    'Salary' here is base plus car allowance, which is what the single stored figure always
    was. It excludes the home working allowance, which is paid on top -- see salary_components.
    """
    salary = salary_in_force(profiles, period_start(period), params)
    if salary is None:
        return None
    monthly = (Decimal(salary) / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return monthly + bonus_for(period, bonuses)


def salary_components(
    period: str,
    profiles: pd.DataFrame,
    bonuses: pd.DataFrame,
    params: dict[str, Decimal] | None = None,
    holiday_pay: Decimal | None = None,
) -> tax.Components | None:
    """A month's expected pay, decomposed -- the Tax Calculator's A18:D25.

    Pension is a percentage of *base* alone. That is the point of keeping base and car
    allowance apart: charging 10% against the combined 128,350.25 would take 1,069.59 a month
    instead of 990.88, and every figure downstream of it would be wrong by the difference.

    `holiday_pay` overrides the parameterised figure, so a month with a real payslip is
    modelled against what was actually deducted rather than the standing assumption.
    """
    params = params or dict(SALARY_PARAMETERS)
    base = base_in_force(profiles, period_start(period))
    if base is None:
        return None

    twelfth = lambda value: (Decimal(value) / 12).quantize(  # noqa: E731
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return tax.Components(
        base=twelfth(base),
        car=twelfth(car_allowance(base, params)),
        bonus=bonus_for(period, bonuses),
        home_working=params["home_working_allowance"],
        pension=(base * params["pension_rate"] / HUNDRED / 12).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ),
        holiday_pay=(
            params["holiday_pay_monthly"] if holiday_pay is None else Decimal(holiday_pay)
        ),
    )


def cumulative_tax(
    frame: pd.DataFrame, bands_for, tax_year: int | None = None
) -> pd.DataFrame:
    """The year-to-date PAYE position, month by month, on HMRC's cumulative basis.

    `frame` needs a row per period with `taxable`, `actual_paye` and `expected_paye`;
    `bands_for` maps a period to the bands in force at its start. Rows without a taxable
    figure are dropped -- a month the model cannot build says nothing about the year.

    Actual PAYE is used where a payslip has been entered and the model's where it has not, so
    the closing row is a projection of where the year lands rather than a statement about
    months that have not happened. `actual` records which it was, so the table can say so.

    Charged and deducted are two different questions, and this answers only the first. See
    tax.year_to_date: payroll here bills each month on its own bands, and a bonus month
    therefore throws pay at the additional rate that a full year's bands would have caught
    lower down. The gap that opens up is not an error in either calculation -- it is the
    overpayment HMRC settles after 5 April.
    """
    columns = [
        "period", "month", "tax_year", "taxable", "taxable_to_date",
        "due", "due_to_date", "deducted", "deducted_to_date", "difference", "actual",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    rows = frame[frame["taxable"].notna()].copy()
    if tax_year is not None:
        rows = rows[rows["period"].map(tax_year_of) == tax_year]
    if rows.empty:
        return pd.DataFrame(columns=columns)

    rows = rows.sort_values("period")
    out: list[dict] = []

    for year, group in rows.groupby(rows["period"].map(tax_year_of), sort=True):
        entries = []
        for _, row in group.iterrows():
            paid = row.get("actual_paye")
            is_actual = paid is not None and pd.notna(paid)
            modelled = row.get("expected_paye")
            deducted = (
                Decimal(str(paid)) if is_actual
                else (Decimal(str(modelled)) if pd.notna(modelled) else Decimal("0"))
            )
            entries.append(
                (
                    Decimal(str(row["taxable"])),
                    bands_for(row["period"]),
                    period_start(row["period"]),
                    deducted,
                    bool(is_actual),
                )
            )

        for period, point in zip(group["period"], tax.year_to_date(entries)):
            out.append(
                {
                    "period": period,
                    "month": period_label(period),
                    "tax_year": year,
                    "taxable": point.taxable,
                    "taxable_to_date": point.taxable_to_date,
                    "due": point.due,
                    "due_to_date": point.due_to_date,
                    "deducted": point.deducted,
                    "deducted_to_date": point.deducted_to_date,
                    "difference": point.difference,
                    "actual": point.actual,
                }
            )

    return pd.DataFrame(out, columns=columns)


def rate_in_force(rates: pd.DataFrame, kind: str, on: dt.date) -> Decimal:
    """The cycling rate applying to a day -- the last change on or before it."""
    if rates.empty:
        return Decimal("0")
    mine = rates[rates["kind"] == kind]
    mine = mine[pd.to_datetime(mine["effective_from"]).dt.date <= on]
    if mine.empty:
        return Decimal("0")
    return Decimal(mine.sort_values("effective_from").iloc[-1]["amount"])


def cycling_savings_dated(days: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    """Saving per day ridden, at the rate in force on that day.

    Priority order is the workbook's: commute, then band, then gym, so a day flagged twice
    counts once at the higher rate.
    """
    if days.empty:
        return days.assign(saving=[], kind=[])

    def classify(row) -> str:
        for key in ("commute", "band", "gym"):
            if row[key]:
                return key
        return ""

    out = days.copy()
    out["kind_key"] = out.apply(classify, axis=1)
    out["saving"] = out.apply(
        lambda r: rate_in_force(rates, r["kind_key"], pd.to_datetime(r["date"]).date())
        if r["kind_key"]
        else Decimal("0"),
        axis=1,
    )
    out["kind"] = out["kind_key"].map(
        {"commute": "Commute", "band": "Band", "gym": "Gym", "": "None"}
    )
    return out.drop(columns="kind_key")


# --------------------------------------------------------------- credit card position


def previous_period(period: str) -> str | None:
    year, month = (int(p) for p in period.split("-"))
    if month == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month - 1:02d}"


def month_end(period: str) -> dt.date:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _clamp_day(year: int, month: int, day: int) -> dt.date:
    """A day-of-month that exists: the 31st of a card's cycle lands on the 30th in April."""
    return dt.date(year, month, min(int(day), calendar.monthrange(year, month)[1]))


def billing_cycle(statement_day, payment_day, on: dt.date):
    """Which statement stands against a card on a date, when it was issued and when it is due.

    A card bills on the same day each month and collects on another. Where the payment day
    falls after the statement day the two sit inside one month -- Platinum Amex issues on the
    16th and is paid on the 30th. Where it falls before, the bill is settled the *following*
    month: BA Amex issues on the 26th and is not collected until the 9th, so the balance
    stands net of that bill from the 26th of one month to the 9th of the next.

    Returns (period of the bill, issue date, due date), or None when either day is unset --
    without both there is no way to say whether what is owed has been billed yet.
    """
    if (
        statement_day is None
        or payment_day is None
        or pd.isna(statement_day)
        or pd.isna(payment_day)
    ):
        return None
    statement_day, payment_day = int(statement_day), int(payment_day)

    issued = _clamp_day(on.year, on.month, statement_day)
    if issued > on:
        earlier = month_add(f"{on.year:04d}-{on.month:02d}", -1)
        year, month = (int(p) for p in earlier.split("-"))
        issued = _clamp_day(year, month, statement_day)

    if payment_day > statement_day:
        due = _clamp_day(issued.year, issued.month, payment_day)
    else:
        later = month_add(f"{issued.year:04d}-{issued.month:02d}", 1)
        year, month = (int(p) for p in later.split("-"))
        due = _clamp_day(year, month, payment_day)

    return f"{issued.year:04d}-{issued.month:02d}", issued, due


def card_outstanding(
    balances: pd.DataFrame,
    statements: pd.DataFrame,
    accounts: pd.DataFrame,
    period: str,
    today: dt.date | None = None,
) -> pd.DataFrame:
    """Month-tab B42:E52 -- what is owed on a card on top of the bill it is about to pay.

    Three states, which the card's own two dates decide:

        before the statement is issued   outstanding = balance
        after it is issued, before it is paid   outstanding = balance - bill
        after it is paid                 outstanding = balance

    So the figure is the spending that has not yet reached a statement. The bill subtracted
    is whichever one is currently standing, which for a card paid the following month is the
    *previous* month's -- not the one in this month's column. The workbook's D52 tried to
    reach the same answer from the month tab alone and could not: it took whichever bill the
    day of the month suggested without ever asking when the payment actually left, so a card
    settled mid-month still showed its bill deducted at the month end.

    The position is read as at today for the current month, and as at the month end for any
    other -- a past month's answer should not keep moving.
    """
    today = today or dt.date.today()
    cards = accounts[accounts["type"] == "credit_card"]
    if cards.empty:
        return pd.DataFrame(
            columns=["account", "closing", "statement", "awaiting", "outstanding",
                     "position", "as_of"]
        )

    as_of = today if period_of(today) == period else month_end(period)
    by_account = balances.set_index("account") if not balances.empty else None

    def bill(account_id: int, for_period: str | None) -> Decimal:
        if statements.empty or for_period is None:
            return Decimal("0")
        match = statements[
            (statements["account_id"] == account_id) & (statements["period"] == for_period)
        ]
        return Decimal(match["bill_eom"].iloc[0]) if not match.empty else Decimal("0")

    rows = []
    for _, card in cards.iterrows():
        name = card["name"]
        closing = (
            by_account.loc[name, "closing"]
            if by_account is not None and name in by_account.index
            else Decimal("0")
        )

        cycle = billing_cycle(card["statement_day"], card["payment_day"], as_of)
        if cycle is None:
            awaiting = Decimal("0")
            position = "No statement or payment day set"
        else:
            bill_period, issued, due = cycle
            if as_of < due:
                awaiting = bill(int(card["id"]), bill_period)
                position = f"Billed {issued:%d %b}, due {due:%d %b}"
            else:
                awaiting = Decimal("0")
                position = f"Paid {due:%d %b} — next bill not yet issued"

        rows.append(
            {
                "account": name,
                "closing": closing,
                "statement": bill(int(card["id"]), period),
                "awaiting": awaiting,
                "outstanding": Decimal(closing) - Decimal(awaiting),
                "position": position,
                "as_of": as_of,
            }
        )
    return sort_human(pd.DataFrame(rows), by="account")


# -------------------------------------------------------------------- account targets


def account_target_table(
    balances: pd.DataFrame, targets: pd.DataFrame, accounts: pd.DataFrame, period: str
) -> pd.DataFrame:
    """Summary B21:E26 -- what each account should hold this month against what it does.

    'Current' is the account's closing balance, which matches the workbook's hand-entered
    column exactly for all four accounts in July.
    """
    mine = targets[targets["period"] == period] if not targets.empty else pd.DataFrame()
    if mine.empty:
        return pd.DataFrame(columns=["account", "target", "current", "required"])

    by_id = accounts.set_index("id")["name"].to_dict()
    by_account = (
        balances.set_index("account")["closing"].to_dict() if not balances.empty else {}
    )

    rows = []
    for _, row in mine.iterrows():
        name = by_id.get(row["account_id"])
        if name is None:
            continue
        current = Decimal(by_account.get(name, Decimal("0")))
        rows.append(
            {
                "account": name,
                "target": row["amount"],
                "current": current,
                "required": Decimal(row["amount"]) - current,
            }
        )
    return sort_human(pd.DataFrame(rows), by="account")


# ------------------------------------------------------- savings and investments series


def savings_series(
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    targets: pd.DataFrame,
    periods: list[str],
    today: dt.date | None = None,
) -> pd.DataFrame:
    """Summary B2:O15 -- savings and investments month by month.

    One row per month, opening and closing side by side, so a month can be read across
    without holding the previous row in your head. The workbook instead led with a standalone
    opening row and then gave closing balances only, which is why 'Added' had to be inferred
    by subtracting the row above.

    Two accumulating figures:

        target_eom    the monthly targets summed to date
        required      that cumulative target less what is actually available

    `required` is positive when the target is ahead of the balance, so a positive number is
    money still to find. It compares a running total of contributions against a balance, so
    it is only meaningful measured from the month the targets start in.
    """
    today = today or dt.date.today()
    flag = accounts["exclude_from_savings"]
    excluded_names = set(accounts.loc[flag.fillna(False).astype(bool), "name"])

    lookup = targets.set_index("period") if not targets.empty else None

    def target_for(period: str, column: str) -> Decimal:
        if lookup is None or period not in lookup.index:
            return Decimal("0")
        value = lookup.loc[period, column]
        return Decimal("0") if pd.isna(value) else Decimal(value)

    rows: list[dict] = []
    savings_to_date = investments_to_date = Decimal("0")

    for period in periods:
        balances = account_balances(postings, openings, period, accounts)
        if balances.empty:
            continue
        savings_rows = balances[balances["is_savings"]]
        invest_rows = balances[balances["is_investment"]]
        available = savings_rows[~savings_rows["account"].isin(excluded_names)]
        # The earmarked pots. Money going in here is still saved, but it is already spoken
        # for, so adding 200 to the wedding pot and 200 to the general one are not the same
        # event -- which a single 'Added' column could not say.
        reserved = savings_rows[savings_rows["account"].isin(excluded_names)]

        def total(frame: pd.DataFrame, column: str) -> Decimal:
            return Decimal(frame[column].sum() or 0)

        savings_bom = total(savings_rows, "opening")
        savings_eom = total(savings_rows, "closing")
        available_bom = total(available, "opening")
        available_eom = total(available, "closing")
        reserved_bom = total(reserved, "opening")
        reserved_eom = total(reserved, "closing")
        investments_bom = total(invest_rows, "opening")
        investments_eom = total(invest_rows, "closing")

        savings_target = target_for(period, "savings")
        investments_target = target_for(period, "investments")
        savings_to_date += savings_target
        investments_to_date += investments_target

        rows.append(
            {
                "period": period,
                "date": month_end(period),
                "month": period_label(period),
                "started": period_start(period) <= today,
                "savings_bom": savings_bom,
                "available_bom": available_bom,
                "reserved_bom": reserved_bom,
                "available_added": available_eom - available_bom,
                "reserved_added": reserved_eom - reserved_bom,
                "savings_added": savings_eom - savings_bom,
                "savings_eom": savings_eom,
                "available_eom": available_eom,
                "reserved_eom": reserved_eom,
                "savings_target": savings_target,
                "savings_target_eom": savings_to_date,
                "savings_required": savings_to_date - available_eom,
                # The same cumulative target measured against each of the three balances,
                # so the Savings table can switch basis without three sets of targets: what
                # changes is which pot is being asked to meet it, not the figure to meet.
                "total_required": savings_to_date - savings_eom,
                "available_required": savings_to_date - available_eom,
                "reserved_required": savings_to_date - reserved_eom,
                "investments_bom": investments_bom,
                "investments_added": investments_eom - investments_bom,
                "investments_eom": investments_eom,
                "investments_target": investments_target,
                "investments_target_eom": investments_to_date,
                "investments_required": investments_to_date - investments_eom,
                "combined": savings_eom + investments_eom,
                "combined_available": available_eom + investments_eom,
            }
        )

    return pd.DataFrame(rows)


# ------------------------------------------------------------------- investment return
#
# The tracker's 'Investment Return' tab. Its balances were typed in month by month and its
# contributions were assumed -- a hard-coded 250 and 100 every row, whatever was actually
# paid in. Both are in the ledger already: a contribution is a transfer into the account and
# a valuation change is a credit or debit commented 'Investment return', so
#
#     closing = opening + contributions + return
#
# and the monthly return is `return / opening`, which is the same thing its
# `=IF(C5>0, E5/C4-1, "")` computed, but from what happened rather than from what was planned.


def investment_return_series(
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    periods: list[str],
) -> pd.DataFrame:
    """One row per investment account per month.

    `periods` decides the span, so the table begins at the earliest month in the database and
    extends as history is backfilled -- the tracker's start was pinned to a row number.
    """
    columns = [
        "period", "month", "date", "account", "opening", "contributions", "gain",
        "closing", "monthly_return",
    ]
    rows = []
    for period in periods:
        balances = account_balances(postings, openings, period, accounts)
        if balances.empty:
            continue
        for _, r in balances[balances["is_investment"]].iterrows():
            # Contributions are the transfers: money moved in from your own accounts. The
            # credits and debits are the valuation moving, which is the return.
            contributions = r["transfer_in"] - r["transfer_out"]
            gain = r["paid_in"] - r["paid_out"]
            opening = r["opening"]
            rows.append(
                {
                    "period": period,
                    "month": period_label(period),
                    "date": month_end(period),
                    "account": r["account"],
                    "opening": opening,
                    "contributions": contributions,
                    "gain": gain,
                    "closing": r["closing"],
                    "monthly_return": (
                        gain / opening if opening else None
                    ),
                }
            )
    return pd.DataFrame(rows, columns=columns)


def investment_return_summary(
    series: pd.DataFrame, today: dt.date | None = None
) -> pd.DataFrame:
    """The tracker's L3:N10, one column per account.

    Everything is measured to today rather than to the end of the series, so a plan that runs
    ahead of itself does not report returns on months that have not happened. `annualised`
    scales the whole-period return by the months elapsed, which is its
    `=(1+M8)^(12/M9)-1` -- meaningful over a year, noisy over three months, and reported as
    such rather than hidden.
    """
    columns = [
        "account", "start", "current", "contributions", "gain", "net", "total_return",
        "months", "annualised",
    ]
    if series.empty:
        return pd.DataFrame(columns=columns)

    today = today or dt.date.today()
    to_date = series[series["date"].map(lambda d: d <= today)]
    if to_date.empty:
        to_date = series[series["period"] == series["period"].min()]

    rows = []
    for account, group in to_date.groupby("account"):
        group = group.sort_values("period")
        start = group.iloc[0]["opening"]
        current = group.iloc[-1]["closing"]
        contributions = group["contributions"].sum() or Decimal("0")
        gain = group["gain"].sum() or Decimal("0")
        months = len(group)
        # Net of what was put in, so growth is not flattered by the standing order. The
        # tracker's 'Net'.
        net = current - contributions
        total = (net / start - 1) if start else None
        annualised = None
        if total is not None and months:
            annualised = (1 + total) ** (Decimal(12) / Decimal(months)) - 1
        rows.append(
            {
                "account": account,
                "start": start,
                "current": current,
                "contributions": contributions,
                "gain": gain,
                "net": net,
                "total_return": total,
                "months": months,
                "annualised": annualised,
            }
        )
    return sort_human(pd.DataFrame(rows, columns=columns), by="account")


def monthly_rate(annual: Decimal) -> Decimal:
    """The monthly equivalent of an annual rate, compounded: (1+r)^(1/12)-1.

    The plan's L5. Not annual/12 -- that overstates the monthly figure, and over twelve
    months compounds to more than the rate it started from.
    """
    return (1 + Decimal(annual)) ** (Decimal(1) / Decimal(12)) - 1


# -------------------------------------------------------------- interest and donations
#
# Both replace a sheet of the Savings interest tracker, and both are aggregated by *tax
# year* rather than by month or by fiscal period -- HMRC's year, running 6 April to 5 April.
# The tracker could not express that boundary, so it split April into two hand-labelled rows
# ('Apr (before 6th)' and 'Apr (after 6th)') and filed them under different years. Here the
# date decides, which is why `tax_year_of_date` exists alongside `tax_year_of`.

INTEREST_CATEGORY = "Interest"


def _signed_by_type(frame: pd.DataFrame) -> pd.Series:
    """Credits add, debits subtract. Interest charged is rare but not impossible."""
    return pd.Series(
        [
            -amount if kind == "Debit" else amount
            for amount, kind in zip(frame["amount"], frame["type"])
        ],
        index=frame.index,
        dtype=object,
    )


def _live(transactions: pd.DataFrame) -> pd.DataFrame:
    if "deleted" in transactions.columns:
        return transactions[~transactions["deleted"].fillna(False).astype(bool)]
    return transactions


def interest_by_tax_year(
    transactions: pd.DataFrame,
    accounts: pd.DataFrame,
    category: str = INTEREST_CATEGORY,
) -> pd.DataFrame:
    """Interest received, one row per account per tax year.

    The interest tracker held this as a grid typed in from statements. Nothing new is needed
    here: interest is already in the ledger as a credit under the Interest category, so this
    is a grouping of transactions rather than a second record of them.

    'basis' is the account's own gross/net flag, which is what the tracker's row 3 held.
    """
    columns = ["tax_year", "year", "account", "basis", "amount"]
    mine = _live(transactions)
    mine = mine[mine["category"].astype("string").eq(category)]
    if mine.empty:
        return pd.DataFrame(columns=columns)

    net_accounts = set()
    if "interest_net" in accounts.columns:
        flag = accounts["interest_net"].fillna(False).astype(bool)
        net_accounts = set(accounts.loc[flag, "name"])

    rows = pd.DataFrame(
        {
            "tax_year": mine["date"].map(tax_year_of_date),
            "account": mine["account_from"],
            "amount": _signed_by_type(mine),
        }
    )
    grouped = rows.groupby(["tax_year", "account"], as_index=False)["amount"].sum()
    grouped["basis"] = grouped["account"].map(
        lambda name: "Net" if name in net_accounts else "Gross"
    )
    grouped["year"] = grouped["tax_year"].map(tax_year_label)
    return sort_human(grouped[columns], by=["tax_year", "account"])


def interest_totals(rows: pd.DataFrame) -> pd.DataFrame:
    """Gross and net side by side for each tax year.

    Kept apart rather than summed into one figure: the two are not interchangeable at tax
    time, which is the whole reason the flag exists.
    """
    columns = ["tax_year", "year", "gross", "net", "total", "accounts"]
    if rows.empty:
        return pd.DataFrame(columns=columns)

    out = []
    for year, group in rows.groupby("tax_year"):
        gross = group.loc[group["basis"] == "Gross", "amount"].sum() or Decimal("0")
        net = group.loc[group["basis"] == "Net", "amount"].sum() or Decimal("0")
        out.append(
            {
                "tax_year": year,
                "year": tax_year_label(year),
                "gross": gross,
                "net": net,
                "total": gross + net,
                "accounts": int((group["amount"] != 0).sum()),
            }
        )
    return pd.DataFrame(out, columns=columns).sort_values("tax_year")


def donations(transactions: pd.DataFrame) -> pd.DataFrame:
    """Every payment flagged as a donation, with its tax year.

    Flagged on the transaction rather than inferred from a category, because the category
    cannot tell them apart: a donation and the platform's transaction fee are one payment out
    of the account, and only one of the two is a gift. Transaction 582 was a single 35.70
    holding both.
    """
    columns = ["tax_year", "year", "id", "date", "account", "amount", "comment"]
    mine = _live(transactions)
    if "is_donation" not in mine.columns:
        return pd.DataFrame(columns=columns)
    mine = mine[mine["is_donation"].fillna(False).astype(bool)]
    if mine.empty:
        return pd.DataFrame(columns=columns)

    out = pd.DataFrame(
        {
            "tax_year": mine["date"].map(tax_year_of_date),
            "id": mine["id"],
            "date": mine["date"],
            "account": mine["account_from"],
            "amount": mine["amount"],
            "comment": mine["comment"],
        }
    )
    out["year"] = out["tax_year"].map(tax_year_label)
    return out[columns].sort_values(["tax_year", "date"])


def donations_by_tax_year(transactions: pd.DataFrame) -> pd.DataFrame:
    columns = ["tax_year", "year", "amount", "count"]
    given = donations(transactions)
    if given.empty:
        return pd.DataFrame(columns=columns)
    out = given.groupby(["tax_year", "year"], as_index=False).agg(
        amount=("amount", "sum"), count=("id", "size")
    )
    return out[columns].sort_values("tax_year")


# ---------------------------------------------------------------- spending calculation


# Salary tracker I20/I21: bills are one grouping; other costs are the rest of the regular
# outgoings less the two lines counted elsewhere -- credit cards go to savings, and food is
# entered separately.
BILLS_GROUPING = "Household bills"
OTHER_GROUPING = "Regular outgoings"
CREDIT_CARD_CATEGORY = "Credit cards"
FOOD_CATEGORY = "Food"


def spending_calculation(
    budgets: pd.DataFrame,
    categories: pd.DataFrame,
    net_salary: Decimal,
    period: str,
    rent: Decimal = Decimal("0"),
    savings: Decimal = Decimal("0"),
    food: Decimal = Decimal("0"),
    essentials: Decimal = Decimal("0"),
    days: int = 30,
) -> dict[str, Decimal]:
    """Salary tracker H17:I28 -- what is left to spend per day.

        card limit    = net salary + rent - bills - other costs - savings
        net (monthly) = card limit - food - essentials
        net (daily)   = net (monthly) / 30

    Bills and other costs come from the month's expected costs; the rest are inputs. The
    division is by 30 whatever the month's length, as the workbook had it -- this is a
    spending allowance, not an apportionment of the month.

    The savings line is the input *plus* the budgeted credit-card repayment (the workbook's
    `=1000+Monthly_Template!D16`). That line is deliberately taken out of other costs and
    counted here instead, because clearing a card balance is saving rather than spending;
    leaving it out of both would quietly drop it from the calculation altogether.
    """
    month_budgets = budgets[budgets["period"] == period]
    grouping = (
        categories.set_index("name")["grouping"].to_dict() if not categories.empty else {}
    )

    def expected(predicate) -> Decimal:
        if month_budgets.empty:
            return Decimal("0")
        mask = month_budgets["category"].map(predicate).fillna(False).astype(bool)
        total = month_budgets.loc[mask, "expected"].dropna().sum()
        return Decimal(total) if total else Decimal("0")

    bills = expected(lambda c: grouping.get(c) == BILLS_GROUPING)
    other = expected(
        lambda c: grouping.get(c) == OTHER_GROUPING
        and c not in (CREDIT_CARD_CATEGORY, FOOD_CATEGORY)
    )
    card_repayment = expected(lambda c: c == CREDIT_CARD_CATEGORY)
    savings_total = savings + card_repayment

    card_limit = net_salary + rent - bills - other - savings_total
    monthly = card_limit - food - essentials
    return {
        "net_salary": net_salary,
        "rent": rent,
        "bills": bills,
        "other": other,
        "savings_input": savings,
        "card_repayment": card_repayment,
        "savings": savings_total,
        "food": food,
        "essentials": essentials,
        "card_limit": card_limit,
        "monthly": monthly,
        "daily": (monthly / days).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    }


def reconcile_monthly_annual(
    monthly, annual, was_monthly, was_annual, tolerance: float = 0.005
) -> Decimal | None:
    """Which of a paired monthly/annual entry to believe, as a monthly figure.

    The two are the same number at different scales, so an edit to either implies the other.
    Whichever moved is the one meant; if both moved in a single save the annual value wins,
    because that is the figure tax bands are actually published as and the one a person is
    more likely to have copied from a payslip or HMRC.

    Returns None when neither moved, so an unchanged row is not rewritten.
    """

    def blank(value) -> bool:
        return value is None or pd.isna(value)

    def same(left, right) -> bool:
        if blank(left) or blank(right):
            return blank(left) and blank(right)
        return abs(float(left) - float(right)) < tolerance

    annual_changed = not same(annual, was_annual)
    monthly_changed = not same(monthly, was_monthly)

    if annual_changed:
        return Decimal(str(0 if blank(annual) else annual)) / 12
    if monthly_changed:
        return Decimal(str(0 if blank(monthly) else monthly))
    return None
