"""Cross-machine sync.

The properties worth pinning are the ones whose failure is silent: a push that believes
itself successful when it was not, a conflict that goes undetected, or a pull that
overwrites unpushed work.
"""

import datetime as dt
import json
from decimal import Decimal

import pytest

from budget import config, service, sync
from budget.db import make_engine, make_session_factory
from budget.models import DbMeta, Txn
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
