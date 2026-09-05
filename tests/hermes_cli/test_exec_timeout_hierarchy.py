"""The timeout hierarchy, and what a non-success is allowed to mean.

Reproduction these tests exist for (t_aef6bbe1, 2026-09-03): execution
``x_3b16666deaab836e`` was authorized for 3600 s and killed at 1804 s by the
supervisor's own stale-heartbeat rule — a level-3 liveness timer overruling a
level-1 authorization at half its budget. It could do so because
``exec_supervisor.heartbeat()`` has never had a call site, so ``heartbeat_at``
is frozen at ``started_at`` for every execution and "no progress in 1800 s"
degenerates into "older than 1800 s".

Two separate defects, so two separate groups of assertions:

1. **Precedence.** The authorized runtime cap outranks every transport, lease
   and liveness timer. A silent-but-authorized executor runs to its own cap;
   only then does it end, and it ends classified as a runtime timeout.
2. **Classification.** The supervisor's own SIGTERM came back to the
   controller's ``proc.communicate()`` as exit 143 and was recorded
   ``failed/nonzero_exit`` — indistinguishable from the executor failing on its
   own. An infrastructure termination must be recorded as one, and must not
   spend the ordinary implementation retry budget.

Nothing here asserts that a signal was sent; liveness is always re-read from
the process, per the house rule in ``test_exec_supervisor.py``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import exec_supervisor as ex
from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane as rl


# ---------------------------------------------------------------------------
# Fixtures (same shape as test_exec_supervisor.py, deliberately independent)
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def workroot(kanban_home: Path) -> Path:
    root = kanban_home / "work"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def live_policy(workroot: Path) -> ex.ExecutionPolicy:
    """The live shape: a 1800 s liveness window under a 3600 s ceiling.

    These are the production defaults (``DEFAULT_STALE_HEARTBEAT_SECONDS`` /
    ``DEFAULT_MAX_RUNTIME_SECONDS``) rather than test-friendly small numbers,
    because the inversion is a relationship between exactly these two values.
    """
    return ex.ExecutionPolicy(
        allowed_executors=("claude", "codex", "shell"),
        allowed_roots=(str(workroot),),
        max_runtime_seconds=ex.DEFAULT_MAX_RUNTIME_SECONDS,
        sync_ceiling_seconds=900,
        stale_heartbeat_seconds=ex.DEFAULT_STALE_HEARTBEAT_SECONDS,
        terminate_grace_seconds=2,
    )


def _sleeper(workroot: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=str(workroot), start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _reap(pid: int, timeout: float = 5.0) -> None:
    if not pid:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ex.process_identity(pid) is None:
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                return
        time.sleep(0.05)


def _wait_gone(pid, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ex.process_identity(pid) is None:
            return True
        time.sleep(0.05)
    return ex.process_identity(pid) is None


def _attached(conn, workroot: Path, proc, *, cap, task_id=None):
    """A supervisor-owned, live execution with a known runtime cap."""
    record = ex.create_execution(
        conn, task_id=task_id, executor_type="shell",
        command_class="shell.argv", cwd=str(workroot),
        controller_pid=os.getpid(),
        controller_key=ex.process_identity(os.getpid()),
        ownership=ex.OWNERSHIP_SUPERVISOR,
        max_runtime_s=cap,
    )
    ex._attach_process(
        conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
        proc_key=ex.process_identity(proc.pid),
    )
    return record


def _age(conn, execution_id: str, seconds: int) -> None:
    """Move the row back in time.

    Both bounds are arithmetic on stored timestamps, so ageing the row is
    exactly equivalent to waiting and is deterministic. ``started_at`` and
    ``heartbeat_at`` move together because that is the live situation: nothing
    ever advances the heartbeat, so the two stay equal for the whole lifetime
    of every real execution.
    """
    conn.execute(
        "UPDATE executions SET started_at = started_at - ?, "
        "heartbeat_at = heartbeat_at - ? WHERE id = ?",
        (seconds, seconds, execution_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Precedence: authorized runtime outranks the liveness timer
# ---------------------------------------------------------------------------


class TestTimeoutHierarchy:
    def test_a_3600s_authorization_survives_past_1800s(
        self, kanban_home, workroot, live_policy
    ):
        """The t_aef6bbe1 reproduction, and the assertion that repairs it.

        1804 s into an authorized 3600 s, with a heartbeat that has never been
        advanced. Before the repair this was a SIGTERM at 1804 s; the executor
        must now still be running, still owned, and still untouched.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age(conn, record.id, 1804)
                result = ex.reconcile(conn, policy=live_policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.untouched
            assert record.id not in result.stale
            assert refreshed.status in ex.ACTIVE_STATUSES
            assert refreshed.termination_reason is None
            assert ex.process_identity(proc.pid) is not None, (
                "a level-3 liveness timer ended a level-1 authorized runtime"
            )
        finally:
            _reap(proc.pid)

    def test_the_bound_is_the_cap_not_the_liveness_window(
        self, kanban_home, workroot, live_policy
    ):
        """Stated directly, so the precedence is checkable without a process."""
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
            )
        assert ex._stale_heartbeat_bound(record, live_policy) == 3600
        assert live_policy.stale_heartbeat_seconds == 1800

    def test_the_runtime_cap_still_ends_it_and_says_so(
        self, kanban_home, workroot, live_policy
    ):
        """Subordinating the liveness timer must not remove the upper bound.

        Past its own cap the execution ends — classified ``timed_out`` with
        ``runtime_cap_exceeded``, never as staleness. Which timer ended it is
        the difference between "you were given an hour and used it" and "we
        thought you were dead".
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age(conn, record.id, 3601)
                result = ex.reconcile(conn, policy=live_policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.timed_out
            assert refreshed.status == ex.STATUS_TIMED_OUT
            assert "runtime_cap_exceeded" in refreshed.termination_reason
            assert "stale" not in refreshed.termination_reason
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_stale_heartbeat_still_reaps_a_short_lived_execution(
        self, kanban_home, workroot, live_policy
    ):
        """The rule is subordinated, not disabled.

        An execution whose authorized cap is BELOW the liveness window is
        still reconciled by whichever bound comes first — here the cap, which
        is the correct authority. The liveness rule remains the backstop for
        an execution with no cap at all.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=60)
                _age(conn, record.id, 600)
                result = ex.reconcile(conn, policy=live_policy)
                refreshed = ex.get_execution(conn, record.id)
            assert refreshed.status in (ex.STATUS_TIMED_OUT, ex.STATUS_STALE)
            assert record.id not in result.untouched
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_lease_expiry_alone_never_kills_an_authorized_worker(
        self, kanban_home, workroot
    ):
        """SIGTERM at lease expiry — the dispatcher half of the same rule.

        The 900 s claim TTL lapsed on t_aef6bbe1 at 11:48:10 while the worker
        was healthy. ``release_stale_claims`` must extend the lease, not
        terminate the worker: a lease is a statement about who owns the card,
        never about how long the work was authorized to take.
        """
        proc = _sleeper(workroot)
        signalled: list[tuple[int, int]] = []
        try:
            with kb.connect_closing() as conn:
                tid = kb.create_task(conn, title="authorized long work",
                                     assignee="default")
                claimed = kb.claim_task(conn, tid)
                assert claimed is not None
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, max_runtime_seconds = 3600, "
                    "claim_expires = ? WHERE id = ?",
                    (proc.pid, int(time.time()) - 1, tid),
                )
                conn.commit()

                reclaimed = kb.release_stale_claims(
                    conn, signal_fn=lambda p, s: signalled.append((p, s)),
                )
                task = kb.get_task(conn, tid)
                kinds = [
                    r["kind"] for r in conn.execute(
                        "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                    )
                ]
            assert signalled == [], "an expired lease signalled a live worker"
            assert reclaimed == 0
            assert task.status == "running"
            assert "claim_extended" in kinds
            assert "reclaimed" not in kinds
            assert ex.process_identity(proc.pid) is not None
        finally:
            _reap(proc.pid)


# ---------------------------------------------------------------------------
# 2. Classification: who ended it, recorded as a fact rather than a race
# ---------------------------------------------------------------------------


class TestTerminationClassification:
    def test_a_supervisor_sigterm_is_not_recorded_as_nonzero_exit(
        self, kanban_home, workroot
    ):
        """The exact misrecording on x_3b16666deaab836e.

        The supervisor signals the group, then waits out the SIGTERM grace
        window; the controller's ``communicate()`` reaps the child first and
        settles with the only evidence it has — exit 143. Recording the intent
        before signalling makes the classification survive that race whichever
        settler wins.
        """
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
            )
            ex._note_termination_intent(
                conn, record.id, status=ex.STATUS_STALE, reason="stale_heartbeat",
            )
            # Exactly what run_supervised's waiter passes on a signalled death.
            assert ex._settle(
                conn, record.id, status=ex.STATUS_FAILED,
                exit_code=143, reason="nonzero_exit",
            )
            refreshed = ex.get_execution(conn, record.id)
        assert refreshed.status == ex.STATUS_STALE
        assert refreshed.exit_code == 143
        assert "stale_heartbeat" in refreshed.termination_reason
        assert refreshed.termination_reason != "nonzero_exit"
        assert ex.is_infrastructure_termination(refreshed.status)

    def test_a_genuine_nonzero_exit_is_still_an_implementation_failure(
        self, kanban_home, workroot, live_policy
    ):
        """The carve-out must not swallow real failures.

        With no termination intent on record, exit 1 means what it has always
        meant. Otherwise the repair would make every failing executor look like
        infrastructure and nothing would ever be charged.
        """
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [sys.executable, "-c", "import sys; sys.exit(1)"]},
            cwd=str(workroot), policy=live_policy,
        )
        assert result.status == ex.STATUS_FAILED
        assert result.termination_reason == "nonzero_exit"
        assert not ex.is_infrastructure_termination(result.status)

    def test_each_infrastructure_cause_is_classified_separately(self):
        """Timeout, lease/liveness, provider/controller loss and operator
        termination are four different facts and stay four different statuses;
        only implementation failure is outside the set."""
        assert ex.is_infrastructure_termination(ex.STATUS_TIMED_OUT)
        assert ex.is_infrastructure_termination(ex.STATUS_STALE)
        assert ex.is_infrastructure_termination(ex.STATUS_CONTROLLER_LOST)
        assert ex.is_infrastructure_termination(ex.STATUS_TERMINATED)
        assert not ex.is_infrastructure_termination(ex.STATUS_FAILED)
        assert not ex.is_infrastructure_termination(ex.STATUS_COMPLETED)
        assert len(set(ex.INFRASTRUCTURE_STATUSES)) == 4


# ---------------------------------------------------------------------------
# 3. Retry budget: infrastructure is not charged to the work
# ---------------------------------------------------------------------------


def _running_task(conn, *, failures: int = 0) -> str:
    tid = kb.create_task(conn, title="repair under supervision", assignee="default")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    if failures:
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ? WHERE id = ?", (failures, tid)
        )
        conn.commit()
    return tid


class TestRetryBudget:
    @pytest.mark.parametrize(
        "status", [ex.STATUS_TIMED_OUT, ex.STATUS_STALE,
                   ex.STATUS_CONTROLLER_LOST, ex.STATUS_TERMINATED],
    )
    def test_infrastructure_termination_does_not_spend_a_retry(
        self, kanban_home, workroot, status
    ):
        """Routed into recovery, yes. Charged for it, no.

        The counter is PRESERVED rather than reset — a genuine earlier failure
        keeps its weight — and a ``infrastructure_failure_not_counted`` event
        says out loud that the carve-out fired, so a counter that declines to
        increment is never mistaken for a broken counter.
        """
        with kb.connect_closing() as conn:
            tid = _running_task(conn, failures=1)
            record = ex.create_execution(
                conn, task_id=tid, executor_type="shell",
                command_class="shell.argv", cwd=str(workroot),
                controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                route_task=True,
            )
            ex._settle(conn, record.id, status=status, reason="infra")
            settled = ex.get_execution(conn, record.id)
            decision = ex.route_task_from_execution(conn, settled)
            task = kb.get_task(conn, tid)
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                )
            ]
        assert decision == "recovery"
        assert task.consecutive_failures == 1, "infrastructure spent a retry"
        assert "gave_up" not in kinds
        assert "infrastructure_failure_not_counted" in kinds
        assert ex.EVENT_EXECUTION_FAILED in kinds

    def test_an_implementation_failure_still_spends_a_retry(
        self, kanban_home, workroot
    ):
        with kb.connect_closing() as conn:
            tid = _running_task(conn, failures=1)
            record = ex.create_execution(
                conn, task_id=tid, executor_type="shell",
                command_class="shell.argv", cwd=str(workroot),
                controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                route_task=True,
            )
            ex._settle(
                conn, record.id, status=ex.STATUS_FAILED,
                exit_code=1, reason="nonzero_exit",
            )
            settled = ex.get_execution(conn, record.id)
            ex.route_task_from_execution(conn, settled)
            task = kb.get_task(conn, tid)
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                )
            ]
        assert task.consecutive_failures == 2
        assert "infrastructure_failure_not_counted" not in kinds

    def test_the_task_level_runtime_timer_does_not_spend_a_retry_either(
        self, kanban_home, workroot
    ):
        """``enforce_max_runtime`` is the SECOND infrastructure-timeout path.

        Added after the independent ``codex_verify`` review of t_f9b3b48b
        found that repairing only ``route_task_from_execution`` left the
        dispatcher's own runtime reaper still charging the implementation
        budget — so a task could be given up on for the offence of being long.
        Both paths must agree, or the guarantee is only true of whichever one
        happens to fire.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                tid = _running_task(conn, failures=kb.DEFAULT_FAILURE_LIMIT - 1)
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, max_runtime_seconds = 1, "
                    "started_at = started_at - 600 WHERE id = ?",
                    (proc.pid, tid),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at = started_at - 600 "
                    "WHERE task_id = ?",
                    (tid,),
                )
                conn.commit()

                timed_out = kb.enforce_max_runtime(
                    conn, signal_fn=lambda p, s: os.kill(p, s),
                )
                task = kb.get_task(conn, tid)
                kinds = [
                    r["kind"] for r in conn.execute(
                        "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                    )
                ]
            assert tid in timed_out
            assert "timed_out" in kinds, "the timeout itself must still be recorded"
            assert "gave_up" not in kinds, "a long task was given up on for the clock"
            assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT - 1
            assert "infrastructure_failure_not_counted" in kinds
        finally:
            _reap(proc.pid)

    def test_the_breaker_cannot_trip_on_an_infrastructure_termination(
        self, kanban_home, workroot
    ):
        """A card one failure below the limit must not be given up on because
        the control plane killed its executor."""
        with kb.connect_closing() as conn:
            tid = _running_task(conn, failures=kb.DEFAULT_FAILURE_LIMIT - 1)
            record = ex.create_execution(
                conn, task_id=tid, executor_type="shell",
                command_class="shell.argv", cwd=str(workroot),
                controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                route_task=True,
            )
            ex._settle(conn, record.id, status=ex.STATUS_STALE, reason="infra")
            ex.route_task_from_execution(conn, ex.get_execution(conn, record.id))
            task = kb.get_task(conn, tid)
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                )
            ]
        assert "gave_up" not in kinds
        assert task.consecutive_failures == kb.DEFAULT_FAILURE_LIMIT - 1


# ---------------------------------------------------------------------------
# 4. Resume: the card must say "continue", not "this approach failed"
# ---------------------------------------------------------------------------


class TestResumeAfterInfrastructureTermination:
    def test_the_lane_marks_an_infrastructure_attempt_as_resumable(self):
        killed = rl.AttemptResult(
            "claude", 143, "", "", execution_status=ex.STATUS_STALE,
            execution_id="x_test",
        )
        failed = rl.AttemptResult(
            "claude", 1, "", "", execution_status=ex.STATUS_FAILED,
            execution_id="x_test",
        )
        assert killed.infrastructure and not killed.ok
        assert not failed.infrastructure and not failed.ok
        assert "failure_class=infrastructure" in killed.evidence
        assert "failure_class=implementation" in failed.evidence

    def test_the_blocked_card_tells_the_next_executor_to_resume(
        self, kanban_home, workroot, monkeypatch
    ):
        """t_aef6bbe1 was blocked with "failed or timed out", so a resumed run
        had no way to tell that its predecessor's workspace and evidence were
        intact and worth continuing from. The block reason now carries that."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="resume me", assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE,
            )
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, max_runtime_seconds = 3600 "
                "WHERE id = ?",
                (str(workroot), tid),
            )
            conn.commit()

        monkeypatch.setattr(
            rl, "_invoke_claude",
            lambda *a, **k: rl.AttemptResult(
                "claude", 143, "", "", execution_status=ex.STATUS_STALE,
                execution_id="x_killed_by_supervisor",
            ),
        )
        assert rl.run_claude_executor(tid) == 0

        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            blocked = conn.execute(
                "SELECT payload FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()["payload"]
        assert task.status == "blocked"
        assert "infrastructure" in blocked
        assert "Resume from the preserved workspace" in blocked
        assert "do not restart" in blocked.lower()
        # The preserved-history rule: blocking is additive, never a rewrite.
        assert task.consecutive_failures == 0
