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

2. **Open a terminal in the project folder**, and stay there for everything below. Every
   command from here on is relative to it — `.venv` is created in whichever directory you
   happen to be in, and `.venv\Scripts\python.exe` only resolves from the project root:

   ```bash
   cd path\to\budget_dashboard
   ```

   In Explorer, Shift-right-click the folder and choose *Open in Terminal*, or type `cmd`
   into the address bar.

3. **Create the environment:**

   ```bash
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. **Get the database.** Once the laptop has pushed, the desktop should **pull** rather than
   migrate — double-click `restore.bat`, or run the app, open **Manage → Sync** and press
   **Pull now**. That guarantees both machines are on the same revision, which re-running the
   migration would not.

   Only if no master exists yet:

   ```bash
   .venv\Scripts\python.exe -m budget.migrate_xlsm
   ```

5. **Check it:**

   ```bash
   .venv\Scripts\python.exe -m budget.reconcile
   .venv\Scripts\python.exe -m pytest tests -q
   ```

6. **Run it:**

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

**Closing the browser tab closes the dashboard**, and the console window with it. The window
prints what it is waiting for:

```
  [dashboard] will close this window 15s after the last browser tab is closed.
  [dashboard] browser closed; stopping in 15s unless you come back.
```

If it does not close, **something is still connected** — most often the dashboard open in a
second tab or another browser, since it stays up while anything is looking at it. `stop.bat`
ends it outright. There is a delay of up to `server.disconnectedSessionTTL` (30 seconds, in
`.streamlit/config.toml`) before a closed tab is noticed, because Streamlit holds a session
briefly in case the browser comes back.

**Double-click `budget.bat`.** It starts the server and opens
<http://127.0.0.1:8501> after a few seconds. The dashboard runs for as long as that window
stays open — closing it, or pressing Ctrl+C, stops the server.

### Pinning it to the taskbar

Windows offers *Pin to taskbar* for programs, and a `.bat` is not one — the option simply
does not appear, for the shortcut either. The way round it is to point a shortcut at
`cmd.exe`, which **is** a program, and hand it the batch file as an argument.

1. Right-click `budget.bat` → *Show more options* → *Create shortcut*. A
   `budget.bat - Shortcut` appears beside it.
2. Right-click that shortcut → *Properties*.
3. Set **Target** to, with your own path:

   ```
   cmd.exe /c "C:\Users\you\PycharmProjects\budget_dashboard\budget.bat"
   ```

4. Set **Start in** to the project folder itself:

   ```
   C:\Users\you\PycharmProjects\budget_dashboard
   ```

5. *Change Icon* if you like — `%SystemRoot%\System32\imageres.dll` holds the stock set.
   The default is the cmd icon, which is indistinguishable from a terminal on the taskbar.
6. *Apply*, then right-click the shortcut again → *Pin to taskbar*.

Rename it to something like **Budget** first: the pin keeps whatever name the shortcut had.

The console window still opens and still has to stay open, exactly as double-clicking does —
`/c` runs the batch file and closes when it ends, and the dashboard *is* that window.

Shortcuts are not tracked (`*.lnk` is in `.gitignore`). They hold an absolute path, so one
made here would be wrong on another machine and would go stale if this checkout moved.

Or from a terminal in the project folder:

```bash
.venv\Scripts\python.exe -m streamlit run app.py
```

If port 8501 is already taken, Streamlit picks the next free one and prints the URL — so a
second copy will not clash with the first, it will just be on 8502.

## When it will not start

Double-clickable helpers sit beside `budget.bat`:

| | What it does |
|---|---|
| **`diagnose.bat`** | Reports what the app sees: the resolved database path, the file's own sha256 and index, what is in the folder, the row counts for the tables the pages report on, and the exact error if it cannot be read. Read-only — it changes nothing. |
| **`repair.bat`** | Rebuilds damaged *indexes* with REINDEX. Lossless, because the rows were never the problem. Backs up first, refuses while anything holds the file, and says plainly when the damage is to the pages instead, where REINDEX cannot help. |
| **`restore-data.bat`** | Re-applies the data work that lives in scripts: the savings plan, the interest gross/net basis, the expected return, the split of transaction 582, and clearing the workbook's assumptions off unpaid months. Idempotent. |
| **`stop.bat`** | Kills whatever holds port 8501. Closing the console window does not reliably stop the server on Windows, and a leftover process serves stale code to the browser. |
| **`restore.bat`** | Rebuilds the local database from the NAS master, for when the dashboard will not start and the Sync page therefore cannot be reached. Refuses if there is unpushed work locally — `python -m budget.restore --discard-local` overrides that, keeping the current database as `budget.discarded-<timestamp>.db`. |

Run `diagnose.bat` first — it is a fresh process with nothing cached between it and the file,
which is what makes it the reading to trust when the app and the database appear to disagree.

**`wrong # of entries in index …`** is index damage: run `repair.bat`, then `restore-data.bat`
if the savings targets are missing afterwards. **A page reporting nothing** where the file has
rows is a stale read — press *Refresh data*, and if that fails the process is older than the
code, so use `stop.bat` and start again. **The folder missing** is what `restore.bat` is for.

**A red "Conflict" on the Sync page** means both machines moved: this one has unpushed work
*and* the master has been pushed by the other since. Push and pull are both refused on
purpose — one would overwrite the other machine's work, the other would discard this
machine's. To get out of it, on the machine showing the conflict:

1. **Manage → Sync → Conflict** — download the local-only transactions as CSV.
2. Open **I have the export — discard these changes and pull**, tick the confirmation, and
   pull. The current database is *moved aside*, not deleted, as
   `budget.discarded-<timestamp>.db` beside the live one.
3. **Record → Import** — feed the CSV back in. It arrives as its own undoable batch.
4. Push.

The export covers **transactions only**. A setting, target or payslip changed on that
machine is not in it and needs re-entering after step 2 — the discarded database is still
there to check against.

**"This dashboard is older than the database it is pointed at"**, or a sync badge reading
**"Update needed"**, is not a data problem and no script will fix it. The other machine is
running newer code, and this one is being kept away from data it would misread rather than
being allowed to guess. Update this machine (`git pull`, then re-run the install step) and
start it again. Nothing is changed in the meantime — both pushing and pulling refuse, because
schema migrations only ever run forwards.

Rebuilding from the workbook (`migrate_xlsm`) is the last resort, not the first: it resets the
sync revision and loses anything entered since the workbook stopped being the source of truth.

## The HTML copies of these notes

Every `.md` in the project has an `.html` beside it, for reading without a Markdown viewer.
They are generated, not written:

```bash
.venv\Scripts\python.exe -m budget.render_docs
```

`--check` reports which have drifted without writing anything, and the test suite runs it —
so editing a document and forgetting to regenerate its page is a failing test rather than a
page that quietly says something out of date. Which is what happened to `README.html`: it
was produced once at the start and left, and by the time anyone opened it, it described a
dashboard that had moved on considerably.

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

It is also the one place still scoped to a single fiscal year, deliberately: it compares
against `Budget 26-27.xlsm`, so April-to-March is what it should cover. Everywhere else the
month lists run from the first month anything is recorded against to the current one, plus
the look-forward set under **Settings → General**.

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

Months are the exception — they stay in chronological order, since alphabetical would give
"April, August, December".

**Money carries a thousands separator, percentages are quoted to two places.** Three places
need saying explicitly, because each has its own formatter and only the first is obvious:

| Where | How |
|---|---|
| `st.dataframe` | `ui.money_table(df, columns)` — a pandas Styler, `£{:,.2f}` |
| `st.data_editor` | `ui.editable_money(label)` — sprintf-js, `£%,.2f` |
| A Plotly chart | `ui.money_axis(fig)` — `,.2f` on ticks and hover |

The middle one is the trap. `st.column_config.NumberColumn` takes a printf format and printf
has no thousands flag, so `"£%.2f"` gives £39255.98. Streamlit parses these with sprintf-js,
which *does* treat `,` as one. `st.number_input` is the exception: its output must be purely
numeric, so it stays at `%.2f` with the unit in the label.

Without `ui.money_axis`, Plotly's default SI notation draws £10,000 as `10.00000k`.

**Never chain a second `.format()` onto a Styler.** A non-money column that needs its own
format goes through the same call:

```python
ui.money_table(df, ["credit_limit"], formats={"min_pct": "{:,.2f}"})   # right
ui.money_table(df, ["credit_limit"]).format({"Minimum %": "{:.2f}"})   # silently wrong
```

`Styler.format` with no `subset` walks *every* column and assigns a display function to each,
handing the ones its dict does not mention back to the default. So the second call replaces
the first rather than adding to it, and the money columns lose their pound sign and separator
with no error. This is what had un-formatted the Cards page and Settings → Cards.

Missing values render as `—`, not `£nan`: a month with no payslip has no NI, and that is not
the same as zero.

**There are two tax-year functions and they are not interchangeable.**

| Use | For |
|---|---|
| `repo.tax_year_of(period)` | Anything keyed by month — a payslip, a set of bands |
| `repo.tax_year_of_date(date)` | Anything keyed by a day — interest, donations |

The UK tax year runs 6 April to 5 April, so a payment dated 1–5 April belongs to the year
before. A period is only ever right to the month and cannot express that; the interest
tracker worked around it by splitting April into two hand-labelled rows. Reach for the date
version whenever the thing being grouped has a date of its own.

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
  salary_page.py        Payslips, the PAYE/NI model, the tax year to date, what is left
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
