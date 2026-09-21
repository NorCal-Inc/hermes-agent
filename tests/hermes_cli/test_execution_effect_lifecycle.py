from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.execution_effects import (
    effect_states,
    mark_applied,
    prepare_effect,
)

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _claimed(conn, title="effect lifecycle"):
    tid = kb.create_task(conn, title=title, assignee="worker", gauntlet=False)
    host = kb._claimer_id().split(":", 1)[0]
    task = kb.claim_task(conn, tid, claimer=f"{host}:effects")
    assert task is not None and task.current_run_id is not None
    return tid, int(task.current_run_id)


def _applied_file_effect(conn, tmp_path: Path, tid: str, run_id: int, *, text="owned"):
    path = tmp_path / f"{tid}.txt"
    before = {"exists": path.exists()}
    if path.exists():
        before["text"] = path.read_text(encoding="utf-8")
    eid = prepare_effect(
        conn,
        task_id=tid,
        run_id=run_id,
        effect_type="test_text_write",
        resource=str(path),
        before_state=before,
        after_state={"exists": True, "text": text},
        inverse_action="restore_text_file",
        inverse_payload={"existed": bool(before["exists"]), "text": before.get("text", "")},
    )
    path.write_text(text, encoding="utf-8")
    mark_applied(conn, eid)
    return path, eid


def test_success_commits_run_effects(conn, tmp_path):
    tid, run_id = _claimed(conn)
    path, _ = _applied_file_effect(conn, tmp_path, tid, run_id)

    assert kb.complete_task(conn, tid, result="done", expected_run_id=run_id)
    assert path.read_text(encoding="utf-8") == "owned"
    assert effect_states(conn, task_id=tid, run_id=run_id) == ["committed"]


def test_dependency_wait_holds_then_replacement_adopts_and_reclaim_rolls_back(conn, tmp_path):
    tid, run1 = _claimed(conn)
    path, _ = _applied_file_effect(conn, tmp_path, tid, run1)

    assert kb.block_task(
        conn,
        tid,
        reason="waiting for dependency",
        kind="dependency",
        expected_run_id=run1,
    )
    assert kb.get_task(conn, tid).status == "todo"
    assert effect_states(conn, task_id=tid, run_id=run1) == ["held"]
    assert path.exists()

    assert kb.recompute_ready(conn) == 1
    host = kb._claimer_id().split(":", 1)[0]
    task2 = kb.claim_task(conn, tid, claimer=f"{host}:replacement")
    assert task2 is not None and task2.current_run_id is not None
    run2 = int(task2.current_run_id)
    assert run2 != run1
    assert effect_states(conn, task_id=tid, run_id=run2) == ["applied"]
    row = conn.execute(
        "SELECT origin_run_id FROM execution_effects WHERE task_id=?", (tid,)
    ).fetchone()
    assert int(row["origin_run_id"]) == run1

    assert kb.reclaim_task(conn, tid, reason="fault injection")
    row = conn.execute(
        "SELECT state FROM execution_effects WHERE task_id=?", (tid,)
    ).fetchone()
    assert row["state"] == "rollback_pending"

    reverted, conflicts = kb.reconcile_execution_effect_rollbacks(conn)
    assert reverted == 1
    assert conflicts == 0
    assert not path.exists()
    row = conn.execute(
        "SELECT state FROM execution_effects WHERE task_id=?", (tid,)
    ).fetchone()
    assert row["state"] == "reverted"


def test_recovery_conflict_never_overwrites_newer_state(conn, tmp_path):
    tid, run_id = _claimed(conn)
    path, _ = _applied_file_effect(conn, tmp_path, tid, run_id, text="worker")

    assert kb.reclaim_task(conn, tid, reason="fault injection")
    path.write_text("newer-owner", encoding="utf-8")

    reverted, conflicts = kb.reconcile_execution_effect_rollbacks(conn)
    assert reverted == 0
    assert conflicts == 1
    assert path.read_text(encoding="utf-8") == "newer-owner"
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    row = conn.execute("SELECT block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["block_kind"] == "needs_input"


def test_unresolved_effect_refuses_replacement_claim(conn, tmp_path):
    tid, run_id = _claimed(conn)
    _applied_file_effect(conn, tmp_path, tid, run_id)
    assert kb.reclaim_task(conn, tid, reason="leave rollback pending")

    host = kb._claimer_id().split(":", 1)[0]
    assert kb.claim_task(conn, tid, claimer=f"{host}:too-early") is None
    assert kb.get_task(conn, tid).status == "ready"

    kb.reconcile_execution_effect_rollbacks(conn)
    assert kb.claim_task(conn, tid, claimer=f"{host}:after-reconcile") is not None


def test_review_handoff_commits_implementation_effects(conn, tmp_path):
    tid, run_id = _claimed(conn)
    path, _ = _applied_file_effect(conn, tmp_path, tid, run_id)

    ok = kb.request_review(
        conn,
        tid,
        summary="implementation ready for review",
        expected_run_id=run_id,
    )
    assert ok
    assert path.exists()
    assert effect_states(conn, task_id=tid, run_id=run_id) == ["committed"]


def test_normal_file_tool_write_is_automatically_owned_by_active_run(
    conn, tmp_path, monkeypatch
):
    import json
    from tools.file_tools import write_file_tool

    tid, run_id = _claimed(conn, title="ordinary file tool ownership")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "ordinary.txt"
    db_path = kb.kanban_db_path(board="default")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace))
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    result = json.loads(write_file_tool(str(target), "ordinary-write", task_id="effect-test"))
    assert not result.get("error"), result
    assert target.read_text(encoding="utf-8") == "ordinary-write"

    row = conn.execute(
        "SELECT state, effect_type, run_id FROM execution_effects WHERE task_id=?",
        (tid,),
    ).fetchone()
    assert row is not None
    assert row["state"] == "applied"
    assert row["effect_type"] == "workspace_text_write"
    assert int(row["run_id"]) == run_id

    assert kb.reclaim_task(conn, tid, reason="ordinary write fault injection")
    reverted, conflicts = kb.reconcile_execution_effect_rollbacks(conn)
    assert (reverted, conflicts) == (1, 0)
    assert not target.exists()


def test_real_worker_process_death_is_reconciled_without_repair_task(
    conn, tmp_path, monkeypatch
):
    import os
    import subprocess
    import sys

    tid, run_id = _claimed(conn, title="real process death")
    workspace = tmp_path / "death-workspace"
    workspace.mkdir()
    target = workspace / "owned.txt"
    db_path = kb.kanban_db_path(board="default")

    env = dict(os.environ)
    env.update(
        HERMES_KANBAN_TASK=tid,
        HERMES_KANBAN_RUN_ID=str(run_id),
        HERMES_KANBAN_DB=str(db_path),
        HERMES_KANBAN_WORKSPACE=str(workspace),
        TERMINAL_CWD=str(workspace),
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
    )
    code = f"""
import os
from tools.file_tools import write_file_tool
result = write_file_tool({str(target)!r}, 'process-owned', task_id='death-child')
if 'error' in result.lower():
    os._exit(33)
os._exit(17)
"""
    child = subprocess.Popen([sys.executable, "-c", code], env=env, cwd=str(workspace))
    kb._set_worker_pid(conn, tid, child.pid)
    rc = child.wait(timeout=30)
    assert rc == 17
    assert target.read_text(encoding="utf-8") == "process-owned"

    # Make the run immediately eligible for crash detection and supply the
    # process exit status the dispatcher normally receives from its reap pass.
    conn.execute("UPDATE tasks SET started_at=started_at-9999 WHERE id=?", (tid,))
    conn.execute("UPDATE task_runs SET started_at=started_at-9999 WHERE id=?", (run_id,))
    conn.commit()
    kb._record_worker_exit(child.pid, rc << 8)

    crashed = kb.detect_crashed_workers(conn)
    assert tid in crashed
    state = conn.execute(
        "SELECT state FROM execution_effects WHERE task_id=?", (tid,)
    ).fetchone()[0]
    assert state == "rollback_pending"

    reverted, conflicts = kb.reconcile_execution_effect_rollbacks(conn)
    assert (reverted, conflicts) == (1, 0)
    assert not target.exists()
    # Recovery reused the original task. No repair/recovery card was created.
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_effect_conflict_remains_sticky_across_ready_recompute(conn, tmp_path):
    tid, run_id = _claimed(conn, title="sticky effect conflict")
    path, _ = _applied_file_effect(conn, tmp_path, tid, run_id, text="worker-state")

    assert kb.reclaim_task(conn, tid, reason="fault injection")
    path.write_text("newer-state", encoding="utf-8")
    reverted, conflicts = kb.reconcile_execution_effect_rollbacks(conn)
    assert (reverted, conflicts) == (0, 1)

    for _ in range(5):
        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        row = conn.execute("SELECT block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        assert row["block_kind"] == "needs_input"
    assert path.read_text(encoding="utf-8") == "newer-state"
