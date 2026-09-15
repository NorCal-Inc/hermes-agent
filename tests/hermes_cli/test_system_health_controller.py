"""System health controller (t_b8d62378): recovery, escalation and failure injection.

Every scenario runs on an isolated HERMES_HOME board. Failures are injected in
the shapes observed live on 2026-09-14 (t_3883034a, t_992e8161, t_4d21959c,
t_15d87799, t_e48487e5, t_69440ff2) and the controller must:

  (1) detect it, (2) run only its allowlisted recovery, (3) rerun the exact
  invariant, (4) return to GREEN with normal workflow continuing, (5) stop a
  failing recovery at its budget and escalate exactly once, (6) never create a
  duplicate repair card, (7) keep task/company content out of alerts and cards,
  (8) watch the watchers, (9) give the implementation a legally independent
  verifier without manual rescue, and (10) never build a verifier-of-verifier.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

_CONTROLLER_PATH = Path(__file__).resolve().parents[2] / "norcal/health/system_health_controller.py"


def _load_controller():
    name = "norcal_system_health_controller_under_test"
    spec = importlib.util.spec_from_file_location(name, _CONTROLLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shc = _load_controller()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class Clock:
    def __init__(self) -> None:
        # Past the route invariant's settle window for anything created now.
        self.t = time.time() + 3600

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Alerts:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[tuple[str, str]] = []

    def __call__(self, subject: str, text: str):
        self.sent.append((subject, text))
        return self.ok, "fake-sender"


def _ctx(home: Path, *, clock=None, alerts=None, config=None, run_command=None, dry_run=False):
    return shc.Context(
        state_dir=home / "state" / "system-health-controller",
        hermes_home=home,
        config=config or {},
        kanban=kb.connect_closing,
        send_alert=alerts if alerts is not None else Alerts(),
        run_command=run_command or (lambda argv, timeout: (0, "")),
        create_card=shc.kanban_repair_card,
        now=clock or Clock(),
        dry_run=dry_run,
    )


def _health_cards(conn):
    return conn.execute(
        "SELECT id, title, body, status, assignee, tenant, idempotency_key FROM tasks "
        "WHERE idempotency_key LIKE 'health:%' ORDER BY created_at",
    ).fetchall()


def _kinds(conn, tid):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]


def _attach(conn, tid, name="evidence.md"):
    kb.add_attachment(conn, tid, filename=name, stored_path=f"/tmp/{tid}/{name}",
                      size=512, uploaded_by="claude-lane")


def _subject_evidence_after_handoff(conn, *, title="overlay extraction", implementer="default", tenant=None, body=None):
    """t_3883034a run 2814: handed off before the evidence packet existed."""
    tid = kb.create_task(conn, title=title, body=body, assignee=implementer,
                         gauntlet=True, tenant=tenant)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(conn, tid, summary="extracted",
                             expected_run_id=claimed.current_run_id) is True
    assert kb._open_verifier_child(conn, tid) is None
    _attach(conn, tid, "phase3-evidence-min.md")
    return tid


def _subject_without_evidence(conn, *, title="delegated work", body=None, tenant=None):
    tid = kb.create_task(conn, title=title, body=body, assignee="default",
                         gauntlet=True, tenant=tenant)
    claimed = kb.claim_task(conn, tid)
    assert kb.request_review(conn, tid, summary="nothing attached",
                             expected_run_id=claimed.current_run_id) is True
    return tid


def _subject_with_route(conn, *, assignee="default", title="overlay extraction"):
    tid = kb.create_task(conn, title=title, assignee=assignee, gauntlet=True)
    claimed = kb.claim_task(conn, tid)
    _attach(conn, tid)
    assert kb.request_review(conn, tid, summary="extracted",
                             expected_run_id=claimed.current_run_id) is True
    assert kb.get_task(conn, tid).verification_state == kb.VERIFICATION_PENDING
    return tid


class Counting:
    """Wrap an invariant to count checks and recoveries."""

    def __init__(self, inner, *, recover_noop=False):
        self.inner = inner
        self.name = inner.name
        self.tier = inner.tier
        self.max_attempts = inner.max_attempts
        self.confirm_cycles = inner.confirm_cycles
        self.checks = 0
        self.recoveries = 0
        self.recover_noop = recover_noop

    def check(self, ctx):
        self.checks += 1
        return self.inner.check(ctx)

    def recover(self, ctx, finding):
        self.recoveries += 1
        if self.recover_noop:
            return shc.RecoveryOutcome(False, "noop_for_test")
        return self.inner.recover(ctx, finding)


# ---------------------------------------------------------------------------
# Verifier routing: recovery that returns the system to GREEN
# ---------------------------------------------------------------------------


class TestVerifierRouteRecovery:
    def test_detect_recover_revalidate_green_and_workflow_continues(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        route = Counting(shc.VerifierRouteOpen())
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts), [route])
        with kb.connect_closing() as conn:
            tid = _subject_evidence_after_handoff(conn)
            before = conn.execute(
                "SELECT status, assignee, executor_lane, verification_state FROM tasks WHERE id = ?",
                (tid,)).fetchone()

        result = controller.run(shc.TIER_LIGHT)

        # (1) detected, (3) the exact invariant was rerun once after recovery
        assert len(result.recovered) == 1
        assert route.checks == 2 and route.recoveries == 1
        assert result.escalated == [] and alerts.sent == []
        with kb.connect_closing() as conn:
            # (2) only the allowlisted mutation: a codex_verify child, subject untouched
            after = conn.execute(
                "SELECT status, assignee, executor_lane, verification_state FROM tasks WHERE id = ?",
                (tid,)).fetchone()
            assert tuple(after) == tuple(before)
            child = kb._open_verifier_child(conn, tid)
            assert child is not None
            child_task = kb.get_task(conn, child)
            assert child_task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert child_task.status == "ready"
            assert _health_cards(conn) == []

            # (4)+(9) the independent verifier runs and its verdict closes the subject
            claimed = kb.claim_task(conn, child)
            assert claimed is not None
            with kb.write_txn(conn):
                kb._append_event(conn, child, "codex_verifier_started", {"executor": "codex"},
                                 run_id=claimed.current_run_id)
            assert kb.complete_task(
                conn, child, summary="VERDICT: PASS\nACCEPTANCE: PASS\nchecked the packet",
                expected_run_id=claimed.current_run_id,
            ) is True
            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_VERIFIED
            assert subject.status == "done"

        clock.advance(300)
        again = controller.run(shc.TIER_LIGHT)
        assert again.status == "GREEN" and again.findings == 0
        assert alerts.sent == []
        beat = shc.StateStore(kanban_home / "state" / "system-health-controller").read_heartbeat("light")
        assert beat["status"] == "GREEN"

    def test_failing_recovery_is_bounded_and_escalates_exactly_once(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        route = Counting(shc.VerifierRouteOpen(), recover_noop=True)
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts), [route])
        with kb.connect_closing() as conn:
            _subject_evidence_after_handoff(conn)

        first = controller.run(shc.TIER_LIGHT)
        assert first.escalated == [] and len(first.open) == 1
        assert route.recoveries == 1 and alerts.sent == []

        for _ in range(3):
            clock.advance(300)
            controller.run(shc.TIER_LIGHT)

        # (5) the budget (2) was spent once, then exactly one escalation
        assert route.recoveries == 2
        assert len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            cards = _health_cards(conn)
            assert len(cards) == 1
            assert cards[0]["status"] == "triage" and cards[0]["assignee"] is None

    def test_evidence_less_subject_is_not_repaired_it_is_escalated(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        route = Counting(shc.VerifierRouteOpen())
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts), [route])
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn)

        result = controller.run(shc.TIER_LIGHT)
        assert route.recoveries == 0
        assert len(result.escalated) == 1 and len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            assert kb._open_verifier_child(conn, tid) is None
            assert kb.get_task(conn, tid).status == "review"

    def test_no_duplicate_card_even_after_controller_state_is_lost(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts)
        controller = shc.Controller(ctx, [shc.VerifierRouteOpen()])
        with kb.connect_closing() as conn:
            _subject_without_evidence(conn)

        controller.run(shc.TIER_LIGHT)
        (ctx.state_dir / "state.json").unlink()
        clock.advance(300)
        controller.run(shc.TIER_LIGHT)
        clock.advance(300)
        controller.run(shc.TIER_LIGHT)

        # (6)
        with kb.connect_closing() as conn:
            assert len(_health_cards(conn)) == 1
        assert len(alerts.sent) == 1

    def test_undelivered_alert_is_retried_but_bounded(self, kanban_home):
        clock, alerts = Clock(), Alerts(ok=False)
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts),
                                    [shc.VerifierRouteOpen()])
        with kb.connect_closing() as conn:
            _subject_without_evidence(conn)
        for _ in range(6):
            controller.run(shc.TIER_LIGHT)
            clock.advance(300)
        assert len(alerts.sent) == shc.MAX_ALERT_ATTEMPTS
        with kb.connect_closing() as conn:
            assert len(_health_cards(conn)) == 1

    def test_dry_run_mutates_nothing(self, kanban_home):
        alerts = Alerts()
        controller = shc.Controller(_ctx(kanban_home, alerts=alerts, dry_run=True),
                                    [shc.VerifierRouteOpen()])
        with kb.connect_closing() as conn:
            tid = _subject_evidence_after_handoff(conn)
        result = controller.run(shc.TIER_LIGHT)
        assert len(result.open) == 1
        with kb.connect_closing() as conn:
            assert kb._open_verifier_child(conn, tid) is None
            assert _health_cards(conn) == []
        assert alerts.sent == []


# ---------------------------------------------------------------------------
# Subject damage and verifier-child deadlock (failures 6 and 7)
# ---------------------------------------------------------------------------


class TestSubjectDamageRecovery:
    def test_relabelled_subject_lane_is_restored(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_with_route(conn, assignee="claude")
            assert kb.get_task(conn, tid).executor_lane == kb.EXECUTOR_LANE_CLAUDE
            # Board history written by the pre-A7 reassign path (event 151185).
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                             (kb.EXECUTOR_LANE_CODEX_VERIFY, tid))
                kb._append_event(conn, tid, "executor_lane_normalized", {
                    "from_assignee": "atlas", "assignee": "default",
                    "executor_lane": kb.EXECUTOR_LANE_CODEX_VERIFY,
                    "source": "assign_task", "status": "review"})
        alerts = Alerts()
        relabel = Counting(shc.SubjectLaneRelabelled())
        result = shc.Controller(_ctx(kanban_home, alerts=alerts), [relabel]).run(shc.TIER_LIGHT)

        assert len(result.recovered) == 1 and relabel.checks == 2
        assert alerts.sent == []
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            assert task.executor_lane == kb.EXECUTOR_LANE_CLAUDE
            assert task.status == "review"
            assert "subject_executor_lane_restored" in _kinds(conn, tid)

    def test_restore_helper_refuses_a_real_verifier_card(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            child = kb._open_verifier_child(conn, subject)
            assert kb.restore_relabelled_subject_lane(conn, child, actor="test") == (False, None)
            assert kb.get_task(conn, child).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY

    def test_review_to_triage_regression_escalates_without_touching_status(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_with_route(conn)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (tid,))
        alerts = Alerts()
        result = shc.Controller(_ctx(kanban_home, alerts=alerts),
                                [shc.SubjectReviewRegressed()]).run(shc.TIER_LIGHT)
        assert len(result.escalated) == 1 and len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, tid).status == "triage"

    def _gated_child(self, conn, subject, *, title, assignee):
        child = kb.create_task(conn, title=title, assignee=assignee,
                               parents=[subject], gauntlet=True)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
        assert kb.claim_task(conn, child) is None  # the t_4d21959c claim_rejected
        assert kb.get_task(conn, child).status == "todo"
        return child

    def test_deadlocked_verifier_child_is_declared_and_released(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            child = self._gated_child(
                conn, subject,
                title="Independent compliance verification Phase 3 overlay extraction",
                assignee="reviewer",
            )
        alerts = Alerts()
        result = shc.Controller(_ctx(kanban_home, alerts=alerts),
                                [shc.VerifierChildDeadlocked()]).run(shc.TIER_LIGHT)
        assert len(result.recovered) == 1 and alerts.sent == []
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, child).status == "ready"
            assert kb.claim_task(conn, child) is not None

    @pytest.mark.parametrize("title,assignee", [
        ("follow-on implementation", "reviewer"),          # not a verification card
        ("Independent verification of overlay", "default"),  # the implementer itself
    ])
    def test_unclassified_or_implementer_child_escalates_instead(self, kanban_home, title, assignee):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            child = self._gated_child(conn, subject, title=title, assignee=assignee)
        alerts = Alerts()
        result = shc.Controller(_ctx(kanban_home, alerts=alerts),
                                [shc.VerifierChildDeadlocked()]).run(shc.TIER_LIGHT)
        assert result.recovered == [] and len(result.escalated) == 1
        with kb.connect_closing() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_relations WHERE from_task_id = ?", (child,),
            ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Escalate-only verifier-graph invariants
# ---------------------------------------------------------------------------


class TestVerifierGraphEscalations:
    def test_bare_lane_verified_closure_is_escalated_and_history_preserved(self, kanban_home):
        """The t_e48487e5 shape, as it exists on the board."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="Recover degraded shared boot gate",
                                 assignee="default", gauntlet=True)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_passed",
                                 {"verifier": "codex_verify", "source": "record_verification"})
                conn.execute("UPDATE tasks SET status = 'done', verification_state = ? WHERE id = ?",
                             (kb.VERIFICATION_VERIFIED, tid))
            events_before = _kinds(conn, tid)
        alerts = Alerts()
        result = shc.Controller(_ctx(kanban_home, alerts=alerts),
                                [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        assert len(result.escalated) == 1 and len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            assert _kinds(conn, tid) == events_before
            task = kb.get_task(conn, tid)
            assert (task.status, task.verification_state) == ("done", kb.VERIFICATION_VERIFIED)

    def test_verdict_returned_from_unattested_verifier_is_escalated(self, kanban_home):
        """The t_69440ff2 / t_fa3d6af3 shape."""
        with kb.connect_closing() as conn:
            subject = kb.create_task(conn, title="subject", assignee="default", gauntlet=True)
            verifier = kb.create_task(conn, title="verify", assignee="atlas",
                                      parents=[subject], gauntlet=True)
            with kb.write_txn(conn):
                kb._append_event(conn, subject, "verifier_verdict_returned",
                                 {"verifier_task": verifier, "verdict": "PASS", "recorded": True})
        result = shc.Controller(_ctx(kanban_home),
                                [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        assert len(result.escalated) == 1

    def test_verifier_of_verifier_is_escalated_never_recovered(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            outer = kb._open_verifier_child(conn, subject)
            inner = kb.create_task(conn, title="verify the verifier", assignee="atlas",
                                   parents=[outer], gauntlet=True)
        inv = Counting(shc.VerifierOfVerifier())
        result = shc.Controller(_ctx(kanban_home), [inv]).run(shc.TIER_DEEP)
        assert len(result.escalated) == 1 and inv.recoveries == 0
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, inner).status != "archived"

    def test_recovery_never_builds_a_verifier_of_verifier(self, kanban_home):
        """(10) After the route is opened, nothing treats the verifier as a subject."""
        clock = Clock()
        ctx = _ctx(kanban_home, clock=clock)
        with kb.connect_closing() as conn:
            _subject_evidence_after_handoff(conn)
        shc.Controller(ctx, [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        for _ in range(3):
            clock.advance(300)
            light = shc.Controller(ctx, [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
            assert light.findings == 0
        deep = shc.Controller(ctx, [shc.VerifierOfVerifier()]).run(shc.TIER_DEEP)
        assert deep.findings == 0
        with kb.connect_closing() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_links l JOIN tasks c ON c.id = l.child_id "
                "JOIN tasks p ON p.id = l.parent_id WHERE c.executor_lane = ? AND p.executor_lane = ?",
                (kb.EXECUTOR_LANE_CODEX_VERIFY, kb.EXECUTOR_LANE_CODEX_VERIFY),
            ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# (8) The watchers are watched
# ---------------------------------------------------------------------------


def _write_jobs(home: Path, jobs: list[dict]) -> None:
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def _iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


class TestWatchersAreWatched:
    def test_critical_cron_job_conditions(self, kanban_home):
        clock = Clock()
        now = clock()
        script = kanban_home / "scripts" / "present.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        _write_jobs(kanban_home, [
            {"name": "healthy", "enabled": True, "state": "scheduled", "last_status": "ok",
             "last_run_at": _iso(now - 60), "failure_streak": 0, "script": "present.sh"},
            {"name": "paused-silently", "enabled": False, "state": "paused", "paused_reason": None},
            {"name": "paused-with-reason", "enabled": False, "state": "paused",
             "paused_reason": "migration window", "paused_at": _iso(now - 60)},
            {"name": "paused-too-long", "enabled": False, "state": "paused",
             "paused_reason": "forgotten", "paused_at": _iso(now - 10 * 86400)},
            {"name": "stale", "enabled": True, "state": "scheduled", "last_status": "ok",
             "last_run_at": _iso(now - 7200)},
            {"name": "failing", "enabled": True, "state": "scheduled", "last_status": "error",
             "last_run_at": _iso(now - 60), "failure_streak": 3,
             "last_delivery_error": "telegram 403"},
            {"name": "script-gone", "enabled": True, "state": "scheduled", "last_status": "ok",
             "last_run_at": _iso(now - 60), "script": "missing.sh"},
        ])
        config = {"critical_cron_jobs": {
            "healthy": {"max_age_seconds": 900},
            "paused-silently": {},
            "paused-with-reason": {"pause_max_age_seconds": 86400},
            "paused-too-long": {"pause_max_age_seconds": 86400},
            "stale": {"max_age_seconds": 900},
            "failing": {"max_age_seconds": 900},
            "script-gone": {},
            "absent-job": {},
        }}
        findings = shc.CriticalCronJobsHealthy().check(_ctx(kanban_home, clock=clock, config=config))
        got = {(f.subject, f.signature) for f in findings}
        assert got == {
            ("paused-silently", "paused_without_reason"),
            ("paused-too-long", "pause_expired"),
            ("stale", "stale"),
            ("failing", "last_status:error"),
            ("failing", "failure_streak"),
            ("failing", "delivery_failed"),
            ("script-gone", "script_missing"),
            ("absent-job", "job_missing"),
        }

    def test_light_and_deep_passes_watch_each_other(self, kanban_home):
        clock = Clock()
        ctx = _ctx(kanban_home, clock=clock)
        light = shc.Controller(ctx, [shc.CounterpartHeartbeatFresh(shc.TIER_LIGHT)])
        deep = shc.Controller(ctx, [shc.CounterpartHeartbeatFresh(shc.TIER_DEEP)])

        assert light.run(shc.TIER_LIGHT).findings == 0   # just installed: no false alarm
        clock.advance(2 * 3600 + 200)
        missing_deep = light.run(shc.TIER_LIGHT)
        assert missing_deep.findings == 1 and len(missing_deep.escalated) == 1

        assert deep.run(shc.TIER_DEEP).findings == 0     # light beat is fresh
        assert light.run(shc.TIER_LIGHT).findings == 0   # deep now fresh: resolved

        clock.advance(2 * 300 + 200)                     # light stops running
        assert deep.run(shc.TIER_DEEP).findings == 1

    def test_a_check_that_cannot_look_is_itself_escalated(self, kanban_home):
        class Broken(shc.Invariant):
            name = "broken_monitor"
            tier = shc.TIER_LIGHT

            def check(self, ctx):
                raise RuntimeError("source unreadable")

        alerts = Alerts()
        result = shc.Controller(_ctx(kanban_home, alerts=alerts), [Broken()]).run(shc.TIER_LIGHT)
        assert len(result.escalated) == 1
        assert "check_error:RuntimeError" in alerts.sent[0][1]

    def test_user_timers_are_parsed_from_machine_readable_listing(self, kanban_home):
        clock = Clock()
        now = clock()
        listing = json.dumps([
            {"unit": "fresh.timer", "last": int((now - 60) * 1_000_000)},
            {"unit": "old.timer", "last": int((now - 90000) * 1_000_000)},
            {"unit": "stopped.timer", "last": int((now - 60) * 1_000_000)},
        ])

        def runner(argv, timeout):
            if "list-timers" in argv:
                return 0, listing
            if "is-active" in argv:
                return (0, "active\n") if argv[-1] != "stopped.timer" else (3, "inactive\n")
            return 1, ""

        config = {"critical_user_timers": {
            "fresh.timer": {"max_age_seconds": 900},
            "old.timer": {"max_age_seconds": 900},
            "stopped.timer": {"max_age_seconds": 900},
            "unloaded.timer": {"max_age_seconds": 900},
        }}
        findings = shc.CriticalTimersActive().check(
            _ctx(kanban_home, clock=clock, config=config, run_command=runner))
        assert {(f.subject, f.signature) for f in findings} == {
            ("old.timer", "not_triggered_recently"),
            ("stopped.timer", "not_active:inactive"),
            ("unloaded.timer", "timer_not_loaded"),
        }

    @pytest.mark.parametrize("rc,output,healthy", [
        (0, "BOOT STATUS: COMPLETE\nDOCTRINE VERSION: 2.28\n", True),
        (0, "prose mentioning BOOT STATUS: COMPLETE\nBOOT STATUS: DEGRADED\n", False),
        (1, "BOOT STATUS: COMPLETE\n", False),
        (0, "nothing useful\n", False),
    ])
    def test_boot_gate_parses_the_authoritative_state_line_exactly(self, kanban_home, rc, output, healthy):
        findings = shc.CanonicalBootComplete().check(
            _ctx(kanban_home, run_command=lambda argv, t: (rc, output)))
        assert (findings == []) is healthy

    def test_gateway_heartbeat_must_persist_before_escalating(self, kanban_home):
        clock = Clock()
        (kanban_home / "state").mkdir(parents=True, exist_ok=True)
        (kanban_home / "state" / "gateway.heartbeat").write_text(
            json.dumps({"updated_at": _iso(clock() - 3600)}), encoding="utf-8")
        alerts = Alerts()
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts),
                                    [shc.GatewayHeartbeatFresh()])
        assert controller.run(shc.TIER_LIGHT).escalated == []
        clock.advance(300)
        assert len(controller.run(shc.TIER_LIGHT).escalated) == 1
        assert len(alerts.sent) == 1

    def test_runtime_ceiling_drift_against_doctrine(self, kanban_home):
        values = {"execution.max_runtime_seconds": "600", "kanban.gauntlet_stale_timeout_seconds": "300"}
        findings = shc.RuntimeCeilingsMatchDoctrine().check(_ctx(
            kanban_home,
            config={"expected_runtime_config": {k: "300" for k in values}},
            run_command=lambda argv, t: (0, values[argv[-1]] + "\n"),
        ))
        assert [(f.subject, f.signature) for f in findings] == [
            ("execution.max_runtime_seconds", "value_mismatch:600"),
        ]


# ---------------------------------------------------------------------------
# (7) Boundaries and silence
# ---------------------------------------------------------------------------


class TestBoundariesAndSilence:
    def test_alerts_and_cards_carry_no_task_or_company_content(self, kanban_home):
        private_title = "Orion client intake SSN 123-45-6789"
        private_body = "customer card ending 4242, home address 1 Main St"
        alerts = Alerts()
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn, title=private_title, body=private_body,
                                            tenant="orion")
        shc.Controller(_ctx(kanban_home, alerts=alerts),
                       [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        assert len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            (card,) = _health_cards(conn)
        blob = " ".join([alerts.sent[0][0], alerts.sent[0][1], card["title"], card["body"] or ""])
        for secret in ("Orion", "SSN", "123-45-6789", "4242", "Main St"):
            assert secret not in blob
        assert tid in blob
        assert card["tenant"] is None
        assert card["status"] == "triage" and card["assignee"] is None

    def test_green_pass_is_silent_on_the_command_line(self, kanban_home, tmp_path, capsys):
        (kanban_home / "state").mkdir(parents=True, exist_ok=True)
        (kanban_home / "state" / "gateway.heartbeat").write_text(
            json.dumps({"updated_at": _iso(time.time())}), encoding="utf-8")
        config = tmp_path / "health.json"
        config.write_text(json.dumps({"critical_cron_jobs": {}}), encoding="utf-8")
        _write_jobs(kanban_home, [])
        rc = shc.main(["run", "--tier", "light", "--config", str(config),
                       "--state-dir", str(tmp_path / "state")])
        assert rc == 0
        assert capsys.readouterr().out == ""
        beat = json.loads((tmp_path / "state" / "heartbeat-light.json").read_text())
        assert beat["status"] == "GREEN"


# ---------------------------------------------------------------------------
# Refinements from the live-snapshot evidence pass (2026-09-14)
# ---------------------------------------------------------------------------


class TestLiveSnapshotRefinements:
    def test_one_batched_alert_covers_every_new_escalation(self, kanban_home):
        alerts = Alerts()
        ctx = _ctx(kanban_home, alerts=alerts)
        with kb.connect_closing() as conn:
            first = _subject_without_evidence(conn, title="first")
            second = _subject_without_evidence(conn, title="second")
        result = shc.Controller(ctx, [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        assert len(result.escalated) == 2
        assert len(alerts.sent) == 1
        assert first in alerts.sent[0][1] and second in alerts.sent[0][1]
        state = shc.StateStore(ctx.state_dir).load()
        assert all(rec["alert_delivered"] and rec["alert_attempts"] == 1
                   for rec in state["fingerprints"].values())

    def test_blocked_subject_is_not_judged_for_routing(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_evidence_after_handoff(conn)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
        assert shc.VerifierRouteOpen().check(_ctx(kanban_home)) == []

    def test_relabelled_subject_is_not_reported_as_verifier_of_verifier(self, kanban_home):
        """All six codex_verify->codex_verify edges on the live board were this shape."""
        with kb.connect_closing() as conn:
            tid = _subject_with_route(conn, assignee="claude")
            assert kb._open_verifier_child(conn, tid) is not None
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                             (kb.EXECUTOR_LANE_CODEX_VERIFY, tid))
                kb._append_event(conn, tid, "executor_lane_normalized", {
                    "from_assignee": "atlas", "executor_lane": kb.EXECUTOR_LANE_CODEX_VERIFY,
                    "source": "assign_task"})
        ctx = _ctx(kanban_home)
        assert shc.VerifierOfVerifier().check(ctx) == []
        assert [f.subject for f in shc.SubjectLaneRelabelled().check(ctx)] == [tid]

    def test_finished_verifier_chains_are_history_not_health(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            outer = kb._open_verifier_child(conn, subject)
            inner = kb.create_task(conn, title="old re-verification", assignee="atlas",
                                   parents=[outer], gauntlet=True)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (inner,))
        assert shc.VerifierOfVerifier().check(_ctx(kanban_home)) == []


# ---------------------------------------------------------------------------
# Recovery holds (Christopher, 2026-09-14): frozen cards are detected and
# escalated once, never repaired, released, reassigned, promoted or dispatched.
# ---------------------------------------------------------------------------


def _hold_config(*task_ids, **extra):
    hold = {"name": "phase3-freeze", "task_ids": list(task_ids), "reason": "cutover frozen",
            "authorized_by": "test", "release": "remove when the freeze is released"}
    hold.update(extra)
    return {"recovery_holds": [hold]}


def _relabelled_subject(conn, title="phase 3 overlay"):
    tid = _subject_with_route(conn, assignee="claude", title=title)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                     (kb.EXECUTOR_LANE_CODEX_VERIFY, tid))
        kb._append_event(conn, tid, "executor_lane_normalized", {
            "from_assignee": "atlas", "executor_lane": kb.EXECUTOR_LANE_CODEX_VERIFY,
            "source": "assign_task"})
    return tid


def _deadlocked_verification_child(conn, subject):
    child = kb.create_task(conn, title="Independent compliance verification Phase 3 overlay",
                           assignee="reviewer", parents=[subject], gauntlet=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    assert kb.claim_task(conn, child) is None
    return child


def _board_snapshot(conn, ids):
    marks = ",".join("?" for _ in ids)
    return {
        "rows": [tuple(r) for r in conn.execute(
            f"SELECT id, status, assignee, executor_lane, verification_state, claim_lock "
            f"FROM tasks WHERE id IN ({marks}) ORDER BY id", ids)],
        "events": conn.execute(f"SELECT COUNT(*) FROM task_events WHERE task_id IN ({marks})", ids).fetchone()[0],
        "relations": conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0],
        "links": conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0],
    }


class TestRecoveryHold:
    def _invariants(self):
        return [Counting(shc.VerifierRouteOpen()), Counting(shc.SubjectLaneRelabelled()),
                Counting(shc.VerifierChildDeadlocked()), Counting(shc.SubjectReviewRegressed())]

    def test_held_cards_are_detected_but_never_mutated_across_passes(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
            verifier = kb._open_verifier_child(conn, subject)
            child = _deadlocked_verification_child(conn, subject)
            unroutable = _subject_without_evidence(conn, title="compliance audit")
            held_ids = [subject, verifier, child, unroutable]
            before = _board_snapshot(conn, held_ids)
            tasks_before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        invariants = self._invariants()
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts, config=_hold_config(*held_ids))
        controller = shc.Controller(ctx, invariants)

        results = []
        for _ in range(3):
            results.append(controller.run(shc.TIER_LIGHT))
            clock.advance(300)

        assert all(inv.recoveries == 0 for inv in invariants)
        for result in results:
            assert result.recovered == [] and result.open == []
            assert len(result.held) >= 3            # relabel, deadlock, unroutable
            assert result.status == "DEGRADED"
        assert len(results[0].escalated) == 1        # the single hold escalation
        assert results[1].escalated == [] and results[2].escalated == []
        assert len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            assert _board_snapshot(conn, held_ids) == before
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == tasks_before + 1
            (card,) = _health_cards(conn)
            assert card["idempotency_key"].startswith("health:recovery_hold:")
            assert card["status"] == "triage" and card["assignee"] is None
            for tid in (subject, child, unroutable):
                assert tid in card["body"]
        state = shc.StateStore(ctx.state_dir).load()
        held_recs = [r for r in state["fingerprints"].values() if r["invariant"] != shc.HOLD_INVARIANT]
        assert held_recs and all(r["status"] == shc.STATUS_HELD and r["attempts"] == 0 for r in held_recs)

    def test_non_held_defects_are_still_repaired_in_the_same_pass(self, kanban_home):
        with kb.connect_closing() as conn:
            frozen = _relabelled_subject(conn, title="frozen subject")
            free_relabel = _relabelled_subject(conn, title="free subject")
            free_route = _subject_evidence_after_handoff(conn, title="free route")
        ctx = _ctx(kanban_home, config=_hold_config(frozen))
        result = shc.Controller(ctx, self._invariants()).run(shc.TIER_LIGHT)
        assert len(result.recovered) == 2
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, frozen).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert kb.get_task(conn, free_relabel).executor_lane == kb.EXECUTOR_LANE_CLAUDE
            assert kb._open_verifier_child(conn, free_route) is not None

    def test_removing_the_hold_resumes_normal_recovery(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
        held_ctx = _ctx(kanban_home, clock=clock, config=_hold_config(subject))
        assert shc.Controller(held_ctx, self._invariants()).run(shc.TIER_LIGHT).recovered == []
        clock.advance(300)
        released = _ctx(kanban_home, clock=clock, config={})
        result = shc.Controller(released, self._invariants()).run(shc.TIER_LIGHT)
        assert len(result.recovered) == 1
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, subject).executor_lane == kb.EXECUTOR_LANE_CLAUDE

    def test_hold_escalation_stays_one_card_as_suppressed_findings_change(self, kanban_home):
        clock, alerts = Clock(), Alerts()
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (subject,))
        controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts,
                                         config=_hold_config(subject)), self._invariants())
        first = controller.run(shc.TIER_LIGHT)
        assert len(first.held) == 1
        clock.advance(300)
        with kb.connect_closing() as conn:
            # a second, different held finding appears on the same card
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                             (kb.EXECUTOR_LANE_CODEX_VERIFY, subject))
                kb._append_event(conn, subject, "executor_lane_normalized",
                                 {"executor_lane": kb.EXECUTOR_LANE_CODEX_VERIFY, "source": "assign_task"})
        second = controller.run(shc.TIER_LIGHT)
        assert len(second.held) == 2
        with kb.connect_closing() as conn:
            assert len(_health_cards(conn)) == 1
        assert len(alerts.sent) == 1

    def test_a_finding_that_references_a_held_card_is_held_too(self, kanban_home):
        """Only the frozen subject is listed; its gated verification child points at it."""
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            child = _deadlocked_verification_child(conn, subject)
            relations_before = conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0]
        deadlock = Counting(shc.VerifierChildDeadlocked())
        result = shc.Controller(_ctx(kanban_home, config=_hold_config(subject)), [deadlock]).run(shc.TIER_LIGHT)
        assert deadlock.recoveries == 0 and len(result.held) == 1
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT COUNT(*) FROM task_relations").fetchone()[0] == relations_before
            assert kb.get_task(conn, child).status == "todo"

    @pytest.mark.parametrize("bad", [
        {"recovery_holds": [{"name": "x", "task_ids": ["t_1"], "reason": "r", "authorized_by": "a"}]},
        {"recovery_holds": [{"name": "x", "task_ids": [], "reason": "r", "authorized_by": "a", "release": "z"}]},
        {"recovery_holds": {"name": "not a list"}},
    ])
    def test_malformed_hold_fails_closed(self, kanban_home, bad):
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
        with pytest.raises(ValueError):
            shc.Controller(_ctx(kanban_home, config=bad), self._invariants()).run(shc.TIER_LIGHT)
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, subject).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY

    def test_repository_config_holds_the_phase3_cards(self):
        config = json.loads((_CONTROLLER_PATH.parent / "health-controller.json").read_text())
        (hold,) = shc.recovery_holds(config)
        assert hold["task_ids"] == sorted(["t_3883034a", "t_992e8161", "t_4d21959c", "t_15d87799"])
        assert "Christopher" in hold["authorized_by"] and hold["release"]


# ---------------------------------------------------------------------------
# Governed-exception state model (TEMPORARY stabilization, Christopher,
# 2026-09-14): GREEN / GREEN_WITH_HOLDS / RECOVERY / DEGRADED / ESCALATED.
# ---------------------------------------------------------------------------


PHASE3_IDS = ["t_3883034a", "t_992e8161", "t_4d21959c", "t_15d87799"]


def _exception(name, kind, conditions, *, task_ids=None, expires_at=None, **overrides):
    entry = {
        "name": name, "kind": kind, "conditions": list(conditions),
        "owner": "Christopher", "reason": "test reason",
        "authorized_by": "Christopher, 2026-09-14 (test)", "created": "2026-09-14",
        "review_condition": "review when the backlog is cleared", "expires_at": expires_at,
    }
    if task_ids is not None:
        entry["task_ids"] = list(task_ids)
    entry.update(overrides)
    entry["authorization_sha256"] = shc.exception_authorization_digest(entry)
    return entry


def _governed(*entries, recovery_holds=None, model=shc.STATE_MODEL_GOVERNED):
    config = {
        "governed_exceptions": {
            "temporary": True, "authorized_by": "Christopher, 2026-09-14 (test)",
            "purpose": "stabilization", "revert": "delete state_model",
            "entries": list(entries),
        },
    }
    if model is not None:
        config["state_model"] = model
    if recovery_holds is not None:
        config["recovery_holds"] = recovery_holds
    return config


def _bare_lane_verified_closure(conn, title="Recover degraded shared boot gate"):
    tid = kb.create_task(conn, title=title, assignee="default", gauntlet=True)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "verification_passed",
                         {"verifier": "codex_verify", "source": "record_verification"})
        conn.execute("UPDATE tasks SET status = 'done', verification_state = ? WHERE id = ?",
                     (kb.VERIFICATION_VERIFIED, tid))
    return tid


def _relabel_condition(tid, status="review"):
    return f"subject_lane_relabelled|{tid}|subject_on_codex_verify_lane:{status}"


class TestGovernedExceptionStateModel:
    def test_no_holds_and_no_faults_is_green(self, kanban_home):
        result = shc.Controller(_ctx(kanban_home, config=_governed()),
                                [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_GREEN
        beat = shc.StateStore(kanban_home / "state" / "system-health-controller").read_heartbeat("light")
        assert beat["status"] == "GREEN" and beat["state_model"] == "governed_exceptions"
        assert beat["state_model_temporary"] is True

    def test_only_valid_frozen_phase3_holds_is_green_with_holds(self, kanban_home):
        alerts = Alerts()
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
            before = _board_snapshot(conn, [subject])
        inv = Counting(shc.SubjectLaneRelabelled())
        config = _governed(_exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)],
                                      task_ids=[subject]))
        result = shc.Controller(_ctx(kanban_home, alerts=alerts, config=config), [inv]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_GREEN_WITH_HOLDS
        assert inv.recoveries == 0 and len(result.held) == 1
        assert result.classes == {"escalated": 0, "degraded": 0, "recovery": 0, "exception": 2}
        with kb.connect_closing() as conn:
            assert _board_snapshot(conn, [subject])["rows"] == before["rows"]
            (card,) = _health_cards(conn)          # the exception stays visible
            assert card["idempotency_key"].startswith("health:recovery_hold:")
        assert len(alerts.sent) == 1

    def test_only_preserved_false_verified_records_is_green_with_holds(self, kanban_home):
        with kb.connect_closing() as conn:
            first = _bare_lane_verified_closure(conn)
            second = _bare_lane_verified_closure(conn, title="second")
            events_before = [_kinds(conn, first), _kinds(conn, second)]
        conditions = [f"verified_closure_attributable|{t}|verified_by_bare_or_missing_identity"
                      for t in (first, second)]
        config = _governed(_exception("preserved-false-verified", "preserved_condition", conditions,
                                      owner="Erika"))
        result = shc.Controller(_ctx(kanban_home, config=config),
                                [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        assert result.status == shc.AGGREGATE_GREEN_WITH_HOLDS and len(result.held) == 2
        with kb.connect_closing() as conn:
            assert [_kinds(conn, first), _kinds(conn, second)] == events_before
            for tid in (first, second):
                task = kb.get_task(conn, tid)
                assert (task.status, task.verification_state) == ("done", kb.VERIFICATION_VERIFIED)
        beat = shc.StateStore(kanban_home / "state" / "system-health-controller").read_heartbeat("deep")
        (summary,) = beat["exceptions"]
        assert summary["owner"] == "Erika" and summary["conditions_present_this_pass"] == 2

    def test_valid_hold_plus_real_actionable_fault_is_degraded(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
            _subject_without_evidence(conn, title="active work, no evidence")
        config = _governed(_exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)],
                                      task_ids=[subject]))
        result = shc.Controller(_ctx(kanban_home, config=config),
                                [shc.SubjectLaneRelabelled(), shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_DEGRADED
        assert result.classes["degraded"] == 1 and result.classes["exception"] == 2

    @pytest.mark.parametrize("breakage", ["expired", "digest", "missing_owner"])
    def test_expired_or_invalid_hold_is_degraded_and_frozen_work_stays_frozen(self, kanban_home, breakage):
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
        if breakage == "expired":
            entry = _exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)],
                               task_ids=[subject], expires_at="2020-01-01T00:00:00+00:00")
        elif breakage == "digest":   # scope widened after authorization was recorded
            entry = _exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)],
                               task_ids=[subject])
            entry["conditions"].append(f"verifier_route_open|{subject}|no_route:evidence_missing")
        else:
            entry = _exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)],
                               task_ids=[subject], owner="")
        inv = Counting(shc.SubjectLaneRelabelled())
        ctx = _ctx(kanban_home, config=_governed(entry))
        result = shc.Controller(ctx, [inv]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_DEGRADED
        assert inv.recoveries == 0 and result.held == []
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, subject).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
        beat = shc.StateStore(ctx.state_dir).read_heartbeat("light")
        assert beat["invalid_exceptions"] and beat["exceptions"] == []
        state = shc.StateStore(ctx.state_dir).load()
        (validity,) = [r for r in state["fingerprints"].values()
                       if r["invariant"] == shc.EXCEPTION_VALIDITY_INVARIANT]
        assert validity["escalation_reason"] == shc.ESCALATION_EXCEPTION_INVALID
        assert validity["subject"] == "phase3-freeze@light" and validity["card_id"]

    def test_an_invalid_exception_is_itself_an_actionable_fault(self, kanban_home):
        """Nothing it names is present, so only its own invalidity can degrade the pass."""
        alerts = Alerts()
        stale = _exception("expired-preservation", "preserved_condition",
                           ["verified_closure_attributable|t_gone|verified_by_bare_or_missing_identity"],
                           expires_at="2020-01-01")
        result = shc.Controller(_ctx(kanban_home, alerts=alerts, config=_governed(stale)),
                                [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        assert result.status == shc.AGGREGATE_DEGRADED
        assert len(alerts.sent) == 1 and "invalid:expired" in alerts.sent[0][1]

    def test_active_repair_is_recovery(self, kanban_home):
        with kb.connect_closing() as conn:
            _subject_evidence_after_handoff(conn)
        route = Counting(shc.VerifierRouteOpen(), recover_noop=True)
        result = shc.Controller(_ctx(kanban_home, config=_governed()), [route]).run(shc.TIER_LIGHT)
        assert route.recoveries == 1 and len(result.open) == 1
        assert result.status == shc.AGGREGATE_RECOVERY

    def test_repair_that_exhausts_its_budget_is_escalated(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            _subject_evidence_after_handoff(conn)
        controller = shc.Controller(_ctx(kanban_home, clock=clock, config=_governed()),
                                    [Counting(shc.VerifierRouteOpen(), recover_noop=True)])
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_RECOVERY
        clock.advance(300)
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_ESCALATED

    def test_unsafe_fault_is_escalated_at_once_even_on_an_excepted_condition(self, kanban_home):
        class BoundaryBreach(shc.Invariant):
            name = "company_boundary"
            tier = shc.TIER_LIGHT
            confirm_cycles = 3
            recoveries = 0

            def check(self, ctx):
                return [shc.Finding(self.name, "t_x", "cross_lane_attachment", {},
                                    recoverable=True, unsafe=True)]

            def recover(self, ctx, finding):
                BoundaryBreach.recoveries += 1
                return shc.RecoveryOutcome(True, "should_never_run")

        alerts = Alerts()
        config = _governed(_exception("would-cover", "preserved_condition",
                                      ["company_boundary|t_x|cross_lane_attachment"]))
        result = shc.Controller(_ctx(kanban_home, alerts=alerts, config=config),
                                [BoundaryBreach()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_ESCALATED
        assert BoundaryBreach.recoveries == 0 and result.held == []
        assert len(alerts.sent) == 1 and "reason: unsafe" in alerts.sent[0][1]

    def test_held_condition_is_rechecked_every_pass_and_a_material_change_degrades(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
        inv = Counting(shc.SubjectLaneRelabelled())
        ctx = _ctx(kanban_home, clock=clock, config=_governed(
            _exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)], task_ids=[subject])))
        controller = shc.Controller(ctx, [inv])
        for expected_checks in (1, 2, 3):
            assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_GREEN_WITH_HOLDS
            assert inv.checks == expected_checks
            clock.advance(300)
        state = shc.StateStore(ctx.state_dir).load()
        (held,) = [r for r in state["fingerprints"].values() if r["status"] == shc.STATUS_HELD]
        assert held["held_by"] == "phase3-freeze" and held["observations"] == 3

        with kb.connect_closing() as conn:   # the frozen card changes: a new condition
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (subject,))
        changed = controller.run(shc.TIER_LIGHT)
        assert changed.status == shc.AGGREGATE_DEGRADED
        assert inv.recoveries == 0
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, subject).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
        state = shc.StateStore(ctx.state_dir).load()
        assert any(r["escalation_reason"] == shc.ESCALATION_FROZEN_UNCOVERED
                   for r in state["fingerprints"].values() if r.get("escalation_reason"))

    def test_existing_escalation_card_pointer_survives_the_switch_to_held(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _bare_lane_verified_closure(conn)
        ctx_strict = _ctx(kanban_home, config={})
        shc.Controller(ctx_strict, [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        (card_id,) = [r["card_id"] for r in shc.StateStore(ctx_strict.state_dir).load()["fingerprints"].values()]
        config = _governed(_exception("preserved", "preserved_condition",
                                      [f"verified_closure_attributable|{tid}|verified_by_bare_or_missing_identity"]))
        shc.Controller(_ctx(kanban_home, config=config), [shc.VerifiedClosureAttributable()]).run(shc.TIER_DEEP)
        recs = shc.StateStore(ctx_strict.state_dir).load()["fingerprints"].values()
        (held,) = [r for r in recs if r["status"] == shc.STATUS_HELD]
        assert held["previous_card_id"] == card_id
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT status FROM tasks WHERE id = ?", (card_id,)).fetchone()[0] == "triage"

    def test_removing_the_state_model_flag_restores_strict_semantics(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _relabelled_subject(conn)
        entry = _exception("phase3-freeze", "recovery_hold", [_relabel_condition(subject)], task_ids=[subject])
        strict_hold = [{"name": "phase3-freeze", "task_ids": [subject], "reason": "frozen",
                        "authorized_by": "test", "release": "on release"}]
        governed = _governed(entry, recovery_holds=strict_hold)
        assert shc.Controller(_ctx(kanban_home, config=governed),
                              [shc.SubjectLaneRelabelled()]).run(shc.TIER_LIGHT).status == "GREEN_WITH_HOLDS"

        reverted = _governed(entry, recovery_holds=strict_hold, model=None)   # flag deleted
        ctx = _ctx(kanban_home, config=reverted)
        inv = Counting(shc.SubjectLaneRelabelled())
        result = shc.Controller(ctx, [inv]).run(shc.TIER_LIGHT)
        assert result.status == "DEGRADED" and result.state_model == shc.STATE_MODEL_STRICT
        assert inv.recoveries == 0 and len(result.held) == 1      # original hold semantics
        beat = shc.StateStore(ctx.state_dir).read_heartbeat("light")
        assert "state_model" not in beat and beat["status"] == "DEGRADED"
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, subject).executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY

    @pytest.mark.parametrize("bad", [
        {"state_model": "lenient"},
        {"state_model": "governed_exceptions"},
        {"state_model": "governed_exceptions", "governed_exceptions": {"temporary": False, "authorized_by": "a",
                                                                      "purpose": "p", "revert": "r", "entries": []}},
    ])
    def test_malformed_state_model_fails_closed(self, kanban_home, bad):
        with pytest.raises(ValueError):
            shc.Controller(_ctx(kanban_home, config=bad), [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)

    def test_repository_config_governed_exceptions_are_valid_and_temporary(self):
        config = json.loads((_CONTROLLER_PATH.parent / "health-controller.json").read_text())
        assert config["state_model"] == shc.STATE_MODEL_GOVERNED
        block = config["governed_exceptions"]
        assert block["temporary"] is True and "Christopher" in block["authorized_by"] and block["revert"]
        valid, invalid, frozen = shc.governed_exceptions(config, time.time())
        assert invalid == []
        by_kind = {e["kind"]: e for e in valid}
        hold = by_kind["recovery_hold"]
        assert sorted(hold["task_ids"]) == sorted(PHASE3_IDS)
        assert frozen == set(PHASE3_IDS)
        (strict_hold,) = shc.recovery_holds(config)
        assert hold["name"] == strict_hold["name"]      # same aggregate fingerprint and card
        preserved = by_kind["preserved_condition"]["conditions"]
        assert {c.split("|")[1] for c in preserved} == {"t_e48487e5", "t_29c7a57b"}


# ---------------------------------------------------------------------------
# F1 — detect-only invariants (Christopher, 2026-09-14). Every scenario injects
# the fault on an isolated board or filesystem and proves a healthy case stays
# silent. No F1 invariant defines a recovery.
# ---------------------------------------------------------------------------


def _signatures(findings):
    return sorted((f.subject, f.signature) for f in findings)


class TestF1Contract:
    def test_every_invariant_documents_source_failure_evidence_and_tier(self):
        for inv in shc.default_invariants():
            assert inv.tier in shc.TIERS, inv.name
            for field in ("source", "failure", "evidence"):
                assert str(getattr(inv, field)).strip(), f"{inv.name} has no {field}"

    def test_invariants_command_prints_the_metadata_without_touching_anything(self, capsys):
        assert shc.main(["invariants"]) == 0
        listed = {row["name"]: row for row in json.loads(capsys.readouterr().out)}
        assert set(listed) == {inv.name for inv in shc.default_invariants()}
        assert listed["resource_thresholds"]["tier"] == "light" and listed["resource_thresholds"]["detect_only"]
        assert not listed["verifier_route_open"]["detect_only"]

    def test_f1_invariants_are_detection_only(self):
        names = {inv.name for inv in shc.default_invariants()}
        for cls in shc.F1_DETECT_ONLY:
            assert cls.recover is shc.Invariant.recover, cls.__name__
            assert cls.name in names

    def test_f1_findings_mutate_nothing_but_their_escalation_card(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a", assignee="worker")
            b = kb.create_task(conn, title="b", assignee="worker")
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (a, b))
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (b, a))
            before = _board_snapshot(conn, [a, b])
        result = shc.Controller(_ctx(kanban_home, clock=clock), [shc.TaskGraphIntegrity()]).run(shc.TIER_DEEP)
        assert len(result.escalated) == 1 and result.recovered == []
        with kb.connect_closing() as conn:
            assert _board_snapshot(conn, [a, b]) == before
            assert len(_health_cards(conn)) == 1

    def test_escalation_card_carries_the_last_recovery_attempt(self, kanban_home):
        clock = Clock()
        route = Counting(shc.VerifierRouteOpen(), recover_noop=True)
        with kb.connect_closing() as conn:
            _subject_evidence_after_handoff(conn)
        controller = shc.Controller(_ctx(kanban_home, clock=clock), [route])
        controller.run(shc.TIER_LIGHT)
        clock.advance(300)
        controller.run(shc.TIER_LIGHT)
        with kb.connect_closing() as conn:
            (card,) = _health_cards(conn)
        assert "last recovery: `noop_for_test` applied=False" in card["body"]

    def test_repository_config_carries_the_f1_settings(self):
        config = json.loads((_CONTROLLER_PATH.parent / "health-controller.json").read_text())
        assert config["required_gateway_platforms"] == ["telegram", "api_server", "webhook"]
        assert config["control_defect_watch_since"] == 1789427640   # ded3b68681 deploy, 18:14 CDT
        assert config["resource_thresholds"]["disk_used_pct"] == 90
        assert config["life_wiki_daily_note"]["cutoff_hour"] == 6


class TestReadyBacklogExplained:
    def _stranded(self, conn):
        tid = kb.create_task(conn, title="waiting", assignee="worker")
        assert kb.get_task(conn, tid).status == "ready"
        return tid

    def test_spawnable_unclaimed_backlog_is_a_finding(self, kanban_home, monkeypatch):
        monkeypatch.setattr(kb, "resolve_max_in_progress", lambda configured: 5)
        with kb.connect_closing() as conn:
            tid = self._stranded(conn)
        findings = shc.ReadyBacklogExplained().check(_ctx(kanban_home))
        assert _signatures(findings) == [(tid, "spawnable_ready_unclaimed")]

    def test_fresh_ready_work_is_healthy(self, kanban_home, monkeypatch):
        monkeypatch.setattr(kb, "resolve_max_in_progress", lambda configured: 5)
        with kb.connect_closing() as conn:
            self._stranded(conn)
        assert shc.ReadyBacklogExplained().check(_ctx(kanban_home, clock=time.time)) == []

    def test_a_full_concurrency_cap_explains_the_wait(self, kanban_home, monkeypatch):
        monkeypatch.setattr(kb, "resolve_max_in_progress", lambda configured: 0)
        with kb.connect_closing() as conn:
            self._stranded(conn)
        assert shc.ReadyBacklogExplained().check(_ctx(kanban_home)) == []

    def test_a_respawn_guard_explains_the_wait(self, kanban_home, monkeypatch):
        monkeypatch.setattr(kb, "resolve_max_in_progress", lambda configured: 5)
        monkeypatch.setattr(kb, "check_respawn_guard", lambda conn, tid, lane="ready": "rate_limit_cooldown")
        with kb.connect_closing() as conn:
            self._stranded(conn)
        assert shc.ReadyBacklogExplained().check(_ctx(kanban_home)) == []

    def test_an_assignee_nothing_can_spawn_is_a_finding(self, kanban_home, monkeypatch):
        from hermes_cli import profiles
        with kb.connect_closing() as conn:
            tid = self._stranded(conn)
        monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
        findings = shc.ReadyBacklogExplained().check(_ctx(kanban_home))
        assert _signatures(findings) == [(tid, "ready_assignee_not_spawnable")]


class TestRunLeaseConsistency:
    def test_a_live_claim_with_its_open_run_is_healthy(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="worker")
            assert kb.claim_task(conn, tid) is not None
        assert shc.RunLeaseConsistency().check(_ctx(kanban_home, clock=time.time)) == []

    def test_expired_claim_that_nothing_reclaimed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="worker")
            kb.claim_task(conn, tid)
        late = lambda: time.time() + kb.DEFAULT_CLAIM_TTL_SECONDS + 3600  # noqa: E731
        findings = shc.RunLeaseConsistency().check(_ctx(kanban_home, clock=late))
        assert _signatures(findings) == [(tid, "running_claim_expired_unreclaimed")]

    def test_running_card_whose_run_already_ended(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="worker")
            claimed = kb.claim_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE task_runs SET ended_at = ? WHERE id = ?",
                             (int(time.time()), claimed.current_run_id))
        findings = shc.RunLeaseConsistency().check(_ctx(kanban_home, clock=time.time))
        assert _signatures(findings) == [(tid, "running_without_open_run")]

    def test_open_run_left_behind_by_a_card_that_moved_on(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="worker")
            claimed = kb.claim_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        findings = shc.RunLeaseConsistency().check(_ctx(kanban_home, clock=time.time))
        assert _signatures(findings) == [(tid, f"open_run_detached:{claimed.current_run_id}")]

    def test_live_execution_whose_heartbeat_went_stale(self, kanban_home):
        from hermes_cli import exec_supervisor
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="work", assignee="worker")
            record = exec_supervisor.create_execution(
                conn, executor_type="claude", command_class="claude.headless", cwd="/tmp", task_id=tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE executions SET status = 'running', heartbeat_at = ? WHERE id = ?",
                             (int(time.time()) - 5000, record.id))
        findings = shc.RunLeaseConsistency().check(_ctx(kanban_home, clock=time.time))
        assert _signatures(findings) == [(tid, f"execution_heartbeat_stale_unreconciled:{record.id}")]


class TestVerdictReturnedToSubject:
    def _finished_child(self, conn, completed_at):
        subject = _subject_with_route(conn)
        child = kb._open_verifier_child(conn, subject)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                         (int(completed_at), child))
        return subject, child

    def test_finished_verifier_whose_verdict_never_arrived(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, child = self._finished_child(conn, time.time())
        findings = shc.VerdictReturnedToSubject().check(_ctx(kanban_home))
        assert _signatures(findings) == [(subject, f"verifier_done_verdict_undelivered:{child}")]
        assert findings[0].detail["verdict"] == "none"

    @pytest.mark.parametrize("kind", ["verifier_verdict_returned", "verification_blocker_returned",
                                      "verifier_verdict_unreadable"])
    def test_any_return_path_event_counts_as_delivered(self, kanban_home, kind):
        with kb.connect_closing() as conn:
            subject, child = self._finished_child(conn, time.time())
            with kb.write_txn(conn):
                kb._append_event(conn, subject, kind, {"verifier_task": child, "verdict": "BLOCKER"})
        assert shc.VerdictReturnedToSubject().check(_ctx(kanban_home)) == []

    def test_a_just_finished_verifier_is_given_time(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            self._finished_child(conn, clock() - 30)
        assert shc.VerdictReturnedToSubject().check(_ctx(kanban_home, clock=clock)) == []


class TestVerifierChildStalledInTodo:
    def test_codex_verifier_parked_in_todo_is_a_finding(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            child = kb._open_verifier_child(conn, subject)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        findings = shc.VerifierChildStalledInTodo().check(_ctx(kanban_home))
        assert _signatures(findings) == [(child, "codex_verifier_child_stalled_in_todo")]

    def test_a_ready_verifier_is_healthy(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = _subject_with_route(conn)
            assert kb.get_task(conn, kb._open_verifier_child(conn, subject)).status == "ready"
        assert shc.VerifierChildStalledInTodo().check(_ctx(kanban_home)) == []


class TestGatewayPlatformsConnected:
    CONFIG = {"required_gateway_platforms": ["telegram", "api_server", "webhook"]}

    def _write(self, home, **overrides):
        pid = os.getpid()
        doc = {"pid": pid, "gateway_state": "running", "platforms": {
            "telegram": {"state": "connected", "writer_pid": pid},
            "api_server": {"state": "connected", "writer_pid": pid},
            "webhook": {"state": "connected", "writer_pid": pid},
        }}
        doc.update(overrides)
        (home / "gateway_state.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_all_required_platforms_connected_is_healthy(self, kanban_home):
        self._write(kanban_home)
        assert shc.GatewayPlatformsConnected().check(_ctx(kanban_home, config=self.CONFIG)) == []

    def test_platform_faults(self, kanban_home):
        pid = os.getpid()
        self._write(kanban_home, platforms={
            "telegram": {"state": "disconnected", "writer_pid": pid},
            "webhook": {"state": "connected", "writer_pid": pid + 99999},
        })
        findings = shc.GatewayPlatformsConnected().check(_ctx(kanban_home, config=self.CONFIG))
        assert _signatures(findings) == [
            ("api_server", "platform_missing"),
            ("telegram", "platform_state:disconnected"),
            ("webhook", "platform_writer_not_gateway"),
        ]

    def test_dead_gateway_pid_and_unreadable_state(self, kanban_home):
        self._write(kanban_home, pid=2 ** 22 + 4321)
        assert _signatures(shc.GatewayPlatformsConnected().check(_ctx(kanban_home, config=self.CONFIG))) == [
            ("gateway", "gateway_pid_dead")]
        (kanban_home / "gateway_state.json").unlink()
        (sig,) = shc.GatewayPlatformsConnected().check(_ctx(kanban_home, config=self.CONFIG))
        assert sig.signature.startswith("gateway_state_unreadable:")

    def test_unconfigured_is_silent(self, kanban_home):
        assert shc.GatewayPlatformsConnected().check(_ctx(kanban_home, config={})) == []


class _Vfs:
    def __init__(self, used_pct, inode_pct):
        self.f_blocks, self.f_bfree = 1000, 1000 - used_pct * 10
        self.f_bavail = self.f_bfree
        self.f_files, self.f_ffree = 1000, 1000 - inode_pct * 10
        self.f_favail = self.f_ffree


class TestResourceThresholds:
    CONFIG = {"resource_thresholds": {"paths": [], "disk_used_pct": 90, "inode_used_pct": 90,
                                      "mem_available_pct_min": 10, "mem_available_mib_min": 1536,
                                      "swap_used_pct": 90, "load_per_cpu": 2.0, "kanban_wal_mib": 1}}

    def _patch(self, monkeypatch, *, used, inodes, avail_kb, swap_free_kb, load):
        monkeypatch.setattr(shc, "_statvfs", lambda path: _Vfs(used, inodes))
        monkeypatch.setattr(shc, "_meminfo", lambda: {"MemTotal": 8_000_000, "MemAvailable": avail_kb,
                                                     "SwapTotal": 4_000_000, "SwapFree": swap_free_kb})
        monkeypatch.setattr(shc, "_loadavg5", lambda: load)
        monkeypatch.setattr(os, "cpu_count", lambda: 4)

    def test_within_thresholds_is_healthy(self, kanban_home, tmp_path, monkeypatch):
        self._patch(monkeypatch, used=69, inodes=24, avail_kb=5_000_000, swap_free_kb=2_000_000, load=0.5)
        config = {"resource_thresholds": {**self.CONFIG["resource_thresholds"], "paths": [str(tmp_path)]}}
        assert shc.ResourceThresholds().check(_ctx(kanban_home, config=config)) == []

    def test_every_threshold_breach_is_reported(self, kanban_home, tmp_path, monkeypatch):
        self._patch(monkeypatch, used=95, inodes=92, avail_kb=1_000_000, swap_free_kb=100_000, load=9.0)
        with (kanban_home / "kanban.db-wal").open("wb") as fh:
            fh.truncate(3 * 1024 * 1024)
        config = {"resource_thresholds": {**self.CONFIG["resource_thresholds"], "paths": [str(tmp_path)]}}
        findings = shc.ResourceThresholds().check(_ctx(kanban_home, config=config))
        assert {f.signature for f in findings} == {
            f"disk_used_over:{tmp_path}", f"inodes_used_over:{tmp_path}", "memory_available_low",
            "swap_used_over", "load_per_cpu_over", "kanban_wal_oversized"}

    def test_unconfigured_is_silent(self, kanban_home):
        assert shc.ResourceThresholds().check(_ctx(kanban_home, config={})) == []


class TestOwnershipAndLinkage:
    def test_live_verifier_without_subject_but_not_a_relabelled_subject(self, kanban_home):
        with kb.connect_closing() as conn:
            relabelled = _relabelled_subject(conn)
            loose = kb.create_task(conn, title="verify something", assignee="worker")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                             (kb.EXECUTOR_LANE_CODEX_VERIFY, loose))
        findings = shc.OwnershipAndLinkage().check(_ctx(kanban_home))
        assert _signatures(findings) == [(loose, "live_verifier_without_subject")]
        assert relabelled not in {f.subject for f in findings}

    def test_census_functions_are_consumed_for_live_cards_only(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            live = kb.create_task(conn, title="repair", assignee="worker")
            closed = kb.create_task(conn, title="old repair", assignee="worker")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (closed,))
        monkeypatch.setattr(kb, "unowned_tasks", lambda conn: [live])
        monkeypatch.setattr(kb, "orphaned_repair_tasks", lambda conn: [live, closed])
        monkeypatch.setattr(kb, "missing_repair_relations", lambda conn, tid: ["repairs", "umbrella"])
        findings = shc.OwnershipAndLinkage().check(_ctx(kanban_home))
        assert _signatures(findings) == [
            (live, "repair_card_missing_relations:repairs,umbrella"),
            (live, "unowned_live_card"),
        ]


class TestTaskGraphIntegrity:
    def test_cycle_missing_endpoint_given_up_parent_and_unlinked_recovery(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a", assignee="worker")
            b = kb.create_task(conn, title="b", assignee="worker")
            parent = kb.create_task(conn, title="exhausted", assignee="worker")
            child = kb.create_task(conn, title="dependent", assignee="worker", parents=[parent])
            recovery = kb.create_task(conn, title="recover gate", assignee="worker")
            with kb.write_txn(conn):
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (a, b))
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (b, a))
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (a, "t_deadbeef"))
                conn.execute("UPDATE tasks SET status = 'blocked', block_kind = 'attempt_budget_exhausted' "
                             "WHERE id = ?", (parent,))
                conn.execute("UPDATE tasks SET executor_lane = ?, status = 'blocked' WHERE id = ?",
                             (kb.EXECUTOR_LANE_CLAUDE_RECOVERY, recovery))
            assert kb.get_task(conn, child).status == "todo"
        findings = shc.TaskGraphIntegrity().check(_ctx(kanban_home))
        assert _signatures(findings) == sorted([
            (min(a, b), "link_cycle"),
            (a, "link_endpoint_missing"),
            (child, f"waiting_on_given_up_parent:{parent}"),
            (recovery, "recovery_card_unlinked"),
        ])

    def test_an_ordinary_dag_and_a_linked_recovery_card_are_healthy(self, kanban_home):
        with kb.connect_closing() as conn:
            parent = kb.create_task(conn, title="p", assignee="worker")
            kb.create_task(conn, title="c", assignee="worker", parents=[parent])
            recovery = kb.create_task(conn, title="recover", assignee="worker", parents=[parent])
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?",
                             (kb.EXECUTOR_LANE_CLAUDE_RECOVERY, recovery))
        assert shc.TaskGraphIntegrity().check(_ctx(kanban_home)) == []

    def test_cycle_finder(self):
        assert shc._cycles({"a": ["b"], "b": ["c"]}) == []
        assert shc._cycles({"a": ["a"]}) == [frozenset({"a"})]
        assert set(shc._cycles({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})) == {frozenset("abc")}


class TestControlDefectRegressions:
    def _released(self, conn, *, parked_for, owner_comment=False):
        from hermes_cli import exec_supervisor
        tid = kb.create_task(conn, title="parked", assignee="worker")
        record = exec_supervisor.create_execution(
            conn, executor_type="claude", command_class="claude.headless", cwd="/tmp", task_id=tid)
        ended = int(time.time()) - 5000
        with kb.write_txn(conn):
            conn.execute("UPDATE executions SET status = 'timed_out', ended_at = ? WHERE id = ?",
                         (ended, record.id))
            kb._append_event(conn, tid, "blocked", {"kind": "infrastructure"})
        if owner_comment:
            kb.add_comment(conn, tid, "christopher", "leave this parked, I am on it")
        else:
            kb.add_comment(conn, tid, "claude-lane", "timed out")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "gauntlet_stale_disposition", {
                "action": "infrastructure_recovery_released", "execution_id": record.id,
                "detected_at": ended + parked_for, "source": "stale_supervision"})
        return tid, record.id

    def test_release_past_the_retry_window(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, ex = self._released(conn, parked_for=4000)
        findings = shc.ControlDefectRegressions().check(_ctx(kanban_home, config={"control_defect_watch_since": 0}))
        assert _signatures(findings) == [(tid, f"stale_release_violated:retry_window_expired:{ex}")]
        assert not findings[0].unsafe

    def test_release_after_the_owner_engaged(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, ex = self._released(conn, parked_for=100, owner_comment=True)
        findings = shc.ControlDefectRegressions().check(_ctx(kanban_home, config={"control_defect_watch_since": 0}))
        assert _signatures(findings) == [(tid, f"stale_release_violated:owner_engaged:{ex}")]

    def test_prompt_release_of_an_untouched_park_is_healthy(self, kanban_home):
        with kb.connect_closing() as conn:
            self._released(conn, parked_for=100)
        assert shc.ControlDefectRegressions().check(_ctx(kanban_home, config={"control_defect_watch_since": 0})) == []

    def test_harvested_denied_file_is_unsafe_and_escalates(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="shared infra", assignee="worker")
            kb.add_attachment(conn, tid, filename="orion-api.log", stored_path="/tmp/x/orion-api.log",
                              size=10, uploaded_by="claude-lane")
            kb.add_attachment(conn, tid, filename="REWORK-BLOCKER.md", stored_path="/tmp/x/b.md",
                              size=10, uploaded_by="claude-lane")
        alerts = Alerts()
        config = {**_governed(), "control_defect_watch_since": 0}
        result = shc.Controller(_ctx(kanban_home, alerts=alerts, config=config),
                                [shc.ControlDefectRegressions()]).run(shc.TIER_DEEP)
        assert result.status == shc.AGGREGATE_ESCALATED
        body = alerts.sent[0][1]
        assert "harvested_denied_file:" in body and "orion" not in body.lower()

    def test_attachments_before_the_watch_are_not_a_regression(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="shared infra", assignee="worker")
            kb.add_attachment(conn, tid, filename="agent.log", stored_path="/tmp/x/agent.log",
                              size=10, uploaded_by="claude-lane")
        config = {"control_defect_watch_since": int(time.time()) + 3600}
        assert shc.ControlDefectRegressions().check(_ctx(kanban_home, config=config)) == []

    @pytest.mark.parametrize("payload,bad", [
        ({"actor_kind": "human_interactive", "authorized_by": "Christopher", "actor_id": "christopher"}, False),
        ({"actor_kind": "system", "authorized_by": "Christopher", "actor_id": "christopher"}, True),
        ({"actor_kind": "human_interactive", "authorized_by": "default", "actor_id": "christopher"}, True),
        ({"actor_kind": "human_interactive", "authorized_by": "Christopher", "actor_id": "claude-lane"}, True),
    ])
    def test_attempt_grant_provenance(self, kanban_home, payload, bad):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="objective", assignee="worker")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, kb.OBJECTIVE_ATTEMPT_GRANT_EVENT, {**payload, "added_attempts": 2})
        findings = shc.ControlDefectRegressions().check(_ctx(kanban_home, config={"control_defect_watch_since": 0}))
        assert bool(findings) is bad
        assert all(f.unsafe for f in findings)


def _chicago_ts(hour):
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime(2026, 9, 14, hour, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()


class TestLifeWikiDailyNote:
    def _config(self, vault):
        return {"life_wiki_daily_note": {"vault": str(vault), "timezone": "America/Chicago", "cutoff_hour": 6}}

    def test_missing_after_cutoff_present_and_before_cutoff(self, kanban_home, tmp_path):
        vault = tmp_path / "vault"
        (vault / "Logs" / "daily").mkdir(parents=True)
        clock = Clock()
        clock.t = _chicago_ts(7)
        inv = shc.LifeWikiDailyNote()
        assert _signatures(inv.check(_ctx(kanban_home, clock=clock, config=self._config(vault)))) == [
            ("life-wiki-daily-note", "daily_note_missing_after_cutoff:2026-09-14")]
        clock.t = _chicago_ts(5)
        assert inv.check(_ctx(kanban_home, clock=clock, config=self._config(vault))) == []
        (vault / "Logs" / "daily" / "2026-09-14.md").write_text("# day\n", encoding="utf-8")
        clock.t = _chicago_ts(7)
        assert inv.check(_ctx(kanban_home, clock=clock, config=self._config(vault))) == []

    def test_missing_vault(self, kanban_home, tmp_path):
        findings = shc.LifeWikiDailyNote().check(_ctx(kanban_home, config=self._config(tmp_path / "nope")))
        assert _signatures(findings) == [("life-wiki-daily-note", "vault_missing")]


class TestBackupResults:
    def _setup(self, tmp_path, *, verdict="OK", ran_age=60, dump_age=60, dump_bytes=2 * 1024 * 1024, dump=True):
        state = tmp_path / "dr-backup-verify.json"
        ran = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ran_age))
        state.write_text(json.dumps({"verdict": verdict, "ran_at": ran}), encoding="utf-8")
        dumps = tmp_path / "pg"
        dumps.mkdir()
        if dump:
            path = dumps / "hermes-20260914T022048.dump"
            with path.open("wb") as fh:
                fh.truncate(dump_bytes)
            os.utime(path, (time.time() - dump_age, time.time() - dump_age))
        return {"backup_results": {"dr_verify_state": str(state), "dr_max_age_seconds": 93600,
                                   "postgres_dump_dir": str(dumps), "postgres_dump_glob": "hermes-*.dump",
                                   "postgres_max_age_seconds": 93600, "postgres_min_bytes": 1048576}}

    def test_fresh_ok_results_are_healthy(self, kanban_home, tmp_path):
        assert shc.BackupResults().check(_ctx(kanban_home, clock=time.time, config=self._setup(tmp_path))) == []

    def test_failed_stale_and_small_results(self, kanban_home, tmp_path):
        config = self._setup(tmp_path, verdict="FAIL", ran_age=200000, dump_age=200000, dump_bytes=10)
        findings = shc.BackupResults().check(_ctx(kanban_home, clock=time.time, config=config))
        assert {f.signature for f in findings} == {"dr_verdict:FAIL", "dr_verify_stale",
                                                   "postgres_dump_stale", "postgres_dump_too_small"}

    def test_missing_dump(self, kanban_home, tmp_path):
        config = self._setup(tmp_path, dump=False)
        findings = shc.BackupResults().check(_ctx(kanban_home, clock=time.time, config=config))
        assert _signatures(findings) == [("postgres-daily", "postgres_dump_missing")]


class TestEscalationCardsDispositioned:
    def test_controller_cards_left_undispositioned_past_seven_days(self, kanban_home):
        clock = Clock()
        with kb.connect_closing() as conn:
            _subject_without_evidence(conn)
        shc.Controller(_ctx(kanban_home, clock=clock), [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        inv = shc.EscalationCardsDispositioned()
        assert inv.check(_ctx(kanban_home, clock=lambda: time.time() + 86400)) == []
        (finding,) = inv.check(_ctx(kanban_home, clock=lambda: time.time() + 8 * 86400))
        assert finding.signature == "undispositioned_past_threshold" and finding.detail["count"] == "1"


# ---------------------------------------------------------------------------
# F2 — endpoint health, watcher integrity, repository drift, company isolation
# (Christopher, 2026-09-14). Company probes run against real local HTTP servers
# so the no-content and dormant-exclusion guarantees are proven on the wire.
# ---------------------------------------------------------------------------

import contextlib as _contextlib  # noqa: E402
import http.server  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402

SECRET_BODY = b"SECRET-CUSTOMER-DATA card=4242 ssn=123-45-6789"


@_contextlib.contextmanager
def _health_server(status=200, delay=0.0, body_delay=0.0):
    hits: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Set-Cookie", "session=SECRET-COOKIE")
            self.send_header("X-Diagnostic", "SECRET-HEADER")
            self.send_header("Content-Length", str(len(SECRET_BODY)))
            self.end_headers()
            self.wfile.flush()
            if body_delay:
                time.sleep(body_delay)
            try:
                self.wfile.write(SECRET_BODY)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/healthz", hits
    finally:
        server.shutdown()
        server.server_close()


def _probe_block(entities, excluded=(("ENT-001", "North Caledonia"),), record_dir=None, timeout=2):
    block = {
        "authorized_by": "Christopher, 2026-09-14 (test)",
        "boundaries": "status line only",
        "timeout_seconds": timeout,
        "entities": [{"entity_id": eid, "name": name, "service": svc, "url": url}
                     for eid, name, svc, url in entities],
        "excluded_entities": [{"entity_id": eid, "name": name, "reason": "dormant"} for eid, name in excluded],
    }
    if record_dir is not None:
        block["record_dir"] = str(record_dir)
    block["authorization_sha256"] = shc.company_probe_authorization_digest(block)
    return {"company_health_probes": block}


class TestF2Contract:
    def test_f2_invariants_are_detection_only_and_registered(self):
        names = {inv.name for inv in shc.default_invariants()}
        for cls in shc.F2_DETECT_ONLY:
            assert cls.recover is shc.Invariant.recover, cls.__name__
            assert cls.name in names

    @pytest.mark.parametrize("url", ["http://example.com/health", "https://127.0.0.1:1/health",
                                     "http://user:pw@127.0.0.1:1/health", "http://10.0.0.5:5160/api/healthz"])
    def test_probe_refuses_non_loopback_tls_or_credentialed_urls(self, url):
        with pytest.raises(ValueError):
            shc._http_probe(url, 1)

    def test_repository_config_probes_exactly_the_authorized_active_companies(self):
        config = json.loads((_CONTROLLER_PATH.parent / "health-controller.json").read_text())
        block = config["company_health_probes"]
        assert shc._company_probe_problem(block) is None
        assert {e["entity_id"]: e["name"] for e in block["entities"]} == {
            "ENT-004": "Orion Formation Services", "ENT-003": "Logos Covenant", "ENT-007": "The Glass Pepper"}
        assert {e["entity_id"] for e in block["excluded_entities"]} == {"ENT-001", "ENT-002"}
        assert all(e["url"].startswith("http://127.0.0.1:") for e in block["entities"])
        assert "Christopher" in block["authorized_by"]
        assert "http://127.0.0.1:8082" not in json.dumps(config)   # north caledonia's unit is never probed


class TestCompanyHealthEndpoints:
    def test_healthy_probe_records_only_the_allowed_fields_and_reads_no_content(self, kanban_home, tmp_path):
        record_dir = tmp_path / "company-health"
        with _health_server(200) as (url, hits):
            config = _probe_block([("ENT-004", "Orion", "orion-api", url)], record_dir=record_dir)
            assert shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config)) == []
        (hit,) = hits
        assert "authorization" not in hit["headers"] and "cookie" not in hit["headers"]
        path = record_dir / "ENT-004.json"
        record = json.loads(path.read_text())
        assert set(record) == {"entity_id", "service", "timestamp", "http_status", "latency_ms", "result"}
        assert record["http_status"] == 200 and record["result"] == "HEALTHY"
        assert b"SECRET" not in path.read_bytes()
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_a_dry_run_probe_writes_no_company_record(self, kanban_home, tmp_path):
        record_dir = tmp_path / "company-health"
        with _health_server(503) as (url, hits):
            config = _probe_block([("ENT-007", "Glass Pepper", "glass-pepper-api", url)], record_dir=record_dir)
            findings = shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config, dry_run=True))
        assert [f.signature for f in findings] == ["HTTP_5XX"] and len(hits) == 1
        assert not record_dir.exists()

    def test_the_probe_returns_on_the_status_line_without_waiting_for_the_body(self):
        with _health_server(200, body_delay=2.0) as (url, _):
            started = time.monotonic()
            coarse, status, _latency = shc._http_probe(url, 5)
            elapsed = time.monotonic() - started
        assert (coarse, status) == ("HEALTHY", 200)
        assert elapsed < 1.5, "the probe waited for (read) the response body"

    def test_failed_company_probe_escalates_coarse_state_without_a_shared_card(self, kanban_home, tmp_path):
        clock, alerts = Clock(), Alerts()
        with _health_server(503) as (url, _hits):
            config = _probe_block([("ENT-003", "Logos", "logos-covenant-api", url)], record_dir=tmp_path / "rec")
            controller = shc.Controller(_ctx(kanban_home, clock=clock, alerts=alerts, config=config),
                                        [shc.CompanyHealthEndpoints()])
            assert controller.run(shc.TIER_LIGHT).escalated == []      # confirm cycle
            clock.advance(300)
            assert len(controller.run(shc.TIER_LIGHT).escalated) == 1
        (subject, text) = alerts.sent[0]
        assert "ENT-003" in text and "HTTP_5XX" in text and "company lane" in text
        for leaked in ("503", "SECRET", "logos-covenant-api", "Logos"):
            assert leaked not in text and leaked not in subject
        with kb.connect_closing() as conn:
            assert _health_cards(conn) == []
        (rec,) = shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
        assert rec["route"] == "company" and rec["card_id"] is None

    def test_unreachable_and_timeout(self, kanban_home, tmp_path):
        with _health_server(200) as (url, _):
            dead = url
        with _health_server(200, delay=1.5) as (slow, _):
            config = _probe_block([("ENT-004", "Orion", "orion-api", dead),
                                   ("ENT-007", "Glass Pepper", "glass-pepper-api", slow)],
                                  record_dir=tmp_path / "rec", timeout=0.3)
            findings = shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config))
        assert _signatures(findings) == [("ENT-004", "UNREACHABLE"), ("ENT-007", "TIMEOUT")]
        assert all(f.route == "company" for f in findings)

    def test_dormant_company_with_a_reachable_endpoint_is_never_polled(self, kanban_home, tmp_path):
        with _health_server(200) as (active_url, active_hits), _health_server(200) as (dormant_url, dormant_hits):
            config = _probe_block([("ENT-004", "Orion", "orion-api", active_url)],
                                  excluded=[("ENT-001", "North Caledonia")], record_dir=tmp_path / "rec")
            assert shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config)) == []
            assert len(active_hits) == 1 and dormant_hits == []
            assert not (tmp_path / "rec" / "ENT-001.json").exists()

    def test_listing_a_dormant_company_fails_closed_and_probes_nothing(self, kanban_home, tmp_path):
        with _health_server(200) as (active_url, active_hits), _health_server(200) as (dormant_url, dormant_hits):
            config = _probe_block([("ENT-004", "Orion", "orion-api", active_url),
                                   ("ENT-001", "North Caledonia", "northcaledonia-api", dormant_url)],
                                  excluded=[("ENT-001", "North Caledonia")], record_dir=tmp_path / "rec")
            findings = shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config))
            assert _signatures(findings) == [("company_health_probes", "probe_authorization_invalid:excluded_entity_listed")]
            assert active_hits == [] and dormant_hits == []

    def test_an_unauthorized_addition_to_the_probe_set_probes_nothing(self, kanban_home, tmp_path):
        with _health_server(200) as (url, hits), _health_server(200) as (extra_url, extra_hits):
            config = _probe_block([("ENT-004", "Orion", "orion-api", url)], record_dir=tmp_path / "rec")
            config["company_health_probes"]["entities"].append(
                {"entity_id": "ENT-005", "name": "Trip Tracker", "service": "triptracker", "url": extra_url})
            findings = shc.CompanyHealthEndpoints().check(_ctx(kanban_home, config=config))
            assert _signatures(findings) == [("company_health_probes", "probe_authorization_invalid:authorization_digest_mismatch")]
            assert hits == [] and extra_hits == []


class TestSharedEndpointsAndUnits:
    def test_shared_endpoint_health(self, kanban_home):
        with _health_server(200) as (ok, _), _health_server(502) as (bad, _):
            config = {"shared_health_endpoints": {"gateway": ok, "command-center": bad}}
            findings = shc.SharedEndpointsHealthy().check(_ctx(kanban_home, config=config))
        assert _signatures(findings) == [("command-center", "HTTP_5XX")]
        assert findings[0].detail == {"http_status": "502"} and findings[0].route == "shared"

    def test_shared_units_must_be_active(self, kanban_home):
        seen = []

        def runner(argv, timeout):
            seen.append(argv)
            unit = argv[-1]
            return (0, "active\n") if unit in ("hermes-gateway.service", "caddy.service") else (3, "failed\n")

        config = {"shared_user_units": ["hermes-gateway.service", "life-wiki-api.service"],
                  "shared_system_units": ["caddy.service", "postgresql@16-main.service"]}
        findings = shc.SharedUnitsActive().check(_ctx(kanban_home, config=config, run_command=runner))
        assert _signatures(findings) == [("life-wiki-api.service", "not_active:failed"),
                                         ("postgresql@16-main.service", "not_active:failed")]
        assert ["systemctl", "--user", "is-active", "hermes-gateway.service"] in seen
        assert ["systemctl", "is-active", "caddy.service"] in seen


class TestWatcherIntegrity:
    def test_service_results_unit_files_crontab_and_script_pins(self, kanban_home, tmp_path):
        good = tmp_path / "good.sh"
        good.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        drifted = tmp_path / "drifted.sh"
        drifted.write_text("#!/bin/sh\necho changed\n", encoding="utf-8")

        def runner(argv, timeout):
            if "list-timers" in argv:
                return 0, json.dumps([{"unit": "a.timer", "activates": "a.service"},
                                      {"unit": "b.timer", "activates": "b.service"}])
            if "is-failed" in argv:
                return (0, "failed\n") if argv[-1] == "b.service" else (1, "inactive\n")
            if "list-unit-files" in argv:
                return 0, json.dumps([{"unit_file": "present-runner.service"}])
            if argv[:2] == ["crontab", "-l"]:
                return 0, "*/5 * * * * TOKEN=do-not-store /x/watchdog.sh\n"
            return 1, ""

        config = {
            "critical_user_timers": {"a.timer": {}, "b.timer": {}},
            "expected_user_unit_files": ["present-runner.service", "missing-runner.service"],
            "crontab_watchers": ["watchdog.sh", "reaper.sh"],
            "pinned_scripts": {str(good): shc._sha256_file(good), str(drifted): "0" * 64,
                               str(tmp_path / "gone.sh"): "0" * 64},
        }
        findings = shc.WatcherIntegrity().check(_ctx(kanban_home, config=config, run_command=runner))
        assert _signatures(findings) == sorted([
            ("b.service", "timer_service_failed"),
            ("missing-runner.service", "unit_file_missing"),
            ("reaper.sh", "crontab_entry_missing"),
            (str(drifted), "script_checksum_drift"),
            (str(tmp_path / "gone.sh"), "script_missing"),
        ])
        assert "do-not-store" not in json.dumps([f.detail for f in findings])


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],
                   check=True, capture_output=True, text=True)


class TestRepositoryDrift:
    def test_dirty_unpushed_and_deploy_drift_verdicts(self, kanban_home, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-q", "-m", "one")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        (repo / "b.txt").write_text("b", encoding="utf-8")
        _git(repo, "add", "b.txt")
        _git(repo, "commit", "-q", "-m", "two")
        (repo / "dirty.txt").write_text("x", encoding="utf-8")
        clean = tmp_path / "clean"
        clean.mkdir()
        _git(clean, "init", "-q")
        state = tmp_path / "drift.json"
        state.write_text(json.dumps({"verdict": "DRIFT", "ran_at": "2020-01-01T00:00:00Z"}), encoding="utf-8")
        config = {"repository_drift": {"deploy_drift_state": str(state), "deploy_drift_max_age_seconds": 25200,
                                       "watched_repositories": [
                                           {"name": "runtime", "path": str(repo), "upstream": "origin/main"},
                                           {"name": "doctrine", "path": str(clean), "upstream": None},
                                           {"name": "missing", "path": str(tmp_path / "nope"), "upstream": None}]}}
        findings = shc.RepositoryDrift().check(_ctx(kanban_home, clock=time.time, config=config,
                                                    run_command=shc.run_command))
        assert _signatures(findings) == sorted([
            ("deploy-drift-check", "deploy_drift_verdict:DRIFT"), ("deploy-drift-check", "deploy_drift_stale"),
            ("runtime", "worktree_dirty"), ("runtime", "unpushed_commits"), ("missing", "repo_unreadable")])

    def test_clean_state_is_healthy(self, kanban_home, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        state = tmp_path / "drift.json"
        state.write_text(json.dumps({"verdict": "CLEAN", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                         encoding="utf-8")
        config = {"repository_drift": {"deploy_drift_state": str(state),
                                       "watched_repositories": [{"name": "r", "path": str(repo), "upstream": None}]}}
        assert shc.RepositoryDrift().check(_ctx(kanban_home, clock=time.time, config=config,
                                                run_command=shc.run_command)) == []


ISOLATION_COMPANIES = {
    "ENT-003": {"tokens": ["logos"], "lead_profiles": ["logos_covenant_lead"]},
    "ENT-004": {"tokens": ["orion"], "lead_profiles": ["orion_formation_services_lead"]},
}


class TestCompanyIsolation:
    def _config(self, watch_since, rc=0):
        return {"company_isolation": {"registry_check_command": ["entity-registry-check"],
                                      "isolation_watch_since": watch_since, "companies": ISOLATION_COMPANIES}}

    def _board(self, conn):
        orion_card = kb.create_task(conn, title="orion work", assignee="orion_formation_services_lead")
        shared_card = kb.create_task(conn, title="infra work", assignee="worker")
        kb.add_attachment(conn, orion_card, filename="orion-funnel.md", stored_path="/tmp/a", size=1, uploaded_by="lead")
        kb.add_attachment(conn, orion_card, filename="orion-api.log", stored_path="/tmp/b", size=1, uploaded_by="lead")
        kb.add_attachment(conn, orion_card, filename="logos-invoice.pdf", stored_path="/tmp/c", size=1, uploaded_by="lead")
        kb.add_attachment(conn, shared_card, filename="Orion-API.log", stored_path="/tmp/d", size=1, uploaded_by="claude-lane")
        kb.add_attachment(conn, shared_card, filename="agent.log", stored_path="/tmp/e", size=1, uploaded_by="claude-lane")
        kb.add_attachment(conn, shared_card, filename="PHASE-D-EVIDENCE.md", stored_path="/tmp/f", size=1, uploaded_by="op")
        return orion_card, shared_card

    def test_new_boundary_attachments_are_unsafe_per_card(self, kanban_home):
        with kb.connect_closing() as conn:
            orion_card, shared_card = self._board(conn)
        runner = lambda argv, timeout: (0, "names ENT-004 Orion routing")  # noqa: E731
        findings = shc.CompanyIsolation().check(_ctx(kanban_home, config=self._config(0), run_command=runner))
        assert _signatures(findings) == sorted([(orion_card, "boundary_attachments_on_card"),
                                                (shared_card, "boundary_attachments_on_card")])
        counts = {f.subject: f.detail["count"] for f in findings}
        assert counts == {orion_card: "1", shared_card: "2"}     # own-company files on its lead card are not foreign
        assert all(f.unsafe for f in findings)
        blob = json.dumps([(f.subject, f.signature, f.detail) for f in findings]).lower()
        assert "orion" not in blob and "logos" not in blob and ".log" not in blob

    def test_history_is_one_unsafe_inventory_finding_and_is_never_excepted(self, kanban_home):
        with kb.connect_closing() as conn:
            orion_card, shared_card = self._board(conn)
        alerts = Alerts()
        condition = "company_isolation|attachment-boundary-inventory|historical_boundary_attachment_inventory"
        config = {**self._config(int(time.time()) + 3600),
                  **_governed(_exception("would-hide", "preserved_condition", [condition]))}
        result = shc.Controller(_ctx(kanban_home, alerts=alerts, config=config,
                                     run_command=lambda argv, t: (0, "")),
                                [shc.CompanyIsolation()]).run(shc.TIER_DEEP)
        assert result.status == shc.AGGREGATE_ESCALATED and result.held == []
        state = shc.StateStore(_ctx(kanban_home).state_dir).load()
        (rec,) = [r for r in state["fingerprints"].values() if r["invariant"] == "company_isolation"]
        assert rec["subject"] == "attachment-boundary-inventory" and rec["escalation_reason"] == "unsafe"
        with kb.connect_closing() as conn:
            (card,) = [c for c in _health_cards(conn) if "company_isolation" in c["title"]]
        assert orion_card in card["body"] and shared_card in card["body"]
        assert "orion-api" not in card["body"].lower() and "orion-api" not in alerts.sent[0][1].lower()

    @pytest.mark.parametrize("rc,signature", [(1, "registry_runtime_drift"), (2, "registry_check_incomplete"),
                                              (127, "registry_check_failed:rc=127"), (0, None)])
    def test_registry_check_exit_codes_output_discarded(self, kanban_home, rc, signature):
        config = {"company_isolation": {"registry_check_command": ["entity-registry-check"], "companies": {}}}
        findings = shc.CompanyIsolation().check(_ctx(kanban_home, config=config,
                                                      run_command=lambda argv, t: (rc, "ENT-004 Orion secret routing")))
        assert [f.signature for f in findings] == ([signature] if signature else [])
        assert "Orion" not in json.dumps([f.detail for f in findings])


# ---------------------------------------------------------------------------
# F3 — bounded recovery classes (Christopher, 2026-09-14): build and test only.
# Generic controller flow first (a fake class), then each class against its
# real mechanism on an isolated board / git remote / HTTP server / fake systemd.
# ---------------------------------------------------------------------------

AUTHORIZER = "Christopher, 2026-09-14 (test)"


def _authorize(config, name, **spec):
    entry = {"enabled": True, "authorized_by": AUTHORIZER, **spec}
    entry["authorization_sha256"] = shc.recovery_authorization_digest(name, entry)
    config.setdefault("recovery_classes", {})[name] = entry
    return config


class Toggle(shc.Invariant):
    """A detector over a mutable ``state`` dict."""

    name = "toggle_detector"
    tier = shc.TIER_LIGHT
    source = failure = evidence = "test"

    def __init__(self, state):
        self.state = state
        self.checks = 0

    def check(self, ctx):
        self.checks += 1
        return [shc.Finding(self.name, s, "broken", {}) for s in sorted(self.state.get("broken", ()))]


class FakeRecovery(shc.RecoveryClass):
    name = "fake_recovery"
    binds = {"toggle_detector": ("broken",)}
    trigger = mutation = authorization_boundary = validator = rollback = escalation_only = "test"

    def __init__(self, state, *, fixes=True, post=None, gate=None, after_cycles=1, settle=0, recurrence=3600,
                 max_deferral=3600):
        self.state = state
        self.fixes = fixes
        self.post = post or {}
        self.gate_decision = gate
        self.recover_after_cycles = after_cycles
        self.recurrence_window_seconds = recurrence
        self.max_deferral_seconds = max_deferral
        self._settle = settle
        self.recoveries = []
        self.gates = 0

    def settle_seconds(self, spec):
        return self._settle

    def gate(self, ctx, finding, rec, spec):
        self.gates += 1
        if self.gate_decision:
            return self.gate_decision
        if finding.subject not in self.state["broken"] and not rec.get("recovery_pending"):
            return shc.Gate(shc.GATE_CLEARED)
        return shc.Gate(shc.GATE_PROCEED)

    def recover(self, ctx, finding, spec):
        self.recoveries.append(finding.subject)
        if self.fixes:
            self.state["broken"].discard(finding.subject)
        return shc.RecoveryOutcome(True, "fake_fix")

    def postcondition(self, ctx, finding, rec, spec):
        return self.post.get("value")


def _fake_controller(kanban_home, state, klass, *, clock=None, alerts=None, model=True):
    config = _authorize(_governed() if model else {}, klass.name)
    ctx = _ctx(kanban_home, clock=clock or Clock(), alerts=alerts or Alerts(), config=config)
    return shc.Controller(ctx, [Toggle(state)], recoveries=[klass]), ctx


class TestF3Contract:
    def test_every_class_documents_its_boundary_and_is_bounded(self):
        for klass in shc.default_recovery_classes():
            for field in ("trigger", "mutation", "authorization_boundary", "validator", "rollback", "escalation_only"):
                assert str(getattr(klass, field)).strip(), f"{klass.name} has no {field}"
            assert klass.max_attempts == 2, klass.name        # the first attempt plus one bounded retry

    def test_no_class_binds_an_escalate_only_condition(self):
        for klass in shc.default_recovery_classes():
            for invariant, prefixes in klass.binds.items():
                assert invariant not in shc.ESCALATE_ONLY_INVARIANTS, (klass.name, invariant)
                assert not any(p.startswith(("worktree_dirty", "deploy_drift", "repo_unreadable", "no_route:evidence_ready",
                                             "paused_without_reason", "pause_expired", "script_missing", "job_missing"))
                               for p in prefixes), (klass.name, prefixes)
        bound = {inv for klass in shc.default_recovery_classes() for inv in klass.binds}
        for name in ("subject_lane_relabelled", "verifier_child_deadlocked"):   # v1 verifier routing stays in-code
            assert name not in bound

    def test_repository_config_declares_every_class_disabled(self):
        config = json.loads((_CONTROLLER_PATH.parent / "health-controller.json").read_text())
        names = {k for k in config["recovery_classes"] if k != "status"}
        assert names == {c.name for c in shc.default_recovery_classes()}
        for klass in shc.default_recovery_classes():
            assert config["recovery_classes"][klass.name]["enabled"] is False
            assert shc.recovery_authorization(config, klass) == (None, None)

    def test_invalid_authorization_runs_nothing_and_degrades(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state)
        config = _authorize(_governed(), klass.name)
        config["recovery_classes"][klass.name]["authorized_by"] = "someone else"     # scope edited, digest stale
        result = shc.Controller(_ctx(kanban_home, config=config), [Toggle(state)], recoveries=[klass]).run(shc.TIER_LIGHT)
        assert klass.recoveries == [] and result.status == shc.AGGREGATE_DEGRADED
        state_file = shc.StateStore(_ctx(kanban_home).state_dir).load()
        assert any(r["invariant"] == shc.RECOVERY_VALIDITY_INVARIANT and r["signature"] ==
                   "invalid:authorization_digest_mismatch" for r in state_file["fingerprints"].values())

    @pytest.mark.parametrize("variant", ["company_route", "unsafe", "frozen"])
    def test_company_unsafe_and_frozen_findings_are_never_recovered(self, kanban_home, variant):
        class Detector(shc.Invariant):
            name = "toggle_detector"
            tier = shc.TIER_LIGHT
            source = failure = evidence = "test"

            def check(self, ctx):
                return [shc.Finding(self.name, "t_frozen1" if variant == "frozen" else "x", "broken", {},
                                    recoverable=True, unsafe=variant == "unsafe",
                                    route="company" if variant == "company_route" else "shared")]

        klass = FakeRecovery({"broken": {"x", "t_frozen1"}})
        config = _authorize(_governed(), klass.name)
        if variant == "frozen":
            config["recovery_holds"] = [{"name": "freeze", "task_ids": ["t_frozen1"], "reason": "r",
                                         "authorized_by": "a", "release": "z"}]
        controller = shc.Controller(_ctx(kanban_home, config=config), [Detector()], recoveries=[klass])
        controller.run(shc.TIER_LIGHT)
        shc.Controller(_ctx(kanban_home, clock=lambda: time.time() + 7200, config=config), [Detector()],
                       recoveries=[klass]).run(shc.TIER_LIGHT)
        assert klass.recoveries == [] and klass.gates == 0

    def test_v1_verifier_routing_keeps_its_in_code_path(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_evidence_after_handoff(conn)
        config = _authorize(_governed(), "evidence_attachment", patterns=["*EVIDENCE*.md"], max_files=5, max_bytes=1024)
        route = Counting(shc.VerifierRouteOpen())
        result = shc.Controller(_ctx(kanban_home, config=config), [route]).run(shc.TIER_LIGHT)
        assert route.recoveries == 1 and len(result.recovered) == 1
        with kb.connect_closing() as conn:
            assert kb._open_verifier_child(conn, tid) is not None


class TestF3ControllerFlow:
    def test_detector_first_then_after_cycles_then_recovered_and_idempotent(self, kanban_home):
        state = {"broken": set()}
        klass = FakeRecovery(state, after_cycles=2)
        clock = Clock()
        controller, ctx = _fake_controller(kanban_home, state, klass, clock=clock)
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_GREEN and klass.gates == 0   # no detection
        state["broken"].add("x")
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_DEGRADED and klass.recoveries == []
        clock.advance(300)
        second = controller.run(shc.TIER_LIGHT)
        assert klass.recoveries == ["x"] and len(second.recovered) == 1 and second.status == shc.AGGREGATE_GREEN
        for _ in range(3):
            clock.advance(300)
            assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_GREEN
        assert klass.recoveries == ["x"]                                                  # idempotent: no repeat
        (entry,) = shc.StateStore(ctx.state_dir).load()["recovery_history"].values()
        assert entry["recovery_class"] == "fake_recovery" and entry["attempts"] == 1

    def test_failing_recovery_is_bounded_to_two_attempts_and_escalates_once(self, kanban_home):
        state = {"broken": {"x"}}
        alerts = Alerts()
        klass = FakeRecovery(state, fixes=False)
        clock = Clock()
        controller, ctx = _fake_controller(kanban_home, state, klass, clock=clock, alerts=alerts)
        statuses = []
        for _ in range(6):
            statuses.append(controller.run(shc.TIER_LIGHT).status)
            clock.advance(300)
        assert klass.recoveries == ["x", "x"]
        assert statuses[0] == shc.AGGREGATE_RECOVERY and statuses[-1] == shc.AGGREGATE_ESCALATED
        assert len(alerts.sent) == 1
        with kb.connect_closing() as conn:
            assert len(_health_cards(conn)) == 1

    def test_unchanged_recurrence_inside_the_window_escalates_without_mutation(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        controller.run(shc.TIER_LIGHT)
        assert klass.recoveries == ["x"]
        clock.advance(300)
        state["broken"].add("x")
        result = controller.run(shc.TIER_LIGHT)
        assert klass.recoveries == ["x"] and klass.gates == 1
        assert result.status == shc.AGGREGATE_ESCALATED
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "toggle_detector"][0]
        assert rec["escalation_reason"] == shc.ESCALATION_RECURRED

    def test_recurrence_after_the_window_is_a_fresh_bounded_episode(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state, recurrence=600)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        controller.run(shc.TIER_LIGHT)
        clock.advance(900)
        state["broken"].add("x")
        assert len(controller.run(shc.TIER_LIGHT).recovered) == 1 and klass.recoveries == ["x", "x"]

    def test_refused_gate_escalates_without_mutation(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state, gate=shc.Gate(shc.GATE_REFUSED, "ambiguous_state"))
        controller, _ = _fake_controller(kanban_home, state, klass)
        result = controller.run(shc.TIER_LIGHT)
        assert klass.recoveries == [] and result.status == shc.AGGREGATE_ESCALATED
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "toggle_detector"][0]
        assert rec["escalation_reason"] == "recovery_refused:ambiguous_state"

    def test_deferral_is_bounded(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state, gate=shc.Gate(shc.GATE_DEFERRED, "cooldown_active"), max_deferral=900)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        statuses = []
        for _ in range(5):
            statuses.append(controller.run(shc.TIER_LIGHT).status)
            clock.advance(300)
        assert klass.recoveries == []
        assert statuses[:3] == [shc.AGGREGATE_DEGRADED] * 3 and statuses[-1] == shc.AGGREGATE_ESCALATED

    def test_failed_postcondition_is_not_silently_resolved_when_the_detector_goes_quiet(self, kanban_home):
        state = {"broken": {"x"}}
        post = {"value": "health_endpoint:HTTP_5XX"}
        klass = FakeRecovery(state, post=post)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        first = controller.run(shc.TIER_LIGHT)                  # detector quiet after fix, postcondition fails
        assert first.recovered == [] and first.status == shc.AGGREGATE_RECOVERY
        clock.advance(300)
        second = controller.run(shc.TIER_LIGHT)                 # carried, retried once more, still failing
        assert klass.recoveries == ["x", "x"] and second.status == shc.AGGREGATE_ESCALATED
        post["value"] = None
        clock.advance(300)
        controller.run(shc.TIER_LIGHT)
        assert klass.recoveries == ["x", "x"]

    def test_settle_window_waits_without_a_second_mutation(self, kanban_home):
        state = {"broken": {"x"}}
        post = {"value": "job_not_rerun_yet"}
        klass = FakeRecovery(state, fixes=False, post=post, settle=1800)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_RECOVERY
        clock.advance(300)
        assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_RECOVERY and klass.recoveries == ["x"]
        state["broken"].discard("x")
        post["value"] = None
        clock.advance(300)
        assert len(controller.run(shc.TIER_LIGHT).recovered) == 1 and klass.recoveries == ["x"]

    def test_settle_window_class_is_still_bounded_to_two_mutations(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state, fixes=False, post={"value": "job_not_rerun_yet"}, settle=600)
        clock = Clock()
        controller, _ = _fake_controller(kanban_home, state, klass, clock=clock)
        statuses = []
        for _ in range(12):
            statuses.append(controller.run(shc.TIER_LIGHT).status)
            clock.advance(300)
        assert klass.recoveries == ["x", "x"] and statuses[-1] == shc.AGGREGATE_ESCALATED

    def test_recovery_for_refuses_a_frozen_task_directly(self, kanban_home):
        state = {"broken": set()}
        klass = FakeRecovery(state)
        config = _authorize(_governed(), klass.name)
        config["recovery_holds"] = [{"name": "freeze", "task_ids": ["t_frozen1"], "reason": "r",
                                     "authorized_by": "a", "release": "z"}]
        controller = shc.Controller(_ctx(kanban_home, config=config), [Toggle(state)], recoveries=[klass])
        controller.run(shc.TIER_LIGHT)
        inv = Toggle(state)
        assert controller._recovery_for(inv, shc.Finding("toggle_detector", "t_frozen1", "broken", {}), None) is None
        assert controller._recovery_for(inv, shc.Finding("toggle_detector", "x", "broken", {"parent": "t_frozen1"}),
                                        None) is None
        assert controller._recovery_for(inv, shc.Finding("toggle_detector", "x", "broken", {}), None) is not None

    def test_dry_run_never_gates_or_recovers(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state)
        config = _authorize(_governed(), klass.name)
        shc.Controller(_ctx(kanban_home, config=config, dry_run=True), [Toggle(state)],
                       recoveries=[klass]).run(shc.TIER_LIGHT)
        assert klass.gates == 0 and klass.recoveries == []

    def test_unauthorized_class_leaves_the_detector_escalate_only(self, kanban_home):
        state = {"broken": {"x"}}
        klass = FakeRecovery(state)
        result = shc.Controller(_ctx(kanban_home, config=_governed()), [Toggle(state)],
                                recoveries=[klass]).run(shc.TIER_LIGHT)
        assert klass.gates == 0 and len(result.escalated) == 1


# -- class 1: gateway -----------------------------------------------------------


class FakeSystemd:
    def __init__(self, states, *, results=None, installed=("hermes-gateway.service",), on_restart=None):
        self.states = dict(states)
        self.results = dict(results or {})
        self.installed = set(installed)
        self.on_restart = on_restart
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        if argv[-2:] == ["gateway", "restart"]:
            if self.on_restart:
                self.on_restart("hermes-gateway.service")
            return 0, ""
        if argv[:2] == ["systemctl", "is-active"]:
            state = self.states.get(argv[-1], "active")
            return (0 if state == "active" else 3), state + "\n"
        if argv[:2] == ["systemctl", "--user"]:
            verb, unit = argv[2], argv[-1]
            if verb == "is-active":
                state = self.states.get(unit, "active")
                return (0 if state == "active" else 3), state + "\n"
            if verb == "is-enabled":
                return (0, "enabled\n") if unit in self.installed else (1, "not-found\n")
            if verb == "show":
                return 0, self.results.get(unit, "success") + "\n"
            if verb == "reset-failed":
                return 0, ""
            if verb == "restart":
                if self.on_restart:
                    self.on_restart(unit)
                return 0, ""
        return 1, ""

    def mutations(self):
        return [c for c in self.calls if c[-2:] == ["gateway", "restart"] or (len(c) > 2 and c[2] in ("restart", "reset-failed"))]


def _write_gateway(home, clock, *, fresh=True, pid=None, platforms=("telegram", "api_server", "webhook")):
    pid = os.getpid() if pid is None else pid
    (home / "state").mkdir(parents=True, exist_ok=True)
    updated = clock() if fresh else clock() - 3600
    (home / "state" / "gateway.heartbeat").write_text(json.dumps({"pid": pid, "updated_at": _iso(updated)}),
                                                       encoding="utf-8")
    (home / "state" / "gateway.lifecycle.json").write_text(json.dumps({"phase": "running", "pid": pid}),
                                                            encoding="utf-8")
    (home / "gateway_state.json").write_text(json.dumps({
        "pid": pid, "gateway_state": "running", "restart_requested": False,
        "platforms": {p: {"state": "connected", "writer_pid": pid} for p in platforms}}), encoding="utf-8")


def _gateway_config(tmp_path):
    return _authorize(_governed(), "gateway_restart", unit="hermes-gateway.service",
                      restart_command=["python", "-m", "hermes_cli.main", "gateway", "restart"],
                      cooldown_seconds=1800, cooldown_marker=str(tmp_path / "gateway_watchdog_last_restart"),
                      postcondition_timeout_seconds=10, required_platforms=["telegram", "api_server", "webhook"])


class TestGatewayRestartRecovery:
    def _run(self, kanban_home, tmp_path, systemd, clock, passes, config=None, before_pass=None):
        config = config or _gateway_config(tmp_path)
        ctx = _ctx(kanban_home, clock=clock, config={**config, "gateway_heartbeat_max_age_seconds": 180},
                   run_command=systemd)
        ctx.sleep = lambda seconds: None
        controller = shc.Controller(ctx, [shc.GatewayHeartbeatFresh()])
        results = []
        for _ in range(passes):
            if before_pass:
                before_pass()
            results.append(controller.run(shc.TIER_LIGHT))
            clock.advance(300)
        return results, ctx

    def test_proven_stale_gateway_is_restarted_once_and_revalidated(self, kanban_home, tmp_path):
        clock = Clock()
        _write_gateway(kanban_home, clock, fresh=False)
        alive = {"restarted": False}

        def restart(unit):
            alive["restarted"] = True
            _write_gateway(kanban_home, clock, fresh=True)

        systemd = FakeSystemd({"hermes-gateway.service": "active"}, on_restart=restart)
        refresh = lambda: alive["restarted"] and _write_gateway(kanban_home, clock, fresh=True)  # noqa: E731
        results, ctx = self._run(kanban_home, tmp_path, systemd, clock, 4, before_pass=refresh)
        assert systemd.mutations() == [["python", "-m", "hermes_cli.main", "gateway", "restart"]]
        assert results[0].recovered == [] and len(results[1].recovered) == 1         # needs 2 cycles first
        assert results[-1].status == shc.AGGREGATE_GREEN
        assert int((tmp_path / "gateway_watchdog_last_restart").read_text()) > 0     # shared cooldown marker

    def test_watchdog_cooldown_defers_then_bounded_deferral_escalates(self, kanban_home, tmp_path):
        clock = Clock()
        _write_gateway(kanban_home, clock, fresh=False)
        (tmp_path / "gateway_watchdog_last_restart").write_text(f"{int(clock())}\n")
        systemd = FakeSystemd({"hermes-gateway.service": "active"})
        config = _gateway_config(tmp_path)
        results, _ = self._run(kanban_home, tmp_path, systemd, clock, 3, config=config)
        assert systemd.mutations() == [] and results[-1].status == shc.AGGREGATE_DEGRADED
        klass = shc.GatewayRestartRecovery()
        assert klass.max_deferral_seconds > int(config["recovery_classes"]["gateway_restart"]["cooldown_seconds"])

    @pytest.mark.parametrize("setup,reason", [("draining", shc.GATE_DEFERRED), ("uninstalled", shc.GATE_REFUSED),
                                              ("fresh", shc.GATE_CLEARED)])
    def test_gate_decisions(self, kanban_home, tmp_path, setup, reason):
        clock = Clock()
        _write_gateway(kanban_home, clock, fresh=setup == "fresh")
        if setup == "draining":
            (kanban_home / "state" / "gateway.lifecycle.json").write_text(json.dumps({"phase": "draining"}))
        systemd = FakeSystemd({"hermes-gateway.service": "active"},
                              installed=() if setup == "uninstalled" else ("hermes-gateway.service",))
        config = _gateway_config(tmp_path)
        spec = config["recovery_classes"]["gateway_restart"]
        ctx = _ctx(kanban_home, clock=clock, config=config, run_command=systemd)
        gate = shc.GatewayRestartRecovery().gate(ctx, shc.Finding("gateway_heartbeat_fresh", "gateway",
                                                                  "heartbeat_stale", {}), {}, spec)
        assert gate.decision == reason and systemd.mutations() == []

    def test_restart_that_does_not_restore_is_retried_once_after_cooldown_then_escalates(self, kanban_home, tmp_path):
        clock = Clock()
        _write_gateway(kanban_home, clock, fresh=False)
        systemd = FakeSystemd({"hermes-gateway.service": "active"})          # restart never brings it back
        alerts = Alerts()
        config = _gateway_config(tmp_path)
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts, config=config, run_command=systemd)
        ctx.sleep = lambda seconds: None
        controller = shc.Controller(ctx, [shc.GatewayHeartbeatFresh()])
        statuses = []
        for _ in range(12):
            statuses.append(controller.run(shc.TIER_LIGHT).status)
            clock.advance(300)
        assert len(systemd.mutations()) == 2 and statuses[-1] == shc.AGGREGATE_ESCALATED
        assert len(alerts.sent) == 1

    def test_heartbeat_back_but_messaging_disconnected_is_not_recovered(self, kanban_home, tmp_path):
        clock = Clock()
        _write_gateway(kanban_home, clock, fresh=False)

        def restart(unit):
            _write_gateway(kanban_home, clock, fresh=True, platforms=("api_server", "webhook"))

        systemd = FakeSystemd({"hermes-gateway.service": "active"}, on_restart=restart)
        results, _ = self._run(kanban_home, tmp_path, systemd, clock, 2)
        assert len(systemd.mutations()) == 1
        assert results[1].recovered == [] and results[1].status == shc.AGGREGATE_RECOVERY

    @pytest.mark.parametrize("change,problem", [
        ({"unit": "NorCal_Hermes.service"}, "unit_must_be_hermes_gateway"),
        ({"restart_command": ["systemctl", "--user", "restart", "hermes-gateway.service"]},
         "restart_command_must_be_governed_gateway_restart"),
        ({"restart_command": ["python", "-m", "hermes_cli.main", "--system", "gateway", "restart"]},
         "restart_command_flag_not_allowed"),
        ({"cooldown_seconds": 600}, "cooldown_below_watchdog"),
    ])
    def test_authorization_boundary(self, tmp_path, change, problem):
        spec = {**_gateway_config(tmp_path)["recovery_classes"]["gateway_restart"], **change}
        assert shc.GatewayRestartRecovery().validate_spec(spec, {}) == problem


# -- class 2: shared service restart ---------------------------------------------


def _service_config(url=None, **overrides):
    base = {"shared_user_units": ["life-wiki-api.service", "norcal-opsbridge.service", "erika-phone.service"],
            "shared_system_units": ["caddy.service"],
            "company_health_probes": {"entities": [{"entity_id": "ENT-004", "service": "orion-api"}]},
            "company_isolation": {"companies": {"ENT-004": {"tokens": ["orion"]}}}}
    services = overrides.pop("services", {"life-wiki-api.service": {"health_url": url}})
    return _authorize({**_governed(), **base}, "shared_service_restart", services=services,
                      cooldown_seconds=600, postcondition_timeout_seconds=5, **overrides)


class TestSharedServiceRestartRecovery:
    def _controller(self, kanban_home, config, systemd, clock, alerts=None):
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts or Alerts(), config=config, run_command=systemd)
        ctx.sleep = lambda seconds: None
        return shc.Controller(ctx, [shc.SharedUnitsActive()])

    def test_failed_allowlisted_unit_is_restarted_and_its_endpoint_revalidated(self, kanban_home):
        clock = Clock()
        with _health_server(200) as (url, hits):
            systemd = FakeSystemd({"life-wiki-api.service": "failed", "norcal-opsbridge.service": "active",
                                   "erika-phone.service": "active"})
            systemd.on_restart = lambda unit: systemd.states.__setitem__(unit, "active")
            controller = self._controller(kanban_home, _service_config(url), systemd, clock)
            first = controller.run(shc.TIER_LIGHT)
            clock.advance(300)
            second = controller.run(shc.TIER_LIGHT)
            assert first.recovered == [] and len(second.recovered) == 1 and hits
        assert systemd.mutations() == [["systemctl", "--user", "reset-failed", "life-wiki-api.service"],
                                       ["systemctl", "--user", "restart", "life-wiki-api.service"]]
        assert second.status == shc.AGGREGATE_GREEN

    def test_clean_stop_is_refused_as_possibly_deliberate(self, kanban_home):
        clock = Clock()
        systemd = FakeSystemd({"life-wiki-api.service": "inactive", "norcal-opsbridge.service": "active",
                               "erika-phone.service": "active"}, results={"life-wiki-api.service": "success"})
        controller = self._controller(kanban_home, _service_config(), systemd, clock)
        controller.run(shc.TIER_LIGHT)
        clock.advance(300)
        result = controller.run(shc.TIER_LIGHT)
        assert systemd.mutations() == [] and result.status == shc.AGGREGATE_ESCALATED

    def test_crashed_inactive_unit_is_restarted(self, kanban_home):
        clock = Clock()
        systemd = FakeSystemd({"life-wiki-api.service": "inactive", "norcal-opsbridge.service": "active",
                               "erika-phone.service": "active"}, results={"life-wiki-api.service": "exit-code"})
        systemd.on_restart = lambda unit: systemd.states.__setitem__(unit, "active")
        controller = self._controller(kanban_home, _service_config(), systemd, clock)
        controller.run(shc.TIER_LIGHT)
        clock.advance(300)
        assert len(controller.run(shc.TIER_LIGHT).recovered) == 1

    def test_non_allowlisted_shared_unit_stays_escalate_only(self, kanban_home):
        clock = Clock()
        systemd = FakeSystemd({"life-wiki-api.service": "active", "norcal-opsbridge.service": "failed",
                               "erika-phone.service": "active"})
        controller = self._controller(kanban_home, _service_config(), systemd, clock)
        controller.run(shc.TIER_LIGHT)
        clock.advance(300)
        result = controller.run(shc.TIER_LIGHT)
        assert systemd.mutations() == [] and len(result.escalated) == 1

    def test_unit_up_but_endpoint_down_retries_once_after_cooldown_then_escalates(self, kanban_home):
        clock = Clock()
        alerts = Alerts()
        with _health_server(503) as (url, _):
            systemd = FakeSystemd({"life-wiki-api.service": "failed", "norcal-opsbridge.service": "active",
                                   "erika-phone.service": "active"})
            systemd.on_restart = lambda unit: systemd.states.__setitem__(unit, "active")
            controller = self._controller(kanban_home, _service_config(url), systemd, clock, alerts)
            statuses = []
            for _ in range(8):
                statuses.append(controller.run(shc.TIER_LIGHT).status)
                clock.advance(300)
        restarts = [c for c in systemd.mutations() if c[2] == "restart"]
        assert len(restarts) == 2 and statuses[-1] == shc.AGGREGATE_ESCALATED and len(alerts.sent) == 1
        # the endpoint recovers on its own: the escalation then clears on the next pass
        with _health_server(200) as (url2, _):
            spec = _service_config(url2)
            controller = self._controller(kanban_home, spec, systemd, clock, alerts)
            clock.advance(300)
            assert controller.run(shc.TIER_LIGHT).status == shc.AGGREGATE_GREEN
        assert len([c for c in systemd.mutations() if c[2] == "restart"]) == 2

    @pytest.mark.parametrize("services,problem", [
        ({"orion-api.service": {}}, "company_service_not_allowed:orion-api.service"),
        ({"caddy.service": {}}, "not_a_shared_user_unit:caddy.service"),
        ({"hermes-gateway.service": {}}, "gateway_has_its_own_class"),
        ({"life-wiki-api.service": {"health_url": "http://10.1.1.1/h"}}, "health_url_not_loopback:life-wiki-api.service"),
    ])
    def test_authorization_boundary(self, services, problem):
        config = _service_config(services=services)
        spec = config["recovery_classes"]["shared_service_restart"]
        assert shc.SharedServiceRestartRecovery().validate_spec(spec, config) == problem

    def test_transitioning_unit_is_deferred(self, kanban_home):
        systemd = FakeSystemd({"life-wiki-api.service": "activating"})
        config = _service_config()
        gate = shc.SharedServiceRestartRecovery().gate(
            _ctx(kanban_home, config=config, run_command=systemd),
            shc.Finding("shared_units_active", "life-wiki-api.service", "not_active:activating", {}), {},
            config["recovery_classes"]["shared_service_restart"])
        assert gate.decision == shc.GATE_DEFERRED and systemd.mutations() == []


# -- class 3: lease / dead execution reconciliation ---------------------------------


def _dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _lease_config():
    return _authorize({**_governed(), "lease_grace_seconds": 300, "execution_heartbeat_stale_seconds": 900},
                      "lease_reconciliation", reconcile=["claims", "detached_runs", "executions"])


class TestLeaseReconciliationRecovery:
    def _expired(self, conn, *, pid, host=None, title="stuck"):
        tid = kb.create_task(conn, title=title, assignee="worker")
        kb.claim_task(conn, tid)
        lock = f"{host or kb._claimer_id().split(':', 1)[0]}:{pid}"
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET claim_expires = ?, claim_lock = ?, worker_pid = ? WHERE id = ?",
                         (int(time.time()) - 5000, lock, pid, tid))
        return tid

    def _run(self, kanban_home, passes=2):
        clock = lambda: time.time()  # noqa: E731
        controller = shc.Controller(_ctx(kanban_home, clock=clock, config=_lease_config()), [shc.RunLeaseConsistency()])
        return [controller.run(shc.TIER_LIGHT) for _ in range(passes)]

    def test_expired_claim_of_a_dead_worker_is_reclaimed_and_revalidated(self, kanban_home):
        with kb.connect_closing() as conn:
            stuck = self._expired(conn, pid=_dead_pid())
            healthy = kb.create_task(conn, title="healthy long run", assignee="worker")
            kb.claim_task(conn, healthy)
            before = _board_snapshot(conn, [healthy])
        results = self._run(kanban_home)
        assert results[0].recovered == [] and len(results[1].recovered) == 1
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, stuck)
            assert task.status == "ready" and task.claim_lock is None
            assert "reclaimed" in _kinds(conn, stuck)
            assert _board_snapshot(conn, [healthy])["rows"] == before["rows"]    # old but live: untouched

    @pytest.mark.parametrize("variant,reason", [("alive", "worker_process_alive"),
                                                ("other_host", "claim_held_by_other_host")])
    def test_live_worker_or_foreign_claim_is_refused_without_mutation(self, kanban_home, variant, reason):
        with kb.connect_closing() as conn:
            tid = self._expired(conn, pid=os.getpid() if variant == "alive" else _dead_pid(),
                                host="some-other-host" if variant == "other_host" else None)
            before = _board_snapshot(conn, [tid])
        results = self._run(kanban_home)
        assert results[-1].status == shc.AGGREGATE_ESCALATED
        with kb.connect_closing() as conn:
            assert _board_snapshot(conn, [tid])["rows"] == before["rows"]
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "run_lease_consistency"][0]
        assert rec["escalation_reason"] == f"recovery_refused:{reason}"

    def test_live_execution_with_fresh_heartbeat_is_never_touched(self, kanban_home):
        from hermes_cli import exec_supervisor
        with kb.connect_closing() as conn:
            tid = self._expired(conn, pid=_dead_pid())
            record = exec_supervisor.create_execution(conn, executor_type="claude", command_class="claude.headless",
                                                      cwd="/tmp", task_id=tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE executions SET status = 'running', heartbeat_at = ? WHERE id = ?",
                             (int(time.time()), record.id))
            before = _board_snapshot(conn, [tid])
        self._run(kanban_home)
        with kb.connect_closing() as conn:
            assert _board_snapshot(conn, [tid])["rows"] == before["rows"]
            assert conn.execute("SELECT ended_at FROM executions WHERE id = ?", (record.id,)).fetchone()[0] is None

    def test_stale_execution_whose_process_is_gone_is_settled(self, kanban_home):
        from hermes_cli import exec_supervisor
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="exec", assignee="worker")
            record = exec_supervisor.create_execution(conn, executor_type="claude", command_class="claude.headless",
                                                      cwd="/tmp", task_id=tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE executions SET status = 'running', pid = ?, heartbeat_at = ? WHERE id = ?",
                             (_dead_pid(), int(time.time()) - 5000, record.id))
        results = self._run(kanban_home)
        assert len(results[1].recovered) == 1
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT ended_at FROM executions WHERE id = ?", (record.id,)).fetchone()[0] is not None

    def test_stale_heartbeat_but_live_process_is_refused(self, kanban_home):
        from hermes_cli import exec_supervisor
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="exec", assignee="worker")
            record = exec_supervisor.create_execution(conn, executor_type="claude", command_class="claude.headless",
                                                      cwd="/tmp", task_id=tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE executions SET status = 'running', pid = ?, proc_key = NULL, heartbeat_at = ? "
                             "WHERE id = ?", (os.getpid(), int(time.time()) - 5000, record.id))
        results = self._run(kanban_home)
        assert results[-1].status == shc.AGGREGATE_ESCALATED
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT ended_at FROM executions WHERE id = ?", (record.id,)).fetchone()[0] is None

    def test_detached_current_run_is_closed_but_a_non_current_one_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            current = kb.create_task(conn, title="moved on", assignee="worker")
            claimed = kb.claim_task(conn, current)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'blocked', worker_pid = NULL WHERE id = ?", (current,))
                conn.execute("UPDATE task_runs SET worker_pid = NULL WHERE id = ?", (claimed.current_run_id,))
        self._run(kanban_home)
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT ended_at FROM task_runs WHERE id = ?",
                                (claimed.current_run_id,)).fetchone()[0] is not None
            other = kb.create_task(conn, title="orphan run", assignee="worker")
            orphan = kb.claim_task(conn, other)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'blocked', current_run_id = NULL, worker_pid = NULL WHERE id = ?",
                             (other,))
        results = self._run(kanban_home)
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT ended_at FROM task_runs WHERE id = ?",
                                (orphan.current_run_id,)).fetchone()[0] is None
        assert results[-1].status == shc.AGGREGATE_ESCALATED


# -- class 4: Life Wiki retry ---------------------------------------------------------


def _wiki_setup(home, vault, *, job_state="scheduled", enabled=True, last_status="error", last_run=None):
    (vault / "Logs" / "daily").mkdir(parents=True, exist_ok=True)
    _write_jobs(home, [
        {"name": "nightly-executive-continuity-reconciliation", "enabled": enabled, "state": job_state,
         "last_status": last_status, "last_run_at": last_run, "failure_streak": 1},
        {"name": "life-wiki-daily-validation", "enabled": True, "state": "scheduled", "last_status": "error",
         "last_run_at": last_run, "failure_streak": 1},
    ])


def _wiki_config(vault, settle=0):
    config = {**_governed(), "life_wiki_daily_note": {"vault": str(vault), "timezone": "America/Chicago",
                                                      "cutoff_hour": 6},
              "critical_cron_jobs": {"life-wiki-daily-validation": {}}}
    return _authorize(config, "life_wiki_retry", daily_note_job="nightly-executive-continuity-reconciliation",
                      validation_jobs=["life-wiki-daily-validation"], settle_seconds=settle,
                      validator_command=["validate", "--date", "{date}"])


class FakeCron:
    def __init__(self, home, vault, clock, *, writes_note=True, validator_rc=0):
        self.home, self.vault, self.clock = home, vault, clock
        self.writes_note = writes_note
        self.validator_rc = validator_rc
        self.triggered: list[str] = []

    def trigger(self, name):
        self.triggered.append(name)
        doc = json.loads((self.home / "cron" / "jobs.json").read_text())
        for job in doc["jobs"]:
            if job["name"] == name:
                job.update({"last_status": "ok", "failure_streak": 0, "last_run_at": _iso(self.clock() + 1)})
        (self.home / "cron" / "jobs.json").write_text(json.dumps(doc))
        if name.startswith("nightly") and self.writes_note:
            (self.vault / "Logs" / "daily" / "2026-09-14.md").write_text("# day\n")
        return True, "scheduled"

    def run_command(self, argv, timeout):
        return (self.validator_rc, "") if argv[0] == "validate" else (1, "")


class TestLifeWikiRetryRecovery:
    def _controller(self, kanban_home, tmp_path, fake, config, clock, alerts=None):
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts or Alerts(), config=config, run_command=fake.run_command)
        ctx.cron_trigger = fake.trigger
        return shc.Controller(ctx, [shc.LifeWikiDailyNote(), shc.CriticalCronJobsHealthy()])

    def test_missing_note_triggers_the_existing_job_and_validator_passes(self, kanban_home, tmp_path):
        vault = tmp_path / "vault"
        _wiki_setup(kanban_home, vault)
        clock = Clock()
        clock.t = _chicago_ts(7)
        fake = FakeCron(kanban_home, vault, clock)
        deep = self._controller(kanban_home, tmp_path, fake, _wiki_config(vault), clock).run(shc.TIER_DEEP)
        assert fake.triggered == ["nightly-executive-continuity-reconciliation"] and len(deep.recovered) == 1
        assert (vault / "Logs" / "daily" / "2026-09-14.md").is_file()

    def test_failed_validation_job_is_retried_through_the_scheduler(self, kanban_home, tmp_path):
        vault = tmp_path / "vault"
        _wiki_setup(kanban_home, vault)
        clock = Clock()
        clock.t = _chicago_ts(5)                               # before the note cutoff: only the job finding
        fake = FakeCron(kanban_home, vault, clock)
        light = self._controller(kanban_home, tmp_path, fake, _wiki_config(vault), clock).run(shc.TIER_LIGHT)
        assert fake.triggered == ["life-wiki-daily-validation"] and len(light.recovered) == 1

    @pytest.mark.parametrize("variant,reason", [("paused", "job_paused_owner_decision"),
                                                ("succeeded", "job_succeeded_without_note")])
    def test_owner_paused_or_already_succeeded_job_is_refused(self, kanban_home, tmp_path, variant, reason):
        vault = tmp_path / "vault"
        clock = Clock()
        clock.t = _chicago_ts(7)
        if variant == "paused":
            _wiki_setup(kanban_home, vault, job_state="paused", enabled=False)
        else:
            _wiki_setup(kanban_home, vault, last_status="ok", last_run=_iso(_chicago_ts(4)))
        fake = FakeCron(kanban_home, vault, clock)
        config = _wiki_config(vault)
        config["critical_cron_jobs"] = {}
        result = self._controller(kanban_home, tmp_path, fake, config, clock).run(shc.TIER_DEEP)
        assert fake.triggered == [] and result.status == shc.AGGREGATE_ESCALATED
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "life_wiki_daily_note"][0]
        assert rec["escalation_reason"] == f"recovery_refused:{reason}"

    def test_validator_still_failing_is_bounded_and_escalates(self, kanban_home, tmp_path):
        vault = tmp_path / "vault"
        _wiki_setup(kanban_home, vault)
        clock = Clock()
        clock.t = _chicago_ts(7)
        fake = FakeCron(kanban_home, vault, clock, validator_rc=2)
        config = _wiki_config(vault)
        config["critical_cron_jobs"] = {}
        alerts = Alerts()
        controller = self._controller(kanban_home, tmp_path, fake, config, clock, alerts)
        statuses = []
        for _ in range(4):
            statuses.append(controller.run(shc.TIER_DEEP).status)
            clock.advance(3600)
            (vault / "Logs" / "daily" / "2026-09-14.md").unlink(missing_ok=True)
        assert len(fake.triggered) == 2 and shc.AGGREGATE_ESCALATED in statuses and len(alerts.sent) == 1

    def test_settle_window_waits_for_the_scheduled_run(self, kanban_home, tmp_path):
        vault = tmp_path / "vault"
        _wiki_setup(kanban_home, vault)
        clock = Clock()
        clock.t = _chicago_ts(7)
        fake = FakeCron(kanban_home, vault, clock, writes_note=False)
        config = _wiki_config(vault, settle=3600)
        config["critical_cron_jobs"] = {}
        controller = self._controller(kanban_home, tmp_path, fake, config, clock)
        assert controller.run(shc.TIER_DEEP).status == shc.AGGREGATE_RECOVERY
        clock.advance(1200)
        (vault / "Logs" / "daily" / "2026-09-14.md").write_text("# day\n")
        assert len(controller.run(shc.TIER_DEEP).recovered) == 1 and len(fake.triggered) == 1

    def test_github_sync_job_can_never_be_authorized(self, tmp_path):
        config = _wiki_config(tmp_path)
        spec = {**config["recovery_classes"]["life_wiki_retry"], "validation_jobs": ["life-wiki-github-sync"]}
        assert shc.LifeWikiRetryRecovery().validate_spec(spec, config) == "forbidden_job"


# -- class 5: safe vault / git sync ----------------------------------------------------


def _git_repo_pair(tmp_path, name="vault"):
    bare = tmp_path / f"{name}-remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    work = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-q", "-B", "main")
    (work / "a.md").write_text("a", encoding="utf-8")
    _git(work, "add", "a.md")
    _git(work, "commit", "-q", "-m", "a")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(work, "fetch", "-q", "origin")
    return bare, work


def _remote_head(bare):
    return subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "refs/heads/main"], check=True,
                          capture_output=True, text=True).stdout.strip()


def _vault_config(tmp_path, work, name="vault"):
    state = tmp_path / "drift.json"
    state.write_text(json.dumps({"verdict": "CLEAN", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    config = {**_governed(), "repository_drift": {"deploy_drift_state": str(state), "watched_repositories": [
        {"name": name, "path": str(work), "upstream": "origin/main"}]}}
    return _authorize(config, "vault_git_sync", repositories={name: {"path": str(work), "remote": "origin",
                                                                      "branch": "main"}})


class TestVaultSyncRecovery:
    FORBIDDEN_GIT = ("--force", "-f", "--force-with-lease", "reset", "commit", "add", "rebase", "clean", "stash")

    def _run(self, kanban_home, config):
        self.git_calls = []

        def recording(argv, timeout):
            self.git_calls.append(list(argv))
            return shc.run_command(argv, timeout)

        ctx = _ctx(kanban_home, clock=time.time, config=config, run_command=recording)
        result = shc.Controller(ctx, [shc.RepositoryDrift()]).run(shc.TIER_DEEP)
        for argv in self.git_calls:
            assert not any(part in self.FORBIDDEN_GIT or part.startswith("+") for part in argv[4:]), argv
        return result

    def test_clean_unpushed_commit_is_pushed_and_verified_on_the_remote(self, kanban_home, tmp_path):
        bare, work = _git_repo_pair(tmp_path)
        (work / "b.md").write_text("b", encoding="utf-8")
        _git(work, "add", "b.md")
        _git(work, "commit", "-q", "-m", "b")
        head = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        other_bare, other = _git_repo_pair(tmp_path, name="unrelated")
        (other / "c.md").write_text("c", encoding="utf-8")
        _git(other, "add", "c.md")
        _git(other, "commit", "-q", "-m", "c")
        other_remote = _remote_head(other_bare)
        result = self._run(kanban_home, _vault_config(tmp_path, work))
        assert len(result.recovered) == 1 and _remote_head(bare) == head
        assert _remote_head(other_bare) == other_remote                    # unrelated repo untouched
        assert ["push", "origin", "HEAD:refs/heads/main"] in [c[4:] for c in self.git_calls]

    def test_a_push_that_reports_success_without_reaching_the_remote_is_not_recovered(self, kanban_home, tmp_path):
        bare, work = _git_repo_pair(tmp_path)
        (work / "b.md").write_text("b", encoding="utf-8")
        _git(work, "add", "b.md")
        _git(work, "commit", "-q", "-m", "b")
        remote_before = _remote_head(bare)

        def silent_push(argv, timeout):
            if argv[4:5] == ["push"]:
                # claims success and moves the local tracking ref, but pushes nothing
                _git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
                return 0, ""
            return shc.run_command(argv, timeout)

        ctx = _ctx(kanban_home, clock=time.time, config=_vault_config(tmp_path, work), run_command=silent_push)
        result = shc.Controller(ctx, [shc.RepositoryDrift()]).run(shc.TIER_DEEP)
        assert result.recovered == [] and _remote_head(bare) == remote_before
        rec = [r for r in shc.StateStore(ctx.state_dir).load()["fingerprints"].values()
               if r["invariant"] == "repository_drift"][0]
        assert rec["last_postcondition"] == "remote_does_not_contain_expected_commit"

    def test_behind_is_fast_forwarded(self, kanban_home, tmp_path):
        bare, work = _git_repo_pair(tmp_path)
        peer = tmp_path / "peer"
        subprocess.run(["git", "clone", "-q", str(bare), str(peer)], check=True, capture_output=True)
        (peer / "p.md").write_text("p", encoding="utf-8")
        _git(peer, "add", "p.md")
        _git(peer, "commit", "-q", "-m", "p")
        _git(peer, "push", "-q", "origin", "HEAD:refs/heads/main")
        _git(work, "fetch", "-q", "origin")
        result = self._run(kanban_home, _vault_config(tmp_path, work))
        assert len(result.recovered) == 1 and (work / "p.md").is_file()

    @pytest.mark.parametrize("variant,reason", [("dirty", "worktree_dirty_unknown_changes"), ("diverged", "diverged")])
    def test_dirty_or_diverged_is_escalated_without_commit_or_push(self, kanban_home, tmp_path, variant, reason):
        bare, work = _git_repo_pair(tmp_path)
        (work / "b.md").write_text("b", encoding="utf-8")
        _git(work, "add", "b.md")
        _git(work, "commit", "-q", "-m", "b")
        if variant == "dirty":
            (work / "unknown.md").write_text("not attributed", encoding="utf-8")
        else:
            peer = tmp_path / "peer"
            subprocess.run(["git", "clone", "-q", str(bare), str(peer)], check=True, capture_output=True)
            (peer / "p.md").write_text("p", encoding="utf-8")
            _git(peer, "add", "p.md")
            _git(peer, "commit", "-q", "-m", "p")
            _git(peer, "push", "-q", "origin", "HEAD:refs/heads/main")
            _git(work, "fetch", "-q", "origin")
        remote_before = _remote_head(bare)
        log_before = subprocess.run(["git", "-C", str(work), "log", "--oneline"], capture_output=True, text=True).stdout
        result = self._run(kanban_home, _vault_config(tmp_path, work))
        assert _remote_head(bare) == remote_before and result.status == shc.AGGREGATE_ESCALATED
        assert subprocess.run(["git", "-C", str(work), "log", "--oneline"], capture_output=True, text=True).stdout == log_before
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "repository_drift" and str(r.get("escalation_reason") or "").startswith("recovery")][0]
        assert rec["escalation_reason"] == f"recovery_refused:{reason}"

    @pytest.mark.parametrize("name,path_name,problem", [
        ("hermes-agent-next", "hermes-agent-next", "repository_never_auto_synced:hermes-agent-next"),
        ("docs", "doctrine", "repository_never_auto_synced:docs"),
        ("unwatched", "unwatched", "not_watched_with_same_upstream:unwatched"),
    ])
    def test_authorization_boundary(self, tmp_path, name, path_name, problem):
        config = _vault_config(tmp_path, tmp_path / "vault")
        spec = {**config["recovery_classes"]["vault_git_sync"],
                "repositories": {name: {"path": str(tmp_path / path_name), "remote": "origin", "branch": "main"}}}
        assert shc.VaultSyncRecovery().validate_spec(spec, config) == problem



# -- class 6: evidence attachment -------------------------------------------------------


def _evidence_config(**overrides):
    config = {**_governed(), "company_isolation": {"companies": {"ENT-004": {"tokens": ["orion"],
                                                                               "lead_profiles": ["orion_lead"]}}}}
    return _authorize(config, "evidence_attachment", patterns=["*EVIDENCE*.md", "*evidence*.json"], max_files=5,
                      max_bytes=4096, **overrides)


def _owned_workspace(conn, tid):
    root = kb.workspaces_root() / tid
    root.mkdir(parents=True, exist_ok=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_kind = 'scratch', workspace_path = ? WHERE id = ?", (str(root), tid))
    return root


class TestEvidenceAttachmentRecovery:
    def test_task_owned_evidence_is_attached_then_the_route_opens(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn)
            root = _owned_workspace(conn, tid)
            (root / "PHASE-EVIDENCE.md").write_text("# evidence\nchecked", encoding="utf-8")
            (root / "agent.log").write_text("log", encoding="utf-8")
        ctx = _ctx(kanban_home, config=_evidence_config())
        controller = shc.Controller(ctx, [shc.VerifierRouteOpen()])
        first = controller.run(shc.TIER_LIGHT)
        assert len(first.recovered) == 1
        with kb.connect_closing() as conn:
            names = [a.filename for a in kb.list_attachments(conn, tid)]
            assert names == ["PHASE-EVIDENCE.md"] and kb.subject_has_evidence(conn, tid)
        second = controller.run(shc.TIER_LIGHT)                    # v1 routing now opens the verifier
        assert len(second.recovered) == 1
        with kb.connect_closing() as conn:
            assert kb._open_verifier_child(conn, tid) is not None
            assert [a.filename for a in kb.list_attachments(conn, tid)] == ["PHASE-EVIDENCE.md"]

    def test_only_denied_or_company_files_is_refused_and_nothing_attached(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn)
            root = _owned_workspace(conn, tid)
            for name in ("agent.log", "kanban.db-wal", ".env", "key.pem", "orion-EVIDENCE.md", "notes.txt"):
                (root / name).write_text("x", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "EVIDENCE.md").write_text("x", encoding="utf-8")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "EVIDENCE.md").write_text("x", encoding="utf-8")
        result = shc.Controller(_ctx(kanban_home, config=_evidence_config()), [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_ESCALATED
        with kb.connect_closing() as conn:
            assert kb.list_attachments(conn, tid) == []

    @pytest.mark.parametrize("variant,reason", [("shared_home", "no_task_owned_workspace"),
                                                ("company_lead", "company_lane_task")])
    def test_shared_workspace_or_company_card_is_refused(self, kanban_home, variant, reason):
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn)
            if variant == "shared_home":
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET workspace_kind = 'dir', workspace_path = ? WHERE id = ?",
                                 (str(kanban_home), tid))
                (kanban_home / "EVIDENCE.md").write_text("x", encoding="utf-8")
            else:
                (_owned_workspace(conn, tid) / "EVIDENCE.md").write_text("x", encoding="utf-8")
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET assignee = 'orion_lead' WHERE id = ?", (tid,))
        shc.Controller(_ctx(kanban_home, config=_evidence_config()), [shc.VerifierRouteOpen()]).run(shc.TIER_LIGHT)
        with kb.connect_closing() as conn:
            assert kb.list_attachments(conn, tid) == []
        rec = [r for r in shc.StateStore(_ctx(kanban_home).state_dir).load()["fingerprints"].values()
               if r["invariant"] == "verifier_route_open"][0]
        assert rec["escalation_reason"] == f"recovery_refused:{reason}"

    def test_recover_is_idempotent_for_already_attached_content(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_without_evidence(conn)
            root = _owned_workspace(conn, tid)
            (root / "EVIDENCE.md").write_text("same", encoding="utf-8")
        klass = shc.EvidenceAttachmentRecovery()
        config = _evidence_config()
        spec = config["recovery_classes"]["evidence_attachment"]
        ctx = _ctx(kanban_home, config=config)
        finding = shc.Finding("verifier_route_open", tid, "no_route:evidence_missing", {})
        assert klass.recover(ctx, finding, spec).detail == {"attached": 1}
        assert klass.recover(ctx, finding, spec).detail == {"attached": 0}
        with kb.connect_closing() as conn:
            assert len(kb.list_attachments(conn, tid)) == 1

    @pytest.mark.parametrize("pattern", ["*.log", "*", "../*.md", "sub/EVIDENCE.md", "*.db-wal"])
    def test_pattern_boundary(self, pattern):
        config = _evidence_config()
        spec = {**config["recovery_classes"]["evidence_attachment"], "patterns": [pattern]}
        assert shc.EvidenceAttachmentRecovery().validate_spec(spec, config).startswith("pattern_not_allowed")
