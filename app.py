"""Entrypoint and navigation.

Run with:  streamlit run app.py

Pages are declared explicitly via st.navigation rather than relying on a pages/ directory,
because that convention takes each tab's label from its filename -- which made the
entrypoint show up as "app". Declaring them here also keeps set_page_config in one place:
st.navigation runs the entrypoint and the selected page as a single script run, so it may
only be called once.
"""

from __future__ import annotations

import streamlit as st

from budget import config, ui

st.set_page_config(page_title="Budget", page_icon="💷", layout="wide")

check = ui.database_check()
if not check["ok"]:
    st.error(f"No database found at `{check['path']}` — {check['reason']}")

    # What this process actually sees, not what it ought to see. Without it, diagnosing a
    # path that resolves differently under one launcher than another is pure guesswork.
    st.markdown("**What the app sees**")
    st.code(
        "\n".join(
            [
                f"resolved path   {check['path']}",
                f"LOCALAPPDATA    {check['localappdata']}",
                f"BUDGET_DB_PATH  {check['env_override']}",
                f"machine / user  {check['machine']} / {check['user']}",
                f"folder exists   {check['parent_exists']}",
                "folder contains "
                + (", ".join(check["siblings"]) if check["siblings"] else "(nothing)"),
                f"error           {check['error'] or '(none)'}",
            ]
        ),
        language="text",
    )

    if "budget.db" in check["siblings"]:
        st.warning(
            "**`budget.db` is in that folder, yet this process cannot read it.** That points "
            "at something holding or blocking the file rather than at a missing database — "
            "a virus scanner, Controlled Folder Access, or another copy of the app still "
            "running. Close this window and relaunch; if it recurs, the panel above is what "
            "to go on."
        )
    else:
        st.markdown(
            "**If the file really is gone**, in order of preference:\n\n"
            "1. **Pull from the NAS** — the master copy lives in "
            f"`{config.NAS_DIR}`. This is the one to use: it keeps the sync revision, so "
            "both machines stay agreed about what has happened.\n"
            "2. **Restore a backup** — look for `budget.pre-phase5.db` or similar beside "
            "where the database should be, and rename it to `budget.db`.\n"
            "3. **Rebuild from the workbook** — `python -m budget.migrate_xlsm`. Last "
            "resort: it resets the sync revision and loses anything entered since the "
            "workbook was last the source of truth."
        )
    st.stop()

# Emoji rather than :material/...: icons -- the Material Symbols font is fetched remotely
# and falls back to rendering the literal name ("dashboard") if it does not load.
navigation = st.navigation(
    {
        "Review": [
            st.Page("views/summary.py", title="Summary", icon="📊", default=True),
            st.Page("views/month.py", title="Month", icon="📅"),
            st.Page("views/transactions.py", title="Transactions", icon="🧾"),
            st.Page("views/trends_page.py", title="Trends", icon="📈"),
            st.Page("views/projections_page.py", title="Projections", icon="🔮"),
        ],
        "Track": [
            st.Page("views/savings_page.py", title="Savings and investments", icon="🏦"),
            st.Page("views/salary_page.py", title="Salary", icon="💼"),
            st.Page("views/cards_page.py", title="Cards", icon="💳"),
            st.Page("views/cycling_page.py", title="Cycling", icon="🚲"),
        ],
        "Record": [
            st.Page("views/add.py", title="Add transaction", icon="➕"),
            st.Page("views/import_page.py", title="Import", icon="📥"),
            st.Page("views/cycling_record.py", title="Cycling", icon="🚴"),
        ],
        "Manage": [
            st.Page("views/settings_page.py", title="Settings", icon="⚙️"),
            st.Page("views/sync_page.py", title="Sync", icon="🔄"),
        ],
    }
)
navigation.run()
