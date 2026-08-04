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


@st.cache_resource
def _session_factory():
    engine = make_engine()
    # Applied once per process, on the cached engine: an existing database created before a
    # schema change is brought up to date, and any table added since is created, rather than
    # failing at the first read.
    from budget.db import create_all

    create_all(engine)
    return make_session_factory(engine)


def session():
    """A session for write operations. Use as `with ui.session() as s, s.begin():`."""
    return _session_factory()()


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


@st.cache_data(ttl=300)
def load_all() -> dict:
    """Everything the dashboard needs, in one cached read.

    The whole year is a few thousand rows, so loading it wholesale and slicing in pandas is
    simpler than round-tripping per page and fast enough not to matter.
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
            "savings_targets": repo.load_savings_targets(session),
        }
    full_year = repo.fiscal_periods(tax_year)
    # `periods` is what most pages offer; `all_periods` keeps the full fiscal year for the
    # things that genuinely need it -- year-end projections, and setting a future month's
    # parameters before it arrives.
    data["all_periods"] = full_year
    data["periods"] = repo.periods_to_date(full_year)
    data["tax_year"] = tax_year
    return data


def alphabetical(values) -> list:
    """Case-insensitive sort for dropdown options.

    Plain sorted() is ASCII, so it puts HSBC before Halifax and ISA before Investments --
    correct by codepoint, wrong to a reader.
    """
    return sorted({v for v in values if v is not None}, key=lambda v: str(v).casefold())


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
        if state.conflict or state.blocked_by:
            st.warning("Not synced — see the Sync page.")
            return
        result = sync.push(s)

    if result.ok:
        st.caption(f"Synced to the NAS after {label}.")
    else:
        st.caption(f"Not synced: {result.message}")


def money(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    return f"£{Decimal(str(value)):,.2f}"


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


def money_table(df: pd.DataFrame, money_columns: list[str], labels: dict[str, str] | None = None):
    """Table with thousands separators on the money columns.

    st.column_config.NumberColumn takes a printf format, and printf has no thousands
    separator -- '%.2f' gives £39255.98. A pandas Styler does support '{:,.2f}' and keeps the
    underlying values numeric, so sorting still works on magnitude rather than on text.
    """
    out = df.copy()
    if labels:
        out = out.rename(columns=labels)
        money_columns = [labels.get(c, c) for c in money_columns]
    out = to_float(out, money_columns)
    return out.style.format({c: "£{:,.2f}" for c in money_columns})


def currency_columns(*names: str) -> dict:
    return {
        n: st.column_config.NumberColumn(n.replace("_", " ").title(), format="£%.2f")
        for n in names
    }


def running_totals(data: dict):
    """Daily running totals and month-end closes from the rollover engine (DESIGN.md 6a)."""
    from decimal import Decimal

    retention = Decimal(str(data["settings"].get("excess_retention", 1)))
    return repo.running_by_period(
        data["postings"],
        data["projections"],
        data["allowances"],
        data["classifications"],
        data["all_periods"],
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


def periods_with_data(data: dict) -> list[str]:
    used = set(data["postings"]["period"].unique()) if not data["postings"].empty else set()
    return [p for p in data["periods"] if p in used]


def page_header(title: str, subtitle: str = "") -> dict:
    """Per-page chrome. set_page_config lives in app.py because st.navigation runs the
    entrypoint and the page in a single script run, so it may only be called once."""
    data = load_all()
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
        if st.button("Refresh data", use_container_width=True):
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
    """A money column for st.data_editor.

    NumberColumn takes a printf format and printf has no thousands separator, so editable
    money is shown to two decimal places without one -- the separator is only available
    through a Styler, which a data_editor cannot use.
    """
    import streamlit as st

    return st.column_config.NumberColumn(label, format="%.2f", step=0.01, help=help_text)
