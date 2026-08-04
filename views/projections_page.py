"""Projections -- replaces the Projected Costs tab.

The workbook held one month at a time and the month tabs read it for any date after today.
Here it is keyed by date, so several months coexist and last month's plan is still there to
compare against.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from budget import reference, repo, ui

data = ui.page_header(
    "Projections", "Expected spend per day against what actually happened."
)

projections = data["projections"]
classifications = list(data["classifications"]["name"])
periods = data["periods"]
READ_ONLY = ui.read_only()

projections = projections.copy()
if not projections.empty:
    projections["period"] = projections["date"].dt.strftime("%Y-%m")
    available = sorted(set(projections["period"]))
else:
    projections["period"] = pd.Series(dtype="str")
    available = []

# Months with projections, plus every month of the year: a month can only be planned if it
# can be selected before anything has been entered for it.
selectable = sorted(set(available) | set(data["all_periods"]))
default = available[-1] if available else data["periods"][-1]

period = st.selectbox(
    "Month",
    options=selectable,
    index=selectable.index(default),
    format_func=repo.period_label,
)

month_proj = projections[projections["period"] == period]
daily_actual = repo.daily_classification(data["postings"], period)

# ------------------------------------------------------------------------- headline

projected_total = month_proj["amount"].sum()
actual_total = daily_actual["total"].sum() if not daily_actual.empty else Decimal("0")

cols = st.columns(3)
cols[0].metric("Projected", ui.money(projected_total))
cols[1].metric("Actual so far", ui.money(actual_total))
cols[2].metric(
    "Difference",
    ui.money(actual_total - projected_total),
    help="Positive means more was spent than planned",
)

st.divider()

# --------------------------------------------------------------- by classification

st.subheader("By classification")

proj_by_class = month_proj.groupby("classification", as_index=False)["amount"].sum()
if daily_actual.empty:
    actual_by_class = pd.DataFrame(columns=["classification", "total"])
else:
    actual_by_class = daily_actual.groupby("classification", as_index=False)["total"].sum()

comparison = proj_by_class.merge(
    actual_by_class, on="classification", how="outer"
).fillna(Decimal("0"))
comparison = comparison.rename(columns={"amount": "projected", "total": "actual"})
comparison["difference"] = comparison["actual"] - comparison["projected"]
comparison = repo.sort_human(comparison, by="classification")

st.dataframe(
    ui.money_table(
        comparison,
        ["projected", "actual", "difference"],
        labels={
            "classification": "Classification",
            "projected": "Projected",
            "actual": "Actual",
            "difference": "Difference",
        },
    ),
    use_container_width=True,
    hide_index=True,
)

chart = ui.to_float(comparison, ["projected", "actual"]).melt(
    id_vars="classification", value_vars=["projected", "actual"],
    var_name="series", value_name="amount",
)
fig = px.bar(
    chart, x="classification", y="amount", color="series", barmode="group",
    labels={"amount": "£", "classification": "", "series": ""},
)
fig.update_layout(margin=dict(t=10))
st.plotly_chart(fig, use_container_width=True)

st.divider()

# ------------------------------------------------------------------------ by day

st.subheader("Day by day")

chosen = st.multiselect(
    "Classification", ui.alphabetical(projections["classification"]),
    default=[c for c in ["Excess", "Bills"] if c in classifications],
)

if chosen:
    proj = month_proj[month_proj["classification"].isin(chosen)]
    act = (
        daily_actual[daily_actual["classification"].isin(chosen)]
        if not daily_actual.empty
        else pd.DataFrame(columns=["date", "classification", "total"])
    )
    merged = proj.merge(
        act, on=["date", "classification"], how="outer"
    ).fillna(Decimal("0"))
    merged = merged.rename(columns={"amount": "projected", "total": "actual"})
    merged["difference"] = merged["actual"] - merged["projected"]
    merged = merged.sort_values(["date", "classification"])

    display = merged[["date", "classification", "projected", "actual", "difference",
                      "comment"]].copy()
    display["date"] = pd.to_datetime(display["date"]).dt.date
    st.dataframe(
        ui.money_table(
            display,
            ["projected", "actual", "difference"],
            labels={
                "date": "Date", "classification": "Classification",
                "projected": "Projected", "actual": "Actual",
                "difference": "Difference", "comment": "Note",
            },
        ),
        use_container_width=True,
        hide_index=True,
        height=420,
    )
else:
    st.caption("Pick one or more classifications to see the daily detail.")

st.caption(
    "Projections drive the running totals for any day after today — which is why the "
    "Summary matrix shows figures for months that have not happened yet."
)

st.divider()

# ----------------------------------------------------------------------- plan a month

st.subheader(f"Plan {repo.period_label(period)}")

if READ_ONLY:
    st.warning(
        "**Read-only while checked out for offline use.** Check in on the Sync page first."
    )

st.caption(
    "Same shape as the Import page, minus the account and category — a projection is a "
    "day's expected spend against a classification, which is all the workbook's Projected "
    "Costs grid held. Rows are keyed by date and classification, so re-entering a day "
    "replaces it."
)

first_day = repo.period_start(period)
last_day = repo.month_end(period)
class_names = ui.alphabetical(data["classifications"]["name"])
class_ids = dict(zip(data["classifications"]["name"], data["classifications"]["id"]))

existing = month_proj[["date", "classification", "amount", "comment"]].copy()
if not existing.empty:
    existing["date"] = pd.to_datetime(existing["date"]).dt.date
    existing = existing.sort_values(["date", "classification"])
    existing["amount"] = existing["amount"].astype(float)
else:
    existing = pd.DataFrame(
        {
            "date": pd.Series(dtype="object"),
            "classification": pd.Series(dtype="str"),
            "amount": pd.Series(dtype="float"),
            "comment": pd.Series(dtype="str"),
        }
    )

edited = st.data_editor(
    existing,
    use_container_width=True,
    hide_index=True,
    num_rows="fixed" if READ_ONLY else "dynamic",
    column_config={
        "date": st.column_config.DateColumn(
            "Date", format="DD/MM/YYYY", min_value=first_day, max_value=last_day
        ),
        "classification": st.column_config.SelectboxColumn(
            "Classification", options=class_names, required=True
        ),
        "amount": ui.editable_money("Amount"),
        "comment": st.column_config.TextColumn("Note"),
    },
    key=f"projection_editor_{period}",
    height=420,
)

st.caption(
    f"Dates must fall within {first_day:%d %b} – {last_day:%d %b %Y}. Anything outside the "
    "month is ignored, since it would belong to a different month's plan."
)

if st.button(
    f"Save {repo.period_label(period)}", type="primary", disabled=READ_ONLY,
    key="save_projections",
):
    rows, skipped = [], 0
    for _, row in edited.iterrows():
        when, name = row["date"], row["classification"]
        if when is None or pd.isna(when) or not name or pd.isna(name):
            skipped += 1
            continue
        when = pd.to_datetime(when).date()
        if not (first_day <= when <= last_day) or name not in class_ids:
            skipped += 1
            continue
        rows.append((when, int(class_ids[name]), Decimal(str(row["amount"] or 0)),
                     None if pd.isna(row["comment"]) else row["comment"]))

    if not rows and not skipped:
        st.info("Nothing to save.")
    else:
        with ui.session() as session, session.begin():
            # Cleared and rewritten rather than merged: a shorter replacement must not
            # leave behind days from the longer version it replaced.
            removed = reference.clear_projections(session, period)
            for when, class_id, amount, comment in rows:
                reference.set_projection(session, when, class_id, amount, comment)
            outcome = reference.Outcome(
                True,
                f"Saved {len(rows)} projection(s) for {repo.period_label(period)}"
                + (f", replacing {removed}." if removed else "."),
                [f"{skipped} row(s) skipped — outside the month or incomplete."]
                if skipped
                else [],
            )
        ui.show_outcome(outcome, "the projections")
        st.rerun()
