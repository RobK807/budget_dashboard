# Budget Dashboard

A database-backed replacement for `Budget 26-27.xlsm`.

- [DESIGN.md](DESIGN.md) — architecture, schema, multi-machine plan
- [PHASE0_FINDINGS.md](PHASE0_FINDINGS.md) — migration and reconciliation results
- [PHASE1_NOTES.md](PHASE1_NOTES.md) — the read-only dashboard
- [PHASE2_NOTES.md](PHASE2_NOTES.md) — adding, importing and removing transactions
- [PHASE2B_NOTES.md](PHASE2B_NOTES.md) — cross-machine sync
- [PHASE3_NOTES.md](PHASE3_NOTES.md) — accounts, categories, classifications and settings
- [PHASE4_NOTES.md](PHASE4_NOTES.md) — projections, trends, salary, cards and cycling
- [PHASE5_NOTES.md](PHASE5_NOTES.md) — editable parameters, savings targets, card billing

## Where this runs

**Entirely on your own machine. Nothing is hosted anywhere.**

Streamlit is not a service — it is a small web server that runs as a local Python process.
`streamlit run app.py` starts it, your browser connects to `127.0.0.1:8501`, and it stops
when you close the terminal. There is no cloud account, no external host, and no data
leaving the machine. `.streamlit/config.toml` binds it to loopback, so it is not reachable
from anywhere else on the network — deliberate, given this page shows salary and balances.

So "where is it hosted" has three separate answers:

| | Lives | Notes |
|---|---|---|
| **Code** | This project folder | Copy or clone it to each machine |
| **Python + packages** | `.venv/` in the project folder | Per machine, not shared — never copy `.venv` between machines |
| **Database** | `%LOCALAPPDATA%\BudgetDashboard\budget.db` | **Per machine today.** Sharing it is Phase 2b |

That last row is the one that matters for your desktop.

**Sync is now built** — see [PHASE2B_NOTES.md](PHASE2B_NOTES.md). The master lives on the
NAS at `K:\Private\Finance\budget_db`; each machine keeps a local working copy and pushes
after every write. To get started:

1. On this laptop, open **Manage → Sync** and press **Push now** to create the master.
2. On the desktop, set up as below, then press **Pull now** instead of running the migration.

## Setting up a second machine

1. **Install Python 3.11+** if it is not already there, and get the project folder across
   (git clone, or copy the folder — but not `.venv`).

2. **Create the environment:**

   ```bash
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. **Get the database.** Once the laptop has pushed, the desktop should **pull** rather than
   migrate: run the app, open **Manage → Sync**, and press **Pull now**. That guarantees both
   machines are on the same revision, which re-running the migration would not.

   Only if no master exists yet:

   ```bash
   .venv\Scripts\python.exe -m budget.migrate_xlsm
   ```

4. **Check it:**

   ```bash
   .venv\Scripts\python.exe -m budget.reconcile
   .venv\Scripts\python.exe -m pytest tests -q
   ```

5. **Run it:**

   ```bash
   .venv\Scripts\python.exe -m streamlit run app.py
   ```

   Then open <http://127.0.0.1:8501>.

If `K:` is mapped to a different letter on the desktop, set `BUDGET_WORKBOOK_PATH` rather
than editing code. Every path is resolved in [budget/config.py](budget/config.py) and can be
overridden by environment variable:

| Variable | Default |
|---|---|
| `BUDGET_DB_PATH` | `%LOCALAPPDATA%\BudgetDashboard\budget.db` |
| `BUDGET_WORKBOOK_PATH` | `K:\Private\Finance\Budget 26-27.xlsm` |
| `BUDGET_NAS_DIR` | `K:\Private\Finance\budget_db` |

## Launching it

**Double-click `budget.bat`.** It starts the server and opens
<http://127.0.0.1:8501> after a few seconds. The dashboard runs for as long as that window
stays open — closing it, or pressing Ctrl+C, stops the server.

For a Start Menu or taskbar entry, right-click `budget.bat` → *Send to* → *Desktop (create
shortcut)*, then pin or rename the shortcut. Set its icon under *Properties* if you like.

Or from a terminal in the project folder:

```bash
.venv\Scripts\python.exe -m streamlit run app.py
```

If port 8501 is already taken, Streamlit picks the next free one and prints the URL — so a
second copy will not clash with the first, it will just be on 8502.

## Everyday commands

```bash
python -m budget.reconcile      # check every figure against the workbook
python -m pytest tests -q       # unit tests
python -m budget.import_phase4  # reload projections, salary, cards, cycling
python -m budget.import_phase5  # seed salary profile, rates, targets, card billing
python -m budget.migrate_xlsm --force   # rebuild the database from scratch
```

`reconcile` is worth re-running after any change to the query layer: it checks 264
account-months, ~2,700 daily classification cells, ~490 category figures and the Summary
position against the workbook, and exits non-zero if anything moved unexpectedly.

The test suite includes `tests/test_pages_render.py`, which opens all fourteen pages through
Streamlit's `AppTest` and fails on any uncaught exception — pages are scripts, so a typo in
one would otherwise only surface when someone opened it. It runs against a copy of the
database, never the real one.

## Conventions

**Names sort case-insensitively, everywhere.** Every default ordering in this stack is
ASCII, which puts uppercase before lowercase — `HSBC` before `Halifax`, `ISA` before
`Investments`, `NS&I` before `Nationwide`. Correct by codepoint, wrong to a reader. Any new
list or table must use one of:

| Sorting | Use |
|---|---|
| A SQL query | `select(X).order_by(func.lower(X.name))` |
| A list or set | `ui.alphabetical(values)` |
| A DataFrame | `repo.sort_human(df, by=...)` |
| Pivot columns | `sorted(cols, key=str.casefold)` |

`repo.sort_human` leaves non-text columns alone, so it is safe on mixed keys such as
`["affected", "account"]`.

One trap worth knowing: `repo.casefold_key` tests with `pd.api.types.is_string_dtype`, not
`dtype == object`. pandas 3 gives string columns a dedicated `str` dtype, so an object check
silently stops matching and the sort quietly reverts to ASCII with no error.

Months are the exception — they stay in fiscal order (April → March), since alphabetical
would give "April, August, December".

## Layout

```
app.py                  Streamlit entrypoint and navigation
views/
  summary.py            Summary -- the year at a glance, and account targets
  month.py              Month -- balances, budget, daily spend, cards outstanding
  transactions.py       Transactions -- filtered ledger, remove and restore
  add.py                Add a single transaction
  import_page.py        Bulk import with validation, preview and undo
  cycling_record.py     Record a ride or a running cost
  settings_page.py      Reference data and every periodic parameter
  sync_page.py          Push, pull, offline checkout, reconciliation
  trends_page.py        Cumulative position, rollover and projections
  projections_page.py   Projected against actual spend, and planning a month
  savings_page.py       Savings and investments against target
  salary_page.py        Payslips, the PAYE/NI model and what is left to spend
  cards_page.py         Balance-transfer amortisation
  cycling_page.py       Fares saved against running costs
budget/
  validation.py         transaction rules
  service.py            write operations
  reference.py          CRUD for reference data and periodic parameters
  importer.py           parsing pasted or uploaded tables
  sync.py               cross-machine sync
  tax.py                UK PAYE and NI
  cards.py              card amortisation
  schema.py             in-place schema migrations
  import_phase4.py      projections, salary, cards, cycling
  import_phase5.py      salary profile, rates, targets, card billing
  config.py             all filesystem paths
  models.py             SQLAlchemy schema
  db.py                 engine and session
  postings.py           the posting sign rules
  repo.py               query layer -- every figure the dashboard shows
  xlsm_reader.py        workbook parsing
  migrate_xlsm.py       one-off import
  reconcile.py          verification against the workbook
  ui.py                 shared Streamlit helpers
tests/
```
