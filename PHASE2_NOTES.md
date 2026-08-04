# Phase 2 — writes

```bash
streamlit run app.py          # Add transaction, Import, and delete/restore on Transactions
python -m budget.reconcile    # the gate: green
python -m pytest tests -q     # 85 passing
```

Navigation is now grouped: **Review** (Summary, Month, Transactions) and **Record**
(Add transaction, Import).

| Page | Replaces |
|---|---|
| **Add transaction** (`views/add.py`) | The Input tab and `New_entry` |
| **Import** (`views/import_page.py`) | The BulkImport tab and `bulk_upload` |
| **Transactions** — remove/restore | `RemoveTransaction` + `TransactList` + `remove_transaction` |

## The period is derived, not chosen

The workbook asked for the month as a separate field from the date, which is how three rows
came to sit under the wrong month — one dated 2029 filed under May, invisible in the daily
totals for years because `MATCH` found no day to post it against.

`validation.period_for()` derives the period from the date. The class of error is gone
rather than guarded against.

## Validation is enforced rather than displayed

`Input!E4:E10` held messages like "Include account to", "Update to credit" and
"Delete comment" — advisory text in a cell that nothing acted on. All are now hard errors:

- a transfer needs a destination, and two *different* accounts
- only transfers may have a destination
- a Credit-only category rejects a Debit, and vice versa (from `Selections!AO`)
- a category comment needs a category
- an amount must be positive; direction comes from the type

Plus rules the workbook had no way to express, because effective dating did not exist:

- an account cannot be used before it opened or after it closed
- a category cannot be used outside its valid span — so the recovered `Claude` category
  works for April–June and is rejected from July

Warnings that inform without blocking: no category, no purchase type, a future date, a date
more than a year old.

## Imports are all-or-nothing, and undoable

`bulk_upload` looped row by row calling `New_entry`, so a bad row midway through left the
first half imported, the rest not, and no record of where it stopped. Here every row is
validated first; if any fails, nothing is written and the failures are listed by row number.

Each import records an `import_batch`, so it can be undone in one click. The **initial
migration batch is protected** — it holds the whole imported history, and undoing it would
soft-delete all 738 transactions from one mis-click. Rebuild with `migrate_xlsm --force`
instead. Other batches require a confirmation checkbox.

Column headings are matched loosely, so a block pasted straight out of the old BulkImport
tab works unchanged — including its `Item` and `Month` columns, which are ignored. Dates are
read day-first, and `0` is treated as empty in text fields, which is what the workbook wrote
into unused optional cells.

## The import grid uses the same pickers as Add transaction

Dates are a date picker, amounts a numeric field, and Type / Account From / Account To /
Category / Purchase type are dropdowns fed from the same configured lists as the Add
transaction page. The template frame carries real dtypes so `st.data_editor` can offer those
widgets rather than free text — a blank `object` column renders as text and lets a typo
through to validation instead of preventing it.

Pasting and CSV upload still accept loose text, since a block copied out of the old workbook
will not match the dropdowns exactly.

## Balance check (BulkImport!CK3:CQ32)

The workbook's verification carried over: enter each account's real balance and confirm that
its current position plus the import's net effect lands on it. It catches a transaction
entered twice, one with the wrong amount or direction, and — because every account is listed,
not just the ones the import touches — one left out altogether.

| Column | Meaning |
|---|---|
| Current | The account's balance now, before the import |
| In / Out | Money arriving / leaving, by direction |
| Projected | Current plus the import's net signed effect |
| Target | The real balance, typed in; blank skips the account |

The workbook implemented this as SUMIFS pairs, **negated for the three credit-card rows**
because a card balance is positive debt. Here the sign comes from `Posting.signed()` — the
same rule the balances and the reconciliation gate already use — so it cannot drift from
them. In and Out follow the direction money actually moved, which reads naturally for a
current account; Net applies the signed rule, so card spending correctly increases the
balance owed even though it appears under Out.

Verified against real data: BA Amex £1,653.27 + £25 spend − £100 payment = £1,578.27; HSBC
£1,530.00 − £100 transfer out + £500 income = £1,930.00. Both Current figures match the
workbook's own CM column.

Mismatches warn but do not block — you may legitimately not have a target for every account.

**Targets survive filtering.** `st.data_editor` keys its edits by row *position*, so
toggling "show only affected accounts" would otherwise discard what had been typed — or
worse, replay those edits onto a different set of accounts. Targets are therefore held per
account in session state and re-seeded on every render, and the editor's key is tied to the
exact rows on screen so a changed row set builds a fresh editor rather than reusing stale
positional edits. Entering a value in either view and switching to the other keeps it, in
both directions.

The pass/fail summary is evaluated against **every** stored target, not just the visible
rows, so narrowing the filter hides a row without hiding a mismatch. `seed_targets` and
`capture_targets` are pure functions in `importer.py` precisely so this round-trip is
testable without a Streamlit runtime — the UI itself cannot be automated (see below).

## Deletion stays soft

`remove_transaction` cleared cells, copied every row below up by one, adjusted the parallel
identifier and category columns, and stripped a substring out of a cell comment by searching
for its text — which corrupted the comment if the text appeared twice.

Here it is `UPDATE txn SET deleted_at = ...` against a primary key. The row stays visible
under the Deleted filter, carries a reason, and can be restored. Nothing is ever erased.

## Revision counter

Every write bumps `db_meta.revision`; a rejected write does not. Nothing consumes it yet —
it is what the Phase 2b sync keys off (DESIGN.md 6.3). Putting it in with the first write
avoids back-filling revisions for rows created before sync existed.

## Verification

85 tests, covering validation rules, effective dating, the identifier format, batch
all-or-nothing behaviour, undo, and loose column matching.

The reconciliation gate still passes: the ledger reproduces the workbook bar the three
accepted differences. Writes cannot break it, since it recomputes from whatever is in the
database.

Two things checked in the running app rather than in tests: the form → validation → error
path (submitting an empty amount surfaces "Amount must be positive"), and that
`ui.session()` + `session.begin()` genuinely commits — verified against a scratch copy of
the database, inserting (737 → 738), generating identifier `0803_HSB_0`, deriving period
`2026-08`, then soft-deleting back to 737 with the row retained.

Note that Streamlit's widgets do not accept synthetic input — values must round-trip through
its websocket — so the widget-to-Python wiring itself is not automatable from outside. It is
a thin `st.form_submit_button`, and the layers either side are covered.

## Next

**Phase 2b — sync (DESIGN.md 6.3).** Now genuinely needed: the dashboard can write, so the
two machines can diverge. Until it lands, **enter data on one machine only** — the sidebar
says so on every page.

Phase 2b also brings the reconciliation route in 6.3.5, which exports local-only
transactions as CSV and feeds them back through the importer built here.
