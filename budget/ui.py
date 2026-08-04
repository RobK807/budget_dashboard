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
        data["savings_targets"]["period"] if not data["savings_targets"].empty else None,
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
