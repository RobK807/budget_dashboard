"""Shared Streamlit helpers: cached loading, formatting, common chrome."""

from __future__ import annotations

import time
from decimal import Decimal

import pandas as pd
import streamlit as st

from budget import config, repo
from budget.db import make_engine, make_session_factory

ACCENT = "#4C78A8"
POSITIVE = "#54A24B"
NEGATIVE = "#E45756"
# For reference lines drawn over data -- targets, expected returns. Deliberately outside the
# categorical palette the bars come from, so it cannot be read as another series, and bright
# enough to survive both the dark theme and a column crossing underneath it. Plotly's default
# is near-black, which disappeared into both.
REFERENCE = "#F2B701"


@st.cache_resource
def _engine():
    engine = make_engine()
    # Applied once per process, on the cached engine: an existing database created before a
    # schema change is brought up to date, and any table added since is created, rather than
    # failing at the first read.
    from budget.db import create_all

    create_all(engine)
    return engine


@st.cache_resource
def _session_factory():
    return make_session_factory(_engine())


def session():
    """A session for write operations. Use as `with ui.session() as s, s.begin():`."""
    return _session_factory()()


def close_connections() -> None:
    """Drop every pooled connection to the local database.

    Required before anything *replaces* the file rather than writing through it -- a pull,
    or a restore. Closing a Session only returns its connection to the pool; the engine keeps
    the file handle and the WAL open, so replacing the database underneath it corrupts the
    result. The engine is cached for the life of the process, so without this it is still
    holding the old file when the new one lands.
    """
    try:
        _engine().dispose()
    except Exception:  # noqa: BLE001 -- a broken engine is exactly what we are clearing
        pass
    _engine.clear()
    _session_factory.clear()


def database_check() -> dict:
    """Whether the database is there -- and, when it is not, everything needed to say why.

    Path.exists() swallows OSError and returns False, so a file briefly locked by another
    process (a migration mid-write, an antivirus scan) is indistinguishable from one that was
    never created. Telling someone their financial data is missing when it is merely busy is
    the worse failure of the two, so a stat that raises is retried, and a directory holding a
    -wal or -journal file is taken as proof the database is there and in use.

    The diagnosis matters as much as the verdict: a bare False sends you hunting for a file
    that may well be sitting exactly where it should, and says nothing about which of the
    several possible causes it was.
    """
    import getpass
    import os
    import platform

    path = config.DB_PATH
    report = {
        "ok": False,
        "path": str(path),
        "reason": "",
        "localappdata": os.environ.get("LOCALAPPDATA", "(not set)"),
        "env_override": os.environ.get("BUDGET_DB_PATH", "(not set)"),
        "machine": platform.node(),
        "user": getpass.getuser(),
        "parent_exists": False,
        "siblings": [],
        "error": "",
    }

    try:
        report["parent_exists"] = path.parent.is_dir()
        if report["parent_exists"]:
            report["siblings"] = sorted(p.name for p in path.parent.iterdir())
    except OSError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    for attempt in range(3):
        try:
            size = path.stat().st_size
            if size > 0:
                report["ok"] = True
                return report
            report["reason"] = "the file is there but empty (0 bytes)"
            return report
        except FileNotFoundError as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            if _has_sidecar(path):
                report["ok"] = True
                return report
            report["reason"] = (
                "no such file, and no -wal/-shm beside it to suggest it is merely open"
            )
            return report
        except OSError as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            if attempt == 2:
                if _has_sidecar(path):
                    report["ok"] = True
                    return report
                report["reason"] = "the file could not be read after three attempts"
                return report
            time.sleep(0.2)

    return report


def schema_check() -> str | None:
    """None if this code can read the database, else why it cannot.

    Building the engine is what runs the migrations, so this is also what performs them --
    doing it here rather than at the first read means a database written by newer code stops
    the app at the front door with an explanation, instead of surfacing as a KeyError on
    whichever page happened to touch the unfamiliar column first.
    """
    from budget.schema import SchemaTooNew

    try:
        _engine()
    except SchemaTooNew as exc:
        return str(exc)
    return None


def database_exists() -> bool:
    return database_check()["ok"]


def _has_sidecar(path) -> bool:
    """A -wal or -journal beside the database means SQLite has it open right now."""
    try:
        return any(
            path.with_name(path.name + suffix).exists()
            for suffix in ("-wal", "-journal", "-shm")
        )
    except OSError:
        return False


def db_fingerprint() -> tuple:
    """Cheap identity for the database file: when it was last written, and how big it is.

    Used as a cache key so a cached read cannot outlive its source. Anything that changes the
    file changes this -- a one-off script, a sync pull, a restore, the seed -- and the next
    script run reads afresh rather than serving what was true when the process started.

    Without it the cache was keyed on nothing at all, so a dashboard that happened to load
    while a table was empty went on reporting it empty for as long as it stayed open. That is
    exactly what made 25 stored savings targets read as 'no plan set yet', on a database that
    had them, in a process running the right code.

    Size as well as mtime because a filesystem timestamp is only so precise, and two writes
    within the same tick are not far-fetched when a script is doing the writing.

    **The -wal matters as much as the database.** This application runs SQLite in WAL mode,
    where a write lands in budget.db-wal and the main file is not touched until a checkpoint.
    Fingerprinting budget.db alone therefore misses every recent write -- which the first
    version of this did, and it was caught only by changing a target from outside a running
    dashboard and watching it go on showing the old figure.
    """
    parts = []
    for suffix in ("", "-wal"):
        path = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        try:
            stat = path.stat()
            parts.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            # Missing or unreadable: a constant, so the cache behaves as it used to rather
            # than thrashing on every rerun. A checkpointed database has no -wal at all.
            parts.append((0, 0))
    return tuple(parts)


@st.cache_data(ttl=300)
def _load_all(fingerprint: tuple) -> dict:
    """Everything the dashboard needs, in one cached read.

    The whole year is a few thousand rows, so loading it wholesale and slicing in pandas is
    simpler than round-tripping per page and fast enough not to matter.

    `fingerprint` is not read -- it is the cache key. See `db_fingerprint`.
    """
    with _session_factory()() as session:
        reference = repo.load_reference(session)
        outgoings, ridden = repo.load_cycling(session)
        tax_year = int(reference["settings"].get("tax_year", 0))
        data = {
            **reference,
            "postings": repo.load_postings(session),
            "transactions": repo.load_transactions(session, include_deleted=True),
            "openings": repo.load_opening_balances(session),
            "budgets": repo.load_budgets(session),
            "projections": repo.load_projections(session),
            "allowances": repo.load_allowances(session),
            "class_openings": repo.load_class_openings(session),
            "payslips": repo.load_payslips(session),
            "bands": repo.salary_bands(session, tax_year),
            "assumptions": repo.load_salary_assumptions(session, tax_year),
            "tax_years": repo.assumption_tax_years(session) or [tax_year],
            "salary_profiles": repo.load_salary_profiles(session),
            "bonuses": repo.load_bonuses(session),
            "cards": repo.load_cards(session),
            "cycling_outgoings": outgoings,
            "cycling_days": ridden,
            "cycling_rates": repo.load_cycling_rates(session),
            "card_statements": repo.load_card_statements(session),
            "account_targets": repo.load_account_targets(session),
            "savings_plan": repo.load_savings_plan(session),
            "savings_adjustments": repo.load_savings_adjustments(session),
            # The pre-split record, kept so the old figures can still be seen. The live
            # targets are derived from the plan below.
            "stored_savings_targets": repo.load_savings_targets(session),
        }
    # Every month dropdown used to be repo.fiscal_periods(tax_year) -- April 2026 to March
    # 2027 and no further, because that is how many months a workbook had. Here the range
    # starts at the first month anything is recorded against and ends at today, so a second
    # year of history extends the lists rather than falling outside them.
    data["tax_year"] = tax_year
    data["earliest_period"] = repo.earliest_period(
        data["postings"]["period"] if not data["postings"].empty else None,
        data["openings"]["period"] if not data["openings"].empty else None,
        data["budgets"]["period"] if not data["budgets"].empty else None,
        data["allowances"]["period"] if not data["allowances"].empty else None,
        data["class_openings"]["period"] if not data["class_openings"].empty else None,
        data["payslips"]["period"] if not data["payslips"].empty else None,
        data["bonuses"]["period"] if not data["bonuses"].empty else None,
        data["card_statements"]["period"] if not data["card_statements"].empty else None,
        data["account_targets"]["period"] if not data["account_targets"].empty else None,
        (
            data["stored_savings_targets"]["period"]
            if not data["stored_savings_targets"].empty
            else None
        ),
        (
            data["projections"]["date"].map(repo.period_of)
            if not data["projections"].empty
            else None
        ),
        default=repo.fiscal_periods(tax_year)[0],
    )
    # `periods` is what most pages offer; `all_periods` runs on past the current month for
    # the things that plan ahead -- next month's targets, a projection, a future payslip.
    data["look_forward"] = int(data["settings"].get("look_forward_months", 12) or 0)
    data["periods"] = repo.span(data["earliest_period"])
    data["all_periods"] = repo.span(data["earliest_period"], data["look_forward"])
    # Derived from the per-account plan rather than stored beside it, so the headline and the
    # breakdown cannot disagree. Over `all_periods`, since a target for a month that has not
    # arrived yet is the point of having one.
    data["savings_targets"] = repo.targets_from_plan(
        data["savings_plan"], data["accounts"], data["all_periods"],
        data["savings_adjustments"],
    )
    data["plan_detail"] = repo.plan_by_period(
        data["savings_plan"], data["accounts"], data["all_periods"],
        data["savings_adjustments"],
    )
    data["bucket_targets"] = repo.targets_by_bucket(
        data["savings_plan"], data["accounts"], data["all_periods"],
        data["savings_adjustments"],
    )
    return data


def load_all() -> dict:
    """The cached read, keyed on the state of the database file."""
    return _load_all(db_fingerprint())


# Every page and every write path calls ui.load_all.clear(); keep that working now that the
# cache lives on the inner function.
load_all.clear = _load_all.clear


# What this build of the code expects the loaded data to carry.
#
# Streamlit re-executes a page script from disk on every rerun but does not re-import modules
# already in sys.modules, and `load_all` is cached for five minutes on top of that. So a
# process started before a change can run a new views/ against an old budget/ -- and the page
# dies with a bare KeyError naming a column, which says nothing about the cause. The cause is
# always the same and the fix is always the same: restart.
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "accounts": ("exclude_from_savings", "interest_net"),
    "transactions": ("is_donation",),
}
EXPECTED_KEYS = ("savings_plan", "plan_detail", "savings_targets")


def _stale_build(data: dict) -> list[str]:
    """Anything this code needs that the loaded data does not have."""
    missing = [f"data[{key!r}]" for key in EXPECTED_KEYS if key not in data]
    for name, columns in EXPECTED_COLUMNS.items():
        frame = data.get(name)
        if frame is None:
            missing.append(f"data[{name!r}]")
            continue
        missing += [f"{name}.{c}" for c in columns if c not in frame.columns]
    return missing


def alphabetical(values) -> list:
    """Case-insensitive sort for dropdown options, with blanks dropped.

    Plain sorted() is ASCII, so it puts HSBC before Halifax and ISA before Investments --
    correct by codepoint, wrong to a reader.

    `is not None` was not enough: a missing value arrives from pandas as float NaN, which is
    not None, so every filter built from a column that transfers leave empty offered 'nan' as
    something to pick.
    """
    return sorted(
        {v for v in values if v is not None and not (isinstance(v, float) and pd.isna(v))},
        key=lambda v: str(v).casefold(),
    )


def auto_push(label: str = "write") -> None:
    """Push after a committed write, so the window in which two machines can diverge is
    seconds rather than days.

    Suppressed in deliberate offline mode -- the whole point of a checkout is to stop
    trying. An unreachable NAS is reported quietly: away from home it is the normal state,
    and an error banner on every entry would train you to ignore the real ones.
    """
    from budget import sync

    with session() as s, s.begin():
        state = sync.status(s)
        if state.local.mode == sync.OFFLINE:
            st.caption(f"Offline mode — {state.local.pending} change(s) held for check-in.")
            return
        if not state.nas.reachable:
            st.caption(f"Not synced — NAS unreachable. {state.local.pending} change(s) pending.")
            return
        if state.conflict:
            st.warning("Not synced — this machine and the master have both moved. See Sync.")
            return
        if state.behind:
            st.warning(
                f"Not synced — the master is at revision {state.nas.revision} and this "
                "machine is behind. Pull on the Sync page first, then re-enter this."
            )
            return
        if state.blocked_by:
            st.warning("Not synced — see the Sync page.")
            return
        result = sync.push(s)

    if result.ok:
        st.caption(f"Synced to the NAS after {label}.")
    else:
        st.caption(f"Not synced: {result.message}")


def split_at_zero(x, y):
    """One series as two, so the part below the axis can be drawn in a different colour.

    Returns `(x, above, below)` sharing one x axis, with None wherever the other half owns
    the point. A crossing is interpolated and added to both at exactly zero, so the two lines
    meet on the axis instead of leaving a gap the width of one sample -- which on a daily
    series is a visible break every time the running total passes through nothing.

    Plotly colours a line per *trace*, not per segment, which is why this cannot be done by
    handing it a list of colours.
    """
    xs: list = []
    above: list = []
    below: list = []

    for index, (point, value) in enumerate(zip(x, y)):
        if index:
            previous_x, previous_y = x[index - 1], y[index - 1]
            crosses = (previous_y < 0) != (value < 0)
            if crosses and previous_y != value:
                share = (0 - previous_y) / (value - previous_y)
                xs.append(previous_x + (point - previous_x) * share)
                above.append(0.0)
                below.append(0.0)
        xs.append(point)
        # Zero belongs to both, so a line that touches the axis without crossing it stays
        # joined rather than ending and restarting.
        above.append(value if value >= 0 else None)
        below.append(value if value <= 0 else None)

    return xs, above, below


def money(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"£{Decimal(str(value)):,.2f}"


def percent(value, places: int = 2) -> str:
    """A rate as it is quoted -- 80.00, not 0.8.

    Fractions belong in the arithmetic, not on the screen. Everything stored as a fraction
    (retention, a card's minimum payment) is multiplied out at the point of display; anything
    already held as a percentage (the tax rates) is passed straight through.
    """
    if value is None or pd.isna(value):
        return "-"
    return f"{Decimal(str(value)):,.{places}f}"


# printf-style, which is what st.column_config takes. sprintf-js treats ',' as a thousands
# flag, so an editable money column can carry the separator that Styler formats already had
# -- '£%.2f' gave £39255.98 in every data_editor in the app.
MONEY_FORMAT = "£%,.2f"
PLAIN_MONEY_FORMAT = "%,.2f"


def to_float(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Decimal is right for storage and sums; float is what charts and column_config want."""
    out = df.copy()
    for c in columns:
        if c in out.columns:
            out[c] = out[c].astype(float)
    return out


def heatmap(df: pd.DataFrame):
    """Diverging red/blue shading for a numeric matrix.

    Hand-rolled rather than Styler.background_gradient, which pulls in matplotlib -- a
    large dependency to carry for one table's background colours.
    """
    vmax = float(df.abs().to_numpy().max() or 0)

    def shade(value):
        if not vmax or pd.isna(value) or value == 0:
            return ""
        alpha = min(abs(float(value)) / vmax, 1.0) * 0.45
        rgb = "76,120,168" if value > 0 else "228,87,86"
        return f"background-color: rgba({rgb},{alpha:.2f})"

    return df.style.format("£{:,.2f}").map(shade)


def money_table(
    df: pd.DataFrame,
    money_columns: list[str],
    labels: dict[str, str] | None = None,
    formats: dict[str, str] | None = None,
    integers: list[str] | None = None,
):
    """Table with thousands separators on the money columns.

    st.column_config.NumberColumn takes a printf format, and printf has no thousands
    separator -- '%.2f' gives £39255.98. A pandas Styler does support '{:,.2f}' and keeps the
    underlying values numeric, so sorting still works on magnitude rather than on text.

    `formats` is for the non-money columns that still need a format -- a percentage, say --
    and exists because the obvious spelling of that is silently wrong:

        ui.money_table(...).format({"Minimum %": "{:.2f}"})

    Styler.format with no `subset` walks *every* column and assigns a display function to
    each, falling back to the default for any the dict does not mention. So the second call
    does not add to the first, it replaces it, and the money columns lose their pound sign
    and separator. That is what had happened to the Cards page and to Settings > Cards.
    Passing both through one call means there is only ever one dict to overwrite.

    `integers` is for a column of whole numbers that has blanks in it -- a day of the month,
    say. One missing value makes the whole column float, and a payday then reads '1.0'.
    Int64 is pandas' nullable integer: the blanks survive as <NA> and the rest stay whole.
    """
    out = df.copy()
    spec = dict.fromkeys(money_columns, "£{:,.2f}")
    whole: list[str] = []
    for column in integers or []:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
            # Given a format as well as the type, so the blanks pick up na_rep below rather
            # than rendering as pandas' own '<NA>'.
            spec[column] = "{:,.0f}"
            whole.append(column)
    spec.update(formats or {})
    if labels:
        out = out.rename(columns=labels)
        spec = {labels.get(c, c): f for c, f in spec.items()}
        whole = [labels.get(c, c) for c in whole]
    # Everything but the whole-number columns: to_float would undo the Int64 that is the
    # whole point of them, and hand back the '1.0' this exists to avoid.
    out = to_float(out, [c for c in spec if c not in whole])
    # A blank is not zero: a month with no payslip yet has no NI, and '£nan' says that
    # badly. The dash matches ui.name_blanks, so an empty cell reads the same everywhere.
    return out.style.format(spec, na_rep="—")


def currency_columns(*names: str) -> dict:
    return {
        n: st.column_config.NumberColumn(n.replace("_", " ").title(), format=MONEY_FORMAT)
        for n in names
    }


def money_axis(fig, axis: str = "y", label: str | None = None):
    """Thousands separators on a chart's value axis and in its hover text.

    Plotly's default tick format is SI-prefixed, so £10,000 is drawn as '10.00000k' -- a
    notation nobody reading a bank balance wants. `,.2f` is the same format the tables use,
    so a figure means the same thing wherever it appears.
    """
    settings = {"tickformat": ",.2f", "hoverformat": ",.2f"}
    if label is not None:
        settings["title_text"] = label
    if axis == "y":
        fig.update_yaxes(**settings)
    else:
        fig.update_xaxes(**settings)
    return fig


def default_range(
    days: int = 90, earliest=None, latest=None
) -> tuple["dt.date", "dt.date"]:
    """A date filter's opening position: the last `days` days, ending today.

    Relative to today rather than to the extent of the data, so the default does not silently
    widen as history accumulates. Clamped to what actually exists where bounds are given,
    since a date_input rejects a value outside its own min/max.
    """
    import datetime as dt

    end = dt.date.today()
    if latest is not None:
        end = min(end, latest) if earliest is None else max(min(end, latest), earliest)
    start = end - dt.timedelta(days=days)
    if earliest is not None:
        start = max(start, earliest)
    return start, end


def running_totals(data: dict, periods: list[str] | None = None):
    """Daily running totals and month-end closes from the rollover engine (DESIGN.md 6a).

    `periods` overrides how far ahead to carry the chain. It has to be an argument rather
    than a fixed year: the balances are cumulative, so a look-forward of six months and one
    of eighteen are different calculations, not the same one truncated.
    """
    from decimal import Decimal

    retention = Decimal(str(data["settings"].get("excess_retention", 1)))
    return repo.running_by_period(
        data["postings"],
        data["projections"],
        data["allowances"],
        data["classifications"],
        periods or data["all_periods"],
        retention,
        openings=data["class_openings"],
    )


def cycling_rates(data: dict) -> dict:
    """The rate in force today, for the headline caption.

    Individual days are valued at the rate that applied on the day -- see
    repo.cycling_savings_dated -- so this is a summary, not what the totals are built from.
    """
    import datetime as dt

    rates = data["cycling_rates"]
    today = dt.date.today()
    return {key: repo.rate_in_force(rates, key, today) for key in ("commute", "band", "gym")}


def newest_first(frame: pd.DataFrame) -> pd.DataFrame:
    """Reverse a month-ordered table so the most recent month is the first row.

    Charts stay in time order -- a line running right to left is unreadable -- but a table
    is looked at rather than traced, and the month wanted is almost always the last one. With
    a second year backfilled that is now two dozen rows down.

    Not for a schedule that runs forwards by nature: a balance transfer card's payoff plan is
    a sequence to follow from where it starts, not a history to scan back through.
    """
    return frame.iloc[::-1]


def month_from(
    periods: list[str],
    key: str,
    tax_year: int | None = None,
    label: str = "Show from",
) -> str:
    """A 'from this month' picker for a chart. Returns the chosen period.

    Backfilling a second year doubled the span every chart drew, which is more than most of
    them can say anything with. This trims the *drawing*, and only the drawing: a running
    total is cumulative, so trimming what goes into it would change the answer rather than
    the view. Each caller filters its own plotting frame with the period this returns.

    Defaults to the start of the current tax year where that is in range -- the most recent
    complete story -- rather than to the earliest month there is.
    """
    if not periods:
        return ""
    default = repo.fiscal_periods(tax_year)[0] if tax_year else periods[0]
    if default not in periods:
        default = periods[0]
    return st.selectbox(
        label,
        options=periods,
        index=periods.index(default),
        format_func=repo.period_label,
        key=key,
        help="The earliest month the charts below show. Figures are unaffected — this "
             "changes the window, not the arithmetic.",
    )


def periods_with_data(data: dict) -> list[str]:
    used = set(data["postings"]["period"].unique()) if not data["postings"].empty else set()
    return [p for p in data["periods"] if p in used]


def page_header(title: str, subtitle: str = "") -> dict:
    """Per-page chrome. set_page_config lives in app.py because st.navigation runs the
    entrypoint and the page in a single script run, so it may only be called once."""
    data = load_all()

    stale = _stale_build(data)
    if stale:
        st.title(title)
        st.error(
            "**This dashboard is running older code than it is displaying.**\n\n"
            "Close every dashboard window and start it again — `run.bat`, or whatever you "
            "launched it with. Nothing is wrong with your data.\n\n"
            "Streamlit re-reads a page from disk on every rerun but keeps the modules "
            "behind it in memory, so after an update the two can be a version apart. "
            "**Refresh data** in the sidebar will not fix it; only a restart will."
        )
        st.caption("Missing: " + ", ".join(stale))
        st.stop()

    st.title(title)
    if subtitle:
        st.caption(subtitle)

    with st.sidebar:
        st.caption("Financial year")
        st.write(f"**{data['tax_year']}/{str(data['tax_year'] + 1)[-2:]}**")
        live = data["transactions"][~data["transactions"]["deleted"]]
        st.caption("Transactions")
        st.write(f"**{len(live):,}** live")
        st.divider()
        sync_badge()
        st.divider()
        if st.button("Refresh data", width="stretch"):
            load_all.clear()
            st.rerun()

    return data


def sync_badge() -> None:
    """Persistent sync state.

    Deliberately always visible rather than a toast: the laptop will routinely be off the
    network, and if that state disappears after a few seconds the genuine conflicts get
    missed along with it.
    """
    from budget import sync

    with session() as s:
        state = sync.status(s)

    icon = {"ok": "🟢", "warning": "🟡", "error": "🔴"}[state.tone]
    st.caption("Sync")
    st.write(f"{icon} {state.label}")
    if state.blocked_by:
        st.caption(f"Locked by {state.blocked_by.machine}")

    # One button, and only where it is the right one. Amber covers four different states and
    # the action differs by state: behind wants a pull, unpushed work wants a push, an
    # unreachable NAS wants neither, and offline mode is deliberate. Green needs nothing and
    # red needs the Sync page, where the consequences can be explained -- a conflict button
    # here would be a one-click way to lose a machine's work.
    if state.tone != "warning" or not state.nas.reachable or state.local.mode == sync.OFFLINE:
        return

    if state.behind:
        # Safe by definition: nothing here is unpushed, so adopting the master loses nothing.
        if st.button("Pull now", width="stretch", key="sidebar_pull"):
            close_connections()  # before the file is replaced, not after
            result = sync.pull()
            load_all.clear()
            st.session_state["sidebar_sync_outcome"] = _outcome(result)
            st.rerun()
    elif state.local.dirty:
        if st.button(
            f"Push {state.local.pending} change(s)", width="stretch", key="sidebar_push"
        ):
            with session() as s, s.begin():
                result = sync.push(s)
            load_all.clear()
            st.session_state["sidebar_sync_outcome"] = _outcome(result)
            st.rerun()

    outcome = st.session_state.pop("sidebar_sync_outcome", None)
    if outcome:
        (st.success if outcome["ok"] else st.error)(outcome["message"])


def _outcome(result) -> dict:
    """A Result reduced to what survives a rerun. See sync_badge."""
    return {"ok": result.ok, "message": result.message}


def read_only() -> bool:
    """True while the database is checked out for offline use.

    Transactions merge across machines; changes to the parameters both machines refer to do
    not (DESIGN.md 6.3.2), so anything that edits reference or periodic data is disabled
    until the checkout is handed back.
    """
    from budget import sync

    with session() as s:
        return sync.status(s).local.mode == sync.OFFLINE


def show_outcome(outcome, label: str = "the change") -> None:
    """Report a write and push it, the pattern every editable page follows."""
    import streamlit as st

    if outcome.ok:
        st.success(outcome.message)
        for warning in getattr(outcome, "warnings", []):
            st.info(warning)
        load_all.clear()
        auto_push(label)
    else:
        st.error(outcome.message)


def editable_money(label: str, help_text: str | None = None):
    """A money column for st.data_editor, with the pound sign and thousands separator."""
    import streamlit as st

    return st.column_config.NumberColumn(
        label, format=MONEY_FORMAT, step=0.01, help=help_text
    )


def editable_percent(label: str, help_text: str | None = None):
    """A rate column for st.data_editor, quoted as a percentage rather than a fraction."""
    import streamlit as st

    return st.column_config.NumberColumn(
        label, format="%.2f", step=0.01, min_value=0.0, max_value=100.0, help=help_text
    )


TRANSFER_LABEL = "Transfer"


def name_blanks(df: pd.DataFrame, columns, *, transfers: str | None = "type") -> pd.DataFrame:
    """Replace missing text with something a reader can act on.

    A transfer carries no category or classification -- New_entry never wrote one -- so those
    cells are genuinely empty rather than merely unfilled. pandas renders that as 'nan',
    which reads like a broken row; naming it 'Transfer' says what it actually is.

    `transfers` names the column holding the transaction type. Where it is absent, or the row
    is not a transfer, a blank is shown as an em dash instead.
    """
    out = df.copy()
    is_transfer = (
        out[transfers].astype("string").str.lower().eq(TRANSFER_LABEL.lower())
        if transfers and transfers in out.columns
        else pd.Series(False, index=out.index)
    )
    for column in columns:
        if column not in out.columns:
            continue
        text = out[column].astype("string")
        out[column] = text.where(text.notna(), is_transfer.map({True: TRANSFER_LABEL,
                                                               False: "—"}))
    return out


def describe_txn(row) -> str:
    """One transaction, in the form the remove and restore pickers show it.

    Shared so the two cannot drift, and because the obvious spelling of it -- `category or
    type` -- is wrong: a missing category arrives as float NaN, which is truthy, so every
    transfer was labelled 'nan'.
    """

    def text(value, fallback: str = "") -> str:
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return fallback
        return str(value)

    kind = text(getattr(row, "type", None), "")
    label = text(getattr(row, "category", None)) or text(
        getattr(row, "classification", None)
    ) or kind or TRANSFER_LABEL
    to = text(getattr(row, "account_to", None))
    route = f"{text(getattr(row, 'account_from', None))}" + (f" → {to}" if to else "")
    return (
        f"#{int(row.id)} · {row.date:%d %b %Y} · {money(row.amount)} · {route} · {label}"
    )
