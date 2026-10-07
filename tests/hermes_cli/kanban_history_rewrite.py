"""TEST FIXTURE ONLY: rewrite task history inside a test.

Task history (``task_comments``, ``task_events``, ``task_runs``) is append-only
at the storage layer — see ``kanban_db.TASK_HISTORY_TRIGGERS_SQL`` and
``tests/hermes_cli/test_kanban_task_history_append_only.py``. Production code
has no way around those triggers, and that is the point.

Tests, however, routinely need to *simulate* history that never happened the
slow way: backdate an event so a card looks stale, corrupt a payload so a
recovery path sees malformed provenance, delete a run row to fake a board
from before ``task_runs`` existed. Every such write is a fixture, not a
behaviour under test, and it must say so. This context manager is how it
says so: it drops the triggers for the duration of the block and reinstalls
them on the way out, so the code under test runs against the real rule.

It lives in ``tests/`` deliberately. Putting a "suspend the append-only rule"
helper in ``hermes_cli`` would hand every production caller a one-line bypass
of a doctrine gate (execution-honesty.md 1.5).
"""

from __future__ import annotations

import contextlib
import sqlite3
from typing import Iterator

from hermes_cli import kanban_db as kb


@contextlib.contextmanager
def rewrite_task_history(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Suspend the append-only history triggers for a fixture write.

    Works inside or outside an open ``write_txn``: DDL is transactional in
    SQLite, so the drop and the reinstall ride the caller's transaction when
    there is one.
    """
    for name in kb.TASK_HISTORY_TRIGGER_NAMES:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    try:
        yield conn
    finally:
        kb._ensure_task_history_append_only(conn)
