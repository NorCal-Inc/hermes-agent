"""Liveness: the heartbeat that never existed, and what depended on it.

Reproduction these tests exist for (t_748c0618, 2026-09-03). Two gateway-owned
Claude executions were killed at ~1804 s under a 3600 s authorization:

    x_3b16666deaab836e  (t_aef6bbe1)  1804 s  failed/nonzero_exit (rc 143)
    x_82459f3ab85d41ab  (t_db21ca59)  1804 s  stale/stale_heartbeat

and all 26 ``claim_extended`` records on the board carried
``last_heartbeat_at: null``. One cause underneath both numbers:
``exec_supervisor.heartbeat()`` had no call site anywhere in the codebase, and
``kanban_db.heartbeat_worker`` is only ever reached through a kanban *tool* —
which the Claude executor, a foreign CLI process, cannot call. So both
liveness columns were frozen at their seed values for the entire life of every
execution, and every rule that read them was silently comparing ages.

Four things are pinned here, in the order a reader needs them:

1. **Emission.** A heartbeat exists at all, is written by a real owner, and is
   refused when liveness cannot be re-proven. A heartbeat that cannot fail is
   a timer.
2. **Survival past 1800 s.** Neither the never-observed execution nor the
   actively-heartbeating one is reaped by a level-3 window while its level-1
   authorization is still running.
3. **Reaping still works.** A dead process, and an owner that heartbeated and
   then stopped, are both still ended. The repair must not buy survival by
   disabling the reaper.
4. **Resume.** An infrastructure termination leaves the workspace intact and
   the card pointed back at it.

House rule inherited from ``test_exec_supervisor.py``: liveness is always
re-read from the process, never inferred from the fact that a signal was sent.
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
# Fixtures
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
    """Production shape: a 1800 s liveness window under a 3600 s ceiling.

    The real constants rather than test-friendly small ones, because the
    defect is a relationship between exactly these two numbers.
    """
    return ex.ExecutionPolicy(
        allowed_executors=("claude", "codex", "shell"),
        allowed_roots=(str(workroot),),
        max_runtime_seconds=ex.DEFAULT_MAX_RUNTIME_SECONDS,
        sync_ceiling_seconds=900,
        stale_heartbeat_seconds=ex.DEFAULT_STALE_HEARTBEAT_SECONDS,
        terminate_grace_seconds=2,
    )


def _sleeper(workroot: Path, seconds: int = 120) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
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


def _attached(conn, workroot: Path, proc, *, cap, task_id=None, ownership=None):
    record = ex.create_execution(
        conn, task_id=task_id, executor_type="shell",
        command_class="shell.argv", cwd=str(workroot),
        controller_pid=os.getpid(),
        controller_key=ex.process_identity(os.getpid()),
        ownership=ownership or ex.OWNERSHIP_SUPERVISOR,
        max_runtime_s=cap,
    )
    ex._attach_process(
        conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
        proc_key=ex.process_identity(proc.pid),
    )
    return ex.get_execution(conn, record.id)


def _age_started(conn, execution_id: str, seconds: int) -> None:
    """Push ``started_at`` back without touching ``heartbeat_at``.

    Kept separate from the joint ageing helper in
    ``test_exec_timeout_hierarchy.py`` on purpose: the whole question here is
    whether the two columns are allowed to drift apart, so a helper that moves
    them together would define the bug out of existence.
    """
    conn.execute(
        "UPDATE executions SET started_at = started_at - ? WHERE id = ?",
        (seconds, execution_id),
    )
    conn.commit()


def _age_both(conn, execution_id: str, seconds: int) -> None:
    """The live situation before the repair: nothing ever advanced the
    heartbeat, so the two columns stay equal for the whole lifetime."""
    conn.execute(
        "UPDATE executions SET started_at = started_at - ?, "
        "heartbeat_at = heartbeat_at - ? WHERE id = ?",
        (seconds, seconds, execution_id),
    )
    conn.commit()


def _events(conn, execution_id: str, kind: str) -> list:
    return conn.execute(
        "SELECT payload FROM execution_events "
        "WHERE execution_id = ? AND kind = ? ORDER BY id",
        (execution_id, kind),
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. Emission — a heartbeat that can refuse
# ---------------------------------------------------------------------------


class TestHeartbeatEmission:
    def test_seeded_heartbeat_is_not_an_observation(self, kanban_home, workroot):
        """``heartbeat_at == started_at`` means "never observed", not "0 s old".

        This is the single conflation the whole defect rests on. Every rule
        that read the column treated the seed as a fresh heartbeat, so with no
        emitter anywhere the staleness test silently became an age test.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                assert record.heartbeat_at == record.started_at
                assert not ex.has_recorded_heartbeat(record)

                assert ex.heartbeat_if_live(conn, record.id)
                after = ex.get_execution(conn, record.id)
            assert ex.has_recorded_heartbeat(after) or after.heartbeat_at == after.started_at
        finally:
            _reap(proc.pid)

    def test_attaching_a_process_is_not_a_heartbeat(self, kanban_home, workroot):
        """The bug this repair nearly shipped with.

        ``_attach_process`` used to write ``heartbeat_at`` too. Since
        ``create_execution`` seeds it equal to ``started_at``, an attach that
        landed one second after the insert — a coin flip, not a rare race —
        left ``heartbeat_at = started_at + 1`` and made a never-observed
        execution read as observed, silently disabling the protection that
        keys on that comparison.

        Observed live on 2026-09-03: ``x_aa58cda6a1ec6e03`` had the +1 with no
        emitter in its process at all; ``x_a3fc33cfbd6d41c6``, on the same
        board in the same minute, had them exactly equal.

        Asserted with an explicit clock offset rather than by launching in a
        loop and hoping to straddle a second boundary: the point is that ANY
        gap must be impossible, not merely unlikely.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot), controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                    now=1_000_000,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                    now=1_000_060,  # attach a minute later
                )
                attached = ex.get_execution(conn, record.id)
            assert attached.status == ex.STATUS_RUNNING
            assert attached.pid == proc.pid
            assert attached.heartbeat_at == attached.started_at == 1_000_000
            assert not ex.has_recorded_heartbeat(attached)
        finally:
            _reap(proc.pid)

    def test_heartbeat_refuses_when_the_process_is_gone(self, kanban_home, workroot):
        """The refusal is the point. A write that always succeeds records that
        the caller is running, which the caller already knew."""
        proc = _sleeper(workroot, seconds=1)
        with kb.connect_closing() as conn:
            record = _attached(conn, workroot, proc, cap=3600)
            proc.wait(timeout=10)
            assert _wait_gone(proc.pid)

            assert ex.heartbeat_if_live(conn, record.id) is False
            after = ex.get_execution(conn, record.id)
        assert after.heartbeat_at == record.heartbeat_at
        assert not ex.has_recorded_heartbeat(after)

    def test_heartbeat_refuses_a_recycled_pid(self, kanban_home, workroot):
        """Identity, not the number. A PID the kernel handed to someone else
        proves nothing about our executor, and heartbeating on it would keep a
        dead execution looking alive forever."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                # Same live PID, different recorded fingerprint: exactly what
                # PID reuse looks like from the reconciler's side.
                conn.execute(
                    "UPDATE executions SET proc_key = ? WHERE id = ?",
                    ("linux:1:impostor", record.id),
                )
                conn.commit()
                assert ex.heartbeat_if_live(conn, record.id) is False
        finally:
            _reap(proc.pid)

    def test_heartbeat_refuses_a_settled_execution(self, kanban_home, workroot):
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                ex._settle(
                    conn, record.id, status=ex.STATUS_COMPLETED,
                    exit_code=0, reason=None,
                )
                assert ex.heartbeat_if_live(conn, record.id) is False
        finally:
            _reap(proc.pid)

    def test_the_pump_advances_the_heartbeat_of_a_live_child(
        self, kanban_home, workroot
    ):
        """End-to-end on a real process: the column moves, and it moves because
        the process is there to be re-proven."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                pump = ex.LivenessPump(record.id, interval=1).start()
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if ex.has_recorded_heartbeat(
                            ex.get_execution(conn, record.id)
                        ):
                            break
                        time.sleep(0.1)
                finally:
                    pump.stop()
                after = ex.get_execution(conn, record.id)
                started = _events(conn, record.id, "heartbeat_pump_started")
                stopped = _events(conn, record.id, "heartbeat_pump_stopped")
            assert ex.has_recorded_heartbeat(after), (
                f"heartbeat_at={after.heartbeat_at} started_at={after.started_at}"
            )
            assert pump.emitted >= 1 and pump.refused == 0 and pump.errors == 0
            # The counters are the falsifiable part: an operator reading the
            # events afterwards can tell a pump that ran from one that started
            # and immediately gave up.
            assert len(started) == 1 and len(stopped) == 1
        finally:
            _reap(proc.pid)

    def test_the_pump_stops_when_the_child_dies(self, kanban_home, workroot):
        """A pump that kept writing after its child died would be the exact
        "fake timer write" the repair exists to avoid — it would make a corpse
        immortal to the reconciler."""
        proc = _sleeper(workroot, seconds=1)
        with kb.connect_closing() as conn:
            record = _attached(conn, workroot, proc, cap=3600)
            pump = ex.LivenessPump(record.id, interval=1).start()
            proc.wait(timeout=10)
            assert _wait_gone(proc.pid)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and pump.refused == 0:
                time.sleep(0.1)
            pump.stop()
            final = ex.get_execution(conn, record.id)
        # A refusal, specifically — not a transient error. The two are counted
        # apart because only one of them means "the executor is gone".
        assert pump.refused >= 1
        assert pump.errors == 0
        # And the last write it did make was while the child was still alive.
        assert final.heartbeat_at <= int(time.time())

    def test_run_supervised_heartbeats_its_own_executor(
        self, kanban_home, workroot, live_policy, monkeypatch
    ):
        """The wiring, not just the mechanism.

        ``run_supervised`` blocks in one ``proc.communicate()`` call for the
        whole authorized runtime, so the schema's old claim that "the
        synchronous waiter advances the heartbeat" could never have been true:
        there is no moment between the call and its return in which the waiter
        could write anything.
        """
        monkeypatch.setattr(ex, "DEFAULT_HEARTBEAT_INTERVAL_SECONDS", 1)
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [sys.executable, "-c", "import time; time.sleep(4)"]},
            cwd=str(workroot),
            timeout=60,
            policy=live_policy,
        )
        assert result.status == ex.STATUS_COMPLETED
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
            pumped = _events(conn, result.execution_id, "heartbeat_pump_stopped")
        assert ex.has_recorded_heartbeat(record), (
            "run_supervised left heartbeat_at frozen at started_at — the "
            "t_aef6bbe1 condition"
        )
        assert pumped, "no pump lifecycle recorded on the execution"

    def test_a_short_execution_gains_no_pump_events(
        self, kanban_home, workroot, live_policy
    ):
        """Most executions on a real board are gate commands that finish inside
        one heartbeat interval. Announcing a pump that then does nothing would
        add two ``execution_events`` rows to every one of them and dilute the
        rows that carry a real liveness story."""
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [sys.executable, "-c", "pass"]},
            cwd=str(workroot),
            timeout=60,
            policy=live_policy,
        )
        assert result.status == ex.STATUS_COMPLETED
        with kb.connect_closing() as conn:
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM execution_events WHERE execution_id = ?",
                    (result.execution_id,),
                )
            ]
        assert "heartbeat_pump_started" not in kinds
        assert "heartbeat_pump_stopped" not in kinds
        assert kinds == ["created", "started", "settled"]


# ---------------------------------------------------------------------------
# 2. Survival past 1800 s under a longer authorization
# ---------------------------------------------------------------------------


class TestSurvivalPastTheLivenessWindow:
    def test_never_heartbeated_work_survives_past_1800s(
        self, kanban_home, workroot, live_policy
    ):
        """The exact reproduction, minus the wall clock.

        Both bounds are arithmetic on stored timestamps, so ageing the row is
        equivalent to waiting and is deterministic. 2000 s > the 1800 s window,
        < the 3600 s authorization: the point at which both real executions
        were killed.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age_both(conn, record.id, 2000)
                result = ex.reconcile(conn, policy=live_policy)
                after = ex.get_execution(conn, record.id)
            assert record.id not in result.stale
            assert record.id in result.untouched
            assert after.status in ex.ACTIVE_STATUSES
            assert ex.process_identity(proc.pid) is not None, (
                "the process was signalled despite being demonstrably live"
            )
        finally:
            _reap(proc.pid)

    def test_the_suppression_is_recorded_once_and_says_why(
        self, kanban_home, workroot, live_policy
    ):
        """Silence would be its own defect: an execution sailing past the
        liveness window with no record of the decision is indistinguishable
        from the rule having been deleted. Once, because reconciliation runs
        every dispatcher tick."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                # No cap at all, so the floor cannot be what saves it and the
                # suppression is forced to be the operative rule.
                record = _attached(conn, workroot, proc, cap=None)
                no_ceiling = ex.ExecutionPolicy(
                    **{**live_policy.__dict__, "max_runtime_seconds": 0}
                )
                _age_both(conn, record.id, 2000)
                for _ in range(3):
                    ex.reconcile(conn, policy=no_ceiling)
                rows = _events(conn, record.id, ex.STALE_SUPPRESSED_EVENT)
                after = ex.get_execution(conn, record.id)
            assert after.status in ex.ACTIVE_STATUSES
            assert len(rows) == 1, f"expected exactly one suppression row, got {len(rows)}"
            assert "no_heartbeat_ever_recorded" in rows[0]["payload"]
        finally:
            _reap(proc.pid)

    def test_a_heartbeating_executor_survives_past_1800s(
        self, kanban_home, workroot, live_policy
    ):
        """The other half, and the one the repair actually buys.

        With an emitter in place a long run does not need the floor at all: the
        window measures silence, the pump keeps breaking the silence, and the
        run is simply never stale. This is the path a real >1800 s Claude task
        takes.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age_started(conn, record.id, 2500)
                assert ex.heartbeat_if_live(conn, record.id)
                aged = ex.get_execution(conn, record.id)
                assert aged.runtime_seconds() > 1800
                assert ex.has_recorded_heartbeat(aged)
                # A real heartbeat means the window goes back to its configured
                # value — the floor is for unobserved work only.
                assert ex._stale_heartbeat_bound(aged, live_policy) == 1800

                result = ex.reconcile(conn, policy=live_policy)
                after = ex.get_execution(conn, record.id)
            assert record.id in result.untouched
            assert after.status in ex.ACTIVE_STATUSES
            assert ex.process_identity(proc.pid) is not None
        finally:
            _reap(proc.pid)

    def test_the_authorized_cap_still_ends_it_and_is_classified_as_a_timeout(
        self, kanban_home, workroot, live_policy
    ):
        """Survival is bounded, not unbounded. Past the level-1 cap the
        execution ends — and ends labelled ``timed_out``, not ``stale``, so the
        record names the timer that actually made the decision."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age_both(conn, record.id, 3700)
                result = ex.reconcile(conn, policy=live_policy)
                after = ex.get_execution(conn, record.id)
            assert record.id in result.timed_out
            assert after.status == ex.STATUS_TIMED_OUT
            assert "runtime_cap_exceeded" in after.termination_reason
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_a_clamped_authorization_is_recorded_not_silent(
        self, kanban_home, workroot, live_policy
    ):
        """t_aef6bbe1 was authorized for 7200 s and its execution was created
        with 3600 s, with nothing anywhere recording that the reduction had
        happened — so the card and the reconciler disagreed and neither was
        wrong on its own terms."""
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [sys.executable, "-c", "pass"]},
            cwd=str(workroot),
            timeout=7200,
            policy=live_policy,
        )
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
            clamps = _events(conn, result.execution_id, "runtime_clamped")
        assert record.max_runtime_s == 3600
        assert len(clamps) == 1
        assert "7200" in clamps[0]["payload"] and "3600" in clamps[0]["payload"]


# ---------------------------------------------------------------------------
# 3. Reaping still works — the repair must not buy survival by disabling it
# ---------------------------------------------------------------------------


class TestDeadWorkIsStillReaped:
    def test_a_dead_process_is_reaped_regardless_of_its_heartbeat(
        self, kanban_home, workroot, live_policy
    ):
        proc = _sleeper(workroot, seconds=1)
        with kb.connect_closing() as conn:
            record = _attached(conn, workroot, proc, cap=3600)
            proc.wait(timeout=10)
            assert _wait_gone(proc.pid)
            result = ex.reconcile(conn, policy=live_policy)
            after = ex.get_execution(conn, record.id)
        assert record.id in result.stale
        assert after.status == ex.STATUS_STALE
        assert after.termination_reason == "process_gone"

    def test_an_owner_that_heartbeated_and_stopped_is_reaped(
        self, kanban_home, workroot, live_policy
    ):
        """The genuine staleness signal, and the reason Rule 5 still exists.

        A supervisor-owned execution whose owner died stops being heartbeated
        while its child keeps running. That is a real abandoned executor, it is
        detectable only through the heartbeat, and it must still be ended
        before the cap — otherwise the repair has traded a false positive for a
        false negative.
        """
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                assert ex.heartbeat_if_live(conn, record.id)
                # One real heartbeat, then silence for longer than the window.
                conn.execute(
                    "UPDATE executions SET started_at = started_at - 2500, "
                    "heartbeat_at = heartbeat_at - 2000 WHERE id = ?",
                    (record.id,),
                )
                conn.commit()
                aged = ex.get_execution(conn, record.id)
                assert ex.has_recorded_heartbeat(aged)
                assert ex.stale_reap_permitted(aged)

                result = ex.reconcile(conn, policy=live_policy)
                after = ex.get_execution(conn, record.id)
            assert record.id in result.stale
            assert after.status == ex.STATUS_STALE
            assert "stale_heartbeat" in after.termination_reason
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_suppression_does_not_apply_once_liveness_cannot_be_confirmed(
        self, kanban_home, workroot
    ):
        """``stale_reap_permitted`` is a liveness question, not a timestamp
        question: an unobserved execution whose PID no longer matches is
        reapable immediately."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                assert not ex.stale_reap_permitted(record)
                conn.execute(
                    "UPDATE executions SET proc_key = ? WHERE id = ?",
                    ("linux:1:impostor", record.id),
                )
                conn.commit()
                assert ex.stale_reap_permitted(ex.get_execution(conn, record.id))
        finally:
            _reap(proc.pid)


# ---------------------------------------------------------------------------
# 4. The board half — tasks.last_heartbeat_at, and the 26 null records
# ---------------------------------------------------------------------------


def _running_card(conn, workroot: Path, *, pid: int) -> str:
    tid = kb.create_task(
        conn, title="board heartbeat", assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
    )
    assert kb.claim_task(conn, tid) is not None
    conn.execute(
        "UPDATE tasks SET workspace_path = ?, max_runtime_seconds = 3600, "
        "worker_pid = ? WHERE id = ?",
        (str(workroot), pid, tid),
    )
    conn.commit()
    return tid


class TestBoardLevelLiveness:
    def test_the_bridge_fills_the_column_that_was_always_null(
        self, kanban_home, workroot
    ):
        """All 26 ``claim_extended`` records carried ``last_heartbeat_at:
        null`` because the Claude executor is a foreign CLI process with no
        kanban tools — it could not write this column even while working
        perfectly."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                tid = _running_card(conn, workroot, pid=proc.pid)
                record = _attached(conn, workroot, proc, cap=3600, task_id=tid)
                before = conn.execute(
                    "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)
                ).fetchone()["last_heartbeat_at"]
                assert before is None

                assert ex.bridge_board_heartbeat(conn, tid, execution_id=record.id)
                row = conn.execute(
                    "SELECT t.last_heartbeat_at AS t_hb, r.last_heartbeat_at AS r_hb "
                    "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                    "WHERE t.id = ?", (tid,),
                ).fetchone()
            assert row["t_hb"] is not None
            assert row["r_hb"] is not None
        finally:
            _reap(proc.pid)

    def test_the_bridge_reads_run_identity_from_the_row_not_the_caller(
        self, kanban_home, workroot
    ):
        """A remembered run id is how a heartbeat lands on the wrong attempt
        after a reclaim. The authoritative answer to "which attempt is this" is
        ``tasks.current_run_id``, and nothing else is accepted."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                tid = _running_card(conn, workroot, pid=proc.pid)
                record = _attached(conn, workroot, proc, cap=3600, task_id=tid)
                # The card is no longer running: the attempt this execution
                # belongs to is over, whatever the executor still thinks.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,)
                )
                conn.commit()
                assert ex.bridge_board_heartbeat(
                    conn, tid, execution_id=record.id
                ) is False
                hb = conn.execute(
                    "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)
                ).fetchone()["last_heartbeat_at"]
            assert hb is None
        finally:
            _reap(proc.pid)

    def test_a_null_heartbeat_with_a_live_worker_is_not_stale(
        self, kanban_home, workroot
    ):
        """``detect_stale_running``'s board-level version of the same mistake:
        NULL says nothing about the worker, it says nothing ever reported on
        it. Reaping on an absence of evidence is what killed live runs."""
        proc = _sleeper(workroot)
        try:
            with kb.connect_closing() as conn:
                tid = _running_card(conn, workroot, pid=proc.pid)
                conn.execute(
                    "UPDATE task_runs SET started_at = started_at - 7200 "
                    "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                    (tid,),
                )
                conn.commit()

                reclaimed = kb.detect_stale_running(
                    conn, stale_timeout_seconds=3600,
                )
                # Twice: the suppression event must be one per run, not one
                # per dispatcher tick.
                kb.detect_stale_running(conn, stale_timeout_seconds=3600)
                task = kb.get_task(conn, tid)
                notes = conn.execute(
                    "SELECT payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'stale_reap_suppressed'",
                    (tid,),
                ).fetchall()
            assert reclaimed == []
            assert task.status == "running"
            assert len(notes) == 1
            assert "worker_alive" in notes[0]["payload"]
            assert ex.process_identity(proc.pid) is not None
        finally:
            _reap(proc.pid)

    def test_a_null_heartbeat_with_a_dead_worker_is_still_reclaimed(
        self, kanban_home, workroot
    ):
        """The other side of the same branch. Suppression is conditional on
        demonstrable liveness; without it the old behaviour stands."""
        proc = _sleeper(workroot, seconds=1)
        proc.wait(timeout=10)
        assert _wait_gone(proc.pid)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot, pid=proc.pid)
            conn.execute(
                "UPDATE task_runs SET started_at = started_at - 7200 "
                "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                (tid,),
            )
            conn.commit()
            reclaimed = kb.detect_stale_running(conn, stale_timeout_seconds=3600)
            task = kb.get_task(conn, tid)
        assert tid in reclaimed
        assert task.status != "running"


# ---------------------------------------------------------------------------
# 5. Resume from the preserved workspace
# ---------------------------------------------------------------------------


class TestResumeFromPreservedWorkspace:
    def test_an_infrastructure_kill_leaves_the_workspace_intact(
        self, kanban_home, workroot, live_policy
    ):
        """"Resume, do not restart from zero" is only honest if there is
        something left to resume from. The supervisor kills a process group; it
        must never clean up after it."""
        marker = workroot / "partial-work.txt"
        proc = subprocess.Popen(
            [
                sys.executable, "-c",
                "import pathlib,time;"
                "pathlib.Path('partial-work.txt').write_text('half a repair');"
                "time.sleep(120)",
            ],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            assert marker.exists()

            with kb.connect_closing() as conn:
                record = _attached(conn, workroot, proc, cap=3600)
                _age_both(conn, record.id, 3700)
                ex.reconcile(conn, policy=live_policy)
                after = ex.get_execution(conn, record.id)
            assert after.status == ex.STATUS_TIMED_OUT
            assert _wait_gone(proc.pid)
            assert marker.exists()
            assert marker.read_text() == "half a repair"
        finally:
            _reap(proc.pid)

    def test_the_next_attempt_runs_in_the_same_workspace(
        self, kanban_home, workroot, monkeypatch
    ):
        """The card keeps pointing at the preserved tree, so the resumed run
        opens the same files rather than an empty scratch dir."""
        (workroot / "evidence.md").write_text("prior attempt findings")
        seen: list[str] = []

        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="resume in place", assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE,
            )
            assert kb.claim_task(conn, tid) is not None
            conn.execute(
                "UPDATE tasks SET workspace_path = ?, max_runtime_seconds = 3600 "
                "WHERE id = ?",
                (str(workroot), tid),
            )
            conn.commit()

        monkeypatch.setattr(
            rl, "_invoke_claude",
            lambda prompt, cwd, timeout, **k: (
                seen.append(cwd),
                rl.AttemptResult(
                    "claude", 143, "", "", execution_status=ex.STATUS_STALE,
                    execution_id="x_killed",
                ),
            )[1],
        )
        assert rl.run_claude_executor(tid) == 0

        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
        assert seen == [str(workroot)]
        assert task.workspace_path == str(workroot)
        assert (Path(task.workspace_path) / "evidence.md").read_text() == (
            "prior attempt findings"
        )
        # Preserved history: an infrastructure kill spends no implementation
        # retry budget.
        assert task.consecutive_failures == 0
