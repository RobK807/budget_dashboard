"""Load the standing account commitments -- what leaves each account, and on which day.

    python -m budget.seed_commitments            # write anything missing
    python -m budget.seed_commitments --report   # say what would change, write nothing

Idempotent. A commitment is identified by its account and its name, so a second run finds
everything already there and writes nothing. An existing row whose amount or day differs is
*left alone* unless --update is passed: the Settings grid is the place these are maintained,
and a seed script that quietly overwrote a figure edited there would undo real work.

The Stocks & Shares Isa row arrived with a date and no amount. The 250.00 here is not a
guess: `savings_plan` has carried a 250 monthly target for that account since August 2025,
and HSBC has actually sent it 250.00 on the 4th every month (the 6th when the 4th falls on a
weekend). Both sources agree, so the figure is recorded rather than left at zero.

Note that this takes HSBC's commitments to 2,850.00 against a 2,800.00 target, which the
Summary page flags. That is the flag doing its job: several of HSBC's figures are round
stand-ins for variable amounts, so 50.00 is within an ordinary month's noise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget import config, reference
from budget.db import create_all, in_use, make_engine, make_session_factory
from budget.models import Account, AccountCommitment

# (account, item, amount, day). Amounts as strings: a float would not survive the round trip
# to pence intact, which is the whole reason Money exists.
COMMITMENTS: list[tuple[str, str, str, int]] = [
    ("First Direct", "Council tax", "215.00", 1),
    ("First Direct", "Dentist", "26.61", 1),
    ("First Direct", "Gym", "625.00", 1),
    ("First Direct", "Mortgage", "2642.85", 1),
    ("First Direct", "Water", "39.00", 1),
    ("First Direct", "Savings", "300.00", 2),
    ("First Direct", "Internet", "31.99", 19),

    ("Halifax", "Phone", "20.00", 4),
    ("Halifax", "Lottery", "12.50", 7),

    ("HSBC", "Nationwide", "200.00", 1),
    ("HSBC", "Service charge", "250.00", 1),
    ("HSBC", "Spending", "50.00", 1),
    ("HSBC", "GBBB", "15.00", 1),
    ("HSBC", "MBNA", "200.00", 1),
    ("HSBC", "Halifax", "350.00", 1),
    ("HSBC", "Lottery", "200.00", 1),
    ("HSBC", "Earnings data", "15.00", 3),
    ("HSBC", "Stocks & Shares Isa", "250.00", 4),
    ("HSBC", "Barclaycard", "120.00", 6),
    ("HSBC", "Base", "500.00", 19),
    ("HSBC", "Wedding", "350.00", 20),
    ("HSBC", "NS&I", "250.00", 20),
    ("HSBC", "HSBC investments", "100.00", 20),

    ("Nationwide", "Contact lenses", "55.00", 15),
    ("Nationwide", "Financial Times", "39.00", 22),
]

# The day each account is funded, which is where its cycle starts. First Direct is topped up
# at the start of the month; the rest run from payday on the 19th, so a payment on the 4th
# belongs to the cycle that began the month before.
START_DAYS: dict[str, int] = {
    "First Direct": 1,
    "Halifax": 19,
    "HSBC": 19,
    "Nationwide": 19,
}


def snapshot() -> str:
    """VACUUM INTO rather than a file copy: transactionally consistent even mid-write, where
    copying a live SQLite file can capture a torn state (DESIGN.md 7)."""
    target = config.DB_PATH.with_name(
        f"budget.pre-commitment-seed-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target.name


def apply(session: Session, *, write: bool, update: bool) -> dict:
    """Reconcile COMMITMENTS against what is stored. Returns what happened, or would."""
    accounts = {a.name: a.id for a in session.scalars(select(Account))}
    stored = {
        (c.account_id, c.name.casefold()): c
        for c in session.scalars(select(AccountCommitment))
    }

    added: list[str] = []
    updated: list[str] = []
    differs: list[str] = []
    refused: list[str] = []
    cycles: list[str] = []
    present = 0
    missing_accounts: set[str] = set()

    for account_name, start in START_DAYS.items():
        account_id = accounts.get(account_name)
        if account_id is None:
            missing_accounts.add(account_name)
            continue
        account = session.get(Account, account_id)
        if account.commitment_start_day == start:
            continue
        # A start day already set to something else was set deliberately, in Settings.
        if account.commitment_start_day is not None and not update:
            differs.append(
                f"{account_name}: cycle starts on the {account.commitment_start_day}, "
                f"script says the {start}"
            )
            continue
        if write:
            reference.update_account(session, account_id, commitment_start_day=start)
        cycles.append(f"{account_name}: cycle starts on the {start}")

    for account_name, item, amount, day in COMMITMENTS:
        account_id = accounts.get(account_name)
        if account_id is None:
            missing_accounts.add(account_name)
            continue

        existing = stored.get((account_id, item.casefold()))
        want = Decimal(amount)
        if existing is None:
            if write:
                outcome = reference.set_account_commitment(
                    session, account_id, item, want, day
                )
                if not outcome.ok:
                    refused.append(f"{account_name} / {item}: {outcome.message}")
                    continue
            added.append(f"{account_name} / {item}  {want} on the {day}")
        elif existing.amount != want or int(existing.day) != day:
            where = (
                f"{account_name} / {item}: stored {existing.amount} on the "
                f"{existing.day}, script says {want} on the {day}"
            )
            if update and write:
                reference.set_account_commitment(
                    session, account_id, item, want, day, commitment_id=existing.id
                )
                updated.append(where)
            else:
                differs.append(where)
        else:
            present += 1

    return {
        "added": added, "updated": updated, "differs": differs, "refused": refused,
        "cycles": cycles, "present": present,
        "missing_accounts": sorted(missing_accounts),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", action="store_true", help="say what would change, write nothing"
    )
    parser.add_argument(
        "--update", action="store_true",
        help="also correct the amount or day of a commitment already stored",
    )
    args = parser.parse_args(argv)

    if not args.report and in_use():
        print("The dashboard still has the database open. Close every window and re-run.")
        return 1

    if not args.report:
        print(f"Snapshot taken: {snapshot()}")

    engine = make_engine()
    try:
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as session, session.begin():
            result = apply(session, write=not args.report, update=args.update)
    finally:
        engine.dispose()

    for line in result["cycles"]:
        print(f"  @ {line}")
    for line in result["added"]:
        print(f"  + {line}")
    for line in result["updated"]:
        print(f"  ~ {line}")
    for line in result["differs"]:
        print(f"  ? {line}")
    for line in result["refused"]:
        print(f"  ! {line}")

    if result["missing_accounts"]:
        print(
            "\nNo account of that name: "
            + ", ".join(result["missing_accounts"])
            + "\nThese are matched by name, so check Settings > Accounts."
        )

    verb = "would add" if args.report else "added"
    print(
        f"\n{len(result['added'])} {verb}, {len(result['updated'])} updated, "
        f"{result['present']} already present, {len(result['differs'])} differing, "
        f"{len(result['cycles'])} cycle day(s) set."
    )
    if result["differs"] and not args.update:
        print("Pass --update to bring the differing ones into line with this script.")
    if not args.report and (
        result["added"] or result["updated"] or result["cycles"]
    ):
        print("\nThere is now a push pending.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
