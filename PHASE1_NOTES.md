# Phase 1 results — read-only dashboard

```bash
streamlit run app.py          # http://127.0.0.1:8501
python -m budget.reconcile    # the gate: green
python -m pytest tests -q     # 30 passing
```

Three pages, all read-only:

| Page | Replaces | Shows |
|---|---|---|
| **Summary** (`views/summary.py`) | Summary (actuals) | Position at a chosen month, savings/investments over time, net cashflow, classification-by-month matrix, one account across the year, account detail |
| **Month** (`views/month.py`) | A whole month tab | Account balances, budget vs actual by category, daily and cumulative spend by classification |
| **Transactions** (`views/transactions.py`) | `RemoveTransaction` + `TransactList` | Filter by date/account/type/category/classification/comment, CSV export |

Pages are declared with `st.navigation` in `app.py` rather than via a `pages/` directory:
that convention takes each tab's label from the filename, which is why the first tab read
"app". Declaring them explicitly also keeps `set_page_config` in one place, since
`st.navigation` runs the entrypoint and the selected page as a single script run.

Bound to `127.0.0.1` in `.streamlit/config.toml` — this page shows salary, balances and card
debt, so it does not listen on the network.

## The gate: proved, not eyeballed

"Numbers match on screen" is a weak gate if it means looking at them. Instead every figure
the pages display is checked against the workbook by `budget/reconcile.py`, which now runs
five checks:

| | Check | Against | Result |
|---|---|---|---|
| A | Account balances | Month-tab row 61 `End` | 264 account-months, **all tie** |
| B | Daily classification totals | Month-tab `DB:DI` | ~2,700 cells, 1 accepted difference |
| C | Per-category income and spend | Month-tab `C` and `E` | ~490 cells, 4 pending |
| D | Summary classification matrix | — | **skipped**, see below |
| E | Savings / investment position | `Summary!D` and `K` | 8 cells, **all tie** |

Check E covers the Overview's headline metrics, so a mis-imported `is_savings` flag would
fail the build rather than quietly misreport £39k.

Check D is deliberately not run. `Summary!R20` is
`=OFFSET(INDIRECT("xlCloseValue"&LEFT($Q20,3)),0,P$18)` — it reads row 37 of the running
columns, which is the **cumulative closing position**: prior months rolled forward per each
classification's rollover rule, plus projections for dates after today. It is not monthly
actuals. Reproducing it needs the rollover engine and the projection table, both of which
arrive in Phase 4. The Overview therefore shows actual spend per classification per month,
labelled as such.

Check E also skips month-ends in the future, because both Summary formulas are
`=IF(TODAY()>=<month end>, SUM(...), 0)` — comparing against those zeroes would be asserting
the workbook's own placeholder.

## Two things the workbook did that the design had wrong

**Income and spend are not netted.** `New_entry` sends a credit to column C and a debit to
column E:

```vba
If k = 1 Then Selection.Offset(0, 1)             ' C, Income
Else          Selection.Offset(0, intColReduced) ' E, Total Spent
```

So a category with `spend_type = 'All'` — `Other`, `Going Out`, `Band` — accumulates both
independently. Netting them understated June's `Other` spend by over £20,000. The repo now
returns `spent` and `income` separately and check C verifies both.

**`Budget.income` is redundant and should be dropped.** Month-tab column C is not a budget:
it is an accumulator of actual credits, maintained by the macro. Only column D
(`Expected Costs`) is a budget. The Month page therefore derives income from the ledger and
ignores the imported value. Removing the column is a small migration, worth doing before
Phase 2 adds writes.

Related: these columns are macro-maintained accumulators (`ActiveCell = dblAmt + dblCatAmt`),
not formulas, so they can drift from the ledger in a way a formula could not — which is what
findings below are.

## Pending decisions (4 differences, 2 underlying issues)

Both are category attribution only and net to zero on the month totals, so they do not
affect any balance. Reported by every reconciliation run until resolved.

**£75 — txn 532**, 25 Jun, HSBC, "Emma birthday - M&D". Ledger says `Omaze`; the month tab
says `Other`, and its cell comment lists "Emma birthday - M&D 25/06" under `Other`. `Omaze`
is otherwise only a £15/month subscription, so the ledger looks wrong.

**£100 — txn 566**, 30 Jun, Platinum Amex, "Cashback". Ledger says `Other`; the month tab
says `Going Out` per its comment "Cashback 30/06". Note the 27 Jun cashback of £100 is
`Other` in both — so here the *ledger* is the self-consistent reading and the tab looks
wrong. This one runs the opposite way to the £75.

Also still open from Phase 0: the recovered `Claude` category sits in grouping `Other` and
should be reassigned, most likely `Regular outgoings`.

## Observation, not a defect

July shows 15 categories over budget, which is faithful — the workbook's own `Total Left`
column agrees to the penny (Mortgage −1,311.42, Service Charge −1,153.35, Council Tax
−107.50). July genuinely carries double payments: Council Tax £215 against a £107.50 budget,
Water £39 against £19.50. Worth knowing why, but the dashboard is reporting it correctly.

## Transfers split out of paid in / paid out

The workbook's rows 62 and 63 are `=SUM(I4:I59)` and `=SUM(J4:J59)` — the whole Credit and
Debit columns — so transfers between your own accounts are counted as income and spending.
July's HSBC is the clearest case:

| | Workbook | Split out |
|---|---|---|
| Paid in | £20,360.29 | £9,809.25 real, £10,551.04 transfers |
| Paid out | £20,630.29 | £2,842.23 real, £17,788.06 transfers |

More than half of each side was money moving between your own accounts. `account_balances`
now returns `paid_in`, `paid_out`, `transfer_in` and `transfer_out` separately, keeping
`total_in` / `total_out` for the workbook's original definition. Balances are unaffected —
the split only changes how the movement is bucketed, and a test pins that.

## Formatting

`st.column_config.NumberColumn` takes a *printf* format string, and printf has no thousands
separator — hence `£39255.98`. Tables now render through a pandas `Styler` with `{:,.2f}`,
which gives `£39,255.98` while keeping the underlying values numeric so sorting still works
on magnitude rather than on text. The `st.metric` figures were already comma-formatted.

## Ordering and horizon

**Alphabetical, case-insensitively.** Accounts previously came out in the workbook's column
order, which was a storage detail — an account's position determined its column offset
(`position * 4`) and means nothing here. Sorting is case-*insensitive* because a plain
`ORDER BY` / `sorted()` is ASCII, which gives `HSBC, Halifax` and `ISA, Investments`:
correct by codepoint, wrong to a reader. Handled by `func.lower()` in the query and
`ui.alphabetical()` (keyed on `casefold`) for dropdowns.

Months stay in fiscal order — alphabetical would give "April, August, December".

**Nothing beyond the current month.** `repo.periods_to_date()` trims the fiscal year to
months that have started, so with today at 3 August 2026 the UI offers April–August and
hides September onward. The current month is included: it is in progress, not future.

Two details worth keeping: `'YYYY-MM'` strings are zero-padded and fixed width, so a plain
string comparison orders them correctly across the January–March rollover into the next
calendar year; and a year that has not begun falls back to the full list, so the page cannot
end up with no months to select. `data["all_periods"]` retains the full year for the
year-end projections coming in Phase 4.

## Next

Phase 2 — add transaction, bulk import, soft delete. Phase 2b — sync (§6.3). Dropping
`Budget.income` is worth folding into whichever comes first.

See [README.md](README.md) for setting the project up on a second machine — note that until
Phase 2b lands, each machine keeps its own separate database.
