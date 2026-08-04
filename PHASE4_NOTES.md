# Phase 4 — projections, trends, salary, cards, cycling

```bash
python -m budget.import_phase4   # loads Phase 4 data into an existing database
python -m budget.reconcile       # gate: green, all six checks live
python -m pytest tests -q        # 209 passing
```

Five new pages, and the rollover engine underneath them.

| Page | Replaces |
|---|---|
| **Trends** (`views/trends_page.py`) | Cumulative Analysis |
| **Projections** (`views/projections_page.py`) | Projected Costs |
| **Salary** (`views/salary_page.py`) | Salary tracker |
| **Cards** (`views/cards_page.py`) | Balance Transfer Cards |
| **Cycling** (`views/cycling_page.py`) | Cycling |

## The rollover engine

Reproduces the month tabs' `Running <Classification>` columns. Each day:

```
running = carried_forward(previous month's close)
          + (actual daily total, or the projection if the day is in the future)
          + any daily allowance
```

Two reconciliation checks cover it, both live:

| | Check | Result |
|---|---|---|
| **D** | Summary!Q19:Y31 — skipped since Phase 1 | 84 cells, all tie |
| **F** | Month-tab `Running …` columns, 12 months | 84 columns, all tie |

Check D was the last skipped check. Every figure the workbook computes is now reproduced.

## Salary: matches to the penny

`budget/tax.py` is a pure port of the Salary tracker's NI (`S8`) and PAYE (`U8`) formulas,
including the personal allowance tapered in four dated steps. Verified against all twelve
months — ordinary months, the May bonus month that reaches additional rate, and each side of
every allowance step. Zero differences.

`expected_gross` is stored rather than derived: it is **not** `salary / 12`. May carries
its own figure (£39,724 against an annual salary of £128,350), and deriving it produced a
£15,480 phantom gap before I caught it. The remaining £1,284 difference between actual and
modelled net pay is real, and the workbook records the same figure in its own `AB` column.

> **Superseded by Phase 5.** It *is* `salary/12` — plus a bonus, which was welded into May's
> formula as `+29028.48`. With the bonus held as data all twelve months derive exactly. See
> PHASE5_NOTES.md.

## Cycling: matches exactly

£367.20 saved, £102.99 of running costs, £264.21 net — the workbook's `L1` and `V1`. The
nested `IF(commute, 10.5, IF(band, 8.9, IF(gym, 4.6, 0)))` becomes stored flags plus rates
in settings, so changing a fare does not mean editing every historic row.

## Three findings in the workbook

**The Total column omits MBNA 2.** `Balance Transfer Cards` column M sums only four of the
five cards. From June, when MBNA 2 opens, the stated total is short by exactly that card's
balance — £4,198.28 in June, £3,990.99 in August. The per-card columns are right; only the
total is wrong.

**Card terms are counted from April, not from when the card started.** The schedule's payoff
test is `IF($A5 = term, …)`, where `A` is the row index from the start of the whole schedule.
So Halifax, opened in June with a 13-month term, is shown clearing in May 2027 — thirteen
months after *April*, i.e. eleven months after it actually started.

This build measures the term from each card's own opening date, which is what "13-month
balance transfer" normally means, so Halifax clears in July 2027 instead. Barclaycard and
MBNA both start in April and match the workbook exactly (0.00 across all 22 and 24 rows).

**Confirmed: the term runs from the card's own start date**, which is what this build does.
Counting from April was tolerable only because every card's term began before the year did;
it stops being tolerable once prior years are backfilled.

**Outstanding is now measured at a date.** The cards start in different months, so adding
their opening balances totals figures from different points in the year. The page shows the
balance today.

## Also carried across

- **Projections** are keyed by date, so several months coexist rather than the workbook's one
  at a time.
- **Daily allowance** ('Spend per day') is keyed by period and classification instead of
  being applied to whichever column was named Excess.
- **April's opening balance** for Running Excess was a bare `-2632.45` typed into the
  formula; it now has a table.

## Schema

New tables: `projection`, `classification_allowance`, `classification_opening`, `payslip`,
`salary_assumption`, `card`, `cycling_outgoing`, `cycling_day`. Schema version 2 adds
`payslip.expected_gross` to an existing database via `ALTER TABLE` — `create_all` only
creates whole tables, so a new column on an existing one needs an explicit step.

## Remaining

Phase 5 made every parameter on these pages editable — see PHASE5_NOTES.md.
