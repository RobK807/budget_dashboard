"""Rebuild the local database from the NAS master.

For when the local copy is gone or unreadable and the dashboard will not start, so the Sync
page cannot be reached to pull from there. Same operation, without needing the app to run.

Safe by construction: it refuses if a local database already holds unpushed work, because
overwriting that is precisely the loss the sync module exists to prevent.

Run with:  python -m budget.restore      (or double-click restore.bat)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from budget import config, sync


def summarise(path: Path) -> str:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            revision = conn.execute(
                "SELECT revision FROM db_meta WHERE id = 1"
            ).fetchone()
            count = conn.execute(
                "SELECT count(*) FROM txn WHERE deleted_at IS NULL"
            ).fetchone()
        return f"revision {revision[0]}, {count[0]:,} transactions"
    except Exception as exc:  # noqa: BLE001 -- reporting beats raising here
        return f"could not read it: {type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore the local database from the NAS.")
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument(
        "--discard-local",
        action="store_true",
        help="Proceed even if this machine has unpushed work. The current database is kept "
             "as budget.discarded-<timestamp>.db rather than deleted. Export the local-only "
             "transactions from the Sync page first.",
    )
    args = parser.parse_args(argv)

    print(f"Local database  {args.db}")
    print(f"NAS master      {config.NAS_DIR}")
    print()

    nas = sync.read_nas()
    if not nas.reachable:
        print("The NAS is not reachable. Connect to the network and try again.")
        return 1
    if not nas.has_master:
        print("There is no master on the NAS to restore from.")
        return 1

    print(f"Master on the NAS: revision {nas.revision}, last pushed by {nas.machine}.")

    if args.db.exists() and args.db.stat().st_size:
        print(f"A local database is already here: {summarise(args.db)}")
        print("Restoring replaces it. Anything not pushed would be lost, so pull refuses")
        print("outright if it finds unpushed work.")
        print()

    result = sync.pull(args.db, discard_local=args.discard_local)
    print(result.message)
    for note in result.detail:
        print(f"  {note}")

    if result.ok:
        print(f"\nRestored: {summarise(args.db)}")
        print("Launch the dashboard with budget.bat.")
        return 0

    print("\nNothing was changed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
