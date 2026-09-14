"""Per-objective operator continuation grants for the attempt ceiling (2026-09-14).

``t_b8d62378`` exhausted its six-run objective budget on a stale-release loop.
The only ways on were raising the board-wide limit or editing the database, and
``unblock`` simply re-blocked at the next claim. A grant is one finite, audited
operator decision for one objective lineage; the base budget stays six and the
effective ceiling is derived, never stored.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

OPERATOR = {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "christopher"}
REASON = "stale-loop defect consumed the objective budget; finish evidence handoff and Phase D"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (*kb._GOVERNED_RUN_ENV_MARKERS, "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _spend(conn, tid, n):
    with kb.write_txn(conn):
        for i in range(n):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'default', 'timed_out', ?, ?, 'timed_out')",
                (tid, 100 + i, 101 + i),
            )


def _exhausted(conn, title="objective", **kw):
    tid = kb.create_task(conn, title=title, assignee="default", **kw)
    _spend(conn, tid, kb.GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT)
    assert kb.claim_task(conn, tid) is None
    assert kb.get_task(conn, tid).block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
    return tid


def _grant(conn, tid, n=3, env=OPERATOR, **kw):
    return kb.grant_objective_attempts(
        conn, tid, added_attempts=n, authorized_by=kw.pop("authorized_by", "Christopher"),
        reason=kw.pop("reason", REASON), env=env, **kw,
    )


def _grant_events(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE kind = ?", (kb.OBJECTIVE_ATTEMPT_GRANT_EVENT,)
    ).fetchone()[0]


def test_base_budget_is_six():
    assert kb.GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT == 6


# 1 ------------------------------------------------------------------------------
def test_exhausted_objective_cannot_continue_through_ordinary_unblock(kanban_home):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        for _ in range(2):
            assert kb.unblock_task(conn, tid)
            assert kb.claim_task(conn, tid) is None
            task = kb.get_task(conn, tid)
            assert task.status == "blocked"
            assert task.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert kb.gauntlet_objective_attempts(conn, tid) == 6
        assert kb.effective_objective_attempt_limit(conn, tid) == 6


# 2 + 3 --------------------------------------------------------------------------
def test_grant_applies_only_to_the_named_lineage_and_unrelated_tasks_stay_at_six(kanban_home):
    with kb.connect_closing() as conn:
        granted = _exhausted(conn, title="granted objective")
        other = _exhausted(conn, title="other exhausted objective")
        fresh = kb.create_task(conn, title="unrelated", assignee="default")
        payload = _grant(conn, granted)
        assert payload["objective"] == granted and payload["new_effective_limit"] == 9
        assert kb.effective_objective_attempt_limit(conn, granted) == 9
        assert kb.effective_objective_attempt_limit(conn, other) == 6
        assert kb.effective_objective_attempt_limit(conn, fresh) == 6
        assert kb.gauntlet_objective_attempt_limit() == 6
        assert kb.unblock_task(conn, other)
        assert kb.claim_task(conn, other) is None
        assert kb.unblock_task(conn, granted)
        assert kb.claim_task(conn, granted) is not None


# 4 + 6 --------------------------------------------------------------------------
def test_history_keeps_counting_and_a_second_exhaustion_blocks_again(kanban_home):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        _grant(conn, tid)
        assert kb.gauntlet_objective_attempts(conn, tid) == 6
        _spend(conn, tid, 2)                          # attempts 7 and 8
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid) is not None   # attempt 9 is allowed
        assert kb.gauntlet_objective_attempts(conn, tid) == 9
        assert kb.block_task(conn, tid, reason="bounded correction failed", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        ceiling = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='objective_attempt_ceiling_reached' "
            "ORDER BY id", (tid,))]
        assert ceiling[-1]["attempts"] == 9 and ceiling[-1]["limit"] == 9


# 5 ------------------------------------------------------------------------------
@pytest.mark.parametrize("env, who", [
    ({**OPERATOR, "HERMES_KANBAN_TASK": "t_x"}, "Christopher"),
    ({**OPERATOR, "HERMES_KANBAN_RUN_ID": "7"}, "Christopher"),
    ({**OPERATOR, "HERMES_EXECUTION_ID": "x_1"}, "Christopher"),
    ({**OPERATOR, "HERMES_EXECUTOR_LANE": kb.EXECUTOR_LANE_CODEX_VERIFY}, "Christopher"),
    ({}, "Christopher"),                                                    # absent provenance
    ({kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE}, "Christopher"),  # no named actor
    ({kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_GOVERNED_AUTOMATION, kb.ENV_ACTOR_ID: "christopher"}, "Christopher"),
    ({kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_SYSTEM, kb.ENV_ACTOR_ID: "christopher"}, "Christopher"),
    ({kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "default"}, "Christopher"),
    (OPERATOR, "default"),
    (OPERATOR, kb.EXECUTOR_LANE_CODEX_VERIFY),
    (OPERATOR, "atlas"),
    (OPERATOR, "claude-lane"),
    (OPERATOR, "stale-supervision"),
    (OPERATOR, "system-health-controller"),
    (OPERATOR, "erika"),                                                    # a worker profile
])
def test_automation_cannot_grant_itself_attempts(kanban_home, env, who):
    if who == "erika":
        (kanban_home / "profiles" / "erika").mkdir(parents=True)
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        with pytest.raises(kb.ObjectiveAttemptGrantRefused):
            _grant(conn, tid, env=env, authorized_by=who)
        assert _grant_events(conn, tid) == 0
        assert kb.effective_objective_attempt_limit(conn, tid) == 6


@pytest.mark.parametrize("added", [0, -1, kb.OBJECTIVE_ATTEMPT_GRANT_MAX + 1, True, "3", 2.5])
def test_every_grant_is_finite_and_explicit(kanban_home, added):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        with pytest.raises(kb.ObjectiveAttemptGrantRefused):
            kb.grant_objective_attempts(conn, tid, added_attempts=added, authorized_by="Christopher",
                                        reason=REASON, env=OPERATOR)
        with pytest.raises(kb.ObjectiveAttemptGrantRefused):
            _grant(conn, tid, reason="  ")
        assert _grant_events(conn, tid) == 0


def test_a_live_execution_descendant_cannot_grant_even_with_a_clean_env(kanban_home):
    from hermes_cli import exec_supervisor as ex

    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        execution = ex.create_execution(conn, executor_type="claude", command_class="claude.headless",
                                        cwd=str(kanban_home), task_id=tid, max_runtime_s=300)
        with kb.write_txn(conn):
            conn.execute("UPDATE executions SET pid = ?, ended_at = NULL WHERE id = ?",
                         (os.getppid(), execution.id))
        with pytest.raises(kb.ObjectiveAttemptGrantRefused, match="live execution"):
            _grant(conn, tid)
        assert _grant_events(conn, tid) == 0


def test_no_worker_or_supervision_surface_can_reach_the_grant():
    root = Path(kb.__file__).resolve().parents[1]
    for rel in ("tools/kanban_tools.py", "hermes_cli/recovery_lane.py",
                "norcal/health/system_health_controller.py"):
        path = root / rel
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert "grant_objective_attempts" not in text and "attempt-budget" not in text, rel
    source = Path(kb.__file__).read_text(encoding="utf-8")
    calls = [line for line in source.splitlines()
             if "grant_objective_attempts(" in line and not line.lstrip().startswith("def ")]
    assert calls == []


# 7 ------------------------------------------------------------------------------
def test_grant_audit_event_is_complete_and_survives_completion(kanban_home):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        _grant(conn, tid, now=1_789_430_000)
        assert kb.get_task(conn, tid).status == "blocked"   # granting never resumes
        assert kb.unblock_task(conn, tid)
        assert kb.complete_task(conn, tid, result="done")
        assert kb.get_task(conn, tid).status == "done"
        (grant,) = kb.objective_attempt_grants(conn, tid)
    assert {k: grant[k] for k in grant if k != "event_id"} == {
        "objective": tid, "subject_requested": tid, "added_attempts": 3, "attempts_before": 6,
        "base_limit": 6, "prior_effective_limit": 6, "new_effective_limit": 9,
        "authorized_by": "Christopher", "reason": REASON, "actor_kind": kb.ACTOR_KIND_HUMAN_INTERACTIVE,
        "actor_id": "christopher", "authorization_source": "hermes kanban attempt-budget --grant",
        "granted_at": 1_789_430_000,
    }


def test_granting_changes_no_card_state(kanban_home):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
        before = tuple(conn.execute("SELECT status, block_kind, claim_lock, current_run_id, assignee "
                                    "FROM tasks WHERE id=?", (tid,)).fetchone())
        runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        _grant(conn, tid)
        assert tuple(conn.execute("SELECT status, block_kind, claim_lock, current_run_id, assignee "
                                  "FROM tasks WHERE id=?", (tid,)).fetchone()) == before
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == runs
        kinds = [k for (k,) in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,))]
        assert kinds == [kb.OBJECTIVE_ATTEMPT_GRANT_EVENT]


# 8 ------------------------------------------------------------------------------
def test_verifier_children_consume_the_extended_lineage_budget(kanban_home):
    with kb.connect_closing() as conn:
        subject = kb.create_task(conn, title="subject", assignee="default", gauntlet=True)
        verifier = kb.create_task(conn, title="verify", assignee="atlas", parents=[subject], gauntlet=True)
        _spend(conn, subject, 6)
        payload = _grant(conn, verifier)                 # requested on the child...
        assert payload["objective"] == subject          # ...bound to the objective root
        assert payload["subject_requested"] == verifier
        assert kb.effective_objective_attempt_limit(conn, verifier) == 9
        assert kb.effective_objective_attempt_limit(conn, subject) == 9
        _spend(conn, verifier, 3)
        assert kb.gauntlet_objective_attempts(conn, verifier) == 9
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (verifier,))
        assert kb.claim_task(conn, verifier) is None
        assert kb.get_task(conn, verifier).block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED


def test_cli_grant_records_one_grant_and_refuses_inside_a_governed_run(kanban_home, monkeypatch, capsys):
    with kb.connect_closing() as conn:
        tid = _exhausted(conn)
    args = argparse.Namespace(task_id=tid, grant=3, authorized_by="Christopher", reason=REASON, json=True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    for key, value in OPERATOR.items():
        monkeypatch.setenv(key, value)
    assert kc._cmd_attempt_budget(args) == 2
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert kc._cmd_attempt_budget(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["effective_limit"] == 9 and report["granted_now"]["added_attempts"] == 3
    show = argparse.Namespace(task_id=tid, grant=None, authorized_by=None, reason=None, json=True)
    assert kc._cmd_attempt_budget(show) == 0
    assert len(json.loads(capsys.readouterr().out)["grants"]) == 1
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
