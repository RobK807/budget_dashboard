# Phase 2b — cross-machine sync

```bash
streamlit run app.py          # Manage → Sync
python -m pytest tests -q     # 124 passing
```

Implements DESIGN.md §6.3. The database always lives on a local disk; the NAS at
`K:\Private\Finance\budget_db` holds pushed copies only. Nothing ever opens the master over
SMB, because SQLite's locking is not reliable there.

## First-time setup

1. On the machine with the good database (this laptop), open **Manage → Sync** and press
   **Push now**. That creates `budget.db`, `budget.meta.json` and `budget.db.bak` on the NAS.
2. On the desktop, follow [README.md](README.md) to install, then press **Pull now** instead
   of running the migration. Both machines are then on the same revision.

## How it protects you

Two guards, and the second is the one that matters.

A **lock** (`budget.lock`) records who is working. A **revision counter** records what they
started from: every write bumps `db_meta.revision`, and a push refuses if the master's
revision has moved away from the `base_revision` this machine pulled. Without that check a
lock slipped or force-taken loses a machine's work silently; with it, the same slip becomes
a refusal you can reconcile.

**Pushes are staged**, so a dropped connection cannot leave a truncated master:

```
VACUUM INTO snapshot → integrity_check + sha256 → re-check master revision
   → upload as budget.db.incoming → verify sha256 → promote (old → .bak) → write sidecar
```

`VACUUM INTO` rather than a file copy, because copying a live SQLite database can capture a
torn state. The upload is verified *after* transfer, which is what catches silent SMB write
corruption. The master is only replaced once a complete verified copy sits beside it, and the
previous generation is kept as `budget.db.bak`.

**The invariant: `pushed_revision` only advances after a push verifies end to end.** Every
failure path leaves it untouched, so the next trigger retries rather than believing itself
synced. Two tests pin this.

**Revision is read from the sidecar**, never by opening the master over SMB — a small JSON
file is safe to read over a network share.

## Automatic push

After every committed write — add, import, undo, remove, restore — so the window in which two
machines can diverge is seconds rather than days.

Failure handling distinguishes the normal from the exceptional:

| Situation | Treatment |
|---|---|
| NAS unreachable (laptop away) | Quiet caption, stays pending, retries next write |
| Deliberate offline mode | Suppressed entirely — no pointless retries |
| Conflict or foreign lock | Warning pointing at the Sync page |

The unreachable case is deliberately understated. Away from home it is the normal state, and
an error banner on every entry would train you to ignore the real ones.

The sidebar carries a **persistent** badge — 🟢 in sync · rev N, 🟡 N pending, 🔴 conflict —
rather than a toast, for the same reason: a status that disappears after a few seconds takes
the genuine conflicts with it.

## Deliberate offline mode

**Check out** before going away with the laptop: it requires being in sync, takes a
long-lived lease with a stated return date, and refreshes the ancestor snapshot. Automatic
pushing is then suspended. **Check in** pushes and releases the lease.

The value is what the *other* machine sees. Under accidental divergence the desktop finds a
stale lock and has to guess; under a deliberate checkout it sees a stated intent and an
expected return, which is a far better basis for deciding whether to force-take.

Force-taking is available and explicit. The other machine's work is not lost — its next push
hits the revision check and routes to reconciliation.

## Reconciliation

`budget.base.db` holds the **common ancestor**: a copy of exactly what was last pulled.
Without it there is no way to distinguish *"this machine added a row"* from *"the other
machine deleted it"* — a two-way diff gets that wrong.

On a conflict the Sync page lists the transactions that exist only here (by `uid`, absent
from the ancestor) and exports them as CSV **in the Import page's own format**. Pull the
fresh master, feed the file back through Import, and it arrives validated, previewed and
tagged as its own undoable batch. That is why §6.3.5 needed almost no new code — and why
Phase 2 had to come first.

A test asserts the export parses cleanly through `importer.parse`, so the two halves cannot
drift apart.

## Verified

124 tests, covering conflict detection, the dirty-flag invariant on failure, lock refusal
and force-take, overdue leases, checkout requiring sync, pull refusing to overwrite unpushed
work, and the export round-trip.

Also exercised end to end against the real NAS on a scratch database and a scratch folder:
first push created the master and sidecar (327,680 bytes, sha256 verified, backup kept), a
local edit showed as pending, a simulated push from another machine was detected as a
conflict, the push was refused with the revisions named, and the reconciliation export
produced exactly the one local-only transaction. The scratch folder was then removed — your
real `budget_db` folder does not exist yet and will be created by your first push.

**A bug that probe caught:** reachability originally tested whether `budget_db/` itself
existed, which meant the very first push could never happen — that folder is ours to create.
It now tests the share above it. Worth noting because it would only ever have appeared on a
first run, on a machine with nothing to fall back on.

## Next

Phase 3 — settings CRUD for accounts, categories and classifications, which is where
effective dating finally replaces `update_months` in the interface as well as the schema.
Then Phase 4 for projections, salary, cards and cycling.
