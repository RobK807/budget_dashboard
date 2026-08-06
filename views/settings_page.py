"""Settings -- replaces the six Add/Remove UserForms and the Control tab.

Every one of those forms opened with a warning that it would overwrite all months from the
chosen one onwards. None of that applies here: nothing is stored positionally, so adding an
account is an INSERT with a start date and earlier months are untouched by construction.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pandas as pd
import streamlit as st

from budget import reference, repo, sync, ui

data = ui.page_header(
    "Settings", "Accounts, categories, classifications and the year's parameters."
)

with ui.session() as session:
    state = sync.status(session)
    counts = reference.usage(session)

# Reference-data edits require being in sync (DESIGN.md 6.3.2). Inserting a transaction
# merges cleanly across machines; changing the lists both sides refer to does not.
if state.local.mode == sync.OFFLINE:
    st.warning(
        "**Read-only while checked out for offline use.** Transactions merge across "
        "machines; changes to the accounts and lists both machines refer to do not. "
        "Check in on the Sync page first."
    )
    READ_ONLY = True
else:
    READ_ONLY = False


def status_of(row) -> str:
    if row["valid_to"] is None:
        return "Open"
    return f"Closed {pd.to_datetime(row['valid_to']):%d %b %Y}"


def show_outcome(outcome) -> None:
    if outcome.ok:
        st.success(outcome.message)
        for w in outcome.warnings:
            st.info(w)
        ui.load_all.clear()
        ui.auto_push("the settings change")
    else:
        st.error(outcome.message)


# Alphabetical, case-insensitively, like every other list in the app. The sections had
# accreted in the order they were built, which meant Cards sat between Cycling and Monthly
# figures for no reason a reader could infer. Streamlit renders into whichever tab a block
# is attached to, so the `with` blocks below stay grouped by subject.
(
    tab_accounts,
    tab_cards,
    tab_categories,
    tab_classes,
    tab_cycling,
    tab_general,
    tab_monthly,
    tab_salary,
    tab_savings,
) = st.tabs(
    ["Accounts", "Cards", "Categories", "Classifications", "Cycling", "General",
     "Monthly figures", "Salary", "Savings targets"]
)

# ---------------------------------------------------------------------------- accounts

with tab_accounts:
    accounts = data["accounts"].copy()
    accounts["status"] = accounts.apply(status_of, axis=1)
    accounts["used by"] = accounts["id"].map(lambda i: counts["account"].get(i, 0))
    accounts["kind"] = accounts["type"].map(
        {"bank": "Bank account", "credit_card": "Credit card"}
    )

    st.dataframe(
        repo.sort_human(
            accounts[["name", "short_code", "kind", "status", "used by",
                      "is_savings", "is_investment", "is_isa", "interest_net"]],
            by="name",
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "name": "Account",
            "short_code": "Code",
            "kind": "Type",
            "status": "Status",
            "used by": st.column_config.NumberColumn("Transactions", format="%d"),
            "is_savings": st.column_config.CheckboxColumn("Savings"),
            "is_investment": st.column_config.CheckboxColumn("Investment"),
            "is_isa": st.column_config.CheckboxColumn("ISA"),
            "interest_net": st.column_config.CheckboxColumn(
                "Interest net",
                help="Ticked where interest arrives already taxed. Left clear it is gross.",
            ),
        },
    )

    st.markdown("**How interest is paid**")
    st.caption(
        "Whether an account's interest arrives already taxed. The interest tracker held this "
        "as a Gross/Net row per account and used it to decide whether tax should be taken "
        "off again. Almost everything is gross, so this flags the exceptions. It drives the "
        "interest table under **Savings and investments**."
    )
    interest_frame = repo.sort_human(
        accounts[accounts["kind"] == "Bank account"][["id", "name", "interest_net"]],
        by="name",
    ).copy()
    interest_frame["interest_net"] = (
        interest_frame["interest_net"].fillna(False).astype(bool)
    )
    edited_interest = st.data_editor(
        interest_frame,
        width="stretch",
        hide_index=True,
        disabled=["id", "name"] if not READ_ONLY else True,
        column_order=["name", "interest_net"],
        column_config={
            "name": "Account",
            "interest_net": st.column_config.CheckboxColumn("Paid net of tax"),
        },
        key="interest_basis_editor",
    )
    if st.button("Save interest basis", disabled=READ_ONLY):
        changed = 0
        outcome = reference.Outcome(True, "Nothing changed.")
        with ui.session() as session, session.begin():
            for _, row in edited_interest.iterrows():
                was = bool(
                    interest_frame.loc[
                        interest_frame["id"] == row["id"], "interest_net"
                    ].iloc[0]
                )
                if bool(row["interest_net"]) != was:
                    outcome = reference.update_account(
                        session, int(row["id"]), interest_net=bool(row["interest_net"])
                    )
                    changed += 1
        if changed:
            show_outcome(outcome)
        else:
            st.info("Nothing changed.")

    st.divider()

    with st.expander("Add an account", expanded=False):
        st.caption(
            "Available from the date you choose. Earlier months are untouched — the account "
            "simply has no transactions in them."
        )
        with st.form("add_account"):
            row1 = st.columns(4)
            name = row1[0].text_input("Name")
            code = row1[1].text_input("Short code", help="e.g. HFX, BAAM")
            kind = row1[2].selectbox("Type", ["Bank account", "Credit card"])
            available = row1[3].date_input(
                "Available from", value=dt.date.today().replace(day=1), format="DD/MM/YYYY"
            )

            row2 = st.columns(4)
            savings = row2[0].checkbox("Savings account")
            savings_limit = row2[1].number_input(
                "Savings limit (£)", min_value=0.0, step=100.0,
                help="0 for no cap — mirrors Selections!S",
            )
            investment = row2[2].checkbox("Investment account")
            investment_limit = row2[3].number_input(
                "Investment limit (£)", min_value=0.0, step=100.0
            )
            isa = st.checkbox("Counts towards the ISA allowance")

            if st.form_submit_button("Add account", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    _, outcome = reference.add_account(
                        session,
                        name=name,
                        short_code=code,
                        account_type="bank" if kind == "Bank account" else "credit_card",
                        valid_from=available,
                        is_savings=savings,
                        savings_limit=Decimal(str(savings_limit)) or None,
                        is_investment=investment,
                        investment_limit=Decimal(str(investment_limit)) or None,
                        is_isa=isa,
                    )
                show_outcome(outcome)

    with st.expander("Amend, close or reopen an account"):
        chosen = st.selectbox(
            "Account", ui.alphabetical(accounts["name"]), key="edit_account"
        )
        record = accounts[accounts["name"] == chosen].iloc[0]
        used = counts["account"].get(int(record["id"]), 0)
        st.caption(
            f"{used} transaction(s) reference this account. "
            + ("Closing keeps it available for the months it was used in."
               if used else "It has never been used, so it can be deleted outright.")
        )

        st.markdown("**Rename**")
        st.caption(
            "Applies to history as well as new entries. Transactions reference the account "
            "by id, not by name, so every existing one follows automatically — nothing is "
            "re-pointed and no history is rewritten."
        )
        with st.form("rename_account"):
            rename_cols = st.columns([2, 1, 1])
            new_name = rename_cols[0].text_input("New name", value=str(record["name"]))
            new_code = rename_cols[1].text_input(
                "Short code", value=str(record["short_code"])
            )
            rename_cols[2].write("")
            if rename_cols[2].form_submit_button("Rename", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.update_account(
                        session, int(record["id"]),
                        name=new_name, short_code=new_code,
                    )
                show_outcome(outcome)

        st.divider()

        left, right = st.columns(2)
        with left:
            st.markdown("**Close from a date**")
            closes = st.date_input(
                "Closed from", value=dt.date.today(), format="DD/MM/YYYY", key="close_acc"
            )
            if st.button("Close account", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.retire(
                        session, "account", int(record["id"]), closes
                    )
                show_outcome(outcome)

        with right:
            st.markdown("**Reopen or delete**")
            if st.button("Reopen", disabled=READ_ONLY or record["valid_to"] is None):
                with ui.session() as session, session.begin():
                    outcome = reference.reinstate(session, "account", int(record["id"]))
                show_outcome(outcome)
            if st.button(
                "Delete permanently", disabled=READ_ONLY or used > 0,
                help="Only possible for an account that has never been used",
            ):
                with ui.session() as session, session.begin():
                    outcome = reference.delete(session, "account", int(record["id"]))
                show_outcome(outcome)

# -------------------------------------------------------------------------- categories

with tab_categories:
    categories = data["categories"].copy()
    categories["status"] = categories.apply(status_of, axis=1)
    categories["used by"] = categories["id"].map(lambda i: counts["category"].get(i, 0))

    st.dataframe(
        repo.sort_human(
            categories[["grouping", "name", "spend_type", "status", "used by"]],
            by=["grouping", "name"],
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "name": "Category",
            "grouping": "Grouping",
            "spend_type": "Eligible transactions",
            "status": "Status",
            "used by": st.column_config.NumberColumn("Transactions", format="%d"),
        },
    )

    groupings = ui.alphabetical(categories["grouping"])

    with st.expander("Add a category"):
        with st.form("add_category"):
            row = st.columns(4)
            name = row[0].text_input("Name")
            grouping = row[1].selectbox("Grouping", groupings)
            spend_type = row[2].selectbox(
                "Eligible transactions", reference.SPEND_TYPES,
                help="Credit-only categories reject Debits, and vice versa",
            )
            available = row[3].date_input(
                "Available from", value=dt.date.today().replace(day=1),
                format="DD/MM/YYYY", key="cat_from",
            )
            if st.form_submit_button("Add category", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    _, outcome = reference.add_category(
                        session, name, grouping, spend_type, available
                    )
                show_outcome(outcome)

    with st.expander("Amend, close or reopen a category"):
        chosen = st.selectbox(
            "Category", ui.alphabetical(categories["name"]), key="edit_category"
        )
        record = categories[categories["name"] == chosen].iloc[0]
        used = counts["category"].get(int(record["id"]), 0)
        st.caption(f"{used} transaction(s) reference this category.")

        edit_col, close_col = st.columns(2)
        with edit_col:
            st.markdown("**Amend**")
            new_grouping = st.selectbox(
                "Grouping", groupings,
                index=groupings.index(record["grouping"])
                if record["grouping"] in groupings else 0,
                key="cat_grouping",
            )
            new_spend = st.selectbox(
                "Eligible transactions", reference.SPEND_TYPES,
                index=reference.SPEND_TYPES.index(record["spend_type"])
                if record["spend_type"] in reference.SPEND_TYPES else 0,
                key="cat_spend",
            )
            if st.button("Save changes", disabled=READ_ONLY, key="save_cat"):
                with ui.session() as session, session.begin():
                    outcome = reference.update_category(
                        session, int(record["id"]),
                        grouping=new_grouping, spend_type=new_spend,
                    )
                show_outcome(outcome)

        with close_col:
            st.markdown("**Close or reopen**")
            closes = st.date_input(
                "Closed from", value=dt.date.today(), format="DD/MM/YYYY", key="close_cat"
            )
            if st.button("Close category", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.retire(
                        session, "category", int(record["id"]), closes
                    )
                show_outcome(outcome)
            if st.button(
                "Reopen", disabled=READ_ONLY or record["valid_to"] is None, key="reopen_cat"
            ):
                with ui.session() as session, session.begin():
                    outcome = reference.reinstate(session, "category", int(record["id"]))
                show_outcome(outcome)

# --------------------------------------------------------------------- classifications

with tab_classes:
    classes = data["classifications"].copy()
    classes["status"] = classes.apply(status_of, axis=1)
    classes["used by"] = classes["id"].map(lambda i: counts["classification"].get(i, 0))

    st.caption(
        "Direction is the multiplier applied to a classification's totals — only Excess "
        "uses −1. Rollover controls whether a month's closing balance carries forward."
    )
    st.dataframe(
        repo.sort_human(
            classes[["name", "direction", "rollover", "status", "used by"]], by="name"
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "name": "Classification",
            "direction": st.column_config.NumberColumn("Direction", format="%d"),
            "rollover": "Rollover",
            "status": "Status",
            "used by": st.column_config.NumberColumn("Transactions", format="%d"),
        },
    )

    with st.expander("Add a classification"):
        with st.form("add_class"):
            row = st.columns(4)
            name = row[0].text_input("Name")
            direction = row[1].selectbox(
                "Direction", [1, -1],
                help="−1 inverts the sign of this classification's totals, as Excess does",
            )
            rollover = row[2].selectbox("Rollover", reference.ROLLOVERS)
            available = row[3].date_input(
                "Available from", value=dt.date.today().replace(day=1),
                format="DD/MM/YYYY", key="class_from",
            )
            spend = st.checkbox("Counts as spending", value=True)
            if st.form_submit_button(
                "Add classification", type="primary", disabled=READ_ONLY
            ):
                with ui.session() as session, session.begin():
                    _, outcome = reference.add_classification(
                        session, name, int(direction), rollover, available, spend
                    )
                show_outcome(outcome)

    with st.expander("Amend, close or reopen a classification"):
        chosen = st.selectbox(
            "Classification", ui.alphabetical(classes["name"]), key="edit_class"
        )
        record = classes[classes["name"] == chosen].iloc[0]
        used = counts["classification"].get(int(record["id"]), 0)
        st.caption(f"{used} transaction(s) reference this classification.")

        edit_col, close_col = st.columns(2)
        with edit_col:
            st.markdown("**Amend**")
            new_rollover = st.selectbox(
                "Rollover", reference.ROLLOVERS,
                index=reference.ROLLOVERS.index(record["rollover"])
                if record["rollover"] in reference.ROLLOVERS else 0,
                key="class_rollover",
            )
            new_direction = st.selectbox(
                "Direction", [1, -1],
                index=0 if int(record["direction"]) == 1 else 1,
                key="class_direction",
            )
            if st.button("Save changes", disabled=READ_ONLY, key="save_class"):
                with ui.session() as session, session.begin():
                    outcome = reference.update_classification(
                        session, int(record["id"]),
                        rollover=new_rollover, direction=int(new_direction),
                    )
                show_outcome(outcome)

        with close_col:
            st.markdown("**Close or reopen**")
            closes = st.date_input(
                "Closed from", value=dt.date.today(), format="DD/MM/YYYY", key="close_class"
            )
            if st.button("Close classification", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    outcome = reference.retire(
                        session, "classification", int(record["id"]), closes
                    )
                show_outcome(outcome)
            if st.button(
                "Reopen", disabled=READ_ONLY or record["valid_to"] is None, key="reopen_class"
            ):
                with ui.session() as session, session.begin():
                    outcome = reference.reinstate(
                        session, "classification", int(record["id"])
                    )
                show_outcome(outcome)

# ------------------------------------------------------------------------------ cycling

with tab_cycling:
    st.caption(
        "What a day's cycling saves, by kind. These were buried in a nested IF inside the "
        "Cycling tab — `IF(commute, 10.5, IF(band, 8.9, IF(gym, 4.6, 0)))` — so a fare rise "
        "meant either rewriting every historic row or letting old rows claim the new fare. "
        "Dating each rate settles both: history keeps the fare that applied on the day."
    )

    rates = data["cycling_rates"]
    if rates.empty:
        st.info("No rates set. Nothing ridden will score a saving until there are.")
    else:
        shown = rates.copy()
        shown["kind"] = shown["kind"].str.title()
        st.dataframe(
            ui.money_table(
                repo.sort_human(shown, by=["kind", "effective_from"]),
                ["amount"],
                labels={"kind": "Kind", "effective_from": "From", "amount": "Saving per day"},
            ),
            width="stretch",
            hide_index=True,
        )

    with st.form("cycling_rate"):
        fields = st.columns(3)
        kind = fields[0].selectbox(
            "Kind", ["commute", "band", "gym"], format_func=str.title,
            help="A day flagged more than once counts once, at the highest of these",
        )
        rate_from = fields[1].date_input(
            "From", value=dt.date(2026, 4, 1), format="DD/MM/YYYY", key="rate_from"
        )
        rate_amount = fields[2].number_input(
            "Saving per day (£)", min_value=0.0, step=0.10, format="%.2f"
        )
        if st.form_submit_button("Save rate", type="primary", disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                outcome = reference.set_cycling_rate(
                    session, kind, rate_from, Decimal(str(rate_amount))
                )
            show_outcome(outcome)

    if not rates.empty:
        options = list(rates.itertuples(index=False))
        chosen_rate = st.selectbox(
            "Remove a rate",
            options=range(len(options)),
            format_func=lambda i: (
                f"{options[i].kind.title()} from "
                f"{options[i].effective_from:%d %b %Y} — {ui.money(options[i].amount)}"
            ),
            key="remove_rate",
        )
        if st.button("Remove rate", disabled=READ_ONLY):
            row = options[chosen_rate]
            with ui.session() as session, session.begin():
                outcome = reference.remove_cycling_rate(
                    session, row.kind, row.effective_from
                )
            show_outcome(outcome)

# -------------------------------------------------------------------------------- cards

with tab_cards:
    st.caption(
        "Balance-transfer cards, tracked for payoff. These are separate from the credit "
        "cards transactions are recorded against, which is why they carry their own "
        "borrowed amount rather than deriving one from the ledger."
    )

    cards = data["cards"]
    if cards.empty:
        st.info("No cards yet.")
    else:
        shown = repo.sort_human(cards, by="name").copy()
        shown["min_pct"] = (shown["min_payment_pct"].astype(float) * 100).round(2)
        st.dataframe(
            ui.money_table(
                shown[["name", "credit_limit", "opening_balance", "opening_date",
                       "term_months", "payment_day", "min_pct"]],
                ["credit_limit", "opening_balance"],
                labels={
                    "name": "Card", "credit_limit": "Credit limit",
                    "opening_balance": "Amount borrowed", "opening_date": "Date of borrowing",
                    "term_months": "Term (months)", "payment_day": "Payment day",
                    "min_pct": "Minimum %",
                },
                formats={"min_pct": "{:,.2f}"},
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "The term runs from the date of borrowing, so a 13-month transfer taken out in "
            "June clears the following July. The workbook counted every card's term from "
            "April regardless of when it started."
        )

    with st.expander("Add a card"):
        with st.form("add_card"):
            row_one = st.columns(3)
            card_name = row_one[0].text_input("Name")
            card_limit = row_one[1].number_input(
                "Credit limit (£)", min_value=0.0, step=500.0, format="%.2f"
            )
            card_borrowed = row_one[2].number_input(
                "Amount borrowed (£)", min_value=0.0, step=500.0, format="%.2f"
            )

            row_two = st.columns(4)
            card_date = row_two[0].date_input(
                "Date of borrowing", value=dt.date.today().replace(day=1),
                format="DD/MM/YYYY", key="card_date",
            )
            card_term = row_two[1].number_input(
                "Term (months)", min_value=1, max_value=120, value=12, step=1, format="%d",
                help="The promotional period. The final instalment settles the balance.",
            )
            card_pay_day = row_two[2].number_input(
                "Payment day", min_value=1, max_value=31, value=1, step=1, format="%d"
            )
            card_min = row_two[3].number_input(
                "Minimum % ", min_value=0.0, max_value=100.0, value=2.5, step=0.25,
                format="%.2f", help="Of the outstanding balance, each month",
            )

            if st.form_submit_button("Add card", type="primary", disabled=READ_ONLY):
                with ui.session() as session, session.begin():
                    _, outcome = reference.add_card(
                        session,
                        name=card_name,
                        opening_balance=Decimal(str(card_borrowed)),
                        opening_date=card_date,
                        term_months=int(card_term),
                        # Stored as a fraction, which is what the amortisation expects and
                        # how the workbook's S column held it; shown as a percentage.
                        min_payment_pct=Decimal(str(card_min)) / 100,
                        payment_day=int(card_pay_day),
                        credit_limit=Decimal(str(card_limit)) or None,
                    )
                show_outcome(outcome)

    if not cards.empty:
        with st.expander("Amend or remove a card"):
            chosen_card = st.selectbox(
                "Card", ui.alphabetical(cards["name"]), key="edit_card"
            )
            card = cards[cards["name"] == chosen_card].iloc[0]

            with st.form("edit_card_form"):
                row_one = st.columns(3)
                new_name = row_one[0].text_input("Name", value=str(card["name"]))
                new_limit = row_one[1].number_input(
                    "Credit limit (£)", min_value=0.0, step=500.0, format="%.2f",
                    value=float(card["credit_limit"] or 0),
                )
                new_borrowed = row_one[2].number_input(
                    "Amount borrowed (£)", min_value=0.0, step=500.0, format="%.2f",
                    value=float(card["opening_balance"]),
                )

                row_two = st.columns(4)
                new_date = row_two[0].date_input(
                    "Date of borrowing", value=card["opening_date"], format="DD/MM/YYYY",
                    key="edit_card_date",
                )
                new_term = row_two[1].number_input(
                    "Term (months)", min_value=1, max_value=120, step=1, format="%d",
                    value=int(card["term_months"]),
                )
                new_pay_day = row_two[2].number_input(
                    "Payment day", min_value=1, max_value=31, step=1, format="%d",
                    value=int(card["payment_day"] or 1),
                )
                new_min = row_two[3].number_input(
                    "Minimum %", min_value=0.0, max_value=100.0, step=0.25, format="%.2f",
                    value=round(float(card["min_payment_pct"]) * 100, 2),
                )

                if st.form_submit_button("Save card", disabled=READ_ONLY):
                    with ui.session() as session, session.begin():
                        outcome = reference.update_card(
                            session, int(card["id"]),
                            name=new_name,
                            credit_limit=Decimal(str(new_limit)) or None,
                            opening_balance=Decimal(str(new_borrowed)),
                            opening_date=new_date,
                            term_months=int(new_term),
                            payment_day=int(new_pay_day),
                            min_payment_pct=Decimal(str(new_min)) / 100,
                        )
                    show_outcome(outcome)

            if st.button("Remove card", disabled=READ_ONLY, key="remove_card"):
                with ui.session() as session, session.begin():
                    outcome = reference.delete_card(session, int(card["id"]))
                show_outcome(outcome)
            st.caption(
                "Removing is a real delete: cards carry no transactions, so there is no "
                "history to orphan."
            )

# ----------------------------------------------------------------------- monthly figures

with tab_monthly:
    st.caption(
        "The two things that vary by month: the daily allowance that feeds a "
        "classification's running total, and the target spend for each category. Both were "
        "cells inside a month tab — C54 and column D — so a new month meant copying the "
        "template and hoping the right numbers came with it."
    )

    monthly_period = st.selectbox(
        "Month", data["all_periods"],
        index=data["all_periods"].index(data["periods"][-1]),
        format_func=repo.period_label, key="monthly_period",
    )

    allowances = data["allowances"]
    budgets = data["budgets"]
    classifications = data["classifications"]
    categories_all = data["categories"]

    # A month with nothing stored inherits the most recent month that has something, so a
    # new month opens with a sensible starting point rather than a blank sheet.
    def most_recent_with(frame: pd.DataFrame, before: str) -> str | None:
        if frame.empty:
            return None
        earlier = sorted({p for p in frame["period"] if p < before})
        return earlier[-1] if earlier else None

    allowance_here = allowances[allowances["period"] == monthly_period]
    allowance_source = monthly_period
    if allowance_here.empty:
        fallback = most_recent_with(allowances, monthly_period)
        if fallback:
            allowance_here = allowances[allowances["period"] == fallback]
            allowance_source = fallback

    st.subheader("Daily allowance")
    if allowance_source != monthly_period:
        st.caption(
            f"Nothing stored for {repo.period_label(monthly_period)} — showing "
            f"{repo.period_label(allowance_source)}'s figures as a starting point. Saving "
            "writes them to the month you have chosen."
        )

    allowance_rows = []
    for _, cls in classifications.iterrows():
        match = allowance_here[allowance_here["classification"] == cls["name"]]
        allowance_rows.append(
            {
                "id": int(cls["id"]),
                "classification": cls["name"],
                "daily": float(match["daily_amount"].iloc[0]) if not match.empty else 0.0,
            }
        )
    allowance_frame = repo.sort_human(pd.DataFrame(allowance_rows), by="classification")

    edited_allowances = st.data_editor(
        allowance_frame,
        width="stretch",
        hide_index=True,
        disabled=["id", "classification"] if not READ_ONLY else True,
        column_order=["classification", "daily"],
        column_config={
            "classification": "Classification",
            "daily": ui.editable_money(
                "Per day", "Added to the running total on every day of the month"
            ),
        },
        key=f"allowance_editor_{monthly_period}",
    )

    st.subheader("Target spend by category")
    budget_here = budgets[budgets["period"] == monthly_period]
    budget_source = monthly_period
    if budget_here.empty:
        fallback = most_recent_with(budgets, monthly_period)
        if fallback:
            budget_here = budgets[budgets["period"] == fallback]
            budget_source = fallback
    if budget_source != monthly_period:
        st.caption(
            f"Showing {repo.period_label(budget_source)}'s targets as a starting point."
        )

    targets_by_category = (
        budget_here.set_index("category")["expected"].to_dict()
        if not budget_here.empty
        else {}
    )
    budget_rows = [
        {
            "id": int(cat["id"]),
            "grouping": cat["grouping"],
            "category": cat["name"],
            "target": float(targets_by_category.get(cat["name"], 0) or 0),
        }
        for _, cat in categories_all.iterrows()
    ]
    budget_frame = repo.sort_human(
        pd.DataFrame(budget_rows), by=["grouping", "category"]
    )

    edited_budgets = st.data_editor(
        budget_frame,
        width="stretch",
        hide_index=True,
        disabled=["id", "grouping", "category"] if not READ_ONLY else True,
        column_order=["grouping", "category", "target"],
        column_config={
            "grouping": "Grouping",
            "category": "Category",
            "target": ui.editable_money("Target"),
        },
        key=f"budget_editor_{monthly_period}",
        height=420,
    )

    if st.button(
        f"Save {repo.period_label(monthly_period)}", type="primary", disabled=READ_ONLY
    ):
        with ui.session() as session, session.begin():
            for _, row in edited_allowances.iterrows():
                reference.set_allowance(
                    session, monthly_period, int(row["id"]),
                    Decimal(str(row["daily"] or 0)),
                )
            for _, row in edited_budgets.iterrows():
                reference.set_budget(
                    session, monthly_period, int(row["id"]),
                    Decimal(str(row["target"] or 0)),
                )
            outcome = reference.Outcome(
                True, f"Saved figures for {repo.period_label(monthly_period)}."
            )
        show_outcome(outcome)

# ------------------------------------------------------------------------------ salary

with tab_salary:
    st.caption(
        "What a payslip is made of, beyond the tax bands. These are policy rather than pay — "
        "how much of base goes into the pension, what the car allowance is worth — and they "
        "are kept deliberately apart from the thresholds and allowances on the **Salary → "
        "Tax bands** tab, so editing one cannot disturb the other."
    )

    params = repo.salary_parameters(data["settings"])

    with st.form("salary_parameters"):
        st.markdown("**Deductions and allowances**")
        row = st.columns(3)
        pension_rate = row[0].number_input(
            "Pension (% of base)", min_value=0.0, max_value=100.0, step=0.5,
            format="%.2f", value=float(params["pension_rate"]),
            help="Charged on base salary only — the car allowance is not pensionable.",
        )
        home_working = row[1].number_input(
            "Home working allowance (£/month)", min_value=0.0, step=1.0, format="%.2f",
            value=float(params["home_working_allowance"]),
            help="Paid on top and not taxable, so it passes straight through to net pay.",
        )
        holiday_pay = row[2].number_input(
            "Holiday pay (£/month)", min_value=0.0, step=1.0, format="%.2f",
            value=float(params["holiday_pay_monthly"]),
            help="Deducted before tax. A month with a recorded payslip uses that figure "
                 "instead of this one.",
        )

        st.markdown("**Car allowance**")
        st.caption(
            "A percentage of base salary up to a threshold and a lower one above it, so a "
            "base of £118,905 gives 12% of £50,000 plus 5% of £68,905 — £9,445.25 a year. "
            "Derived rather than stored, so a pay rise moves it automatically."
        )
        car = st.columns(3)
        car_threshold = car[0].number_input(
            "Threshold (£)", min_value=0.0, step=1000.0, format="%.2f",
            value=float(params["car_allowance_threshold"]),
        )
        car_lower = car[1].number_input(
            "Rate up to the threshold (%)", min_value=0.0, max_value=100.0, step=0.5,
            format="%.2f", value=float(params["car_allowance_lower_rate"]),
        )
        car_upper = car[2].number_input(
            "Rate above the threshold (%)", min_value=0.0, max_value=100.0, step=0.5,
            format="%.2f", value=float(params["car_allowance_upper_rate"]),
        )

        if st.form_submit_button("Save salary parameters", type="primary",
                                 disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                for key, value in (
                    ("pension_rate", pension_rate),
                    ("home_working_allowance", home_working),
                    ("holiday_pay_monthly", holiday_pay),
                    ("car_allowance_threshold", car_threshold),
                    ("car_allowance_lower_rate", car_lower),
                    ("car_allowance_upper_rate", car_upper),
                ):
                    outcome = reference.set_setting(session, key, value)
            show_outcome(outcome)

    # What the parameters currently produce, so a change can be checked before it matters.
    profiles = data["salary_profiles"]
    if not profiles.empty:
        base = repo.base_in_force(profiles, dt.date.today())
        if base is not None:
            allowance = repo.car_allowance(base, params)
            pension = base * params["pension_rate"] / repo.HUNDRED
            st.divider()
            st.markdown("**On the salary currently in force**")
            summary = pd.DataFrame(
                [
                    {"line": "Base salary", "annual": base, "monthly": base / 12},
                    {"line": "Car allowance", "annual": allowance,
                     "monthly": allowance / 12},
                    {"line": "Home working allowance",
                     "annual": params["home_working_allowance"] * 12,
                     "monthly": params["home_working_allowance"]},
                    {"line": "Pension", "annual": -pension, "monthly": -pension / 12},
                    {"line": "Holiday pay",
                     "annual": -params["holiday_pay_monthly"] * 12,
                     "monthly": -params["holiday_pay_monthly"]},
                ]
            )
            st.dataframe(
                ui.money_table(
                    summary, ["annual", "monthly"],
                    labels={"line": "", "annual": "Annual", "monthly": "Monthly"},
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "Deductions are shown negative. NI and PAYE are charged on base plus car "
                "allowance less pension and holiday pay — the home working allowance is "
                "outside the tax calculation entirely."
            )

# ---------------------------------------------------------------------- savings targets

with tab_savings:
    st.caption(
        "How much should go into each pot every month. These drive the 'Required' columns on "
        "the Savings and investments page, which accumulate: a month that falls short raises "
        "the bar for the next one."
    )

    plan = data["savings_plan"]
    pots = data["accounts"][
        data["accounts"]["is_savings"].astype(bool)
        | data["accounts"]["is_investment"].astype(bool)
    ]
    pots = repo.sort_human(pots, by="name")

    # ---- the plan, per account, effective-dated ------------------------------------
    st.markdown("**The plan**")
    st.caption(
        "Per account and effective-dated, which is how the interest tracker held it — one "
        "column per revision, with the date above. Saving against a date that has no set yet "
        "creates one and leaves the earlier figures intact for the months they applied to. "
        "A pot being wound down needs an explicit **0**, not a blank: a blank carries the "
        "previous figure forward."
    )

    stored_dates = repo.plan_dates(plan)
    # The set in force *now*, not the last one stored. The plan runs ahead of itself -- there
    # is a revision dated May 2027 -- and opening on that would show a future set as though
    # it were current.
    started = [d for d in stored_dates if d <= dt.date.today()]
    current = (
        started[-1] if started
        else (stored_dates[0] if stored_dates else dt.date.today().replace(day=1))
    )

    # Every revision is kept, so an earlier plan can be read back rather than reconstructed.
    # The dropdown is the stored ones; the date picker beside it is for starting a new set,
    # which is the only case where a date that is not already in the list makes sense.
    NEW = "Start a new revision…"
    date_cols = st.columns([2, 1, 2])
    choice = date_cols[0].selectbox(
        "Revision",
        options=[NEW] + list(reversed(stored_dates)),
        index=(list(reversed(stored_dates)).index(current) + 1) if stored_dates else 0,
        format_func=lambda d: (
            d if isinstance(d, str)
            else f"{d:%d %b %Y}"
            + ("  ← in force" if d == current else "")
            + ("  (future)" if d > dt.date.today() else "")
        ),
        key="plan_revision",
    )
    if choice == NEW:
        plan_from = date_cols[1].date_input(
            "From", value=dt.date.today().replace(day=1), format="DD/MM/YYYY",
            key="plan_effective_new",
        )
    else:
        plan_from = choice
        date_cols[1].caption("")
        date_cols[1].caption(f"Editing {plan_from:%d %b %Y}")

    date_cols[2].caption("")
    date_cols[2].caption(
        f"{len(stored_dates)} revision(s) stored: "
        + (", ".join(f"{d:%d/%m/%Y}" for d in stored_dates) or "none")
        + ". Earlier ones stay as they are — a past month keeps the plan it was measured "
        "against."
    )

    # An empty plan is either a genuinely empty table or a dashboard reading somewhere other
    # than you think, and those look identical on screen. Rather than guess, say which file
    # was opened and what is in it -- counted here and now, not from the cached read, so a
    # disagreement between the two is itself the answer.
    if not stored_dates:
        st.warning(
            "**No savings plan was loaded.** If you are expecting targets here, the figures "
            "below say which database was read and what it contains."
        )
        import sqlite3

        from budget import config

        def count(table: str) -> str:
            try:
                conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
                try:
                    return f"{conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:,}"
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                return f"unreadable ({type(exc).__name__})"

        import hashlib
        import platform

        def identity() -> str:
            """The file's own fingerprint, so this can be compared against diagnose.bat --
            two processes disagreeing about one path is settled by the bytes, not by
            argument."""
            try:
                raw = config.DB_PATH.read_bytes()
                return hashlib.sha256(raw).hexdigest()[:32]
            except OSError as exc:
                return f"unreadable ({type(exc).__name__})"

        st.code(
            "\n".join(
                [
                    f"machine           {platform.node()}",
                    f"database          {config.DB_PATH}",
                    f"exists            {config.DB_PATH.exists()}",
                    f"BUDGET_DB_PATH    {os.environ.get('BUDGET_DB_PATH', '(not set)')}",
                    f"sha256            {identity()}",
                    f"savings_plan      {count('savings_plan')} row(s) in the file",
                    f"loaded into page  {len(plan):,} row(s)",
                    f"transactions      {count('txn')} row(s) in the file",
                    f"fingerprint       {ui.db_fingerprint()}",
                ]
            ),
            language="text",
        )
        st.caption(
            "If **savings_plan** has rows in the file but none loaded into the page, the "
            "read is stale — use **Refresh data** in the sidebar. If the file itself has "
            "none, the plan needs entering. If the path is not the one you expect, something "
            "is overriding it."
        )

    in_force = repo.plan_in_force(plan, plan_from).set_index("account")
    plan_rows = [
        {
            "id": int(row["id"]),
            "account": row["name"],
            "kind": "Investments" if row["is_investment"] else "Savings",
            "amount": (
                float(in_force.loc[row["name"], "amount"])
                if row["name"] in in_force.index
                else 0.0
            ),
        }
        for _, row in pots.iterrows()
    ]

    edited_plan = st.data_editor(
        pd.DataFrame(plan_rows),
        width="stretch",
        hide_index=True,
        disabled=["id", "account", "kind"] if not READ_ONLY else True,
        column_order=["account", "kind", "amount"],
        column_config={
            "account": "Account",
            "kind": "Counts towards",
            "amount": ui.editable_money("Monthly target"),
        },
        key=f"savings_plan_editor_{plan_from}",
    )

    totals = edited_plan.groupby("kind")["amount"].sum().to_dict()
    st.caption(
        f"That set totals **{ui.money(totals.get('Savings', 0))}** into savings and "
        f"**{ui.money(totals.get('Investments', 0))}** into investments each month — "
        f"{ui.money(sum(totals.values()))} altogether. The overview below is this sum, not a "
        "second figure typed beside it. A **negative** target is allowed and means what it "
        "says: money leaving that pot, usually because it is going into another one, where "
        "the matching positive target sits."
    )

    buttons = st.columns([1, 1, 3])
    if buttons[0].button("Save the plan", type="primary", disabled=READ_ONLY,
                         key="save_plan"):
        with ui.session() as session, session.begin():
            for _, row in edited_plan.iterrows():
                reference.set_savings_plan(
                    session, int(row["id"]), plan_from,
                    Decimal(str(row["amount"] or 0)),
                )
            outcome = reference.Outcome(
                True, f"Plan saved, in force from {plan_from:%d %b %Y}."
            )
        show_outcome(outcome)

    if buttons[1].button(
        "Remove this revision",
        disabled=READ_ONLY or plan_from not in stored_dates,
        key="drop_plan",
        help="Deletes every target dated from this day, so the previous set applies again.",
    ):
        with ui.session() as session, session.begin():
            outcome = reference.remove_savings_plan_date(session, plan_from)
        show_outcome(outcome)

    # ---- one-offs ------------------------------------------------------------------
    st.markdown("**One-off additions and withdrawals**")
    st.caption(
        "A lump sum planned into or out of a pot in a single month, and not again — a "
        "windfall going in, or a deposit coming out. Folding one into the plan above would "
        "misstate every month after it, and leaving it out means the target is knowingly "
        "wrong for the month it lands in. These are **netted into** the monthly target for "
        "their month. Negative means money coming out."
    )

    adjustments = data["savings_adjustments"]
    if adjustments.empty:
        st.caption("None recorded.")
    else:
        shown = adjustments.copy()
        shown["month"] = shown["period"].map(repo.period_label)
        st.dataframe(
            ui.money_table(
                repo.sort_human(shown, by=["period", "account"])[
                    ["month", "account", "amount", "note"]
                ],
                ["amount"],
                labels={"month": "Month", "account": "Account", "amount": "Amount",
                        "note": "Note"},
            ),
            width="stretch",
            hide_index=True,
        )

    with st.form("savings_adjustment"):
        fields = st.columns([1, 1, 1, 2])
        one_off_period = fields[0].selectbox(
            "Month", data["all_periods"], index=len(data["periods"]) - 1,
            format_func=repo.period_label, key="one_off_period",
        )
        one_off_account = fields[1].selectbox(
            "Account", ui.alphabetical(pots["name"]), key="one_off_account",
        )
        one_off_amount = fields[2].number_input(
            "Amount (£)", step=100.0, format="%.2f", value=0.0,
            help="Negative for money coming out of the pot.",
        )
        one_off_note = fields[3].text_input(
            "Note", placeholder="e.g. deposit for the flat", key="one_off_note"
        )
        if st.form_submit_button("Add one-off", disabled=READ_ONLY):
            account_id = int(pots.loc[pots["name"] == one_off_account, "id"].iloc[0])
            with ui.session() as session, session.begin():
                _, outcome = reference.add_savings_adjustment(
                    session, one_off_period, account_id,
                    Decimal(str(one_off_amount)), one_off_note or None,
                )
            show_outcome(outcome)

    if not adjustments.empty:
        options = list(adjustments["id"])
        indexed = adjustments.set_index("id")
        drop = st.selectbox(
            "Remove a one-off",
            options=options,
            format_func=lambda i: (
                f"{repo.period_label(indexed.loc[i, 'period'])} · "
                f"{indexed.loc[i, 'account']} · {ui.money(indexed.loc[i, 'amount'])}"
                + (f" · {indexed.loc[i, 'note']}" if indexed.loc[i, "note"] else "")
            ),
            key="drop_one_off",
        )
        if st.button("Remove one-off", disabled=READ_ONLY, key="do_drop_one_off"):
            with ui.session() as session, session.begin():
                outcome = reference.remove_savings_adjustment(session, int(drop))
            show_outcome(outcome)

    # ---- the expected return -------------------------------------------------------
    st.markdown("**Expected investment return**")
    with st.form("investment_return"):
        rate_cols = st.columns([1, 3])
        annual = rate_cols[0].number_input(
            "Annual (%)",
            value=float(repo.investment_return_rate(data["settings"]) * 100),
            min_value=0.0, max_value=100.0, step=0.25, format="%.2f",
        )
        monthly = repo.monthly_rate(Decimal(str(annual)) / 100)
        rate_cols[1].caption("")
        rate_cols[1].caption(
            f"Compounds to **{ui.percent(monthly * 100, 4)}%** a month — the plan's "
            "`=(1+L4)^(1/12)-1`, not the annual figure over twelve, which would overstate "
            "the month and compound past the rate it started from."
        )
        if st.form_submit_button("Save the return", disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                outcome = reference.set_setting(
                    session, repo.INVESTMENT_RETURN_KEY, str(annual)
                )
            show_outcome(outcome)

    # ---- the derived overview ------------------------------------------------------
    with st.expander("The resulting monthly overview"):
        st.caption(
            "Derived from the plan above, not entered. The two used to be typed separately, "
            "which is how the dashboard came to hold 900 and 350 while the plan behind them "
            "said 250 + 350 + 300 and 250 + 100 — the same figures, but only because nobody "
            "had yet changed one without the other."
        )
        st.dataframe(
            ui.money_table(
                data["savings_targets"].assign(
                    month=data["savings_targets"]["period"].map(repo.period_label)
                )[["month", "savings", "investments"]],
                ["savings", "investments"],
                labels={"month": "Month", "savings": "Savings target",
                        "investments": "Investments target"},
            ),
            width="stretch",
            hide_index=True,
        )

    st.divider()
    st.subheader("Earmarked savings")
    st.caption(
        "Pots that are nominally saved but already spoken for. They are excluded from the "
        "'available' column so the figure means money that is genuinely spare. The workbook "
        "subtracted a hand-typed total under the heading 'Less SC & Wed' — which had "
        "silently come to include Tembo as well by June, without the label changing."
    )

    excluded_frame = data["accounts"][data["accounts"]["is_savings"].astype(bool)].copy()
    excluded_frame["excluded"] = (
        excluded_frame["exclude_from_savings"].fillna(False).astype(bool)
    )
    edited_excluded = st.data_editor(
        repo.sort_human(excluded_frame[["id", "name", "excluded"]], by="name"),
        width="stretch",
        hide_index=True,
        disabled=["id", "name"] if not READ_ONLY else True,
        column_order=["name", "excluded"],
        column_config={
            "name": "Savings account",
            "excluded": st.column_config.CheckboxColumn("Earmarked"),
        },
        key="excluded_editor",
    )
    if st.button("Save earmarking", disabled=READ_ONLY, key="save_excluded"):
        with ui.session() as session, session.begin():
            for _, row in edited_excluded.iterrows():
                outcome = reference.update_account(
                    session, int(row["id"]), exclude_from_savings=bool(row["excluded"])
                )
        show_outcome(outcome)

# ----------------------------------------------------------------------------- general

with tab_general:
    st.caption("Replaces the Control tab. DeveloperParameters is gone — it only ever "
               "described the spreadsheet's own geometry.")
    settings = data["settings"]

    with st.form("general"):
        row = st.columns(4)
        tax_year = row[0].number_input(
            "Tax year", min_value=2000, max_value=2100,
            value=int(settings.get("tax_year", dt.date.today().year)),
            help="The April in which the financial year starts",
        )
        user = row[1].text_input("User", value=settings.get("user", ""))
        month_start = row[2].number_input(
            "Month start day", min_value=1, max_value=28,
            value=int(settings.get("month_start_day", 1)),
        )
        # Stored as the workbook's fraction (1 = 100%) so any formula carried over still
        # matches; shown as a percentage because that is how it reads, and to two decimal
        # places because a retention of 87.5% is as plausible as one of 100%.
        # The unit lives in the label: st.number_input rejects '%%' in a format string.
        excess_pct = row[3].number_input(
            "Excess retention (%)", min_value=0.0, max_value=100.0, step=0.25,
            format="%.2f",
            value=round(float(settings.get("excess_retention", 1)) * 100, 2),
            help="Share of a month's excess carried forward. Stored as a fraction, so "
                 "100% is saved as 1.",
        )

        # Month dropdowns run from the first month anything is recorded against to the
        # current one. This is how far past it they also offer, for the figures that are set
        # before the month arrives -- next month's savings target, a projection.
        look_forward = st.number_input(
            "Look forward (months)", min_value=0, max_value=36, step=1, format="%d",
            value=int(settings.get("look_forward_months", 12)),
            help="How many months beyond the current one the month lists and the Trends "
                 "projection offer.",
        )

        if st.form_submit_button("Save", type="primary", disabled=READ_ONLY):
            with ui.session() as session, session.begin():
                for key, value in (
                    ("tax_year", tax_year), ("user", user),
                    ("month_start_day", month_start),
                    ("excess_retention", excess_pct / 100),
                    ("look_forward_months", int(look_forward)),
                ):
                    outcome = reference.set_setting(session, key, value)
            show_outcome(outcome)

    st.divider()

    # ---- credit card billing days ---------------------------------------------------
    st.subheader("Credit card billing")
    st.caption(
        "The day the statement is issued and the day the direct debit is taken. The "
        "workbook held these in every month tab even though they never varied by month, so "
        "changing one meant editing twelve. They drive the 'outstanding' figure on the "
        "Month page, which is the spending not yet on a statement."
    )

    card_accounts = data["accounts"][data["accounts"]["type"] == "credit_card"].copy()
    if card_accounts.empty:
        st.info("No credit card accounts.")
    else:
        billing = repo.sort_human(
            card_accounts[["id", "name", "statement_day", "payment_day"]], by="name"
        ).copy()
        for column in ("statement_day", "payment_day"):
            billing[column] = billing[column].astype("Int64")

        edited_billing = st.data_editor(
            billing,
            width="stretch",
            hide_index=True,
            disabled=["id", "name"] if not READ_ONLY else True,
            column_order=["name", "statement_day", "payment_day"],
            column_config={
                "name": "Card",
                "statement_day": st.column_config.NumberColumn(
                    "Statement day", min_value=1, max_value=31, step=1, format="%d"
                ),
                "payment_day": st.column_config.NumberColumn(
                    "Payment day", min_value=1, max_value=31, step=1, format="%d"
                ),
            },
            key="billing_editor",
        )
        if st.button("Save billing days", disabled=READ_ONLY, key="save_billing"):
            with ui.session() as session, session.begin():
                for _, row in edited_billing.iterrows():
                    outcome = reference.update_account(
                        session, int(row["id"]),
                        statement_day=(
                            int(row["statement_day"])
                            if pd.notna(row["statement_day"]) else None
                        ),
                        payment_day=(
                            int(row["payment_day"])
                            if pd.notna(row["payment_day"]) else None
                        ),
                    )
            show_outcome(outcome)

    st.divider()

    # ---- per-account monthly targets ------------------------------------------------
    st.subheader("Account targets")
    st.caption(
        "How much an account should hold in a month, shown against its balance on the "
        "Summary page. The workbook held one set of these for whichever month it happened "
        "to be showing; here each month keeps its own."
    )

    target_period = st.selectbox(
        "Month", data["all_periods"],
        index=data["all_periods"].index(data["periods"][-1]),
        format_func=repo.period_label, key="account_target_period",
    )
    stored_targets = data["account_targets"]
    here = stored_targets[stored_targets["period"] == target_period]
    by_account_id = dict(zip(here["account_id"], here["amount"])) if not here.empty else {}

    bank_accounts = data["accounts"][data["accounts"]["type"] == "bank"]
    target_frame = repo.sort_human(
        pd.DataFrame(
            [
                {
                    "id": int(row["id"]),
                    "account": row["name"],
                    "target": float(by_account_id.get(int(row["id"]), 0) or 0),
                }
                for _, row in bank_accounts.iterrows()
            ]
        ),
        by="account",
    )

    edited_account_targets = st.data_editor(
        target_frame,
        width="stretch",
        hide_index=True,
        disabled=["id", "account"] if not READ_ONLY else True,
        column_order=["account", "target"],
        column_config={"account": "Account", "target": ui.editable_money("Target")},
        key=f"account_target_editor_{target_period}",
        height=420,
    )

    if st.button(
        "Save account targets", disabled=READ_ONLY, key="save_account_targets"
    ):
        with ui.session() as session, session.begin():
            for _, row in edited_account_targets.iterrows():
                amount = Decimal(str(row["target"] or 0))
                # Zero means 'no target' rather than 'a target of nothing', so the row is
                # dropped instead of stored -- otherwise every account would appear on the
                # Summary table whether or not it was being tracked.
                reference.set_account_target(
                    session, target_period, int(row["id"]),
                    amount if amount else None,
                )
            outcome = reference.Outcome(
                True, f"Targets for {repo.period_label(target_period)} saved."
            )
        show_outcome(outcome)

st.divider()
st.caption(
    "Adding or closing anything here never rewrites a month. The workbook's equivalent "
    "forms each warned that they would overwrite every month from the chosen one onwards, "
    "because an account's position in the sheet *was* its storage location."
)
