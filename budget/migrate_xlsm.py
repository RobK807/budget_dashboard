"""One-off migration: legacy workbook -> SQLite.

Run with:  python -m budget.migrate_xlsm [--db PATH] [--workbook PATH] [--force]
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget import config, service, xlsm_reader as xr
from budget.db import create_all, make_engine, make_session_factory
from budget.models import (
    Account,
    Budget,
    Category,
    Classification,
    ClassificationAllowance,
    ClassificationOpening,
    DbMeta,
    ImportBatch,
    OpeningBalance,
    Projection,
    Setting,
    Txn,
)


def _valid_from(ref: xr.RefData, month_name: str | None) -> dt.date:
    """Fiscal month name -> first day of that month, defaulting to the year start."""
    period = ref.period_for(month_name or "April")
    year, month = (int(p) for p in period.split("-"))
    return dt.date(year, month, 1)


def import_reference(session: Session, values, formulas, ref: xr.RefData) -> None:
    # Account type is not stored anywhere in Selections -- add_account takes it as a form
    # argument and only uses it to pick a formula. Recover it from the month-tab formulas.
    type_cache: dict[str, dict[str, str]] = {}

    def account_type(name: str, first_month: str | None) -> str:
        month = first_month or "April"
        if month not in type_cache:
            layout = xr.month_layout(values, formulas, month, ref.period_for(month))
            type_cache[month] = {b.name: b.type for b in layout.blocks}
        return type_cache[month].get(name, "bank")

    for a in ref.accounts:
        session.add(
            Account(
                name=a.name,
                short_code=a.short_code,
                type=account_type(a.name, a.first_month),
                is_savings=a.is_savings,
                savings_limit=a.savings_limit or None,
                is_investment=a.is_investment,
                investment_limit=a.investment_limit or None,
                is_isa=a.is_isa,
                display_order=a.display_order,
                valid_from=_valid_from(ref, a.first_month),
            )
        )

    rollovers = xr.read_rollovers(values, "April")
    for c in ref.classifications:
        session.add(
            Classification(
                name=c.name,
                legacy_ref=c.legacy_ref,
                direction=c.direction,
                rollover=rollovers.get(c.name, "none"),
                counts_as_spend=True,
                display_order=c.display_order,
                valid_from=_valid_from(ref, "April"),
            )
        )

    for c in ref.categories:
        session.add(
            Category(
                name=c.name,
                grouping=c.grouping,
                spend_type=c.spend_type,
                display_order=c.display_order,
                valid_from=_valid_from(ref, "April"),
            )
        )

    for key, value in ref.settings.items():
        session.add(Setting(key=key, value=value))

    session.add(DbMeta(id=1, revision=1, base_revision=0, pushed_revision=0, mode="online"))
    session.flush()


# Groupings for categories that Selections no longer defines. Without an entry here a
# recovered category lands in 'Other', which is a placeholder rather than an answer.
RECOVERED_GROUPINGS: dict[str, str] = {
    "Claude": "Regular outgoings",
}


def recover_orphan_categories(session: Session, rows: list[xr.LedgerRow]) -> list[str]:
    """Re-create categories that transactions reference but Selections no longer lists.

    remove_category deletes a category from Selections and the month tabs but leaves the
    name on every historic Debug row, so the definition is lost while the spending remains.
    Rather than drop real transactions, recreate the category effective-dated to the span it
    was actually used in -- which is what the workbook should have done and could not.

    Grouping cannot be recovered, so these land in 'Other' and are reported for correction.
    """
    known = {c.name for c in session.scalars(select(Category))}
    orphans: dict[str, list[xr.LedgerRow]] = {}
    for row in rows:
        if row.category and row.category not in known:
            orphans.setdefault(row.category, []).append(row)

    notes = []
    for name, used_by in sorted(orphans.items(), key=lambda kv: kv[0].casefold()):
        dates = [r.txn_date for r in used_by]
        first, last = min(dates), max(dates)
        types = {r.type for r in used_by}
        spend_type = types.pop() if len(types) == 1 and types <= {"Credit", "Debit"} else "All"
        last_day = calendar.monthrange(last.year, last.month)[1]

        grouping = RECOVERED_GROUPINGS.get(name, "Other")
        session.add(
            Category(
                name=name,
                grouping=grouping,
                spend_type=spend_type,
                display_order=None,
                valid_from=first.replace(day=1),
                valid_to=last.replace(day=last_day),
            )
        )
        suffix = (
            f"grouping {grouping!r}"
            if name in RECOVERED_GROUPINGS
            else "filed under grouping 'Other', please reclassify"
        )
        notes.append(
            f"recovered category {name!r}: {len(used_by)} transactions {first}..{last}, "
            f"absent from Selections -- {suffix}"
        )

    session.flush()
    return notes


def import_ledger(session: Session, values, ref: xr.RefData) -> list[str]:
    rows, notes = xr.read_ledger(values)
    notes += recover_orphan_categories(session, rows)

    accounts = {a.name: a.id for a in session.scalars(select(Account))}
    categories = {c.name: c.id for c in session.scalars(select(Category))}
    classifications = {c.name: c.id for c in session.scalars(select(Classification))}

    batch = ImportBatch(
        filename=config.WORKBOOK_PATH.name,
        row_count=len(rows),
        note=f"{service.MIGRATION_NOTE} from the workbook's Debug sheet",
    )
    session.add(batch)
    session.flush()

    for row in rows:
        if row.account_from not in accounts:
            raise ValueError(f"row {row.source_row}: unknown account {row.account_from!r}")
        if row.account_to and row.account_to not in accounts:
            raise ValueError(f"row {row.source_row}: unknown destination {row.account_to!r}")
        if row.category and row.category not in categories:
            raise ValueError(f"row {row.source_row}: unknown category {row.category!r}")
        if row.classification and row.classification not in classifications:
            raise ValueError(
                f"row {row.source_row}: unknown classification {row.classification!r}"
            )

        session.add(
            Txn(
                txn_date=row.txn_date,
                period=ref.period_for(row.month),
                type=row.type,
                amount=row.amount,
                account_from_id=accounts[row.account_from],
                account_to_id=accounts.get(row.account_to) if row.account_to else None,
                category_id=categories.get(row.category) if row.category else None,
                classification_id=(
                    classifications.get(row.classification) if row.classification else None
                ),
                comment=row.comment,
                category_comment=row.category_comment,
                legacy_identifier=row.identifier,
                created_at=row.created_at or dt.datetime.now(),
                deleted_at=dt.datetime.now() if row.removed else None,
                deleted_reason=(
                    row.removed_reason or "Flagged removed in the workbook"
                    if row.removed
                    else None
                ),
                source="bulk",
                batch_id=batch.id,
            )
        )

    session.flush()
    return notes


def import_periodic(session: Session, values, formulas, ref: xr.RefData) -> list[str]:
    accounts = {a.name: a.id for a in session.scalars(select(Account))}
    categories = {c.name: c.id for c in session.scalars(select(Category))}
    classifications = {c.name: c.id for c in session.scalars(select(Classification))}
    notes: list[str] = []

    for month in xr.FISCAL_MONTHS:
        period = ref.period_for(month)
        layout = xr.month_layout(values, formulas, month, period)

        for name, amount in xr.read_opening_balances(values, layout).items():
            if name in accounts:
                session.add(
                    OpeningBalance(account_id=accounts[name], period=period, amount=amount)
                )

        for name, (income, expected) in xr.read_budgets(values, month).items():
            if name in categories:
                session.add(
                    Budget(
                        period=period,
                        category_id=categories[name],
                        income=income or None,
                        expected=expected or None,
                    )
                )

        # Opening adjustments typed into the running-total formulas (April's Excess).
        for name, amount in xr.read_running_opening(formulas, values, month).items():
            if name in classifications and amount:
                session.add(
                    ClassificationOpening(
                        period=period,
                        classification_id=classifications[name],
                        amount=amount,
                    )
                )
                notes.append(f"{month} {name}: opening balance {amount} recovered from formula")

        # The workbook applied 'Spend per day' to whichever running column was named Excess.
        allowance = xr.read_daily_allowance(values, month)
        if allowance and "Excess" in classifications:
            session.add(
                ClassificationAllowance(
                    period=period,
                    classification_id=classifications["Excess"],
                    daily_amount=allowance,
                )
            )

    projections = xr.read_projections(values)
    unknown = {name for _, name, _, _ in projections if name not in classifications}
    for day, name, amount, comment in projections:
        if name in classifications:
            session.add(
                Projection(
                    proj_date=day,
                    classification_id=classifications[name],
                    amount=amount,
                    comment=comment,
                )
            )
    if unknown:
        notes.append(
            "projection columns with no matching classification, skipped: "
            + ", ".join(sorted(unknown))
        )
    if projections:
        days = {day for day, _, _, _ in projections}
        notes.append(
            f"projections: {len(days)} day(s) "
            f"{min(days)}..{max(days)} across {len(classifications)} classifications"
        )

    session.flush()
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate the budget workbook into SQLite.")
    parser.add_argument("--workbook", type=Path, default=config.WORKBOOK_PATH)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--force", action="store_true", help="overwrite an existing database")
    args = parser.parse_args(argv)

    if args.db.exists():
        if not args.force:
            print(f"Refusing to overwrite {args.db} (pass --force).", file=sys.stderr)
            return 2
        args.db.unlink()
        for suffix in ("-wal", "-shm"):
            extra = args.db.with_name(args.db.name + suffix)
            if extra.exists():
                extra.unlink()

    print(f"Reading  {args.workbook}")
    values, formulas = xr.load(args.workbook)

    ref = xr.read_reference(values)
    print(
        f"  reference: {len(ref.accounts)} accounts, {len(ref.categories)} categories, "
        f"{len(ref.classifications)} classifications, tax year {ref.tax_year}"
    )

    engine = make_engine(args.db)
    create_all(engine)
    factory = make_session_factory(engine)

    with factory() as session, session.begin():
        import_reference(session, values, formulas, ref)
        notes = import_ledger(session, values, ref)
        notes += import_periodic(session, values, formulas, ref)

    with factory() as session:
        n_txn = session.scalar(select(func.count()).select_from(Txn))
        n_del = session.scalar(
            select(func.count()).select_from(Txn).where(Txn.deleted_at.is_not(None))
        )
        n_ob = session.scalar(select(func.count()).select_from(OpeningBalance))
        n_bud = session.scalar(select(func.count()).select_from(Budget))
        n_card = session.scalar(
            select(func.count()).select_from(Account).where(Account.type == "credit_card")
        )

    print(f"Wrote    {args.db}")
    print(f"  transactions:     {n_txn} ({n_del} soft-deleted)")
    print(f"  credit cards:     {n_card} of {len(ref.accounts)} accounts")
    print(f"  opening balances: {n_ob}")
    print(f"  budget rows:      {n_bud}")
    for note in notes:
        print(f"  fix: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
