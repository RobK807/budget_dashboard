"""Summary -- the year at a glance. Replaces the Summary tab's actuals."""

from __future__ import annotations

import datetime as dt

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

# The one place the privacy switch is set; every page reads it. Above the early return as
# well as above the figures, so it can still be turned back off on an empty database.
PRIVATE = ui.privacy_switch()

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
selected = ui.month_select(
    "Month", periods,
    default=live_periods[-1] if repo.period_of(dt.date.today()) not in periods else None,
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
    ui.metric(col, label, ui.money(position[key]), help=help_text)

st.divider()

# ------------------------------------------------------------------------ account detail

st.subheader(f"Accounts at end of {repo.period_label(selected)}")
st.caption(
    "Paid in and out are money genuinely entering or leaving. Transfers between your own "
    "accounts are shown separately, so neither side is flattered by them."
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
    width="stretch",
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
    matrix = matrix.reindex([p for p in periods if p in matrix.index][::-1])
    display = matrix.copy()
    display.index = [repo.period_label(p) for p in display.index]
    display = display.astype(float)
    st.dataframe(ui.heatmap(display), width="stretch")

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
            ui.newest_first(history[["month"] + ACCOUNT_COLUMNS]),
            ACCOUNT_COLUMNS,
            labels={"month": "Month", **ACCOUNT_LABELS},
        ),
        width="stretch",
        hide_index=True,
    )

st.divider()

# --------------------------------------------------------------------- savings over time

# Trims what the charts draw, not what was computed: `series` is built over every period
# above and only the window changes here.
chart_from = ui.month_from(live_periods, key="summary_chart_from", tax_year=data["tax_year"])
charted = [p for p in live_periods if p >= chart_from]

left, right = st.columns(2)

with left:
    st.subheader("Savings and investments")
    plot = series[series["period"].isin(charted)].copy()
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
    st.plotly_chart(ui.money_axis(fig), width="stretch")

with right:
    st.subheader("Net cashflow by month")
    plot = series[series["period"].isin(charted)].copy()
    plot["Month"] = plot["period"].map(repo.period_label)
    plot = ui.to_float(plot, ["net_cashflow"])
    fig = px.bar(plot, x="Month", y="net_cashflow", labels={"net_cashflow": "Net (£)"})
    fig.update_traces(
        marker_color=[
            ui.POSITIVE if v >= 0 else ui.NEGATIVE for v in plot["net_cashflow"]
        ]
    )
    fig.update_layout(margin=dict(t=10))
    st.plotly_chart(ui.money_axis(fig), width="stretch")
    st.caption(
        "Sum of all signed account movements. Transfers net to zero, so this is money "
        "genuinely entering or leaving."
    )

st.divider()

# ------------------------------------------------------------------------ account targets

st.subheader(f"Account targets for {repo.period_label(selected)}")
st.caption(
    "What each account should be holding against what it is. 'Current' is the closing "
    "balance; 'Required' is the shortfall. Targets are set per month under "
    "**Settings → General → Account targets**."
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
        width="stretch",
        hide_index=True,
    )
    short = targets[targets["required"] > 0]
    if short.empty:
        st.success("Every account is on or above its target.")
    else:
        st.warning(
            f"{len(short)} account(s) short by {ui.money(short['required'].sum())} in total."
        )

# ---- what those targets are made of ------------------------------------------------

st.markdown("##### What is due, and when")
st.caption(
    "The standing payments behind each target. 'Still needed' counts down what has yet to "
    "go out of that account after each payment, reaching zero on the last. Each account "
    "runs from its own funding day, so one paid on the 19th covers the 19th to the 18th "
    "rather than a calendar month. Both are set under "
    "**Settings → General → Account commitments**."
)

commitments = repo.account_commitment_table(
    data["account_commitments"], selected, accounts
)
if commitments.empty:
    st.info(
        "No commitments listed. Add them under "
        "**Settings → General → Account commitments**."
    )
else:
    shown = commitments.drop(columns=["day"])
    shown["due"] = [d.strftime("%a %d %b") for d in shown["due"]]
    st.dataframe(
        ui.money_table(
            shown, ["amount", "still_needed"],
            labels={"account": "Account", "name": "Item", "due": "Due",
                    "amount": "Amount", "still_needed": "Still needed"},
        ),
        width="stretch",
        hide_index=True,
    )

    # The dates now run across two calendar months for anything not funded on the 1st, so
    # say which window each account is being shown on rather than leaving it to be inferred.
    windows = []
    for account in commitments["account"].unique():
        row = accounts[accounts["name"] == account]
        start = repo.commitment_start_day(row.iloc[0] if not row.empty else None)
        opens, closes = repo.commitment_cycle(selected, start)
        windows.append(f"{account} {opens:%d %b} – {closes:%d %b}")
    st.caption("Cycles: " + " · ".join(windows))

    blank = commitments[commitments["amount"] == 0]
    if not blank.empty:
        st.warning(
            f"{len(blank)} item(s) have no amount yet: "
            + ", ".join(f"{r['account']} — {r['name']}" for _, r in blank.iterrows())
            + ". They are listed for their date; set the amount under Settings."
        )

    # The two are stored separately and neither derives from the other, so say so when they
    # disagree. A target *below* its items is the one that matters.
    against = repo.commitments_against_targets(
        data["account_commitments"], data["account_targets"], accounts, selected
    )
    under = against[
        against["difference"].notna() & (against["difference"] < 0)
    ]
    if not under.empty:
        st.warning(
            "Target set below what is due out of it: "
            + "; ".join(
                f"{r['account']} — {ui.money(r['itemised'])} due against a "
                f"{ui.money(r['target'])} target"
                for _, r in under.iterrows()
            )
        )

    with st.expander("Itemised totals against each target"):
        st.dataframe(
            ui.money_table(
                against, ["itemised", "target", "difference"],
                labels={"account": "Account", "items": "Items",
                        "itemised": "Itemised", "target": "Target",
                        "difference": "Target less items"},
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "The target is a figure in its own right, not a total of the list — it can "
            "hold a buffer above the payments, or lag behind a commitment that has just "
            "changed. Neither number is derived from the other."
        )
