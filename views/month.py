"""Month -- what a month tab showed, as a query rather than 400 columns of stored pivot."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from budget import reference, repo, ui

data = ui.page_header("Month", "Balances, budget and daily spend for a single month.")

postings = data["postings"]
live_periods = ui.periods_with_data(data)

if not live_periods:
    st.info("No transactions yet.")
    st.stop()

period = st.selectbox(
    "Month",
    options=data["periods"],
    index=data["periods"].index(live_periods[-1]),
    format_func=repo.period_label,
)

balances = repo.account_balances(postings, data["openings"], period, data["accounts"])
budget = repo.budget_vs_actual(postings, data["budgets"], period, data["categories"])
daily = repo.daily_classification(postings, period)
month_postings = postings[postings["period"] == period]

if month_postings.empty:
    st.info(f"No transactions recorded for {repo.period_label(period)}.")
    st.stop()

# ------------------------------------------------------------------------------ headline

n_txn = month_postings["txn_id"].nunique()

cols = st.columns(5)
cols[0].metric("Transactions", f"{n_txn:,}")
cols[1].metric(
    "Paid in", ui.money(balances["paid_in"].sum()), help="Excluding transfers between accounts"
)
cols[2].metric(
    "Paid out", ui.money(balances["paid_out"].sum()), help="Excluding transfers between accounts"
)
cols[3].metric(
    "Transfers",
    ui.money(balances["transfer_out"].sum()),
    help="Moved between your own accounts; nets to zero overall",
)
cols[4].metric(
    "Spend vs budget",
    ui.money(budget["spent"].sum()),
    delta=ui.money(budget["left"].sum()) + " left",
    delta_color="off",
)

st.divider()

# ------------------------------------------------------------------------------ balances

st.subheader("Accounts")
st.caption(
    "Month-tab rows 60–63, with transfers separated out. The workbook's 'Total paid in' and "
    "'Total paid out' are `=SUM(I4:I59)` and `=SUM(J4:J59)`, so moving money between your "
    "own accounts inflated both sides."
)

table = balances[
    (balances["closing"] != 0) | (balances["total_in"] != 0) | (balances["total_out"] != 0)
].copy()

money_cols = [
    "opening", "paid_in", "paid_out", "transfer_in", "transfer_out", "movement", "closing"
]
st.dataframe(
    ui.money_table(
        table[["account", "type"] + money_cols],
        money_cols,
        labels={
            "account": "Account",
            "type": "Type",
            "opening": "Opening",
            "paid_in": "Paid in",
            "paid_out": "Paid out",
            "transfer_in": "Transfers in",
            "transfer_out": "Transfers out",
            "movement": "Movement",
            "closing": "Closing",
        },
    ),
    use_container_width=True,
    hide_index=True,
)

st.divider()

# --------------------------------------------------------------- credit card outstanding

st.subheader("Credit cards outstanding")
st.caption(
    "Month-tab B42:E52. The balance on the card less whatever has already been billed — so "
    "what is left is the spending that has not yet reached a statement, owed on top of the "
    "next direct debit. Before the payment day the opening statement is still owed; once "
    "the closing statement has been issued that is what is owed instead."
)

card_accounts = data["accounts"][data["accounts"]["type"] == "credit_card"]

if card_accounts.empty:
    st.caption("No credit card accounts.")
else:
    outstanding = repo.card_outstanding(
        balances, data["card_statements"], data["accounts"], period
    )
    st.dataframe(
        ui.money_table(
            outstanding,
            ["closing", "bill_bom", "bill_eom", "billed", "outstanding"],
            labels={
                "account": "Card",
                "closing": "Balance",
                "bill_bom": "Bill at start",
                "bill_eom": "Bill at end",
                "billed": "Already billed",
                "outstanding": "Outstanding",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )

    missing = [
        row["name"] for _, row in card_accounts.iterrows()
        if pd.isna(row["statement_day"]) or pd.isna(row["payment_day"])
    ]
    if missing:
        st.info(
            "No statement or payment day set for "
            + ", ".join(repo.sort_human(pd.DataFrame({"n": missing}), by="n")["n"])
            + ". Set them under **Settings → General → Credit card billing**, or the "
            "outstanding figure cannot tell which bill applies."
        )

    with st.expander(f"Enter the closing bills for {repo.period_label(period)}"):
        st.caption(
            "The bill at the start of a month is the previous month's closing bill, so only "
            "the closing figure is entered. That held exactly for BA Amex and Mastercard in "
            "every month of the workbook; Platinum Amex's July opening had been adjusted by "
            "hand and differed by £76.05."
        )
        statements = data["card_statements"]
        here = statements[statements["period"] == period]
        stored = dict(zip(here["account_id"], here["bill_eom"])) if not here.empty else {}

        bill_frame = repo.sort_human(
            pd.DataFrame(
                [
                    {
                        "id": int(row["id"]),
                        "card": row["name"],
                        "bill": float(stored.get(int(row["id"]), 0) or 0),
                    }
                    for _, row in card_accounts.iterrows()
                ]
            ),
            by="card",
        )

        read_only = ui.read_only()
        edited_bills = st.data_editor(
            bill_frame,
            use_container_width=True,
            hide_index=True,
            disabled=["id", "card"] if not read_only else True,
            column_order=["card", "bill"],
            column_config={
                "card": "Card",
                "bill": ui.editable_money("Credit card bill EoM"),
            },
            key=f"bill_editor_{period}",
        )
        if st.button("Save bills", type="primary", disabled=read_only, key="save_bills"):
            with ui.session() as session, session.begin():
                for _, row in edited_bills.iterrows():
                    amount = Decimal(str(row["bill"] or 0))
                    reference.set_card_statement(
                        session, period, int(row["id"]), amount if amount else None
                    )
                outcome = reference.Outcome(
                    True, f"Bills for {repo.period_label(period)} saved."
                )
            ui.show_outcome(outcome, "the card bills")

st.divider()

# ------------------------------------------------------------------------ budget vs spend

st.subheader("Budget vs actual")
st.caption(
    "Month-tab columns B–F. Income and Spent are actuals from the ledger; only Expected "
    "Costs is a budget. The workbook keeps these two separate rather than netting them, so "
    "a category can carry both."
)

groupings = ui.alphabetical(budget["grouping"])
chosen = st.multiselect("Grouping", groupings, default=groupings)
view = budget[budget["grouping"].isin(chosen)].copy()

st.dataframe(
    ui.money_table(
        view[["category", "grouping", "income", "expected", "spent", "left"]],
        ["income", "expected", "spent", "left"],
        labels={
            "category": "Category",
            "grouping": "Grouping",
            "income": "Income",
            "expected": "Expected",
            "spent": "Spent",
            "left": "Left",
        },
    ),
    use_container_width=True,
    hide_index=True,
)

overspent = view[(view["expected"] > 0) & (view["left"] < 0)].sort_values("left")
if not overspent.empty:
    worst = ", ".join(
        f"{r.category} by {ui.money(-r.left)}" for r in overspent.head(5).itertuples()
    )
    more = len(overspent) - 5
    st.warning(
        f"Over budget in {len(overspent)} categories, worst first: {worst}"
        + (f", and {more} more." if more > 0 else ".")
    )

st.divider()

# --------------------------------------------------------------------------- daily spend

st.subheader("Daily spend by classification")

if daily.empty:
    st.info("No classified spending this month.")
else:
    plot = daily.copy()
    plot["total"] = plot["total"].astype(float)

    tab_daily, tab_cumulative = st.tabs(["Daily", "Cumulative"])

    with tab_daily:
        fig = px.bar(
            plot,
            x="date",
            y="total",
            color="classification",
            labels={"total": "£", "date": "", "classification": ""},
        )
        fig.update_layout(hovermode="x unified", margin=dict(t=10), barmode="relative")
        st.plotly_chart(fig, use_container_width=True)

    with tab_cumulative:
        wide = (
            plot.pivot_table(
                index="date", columns="classification", values="total", aggfunc="sum"
            )
            .fillna(0)
            .sort_index()
            .cumsum()
        )
        fig = px.line(wide, labels={"value": "£", "date": "", "variable": ""})
        fig.update_layout(hovermode="x unified", margin=dict(t=10))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Cumulative within this month only. For balances carried across months under "
            "each classification's rollover rule, see **Trends**."
        )

    totals = (
        plot.groupby("classification", as_index=False)["total"]
        .sum()
        .sort_values("total", ascending=False)
    )
    st.dataframe(
        ui.money_table(
            totals, ["total"], labels={"classification": "Classification", "total": "Total"}
        ),
        use_container_width=True,
        hide_index=True,
    )
