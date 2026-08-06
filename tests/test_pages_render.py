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
