"""Purging an archived card removes everything keyed to it.

2026-10-13, t_6cc051f2: ``hermes kanban archive --rm`` printed "Deleted" and
left 175 rows behind (execution events, executions, attachments, verifier
verdicts, candidate lessons) plus the card's attachment folder and worker log.
Christopher had to delete the residue by hand. These tests pin the full
cascade, the two things it deliberately keeps (active and retired lessons are
durable learning), and the one case it refuses (an unresolved execution effect
is work still owed outside the board).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Same isolation as test_kanban_task_history_append_only: never let a
    # worker-inherited pin point these writes at the live board.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


# Every table the purge must empty for the card, and the column it is keyed by.
_TASK_KEYED = {
    "task_comments": "task_id",
    "task_events": "task_id",
    "task_runs": "task_id",
    "executions": "task_id",
    "execution_effects": "task_id",
    "task_attachments": "task_id",
    "task_verifications": "task_id",
    "task_timeout_checkpoints": "task_id",
}


def _count(conn, table: str, column: str, value) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()[0]


def _exec_events(conn, execution_id: str) -> int:
    return _count(conn, "execution_events", "execution_id", execution_id)


def _add_legacy_checkpoint_table(conn) -> None:
    """The live board carries this table from earlier timeout-checkpoint code
    that init_db no longer creates (3 rows, 2026-09-05)."""
    with kb.write_txn(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS task_timeout_checkpoints ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER, "
            "created_at INTEGER NOT NULL, diff_hash TEXT NOT NULL, next_action TEXT NOT NULL)"
        )


def _seed_archived_card(conn, *, effect_state: str = "committed",
                        legacy_table: bool = True) -> tuple[str, str, dict]:
    """An archived card with a row in every keyed table, plus its files."""
    now = int(time.time())
    if legacy_table:
        _add_legacy_checkpoint_table(conn)
    tid = kb.create_task(conn, title="purge bearer", assignee="worker")
    kb.add_comment(conn, tid, "worker", "note")
    assert kb.claim_task(conn, tid) is not None
    # Blocked then archived: the t_6cc051f2 shape (stopped at its attempt
    # limit, then killed), and it needs no verification chain to set up.
    assert kb.block_task(conn, tid, reason="attempt limit reached")

    att_dir = kb.task_attachments_dir(tid)
    att_dir.mkdir(parents=True, exist_ok=True)
    blob = att_dir / "evidence.md"
    blob.write_text("evidence", encoding="utf-8")
    kb.add_attachment(conn, tid, filename="evidence.md", stored_path=str(blob),
                      size=8, uploaded_by="worker")

    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("worker output\n", encoding="utf-8")

    exec_id = f"exec-{tid}"
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO executions (id, task_id, executor_type, command_class, cwd, nonce, "
            "controller_token, ownership, started_at, heartbeat_at, status, created_at) "
            "VALUES (?, ?, 'shell', 'test', '/tmp', 'n', 'tok', 'controller', ?, ?, 'completed', ?)",
            (exec_id, tid, now, now, now),
        )
        conn.execute(
            "INSERT INTO execution_events (execution_id, kind, payload, created_at) "
            "VALUES (?, 'started', NULL, ?)", (exec_id, now),
        )
        conn.execute(
            "INSERT INTO execution_effects (task_id, effect_type, resource, inverse_action, "
            "state, created_at) VALUES (?, 'file', '/tmp/x', 'restore', ?, ?)",
            (tid, effect_state, now),
        )
        conn.execute(
            "INSERT INTO task_verifications (task_id, state, verifier, created_at) "
            "VALUES (?, 'VERIFIED', 'verifier', ?)", (tid, now),
        )
        if legacy_table:
            conn.execute(
                "INSERT INTO task_timeout_checkpoints (task_id, created_at, diff_hash, next_action) "
                "VALUES (?, ?, 'unavailable', 'resume')", (tid, now),
            )
        lesson_ids = {}
        for state in (kb.LESSON_STATE_CANDIDATE, "active", "retired"):
            cur = conn.execute(
                "INSERT INTO task_lessons (source_task_id, tenant, scope, applicability, lesson, "
                "created_at, active, state) VALUES (?, 't', 'tenant', 'all', ?, ?, ?, ?)",
                (tid, f"{state} lesson", now, 1 if state == "active" else 0, state),
            )
            lesson_ids[state] = cur.lastrowid
    assert kb.archive_task(conn, tid)
    return tid, exec_id, {"att_dir": att_dir, "log": log, "lessons": lesson_ids}


@pytest.mark.parametrize("legacy_table", [True, False], ids=["with_checkpoint_table", "fresh_board"])
@pytest.mark.parametrize("purge", ["delete_archived_task", "delete_task"])
def test_purge_removes_every_row_and_file_keyed_to_the_card(kanban_home, purge, legacy_table):
    with kb.connect_closing() as conn:
        tid, exec_id, extra = _seed_archived_card(conn, legacy_table=legacy_table)
        keyed = dict(_TASK_KEYED)
        if not legacy_table:
            keyed.pop("task_timeout_checkpoints")
        for table, col in keyed.items():
            assert _count(conn, table, col, tid) >= 1, table
        assert _exec_events(conn, exec_id) == 1
        assert extra["att_dir"].is_dir() and extra["log"].is_file()

        assert getattr(kb, purge)(conn, tid) is True

        assert _count(conn, "tasks", "id", tid) == 0
        leftovers = {t: _count(conn, t, c, tid) for t, c in keyed.items()}
        assert leftovers == {t: 0 for t in keyed}
        assert _exec_events(conn, exec_id) == 0
        kept = {r[0] for r in conn.execute(
            "SELECT state FROM task_lessons WHERE source_task_id = ?", (tid,))}
        # Candidates go with their card; verified learning outlives it.
        assert kept == {"active", "retired"}
    assert not extra["att_dir"].exists()
    assert not extra["log"].exists()


@pytest.mark.parametrize("state", ["rollback_pending", "conflict"])
def test_purge_refuses_while_an_execution_effect_is_unresolved(kanban_home, state):
    with kb.connect_closing() as conn:
        tid, exec_id, extra = _seed_archived_card(conn, effect_state=state)

        assert kb.delete_archived_task(conn, tid) is False

        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row is not None and row[0] == "archived"
        assert _count(conn, "execution_effects", "task_id", tid) == 1
        assert _exec_events(conn, exec_id) == 1
        refused = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'history_delete_refused'",
            (tid,),
        ).fetchall()
        assert refused and "unresolved_execution_effects" in refused[-1][0]
    assert extra["att_dir"].is_dir() and extra["log"].is_file()
