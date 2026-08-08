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

period = ui.month_select(
    "Month", selectable,
    default=default if repo.period_of(dt.date.today()) not in selectable else None,
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
    width="stretch",
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
st.plotly_chart(ui.money_axis(fig), width="stretch")

st.divider()

# ------------------------------------------------------------------------ by day

st.subheader("Day by day")

chosen = st.multiselect(
    "Classification", ui.alphabetical(classifications),
    default=[c for c in ["Excess", "Bills"] if c in classifications],
)

if chosen:
    proj = month_proj[month_proj["classification"].isin(chosen)]
    if daily_actual.empty:
        # Sliced from proj rather than built fresh: a DataFrame declared by column names
        # alone types every column as object, and merging an object 'date' against the
        # datetime one proj carries raises instead of returning the projections unmatched.
        # That is any month with projections but nothing spent yet -- including, now that
        # the dropdown opens on the current month, the common case.
        act = proj.iloc[:0][["date", "classification"]].copy()
        act["total"] = pd.Series(dtype="object")
    else:
        act = daily_actual[daily_actual["classification"].isin(chosen)]
    merged = proj.merge(act, on=["date", "classification"], how="outer")
    # Only the amounts. A blanket fillna(Decimal("0")) reached the comment too, so a day
    # with no note showed '0' -- and left the column holding Decimals beside strings, which
    # Arrow cannot type. A missing note stays missing and renders as a dash.
    for column in ("amount", "total"):
        merged[column] = merged[column].fillna(Decimal("0"))
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
        width="stretch",
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
    "day's expected spend against a classification. The grid opens with every day of the "
    "month against every classification, so planning is filling figures in rather than "
    "building the rows first."
)

first_day = repo.period_start(period)
last_day = repo.month_end(period)
class_names = ui.alphabetical(data["classifications"]["name"])
class_ids = dict(zip(data["classifications"]["name"], data["classifications"]["id"]))

plan_for = st.multiselect(
    "Plan which classifications",
    class_names,
    default=class_names,
    key=f"plan_filter_{period}",
    help="Only the ones shown are saved, so a filtered screen cannot wipe the rest.",
)

if not plan_for:
    st.caption("Pick at least one classification to plan.")
    st.stop()

# ---- copy a previous month ---------------------------------------------------------

earlier_months = [p for p in selectable if p < period]
if earlier_months:
    copy_left, copy_right = st.columns([3, 1])
    source = copy_left.selectbox(
        "Copy from",
        options=list(reversed(earlier_months)),
        format_func=repo.period_label,
        key=f"copy_source_{period}",
        help="Copies by day of the month, for the classifications selected above. Bills "
             "that repeat on the same date each month need entering once.",
    )
    copy_right.write("")
    if copy_right.button(
        "Copy across", disabled=READ_ONLY, key=f"do_copy_{period}",
        width="stretch",
    ):
        source_rows = projections[projections["period"] == source]
        source_rows = source_rows[source_rows["classification"].isin(plan_for)]
        copied = out_of_range = 0
        with ui.session() as session, session.begin():
            reference.clear_projections(
                session, period, [int(class_ids[n]) for n in plan_for]
            )
            for _, row in source_rows.iterrows():
                day = pd.to_datetime(row["date"]).day
                # A 31-day month copied onto a 30-day one has a day with nowhere to go.
                # Dropping it is the honest outcome; shifting it to the 30th would invent a
                # payment date that was never planned.
                if day > last_day.day:
                    out_of_range += 1
                    continue
                reference.set_projection(
                    session, first_day.replace(day=day), int(class_ids[row["classification"]]),
                    Decimal(str(row["amount"])),
                    None if pd.isna(row["comment"]) else row["comment"],
                )
                copied += 1
            outcome = reference.Outcome(
                True,
                f"Copied {copied} projection(s) from {repo.period_label(source)} into "
                f"{repo.period_label(period)}.",
                [f"{out_of_range} row(s) dropped — that day does not exist in this month."]
                if out_of_range
                else [],
            )
        ui.show_outcome(outcome, "the copied projections")
        st.rerun()

# ---- the grid ----------------------------------------------------------------------

# Pre-populated with the full month rather than only what is stored: the workbook's grid was
# a fixed block of dates down the side, and starting from a blank editor meant typing a date
# and picking a classification before a figure could go anywhere.
stored = (
    month_proj.set_index(
        [pd.to_datetime(month_proj["date"]).dt.date, month_proj["classification"]]
    )
    if not month_proj.empty
    else None
)

grid = pd.DataFrame(
    [
        {
            "date": day.date(),
            "classification": name,
            "amount": (
                float(stored.loc[(day.date(), name), "amount"])
                if stored is not None and (day.date(), name) in stored.index
                else 0.0
            ),
            "comment": (
                stored.loc[(day.date(), name), "comment"]
                if stored is not None and (day.date(), name) in stored.index
                else None
            ),
        }
        for day in pd.date_range(first_day, last_day, freq="D")
        for name in plan_for
    ]
)
grid["comment"] = grid["comment"].astype("string")

edited = st.data_editor(
    grid,
    width="stretch",
    hide_index=True,
    num_rows="fixed",
    disabled=["date", "classification"] if not READ_ONLY else True,
    column_config={
        "date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
        "classification": st.column_config.TextColumn("Classification"),
        "amount": ui.editable_money("Amount"),
        "comment": st.column_config.TextColumn("Note"),
    },
    key=f"projection_editor_{period}_{'|'.join(plan_for)}",
    height=520,
)

st.caption(
    f"{first_day:%d %b} – {last_day:%d %b %Y}. A day left at zero is not stored — a zero "
    "projection and no projection are the same thing to the running totals, and keeping "
    "every empty cell would mean writing several hundred rows a month for nothing."
)

if st.button(
    f"Save {repo.period_label(period)}", type="primary", disabled=READ_ONLY,
    key="save_projections",
):
    rows = [
        (
            row["date"],
            int(class_ids[row["classification"]]),
            Decimal(str(row["amount"])),
            None if pd.isna(row["comment"]) else row["comment"],
        )
        for _, row in edited.iterrows()
        if row["amount"] is not None
        and not pd.isna(row["amount"])
        and Decimal(str(row["amount"])) != 0
    ]

    with ui.session() as session, session.begin():
        # Cleared and rewritten rather than merged, but only for the classifications on
        # screen: a shorter replacement must not leave behind days from the longer version,
        # and a filtered screen must not delete what the filter is hiding.
        removed = reference.clear_projections(
            session, period, [int(class_ids[n]) for n in plan_for]
        )
        for when, class_id, amount, comment in rows:
            reference.set_projection(session, when, class_id, amount, comment)
        outcome = reference.Outcome(
            True,
            f"Saved {len(rows)} projection(s) for {repo.period_label(period)}"
            + (f", replacing {removed}." if removed else "."),
        )
    ui.show_outcome(outcome, "the projections")
    st.rerun()
