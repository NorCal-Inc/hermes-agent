"""Approved per-card runtime caps above the global execution ceiling.

Christopher, 2026-09-15:

    "Set the normal global execution ceiling to 300 seconds, but make the
     smallest tested code change necessary so explicitly authorized per-card
     runtime caps can exceed that baseline ... A card explicitly authorized for
     600, 900, or another recorded human-approved cap must keep that cap. An
     ordinary card with no approved override must be limited to 300."

``execution.max_runtime_seconds`` clamps every supervised execution
(``ExecutionPolicy.resolve_max_runtime``). Before this change that clamp also
cut the ladder's own rungs: a card promoted to 600 by the 2026-09-07 ladder ran
600 only while the global value happened to be 600. These tests pin the
baseline at 300 and check the runtime a card's supervised execution is actually
granted, through the real recovery lane and the real policy resolution.
"""
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import exec_supervisor as ex
from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane

BASELINE = 300


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


def _cap(conn, tid):
    return conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id=?", (tid,)
    ).fetchone()["max_runtime_seconds"]


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

    def test_a_ladder_record_past_the_cards_ceiling_is_not_approval(
        self, kanban_home, baseline, monkeypatch,
    ):
        """Verifier children stop at 600; a 900 'ladder' record on one is forged."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=900, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "runtime_cap_raised", {
                    "max_runtime_seconds": 900, "previous": 600,
                    "source": "runtime_cap_ladder",
                })
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _verifier_grant(monkeypatch, tid) == 300


class TestHumanApproved:
    def test_recorded_human_approved_higher_cap_remains_effective(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            kb.approve_runtime_cap(
                conn, tid, 2700, approved_by="christopher",
                reason="long controller build, approved in session",
            )
            assert _cap(conn, tid) == 2700
        assert _direct_claude_grant(monkeypatch, tid) == 2700

    def test_existing_human_instructed_record_remains_effective(
        self, kanban_home, baseline, monkeypatch,
    ):
        """The t_376c07c8 record (2026-09-07), written before this function existed."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=600)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "runtime_cap_raised", {
                    "max_runtime_seconds": 600, "previous": 300,
                    "actor_kind": "human_instructed", "actor_id": "christopher",
                    "executed_by": "claude_code_opus5",
                    "reason": "Owner instruction 2026-09-07",
                })
        assert _direct_claude_grant(monkeypatch, tid) == 600

    def test_approved_cap_applies_to_the_recovery_gate_too(
        self, kanban_home, baseline, monkeypatch,
    ):
        captured = []

        def fake_run_supervised(**kw):
            captured.append(kw)
            raise ex.ExecutionPolicyError("test_stop", "captured")

        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            kb.approve_runtime_cap(conn, tid, 900, approved_by="christopher", reason="r")
        monkeypatch.setattr(recovery_lane.ex, "run_supervised", fake_run_supervised)
        recovery_lane._run_gate("true", "/tmp", 600, task_id=tid)
        assert _granted(captured[0]) == 600


class TestUnapproved:
    def test_a_cap_written_at_creation_is_limited_to_300(
        self, kanban_home, baseline, monkeypatch,
    ):
        """What a worker's ``kanban_create`` call can do: pass a bigger number."""
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=2700)
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_raising_the_cap_after_an_approval_is_not_approved(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            _ladder(conn, tid, 300)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET max_runtime_seconds=7200 WHERE id=?", (tid,),
                )
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    @pytest.mark.parametrize("payload", [
        {"max_runtime_seconds": 1800, "previous": 300},
        {"max_runtime_seconds": 1800, "previous": 300, "actor_kind": "human_instructed"},
        {"max_runtime_seconds": 1800, "previous": 300, "actor_kind": "human_instructed", "actor_id": "  "},
        {"max_runtime_seconds": 1800, "previous": 300, "actor_kind": "agent", "actor_id": "erika"},
        {"max_runtime_seconds": 1800, "previous": 300, "source": "runtime_cap_ladder"},
        {"from": 300, "to": 1800, "actor": "claude-code"},
        {"max_runtime_seconds": "lots", "actor_kind": "human_instructed", "actor_id": "x"},
    ], ids=["no-provenance", "no-actor-id", "blank-actor-id", "agent-actor",
            "ladder-off-rung", "legacy-unattributed", "malformed"])
    def test_an_unattributed_raise_record_is_not_approval(
        self, kanban_home, baseline, monkeypatch, payload,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=1800)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "runtime_cap_raised", payload)
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_a_newer_unapproved_record_supersedes_an_older_approval(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            kb.approve_runtime_cap(conn, tid, 900, approved_by="christopher", reason="r")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "runtime_cap_raised", {
                    "max_runtime_seconds": 900, "previous": 900,
                })
            assert kb.approved_runtime_cap(conn, tid) is None
        assert _direct_claude_grant(monkeypatch, tid) == 300

    def test_an_approval_lookup_failure_never_grants_time(
        self, kanban_home, baseline, monkeypatch,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None)
            kb.approve_runtime_cap(conn, tid, 900, approved_by="christopher", reason="r")

        def boom(conn, task_id):
            raise RuntimeError("board unavailable")

        monkeypatch.setattr(recovery_lane.kb, "approved_runtime_cap", boom)
        assert _direct_claude_grant(monkeypatch, tid) == 300


class TestOnlyTheGovernedPaths:
    def test_only_ladder_or_named_approval_exceeds_the_baseline(self, kanban_home):
        with kb.connect_closing() as conn:
            plain = _card(conn, cap=900)
            laddered = _card(conn, cap=300)
            _ladder(conn, laddered, 300)
            approved = _card(conn, cap=None)
            kb.approve_runtime_cap(conn, approved, 1200, approved_by="christopher", reason="r")
            assert kb.approved_runtime_cap(conn, plain) is None
            assert kb.approved_runtime_cap(conn, laddered) == 600
            assert kb.approved_runtime_cap(conn, approved) == 1200

    @pytest.mark.parametrize("kwargs", [
        {"approved_by": "", "reason": "r"},
        {"approved_by": "christopher", "reason": "  "},
        {"approved_by": "christopher", "reason": "r", "seconds": 0},
    ], ids=["no-approver", "no-reason", "no-cap"])
    def test_an_incomplete_approval_writes_nothing(self, kanban_home, kwargs):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            seconds = kwargs.pop("seconds", 900)
            with pytest.raises(ValueError):
                kb.approve_runtime_cap(conn, tid, seconds, **kwargs)
            assert _cap(conn, tid) == 300
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='runtime_cap_raised'",
                (tid,),
            ).fetchone()[0] == 0

    def test_approval_is_recorded_with_provenance(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=300)
            kb.approve_runtime_cap(conn, tid, 1800, approved_by="christopher", reason="why")
            (payload,) = [
                json.loads(r["payload"]) for r in conn.execute(
                    "SELECT payload FROM task_events WHERE task_id=? AND kind='runtime_cap_raised'",
                    (tid,),
                )
            ]
        assert payload == {
            "max_runtime_seconds": 1800, "previous": 300,
            "actor_kind": "human_instructed", "actor_id": "christopher",
            "reason": "why", "source": "human_approval",
        }

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

    def test_approved_card_execution_is_recorded_at_its_cap(
        self, kanban_home, workroot, baseline,
    ):
        with kb.connect_closing() as conn:
            tid = _card(conn, cap=None, lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY)
            kb.approve_runtime_cap(conn, tid, 900, approved_by="christopher", reason="r")
        granted, clamped = self._gate(workroot, tid, 900)
        assert granted == 900
        assert clamped == []
