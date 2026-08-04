"""Loads the Phase 4 periodic data into an existing database.

Projections, daily allowances and classification opening balances live in the workbook but
had nowhere to go until Phase 4 added tables for them. Re-running the full migration would
work, but it rebuilds the ledger and resets the sync revision -- which would mean
force-pushing over a master that is already correct.

This imports only the new tables, replacing whatever is there, and bumps the revision so the
change syncs like any other edit.

Run with:  python -m budget.import_phase4 [--db PATH] [--workbook PATH]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import delete, select

from budget import config, service, xlsm_reader as xr
from budget.db import create_all, make_engine, make_session_factory
from budget.models import (
    Card,
    Classification,
    ClassificationAllowance,
    ClassificationOpening,
    CyclingDay,
    CyclingOutgoing,
    Payslip,
    Projection,
    SalaryAssumption,
    Setting,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import projections and allowances.")
    parser.add_argument("--workbook", type=Path, default=config.WORKBOOK_PATH)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    args = parser.parse_args(argv)

    print(f"Reading  {args.workbook}")
    values, formulas = xr.load(args.workbook)
    ref = xr.read_reference(values)

    engine = make_engine(args.db)
    create_all(engine)
    factory = make_session_factory(engine)

    with factory() as session, session.begin():
        classifications = {c.name: c.id for c in session.scalars(select(Classification))}

        # Replace wholesale: the workbook holds one month of projections at a time, and a
        # partial update would leave a stale mixture of months.
        session.execute(delete(Projection))
        session.execute(delete(ClassificationAllowance))
        session.execute(delete(ClassificationOpening))

        projections = xr.read_projections(values)
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

        allowances = openings = 0
        for month in xr.FISCAL_MONTHS:
            period = ref.period_for(month)

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
                    print(f"  opening: {month} {name} {value}")

        # --- salary, cards, cycling -------------------------------------------------
        session.execute(delete(Payslip))
        session.execute(delete(SalaryAssumption))
        session.execute(delete(Card))
        session.execute(delete(CyclingOutgoing))
        session.execute(delete(CyclingDay))

        for key, effective_from, value in xr.read_salary_assumptions(values, ref.tax_year):
            session.add(
                SalaryAssumption(
                    tax_year=ref.tax_year,
                    key=key,
                    effective_from=effective_from,
                    value=value,
                )
            )

        payslips = xr.read_payslips(values, ref)
        for row in payslips:
            session.add(Payslip(**row))

        cards = xr.read_cards(values)
        for row in cards:
            session.add(Card(**row))

        outgoings, ridden, rates = xr.read_cycling(values)
        for row in outgoings:
            session.add(CyclingOutgoing(**row))
        for row in ridden:
            session.add(CyclingDay(**row))
        for key, value in rates.items():
            if value:
                # merge, not add: re-running must overwrite rather than collide on the key.
                session.merge(Setting(key=f"cycling_rate_{key}", value=str(value)))

        revision = service.bump_revision(session)

    projection_days = {day for day, _, _, _ in projections}
    print(f"Wrote    {args.db}")
    print(f"  projections:  {len(projections)} rows over {len(projection_days)} day(s)")
    print(f"  allowances:   {allowances} month(s)")
    print(f"  openings:     {openings}")
    print(f"  payslips:     {len(payslips)}")
    print(f"  cards:        {len(cards)}")
    print(f"  cycling:      {len(outgoings)} outgoing(s), {len(ridden)} day(s) ridden")
    print(f"  cycling rates: { {k: str(v) for k, v in rates.items()} }")
    print(f"  revision now: {revision} — push from the Sync page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
