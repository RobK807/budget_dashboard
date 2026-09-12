"""Fold the category comment into the comment, so the field can stop being used.

    python -m budget.merge_category_comments --report   # say what would change
    python -m budget.merge_category_comments            # apply it

`category_comment` was a second free-text box beside `comment`, and it earned its keep
nowhere: the Transactions table shows the comment and not the category comment, no figure is
grouped or totalled by it, and its only distinct behaviours were a rule saying it needed a
category and an auto-fill that copied the comment into it. See README, 'Two boxes for one
sentence'.

Of the rows that had one, the overwhelming majority repeat the comment word for word, and
none has a category comment without a comment -- so nothing here is the only record of
anything. A minority differ, and those are the reason this is a script rather than a delete:

  comment 'Boots', category comment 'Shaving gel'   -- where, and what

Three cases, in order of preference:

  * identical (ignoring case and spacing)     -- nothing to keep, the column is just cleared
  * one contains the other                    -- the longer one already says it all
  * genuinely different                       -- joined as 'comment - category comment'

The column itself stays on the table, holding what it held. Retaining a superseded column
rather than dropping it is what was done with `savings_target` and `salary_profile.
annual_salary`: the data is evidence of how a figure was once recorded, and an ALTER that
drops it cannot be undone from the sidecar.

Idempotent. A second run finds every row already merged and writes nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3

from sqlalchemy import select

from budget import config
from budget.db import create_all, in_use, make_engine, make_session_factory
from budget.models import Txn
from budget.service import bump_revision

JOIN = " - "


def _normalise(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


def merged(comment: str | None, category_comment: str | None) -> str | None:
    """What the comment should say once the category comment is folded into it.

    Returns None when there is nothing to add, which is the common case.
    """
    extra = (category_comment or "").strip()
    if not extra:
        return None

    existing = (comment or "").strip()
    if not existing:
        return extra

    here, there = _normalise(existing), _normalise(extra)
    if here == there or there in here:
        return None            # the comment already says it
    if here in there:
        return extra           # the category comment is the fuller of the two
    return f"{existing}{JOIN}{extra}"


def snapshot() -> str:
    """VACUUM INTO rather than a file copy: consistent even mid-write (DESIGN.md 7)."""
    target = config.DB_PATH.with_name(
        f"budget.pre-comment-merge-{dt.datetime.now():%Y%m%d-%H%M%S}.db"
    )
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute(f"VACUUM INTO '{str(target).replace(chr(39), chr(39) * 2)}'")
    finally:
        conn.close()
    return target.name


def apply(session, *, write: bool) -> dict:
    """Fold every category comment into its comment. Returns what happened, or would."""
    joined: list[str] = []
    replaced: list[str] = []
    identical = 0

    rows = session.scalars(
        select(Txn).where(Txn.category_comment.is_not(None))
    ).all()

    for txn in rows:
        if not (txn.category_comment or "").strip():
            continue
        wanted = merged(txn.comment, txn.category_comment)
        if wanted is None:
            identical += 1
            if write:
                txn.category_comment = None
            continue

        line = (
            f"{txn.txn_date}  {txn.comment!r} + {txn.category_comment!r}  ->  {wanted!r}"
        )
        (joined if JOIN in wanted else replaced).append(line)
        if write:
            txn.comment = wanted
            txn.category_comment = None

    if write and (joined or replaced or identical):
        session.flush()
        bump_revision(session)

    return {
        "joined": joined, "replaced": replaced, "identical": identical,
        "total": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", action="store_true", help="say what would change, write nothing"
    )
    args = parser.parse_args(argv)

    if not args.report and in_use():
        print("The dashboard still has the database open. Close every window and re-run.")
        return 1
    if not args.report:
        print(f"Snapshot taken: {snapshot()}")

    engine = make_engine()
    try:
        create_all(engine)
        factory = make_session_factory(engine)
        with factory() as session, session.begin():
            result = apply(session, write=not args.report)
            if args.report:
                session.rollback()
    finally:
        engine.dispose()

    for line in result["joined"]:
        print(f"  + {line}")
    for line in result["replaced"]:
        print(f"  ~ {line}")

    verb = "would keep" if args.report else "kept"
    print(
        f"\n{result['total']} row(s) carried a category comment. "
        f"{result['identical']} repeated the comment and {verb} nothing; "
        f"{len(result['joined'])} joined, {len(result['replaced'])} replaced."
    )
    if not args.report:
        print("\nThere is now a push pending.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
