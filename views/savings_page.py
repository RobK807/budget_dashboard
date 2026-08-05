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
#
# Six columns whichever basis is chosen, in the order the money moves: what was there, what
# went in, what was there at the end, then the two targets and the shortfall. Splitting the
# basis into a dropdown rather than widening the table keeps that reading order -- nine
# columns interleaving total and available made a row something to decode rather than read.
COLUMNS = ["bom", "added", "eom", "target", "target_eom", "required"]
LABELS = {
    "month": "Month",
    "bom": "BoM",
    "added": "Added",
    "eom": "EoM",
    "target": "Monthly target",
    "target_eom": "Cumulative target",
    "required": "Required",
}

# The cumulative target is one figure per month whichever pot is being measured against it:
# what changes with the basis is which balance is being asked to meet it, not the target.
BASES = {
    "Total": ("savings_bom", "savings_added", "savings_eom", "total_required"),
    "Available": ("available_bom", "available_added", "available_eom", "available_required"),
    "Reserved": ("reserved_bom", "reserved_added", "reserved_eom", "reserved_required"),
}

head, picker = st.columns([3, 1])
head.subheader("Savings")
basis = picker.selectbox(
    "Basis",
    options=list(BASES),
    key="savings_basis",
    help="Total is every savings account; available excludes the earmarked pots; reserved "
         "is those pots alone.",
)

bom, added, eom, required = BASES[basis]
savings = series[
    ["month", bom, added, eom, "savings_target", "savings_target_eom", required]
].set_axis(["month"] + COLUMNS, axis="columns")

st.dataframe(
    ui.money_table(savings, COLUMNS, labels=LABELS),
    use_container_width=True,
    hide_index=True,
)

st.subheader("Investments")
investments = series[
    ["month", "investments_bom", "investments_added", "investments_eom",
     "investments_target", "investments_target_eom", "investments_required"]
].set_axis(["month"] + COLUMNS, axis="columns")

st.dataframe(
    ui.money_table(investments, COLUMNS, labels=LABELS),
    use_container_width=True,
    hide_index=True,
)

st.caption(
    "**Added** is a change in balance rather than a deposit total, so a month that spends "
    "out of savings shows a negative. On the *available* basis it is what went into the "
    "unearmarked accounts, on *reserved* what went into the earmarked pots"
    + (f" ({', '.join(earmarked_names)})" if earmarked_names else "")
    + "; the two sum to the total. **Cumulative target** is the monthly targets summed to "
    "date, and **Required** is that less the balance on the chosen basis, so a positive "
    "figure is money still to find. It measures a running total of contributions against a "
    "balance, which only lines up from the month the targets start in."
)

st.divider()

# ------------------------------------------------------------------------- over time

st.subheader("Balances over the year")

BALANCE_SERIES = {
    "available_eom": "Savings (available)",
    "reserved_eom": "Savings (reserved)",
    "investments_eom": "Investments",
    "combined": "Combined (total)",
    "combined_available": "Combined (available)",
}
plot = ui.to_float(series, list(BALANCE_SERIES)).rename(columns=BALANCE_SERIES)
fig = px.line(
    plot, x="month", markers=True, y=list(BALANCE_SERIES.values()),
    labels={"value": "Balance (£)", "month": "", "variable": ""},
)
fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), use_container_width=True)
st.caption(
    "**Combined (total)** is all savings plus investments; **combined (available)** leaves "
    "out the earmarked pots, so the gap between the two lines is what is already spoken for."
)

st.subheader("Added against target")

# Against the *cumulative* target, which means what is added has to accumulate too: a month's
# 200 set beside a target that has reached 2,400 by December is not a comparison, it is two
# quantities on scales an order of magnitude apart. Both sides run from the first month shown.
ADDED_SERIES = {
    "available_added": "Savings added (available)",
    "reserved_added": "Savings added (reserved)",
    "savings_target_eom": "Savings target",
    "investments_added": "Investments added",
    "investments_target_eom": "Investments target",
}
cumulative = ui.to_float(series, list(ADDED_SERIES)).copy()
for column in ("available_added", "reserved_added", "investments_added"):
    cumulative[column] = cumulative[column].cumsum()

chart = cumulative.melt(
    id_vars="month", value_vars=list(ADDED_SERIES),
    var_name="series", value_name="amount",
)
chart["series"] = chart["series"].map(ADDED_SERIES)
fig = px.bar(
    chart.dropna(subset=["amount"]), x="month", y="amount", color="series",
    barmode="group", labels={"amount": "£", "month": "", "series": ""},
)
fig.update_layout(margin=dict(t=10))
st.plotly_chart(ui.money_axis(fig), use_container_width=True)
st.caption(
    "Everything here is cumulative from the first month shown, so a bar that stays below its "
    "target bar is a shortfall that has not been made up. The two savings bars stack "
    "conceptually: together they are the change in the total balance."
)

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
