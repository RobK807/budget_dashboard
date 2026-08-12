"""The privacy switch, and the rule that keeps the old spreadsheet out of the screen text.

The switch is one boolean in session state, set on Summary and read everywhere. Most of what
it does is presentational, so most of these tests are about the two helpers that do the
hiding -- and two are structural, pinning invariants whose only symptom otherwise is a figure
still on the screen after the switch has been turned on. That is a failure nobody notices
until it matters, which is exactly the kind worth pinning.
"""

from __future__ import annotations

import ast
import pathlib
import re
from decimal import Decimal

import pandas as pd
import plotly.express as px
import pytest
import streamlit as st

from budget import ui

ROOT = pathlib.Path(__file__).resolve().parent.parent
VIEWS = sorted((ROOT / "views").glob("*.py"))


@pytest.fixture(autouse=True)
def privacy_off():
    """Every test starts with the switch off, and leaves it off.

    Session state is process-wide in bare mode, so a test that turns it on would otherwise
    turn it on for everything that ran afterwards.
    """
    st.session_state[ui.PRIVACY_KEY] = False
    yield
    st.session_state[ui.PRIVACY_KEY] = False


class Recorder:
    """Stands in for a column, so ui.metric can be checked without a Streamlit run."""

    def __init__(self):
        self.calls: list[tuple] = []

    def metric(self, label, value, **kwargs):
        self.calls.append((label, value, kwargs))


# ------------------------------------------------------------------------- the flag


class TestTheFlag:
    def test_it_is_off_unless_it_has_been_set(self):
        del st.session_state[ui.PRIVACY_KEY]
        assert ui.private() is False

    def test_it_reads_what_the_switch_wrote(self):
        st.session_state[ui.PRIVACY_KEY] = True
        assert ui.private() is True

    def test_hidden_passes_text_through_until_it_is_on(self):
        assert ui.hidden("£1,000.00") == "£1,000.00"
        st.session_state[ui.PRIVACY_KEY] = True
        assert ui.hidden("£1,000.00") == ui.MASK

    def test_the_switch_does_not_live_on_its_own_widget_key(self):
        """Streamlit drops the session state of a widget it did not render this run, and the
        switch is rendered on one page out of fourteen. Storing the flag under the toggle's
        own key would therefore have turned privacy off on the first navigation -- which is
        the failure this separation exists to prevent, and is invisible in a single-page
        test."""
        assert ui.PRIVACY_KEY != ui._PRIVACY_WIDGET


# ----------------------------------------------------------------------- the metrics


class TestMaskedMetrics:
    def test_a_money_figure_is_replaced(self):
        where = Recorder()
        st.session_state[ui.PRIVACY_KEY] = True
        ui.metric(where, "Savings", "£21,000.00")
        assert where.calls[0][1] == ui.MASK

    def test_a_delta_is_replaced_too(self):
        """The delta on 'Tax paid' is the over/underpayment -- an amount in its own right."""
        where = Recorder()
        st.session_state[ui.PRIVACY_KEY] = True
        ui.metric(where, "Tax paid", "£42,245.88", delta="£1,200.00")
        assert where.calls[0][1] == ui.MASK
        assert where.calls[0][2]["delta"] == ui.MASK

    def test_an_insensitive_figure_survives(self):
        """A count of transactions says nothing about the money behind them."""
        where = Recorder()
        st.session_state[ui.PRIVACY_KEY] = True
        ui.metric(where, "Transactions", "1,842", sensitive=False)
        assert where.calls[0][1] == "1,842"

    def test_nothing_changes_while_the_switch_is_off(self):
        where = Recorder()
        ui.metric(where, "Savings", "£21,000.00", help="Every savings account")
        assert where.calls[0][1] == "£21,000.00"
        assert where.calls[0][2]["help"] == "Every savings account"


# ------------------------------------------------------------------------ the tables


class TestMaskedTables:
    FRAME = pd.DataFrame(
        [
            {"month": "April", "net": Decimal("6543.21"), "payday": 1},
            {"month": "May", "net": Decimal("9876.54"), "payday": 3},
        ]
    )

    def styled(self, **kwargs):
        return ui.money_table(
            self.FRAME, ["net"],
            labels={"month": "Month", "net": "Net", "payday": "Payday"},
            integers=["payday"], **kwargs,
        )

    def test_the_figures_are_gone_from_the_rendered_table(self):
        rendered = self.styled(mask=True).to_html()
        assert "6,543.21" not in rendered
        assert "9,876.54" not in rendered
        assert ui.MASK in rendered

    def test_the_figures_are_gone_from_the_data_as_well(self):
        """st.dataframe sends the frame to the browser beside the display text, so a
        formatter alone would leave the real amounts sitting in the page."""
        underlying = self.styled(mask=True).data
        assert underlying["Net"].tolist() == [ui.MASK, ui.MASK]
        assert "6543.21" not in underlying.to_csv()

    def test_a_whole_number_column_is_masked_too(self):
        """A payday is a date, but it is still a figure in a column the switch covers."""
        assert self.styled(mask=True).data["Payday"].tolist() == [ui.MASK, ui.MASK]

    def test_the_table_keeps_its_shape(self):
        """Masked, not removed: the columns and the rows are what make it recognisable as
        the table it is, and what makes the gap read as deliberate."""
        rendered = self.styled(mask=True).to_html()
        assert "Month" in rendered and "Net" in rendered
        assert "April" in rendered and "May" in rendered

    def test_an_unmasked_table_is_unchanged(self):
        rendered = self.styled().to_html()
        assert "£6,543.21" in rendered
        assert ui.MASK not in rendered


# ------------------------------------------------------------------------ the charts


class TestMaskedCharts:
    def figure(self):
        return px.line(pd.DataFrame({"x": [1, 2], "y": [1000.0, 2000.0]}), x="x", y="y")

    def test_the_value_axis_loses_its_labels(self):
        fig = ui.money_axis(self.figure(), mask=True)
        assert fig.layout.yaxis.showticklabels is False

    def test_hovering_cannot_read_a_point_off_instead(self):
        """Dropping the tick labels alone leaves every point one hover away from being
        legible, which on a chart of net pay is the whole figure rather than a hint of it."""
        assert ui.money_axis(self.figure(), mask=True).layout.hovermode is False

    def test_an_unmasked_chart_keeps_both(self):
        fig = ui.money_axis(self.figure())
        assert fig.layout.yaxis.showticklabels is None  # Plotly's default: shown
        assert fig.layout.yaxis.tickformat == ",.2f"


# -------------------------------------------------------------------- structural pins


def _string_literals(path: pathlib.Path):
    """Every string in a file except the docstrings, with line numbers.

    Docstrings are developer notes and never reach the screen, so they are allowed to
    explain what the code replaced. Everything else is something a reader sees.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


SPREADSHEET = re.compile(
    r"workbook|spreadsheet|tax calculator|the tracker|\bexcel\b|\bxlsm\b|nested IF|"
    r"[A-Za-z]+![A-Z]{1,2}\d|\b[A-Z]{1,2}\d{1,3}:[A-Z]{1,2}\d{1,3}\b",
    re.I,
)


def test_no_page_explains_itself_by_comparing_to_the_spreadsheet():
    """The dashboard should say how it works, not how it improves on what it replaced.

    Cell references were the worst of it -- 'the tracker's L3:N10' means nothing to a reader
    who has never opened the file, and less every year. Pinned rather than merely tidied
    because these arrived one caption at a time, each copied from the section above it.
    """
    offenders = [
        f"{path.relative_to(ROOT)}:{line}: {text[:70]}"
        for path in VIEWS
        for line, text in _string_literals(path)
        if SPREADSHEET.search(text)
    ]
    assert offenders == []


def test_every_money_metric_can_be_masked():
    """A headline figure built with ui.money must go through ui.metric.

    `cols[0].metric(...)` renders the same thing and ignores the switch entirely, so a page
    that uses it leaks its headline row -- and the next page written by copying it inherits
    the leak. Nothing about the output says which spelling was used, which is why this is a
    test and not a review comment.
    """
    offenders = []
    for path in VIEWS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "metric":
                continue
            # ui.metric is the wrapper itself; anything else renders straight to the page.
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "ui":
                continue
            money = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "money"
                for arg in node.args
                for inner in ast.walk(arg)
            )
            if money:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_the_switch_is_created_in_exactly_one_place():
    """Two controls for one flag would disagree the moment either was used, and the sidebar
    indicator is deliberately a statement rather than a second toggle."""
    creators = [
        path.relative_to(ROOT).as_posix()
        for path in VIEWS
        if "ui.privacy_switch()" in path.read_text(encoding="utf-8")
    ]
    assert creators == ["views/summary.py"]


ACCOUNT_NUMBER = re.compile(
    # A bare 8-digit run, or a UK sort code. Both are what a bank export is full of and
    # neither has any business in source: an example needs to be recognisable as invented.
    r"(?<!\d)\d{8}(?!\d)|(?<!\d)\d{2}-\d{2}-\d{2}(?!\d)"
)

# Invented stand-ins used in the bank-import samples. Each is either all-zero after a short
# prefix or an obvious counter, so none of them can be mistaken for a real one.
INVENTED = {
    "12345678", "10000001", "20000002", "30000003", "40000004",
    "00-00-00", "20260805", "20260731",
}

SOURCE_FILES = sorted(
    [p for p in (ROOT / "budget").glob("*.py")]
    + [p for p in (ROOT / "views").glob("*.py")]
    + [p for p in (ROOT / "tests").glob("*.py")]
    + [ROOT / "README.md"]
)


def test_no_real_account_number_is_written_into_the_repository():
    """Bank identifiers belong in the database, never in source, tests or documentation.

    This is here because it already happened: the first version of the bank-import tests
    used samples copied out of real exports, so account numbers and sort codes for six
    accounts went into the repository and were pushed. Source is the one place they cannot
    be taken back from, which is precisely why nothing should put them there.

    Dates in `YYYYMMDD` form are exempt -- one export writes them that way, and the sample
    has to show it.
    """
    offenders = []
    for path in SOURCE_FILES:
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for hit in ACCOUNT_NUMBER.findall(line):
                if hit in INVENTED:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {hit}")
    assert offenders == []
