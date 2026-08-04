"""In-place schema migrations.

Small and hand-rolled rather than Alembic: there is one database, it is single-user, and the
alternative to migrating is rebuilding from the workbook — which works, but resets the sync
revision and would mean force-pushing over a master that is already correct.

SQLite cannot ALTER a CHECK constraint, so changing one means the twelve-step table rebuild:
create the replacement, copy across, drop the original, rename. Foreign keys are disabled for
the duration, which is why this runs outside the app's normal engine.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

SCHEMA_VERSION = 5

# Columns added to tables that already existed. create_all only creates whole tables, so a
# new column on an existing one needs an explicit ALTER -- cheap in SQLite, unlike a CHECK.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    # Expected gross is not salary/12: a bonus month carries its own figure.
    ("payslip", "expected_gross", "INTEGER"),
    # v3 -----------------------------------------------------------------------------
    ("account", "exclude_from_savings", "BOOLEAN NOT NULL DEFAULT 0"),
    ("account", "statement_day", "INTEGER"),
    ("account", "payment_day", "INTEGER"),
    ("card", "credit_limit", "INTEGER"),
    # v4 -----------------------------------------------------------------------------
    # A bonus paid separately from salary is its own payment with its own deductions. Held
    # on the bonus row rather than the payslip so entering it does not overwrite the month's
    # salary, which a single payslip per period would have forced.
    ("bonus", "gross", "INTEGER"),
    ("bonus", "ni", "INTEGER"),
    ("bonus", "paye", "INTEGER"),
    ("bonus", "net", "INTEGER"),
    ("bonus", "payday", "INTEGER"),
    # v5 -----------------------------------------------------------------------------
    ("salary_profile", "base_salary", "INTEGER"),
]

# Splitting the stored annual salary back into its two parts. What was held was base *plus*
# car allowance, and the allowance is a function of the base:
#
#     car  = LOWER% x THRESHOLD + UPPER% x (base - THRESHOLD)
#     total = base + car = base x (1 + UPPER%) + THRESHOLD x (LOWER% - UPPER%)
#
# so base = (total - THRESHOLD x (LOWER% - UPPER%)) / (1 + UPPER%). With the defaults that
# is (total - 3,500) / 1.05, which recovers 118,905.00 from 128,350.25 and 116,688.00 from
# 126,022.40 -- both exactly, and both round numbers, which is the check that this is the
# right inversion rather than a plausible one.
CAR_THRESHOLD_PENCE = 5_000_000
CAR_LOWER_RATE = 0.12
CAR_UPPER_RATE = 0.05

# Rates moved from fractions to percentages (0.08 -> 8.00). The Money column stores two
# decimal places, so as a fraction a rate could only ever be a whole percentage point --
# 8.5% would have rounded to 9%. Nothing in the data needed the precision yet, which is
# exactly why it was worth fixing before something did.
RATE_KEYS = (
    "ni_lower_rate",
    "ni_higher_rate",
    "basic_rate",
    "higher_rate",
    "additional_rate",
)

# The workbook's vocabulary (Selections!AL) described the *sign* of a running total, which
# is ambiguous because the meaning of the sign depends on the classification's direction.
# Excess has direction -1, so a positive running total there is credits exceeding debits --
# a credit balance. Hence positive -> credit, negative -> debit.
ROLLOVER_RENAMES = {"positive": "credit", "negative": "debit"}

_NEW_CLASSIFICATION = """
CREATE TABLE classification_migrated (
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
        CHECK (rollover IN ('none','all','credit','debit'))
)
"""


def _needs_rollover_rename(cursor) -> bool:
    row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='classification'"
    ).fetchone()
    return bool(row) and "'positive'" in (row[0] or "")


def _rename_rollover_vocabulary(cursor) -> None:
    cursor.execute(_NEW_CLASSIFICATION)
    cursor.execute(
        """
        INSERT INTO classification_migrated
            (id, uid, name, legacy_ref, direction, rollover, counts_as_spend,
             display_order, valid_from, valid_to)
        SELECT id, uid, name, legacy_ref, direction,
               CASE rollover
                   WHEN 'positive' THEN 'credit'
                   WHEN 'negative' THEN 'debit'
                   ELSE rollover
               END,
               counts_as_spend, display_order, valid_from, valid_to
        FROM classification
        """
    )
    cursor.execute("DROP TABLE classification")
    cursor.execute("ALTER TABLE classification_migrated RENAME TO classification")


def _stored_schema_version(cursor) -> int:
    row = cursor.execute("SELECT value FROM setting WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def _rescale_rates_to_percentages(cursor) -> int:
    """Multiply the rate rows by 100, once.

    Guarded by the stored schema version rather than by inspecting the values: 0.2 and 20
    are both plausible rates, so there is no way to tell from a number alone whether it has
    already been converted, and running this twice would silently turn 20% into 2000%.
    """
    placeholders = ",".join("?" * len(RATE_KEYS))
    cursor.execute(
        f"UPDATE salary_assumption SET value = value * 100 WHERE key IN ({placeholders})",
        RATE_KEYS,
    )
    return cursor.rowcount


def _split_out_base_salary(cursor) -> int:
    """Recover the base salary from a stored base-plus-car total.

    Only rows that have no base yet, so it cannot run twice over the same figure. Rounded to
    the penny: the arithmetic is exact for the salaries actually stored, and a half-penny
    drift on some future one is better than a truncation that loses a pound.
    """
    constant = CAR_THRESHOLD_PENCE * (CAR_LOWER_RATE - CAR_UPPER_RATE)
    cursor.execute(
        "UPDATE salary_profile "
        "SET base_salary = CAST(ROUND((annual_salary - ?) / ?) AS INTEGER) "
        "WHERE base_salary IS NULL AND annual_salary IS NOT NULL",
        (constant, 1 + CAR_UPPER_RATE),
    )
    return cursor.rowcount


def apply_migrations(engine: Engine) -> list[str]:
    """Bring an existing database up to SCHEMA_VERSION. Safe to call on every start."""
    applied: list[str] = []

    raw = engine.raw_connection()
    try:
        connection = raw.driver_connection
        previous_isolation = connection.isolation_level
        connection.isolation_level = None  # autocommit; PRAGMA cannot run in a transaction
        cursor = connection.cursor()
        try:
            if not cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='classification'"
            ).fetchone():
                return applied  # brand new database; create_all builds it correctly

            was_at = _stored_schema_version(cursor)

            if _needs_rollover_rename(cursor):
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("BEGIN")
                try:
                    _rename_rollover_vocabulary(cursor)
                    cursor.execute("COMMIT")
                except Exception:
                    cursor.execute("ROLLBACK")
                    raise
                finally:
                    cursor.execute("PRAGMA foreign_keys=ON")
                applied.append("rollover vocabulary: positive → debit, negative → credit")

            for table, column, column_type in ADDED_COLUMNS:
                if not cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone():
                    continue  # create_all will build it with the column already present
                existing = {
                    row[1] for row in cursor.execute(f"PRAGMA table_info({table})")
                }
                if column not in existing:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    applied.append(f"added {table}.{column}")

            if was_at < 3 and cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='salary_assumption'"
            ).fetchone():
                changed = _rescale_rates_to_percentages(cursor)
                if changed:
                    applied.append(f"rates rescaled to percentages ({changed} row(s))")

            if cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='salary_profile'"
            ).fetchone():
                split = _split_out_base_salary(cursor)
                if split:
                    applied.append(f"base salary split out of {split} salary record(s)")

            cursor.execute(
                "INSERT INTO setting (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        finally:
            cursor.close()
            connection.isolation_level = previous_isolation
    finally:
        raw.close()

    return applied
