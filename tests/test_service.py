"""Write operations: insert, soft delete, restore, batch import and undo."""

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select

from budget import service
from budget.models import DbMeta, Txn
from budget.validation import Candidate

JUNE = dt.date(2026, 6, 15)


def candidate(**kw):
    base = dict(
        txn_date=JUNE, type="Debit", amount=Decimal("12.50"),
        account_from="HSBC", category="Food", classification="Food",
    )
    return Candidate(**{**base, **kw})


def live_count(session):
    return session.scalar(
        select(func.count()).select_from(Txn).where(Txn.deleted_at.is_(None))
    )


class TestAddTransaction:
    def test_inserts_and_derives_the_period(self, session):
        txn, result = service.add_transaction(session, candidate())
        assert result.ok
        assert txn.period == "2026-06"
        assert txn.amount == Decimal("12.50")

    def test_rejects_an_invalid_candidate_without_writing(self, session):
        before = live_count(session)
        txn, result = service.add_transaction(session, candidate(amount=Decimal("-1")))
        assert txn is None
        assert not result.ok
        assert live_count(session) == before

    def test_builds_the_workbook_style_identifier(self, session):
        txn, _ = service.add_transaction(session, candidate())
        assert txn.legacy_identifier.startswith("0615_HSB_")

    def test_identifier_counts_up_within_a_day(self, session):
        first, _ = service.add_transaction(session, candidate())
        second, _ = service.add_transaction(session, candidate())
        assert first.legacy_identifier != second.legacy_identifier

    def test_transfer_records_both_accounts(self, session):
        txn, result = service.add_transaction(
            session,
            candidate(type="Transfer", account_to="Savings", category=None,
                      classification=None),
        )
        assert result.ok
        assert txn.account_to_id is not None


class TestRevision:
    """Nothing consumes this yet; it is what Phase 2b's sync keys off, and back-filling it
    later would be guesswork."""

    def test_every_write_bumps_the_revision(self, session):
        start = session.get(DbMeta, 1).revision
        service.add_transaction(session, candidate())
        assert session.get(DbMeta, 1).revision > start

    def test_a_rejected_write_does_not_bump_it(self, session):
        start = session.get(DbMeta, 1).revision
        service.add_transaction(session, candidate(amount=Decimal("0")))
        assert session.get(DbMeta, 1).revision == start


class TestSoftDelete:
    def test_removes_from_live_but_keeps_the_row(self, session):
        txn, _ = service.add_transaction(session, candidate())
        before = live_count(session)

        assert service.soft_delete(session, txn.id, "typo") is True

        assert live_count(session) == before - 1
        assert session.get(Txn, txn.id) is not None
        assert session.get(Txn, txn.id).deleted_reason == "typo"

    def test_deleting_twice_is_a_no_op(self, session):
        txn, _ = service.add_transaction(session, candidate())
        service.soft_delete(session, txn.id)
        assert service.soft_delete(session, txn.id) is False

    def test_restore_brings_it_back(self, session):
        txn, _ = service.add_transaction(session, candidate())
        service.soft_delete(session, txn.id)
        assert service.restore(session, txn.id) is True
        assert session.get(Txn, txn.id).deleted_at is None

    def test_unknown_id_is_handled(self, session):
        assert service.soft_delete(session, 9999) is False


class TestImportBatch:
    def test_imports_a_whole_batch(self, session):
        outcome = service.import_candidates(
            session, [candidate(source_row=2), candidate(source_row=3)], filename="x.csv"
        )
        assert outcome.created == 2
        assert outcome.batch_id is not None
        assert not outcome.rejected

    def test_one_bad_row_blocks_the_entire_batch(self, session):
        """bulk_upload called New_entry row by row, so a failure midway left half the rows
        imported with no record of where it stopped."""
        before = live_count(session)
        outcome = service.import_candidates(
            session,
            [candidate(source_row=2), candidate(source_row=3, account_from="Nope")],
            filename="x.csv",
        )
        assert outcome.created == 0
        assert outcome.batch_id is None
        assert outcome.rejected[0][0] == 3
        assert live_count(session) == before

    def test_undo_soft_deletes_the_batch(self, session):
        outcome = service.import_candidates(
            session, [candidate(source_row=2), candidate(source_row=3)]
        )
        before = live_count(session)

        assert service.undo_batch(session, outcome.batch_id) == 2

        assert live_count(session) == before - 2
        assert session.scalar(select(func.count()).select_from(Txn)) >= 2

    def test_undoing_twice_affects_nothing(self, session):
        outcome = service.import_candidates(session, [candidate(source_row=2)])
        service.undo_batch(session, outcome.batch_id)
        assert service.undo_batch(session, outcome.batch_id) == 0
