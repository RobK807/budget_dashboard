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

**A savings projection is two lines with the same slope.** `repo.savings_projection` carries
the balance forward from the latest actuals and the cumulative target forward from the same
month, both gaining each future month's target. The gap between them is `required` as it
stands, and it stays exactly that wide across the whole projection — saving to plan from here
keeps pace with the target rather than catching up on it. That is the finding, not a defect
of the model, and a chart whose lines quietly converged would assert the opposite. The
per-account view (`repo.savings_by_account`) sums to the overview exactly on balances and on
`required`; a test pins that, because two tables on one page disagreeing about the same money
is worse than either being absent.

**A target only counts in a month its account was open.** A pot cannot be asked to save
anything before it existed or after it was closed, so `repo._live_months` gates both the plan
and the seed, and it does so in `plan_by_period` — one place, because the overview, the two
per-account views and the projection all derive from it and any one of them applying the rule
alone would disagree with the rest. A seed in particular is *what the pot already held*, so
carrying it past closure reported a shortfall against an account shut a year earlier whose
balance had long since left the totals.

The rule drops a stranded target rather than moving it, which is silent and the amounts are
lump sums — one mis-dated month can move the cumulative target by thousands.
`repo.targets_outside_account_life` exists so the Savings page can say so, and an account's
opening date is editable under **Settings → Accounts → Available from** so the fix is a date
change rather than a re-entry. The case that prompted it: a £18,026 one-off dated to the
month the money was expected, on an account opened the month it actually arrived.

Both per-account views are driven by the **account list**, not by what `account_balances`
returns. That function rightly drops an account which is closed and has nothing to show for
the month — but a closed pot's `savings_seed` still counts towards the overview's cumulative
target, so taking the rows from it made the two tables disagree by exactly the seeds of the
pots that had been shut, with nothing on the page to explain the difference. Three closed
accounts carrying £2,000 each is a £6,000 gap. A closed pot is therefore listed while it
still carries a seed or a target, flagged `Closed`, and omitted once it carries neither.

`repo.savings_account_history` is the transpose of it — one pot across every month — and it
walks the **whole** run of periods however short a window the chart draws. Trimming the walk
instead restarts the cumulative target part way through, which reports the pot as far further
ahead than it is and looks entirely plausible on the chart. The caller filters the result.

**Bank identifiers never go in the repository.** Account numbers and sort codes belong in the
database. A test sample that needs one uses an obviously invented stand-in, and
`tests/test_privacy.py` fails on any bare eight-digit run or `nn-nn-nn` sort code in
`budget/`, `views/`, `tests/` or the README. This exists because it already happened: the
first bank-import tests used samples copied out of real exports, and six accounts' details
were committed and pushed before anyone noticed. Source is the one place they cannot be taken
back from.

**Privacy is a switch on Summary, and every page has to opt in.** It hides the headline
figures on every page and every amount on **Salary**, for reading the dashboard where
someone else can see the screen. Nothing is changed on disk, and the sidebar says so while
it is on. Four helpers do the work:

| Where | How |
|---|---|
| A headline metric | `ui.metric(col, label, value)` — pass `sensitive=False` for a count |
| `st.dataframe` | `ui.money_table(df, columns, mask=ui.private())` |
| A Plotly chart | `ui.money_axis(fig, mask=ui.private())` |
| A caption or a form | branch on `ui.private()` yourself |

`cols[0].metric(...)` renders the same headline and ignores the switch, so a new page that
uses it leaks its top row. `tests/test_privacy.py` fails on any `.metric` call whose value is
built with `ui.money` and does not go through `ui.metric`.

Masking a table *replaces* the values rather than formatting them away — `st.dataframe` sends
the frame to the browser beside the display text, so a formatter alone would leave the real
amounts in the page. Masking a chart drops the tick labels **and** turns hover off; without
the second, every point is one hover away from being read off.

Forms are the case that needs judgement. A `number_input` pre-filled from what is stored puts
the figure straight back on screen, so Salary withholds its five entry forms outright and
says so where each one was. Anything new that displays a stored amount in an input needs the
same treatment.

**Importing a bank's own CSV never writes to the ledger.** **Import → Bank files** takes the
exports as the banks give them, fills in what a statement actually knows, and puts the result
in the grid on **Paste / edit** for review. Committing is still a separate, deliberate click.

Adding a bank means adding a `BankFormat` to `budget/bank_formats.py` — where the header is,
which column is which, and which way is out. Four things there are load-bearing:

| | |
|---|---|
| `out_is_negative` | Amex counts a purchase as **positive**; everyone else counts it negative. Get this wrong and every card transaction inverts, with plausible totals. |
| `out_column` / `in_column` | Read by **name**, never by position. Virgin Money's pair is (out, in) and Coventry's is (in, out) — the same two headings reversed. |
| The blank line | Ends the data. Virgin prints an address and six paragraphs of small print after it, and an address parses far enough to become a transaction. |
| `headerless` | HSBC exports no header row at all, so it is the fallback and identifies no account: three accounts share that shape. |

`budget/bank_import.py` then decides what the rows mean, in four passes: type from direction,
drop what the ledger already holds, pair transfers, then apply the rules. Two of those need
care.

*Duplicates* are matched on account, direction, amount and a date within a few days — a card
posts on a different day from the one you wrote down. Matching **consumes**: two identical
purchases on one day need two ledger entries to be dropped. The index is built from
`repo.load_postings`, not from transactions, so a stored Transfer counts against **both** of
its accounts; otherwise the second bank's file brings the same money in again.

*Transfers* are found by pairing opposite movements of equal amount across two accounts in
the same batch. That is why importing several files together beats one at a time, and why it
beats reading the description: 'AMERICAN EXPRESS DD' cannot say which card it paid, and both
of them appear. The rules under **Settings → General → Transfer rules** are the fallback for
a counterpart that is not in the batch.

**Wording is never treated as evidence of a transfer.** A movement with no counterpart on
another of your accounts and no rule is recorded as the plain debit or credit its bank called
it. It is tempting to mark the ones that *read* like transfers — 'withdrawal to', 'payment
received' — but the description cannot settle it: money leaving a joint account for the other
holder's own account is worded identically to an internal transfer and is an ordinary debit.
A marker on those is raised every month and can never be resolved, so it stops being read.
The matching movement is the evidence; where there is none, the direction the bank gave the
row stands.

Banks export further back than the ledger goes for a quiet account, so anything **more than a
week before** that account's last recorded movement is held back by default. Without it, an
ordinary import backfills months on the strength of an upload; without the week's grace it
would also swallow genuine gaps, since an account is not written up in strict date order and
its last entry is a rough edge rather than a watermark. Whatever the grace lets through still
faces the duplicate check — that runs first, so a row that is both old and already recorded is
reported as recorded, which stays true however the window is set.

**Every exclusion is reversible.** Each left-out row is itemised with its reason and a tick
box, and ticking one re-runs the whole decision with that row spared rather than patching the
result — because reversing an exclusion has consequences beyond the row itself. Sparing half
of a paired transfer has to un-pair the other half, or the same money is counted twice. A
spared row bypasses every test and arrives as a plain movement; `SourceRow.key` (`file#line`)
is what survives the rerun, since a position in a list changes as rows are reinstated.

**The dashboard explains itself, not its predecessor.** Screen text says how the tool works;
it does not compare itself to the spreadsheet this replaced, and it never cites a cell
reference — `the tracker's L3:N10` means nothing to a reader who has never opened that file,
and less every year. Source docstrings and comments are exempt: they record why the design is
what it is, and nobody reads them from the browser. `tests/test_privacy.py` pins this over
every string literal in `views/` that is not a docstring.

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
  summary.py            Summary -- the year at a glance, account targets, privacy switch
  month.py              Month -- balances, budget, daily spend, cards outstanding
  transactions.py       Transactions -- filtered ledger, remove and restore
  add.py                Add a single transaction
  import_page.py        Bank files, bulk import with validation, preview and undo
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
  bank_formats.py       one declaration per bank's own CSV export
  bank_import.py        dedupe against the ledger, pair transfers, fill the grid
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
