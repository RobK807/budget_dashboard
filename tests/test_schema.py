"""In-place schema migration.

The alternative to migrating is rebuilding from the workbook, which works but resets the
sync revision and would mean force-pushing over a master that is already correct. So the
properties that matter are: values convert, everything else survives, and running it twice
changes nothing.
"""

import sqlite3

import pytest

from budget.db import create_all, make_engine
from budget.schema import (
    SCHEMA_VERSION,
    SchemaTooNew,
    apply_migrations,
    stored_version,
)

OLD_CLASSIFICATION = """
CREATE TABLE classification (
    id INTEGER NOT NULL,
    uid VARCHAR(32) DEFAULT (lower(hex(randomblob(16)))) NOT NULL,
    name VARCHAR(64) NOT NULL,
    legacy_ref INTEGER NOT NULL,
    direction INTEGER NOT NULL,
    rollover VARCHAR(16) NOT NULL,
    counts_as_spend BOOLEAN NOT NULL,
    display_order INTEGER,
    valid_from DATE NOT NULL,
    valid_to DATE,
    PRIMARY KEY (id),
    UNIQUE (uid),
    UNIQUE (name),
    CONSTRAINT ck_classification_rollover
        CHECK (rollover IN ('none','all','positive','negative'))
)
"""


@pytest.fixture
def legacy_db(tmp_path):
    """A database as it existed before the vocabulary change."""
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(OLD_CLASSIFICATION)
    connection.execute("CREATE TABLE setting (key VARCHAR(64) PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO classification "
        "(id, name, legacy_ref, direction, rollover, counts_as_spend, valid_from) "
        "VALUES (?,?,?,?,?,1,'2026-04-01')",
        [
            (1, "Bills", 1, 1, "none"),
            (2, "Excess", 2, -1, "negative"),
            (3, "Expenses", 5, 1, "all"),
            (4, "Overspend", 9, 1, "positive"),
        ],
    )
    connection.commit()
    connection.close()
    return path


def rollovers(path) -> dict[str, str]:
    connection = sqlite3.connect(path)
    rows = dict(connection.execute("SELECT name, rollover FROM classification"))
    connection.close()
    return rows


def test_positive_and_negative_are_renamed(legacy_db):
    engine = make_engine(legacy_db)
    applied = apply_migrations(engine)
    engine.dispose()

    # Excess has direction -1, so a positive running total there means credits exceeded
    # debits. The sign alone is ambiguous; the balance type is not.
    assert applied
    assert rollovers(legacy_db) == {
        "Bills": "none",
        "Excess": "debit",        # negative -> debit
        "Expenses": "all",        # unchanged
        "Overspend": "credit",    # positive -> credit
    }


def test_other_columns_survive(legacy_db):
    engine = make_engine(legacy_db)
    apply_migrations(engine)
    engine.dispose()

    connection = sqlite3.connect(legacy_db)
    row = connection.execute(
        "SELECT name, legacy_ref, direction, counts_as_spend, valid_from "
        "FROM classification WHERE id = 2"
    ).fetchone()
    connection.close()
    assert row == ("Excess", 2, -1, 1, "2026-04-01")


def test_the_new_constraint_is_in_place(legacy_db):
    engine = make_engine(legacy_db)
    apply_migrations(engine)
    engine.dispose()

    connection = sqlite3.connect(legacy_db)
    ddl = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name='classification'"
    ).fetchone()[0]
    connection.close()
    assert "'credit'" in ddl and "'debit'" in ddl
    assert "'positive'" not in ddl


def test_running_twice_is_a_no_op(legacy_db):
    engine = make_engine(legacy_db)
    first = apply_migrations(engine)
    second = apply_migrations(engine)
    engine.dispose()

    assert first and not second
    assert rollovers(legacy_db)["Excess"] == "debit"


def test_version_is_stamped(legacy_db):
    engine = make_engine(legacy_db)
    apply_migrations(engine)
    engine.dispose()

    connection = sqlite3.connect(legacy_db)
    version = connection.execute(
        "SELECT value FROM setting WHERE key='schema_version'"
    ).fetchone()
    connection.close()
    assert version[0] == str(SCHEMA_VERSION)


def test_a_brand_new_database_needs_no_migration(tmp_path):
    engine = make_engine(tmp_path / "fresh.db")
    applied = create_all(engine)
    engine.dispose()
    assert applied == []


class TestStampingANewDatabase:
    """A database this code just built has to say which version it is.

    Left unstamped it reads as version 0 -- identical to a legacy file -- so the *second*
    start ran every historic migration over data that had never needed them. The rate rescale
    is guarded by `was_at < 3`, which meant a fresh database seeded with a 20.00% basic rate
    came back from its second launch holding 2000.00%.
    """

    def fresh(self, tmp_path):
        engine = make_engine(tmp_path / "fresh.db")
        create_all(engine)
        engine.dispose()
        return tmp_path / "fresh.db"

    def version(self, path) -> str | None:
        connection = sqlite3.connect(path)
        row = connection.execute(
            "SELECT value FROM setting WHERE key='schema_version'"
        ).fetchone()
        connection.close()
        return row[0] if row else None

    def test_creation_stamps_the_current_version(self, tmp_path):
        assert self.version(self.fresh(tmp_path)) == str(SCHEMA_VERSION)

    def test_a_second_start_does_not_rescale_rates_that_were_never_fractions(self, tmp_path):
        path = self.fresh(tmp_path)

        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO salary_assumption (tax_year, key, effective_from, value) "
            "VALUES (2026, 'basic_rate', '2026-04-06', 2000)"  # 20.00%, in pence
        )
        connection.commit()
        connection.close()

        engine = make_engine(path)
        applied = apply_migrations(engine)
        engine.dispose()

        connection = sqlite3.connect(path)
        rate = connection.execute(
            "SELECT value FROM salary_assumption WHERE key='basic_rate'"
        ).fetchone()[0]
        connection.close()

        assert applied == []
        assert rate == 2000  # not 200000

    def test_an_unstamped_database_that_holds_data_is_still_migrated(self, legacy_db):
        """The other half of the same decision. Version 0 on a populated file means legacy,
        and stamping those on sight would skip the migrations they genuinely need."""
        engine = make_engine(legacy_db)
        applied = apply_migrations(engine)
        engine.dispose()

        assert applied  # the rollover rename ran rather than being stamped past


class TestADatabaseFromNewerCode:
    def stamped_at(self, legacy_db, version):
        connection = sqlite3.connect(legacy_db)
        connection.execute(
            "INSERT INTO setting (key, value) VALUES ('schema_version', ?)", (str(version),)
        )
        connection.commit()
        connection.close()
        return legacy_db

    def test_migrating_is_refused(self, legacy_db):
        self.stamped_at(legacy_db, SCHEMA_VERSION + 1)
        engine = make_engine(legacy_db)
        with pytest.raises(SchemaTooNew) as raised:
            apply_migrations(engine)
        engine.dispose()

        assert raised.value.found == SCHEMA_VERSION + 1
        assert raised.value.understood == SCHEMA_VERSION

    def test_the_version_is_not_stamped_backwards(self, legacy_db):
        """The refusal has to come before the stamp at the end, or the act of refusing would
        destroy the only evidence of what the file actually is."""
        self.stamped_at(legacy_db, SCHEMA_VERSION + 1)
        engine = make_engine(legacy_db)
        with pytest.raises(SchemaTooNew):
            apply_migrations(engine)
        engine.dispose()

        connection = sqlite3.connect(legacy_db)
        stored = connection.execute(
            "SELECT value FROM setting WHERE key='schema_version'"
        ).fetchone()[0]
        connection.close()
        assert stored == str(SCHEMA_VERSION + 1)

    def test_nothing_is_altered_before_the_refusal(self, legacy_db):
        """The rollover rename is pending on this fixture; it must not have run."""
        self.stamped_at(legacy_db, SCHEMA_VERSION + 1)
        engine = make_engine(legacy_db)
        with pytest.raises(SchemaTooNew):
            apply_migrations(engine)
        engine.dispose()

        assert rollovers(legacy_db)["Excess"] == "negative"  # untouched


class TestStoredVersion:
    def test_reads_the_version_without_writing_to_the_database(self, tmp_path):
        """make_engine sets journal_mode=WAL on connect, which writes to the header. A file
        being examined in order to decide whether to accept it must not be modified by the
        examining -- the callers are about to move or replace it.

        The -wal and -shm sidecars are a different matter: in WAL mode even a read-only
        connection maps the shared-memory index, so empty ones appear and the callers clean
        them up. What must not change is the database itself.
        """
        path = tmp_path / "fresh.db"
        engine = make_engine(path)
        create_all(engine)
        engine.dispose()

        before = path.stat().st_mtime_ns, path.stat().st_size
        assert stored_version(path) == SCHEMA_VERSION
        assert (path.stat().st_mtime_ns, path.stat().st_size) == before

    def test_an_unreadable_file_makes_no_claim(self, tmp_path):
        rubbish = tmp_path / "not-a-database.db"
        rubbish.write_bytes(b"certainly not SQLite")
        assert stored_version(rubbish) == 0

    def test_a_missing_file_makes_no_claim(self, tmp_path):
        assert stored_version(tmp_path / "absent.db") == 0


class TestMinimumPaymentsBecomePercentages:
    """Schema 8. As a fraction in a Money column a minimum payment could only ever be a
    whole percentage point, so every card at 2.5% was stored -- and charged -- at 3%."""

    def card(self, path, min_payment_pct: int) -> int:
        """Insert a card holding a raw pence value, migrate, return what it holds after."""
        connection = sqlite3.connect(path)
        connection.execute(
            "INSERT INTO card (name, opening_balance, opening_date, term_months, "
            "min_payment_pct) VALUES ('Test', 100000, '2025-04-01', 24, ?)",
            (min_payment_pct,),
        )
        connection.execute(
            "UPDATE setting SET value = '7' WHERE key = 'schema_version'"
        )
        connection.commit()
        connection.close()

        engine = make_engine(path)
        apply_migrations(engine)
        engine.dispose()

        connection = sqlite3.connect(path)
        value = connection.execute("SELECT min_payment_pct FROM card").fetchone()[0]
        connection.close()
        return value

    def fresh(self, tmp_path):
        engine = make_engine(tmp_path / "cards.db")
        create_all(engine)
        engine.dispose()
        return tmp_path / "cards.db"

    def test_a_fraction_is_rescaled(self, tmp_path):
        assert self.card(self.fresh(tmp_path), 1) == 100  # 0.01 -> 1.00%

    def test_a_value_already_rounded_keeps_its_scale_not_its_precision(self, tmp_path):
        """0.025 was stored as 3 pence before this ran. The migration recovers the scale --
        3.00% -- but nothing can recover the 2.5 that was thrown away on the way in."""
        assert self.card(self.fresh(tmp_path), 3) == 300

    def test_running_twice_does_not_rescale_twice(self, tmp_path):
        path = self.fresh(tmp_path)
        self.card(path, 1)

        engine = make_engine(path)
        applied = apply_migrations(engine)
        engine.dispose()

        connection = sqlite3.connect(path)
        value = connection.execute("SELECT min_payment_pct FROM card").fetchone()[0]
        connection.close()

        assert value == 100  # not 10000
        assert not any("minimum payment" in line for line in applied)
