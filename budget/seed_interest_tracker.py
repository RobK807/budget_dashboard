"""Bring across what the Savings interest tracker held that the ledger did not.

Three things, none of which is new *data* so much as new structure over data already there:

  1. The savings and investment plan, as an effective-dated monthly target per account.
     The tracker held one column per revision (`Target - 08/25`, `Target - 10/25` ...) with
     the date in the row above, so reading it meant knowing which column was current.

     The totals are the ones the dashboard already had, which is the check that the mapping
     is right rather than merely plausible:

         savings      NS&I 250 + Wedding 350 + Marcus 300 = 900
         investments  CSD  250 + HSBC     100            = 350

     'CSD' is Charles Stanley Direct, which is the ledger's Stocks & Shares ISA -- proven by
     the balance, not the name: the plan's April 2026 actual of 7,509.80 is that account's
     April opening to the penny, and its 1,679.75 is HSBC Investments'.

  2. Halifax's interest is paid net. Every other account is gross, which is why the flag
     names the exception.

  3. Transaction 582 was one payment of 35.70 holding two different things: a 30.00 donation
     and a 5.70 transaction fee. Aggregating donations needs them apart, because only one of
     the two is a gift. Everything else about the payment -- date, account, category,
     classification -- is unchanged, and 30.00 + 5.70 still leaves the month's totals exactly
     where they were, which is what keeps the reconciliation green.

Idempotent: it writes the same values whatever it finds. Run it twice and the second run
changes nothing.

Run with:  python -m budget.seed_interest_tracker
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from decimal import Decimal

from sqlalchemy import select

from budget import config, reference, repo
from budget.db import create_all, make_engine, make_session_factory
from budget.models import Category, Classification, Txn
from budget.service import add_transaction
from budget.validation import Candidate

# 'Savings & investment plan' B5:H9. The spreadsheet's account names on the left, the
# ledger's on the right -- the only place the two vocabularies meet.
ACCOUNTS = {
    "CSD": "Stocks & Shares ISA",
    "HSBC": "HSBC Investments",
    "NSI": "NS&I",
    "Wedding": "Savings - Wedding",
    "Marcus": "Savings - Marcus",
}

# Each revision of the plan and the date it took effect (row 3). A zero is stored rather than
# omitted: from November 2026 the plan stops paying into Wedding, and leaving the row out
# would carry the previous 350 forward instead.
PLAN = {
    dt.date(2025, 8, 1): {"CSD": 250, "HSBC": 50, "NSI": 200, "Wedding": 250, "Marcus": 250},
    dt.date(2025, 10, 1): {"CSD": 250, "HSBC": 100, "NSI": 250, "Wedding": 300, "Marcus": 300},
    dt.date(2026, 3, 1): {"CSD": 250, "HSBC": 100, "NSI": 250, "Wedding": 350, "Marcus": 300},
    dt.date(2026, 11, 1): {"CSD": 250, "HSBC": 100, "NSI": 250, "Wedding": 0, "Marcus": 700},
    dt.date(2027, 5, 1): {"CSD": 500, "HSBC": 250, "NSI": 500, "Wedding": 0, "Marcus": 0},
}

# 'Savings & investment plan' L4. Held as a percentage, as every other rate in the database
# is since v3 -- a fraction in a two-decimal column could only ever be a whole point.
INVESTMENT_RETURN_ANNUAL = Decimal("6.00")

# Interest tracker row 3: 'Net' for Halifax, 'Gross' for everything else.
NET_INTEREST_ACCOUNTS = ("Halifax",)

# The donation and the fee it was paid with.
DONATION_TXN_ID = 582
DONATION_AMOUNT = Decimal("30.00")
FEE_AMOUNT = Decimal("5.70")
DONATION_COMMENT = "Charity"
FEE_COMMENT = "transaction fee"


def in_use() -> bool:
    """True if another connection holds the database.

    Tested by taking an exclusive lock rather than by looking for -wal/-shm: those linger
    after a perfectly clean close, so their presence says nothing.
    """
    try:
        conn = sqlite3.connect(config.DB_PATH, timeout=1)
    except sqlite3.Error:
        return True
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()


def snapshot() -> str:
    """VACUUM INTO rather than a file copy: transactionally consistent even mid-write, where
    copying a live SQLite file can capture a torn state (DESIGN.md 7)."""
    target = config.DB_PATH.with_name(
        f"budget.pre-interest-seed-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target.name


def report() -> bool:
    """Print what will be written and check it adds up. ASCII only -- this prints to a
    cp1252 console, where a tick mark raises UnicodeEncodeError before anything is written."""
    print("\nSavings and investment plan")
    print("-" * 78)
    names = list(ACCOUNTS)
    print(f"{'from':<14}" + "".join(f"{n:>11}" for n in names) + f"{'total':>11}")
    for when in sorted(PLAN):
        row = PLAN[when]
        print(
            f"{when:%d %b %Y}".ljust(14)
            + "".join(f"{row[n]:>11,}" for n in names)
            + f"{sum(row.values()):>11,}"
        )
    print("-" * 78)

    current = PLAN[dt.date(2026, 3, 1)]
    savings = current["NSI"] + current["Wedding"] + current["Marcus"]
    investments = current["CSD"] + current["HSBC"]
    ok = savings == 900 and investments == 350
    print(f"  in force now:  savings {savings:,} (dashboard had 900)   "
          f"investments {investments:,} (dashboard had 350)   "
          f"{'[ok]' if ok else '[DOES NOT MATCH]'}")

    print("\nTransaction 582")
    print("-" * 78)
    total = DONATION_AMOUNT + FEE_AMOUNT
    matches = total == Decimal("35.70")
    ok = ok and matches
    print(f"  donation {DONATION_AMOUNT:>8,.2f}  '{DONATION_COMMENT}'")
    print(f"  fee      {FEE_AMOUNT:>8,.2f}  '{FEE_COMMENT}'")
    print(f"  total    {total:>8,.2f}  vs the recorded 35.70   "
          f"{'[ok]' if matches else '[DOES NOT MATCH]'}")

    print(f"\nNet interest: {', '.join(NET_INTEREST_ACCOUNTS)}")
    print(f"Expected annual investment return: {INVESTMENT_RETURN_ANNUAL}%")
    return ok


def apply(session) -> list[str]:
    """Everything this script writes, in one transaction."""
    done: list[str] = []
    accounts = repo.load_reference(session)["accounts"]
    by_name = accounts.set_index("name")

    missing = [
        ledger for ledger in ACCOUNTS.values() if ledger not in by_name.index
    ]
    if missing:
        raise SystemExit(f"No such account(s): {', '.join(missing)}")

    # ---- the plan -----------------------------------------------------------------
    written = 0
    for when, row in sorted(PLAN.items()):
        for label, amount in row.items():
            account_id = int(by_name.loc[ACCOUNTS[label], "id"])
            reference.set_savings_plan(session, account_id, when, Decimal(amount))
            written += 1
    done.append(f"savings plan: {written} target(s) across {len(PLAN)} revision(s)")

    reference.set_setting(
        session, "investment_return_annual", str(INVESTMENT_RETURN_ANNUAL)
    )
    done.append(f"setting investment_return_annual = {INVESTMENT_RETURN_ANNUAL}")

    # ---- gross / net --------------------------------------------------------------
    for name in NET_INTEREST_ACCOUNTS:
        if name in by_name.index:
            reference.update_account(
                session, int(by_name.loc[name, "id"]), interest_net=True
            )
            done.append(f"{name}: interest flagged as net")

    # ---- the donation -------------------------------------------------------------
    txn = session.get(Txn, DONATION_TXN_ID)
    if txn is None:
        raise SystemExit(f"Transaction {DONATION_TXN_ID} not found.")

    if txn.amount + _fee_already_there(session, txn) != Decimal("35.70"):
        raise SystemExit(
            f"Transaction {DONATION_TXN_ID} is {txn.amount}, and the fee beside it does not "
            "bring the pair back to 35.70. Nothing was changed."
        )

    txn.amount = DONATION_AMOUNT
    txn.comment = DONATION_COMMENT
    txn.is_donation = True
    session.flush()
    done.append(f"txn {DONATION_TXN_ID}: {DONATION_AMOUNT} '{DONATION_COMMENT}', donation")

    if _fee_row(session, txn) is None:
        candidate = Candidate(
            txn_date=txn.txn_date,
            type=txn.type,
            amount=FEE_AMOUNT,
            account_from=by_name.index[by_name["id"] == txn.account_from_id][0],
            category=_name_of(session, "category", txn.category_id),
            classification=_name_of(session, "classification", txn.classification_id),
            comment=FEE_COMMENT,
            category_comment=txn.category_comment,
            is_donation=False,
        )
        created, result = add_transaction(session, candidate, source="manual")
        if created is None:
            raise SystemExit(f"The fee row was rejected: {'; '.join(result.errors)}")
        done.append(f"txn {created.id}: {FEE_AMOUNT} '{FEE_COMMENT}', not a donation")
    else:
        done.append("the fee row is already there")

    return done


def _fee_row(session, txn):
    """The 5.70 counterpart, if this has already run. Matched on date, account and comment
    rather than on amount, so a re-run finds it whatever it now holds."""
    return session.scalars(
        select(Txn).where(
            Txn.txn_date == txn.txn_date,
            Txn.account_from_id == txn.account_from_id,
            Txn.comment == FEE_COMMENT,
            Txn.deleted_at.is_(None),
        )
    ).first()


def _fee_already_there(session, txn) -> Decimal:
    row = _fee_row(session, txn)
    return row.amount if row is not None else Decimal("0")


def _name_of(session, kind: str, row_id: int | None) -> str | None:
    if row_id is None:
        return None
    model = Category if kind == "category" else Classification
    row = session.get(model, row_id)
    return row.name if row else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the interest tracker's structure.")
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
            for line in apply(session):
                print(f"  {line}")

        with factory() as session:
            plan = repo.load_savings_plan(session)
            accounts = repo.load_reference(session)["accounts"]
            txns = repo.load_transactions(session)
            given = repo.donations(txns)

        print(f"\n  plan rows stored: {len(plan)}")
        periods = ["2026-07", "2026-11", "2027-05"]
        derived = repo.targets_from_plan(plan, accounts, periods)
        print("\n  derived overview:")
        for _, row in derived.iterrows():
            print(f"    {row['period']}   savings {row['savings']:>9,.2f}   "
                  f"investments {row['investments']:>9,.2f}")
        print("\n  donations recorded:")
        for _, row in given.iterrows():
            print(f"    {row['year']}  #{row['id']}  {row['date']:%d %b %Y}  "
                  f"{row['amount']:>8,.2f}  {row['comment']}")
    finally:
        engine.dispose()

    print("\nThere is now a push pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
