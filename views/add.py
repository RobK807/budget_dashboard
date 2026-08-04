"""Add -- replaces the Input tab and New_entry.

The workbook asked for the month as a separate field; here the period is derived from the
date, so a row can no longer be filed under the wrong month.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import streamlit as st

from budget import repo, service, ui
from budget.validation import Candidate, period_for

data = ui.page_header("Add transaction", "One transaction at a time. For many, use Import.")

accounts = ui.alphabetical(data["accounts"]["name"])
categories = ui.alphabetical(data["categories"]["name"])
classifications = ui.alphabetical(data["classifications"]["name"])

with st.form("add_transaction", clear_on_submit=False):
    row1 = st.columns(3)
    txn_date = row1[0].date_input("Date", value=dt.date.today(), format="DD/MM/YYYY")
    txn_type = row1[1].selectbox("Type", ["Debit", "Credit", "Transfer"])
    amount = row1[2].number_input("Amount (£)", min_value=0.0, step=1.0, format="%.2f")

    row2 = st.columns(2)
    account_from = row2[0].selectbox("Account From", accounts)
    account_to = row2[1].selectbox(
        "Account To", ["—"] + accounts, help="Transfers only"
    )

    row3 = st.columns(2)
    category = row3[0].selectbox("Category", ["—"] + categories)
    classification = row3[1].selectbox("Purchase type", ["—"] + classifications)

    row4 = st.columns(2)
    comment = row4[0].text_input("Comment")
    category_comment = row4[1].text_input("Category comment")

    submitted = st.form_submit_button("Add transaction", type="primary")

if submitted:
    candidate = Candidate(
        txn_date=txn_date,
        type=txn_type,
        amount=Decimal(str(amount)),
        account_from=account_from,
        account_to=None if account_to == "—" else account_to,
        category=None if category == "—" else category,
        classification=None if classification == "—" else classification,
        comment=comment or None,
        category_comment=category_comment or None,
    )

    with ui.session() as session, session.begin():
        txn, result = service.add_transaction(session, candidate)
        if txn is not None:
            saved = (txn.id, txn.legacy_identifier, txn.period)
        else:
            saved = None

    if saved is None:
        st.error("Not saved:\n\n" + "\n".join(f"- {e}" for e in result.errors))
    else:
        txn_id, identifier, period = saved
        ui.load_all.clear()
        st.success(
            f"Added #{txn_id} — {ui.money(candidate.amount)} {txn_type.lower()} on "
            f"{txn_date:%d %b %Y}, filed under {repo.period_label(period)} "
            f"(`{identifier}`)."
        )
        for w in result.warnings:
            st.warning(w)
        ui.auto_push("adding a transaction")

st.divider()
st.caption(
    "Validation mirrors the checks the Input tab displayed but never enforced — a transfer "
    "needs a destination, a Credit-only category will not take a Debit, a category comment "
    "needs a category. The period is derived from the date rather than chosen separately."
)
