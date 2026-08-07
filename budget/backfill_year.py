"""Add a prior year's workbook to an existing database.

`migrate_xlsm` builds a database from one workbook and refuses to touch a populated one.
Backfilling is the opposite problem: the current year is already there, correct, and
reconciled, and a previous year has to arrive underneath it without disturbing any of it.

Run with:
    python -m budget.backfill_year --workbook "K:\\Private\\Finance\\Budget 25-26.xlsm"

Nothing is written unless the whole thing succeeds, and `--dry-run` reports what would
happen and rolls back. It refuses outright if the year is already present, so running it
twice cannot double the ledger.

**It does not push.** The NAS copy is the user's to update once they have checked the result.

Three things make a backfill different from a migration:

* **Effective dating runs backwards.** Every account, category and classification already
  carries valid_from = 1 April 2026, because that is when the database started. A 2025
  transaction against them fails validation until those dates are moved back, so the first
  pass widens the reference data and only then imports.

* **Accounts change names and split.** 'Amex' in 25-26 is 'BA Amex' here, and from 24 March
  2026 the card ran alongside a second one -- see AMEX_CUTOVER. Five more accounts existed
  in 25-26 and are gone now; they are created closed, with the months they really ran.

* **Opening balances are an anchor, not a fact per month.** Since repo.rolled_forward_openings
  a month's opening is derived from the one before it, so the only stored figure that matters
  is the earliest per account. Backfilling moves that anchor from April 2026 to April 2025,
  which is why the year-end join is checked before anything is committed: if March 2026 does
  not meet April 2026, every figure in the current year moves.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget import config, repo, service, xlsm_reader as xr
from budget.db import create_all, in_use, make_engine, make_session_factory
from budget.models import (
    Account,
    Bonus,
    Card,
    CardStatement,
    Category,
    Classification,
    ClassificationAllowance,
    ClassificationOpening,
    CyclingDay,
    CyclingOutgoing,
    CyclingRate,
    ImportBatch,
    OpeningBalance,
    Payslip,
    SalaryAssumption,
    SalaryProfile,
    SavingsAdjustment,
    SavingsPlan,
    Txn,
)
from budget.postings import postings_for

# Workbook name -> database name, for accounts renamed since a workbook was written. A
# workbook is a historical record and cannot be expected to know what an account is called
# now; the reconciliation matches a month tab's columns to accounts by name, so without this
# a rename reads as the account holding nothing.
#
#   Amex -> BA Amex          the card was renamed, not replaced
#   Tembo -> Savings - Tembo renamed in the dashboard, August 2026
#
# The alias is a hint, not a requirement: where it names no account and the workbook's own
# name does, the workbook's is used. That keeps a database written before the rename working
# as well as one written after.
ACCOUNT_ALIASES = {"Amex": "BA Amex", "Tembo": "Savings - Tembo"}

# From this date the single 'Amex' column in 25-26 covers two real cards. Everything on or
# after it belongs to Platinum Amex except two transactions that stayed on the BA card --
# confirmed by the user, and the split lands both openings on their 26-27 figures to the
# penny.
AMEX_CUTOVER = dt.date(2026, 3, 24)
AMEX_STAYS_ON_BA = (
    ("Debit", Decimal("0.24"), "Sainsbury's"),
    ("Credit", Decimal("492.30"), None),  # comment not matched: 'NY hotel refund'
)

CORRECTIONS = {2025: xr.CORRECTIONS_25_26, 2026: xr.CORRECTIONS_26_27}

# One workbook column can be several database accounts. A month tab's 'Amex' block is the
# two cards added together, so that is what a reconciliation has to compare against.
BLOCK_TO_DB = {"Amex": ("BA Amex", "Platinum Amex")}


# Bands a workbook does not model, and the database should hold anyway. 25-26 stops at the
# higher rate: it has no 'Additional rate' row at all, which understated its own expected
# PAYE for a salary above the GBP 125,140 threshold. 45% is the real 2025/26 rate, and
# storing it means the database's 2025 figures deliberately differ from the workbook's
# expected column. Confirmed with the user.
MISSING_BANDS: dict[int, dict[str, Decimal]] = {
    2025: {"additional_rate": Decimal("45")},
}

# What to do with a prior year's balance transfer cards, confirmed with the user. A card is
# one row describing one borrowing, not a row per year, so a card in both workbooks is
# either the same debt seen earlier or a different debt that happens to share a lender's
# name -- and only the user knows which.
#
#   extend       the same borrowing: move its start back to where it really began, and
#                lengthen the term to match, so the payoff still ends where it does now
#   <a name>     a different borrowing: its own row, under that name
#   absent       leave the workbook's card alone
#
# Barclaycard and Barclaycard (2) are absent deliberately: several tranches the user is
# sorting out separately.
CARD_PLAN: dict[str, str] = {
    "MBNA": "extend",  # 5,201 over 34 months from 31 Oct 2025 reaches 26-27's stated 4,468.02
    "Tesco": "extend",  # cleared before April 2026, so 26-27 carries only a zero
    "Halifax": "Halifax 2025",  # a separate, earlier borrowing
}

# Which card each xlCardBillEom<n><Month> cell belongs to, for a workbook whose statement
# block carries no header row. 25-26 has none -- 26-27 names its columns four rows above the
# figure, 25-26 names them nowhere -- so the order has to be stated, and the user did:
# index 1 is the BA Amex bill, index 2 Mastercard.
#
# Checked rather than trusted: the block also holds each card's statement and payment day,
# and _backfill_card_bills refuses if those disagree with the account they are claimed for.
CARD_BILL_COLUMNS: dict[int, tuple[str, ...]] = {
    2025: ("BA Amex", "Mastercard"),
}

# The savings plan for the months before the interest tracker begins.
#
# 25-26's Summary column G is one figure a month with no breakdown behind it -- 750 from
# April to September -- and column M is 0 until July, then 50. The user confirmed the whole
# of the savings figure went into Savings - Marcus, and that the 50 is the Stocks & Shares
# ISA. From 1 August 2025 the tracker takes over: it holds the real per-account split, it
# disagrees with column G, and the user asked for it to win. It is already in the database,
# seeded from the *26-27* workbook, which is where that history is kept.
#
# Dating is per account, not per revision (repo.plan_in_force), so Marcus's 750 needs no
# closing zero -- the tracker's 1 August row supersedes it, and nothing else claimed a
# target in the meantime.
PRE_TRACKER_PLAN: dict[int, dict[dt.date, dict[str, Decimal]]] = {
    2025: {
        dt.date(2025, 4, 1): {"Savings - Marcus": Decimal("750")},
        dt.date(2025, 7, 1): {"Stocks & Shares ISA": Decimal("50")},
    },
}

# Planned one-off withdrawals, from Summary column G. All three land on Savings - Marcus,
# which is where the money actually left from: 6,757.54 on 14 October, 6,796.29 on 5
# December, and February's 823.37 and 1,860.82. It is the working pot the standing plan
# also feeds.
#
# The figure here is the month's **net** savings target -- what column G says the month
# should add once everything is counted -- and the adjustment written is whatever reaches it
# from the standing plan in force. Stated net because that is what the user means by it, and
# because the plan underneath is the interest tracker's rather than this workbook's: the two
# differ, and a hard-coded difference would quietly stop being right the day either moves.
SAVINGS_ADJUSTMENTS: dict[int, tuple[tuple[str, str, Decimal, str], ...]] = {
    2025: (
        ("2025-10", "Savings - Marcus", Decimal("-6700"), "Planned withdrawal"),
        ("2025-12", "Savings - Marcus", Decimal("-4000"), "Planned withdrawal"),
        ("2026-02", "Savings - Marcus", Decimal("-4000"), "Planned withdrawal"),
    ),
}


def db_accounts_for_block(block_name: str) -> tuple[str, ...]:
    """The database accounts one month-tab column stands for. Identity for anything the
    backfill does not rename, so the current year's reconciliation is unaffected."""
    if block_name in BLOCK_TO_DB:
        return BLOCK_TO_DB[block_name]
    return (ACCOUNT_ALIASES.get(block_name, block_name),)

# Every section is written now: reference data, opening balances, the ledger, salary,
# cycling, cards, card statements and the savings plan. The guard stays as the switch a
# half-finished section would be turned off with.
INCOMPLETE = False


def account_for(name: str, row: xr.LedgerRow) -> str:
    """The database account a ledger row belongs to.

    Usually just the alias. Amex is the exception: one workbook column, two real cards from
    24 March 2026 onwards.
    """
    mapped = ACCOUNT_ALIASES.get(name, name)
    if mapped != "BA Amex" or row.txn_date < AMEX_CUTOVER:
        return mapped
    for kind, amount, comment in AMEX_STAYS_ON_BA:
        if row.type == kind and row.amount == amount:
            if comment is None or row.comment == comment:
                return "BA Amex"
    return "Platinum Amex"


def _month_start(period: str) -> dt.date:
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1)


def _short_code(name: str) -> str:
    """A code for an account the Selections sheet never described.

    Identifiers are rebuilt from this, so it only has to be stable and distinct: the letters
    of the name, upper-cased, prefixed for a savings pot the way the workbook's own codes are
    (SAV_FD, SAV_SCG). Uniqueness is checked by the caller through the account name, which is
    what actually has to be unique.
    """
    stem = name.replace("Savings - ", "").replace(" ", "")[:4].upper()
    return f"SAV_{stem}" if name.startswith("Savings") else stem


def first_month_of(ref: xr.RefData, account: xr.AccountRef) -> dt.date:
    period = ref.period_for(account.first_month or "April")
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1)


def last_month_end(ref: xr.RefData, months: list[str]) -> dt.date | None:
    """The last day of the last fiscal month an account appears in, or None if it runs to
    the end of the year -- in which case it is not closed, merely not yet reopened."""
    if not months or months[-1] == xr.FISCAL_MONTHS[-1]:
        return None
    period = ref.period_for(months[-1])
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)


def widen(session: Session, model, name: str, when: dt.date) -> bool:
    """Move a reference row's valid_from back, if it does not already reach that far."""
    row = session.scalar(select(model).where(model.name == name))
    if row is None or row.valid_from <= when:
        return False
    row.valid_from = when
    return True


_LEDGER_CACHE: dict[int, list[xr.LedgerRow]] = {}


def ledger_rows(values, year: int) -> list[xr.LedgerRow]:
    """The workbook's ledger, corrected. Cached: read once, asked about several times."""
    if year not in _LEDGER_CACHE:
        _LEDGER_CACHE[year], _ = xr.read_ledger(values, CORRECTIONS[year])
    return _LEDGER_CACHE[year]


def account_months(values, formulas, ref: xr.RefData) -> dict[str, list[str]]:
    """Which fiscal months each account actually appears in, from the month tabs."""
    out: dict[str, list[str]] = {}
    for month in xr.FISCAL_MONTHS:
        layout = xr.month_layout(values, formulas, month, ref.period_for(month))
        for block in layout.blocks:
            out.setdefault(block.name, []).append(month)
    return out


def backfill(session: Session, workbook: Path, verbose: bool = False) -> list[str]:
    """Everything, in one transaction. Returns a report; raises on anything unexpected."""
    done: list[str] = []
    values, formulas = xr.load(workbook)
    ref = xr.read_reference(values)
    year = ref.tax_year
    if year not in CORRECTIONS:
        raise ValueError(
            f"No confirmed corrections for the {year}/{str(year + 1)[-2:]} workbook. "
            "Its bad rows must be identified and confirmed before it can be imported."
        )
    periods = [ref.period_for(m) for m in xr.FISCAL_MONTHS]

    already = session.scalar(
        select(func.count()).select_from(Txn).where(Txn.period.in_(periods))
    )
    if already:
        raise ValueError(
            f"{already} transaction(s) already exist in {year}/{str(year + 1)[-2:]}. "
            "The year is present; backfilling again would double it."
        )

    # --- reference data, widened backwards -------------------------------------------
    months_by_account = account_months(values, formulas, ref)
    year_start = dt.date(year, 4, 1)

    # Driven by the month tabs, not by Selections. The 25-26 Selections sheet lists 17
    # accounts where the tabs carry 23: the ones closed before the sheet was last tidied are
    # missing from it, and they are exactly the ones a backfill has to create. Selections is
    # still consulted for the details a tab cannot give -- short code, savings flags.
    described = {a.name: a for a in ref.accounts}
    cards = _card_names(values, formulas, ref)
    created, widened_accounts = [], 0

    for name, months in sorted(months_by_account.items()):
        db_name = ACCOUNT_ALIASES.get(name, name)
        detail = described.get(name)
        from_date = (
            first_month_of(ref, detail) if detail
            else _month_start(ref.period_for(months[0]))
        )
        closes = last_month_end(ref, months)

        if session.scalar(select(Account).where(Account.name == db_name)) is None:
            session.add(
                Account(
                    name=db_name,
                    short_code=detail.short_code if detail else _short_code(name),
                    type="credit_card" if name in cards else "bank",
                    valid_from=from_date,
                    valid_to=closes,
                    # Undescribed accounts are read from their name, which is how the
                    # workbook itself distinguishes them.
                    is_savings=detail.is_savings if detail else name.startswith("Savings"),
                    is_investment=detail.is_investment if detail else False,
                    display_order=detail.display_order if detail else 90,
                )
            )
            created.append(
                f"{db_name} ({from_date}" + (f" to {closes})" if closes else ", open)")
            )
        elif widen(session, Account, db_name, from_date):
            widened_accounts += 1

    # The other half of the Amex split. Platinum takes transactions from 24 March 2026, a
    # month before the database currently lets it exist, so it has to reach back to the
    # start of that month or every one of them fails validation.
    if any(account_for("Amex", r) == "Platinum Amex" for r in ledger_rows(values, year)):
        cutover_month = dt.date(AMEX_CUTOVER.year, AMEX_CUTOVER.month, 1)
        if widen(session, Account, "Platinum Amex", cutover_month):
            widened_accounts += 1
            done.append(f"Platinum Amex opened back to {cutover_month} for the Amex split")

    session.flush()
    done.append(
        f"accounts: {len(created)} created, {widened_accounts} widened back to {year_start}"
    )
    for line in created:
        done.append(f"    created {line}")

    widened = sum(
        widen(session, Category, c.name, year_start) for c in ref.categories
    )
    done.append(f"categories: {widened} widened")
    widened = sum(
        widen(session, Classification, c.name, year_start) for c in ref.classifications
    )
    done.append(f"classifications: {widened} widened")
    session.flush()

    # --- opening balances --------------------------------------------------------------
    #
    # Stored for every month as the workbook states them, but only the earliest per account
    # is now consulted: repo.rolled_forward_openings derives the rest. Storing them all keeps
    # the record faithful and gives the reconciliation something to check against.
    stored = 0
    for month, period in zip(xr.FISCAL_MONTHS, periods):
        layout = xr.month_layout(values, formulas, month, period)
        for name, amount in xr.read_opening_balances(values, layout).items():
            db_name = ACCOUNT_ALIASES.get(name, name)
            account = session.scalar(select(Account).where(Account.name == db_name))
            if account is None:
                continue
            session.add(
                OpeningBalance(account_id=account.id, period=period, amount=amount)
            )
            stored += 1

    # Platinum Amex has no column in this workbook but does have transactions in it, from
    # the 24 March split. Anchoring it at zero that month is what lets its April 2026
    # opening derive to the stated figure instead of being inherited from BA Amex.
    platinum = session.scalar(select(Account).where(Account.name == "Platinum Amex"))
    split_period = f"{AMEX_CUTOVER.year:04d}-{AMEX_CUTOVER.month:02d}"
    if platinum is not None and split_period in periods:
        session.add(
            OpeningBalance(
                account_id=platinum.id, period=split_period, amount=Decimal("0")
            )
        )
        stored += 1
        done.append(f"Platinum Amex anchored at zero for {split_period}")
    session.flush()
    done.append(f"opening balances: {stored} stored across {len(periods)} months")

    # --- transactions ------------------------------------------------------------------
    rows = ledger_rows(values, year)
    batch = ImportBatch(
        filename=workbook.name,
        row_count=len(rows),
        note=f"Backfill of {year}/{str(year + 1)[-2:]}",
    )
    session.add(batch)
    session.flush()

    accounts = {a.name: a for a in session.scalars(select(Account))}
    categories = {c.name: c for c in session.scalars(select(Category))}
    classifications = {c.name: c for c in session.scalars(select(Classification))}

    rejected: list[str] = []
    imported = soft_deleted = 0
    by_day: dict[tuple, int] = {}

    for row in rows:
        from_name = account_for(row.account_from, row)
        to_name = account_for(row.account_to, row) if row.account_to else None
        account = accounts.get(from_name)
        if account is None:
            rejected.append(f"row {row.source_row}: unknown account {from_name!r}")
            continue
        if row.category and row.category not in categories:
            rejected.append(f"row {row.source_row}: unknown category {row.category!r}")
            continue
        if row.classification and row.classification not in classifications:
            rejected.append(
                f"row {row.source_row}: unknown purchase type {row.classification!r}"
            )
            continue

        # The workbook's own identifier scheme, rebuilt rather than copied: the Amex split
        # sends some rows to a different account, and a copied identifier would still name
        # the old one.
        key = (row.txn_date, from_name)
        seq = by_day.get(key, 0)
        by_day[key] = seq + 1
        stem = f"{row.txn_date.month:02d}{row.txn_date.day:02d}_{account.short_code}"
        if row.type == "Transfer" and to_name:
            stem += f"_{accounts[to_name].short_code}"

        session.add(
            Txn(
                txn_date=row.txn_date,
                period=f"{row.txn_date.year:04d}-{row.txn_date.month:02d}",
                type=row.type,
                amount=row.amount,
                account_from_id=account.id,
                account_to_id=accounts[to_name].id if to_name else None,
                category_id=categories[row.category].id if row.category else None,
                classification_id=(
                    classifications[row.classification].id if row.classification else None
                ),
                comment=row.comment,
                category_comment=row.category_comment,
                legacy_identifier=f"{stem}_{seq}",
                source="bulk",
                batch_id=batch.id,
                created_at=row.created_at,
                deleted_at=dt.datetime.now() if row.removed else None,
                deleted_reason=row.removed_reason,
            )
        )
        imported += 1
        soft_deleted += bool(row.removed)

    if rejected:
        raise ValueError(
            f"{len(rejected)} row(s) could not be imported:\n    "
            + "\n    ".join(rejected[:20])
        )

    session.flush()
    done.append(
        f"transactions: {imported} imported as batch {batch.id}"
        f" ({soft_deleted} soft-deleted)"
    )

    done += _backfill_salary(session, values, formulas, ref)
    done += _backfill_cycling(session, values, ref)
    done += _backfill_cards(session, values)
    done += _backfill_card_bills(session, values, ref)
    done += _backfill_savings_plan(session, ref)
    done += _backfill_rollover_inputs(session, values, formulas, ref)

    return done


# --------------------------------------------------------------- the rollover engine


def _backfill_rollover_inputs(session, values, formulas, ref: xr.RefData) -> list[str]:
    """The two figures the running totals need besides the ledger: what each classification
    carried in, and the daily allowance the month adds on top.

    Both are per year and neither can be inferred. 25-26 opens at -4,239.91 of Excess and
    +130 of Expenses, typed into April's formulas because the workbook had nowhere to put
    them, and its allowance moves -- 30 in April, 40 in May, 35 from June. Without them
    April's running Excess comes out 3,339.91 adrift, and every later month inherits it.

    Projections are deliberately not imported. They only apply to dates after today, the
    whole of a backfilled year is behind us, and the sheet holds one month at a time -- so
    there is nothing they could correctly contribute.
    """
    classifications = {c.name: c.id for c in session.scalars(select(Classification))}
    periods = [ref.period_for(m) for m in xr.FISCAL_MONTHS]

    for model, label in (
        (ClassificationOpening, "openings"),
        (ClassificationAllowance, "allowances"),
    ):
        if session.scalar(
            select(func.count()).select_from(model).where(model.period.in_(periods))
        ):
            raise ValueError(
                f"Classification {label} already exist for "
                f"{ref.tax_year}/{str(ref.tax_year + 1)[-2:]}."
            )

    done: list[str] = []
    allowances = openings = 0
    for month, period in zip(xr.FISCAL_MONTHS, periods):
        amount = xr.read_daily_allowance(values, month)
        if amount and "Excess" in classifications:
            session.add(
                ClassificationAllowance(
                    period=period,
                    classification_id=classifications["Excess"],
                    daily_amount=amount,
                )
            )
            allowances += 1

        for name, value in xr.read_running_opening(formulas, values, month).items():
            if name in classifications and value:
                session.add(
                    ClassificationOpening(
                        period=period,
                        classification_id=classifications[name],
                        amount=value,
                    )
                )
                openings += 1
                done.append(f"    {month} {name} opens at {value}")

    done.insert(0, f"rollover inputs: {openings} opening(s), {allowances} allowance(s)")
    session.flush()
    return done


# ------------------------------------------------------------------------ savings plan


def _backfill_savings_plan(session, ref: xr.RefData) -> list[str]:
    """The months the interest tracker does not reach, and the year's planned one-offs.

    Only the part the tracker cannot supply. From August 2025 the plan is already here --
    seeded from the 26-27 workbook, which keeps the history -- so writing 25-26's Summary
    column G over it would replace a per-account split with a single lump sum.
    """
    year = ref.tax_year
    periods = set(ref.period_for(m) for m in xr.FISCAL_MONTHS)
    accounts = {a.name: a for a in session.scalars(select(Account))}
    done: list[str] = []

    added = 0
    for effective_from, amounts in sorted(PRE_TRACKER_PLAN.get(year, {}).items()):
        for name, amount in amounts.items():
            account = accounts.get(name)
            if account is None:
                raise ValueError(f"No account named {name!r} for the savings plan.")
            if session.scalar(
                select(SavingsPlan).where(
                    SavingsPlan.account_id == account.id,
                    SavingsPlan.effective_from == effective_from,
                )
            ):
                continue
            session.add(
                SavingsPlan(
                    account_id=account.id, effective_from=effective_from, amount=amount
                )
            )
            added += 1
            done.append(f"    {effective_from}: {name} {amount}")
    done.insert(0, f"savings plan: {added} row(s) before the interest tracker")

    existing = session.scalar(
        select(func.count())
        .select_from(SavingsAdjustment)
        .where(SavingsAdjustment.period.in_(periods))
    )
    if existing:
        raise ValueError(f"Savings adjustments already exist in {year}/{str(year + 1)[-2:]}.")

    # Derived against the plan that is actually in force, so the month lands on the net the
    # user stated rather than on it plus whatever the standing contributions happen to be.
    plan = repo.load_savings_plan(session)
    kinds = repo.account_kinds(repo.load_reference(session)["accounts"])

    one_offs = SAVINGS_ADJUSTMENTS.get(year, ())
    for period, name, net, note in one_offs:
        account = accounts.get(name)
        if account is None:
            raise ValueError(f"No account named {name!r} for a savings adjustment.")
        in_force = repo.plan_in_force(plan, repo.period_start(period))
        standing = sum(
            (
                row["amount"]
                for _, row in in_force.iterrows()
                if kinds.get(row["account"]) == "Savings"
            ),
            Decimal("0"),
        )
        amount = net - standing
        session.add(
            SavingsAdjustment(
                period=period, account_id=account.id, amount=amount, note=note
            )
        )
        done.append(
            f"    {period}: {name} {amount} ({note}) -- {standing} standing, "
            f"so the month nets {net}"
        )
    done.append(f"savings adjustments: {len(one_offs)}")

    session.flush()
    return done


# ------------------------------------------------------------------- card statements


def _backfill_card_bills(session, values, ref: xr.RefData) -> list[str]:
    """Each month tab's closing credit card statement.

    Zeroes are stored as well as figures. For a year that has finished, a card billing
    nothing is a fact about that month; leaving the row out would say the month is unknown.
    """
    year = ref.tax_year
    periods = [ref.period_for(m) for m in xr.FISCAL_MONTHS]
    if session.scalar(
        select(func.count()).select_from(CardStatement).where(CardStatement.period.in_(periods))
    ):
        raise ValueError(f"Card statements already exist for {year}/{str(year + 1)[-2:]}.")

    stated = CARD_BILL_COLUMNS.get(year, ())
    accounts = {a.name: a for a in session.scalars(select(Account))}
    written = 0
    seen: set[str] = set()

    for month, period in zip(xr.FISCAL_MONTHS, periods):
        for bill in xr.read_card_bills(values, month):
            name = bill.name or (
                stated[bill.index - 1] if bill.index <= len(stated) else None
            )
            if name is None:
                raise ValueError(
                    f"{month}: xlCardBillEom{bill.index}{month} belongs to no known card. "
                    "The tab names no columns, so add the order to CARD_BILL_COLUMNS."
                )
            name = ACCOUNT_ALIASES.get(name, name)
            account = accounts.get(name)
            if account is None:
                raise ValueError(f"{month}: no account named {name!r} for the card bill.")

            # The block's own statement and payment days must agree with the account they
            # are being filed against. Without this the column order is an unverifiable
            # claim, and getting it backwards would silently swap two cards' whole year.
            for label, from_tab, stored in (
                ("statement", bill.statement_day, account.statement_day),
                ("payment", bill.payment_day, account.payment_day),
            ):
                if from_tab is not None and stored is not None and from_tab != stored:
                    raise ValueError(
                        f"{month}: xlCardBillEom{bill.index}{month} is claimed for {name}, "
                        f"but the tab's {label} day is {from_tab} where {name} uses "
                        f"{stored}. The column order is wrong."
                    )

            session.add(
                CardStatement(
                    period=period, account_id=account.id, bill_eom=bill.bill_eom
                )
            )
            written += 1
            seen.add(name)

    session.flush()
    return [f"card statements: {written} across {len(seen)} card(s) -- {', '.join(sorted(seen))}"]


# ------------------------------------------------------------------------------- salary


def _backfill_salary(session, values, formulas, ref: xr.RefData) -> list[str]:
    """Payslips, PAYE bands, the salary profile and any bonus.

    All of it is year-scoped except the salary profile, which is a single effective-dated
    history that the backfill extends backwards rather than replaces.
    """
    year = ref.tax_year
    periods = [ref.period_for(m) for m in xr.FISCAL_MONTHS]

    if session.scalar(select(func.count()).select_from(Payslip).where(Payslip.period.in_(periods))):
        raise ValueError(f"Payslips already exist for {year}/{str(year + 1)[-2:]}.")
    if session.scalar(
        select(func.count()).select_from(SalaryAssumption).where(SalaryAssumption.tax_year == year)
    ):
        raise ValueError(f"Salary assumptions already exist for tax year {year}.")

    done: list[str] = []

    bands = xr.read_salary_assumptions(values, year)
    stated = {key for key, _, _ in bands}
    for key, value in MISSING_BANDS.get(year, {}).items():
        if key not in stated:
            bands.append((key, dt.date(year, 4, 1), value))
            done.append(f"    {key} set to {value} -- the workbook models none")
    for key, effective_from, value in bands:
        session.add(
            SalaryAssumption(
                tax_year=year, key=key, effective_from=effective_from, value=value
            )
        )
    done.insert(0, f"salary bands: {len(bands)} for tax year {year}")

    # Salary and bonus come from the formulas, not the values -- see read_salary_extras.
    extras = xr.read_salary_extras(formulas, values, ref)
    payslips = xr.read_payslips(values, ref)
    bonuses = 0
    for row in payslips:
        salary, bonus = extras.get(row["period"], (row["salary"], Decimal("0")))
        row["salary"] = salary
        session.add(Payslip(**row))
        if bonus:
            session.add(Bonus(period=row["period"], amount=bonus, note="Bonus"))
            bonuses += 1
    done.append(f"payslips: {len(payslips)} ({bonuses} with a bonus)")

    # The profile is one history across every year, so a row is added only where the salary
    # actually changes -- and never on top of one that is already there.
    existing = {
        p.effective_from: p.annual_salary
        for p in session.scalars(select(SalaryProfile))
    }
    in_force = None
    profiles = 0
    for row in sorted(payslips, key=lambda r: r["period"]):
        salary = row["salary"]
        if salary is None:
            continue
        y, m = (int(p) for p in row["period"].split("-"))
        start = dt.date(y, m, 1)
        if salary != in_force and start not in existing:
            session.add(
                SalaryProfile(
                    effective_from=start,
                    annual_salary=salary,
                    note=f"Backfilled from the {year}/{str(year + 1)[-2:]} Salary tracker",
                )
            )
            profiles += 1
        in_force = salary
    done.append(f"salary profile: {profiles} change(s) added")

    session.flush()
    return done


# ------------------------------------------------------------------------------ cycling


def _backfill_cycling(session, values, ref: xr.RefData) -> list[str]:
    """Outgoings, days ridden and the fares that valued them.

    The rates matter as much as the days: 25-26's commute fare was 8.90 against 26-27's
    10.50, so without a rate dated to this year every 2025 ride would be valued at a fare
    that had not happened yet -- or, since the rates only start in April 2026, at nothing.
    """
    outgoings, ridden, rates = xr.read_cycling(values)
    year_start = dt.date(ref.tax_year, 4, 1)
    done: list[str] = []

    if ridden and session.scalar(
        select(func.count())
        .select_from(CyclingDay)
        .where(CyclingDay.date.between(ridden[0]["date"], ridden[-1]["date"]))
    ):
        raise ValueError("Cycling days already exist in this workbook's range.")

    # Not keyed, so a re-run would duplicate rather than collide. Matched on what makes an
    # outgoing the same outgoing. One 25-26 row predates the fiscal year -- a saddle clamp
    # bought on 20 March 2025 -- and is kept: it is bike spending the workbook records, and
    # nothing else holds it.
    seen = {
        (o.date, o.item, o.amount) for o in session.scalars(select(CyclingOutgoing))
    }
    added = 0
    for row in outgoings:
        if (row["date"], row["item"], row["amount"]) in seen:
            continue
        session.add(CyclingOutgoing(**row))
        added += 1
    done.append(f"cycling outgoings: {added} of {len(outgoings)} added")

    for row in ridden:
        session.add(CyclingDay(**row))
    done.append(
        f"cycling days: {len(ridden)}"
        + (f" ({ridden[0]['date']} to {ridden[-1]['date']})" if ridden else "")
    )

    for kind, amount in rates.items():
        if amount and session.get(CyclingRate, (kind, year_start)) is None:
            session.add(
                CyclingRate(kind=kind, effective_from=year_start, amount=amount)
            )
    done.append(
        "cycling rates from "
        f"{year_start}: " + ", ".join(f"{k} {v}" for k, v in rates.items())
    )

    session.flush()
    return done


# -------------------------------------------------------------------------------- cards


def _backfill_cards(session, values) -> list[str]:
    """Balance transfer cards, per CARD_PLAN -- a card at a time, never wholesale."""
    done: list[str] = []
    order = max(
        (c.display_order or 0 for c in session.scalars(select(Card))), default=-1
    )

    for card in xr.read_cards(values):
        plan = CARD_PLAN.get(card["name"])
        if plan is None:
            continue

        if plan == "extend":
            existing = session.scalar(select(Card).where(Card.name == card["name"]))
            if existing is None:
                raise ValueError(
                    f"{card['name']} is marked to extend backwards but does not exist."
                )
            was = (existing.opening_balance, existing.opening_date, existing.term_months)
            existing.opening_balance = card["opening_balance"]
            existing.opening_date = card["opening_date"]
            existing.term_months = card["term_months"]
            # payment_day, min_payment_pct and credit_limit are left alone: those are the
            # card's current settings, which the user maintains in the dashboard.
            done.append(
                f"    {card['name']}: {was[0]} from {was[1]} over {was[2]}m"
                f"  ->  {card['opening_balance']} from {card['opening_date']}"
                f" over {card['term_months']}m"
            )
            continue

        if session.scalar(select(Card).where(Card.name == plan)) is not None:
            done.append(f"    {plan}: already present, left alone")
            continue
        order += 1
        session.add(Card(**{**card, "name": plan, "display_order": order}))
        done.append(
            f"    {plan}: created, {card['opening_balance']} from "
            f"{card['opening_date']} over {card['term_months']}m"
        )

    done.insert(0, f"cards: {len(done)} change(s)")
    session.flush()
    return done


def snapshot(path: Path) -> Path:
    """A copy of the database beside it, before anything is written.

    VACUUM INTO rather than a file copy: transactionally consistent even mid-write, where
    copying a live SQLite file can capture a torn state (DESIGN.md 7). Takes the path rather
    than reading config, because this script accepts --db and a snapshot of the wrong file
    is worse than none.
    """
    target = path.with_name(f"{path.stem}.pre-backfill-{dt.datetime.now():%Y%m%d-%H%M%S}.db")
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target


def _card_names(values, formulas, ref: xr.RefData) -> set[str]:
    """Accounts the month tabs treat as credit cards."""
    layout = xr.month_layout(values, formulas, "April", ref.period_for("April"))
    return {b.name for b in layout.blocks if b.type == "credit_card"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--dry-run", action="store_true", help="report, then roll back")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.workbook.exists():
        print(f"No workbook at {args.workbook}")
        return 1

    print(f"Workbook  {args.workbook}")
    print(f"Database  {args.db}")
    print()

    # The same two guards seed_interest_tracker has, and this writes a great deal more than
    # that does. A dry run needs neither: it rolls back, and the migration it does leave
    # behind is one the dashboard would have applied on its next start anyway.
    if not args.dry_run:
        if in_use(args.db):
            print("The dashboard still has the database open. Close every window and re-run.")
            return 1
        print(f"Snapshot taken: {snapshot(args.db)}\n")

    engine = make_engine(args.db)
    try:
        # Migrate first, and never assume the dashboard has already done it. This writes
        # values in the units the *current* code uses -- a card's minimum payment is a
        # percentage since schema 8 -- so running against an unmigrated file would leave
        # 2.50 sitting in a column where every other row still held 0.03.
        for line in create_all(engine):
            print(f"  migrated: {line}")

        with make_session_factory(engine)() as session:
            try:
                report = backfill(session, args.workbook, args.verbose)
            except ValueError as exc:
                session.rollback()
                print(f"Refused: {exc}")
                return 1
            for line in report:
                print(f"  {line}")
            if not args.dry_run and INCOMPLETE:
                session.rollback()
                print(
                    "\nRefused: this script is not finished. It currently widens the "
                    "reference data\nand creates closed accounts, but imports no "
                    "transactions, openings, salary,\ncycling, cards or savings plan -- so "
                    "committing it would leave a year of accounts\nwith nothing in them. "
                    "Use --dry-run until INCOMPLETE is cleared."
                )
                return 1
            if args.dry_run:
                session.rollback()
                print("\nDry run -- nothing written.")
            else:
                service.bump_revision(session)
                session.commit()
                print("\nWritten. Check the dashboard, then push from the Sync page.")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
