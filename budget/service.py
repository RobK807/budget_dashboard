"""Write operations.

Every write bumps `db_meta.revision`. Nothing uses that yet -- it is what the cross-machine
sync in Phase 2b keys off (DESIGN.md 6.3), and having it in place from the first write means
no back-filling of revisions for rows created before sync existed.

Deletion is always soft. The workbook's remove_transaction cleared cells, shuffled every row
below up one and stripped a substring out of a cell comment; here it is a single UPDATE
against a primary key, and the row remains visible in the audit trail.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from budget.models import Account, Category, Classification, DbMeta, ImportBatch, Txn
from budget.validation import Candidate, Reference, Result, period_for, validate


# Marks the one-off workbook migration. That batch holds the whole imported history, so the
# UI refuses to undo it -- rebuilding is `migrate_xlsm --force`, not a button.
MIGRATION_NOTE = "Initial migration"


@dataclass
class ImportOutcome:
    batch_id: int | None
    created: int
    rejected: list[tuple[int, list[str]]]  # (source_row, errors)
    warnings: list[tuple[int, list[str]]]


def bump_revision(session: Session) -> int:
    meta = session.get(DbMeta, 1)
    if meta is None:
        meta = DbMeta(id=1, revision=0, base_revision=0, pushed_revision=0, mode="online")
        session.add(meta)
        session.flush()
    meta.revision += 1
    meta.updated_at = dt.datetime.now()
    session.flush()
    return meta.revision


def load_reference(session: Session) -> Reference:
    return Reference(
        accounts={
            a.name: {"id": a.id, "valid_from": a.valid_from, "valid_to": a.valid_to}
            for a in session.scalars(select(Account))
        },
        categories={
            c.name: {
                "id": c.id,
                "spend_type": c.spend_type,
                "valid_from": c.valid_from,
                "valid_to": c.valid_to,
            }
            for c in session.scalars(select(Category))
        },
        classifications={c.name for c in session.scalars(select(Classification))},
    )


def build_identifier(session: Session, c: Candidate, ref: Reference) -> str:
    """Recreates the workbook's xlIdent: MMDD_CODE[_TOCODE]_n.

    n counts transactions already recorded that day for the same account, deleted ones
    included, so an identifier is never silently reused after a deletion. Useful as a
    natural key when de-duplicating a re-imported bank statement.
    """
    codes = {a.name: a.short_code for a in session.scalars(select(Account))}
    same_day = session.scalar(
        select(func.count())
        .select_from(Txn)
        .where(
            Txn.txn_date == c.txn_date,
            Txn.account_from_id == ref.accounts[c.account_from]["id"],
        )
    )
    stem = f"{c.txn_date.month:02d}{c.txn_date.day:02d}_{codes.get(c.account_from, '???')}"
    if c.type == "Transfer" and c.account_to:
        stem += f"_{codes.get(c.account_to, '???')}"
    return f"{stem}_{same_day}"


def add_transaction(
    session: Session,
    candidate: Candidate,
    ref: Reference | None = None,
    source: str = "manual",
    batch_id: int | None = None,
) -> tuple[Txn | None, Result]:
    """Validate and insert. Returns (None, result) if the candidate is rejected."""
    ref = ref or load_reference(session)
    result = validate(candidate, ref)
    if not result.ok:
        return None, result

    category_id = ref.categories[candidate.category]["id"] if candidate.category else None
    classification_id = None
    if candidate.classification:
        classification_id = session.scalar(
            select(Classification.id).where(Classification.name == candidate.classification)
        )

    txn = Txn(
        txn_date=candidate.txn_date,
        period=period_for(candidate.txn_date),
        type=candidate.type,
        amount=candidate.amount,
        account_from_id=ref.accounts[candidate.account_from]["id"],
        account_to_id=(
            ref.accounts[candidate.account_to]["id"] if candidate.account_to else None
        ),
        category_id=category_id,
        classification_id=classification_id,
        comment=candidate.comment or None,
        category_comment=candidate.category_comment or None,
        legacy_identifier=build_identifier(session, candidate, ref),
        source=source,
        batch_id=batch_id,
    )
    session.add(txn)
    session.flush()
    bump_revision(session)
    return txn, result


def soft_delete(session: Session, txn_id: int, reason: str | None = None) -> bool:
    txn = session.get(Txn, txn_id)
    if txn is None or txn.deleted_at is not None:
        return False
    txn.deleted_at = dt.datetime.now()
    txn.deleted_reason = reason or "Removed via the dashboard"
    session.flush()
    bump_revision(session)
    return True


def restore(session: Session, txn_id: int) -> bool:
    txn = session.get(Txn, txn_id)
    if txn is None or txn.deleted_at is None:
        return False
    txn.deleted_at = None
    txn.deleted_reason = None
    session.flush()
    bump_revision(session)
    return True


def import_candidates(
    session: Session,
    candidates: list[Candidate],
    filename: str | None = None,
    note: str | None = None,
) -> ImportOutcome:
    """All-or-nothing: if any row fails validation, nothing is written.

    The workbook's bulk_upload looped row by row calling New_entry, so a bad row midway
    through left the first half imported and the rest not, with no record of where it
    stopped. Here the batch either lands whole or not at all.
    """
    ref = load_reference(session)

    rejected: list[tuple[int, list[str]]] = []
    warnings: list[tuple[int, list[str]]] = []
    for c in candidates:
        result = validate(c, ref)
        row = c.source_row or 0
        if not result.ok:
            rejected.append((row, result.errors))
        if result.warnings:
            warnings.append((row, result.warnings))

    if rejected:
        return ImportOutcome(None, 0, rejected, warnings)

    batch = ImportBatch(filename=filename, row_count=len(candidates), note=note)
    session.add(batch)
    session.flush()

    for c in candidates:
        add_transaction(session, c, ref=ref, source="bulk", batch_id=batch.id)

    bump_revision(session)
    return ImportOutcome(batch.id, len(candidates), [], warnings)


def undo_batch(session: Session, batch_id: int, reason: str | None = None) -> int:
    """Soft-delete every transaction in a batch. Returns the number affected."""
    affected = 0
    for txn in session.scalars(
        select(Txn).where(Txn.batch_id == batch_id, Txn.deleted_at.is_(None))
    ):
        txn.deleted_at = dt.datetime.now()
        txn.deleted_reason = reason or f"Undid import batch {batch_id}"
        affected += 1
    if affected:
        session.flush()
        bump_revision(session)
    return affected


def recent_batches(session: Session, limit: int = 10) -> list[ImportBatch]:
    return list(
        session.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(limit))
    )
