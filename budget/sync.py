"""Cross-machine sync (DESIGN.md 6.3).

The database always lives on a local disk and the NAS only ever holds pushed copies.
SQLite's locking is not reliable over SMB, so nothing ever opens the master in place --
it is copied down, worked on locally, and copied back.

Three guards make that safe. A **lock** records who is working; a **revision counter** records
what they started from; a **schema version** records what shape the file is. The revision
check is the one that matters most: without it, a lock slipped or force-taken loses a
machine's work silently, whereas with it the same slip becomes a refusal you can reconcile.

The schema version is a separate axis on purpose, and migrations deliberately do *not* touch
the revision -- see the header of budget/schema.py for why bumping it would manufacture
unresolvable conflicts. The counter answers "who has edits the other has not seen"; the
version answers "can this code read that file at all". Two machines at the same revision on
different schemas is a real state, and it used to report as a plain "In sync".

Pushes are staged -- snapshot, verify, upload beside the master, verify again, then promote
-- so a dropped connection can never leave a truncated master. The invariant throughout is
that `pushed_revision` only advances after a push has verified end to end.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from budget import config
from budget.db import make_engine, make_session_factory
from budget.models import DbMeta, Txn
from budget.schema import SCHEMA_VERSION, stored_version

MASTER = "budget.db"
BACKUP = "budget.db.bak"
INCOMING = "budget.db.incoming"
META = "budget.meta.json"
LOCK = "budget.lock"

ONLINE, OFFLINE, CONFLICT = "online", "offline", "conflict"


def _discard(path: Path) -> None:
    """Remove a staging file and the WAL sidecars any read of it will have created.

    Opening a WAL database maps a shared-memory index, so even a read-only probe leaves an
    empty `-wal` and `-shm` beside it. Unlinking only the staging file leaves those orphaned
    in the same folder as the live database -- and a WAL that outlives what it belongs to is
    how this project twice met 'database disk image is malformed'.
    """
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def machine_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or "unknown"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------------- state


@dataclass
class NasState:
    reachable: bool = False
    has_master: bool = False
    revision: int = 0
    machine: str | None = None
    updated_at: str | None = None
    sha256: str | None = None
    # 0 means the master makes no claim -- a sidecar written before the version travelled, or
    # a database that predates stamping. Never read as "older than version 1".
    schema_version: int = 0


@dataclass
class Lock:
    machine: str
    mode: str
    taken_at: str
    expected_return: str | None = None

    @property
    def mine(self) -> bool:
        return self.machine == machine_name()

    @property
    def overdue(self) -> bool:
        if not self.expected_return:
            # No stated return: treat a day as the point at which it looks abandoned.
            taken = dt.datetime.fromisoformat(self.taken_at)
            return (dt.datetime.now() - taken).days >= 1
        return dt.date.fromisoformat(self.expected_return) < dt.date.today()


@dataclass
class LocalState:
    revision: int = 0
    base_revision: int = 0
    pushed_revision: int = 0
    mode: str = ONLINE

    @property
    def dirty(self) -> bool:
        return self.revision > self.pushed_revision

    @property
    def pending(self) -> int:
        return max(0, self.revision - self.pushed_revision)


@dataclass
class Status:
    local: LocalState
    nas: NasState
    lock: Lock | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        """The master is at a different revision from the one this machine started from."""
        return self.nas.has_master and self.nas.revision != self.local.base_revision

    @property
    def behind(self) -> bool:
        """The master moved and there is nothing here that is not already on it.

        The ordinary case after the other machine has done a day's work: catching up is a
        pull, and it loses nothing. Kept apart from `conflict` because calling it one made an
        everyday catch-up look like a data-loss risk -- red banner, reconciliation
        instructions, export-and-reimport -- for a machine with nothing to reconcile.
        """
        return self.moved and not self.local.dirty

    @property
    def conflict(self) -> bool:
        """The master moved *and* this machine has unpushed work.

        Only this needs reconciling: both sides have changes, so one of them has to be
        replayed onto the other rather than either simply adopting the other.
        """
        return self.moved and self.local.dirty

    @property
    def blocked_by(self) -> Lock | None:
        return self.lock if self.lock and not self.lock.mine else None

    @property
    def stale_code(self) -> bool:
        """The master was pushed by code newer than this machine's.

        Neither direction of sync is safe: pulling hands this code data it cannot read, and
        pushing puts an older structure over a newer master. Both are refused, and the remedy
        is the same one -- update this machine.

        Only ever true of a *newer* master. A master older than this code is the ordinary
        case after a code update, and needs no comment: pulling it migrates it forward.
        """
        return self.nas.schema_version > SCHEMA_VERSION

    @property
    def label(self) -> str:
        if not self.nas.reachable:
            return f"{self.local.pending} pending · NAS unreachable" if self.local.dirty \
                else "NAS unreachable"
        if self.stale_code:
            # Ahead of the conflict check deliberately: this blocks the remedy for a
            # conflict as well as the conflict itself, so it is the thing to say first.
            return (
                f"Update needed — master needs schema {self.nas.schema_version}, "
                f"this code has {SCHEMA_VERSION}"
            )
        if self.conflict:
            return f"Conflict — {self.local.pending} change(s) here, master at rev " \
                   f"{self.nas.revision}"
        if self.behind:
            return f"Behind — master at rev {self.nas.revision}, pull to catch up"
        if self.local.mode == OFFLINE:
            return f"Offline · {self.local.pending} change(s) since checkout"
        if self.local.dirty:
            return f"{self.local.pending} change(s) pending"
        return f"In sync · rev {self.local.revision}"

    @property
    def tone(self) -> str:
        if self.conflict or self.stale_code:
            return "error"
        if (
            not self.nas.reachable
            or self.behind
            or self.local.dirty
            or self.local.mode == OFFLINE
        ):
            return "warning"
        return "ok"


def nas_dir() -> Path:
    return config.NAS_DIR


def read_nas() -> NasState:
    """Reads the sidecar rather than the database.

    Opening the master over SMB just to read its revision is exactly what this design
    avoids; a small JSON file is safe to read over a network share.
    """
    directory = nas_dir()
    try:
        # Reachability is about the share, not our folder: on first use the folder does not
        # exist yet and is ours to create, so testing it directly would make the very first
        # push impossible.
        reachable = directory.exists() or directory.parent.exists()
    except OSError:
        return NasState(reachable=False)
    if not reachable:
        return NasState(reachable=False)

    state = NasState(reachable=True, has_master=(directory / MASTER).exists())
    meta_path = directory / META
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            state.revision = int(meta.get("revision", 0))
            state.machine = meta.get("machine")
            state.updated_at = meta.get("updated_at")
            state.sha256 = meta.get("sha256")
            state.schema_version = int(meta.get("schema_version", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            pass  # a damaged sidecar must not stop the app from starting
    return state


def read_lock() -> Lock | None:
    path = nas_dir() / LOCK
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Lock(
            machine=data["machine"],
            mode=data.get("mode", ONLINE),
            taken_at=data["taken_at"],
            expected_return=data.get("expected_return"),
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def write_lock(mode: str = ONLINE, expected_return: dt.date | None = None) -> Lock:
    lock = Lock(
        machine=machine_name(),
        mode=mode,
        taken_at=dt.datetime.now().isoformat(timespec="seconds"),
        expected_return=expected_return.isoformat() if expected_return else None,
    )
    nas_dir().mkdir(parents=True, exist_ok=True)
    (nas_dir() / LOCK).write_text(
        json.dumps(lock.__dict__, indent=2), encoding="utf-8"
    )
    return lock


def release_lock() -> None:
    path = nas_dir() / LOCK
    if path.exists():
        path.unlink()


def read_local(session: Session) -> LocalState:
    meta = session.get(DbMeta, 1)
    if meta is None:
        return LocalState()
    return LocalState(
        revision=meta.revision,
        base_revision=meta.base_revision,
        pushed_revision=meta.pushed_revision,
        mode=meta.mode,
    )


def status(session: Session) -> Status:
    return Status(local=read_local(session), nas=read_nas(), lock=read_lock())


# ------------------------------------------------------------------------------ push


@dataclass
class Result:
    ok: bool
    message: str
    detail: list[str] = field(default_factory=list)


def _snapshot(db_path: Path, target: Path) -> None:
    """VACUUM INTO rather than a file copy: transactionally consistent even mid-write,
    where copying a live SQLite file can capture a torn state."""
    if target.exists():
        target.unlink()
    engine = make_engine(db_path)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        engine.dispose()


def _integrity_ok(path: Path) -> bool:
    engine = make_engine(path)
    try:
        with engine.connect() as conn:
            return conn.exec_driver_sql("PRAGMA integrity_check").scalar() == "ok"
    finally:
        engine.dispose()


def _write_meta(revision: int, digest: str, schema_version: int) -> None:
    (nas_dir() / META).write_text(
        json.dumps(
            {
                "revision": revision,
                "machine": machine_name(),
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                "sha256": digest,
                # The version of the file being promoted, read from it rather than taken from
                # this process's SCHEMA_VERSION: the sidecar describes the master, and saying
                # what the code happened to know would be a claim about the wrong thing.
                "schema_version": schema_version,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def push(session: Session, db_path: Path | None = None, force: bool = False) -> Result:
    """Staged push. Every failure path leaves pushed_revision untouched, so the next
    trigger retries rather than silently believing itself synced."""
    db_path = db_path or config.DB_PATH
    local = read_local(session)
    nas = read_nas()

    if not local.dirty and nas.has_master and not force:
        # "Up to date" and "nothing to send" are not the same thing. A machine that is
        # thirteen revisions behind has nothing to push and is emphatically not up to date;
        # saying so sent you looking for the problem in the wrong place.
        if nas.revision != local.base_revision:
            return Result(
                True,
                f"Nothing to push — but the master is at revision {nas.revision} and this "
                f"machine is at {local.base_revision}. Pull to catch up.",
            )
        return Result(True, "Already up to date — nothing to push.")
    if not nas.reachable:
        return Result(False, "NAS not reachable. Changes stay pending and will retry.")

    # Before the lock and before any snapshot work: promoting an older structure over a newer
    # master is a downgrade, and unlike a revision conflict it cannot be reconciled afterwards
    # -- there is no migration that runs backwards. Forced only, and forcing it is a decision
    # to lose whatever the newer schema was carrying.
    if nas.schema_version > SCHEMA_VERSION and not force:
        return Result(
            False,
            f"The master needs schema version {nas.schema_version} and this code has "
            f"{SCHEMA_VERSION}.",
            [
                "Another machine is running newer code. Update this one and try again — "
                "pushing would put an older structure over a newer master."
            ],
        )

    lock = read_lock()
    if lock and not lock.mine and not force:
        return Result(
            False,
            f"Locked by {lock.machine} since {lock.taken_at}.",
            ["Use 'force take' only if that machine is not mid-edit."],
        )

    # The check that makes a lost lock recoverable rather than silent. It fires whether or
    # not there is local work: pushing from a stale base overwrites the other machine either
    # way. What differs is the remedy, so say which one applies.
    if nas.has_master and nas.revision != local.base_revision and not force:
        remedy = (
            "Another machine has pushed since. Pull first, then re-enter these changes."
            if local.dirty
            else "Another machine has pushed since. Pull to catch up — nothing here is "
                 "unpushed, so nothing would be lost."
        )
        return Result(
            False,
            f"Master is at revision {nas.revision}, this machine started from "
            f"{local.base_revision}.",
            [remedy],
        )

    directory = nas_dir()
    directory.mkdir(parents=True, exist_ok=True)
    staging = db_path.with_name("budget.push.tmp")

    detail = []
    try:
        _snapshot(db_path, staging)
        if not _integrity_ok(staging):
            return Result(False, "Snapshot failed its integrity check. Nothing was sent.")
        digest = _sha256(staging)
        detail.append(f"snapshot {staging.stat().st_size:,} bytes, sha256 {digest[:12]}…")

        # Re-read immediately before uploading: the window between the check above and here
        # is small, but a second machine could have pushed inside it.
        if not force:
            recheck = read_nas()
            if recheck.has_master and recheck.revision != local.base_revision:
                return Result(
                    False,
                    f"Conflict: master moved to revision {recheck.revision} while preparing.",
                )

        incoming = directory / INCOMING
        shutil.copy2(staging, incoming)
        if _sha256(incoming) != digest:
            incoming.unlink(missing_ok=True)
            return Result(False, "Upload did not verify. The master is untouched.")
        detail.append("upload verified")

        master, backup = directory / MASTER, directory / BACKUP
        if master.exists():
            backup.unlink(missing_ok=True)
            master.replace(backup)
        incoming.replace(master)
        detail.append("promoted; previous master kept as budget.db.bak")

        _write_meta(local.revision, digest, stored_version(staging))

        meta = session.get(DbMeta, 1)
        meta.pushed_revision = local.revision
        meta.base_revision = local.revision
        meta.machine = machine_name()
        meta.updated_at = dt.datetime.now()
        session.flush()

        if meta.mode == ONLINE:
            release_lock()

        shutil.copy2(staging, config.BASE_DB_PATH)  # new common ancestor
        return Result(True, f"Pushed revision {local.revision}.", detail)
    except OSError as exc:
        return Result(False, f"Push failed: {exc}", detail)
    finally:
        _discard(staging)


# ------------------------------------------------------------------------------ pull


def _read_counters(db_path: Path) -> LocalState:
    """The sync counters, read without SQLAlchemy and without writing anything.

    Deliberately plain sqlite3, opened read-only, for the one caller that is about to *move*
    this file. Two reasons an Engine is wrong here:

    * It closes on the pool's schedule, not ours. A connection that fails during setup can
      outlive dispose() until garbage collection, and the file stays locked -- which on
      Windows turns the subsequent rename into WinError 32.
    * make_engine runs `PRAGMA journal_mode=WAL` on connect, which *writes to the header*.
      Probing a database in order to decide whether to replace it should not modify it, and
      doing so to one whose WAL state is already inconsistent is a way to make things worse.

    Raises sqlite3.DatabaseError if the file is not a readable database, which is the
    caller's signal to preserve it rather than overwrite it.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT revision, base_revision, pushed_revision, mode FROM db_meta WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # No db_meta table at all: a file created but never migrated.
        return LocalState()
    finally:
        conn.close()
    if row is None:
        return LocalState()
    return LocalState(
        revision=row[0], base_revision=row[1], pushed_revision=row[2], mode=row[3]
    )


def pull(db_path: Path | None = None, discard_local: bool = False) -> Result:
    """Replaces the local database with the master and records it as the new ancestor.

    Refuses if there are unpushed changes -- overwriting them is exactly the data loss this
    module exists to prevent.

    `discard_local` is the way *out* of that refusal, and it exists because without it the
    documented remedy for a conflict could not be carried out. 6.3.4 says to export the
    local-only transactions, pull the fresh master, then re-import them -- but every route to
    that middle step went through this function, which refused precisely because the machine
    had the work the export had just captured. The Sync page offered the instruction and no
    button that could follow it.

    So the refusal stays the default and becomes deliberate rather than absolute. Nothing is
    deleted either way: the local database is *moved aside*, with its WAL, to
    `budget.discarded-<timestamp>.db`. Renamed rather than copied so nothing has to be read
    out of a file that may still be mid-write, and kept rather than removed because "discard"
    should mean "stop using", not "destroy" -- the export only covers transactions, and a
    setting or a target changed on this machine would otherwise be gone with no record of it.
    """
    db_path = db_path or config.DB_PATH
    nas = read_nas()
    if not nas.reachable:
        return Result(False, "NAS not reachable.")
    if not nas.has_master:
        return Result(False, "No master on the NAS yet. Push from this machine first.")

    # A machine with no database at all is the ordinary case for a first pull -- setting up
    # the desktop, or recovering after the local copy is lost. read_local copes with a
    # missing db_meta *row*, but querying it at all fails when the table has never been
    # created, so the absence has to be established before opening anything.
    salvaged: Path | None = None
    if not db_path.exists() or db_path.stat().st_size == 0:
        local = LocalState()
    else:
        try:
            local = _read_counters(db_path)
        except sqlite3.DatabaseError as exc:
            # Unreadable: no schema (an interrupted earlier attempt), or damaged. Either way
            # there is no way to know whether it held unpushed work, so it is moved aside
            # rather than overwritten. Replacing a file we could not read would destroy the
            # only evidence of what was in it -- and the reason for pulling is usually that
            # something has already gone wrong once.
            salvaged = db_path.with_name(
                f"{db_path.stem}.unreadable-"
                f"{dt.datetime.now():%Y%m%d-%H%M%S}{db_path.suffix}"
            )
            try:
                db_path.replace(salvaged)
            except OSError as move_failed:
                return Result(
                    False,
                    f"The local database cannot be read ({exc}) and could not be moved "
                    "aside. Nothing was changed.",
                    [f"{move_failed}", "Close every dashboard window and try again."],
                )
            for suffix in ("-wal", "-shm", "-journal"):
                db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
            local = LocalState()

    if local.dirty and not discard_local:
        return Result(
            False,
            f"{local.pending} unpushed change(s) here. Push or reconcile before pulling.",
            [
                "Export the local-only transactions from the Sync page first. Then pull with "
                "'discard local changes', which keeps the current database as "
                "budget.discarded-<timestamp>.db rather than deleting it."
            ],
        )

    master = nas_dir() / MASTER
    # Opening an engine used to create this as a side effect; a first pull skips that, so
    # the directory has to be made explicitly or staging has nowhere to land.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    staging = db_path.with_name("budget.pull.tmp")
    try:
        shutil.copy2(master, staging)
        if nas.sha256 and _sha256(staging) != nas.sha256:
            return Result(False, "Downloaded copy does not match the recorded checksum.")
        if not _integrity_ok(staging):
            return Result(False, "Downloaded copy failed its integrity check.")

        # Read from the downloaded file rather than trusting the sidecar: the sidecar is what
        # the status badge goes on, but replacing this machine's database is worth checking
        # against the thing itself. Refusing here costs a code update; accepting would run
        # newer data through older readers, which does not error, it just quietly disagrees.
        master_version = stored_version(staging)
        if master_version > SCHEMA_VERSION:
            return Result(
                False,
                f"The master needs schema version {master_version} and this code has "
                f"{SCHEMA_VERSION}. Nothing was changed.",
                [
                    "Another machine has pushed from newer code. Update this one and pull "
                    "again — migrations only ever move forward, so there is no way to read "
                    "it here."
                ],
            )

        # Only now, with a verified master in hand. Moving the local database aside earlier
        # would mean a download that failed its checksum left this machine with no database
        # at all -- the pull would have destroyed the thing it could not replace.
        discarded: Path | None = None
        if local.dirty and discard_local:
            discarded = db_path.with_name(
                f"{db_path.stem}.discarded-"
                f"{dt.datetime.now():%Y%m%d-%H%M%S}{db_path.suffix}"
            )
            try:
                for suffix in ("-wal", "-shm"):
                    sidecar = db_path.with_name(db_path.name + suffix)
                    if sidecar.exists():
                        sidecar.replace(
                            discarded.with_name(discarded.name + suffix)
                        )
                db_path.replace(discarded)
            except OSError as exc:
                return Result(
                    False,
                    "The local database is still open, so it could not be set aside. "
                    "Nothing was changed.",
                    [f"{exc}", "Close every dashboard window and try again."],
                )

        # Anything still holding the database has to have let go by now. On Windows this
        # unlink raises rather than succeeding, which is the safe failure: the local file is
        # untouched and the caller is told to close things. On POSIX it would succeed and
        # leave the open connection pointing at a deleted inode, so the caller's obligation
        # to release first is real on every platform -- see ui.close_connections.
        try:
            for suffix in ("-wal", "-shm"):
                db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
        except OSError as exc:
            return Result(
                False,
                "The local database is still open, so it cannot be replaced. Nothing was "
                "changed.",
                [
                    f"{exc}",
                    "Close every dashboard window and try again. If this came from a script, "
                    "call ui.close_connections() (or dispose the engine) first.",
                ],
            )
        staging.replace(db_path)
        shutil.copy2(db_path, config.BASE_DB_PATH)

        fresh = make_engine(db_path)
        try:
            with make_session_factory(fresh)() as session, session.begin():
                meta = session.get(DbMeta, 1)
                meta.base_revision = meta.revision
                meta.pushed_revision = meta.revision
                meta.mode = ONLINE
                revision = meta.revision
        finally:
            # Leaving this engine pooled would hand the caller a stale handle on a file that
            # is about to be opened again by the app's own cached engine.
            fresh.dispose()

        detail = []
        if salvaged is not None:
            detail.append(
                f"the previous local database could not be read and was kept as "
                f"{salvaged.name}"
            )
        if discarded is not None:
            detail.append(
                f"the local changes were set aside, not deleted — the previous database is "
                f"{discarded.name}, beside this one"
            )
        return Result(True, f"Pulled revision {revision} from the NAS.", detail)
    except OSError as exc:
        return Result(False, f"Pull failed: {exc}")
    finally:
        _discard(staging)


# ------------------------------------------------------------ deliberate offline mode


def checkout(session: Session, expected_return: dt.date | None = None) -> Result:
    """Take a long-lived lease. Requires being in sync -- checking out dirty would make the
    ancestor snapshot meaningless."""
    local = read_local(session)
    nas = read_nas()

    if not nas.reachable:
        return Result(False, "NAS not reachable, so a lease cannot be recorded.")
    if nas.schema_version > SCHEMA_VERSION:
        # A lease is a promise to check in later, and check-in pushes. Granting one this
        # machine could not honour would mean a week of offline edits stranded behind a
        # refusal that was already knowable now.
        return Result(
            False,
            f"The master needs schema version {nas.schema_version} and this code has "
            f"{SCHEMA_VERSION}. Update this machine before checking out.",
        )
    if local.dirty:
        return Result(False, f"{local.pending} unpushed change(s). Push before checking out.")
    if nas.has_master and nas.revision != local.base_revision:
        return Result(False, "Master has moved. Pull before checking out.")

    lock = read_lock()
    if lock and not lock.mine:
        return Result(False, f"Locked by {lock.machine} since {lock.taken_at}.")

    write_lock(mode=OFFLINE, expected_return=expected_return)
    shutil.copy2(config.DB_PATH, config.BASE_DB_PATH)

    meta = session.get(DbMeta, 1)
    meta.mode = OFFLINE
    meta.machine = machine_name()
    session.flush()

    until = f" until {expected_return}" if expected_return else ""
    return Result(True, f"Checked out for offline use{until}.")


def checkin(session: Session) -> Result:
    result = push(session)
    if not result.ok:
        return result
    meta = session.get(DbMeta, 1)
    meta.mode = ONLINE
    session.flush()
    release_lock()
    return Result(True, "Checked in. " + result.message, result.detail)


def force_take(session: Session) -> Result:
    """Take a lock held by another machine. Explicit and logged: that machine's next push
    will hit the revision check and be routed to reconciliation rather than overwriting."""
    lock = read_lock()
    if lock and lock.mine:
        return Result(True, "Lock is already held by this machine.")
    previous = lock.machine if lock else "nobody"
    write_lock(mode=ONLINE)
    return Result(True, f"Lock taken from {previous}.")


# --------------------------------------------------------------------- reconciliation


def local_only(session: Session, base_path: Path | None = None) -> list[Txn]:
    """Transactions present here but not in the ancestor snapshot.

    A three-way merge needs the common ancestor: without it there is no way to tell
    "this machine added a row" from "the other machine deleted it".
    """
    base_path = base_path or config.BASE_DB_PATH
    if not base_path.exists():
        return []

    base_engine = make_engine(base_path)
    try:
        with make_session_factory(base_engine)() as base:
            known = {uid for uid in base.scalars(select(Txn.uid))}
    finally:
        base_engine.dispose()

    return [
        t
        for t in session.scalars(select(Txn).where(Txn.deleted_at.is_(None)))
        if t.uid not in known
    ]


def local_only_frame(session: Session, base_path: Path | None = None):
    """The local-only transactions in the shape the Import page accepts.

    This is the manual reconciliation route of DESIGN.md 6.3.5, and the reason it needs
    almost no new code: export, pull the fresh master, then feed the file back through the
    importer, which already does validation, preview, batch tagging and undo.
    """
    import pandas as pd

    from budget.models import Account, Category, Classification

    accounts = {a.id: a.name for a in session.scalars(select(Account))}
    categories = {c.id: c.name for c in session.scalars(select(Category))}
    classifications = {c.id: c.name for c in session.scalars(select(Classification))}

    return pd.DataFrame(
        [
            {
                "Date": t.txn_date,
                "Type": t.type,
                "Amount": float(t.amount),
                "Account From": accounts.get(t.account_from_id),
                "Account To": accounts.get(t.account_to_id),
                "Category": categories.get(t.category_id),
                "Purchase type": classifications.get(t.classification_id),
                "Comment": t.comment,
                "Category comment": t.category_comment,
            }
            for t in local_only(session, base_path)
        ],
        columns=[
            "Date", "Type", "Amount", "Account From", "Account To",
            "Category", "Purchase type", "Comment", "Category comment",
        ],
    )
