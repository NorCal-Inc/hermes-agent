"""Cancellation and spawn-time authority are hard execution fences."""

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as c:
        yield c


def _claimed(conn, title="original objective", *, assignee="default"):
    tid = kb.create_task(conn, title=title, body="do only the original work", assignee=assignee)
    task = kb.claim_task(conn, tid, claimer="testhost:owner")
    assert task is not None
    return tid, task


def test_cancel_preserves_history_is_not_completion_and_cannot_redispatch(conn):
    tid, claimed = _claimed(conn)
    run_id = claimed.current_run_id
    assert kb.cancel_task(conn, tid, reason="owner stop", actor="Christopher")

    task = kb.get_task(conn, tid)
    assert task.status == "cancelled"
    assert task.completed_at is None
    assert task.result is None
    assert task.current_run_id is None
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["outcome"] == "cancelled"
    assert run["ended_at"] is not None
    assert kb.claim_task(conn, tid) is None
    assert kb.claim_review_task(conn, tid) is None
    assert not kb.request_review(conn, tid, force=True)
    assert not kb.unblock_task(conn, tid)
    assert not kb.reclaim_task(conn, tid)

    spawned = []
    result = kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a))
    assert result.spawned == []
    assert spawned == []
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='cancelled'", (tid,)
    ).fetchone()
    assert json.loads(event["payload"])["reason"] == "owner stop"


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("objective", "objective"),
        ("lane", "lane"),
        ("assignee", "assignee"),
        ("cancel", "cancelled"),
    ],
)
def test_live_authority_rejects_supersession_lane_bar_reassignment_and_cancel(
    conn, mutation, expected
):
    tid, task = _claimed(conn)
    fingerprint = kb.task_authority_fingerprint(task)
    if mutation == "objective":
        conn.execute("UPDATE tasks SET body='superseded delete objective' WHERE id=?", (tid,))
    elif mutation == "lane":
        conn.execute("UPDATE tasks SET executor_lane='codex_verify' WHERE id=?", (tid,))
    elif mutation == "assignee":
        conn.execute("UPDATE tasks SET assignee='barred-claude' WHERE id=?", (tid,))
    else:
        assert kb.cancel_task(conn, tid, reason="bar Claude lane", actor="owner")
    ok, reason = kb.check_worker_authority(
        conn, tid, expected_run_id=task.current_run_id,
        expected_fingerprint=fingerprint,
    )
    assert ok is False
    assert expected in reason


def test_model_tool_guard_prevents_post_cancel_external_mutation(conn, monkeypatch):
    tid, task = _claimed(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_AUTHORITY", kb.task_authority_fingerprint(task))
    assert kb.cancel_task(conn, tid, reason="stop before mutation", actor="owner")

    import model_tools
    called = []
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda *a, **k: called.append(a) or "bad")
    result = model_tools.handle_function_call("terminal", {"command": "touch forbidden"})
    assert called == []
    assert "authority denied" in result.lower()
    assert "cancelled" in result.lower()
