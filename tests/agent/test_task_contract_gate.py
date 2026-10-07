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
