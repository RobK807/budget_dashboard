"""Money is stored as integer pence so that SUM() stays exact. Every balance in this
application is a sum, and the workbook already demonstrates what float storage does to
one (1530.0000000000146, 49.67999999999853)."""

from decimal import Decimal

import pytest
from sqlalchemy import Column, Integer, select
from sqlalchemy.orm import Session

from budget.db import create_all, make_engine, make_session_factory
from budget.models import Base, Money


class Amount(Base):
    __tablename__ = "_test_amount"
    id = Column(Integer, primary_key=True)
    value = Column(Money)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    create_all(engine)
    with make_session_factory(engine)() as s:
        yield s


@pytest.mark.parametrize(
    "value",
    [Decimal("0.00"), Decimal("0.01"), Decimal("-0.01"), Decimal("1234.56"), Decimal("-99.99")],
)
def test_round_trips_exactly(session, value):
    session.add(Amount(value=value))
    session.commit()
    assert session.scalars(select(Amount.value)).one() == value


def test_float_noise_from_the_workbook_is_rounded_to_pence(session):
    session.add(Amount(value=1530.0000000000146))
    session.commit()
    assert session.scalars(select(Amount.value)).one() == Decimal("1530.00")


def test_sums_are_exact_where_floats_would_drift(session):
    # 0.1 + 0.2 != 0.3 in binary floating point.
    session.add_all([Amount(value=Decimal("0.10")), Amount(value=Decimal("0.20"))])
    session.commit()
    total = sum(session.scalars(select(Amount.value)), Decimal("0"))
    assert total == Decimal("0.30")


def test_null_survives(session):
    session.add(Amount(value=None))
    session.commit()
    assert session.scalars(select(Amount.value)).one() is None
