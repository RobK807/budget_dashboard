# Backfilling 2025-26

`python -m budget.backfill_year --workbook "K:\Private\Finance\Budget 25-26.xlsm"`

Both years now reconcile against the same database, and nothing in 2026-27 moved.

## The result

| | 25-26 | 26-27 |
|---|---|---|
| Reconciliation | RECONCILED, 40 accepted differences | RECONCILED, 5 accepted differences |
| Transactions | 1,944 (2 soft-deleted) | unchanged |
| Accounts | 5 created closed, 19 widened back to 1 April 2025 | unchanged |

The last row of that table is the one that mattered. `repo.rolled_forward_openings` anchors
on the *earliest* stored opening per account, so backfilling moves the anchor from April
2026 to April 2025 and every current-year figure becomes the far end of a twelve-month
roll-forward that did not exist before. Comparing every account in every month of 2026-27
before and against the backfilled database: **0 cells moved**.

## What the workbook needed to be told

Nothing here is guessed. Each is confirmed with the user and each self-verifies — a
correction asserts the value it expects to find, so a workbook that has changed since fails
loudly rather than correcting the wrong row.

**Ten ledger corrections** (`CORRECTIONS_25_26`). Three dates a day early on 8 January, a
duplicate HSBC → First Direct transfer entered twice, a £148.40 charge on the wrong card,
three amounts left at zero or mistyped, and row 1929's NY hotel charge at £499.85 rather
than £500.61 — the 76p that stopped Platinum Amex's opening reconciling across the year end.

**The Amex split.** From 24 March 2026 the single 'Amex' column covers two cards. With the
split applied, BA Amex and Platinum land on their stated April 2026 openings to the penny.

**Cards, one at a time** (`CARD_PLAN`). A card is one row describing one borrowing, not a row
per year, so a name in both workbooks may be the same debt seen earlier or a different debt
sharing a lender. MBNA and Tesco extend backwards to where their borrowing really began;
Halifax's earlier tranche becomes its own row; Barclaycard is left alone.

**A band the workbook does not model** (`MISSING_BANDS`). 25-26 stops at 40%, which
understated its own expected PAYE above £125,140. With the real 45% as the only deliberate
difference, eleven of the twelve months reproduce the workbook's expected PAYE exactly.

**The savings plan.** April to July 2025 only — the months the interest tracker does not
reach. From August the tracker wins, at the user's direction; it holds the real per-account
split where Summary column G holds a single lump sum.

## Layout that could not be read by position

Four readers were written against 26-27 and would have imported nonsense from 25-26 rather
than failing. Each rewrite was checked to give byte-identical results on 26-27 first.

| Read by | Would have produced |
|---|---|
| `Summary!Q19:Y31` | a month named `-4123.37` — 25-26 has two credit cards fewer, so the block is at `O19:W31` |
| Salary band rows | a **47,583% additional rate** — 25-26 models no additional rate, so everything below it sits a row higher |
| Card parameters at column P | cards named `'10'` and `'24'`, with terms of 611 and −3,665 months |
| `Credit card bill BoM`, names two rows above | **no statements at all** — 25-26 names its columns nowhere |

A bonus is never a field of its own either. It is welded into whichever cell was convenient,
and the two workbooks chose differently: 26-27 puts it in column P's formula, 25-26 in
column O's (`=126022.4+7854`). Read as a value, 25-26 says May's annual salary was
£133,876.40 — a pay rise that reverses the month after — and leaves a bonus of £7,199.50, a
figure nobody was ever paid.

## The accepted differences, and why

Every one is the ledger being right and the workbook being wrong. They fall into four kinds.

**A row on the wrong month tab.** Row 1796's £85.05 Lottery debit is dated 27 February and
was posted to March. Shows in February and closes again by March.

**The category accumulator drifting one row.** 'Expenses' sits directly above 'Other', and
for a run of entries the macro added an 'Other' amount to the Expenses row. October is the
larger case — every 'Other' debit from the 21st to month end, ten without exception, plus the
four 31 October 'Emma –' credits, £3,053.41 in all. March is the same fault for five entries,
£249.95.

These are accumulators, not formulas: `New_entry` does `ActiveCell = dblAmt + dblCatAmt`, so
nothing recomputes them and the drift is permanent in the workbook.

**A purchase type the month tab records differently.** Seven rows. Every other Coffee in the
year is Excess and every other Waitrose is Food; the other Halifax Lottery entries are Bills
and reconcile; three savings-interest credits carry no purchase type on the tab at all,
though the money itself is there.

**Cumulative views of the above.** A running total carries an accepted difference into every
later month until something cancels it, so the same figures appear again in checks D and F.
Listed month by month rather than derived, so a *new* difference still fails the gate.

Two more are the workbook netting off by hand: `Summary!D6` ends in `-10000` and `D7` in
`-4130`, typed into the formula. That money is really in the accounts, and section A
reconciles both of them in both months.

## One defect the backfill exposed

`account_balances` returned a row for every account whatever the month. With one year that
never showed, because every account ran the whole of it. With two, five accounts closed in
2025 sat at £0.00 in every month of 2026-27. It now omits an account outside its validity
window — but only when it has nothing to show, so a balance can never vanish because a
`valid_to` was typed a month early.

## Adding a further year

1. Add a `Corrections` for the workbook, with its own tax year. The row check makes a wrong
   guess fail rather than corrupt.
2. Add the year to `CORRECTIONS`, and to `ACCEPTED_BY_YEAR` in `reconcile.py` — the
   acceptance list is per workbook, because `(month, key)` is not unique across years.
3. Run `--dry-run` until the report is right. `INCOMPLETE` is the switch to set while a
   section is half-built.
4. Reconcile **both** years against the result. The older one passing proves the import; the
   newer one passing proves the join.
