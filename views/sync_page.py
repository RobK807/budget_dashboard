"""Sync -- push and pull the master on the NAS, and work offline deliberately.

See DESIGN.md 6.3. The short version: the database always lives on a local disk, the NAS
holds pushed copies, and a revision counter turns a lost lock into a refusal rather than
silent data loss.
"""

from __future__ import annotations

import datetime as dt

import streamlit as st

from budget import config, sync, ui

data = ui.page_header("Sync", "Keep the laptop and the desktop on the same database.")

with ui.session() as session:
    state = sync.status(session)

# --------------------------------------------------------------------------- position

cols = st.columns(4)
cols[0].metric("This machine", sync.machine_name())
cols[1].metric("Local revision", state.local.revision)
cols[2].metric(
    "Master revision", state.nas.revision if state.nas.has_master else "—",
    help="Read from budget.meta.json, not by opening the database over SMB",
)
cols[3].metric("Unpushed", state.local.pending)

if state.tone == "error":
    st.error(f"**{state.label}**")
elif state.tone == "warning":
    st.warning(f"**{state.label}**")
else:
    st.success(f"**{state.label}**")

if not state.nas.reachable:
    st.caption(
        f"`{config.NAS_DIR}` is not reachable. That is normal away from home — changes stay "
        "pending and the next push will pick them up."
    )

if state.stale_code:
    st.caption(
        f"The master was pushed from a machine running newer code — it needs schema version "
        f"{state.nas.schema_version}, and this copy understands {sync.SCHEMA_VERSION}. "
        "Both directions are refused: pulling would hand this code data it cannot read, and "
        "pushing would put an older structure over a newer master, which no later "
        "reconciliation could undo.\n\n"
        "**Nothing here is at risk and nothing needs reconciling.** Update this machine's "
        "code and start it again; the sync then proceeds normally."
    )

if state.blocked_by:
    lock = state.blocked_by
    st.warning(
        f"Locked by **{lock.machine}** since {lock.taken_at}"
        + (f", expected back {lock.expected_return}" if lock.expected_return else "")
        + (". That lease is overdue." if lock.overdue else ".")
    )
    with st.expander("Take the lock anyway"):
        st.caption(
            "Only if that machine is not mid-edit. Its unpushed work is not lost — its next "
            "push will hit the revision check and be routed to reconciliation below."
        )
        if st.button("Force take the lock"):
            with ui.session() as session, session.begin():
                result = sync.force_take(session)
            st.success(result.message) if result.ok else st.error(result.message)
            st.rerun()

st.divider()

# ------------------------------------------------------------------------ conflict

if state.behind:
    # Not a conflict, and worth being emphatic about the difference: the master having moved
    # while this machine has nothing unpushed is the *ordinary* state after the other machine
    # has done a day's work. Presenting it as a conflict sent you looking for a reconciliation
    # that had nothing to reconcile.
    st.subheader("Behind the master")
    st.info(
        f"The master is at revision {state.nas.revision} (pushed by "
        f"{state.nas.machine or 'another machine'}"
        + (f" at {state.nas.updated_at}" if state.nas.updated_at else "")
        + f") and this machine started from {state.local.base_revision}. There is nothing "
        "unpushed here and nothing that exists only here, so pulling adopts the master and "
        "loses nothing."
    )
    st.caption(
        "**Refresh data** will not help with this — it only clears the cached queries, and "
        "the local database really is a different, older one. Pulling is what replaces it."
    )
    if st.button("Pull now and catch up", type="primary", key="catch_up"):
        # Every pooled connection has to go before the file is replaced, not after.
        ui.close_connections()
        result = sync.pull()
        ui.load_all.clear()
        st.success(result.message) if result.ok else st.error(result.message)
        st.rerun()
    st.divider()

if state.conflict:
    st.subheader("Conflict")
    st.error(
        f"The master is at revision {state.nas.revision}; this machine started from "
        f"{state.local.base_revision} and has {state.local.pending} unpushed change(s). "
        "Both sides have moved, so pushing would overwrite the other machine's work and "
        "pulling would discard this machine's."
    )
    st.markdown(
        "**To reconcile:** export the transactions that exist only here, pull the fresh "
        "master, then feed the file back through **Import** — which validates, previews and "
        "tags them as their own undoable batch."
    )

    with ui.session() as session:
        frame = sync.local_only_frame(session)

    if frame.empty:
        st.info(
            "No *transactions* exist only on this machine, but the revision counter has "
            "moved, so something else here has — a setting, a target, a payslip. Those are "
            "not exported by the reconciliation below; check what you changed before pulling."
        )
    else:
        st.caption(f"{len(frame)} transaction(s) exist only on this machine:")
        st.dataframe(
            ui.money_table(frame, ["Amount"]), use_container_width=True, hide_index=True
        )
        st.download_button(
            "Download local-only transactions (CSV)",
            frame.to_csv(index=False).encode("utf-8"),
            file_name=f"local_only_{sync.machine_name()}_{dt.date.today()}.csv",
            mime="text/csv",
            type="primary",
        )
    st.divider()

# --------------------------------------------------------------------------- actions

st.subheader("Push and pull")

push_col, pull_col = st.columns(2)

with push_col:
    st.markdown("**Push to the NAS**")
    st.caption(
        "Snapshot, verify, upload beside the master, verify again, then promote. The live "
        "master is only replaced once a complete verified copy sits next to it."
    )
    can_push = (
        state.nas.reachable
        and not state.moved
        and not state.blocked_by
        and not state.stale_code
    )
    if st.button(
        "Push now", type="primary", disabled=not can_push,
        help=None if can_push else "Blocked — see the status above",
    ):
        with ui.session() as session, session.begin():
            result = sync.push(session)
        ui.load_all.clear()
        if result.ok:
            st.success(result.message)
            for line in result.detail:
                st.caption(f"· {line}")
        else:
            st.error(result.message)
            for line in result.detail:
                st.caption(f"· {line}")
        st.rerun()

with pull_col:
    st.markdown("**Pull from the NAS**")
    st.caption(
        "Replaces the local database with the master and records it as the new ancestor. "
        "Refuses while there are unpushed changes."
    )
    can_pull = (
        state.nas.reachable
        and state.nas.has_master
        and not state.local.dirty
        and not state.stale_code
    )
    if st.button(
        "Pull now", disabled=not can_pull, key="pull_now",
        help=None if can_pull else "Blocked — see the status above",
    ):
        ui.close_connections()
        result = sync.pull()
        ui.load_all.clear()
        st.success(result.message) if result.ok else st.error(result.message)
        for line in result.detail:
            st.caption(f"· {line}")
        st.rerun()

if state.nas.reachable and not state.nas.has_master:
    st.info(
        "No master on the NAS yet. Push from the machine holding the good database to "
        "create it, then pull on the other."
    )

st.divider()

# ---------------------------------------------------------------------- offline mode

st.subheader("Working offline")

if state.local.mode == sync.OFFLINE:
    lock = state.lock
    st.info(
        "**Checked out for offline use**"
        + (f" since {lock.taken_at}" if lock else "")
        + (f", expected back {lock.expected_return}" if lock and lock.expected_return else "")
        + f". {state.local.pending} change(s) so far."
    )
    st.caption(
        "Automatic pushing is suspended while checked out, so there are no retry errors "
        "away from the network. Reference data is read-only until check-in."
    )
    if st.button("Check in and push", type="primary", disabled=not state.nas.reachable):
        with ui.session() as session, session.begin():
            result = sync.checkin(session)
        ui.load_all.clear()
        st.success(result.message) if result.ok else st.error(result.message)
        st.rerun()
else:
    st.caption(
        "Take a lease before going away with the laptop. The other machine then sees a "
        "stated intent and a return date rather than guessing at a stale lock."
    )
    left, right = st.columns([1, 2])
    expected = left.date_input(
        "Expected back", value=dt.date.today() + dt.timedelta(days=7), format="DD/MM/YYYY"
    )
    right.write("")
    can_checkout = state.nas.reachable and not state.local.dirty and not state.blocked_by
    if right.button(
        "Check out for offline use", disabled=not can_checkout,
        help=None if can_checkout else "Must be in sync, unlocked and connected",
    ):
        with ui.session() as session, session.begin():
            result = sync.checkout(session, expected)
        ui.load_all.clear()
        st.success(result.message) if result.ok else st.error(result.message)
        st.rerun()

st.divider()

with st.expander("Where things live"):
    st.markdown(
        f"""
| | Path |
|---|---|
| Live database | `{config.DB_PATH}` |
| Ancestor snapshot | `{config.BASE_DB_PATH}` |
| Master and sidecar | `{config.NAS_DIR}` |

The ancestor snapshot is what makes reconciliation possible: comparing local against it
distinguishes *"this machine added a row"* from *"the other machine deleted it"*. A two-way
diff cannot tell those apart.
"""
    )
