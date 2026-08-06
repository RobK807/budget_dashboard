"""Separate May 2026's bonus from May 2026's salary.

`payslip` is keyed by period, so before `bonus` gained its own actual columns the only place
May's two payments could go was one row -- and that is what happened: `payslip.2026-05` held
gross 38,757.45, which is the salary and the bonus added together. Recording the bonus
separately without splitting the payslip would count 29,028.48 twice.

The split is not a guess. Subtracting the bonus from the combined row leaves gross 9,728.97
and NI 357.86, which match June and July *to the penny*, and all four columns sum back to the
combined figures exactly:

    gross   9,728.97 + 29,028.48 = 38,757.45
    NI        357.86 +    580.57 =    938.43
    PAYE    3,158.55 + 13,011.09 = 16,169.64
    net     6,025.56 + 15,436.82 = 21,462.38

Holiday pay of 187.00 stays on the salary payslip, where every other month has it. It is
already inside gross, which is why the consistency identity is

    gross = NI + holiday pay + PAYE + net

-- the workbook's column N check -- and not `gross - NI - PAYE = net`, which is out by the
holiday pay in every month.

Idempotent: it writes the same values whatever it finds, so running it twice changes nothing
the second time. Everything here can equally be typed into the Salary page.

Run with:  python -m budget.split_may_bonus
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from decimal import Decimal

from budget import config, reference, repo
from budget.db import create_all, in_use, make_engine, make_session_factory

PERIOD = "2026-05"

# The bonus, as paid on 1 May.
BONUS = {
    "gross": Decimal("29028.48"),
    "ni": Decimal("580.57"),
    "paye": Decimal("13011.09"),
    "net": Decimal("15436.82"),
}
BONUS_PAYDAY = 1

# The salary payslip, with the bonus taken back out.
PAYSLIP = {
    "gross": Decimal("9728.97"),
    "ni": Decimal("357.86"),
    "holiday_pay": Decimal("187.00"),
    "paye": Decimal("3158.55"),
    "net": Decimal("6025.56"),
}


def snapshot() -> str:
    """VACUUM INTO rather than a file copy: transactionally consistent even mid-write, where
    copying a live SQLite file can capture a torn state (DESIGN.md 7)."""
    target = config.DB_PATH.with_name(
        f"budget.pre-may-split-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target.name


def report() -> bool:
    """Print the arithmetic. Returns whether every check passes."""
    combined = {k: PAYSLIP[k] + BONUS[k] for k in ("gross", "ni", "paye", "net")}
    expected = {
        "gross": Decimal("38757.45"), "ni": Decimal("938.43"),
        "paye": Decimal("16169.64"), "net": Decimal("21462.38"),
    }

    # ASCII only: this prints to a cp1252 console on Windows, where a tick mark raises
    # UnicodeEncodeError and takes the script down before it writes anything.
    print("\nMay 2026 - splitting the bonus out of the payslip")
    print("-" * 66)
    print(f"{'':<12}{'salary':>13}{'bonus':>13}{'combined':>13}{'was':>13}")
    ok = True
    for key in ("gross", "ni", "paye", "net"):
        matches = combined[key] == expected[key]
        ok = ok and matches
        print(f"{key.upper():<12}{PAYSLIP[key]:>13,.2f}{BONUS[key]:>13,.2f}"
              f"{combined[key]:>13,.2f}{expected[key]:>13,.2f}"
              f"{'' if matches else '   <-- DOES NOT MATCH'}")
    print(f"{'HOLIDAY':<12}{PAYSLIP['holiday_pay']:>13,.2f}"
          f"{'-':>13}{'-':>13}{'187.00':>13}")
    print("-" * 66)

    # gross = NI + holiday + PAYE + net. Holiday pay is inside gross, which is why the
    # identity is not `gross - NI - PAYE = net`.
    for label, figures in (("salary", PAYSLIP), ("bonus", BONUS)):
        total = (
            figures["ni"] + figures.get("holiday_pay", Decimal("0"))
            + figures["paye"] + figures["net"]
        )
        matches = total == figures["gross"]
        ok = ok and matches
        print(f"  {label:<7} NI + holiday + PAYE + net = {total:>12,.2f}  vs gross "
              f"{figures['gross']:>12,.2f}   {'[ok]' if matches else '[CHECK THIS]'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Split May 2026's bonus from its payslip.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args(argv)

    if not report():
        print("\nThe figures do not reconcile. Nothing was changed.")
        return 1

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
            bonuses = repo.load_bonuses(session)
            match = bonuses[bonuses["period"] == PERIOD] if not bonuses.empty else None
            if match is None or match.empty:
                print(f"No bonus recorded for {PERIOD}; add it on the Salary page first.")
                return 1
            # The expected amount and note are left exactly as they are -- this is about the
            # actual figures, and the expected amount is what drives expected gross.
            reference.set_bonus(
                session, PERIOD,
                Decimal(match["amount"].iloc[0]), match["note"].iloc[0],
                payday=BONUS_PAYDAY, **BONUS,
            )
            reference.set_payslip(session, PERIOD, **PAYSLIP)

        with factory() as session:
            payslips = repo.load_payslips(session).set_index("period")
            bonuses = repo.load_bonuses(session).set_index("period")
            rows = len(bonuses)
            p, b = payslips.loc[PERIOD], bonuses.loc[PERIOD]

        print("\nStored:")
        print(f"  payslip  gross {p['gross']:>11,.2f}  NI {p['ni']:>8,.2f}  "
              f"holiday {p['holiday_pay']:>7,.2f}  PAYE {p['paye']:>10,.2f}  "
              f"net {p['net']:>10,.2f}")
        print(f"  bonus    gross {b['gross']:>11,.2f}  NI {b['ni']:>8,.2f}  "
              f"{'':>15}  PAYE {b['paye']:>10,.2f}  net {b['net']:>10,.2f}")
        print(f"  month    gross {p['gross'] + b['gross']:>11,.2f}  "
              f"NI {p['ni'] + b['ni']:>8,.2f}  {'':>15}  "
              f"PAYE {p['paye'] + b['paye']:>10,.2f}  "
              f"net {p['net'] + b['net']:>10,.2f}")
        print(f"\n  bonus rows: {rows} (period is the primary key, so this updated the "
              "existing row)")
    finally:
        engine.dispose()

    print("\nThere is now a push pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
