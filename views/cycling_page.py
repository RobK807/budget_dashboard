"""Cycling -- replaces the Cycling tab.

What the bike saves in fares against what it costs to run. The workbook derived the saving
with a nested IF in priority order (commute, then band, then gym); here the flags are stored
and the rates are effective-dated, so raising a fare does not rewrite what earlier rides were
worth -- and does not mean editing every historic row either.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from budget import repo, ui

data = ui.page_header("Cycling", "What cycling saves, against what it costs.")

outgoings = data["cycling_outgoings"]
days = data["cycling_days"]
rate_rows = data["cycling_rates"]
rates = ui.cycling_rates(data)

if days.empty and outgoings.empty:
    st.info("Nothing recorded yet — add a ride under **Record → Cycling**.")
    st.stop()

savings = repo.cycling_savings_dated(days, rate_rows)

total_saved = savings["saving"].sum() if not savings.empty else Decimal("0")
total_cost = outgoings["amount"].sum() if not outgoings.empty else Decimal("0")
ridden = int((savings["saving"] > 0).sum()) if not savings.empty else 0

cols = st.columns(4)
cols[0].metric("Days ridden", f"{ridden:,}")
cols[1].metric("Fares saved", ui.money(total_saved))
cols[2].metric("Running costs", ui.money(total_cost))
cols[3].metric(
    "Net saving", ui.money(total_saved - total_cost),
    help="Fares avoided less what the bike has cost",
)

st.caption(
    "Rates in force today: "
    + (", ".join(f"{k.title()} {ui.money(v)}" for k, v in rates.items() if v) or "none set")
    + ". Each day is valued at the rate that applied on that date, so these are a summary "
    "rather than what the totals above are built from. Set them under "
    "**Settings → Cycling**."
)

st.divider()

# --------------------------------------------------------------------- over time

if not savings.empty:
    st.subheader("Cumulative net saving")

    chart_from = ui.month_from(
        data["periods"], key="cycling_chart_from", tax_year=data["tax_year"]
    )

    timeline = savings[["date", "saving", "kind"]].copy()
    timeline["cost"] = Decimal("0")
    if not outgoings.empty:
        costs = outgoings.groupby("date", as_index=False)["amount"].sum()
        timeline = timeline.merge(
            costs.rename(columns={"amount": "outgoing"}), on="date", how="outer"
        ).fillna(Decimal("0"))
    else:
        timeline["outgoing"] = Decimal("0")

    timeline = timeline.sort_values("date")
    timeline["net"] = timeline["saving"].astype(float) - timeline["outgoing"].astype(float)
    timeline["cumulative"] = timeline["net"].cumsum()

    # Days are recorded for the whole year ahead, unridden ones included, so the line ran
    # flat out to next March -- a long tail of nothing happening that squeezed the part
    # where it did. Cut after the cumsum, so today's figure is still the running total of
    # everything before it rather than of a truncated series.
    to_date = timeline[pd.to_datetime(timeline["date"]).dt.date <= dt.date.today()]
    # After the cumsum for the same reason: the line starts where you asked, holding the
    # running total of everything before it rather than restarting from zero.
    if chart_from:
        to_date = to_date[to_date["date"].map(repo.period_of) >= chart_from]

    fig = px.area(
        to_date if not to_date.empty else timeline, x="date", y="cumulative",
        labels={"cumulative": "Net saving (£)", "date": ""},
    )
    fig.update_layout(margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig), width="stretch")

    st.subheader("Days by kind")
    by_kind = (
        savings[savings["saving"] > 0]
        .groupby("kind", as_index=False)
        .agg(days=("saving", "size"), saved=("saving", "sum"))
    )
    by_kind = repo.sort_human(by_kind, by="kind")
    st.dataframe(
        ui.money_table(
            by_kind, ["saved"],
            labels={"kind": "Kind", "days": "Days", "saved": "Saved"},
        ),
        width="stretch",
        hide_index=True,
    )

    fig = px.bar(
        ui.to_float(by_kind, ["saved"]), x="kind", y="saved",
        labels={"saved": "£", "kind": ""},
    )
    fig.update_layout(margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig), width="stretch")

st.divider()

# ------------------------------------------------------------------------ detail

left, right = st.columns(2)

with left:
    st.subheader("Running costs")
    if outgoings.empty:
        st.caption("Nothing recorded.")
    else:
        detail = outgoings.copy()
        detail["date"] = detail["date"].dt.date
        st.dataframe(
            ui.money_table(
                repo.sort_human(detail, by="date"), ["amount"],
                labels={"date": "Date", "item": "Item", "amount": "Amount",
                        "flag": "Kind"},
            ),
            width="stretch",
            hide_index=True,
        )

with right:
    st.subheader("Days ridden")
    if savings.empty:
        st.caption("Nothing recorded.")
    else:
        all_dates = pd.to_datetime(savings["date"])
        earliest, latest = all_dates.min().date(), all_dates.max().date()

        span = st.date_input(
            "Date range",
            value=ui.default_range(90, earliest, latest),
            min_value=earliest,
            max_value=latest,
            format="DD/MM/YYYY",
            key="ridden_range",
            help="Defaults to the last ninety days. Both the table and the totals beneath "
                 "it follow this range.",
        )
        # A date_input in range mode returns one date while the second is still being
        # picked, so the range is only applied once both ends are in.
        if isinstance(span, tuple) and len(span) == 2:
            start, end = span
        else:
            start, end = earliest, latest

        window = savings[
            (all_dates.dt.date >= start) & (all_dates.dt.date <= end)
        ].copy()
        window["date"] = pd.to_datetime(window["date"]).dt.date
        ridden_in_range = int((window["saving"] > 0).sum())

        counts = st.columns(2)
        counts[0].metric("Days ridden", f"{ridden_in_range:,}")
        counts[1].metric("Saved", ui.money(window["saving"].sum()))

        st.dataframe(
            ui.money_table(
                window.sort_values("date", ascending=False)[["date", "kind", "saving"]],
                ["saving"],
                labels={"date": "Date", "kind": "Kind", "saving": "Saved"},
            ),
            width="stretch",
            hide_index=True,
            height=380,
        )
        st.caption(
            f"{start:%d %b %Y} to {end:%d %b %Y} — "
            f"{len(window)} day(s) recorded, {ridden_in_range} of them ridden."
        )
