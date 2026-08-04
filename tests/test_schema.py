"""In-place schema migration.

The alternative to migrating is rebuilding from the workbook, which works but resets the
sync revision and would mean force-pushing over a master that is already correct. So the
properties that matter are: values convert, everything else survives, and running it twice
changes nothing.
"""

import sqlite3

import pytest

from budget.db import create_all, make_engine
from budget.schema import SCHEMA_VERSION, apply_migrations

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
