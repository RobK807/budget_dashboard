"""Record a day's cycling, or something the bike has cost.

The workbook's Cycling tab was two hand-maintained columns of Y/N flags running to a row per
weekday, pre-filled by `WORKDAY(H3,1)` for the whole year. Entering a day meant finding it in
the list. Here a day is a row that exists only once it has happened.

The date is the primary key of `cycling_day`, so at most one entry per day is a property of
the schema rather than a check that could be skipped.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import streamlit as st

from budget import reference, repo, ui

data = ui.page_header("Cycling", "Record a ride, or a running cost.")

days = data["cycling_days"]
outgoings = data["cycling_outgoings"]
rates = data["cycling_rates"]
READ_ONLY = ui.read_only()

if READ_ONLY:
    st.warning(
        "**Read-only while checked out for offline use.** Check in on the Sync page first."
    )

KINDS = {"commute": "Commute", "band": "Band", "gym": "Gym"}


def existing_kind(on: dt.date) -> str | None:
    if days.empty:
        return None
    match = days[pd.to_datetime(days["date"]).dt.date == on]
    if match.empty:
        return None
    row = match.iloc[0]
    for key in KINDS:
        if row[key]:
            return key
    return None


left, right = st.columns(2)

# ------------------------------------------------------------------------ a day ridden

with left:
    st.subheader("A day's cycling")

    chosen_day = st.date_input(
        "Date", value=dt.date.today(), format="DD/MM/YYYY", key="ride_date"
    )
    already = existing_kind(chosen_day)

    if already:
        st.info(
            f"**{KINDS[already]}** is already recorded for {chosen_day:%A %d %B %Y}. "
            "Saving a different kind replaces it — there is one entry per day."
        )

    kind_keys = list(KINDS)
    chosen_kind = st.selectbox(
        "Kind",
        options=kind_keys,
        index=kind_keys.index(already) if already else 0,
        format_func=lambda k: KINDS[k],
        help="What the ride replaced. Each kind saves a different fare.",
    )

    rate = repo.rate_in_force(rates, chosen_kind, chosen_day)
    if rate:
        st.caption(
            f"{KINDS[chosen_kind]} saves {ui.money(rate)} on {chosen_day:%d %b %Y}, at the "
            "rate in force that day."
        )
    else:
        st.warning(
            f"No {KINDS[chosen_kind].lower()} rate applies on {chosen_day:%d %b %Y}, so this "
            "day would score nothing. Set one under **Settings → Cycling**."
        )

    buttons = st.columns(2)
    if buttons[0].button("Save", type="primary", disabled=READ_ONLY, key="save_ride"):
        with ui.session() as session, session.begin():
            outcome = reference.record_cycling_day(session, chosen_day, chosen_kind)
        ui.show_outcome(outcome, "the ride")

    if buttons[1].button(
        "Remove", disabled=READ_ONLY or not already, key="remove_ride"
    ):
        with ui.session() as session, session.begin():
            outcome = reference.record_cycling_day(session, chosen_day, None)
        ui.show_outcome(outcome, "the ride")

# ------------------------------------------------------------------------ running costs

with right:
    st.subheader("A running cost")

    with st.form("cycling_outgoing"):
        cost_date = st.date_input(
            "Date", value=dt.date.today(), format="DD/MM/YYYY", key="cost_date"
        )
        item = st.text_input("Item", placeholder="e.g. Bike service")
        fields = st.columns(2)
        amount = fields[0].number_input(
            "Amount (£)", min_value=0.0, step=5.0, format="%.2f"
        )
        flag = fields[1].selectbox(
            "Kind", ["Commute", "General"],
            help="Which running total it comes off",
        )
        if st.form_submit_button("Add cost", type="primary", disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                outcome = reference.add_cycling_outgoing(
                    session, cost_date, item, Decimal(str(amount)), flag
                )
            ui.show_outcome(outcome, "the running cost")

    if not outgoings.empty:
        st.caption("Recent costs")
        recent = outgoings.sort_values("date", ascending=False).head(10).copy()
        recent["date"] = recent["date"].dt.date
        st.dataframe(
            ui.money_table(
                recent[["date", "item", "amount", "flag"]], ["amount"],
                labels={"date": "Date", "item": "Item", "amount": "Amount", "flag": "Kind"},
            ),
            width="stretch",
            hide_index=True,
        )

st.divider()

# ------------------------------------------------------------------------ recent rides

st.subheader("Recently recorded")

recent = pd.DataFrame()
if not days.empty:
    valued = repo.cycling_savings_dated(days, rates)
    valued["date"] = pd.to_datetime(valued["date"])

    # Back from today, not back from the end of the data. The fiscal year runs to March
    # 2027, so sorting every dated row descending filled the list with months that have not
    # happened -- 'Recently recorded' showing next March is not recent by any reading.
    today = pd.Timestamp(dt.date.today())
    recent = valued[valued["date"] <= today].sort_values(
        "date", ascending=False
    ).head(30).copy()
    recent["date"] = recent["date"].dt.date

if days.empty:
    st.info("Nothing recorded yet.")
elif recent.empty:
    # Rides exist, but every one of them is dated ahead of today. Saying 'nothing recorded'
    # would be wrong and saying nothing at all would look broken.
    st.info("Nothing recorded on or before today — everything on file is dated ahead.")
else:
    st.dataframe(
        ui.money_table(
            recent[["date", "kind", "saving"]], ["saving"],
            labels={"date": "Date", "kind": "Kind", "saving": "Saved"},
        ),
        width="stretch",
        hide_index=True,
        height=360,
    )
    st.caption(
        f"The most recent {len(recent)} day(s) up to today. Each is valued at the rate that "
        "applied on that date, so raising a fare does not rewrite what earlier rides were "
        "worth."
    )
