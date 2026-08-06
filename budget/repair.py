"""Rebuild damaged indexes in the local database.

SQLite distinguishes between damage to the *data* and damage to an *index over* the data.
The second sort reads as 'database disk image is malformed' and stops queries dead, but the
rows themselves are intact and REINDEX rebuilds every index from them. That is the whole
repair, and it is lossless.

This exists because it happened twice: `wrong # of entries in index sqlite_autoindex_setting_1`
-- the primary key on `setting`. Nothing could read a setting, which meant every write that
touched one died mid-transaction and rolled back, silently taking unrelated changes with it.

Where it will not help
----------------------

If integrity_check reports damaged *pages* rather than indexes, REINDEX cannot invent the
rows back. The script says so and stops rather than pretending; recover from a backup beside
the database or pull the master from the NAS on the Sync page.

Run with:  python -m budget.repair       (or double-click repair.bat)
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sqlite3

from budget import config
from budget.db import in_use

# The repairable kind: a count or an entry in an index, not a lost page.
INDEX_FAULTS = ("index", "wrong # of entries")


def integrity(path) -> list[str]:
    """Every complaint integrity_check makes, or ['ok']."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"cannot open: {exc}"]
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        # A fault bad enough that the check itself cannot run.
        return [f"integrity_check failed: {exc}"]
    finally:
        conn.close()


def repairable(faults: list[str]) -> bool:
    return bool(faults) and all(
        any(marker in fault.lower() for marker in INDEX_FAULTS) for fault in faults
    )


def backup() -> str:
    """A plain copy, deliberately.

    VACUUM INTO is the right way to snapshot a *healthy* database and is what the rest of
    this project uses -- but it reads through the very structures that are broken here, so on
    a damaged file it either fails or writes out the damage. `in_use` has already established
    that nothing else holds the file, so a byte copy is both safe and faithful.
    """
    target = config.DB_PATH.with_name(
        f"budget.pre-repair-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    shutil.copy2(config.DB_PATH, target)
    return target.name


def describe(path) -> None:
    """Whatever can still be read out of it, so the repair can be judged."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"  cannot open: {exc}")
        return
    try:
        for label, query in (
            ("revision", "SELECT revision FROM db_meta WHERE id = 1"),
            ("schema version", "SELECT value FROM setting WHERE key = 'schema_version'"),
            ("transactions", "SELECT count(*) FROM txn WHERE deleted_at IS NULL"),
            ("savings plan", "SELECT count(*) FROM savings_plan"),
        ):
            try:
                print(f"  {label:<16} {conn.execute(query).fetchone()[0]}")
            except sqlite3.Error as exc:
                print(f"  {label:<16} unreadable ({exc})")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild damaged database indexes.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args(argv)

    print(f"\nDatabase: {config.DB_PATH}")
    if not config.DB_PATH.exists():
        print("  There is no database at that path. Nothing to repair.")
        return 1

    faults = integrity(config.DB_PATH)
    print("\nBefore")
    print("-" * 66)
    for fault in faults:
        print(f"  {fault}")
    describe(config.DB_PATH)

    if faults == ["ok"]:
        print("\nNothing to repair -- this database is sound.")
        return 0

    if not repairable(faults):
        print(
            "\nThis is not index damage, so rebuilding indexes cannot fix it: the rows\n"
            "themselves are affected. Do NOT keep using this file.\n\n"
            "  - restore one of the budget.pre-*.db backups beside it, by renaming it to\n"
            "    budget.db with every dashboard window closed, or\n"
            "  - pull the master from the NAS on the Sync page.\n"
        )
        return 1

    if in_use():
        print("\nSomething still has the database open. Close every dashboard window,")
        print("run stop.bat if one is stuck, and try again.")
        return 1

    if not args.yes and input("\nRepair it? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing was changed.")
        return 1

    print(f"\nBackup taken: {backup()}")

    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("REINDEX")
        conn.commit()
    except sqlite3.DatabaseError as exc:
        print(f"\nREINDEX failed: {exc}")
        print("Restore from the backup above, or pull the master from the NAS.")
        return 1
    finally:
        conn.close()

    after = integrity(config.DB_PATH)
    print("\nAfter")
    print("-" * 66)
    for fault in after:
        print(f"  {fault}")
    describe(config.DB_PATH)

    if after == ["ok"]:
        print("\nRepaired. Nothing was lost -- the rows were always intact; it was the")
        print("indexes over them that were not.")
        return 0

    print("\nStill damaged. Restore from the backup above, or pull from the NAS.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
