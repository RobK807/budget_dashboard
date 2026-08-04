"""Transactions -- replaces the RemoveTransaction / TransactList form pair with one
filtered table plus soft delete.

The workbook needed two forms and a hidden filter area on the Debug sheet to locate a row
before it could shuffle it out. Here deletion is an UPDATE against a primary key, and the
row stays visible under 'Deleted' rather than disappearing.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from budget import service, ui

data = ui.page_header("Transactions", "Every transaction in the ledger, filtered.")

txns = data["transactions"]
if txns.empty:
    st.info("No transactions yet.")
    st.stop()

# -------------------------------------------------------------------------------- filters

with st.container(border=True):
    row1 = st.columns([2, 1, 1])

    min_date, max_date = txns["date"].min().date(), txns["date"].max().date()
    date_range = row1[0].date_input(
        "Date range", value=(min_date, max_date), min_value=min_date, max_value=max_date
    )
    types = row1[1].multiselect("Type", ui.alphabetical(txns["type"]))
    show_deleted = row1[2].selectbox(
        "Deleted", ["Hide", "Show", "Only"], help="Deleted rows are retained, never removed"
    )

    row2 = st.columns(4)
    all_accounts = ui.alphabetical(
        set(txns["account_from"].dropna()) | set(txns["account_to"].dropna())
    )
    accounts = row2[0].multiselect("Account", all_accounts)
    categories = row2[1].multiselect("Category", ui.alphabetical(txns["category"]))
    classifications = row2[2].multiselect(
        "Classification", ui.alphabetical(txns["classification"])
    )
    search = row2[3].text_input("Comment contains")

view = txns.copy()

if isinstance(date_range, tuple) and len(date_range) == 2:
    start, end = (pd.Timestamp(d) for d in date_range)
    view = view[(view["date"] >= start) & (view["date"] <= end)]

if show_deleted == "Hide":
    view = view[~view["deleted"]]
elif show_deleted == "Only":
    view = view[view["deleted"]]

if types:
    view = view[view["type"].isin(types)]
if accounts:
    view = view[view["account_from"].isin(accounts) | view["account_to"].isin(accounts)]
if categories:
    view = view[view["category"].isin(categories)]
if classifications:
    view = view[view["classification"].isin(classifications)]
if search:
    haystack = (
        view["comment"].fillna("") + " " + view["category_comment"].fillna("")
    ).str.lower()
    view = view[haystack.str.contains(search.lower(), regex=False)]

# ------------------------------------------------------------------------------- summary

debits = view.loc[view["type"] == "Debit", "amount"].sum()
credits = view.loc[view["type"] == "Credit", "amount"].sum()
transfers = view.loc[view["type"] == "Transfer", "amount"].sum()

cols = st.columns(4)
cols[0].metric("Matching", f"{len(view):,}")
cols[1].metric("Debits", ui.money(debits))
cols[2].metric("Credits", ui.money(credits))
cols[3].metric("Transfers", ui.money(transfers))

# --------------------------------------------------------------------------------- table

display = view[
    ["id", "date", "type", "amount", "account_from", "account_to", "category",
     "classification", "comment", "deleted"]
].copy()
display["date"] = display["date"].dt.date

st.dataframe(
    ui.money_table(
        display,
        ["amount"],
        labels={
            "id": "ID",
            "date": "Date",
            "type": "Type",
            "amount": "Amount",
            "account_from": "From",
            "account_to": "To",
            "category": "Category",
            "classification": "Classification",
            "comment": "Comment",
            "deleted": "Deleted",
        },
    ),
    use_container_width=True,
    hide_index=True,
    height=560,
)

st.download_button(
    "Download as CSV",
    view.to_csv(index=False).encode("utf-8"),
    file_name="transactions.csv",
    mime="text/csv",
)

st.divider()

# ------------------------------------------------------------------- delete and restore

st.subheader("Remove or restore")
st.caption(
    "Deletion is always soft: the row is flagged, never erased, and stays visible via the "
    "Deleted filter above."
)

live = view[~view["deleted"]]
gone = view[view["deleted"]]

remove_col, restore_col = st.columns(2)

with remove_col:
    st.markdown("**Remove a transaction**")
    if live.empty:
        st.caption("Nothing in the current selection to remove.")
    else:
        options = {
            int(r.id): f"#{r.id} · {r.date:%d %b} · {ui.money(r.amount)} · "
            f"{r.account_from} · {r.category or r.type}"
            for r in live.head(500).itertuples()
        }
        target = st.selectbox(
            "Transaction", list(options), format_func=lambda i: options[i], key="rm_target"
        )
        reason = st.text_input("Reason (optional)", key="rm_reason")
        if st.button("Remove", type="primary"):
            with ui.session() as session, session.begin():
                done = service.soft_delete(session, target, reason or None)
            ui.load_all.clear()
            if done:
                st.success(f"Removed #{target}. It can be restored on the right.")
                ui.auto_push("the removal")
                st.rerun()
            else:
                st.warning(f"#{target} was already removed.")

with restore_col:
    st.markdown("**Restore a removed transaction**")
    if gone.empty:
        st.caption("Nothing removed in the current selection. Set Deleted to Show or Only.")
    else:
        options = {
            int(r.id): f"#{r.id} · {r.date:%d %b} · {ui.money(r.amount)} · "
            f"{r.account_from} · {r.deleted_reason or 'no reason given'}"
            for r in gone.head(500).itertuples()
        }
        target = st.selectbox(
            "Removed transaction",
            list(options),
            format_func=lambda i: options[i],
            key="restore_target",
        )
        if st.button("Restore"):
            with ui.session() as session, session.begin():
                done = service.restore(session, target)
            ui.load_all.clear()
            if done:
                st.success(f"Restored #{target}.")
                ui.auto_push("the restore")
                st.rerun()
            else:
                st.warning(f"#{target} is not currently removed.")
