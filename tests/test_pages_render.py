"""Every page renders against a real database without raising.

Streamlit pages are scripts, so a typo in one only shows up when it is opened. These run each
page top to bottom through AppTest and fail on any uncaught exception -- which is what a user
would otherwise see as a stack trace in the browser.

Skipped when there is no database to point at: the pages read the real schema, and a fixture
elaborate enough to drive twelve pages would be testing the fixture rather than the pages.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from budget import config

pytest.importorskip("streamlit.testing.v1")
from streamlit.testing.v1 import AppTest  # noqa: E402

PAGES = [
    "summary.py",
    "month.py",
    "transactions.py",
    "trends_page.py",
    "projections_page.py",
    "savings_page.py",
    "salary_page.py",
    "pension_page.py",
    "cards_page.py",
    "cycling_page.py",
    "add.py",
    "import_page.py",
    "cycling_record.py",
    "settings_page.py",
    "sync_page.py",
]

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def database_copy(tmp_path_factory):
    """Run against a copy, never the real database.

    Opening a page calls create_all, which applies pending schema migrations -- so pointing
    these at the live file would quietly migrate it as a side effect of running the tests.
    Patched at module scope because ui._session_factory is a cache_resource: the first page
    to run fixes the engine for the whole process.
    """
    if not config.DB_PATH.exists():
        pytest.skip(f"no database at {config.DB_PATH}")

    patch = pytest.MonkeyPatch()
    copy = tmp_path_factory.mktemp("render") / "budget.db"
    shutil.copy(config.DB_PATH, copy)
    patch.setattr(config, "DB_PATH", copy)
    yield copy
    patch.undo()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page):
    app = AppTest.from_file(str(ROOT / "views" / page), default_timeout=90)
    app.run()
    assert not app.exception, (
        f"{page} raised: "
        + "; ".join(f"{e.type}: {e.message}" for e in app.exception)
    )


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_with_privacy_on(page):
    """The same sweep with the privacy switch set.

    It is a second code path, not a stylesheet: Salary withholds five entry forms and takes
    the variables they define with them, and every masked table and chart is a different
    call. Half of that page is only ever executed with the switch on, so without this it is
    only ever executed by the person who turned it on.
    """
    from budget import ui

    app = AppTest.from_file(str(ROOT / "views" / page), default_timeout=90)
    app.session_state[ui.PRIVACY_KEY] = True
    app.run()
    assert not app.exception, (
        f"{page} raised with privacy on: "
        + "; ".join(f"{e.type}: {e.message}" for e in app.exception)
    )


def test_privacy_actually_reaches_the_salary_page():
    """The sweep above only proves nothing raised, which a switch that did nothing would
    also satisfy. This is the one that says it worked: the headline amounts are masked, the
    count beside them is not, and the entry forms have been withheld."""
    from budget import ui

    app = AppTest.from_file(str(ROOT / "views" / "salary_page.py"), default_timeout=90)
    app.session_state[ui.PRIVACY_KEY] = True
    app.run()
    assert not app.exception

    figures = {m.label: m.value for m in app.metric}
    assert figures["Gross to date"] == ui.MASK
    assert figures["Net to date"] == ui.MASK
    # A count of payslips is not an amount, and reads as '4 of 8'.
    assert ui.MASK not in figures["Payslips received"]

    withheld = sum("Held back while privacy is on" in c.value for c in app.caption)
    assert withheld >= 5, f"only {withheld} form(s) withheld"


def test_the_pension_page_draws_once_it_has_something_to_draw(database_copy):
    """The sweep above only proves the empty page renders.

    Every chart, pivot and return on that page is inside the branch taken when there *are*
    valuations, so without this the whole of it is only ever executed by whoever has data.
    Two dates and a payment is the smallest case that exercises all of it: a carried-forward
    pot, a period return with a contribution deducted, and a total across two pots.
    """
    import datetime as dt
    from decimal import Decimal

    from budget.db import make_engine, make_session_factory
    from budget.models import PensionContribution, PensionPot, PensionValuation

    engine = make_engine(database_copy)
    try:
        with make_session_factory(engine)() as session, session.begin():
            if session.query(PensionPot).count() == 0:
                start, later = dt.date(2024, 1, 1), dt.date(2024, 7, 1)
                frozen = PensionPot(name="Pot one", valid_from=start, display_order=1)
                active = PensionPot(name="Pot two", valid_from=start, display_order=2)
                session.add_all([frozen, active])
                session.flush()
                session.add_all(
                    [
                        PensionValuation(
                            pot_id=frozen.id, on_date=start, value=Decimal("1000")
                        ),
                        PensionValuation(
                            pot_id=active.id, on_date=start, value=Decimal("500")
                        ),
                        # Only one pot is valued on the second date, so the page has to
                        # carry the other forward and say that it did.
                        PensionValuation(
                            pot_id=active.id, on_date=later, value=Decimal("700")
                        ),
                        PensionContribution(
                            pot_id=active.id, on_date=dt.date(2024, 4, 1),
                            amount=Decimal("100"), kind="contribution",
                        ),
                        PensionContribution(
                            pot_id=active.id, on_date=dt.date(2024, 5, 1),
                            amount=Decimal("-2"), kind="charge",
                        ),
                    ]
                )
    finally:
        engine.dispose()

    from budget import ui

    ui.load_all.clear()
    app = AppTest.from_file(str(ROOT / "views" / "pension_page.py"), default_timeout=90)
    app.run()
    assert not app.exception, "; ".join(
        f"{e.type}: {e.message}" for e in app.exception
    )

    figures = {m.label: m.value for m in app.metric}
    assert figures["Total value"] == "£1,700.00"
    assert figures["Paid in"] == "£1,598.00"
    assert figures["Growth"] == "£102.00"

    # And the same page with the switch on: the amounts go, the percentage stays.
    app = AppTest.from_file(str(ROOT / "views" / "pension_page.py"), default_timeout=90)
    app.session_state[ui.PRIVACY_KEY] = True
    app.run()
    assert not app.exception
    masked = {m.label: m.value for m in app.metric}
    assert masked["Total value"] == ui.MASK
    assert masked["Growth"] == ui.MASK
    assert ui.MASK not in masked["Return to date"]
    ui.load_all.clear()


def test_no_page_uses_the_retired_width_flag():
    """`use_container_width` was removed from Streamlit after 2025-12-31.

    Every call site now passes `width="stretch"` instead. Pinned because the replacement is
    not equivalent everywhere -- st.button defaults to "content", so dropping the argument
    rather than translating it would silently shrink a full-width button -- and because the
    only symptom of a new page reintroducing it is a warning in a console window nobody is
    looking at.
    """
    offenders = [
        path.relative_to(ROOT)
        for path in list((ROOT / "views").rglob("*.py")) + [ROOT / "budget" / "ui.py"]
        if "use_container_width" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_every_sync_action_reports_through_finish():
    """A sync action must not write its result and then rerun.

    Each of these changes the state the page is drawn from, so each has to redraw or the
    banner keeps describing the position before the button was pressed -- which is how a
    pull that had worked went on reporting a conflict it had already resolved. But
    st.rerun() discards whatever was written in the same run, so writing the message first
    threw it away: a push reporting 'promoted; previous master kept as budget.db.bak' showed
    that to nobody, and a *refused* push lost both the reason and the remedy.

    `finish` stashes the outcome and reruns; `show_outcome` renders it on the far side.
    Pinned structurally because the failure is invisible -- the page looks right, it is
    simply silent, and the next action added by copying its neighbour inherits it.
    """
    import re

    source = (ROOT / "views" / "sync_page.py").read_text(encoding="utf-8")
    actions = list(
        re.finditer(r"result = sync\.(push|pull|force_take|checkin|checkout)\(", source)
    )
    assert actions, "no sync actions found -- has the page been restructured?"

    for action in actions:
        following = source[action.end(): action.end() + 400]
        verb = action.group(1)
        assert "finish(result" in following, f"sync.{verb} does not report through finish()"
        # And not the old shape, which finish() exists to replace.
        before_finish = following[: following.index("finish(result")]
        assert "st.rerun()" not in before_finish, f"sync.{verb} reruns before reporting"
        assert "st.success(" not in before_finish, f"sync.{verb} writes a message it discards"


def test_the_summary_renders_the_commitments_it_is_given(database_copy):
    """The itemised commitments, against a database that actually has some.

    The sweep above proves the Summary page does not raise, but the database it runs on has
    no commitments in it -- so the branch it takes is the 'none listed' one, and the table,
    the running total and the two warnings are never executed by any test. This seeds a few
    into the copy and looks for them on the page.

    The rows are deliberately awkward: a 31st, so the clamp runs on whatever month the page
    opens on, and a zero amount, so the 'no amount yet' warning fires.
    """
    import datetime as dt
    from decimal import Decimal

    from sqlalchemy import select

    from budget import reference
    from budget.db import create_all, make_engine, make_session_factory
    from budget.models import Account

    engine = make_engine(database_copy)
    create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session, session.begin():
            account = session.scalars(
                select(Account).where(Account.type == "bank").order_by(Account.id)
            ).first()
            assert account is not None, "no bank account to hang a commitment on"
            opened = account.valid_from or dt.date(2020, 1, 1)
            for name, amount, day in (
                ("Rent for the test", "1234.56", 31),
                ("Amount not set", "0", 4),
            ):
                reference.set_account_commitment(
                    session, account.id, name, Decimal(amount), day
                )
    finally:
        engine.dispose()

    app = AppTest.from_file(str(ROOT / "views" / "summary.py"), default_timeout=90)
    app.run()
    assert not app.exception, "; ".join(
        f"{e.type}: {e.message}" for e in app.exception
    )

    # The page opens on the latest month with transactions, which is after the account was
    # opened in any real database -- so the table is expected, not merely tolerated.
    selected = app.session_state["summary_period"]
    assert selected >= f"{opened:%Y-%m}", (
        f"the page opened on {selected}, before the account existed -- this test needs a "
        "commitment on an account that is live in the month the page shows"
    )

    listed = [
        frame.value for frame in app.dataframe
        if {"Item", "Still needed"} <= set(getattr(frame.value, "columns", []))
    ]
    assert len(listed) == 1, "the itemised commitments table did not render"
    table = listed[0]
    assert "Rent for the test" in set(table["Item"])

    # The running total must reach the row: the clamp on a 31st is only exercised if the
    # row survives as far as the table.
    rent = table[table["Item"] == "Rent for the test"].iloc[0]
    assert float(rent["Amount"]) == pytest.approx(1234.56)

    warnings = " ".join(str(getattr(element, "value", "")) for element in app.warning)
    assert "Amount not set" in warnings, "the zero-amount warning did not fire"
