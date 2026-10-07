"""Task history is append-only — Governance Phase 3, rule 8.

Doctrine basis: operations.md ("prior runs, evidence, verifier results,
comments, and failure history are never deleted or rewritten") and
execution-honesty.md 1.5 (an agent's own violation record is append-only task
history; the agent who committed the violation may not edit, delete, supersede
or hide it — corrections are appended as new entries).

Independent verifier t_85eaad92 (2026-10-06) found that ``delete_task()`` had
no status check at all and erased a live card's comments, events and runs in
one call. These tests pin the storage-layer gate that closes that, and the
generic append-only property it rests on.
"""

from __future__ import annotations

import sqlite3
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
    # Pin the board root to the temp home and drop every kanban pin a
    # dispatched worker inherits. HERMES_KANBAN_DB outranks HERMES_HOME in
    # kanban_db_path(); without this, running the module from a worker
    # outside the repo's conftest sandbox writes fixture cards to the
    # operator's live board (this happened: t_92521910 run 3496 created
    # t_3f7e43ae, 2026-10-07).
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _history_counts(conn, tid: str) -> tuple[int, int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0],
    )


def _card_with_history(conn, *, status: str) -> str:
    """A card carrying a comment, several events and one run, in ``status``."""
    tid = kb.create_task(conn, title="history bearer", assignee="worker")
    kb.add_comment(conn, tid, "worker", "VIOLATION: claimed cross-company scope")
    assert kb.claim_task(conn, tid) is not None
    if status == "running":
        return tid
    if status == "blocked":
        assert kb.block_task(conn, tid, reason="needs input")
        return tid
    assert kb.complete_task(conn, tid, result="done")
    if status == "done":
        return tid
    if status == "archived":
        assert kb.archive_task(conn, tid)
        return tid
    raise AssertionError(status)


# ---------------------------------------------------------------------------
# delete_task(): archived-only, same as delete_archived_task()
# ---------------------------------------------------------------------------

class TestDeleteTaskRequiresArchived:
    @pytest.mark.parametrize("status", ["running", "blocked", "done"])
    def test_live_card_is_refused_and_history_untouched(self, kanban_home, status):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status=status)
            before = _history_counts(conn, tid)
            assert before[0] == 1 and before[1] > 0 and before[2] == 1

            assert kb.delete_task(conn, tid) is False

            assert kb.get_task(conn, tid) is not None
            comments, events, runs = _history_counts(conn, tid)
            assert comments == before[0]
            assert runs == before[2]
            # One more event than before: the refusal itself is on the record.
            assert events == before[1] + 1
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            assert kinds[-1] == "history_delete_refused"

    def test_refusal_event_is_not_lifecycle_progress(self):
        assert "history_delete_refused" in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS

    def test_todo_card_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="fresh", assignee="worker")
            kb.add_comment(conn, tid, "user", "note")
            assert kb.delete_task(conn, tid) is False
            assert kb.get_task(conn, tid) is not None
            assert [c.body for c in kb.list_comments(conn, tid)] == ["note"]

    def test_archived_card_is_purged_with_its_history(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="archived")
            assert kb.delete_task(conn, tid) is True
            assert kb.get_task(conn, tid) is None
            assert _history_counts(conn, tid) == (0, 0, 0)

    def test_unknown_card_returns_false_without_an_event(self, kanban_home):
        with kb.connect_closing() as conn:
            assert kb.delete_task(conn, "t_nope") is False
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 't_nope'"
            ).fetchone()[0] == 0

    def test_delete_archived_task_still_purges(self, kanban_home):
        """The reordered cascade (card row first) still clears everything."""
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="archived")
            conn.execute(
                "INSERT INTO kanban_notify_subs(task_id, platform, chat_id, thread_id, user_id, created_at, last_event_id) "
                "VALUES (?, 'telegram', '1', '', 'u', 0, 0)",
                (tid,),
            )
            conn.commit()
            assert kb.delete_archived_task(conn, tid) is True
            assert kb.get_task(conn, tid) is None
            assert _history_counts(conn, tid) == (0, 0, 0)
            assert conn.execute(
                "SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (tid,)
            ).fetchone()[0] == 0

    def test_delete_archived_task_refuses_live_card(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="done")
            assert kb.delete_archived_task(conn, tid) is False
            assert kb.get_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Storage-layer triggers: no edit, no delete while the card exists
# ---------------------------------------------------------------------------

class TestHistoryTriggers:
    def test_triggers_are_installed(self, kanban_home):
        with kb.connect_closing() as conn:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
        assert set(kb.TASK_HISTORY_TRIGGER_NAMES) <= names

    def test_comment_cannot_be_edited(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="running")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE task_comments SET body = 'nothing happened' WHERE task_id = ?",
                        (tid,),
                    )
            assert [c.body for c in kb.list_comments(conn, tid)] == [
                "VIOLATION: claimed cross-company scope"
            ]

    def test_comment_cannot_be_deleted_while_card_exists(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="archived")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute("DELETE FROM task_comments WHERE task_id = ?", (tid,))
            assert len(kb.list_comments(conn, tid)) == 1

    def test_event_cannot_be_edited(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="running")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE task_events SET kind = 'benign' WHERE task_id = ?",
                        (tid,),
                    )
            assert "benign" not in [e.kind for e in kb.list_events(conn, tid)]

    def test_event_cannot_be_deleted_while_card_exists(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="done")
            n = len(kb.list_events(conn, tid))
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute("DELETE FROM task_events WHERE task_id = ?", (tid,))
            assert len(kb.list_events(conn, tid)) == n

    def test_run_cannot_be_deleted_or_rehomed_while_card_exists(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="done")
            other = kb.create_task(conn, title="other", assignee="worker")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute("DELETE FROM task_runs WHERE task_id = ?", (tid,))
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE task_runs SET task_id = ? WHERE task_id = ?",
                        (other, tid),
                    )
            assert len(kb.list_runs(conn, tid)) == 1

    def test_run_lifecycle_columns_still_update(self, kanban_home):
        """Ending a run writes history; it is not a rewrite and must still work."""
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="running")
            assert kb.heartbeat_claim(conn, tid, claimer=kb._claimer_id()) is True
            assert kb.complete_task(conn, tid, result="ok")
            run = kb.latest_run(conn, tid)
            assert run is not None and run.ended_at is not None


# ---------------------------------------------------------------------------
# Legacy migration and retention paths keep working under the triggers
# ---------------------------------------------------------------------------

class TestMigrationAndRetention:
    def test_legacy_event_kind_rename_still_migrates(self, kanban_home):
        """The one sanctioned event rewrite suspends the trigger for itself."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="x")
            now = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'spawn_auto_blocked', NULL, ?)",
                    (tid, now),
                )
        kb.init_db()
        with kb.connect_closing() as conn:
            kinds = [e.kind for e in kb.list_events(conn, tid)]
            assert "spawn_auto_blocked" not in kinds and "gave_up" in kinds
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            # ...and the trigger is back afterwards.
            assert "trg_task_events_append_only_update" in names

    def test_gc_events_keeps_history_of_existing_cards(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card_with_history(conn, status="done")
            # Age every event far past any retention window.
            with kb.write_txn(conn):
                conn.execute("DROP TRIGGER trg_task_events_append_only_update")
                conn.execute("UPDATE task_events SET created_at = 1 WHERE task_id = ?", (tid,))
            kb._ensure_task_history_append_only(conn)
            n = len(kb.list_events(conn, tid))
            assert n > 0
            assert kb.gc_events(conn, older_than_seconds=0) == 0
            assert len(kb.list_events(conn, tid)) == n

    def test_gc_events_reaps_only_orphans(self, kanban_home):
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES ('t_gone', 'promoted', NULL, 1)"
                )
            assert kb.gc_events(conn, older_than_seconds=0) == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 't_gone'"
            ).fetchone()[0] == 0
