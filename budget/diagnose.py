"""Report what the application sees when it looks for the database.

Read-only: it opens nothing for writing and changes nothing. The point is to answer "why
does this machine say there is no database when the file is plainly there" with facts rather
than with theories -- the resolved path, the environment behind it, who is asking, what is
actually in the folder, and the exact error if the file cannot be read.

Run with:  python -m budget.diagnose      (or double-click diagnose.bat)
"""

from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import os
import platform
import sqlite3
import sys
from pathlib import Path

from budget import config


def line(label: str, value) -> None:
    print(f"  {label:<20} {value}")


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def describe(path: Path) -> str:
    """Stat a path without the usual exists() shrug: report the failure, do not hide it."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "MISSING"
    except OSError as exc:
        return f"UNREADABLE -- {type(exc).__name__}: {exc}"
    when = dt.datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y %H:%M:%S")
    return f"{stat.st_size:,} bytes, modified {when}"


def main(argv: list[str] | None = None) -> int:
    heading("Who is asking")
    line("machine", platform.node())
    line("user", getpass.getuser())
    line("python", sys.executable)
    line("version", platform.python_version())
    line("working dir", Path.cwd())

    heading("Where it looks")
    line("LOCALAPPDATA", os.environ.get("LOCALAPPDATA", "(not set)"))
    line("BUDGET_DB_PATH", os.environ.get("BUDGET_DB_PATH", "(not set — using default)"))
    line("resolved to", config.DB_PATH)
    line("NAS master", config.NAS_DIR)
    line("workbook", config.WORKBOOK_PATH)

    heading("What is there")
    folder = config.DB_PATH.parent
    try:
        exists = folder.is_dir()
    except OSError as exc:
        exists = False
        line("folder", f"UNREADABLE -- {type(exc).__name__}: {exc}")
    else:
        line("folder", f"{folder}  ({'exists' if exists else 'MISSING'})")

    if exists:
        try:
            entries = sorted(folder.iterdir())
        except OSError as exc:
            print(f"    cannot list it: {type(exc).__name__}: {exc}")
            entries = []
        if not entries:
            print("    (empty)")
        for entry in entries:
            print(f"    {entry.name:<26} {describe(entry)}")

    heading("The database itself")
    line("path", config.DB_PATH)
    line("status", describe(config.DB_PATH))

    # The file's own identity, so two processes claiming different things about one path can
    # be compared directly rather than argued about. Same digest and same inode means the
    # same bytes; different means they are not looking at what they both call budget.db.
    try:
        raw = config.DB_PATH.read_bytes()
        stat = config.DB_PATH.stat()
        line("sha256", hashlib.sha256(raw).hexdigest()[:32])
        line("mtime_ns", stat.st_mtime_ns)
        line("inode / index", f"{stat.st_ino} on device {stat.st_dev}")
    except OSError as exc:
        line("identity", f"unreadable -- {type(exc).__name__}: {exc}")

    readable = False
    try:
        with sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            line("integrity", integrity)
            revision = conn.execute("SELECT revision FROM db_meta WHERE id = 1").fetchone()
            line("revision", revision[0] if revision else "(none)")
            count = conn.execute(
                "SELECT count(*) FROM txn WHERE deleted_at IS NULL"
            ).fetchone()
            line("transactions", f"{count[0]:,}")

            version = conn.execute(
                "SELECT value FROM setting WHERE key = 'schema_version'"
            ).fetchone()
            line("schema version", version[0] if version else "(none)")

            # What is actually in the tables the dashboard reports on. A page that says it
            # has nothing, against a file that has plenty, is a different fault from a file
            # that really is empty -- and the two are indistinguishable from the page alone.
            for table in (
                "savings_plan",
                "savings_adjustment",
                "savings_target",
                "account",
                "payslip",
            ):
                try:
                    rows = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    line(f"  {table}", f"{rows:,} row(s)")
                except sqlite3.Error as exc:
                    line(f"  {table}", f"unavailable -- {exc}")

            readable = True
    except Exception as exc:  # noqa: BLE001 -- reporting the failure is the whole point
        line("could not open", f"{type(exc).__name__}: {exc}")

    heading("The NAS master")
    try:
        reachable = config.NAS_DIR.parent.is_dir()
    except OSError as exc:
        reachable = False
        line("share", f"UNREADABLE -- {type(exc).__name__}: {exc}")
    line("reachable", reachable)
    if reachable:
        line("master", describe(config.NAS_DIR / "budget.db"))
        meta = config.NAS_DIR / "budget.meta.json"
        if meta.exists():
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                line("revision", data.get("revision"))
                line("last pushed by", data.get("machine"))
                line("last pushed at", data.get("updated_at"))
                master = config.NAS_DIR / "budget.db"
                if master.exists():
                    digest = hashlib.sha256(master.read_bytes()).hexdigest()
                    line("sha256 matches", digest == data.get("sha256"))
            except Exception as exc:  # noqa: BLE001
                line("meta unreadable", f"{type(exc).__name__}: {exc}")

    heading("Verdict")
    if readable:
        print("  The database is present and readable from this process.")
        print("  If the dashboard still says otherwise, something is holding the file only")
        print("  while it runs -- a virus scanner, or another copy of the app. Close every")
        print("  dashboard window and try once more.")
    elif config.DB_PATH.parent.is_dir() and any(
        p.name == "budget.db" for p in config.DB_PATH.parent.iterdir()
    ):
        print("  budget.db is in the folder but this process cannot read it. That is a")
        print("  permissions or locking problem, not a missing database -- do NOT rebuild")
        print("  from the workbook.")
    else:
        print("  No readable database. Pull from the NAS on the Sync page, or restore one")
        print("  of the backups listed above by renaming it to budget.db.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
