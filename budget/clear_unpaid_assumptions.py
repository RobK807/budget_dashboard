"""Take the workbook's standing assumptions off months that have not been paid.

`payslip` came across from the Salary tracker with all twelve rows populated, because the
workbook held its assumptions there: benefits of 1,177.88 and additional pay of 24.00 on
every month of the year, whether or not that month had happened. They were inputs to its
expected-pay formula, not records of anything.

Here they are neither. The expected figures are built from `salary_profile` and the salary
parameters, so nothing reads these two columns any more -- but they still surface under
Pension (A) and Home working (A) on the comparison table, beside a blank gross and a blank
net. A month with no payslip has no actual pension, and saying otherwise invites the figures
to be reconciled against something that was never paid.

Cleared only where the month has neither a gross nor a net, which is what 'not paid yet'
means. April to July keep theirs. `payday` stays: an expected pay date is a genuine
forward-looking figure, not a claim about what happened.

Idempotent -- a second run finds nothing to do.

Run with:  python -m budget.clear_unpaid_assumptions
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3

from budget import config, repo
from budget.db import create_all, in_use, make_engine, make_session_factory
from budget.models import Payslip

# What the workbook used as inputs and this application derives instead.
ASSUMPTION_COLUMNS = ("benefits", "additional")


def snapshot() -> str:
    """VACUUM INTO rather than a file copy: transactionally consistent even mid-write."""
    target = config.DB_PATH.with_name(
        f"budget.pre-clear-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target.name


def unpaid(payslips) -> list[str]:
    """Periods with a payslip row but nothing actually paid."""
    if payslips.empty:
        return []
    import pandas as pd

    out = []
    for _, row in payslips.iterrows():
        paid = pd.notna(row["gross"]) or pd.notna(row["net"])
        carries = any(pd.notna(row[c]) for c in ASSUMPTION_COLUMNS)
        if not paid and carries:
            out.append(row["period"])
    return out


def report(payslips) -> list[str]:
    """Print what will be cleared. ASCII only -- this prints to a cp1252 console."""
    import pandas as pd

    periods = unpaid(payslips)
    print("\nPayslip rows carrying assumptions with nothing paid")
    print("-" * 72)
    print(f"{'month':<10}{'gross':>12}{'net':>12}{'benefits':>12}{'additional':>12}")
    for _, row in payslips.iterrows():
        marker = "  <- clear" if row["period"] in periods else ""

        def show(value):
            return f"{value:,.2f}" if pd.notna(value) else "-"

        print(
            f"{row['period']:<10}{show(row['gross']):>12}{show(row['net']):>12}"
            f"{show(row['benefits']):>12}{show(row['additional']):>12}{marker}"
        )
    print("-" * 72)
    print(f"  {len(periods)} month(s) to clear; payday is left alone on all of them.")
    return periods


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clear unpaid months' assumptions.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args(argv)

    engine = make_engine()
    try:
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as session:
            payslips = repo.load_payslips(session)
    finally:
        engine.dispose()

    periods = report(payslips)
    if not periods:
        print("\nNothing to do.")
        return 0

    if in_use():
        print("\nThe dashboard still has the database open. Close every window and re-run.")
        return 1

    if not args.yes and input("\nApply? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was changed.")
        return 1

    print(f"\nSnapshot taken: {snapshot()}")

    engine = make_engine()
    try:
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as session, session.begin():
            from budget.service import bump_revision

            for period in periods:
                row = session.get(Payslip, period)
                if row is None:
                    continue
                for column in ASSUMPTION_COLUMNS:
                    setattr(row, column, None)
            session.flush()
            bump_revision(session)

        with factory() as session:
            after = repo.load_payslips(session)
    finally:
        engine.dispose()

    remaining = unpaid(after)
    print(f"\n  cleared {len(periods)} month(s); {len(remaining)} still carrying assumptions")
    print("\nThere is now a push pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
