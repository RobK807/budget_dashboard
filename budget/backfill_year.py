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
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget import config, service, xlsm_reader as xr
from budget.db import make_engine, make_session_factory
from budget.models import (
    Account,
    Category,
    Classification,
    ImportBatch,
    OpeningBalance,
    Txn,
)
from budget.postings import postings_for

# Workbook name -> database name. Confirmed with the user: the card was renamed, not replaced.
ACCOUNT_ALIASES = {"Amex": "BA Amex"}

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

# Stage one only: reference data. Clear this once transactions, opening balances,
# salary, cycling, cards and the savings plan are all in, so a half-built import cannot
# be committed to a real database by accident.
INCOMPLETE = True


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

    return done


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

    engine = make_engine(args.db)
    try:
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
