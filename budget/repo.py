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
         "exclude_from_savings", "statement_day", "payment_day",
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
            "deleted": t.deleted_at is not None,
            "deleted_reason": t.deleted_reason,
        }
        for t in session.scalars(stmt)
    ]
    df = pd.DataFrame(
        rows,
        columns=["id", "date", "period", "type", "amount", "account_from", "account_to",
                 "category", "classification", "comment", "category_comment", "identifier",
                 "deleted", "deleted_reason"],
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


# --------------------------------------------------------------------------- aggregations


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
    opening = openings[openings["period"] == period].set_index("account")["opening"]

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
            }
        )
    return pd.DataFrame(rows)


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


def salary_bands(session: Session, tax_year: int) -> tax.Bands:
    """Assemble the PAYE/NI bands for a tax year from stored assumptions.

    `basic_band` is derived rather than read: the workbook's D36 is `=D28 - D22`, the basic
    rate threshold less the personal allowance. Storing it as well as its two inputs would
    let the three drift apart the moment one was edited, so the stored value is only a
    fallback for a database that predates the inputs being kept.
    """
    rows = list(
        session.scalars(
            select(SalaryAssumption).where(SalaryAssumption.tax_year == tax_year)
        )
    )
    simple = {
        r.key: (r.value / HUNDRED if r.key in RATE_KEYS else r.value)
        for r in rows
        if r.key != "personal_allowance_adjustment"
    }
    steps = tuple(
        (r.effective_from, r.value)
        for r in rows
        if r.key == "personal_allowance_adjustment"
    )

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
    """Headline totals from the Summary tab, driven by the account flags."""
    return {
        "savings": balances.loc[balances["is_savings"], "closing"].sum() or Decimal("0"),
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
    rows = [
        {"id": p.id, "effective_from": p.effective_from,
         "annual_salary": p.annual_salary, "note": p.note}
        for p in session.scalars(select(SalaryProfile).order_by(SalaryProfile.effective_from))
    ]
    return pd.DataFrame(rows, columns=["id", "effective_from", "annual_salary", "note"])


def load_bonuses(session: Session) -> pd.DataFrame:
    rows = [
        {"period": b.period, "amount": b.amount, "note": b.note}
        for b in session.scalars(select(Bonus).order_by(Bonus.period))
    ]
    return pd.DataFrame(rows, columns=["period", "amount", "note"])


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
    rows = [
        {"period": t.period, "savings": t.savings, "investments": t.investments}
        for t in session.scalars(select(SavingsTarget).order_by(SavingsTarget.period))
    ]
    return pd.DataFrame(rows, columns=["period", "savings", "investments"])


# ------------------------------------------------------------------ expected salary


def period_start(period: str) -> dt.date:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1)


def salary_in_force(profiles: pd.DataFrame, on: dt.date) -> Decimal | None:
    """The annual salary applying on a date -- the last change on or before it.

    The workbook repeated the figure down all twelve rows of column O, so a pay rise meant
    editing every month from that point and hoping none were missed.
    """
    if profiles.empty:
        return None
    applicable = profiles[pd.to_datetime(profiles["effective_from"]).dt.date <= on]
    if applicable.empty:
        return None
    latest = applicable.sort_values("effective_from").iloc[-1]
    return latest["annual_salary"]


def expected_gross(
    period: str, profiles: pd.DataFrame, bonuses: pd.DataFrame
) -> Decimal | None:
    """Salary tracker column P: the annual salary in force / 12, plus any bonus that month.

    May's cell was `=ROUND(O5/12,2)+29028.48` -- the bonus typed into the formula. With the
    bonus held as data the derivation works for every month, so expected gross no longer has
    to be stored alongside the inputs that produce it.
    """
    salary = salary_in_force(profiles, period_start(period))
    if salary is None:
        return None
    monthly = (Decimal(salary) / 12).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not bonuses.empty:
        match = bonuses[bonuses["period"] == period]
        if not match.empty:
            monthly += Decimal(match["amount"].iloc[0])
    return monthly


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


def card_outstanding(
    balances: pd.DataFrame,
    statements: pd.DataFrame,
    accounts: pd.DataFrame,
    period: str,
    today: dt.date | None = None,
) -> pd.DataFrame:
    """Month-tab B42:E52 -- what is owed on a card beyond the bill already issued.

    The workbook's D52, in words:

        balance
          less, if this month and today is before the payment day, the opening statement
          less, if a later month or today is on/after the statement day, the closing one
          less nothing in the window between the two

    Before the direct debit goes out the opening statement is still owed; once the closing
    statement has been issued that is what is owed instead; in between, nothing has been
    billed. Subtracting it leaves the spending that has not yet reached a statement -- what
    is owed on top of the next payment.

    The opening bill is the previous month's closing bill rather than a second stored
    figure. That holds exactly for BA Amex and Mastercard in every month of the workbook;
    Platinum Amex's July opening was hand-adjusted and differs by 76.05.
    """
    today = today or dt.date.today()
    cards = accounts[accounts["type"] == "credit_card"]
    if cards.empty:
        return pd.DataFrame(
            columns=["account", "closing", "bill_bom", "bill_eom", "billed", "outstanding"]
        )

    earlier = previous_period(period)
    by_account = balances.set_index("account") if not balances.empty else None

    def bill(account_id: int, for_period: str | None) -> Decimal:
        if statements.empty or for_period is None:
            return Decimal("0")
        match = statements[
            (statements["account_id"] == account_id) & (statements["period"] == for_period)
        ]
        return Decimal(match["bill_eom"].iloc[0]) if not match.empty else Decimal("0")

    in_this_month = f"{today.year:04d}-{today.month:02d}" == period

    rows = []
    for _, card in cards.iterrows():
        name = card["name"]
        if by_account is not None and name in by_account.index:
            closing = by_account.loc[name, "closing"]
        else:
            closing = Decimal("0")

        bom = bill(int(card["id"]), earlier)
        eom = bill(int(card["id"]), period)

        statement_day = card["statement_day"]
        payment_day = card["payment_day"]

        if in_this_month and payment_day is not None and today.day < int(payment_day):
            billed = bom
        elif not in_this_month or (
            statement_day is not None and today.day >= int(statement_day)
        ):
            billed = eom
        else:
            billed = Decimal("0")

        rows.append(
            {
                "account": name,
                "closing": closing,
                "bill_bom": bom,
                "bill_eom": eom,
                "billed": billed,
                "outstanding": Decimal(closing) - Decimal(billed),
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

    The workbook opens with the year's starting balances and then gives one row per month
    end, which is reproduced by prepending an opening row.

    `required` accumulates: this month's target less what was added, plus whatever was still
    outstanding from last month. A month that falls short raises the bar for the next one,
    which is the whole point of the column.
    """
    today = today or dt.date.today()
    flag = accounts["exclude_from_savings"]
    excluded_names = set(accounts.loc[flag.fillna(False).astype(bool), "name"])

    lookup = targets.set_index("period") if not targets.empty else None

    def target_for(period: str, column: str) -> Decimal | None:
        if lookup is None or period not in lookup.index:
            return None
        value = lookup.loc[period, column]
        return None if pd.isna(value) else Decimal(value)

    rows: list[dict] = []
    previous_savings = previous_investments = None
    outstanding_savings = outstanding_investments = Decimal("0")

    for index, period in enumerate(periods):
        balances = account_balances(postings, openings, period, accounts)
        if balances.empty:
            continue
        savings_rows = balances[balances["is_savings"]]
        invest_rows = balances[balances["is_investment"]]
        available = savings_rows[~savings_rows["account"].isin(excluded_names)]

        if index == 0:
            rows.append(
                {
                    "period": period,
                    "date": period_start(period),
                    "month": period_label(period),
                    "point": "Opening",
                    "savings": savings_rows["opening"].sum() or Decimal("0"),
                    "savings_available": available["opening"].sum() or Decimal("0"),
                    "savings_added": None,
                    "savings_target": None,
                    "savings_required": None,
                    "investments": invest_rows["opening"].sum() or Decimal("0"),
                    "investments_added": None,
                    "investments_target": None,
                    "investments_required": None,
                    "combined": (savings_rows["opening"].sum() or Decimal("0"))
                    + (invest_rows["opening"].sum() or Decimal("0")),
                }
            )
            previous_savings = rows[-1]["savings_available"]
            previous_investments = rows[-1]["investments"]

        started = period_start(period) <= today
        savings_total = savings_rows["closing"].sum() or Decimal("0")
        savings_available = available["closing"].sum() or Decimal("0")
        investments_total = invest_rows["closing"].sum() or Decimal("0")

        added_savings = (
            savings_available - previous_savings
            if started and previous_savings is not None
            else None
        )
        added_investments = (
            investments_total - previous_investments
            if started and previous_investments is not None
            else None
        )

        savings_target = target_for(period, "savings")
        investments_target = target_for(period, "investments")

        if added_savings is not None and savings_target is not None:
            outstanding_savings += savings_target - added_savings
            savings_required = outstanding_savings
        else:
            savings_required = None
        if added_investments is not None and investments_target is not None:
            outstanding_investments += investments_target - added_investments
            investments_required = outstanding_investments
        else:
            investments_required = None

        rows.append(
            {
                "period": period,
                "date": month_end(period),
                "month": period_label(period),
                "point": "Month end",
                "savings": savings_total,
                "savings_available": savings_available,
                "savings_added": added_savings,
                "savings_target": savings_target,
                "savings_required": savings_required,
                "investments": investments_total,
                "investments_added": added_investments,
                "investments_target": investments_target,
                "investments_required": investments_required,
                "combined": savings_total + investments_total,
            }
        )
        previous_savings = savings_available
        previous_investments = investments_total

    return pd.DataFrame(rows)


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
