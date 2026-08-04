"""Savings and investments -- the Summary tab's two headline tables (B2:O15).

Both were built on `SUM(INDIRECT("xlSavingsEom" & month))`: a named range per account block
per month, thirty-six of them, each pointing at a row whose position depended on how many
transactions that month happened to hold. Here it is one query over the ledger.

The 'available' column is the workbook's 'Less SC & Wed'. That label had stopped being true:
the figure it produced excluded Tembo as well from June onwards, without the heading
changing. Flagging the accounts instead means the column follows when a pot is added.
"""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from budget import repo, ui

data = ui.page_header(
    "Savings and investments", "What is put aside each month, against the target."
)

postings = data["postings"]
accounts = data["accounts"]
periods = data["periods"]
live_periods = ui.periods_with_data(data)

if not live_periods:
    st.info("No transactions yet.")
    st.stop()

series = repo.savings_series(
    postings, data["openings"], accounts, data["savings_targets"], periods
)

if series.empty:
    st.info("Nothing to show yet.")
    st.stop()

earmarked = accounts[accounts["exclude_from_savings"].fillna(False).astype(bool)]
earmarked_names = repo.sort_human(earmarked, by="name")["name"].tolist()

latest = series.iloc[-1]

# --------------------------------------------------------------------------- headline

cols = st.columns(4)
cols[0].metric(
    "Savings", ui.money(latest["savings_eom"]),
    help="Every account flagged as savings",
)
cols[1].metric(
    "Available", ui.money(latest["available_eom"]),
    help="Savings less the earmarked pots below",
)
cols[2].metric("Investments", ui.money(latest["investments_eom"]))
cols[3].metric(
    "Combined", ui.money(latest["combined"]),
    help="Savings and investments together",
)

if earmarked_names:
    st.caption(
        "Earmarked and therefore excluded from 'available': "
        + ", ".join(earmarked_names)
        + ". Set under **Settings → Savings targets**."
    )
else:
    st.caption(
        "No accounts are earmarked, so 'available' is the whole savings balance. Earmark "
        "one under **Settings → Savings targets**."
    )

st.divider()

# ---------------------------------------------------------------------------- savings

# Opening and closing side by side, so a month reads across in one line. The workbook gave
# closing balances only, which meant 'Added' could only be checked by finding the row above.
LABELS = {
    "month": "Month",
    "savings_bom": "Total — BoM",
    "available_bom": "Available — BoM",
    "savings_added": "Added",
    "savings_eom": "Total — EoM",
    "available_eom": "Available — EoM",
    "savings_target": "Monthly target",
    "savings_target_eom": "Target — EoM",
    "savings_required": "Required",
    "investments_bom": "Total — BoM",
    "investments_added": "Added",
    "investments_eom": "Total — EoM",
    "investments_target": "Monthly target",
    "investments_target_eom": "Target — EoM",
    "investments_required": "Required",
}

st.subheader("Savings")
savings_columns = [
    "savings_bom", "available_bom", "savings_added", "savings_eom", "available_eom",
    "savings_target", "savings_target_eom", "savings_required",
]
st.dataframe(
    ui.money_table(series[["month"] + savings_columns], savings_columns, labels=LABELS),
    use_container_width=True,
    hide_index=True,
)

st.subheader("Investments")
investment_columns = [
    "investments_bom", "investments_added", "investments_eom", "investments_target",
    "investments_target_eom", "investments_required",
]
st.dataframe(
    ui.money_table(
        series[["month"] + investment_columns], investment_columns, labels=LABELS
    ),
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "**Added** is the change in the total balance over the month, not a deposit total — a "
    "month that spends out of savings shows a negative. **Target — EoM** is the monthly "
    "targets summed to date, and **Required** is that cumulative target less what is "
    "available at the month end, so a positive figure is money still to find. It measures a "
    "running total of contributions against a balance, which only lines up from the month "
    "the targets start in. Note that 'available' excludes the earmarked pots while 'total' "
    "does not, so the two can move by different amounts in a month that touches one."
)

st.divider()

# ------------------------------------------------------------------------- over time

st.subheader("Balances over the year")

plot = ui.to_float(series, ["available_eom", "investments_eom", "combined"]).rename(
    columns={
        "available_eom": "Savings (available)",
        "investments_eom": "Investments",
        "combined": "Combined",
    }
)
fig = px.line(
    plot, x="month", markers=True,
    y=["Savings (available)", "Investments", "Combined"],
    labels={"value": "Balance (£)", "month": "", "variable": ""},
)
fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), use_container_width=True)

st.subheader("Added against target")

chart = ui.to_float(
    series,
    ["savings_added", "savings_target", "investments_added", "investments_target"],
).melt(
    id_vars="month",
    value_vars=["savings_added", "savings_target", "investments_added",
                "investments_target"],
    var_name="series", value_name="amount",
)
chart["series"] = chart["series"].map(
    {
        "savings_added": "Savings added",
        "savings_target": "Savings target",
        "investments_added": "Investments added",
        "investments_target": "Investments target",
    }
)
fig = px.bar(
    chart.dropna(subset=["amount"]), x="month", y="amount", color="series",
    barmode="group", labels={"amount": "£", "month": "", "series": ""},
)
fig.update_layout(margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), use_container_width=True)

st.divider()

# ------------------------------------------------------------------- account detail

st.subheader(f"Accounts at end of {latest['month']}")

balances = repo.account_balances(
    postings, data["openings"], latest["period"], accounts
)
detail = balances[balances["is_savings"] | balances["is_investment"]].copy()
detail["kind"] = detail.apply(
    lambda r: "Investment" if r["is_investment"] else "Savings", axis=1
)
detail["earmarked"] = detail["account"].isin(earmarked_names)

st.dataframe(
    ui.money_table(
        repo.sort_human(
            detail[["account", "kind", "earmarked", "opening", "transfer_in",
                    "transfer_out", "paid_in", "paid_out", "closing"]],
            by=["kind", "account"],
        ),
        ["opening", "transfer_in", "transfer_out", "paid_in", "paid_out", "closing"],
        labels={
            "account": "Account", "kind": "Type", "earmarked": "Earmarked",
            "opening": "Opening", "transfer_in": "Transfers in",
            "transfer_out": "Transfers out", "paid_in": "Paid in",
            "paid_out": "Paid out", "closing": "Closing",
        },
    ),
    use_container_width=True,
    hide_index=True,
)
