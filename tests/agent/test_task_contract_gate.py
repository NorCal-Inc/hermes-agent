"""Execution contract rules 1 + 2: a mutating tool call from a dispatcher-owned
Kanban worker requires a valid, currently-active task contract, re-checked
live on every call.

Pins: (a) interactive sessions (no HERMES_KANBAN_TASK) are untouched;
(b) a worker with a complete contract whose task is running under its lock is
allowed; (c) a half-formed contract, a cancelled task, a foreign claim lock, or
a missing task row is refused; (d) read-only tools are never gated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import task_contract_gate as gate
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # A dispatched worker carries HERMES_KANBAN_DB (the highest-precedence
    # pin on the board path) and HERMES_KANBAN_BOARD. Drop both so this
    # fixture can never resolve to the operator's live board, even when the
    # module is run outside the repo's conftest sandbox. A sibling fixture
    # without this guard leaked a card onto the live board on 2026-10-07
    # (t_92521910 run 3496 -> t_3f7e43ae).
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (gate.ENV_TASK, gate.ENV_RUN_ID, gate.ENV_CLAIM_LOCK):
        monkeypatch.delenv(var, raising=False)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    gate.reset_cache()
    yield home
    gate.reset_cache()


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _running_task(conn):
    tid = kb.create_task(conn, title="gate", assignee="w")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    task = kb.claim_task(conn, tid, claimer="host:1")
    assert task is not None and task.status == "running"
    return task


def _env_for(task):
    return {
        gate.ENV_TASK: task.id,
        gate.ENV_RUN_ID: str(task.current_run_id),
        gate.ENV_CLAIM_LOCK: task.claim_lock or "",
    }


def test_interactive_session_is_not_gated(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    assert gate.task_contract_refusal("terminal", environ={}) is None


def test_read_only_tools_are_never_gated(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    env = {gate.ENV_TASK: "t_missing"}
    assert gate.task_contract_refusal("read_file", environ=env) is None


def test_running_task_under_own_lock_is_allowed(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    assert gate.task_contract_refusal("write_file", environ=_env_for(task)) is None


def test_incomplete_contract_is_refused(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = {gate.ENV_TASK: task.id}  # no run id, no claim lock
    reason = gate.task_contract_refusal("terminal", environ=env)
    assert reason is not None and "incomplete" in reason


def test_foreign_claim_lock_is_refused(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    env[gate.ENV_CLAIM_LOCK] = "other-host:999"
    reason = gate.task_contract_refusal("execute_code", environ=env)
    assert reason is not None and "another worker" in reason


def test_missing_task_row_is_refused(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    env = {gate.ENV_TASK: "t_nope", gate.ENV_RUN_ID: "1", gate.ENV_CLAIM_LOCK: "host:1"}
    reason = gate.task_contract_refusal("patch", environ=env)
    assert reason is not None and "does not exist" in reason


def test_cancelled_task_is_refused_on_first_check(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task.id,))
    reason = gate.task_contract_refusal("terminal", environ=_env_for(task))
    assert reason is not None and "not running" in reason


def test_delegated_child_context_is_not_gated(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: False)
    env = {gate.ENV_TASK: "t_nope", gate.ENV_RUN_ID: "1", gate.ENV_CLAIM_LOCK: "host:1"}
    assert gate.task_contract_refusal("terminal", environ=env) is None


def test_dispatcher_refuses_mutating_tool_without_contract(conn, monkeypatch):
    """End to end through handle_function_call: the refusal reaches the model."""
    import json

    import model_tools

    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    monkeypatch.setenv(gate.ENV_TASK, "t_nope")
    monkeypatch.setenv(gate.ENV_RUN_ID, "1")
    monkeypatch.setenv(gate.ENV_CLAIM_LOCK, "host:1")
    out = model_tools.handle_function_call("write_file", {"path": "x", "content": "y"})
    assert "task contract" in json.loads(out).get("error", "")


def test_rule2_cancellation_mid_run_refuses_next_mutating_call(conn, monkeypatch):
    """Rule 2 proper: the first call is allowed, the task is then cancelled by
    the owner, and the very next mutating call is refused without any cache
    reset or heartbeat in between."""
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    assert gate.task_contract_refusal("terminal", environ=env) is None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task.id,))
    reason = gate.task_contract_refusal("terminal", environ=env)
    assert reason is not None and "cancelled" in reason


def test_rule2_reclaim_mid_run_refuses_old_worker(conn, monkeypatch):
    """A reclaimed-and-reclaimed task carries a new claim_lock; the old
    worker's next outside change is refused on the live read."""
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    assert gate.task_contract_refusal("write_file", environ=env) is None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock = 'other:2' WHERE id = ?", (task.id,))
    reason = gate.task_contract_refusal("write_file", environ=env)
    assert reason is not None and "another worker" in reason


def test_rule2_expired_claim_is_refused(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task.id,))
    reason = gate.task_contract_refusal("execute_code", environ=env)
    assert reason is not None and "expired" in reason


# ---------------------------------------------------------------------------
# Phase 4 finding 2 (t_e8eb2485): board-mutating kanban_* tools are gated
# with the same live re-check; read-only kanban tools are not.
# ---------------------------------------------------------------------------

READ_ONLY_KANBAN_TOOLS = ("kanban_show", "kanban_list", "kanban_attachments", "kanban_lessons")


def test_board_mutating_kanban_set_is_disjoint_from_guardrail_set():
    """The kanban set must not leak into the guardrail set (other consumers)
    and must not be empty: both halves of the gate stay independently owned."""
    from agent.tool_guardrails import MUTATING_TOOL_NAMES

    assert gate.BOARD_MUTATING_KANBAN_TOOLS
    assert not (gate.BOARD_MUTATING_KANBAN_TOOLS & MUTATING_TOOL_NAMES)
    assert all(n.startswith("kanban_") for n in gate.BOARD_MUTATING_KANBAN_TOOLS)
    assert not (gate.BOARD_MUTATING_KANBAN_TOOLS & set(READ_ONLY_KANBAN_TOOLS))


def test_every_registered_kanban_tool_is_classified():
    """Invariant against the live registry: each kanban_* tool is either
    board-mutating (gated) or in the read-only list (ungated). A new kanban
    tool must be placed on one side deliberately."""
    import model_tools  # noqa: F401 - triggers tool discovery
    from tools.registry import registry

    registered = {n for n in registry.get_all_tool_names() if n.startswith("kanban_")}
    assert registered, "kanban tools did not register"
    unclassified = registered - gate.BOARD_MUTATING_KANBAN_TOOLS - set(READ_ONLY_KANBAN_TOOLS)
    assert not unclassified, f"unclassified kanban tools: {sorted(unclassified)}"


@pytest.mark.parametrize("tool", sorted(gate.BOARD_MUTATING_KANBAN_TOOLS))
def test_kanban_mutating_tool_allowed_under_live_claim(conn, monkeypatch, tool):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    assert gate.task_contract_refusal(tool, environ=_env_for(task)) is None


def test_kanban_mutating_tools_refused_after_cancellation(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    assert gate.task_contract_refusal("kanban_comment", environ=env) is None
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task.id,))
    for tool in ("kanban_comment", "kanban_complete", "kanban_create", "kanban_request_review"):
        reason = gate.task_contract_refusal(tool, environ=env)
        assert reason is not None and "cancelled" in reason, tool


def test_kanban_mutating_tools_refused_after_reclaim(conn, monkeypatch):
    """A reclaimed task carries a different claim_lock; the old worker may not
    complete, request review, or heartbeat it."""
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock = 'other:2' WHERE id = ?", (task.id,))
    for tool in ("kanban_complete", "kanban_request_review", "kanban_heartbeat", "kanban_block"):
        reason = gate.task_contract_refusal(tool, environ=env)
        assert reason is not None and "another worker" in reason, tool


def test_kanban_heartbeat_cannot_rearm_expired_claim(conn, monkeypatch):
    """An expired claim is a dead contract; the explicit heartbeat tool may not
    be used to extend it back to life (the dispatcher now owns the task)."""
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (task.id,))
    for tool in ("kanban_heartbeat", "kanban_attach", "kanban_promote_lesson"):
        reason = gate.task_contract_refusal(tool, environ=env)
        assert reason is not None and "expired" in reason, tool


@pytest.mark.parametrize("tool", READ_ONLY_KANBAN_TOOLS)
def test_read_only_kanban_tools_allowed_under_dead_contract(conn, monkeypatch, tool):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    env = _env_for(task)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task.id,))
    assert gate.task_contract_refusal(tool, environ=env) is None
    # ...and with no board row at all.
    missing = {gate.ENV_TASK: "t_nope", gate.ENV_RUN_ID: "1", gate.ENV_CLAIM_LOCK: "host:1"}
    assert gate.task_contract_refusal(tool, environ=missing) is None


@pytest.mark.parametrize("tool", sorted(gate.BOARD_MUTATING_KANBAN_TOOLS))
def test_kanban_tools_not_gated_outside_worker_process(conn, monkeypatch, tool):
    """No HERMES_KANBAN_TASK (interactive session, orchestrator profile with the
    kanban toolset) -> the gate does not apply, exactly as for terminal."""
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    assert gate.task_contract_refusal(tool, environ={}) is None


def test_kanban_tools_not_gated_in_delegated_child(conn, monkeypatch):
    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: False)
    env = {gate.ENV_TASK: "t_nope", gate.ENV_RUN_ID: "1", gate.ENV_CLAIM_LOCK: "host:1"}
    assert gate.task_contract_refusal("kanban_complete", environ=env) is None


def test_dispatcher_refuses_kanban_mutation_under_dead_contract(conn, monkeypatch):
    """End to end through handle_function_call: a cancelled worker's
    kanban_comment never reaches the kanban handler."""
    import json

    import model_tools

    monkeypatch.setattr(gate, "_is_dispatcher_owned", lambda: True)
    task = _running_task(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task.id,))
    monkeypatch.setenv(gate.ENV_TASK, task.id)
    monkeypatch.setenv(gate.ENV_RUN_ID, str(task.current_run_id))
    monkeypatch.setenv(gate.ENV_CLAIM_LOCK, task.claim_lock or "")
    out = model_tools.handle_function_call("kanban_comment", {"task_id": task.id, "body": "x"})
    err = json.loads(out).get("error", "")
    assert "task contract" in err and "cancelled" in err
    comments = conn.execute(
        "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (task.id,)
    ).fetchone()[0]
    assert comments == 0
