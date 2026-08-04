"""A small in-memory database with just enough reference data to exercise writes."""

import datetime as dt

import pytest

from budget.db import create_all, make_engine, make_session_factory
from budget.models import Account, Category, Classification, DbMeta

APRIL = dt.date(2026, 4, 1)


@pytest.fixture
def session(tmp_path):
    engine = make_engine(tmp_path / "write.db")
    create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        s.add_all(
            [
                Account(name="HSBC", short_code="HSB", type="bank", valid_from=APRIL),
                Account(name="Savings", short_code="SAV", type="bank", valid_from=APRIL,
                        is_savings=True),
                Account(name="BA Amex", short_code="BAAM", type="credit_card",
                        valid_from=APRIL),
                # Opened mid-year: effective dating replaces the workbook's per-month
                # offset columns, so this must reject transactions dated before June.
                Account(name="Tembo", short_code="TEM", type="bank",
                        valid_from=dt.date(2026, 6, 1)),
                Category(name="Food", grouping="Other", spend_type="Debit", valid_from=APRIL),
                Category(name="Job", grouping="Income", spend_type="Credit", valid_from=APRIL),
                Category(name="Other", grouping="Other", spend_type="All", valid_from=APRIL),
                Category(name="Claude", grouping="Regular outgoings", spend_type="Debit",
                         valid_from=APRIL, valid_to=dt.date(2026, 6, 30)),
                Classification(name="Food", legacy_ref=3, direction=1, valid_from=APRIL),
                Classification(name="Excess", legacy_ref=2, direction=-1, valid_from=APRIL),
                DbMeta(id=1, revision=1),
            ]
        )
        s.commit()
        yield s
