"""Import -- replaces the BulkImport tab and the bulk_upload macro.

Three differences from the macro. It validates everything before writing anything, so a bad
row does not leave half a batch imported. It records the batch, so an import can be undone
in one click. And it derives the period from each date rather than taking a Month column.

The balance check below reproduces BulkImport!CK3:CQ32.
"""

from __future__ import annotations

import hashlib
import io

import pandas as pd
import streamlit as st

from budget import importer, repo, service, ui
from budget.validation import validate

data = ui.page_header(
    "Import", "Paste or upload many transactions, check them, then commit as one batch."
)

accounts = ui.alphabetical(data["accounts"]["name"])
categories = ui.alphabetical(data["categories"]["name"])
classifications = ui.alphabetical(data["classifications"]["name"])
current_period = data["periods"][-1]

tab_paste, tab_upload = st.tabs(["Paste / edit", "Upload CSV"])
frame: pd.DataFrame | None = None
source_name = "pasted"

with tab_paste:
    st.caption(
        "Pick from the lists rather than typing — the same options as the Add transaction "
        "page, filtered to what is currently configured."
    )
    # Rows are seeded from session state rather than always starting empty, so a block of
    # blanks can be added in one go. The key carries a version: st.data_editor keys its
    # edits by row position, so reseeding under the same key would replay them onto the new
    # frame. Bumping the key retires that state, and nothing is lost because the seed *is*
    # what was on screen a moment ago.
    seeded = st.session_state.get("import_seed")
    if seeded is None:
        seeded = importer.template()
    grid_version = st.session_state.get("import_grid_version", 0)

    edited = st.data_editor(
        seeded,
        num_rows="dynamic",
        width="stretch",
        key=f"import_grid_{grid_version}",
        column_config={
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Type": st.column_config.SelectboxColumn(
                "Type", options=["Debit", "Credit", "Transfer"], required=False
            ),
            "Amount": st.column_config.NumberColumn(
                "Amount", min_value=0.0, step=0.01, format=ui.MONEY_FORMAT,
                help="Positive; direction comes from Type",
            ),
            "Account From": st.column_config.SelectboxColumn("Account From", options=accounts),
            "Account To": st.column_config.SelectboxColumn(
                "Account To", options=accounts, help="Transfers only"
            ),
            "Category": st.column_config.SelectboxColumn("Category", options=categories),
            "Purchase type": st.column_config.SelectboxColumn(
                "Purchase type", options=classifications
            ),
            "Comment": st.column_config.TextColumn("Comment"),
            "Category comment": st.column_config.TextColumn("Category comment"),
            "Donation": st.column_config.CheckboxColumn(
                "Donation",
                help="Charitable giving, counted under Savings and investments. A "
                     "transaction fee is its own row with this left clear.",
            ),
        },
    )
    add_count, add_button, clear_button = st.columns([1, 1, 3])
    how_many = add_count.number_input(
        "Rows to add", min_value=1, max_value=200, value=10, step=5,
        key="import_row_count", label_visibility="collapsed",
    )
    if add_button.button("Add blank rows", width="stretch"):
        # Seeded from `edited`, not from the previous seed, so anything typed survives.
        st.session_state["import_seed"] = importer.with_blank_rows(edited, int(how_many))
        st.session_state["import_grid_version"] = grid_version + 1
        st.rerun()
    if clear_button.button("Clear the table", disabled=seeded.empty and edited.empty):
        st.session_state["import_seed"] = importer.template()
        st.session_state["import_grid_version"] = grid_version + 1
        st.rerun()

    if edited is not None and not edited.dropna(how="all").empty:
        frame = edited.dropna(how="all")

with tab_upload:
    st.caption(
        "Column names are matched loosely, so a block copied out of the old BulkImport tab "
        "works unchanged. A Month column is accepted but ignored."
    )
    uploaded = st.file_uploader("CSV file", type=["csv"])
    if uploaded is not None:
        frame = pd.read_csv(io.BytesIO(uploaded.getvalue()))
        source_name = uploaded.name
        st.caption(f"{len(frame)} row(s) read from {uploaded.name}")

        # Editable, like the paste tab. An import is all-or-nothing, so one bad row stops the
        # other forty-seven -- and a read-only upload meant the only way to fix that row was
        # to leave, edit the file elsewhere and upload it again. The rejection message said
        # "nothing will be written until fixed" while offering nothing to fix it with.
        frame = st.data_editor(
            frame, num_rows="dynamic", width="stretch", key="uploaded_rows",
        )

if frame is None or frame.empty:
    st.info("Nothing to import yet.")
    candidates = []
else:
    candidates, problems = importer.parse(frame)
    for p in problems:
        st.warning(p)
    if not candidates:
        st.error("No usable rows found.")
        st.stop()

# ------------------------------------------------------------------------------ preview

if candidates:
    with ui.session() as session:
        reference = service.load_reference(session)

    results = [(c, validate(c, reference)) for c in candidates]
    bad = [(c, r) for c, r in results if not r.ok]
    warned = [(c, r) for c, r in results if r.ok and r.warnings]

    preview = importer.to_frame(candidates)
    preview.insert(
        1,
        "Status",
        ["✗ error" if not r.ok else ("! check" if r.warnings else "✓ ok") for _, r in results],
    )
    # Same convention as the Transactions page: a transfer has no category or purchase
    # type, so the blank is named rather than left to render as 'nan'.
    preview = ui.name_blanks(
        preview,
        ["Account To", "Category", "Purchase type", "Comment", "Category comment"],
        transfers="Type",
    )
    st.dataframe(
        ui.money_table(preview, ["Amount"]), width="stretch", hide_index=True
    )

    if bad:
        st.error(
            f"{len(bad)} row(s) cannot be imported. Nothing will be written until fixed — "
            "correct them in the table above, which is editable."
        )
        for c, r in bad:
            st.markdown(f"**Row {c.source_row}** — " + "; ".join(r.errors))
    elif warned:
        with st.expander(f"{len(warned)} row(s) worth a look (not blocking)"):
            for c, r in warned:
                st.markdown(f"**Row {c.source_row}** — " + "; ".join(r.warnings))

    st.divider()

    # ------------------------------------------------------------- balance verification

    st.subheader("Balance check")
    st.caption(
        "The old BulkImport!CK3:CQ32 check. Enter each account's real balance and confirm "
        "the import lands on it. Leave a target blank to skip that account. Credit-card "
        "balances are debt owed, so spending increases them."
    )

    verification = repo.import_verification(
        candidates, data["postings"], data["openings"], data["accounts"], current_period
    )
    # Alphabetical, like every other list of accounts in the app. It used to float the
    # affected accounts to the top, which put the table in an order that changed with the
    # import and made a named account hard to find -- and the checkbox below already
    # isolates the affected ones for anyone who wants only those.
    verification = repo.sort_human(verification, by="account").reset_index(drop=True)

    filter_col, clear_col = st.columns([3, 1])
    only_affected = filter_col.checkbox(
        "Show only accounts this import touches",
        value=False,
        help="Checking every account is what catches a transaction you left out entirely.",
    )

    # Targets are held per account in session state rather than in the editor's own widget
    # state. st.data_editor keys its edits by row position, so filtering the table would
    # otherwise either discard what was typed or reapply it to the wrong rows.
    targets: dict[str, float] = st.session_state.setdefault("balance_targets", {})

    if clear_col.button("Clear targets", disabled=not targets):
        targets.clear()
        st.rerun()

    shown = verification[verification["affected"]] if only_affected else verification

    editable = ui.to_float(shown, ["current", "in", "out", "projected"]).copy()
    editable["Target"] = pd.Series(
        importer.seed_targets(editable["account"], targets),
        dtype="Float64",  # nullable: an all-None object column is rejected by NumberColumn
        index=editable.index,
    )

    # A key tied to the exact rows on screen, so changing the filter builds a fresh editor
    # instead of replaying positional edits onto a different set of accounts.
    signature = hashlib.md5("|".join(editable["account"]).encode()).hexdigest()[:8]

    checked = st.data_editor(
        editable[["account", "current", "in", "out", "projected", "Target"]],
        width="stretch",
        hide_index=True,
        key=f"balance_check_{signature}",
        disabled=["account", "current", "in", "out", "projected"],
        column_config={
            "account": st.column_config.TextColumn("Account"),
            "current": st.column_config.NumberColumn("Current", format=ui.MONEY_FORMAT),
            "in": st.column_config.NumberColumn("In", format=ui.MONEY_FORMAT),
            "out": st.column_config.NumberColumn("Out", format=ui.MONEY_FORMAT),
            "projected": st.column_config.NumberColumn("Projected", format=ui.MONEY_FORMAT),
            "Target": st.column_config.NumberColumn(
                "Target", format=ui.PLAIN_MONEY_FORMAT,
                help="The account's real balance right now",
            ),
        },
    )

    importer.capture_targets(zip(checked["account"], checked["Target"]), targets)

    # Evaluated against every stored target, not just the visible rows -- otherwise
    # narrowing the filter could hide a mismatch rather than merely hide a row.
    full = ui.to_float(verification, ["current", "in", "out", "projected"]).copy()
    full["Target"] = pd.Series(
        [targets.get(a, pd.NA) for a in full["account"]],
        dtype="Float64",
        index=full.index,
    )
    entered = full[full["Target"].notna()].copy()

    if entered.empty:
        st.info("No targets entered — the balance check is optional but worth doing.")
        mismatches = pd.DataFrame()
    else:
        entered["difference"] = (
            entered["Target"].astype(float) - entered["projected"].astype(float)
        ).round(2)
        mismatches = entered[entered["difference"].abs() >= 0.01]

        cols = st.columns(3)
        cols[0].metric("Checked", f"{len(entered)} of {len(verification)}")
        cols[1].metric("Matching", f"{len(entered) - len(mismatches)}")
        cols[2].metric("Mismatched", f"{len(mismatches)}")
        if only_affected and len(entered) > len(shown):
            st.caption(
                f"Includes {len(entered) - len(entered[entered['affected']])} target(s) on "
                "accounts hidden by the filter."
            )

        if mismatches.empty:
            st.success("Every entered target matches the projected balance.")
        else:
            st.error(
                f"{len(mismatches)} account(s) do not match. A difference usually means a "
                "transaction is missing, duplicated, or has the wrong amount or direction."
            )
            st.dataframe(
                ui.money_table(
                    mismatches[["account", "projected", "Target", "difference"]],
                    ["projected", "Target", "difference"],
                    labels={"account": "Account", "projected": "Projected"},
                ),
                width="stretch",
                hide_index=True,
            )

    st.divider()

    # ------------------------------------------------------------------------- commit

    if bad:
        st.button("Import", disabled=True, help="Fix the errors above first")
    else:
        note = st.text_input("Note for this batch (optional)")
        if not mismatches.empty:
            st.warning(
                "The balance check has mismatches. You can still import, but it is worth "
                "resolving them first."
            )
        if st.button(f"Import {len(candidates)} transaction(s)", type="primary"):
            with ui.session() as session, session.begin():
                outcome = service.import_candidates(
                    session, candidates, filename=source_name, note=note or None
                )
            ui.load_all.clear()
            st.success(
                f"Imported {outcome.created} transaction(s) as batch #{outcome.batch_id}. "
                "It can be undone below."
            )
            ui.auto_push("the import")

st.divider()

# ------------------------------------------------------------------------ recent batches

st.subheader("Recent imports")
st.caption("Undoing a batch soft-deletes its transactions; nothing is erased.")

with ui.session() as session:
    batches = service.recent_batches(session)
    rows = [
        {
            "id": b.id,
            "when": b.created_at,
            "file": b.filename,
            "rows": b.row_count,
            "note": b.note,
        }
        for b in batches
    ]

if not rows:
    st.info("No imports yet.")
else:
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "id": "Batch",
            "when": st.column_config.DatetimeColumn("When", format="DD MMM YYYY HH:mm"),
            "file": "Source",
            "rows": "Rows",
            "note": "Note",
        },
    )

    # The migration batch holds the entire imported history. Undoing it would soft-delete
    # every transaction in one click -- recoverable, but not something to leave one
    # mis-click away.
    undoable = [r for r in rows if not str(r["note"] or "").startswith(service.MIGRATION_NOTE)]

    if not undoable:
        st.info(
            "Only the initial migration is present, which is protected from undo. "
            "Rebuild it with `python -m budget.migrate_xlsm --force` instead."
        )
    else:
        undo_left, undo_right = st.columns([1, 2])
        target = undo_left.selectbox(
            "Batch to undo", [r["id"] for r in undoable], format_func=lambda i: f"#{i}"
        )
        confirmed = undo_right.checkbox(
            f"Yes, undo batch #{target}", help="Soft-deletes every transaction in the batch"
        )
        if st.button("Undo this batch", type="secondary", disabled=not confirmed):
            with ui.session() as session, session.begin():
                affected = service.undo_batch(session, target)
            ui.load_all.clear()
            if affected:
                st.success(
                    f"Soft-deleted {affected} transaction(s) from batch #{target}. "
                    "They can be restored individually on the Transactions page."
                )
                ui.auto_push("the undo")
            else:
                st.info(f"Batch #{target} has nothing left to undo.")
