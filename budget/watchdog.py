"""Stop the server once the last browser tab has gone.

`streamlit run` is a server: it keeps running whether or not anybody is looking at it, and
closing the tab leaves the console window sitting there. That is the right behaviour for a
service and the wrong one for something launched by double-clicking a batch file -- and it
is how this project ended up with a dashboard from 18:52 still serving port 8501 hours after
every window had been closed, which cost an evening of chasing errors that were not there.

Streamlit has no hook for 'the last client disconnected', so this polls the runtime's own
session registry. A browser tab is a session; closing it drops the websocket and the session
goes with it.

Two things make it safe to act on:

  - a grace period, because a page refresh or a laptop lid briefly drops to zero sessions
    and is not someone finishing up, and
  - a startup delay, because the count is legitimately zero between the server binding the
    port and the browser arriving.

Shutting down closes the database first. SQLite runs in WAL mode, so committed data sits in
budget.db-wal until a checkpoint folds it into the main file, and that happens when the last
connection closes. Exiting without closing leaves the WAL behind -- and a WAL that outlives
the database it belongs to is how this project twice met 'database disk image is malformed'.

Off unless BUDGET_EXIT_WHEN_IDLE is set, so `streamlit run app.py` from a terminal still
behaves like a server and a headless test run cannot be killed by its own watchdog. budget.bat
turns it on, which is the case where a leftover process is a genuine hazard.
"""

from __future__ import annotations

import os
import threading
import time

ENV_FLAG = "BUDGET_EXIT_WHEN_IDLE"
DEFAULT_GRACE = 20.0     # seconds with nobody connected before giving up
DEFAULT_STARTUP = 90.0   # seconds to wait for the first browser to arrive
POLL = 2.0


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _session_count() -> int | None:
    """How many browser sessions the runtime is holding, or None if it cannot be asked.

    Reached through streamlit.runtime rather than anything public -- there is no supported
    way to ask. None means 'do not know', which is treated as 'somebody is there': the cost
    of guessing wrong that way is a window left open, and the other way is a server killed
    underneath someone.
    """
    try:
        from streamlit.runtime import Runtime

        if not Runtime.exists():
            return None
        return len(Runtime.instance()._session_mgr.list_active_sessions())
    except Exception:  # noqa: BLE001 -- private API; any change to it must not be fatal
        return None


def _watch(grace: float, startup: float) -> None:
    deadline = time.monotonic() + startup
    seen_anyone = False

    while True:
        time.sleep(POLL)
        count = _session_count()

        if count is None:
            continue
        if count > 0:
            seen_anyone = True
            deadline = time.monotonic() + grace
            continue

        # Nobody connected. Before the first tab arrives that is normal, so the startup
        # window applies; afterwards the grace period does.
        if time.monotonic() < deadline:
            continue
        if not seen_anyone:
            # No browser ever came. Something else is wrong -- a port clash, a launcher that
            # did not open one -- and exiting would hide it. Keep waiting instead.
            deadline = time.monotonic() + startup
            continue

        _shut_down()


def _shut_down() -> None:
    """Close the database, then end the process.

    The close is not a nicety. SQLite runs in WAL mode here, so committed data lives in
    budget.db-wal until a checkpoint folds it back into the main file, and that normally
    happens when the last connection closes. Ending the process without closing leaves the
    WAL behind -- and a WAL that outlives the database it belongs to is how this project
    twice ended up with 'database disk image is malformed'. Disposing the engine checkpoints
    and closes properly; the main file is then complete on its own.

    os._exit rather than sys.exit, because this is a daemon thread: SystemExit raised here is
    caught by the thread and the server carries on regardless.
    """
    try:
        from budget import ui

        ui.close_connections()
    except Exception:  # noqa: BLE001 -- exiting anyway; a failure here must not hang the app
        pass
    os._exit(0)


def start(grace: float | None = None, startup: float | None = None) -> bool:
    """Begin watching, if the environment asks for it. Returns whether it started."""
    if not enabled():
        return False

    def _float(name: str, fallback: float) -> float:
        try:
            return float(os.environ.get(name, "") or fallback)
        except ValueError:
            return fallback

    thread = threading.Thread(
        target=_watch,
        args=(
            grace if grace is not None else _float("BUDGET_IDLE_GRACE", DEFAULT_GRACE),
            startup if startup is not None else _float("BUDGET_IDLE_STARTUP", DEFAULT_STARTUP),
        ),
        name="budget-idle-watchdog",
        daemon=True,
    )
    thread.start()
    return True
