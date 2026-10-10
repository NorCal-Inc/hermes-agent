"""Rule 2 (live task contract) for foreign CLIs — supervisor-level revocation.

Reproduction (Phase 4 finding 4, 2026-10-07): blocking a claude-lane card did
not stop its process. The gate that enforces the live contract lives inside
Hermes-native workers (``task_contract_gate`` via ``handle_function_call``);
the claude and codex lanes run foreign CLIs under ``run_supervised`` that never
pass through it, so a cancelled, archived or reclaimed card kept its executor
working until exit or the runtime cap. The only remedy was a manual
``hermes kanban exec terminate``.

Pinned here:

1. A cancelled card ends the process group on the next tick with a distinct,
   durable ``contract_revoked`` status; an archived card likewise. The
   recovery lane does NOT read it as an infrastructure termination to resume.
2. A replaced run (``current_run_id`` now names a different run) ends the
   process immediately, and no heartbeat from the old execution lands on the
   new run.
3. A card that LEFT ``running`` with no other owner (a run's own
   ``kanban_request_review``/``kanban_block``, or an operator block — the row
   cannot tell them apart) is NOT revoked on sight: a handoff grace clock
   starts, the run may finish its tail, and the recovery lane still harvests
   its deliverables. Only a process still alive past ``HANDOFF_GRACE_SECONDS``
   is revoked (``handoff_grace_expired:<status>``). A card seen back in
   ``running`` under the same run clears the clock.
4. A healthy running card and an execution with no ``task_id`` are untouched.
5. A transient board read error is not a revocation; only a read that keeps
   failing past the stale-heartbeat bound is.

Live data behind (3): 2026-10-09, 30 days, 23 of 131 ``claude.headless`` runs
moved their own card to review/blocked and kept running 3-266 s before
exiting. v1 of this change revoked on any non-running status and would have
killed those tails.

House rule inherited from ``test_exec_heartbeat_liveness.py``: liveness is
re-read from the process, never inferred from the fact that a signal was sent.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
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
def fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    """One liveness tick per second. The pump resolves the cadence from the
    module global at construction, which is what makes this observable."""
    monkeypatch.setattr(ex, "DEFAULT_HEARTBEAT_INTERVAL_SECONDS", 1)
    monkeypatch.setattr(ex, "DEFAULT_BOARD_HEARTBEAT_INTERVAL_SECONDS", 1)


@pytest.fixture
def handoff_grace(monkeypatch: pytest.MonkeyPatch):
    """Scale ``HANDOFF_GRACE_SECONDS`` (resolved at pump construction) so a
    test can sit inside or run past it with 1 s ticks."""
    def _set(seconds: int) -> None:
        monkeypatch.setattr(ex, "HANDOFF_GRACE_SECONDS", seconds)
    return _set


def _policy(workroot: Path, *, stale: int = ex.DEFAULT_STALE_HEARTBEAT_SECONDS):
    return ex.ExecutionPolicy(
        allowed_executors=("shell", "claude", "codex"),
        allowed_roots=(str(workroot),),
        max_runtime_seconds=120,
        sync_ceiling_seconds=900,
        stale_heartbeat_seconds=stale,
        terminate_grace_seconds=2,
    )


def _sleep_spec(seconds: int) -> dict:
    return {"argv": [sys.executable, "-c", f"import time; time.sleep({seconds})"]}


def _running_card(conn, workroot: Path) -> str:
    tid = kb.create_task(
        conn, title="contract", assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
    )
    assert kb.claim_task(conn, tid) is not None
    conn.execute(
        "UPDATE tasks SET workspace_path = ?, max_runtime_seconds = 3600 "
        "WHERE id = ?",
        (str(workroot), tid),
    )
    conn.commit()
    return tid


class _Run:
    """``run_supervised`` on a thread, so the test can mutate the board while
    the synchronous waiter is blocked in ``communicate``."""

    def __init__(self, **kwargs):
        self.result = None
        self.error = None
        self._thread = threading.Thread(target=self._go, kwargs=kwargs, daemon=True)
        self._thread.start()

    def _go(self, **kwargs):
        try:
            self.result = ex.run_supervised(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc

    def join(self, timeout: float = 30.0):
        self._thread.join(timeout=timeout)
        assert not self._thread.is_alive(), "run_supervised did not return"
        if self.error is not None:
            raise self.error
        return self.result


def _live_execution(conn, *, deadline: float = 10.0):
    """The one execution row this test launched, once it has a PID."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        row = conn.execute(
            "SELECT id, pid FROM executions WHERE pid IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return ex.get_execution(conn, row["id"])
        time.sleep(0.05)
    raise AssertionError("execution never attached a process")


def _wait_gone(pid, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ex.process_identity(pid) is None:
            return True
        time.sleep(0.05)
    return ex.process_identity(pid) is None


def _reap(pid: int) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _events(conn, execution_id: str, kind: str) -> list:
    return conn.execute(
        "SELECT payload FROM execution_events "
        "WHERE execution_id = ? AND kind = ? ORDER BY id",
        (execution_id, kind),
    ).fetchall()


# ---------------------------------------------------------------------------
# 1. Cancelled / archived card → terminated with contract_revoked; recovery does not resume
# ---------------------------------------------------------------------------


class TestCancelledCard:
    def test_cancelling_the_card_ends_the_process_on_the_next_tick(
        self, kanban_home, workroot, fast_ticks
    ):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(60), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                # Let at least one healthy tick pass before revoking.
                time.sleep(1.5)
                assert ex.process_identity(pid) is not None
                # Operator cancels the card under the run. Immediate: no
                # handoff tail is legitimate on a cancelled card.
                conn.execute(
                    "UPDATE tasks SET status = 'cancelled', current_run_id = NULL, "
                    "claim_lock = NULL WHERE id = ?", (tid,)
                )
                conn.commit()
                cancelled_at = time.monotonic()

                result = run.join()
                ended_after = time.monotonic() - cancelled_at
                assert result.status == ex.STATUS_CONTRACT_REVOKED
                assert result.termination_reason.startswith(
                    "contract_revoked:status_cancelled"
                )
                assert _wait_gone(pid)
                # Next tick (1 s) + SIGTERM grace (2 s) — nowhere near the
                # handoff grace, which must not apply here.
                assert ended_after < 6, ended_after

                settled = ex.get_execution(conn, record.id)
                assert settled.status == ex.STATUS_CONTRACT_REVOKED
                assert settled.is_terminal
                bound = _events(conn, record.id, ex.CONTRACT_BOUND_EVENT)
                revoked = _events(conn, record.id, ex.CONTRACT_REVOKED_EVENT)
                assert len(bound) == 1
                assert len(revoked) == 1
                assert "status_cancelled" in revoked[0]["payload"]
                assert _events(conn, record.id, ex.CONTRACT_HANDOFF_OBSERVED_EVENT) == []
            finally:
                _reap(pid)

        # The classification that keeps this from becoming a resume.
        assert ex.is_infrastructure_termination(ex.STATUS_CONTRACT_REVOKED) is False
        assert ex.STATUS_CONTRACT_REVOKED in ex.TERMINAL_STATUSES

    def test_archiving_the_card_ends_the_process_immediately(
        self, kanban_home, workroot, fast_ticks
    ):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(60), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                time.sleep(1.5)
                assert ex.process_identity(pid) is not None
                conn.execute(
                    "UPDATE tasks SET status = 'archived', current_run_id = NULL, "
                    "claim_lock = NULL WHERE id = ?", (tid,)
                )
                conn.commit()
                archived_at = time.monotonic()

                result = run.join()
                ended_after = time.monotonic() - archived_at
                assert result.status == ex.STATUS_CONTRACT_REVOKED
                assert result.termination_reason.startswith(
                    "contract_revoked:status_archived"
                )
                assert _wait_gone(pid)
                assert ended_after < 6, ended_after
                revoked = _events(conn, record.id, ex.CONTRACT_REVOKED_EVENT)
                assert len(revoked) == 1
                assert "status_archived" in revoked[0]["payload"]
            finally:
                _reap(pid)

    def test_a_missing_task_row_revokes_immediately(self, kanban_home, workroot):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            contract = ex.bind_task_contract(conn, tid)
            assert contract is not None
            conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
            conn.commit()
            check = ex.check_task_contract(conn, contract)
        assert check.revoke == "task_missing"

    def test_recovery_lane_does_not_resume_a_revoked_run(
        self, kanban_home, workroot, monkeypatch
    ):
        """A revoked contract must not produce the "resume from the preserved
        workspace" block that an infrastructure kill produces, nor a comment,
        nor a failure charge. The card already has a decision."""
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            before = kb.get_task(conn, tid)
            comments_before = conn.execute(
                "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()["n"]

        monkeypatch.setattr(
            rl, "_invoke_claude",
            lambda prompt, cwd, timeout, **k: rl.AttemptResult(
                "claude", -15, "", "",
                execution_status=ex.STATUS_CONTRACT_REVOKED,
                execution_id="x_revoked",
            ),
        )
        assert rl.run_claude_executor(tid) == 1

        with kb.connect_closing() as conn:
            after = kb.get_task(conn, tid)
            comments_after = conn.execute(
                "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()["n"]
            blocked_events = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked'", (tid,)
            ).fetchone()["n"]
        assert after.status == before.status
        assert after.current_run_id == before.current_run_id
        assert comments_after == comments_before
        assert blocked_events == 0

        attempt = rl.AttemptResult(
            "claude", -15, "", "", execution_status=ex.STATUS_CONTRACT_REVOKED,
        )
        assert attempt.revoked
        assert not attempt.infrastructure
        assert "failure_class=contract_revoked" in attempt.evidence

    def test_routing_writes_nothing_to_the_board_for_a_revoked_execution(
        self, kanban_home, workroot
    ):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            record = ex.create_execution(
                conn, task_id=tid, executor_type="shell",
                command_class="shell.argv", cwd=str(workroot),
                controller_pid=os.getpid(),
                controller_key=ex.process_identity(os.getpid()),
                ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=60,
                route_task=True,
            )
            ex._settle(
                conn, record.id, status=ex.STATUS_CONTRACT_REVOKED,
                reason="contract_revoked:test",
            )
            events_before = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (tid,)
            ).fetchone()["n"]
            decision = ex.route_task_from_execution(conn, ex.get_execution(conn, record.id))
            events_after = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?", (tid,)
            ).fetchone()["n"]
            task = kb.get_task(conn, tid)
        assert decision == "contract_revoked"
        assert events_after == events_before
        assert task.status == "running"


# ---------------------------------------------------------------------------
# 2. Replaced run (a different run owns the card) → immediate
# ---------------------------------------------------------------------------


class TestReplacedRun:
    def test_a_new_run_id_ends_the_old_process_and_gets_no_heartbeat(
        self, kanban_home, workroot, fast_ticks
    ):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            old_run = kb.get_task(conn, tid).current_run_id
            assert old_run is not None

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(60), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                time.sleep(1.5)
                assert ex.process_identity(pid) is not None
                # Reclaim: the card stays running but under a NEW run. Done
                # with raw SQL so the heartbeat column is provably untouched
                # by anything but the pump under test.
                new_run = int(old_run) + 1000
                conn.execute(
                    "INSERT INTO task_runs (id, task_id, status, started_at) "
                    "SELECT ?, task_id, 'running', started_at + 1 FROM task_runs WHERE id = ?",
                    (new_run, old_run),
                )
                conn.execute(
                    "UPDATE tasks SET current_run_id = ?, last_heartbeat_at = NULL "
                    "WHERE id = ?",
                    (new_run, tid),
                )
                conn.commit()
                replaced_at = time.monotonic()

                result = run.join()
                ended_after = time.monotonic() - replaced_at
                assert result.status == ex.STATUS_CONTRACT_REVOKED
                assert result.termination_reason.startswith(
                    f"contract_revoked:run_replaced:{new_run}"
                )
                assert _wait_gone(pid)
                # Immediate — the handoff grace never applies to a replaced run.
                assert ended_after < 6, ended_after

                # Give the pump a moment past its stop; it must have written
                # nothing on the new run.
                time.sleep(1.2)
                row = conn.execute(
                    "SELECT t.last_heartbeat_at AS t_hb, r.last_heartbeat_at AS r_hb "
                    "FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                    "WHERE t.id = ?", (tid,),
                ).fetchone()
                assert row["t_hb"] is None
                assert row["r_hb"] is None
            finally:
                _reap(pid)

    def test_a_different_owner_is_immediate_even_outside_running(
        self, kanban_home, workroot
    ):
        """Status is irrelevant once another run owns the card: a card that is
        e.g. ``blocked`` under a NEW run is ``run_replaced``, not a handoff."""
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            contract = ex.bind_task_contract(conn, tid)
            old_run = contract.run_id
            conn.execute(
                "INSERT INTO task_runs (id, task_id, status, started_at) "
                "SELECT ?, task_id, 'running', started_at + 1 FROM task_runs WHERE id = ?",
                (old_run + 1000, old_run),
            )
            conn.execute(
                "UPDATE tasks SET status = 'blocked', current_run_id = ? WHERE id = ?",
                (old_run + 1000, tid),
            )
            conn.commit()
            check = ex.check_task_contract(conn, contract)
        assert check.revoke == f"run_replaced:{old_run + 1000}"
        assert check.handoff is None

    def test_the_bridge_refuses_a_run_other_than_the_recorded_one(
        self, kanban_home, workroot
    ):
        """Unit-level pin for ``bridge_board_heartbeat(expected_run_id=...)``:
        even if called, it does not heartbeat a run that is not its own."""
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            old_run = int(kb.get_task(conn, tid).current_run_id)
            conn.execute(
                "INSERT INTO task_runs (id, task_id, status, started_at) "
                "SELECT ?, task_id, 'running', started_at + 1 FROM task_runs WHERE id = ?",
                (old_run + 1000, old_run),
            )
            conn.execute(
                "UPDATE tasks SET current_run_id = ? WHERE id = ?",
                (old_run + 1000, tid),
            )
            conn.commit()
            assert ex.bridge_board_heartbeat(
                conn, tid, execution_id="x_old", expected_run_id=old_run,
            ) is False
            hb = conn.execute(
                "SELECT last_heartbeat_at FROM tasks WHERE id = ?", (tid,)
            ).fetchone()["last_heartbeat_at"]
        assert hb is None


# ---------------------------------------------------------------------------
# 3. Handoff grace: a card that left running with no other owner
# ---------------------------------------------------------------------------


def _self_handoff_to_review(conn, tid: str) -> None:
    """What ``kanban_request_review`` leaves on the row (via ``_end_run``):
    status review, current_run_id NULL, claim_lock NULL. Done through the real
    board call so the shape is the one the pump will meet in production."""
    run_id = kb.get_task(conn, tid).current_run_id
    assert kb.request_review(conn, tid, expected_run_id=run_id)
    row = conn.execute(
        "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    assert row["status"] == "review"
    assert row["current_run_id"] is None
    assert row["claim_lock"] is None


class TestHandoffGrace:
    def test_a_self_handoff_tail_inside_the_grace_is_not_terminated(
        self, kanban_home, workroot, fast_ticks, handoff_grace
    ):
        """The run moves its own card to review and keeps running for several
        ticks (the observed 3-266 s tail). Inside the grace it is left alone
        and completes normally."""
        handoff_grace(30)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(6), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                time.sleep(1.5)
                _self_handoff_to_review(conn, tid)
                # Several ticks pass with the card in review and the process alive.
                time.sleep(3)
                assert ex.process_identity(pid) is not None
                result = run.join()
                assert result.status == ex.STATUS_COMPLETED
                assert result.exit_code == 0
                assert result.termination_reason is None
                assert _events(conn, record.id, ex.CONTRACT_REVOKED_EVENT) == []
                observed = _events(conn, record.id, ex.CONTRACT_HANDOFF_OBSERVED_EVENT)
                assert len(observed) == 1
                assert '"status": "review"' in observed[0]["payload"]
                stopped = _events(conn, record.id, "heartbeat_pump_stopped")
                assert stopped and '"contract_revoked": false' in stopped[0]["payload"]
                assert kb.get_task(conn, tid).status == "review"
            finally:
                _reap(pid)

    def test_recovery_lane_harvests_after_a_self_handoff_tail(
        self, kanban_home, workroot, fast_ticks, handoff_grace, monkeypatch
    ):
        """End to end through ``run_claude_executor``: the executor hands its
        own card to review mid-run, writes its deliverable into the canonical
        attachment dir during the tail, and exits 0. The attempt is ``ok``,
        the harvest registers the file, and nothing blocks the card."""
        handoff_grace(30)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            comments_before = conn.execute(
                "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()["n"]

        def fake_invoke(prompt, cwd, timeout, *, task_id=None, **_):
            def _tail():
                with kb.connect_closing() as c:
                    _live_execution(c)
                    time.sleep(1.5)
                    _self_handoff_to_review(c, task_id)
                    out = kb.task_attachments_dir(task_id)
                    out.mkdir(parents=True, exist_ok=True)
                    (out / "deliverable.txt").write_text("done\n")
            threading.Thread(target=_tail, daemon=True).start()
            res = ex.run_supervised(
                command_class="shell.argv", spec=_sleep_spec(5), cwd=cwd,
                task_id=task_id, timeout=timeout, policy=_policy(workroot),
                route_task=False,
            )
            return rl.AttemptResult(
                "claude", res.exit_code, res.stdout, res.stderr,
                execution_status=res.status, execution_id=res.execution_id,
            )

        monkeypatch.setattr(rl, "_invoke_claude", fake_invoke)
        rc = rl.run_claude_executor(tid)

        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            names = {a.filename for a in kb.list_attachments(conn, tid)}
            comments_after = conn.execute(
                "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (tid,)
            ).fetchone()["n"]
            blocked_events = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked'", (tid,)
            ).fetchone()["n"]
            execution = conn.execute(
                "SELECT id, status FROM executions WHERE task_id = ? "
                "ORDER BY started_at DESC LIMIT 1", (tid,)
            ).fetchone()
            revoked = _events(conn, execution["id"], ex.CONTRACT_REVOKED_EVENT)
        # Harvest ran: the deliverable written during the tail is registered.
        assert "deliverable.txt" in names
        assert execution["status"] == ex.STATUS_COMPLETED
        assert revoked == []
        # The card keeps the run's own handoff; the lane neither blocked nor
        # commented on it (a revocation would have returned before harvest,
        # a failure would have blocked).
        assert task.status == "review"
        assert blocked_events == 0
        assert comments_after == comments_before
        assert rc in (0, 1)

    def test_grace_expiry_revokes_with_the_status_named(
        self, kanban_home, workroot, fast_ticks, handoff_grace
    ):
        """Operator block (same row shape as a self-handoff) and the process
        is still alive past the grace → revoked, reason names the status."""
        handoff_grace(2)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(60), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                time.sleep(1.5)
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', current_run_id = NULL, "
                    "claim_lock = NULL WHERE id = ?", (tid,)
                )
                conn.commit()
                blocked_at = time.monotonic()

                result = run.join()
                lived = time.monotonic() - blocked_at
                assert result.status == ex.STATUS_CONTRACT_REVOKED
                assert result.termination_reason == (
                    "contract_revoked:handoff_grace_expired:blocked"
                )
                assert _wait_gone(pid)
                # Not on sight: it lived at least the grace after the block.
                assert lived >= 2, lived
                assert len(_events(conn, record.id, ex.CONTRACT_HANDOFF_OBSERVED_EVENT)) == 1
                revoked = _events(conn, record.id, ex.CONTRACT_REVOKED_EVENT)
                assert len(revoked) == 1
                assert "handoff_grace_expired:blocked" in revoked[0]["payload"]
            finally:
                _reap(pid)

    def test_returning_to_running_under_the_same_run_clears_the_clock(
        self, kanban_home, workroot, fast_ticks, handoff_grace
    ):
        handoff_grace(3)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            run_id = kb.get_task(conn, tid).current_run_id

        run = _Run(
            command_class="shell.argv", spec=_sleep_spec(8), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        with kb.connect_closing() as conn:
            record = _live_execution(conn)
            pid = record.pid
            try:
                time.sleep(1.5)
                conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
                conn.commit()
                time.sleep(1.5)  # inside the grace
                conn.execute(
                    "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                    (run_id, tid),
                )
                conn.commit()
                # Well past the original grace start; the clock must be gone.
                result = run.join()
                assert result.status == ex.STATUS_COMPLETED
                assert _events(conn, record.id, ex.CONTRACT_REVOKED_EVENT) == []
                assert len(_events(conn, record.id, ex.CONTRACT_HANDOFF_OBSERVED_EVENT)) == 1
            finally:
                _reap(pid)

    def test_check_shapes(self, kanban_home, workroot):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
            contract = ex.bind_task_contract(conn, tid)
            assert ex.check_task_contract(conn, contract).holds
            for status in ("review", "blocked", "done", "ready", "todo", "triage"):
                conn.execute(
                    "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ?",
                    (status, tid),
                )
                conn.commit()
                check = ex.check_task_contract(conn, contract)
                assert check.revoke is None
                assert check.handoff == status
        assert ex.HANDOFF_GRACE_SECONDS == 300


# ---------------------------------------------------------------------------
# 4. Untouched: healthy running card; execution with no task
# ---------------------------------------------------------------------------


class TestUntouched:
    def test_a_healthy_running_card_runs_to_completion(
        self, kanban_home, workroot, fast_ticks
    ):
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_sleep_spec(4), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        assert result.status == ex.STATUS_COMPLETED
        assert result.exit_code == 0
        with kb.connect_closing() as conn:
            assert _events(conn, result.execution_id, ex.CONTRACT_BOUND_EVENT)
            assert _events(conn, result.execution_id, ex.CONTRACT_REVOKED_EVENT) == []
            stopped = _events(conn, result.execution_id, "heartbeat_pump_stopped")
            # Several ticks happened and the contract held on every one.
            assert stopped and '"contract_revoked": false' in stopped[0]["payload"]
            assert '"emitted": 0' not in stopped[0]["payload"]
            assert kb.get_task(conn, tid).status == "running"

    def test_an_execution_without_a_task_is_not_bound(
        self, kanban_home, workroot, fast_ticks
    ):
        result = ex.run_supervised(
            command_class="shell.argv", spec=_sleep_spec(3), cwd=str(workroot),
            task_id=None, timeout=60, policy=_policy(workroot),
        )
        assert result.status == ex.STATUS_COMPLETED
        with kb.connect_closing() as conn:
            assert _events(conn, result.execution_id, ex.CONTRACT_BOUND_EVENT) == []
            assert _events(conn, result.execution_id, ex.CONTRACT_UNBOUND_EVENT) == []
            assert _events(conn, result.execution_id, ex.CONTRACT_REVOKED_EVENT) == []

    def test_a_card_that_is_not_running_at_launch_is_recorded_unbound(
        self, kanban_home, workroot, fast_ticks
    ):
        """No contract to enforce → no revocation, and the ledger says why."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="unclaimed", assignee="default")
        result = ex.run_supervised(
            command_class="shell.argv", spec=_sleep_spec(2), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        assert result.status == ex.STATUS_COMPLETED
        with kb.connect_closing() as conn:
            assert _events(conn, result.execution_id, ex.CONTRACT_UNBOUND_EVENT)
            assert _events(conn, result.execution_id, ex.CONTRACT_REVOKED_EVENT) == []


# ---------------------------------------------------------------------------
# 5. Board read errors
# ---------------------------------------------------------------------------


class TestBoardReadErrors:
    def test_a_transient_read_error_is_not_a_revocation(
        self, kanban_home, workroot, fast_ticks, monkeypatch
    ):
        real = ex.check_task_contract
        failures = {"n": 0}

        def flaky(conn, contract):
            if failures["n"] < 2:
                failures["n"] += 1
                raise RuntimeError("database is locked")
            return real(conn, contract)

        monkeypatch.setattr(ex, "check_task_contract", flaky)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_sleep_spec(5), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot), route_task=False,
        )
        assert result.status == ex.STATUS_COMPLETED
        assert failures["n"] == 2
        with kb.connect_closing() as conn:
            assert _events(conn, result.execution_id, ex.CONTRACT_REVOKED_EVENT) == []
            stopped = _events(conn, result.execution_id, "heartbeat_pump_stopped")
            assert stopped and '"errors": 2' in stopped[0]["payload"]

    def test_a_persistently_unreadable_board_revokes_after_the_stale_bound(
        self, kanban_home, workroot, fast_ticks, monkeypatch
    ):
        """The bound is the existing stale-heartbeat window, scaled down here
        so the test can see it fire. ``0`` would disable this path."""
        def always_fails(conn, contract):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(ex, "check_task_contract", always_fails)
        with kb.connect_closing() as conn:
            tid = _running_card(conn, workroot)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_sleep_spec(60), cwd=str(workroot),
            task_id=tid, timeout=60, policy=_policy(workroot, stale=2),
            route_task=False,
        )
        assert result.status == ex.STATUS_CONTRACT_REVOKED
        assert result.termination_reason.startswith(
            "contract_revoked:contract_unreadable:"
        )
