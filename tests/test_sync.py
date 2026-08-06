"""Cross-machine sync.

The properties worth pinning are the ones whose failure is silent: a push that believes
itself successful when it was not, a conflict that goes undetected, or a pull that
overwrites unpushed work.
"""

import datetime as dt
import json
import sqlite3
from decimal import Decimal

import pytest

from budget import config, service, sync
from budget.db import make_engine, make_session_factory
from budget.models import DbMeta, Txn
from budget.schema import SCHEMA_VERSION
from budget.validation import Candidate


@pytest.fixture
def nas(tmp_path, monkeypatch):
    directory = tmp_path / "nas"
    directory.mkdir()
    monkeypatch.setattr(config, "NAS_DIR", directory)
    return directory


@pytest.fixture
def unreachable_nas(tmp_path, monkeypatch):
    """A drive that is not mounted: neither the folder nor the share above it exists.
    (Pointing only one level down would now read as reachable, since the share being
    present is exactly what lets a first push create the folder.)"""
    monkeypatch.setattr(config, "NAS_DIR", tmp_path / "unmounted-share" / "budget_db")
    return config.NAS_DIR


@pytest.fixture
def local(session, tmp_path, monkeypatch):
    """The session fixture's database, wired up as this machine's live database."""
    db_path = tmp_path / "write.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BASE_DB_PATH", tmp_path / "budget.base.db")
    return db_path


def add_one(session, amount="10.00"):
    return service.add_transaction(
        session,
        Candidate(
            txn_date=dt.date(2026, 6, 15), type="Debit", amount=Decimal(amount),
            account_from="HSBC", category="Food", classification="Food",
        ),
    )


class TestStatus:
    def test_unreachable_nas_is_not_an_error_state(self, session, local, unreachable_nas):
        state = sync.status(session)
        assert not state.nas.reachable
        assert state.tone == "warning"  # not 'error' -- this is normal away from home

    def test_a_share_that_exists_is_reachable_before_our_folder_does(
        self, session, local, tmp_path, monkeypatch
    ):
        """First run: the share is mounted but budget_db/ has not been created yet.
        Testing the folder itself would make the very first push impossible."""
        monkeypatch.setattr(config, "NAS_DIR", tmp_path / "share_exists" / "budget_db")
        (tmp_path / "share_exists").mkdir()

        state = sync.read_nas()
        assert state.reachable
        assert not state.has_master

    def test_first_push_creates_the_folder(self, session, local, tmp_path, monkeypatch):
        target = tmp_path / "share_exists" / "budget_db"
        monkeypatch.setattr(config, "NAS_DIR", target)
        (tmp_path / "share_exists").mkdir()

        add_one(session)
        session.commit()
        result = sync.push(session, db_path=local)
        session.commit()

        assert result.ok, result.message
        assert (target / sync.MASTER).exists()

    def test_clean_database_reports_in_sync(self, session, local, nas):
        meta = session.get(DbMeta, 1)
        meta.pushed_revision = meta.revision
        meta.base_revision = meta.revision
        session.commit()
        assert not sync.status(session).local.dirty

    def test_a_write_makes_it_dirty(self, session, local, nas):
        meta = session.get(DbMeta, 1)
        meta.pushed_revision = meta.revision
        session.commit()
        add_one(session)
        session.commit()
        assert sync.status(session).local.dirty


class TestPush:
    def test_first_push_creates_the_master_and_sidecar(self, session, local, nas):
        add_one(session)
        session.commit()

        result = sync.push(session, db_path=local)
        session.commit()

        assert result.ok, result.message
        assert (nas / sync.MASTER).exists()
        meta = json.loads((nas / sync.META).read_text())
        assert meta["revision"] == session.get(DbMeta, 1).revision
        assert meta["sha256"]

    def test_push_advances_pushed_revision_so_it_is_no_longer_dirty(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        assert not sync.status(session).local.dirty

    def test_unreachable_nas_leaves_it_dirty_for_the_next_attempt(
        self, session, local, unreachable_nas
    ):
        add_one(session)
        session.commit()

        result = sync.push(session, db_path=local)
        session.commit()

        assert not result.ok
        # The invariant: pushed_revision only advances on a verified push.
        assert sync.status(session).local.dirty

    def test_previous_master_is_kept_as_a_backup(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        assert (nas / sync.BACKUP).exists()

    def test_the_ancestor_snapshot_is_refreshed(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        assert config.BASE_DB_PATH.exists()


class TestConflictDetection:
    """The check that turns a lost lock into a refusal instead of silent data loss."""

    def test_push_refuses_when_the_master_has_moved(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        # Another machine pushes: the sidecar revision advances beyond our base.
        meta = json.loads((nas / sync.META).read_text())
        meta["revision"] += 5
        meta["machine"] = "DESKTOP-OTHER"
        (nas / sync.META).write_text(json.dumps(meta))

        add_one(session)
        session.commit()
        result = sync.push(session, db_path=local)
        session.commit()

        assert not result.ok
        assert "revision" in result.message
        assert sync.status(session).conflict
        assert sync.status(session).local.dirty  # still ours to reconcile

    def test_force_overrides_the_conflict_check(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        meta = json.loads((nas / sync.META).read_text())
        meta["revision"] += 5
        (nas / sync.META).write_text(json.dumps(meta))

        add_one(session)
        session.commit()
        assert sync.push(session, db_path=local, force=True).ok


class TestBehindVersusConflict:
    """Being behind is not a conflict.

    The master having moved while this machine has nothing unpushed is the ordinary state
    after the other machine has done a day's work. Treating it as a conflict put a red
    banner and a set of export-and-reimport instructions in front of a machine with nothing
    to reconcile -- and buried the one button that fixes it.
    """

    def move_the_master(self, nas, by=5):
        meta = json.loads((nas / sync.META).read_text())
        meta["revision"] += by
        meta["machine"] = "DESKTOP-OTHER"
        (nas / sync.META).write_text(json.dumps(meta))

    def test_a_clean_machine_is_behind_not_conflicted(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        self.move_the_master(nas)

        state = sync.status(session)
        assert state.moved
        assert state.behind
        assert not state.conflict
        assert state.tone == "warning"
        assert "Behind" in state.label

    def test_a_dirty_machine_is_conflicted_not_merely_behind(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        self.move_the_master(nas)
        add_one(session)
        session.commit()

        state = sync.status(session)
        assert state.conflict
        assert not state.behind
        assert state.tone == "error"

    def test_a_clean_machine_behind_the_master_is_not_told_it_is_up_to_date(
        self, session, local, nas
    ):
        """There is genuinely nothing to send, but 'up to date' is a different claim and an
        untrue one -- it sent you looking for the problem in the wrong place."""
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        self.move_the_master(nas)

        result = sync.push(session, db_path=local)
        assert result.ok
        assert "Pull to catch up" in result.message
        assert "Already up to date" not in result.message

    def test_in_sync_and_clean_still_reports_up_to_date(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        assert "Already up to date" in sync.push(session, db_path=local).message

    def test_the_advice_names_the_right_remedy_when_dirty(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        self.move_the_master(nas)
        add_one(session)
        session.commit()

        result = sync.push(session, db_path=local)
        assert not result.ok
        assert any("re-enter these changes" in d for d in result.detail)

    def test_being_behind_does_not_block_a_pull(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        self.move_the_master(nas)

        # The sidecar revision is ahead of the file's, so the checksum still matches and the
        # pull is exactly the catch-up the page offers.
        assert not sync.status(session).local.dirty


class TestInUse:
    """The guard the one-off scripts check before writing.

    It has to detect a *reader*, because an idle dashboard is one and that is the case that
    matters. The original test was `BEGIN EXCLUSIVE`, which cannot: in WAL mode a writer and
    any number of readers coexist by design, so it succeeded with the app open, reported the
    database free, and a script wrote underneath a live connection. The result was a
    malformed sqlite_autoindex_setting_1.
    """

    def database(self, tmp_path):
        import sqlite3

        path = tmp_path / "guard.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        return path

    def test_a_database_nobody_holds_is_free(self, tmp_path):
        from budget.db import in_use

        assert in_use(self.database(tmp_path)) is False

    def test_a_reader_makes_it_busy(self, tmp_path):
        """The case the old guard missed."""
        import sqlite3

        from budget.db import in_use

        path = self.database(tmp_path)
        holder = sqlite3.connect(path)
        holder.execute("SELECT * FROM t").fetchall()  # maps the -shm, as the app does
        try:
            assert in_use(path) is True
        finally:
            holder.close()

    def test_begin_exclusive_would_not_have_noticed(self, tmp_path):
        """Pinned deliberately: this documents why the guard changed, and fails if someone
        decides the simpler spelling was good enough after all."""
        import sqlite3

        path = self.database(tmp_path)
        holder = sqlite3.connect(path)
        holder.execute("SELECT * FROM t").fetchall()
        probe = sqlite3.connect(path, timeout=1)
        try:
            probe.execute("BEGIN EXCLUSIVE")
            probe.execute("ROLLBACK")
            old_guard_says_busy = False
        except sqlite3.OperationalError:
            old_guard_says_busy = True
        finally:
            probe.close()
            holder.close()
        assert old_guard_says_busy is False

    def test_it_leaves_the_file_unlocked(self, tmp_path):
        """Checking must not itself lock the database out for the next caller."""
        import sqlite3

        from budget.db import in_use

        path = self.database(tmp_path)
        assert in_use(path) is False
        after = sqlite3.connect(path)
        try:
            after.execute("INSERT INTO t (id) VALUES (1)")
            after.commit()
        finally:
            after.close()


class TestUnreadableLocalDatabase:
    """A pull is usually reached *because* something has already gone wrong, so the file it
    replaces is evidence. It is moved aside, never overwritten."""

    def prepare(self, session, local):
        """Push, then damage the local file and release every handle on it.

        The release is the point. A pull *replaces* the database rather than writing through
        it, so any pooled connection still holding the old file has to go first -- which in
        the app is ui.close_connections(). Closing a Session is not enough: its connection
        goes back to the pool and the engine keeps the file and its WAL open.
        """
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        engine = session.get_bind()
        session.close()
        engine.dispose()

        with open(local, "r+b") as handle:  # readable as a file, not as a database
            handle.seek(0)
            handle.write(b"not a sqlite file at all")

    def test_a_corrupt_local_database_is_preserved_not_overwritten(
        self, session, local, nas
    ):
        self.prepare(session, local)

        result = sync.pull(db_path=local)
        assert result.ok
        salvaged = list(local.parent.glob("*.unreadable-*.db"))
        assert len(salvaged) == 1
        assert salvaged[0].read_bytes().startswith(b"not a sqlite file")
        assert any("could not be read" in d for d in result.detail)

    def test_the_pull_still_lands_a_working_database(self, session, local, nas):
        self.prepare(session, local)

        assert sync.pull(db_path=local).ok
        engine = make_engine(local)
        try:
            with make_session_factory(engine)() as fresh:
                assert sync.read_local(fresh).revision > 0
        finally:
            engine.dispose()

    def test_a_database_still_held_open_is_refused_not_replaced(
        self, session, local, nas
    ):
        """The failure that matters. Replacing a file underneath a live connection is how a
        healthy database becomes 'malformed database schema: orphan index'."""
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        before = local.read_bytes()

        # session (and its engine) deliberately left open, as a running dashboard would.
        result = sync.pull(db_path=local)

        assert not result.ok
        assert "still open" in result.message
        assert local.read_bytes() == before  # untouched


class TestLocking:
    def test_push_refuses_while_another_machine_holds_the_lock(self, session, local, nas):
        (nas / sync.LOCK).write_text(
            json.dumps(
                {
                    "machine": "DESKTOP-OTHER",
                    "mode": "offline",
                    "taken_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "expected_return": None,
                }
            )
        )
        add_one(session)
        session.commit()

        result = sync.push(session, db_path=local)
        session.commit()

        assert not result.ok
        assert "Locked by DESKTOP-OTHER" in result.message

    def test_force_take_transfers_the_lock(self, session, local, nas):
        (nas / sync.LOCK).write_text(
            json.dumps(
                {"machine": "DESKTOP-OTHER", "mode": "online",
                 "taken_at": dt.datetime.now().isoformat(timespec="seconds")}
            )
        )
        result = sync.force_take(session)
        assert result.ok
        assert sync.read_lock().machine == sync.machine_name()

    def test_an_overdue_lease_is_flagged(self):
        lock = sync.Lock(
            machine="OTHER", mode="offline",
            taken_at=(dt.datetime.now() - dt.timedelta(days=10)).isoformat(),
            expected_return=(dt.date.today() - dt.timedelta(days=3)).isoformat(),
        )
        assert lock.overdue

    def test_a_lease_still_within_its_window_is_not_overdue(self):
        lock = sync.Lock(
            machine="OTHER", mode="offline",
            taken_at=dt.datetime.now().isoformat(),
            expected_return=(dt.date.today() + dt.timedelta(days=3)).isoformat(),
        )
        assert not lock.overdue


class TestOfflineMode:
    def test_checkout_requires_being_in_sync(self, session, local, nas):
        add_one(session)
        session.commit()
        result = sync.checkout(session)
        assert not result.ok
        assert "unpushed" in result.message

    def test_checkout_records_a_lease_and_the_ancestor(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        result = sync.checkout(session, dt.date.today() + dt.timedelta(days=7))
        session.commit()

        assert result.ok
        assert session.get(DbMeta, 1).mode == sync.OFFLINE
        lock = sync.read_lock()
        assert lock.mine and lock.mode == sync.OFFLINE and lock.expected_return
        assert config.BASE_DB_PATH.exists()

    def test_checkin_pushes_and_returns_to_online(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        sync.checkout(session)
        session.commit()

        add_one(session)
        session.commit()
        result = sync.checkin(session)
        session.commit()

        assert result.ok, result.message
        assert session.get(DbMeta, 1).mode == sync.ONLINE
        assert sync.read_lock() is None


class TestPull:
    def test_pull_refuses_to_overwrite_unpushed_work(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        add_one(session)  # unpushed
        session.commit()

        result = sync.pull(db_path=local)
        assert not result.ok
        assert "unpushed" in result.message

    def test_pull_without_a_master_is_refused(self, session, local, nas):
        assert not sync.pull(db_path=local).ok


class TestSchemaVersionCrossesTheWire:
    """Two machines at the same revision on different schemas.

    Migrations deliberately do not bump the revision -- doing so would make two machines that
    each took the same code update look like a mutual conflict neither could reconcile. The
    version travels as its own axis instead, and the failure being pinned here is the silent
    one: a machine reading a master written by code newer than its own, which does not raise,
    it just quietly disagrees about what the columns mean.
    """

    def newer_master(self, nas, local, session, bump=1):
        """Push, then age this code relative to the master by editing what it claims."""
        add_one(session)
        session.commit()
        assert sync.push(session, db_path=local).ok
        session.commit()

        master = nas / sync.MASTER
        connection = sqlite3.connect(master)
        connection.execute(
            "INSERT INTO setting (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION + bump),),
        )
        connection.commit()
        connection.close()

        # The sidecar has to keep describing the file, or the pull fails its checksum test
        # first and this would pass for the wrong reason.
        meta = json.loads((nas / sync.META).read_text())
        meta["schema_version"] = SCHEMA_VERSION + bump
        meta["sha256"] = sync._sha256(master)
        (nas / sync.META).write_text(json.dumps(meta, indent=2))
        return master

    def test_the_sidecar_records_the_version_of_the_file_it_promoted(
        self, session, local, nas
    ):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        meta = json.loads((nas / sync.META).read_text())
        assert meta["schema_version"] == SCHEMA_VERSION

    def test_a_newer_master_is_not_reported_as_in_sync(self, session, local, nas):
        self.newer_master(nas, local, session)
        state = sync.status(session)

        assert state.stale_code
        assert state.tone == "error"
        assert "Update needed" in state.label
        assert "In sync" not in state.label

    def test_push_refuses_to_put_an_older_structure_over_a_newer_master(
        self, session, local, nas
    ):
        self.newer_master(nas, local, session)
        before = (nas / sync.MASTER).read_bytes()

        add_one(session)
        session.commit()
        result = sync.push(session, db_path=local)
        session.commit()

        assert not result.ok
        assert "newer code" in " ".join(result.detail)
        assert (nas / sync.MASTER).read_bytes() == before  # untouched
        assert sync.status(session).local.dirty  # and still queued to retry

    def test_forcing_the_push_is_still_possible(self, session, local, nas):
        self.newer_master(nas, local, session)
        add_one(session)
        session.commit()

        assert sync.push(session, db_path=local, force=True).ok

    def test_pull_refuses_a_master_this_code_cannot_read(self, session, local, nas):
        self.newer_master(nas, local, session)
        before = local.read_bytes()

        result = sync.pull(db_path=local)

        assert not result.ok
        assert "schema version" in result.message
        assert local.read_bytes() == before  # nothing replaced

    def test_pull_reads_the_file_rather_than_trusting_the_sidecar(
        self, session, local, nas
    ):
        """A sidecar can be stale or hand-edited; the file is what gets installed."""
        self.newer_master(nas, local, session)
        meta = json.loads((nas / sync.META).read_text())
        meta["schema_version"] = SCHEMA_VERSION  # lies about the master
        (nas / sync.META).write_text(json.dumps(meta, indent=2))

        assert not sync.pull(db_path=local).ok

    def test_an_older_master_is_the_ordinary_case_and_is_not_blocked(
        self, tmp_path, monkeypatch
    ):
        """After a code update this machine is ahead of the master. Pulling migrates it
        forward, which is the whole point of migrations -- it must not be mistaken for the
        dangerous direction.

        Pulled onto a machine with no database, like TestFirstPull: the session fixture holds
        its own file open, so a pull over it cannot complete on Windows and the happy path
        would never actually be reached.
        """
        nas_dir = tmp_path / "nas"
        nas_dir.mkdir()
        source = tmp_path / "source.db"
        engine = make_engine(source)
        from budget.db import create_all

        create_all(engine)
        with make_session_factory(engine)() as s, s.begin():
            s.add(DbMeta(id=1, revision=4, base_revision=4, pushed_revision=4))
        engine.dispose()

        connection = sqlite3.connect(source)
        connection.execute(
            "UPDATE setting SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION - 1),),
        )
        connection.commit()
        connection.close()

        master = nas_dir / sync.MASTER
        master.write_bytes(source.read_bytes())
        (nas_dir / sync.META).write_text(
            json.dumps(
                {
                    "revision": 4,
                    "machine": "OTHER",
                    "sha256": sync._sha256(master),
                    "schema_version": SCHEMA_VERSION - 1,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "NAS_DIR", nas_dir)
        monkeypatch.setattr(config, "DB_PATH", tmp_path / "local" / "budget.db")
        monkeypatch.setattr(config, "BASE_DB_PATH", tmp_path / "local" / "budget.base.db")

        assert sync.pull().ok

    def test_checkout_is_refused_rather_than_stranding_offline_edits(
        self, session, local, nas
    ):
        """A lease is a promise to check in, and check-in pushes. Granting one this machine
        could not honour buries a knowable refusal under a week of offline work."""
        self.newer_master(nas, local, session)
        session.get(DbMeta, 1).pushed_revision = session.get(DbMeta, 1).revision
        session.commit()

        result = sync.checkout(session)
        assert not result.ok
        assert "schema version" in result.message
        assert sync.read_lock() is None

    def test_a_sidecar_without_a_version_makes_no_claim(self, session, local, nas):
        """Written before the version travelled. Zero must not read as 'older than 1'."""
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()

        meta = json.loads((nas / sync.META).read_text())
        del meta["schema_version"]
        (nas / sync.META).write_text(json.dumps(meta, indent=2))

        state = sync.status(session)
        assert state.nas.schema_version == 0
        assert not state.stale_code


class TestReconciliation:
    def test_local_only_finds_rows_absent_from_the_ancestor(self, session, local, nas):
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)  # snapshots the ancestor
        session.commit()

        txn, _ = add_one(session, "99.00")
        session.commit()

        extra = sync.local_only(session)
        assert [t.uid for t in extra] == [txn.uid]

    def test_export_frame_is_shaped_for_the_importer(self, session, local, nas):
        from budget import importer

        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        add_one(session, "99.00")
        session.commit()

        frame = sync.local_only_frame(session)
        candidates, problems = importer.parse(frame)

        assert problems == []
        assert len(candidates) == 1
        assert candidates[0].amount == Decimal("99.00")


class TestFirstPull:
    """Pulling onto a machine that has no database at all.

    This is the documented way to set up a second machine, and the only way back after a
    local copy is lost -- but it was never exercised, because every test started from a
    database the fixtures had already built. Both failures below were real.
    """

    def _master(self, tmp_path, monkeypatch):
        """A NAS with a master on it, and a local path that does not exist yet."""
        from budget import config, sync
        from budget.db import create_all, make_engine, make_session_factory
        from budget.models import DbMeta

        nas = tmp_path / "nas"
        nas.mkdir()
        source = tmp_path / "source.db"
        engine = make_engine(source)
        create_all(engine)
        with make_session_factory(engine)() as s, s.begin():
            s.add(DbMeta(id=1, revision=4, base_revision=4, pushed_revision=4))
        engine.dispose()

        import hashlib
        import json

        shutil_copy = __import__("shutil").copy2
        shutil_copy(source, nas / "budget.db")
        digest = hashlib.sha256((nas / "budget.db").read_bytes()).hexdigest()
        (nas / "budget.meta.json").write_text(
            json.dumps({"revision": 4, "machine": "OTHER", "sha256": digest}),
            encoding="utf-8",
        )
        monkeypatch.setattr(config, "NAS_DIR", nas)
        return sync

    def test_pull_creates_the_folder_it_needs(self, tmp_path, monkeypatch):
        """Opening an engine used to create the directory as a side effect. Once the
        no-database case skipped that, staging had nowhere to land."""
        from budget import config

        sync = self._master(tmp_path, monkeypatch)
        target = tmp_path / "never" / "made" / "budget.db"
        monkeypatch.setattr(config, "BASE_DB_PATH", target.with_name("budget.base.db"))

        result = sync.pull(target)
        assert result.ok, result.message
        assert target.exists()

    def test_pull_onto_a_machine_with_no_database(self, tmp_path, monkeypatch):
        """read_local copes with a missing db_meta row, but querying it at all raises when
        the table has never been created."""
        from budget import config

        sync = self._master(tmp_path, monkeypatch)
        target = tmp_path / "local" / "budget.db"
        monkeypatch.setattr(config, "BASE_DB_PATH", target.with_name("budget.base.db"))

        result = sync.pull(target)
        assert result.ok, result.message
        assert "revision 4" in result.message

    def test_pull_onto_an_empty_file_left_by_a_failed_attempt(self, tmp_path, monkeypatch):
        from budget import config

        sync = self._master(tmp_path, monkeypatch)
        target = tmp_path / "local" / "budget.db"
        target.parent.mkdir()
        target.touch()
        monkeypatch.setattr(config, "BASE_DB_PATH", target.with_name("budget.base.db"))

        assert sync.pull(target).ok

    def test_pull_still_refuses_when_there_is_unpushed_work(self, tmp_path, monkeypatch):
        """The guard that matters: recovery must never silently discard local changes."""
        from budget import config
        from budget.db import create_all, make_engine, make_session_factory
        from budget.models import DbMeta

        sync = self._master(tmp_path, monkeypatch)
        target = tmp_path / "local" / "budget.db"
        target.parent.mkdir()
        engine = make_engine(target)
        create_all(engine)
        with make_session_factory(engine)() as s, s.begin():
            s.add(DbMeta(id=1, revision=9, base_revision=4, pushed_revision=4))
        engine.dispose()
        monkeypatch.setattr(config, "BASE_DB_PATH", target.with_name("budget.base.db"))

        result = sync.pull(target)
        assert not result.ok
        assert "unpushed" in result.message


class TestDiscardingLocalChanges:
    """The way out of a conflict.

    6.3.4 says to export the local-only transactions, pull the fresh master, then re-import
    them. Every route to that middle step went through `pull`, which refused precisely
    because the machine held the work the export had just captured -- so the documented
    remedy could not be carried out, and a conflict reached through ordinary use was a dead
    end. These pin both halves: the refusal stays the default, and the way past it keeps
    what it sets aside.
    """

    def conflicted(self, session, local, nas):
        """A machine with unpushed work, and a master that has moved on without it."""
        add_one(session)
        session.commit()
        assert sync.push(session, db_path=local).ok
        session.commit()

        meta = json.loads((nas / sync.META).read_text())
        meta["revision"] = meta["revision"] + 5  # the other machine pushed since
        (nas / sync.META).write_text(json.dumps(meta, indent=2))

        add_one(session, "42.00")  # and this one has entered something meanwhile
        session.commit()

        # Nothing may touch the session after this: Windows will not rename a file that is
        # still open, and a single query would reopen it. Which is exactly what the real
        # dashboard does through ui.close_connections before pulling.
        state = sync.status(session)
        session.close()
        session.get_bind().dispose()
        return state

    def test_the_refusal_is_still_the_default(self, session, local, nas):
        self.conflicted(session, local, nas)
        result = sync.pull(db_path=local)

        assert not result.ok
        assert "unpushed" in result.message
        # And it now says how to get past it, rather than describing a dead end.
        assert "discard local changes" in " ".join(result.detail)

    def test_discarding_lets_the_pull_through(self, session, local, nas):
        self.conflicted(session, local, nas)
        assert sync.pull(db_path=local, discard_local=True).ok

    def test_what_is_discarded_is_kept_beside_the_database(self, session, local, nas):
        """'Discard' has to mean 'stop using', not 'destroy'. The CSV export covers
        transactions only, so a setting changed here would otherwise vanish unrecorded."""
        self.conflicted(session, local, nas)
        result = sync.pull(db_path=local, discard_local=True)

        kept = list(local.parent.glob("*.discarded-*.db"))
        assert len(kept) == 1
        assert kept[0].stat().st_size > 0
        assert "set aside, not deleted" in " ".join(result.detail)

    def test_the_kept_copy_is_still_a_readable_database(self, session, local, nas):
        """Renamed rather than copied, so it is whole -- including anything that was still
        sitting in the WAL when the pull happened."""
        self.conflicted(session, local, nas)
        sync.pull(db_path=local, discard_local=True)

        kept = next(local.parent.glob("*.discarded-*.db"))
        connection = sqlite3.connect(f"file:{kept}?mode=ro", uri=True)
        try:
            count = connection.execute(
                "SELECT count(*) FROM txn WHERE deleted_at IS NULL"
            ).fetchone()[0]
        finally:
            connection.close()
        assert count == 2  # both transactions, the pushed one and the unpushed one

    def test_a_master_that_fails_verification_leaves_the_local_database_alone(
        self, session, local, nas
    ):
        """The ordering that matters. Setting the database aside before the download is
        verified would mean a corrupt master destroyed the copy it could not replace."""
        self.conflicted(session, local, nas)
        before = local.read_bytes()

        meta = json.loads((nas / sync.META).read_text())
        meta["sha256"] = "0" * 64  # the master no longer matches what was published
        (nas / sync.META).write_text(json.dumps(meta, indent=2))

        result = sync.pull(db_path=local, discard_local=True)

        assert not result.ok
        assert local.read_bytes() == before
        assert list(local.parent.glob("*.discarded-*.db")) == []

    def test_a_clean_machine_is_unaffected_by_the_flag(self, session, local, nas):
        """Nothing to discard, so nothing is set aside -- the flag is not a second code path
        for the ordinary pull."""
        add_one(session)
        session.commit()
        sync.push(session, db_path=local)
        session.commit()
        session.close()
        session.get_bind().dispose()

        assert sync.pull(db_path=local, discard_local=True).ok
        assert list(local.parent.glob("*.discarded-*.db")) == []
