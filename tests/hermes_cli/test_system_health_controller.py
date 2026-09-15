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
