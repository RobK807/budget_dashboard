"""Layout discovery in the workbook reader.

Backfilling made this worth testing on its own. The reader was written against one
workbook and quietly assumed its geometry; a second workbook with a different number of
columns then read the wrong cells rather than failing, which is the hardest kind of wrong
to notice in a reconciliation whose whole job is to notice things.
"""

import openpyxl
import pytest

from budget import xlsm_reader as xr


def make_summary(card_columns: int) -> openpyxl.Workbook:
    """A Summary sheet shaped like the real one: a credit card table, then the
    month-by-classification block to its right. The card count is what moves the block --
    25-26 has three cards where 26-27 has five, which shifts it two columns left."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"

    col = 9  # I, where the card table starts in both workbooks
    ws.cell(19, col, "Month")
    for i in range(card_columns):
        ws.cell(19, col + 1 + i, f"Card {i + 1}")
    ws.cell(19, col + 1 + card_columns, "Payment")
    for i, month in enumerate(xr.FISCAL_MONTHS):
        ws.cell(20 + i, col, month)

    col += card_columns + 3
    ws.cell(19, col, "Month")
    for i, name in enumerate(["Bills", "Excess", "Food", "Spend"]):
        ws.cell(19, col + 1 + i, name)
    for i, month in enumerate(xr.FISCAL_MONTHS):
        ws.cell(20 + i, col, month)
        ws.cell(20 + i, col + 1, 100 + i)

    return wb, col


@pytest.fixture
def ref():
    return xr.RefData(
        accounts=[],
        categories=[],
        classifications=[
            xr.ClassificationRef(name=n, legacy_ref=i, direction=1, display_order=i)
            for i, n in enumerate(["Bills", "Excess", "Food"])
        ],
        months={m: (i + 4 - 1) % 12 + 1 for i, m in enumerate(xr.FISCAL_MONTHS)},
        settings={"tax_year": "2025"},
    )


@pytest.mark.parametrize("cards", [3, 5])
def test_finds_the_block_wherever_the_card_table_leaves_it(ref, cards):
    wb, expected_col = make_summary(cards)

    month_col, headers, rows = xr.summary_matrix(wb, ref)

    assert month_col == expected_col
    assert list(headers.values()) == ["Bills", "Excess", "Food", "Spend"]
    assert list(rows) == list(range(20, 32))


def test_skips_the_credit_card_table(ref):
    """Both tables head their first column 'Month'. Picking the left one is what read a
    card balance as a month name and asked period_for() about '-4123.37'."""
    wb, expected_col = make_summary(3)

    month_col, _, _ = xr.summary_matrix(wb, ref)

    assert month_col != 9
    assert month_col == expected_col


def test_says_so_when_there_is_no_block(ref):
    wb = openpyxl.Workbook()
    wb.active.title = "Summary"

    with pytest.raises(ValueError, match="no month-by-classification block"):
        xr.summary_matrix(wb, ref)
