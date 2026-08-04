# Phase 3 — settings

```bash
streamlit run app.py          # Manage → Settings
python -m pytest tests -q     # 150 passing
python -m budget.reconcile    # gate: green
```

Replaces all six Add/Remove UserForms, the `Selections` sheet and the `Control` tab.
`DeveloperParameters` has no successor — it only ever described the spreadsheet's own
geometry.

Four tabs: **Accounts**, **Categories**, **Classifications**, **General**.

## The gate: adding an account mid-year destroys nothing

Every one of the workbook's six forms opened with the same warning:

> *"This will override all months from X onwards, if there are any existing transactions in
> the months in scope this may cause errors in the spreadsheet."*

That was not caution, it was accurate. An account's position in a month tab **was** its
storage location — offset `position × 4` — so `add_account` inserted columns, rebuilt the
SUMIFS by string surgery, and then `update_months` copied `Monthly_Template` over every
month from the change onwards, wiping whatever was recorded there.

None of that applies now. Adding an account is one `INSERT` with a `valid_from`. Earlier
months are untouched *by construction*, because nothing is stored positionally — the account
simply has no transactions in them.

The gate is a test rather than a claim: record transactions in April and May, add an account
effective June, and assert that every existing transaction is byte-for-byte identical
afterwards. Two further tests confirm the new account is rejected before its start date and
accepted from it.

## Closing rather than deleting

`remove_account` deleted the columns outright, so an account vanished from months it had
genuinely been used in. `remove_category` was worse: it stripped the category from
`Selections` while leaving every historic `Debug` row still naming it, which is exactly how
the `Claude` category came to be lost.

Here **closing** sets `valid_to`. The row stays selectable for the months it existed in and
for historic reporting, and disappears from new entry. Reopening clears the date.

Guards, each with a test:

- cannot close before the last transaction that uses it
- cannot delete anything with references — including from *soft-deleted* transactions, which
  still point at their category
- duplicate names and short codes rejected case-insensitively
- an account cannot be both savings and investment
- `valid_from` cannot postdate the row's earliest existing transaction

Hard delete remains available for something created by mistake and never used.

## Amendments that reach backwards

Two edits change history rather than just the future, so both warn:

- **Renaming** — the reconciliation script matches the workbook by name and will flag it.
- **Changing a classification's direction** — flips the sign of every historic total for that
  classification, not just new ones. Only `Excess` uses −1.

Changing a category's `spend_type` takes effect immediately in validation: set `Other` to
Credit-only and a Debit against it is refused, exactly as `Input!E4` described but never
enforced.

## Read-only while checked out

Reference-data changes require being in sync (DESIGN.md §6.3.2). Transactions are
append-only facts that merge cleanly across machines; the lists both machines *refer to* do
not — two machines independently adding a category would produce two rows the merge cannot
reconcile.

So in deliberate offline mode the whole page is read-only with an explanatory banner, rather
than failing at save time. Every change bumps the revision and triggers the usual auto-push,
so a settings change propagates like any other write.

## Verified

150 tests. Beyond the gate above: validation rules, closing and reopening, the soft-delete
usage guard, `legacy_ref` allocation for new classifications (kept unique so a migrated prior
year still reads back), revision bumping on success and *not* on rejection, and settings
round-trip.

The reconciliation gate still passes — it recomputes from the database, so reference changes
cannot quietly break it.

## Later additions

**Renaming an account** applies to history and future alike, because transactions reference
the account by `id`. Nothing is re-pointed and no history is rewritten — a test asserts
balances are identical before and after. It warns once, since `reconcile.py` matches the
workbook by name and will flag the difference until the workbook is retired.

**"Applies to" is now "Eligible transactions"**, and the categories table sorts by grouping
then name — in the query, so every consumer inherits it.

**Rollover vocabulary**: the workbook's `positive` / `negative` described the *sign* of a
running total, which is ambiguous because what the sign means depends on the
classification's `direction`. They are now `credit` / `debit`, naming the balance type.
`none` and `all` are unchanged. `xlsm_reader.ROLLOVER_MAP` translates on import, so a
re-migration or prior-year backfill lands on the new words.

Excess has `direction = -1`, so a positive Excess total means credits exceeded debits — a
surplus. Hence `positive → credit`, `negative → debit`.

**Excess is now `rollover = all`**, carrying both balance types, with retention applied
separately to a credit balance. See DESIGN.md §6a for the full specification, including the
warning that Phase 4 must apply retention under `all` — the workbook hid it inside the
branch it called "Negative".

**Excess retention** is entered as a whole percentage. It is still *stored* as the
workbook's fraction (100% → 1), so any formula carried over in Phase 4 still matches.

### The first schema migration

Changing that vocabulary meant changing a CHECK constraint, which SQLite cannot ALTER — it
needs the table rebuilt. Rebuilding from the workbook would have worked but reset the sync
revision, forcing a force-push over a master that was already correct.

`budget/schema.py` therefore does it in place: create the replacement table, copy across
with the values mapped, drop, rename. It runs automatically on start, detects whether it is
needed from the existing DDL, and is a no-op on a fresh database.

Applied to the live database with the expected result — `Excess` moved `negative` → `credit`,
738 transactions intact, sync state untouched at rev 1. A pre-migration copy was left at
`%LOCALAPPDATA%\BudgetDashboard\budget.pre-migration.db`; delete it once you are happy.

The NAS master still holds the old word until the next push. That is harmless: each machine
migrates its own copy on open, so a desktop pulling the current master converts it locally.

## Next

Phase 4 — Projected Costs, Salary tracker, Balance Transfer Cards and Cycling. That is also
where the rollover engine arrives, which unlocks the Summary classification matrix
(reconcile check D, currently skipped) and Cumulative Analysis.

Then Phase 5: retire the workbook, and backfill prior years with the same importer.
