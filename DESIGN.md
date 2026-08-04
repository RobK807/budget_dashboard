# Budget Dashboard — conversion design

Review of `K:\Private\Finance\Budget 26-27.xlsm` and a proposed design for replacing it with a
Python dashboard over a database.

Source reviewed: 25 sheets, ~640 defined names, 3 VBA modules (~1,650 lines), 8 UserForms.

---

## 1. What the workbook actually does

### 1.1 The data flow

```
BulkImport tab ──bulk_upload()──> Input tab ──New_entry()──> Month tab (April..March)
                  (row by row)      (10 fields)      │
                                                     └────────> Debug tab (append-only log)
```

`bulk_upload` sorts `BulkImport` by 5 keys, then loops each row: writes the 10 values into the
named cells on `Input`, calls `New_entry`, repeats. `New_entry` then:

1. Resolves the account to a **column offset** (4 columns per account: `Account | Credit | Debit | Date`).
2. Finds the insertion point in the month tab by scanning the account's date column, inserting a
   row if the transaction is out of date order.
3. Writes the amount into the Credit or Debit column, the date, and a cell comment holding the
   free-text note.
4. Writes the classification integer (`Purchase type`) and the account identifier into the
   parallel `Identifiers` / categories column blocks used by the SUMIFS.
5. For transfers, loops twice (once per side) with different column offsets.
6. Adds the amount to the category row in `B:F` and appends a dated note to that cell's comment.
7. Appends one row to the hidden **`Debug`** sheet.

### 1.2 The key architectural insight

**`Debug` is already a proper transaction ledger.** Its 738 rows carry every field needed to
reconstruct everything else:

| Col | Header (as labelled) | Actual content |
|-----|---------------------|----------------|
| A | Date | transaction date |
| B | *Month* | **identifier** (e.g. `0401_BAAM_0`) — header is mislabelled |
| C | *Identifier* | **month name** — header is mislabelled |
| D/E | Account1 / Account2 | from / to account |
| F/G | Reference1 / Reference2 | the cell addresses written to (positional coupling) |
| H | Transaction | Credit / Debit / Transfer |
| I | Category | e.g. Massage, Electricity |
| J | Amount | |
| K/L | CategoryComment / FieldComment | the two free-text notes |
| M | PurchaseType | **classification** (Bills / Food / …) |
| N–R | TimeMade, Removed, TimeRemoved, Added, TimeAdded | audit flags |

Everything else in the workbook — the 12 month tabs, `Summary`, `Projected Costs`,
`Cumulative Analysis` — is **derived**. In database terms they are all queries. The month tabs are
not data; they are a pivot that happens to be stored.

That is the whole conversion in one sentence: **keep `Debug`, throw away the rest of the storage,
and recompute the reports on demand.**

### 1.3 Reference data (`Selections` + `Control` + `DeveloperParameters`)

Three hidden sheets hold what is really the configuration:

- **Accounts** (`Selections!D4:D25`, 22 rows) — name, column offset, short code (`HFX`, `BAAM`),
  savings flag + limit, investment flag + limit, ISA flag, account type (Bank account / Credit card),
  plus **twelve per-month offset columns** (`Z:AK`) so an account added mid-year has offset `0`
  for earlier months.
- **Categories** (`G4:G39`, 36 rows) — with grouping (Income / Household bills / Regular outgoings /
  Other income / Other) and spend type (Credit / Debit / All).
- **Classifications** (`H4:H11`, 8 rows) — Bills, Excess, Food, Band, Expenses, Essentials, Savings,
  Other — each with an integer reference (used in the SUMIFS) and a direction (+1/−1).
- **Rollover types** (`AL`) — None / All / Positive / Negative, controlling whether a
  classification's closing balance carries into the next month.
- `Control` — tax year, user, month start day, excess retention.
- `DeveloperParameters` — the magic numbers the macros depend on (columns per account = 4,
  start row = 60, tab range 4–16, etc.).

### 1.4 Feature tabs

| Sheet | What it does |
|---|---|
| **Summary** | Savings total vs annual target and monthly required run-rate; investments the same; ISA in/out against the £20k limit; net cashflow by month; per-account target vs current; credit-card balance projection; classification-by-month matrix. |
| **Projected Costs** | A day-by-day grid for the current month: expected spend per classification (**hand-entered**), vs actual pulled from the month tab via `INDEX(INDIRECT("xlTotal"&…))`, plus a comparison column. `Export()` copies it out to a dated `.xlsx` in `Spending Analysis`. |
| **Salary tracker** | Actual payslip vs expected, with a full UK PAYE/NI model — LEL/UEL, 8%/2% NI, tapered personal allowance (4 separate adjustments across the year), basic/higher/additional rate bands — deriving expected net pay, and a spending calculation block. |
| **Cumulative Analysis** | Per-classification daily cumulative total for the year, actual vs predicted, driven by an offset selector, honouring the rollover rules. |
| **Cycling** | Two logs — bike outgoings (service, kit) and per-day savings flags (Commute / Band / Gym) with the fare saved — netted into running totals. |
| **Balance Transfer Cards** | Per-card amortisation: opening balance, term in months, minimum payment %, payment day → projected balance and payment each month to payoff. |

### 1.5 The maintenance macros (`Module2`) and forms

`add_account`, `remove_account`, `add_classification`, `remove_classification`, `add_category`,
`remove_category` — each driven by a UserForm, each doing the same thing: insert/delete columns or
rows in `Monthly_Template`, **rebuild the giant SUMIFS formula by string surgery**, patch
`Selections`, then call `update_months`.

`update_months` copies the whole of `Monthly_Template` over every month tab from the chosen month
to March, then deletes and re-creates ~20 named ranges per month.

This is why every one of those forms opens with:

> *"This will override all months from X onwards, if there are any existing transactions in the
> months in scope this may cause errors in the spreadsheet. Do you wish to continue?"*

**Adding an account destroys the months you add it to.** This is the single strongest argument for
the migration, and it is a problem that simply does not exist once nothing is stored positionally.

---

## 2. Problems found in the current workbook

Worth knowing, because several are silent and the new design should explicitly close them.

1. **Cash transactions never reach the ledger.** `New_entry` line 112: if `Account From = "Cash"`,
   it sets the amount and jumps straight to the category update — skipping the month tab *and* the
   `Debug` append. Cash spend hits category totals only and is invisible to any audit.
   → **Decision: dropped.** Cash is no longer tracked. `"Cash"` was a magic string, never an
   account, and the ledger contains **zero** cash rows — so there is nothing to migrate and no
   cash concept anywhere in the new design. The importer should hard-fail if it ever sees one.
2. **Three bad dates sitting in the ledger right now** — row 2 dated `2019-04-01`, row 360 dated
   `2029-05-29` (May, BA Amex, Food, £17.09), row 601 dated `2024-07-11`.
   Nothing validates that the date matches the month tab it was filed under.
   → **Decision: corrected to 2026 on import**, keeping day and month. See §4.2.
3. **The ledger stores cell addresses** (`$P$4`, `$X$4`) as the link back to the month tab. Any row
   insert silently invalidates them, which is why `remove_transaction` needs `RowAdj1`/`RowAdj2`
   correction factors.
   → **Decision: dropped.** Not needed once rows have surrogate keys; `Debug` cols F/G are
   ignored by the importer.
4. **Deletion is a row-shuffle.** `remove_transaction` clears the cells, copies everything below up
   one, and strips the note out of the category comment by `Search`-ing for the comment text — which
   corrupts the comment if the same text appears twice, and errors if it is absent.
5. **Formula-string surgery.** `add_account` builds the SUMIFS by
   `Left(Right(strExistFormula, …), Len(…) - intClassFormDirLen - 2)`. There is a
   `'######### EDITED CODE` marker on that line. It is one refactor away from breaking.
6. **Recalc cost.** Each daily classification cell is a chain of ~44 SUMIFS (2 per account × 22
   accounts). At 8 classifications × 31 days × 12 tabs that is roughly 130,000 SUMIFS per full
   recalculation.
7. **Broken/dead names** — `xlSelAQ` and `xlSelAR` both resolve to `Selections!#REF!`; leftovers
   like `xlTestCatFebruary`, `xlTempDecember`, `xlTestMarch` remain from past debugging.
8. **Workbook password in plaintext** — `Selections!AR3` = `Budg3tSpr3ad`.
9. `Dim i, j, k As Integer` declares `i` and `j` as **Variant** (VBA gotcha), and several row
   counters are `Integer` (max 32,767) against a 1,048,576-row sheet.
10. Header labels in `Debug!B1`/`C1` are swapped relative to what `New_entry` writes.
    → Handled by reading `Debug` **positionally**, not by header name.

Items 7 and 10 also cover the deletion-lookup machinery — `xlFiltReq`, `xlFiltRes`,
`xlDebugOffset1/2`, `xlDebugRowAdj1/2`, `xlDebugTrans`, `xlDebugOptions`. These exist purely to
let `RemoveTransaction` / `TransactList` locate a row's cell address before shuffling it out.
**Decision: none of it carries over.** Deletion becomes `UPDATE txn SET deleted_at = …` against a
primary key. `DeveloperParameters` goes the same way — it only describes sheet geometry.

---

## 3. Proposed target

### 3.1 Stack

| Layer | Choice | Why |
|---|---|---|
| Database | **SQLite** (single file) | Single user, zero admin, one file to back up — same mental model as the workbook. ~740 txns/year means the data is tiny. Move to Postgres later if ever needed. |
| Access | **SQLAlchemy 2.x** (Core or ORM) | Keeps the door open to Postgres; migrations via Alembic. |
| Analytics | **pandas** | The month-tab / cumulative / projection logic is a handful of groupbys. |
| UI | **Streamlit** | Pure Python, multipage, `st.data_editor` gives an editable grid that directly replaces `BulkImport`. Fastest route from here to working. |
| Charts | **Plotly** | Interactive, good for the cumulative and projection lines. |
| Testing | **pytest** | The PAYE/NI calculator and the balance engine are pure functions — test them properly. |

Alternative if you want a "real" web app later: FastAPI + React. Not worth it for a single-user
tool; Streamlit will do everything described here.

### 3.2 Schema

The important decisions are marked **▸**.

```sql
-- ─── Reference data (replaces Selections / Control / DeveloperParameters) ───

CREATE TABLE account (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    short_code      TEXT NOT NULL UNIQUE,          -- HFX, BAAM, SAV_WED
    type            TEXT NOT NULL,                 -- bank | credit_card | savings | investment
    is_savings      BOOLEAN NOT NULL DEFAULT 0,
    savings_limit   NUMERIC,                       -- 0 = no cap (cf. xlSelS)
    is_investment   BOOLEAN NOT NULL DEFAULT 0,
    investment_limit NUMERIC,
    is_isa          BOOLEAN NOT NULL DEFAULT 0,
    display_order   INTEGER,
    valid_from      DATE NOT NULL,                 -- ▸ replaces the 12 per-month offset columns
    valid_to        DATE                           -- ▸ NULL = still open
);

CREATE TABLE classification (                      -- Bills, Food, Excess, Savings, ...
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    direction     INTEGER NOT NULL,                -- +1 / -1  (Selections!J)
    rollover      TEXT NOT NULL,                   -- none | all | positive | negative
    counts_as_spend BOOLEAN NOT NULL DEFAULT 1,
    display_order INTEGER,
    valid_from    DATE NOT NULL,
    valid_to      DATE
);

CREATE TABLE category (                            -- Mortgage, Going Out, Electricity, ...
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    grouping      TEXT NOT NULL,                   -- Income | Household bills | Regular outgoings | Other income | Other
    summary_group TEXT,
    spend_type    TEXT NOT NULL,                   -- credit | debit | all
    display_order INTEGER,
    valid_from    DATE NOT NULL,
    valid_to      DATE
);

-- ─── The ledger (replaces Debug + all 12 month tabs) ───

CREATE TABLE txn (
    id                INTEGER PRIMARY KEY,
    uid               TEXT NOT NULL UNIQUE                    -- ▸ sync identity, see §6.3.3
                      DEFAULT (lower(hex(randomblob(16)))),
    txn_date          DATE    NOT NULL,
    type              TEXT    NOT NULL,            -- credit | debit | transfer
    amount            NUMERIC NOT NULL CHECK (amount >= 0),
    account_from_id   INTEGER REFERENCES account(id),
    account_to_id     INTEGER REFERENCES account(id),   -- transfers only
    category_id       INTEGER REFERENCES category(id),
    classification_id INTEGER REFERENCES classification(id),
    comment           TEXT,
    category_comment  TEXT,
    -- audit
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at        TIMESTAMP,                   -- ▸ soft delete, never hard delete
    deleted_reason    TEXT,
    source            TEXT NOT NULL,               -- manual | bulk | bank_import
    batch_id          INTEGER REFERENCES import_batch(id)   -- ▸ makes a whole import undoable
);
CREATE INDEX ix_txn_date  ON txn(txn_date) WHERE deleted_at IS NULL;
CREATE INDEX ix_txn_from  ON txn(account_from_id, txn_date);

CREATE TABLE import_batch (
    id          INTEGER PRIMARY KEY,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    filename    TEXT,
    row_count   INTEGER,
    note        TEXT
);

-- ─── Periodic inputs ───

CREATE TABLE opening_balance (                     -- month tab row 60 "Start"
    account_id INTEGER NOT NULL REFERENCES account(id),
    period     TEXT    NOT NULL,                   -- '2026-04'
    amount     NUMERIC NOT NULL,
    PRIMARY KEY (account_id, period)
);

CREATE TABLE budget (                              -- month tab cols C/D: Income & Expected Costs
    period      TEXT NOT NULL,
    category_id INTEGER NOT NULL REFERENCES category(id),
    income      NUMERIC DEFAULT 0,
    expected    NUMERIC DEFAULT 0,
    PRIMARY KEY (period, category_id)
);

CREATE TABLE projection (                          -- Projected Costs E8:K39 + comments
    proj_date         DATE NOT NULL,
    classification_id INTEGER NOT NULL REFERENCES classification(id),
    amount            NUMERIC NOT NULL DEFAULT 0,
    comment           TEXT,
    PRIMARY KEY (proj_date, classification_id)
);

CREATE TABLE target (                              -- Summary: savings/investment annual + monthly
    period  TEXT NOT NULL,                         -- '2026-04' or '2026' for annual
    kind    TEXT NOT NULL,                         -- savings | investment | isa_limit | account
    ref_id  INTEGER,                               -- account_id when kind='account'
    amount  NUMERIC NOT NULL,
    PRIMARY KEY (period, kind, ref_id)
);

-- ─── Feature modules ───

CREATE TABLE payslip (                             -- Salary tracker, actual side
    period TEXT PRIMARY KEY, payday INTEGER,
    gross NUMERIC, ni NUMERIC, holiday_pay NUMERIC,
    cycle_to_work NUMERIC, paye NUMERIC, net NUMERIC
);
CREATE TABLE salary_assumption (                   -- expected side + tax bands
    tax_year INTEGER, key TEXT, value NUMERIC, effective_from DATE,
    PRIMARY KEY (tax_year, key, effective_from)
);

CREATE TABLE card (                                -- Balance Transfer Cards
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    account_id INTEGER REFERENCES account(id),
    opening_balance NUMERIC, opening_date DATE,
    term_months INTEGER, min_payment_pct NUMERIC, payment_day INTEGER,
    total_available NUMERIC
);

CREATE TABLE cycling_outgoing (id INTEGER PRIMARY KEY, date DATE, item TEXT, amount NUMERIC, flag TEXT);
CREATE TABLE cycling_day (
    date DATE PRIMARY KEY,
    commute BOOLEAN, band BOOLEAN, gym BOOLEAN, amount_saved NUMERIC
);

CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT);   -- tax_year, user, month_start_day, excess_retention
```

**▸ Why `valid_from` / `valid_to` matters.** This one change is what removes `update_months`
entirely. "Add an account from June onwards" becomes one `INSERT` with `valid_from = '2026-06-01'`.
Nothing is rewritten, nothing is destroyed, and April–May reports are untouched because the account
simply has no transactions in them. Removing an account is `UPDATE account SET valid_to = …` — it
stays selectable for historic months and disappears from new entry forms. Same for categories and
classifications.

**▸ Why `uid`.** Integer primary keys collide across two machines editing independently. Every
mergeable table (`txn`, `cycling_day`, `cycling_outgoing`, and the reference tables) carries a
`uid` as its sync identity while keeping the integer PK for joins and readability. This must be in
place from the first migration — retrofitting it after the two copies have diverged is painful.
See §6.3.3.

**▸ Why soft delete.** `deleted_at IS NULL` in every reporting view. Your "remove a transaction I
entered wrongly" requirement becomes a single UPDATE with no row-shuffling, no comment surgery, and
a full history of what was removed and when — which the current `Removed` / `TimeRemoved` columns
were reaching for anyway.

**▸ Transfers.** Keep one row per transaction (matching how you enter them and how `BulkImport` is
shaped), and explode into signed postings in a view, so balance maths needs no special-casing:

```sql
CREATE VIEW posting AS
  SELECT id AS txn_id, txn_date, account_from_id AS account_id,
         CASE WHEN type = 'credit' THEN amount ELSE -amount END AS signed_amount,
         category_id, classification_id
  FROM txn WHERE deleted_at IS NULL AND account_from_id IS NOT NULL
  UNION ALL
  SELECT id, txn_date, account_to_id, amount, category_id, classification_id
  FROM txn WHERE deleted_at IS NULL AND type = 'transfer' AND account_to_id IS NOT NULL;
```

Every balance, every classification total, every cumulative line is then a `GROUP BY` over
`posting`. The ~130,000 SUMIFS collapse into one indexed scan.

There is **no cash handling** — no `"Cash"` string, no cash account. Every transaction must resolve
to a real account, which the `account_from_id` foreign key enforces for free.

### 3.3 Sheet → dashboard mapping

| Today | Becomes |
|---|---|
| `Input` + `New_entry` | **Add transaction** form — same 10 fields, dropdowns filtered by `valid_from/valid_to`, live validation (the `Update to credit` / `Delete account to` checks in `Input!E4:E10` become form rules). |
| `BulkImport` + `bulk_upload` | **Import** page: paste or upload CSV → `st.data_editor` grid → validate → preview diff → commit as one `import_batch`. Undo = soft-delete the batch. Optionally add a rules engine mapping bank-statement descriptions to category/classification so you stop typing them. |
| 12 month tabs + `Monthly_Template` | **Deleted.** A *Month* page renders the same three blocks as queries: per-account balances (opening + postings), per-category budget vs actual vs left, and the daily classification grid. |
| `Summary` | **Overview** page — savings/investment/ISA vs target, required run-rate, net cashflow, per-account target vs current, classification-by-month matrix. |
| `Projected Costs` | **Projections** page — editable projection grid vs actual, comparison chart. `Export()` becomes a download button. |
| `Salary tracker` | **Salary** page — payslip entry; the PAYE/NI/personal-allowance-taper model becomes a tested pure function `expected_net(gross, tax_year, month) -> Breakdown`. |
| `Cumulative Analysis` | **Trends** page — cumulative actual vs predicted per classification, rollover rules applied in code. |
| `Cycling` | **Cycling** page — outgoings + per-day savings flags, running net. |
| `Balance Transfer Cards` | **Cards** page — amortisation schedule generated from `card` params rather than 400 stored rows. |
| `Control`, `DeveloperParameters`, `Selections` | **Settings** page — accounts / categories / classifications CRUD (replacing all 6 Add/Remove forms), plus tax year, month start day, excess retention. `DeveloperParameters` disappears entirely; it only exists to describe the sheet geometry. |
| `Debug`, `RemoveTransaction`, `TransactList` | **Transactions** page — filter by date/account/type/category, select, soft delete. Replaces the two-form flow with a single filtered table. |

### 3.4 Suggested layout

```
budget_dashboard/
├── app.py                     # Streamlit entrypoint
├── pages/
│   ├── 1_Overview.py   2_Month.py      3_Transactions.py
│   ├── 4_Import.py     5_Projections.py 6_Salary.py
│   ├── 7_Cards.py      8_Cycling.py    9_Settings.py
├── budget/
│   ├── db.py                  # engine, session
│   ├── models.py              # SQLAlchemy models
│   ├── repo.py                # queries (balances, classification totals, cumulative)
│   ├── ingest.py              # bulk import + validation rules
│   ├── tax.py                 # PAYE / NI / personal allowance taper
│   ├── cards.py               # amortisation
│   └── migrate_xlsm.py        # one-off workbook -> SQLite
├── tests/
└── data/budget.db
```

---

## 4. Migration

1. **Reference data first** — read `Selections` into `account` / `category` / `classification`.
   Set `valid_from` for accounts from the per-month offset columns `Z:AK`: the first month with a
   non-zero offset is the account's start (e.g. `Tembo` and `Savings - First Direct` have `0` for
   April/May, so `valid_from = 2026-06-01`).
2. **Ledger** — `Debug` rows 2..739 → `txn`, read **positionally** (the B/C headers are swapped):
   A→date, B→identifier, C→month, D/E→accounts, H→type, I→category, J→amount, K/L→comments,
   M→classification, N/O/P→audit. Cols F/G (cell addresses) and Q..Y (row-adjustment scratch) are
   ignored. Any row with an account of `Cash` is a hard error, not a warning.
   Three dates are corrected on the way in — see *Date correction* below.

3. **Opening balances** — row 60 (`Start`) of the April tab per account → `opening_balance`.
4. **Budgets** — cols C/D of each month tab → `budget`.
5. **Projections / salary / cards / cycling** — direct table copies.
6. **Reconcile — this is the acceptance test.** Recompute every account's month-end balance from
   `posting` and diff against row 61 (`End`) on each month tab; do the same for the daily
   classification totals against `DB:DI`. If April–July tie to the penny, the model is correct.

   Expect the reconciliation to *fail* initially in two places, and treat both as findings rather
   than bugs in the new code: any **Cash** transactions (never logged to `Debug`, so unreproducible
   from the ledger) and any month-tab cells edited by hand. You will need to decide per difference
   whether to add a correcting `txn` or accept the delta as an opening-balance adjustment.

7. **Prior years** — `Budget 25-26.xlsm`, `24-25`, etc. share this structure, so the same importer
   should backfill them. Worth doing *after* the current year is proven, and it is what turns
   `Cumulative Analysis` from a one-year view into genuine multi-year trend analysis.

### Date correction

Three ledger rows carry a wrong year and are corrected to **2026**, preserving day and month:

| Debug row | Stored date | Corrected | Detail |
|---|---|---|---|
| 2 | 2019-04-01 | 2026-04-01 | First Direct, Electricity, £10 — the original seed row |
| 360 | 2029-05-29 | 2026-05-29 | BA Amex, Food, £17.09 |
| 601 | 2024-07-11 | 2026-07-11 | Stocks & Shares ISA, £169.79 |

Do this as an explicit, enumerated fix in the importer, not a blanket "coerce year to 2026" — a
general rule would silently mangle genuine prior-year data when the backfill in step 7 runs. Log
each correction and assert the count is exactly 3.

Going forward the rule is enforced rather than patched: reject any transaction whose date falls
outside the period it is filed under. That check would have caught all three at entry.

---

## 5. Phasing

| Phase | Deliverable | Gate |
|---|---|---|
| 0 ✅ | Schema + importer + reconciliation script | **Passed** — all 264 account-months tie; see PHASE0_FINDINGS.md |
| 1 ✅ | Read-only Overview / Month / Transactions | **Passed** — every displayed figure checked against the workbook by `reconcile.py`; see PHASE1_NOTES.md |
| 2 ✅ | Add transaction, bulk import, soft delete | **Passed** — validation enforced, batches all-or-nothing and undoable; see PHASE2_NOTES.md |
| 2b ✅ | Sync (§6.3): staged push, revision check, offline checkout/check-in, CSV reconciliation | **Built and tested**; see PHASE2B_NOTES.md. Still to do in anger: first push here, first pull on the desktop |
| 3 ✅ | Settings CRUD (accounts / categories / classifications) | **Passed** — adding an account mid-year leaves every earlier transaction identical, asserted by test; see PHASE3_NOTES.md |
| 4 ✅ | Projections, Salary, Cards, Cycling | **Passed** — five pages, rollover engine, PAYE/NI to the penny; checks D and F live. See PHASE4_NOTES.md |
| 5 ✅ | Refinements: every parameter editable, savings targets, card billing | **Passed** — 21 changes, one new page, five new Settings sections; every figure still ties. See PHASE5_NOTES.md |
| 6 | Retire the workbook; backfill prior years | |

Run phases 2–5 in parallel with the spreadsheet for one full month and diff the two. That is the
only way to be confident before you stop double-entering.

---

## 6. Where the database lives, and multi-machine access

**Requirement: the budget is updated from both the laptop and the desktop.**

That rules out a purely local database, and it is worth being precise about *why* the obvious
options are risky before picking one.

`K:` is not a local drive — it is `\\SynoRk807\Rob_Documents`, an SMB share on a Synology NAS
(DSM responding on 5000/5001, web on 80/443).

### 6.1 Why not just put the SQLite file on `K:`

SQLite's own documentation warns against running a database over a network filesystem: its locking
depends on POSIX/Windows locking primitives that SMB implementations do not honour reliably. The
realistic failure is a dropped connection or a NAS-side write cache mid-transaction, which corrupts
the file rather than merely failing the write. Two machines mounting it at once makes that worse,
not better.

The spreadsheet gets away with living there because Excel uses a completely different access
pattern: read the whole file into memory, work there, write it back in one go — plus an owner-lock
file (`~$Budget 26-27.xlsm`) and a "file in use" prompt. SQLite writes incrementally, page by page,
holding locks across the transaction. Same folder, very different risk.

### 6.2 Recommended — run the application on the NAS

Put **both** the app and the database on the NAS, in a container, and reach it from a browser:

```
laptop  ─┐
         ├─ http://synork807:8501 ──> Streamlit + SQLite (Container Manager, on the NAS)
desktop ─┘                                    └─ /volume1/budget/budget.db  (NAS-local disk)
```

This dissolves the problem rather than managing it:

- **One database, one copy, always current.** No copy-down, no copy-back, no sync step to forget.
- **The SMB concern disappears entirely.** SQLite sits on the NAS's own local filesystem; the
  network is between *browser and app*, not app and database. This is the normal, safe way to use
  SQLite over a network.
- **No lost-update risk**, because there is only ever one database.
- **Nothing to install on the desktop** — no Python, no environment, just a browser.
- **Backups become a NAS-side job**: scheduled `VACUUM INTO` to `K:\Private\Finance\db_backups\`,
  picked up by Hyper Backup alongside everything else.

Requires Container Manager (Docker) in DSM Package Center — available on x86 Synology models, not
on the low-end ARM ones. **Confirm this before committing to the approach.**

To check the model: **DSM → Control Panel → Info Center → General**, and read *Model name* and
*CPU model*. The CPU is the decisive field:

| CPU model reads | Architecture | Container Manager |
|---|---|---|
| Intel Celeron / Pentium / Xeon, AMD Ryzen / Embedded | x86-64 | Available |
| Realtek RTD…, Marvell Armada, Annapurna Labs Alpine | ARM | Not available |

As a shorthand, Synology model numbers ending `+` (DS220+, DS923+) are x86; `j`, `play` and plain
value models (DS120j, DS223) are ARM. If Container Manager is absent from Package Center on
DSM 7.x, that is itself strong evidence of an ARM unit — Synology filters the package list by
model, so it is not a case of simply not having installed it yet.

A middle option if the NAS turns out to be ARM: run the container on whichever machine is
effectively always-on (likely the desktop) and point the laptop's browser at it. This keeps the
single-database property of 6.2, but only while that machine is awake and on the LAN — so it is
only worth it if the desktop genuinely stays on. Otherwise use 6.3.

Practical notes: bind the container to the LAN only (no port-forwarding, no QuickConnect exposure —
this data is salary and account balances); put Streamlit behind DSM's reverse proxy with HTTPS if
you want to avoid plaintext on the wire; give the container a restart policy so it survives NAS
reboots. Development still happens locally against a throwaway copy — the NAS runs the deployed
image.

### 6.3 Fallback — checkout / check-in (if Container Manager is unavailable)

Your proposed model — master on the NAS, copy down, edit, copy back — does work, and it mirrors
what you do today. Its one serious failure mode is the **lost update**: copy down to the laptop on
Monday, forget to copy back; copy down to the desktop on Wednesday, edit, copy back. Monday's
laptop work is silently gone. Excel protects you from this with its lock file; a bare file copy has
nothing.

So it needs two guards, and the second is the one that actually matters:

1. **A lock file** on the NAS (`budget.lock`) holding machine name and timestamp. Checkout refuses
   if someone else holds it; the app refuses to open a local DB whose lock it does not hold; stale
   locks (>24h) warn rather than silently expire.
2. **A revision counter**, which is the real safety net. A `db_meta` table carries a monotonic
   `revision`. Checkout records the NAS revision as `base_revision`. Check-in re-reads the NAS
   revision and **refuses if it has moved** — meaning the other machine wrote in the meantime.

Without (2), a lock-discipline slip loses data silently. With it, the same slip becomes a loud
refusal you can reconcile by hand. Build both or neither.

#### Automatic push, with a manual trigger

Yes to both — and automating the push is what makes this design tolerable, because it shrinks the
window in which the two machines can diverge to almost nothing. Manual-only check-in is precisely
where the lost update creeps in.

**Triggers**, in order of importance:

1. **After a bulk import batch commits** — one push per batch, not per row.
2. **After any single committed write** (add / edit / delete a transaction, settings change).
   The database is small — 738 transactions is a few hundred KB, and years of history stays in
   single-digit MB — so pushing the whole file every time is cheap and needs no delta logic.
   Debounce a few seconds so rapid entry does not queue a dozen pushes.
3. **A "Sync now" button** in the header, always available.
4. **On app start** (pull) and **on clean shutdown** (push).

**Push sequence** (`budget/sync.py`):

```
push():
  0. dirty?            revision > pushed_revision, else no-op
  1. reachable?        NAS path exists  -> else PENDING (not an error, see below)
  2. lock              held by this machine, or free and nas_revision == base_revision
  3. snapshot          VACUUM INTO %TEMP%\budget_push.db     # clean, not a live-file copy
  4. verify            PRAGMA integrity_check + sha256 of snapshot
  5. re-check          re-read NAS meta; nas_revision != base_revision -> CONFLICT, abort
  6. upload            copy snapshot -> K:\...\budget.db.incoming
  7. verify upload     sha256 of the uploaded file matches step 4
  8. promote           budget.db -> budget.db.bak ; budget.db.incoming -> budget.db
  9. publish           write budget.meta.json ; release lock
 10. local             pushed_revision = base_revision = revision
```

Steps 6–8 mean a dropped connection never leaves a truncated master — the live file is only
replaced once a complete, verified copy is sitting beside it, and `.bak` gives a free one-generation
rollback. Step 7 is what catches silent SMB write corruption. Step 3 uses `VACUUM INTO` rather than
a file copy so the snapshot is transactionally consistent even if the app is mid-write.

**The invariant that matters: never clear the dirty flag unless the push verified end-to-end.**
Every failure path below leaves `pushed_revision` untouched, so the next trigger retries.

**Metadata lives in a sidecar, not in the database.** `budget.meta.json` on the NAS holds
`{revision, machine, updated_at, sha256}`. Reading a small JSON file over SMB is safe; opening the
SQLite database over SMB just to read its revision is exactly the thing this whole section exists
to avoid.

**Failure handling** — the distinction that matters for a laptop:

| Situation | Treatment |
|---|---|
| NAS unreachable (laptop away from home) | **Not an error.** Amber banner: *"3 changes pending sync"*. Auto-retry on next write and next launch. This is a normal state, not a fault. |
| Copy or verify failed (network dropped mid-transfer) | Red error, master left untouched, stays dirty, retry available. |
| NAS revision moved (other machine wrote) | **Blocking conflict.** Refuse to push. Offer: export local-only transactions to CSV → pull fresh master → re-import them. |

Because the laptop will routinely be off the LAN, the pending state must be **visible at all
times**, not a toast that disappears:

```
🟢 In sync · rev 412          🟡 3 changes pending · NAS unreachable [Sync now]          🔴 Conflict [Reconcile]
```

**The away-from-home edge case.** If the laptop goes dirty off-LAN it keeps the lock, which blocks
the desktop. If that drags on, the desktop can **force-take** the lock — an explicit, logged
action. The laptop's next push then hits the revision check, refuses, and routes to reconciliation.
Loud rather than silent is the whole point; the alternative is one of the two machines quietly
losing a week's entries.

This is strictly more moving parts than 6.2 for a strictly worse result, so treat it as the
fallback it is — but with automatic push and the revision check, it is a sound fallback rather
than a hopeful one.

**Confirmed: the NAS CPU is a Realtek RTD (ARM), so Container Manager is unavailable, and the
desktop is not always on. This section is the design, not a contingency.**

#### 6.3.1 First — make "off-LAN" rare rather than handling it well

Before building reconciliation machinery, shrink the problem. The whole divergence scenario exists
only because the laptop cannot reach the NAS from outside the house. Fix that and it largely
evaporates:

- **Tailscale** publishes DSM packages for ARM Synology units and installs outside Package Center
  (manual `.spk`). The laptop then reaches the NAS from anywhere as if on the LAN, over WireGuard,
  with no ports forwarded and nothing exposed to the internet. This is the single highest-value
  mitigation available here.
- Failing that, **DSM's built-in VPN Server** (OpenVPN / L2TP) achieves the same with more setup
  and a port to forward — acceptable, but Tailscale is cleaner and does not expose anything.
- Cheapest of all: **prompt on shutdown when dirty** — "3 changes not synced, sync before closing?"
  — and prompt on app start if the last session ended dirty.

With remote sync working, the laptop is rarely dirty for more than minutes, and reconciliation
becomes a genuine edge case rather than a weekly event. Build it anyway, but build it second.

#### 6.3.2 Why this merge is tractable

This is not a general database merge problem, which is what makes it worth automating at all. The
data is overwhelmingly **append-only immutable facts**:

- A transaction entered on the laptop and one entered on the desktop are *independent facts*. They
  do not conflict; they both simply need to exist.
- Soft deletes are monotonic — once deleted, stays deleted. Both machines deleting the same row is
  idempotent.
- The only genuinely conflicting operations are **edits to existing rows**: renaming a category,
  changing a budget figure, adjusting an opening balance.

That yields a rule that removes almost all of the difficulty:

> **Inserts merge automatically. Edits require being in sync.**

So the app permits offline: adding transactions, soft-deleting transactions, adding cycling log
rows. It requires an online, in-sync state before allowing: reference-data changes (accounts,
categories, classifications), budgets, targets, opening balances, projections, salary and card
settings. That matches how the data is actually used — transactions are entered daily, an account
is added twice a year — and it means the merge never has to translate foreign keys between two
divergent copies of the reference tables.

#### 6.3.3 What the schema needs

Two additions, both cheap, both needed *before* first use — retrofitting them after divergence
has occurred is painful:

1. **A sync identity on every mergeable row.** Integer primary keys collide: the laptop's `txn`
   739 and the desktop's `txn` 739 are different transactions. Add alongside the integer PK:

   ```sql
   uid TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16))))
   ```

   The integer PK stays for joins and readability; `uid` is the merge identity. Applies to `txn`,
   `cycling_day`, `cycling_outgoing`, and the reference tables (so their uids are stable from
   migration onward).

2. **A local copy of the common ancestor** — `budget.base.db`, a snapshot of exactly what was last
   pulled, kept beside the working database. Without it you cannot distinguish *"the laptop added
   this row"* from *"the desktop deleted it"*. A three-way merge needs the ancestor; a two-way diff
   will get this wrong. It costs a few MB.

#### 6.3.4 The reconciliation flow

Worked through the scenario you asked about:

```
Mon  laptop pulls master            base=412, local=412, clean, holds lock
Mon  laptop edits off-LAN           local=415  (3 new txns)  DIRTY, cannot push
Wed  desktop blocked by lock -> force-takes (explicit, logged, warned)
Wed  desktop pulls 412, edits, pushes                        NAS=418  (6 changes)
Fri  laptop returns, auto-push fires -> base(412) != NAS(418) -> CONFLICT
```

At this point three databases exist and all are needed: `budget.base.db` (412, the ancestor),
`budget.db` (415, laptop's work), and the NAS master (418, desktop's work). Then:

1. **Pull** the NAS master (418) to a temp path. Never modify the master in place during a merge.
2. **Compute the laptop's delta** by diffing local (415) against base (412), per mergeable table:
   - rows whose `uid` is absent from base → **inserts**
   - rows whose `deleted_at` is set locally but null in base → **deletes**
   - rows otherwise changed → **edits** (should be empty given the 6.3.2 rule; if any appear,
     they are true conflicts and go to manual review)
3. **Replay** that delta onto the pulled 418 copy: insert transactions whose `uid` is not present;
   apply soft-deletes where the `uid` exists and is not already deleted.
4. **Flag near-duplicates.** If the same spend was entered on both machines — you added Tesco
   £12.50 on the laptop, forgot, and added it again on the desktop — the uids differ, so it merges
   as two rows. Match on `date + account + amount` and surface them for review. Do **not** silently
   drop: two genuinely separate £12.50 Tesco trips on one day are perfectly plausible. This is what
   the existing `identifier` field (`0401_BAAM_0`) is naturally good for.
5. **Preview before committing** — *"12 transactions from this laptop will be added to the master.
   2 possible duplicates. 0 conflicts."* Nothing is written until you confirm.
6. **Commit**: revision → 419, push via the normal staged sequence (§6.3), refresh
   `budget.base.db`, release lock.

#### 6.3.5 The manual path, which is also the fallback

If the automatic merge is ever untrustworthy — or you would simply rather see it — the same job is
done by machinery already being built:

```
export local-only txns  ->  BulkImport-format CSV
pull fresh master
re-import via the normal bulk import (validation + preview + batch tag + undo)
```

That is the safest option and it needs almost no new code, because the import path already does
validation, preview, batch tagging and one-click undo. It is also a flow you already understand,
since it is how you enter data anyway.

**Build 6.3.5 first, in Phase 2, alongside the importer.** Add the automatic merge of 6.3.4 later,
as a convenience layer, once the sync has proven itself in real use. That ordering means you are
never without a reconciliation route, and the risky code is the code you add last.

#### 6.3.6 Deliberate offline mode (explicit checkout / check-in)

Yes, this works — and nearly all of it is already specified. It needs one change of mechanism.

**The reframe.** In this design you *always* work on a local copy. The NAS only ever holds the
master file that gets pulled and pushed; nothing ever opens the SQLite database over SMB, because
that is the hazard §6.1 exists to avoid. So "create a local copy in order to work offline" is what
already happens on every single launch.

What you are actually adding is not a different *location* but a different **intent**, and it
changes three behaviours:

| | Normal (`online`) | Deliberate (`offline`) |
|---|---|---|
| Working database | local copy | local copy *(same)* |
| Auto-push | after every write | **suppressed** — no pointless retries or error banners |
| NAS lock | taken and released per push | **long-lived lease**, held for the duration |
| Reference-data edits | allowed | read-only (per the §6.3.2 rule) |
| What the desktop sees | "in sync, rev 412" | "checked out by LAPTOP-RK since Mon, expected Fri" |

That last row is the real prize. Under the accidental-divergence path of §6.3.4, the desktop sees a
stale lock and has to guess whether to force-take it. Under a deliberate checkout it sees a stated
intent and an expected return date, which is a far better basis for that decision.

**Checkout — "Work offline":**

```
1. require  local revision == NAS revision      (else resolve first; never check out dirty)
2. pull     fresh master -> working DB
3. snapshot working DB -> budget.base.db        # the ancestor - what makes the later merge possible
4. lease    write NAS lock {machine, mode:"offline", taken_at, expected_return}
5. state    mode = offline
```

**Check-in — "Go online and push":**

```
1. require  NAS reachable
2. push     the standard staged sequence of 6.3 (revision check -> VACUUM INTO -> verify
            -> upload -> verify -> promote)
3. if NAS revision moved -> reconciliation per 6.3.4, which works precisely because
   step 3 of checkout kept budget.base.db
4. archive  working copy -> checked_in/budget.YYYYMMDD-HHMMSS.db  (keep last N)
5. refresh  budget.base.db ; release lease ; mode = online
```

**Three refinements to the proposal as you framed it:**

1. **Archive the local copy rather than deleting it.** After a verified push, local and master are
   identical, so deletion is *safe* — but it also discards your only second copy at exactly the
   moment you are trusting a network transfer. Keeping the last few checked-in snapshots costs a
   few MB and has saved this kind of setup many times.

2. **Do not infer state from whether the file exists.** "Local copy present" is a fragile state
   machine: an app crash mid-push, a stray manual copy, or a hand-deleted file all silently change
   behaviour. Keep `mode`, `base_revision` and `dirty` in an explicit local `sync_state` record,
   and let the files follow from that state rather than define it.

3. **Freshness comes from the checkout, not from the deletion.** The instinct behind "delete on
   push so next time it recreates from master" is sound, but step 2 of checkout already pulls a
   fresh master unconditionally. Putting the guarantee there is cleaner, and it means an
   interrupted check-in cannot leave you with no local database at all.

**Accidental offline still works.** If the laptop simply leaves the LAN without checking out,
auto-push fails, the pending banner appears, and §6.3.4 handles it exactly as before. Deliberate
offline mode is the well-lit version of the same path, not a replacement for it — both must work,
because you will sometimes forget.

**Forgetting to check in** is handled by the lease: the laptop prompts on every start (*"offline
since Monday — check in?"*), and once `expected_return` passes, the desktop's force-take warning
becomes correspondingly more assertive.

**Nothing new is needed in the schema.** The `uid` columns, `budget.base.db`, the revision counter,
the staged push and the reconciliation flow are all already specified — offline mode is the feature
they were going to be needed for anyway. What is genuinely new is a mode flag, two buttons and the
lease semantics.

#### 6.3.7 Preventing it rather than curing it

- Push on every committed write, so the dirty window is minutes.
- Persistent pending indicator, never a dismissable toast.
- When the desktop force-takes a held lock, **warn with specifics**: *"LAPTOP-RK has held this lock
  since Monday 14:02 and may have unsynced changes. Continue?"*
- Prompt to sync on shutdown when dirty, and warn on start if the last session ended dirty.
- Tailscale, per 6.3.1, which addresses the root cause rather than the symptom.

### 6.4 If you outgrow either

Swap SQLite for **Postgres in a second container** on the NAS. Because everything goes through
SQLAlchemy, that is a connection-string change plus an Alembic run. It buys genuine concurrent
writes — worth it only if this ever stops being a single-user tool.

### 6.5 Keeping the location swappable

Whichever of the above you pick, resolve the path in exactly one place so changing your mind is
configuration rather than a code hunt:

```python
# budget/db.py
import os
from pathlib import Path

DEFAULT_DB = Path(os.environ["LOCALAPPDATA"]) / "BudgetDashboard" / "budget.db"
DB_PATH = Path(os.environ.get("BUDGET_DB_PATH", DEFAULT_DB))
```

On the NAS container that becomes `BUDGET_DB_PATH=/data/budget.db` with a volume mount — no code
change. A SQLite database is a single self-contained file with no absolute paths inside it and
nothing registered anywhere, so relocating it really is just moving the file.

Keep the database **out of the project folder** in every scenario: a `git clean`, a repo move, or
an accidental commit should not be able to take your financial data with it.

## 6a. Rollover and retention — specification for Phase 4

Not yet implemented. Recorded here because the workbook welds two separate rules together,
and reproducing that shape would be a mistake.

**What the workbook does.** `Running Excess` (August!CU4) is:

```
IF(rollover="None",     0,
IF(rollover="Negative", prior_close * IF(prior_close > 0, xlExcessRetention, 1),
IF(rollover="Positive", MAX(0, prior_close),
                        prior_close)))
  + daily classification total
  + IF(this is Excess, "Spend per day", 0)
```

Note the `"Negative"` branch. Despite the name it does **not** restrict to negative values —
`MIN(0, …)` never appears. It carries the whole prior balance and multiplies by
`xlExcessRetention` only when that balance is positive. So "Negative" actually means
*carry everything, with retention applied to a surplus*.

**What we implement instead.** Two independent rules:

| Setting | Controls |
|---|---|
| `classification.rollover` | *which* balances carry: `none`, `credit`, `debit`, `all` |
| `excess_retention` | the proportion of a **credit** balance that carries |

Excess is therefore `rollover = all`, and the retention is applied separately. That gives
the same behaviour as the workbook's `"Negative"` branch without the misleading name, and it
lets any classification use retention rather than just the one the branch was written for.

**▸ Phase 4 must apply retention when `rollover` is `all` or `credit`.** Implementing `all`
as a plain carry-forward would silently drop it — the retention lives in the mode today only
because the workbook put it there.

Sign convention matters here: a classification's running total is
`direction × (debits − credits)`, and Excess has `direction = -1`. So a **positive** Excess
balance means credits exceeded debits — a surplus, i.e. a *credit* balance. That is why
`positive → credit` and `negative → debit` in the vocabulary rename, and why naming the
balance type is less ambiguous than naming the sign.

`excess_retention` is currently **1 (100%)**, so retention is a no-op until changed — the
full balance carries either way.

Two further behaviours to carry across:

- **A daily allowance.** Running Excess adds `Spend per day` (month tab `C54`, currently 30)
  for every day of the month.
- **Future days use projections.** From `CU5` onward, days after today take their value from
  `xlProj<Classification>` rather than from actuals. This is why `Summary!Q19:Y31` shows
  figures for months that have not happened, and why reconcile check D is skipped until the
  projection table exists.

## 6b. Parameters as data — Phase 5

Every phase has moved something out of a formula and into a table. Phase 5 finished the job
for the parameters, so that nothing the model reads is a literal any more.

| Table | Replaces | Why it could not stay a constant |
|---|---|---|
| `salary_profile` | Salary tracker column O | The annual figure was typed into all twelve rows; a pay rise meant editing each one from that point on |
| `bonus` | The `+29028.48` inside May's `P5` | While it sat in the formula, expected gross could not be derived at all — Phase 4 concluded, wrongly, that it was not `salary/12` |
| `cycling_rate` | `IF(commute, 10.5, IF(band, 8.9, IF(gym, 4.6, 0)))` | A fare rise either rewrote every historic row or let old rows claim the new fare. Dating the rate settles both |
| `card_statement` | Month-tab row 46, twelve times | — |
| `account.statement_day` / `payment_day` | Month-tab rows 48 and 50, twelve times | They never varied by month, but changing one meant editing twelve |
| `account_target` | Summary `C23:C26`, one set for whichever month was showing | — |
| `savings_target` | Summary columns G and M | — |
| `account.exclude_from_savings` | The hand-typed subtraction behind 'Less SC & Wed' | The figure silently came to include Tembo while the label did not — see PHASE5_NOTES.md |

### Two decisions worth recording

**Rates are stored as percentages, not fractions.** `salary_assumption.value` holds 8.00 for
8%. The column is integer pence (§3.2), so a fraction resolves only to whole percentage
points — 8.5% would have rounded to 9%. Nothing in the current data needed the precision,
which is exactly why it was worth fixing before something did. The migration is guarded by
the stored schema version rather than by inspecting the values: 0.2 and 20 are both plausible
rates, so a number alone cannot say whether it has already been converted, and running the
rescale twice would turn 20% into 2000%.

**Derived figures are derived, not stored alongside their inputs.** The basic rate band is
`basic_rate_threshold − personal_allowance`, which is what the workbook's `=D28-D22` did.
Phase 4 stored only the result; Phase 5 stores both inputs and computes it. Keeping all three
would let them drift apart the moment one was edited, and there would be no way to tell which
was right. The same reasoning applies to `payslip.expected_gross`, now derived from
`salary_profile` and `bonus` and retained only as a record of what the workbook stated.

## 6c. Nothing is scoped to one fiscal year

Phase 5 left one workbook assumption standing: every month dropdown was built from
`repo.fiscal_periods(tax_year)`, so all of them ran April 2026 to March 2027 and stopped. That
is a property of *one file per year*, not of a database that will hold several. Backfilling
`Budget 25-26.xlsm` would have loaded rows that no dropdown could reach.

The range is now derived:

```
earliest   the first month anything is recorded against, across every table
periods    earliest → the current month
all_periods  earliest → current month + look_forward
```

`look_forward` is a setting (**Settings → General**, default twelve). It exists because some
figures are set *before* the month arrives — next month's savings target, a projection — so a
list that stopped at today would make them unreachable. Trends exposes it as a slider as well,
since the cumulative balances genuinely differ between a six-month and an eighteen-month
horizon; it is a parameter of the calculation, not a display filter.

`fiscal_periods` survives for the reconciliation gate, which compares against a workbook and
therefore *is* scoped to one year.

### Three consequences

**Tax bands are effective-dated for real.** `salary_assumption` always had `effective_from` in
its primary key, but only the allowance taper used it — everything else was written at 1 April
and read back without reference to a date. `repo.bands_from(assumptions, on)` now takes the
last set starting on or before `on`, and each month is taxed under the bands of *its own* tax
year at the values in force on its first day. A mid-year rate change is a new set rather than
an edit that silently rewrites what earlier months were taxed at.

**A month can hold two payments.** `payslip` is keyed by period, so a bonus paid on its own day
had nowhere to go: entering it overwrote the salary. `bonus` therefore carries its own actual
gross, NI, PAYE and net, and the Salary page adds the two together. The alternative — a
surrogate key on `payslip` — would have made every lookup a group-by for the sake of one case.

**A card's cycle is two dates, not a month.** `card_outstanding` used to decide which bill was
standing from the day of the month alone, which is all a month tab could see. It now derives
the statement and its due date from `statement_day` and `payment_day`: where the payment day is
the *smaller* number the bill is collected the following month, which is why Platinum Amex (16th
and 30th) settles inside one month and BA Amex (26th and 9th) does not. The figure subtracted
is the bill actually awaiting collection, which for BA Amex at a July month end is July's, and
on 4 August is still July's.

## 7. Notes

- **The data is sensitive** — salary, account balances, card debt. Keep the app on the LAN: no
  port-forwarding, no QuickConnect, no hosted service without authentication. Do not carry the
  plaintext `Budg3tSpr3ad` password across; it protects nothing in the new design.
- **Back up before each import batch** — the workbook's safety net was "it's one file you can
  copy". Keep that, via `VACUUM INTO` (transactionally safe on a live database, unlike a file copy,
  which can capture a torn state).
- **Decimal, not float** — use `NUMERIC` / `decimal.Decimal` for money. The workbook is already
  carrying artefacts like `1530.0000000000146` and `49.67999999999853`.
- **Keep the identifier** (`0401_BAAM_0`). It is a decent natural key for deduplicating re-imports
  of the same bank statement.
