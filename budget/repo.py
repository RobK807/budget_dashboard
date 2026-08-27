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
    ImportRule,
    OpeningBalance,
    Payslip,
    PensionContribution,
    PensionPot,
    PensionValuation,
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
         "display_order", "valid_from", "valid_to", "savings_seed"],
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


def _live_in(acct, period: str) -> bool:
    """Whether an account was open at any point in a month.

    Both ends are inclusive of the month: an account opened on the 20th, or closed on the
    3rd, was live in that month and its figures belong in it.
    """
    start, end = period_start(period), month_end(period)
    opened = acct.get("valid_from")
    closed = acct.get("valid_to")
    if opened is not None and not pd.isna(opened) and opened > end:
        return False
    if closed is not None and not pd.isna(closed) and closed < start:
        return False
    return True


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
        if not _live_in(acct, period) and mine.empty and not opening.get(acct["name"]):
            # An account that had not opened yet, or had already closed, and has nothing to
            # show for the month either way. Before the 25-26 backfill every account ran the
            # whole of the only year there was, so this never arose; now five accounts closed
            # in 2025 would otherwise sit at 0.00 in every month of 2026-27, and half a dozen
            # that opened later would do the same in reverse.
            #
            # Emptiness is required as well as the dates, so a balance can never vanish
            # because a valid_to was typed a month early -- an account still holding money
            # keeps its row, and looks like the mistake it is.
            continue
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
    columns = ["period", "payday", "gross", "car_allowance", "ni", "holiday_pay",
               "cycle_to_work", "paye", "net", "salary", "expected_gross", "benefits",
               "additional"]
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

# The year's tax inputs that no payslip carries: a benefit in kind, dividends, and the two
# allowances and two Gift Aid rates the annual summary needs. Held as salary assumptions
# because they are per tax year and effective-dated exactly as the bands are -- the savings
# allowance is 1,000 for a basic-rate taxpayer and 500 for a higher-rate one, so it is a
# parameter and not a constant. Interest is absent deliberately: it is in the ledger already.
ANNUAL_TAX_DEFAULTS: dict[str, Decimal] = {
    "annual_benefits": Decimal("0"),
    "annual_dividends": Decimal("0"),
    "savings_allowance": Decimal("500"),
    "dividend_allowance": Decimal("1000"),
    "gift_aid_higher": Decimal("20"),
    "gift_aid_additional": Decimal("25"),
}


def annual_tax_inputs(assumptions: pd.DataFrame) -> dict[str, Decimal]:
    """The stored annual tax inputs for a year, defaulted where nothing has been entered.

    Defaulted rather than zeroed: a year with no stored savings allowance has the statutory
    one, not none of it. Zero would quietly tax the first 500 of interest.
    """
    out = dict(ANNUAL_TAX_DEFAULTS)
    if assumptions is None or assumptions.empty:
        return out
    for key in ANNUAL_TAX_DEFAULTS:
        rows = assumptions[assumptions["key"] == key]
        if not rows.empty:
            out[key] = Decimal(str(rows.sort_values("effective_from").iloc[-1]["value"]))
    return out


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

    A stated opening **replaces** what was carried forward rather than adding to it. While
    there was only ever one year in the database the two could not both apply, so adding was
    indistinguishable from replacing; backfilling 25-26 put a real March underneath April
    2026 and the chain then counted the year-end twice, opening at -5,255.13 where it should
    have been -2,603.34.

    Replacing is also what keeps the current year still reconciling. The derived March close
    is 19.34 from the stated April opening -- the 25-26 differences the reconciliation
    accepts, which are real and deliberate -- so preferring the derived figure would move
    every 2026-27 Excess total off the workbook it is checked against. An explicit opening
    beats an inferred one.
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
                    opening[name] = stated.loc[(period, name)]

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


def load_import_rules(session: Session) -> pd.DataFrame:
    """The description patterns that name the other side of a transfer.

    Longest pattern first, so a specific rule beats a general one that contains it --
    'HSBC CARD PYMT' must win over a bare 'HSBC'.
    """
    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    rows = [
        {
            "id": r.id,
            "pattern": r.pattern,
            "account_id": r.account_id,
            "account": accounts.get(r.account_id),
            "note": r.note,
        }
        for r in session.scalars(select(ImportRule))
    ]
    rows.sort(key=lambda r: (-len(r["pattern"] or ""), (r["pattern"] or "").lower()))
    return pd.DataFrame(rows, columns=["id", "pattern", "account_id", "account", "note"])


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
    live = _live_months(accounts, periods)
    rows = []

    if not plan.empty:
        for period in periods:
            for _, row in plan_in_force(plan, period_start(period)).iterrows():
                if period not in live.get(row["account"], set()):
                    continue
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
            if row["period"] not in live.get(row["account"], set()):
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


def _live_months(accounts: pd.DataFrame, periods: list[str]) -> dict[str, set[str]]:
    """Which months each account was actually open in.

    A pot cannot be asked to save anything in a month it did not exist in, so a target dated
    outside an account's life does not count. That rule has to be applied once, here, rather
    than at each of the half-dozen places that read a target: the overview, the two
    per-account views and the projection all derive from this, and any of them applying it
    alone would disagree with the rest.

    A target stranded outside an account's life is dropped rather than moved, which makes a
    mis-dated one-off silently disappear -- `targets_outside_account_life` exists so the page
    can say so instead.
    """
    live: dict[str, set[str]] = {}
    for _, account in accounts.iterrows():
        live[account["name"]] = {p for p in periods if _live_in(account, p)}
    return live


def targets_outside_account_life(
    plan: pd.DataFrame,
    accounts: pd.DataFrame,
    periods: list[str],
    adjustments: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Targets that fall outside their own account's open period, and are therefore ignored.

    Almost always a date that is out rather than a deliberate choice: a one-off entered
    against the month the money was expected, when the account was opened the month it
    actually arrived. The amounts involved are lump sums, so losing one quietly moves the
    cumulative target by thousands and nothing on the page says why.
    """
    columns = ["account", "period", "month", "source", "amount", "opened", "closed"]
    live = _live_months(accounts, periods)
    dates = accounts.set_index("name") if not accounts.empty else None
    wanted = set(periods)
    rows = []

    def note(account: str, period: str, source: str, amount) -> None:
        if account in live and period in live[account]:
            return
        opened = closed = None
        if dates is not None and account in dates.index:
            opened = dates.loc[account].get("valid_from")
            closed = dates.loc[account].get("valid_to")
        rows.append(
            {
                "account": account, "period": period, "month": period_label(period),
                "source": source, "amount": amount, "opened": opened, "closed": closed,
            }
        )

    if not plan.empty:
        for period in periods:
            for _, row in plan_in_force(plan, period_start(period)).iterrows():
                if row["amount"]:
                    note(row["account"], period, "Plan", row["amount"])

    if adjustments is not None and not adjustments.empty:
        for _, row in adjustments.iterrows():
            if row["period"] in wanted and row["amount"]:
                note(row["account"], row["period"], "One-off", row["amount"])

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
                # 'Not yet issued' said only that nothing was outstanding. The date it will
                # be issued on is the useful half, and the card bills on the same day each
                # month, so it is one month on from the statement just settled.
                following = month_add(f"{issued.year:04d}-{issued.month:02d}", 1)
                year, month = (int(p) for p in following.split("-"))
                next_bill = _clamp_day(year, month, int(card["statement_day"]))
                position = f"Paid {due:%d %b} — next bill due on {next_bill:%d %b}"

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
    bucket_targets: pd.DataFrame | None = None,
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
    money still to find.

    The cumulative target starts from each account's `savings_seed`, not from zero. It is
    measured against a balance, and a balance does not start at zero either -- these pots had
    years of contributions in them before any of this was recorded. Without a seed the
    comparison is between a running total that starts at nothing and a balance that starts at
    tens of thousands, and every month reads as hugely ahead of target.
    """
    today = today or dt.date.today()
    flag = accounts["exclude_from_savings"]
    excluded_names = set(accounts.loc[flag.fillna(False).astype(bool), "name"])

    def seed_total(mask) -> Decimal:
        if "savings_seed" not in accounts.columns:
            return Decimal("0")
        values = accounts.loc[mask, "savings_seed"].dropna()
        return Decimal(values.sum()) if len(values) else Decimal("0")

    is_savings = accounts["is_savings"].fillna(False).astype(bool)
    is_investment = accounts["is_investment"].fillna(False).astype(bool)
    earmarked_mask = accounts["name"].isin(excluded_names)

    # A seed is what a pot already held, so it belongs to the months that pot existed in and
    # to no others. Computed per month rather than once up front: three accounts closed in
    # 2025 were still contributing 6,000 of seed to every month of 2026, which nothing on the
    # page could account for, and their balances had long since left the totals.
    live_by_period = _live_months(accounts, periods)

    def live_mask(period: str) -> pd.Series:
        open_now = {
            name for name, months in live_by_period.items() if period in months
        }
        return accounts["name"].isin(open_now)

    lookup = targets.set_index("period") if not targets.empty else None
    buckets = (
        bucket_targets.set_index("period")
        if bucket_targets is not None and not bucket_targets.empty
        else None
    )

    def _at(frame, period: str, column: str) -> Decimal:
        if frame is None or period not in frame.index:
            return Decimal("0")
        value = frame.loc[period, column]
        return Decimal("0") if pd.isna(value) else Decimal(value)

    def target_for(period: str, column: str) -> Decimal:
        return _at(lookup, period, column)

    def bucket_for(period: str, column: str) -> Decimal:
        """Falls back to the whole savings target where no split is supplied, which is what
        every caller got before there was one."""
        if buckets is None:
            return target_for(period, "savings") if column == "available" else Decimal("0")
        return _at(buckets, period, column)

    rows: list[dict] = []
    # Targets accumulate; seeds are added back per month from whichever pots are open then.
    savings_to_date = investments_to_date = Decimal("0")
    available_to_date = reserved_to_date = Decimal("0")

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
        # Per bucket as well as in total. A target set against an earmarked pot is not a
        # target the available balance was ever asked to meet, so measuring one against the
        # other reports a shortfall that belongs to neither.
        available_target = bucket_for(period, "available")
        reserved_target = bucket_for(period, "reserved")
        savings_to_date += savings_target
        investments_to_date += investments_target
        available_to_date += available_target
        reserved_to_date += reserved_target

        open_now = live_mask(period)
        savings_target_eom = seed_total(is_savings & open_now) + savings_to_date
        available_target_eom = (
            seed_total(is_savings & open_now & ~earmarked_mask) + available_to_date
        )
        reserved_target_eom = (
            seed_total(is_savings & open_now & earmarked_mask) + reserved_to_date
        )
        investments_target_eom = (
            seed_total(is_investment & open_now) + investments_to_date
        )

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
                "savings_target_eom": savings_target_eom,
                "savings_required": available_target_eom - available_eom,
                # Each basis carries its own target as well as its own balance. They were one
                # figure covering all three, on the reasoning that what changed was which pot
                # had to meet it -- but an earmarked pot's target is not the available
                # balance's to meet, so 'required' was wrong on two of the three bases.
                "total_target": savings_target,
                "available_target": available_target,
                "reserved_target": reserved_target,
                "total_target_eom": savings_target_eom,
                "available_target_eom": available_target_eom,
                "reserved_target_eom": reserved_target_eom,
                "total_required": savings_target_eom - savings_eom,
                "available_required": available_target_eom - available_eom,
                "reserved_required": reserved_target_eom - reserved_eom,
                "investments_bom": investments_bom,
                "investments_added": investments_eom - investments_bom,
                "investments_eom": investments_eom,
                "investments_target": investments_target,
                "investments_target_eom": investments_target_eom,
                "investments_required": investments_target_eom - investments_eom,
                "combined": savings_eom + investments_eom,
                "combined_available": available_eom + investments_eom,
            }
        )

    return pd.DataFrame(rows)


# The five things the page draws, and where each one's two starting points come from. Keyed
# in the order they are read: the two halves of savings, their total, investments, the lot.
PROJECTION_BUCKETS: dict[str, tuple[str, str]] = {
    "available": ("available_eom", "available_target_eom"),
    "reserved": ("reserved_eom", "reserved_target_eom"),
    "savings": ("savings_eom", "total_target_eom"),
    "investments": ("investments_eom", "investments_target_eom"),
    "combined": ("combined", ""),  # combined has no stored cumulative; summed below
}


def savings_projection(
    series: pd.DataFrame,
    plan: pd.DataFrame,
    accounts: pd.DataFrame,
    adjustments: pd.DataFrame | None = None,
    months: int = 12,
) -> pd.DataFrame:
    """Where the balances land if every month from here on hits its target.

    Two lines per bucket, same slope, different starting point:

        <bucket>_actual   the balance as it stands, plus each future month's target
        <bucket>_target   the cumulative target as it stands, plus the same

    The gap between them is `required` at the anchor month -- what is already behind or
    ahead -- and it stays exactly that wide for the whole projection, because both lines gain
    the same amount every month. That is the useful part rather than a defect of the model:
    saving the planned amount from here does not close a gap that has already opened, and a
    chart that quietly converged would say it does.

    The first row is the anchor itself, so both lines start from the same month and the
    reader can see which one begins higher. Future targets come from the plan in force,
    which is what the latest revision says to do from here, including any one-off already
    entered against a future month.
    """
    columns = ["period", "month", "date"] + [
        f"{bucket}_{side}" for bucket in PROJECTION_BUCKETS for side in ("actual", "target")
    ]
    if series.empty or months <= 0:
        return pd.DataFrame(columns=columns)

    anchor = series.iloc[-1]
    running: dict[str, dict[str, Decimal]] = {}
    for bucket, (actual_column, target_column) in PROJECTION_BUCKETS.items():
        if bucket == "combined":
            started_target = Decimal(
                anchor["total_target_eom"]
            ) + Decimal(anchor["investments_target_eom"])
        else:
            started_target = Decimal(anchor[target_column])
        running[bucket] = {
            "actual": Decimal(anchor[actual_column]),
            "target": started_target,
        }

    def snapshot(period: str) -> dict:
        row = {
            "period": period,
            "month": period_label(period),
            "date": month_end(period),
        }
        for bucket, sides in running.items():
            row[f"{bucket}_actual"] = sides["actual"]
            row[f"{bucket}_target"] = sides["target"]
        return row

    rows = [snapshot(anchor["period"])]

    ahead = [month_add(anchor["period"], step) for step in range(1, months + 1)]
    monthly = targets_by_bucket(plan, accounts, ahead, adjustments)
    by_period = monthly.set_index("period") if not monthly.empty else None

    def target_at(period: str, column: str) -> Decimal:
        if by_period is None or period not in by_period.index:
            return Decimal("0")
        value = by_period.loc[period, column]
        return Decimal("0") if pd.isna(value) else Decimal(value)

    for period in ahead:
        available = target_at(period, "available")
        reserved = target_at(period, "reserved")
        investments = target_at(period, "investments")
        step = {
            "available": available,
            "reserved": reserved,
            "savings": available + reserved,
            "investments": investments,
            "combined": available + reserved + investments,
        }
        for bucket, amount in step.items():
            running[bucket]["actual"] += amount
            running[bucket]["target"] += amount
        rows.append(snapshot(period))

    return pd.DataFrame(rows, columns=columns)


def savings_by_account(
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    plan_detail: pd.DataFrame,
    period: str,
    periods: list[str],
) -> pd.DataFrame:
    """One month of the overview, per account rather than per bucket.

    The same six figures the tables above carry -- BoM, Added, EoM, the month's target, the
    cumulative target and what is still Required -- for every savings and investment account.
    The overview says the savings are behind; this says which pot is behind.

    `periods` is the full run from the start, not just the month asked for: the cumulative
    target is every target this account has been set up to and including `period`, so it
    cannot be worked out from one month in isolation. It starts from the account's own
    `savings_seed` for the same reason the overview does -- these pots held money before any
    of it was recorded here, and a running total starting at nothing measured against a
    balance starting at thousands reports every month as wildly ahead.
    """
    columns = [
        "account", "kind", "earmarked", "closed", "bom", "added", "eom",
        "target", "target_eom", "required",
    ]
    balances = account_balances(postings, openings, period, accounts)
    # Driven by the account list rather than by whatever `account_balances` returned. It
    # drops an account that is closed and has nothing to show for the month, which is right
    # for a balance table and wrong here: the account's seed still counts towards the
    # overview's cumulative target, so leaving the row out made the two tables on this page
    # disagree by exactly the seeds of the pots that had been closed. Three closed accounts
    # carrying 2,000 each is a 6,000 gap that nothing on the page explains.
    with_balances = (
        balances.set_index("account") if not balances.empty else None
    )

    pots = accounts[
        accounts["is_savings"].fillna(False).astype(bool)
        | accounts["is_investment"].fillna(False).astype(bool)
    ]
    if pots.empty:
        return pd.DataFrame(columns=columns)

    earmarked = set()
    if "exclude_from_savings" in accounts.columns:
        flag = accounts["exclude_from_savings"].fillna(False).astype(bool)
        earmarked = set(accounts.loc[flag, "name"])

    seeds: dict[str, Decimal] = {}
    if "savings_seed" in accounts.columns:
        for _, row in accounts.iterrows():
            value = row["savings_seed"]
            if value is not None and not pd.isna(value):
                seeds[row["name"]] = Decimal(value)

    # Everything up to and including the month asked for. A target set for a later month is
    # not one this month was asked to meet.
    upto = [p for p in periods if p <= period]
    plan = (
        plan_detail[plan_detail["period"].isin(upto)]
        if plan_detail is not None and not plan_detail.empty
        else pd.DataFrame(columns=["period", "account", "amount"])
    )

    def planned(account: str, only_this_month: bool) -> Decimal:
        if plan.empty:
            return Decimal("0")
        rows = plan[plan["account"] == account]
        if only_this_month:
            rows = rows[rows["period"] == period]
        total = rows["amount"].sum()
        return Decimal("0") if total == 0 else Decimal(total)

    built = []
    for _, row in pots.iterrows():
        name = row["name"]
        held = (
            with_balances.loc[name]
            if with_balances is not None and name in with_balances.index
            else None
        )
        bom = Decimal(held["opening"]) if held is not None else Decimal("0")
        eom = Decimal(held["closing"]) if held is not None else Decimal("0")
        # A seed belongs to the months its pot existed in. Outside them it is not a target
        # the account was ever asked to meet, and counting it reports a shortfall against
        # something that had not opened yet or has already been closed.
        live = _live_in(row, period)
        seed = seeds.get(name, Decimal("0")) if live else Decimal("0")
        target_eom = seed + planned(name, only_this_month=False)
        if held is None and not target_eom and not live:
            continue
        built.append(
            {
                "account": name,
                "kind": "Investments" if row["is_investment"] else "Savings",
                "earmarked": name in earmarked,
                "closed": not live,
                "bom": bom,
                "added": eom - bom,
                "eom": eom,
                "target": planned(name, only_this_month=True),
                "target_eom": target_eom,
                "required": target_eom - eom,
            }
        )

    frame = pd.DataFrame(built, columns=columns)
    return sort_human(frame, by=["kind", "account"]).reset_index(drop=True)


def savings_account_history(
    postings: pd.DataFrame,
    openings: pd.DataFrame,
    accounts: pd.DataFrame,
    plan_detail: pd.DataFrame,
    account: str,
    periods: list[str],
) -> pd.DataFrame:
    """One pot, month by month: what it held against what it was meant to hold.

    The other way round from `savings_by_account`, which takes one month across every
    account. Between them they answer the two questions that follow 'the savings are
    behind' -- which pot, and since when.

    Both figures accumulate from the same starting point as everywhere else on the page: the
    account's `savings_seed`, because the cumulative target is measured against a balance and
    the balance did not start at zero either.

    `periods` is walked in full so the cumulative target is right at every point; the caller
    trims the window it draws afterwards. Trimming first would restart the running total part
    way through and quietly report the account as ahead.
    """
    columns = [
        "period", "month", "date", "bom", "added", "eom",
        "target", "target_eom", "required",
    ]
    # A month with no balance row is reported as zeros below rather than skipped, so that a
    # closed pot still shows the gap it left. That is only sensible for an account that
    # exists: an unknown name would otherwise come back as a full run of zeros, which reads
    # as a real account holding nothing rather than as a typo.
    known = accounts[accounts["name"] == account]
    if known.empty:
        return pd.DataFrame(columns=columns)

    seed = Decimal("0")
    if "savings_seed" in accounts.columns:
        value = known["savings_seed"].iloc[0]
        if value is not None and not pd.isna(value):
            seed = Decimal(value)

    plan = (
        plan_detail[plan_detail["account"] == account]
        if plan_detail is not None and not plan_detail.empty
        else pd.DataFrame(columns=["period", "amount"])
    )

    rows: list[dict] = []
    # Targets only. The seed is added per month below, from whether the pot was open then.
    to_date = Decimal("0")
    for period in periods:
        balances = account_balances(postings, openings, period, accounts)
        if balances.empty:
            continue
        here = balances[balances["account"] == account]
        # A month the account was not open in, or was open in and did nothing, still belongs
        # on the chart once it has a seed or a target: the balance is zero and the gap
        # against target is the whole point of drawing it. Skipping the month instead broke
        # the line where the account closed, which read as 'no data' rather than 'emptied'.
        bom = Decimal(here["opening"].iloc[0]) if not here.empty else Decimal("0")
        eom = Decimal(here["closing"].iloc[0]) if not here.empty else Decimal("0")
        month_target = plan.loc[plan["period"] == period, "amount"].sum()
        month_target = Decimal("0") if month_target == 0 else Decimal(month_target)
        to_date += month_target
        # Same rule as everywhere else: the seed applies only while the pot is open, so the
        # line reads zero before it existed and after it was closed.
        target_eom = (
            (seed + to_date) if _live_in(known.iloc[0], period) else to_date
        )
        rows.append(
            {
                "period": period,
                "month": period_label(period),
                "date": month_end(period),
                "bom": bom,
                "added": eom - bom,
                "eom": eom,
                "target": month_target,
                "target_eom": target_eom,
                "required": target_eom - eom,
            }
        )

    return pd.DataFrame(rows, columns=columns)


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


def completed_through(series: pd.DataFrame, today: dt.date | None = None) -> str | None:
    """The last period in `series` whose month has actually ended.

    The same test `investment_return_summary` applies to decide what it measures, exposed so
    that a heading cannot drift away from the figures underneath it. Naming the month with
    `series['period'].max()` instead described the end of the *data*, which runs to the end
    of the fiscal year: on 6 August it announced 'to end of August 2026' over July's numbers,
    and August had been deliberately excluded because it has not finished.

    Falls back to the earliest period exactly as the summary does, so the two agree even
    before a single month has closed.
    """
    if series.empty:
        return None
    today = today or dt.date.today()
    done = series[series["date"].map(lambda d: d <= today)]
    return done["period"].max() if not done.empty else series["period"].min()


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


# ------------------------------------------------------------------------------ pensions
#
# A pension is measured differently from every other balance in this application. Elsewhere
# the question is "how much is there"; here it is "how much of the rise was growth and how
# much was money paid in", and the two are only separable if the payments are recorded.
#
# Everything below is therefore one formula applied over two windows:
#
#     return = value at the end / (value at the start + net money in) - 1
#
# Over the gap between two valuations that is the period return; over the whole life of the
# pot -- start being its first valuation -- it is the return to date. A pot nothing is paid
# into has no flows, so the same expression collapses to end / start - 1, which is why there
# is no separate case for the ones that are closed to contributions.

PENSION_KINDS = ("contribution", "charge", "interest", "other")

# Returns are held as percentages -- 5.52, not 0.0552 -- as every other rate in this
# database is. See models.SalaryAssumption for why: a fraction in a two-decimal column can
# only ever express whole percentage points.
_DAYS_IN_YEAR = 365.25


def load_pension_pots(session: Session) -> pd.DataFrame:
    columns = ["id", "name", "display_order", "valid_from", "valid_to", "note"]
    rows = [
        {c: getattr(p, c) for c in columns}
        for p in session.scalars(select(PensionPot).order_by(PensionPot.display_order))
    ]
    return pd.DataFrame(rows, columns=columns)


def load_pension_valuations(session: Session) -> pd.DataFrame:
    columns = ["pot_id", "on_date", "value"]
    rows = [
        {c: getattr(v, c) for c in columns}
        for v in session.scalars(
            select(PensionValuation).order_by(
                PensionValuation.on_date, PensionValuation.pot_id
            )
        )
    ]
    return pd.DataFrame(rows, columns=columns)


def load_pension_contributions(session: Session) -> pd.DataFrame:
    columns = ["id", "pot_id", "on_date", "amount", "kind", "note"]
    rows = [
        {c: getattr(m, c) for c in columns}
        for m in session.scalars(
            select(PensionContribution).order_by(
                PensionContribution.on_date, PensionContribution.id
            )
        )
    ]
    return pd.DataFrame(rows, columns=columns)


def _pension_rate(end, base):
    """`end / base - 1` as a percentage, or None when there is nothing to measure against.

    A base of zero or less is not a return of infinity, it is an unanswerable question: a pot
    whose contributions have netted to nothing has no denominator, and reporting anything at
    all there would be inventing a figure.
    """
    if end is None or base is None:
        return None
    base = Decimal(str(base))
    if base <= 0:
        return None
    return float((Decimal(str(end)) / base - 1) * 100)


def _pension_annualised(rate, days):
    """A percentage return over `days`, restated as a yearly rate.

    None when the period is empty or the pot lost everything: a total loss makes the growth
    factor zero or negative, and a negative number raised to a fractional power is not real.
    """
    if rate is None or not days or days <= 0:
        return None
    growth = 1 + float(rate) / 100
    if growth <= 0:
        return None
    return (growth ** (_DAYS_IN_YEAR / days) - 1) * 100


def as_date(value):
    """A plain `date` from a frame cell that may hold a Timestamp, a date, a string or NaT.

    pandas hands back whichever of those suits the column it read, and a `date_input` wants
    exactly one of them -- so the conversion has to happen somewhere, and doing it at each
    call site is how a page ends up working for a populated column and raising for an empty
    one. None passes through, because an absent date is a legitimate answer.
    """
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.to_datetime(value).date()


def pension_history(
    pots: pd.DataFrame, valuations: pd.DataFrame, contributions: pd.DataFrame
) -> pd.DataFrame:
    """One row per pot per valuation date, with what the pot did between them.

    The dates are the union of every pot's valuations, because the providers do not publish
    together. A pot with no figure of its own on one of those dates carries its most recent
    one forward and says so in `stated`, rather than dropping out of the total for that date
    -- a pot missing from a sum reads as a fall in the total pension, which is the one
    reading that must never be accidental.

    Columns beyond the obvious:

    | `stated`   | the figure was read on this date rather than carried forward |
    | `opening`  | the pot's value at the previous date; zero on its first row |
    | `arrived`  | the pot's first value, on its first row only, and zero after |
    | `flows`    | net money in since the previous date -- contributions less charges |
    | `base`     | first value plus every net flow since: what the growth is measured from |
    | `growth`   | value less base -- the money the pot has made, in pounds |

    `arrived` is what keeps the total honest when a pot joins part way through. Its first
    value is not growth and it is not a contribution either, but it does have to go into the
    denominator, or the month a pot appears reports the whole of it as a gain.
    """
    columns = [
        "date", "pot", "pot_id", "value", "stated", "as_at", "opening", "arrived",
        "flows", "paid_in", "charges", "days", "base", "growth",
        "period_return", "period_annualised", "total_return", "total_annualised",
        "inception",
    ]
    if pots.empty or valuations.empty:
        return pd.DataFrame(columns=columns)

    dates = sorted({as_date(d) for d in valuations["on_date"]} - {None})
    if not dates:
        return pd.DataFrame(columns=columns)

    rows: list[dict] = []
    for _, pot in pots.iterrows():
        pot_id = pot["id"]
        mine = valuations[valuations["pot_id"] == pot_id]
        if mine.empty:
            continue
        stated_values = {
            as_date(r["on_date"]): Decimal(str(r["value"]))
            for _, r in mine.iterrows()
        }
        inception = min(stated_values)
        first_value = stated_values[inception]

        moves = (
            contributions[contributions["pot_id"] == pot_id]
            if not contributions.empty
            else contributions
        )
        movements = [
            (as_date(r["on_date"]), Decimal(str(r["amount"])), r.get("kind"))
            for _, r in moves.iterrows()
        ] if not moves.empty else []

        def flows_between(after, until):
            """Net movement in `(after, until]` -- the same half-open window everywhere."""
            net = paid = charged = Decimal("0")
            for when, amount, kind in movements:
                if when is None or when <= after or when > until:
                    continue
                net += amount
                if amount >= 0:
                    paid += amount
                else:
                    charged += amount
            return net, paid, charged

        opened = as_date(pot.get("valid_from"))
        closed = as_date(pot.get("valid_to"))

        previous_date = None
        previous_value = None
        for on in dates:
            if on < inception or (opened is not None and on < opened):
                continue
            if closed is not None and on > closed:
                continue

            as_at = max(d for d in stated_values if d <= on)
            value = stated_values[as_at]
            first_row = previous_date is None

            net, paid, charged = flows_between(
                inception if first_row else previous_date, on
            )
            since_start, _, _ = flows_between(inception, on)
            base = first_value + since_start
            days = None if first_row else (on - previous_date).days

            opening = Decimal("0") if first_row else previous_value
            arrived = first_value if first_row else Decimal("0")
            period = (
                None if first_row else _pension_rate(value, opening + net)
            )
            total = _pension_rate(value, base)

            rows.append(
                {
                    "date": on,
                    "pot": pot["name"],
                    "pot_id": pot_id,
                    "value": value,
                    "stated": as_at == on,
                    "as_at": as_at,
                    "opening": opening,
                    "arrived": arrived,
                    "flows": Decimal("0") if first_row else net,
                    "paid_in": Decimal("0") if first_row else paid,
                    "charges": Decimal("0") if first_row else charged,
                    "days": days,
                    "base": base,
                    "growth": value - base,
                    "period_return": period,
                    "period_annualised": _pension_annualised(period, days),
                    "total_return": total,
                    "total_annualised": _pension_annualised(total, (on - inception).days),
                    "inception": inception,
                }
            )
            previous_date, previous_value = on, value

    return pd.DataFrame(rows, columns=columns).sort_values(
        ["date", "pot"], ignore_index=True
    )


def pension_totals(history: pd.DataFrame) -> pd.DataFrame:
    """Every pot added together, per valuation date.

    The returns are recomputed from the summed pounds rather than averaged across the pots.
    A weighted mean of three ratios is not the ratio of the three sums, and it is the second
    one that answers 'what did my pension do' -- the first answers 'what did the average
    pound in it do', which is a different question and differs by most of a percentage point.
    """
    columns = [
        "date", "value", "opening", "flows", "paid_in", "charges", "days",
        "base", "growth", "period_return", "period_annualised",
        "total_return", "total_annualised", "pots", "carried",
    ]
    if history.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for on, block in history.groupby("date", sort=True):
        value = block["value"].sum()
        opening = block["opening"].sum()
        arrived = block["arrived"].sum()
        flows = block["flows"].sum()
        base = block["base"].sum()
        days = block["days"].dropna()
        # The longest gap, which is only ever a choice when a pot joined part way through:
        # every pot valued on the same run of dates has the same span, and a pot on its first
        # row contributes no span at all. Empty means every pot here is on its first row --
        # the very first date, which has no previous period to have returned anything over.
        span = int(days.max()) if not days.empty else None
        started = min(block["inception"])

        period = (
            None if span is None else _pension_rate(value, opening + arrived + flows)
        )
        total = _pension_rate(value, base)
        rows.append(
            {
                "date": on,
                "value": value,
                "opening": opening,
                "flows": flows,
                "paid_in": block["paid_in"].sum(),
                "charges": block["charges"].sum(),
                "days": span,
                "base": base,
                "growth": value - base,
                "period_return": period,
                "period_annualised": _pension_annualised(period, span),
                "total_return": total,
                "total_annualised": _pension_annualised(total, (on - started).days),
                "pots": int(len(block)),
                "carried": [r["pot"] for _, r in block.iterrows() if not r["stated"]],
            }
        )
    return pd.DataFrame(rows, columns=columns)


def pension_ledger(
    contributions: pd.DataFrame, pots: pd.DataFrame, pot_id: int | None = None
) -> pd.DataFrame:
    """The contribution ledger with its running total, newest last.

    The running total is what a valuation is measured against, so it is derived here rather
    than stored: a row inserted out of order rewrites every total after it, and a stored one
    would have to be recalculated on every edit or quietly go wrong.
    """
    columns = ["id", "date", "pot", "amount", "kind", "note", "running"]
    if contributions.empty:
        return pd.DataFrame(columns=columns)

    names = pots.set_index("id")["name"].to_dict() if not pots.empty else {}
    frame = contributions.copy()
    if pot_id is not None:
        frame = frame[frame["pot_id"] == pot_id]
    if frame.empty:
        return pd.DataFrame(columns=columns)

    frame["date"] = [as_date(d) for d in frame["on_date"]]
    frame = frame.sort_values(["pot_id", "date", "id"], kind="stable")
    frame["pot"] = frame["pot_id"].map(names)

    # Accumulated by hand rather than with groupby().cumsum(), which cannot run over a
    # column of Decimals -- pandas takes it as object dtype and refuses. Converting to float
    # to satisfy it is exactly the trade this application does not make: the running total is
    # what a valuation is measured against, so it has to be exact to the penny.
    running: list[Decimal] = []
    carried: dict[int, Decimal] = {}
    for _, row in frame.iterrows():
        total = carried.get(row["pot_id"], Decimal("0")) + Decimal(str(row["amount"]))
        carried[row["pot_id"]] = total
        running.append(total)
    frame["running"] = running
    return frame[columns].reset_index(drop=True)


def pension_contribution_summary(
    contributions: pd.DataFrame, pots: pd.DataFrame
) -> pd.DataFrame:
    """Money in, charges out and the net, per pot -- what the ledger is kept for.

    Charges are the reason the split is worth having. They are single pence a month at the
    start and over ten pounds by the end, which is invisible inside a net figure and is the
    kind of thing a pension is worth watching.
    """
    columns = ["pot", "paid_in", "charges", "net", "entries", "first", "last"]
    if contributions.empty or pots.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for _, pot in pots.iterrows():
        mine = contributions[contributions["pot_id"] == pot["id"]]
        if mine.empty:
            continue
        amounts = [Decimal(str(a)) for a in mine["amount"]]
        when = sorted(d for d in (as_date(x) for x in mine["on_date"]) if d)
        rows.append(
            {
                "pot": pot["name"],
                "paid_in": sum((a for a in amounts if a > 0), Decimal("0")),
                "charges": sum((a for a in amounts if a < 0), Decimal("0")),
                "net": sum(amounts, Decimal("0")),
                "entries": len(mine),
                "first": when[0] if when else None,
                "last": when[-1] if when else None,
            }
        )
    return pd.DataFrame(rows, columns=columns)
