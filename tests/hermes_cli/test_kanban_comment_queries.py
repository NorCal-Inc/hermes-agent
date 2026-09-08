"""Comment-watermark queries in kanban_db.

``list_comments_after`` backs the live worker bridge: it returns only comments
newer than a cursor so a running worker folds in new operator notes without
re-reading the whole thread.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import kanban_db as kb


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    kb._INITIALIZED_PATHS.clear()
    return home


def test_list_comments_after_cursor(fresh_home):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="chat")
        c1 = kb.add_comment(conn, tid, author="alice", body="first")
        c2 = kb.add_comment(conn, tid, author="bob", body="second")

        assert [c.id for c in kb.list_comments_after(conn, tid, after_id=0)] == [c1, c2]

        newer = kb.list_comments_after(conn, tid, after_id=c1)
        assert [c.id for c in newer] == [c2]
        assert newer[0].body == "second"

        assert kb.list_comments_after(conn, tid, after_id=c2) == []
    finally:
        conn.close()


def test_worker_context_christopher_directive_outranks_stale_attempt(fresh_home):
    import time
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="Hermes boot amendment",
            body="Original body: update the boot consumer.",
        )
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs "
                "(task_id,profile,status,outcome,started_at,ended_at,summary) "
                "VALUES(?,?,?,?,?,?,?)",
                (tid, "erika", "done", "blocked", now - 120, now - 90,
                 "STALE REASONING: this must be delegated to Claude."),
            )
        kb.add_comment(
            conn, tid, "Christopher",
            "CURRENT ORDER: do this Hermes boot amendment yourself. Do not delegate.",
        )
        kb.add_comment(conn, tid, "erika", "ordinary worker note")

        ctx = kb.build_worker_context(conn, tid)
        assert ctx.index("## CURRENT CHRISTOPHER DIRECTIVES") < ctx.index("## Body")
        assert ctx.index("## Body") < ctx.index("## Prior attempts on this task")
        assert "HISTORICAL ONLY" in ctx
        assert ctx.count(
            "CURRENT ORDER: do this Hermes boot amendment yourself. Do not delegate."
        ) == 1
        assert "comment from worker `Christopher`" not in ctx
