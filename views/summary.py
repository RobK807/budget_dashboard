"""Summary -- the year at a glance. Replaces the Summary tab's actuals."""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from budget import repo, ui

data = ui.page_header(
    "Summary",
    "Savings, investments and spending across the financial year.",
)

postings = data["postings"]
accounts = data["accounts"]
periods = data["periods"]
live_periods = ui.periods_with_data(data)

if not live_periods:
    st.info("No transactions yet.")
    st.stop()

series = repo.monthly_series(postings, data["openings"], accounts, periods)

ACCOUNT_COLUMNS = ["opening", "paid_in", "paid_out", "transfer_in", "transfer_out", "closing"]
ACCOUNT_LABELS = {
    "opening": "Opening",
    "paid_in": "Paid in",
    "paid_out": "Paid out",
    "transfer_in": "Transfers in",
    "transfer_out": "Transfers out",
    "closing": "Closing",
}

# ------------------------------------------------------------------ headline position

head_left, head_right = st.columns([3, 1])
head_left.subheader("Position at end of month")
selected = head_right.selectbox(
    "Month",
    options=periods,
    index=periods.index(live_periods[-1]),
    format_func=repo.period_label,
    key="summary_period",
    help="Drives the figures below. The charts always cover the whole year.",
)

balances = repo.account_balances(postings, data["openings"], selected, accounts)
position = repo.savings_position(balances)

if selected not in live_periods:
    st.info(
        f"No transactions recorded for {repo.period_label(selected)} — "
        "the figures below are carried-forward opening balances."
    )

cols = st.columns(5)
for col, (label, key, help_text) in zip(
    cols,
    [
        ("Current accounts", "current", "Bank accounts excluding savings and investments"),
        ("Savings", "savings", "Accounts flagged as savings"),
        ("Investments", "investments", "Accounts flagged as investments"),
        ("ISA", "isa", "Accounts flagged as ISA"),
        ("Card balances", "cards", "Credit cards, shown as debt owed"),
    ],
):
    col.metric(label, ui.money(position[key]), help=help_text)

st.divider()

# ------------------------------------------------------------------------ account detail

st.subheader(f"Accounts at end of {repo.period_label(selected)}")
st.caption(
    "Paid in and out are money genuinely entering or leaving. Transfers between your own "
    "accounts are shown separately — the workbook mixed them into the same totals."
)

table = balances[
    (balances["closing"] != 0) | (balances["total_in"] != 0) | (balances["total_out"] != 0)
].copy()
table["kind"] = table.apply(
    lambda r: "Credit card"
    if r["type"] == "credit_card"
    else "Investment"
    if r["is_investment"]
    else "Savings"
    if r["is_savings"]
    else "Current",
    axis=1,
)

st.dataframe(
    ui.money_table(
        table[["account", "kind"] + ACCOUNT_COLUMNS],
        ACCOUNT_COLUMNS,
        labels={"account": "Account", "kind": "Type", **ACCOUNT_LABELS},
    ),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# ------------------------------------------------------- classification actuals by month

st.subheader("Spend by classification")
st.caption(
    "Actual spend per month: direction × (debits − credits), transfers excluded. For the "
    "same figures with balances rolled forward and future days projected, see **Trends**."
)

matrix = repo.classification_by_month(postings)
if not matrix.empty:
    matrix = matrix.reindex([p for p in periods if p in matrix.index])
    display = matrix.copy()
    display.index = [repo.period_label(p) for p in display.index]
    display = display.astype(float)
    st.dataframe(ui.heatmap(display), use_container_width=True)

st.divider()

# --------------------------------------------------------------------- account by month

st.subheader("Account by month")
st.caption("One account across the year, on the same basis as the table above.")

chosen_account = st.selectbox(
    "Account",
    options=ui.alphabetical(accounts["name"]),
    key="summary_account",
    help="Opening, movements and closing balance for each month so far.",
)

history = repo.account_history(
    postings, data["openings"], accounts, chosen_account, periods
)
if history.empty:
    st.info(f"Nothing recorded for {chosen_account}.")
else:
    st.dataframe(
        ui.money_table(
            history[["month"] + ACCOUNT_COLUMNS],
            ACCOUNT_COLUMNS,
            labels={"month": "Month", **ACCOUNT_LABELS},
        ),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# --------------------------------------------------------------------- savings over time

left, right = st.columns(2)

with left:
    st.subheader("Savings and investments")
    plot = series[series["period"].isin(live_periods)].copy()
    plot["Month"] = plot["period"].map(repo.period_label)
    # Available is drawn alongside the total rather than instead of it: the gap between the
    # two lines is what the earmarked pots hold, which is the thing worth seeing.
    plot = ui.to_float(plot, ["savings", "available", "investments", "isa"]).rename(
        columns={
            "savings": "Savings (total)",
            "available": "Savings (available)",
            "investments": "Investments",
        }
    )
    fig = px.line(
        plot,
        x="Month",
        y=["Savings (total)", "Savings (available)", "Investments"],
        markers=True,
        labels={"value": "Balance (£)", "variable": ""},
    )
    fig.update_layout(hovermode="x unified", legend_title_text="", margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig), use_container_width=True)

with right:
    st.subheader("Net cashflow by month")
    plot = series[series["period"].isin(live_periods)].copy()
    plot["Month"] = plot["period"].map(repo.period_label)
    plot = ui.to_float(plot, ["net_cashflow"])
    fig = px.bar(plot, x="Month", y="net_cashflow", labels={"net_cashflow": "Net (£)"})
    fig.update_traces(
        marker_color=[
            ui.POSITIVE if v >= 0 else ui.NEGATIVE for v in plot["net_cashflow"]
        ]
    )
    fig.update_layout(margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig), use_container_width=True)
    st.caption(
        "Sum of all signed account movements. Transfers net to zero, so this is money "
        "genuinely entering or leaving."
    )

st.divider()

# ------------------------------------------------------------------------ account targets

st.subheader(f"Account targets for {repo.period_label(selected)}")
st.caption(
    "Summary B21:E26 — what each account should be holding against what it is. 'Current' is "
    "the closing balance; 'Required' is the shortfall. Targets are set per month under "
    "**Settings → General → Account targets**; the workbook kept one set, for whichever "
    "month it happened to be showing."
)

targets = repo.account_target_table(
    balances, data["account_targets"], accounts, selected
)
if targets.empty:
    st.info(
        f"No targets set for {repo.period_label(selected)}. Set them under "
        "**Settings → General → Account targets**."
    )
else:
    st.dataframe(
        ui.money_table(
            targets, ["target", "current", "required"],
            labels={"account": "Account", "target": "Target", "current": "Current",
                    "required": "Required"},
        ),
        use_container_width=True,
        hide_index=True,
    )
    short = targets[targets["required"] > 0]
    if short.empty:
        st.success("Every account is on or above its target.")
    else:
        st.warning(
            f"{len(short)} account(s) short by {ui.money(short['required'].sum())} in total."
        )
