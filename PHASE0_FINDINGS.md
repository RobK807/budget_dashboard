# Phase 0 results — **gate passed**

All 264 account-months reconcile exactly. One accepted difference remains, where the
database is deliberately more accurate than the workbook (finding 2).

Schema, importer and reconciliation are built and running against
`K:\Private\Finance\Budget 26-27.xlsm`. The workbook is only ever opened read-only.

```
python -m budget.migrate_xlsm --force     # workbook -> SQLite
python -m budget.reconcile                # the acceptance gate
python -m pytest tests -q                 # 18 passing
```

Database: `%LOCALAPPDATA%\BudgetDashboard\budget.db` (deliberately outside the repo).

## What imported

| | |
|---|---|
| Accounts | 22 (**3 detected as credit cards**: Platinum Amex, BA Amex, Mastercard) |
| Categories | 36 + 1 recovered (below) |
| Classifications | 8, with rollover and direction |
| Transactions | 738 — 554 Debit, 103 Transfer, 81 Credit |
| Opening balances | 258 (22 accounts × 12 months, fewer in Apr/May) |
| Budget rows | 435 |

Account type is **derived from the month-tab row-4 formula**, because it is stored
nowhere: `add_account` takes it as a form argument, uses it to choose a formula, and
discards it. `= <start> + <credit> - <debit>` is a bank; `- <credit> + <debit>` is a card.

## Reconciliation

264 account-months and ~2,700 daily classification cells checked.

**All but 4 tie exactly**, and the 4 are three real data problems in the workbook, not
code defects. Both remaining sign conventions were confirmed against the workbook's own
formulas rather than assumed:

- posting signs, from the row-4 balance formulas
- classification totals = `direction × (debits − credits)`, from `DB4`'s trailing `*DB$38`,
  which resolves to `INDEX(xlSelJ, …)`. Only **Excess** has direction −1 — miss this and
  every Excess figure inverts, which is exactly what the first run showed.

### Finding 1 — phantom seed transaction (£10)

`Debug` row 2: First Direct, Electricity, £10, dated 2019-04-01 (corrected to 2026-04-01).

The April tab's First Direct column has five entries on 1 April — 211.07, 26.61, 625.00,
2643.97, 39.00 — and **no £10**. This row is a leftover from the original template: it sits
in the ledger but was never posted to a month tab.

> April / First Direct: workbook 3,600.00 vs computed 3,590.00 (−10.00)

**Resolved — soft-deleted** (`PHANTOM_ROWS` in `xlsm_reader.py`). Imported so the audit
trail survives, but carrying no weight. April / First Direct now ties exactly.

### Finding 2 — the date fix improves on the workbook (£17.09)

`Debug` row 360: BA Amex, Food, £17.09, dated 2029-05-29, corrected to 2026-05-29.

The May *balance* reconciles, so the £17.09 is in the BA Amex column. But at 2029-05-29 the
date falls outside May's daily grid, so the workbook's `MATCH` finds nothing and the amount
contributes **zero** to Food. The workbook has been silently under-reporting May food
spending by £17.09.

> May / Food 2026-05-29: workbook 0.00 vs computed 17.09

**Resolved — registered as an accepted difference** (`ACCEPTED` in `reconcile.py`), so the
gate stays green and a genuine regression would still stand out. Your May food figure is
now £17.09 higher than the spreadsheet ever showed, and correctly so.

### Finding 3 — ledger and month tab disagree (£7.90) — needs a decision

3 July, BA Amex, Travel, classification Excess:

| Source | Amount |
|---|---|
| `Debug` ledger row 592 | **£0.10** |
| July month tab, BA Amex | **£8.00** |

Same date, same account, same position in the sequence (8.50, 8.30, then this one). The two
stores have drifted — most likely a mistyped £0.10 corrected directly in the month tab
without re-running the macro, which updates the tab but never the ledger.

> July / BA Amex: workbook 1,653.27 vs computed 1,645.37 (−7.90)
> July / Excess 2026-07-03: workbook −32.07 vs computed −24.17 (+7.90)

This is the dual-storage drift the single-table design removes: after migration there is
one number, so the two cannot disagree again.

**Resolved — the month tab wins** (`AMOUNT_FIXES` in `xlsm_reader.py`): £0.10 → £8.00.
Both July discrepancies cleared. Still worth a glance at the Amex statement to confirm.

## Also found

**Recovered category `Claude`** — £18/month on Platinum Amex, classified Bills, on the 5th
of April, May and June. Referenced by three transactions but absent from `Selections`:
removed via `remove_category`, which strips the definition while leaving every historic
transaction pointing at the name.

Rather than drop real spending, the importer recreates it effective-dated to its actual
span (2026-04-01 → 2026-06-30) under grouping `Other`. **The grouping is a guess and should
be corrected.** Under the new model this cannot recur — `valid_to` retires a category
without erasing it.

## Corrections applied

All three are enumerated and self-verifying: each asserts the current workbook value before
changing it, so a re-run against a changed workbook fails loudly rather than misapplying.
None is a blanket rule, so the prior-year backfill cannot inherit them.

| Where | Row | Correction |
|---|---|---|
| `DATE_FIXES` | 2, 360, 601 | wrong year → 2026, day and month preserved |
| `AMOUNT_FIXES` | 592 | £0.10 → £8.00, month tab authoritative |
| `PHANTOM_ROWS` | 2 | soft-deleted, template seed row |

## Next

Gate passed. Phase 1 (read-only Overview / Month / Transactions) can start.

One outstanding item for you: the `Claude` category is filed under grouping `Other` and
should be reassigned — most likely `Regular outgoings`.
