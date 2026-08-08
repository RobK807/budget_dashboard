"""Trends -- replaces Cumulative Analysis.

The year-long running total per classification, carrying each month's closing balance into
the next according to its rollover rule, with retention applied to a credit balance and
projections filling in days after today. See DESIGN.md 6a.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from budget import repo, ui

data = ui.page_header(
    "Trends", "Cumulative position across the year, with rollover and projections."
)

classifications = data["classifications"]
rollovers = dict(zip(classifications["name"], classifications["rollover"]))
retention = Decimal(str(data["settings"].get("excess_retention", 1)))

# How far past today to carry the chain. A parameter rather than 'to the end of the fiscal
# year', which is what it used to be: the balances are cumulative, so six months ahead and
# eighteen are different calculations rather than the same one truncated. The default comes
# from Settings → General.
look_forward = st.slider(
    "Look forward (months)",
    min_value=0,
    max_value=36,
    value=int(data["look_forward"]),
    key="trends_look_forward",
    help="Months beyond the current one to project. Set the default under "
         "**Settings → General**.",
)
periods = repo.span(data["earliest_period"], look_forward)

daily, closes = ui.running_totals(data, periods)

if daily.empty:
    st.info("Nothing to show yet.")
    st.stop()

chosen = st.multiselect(
    "Classification",
    ui.alphabetical(classifications["name"]),
    default=[c for c in ["Excess"] if c in set(classifications["name"])] or
            ui.alphabetical(classifications["name"])[:1],
)

if not chosen:
    st.caption("Pick a classification.")
    st.stop()

chart_from = ui.month_from(periods, key="trends_chart_from", tax_year=data["tax_year"])

# Filtered after the chain has run, never before it. The running total is cumulative, so
# starting the *calculation* later would restart it from zero rather than showing it from
# a later month -- which is the same mistake as counting the year-end twice, in reverse.
view = daily[daily["classification"].isin(chosen)].copy()
if chart_from:
    view = view[view["date"].map(repo.period_of) >= chart_from]
view["running"] = view["running"].astype(float)

today = pd.Timestamp(dt.date.today())

if len(chosen) == 1:
    # One series, so colour is free to mean something else: below zero is drawn red. With
    # several selected, colour is already carrying which classification a line is and
    # cannot say both -- see the else branch.
    xs, above, below = ui.split_at_zero(
        list(view["date"]), list(view["running"])
    )
    fig = go.Figure()
    # An invisible full-height trace carries the hover, so a point stays readable whichever
    # side of the axis it falls on. The two coloured traces only draw.
    fig.add_trace(
        go.Scatter(
            x=list(view["date"]), y=list(view["running"]),
            mode="lines", line=dict(width=0), showlegend=False,
            name=chosen[0], hovertemplate=None,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs, y=above, mode="lines", name=chosen[0],
            line=dict(color=ui.ACCENT), hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs, y=below, mode="lines", name=f"{chosen[0]} (below zero)",
            line=dict(color=ui.NEGATIVE), hoverinfo="skip", showlegend=False,
        )
    )
    fig.update_layout(yaxis_title="£", xaxis_title="")
else:
    fig = px.line(
        view, x="date", y="running", color="classification",
        labels={"running": "£", "date": "", "classification": ""},
    )

# Negative figures read red in the hover box whichever branch built the chart. Plotly
# substitutes into the template before rendering it as HTML, so the colour can be chosen per
# point through customdata; 'inherit' leaves positives to the theme's own hover colour.
for trace in fig.data:
    if trace.hoverinfo == "skip":
        continue
    values = [v if v is not None else float("nan") for v in trace.y]
    trace.customdata = [[ui.NEGATIVE if v < 0 else "inherit"] for v in values]
    trace.hovertemplate = (
        '<span style="color:%{customdata[0]}">£%{y:,.2f}</span><extra></extra>'
    )

fig.add_vline(x=today.timestamp() * 1000, line_dash="dash", line_color="grey")
fig.add_annotation(x=today, y=0, text="today", showarrow=False, yshift=10)
fig.update_layout(hovermode="x unified", margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), width="stretch")

st.caption(
    "Solid throughout, but everything to the right of the dashed line is built from "
    "projections rather than actuals. Negative figures are red in the hover box"
    + (
        ", and the line itself is red below zero."
        if len(chosen) == 1
        else " — select a single classification to see the line itself go red below zero, "
        "which with several selected would clash with the colour telling them apart."
    )
)

st.divider()

# ------------------------------------------------------------------- month-end table

st.subheader("Month-end position")

matrix = closes.pivot_table(
    index="period", columns="classification", values="closing", aggfunc="first"
)
matrix = matrix.reindex([p for p in periods if p in matrix.index][::-1])
matrix = matrix[sorted(matrix.columns, key=lambda c: str(c).casefold())]
display = matrix.copy()
display.index = [repo.period_label(p) for p in display.index]
st.dataframe(ui.heatmap(display.astype(float)), width="stretch")

st.caption(
    f"{repo.period_label(periods[0])} to {repo.period_label(periods[-1])} — the range starts "
    "at the first month anything is recorded against and runs to the look-forward set above, "
    "rather than to the end of a fixed fiscal year. These are the figures Summary!Q19:Y31 "
    "reads. They are cumulative, not monthly totals — a month's closing balance carries into "
    "the next according to its rollover rule."
)

st.divider()

# ------------------------------------------------------------------------ the rules

with st.expander("How the carry-forward works"):
    rules = pd.DataFrame(
        [
            {
                "classification": name,
                "rollover": rollovers.get(name, "none"),
                "carries": {
                    "none": "Nothing — starts at zero each month",
                    "all": "Both debit and credit balances",
                    "credit": "Only a credit balance (a surplus)",
                    "debit": "Only a debit balance (an overspend)",
                }.get(rollovers.get(name, "none"), ""),
            }
            for name in ui.alphabetical(classifications["name"])
        ]
    )
    st.dataframe(
        rules, width="stretch", hide_index=True,
        column_config={
            "classification": "Classification",
            "rollover": "Rollover",
            "carries": "What carries forward",
        },
    )
    st.markdown(
        f"""
**Retention is currently {ui.percent(retention * 100)}%** — the share of a *credit* balance that
carries. An overspend always carries in full; retention exists so a surplus is not banked
twice, not to forgive debt.

Some classifications also gain a daily allowance, added for every day of the month on top
of actual spend.
"""
    )
