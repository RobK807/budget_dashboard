# Phase 5 — refinements

```bash
python -m budget.import_phase5   # seeds the new tables from the workbook
python -m budget.reconcile       # gate: green, all six checks
python -m pytest tests -q        # 354 passing
```

> A second round of refinements followed a full review of the dashboard. They are at the end
> of this file, under [After the review](#after-the-review).

Twenty-one refinements across the Salary, Cards, Cycling, Projections, Month and Summary
pages, one new page, and five new Settings sections.

The theme underneath them is the same one every phase has had: **things the workbook held as
constants inside formulas become data.** The annual salary was typed into all twelve rows of
column O. The bonus was `+29028.48` welded into the middle of May's expected-gross formula.
The cycling fares were a nested `IF` in a column. The card terms, the statement days, the
savings targets — all cells with no way in but the cell itself.

## What changed

| | Page | Was |
|---|---|---|
| 1–2 | **Salary** | Holiday pay, benefits and additional pay shown; the actual side is now enterable, for future months too |
| 3 | **Salary** | Gross salary by effective date and bonus by month, which together derive expected gross |
| 4–6 | **Salary** | Bands, rates and allowance steps editable per tax year; rates as whole percentages |
| 7 | **Salary** | The net-salary-to-daily-allowance calculation |
| 8–10 | **Cards** | Minimum % to 2dp; next payment rather than first; total-payment chart segmented by card |
| 11–12 | **Settings → Cards** | Every card parameter, and adding a card |
| 13 | **Settings → Cycling** | Fares with effective dates |
| 14 | **Record → Cycling** | Enter a ride or a running cost |
| 15 | **Cycling** | Days ridden over a chosen date range |
| 17 | **Projections** | Enter a month's projections, Import-style |
| 18 | **Settings → Monthly figures** | Daily allowance and category targets, by month |
| 19 | **Month** | Credit cards outstanding |
| 20 | **Summary** | Account targets against balances |
| 21 | **Savings and investments** | New page — the Summary tab's two headline tables |

## Everything still ties to the workbook

Verified against the workbook's own figures before anything was wired up:

| Check | Result |
|---|---|
| Expected gross, all 12 months | Exact, including May's £39,724.33 bonus month |
| NI and PAYE, all months | Zero difference — the rate rescale changed nothing |
| Cycling total saved | £367.20, the workbook's `L1` |
| Credit card outstanding, July | −£3,400.79 / £75.73 / £272.67, the workbook's C52:E52 |
| Account targets, July | Halifax, HSBC, First Direct and Nationwide all exact |
| Savings and investments | Month-end available: £10,603.72 / £12,601.56 / £13,673.02 / £12,290.58 |
| Spending calculation | Card limit £1,189.74, monthly £639.74, daily £21.32 |

## Three findings

**'Less SC & Wed' had stopped meaning what it said.** The Summary tab's savings column
subtracted a hand-typed 2,819.77 from the total. That figure covered Service Charge and
Wedding in April — but from June it also covered **Tembo**, which had joined at £17,536 and
was silently included in the subtraction while the heading stayed the same. Confirmed by
solving for the excluded set in all four months: only `{Service Charge, Wedding, Tembo}`
reproduces June and July. The accounts are now flagged, so the column follows when a pot is
added and the label cannot go stale.

**The opening row of that table used the wrong month's exclusions.** `E3` is the year's
opening savings less 2,819.77 — but 2,819.77 is April's *closing* balance of the earmarked
pots, not its opening. At the opening they came to 2,263.87. Every month-end row ties
exactly; only the opening row is out, by £556. The build subtracts each row's own balances,
so it reports £10,345.53 where the workbook says £9,789.63.

**Expected gross *is* salary/12 after all.** Phase 4 concluded it was not, because May's
figure could not be reproduced. It could not be reproduced because the bonus was inside the
formula. With `salary_profile` and `bonus` as tables, all twelve months derive exactly, and
`payslip.expected_gross` is no longer the source of truth — it is kept only as what the
workbook stated.

## Rates are stored as percentages now

`salary_assumption.value` holds 8.00 for 8%, not 0.08. The column is integer pence, so a
fraction could only ever express whole percentage points — 8.5% would have rounded to 9%.
Nothing in the data needed the precision, which is exactly why it was worth fixing before
something did.

The migration multiplies the five rate rows by 100, once, guarded by the stored schema
version rather than by inspecting the values: 0.2 and 20 are both plausible rates, so there
is no way to tell from a number alone whether it has already been converted, and running it
twice would turn 20% into 2000%.

## The basic band is derived

The workbook's `D36` is `=D28-D22` — the basic rate threshold less the personal allowance.
Phase 4 stored only the result. Both inputs are now stored and the band is computed, because
storing all three would let them drift apart the moment one was edited.

## Schema

Version 3. New tables: `salary_profile`, `bonus`, `cycling_rate`, `card_statement`,
`account_target`, `savings_target`. New columns: `account.exclude_from_savings`,
`account.statement_day`, `account.payment_day`, `card.credit_limit`.

## A new kind of test

`tests/test_pages_render.py` runs all fourteen pages through Streamlit's `AppTest` and fails
on any uncaught exception. Pages are scripts, so until now a typo in one only showed up when
someone opened it.

It runs against a *copy* of the database, not the real one — opening a page calls
`create_all`, which applies pending migrations, so pointing the tests at the live file would
have quietly migrated it as a side effect of running the suite. It did exactly that once
before the fixture was fixed.

## Note

`import_phase5` has seeded your live database and there is a **push pending** on the Sync
page. The previous backup is at `budget.pre-phase5.db`.

**Credit limits are not imported.** The workbook's 'Total available' column is a derived
figure that goes negative (`=9000-C4/1.033`), not a limit. All five cards need one entered
under **Settings → Cards**.

## Remaining

Backfill prior years (`Budget 25-26.xlsm` and earlier) with the same importer, which is what
turns Trends into genuine multi-year analysis — and makes the tax-year selector on the Salary
page useful rather than a list of one.

---

# After the review

A read-through of the whole dashboard produced a second list. Most of it is presentation, but
four items were substantive, and three of those were the same mistake in different clothes:
**something that was true of a workbook had been carried across as though it were true of the
database.**

## Nothing is scoped to one fiscal year any more

Every month dropdown was `repo.fiscal_periods(tax_year)` — April 2026 to March 2027, and no
further. That is one file's worth of months. Backfilling an earlier year would have loaded
rows that no dropdown could reach, and next April every list would have needed the setting
changed by hand.

The range is derived instead: from the first month anything is recorded against, through the
current month, plus a look-forward for the lists that plan ahead. `look_forward_months` is a
setting (default twelve). Trends also exposes it as a slider, because its balances are
cumulative — six months ahead and eighteen are different calculations, not the same one
truncated. `fiscal_periods` remains, used only by the reconciliation gate, which compares
against a workbook and so genuinely is scoped to a year.

## The credit-card outstanding figure was wrong

Reported: July's Platinum Amex showed a balance of £49.68 and an outstanding of **−£3,400.79**.

The old rule decided which bill was standing from the day of the month, which is all a month
tab could see. It has no way to express "collected on the 30th", so at a July month end it
deducted July's bill from every card — including the one that had already paid it.

The cycle is two dates. Where the payment day is the *smaller* number, the bill is collected
the following month:

| Card | Statement | Payment | Bill standing at 31 July |
|---|---|---|---|
| Platinum Amex | 16th | 30th | none — collected on the 30th |
| BA Amex | 26th | 9th | July's, until 9 August |
| Mastercard | 12th | 6th | July's, until 6 August |

So outstanding is the balance before a statement is issued, the balance less that bill while
it awaits collection, and the balance again once it has gone. July now reads £49.68 / £75.73 /
£272.67 — the last two still matching the workbook's own D52, which happened to be right for
the two cards whose cycle crossed a month.

The table also lost its duplicate column. **This month's bill** is the statement figure entered
for the month; **awaiting payment** is whichever bill is actually standing, which for a card
collected the following month is the previous month's. They looked like duplicates because for
half the year they are.

Bills can be entered as they arrive: a blank stays blank rather than being stored as zero, and
saving writes only what is filled in.

## A month can hold two payments

`payslip` is keyed by period, so a bonus paid on its own day had nowhere to go — entering it
overwrote the salary. `bonus` now carries its own actual gross, NI, PAYE, net and payday, and
the comparison table adds the two together, with an 'of which bonus' column. Both a bonus and
a payslip can be removed outright, for when the wrong month was filled in.

The alternative was a surrogate key on `payslip`, which would have turned every lookup into a
group-by for the sake of one case a year.

## Tax bands are effective-dated for real

`salary_assumption` always had `effective_from` in its primary key, but only the allowance
taper used it: everything else was written at 1 April and read back without reference to a
date. `repo.bands_from(assumptions, on)` takes the last set starting on or before `on`, and
each month is now taxed under the bands of *its own* tax year at the values in force on its
first day — which matters as soon as the month lists run past a fiscal year end.

The editors write against a chosen effective date, so a mid-year change is a new set rather
than an edit that silently rewrites what earlier months were taxed at. The existing figures
keep 01/04/2026. Underneath, read-only tables show the set as at any stored date, and an
expander lists every stored figure by effective date.

## Presentation

- **Money carries a thousands separator everywhere, including editable cells.** `st.dataframe`
  had it via a Styler; every `st.data_editor` did not, because printf has no thousands flag —
  £39255.98. Streamlit parses column formats with sprintf-js, which treats `,` as one, so
  `"£%,.2f"` works where `"£%.2f"` did not. Chart axes get `,.2f` too: Plotly's default is
  SI-prefixed, which drew £10,000 as `10.00000k`.
- **Percentages are quoted, not fractional, to two places** — 80.00, not 0.8 and not 80.
- **Date filters open on the last ninety days** rather than on the full extent of the data, so
  the default does not widen as history accumulates.
- **`nan` is named.** A transfer carries no category or classification, so those cells are
  genuinely empty and pandas rendered them `nan`. The picker on the Transactions page was worse:
  it read `category or type`, and a missing category arrives as float NaN, which is *truthy* —
  so every transfer was labelled `nan` rather than `Transfer`.
- **Settings sections are in alphabetical order**, like every other list in the app.
- **Summary is reordered** to position, accounts, spend by classification, account by month,
  charts, targets.
- **The remove-a-transaction list has its own date, account and comment filters**, since
  picking the right row out of five hundred is the hard part.
- **The projection grid opens pre-populated** with every day of the month against every
  classification, is filterable by classification, and can copy a previous month across — bills
  that repeat on the same date need entering once. Saving only touches the classifications on
  screen, so a filtered view cannot delete what it is hiding, and a day left at zero is not
  stored.
- **Savings and investments** now show BoM and EoM side by side, with a cumulative target
  column and a `Required` that is the cumulative target less what is available. The opening row
  is gone: with both ends of the month in one row it was saying the same thing twice.

## Schema

Version 4. New columns: `bonus.gross`, `bonus.ni`, `bonus.paye`, `bonus.net`, `bonus.payday`.
All nullable and additive; the migration is an `ALTER TABLE` per column.
