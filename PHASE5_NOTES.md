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

---

# The pull that corrupted a database

Worth writing down, because the cause was a single missing `dispose()` and the symptom looked
like disk corruption.

**What happened.** The laptop sat at revision 8 while the desktop had pushed revision 21.
Running `sync.pull()` against the live database left it reporting `malformed database schema
(sqlite_autoindex_txn_1) - orphan index`. The file was provably healthy thirteen seconds
earlier — a copy taken immediately before opens cleanly and passes `integrity_check`.

**Why.** `pull()` opened an Engine to read the local revision and never disposed it. Closing
a `Session` only returns its connection to the *pool*; the engine keeps the file handle and
the WAL open. `pull()` then deletes `-wal`/`-shm` and replaces the database underneath its own
live connection. `_snapshot` and `_integrity_ok` both had `finally: engine.dispose()`. The
pull path was the one that did not, which is exactly the kind of inconsistency worth treating
as a bug report in itself.

Worse, `make_engine` runs `PRAGMA journal_mode=WAL` on connect — a **write to the header**. So
merely asking "what revision is this database at?" modified it, on a file whose WAL state was
about to be interfered with.

**Three fixes.**

1. `_read_counters` reads the revision with plain `sqlite3`, opened `mode=ro`. It writes
   nothing and closes deterministically. An Engine closes on the pool's schedule, and a
   connection that fails during setup can outlive `dispose()` until garbage collection —
   which on Windows turns the following rename into `WinError 32`.
2. `ui.close_connections()` disposes the app's cached engine, and the Sync page calls it
   before any pull. The engine is `@st.cache_resource`, so without this it was still holding
   the old file when the new one landed.
3. An unreadable local database is **moved aside**, never overwritten. A pull is usually
   reached because something has already gone wrong once, which makes the file it replaces
   evidence. And a database still held open is now refused with an explanation rather than
   replaced.

**Behind is not a conflict.** Separately, `Status.conflict` was `nas.revision !=
local.base_revision` — true whenever the master had moved, whether or not this machine had
anything of its own. A clean machine that is simply behind was shown a red *Conflict* banner
and a set of export-and-reimport instructions, with nothing to reconcile. Now `behind` (master
moved, nothing unpushed here → pull, lossless) is distinct from `conflict` (both sides moved →
genuinely needs reconciling), and being behind offers a **Pull now and catch up** button.

`Refresh data` never helped with this, and now says so: it clears cached queries, and the
local database really was a different, older one.

---

# A payslip, decomposed

The salary model held one number where the payslip has five, which is why nothing quite tied
out. Source: `K:\Private\Finance\Tax Calculator v0.3.xlsx`, tab `2026-2027`, `A18:D25` and
`G1:I23`. Rates cross-checked against HMRC — every stored threshold is already correct for
2026/27 and none were changed.

## What the single figure was hiding

| Stored as one | Actually | Check |
|---|---|---|
| `annual_salary` 128,350.25 | base **118,905** + car allowance **9,445.25** | 12% × 50,000 + 5% × 68,905 |
| `benefits` 1,177.88 | pension **990.88** + holiday pay **187.00** | = 1,177.875 |
| `additional` 24 | home working allowance | paid on top, not taxable |

The car allowance is derived, not stored — a pay rise moves it automatically. `base_salary`
is the input; the v5 migration recovers it by inverting the formula, `base = (total − 3,500)
/ 1.05`, which returned 118,905.00 and 116,688.00 exactly. Both round numbers, and the second
matches the Tax Calculator's own April column, which is the check that this is the right
inversion rather than a plausible one.

**Why it has to be split.** The pension is a percentage of base *alone* — the car allowance is
not pensionable. Charged against the combined 128,350.25 it would take £1,069.59 a month
instead of £990.88, and everything downstream would be wrong by the difference.

## Two quantities both called "gross"

`base + car` is **10,695.85**. The payslip says **9,728.97**. The gap is not an error: the
pension is salary sacrifice, so it comes out before gross is stated, and the home working
allowance is inside it.

    payslip gross = base + car + bonus + home working − pension

Verified to the penny for every recorded month, including the bonus one (38,757.45). Both are
now available as `Components.gross` and `Components.payslip_gross`, so the comparison table
puts like against like instead of showing a phantom £966.88 every month.

Taxable is a third quantity again — `base + car + bonus − pension − holiday pay`, excluding
the home working allowance. The workbook says this the roundabout way, in `B25 = B22 − B23 −
B24 − B21`: that last term takes back out the allowance `B22` had just added.

## The higher-rate band overlapped the additional rate

Found by checking May against the real payslip. `income_tax` capped the higher-rate slice at
`higher_threshold` — the point where the *additional* rate starts — rather than at the width
of the higher band. The two differ by exactly the basic band, so any month reaching the
additional rate had £3,141.67 charged at 40% **and** at 45%.

Ordinary months never get there, which is why it survived: the only month it ever bit was the
bonus one.

| May PAYE | |
|---|---|
| Workbook / previous code | 17,452.82 |
| Corrected | **16,196.15** |
| Actual (payslip + bonus) | 16,169.64 |

From £1,283.18 out to £26.51. The thresholds are untouched — this is the arithmetic between
them. `tests/test_tax.py` now carries one case where the workbook is *not* the authority,
because an actual payslip is.

## Where everything now lands

| Month | Payslip gross | NI | PAYE | Net |
|---|---|---|---|---|
| April | exact | exact | −0.21 | −0.21 |
| May | exact | exact | +26.51 | +26.51 |
| June | exact | exact | +0.56 | +0.56 |
| July | exact | exact | +0.56 | +0.56 |

Gross and NI reproduce exactly. The residual PAYE is the tax-code assumption: the model
applies a monthly personal-allowance step where PAYE is really cumulative across the year, so
a month is only ever approximately right on its own. Closing that would mean a cumulative
model — a bigger change, and one that needs the actual tax code rather than a fitted step.

## Parameters

New **Settings → Salary** section: pension rate (10% of base), home working allowance (£24),
holiday pay (£187), and the car allowance threshold and its two rates. Held as plain settings
rather than in `salary_assumption`, deliberately — so editing them cannot disturb a tax
threshold. A month with a recorded payslip uses its actual holiday pay in preference to the
standing figure.

## Schema

Version 5. New column: `salary_profile.base_salary`. `annual_salary` is retained as the
pre-split record and is no longer read.

# The second review

Presentation, mostly — but two of the items turned out to be bugs rather than preferences.

## One dictionary, or none of the formats survive

The Cards page and Settings → Cards were both asked for money formatting they already had in
the code:

```python
ui.money_table(..., ["credit_limit", "opening_balance"]).format({"Minimum %": "{:.2f}"})
```

`Styler.format` clears nothing only when *every* argument is left at its default. Given a
dict, it walks all the columns in the subset — and with no `subset` that is all of them —
assigning a display function to each, falling back to the default formatter for any the dict
does not mention. The second call therefore replaced the first. Both tables lost the pound
sign and the separator, silently, and both were the only two places in the app that chained.

`ui.money_table` now takes `formats=` for the non-money columns, so there is one dict and
nothing to overwrite. `tests/test_refinements.py` asserts the broken spelling stays broken —
if a future pandas makes chaining cumulative, that test says so rather than quietly passing.

The same call now renders missing values as `—`. `£nan` was always wrong and became hard to
miss once expected and actual were shown line for line, where a future month is empty on one
side by definition.

## Holiday pay, counted twice

Putting expected and actual side by side is what exposed it. The comparison table mapped the
stored `benefits` to "Actual — Pension" and read £1,177.88 against an expected £990.88 —
adrift by exactly the £187.00 in the holiday pay column two places to its left.

`benefits` is the old workbook's lumped figure: pension *and* holiday pay. Shown beside its
own holiday pay column it counted that twice. Taking it back out reproduces the expected
pension to the penny in all four recorded months. Nothing stored changed; this was only ever
a display of it.

## A note that said 0

Found while checking the pages rendered. The Projections day-by-day table merged projected
against actual and called `.fillna(Decimal("0"))` on the result, which reached the comment
column too — so a day with no note showed `0`, and the column held Decimals beside strings,
which Arrow cannot type. Streamlit recovered by coercing the column, so it never surfaced as
an error, only as a wrong-looking table. Now only the two amount columns are filled.

## Savings, on three bases

The Savings table had grown to nine columns interleaving total and available. It is six now —
BoM, Added, EoM, monthly target, cumulative target, required — with a **Basis** dropdown
choosing between total, available and reserved. `savings_series` gained `total_required`,
`available_required` and `reserved_required`: one cumulative target measured against each of
the three balances, since what the dropdown switches is which pot is being asked to meet the
target, not the target itself.

'Available' now lives on `account_balances` as an `earmarked` flag rather than being derived
from the account list at each call site, so the Summary chart and the savings tables cannot
disagree about what the word means. NaN is truthy, so the flag is read through a guard — a
bare `bool()` would have earmarked every unflagged pot.

## Added against a cumulative target

Asked for directly, and it forces the other side to accumulate too: a month's £200 set beside
a target that has reached £2,400 by December is not a comparison, it is two quantities an
order of magnitude apart. Both sides now run cumulatively from the first month shown, so a
bar below its target bar is a shortfall not yet made up.

## Charts that stop where the data does

Two of them ran on into empty space. The cycling cumulative saving is built from recorded
days, and days are recorded for the whole year ahead — so the line ran flat out to next
March, squeezing the part where something actually happened. It is cut at today, after the
cumsum, so today's figure is still the running total of everything before it.

The card balance charts started at the oldest card's opening date and spent most of their
width on balances already cleared. They start at the current month, with a **From** dropdown
back to April 2026 for when a card's history is the point.

# The Savings interest tracker

`K:\Private\Finance\Banking\Savings\Savings interest tracker.xlsx`, four sheets of it. None
of it is new *data* — all four are aggregations of a ledger that already holds the underlying
transactions. What was missing was the structure to ask the questions.

## The tax year is not the fiscal year

Everything here is grouped by HMRC's year, 6 April to 5 April. The dashboard's `tax_year_of`
takes a period and so can only ever be right to the month; interest belongs to the day it was
paid, and 1 April is a different tax year from 30 April. Hence `tax_year_of_date` alongside
it, with `TAX_YEAR_START_DAY = 6`.

The tracker could not express the boundary at all, so it split April into two hand-labelled
rows — `Apr (before 6th)` and `Apr (after 6th)` — and filed them under different years by
eye. There are two such payments in the data: 0.16 of Nationwide interest and 2.35 of HSBC's,
both dated 1 April 2026, both belonging to 25-26 rather than to 26-27.

## Interest, gross and net

`account.interest_net`, the tracker's row 3. It held `Gross`/`Net` per account and used it to
decide whether tax should come off again: `IF(D3="Net", D4-D5, D4)`. Everything is gross bar
Halifax, so the flag names the exception and defaults to false.

Interest itself needed nothing: it is already in the ledger under the **Interest** category,
26 payments of it, so the table is a `groupby` rather than a second record. Gross and net are
reported side by side and never summed into one figure — they are not interchangeable at tax
time, which is the entire reason the flag exists.

## A donation and the fee it was paid with

`txn.is_donation`, flagged on the payment rather than inferred from a category, because no
category can tell the two apart. Transaction 582 was a single debit of 35.70 covering both a
30.00 gift and a 5.70 platform fee: same day, same account, same category, same comment.
Only the person entering it knows which is which.

`budget/seed_interest_tracker.py` splits it — 30.00 commented 'Charity' and flagged, 5.70
commented 'transaction fee' and not. Everything else is untouched, and 30.00 + 5.70 leaves
every month total exactly where it was, which is what keeps the reconciliation green.

The flag is offered on **Add transaction**, on **Import** (a `Donation` column, which accepts
`Y`, `TRUE`, `1` or a real tick box), and on the **Transactions** page for anything already
recorded.

## The targets were the totals of a plan nobody could see

The dashboard held 900 a month into savings and 350 into investments. The tracker's
'Savings & investment plan' says where those came from:

    savings      NS&I 250 + Wedding 350 + Marcus 300 = 900
    investments  CSD  250 + HSBC     100            = 350

The same figures — but only because nobody had yet changed one without the other. So the plan
is now the record and the overview is derived from it, the same relationship `base_salary`
has to the car allowance.

`CSD` is Charles Stanley Direct, which is the ledger's **Stocks & Shares ISA**. Proven by the
balance rather than the name: the plan's April 2026 actual of 7,509.80 is that account's
April opening to the penny, and its 1,679.75 is HSBC Investments'.

Effective-dated per account, in five revisions (08/25, 10/25, 03/26, 11/26, 05/27), because
the tracker held one column per revision with the date in the row above — so reading it meant
knowing which column was current. A pot being wound down stores an explicit **0**: from
November 2026 the plan stops paying into Wedding, and leaving the row out would carry the
previous 350 forward.

`savings_target` is retained as the pre-split record and is no longer read.

## Investment return, from what happened rather than what was planned

The tracker's 'Investment Return' tab typed its balances in month by month and *assumed* the
contributions — a fixed 250 and 100 on every row, whatever was really paid. Both are already
in the ledger: a contribution is a transfer into the account, and a valuation change is a
credit or debit commented 'Investment return'. So

    closing = opening + contributions + gain

and the monthly return is `gain / opening`, which is what its
`=IF(C5>0, E5/C4-1, "")` computed — but from the real contributions. The identity balances on
every row of the live data.

The summary reproduces its L3:N10 (start, current, payments, net, return, months,
annualised), measured to *today* rather than to the end of the plan, so months that have not
happened are not counted. `annualised` is its `=(1+M8)^(12/M9)-1`: a fair summary over a year
and a noisy one over a quarter, which the caption says rather than hides.

The table starts at the earliest month in the database, not at a row number, so it extends
when history is backfilled.

The expected annual return (6%) is a setting under **Settings → Savings targets** and is
drawn on the return chart as a benchmark. It converts to a monthly figure by compounding —
`repo.monthly_rate`, the plan's `=(1+L4)^(1/12)-1` — not by dividing by twelve, which would
overstate the month and compound past the rate it started from.

## Not carried over

The 'Savings Account Planner' tab, as asked. The plan's 'Minimum' column is also left out: it
is a floor rather than a dated target, and nothing reads it yet.

## Schema

Version 6. New table `savings_plan`; new columns `account.interest_net` and
`txn.is_donation`. New setting `investment_return_annual`, held as a percentage like every
other rate since v3.

## The guard that could not see a reader

Reported as "the donation has not been split" and "the targets are missing". Both had been
written and both had been verified. They were gone because the database had been corrupted,
and the corruption traces back to `in_use()` — the check the one-off scripts make before
writing.

It tested with `BEGIN EXCLUSIVE`. In WAL mode, which is what this application runs, a writer
and any number of readers coexist *by design*, so an exclusive write transaction is granted
with the dashboard open. An idle Streamlit holds a read connection and nothing else, so the
guard reported the database free and the seed script wrote underneath a live handle. Twice.

The result was one malformed index, `sqlite_autoindex_setting_1` — the primary key on
`setting`. Localised, but enough: `session.get(Setting, key)` goes through it, so every
subsequent run of the seed died mid-transaction and rolled back, taking the 25 plan rows
with it. Nothing announced this; the script's output was truncated at the snapshot line and
the failure looked like the writes had simply not happened.

Proved rather than assumed, with the dashboard running:

    locking_mode=EXCLUSIVE   True    <- correct
    BEGIN EXCLUSIVE          False   <- reported free with the app open

`db.in_use` now takes the file lock outright, which in WAL mode cannot be granted while any
other connection has the shared-memory index mapped — reader or writer. It restores
`locking_mode=NORMAL` on the way out so the check does not itself lock the file. Both
one-off scripts share it rather than carrying a copy each, and `tests/test_sync.py` pins the
old spelling as *not* detecting a reader, so nobody reinstates it.

Recovery was lossless apart from the plan, which the seed rebuilds: `REINDEX` returned
`integrity_check` to `ok`, all 738 transactions were intact, the split and the interest flags
survived, and the repaired file reconciled against the workbook before it was promoted. The
corrupt original is kept as `budget.corrupt-<stamp>.db` rather than discarded.

## Added against target, again

The chart had been made cumulative on both sides so the comparison would be like for like.
But the table's **Added** is a single month, so the bars matched nothing on screen and the
chart quietly disagreed with the numbers printed beside it.

Both scales now coexist on one chart instead: bars are the month's Added against the left
axis, lines are the Cumulative target against the right. Every figure in the chart appears in
the table above it, which is the property that was missing. The investment balance chart
takes a second axis on the same reasoning — one account is four times the size of the others,
so the smaller two were flat lines along the bottom. Which accounts go on the right is a
multiselect, defaulted to those materially below the largest, rather than a hard-coded pair
of names.
