"""Transactions -- replaces the RemoveTransaction / TransactList form pair with one
filtered table plus soft delete.

The workbook needed two forms and a hidden filter area on the Debug sheet to locate a row
before it could shuffle it out. Here deletion is an UPDATE against a primary key, and the
row stays visible under 'Deleted' rather than disappearing.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from budget import reference, service, ui

data = ui.page_header("Transactions", "Every transaction in the ledger, filtered.")

txns = data["transactions"]
if txns.empty:
    st.info("No transactions yet.")
    st.stop()

# A transfer carries no category or classification -- New_entry never wrote one -- so those
# cells are genuinely empty. Naming them up front, before anything reads the column, means
# the filters offer the same words the table shows: 'Transfer' rather than 'nan', and '—'
# for an ordinary row that was left unclassified, which is worth being able to search for.
txns = ui.name_blanks(txns, ["category", "classification"])

# -------------------------------------------------------------------------------- filters

with st.container(border=True):
    row1 = st.columns([2, 1, 1])

    # Bounds come from the data; the opening position is the last ninety days, so the
    # default does not silently widen as history accumulates.
    min_date, max_date = txns["date"].min().date(), txns["date"].max().date()
    date_range = row1[0].date_input(
        "Date range",
        value=ui.default_range(90, min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        format="DD/MM/YYYY",
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
     "classification", "comment", "is_donation", "deleted"]
].copy()
display["date"] = display["date"].dt.date
# Category and classification were named at the top of the page, so the filters and the table
# agree. These two are display-only.
display = ui.name_blanks(display, ["account_to", "comment"])

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
            "is_donation": "Donation",
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
    "Deletion is always soft: the row is flagged, never erased, and stays visible under the "
    "Deleted filter at the top of the page."
)

# Independent of the filters at the top of the page. Reading the ledger and picking a single
# row to remove are different jobs: the table above is usually left wide open, and inheriting
# that made the removal dropdown five hundred entries long, while narrowing it to read
# something silently changed what could be removed.
with st.container(border=True):
    st.caption(
        "These filters apply only to the two lists below — they are independent of the "
        "filters at the top of the page, and search the whole ledger."
    )
    picker = st.columns([2, 2, 1])
    pick_range = picker[0].date_input(
        "Date range",
        value=ui.default_range(90, min_date, max_date),
        min_value=min_date,
        max_value=max_date,
        format="DD/MM/YYYY",
        key="rm_range",
        help="Defaults to the last ninety days.",
    )
    pick_accounts = picker[1].multiselect("Account", all_accounts, key="rm_accounts")
    pick_search = picker[2].text_input("Comment contains", key="rm_search")

selection = txns.copy()
if isinstance(pick_range, tuple) and len(pick_range) == 2:
    pick_start, pick_end = (pd.Timestamp(d) for d in pick_range)
    selection = selection[
        (selection["date"] >= pick_start) & (selection["date"] <= pick_end)
    ]
if pick_accounts:
    selection = selection[
        selection["account_from"].isin(pick_accounts)
        | selection["account_to"].isin(pick_accounts)
    ]
if pick_search:
    text = (
        selection["comment"].fillna("") + " " + selection["category_comment"].fillna("")
    ).str.lower()
    selection = selection[text.str.contains(pick_search.lower(), regex=False)]

live = selection[~selection["deleted"]]
gone = selection[selection["deleted"]]

remove_col, restore_col = st.columns(2)

with remove_col:
    st.markdown("**Remove a transaction**")
    if live.empty:
        st.caption("Nothing matches the filters above this pair of lists.")
    else:
        options = {int(r.id): ui.describe_txn(r) for r in live.head(500).itertuples()}
        st.caption(f"{len(live):,} matching; the {len(options)} most recent are listed.")
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
        st.caption("Nothing removed in this date range or account.")
    else:
        options = {
            int(r.id): ui.describe_txn(r)
            + f" · {r.deleted_reason or 'no reason given'}"
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

st.divider()

# ----------------------------------------------------------------------- donation flag

st.subheader("Flag a donation")
st.caption(
    "Charitable giving is counted under **Savings and investments**, by tax year. New "
    "entries carry the flag from **Add transaction** or **Import**; this is for one already "
    "recorded. Where a payment covers both a gift and a platform fee, split it into two "
    "transactions and flag only the gift — the fee is not a donation."
)

flag_left, flag_right = st.columns([2, 1])
with flag_left:
    if live.empty:
        st.caption("Nothing matches the filters above.")
    else:
        flag_options = {
            int(r.id): ui.describe_txn(r) + (" · donation" if r.is_donation else "")
            for r in live.head(500).itertuples()
        }
        flag_target = st.selectbox(
            "Transaction",
            list(flag_options),
            format_func=lambda i: flag_options[i],
            key="donation_target",
        )
        currently = bool(
            live.loc[live["id"] == flag_target, "is_donation"].iloc[0]
        )
        wanted = st.checkbox(
            "Charitable donation", value=currently, key="donation_flag"
        )
        if st.button("Save the flag", disabled=wanted == currently):
            with ui.session() as session, session.begin():
                outcome = reference.set_donation_flag(session, flag_target, wanted)
            ui.show_outcome(outcome, "the donation flag")
            st.rerun()

with flag_right:
    flagged = txns[~txns["deleted"] & txns["is_donation"].fillna(False).astype(bool)]
    st.metric("Flagged so far", f"{len(flagged):,}")
    if not flagged.empty:
        st.caption(f"Totalling {ui.money(flagged['amount'].sum())}.")
