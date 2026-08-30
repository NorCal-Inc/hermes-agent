"""Tests for the explicit claude-first recovery lane.

Covers:

1. DB layer: executor_lane / recovery_gate_cmd validation, persistence
   through create_task + from_row, and legacy-DB migration — proving the
   new field is backward-compatible (NULL/absent = unchanged behaviour).
2. Tool + CLI exposure: the new fields are reachable from task creation
   surfaces (kanban_create tool schema, `hermes kanban create` flags).
3. recovery_lane.run_claude_first_recovery: Claude runs before Codex, Codex
   only runs after a mechanical (never self-reported) gate failure, each
   gets exactly one bounded attempt, and both completion and escalation
   outcomes call the right kanban_db primitives.
4. cli.py source-order regression: the recovery-lane bypass in the
   dispatcher-spawned worker's single-query path must appear, and must
   appear strictly before the calls that build/run the normal Hermes
   agent/tool loop (_init_agent / run_conversation) — proving an explicit
   recovery task never reaches the Hermes worker loop.
5. Normal (non-recovery) tasks are unaffected: dependency promotion via
   complete_task's recompute_ready still works exactly as before.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def test_recovery_lane_requires_gate_cmd(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="recovery_gate_cmd is required"):
            kb.create_task(
                conn, title="fix it", assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
            )


def test_gate_cmd_requires_recovery_lane(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="only valid with"):
            kb.create_task(
                conn, title="fix it", assignee="default",
                recovery_gate_cmd="pytest -q",
            )


def test_invalid_executor_lane_rejected(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="executor_lane must be one of"):
            kb.create_task(
                conn, title="fix it", assignee="default",
                executor_lane="not_a_real_lane",
                recovery_gate_cmd="pytest -q",
            )


def test_executor_lane_persists_through_create_and_get(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="fix broken gate", assignee="default",
            executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
            recovery_gate_cmd="pytest -q tests/test_gate.py",
        )
        task = kb.get_task(conn, tid)
    assert task.executor_lane == "claude_recovery"
    assert task.recovery_gate_cmd == "pytest -q tests/test_gate.py"


def test_direct_claude_lane_normalizes_to_default_carrier(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="audit skills", assignee="overall_manager",
            executor_lane=kb.EXECUTOR_LANE_CLAUDE,
        )
        task = kb.get_task(conn, tid)
    assert task.executor_lane == "claude"
    assert task.assignee == "default"
    assert task.recovery_gate_cmd is None


def test_assignee_claude_is_shorthand_for_direct_lane(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="audit skills", assignee="claude")
        task = kb.get_task(conn, tid)
    assert task.executor_lane == "claude"
    assert task.assignee == "default"


def test_recovery_gate_cmd_rejected_on_direct_claude_lane(kanban_home):
    with kb.connect() as conn:
        with pytest.raises(ValueError, match="only valid with"):
            kb.create_task(
                conn, title="audit skills", assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE,
                recovery_gate_cmd="pytest -q",
            )


def test_normal_task_defaults_executor_lane_none(kanban_home):
    """Backward compatibility: existing callers that never set the new
    fields get identical behaviour to before this change."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ordinary task", assignee="default")
        task = kb.get_task(conn, tid)
    assert task.executor_lane is None
    assert task.recovery_gate_cmd is None


def test_legacy_db_migrates_executor_lane_columns(tmp_path, monkeypatch):
    """A tasks table created before this change must gain the new columns
    on init, with existing rows defaulting to the pre-existing behaviour."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "executor_lane" in cols
        assert "recovery_gate_cmd" in cols
        task = kb.get_task(conn, "legacy1")
    assert task.executor_lane is None
    assert task.recovery_gate_cmd is None


# ---------------------------------------------------------------------------
# Creation surfaces (tool schema + CLI flags)
# ---------------------------------------------------------------------------


def test_kanban_create_tool_schema_exposes_executor_lane():
    from tools.kanban_tools import KANBAN_CREATE_SCHEMA

    props = KANBAN_CREATE_SCHEMA["parameters"]["properties"]
    assert "executor_lane" in props
    assert "claude" in props["executor_lane"]["enum"]
    assert "claude_recovery" in props["executor_lane"]["enum"]
    assert "recovery_gate_cmd" in props


def test_kanban_cli_create_parser_has_executor_lane_flags():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="kanban_action")
    kc.build_parser(sub)

    args = parser.parse_args([
        "kanban", "create", "fix the gate",
        "--assignee", "default",
        "--executor-lane", "claude_recovery",
        "--recovery-gate-cmd", "pytest -q",
    ])
    assert args.executor_lane == "claude_recovery"
    assert args.recovery_gate_cmd == "pytest -q"


def test_kanban_cli_create_accepts_direct_claude_lane():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="kanban_action")
    kc.build_parser(sub)

    args = parser.parse_args([
        "kanban", "create", "audit skills",
        "--assignee", "default",
        "--executor-lane", "claude",
    ])
    assert args.executor_lane == "claude"
    assert args.recovery_gate_cmd is None


def test_kanban_cli_create_defaults_executor_lane_none():
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="kanban_action")
    kc.build_parser(sub)

    args = parser.parse_args(["kanban", "create", "ordinary task", "--assignee", "default"])
    assert args.executor_lane is None
    assert args.recovery_gate_cmd is None


# ---------------------------------------------------------------------------
# recovery_lane.run_claude_executor — ordinary direct Claude lane
# ---------------------------------------------------------------------------


def test_direct_claude_executor_completes_with_attachment(monkeypatch):
    task = _make_recovery_task(
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
        recovery_gate_cmd=None,
        body="Read-only audit. Attach the final brief as a file.",
        workspace_kind="scratch",
        workspace_path="/tmp/claude-direct-test",
    )
    conn = object()
    complete_calls = []
    block_calls = []
    comments = []
    monkeypatch.setattr(recovery_lane.kb, "connect", lambda: conn)
    monkeypatch.setattr(recovery_lane.kb, "get_task", lambda c, tid: task)
    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, tid, rid: True)
    monkeypatch.setattr(recovery_lane, "_snapshot_workspace", lambda cwd: {})
    monkeypatch.setattr(
        recovery_lane, "_invoke_claude",
        lambda prompt, cwd, timeout, *, task_id=None: recovery_lane.AttemptResult(
            "claude", 0, '{"result":"Audit finished with evidence."}', "",
            execution_id="x_fake_claude",
            execution_status=recovery_lane.ex.STATUS_COMPLETED,
        ),
    )
    monkeypatch.setattr(
        recovery_lane, "_changed_workspace_files",
        lambda cwd, before: [Path("/tmp/claude-direct-test/skill-audit.md")],
    )
    monkeypatch.setattr(
        recovery_lane, "_attach_changed_files",
        lambda c, tid, files: ["/tmp/claude-direct-test/skill-audit.md"],
    )
    monkeypatch.setattr(
        recovery_lane.kb, "complete_task",
        lambda c, tid, **kw: complete_calls.append(kw) or True,
    )
    monkeypatch.setattr(
        recovery_lane.kb, "block_task",
        lambda c, tid, **kw: block_calls.append(kw) or True,
    )
    monkeypatch.setattr(
        recovery_lane.kb, "add_comment",
        lambda c, tid, author, body: comments.append((author, body)) or 1,
    )

    rc = recovery_lane.run_claude_executor("t_recover")

    assert rc == 0
    assert len(complete_calls) == 1
    assert block_calls == []
    assert complete_calls[0]["metadata"]["executor_lane"] == "claude"
    assert complete_calls[0]["metadata"]["attached_files"] == [
        "/tmp/claude-direct-test/skill-audit.md"
    ]


def test_register_attachment_dir_files_makes_files_first_class(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="artifact registration", assignee="default")
        root = kb.task_attachments_dir(tid)
        root.mkdir(parents=True, exist_ok=True)
        artifact = root / "brief.md"
        artifact.write_text("audit evidence\n", encoding="utf-8")

        registered = recovery_lane._register_attachment_dir_files(conn, tid)
        attachments = kb.list_attachments(conn, tid)

    assert registered == [str(artifact.resolve())]
    assert len(attachments) == 1
    assert attachments[0].filename == "brief.md"
    assert Path(attachments[0].stored_path).resolve() == artifact.resolve()
    assert attachments[0].size == artifact.stat().st_size


def test_direct_claude_executor_accepts_existing_registered_attachment(monkeypatch):
    task = _make_recovery_task(
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
        recovery_gate_cmd=None,
        body="Attach the final audit brief.",
        workspace_kind="scratch",
        workspace_path="/tmp/claude-direct-test",
    )
    conn = object()
    complete_calls = []
    block_calls = []
    monkeypatch.setattr(recovery_lane.kb, "connect", lambda: conn)
    monkeypatch.setattr(recovery_lane.kb, "get_task", lambda c, tid: task)
    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, tid, rid: True)
    monkeypatch.setattr(recovery_lane, "_snapshot_workspace", lambda cwd: {})
    monkeypatch.setattr(
        recovery_lane, "_invoke_claude",
        lambda prompt, cwd, timeout, *, task_id=None: recovery_lane.AttemptResult(
            "claude", 0, "done", "",
            execution_id="x_fake_claude",
            execution_status=recovery_lane.ex.STATUS_COMPLETED,
        ),
    )
    monkeypatch.setattr(recovery_lane, "_changed_workspace_files", lambda cwd, before: [])
    monkeypatch.setattr(recovery_lane, "_register_attachment_dir_files", lambda c, tid: [])
    monkeypatch.setattr(recovery_lane.kb, "list_attachments", lambda c, tid: [object()])
    monkeypatch.setattr(
        recovery_lane.kb, "complete_task",
        lambda c, tid, **kw: complete_calls.append(kw) or True,
    )
    monkeypatch.setattr(
        recovery_lane.kb, "block_task",
        lambda c, tid, **kw: block_calls.append(kw) or True,
    )
    monkeypatch.setattr(recovery_lane.kb, "add_comment", lambda *a, **k: 1)

    rc = recovery_lane.run_claude_executor("t_recover")

    assert rc == 0
    assert len(complete_calls) == 1
    assert block_calls == []


def test_direct_claude_executor_blocks_when_required_attachment_missing(monkeypatch):
    task = _make_recovery_task(
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
        recovery_gate_cmd=None,
        body="Attach the final audit brief.",
        workspace_kind="scratch",
        workspace_path="/tmp/claude-direct-test",
    )
    conn = object()
    complete_calls = []
    block_calls = []
    monkeypatch.setattr(recovery_lane.kb, "connect", lambda: conn)
    monkeypatch.setattr(recovery_lane.kb, "get_task", lambda c, tid: task)
    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, tid, rid: True)
    monkeypatch.setattr(recovery_lane, "_snapshot_workspace", lambda cwd: {})
    monkeypatch.setattr(
        recovery_lane, "_invoke_claude",
        lambda prompt, cwd, timeout, *, task_id=None: recovery_lane.AttemptResult(
            "claude", 0, "done", "",
            execution_id="x_fake_claude",
            execution_status=recovery_lane.ex.STATUS_COMPLETED,
        ),
    )
    monkeypatch.setattr(recovery_lane, "_changed_workspace_files", lambda cwd, before: [])
    monkeypatch.setattr(
        recovery_lane.kb, "complete_task",
        lambda c, tid, **kw: complete_calls.append(kw) or True,
    )
    monkeypatch.setattr(
        recovery_lane.kb, "block_task",
        lambda c, tid, **kw: block_calls.append(kw) or True,
    )
    monkeypatch.setattr(recovery_lane.kb, "add_comment", lambda *a, **k: 1)

    rc = recovery_lane.run_claude_executor("t_recover")

    assert rc == 0
    assert complete_calls == []
    assert len(block_calls) == 1
    assert "required an attached artifact" in block_calls[0]["reason"]


# ---------------------------------------------------------------------------
# recovery_lane.run_claude_first_recovery — mechanical harness
# ---------------------------------------------------------------------------


def _make_recovery_task(**overrides):
    fields = dict(
        id="t_recover",
        title="pipeline broken",
        body="tests/test_gate.py is failing on main",
        assignee="default",
        status="running",
        priority=0,
        created_by="dispatcher",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="dir",
        workspace_path="/tmp/does-not-matter",
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
        recovery_gate_cmd="pytest -q tests/test_gate.py",
        max_runtime_seconds=None,
        current_run_id=42,
    )
    fields.update(overrides)
    return kb.Task(**fields)


class _Harness:
    """Wires recovery_lane's kb.* and subprocess-invoking calls to scripted
    fakes and records call order, without touching a real DB or spawning a
    real claude/codex process."""

    def __init__(self, monkeypatch, task, gate_results, already_started=None):
        self.calls: list[str] = []
        self.complete_calls: list[dict] = []
        self.block_calls: list[dict] = []
        self.comment_calls: list[dict] = []
        self._gate_results = list(gate_results)
        self.started = set(already_started or [])

        monkeypatch.setattr(recovery_lane.kb, "connect", lambda: object())
        monkeypatch.setattr(recovery_lane.kb, "get_task", lambda conn, tid: task)

        def fake_claim_attempt(conn, tid, kind, run_id):
            if kind in self.started:
                return False
            self.started.add(kind)
            return True

        monkeypatch.setattr(recovery_lane, "_claim_attempt", fake_claim_attempt)

        # The lane now launches every external process through the execution
        # supervisor, so these doubles stand in for a SUPERVISED attempt:
        # they take the ``task_id`` the lane threads through for the execution
        # record, and a successful one has to carry the supervisor status as
        # well as the exit code. That is not test bookkeeping — ``AttemptResult
        # .ok`` deliberately refuses to read a bare rc=0 as success, because a
        # controller-lost or terminated execution can leave one behind. A fake
        # that could still claim success with an exit code alone would be
        # faking the exact thing the supervisor exists to distinguish.
        def fake_invoke_claude(prompt, cwd, timeout, *, task_id=None):
            self.calls.append("claude")
            return recovery_lane.AttemptResult(
                "claude", 0, "claude did work", "",
                execution_id="x_fake_claude",
                execution_status=recovery_lane.ex.STATUS_COMPLETED,
            )

        def fake_invoke_codex(prompt, cwd, timeout, *, task_id=None):
            self.calls.append("codex")
            return recovery_lane.AttemptResult(
                "codex", 0, "codex did work", "",
                execution_id="x_fake_codex",
                execution_status=recovery_lane.ex.STATUS_COMPLETED,
            )

        def fake_run_gate(cmd, cwd, timeout, *, task_id=None):
            self.calls.append("gate")
            ok = self._gate_results.pop(0)
            return recovery_lane.GateResult(ok, 0 if ok else 1, "gate output")

        def fake_complete_task(conn, tid, **kw):
            self.complete_calls.append(kw)
            return True

        def fake_block_task(conn, tid, **kw):
            self.block_calls.append(kw)
            return True

        def fake_add_comment(conn, tid, author, body):
            self.comment_calls.append({"author": author, "body": body})
            return 1

        monkeypatch.setattr(recovery_lane, "_invoke_claude", fake_invoke_claude)
        monkeypatch.setattr(recovery_lane, "_invoke_codex", fake_invoke_codex)
        monkeypatch.setattr(recovery_lane, "_run_gate", fake_run_gate)
        monkeypatch.setattr(recovery_lane.kb, "complete_task", fake_complete_task)
        monkeypatch.setattr(recovery_lane.kb, "block_task", fake_block_task)
        monkeypatch.setattr(recovery_lane.kb, "add_comment", fake_add_comment)


def test_gate_already_green_on_resume_invokes_no_executor(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(monkeypatch, task, gate_results=[True])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 0
    assert h.calls == ["gate"], "a green resume gate must not consume Claude or Codex"
    assert len(h.complete_calls) == 1
    assert len(h.block_calls) == 0


def test_gate_green_after_claude_never_invokes_codex(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(monkeypatch, task, gate_results=[False, True])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 0
    assert h.calls == ["gate", "claude", "gate"], "must not invoke codex once Claude makes the gate green"
    assert len(h.complete_calls) == 1
    assert len(h.block_calls) == 0


def test_codex_only_invoked_after_claude_gate_failure(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(monkeypatch, task, gate_results=[False, False, True])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 0
    assert h.calls == ["gate", "claude", "gate", "codex", "gate"], (
        "resume gate runs first; codex only after Claude and the mechanical "
        "post-Claude gate check both leave the gate red"
    )
    assert len(h.complete_calls) == 1
    assert len(h.block_calls) == 0


def test_both_fail_escalates_exactly_once_no_retries(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(monkeypatch, task, gate_results=[False, False, False])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 0
    assert h.calls == ["gate", "claude", "gate", "codex", "gate"]
    assert h.calls.count("claude") == 1, "claude gets exactly one bounded attempt"
    assert h.calls.count("codex") == 1, "codex gets exactly one bounded attempt"
    assert len(h.complete_calls) == 0
    assert len(h.block_calls) == 1, "exactly one escalation, no retry loop"
    assert len(h.comment_calls) >= 1


def test_gate_is_reverified_mechanically_not_trusted_from_self_report(monkeypatch):
    """Even though both _invoke_claude and _invoke_codex fakes return
    returncode=0 ("I succeeded"), the harness must still treat the outcome
    as failure because the gate command itself reports red."""
    task = _make_recovery_task()
    h = _Harness(monkeypatch, task, gate_results=[False, False, False])

    recovery_lane.run_claude_first_recovery("t_recover")

    assert len(h.block_calls) == 1
    assert len(h.complete_calls) == 0


def test_missing_gate_cmd_blocks_defensively_without_invoking_either_cli(monkeypatch):
    task = _make_recovery_task(recovery_gate_cmd=None)
    h = _Harness(monkeypatch, task, gate_results=[])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 0
    assert h.calls == []
    assert len(h.block_calls) == 1


def test_non_recovery_task_is_refused_defensively(monkeypatch):
    task = _make_recovery_task(executor_lane=None)
    h = _Harness(monkeypatch, task, gate_results=[])

    rc = recovery_lane.run_claude_first_recovery("t_recover")

    assert rc == 1
    assert h.calls == []
    assert len(h.complete_calls) == 0
    assert len(h.block_calls) == 0


# ---------------------------------------------------------------------------
# cli.py source-order regression: recovery lane must precede the normal
# Hermes worker loop (_init_agent / run_conversation) for the dispatcher's
# single-query worker path — this is the actual defect being fixed.
# ---------------------------------------------------------------------------


def test_cli_recovery_bypass_precedes_hermes_worker_loop():
    cli_path = Path(__file__).resolve().parents[2] / "cli.py"
    source = cli_path.read_text(encoding="utf-8")

    anchor = source.index(
        '_kanban_task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()'
    )
    direct_check = source.index(
        "_lane_task.executor_lane == _kb_lane.EXECUTOR_LANE_CLAUDE",
        anchor,
    )
    direct_invoke = source.index("run_claude_executor(_kanban_task_id)", direct_check)
    direct_exit = source.index("sys.exit(_lane_rc)", direct_invoke)
    lane_check = source.index(
        "_lane_task.executor_lane == _kb_lane.EXECUTOR_LANE_CLAUDE_RECOVERY",
        direct_exit,
    )
    lane_invoke = source.index("run_claude_first_recovery(_kanban_task_id)", lane_check)
    lane_exit = source.index("sys.exit(_lane_rc)", lane_invoke)

    next_init_agent = source.index("cli._init_agent(", lane_exit)
    next_run_conversation = source.index("cli.agent.run_conversation(", lane_exit)

    assert direct_check < direct_invoke < direct_exit < lane_check, (
        "the ordinary Claude executor must exit before the recovery/normal Hermes paths"
    )
    assert lane_check < lane_invoke < lane_exit < next_init_agent, (
        "the recovery-lane bypass must mechanically exit before the worker "
        "ever calls _init_agent — no Hermes agent/system-prompt/toolset may "
        "be built for an explicit recovery task"
    )
    assert lane_exit < next_run_conversation, (
        "the recovery-lane bypass must exit before run_conversation — no "
        "Hermes reasoning/tool loop may run for an explicit recovery task"
    )


def test_recovery_lane_module_has_no_hermes_agent_loop_coupling():
    """The recovery lane itself must never construct a Hermes agent or
    reasoning loop — it is pure subprocess + kanban_db plumbing."""
    import inspect

    source = inspect.getsource(recovery_lane)
    for forbidden in ("AIAgent(", "run_conversation(", "_init_agent(", "conversation_loop"):
        assert forbidden not in source, (
            f"recovery_lane.py must not reference {forbidden!r} — it is a "
            "mechanical bypass of the Hermes worker loop, not a caller of it"
        )


# ---------------------------------------------------------------------------
# Normal Kanban behaviour unchanged: dependency promotion still works for
# both ordinary tasks and completion via the recovery lane's direct
# complete_task call (which must still drive recompute_ready).
# ---------------------------------------------------------------------------


def test_dependency_promotion_unaffected_by_new_field(kanban_home):
    with kb.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="default")
        child_id = kb.create_task(
            conn, title="child", assignee="default", parents=[parent_id],
        )
        child = kb.get_task(conn, child_id)
        assert child.status == "todo", "child must wait on its parent, as before"

        assert kb.complete_task(conn, parent_id, result="done")

        child = kb.get_task(conn, child_id)
        assert child.status == "ready", "recompute_ready must still promote the child"


def test_recovery_lane_completion_promotes_dependents(kanban_home):
    """The recovery lane calls kb.complete_task directly (bypassing the
    worker tool layer) — that call must still drive recompute_ready so
    dependent tasks promote, exactly like a normal worker's completion."""
    with kb.connect() as conn:
        parent_id = kb.create_task(
            conn, title="fix the gate", assignee="default",
            executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
            recovery_gate_cmd="true",
        )
        child_id = kb.create_task(
            conn, title="depends on recovery", assignee="default", parents=[parent_id],
        )
        assert kb.get_task(conn, child_id).status == "todo"

        assert kb.complete_task(
            conn, parent_id, result="Recovery lane: gate green.",
            metadata={"recovery_lane": "claude_recovery"},
        )

        assert kb.get_task(conn, child_id).status == "ready"


def test_restart_skips_already_recorded_claude_attempt(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(
        monkeypatch, task, gate_results=[False, False, True],
        already_started={"recovery_claude_started"},
    )
    rc = recovery_lane.run_claude_first_recovery("t_recover")
    assert rc == 0
    assert h.calls == ["gate", "gate", "codex", "gate"]


def test_restart_after_both_attempts_runs_neither_again(monkeypatch):
    task = _make_recovery_task()
    h = _Harness(
        monkeypatch, task, gate_results=[False, False, False],
        already_started={"recovery_claude_started", "recovery_codex_started"},
    )
    rc = recovery_lane.run_claude_first_recovery("t_recover")
    assert rc == 0
    assert h.calls == ["gate", "gate", "gate"]
    assert len(h.block_calls) == 1


def test_gate_parser_rejects_shell_operators():
    assert recovery_lane._gate_argv("/bin/true --flag") == ["/bin/true", "--flag"]
    with pytest.raises(ValueError, match="shell operators"):
        recovery_lane._gate_argv("/bin/true && rm -rf /tmp/x")


def test_attempt_marker_is_durable_and_stale_run_is_rejected(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="recovery", assignee="default",
            executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
            recovery_gate_cmd="/bin/true",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running', current_run_id=42 WHERE id=?",
                (tid,),
            )
        assert recovery_lane._claim_attempt(
            conn, tid, "recovery_claude_started", 42
        ) is True
        assert recovery_lane._claim_attempt(
            conn, tid, "recovery_claude_started", 42
        ) is False
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET current_run_id=43 WHERE id=?", (tid,))
        with pytest.raises(RuntimeError, match="stale recovery run"):
            recovery_lane._claim_attempt(
                conn, tid, "recovery_codex_started", 42
            )


def test_telegram_notify_wake_inherits_to_dependent_continuation(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="recovery", assignee="default")
        kb.add_notify_sub(
            conn, task_id=parent, platform="telegram", chat_id="-1003809943982",
            chat_type="group", notifier_profile="overall_manager",
            delivery_mode="notify+wake",
        )
        child = kb.create_task(
            conn, title="parked continuation", assignee="paris_worker",
            parents=[parent],
        )
        rows = kb.list_notify_subs(conn, child)
    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == "telegram"
    assert row["chat_id"] == "-1003809943982"
    assert row["notifier_profile"] == "overall_manager"
    assert row["delivery_mode"] == "notify+wake"
