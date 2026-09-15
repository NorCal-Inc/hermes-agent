"""Approved per-card runtime caps above the global execution ceiling.

Christopher, 2026-09-15:

    "Set the normal global execution ceiling to 300 seconds, but make the
     smallest tested code change necessary so explicitly authorized per-card
     runtime caps can exceed that baseline ... A card explicitly authorized for
     600, 900, or another recorded human-approved cap must keep that cap. An
     ordinary card with no approved override must be limited to 300."

Threat model (Christopher, 2026-09-15): an unapproved card cannot obtain a
higher cap through Hermes-supported worker tools, CLI paths, board functions or
normal execution/event APIs without a valid human approval. Direct edits to
``kanban.db``, source or approval records by another process running as the
same OS user are out of scope.

``execution.max_runtime_seconds`` clamps every supervised execution
(``ExecutionPolicy.resolve_max_runtime``). Before this change that clamp also
cut the ladder's own rungs. These tests pin the baseline at 300 and check the
runtime a card's supervised execution is actually granted, through the real
recovery lane and the real policy resolution. The forgery cases come from the
independent verification that failed the first candidate
(x_076804477eae5e31, 2026-09-15).
"""
import json
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import exec_supervisor as ex
from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane

BASELINE = 300
OPERATOR = {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "christopher"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_EXECUTION_ID",
        "HERMES_SESSION_ID", "HERMES_EXECUTOR_LANE", "HERMES_PROFILE",
        "HERMES_PROFILE_NAME", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID,
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    (home / "profiles" / "erika").mkdir(parents=True)
    return home


@pytest.fixture
def workroot(kanban_home: Path) -> Path:
    root = kanban_home / "work"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def baseline(workroot: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """The live ``execution`` config block as it will read after the change."""
    block = {
        "max_runtime_seconds": BASELINE,
        "allowed_roots": [str(workroot)],
        "sync_ceiling_seconds": 3600,
        "stale_heartbeat_seconds": 3600,
    }
    monkeypatch.setattr(ex, "_exec_config", lambda: dict(block))
    return BASELINE


pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


def _card(conn, *, cap=None, lane=kb.EXECUTOR_LANE_CLAUDE):
    tid = kb.create_task(
        conn, title="a long job", body="", assignee="default",
        max_runtime_seconds=cap,
    )
    tid = tid if isinstance(tid, str) else tid["id"]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET executor_lane=? WHERE id=?", (lane, tid))
    return tid


def _approve(conn, tid, seconds, *, approved_by="christopher", reason="approved in session", env=None):
    return kb.approve_runtime_cap(
        conn, tid, seconds, approved_by=approved_by, reason=reason,
        env=OPERATOR if env is None else env,
    )


def _timeouts(conn, tid, cap, n=2):
    with kb.write_txn(conn):
        for _ in range(n):
            kb._append_event(conn, tid, "timed_out", {
                "elapsed_seconds": cap + 2, "limit_seconds": cap,
            })


def _ladder(conn, tid, cap):
    """Two timeouts at ``cap``, then the real ladder step."""
    _timeouts(conn, tid, cap)
    kb._runtime_cap_ladder_step(conn, tid, cap=cap)


def _forge(conn, tid, payload):
    """A hand-written approval record (only reachable outside supported interfaces)."""
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "runtime_cap_raised", payload)


def _cap(conn, tid):
    return conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id=?", (tid,)
    ).fetchone()["max_runtime_seconds"]


def _raise_events(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='runtime_cap_raised'", (tid,),
    ).fetchone()[0]


def _granted(captured):
    """Runtime the supervisor grants for one captured ``run_supervised`` call."""
    policy = captured.get("policy") or ex.load_policy()
    executor = ex.LAUNCHERS[captured["command_class"]].executor_type
    return policy.resolve_max_runtime(executor, captured["timeout"])


def _direct_claude_grant(monkeypatch, tid):
    """Run the real direct-Claude lane for ``tid``; return the granted runtime."""
    captured = []

    def fake_run_supervised(**kw):
        captured.append(kw)
        raise ex.ExecutionPolicyError("test_stop", "captured; attempt not launched")

    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, tid)
    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, t, r: True)
    monkeypatch.setattr(recovery_lane.ex, "run_supervised", fake_run_supervised)
    recovery_lane.run_claude_executor(tid)
    assert len(captured) == 1, "the lane must launch exactly one supervised attempt"
    return _granted(captured[0])


def _verifier_grant(monkeypatch, tid):
    captured = []

    def fake_run_supervised(**kw):
        captured.append(kw)
        raise ex.ExecutionPolicyError("test_stop", "captured")

    monkeypatch.setattr(recovery_lane.ex, "run_supervised", fake_run_supervised)
    with kb.connect_closing() as conn:
        timeout = int(_cap(conn, tid) or recovery_lane.DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
    recovery_lane._invoke_codex_verifier("verify", "/tmp", timeout, task_id=tid)
    return _granted(captured[0])


# ---------------------------------------------------------------------------
# Required cases
# ---------------------------------------------------------------------------


class TestBaseline:
    def test_no_override_is_limited_to_300(self, kanban_home, baseline, monkeypatch):
        """No cap at all: the lane asks for its generous default, gets 300."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_a_card_at_300_gets_300(self, kanban_home, baseline, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_the_baseline_is_the_config_value_not_a_constant(
        self, kanban_home, workroot, monkeypatch,
    ):
        monkeypatch.setattr(ex, "_exec_config", lambda: {
            "max_runtime_seconds": 120, "allowed_roots": [str(workroot)],
        })
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=2700)
        assert _direct_claude_grant(monkeypatch, tid) == 120


class TestLadderApproved:
    def test_ladder_approved_600_runs_600(self, kanban_home, baseline, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            _ladder(conn, tid, 300)
            assert _cap(conn, tid) == 600
        assert _direct_claude_grant(monkeypatch, tid) == 600

    def test_ladder_approved_900_runs_900(self, kanban_home, baseline, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            _ladder(conn, tid, 300)
            _ladder(conn, tid, 600)
            assert _cap(conn, tid) == 900
        assert _direct_claude_grant(monkeypatch, tid) == 900

    def test_verifier_child_ladder_600_runs_600(self, kanban_home, baseline, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
            _ladder(conn, tid, 300)
            assert _cap(conn, tid) == 600
        assert _verifier_grant(monkeypatch, tid) == 600

    def test_ladder_continues_from_a_human_approved_rung(self, kanban_home, baseline, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 600)
            _ladder(conn, tid, 600)
            assert _cap(conn, tid) == 900
        assert _direct_claude_grant(monkeypatch, tid) == 900


class TestHumanApproved:
    def test_recorded_human_approved_higher_cap_remains_effective(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 2700)
            assert _cap(conn, tid) == 2700
        assert _direct_claude_grant(monkeypatch, tid) == 2700

    def test_approval_is_recorded_with_provenance(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            _approve(conn, tid, 1800, reason="why", env={**OPERATOR})
            (payload,) = [
                json.loads(r["payload"]) for r in conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND kind='runtime_cap_raised'",
                    (tid,),
                )
            ]
        approved_at = payload.pop("approved_at")
        assert isinstance(approved_at, int)
        assert payload == {
            "max_runtime_seconds": 1800, "previous": 300,
            "source": "human_approval",
            "authorization_source": "kanban_db.approve_runtime_cap",
            "approved_by": "christopher",
            "actor_kind": "human_interactive", "actor_id": "christopher",
            "reason": "why",
        }

    def test_approved_cap_applies_to_the_recovery_gate_too(
        self, kanban_home, baseline, monkeypatch,
    ):
        captured = []

        def fake_run_supervised(**kw):
            captured.append(kw)
            raise ex.ExecutionPolicyError("test_stop", "captured")

        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 900)
        monkeypatch.setattr(recovery_lane.ex, "run_supervised", fake_run_supervised)
        recovery_lane._run_gate("true", "/tmp", 600, task_id=tid)
        assert _granted(captured[0]) == 600


class TestUnapproved:
    def test_a_cap_written_at_creation_is_limited_to_300(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=2700)
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_the_worker_create_tool_cannot_grant_itself_time(
        self, kanban_home, baseline, monkeypatch,
    ):
        """The supported worker path: ``kanban_create`` with a bigger number."""
        from tools import kanban_tools

        response = json.loads(kanban_tools._handle_create({
            "title": "worker self cap", "body": "", "assignee": "default",
            "max_runtime_seconds": 2700,
        }))
        assert response["ok"], response
        tid = response["task_id"]
        with kb.connect_closing() as conn:
            assert _cap(conn, tid) == 2700
            assert _raise_events(conn, tid) == 0
            assert kb.approved_runtime_cap(conn, tid) is None
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane=? WHERE id=?",
                             (kb.EXECUTOR_LANE_CLAUDE, tid))
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_raising_the_cap_after_a_ladder_approval_is_not_approved(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            _ladder(conn, tid, 300)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET max_runtime_seconds=7200 WHERE id=?", (tid,))
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_raising_a_human_approved_cap_is_not_approved(
        self, kanban_home, baseline, monkeypatch,
    ):
        """An approval covers the cap it names, not whatever the card says later."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 900)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET max_runtime_seconds=7200 WHERE id=?", (tid,))
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_a_card_created_at_600_cannot_ladder_itself_to_900(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=600)
            _ladder(conn, tid, 600)
            assert _cap(conn, tid) == 900
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_an_approval_lookup_failure_never_grants_time(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 900)

        def boom(conn, task_id):
            raise RuntimeError("board unavailable")

        monkeypatch.setattr(recovery_lane.kb, "approved_runtime_cap", boom)
        assert _direct_claude_grant(monkeypatch, tid) == 300


class TestForgedRecordsDoNotCount:
    """Hand-written ``runtime_cap_raised`` records that merely claim authority."""

    @pytest.mark.parametrize("payload", [
        # The exact record independent verification x_076804477eae5e31 used.
        {"max_runtime_seconds": 1800, "previous": 300, "actor_kind": "human_instructed",
         "actor_id": "not-a-human-worker", "reason": "forged"},
        # The retired 2026-09-07 format (t_376c07c8 shape).
        {"max_runtime_seconds": 1800, "previous": 300, "actor_kind": "human_instructed",
         "actor_id": "christopher", "executed_by": "claude_code_opus5", "reason": "Owner instruction"},
        {"max_runtime_seconds": 1800, "previous": 300},
        {"from": 300, "to": 1800, "actor": "claude-code"},
        {"max_runtime_seconds": "lots", "source": "human_approval"},
        # New-format look-alikes missing or contradicting a required field.
        {"max_runtime_seconds": 1800, "previous": 300, "source": "human_approval",
         "approved_by": "christopher", "actor_kind": "human_interactive", "actor_id": "christopher",
         "reason": "no authorization source"},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "human_approval",
         "authorization_source": "kanban_db.approve_runtime_cap", "approved_by": "christopher",
         "actor_kind": "human_instructed", "actor_id": "christopher", "reason": "wrong kind"},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "human_approval",
         "authorization_source": "kanban_db.approve_runtime_cap", "approved_by": "default",
         "actor_kind": "human_interactive", "actor_id": "christopher", "reason": "automation approver"},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "human_approval",
         "authorization_source": "kanban_db.approve_runtime_cap", "approved_by": "christopher",
         "actor_kind": "human_interactive", "actor_id": "erika", "reason": "profile actor"},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "human_approval",
         "authorization_source": "kanban_db.approve_runtime_cap", "approved_by": "christopher",
         "actor_kind": "human_interactive", "actor_id": "christopher", "reason": " "},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "operator",
         "authorization_source": "kanban_db.approve_runtime_cap", "approved_by": "christopher",
         "actor_kind": "human_interactive", "actor_id": "christopher", "reason": "wrong source"},
    ], ids=["codex-forgery", "retired-2026-09-07", "no-provenance", "legacy-unattributed",
            "malformed", "no-authorization-source", "wrong-actor-kind", "automation-approver",
            "profile-actor", "blank-reason", "wrong-source"])
    def test_a_record_claiming_approval_is_not_approval(
        self, kanban_home, baseline, monkeypatch, payload,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=1800)
            _forge(conn, tid, payload)
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    @pytest.mark.parametrize("payload,timeouts", [
        ({"max_runtime_seconds": 600, "previous": 300, "source": "runtime_cap_ladder"}, 0),
        ({"max_runtime_seconds": 600, "previous": 300, "source": "runtime_cap_ladder"}, 1),
        ({"max_runtime_seconds": 900, "previous": 300, "source": "runtime_cap_ladder"}, 2),
        ({"max_runtime_seconds": 1800, "previous": 900, "source": "runtime_cap_ladder"}, 2),
    ], ids=["no-timeouts", "one-timeout", "skips-a-rung", "off-the-ladder"])
    def test_a_ladder_record_without_the_ladders_evidence_is_not_approval(
        self, kanban_home, baseline, monkeypatch, payload, timeouts,
    ):
        cap = payload["max_runtime_seconds"]
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=cap)
            if timeouts:
                _timeouts(conn, tid, payload["previous"], n=timeouts)
            _forge(conn, tid, payload)
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_a_ladder_record_past_the_cards_ceiling_is_not_approval(
        self, kanban_home, baseline, monkeypatch,
    ):
        """Verifier children stop at 600, even with a well-formed chain."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
            _ladder(conn, tid, 300)
            _timeouts(conn, tid, 600)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET max_runtime_seconds=900 WHERE id=?", (tid,))
            _forge(conn, tid, {"max_runtime_seconds": 900, "previous": 600,
                               "source": "runtime_cap_ladder"})
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _verifier_grant(monkeypatch, tid) == 300

    def test_a_newer_invalid_record_supersedes_an_older_approval(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            _approve(conn, tid, 900)
            _forge(conn, tid, {"max_runtime_seconds": 900, "previous": 900})
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300


class TestOnlyAHumanOperatorCanApprove:
    @pytest.mark.parametrize("env", [
        {},
        {kb.ENV_ACTOR_ID: "christopher"},
        {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE},
        {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_GOVERNED_AUTOMATION, kb.ENV_ACTOR_ID: "christopher"},
        {**OPERATOR, "HERMES_KANBAN_TASK": "t_worker"},
        {**OPERATOR, "HERMES_KANBAN_RUN_ID": "7"},
        {**OPERATOR, "HERMES_EXECUTION_ID": "x_worker"},
        {**OPERATOR, "HERMES_EXECUTOR_LANE": "claude"},
        {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "default"},
        {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "erika"},
    ], ids=["no-provenance", "no-kind", "no-actor", "governed-automation", "kanban-task",
            "kanban-run", "execution", "executor-lane", "automation-actor", "profile-actor"])
    def test_approval_refused_without_operator_provenance(self, kanban_home, env):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            with pytest.raises(kb.RuntimeCapApprovalRefused):
                _approve(conn, tid, 2700, env=env)
            assert _cap(conn, tid) == 300
            assert _raise_events(conn, tid) == 0

    @pytest.mark.parametrize("approved_by", [
        "ordinary-worker-lane", "default", "codex_verify", "atlas", "erika",
    ])
    def test_an_automation_identity_cannot_be_the_approver(self, kanban_home, approved_by):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            with pytest.raises(kb.RuntimeCapApprovalRefused):
                _approve(conn, tid, 2700, approved_by=approved_by)
            assert _cap(conn, tid) == 300
            assert _raise_events(conn, tid) == 0

    def test_refused_inside_a_live_supervised_execution(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(kanban_home), task_id=None, controller_pid=os.getpid(),
                controller_key="k", controller_token="t", ownership=ex.OWNERSHIP_SUPERVISOR,
                max_runtime_s=60,
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE executions SET pid=?, pgid=? WHERE ended_at IS NULL",
                             (os.getpid(), os.getpgid(0)))
            with pytest.raises(kb.RuntimeCapApprovalRefused, match="live execution"):
                _approve(conn, tid, 2700)
            assert _cap(conn, tid) == 300 and _raise_events(conn, tid) == 0

    def test_refused_inside_a_running_kanban_worker(self, kanban_home):
        """A worker's terminal tool can drop the env markers; its ancestry cannot."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            worker = _card(conn, cap=300)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running', worker_pid=? WHERE id=?",
                             (os.getppid(), worker))
            with pytest.raises(kb.RuntimeCapApprovalRefused, match="worker"):
                _approve(conn, tid, 2700)
            assert _cap(conn, tid) == 300 and _raise_events(conn, tid) == 0

    def test_a_named_operator_is_required_even_without_a_profiles_directory(self, kanban_home):
        """The actor-id check stands on its own, not on how '' resolves against profiles/."""
        import shutil

        shutil.rmtree(kanban_home / "profiles")
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            with pytest.raises(kb.RuntimeCapApprovalRefused, match="operator provenance"):
                _approve(conn, tid, 2700, env={kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE})
            assert _cap(conn, tid) == 300 and _raise_events(conn, tid) == 0

    def test_refused_when_sharing_a_running_workers_process_group(self, kanban_home):
        """A detached-looking child of the worker still shares its process group."""
        import subprocess

        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            assert sibling.pid not in kb._process_ancestry()
            with kb.connect_closing() as conn:
                tid = _card(conn, cap=300)
                worker = _card(conn, cap=300)
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status='running', worker_pid=? WHERE id=?",
                                 (sibling.pid, worker))
                with pytest.raises(kb.RuntimeCapApprovalRefused, match="process group"):
                    _approve(conn, tid, 2700)
                assert _cap(conn, tid) == 300 and _raise_events(conn, tid) == 0
        finally:
            sibling.kill()
            sibling.wait()

    @pytest.mark.parametrize("kwargs", [
        {"approved_by": "", "reason": "r"},
        {"approved_by": "christopher", "reason": "  "},
        {"approved_by": "christopher", "reason": "r", "seconds": 0},
        {"approved_by": "christopher", "reason": "r", "seconds": True},
    ], ids=["no-approver", "no-reason", "no-cap", "bool-cap"])
    def test_an_incomplete_approval_writes_nothing(self, kanban_home, kwargs):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            seconds = kwargs.pop("seconds", 900)
            with pytest.raises(kb.RuntimeCapApprovalRefused):
                _approve(conn, tid, seconds, **kwargs)
            assert _cap(conn, tid) == 300
            assert _raise_events(conn, tid) == 0

    def test_only_ladder_or_operator_approval_exceeds_the_baseline(self, kanban_home):
        with kb.connect_closing() as conn:
            plain = _card(conn, cap=900)
            laddered = _card(conn, cap=300)
            _ladder(conn, laddered, 300)
            approved = _card(conn, cap=None)
            _approve(conn, approved, 1200)
            assert kb.approved_runtime_cap(conn, plain) is None
            assert kb.approved_runtime_cap(conn, laddered) == 600
            assert kb.approved_runtime_cap(conn, approved) == 1200

    def test_worker_tools_expose_no_approval_path(self):
        source = Path(recovery_lane.__file__).parent.parent.joinpath(
            "tools", "kanban_tools.py",
        ).read_text(encoding="utf-8")
        assert "approve_runtime_cap" not in source
        assert "runtime_cap_raised" not in source


class TestRealSupervisor:
    """No fakes: the execution record the reconciler enforces carries the grant."""

    def _gate(self, workroot, tid, timeout):
        gate = recovery_lane._run_gate(
            f"{sys.executable} -c pass", str(workroot), timeout, task_id=tid,
        )
        assert gate.ok, gate.error
        with kb.connect_closing() as conn:
            row = conn.execute(
                "SELECT id, max_runtime_s FROM executions WHERE task_id=? "
                "ORDER BY created_at DESC LIMIT 1", (tid,),
            ).fetchone()
            clamped = [
                json.loads(r["payload"]) for r in conn.execute(
                    "SELECT payload FROM execution_events WHERE execution_id=? "
                    "AND kind='runtime_clamped'", (row["id"],),
                )
            ]
        return row["max_runtime_s"], clamped

    def test_unapproved_card_execution_is_recorded_at_300_and_the_clamp_is_visible(
        self, kanban_home, workroot, baseline,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=900, lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY)
        granted, clamped = self._gate(workroot, tid, 900)
        assert granted == 300
        assert clamped and clamped[0]["requested_seconds"] == 900
        assert clamped[0]["effective_seconds"] == 300

    def test_forged_approval_execution_is_recorded_at_300(
        self, kanban_home, workroot, baseline,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=1800, lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY)
            _forge(conn, tid, {"max_runtime_seconds": 1800, "previous": 300,
                               "actor_kind": "human_instructed",
                               "actor_id": "not-a-human-worker", "reason": "forged"})
        granted, _ = self._gate(workroot, tid, 1800)
        assert granted == 300

    def test_approved_card_execution_is_recorded_at_its_cap(
        self, kanban_home, workroot, baseline,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None, lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY)
            _approve(conn, tid, 900)
        granted, clamped = self._gate(workroot, tid, 900)
        assert granted == 900
        assert clamped == []
