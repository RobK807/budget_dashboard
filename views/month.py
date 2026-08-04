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
    "What is owed on a card *on top of* the bill it is about to pay. The card's own two "
    "dates decide which of three states it is in: before the statement is issued the whole "
    "balance is outstanding; between the statement and the direct debit the balance less "
    "that bill is outstanding, because the bill is already spoken for; once it has been "
    "paid the balance is outstanding again, building towards the next statement."
)

card_accounts = data["accounts"][data["accounts"]["type"] == "credit_card"]

if card_accounts.empty:
    st.caption("No credit card accounts.")
else:
    outstanding = repo.card_outstanding(
        balances, data["card_statements"], data["accounts"], period
    )
    as_of = outstanding["as_of"].iloc[0] if not outstanding.empty else None
    st.dataframe(
        ui.money_table(
            outstanding.drop(columns="as_of"),
            ["closing", "statement", "awaiting", "outstanding"],
            labels={
                "account": "Card",
                "closing": "Balance",
                "statement": "This month's bill",
                "awaiting": "Awaiting payment",
                "outstanding": "Outstanding",
                "position": "Where in the cycle",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )
    if as_of is not None:
        st.caption(
            f"Read as at {as_of:%d %B %Y} — today for the current month, the month end for "
            "any other, so a past month's answer stops moving. **This month's bill** is the "
            "statement figure entered for this month; **awaiting payment** is whichever bill "
            "is actually standing right now, which for a card collected the following month "
            "is the previous month's — that is why the two can differ."
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

    with st.expander(f"Enter the bills for {repo.period_label(period)}"):
        st.caption(
            "Bills arrive at different times through the month, so fill them in as they "
            "come. A blank is left blank rather than stored as zero, and saving only writes "
            "what is filled in — entering one card today and another next week is fine, and "
            "clearing a figure removes it."
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
                        "bill": (
                            float(stored[int(row["id"])])
                            if int(row["id"]) in stored
                            else None
                        ),
                    }
                    for _, row in card_accounts.iterrows()
                ]
            ),
            by="card",
        )
        # Nullable, so an unentered bill stays visibly empty. A plain float column would
        # coerce the blanks to NaN and then render them as 0.00, which reads as 'no bill
        # this month' rather than 'not told yet'.
        bill_frame["bill"] = bill_frame["bill"].astype("Float64")

        read_only = ui.read_only()
        edited_bills = st.data_editor(
            bill_frame,
            use_container_width=True,
            hide_index=True,
            disabled=["id", "card"] if not read_only else True,
            column_order=["card", "bill"],
            column_config={
                "card": "Card",
                "bill": ui.editable_money(
                    "Bill at end of month", "Leave blank until the statement arrives"
                ),
            },
            key=f"bill_editor_{period}",
        )
        if st.button("Save bills", type="primary", disabled=read_only, key="save_bills"):
            saved = cleared = 0
            with ui.session() as session, session.begin():
                for _, row in edited_bills.iterrows():
                    blank = row["bill"] is None or pd.isna(row["bill"])
                    reference.set_card_statement(
                        session, period, int(row["id"]),
                        None if blank else Decimal(str(row["bill"])),
                    )
                    if blank:
                        cleared += int(int(row["id"]) in stored)
                    else:
                        saved += 1
                outcome = reference.Outcome(
                    True,
                    f"{saved} bill(s) saved for {repo.period_label(period)}"
                    + (f", {cleared} cleared." if cleared else "."),
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
        st.plotly_chart(ui.money_axis(fig), use_container_width=True)

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
        st.plotly_chart(ui.money_axis(fig), use_container_width=True)
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
