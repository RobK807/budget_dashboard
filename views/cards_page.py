"""Cards -- replaces the Balance Transfer Cards tab.

The workbook stored 400 rows of amortisation formulas. Here the schedule is generated from
each card's parameters, so changing a term or a minimum-payment rate is one edit.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from budget import cards as cards_module
from budget import repo, ui

data = ui.page_header("Cards", "Balance-transfer cards and how they pay off.")

cards = data["cards"]

if cards.empty:
    st.info("No cards yet — add one under **Settings → Cards**.")
    st.stop()

cards = repo.sort_human(cards, by="name")

schedules = {
    row["name"]: cards_module.schedule(
        row["opening_balance"], row["opening_date"], int(row["term_months"]),
        row["min_payment_pct"],
    )
    for _, row in cards.iterrows()
}

# ------------------------------------------------------------------------- headline

# As of today, not the sum of opening balances: the cards start in different months, so
# adding their openings would total figures from different points in the year.
today = dt.date.today()
outstanding = sum(
    (cards_module.balance_on(rows, today) for rows in schedules.values()), Decimal("0")
)
remaining = sum(
    (
        sum((r.payment for r in rows if r.date > today), Decimal("0"))
        for rows in schedules.values()
    ),
    Decimal("0"),
)
final = max(
    (cards_module.payoff_date(rows) for rows in schedules.values() if rows), default=None
)

cols = st.columns(3)
cols[0].metric("Outstanding today", ui.money(outstanding))
cols[1].metric("Left to repay", ui.money(remaining))
cols[2].metric("Clear by", final.strftime("%b %Y") if final else "—")

st.divider()

# --------------------------------------------------------------------------- summary

def next_payment(rows) -> Decimal:
    """The next instalment falling due, not the first one ever made.

    The two differ once a card is part-way through its term, and by a long way in the final
    month -- the closing instalment settles the whole remaining balance.
    """
    upcoming = [r for r in rows if r.date > today]
    return upcoming[0].payment if upcoming else Decimal("0")


summary = cards.copy()
summary["next_payment"] = summary["name"].map(lambda n: next_payment(schedules[n]))
summary["clears"] = summary["name"].map(
    lambda n: cards_module.payoff_date(schedules[n])
)
summary["balance_today"] = summary["name"].map(
    lambda n: cards_module.balance_on(schedules[n], today)
)
summary["min_pct"] = (summary["min_payment_pct"].astype(float) * 100).round(2)

st.dataframe(
    ui.money_table(
        summary[["name", "opening_date", "opening_balance", "balance_today", "next_payment",
                 "min_pct", "term_months", "payment_day", "credit_limit", "clears"]],
        ["opening_balance", "balance_today", "next_payment", "credit_limit"],
        labels={
            "name": "Card", "opening_date": "Started",
            "opening_balance": "Borrowed", "balance_today": "Today",
            "next_payment": "Next payment", "min_pct": "Minimum %",
            "term_months": "Term (months)", "payment_day": "Payment day",
            "credit_limit": "Credit limit", "clears": "Clears",
        },
    ).format({"Minimum %": "{:.2f}"}),
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "The term runs from each card's own opening date, so a 13-month transfer taken out in "
    "June clears the following July. The workbook counted from April for every card, "
    "whenever it actually started. The final instalment settles whatever is left — the "
    "promotional term ends and the balance falls due, which is the step the workbook's "
    "`IF(month = term, balance, …)` made. Parameters are under **Settings → Cards**."
)

st.divider()

# ------------------------------------------------------------------------ projection

st.subheader("Balances over time")

frames = []
for name, rows in schedules.items():
    for row in rows:
        frames.append(
            {"card": name, "date": row.date, "balance": float(row.closing),
             "payment": float(row.payment)}
        )
projection = pd.DataFrame(frames)

if not projection.empty:
    fig = px.line(
        projection, x="date", y="balance", color="card",
        labels={"balance": "Balance (£)", "date": "", "card": ""},
    )
    fig.update_layout(hovermode="x unified", margin=dict(t=10))
    st.plotly_chart(fig, use_container_width=True)

    fig = px.bar(
        projection, x="date", y="payment", color="card",
        labels={"payment": "Total payment (£)", "date": "", "card": ""},
    )
    fig.update_layout(margin=dict(t=10), barmode="stack", hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)
    st.caption(
        "Segmented by card, so a spike can be read back to the card causing it — the end of "
        "a promotional term, where the remaining balance is settled in one payment."
    )

st.divider()

# -------------------------------------------------------------------------- schedule

st.subheader("Schedule")

chosen = st.selectbox("Card", ui.alphabetical(cards["name"]))
rows = schedules[chosen]

detail = pd.DataFrame(
    [
        {"month": r.month, "date": r.date, "opening": r.opening,
         "payment": r.payment, "closing": r.closing}
        for r in rows
    ]
)
st.dataframe(
    ui.money_table(
        detail, ["opening", "payment", "closing"],
        labels={"month": "#", "date": "Date", "opening": "Opening",
                "payment": "Payment", "closing": "Closing"},
    ),
    use_container_width=True,
    hide_index=True,
    height=420,
)
