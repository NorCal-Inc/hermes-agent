"""Execution supervisor: deterministic ownership of governed executors.

The bug these tests exist for is specific and was real. A Claude invocation was
launched synchronously, the caller hit its execution timeout and reported
EXECUTION FAILED, and the child survived and kept working outside any
controller — later producing valid commits nobody had authorised.

So the assertions are about ownership, not about exit codes:

* a durable record exists BEFORE the process does;
* a synchronous timeout cannot leave an unmanaged child;
* a dead controller cannot leave a live orphan merely logged;
* a recycled PID cannot be mistaken for the original executor (and is never
  signalled);
* an executor finishing cleanly does NOT complete a Gauntlet card — it hands
  it to verification, and every non-success routes into recovery.

Nothing here asserts that a signal was *sent*. Confusing "we sent SIGKILL"
with "the process ended" is the class of mistake this module was written to
remove, so every liveness assertion re-reads the process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import exec_supervisor as ex
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and a permitted workroot."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def workroot(kanban_home: Path) -> Path:
    """A directory inside the default allowed roots."""
    root = kanban_home / "work"
    root.mkdir(exist_ok=True)
    return root


@pytest.fixture
def policy(workroot: Path) -> ex.ExecutionPolicy:
    """A permissive-but-bounded policy for the happy paths.

    Explicit rather than config-derived so a test never depends on what
    happens to be in the operator's config file.
    """
    return ex.ExecutionPolicy(
        allowed_executors=("claude", "codex", "shell"),
        allowed_roots=(str(workroot),),
        max_runtime_seconds=60,
        sync_ceiling_seconds=60,
        stale_heartbeat_seconds=3600,
        terminate_grace_seconds=2,
    )


def _py(code: str) -> dict:
    """Spec for the ``shell.argv`` launcher running an inline Python program."""
    return {"argv": [sys.executable, "-c", code]}


def _reap(pid: int, timeout: float = 5.0) -> None:
    """Best-effort: make sure a test never leaves a process behind."""
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
    """Whether the process at ``pid`` really ended, re-read from /proc."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ex.process_identity(pid) is None:
            return True
        time.sleep(0.05)
    return ex.process_identity(pid) is None


# ---------------------------------------------------------------------------
# 1. The record exists before the process
# ---------------------------------------------------------------------------


class TestRecordBeforeProcess:
    def test_record_is_committed_before_the_child_starts(
        self, kanban_home, workroot, policy, monkeypatch
    ):
        """The ordering invariant, observed from inside the spawn itself.

        The child cannot be asked whether a row existed when it started, so
        the spawn is intercepted: at the moment Popen is called, a SEPARATE
        connection must already see a committed row in ``launching``. A row
        that existed only in the launching transaction would be invisible
        here, which is exactly the failure mode being excluded.
        """
        seen = {}
        real_start = ex._start_process

        def spy(argv, **kwargs):
            with kb.connect_closing() as other:
                rows = other.execute(
                    "SELECT id, status, ownership, pid FROM executions"
                ).fetchall()
                seen["rows"] = [dict(r) for r in rows]
            return real_start(argv, **kwargs)

        monkeypatch.setattr(ex, "_start_process", spy)

        result = ex.run_supervised(
            command_class="shell.argv",
            spec=_py("pass"),
            cwd=str(workroot),
            policy=policy,
        )
        assert result.succeeded
        assert len(seen["rows"]) == 1
        row = seen["rows"][0]
        assert row["status"] == ex.STATUS_LAUNCHING
        assert row["pid"] is None  # no process yet
        assert row["ownership"] == ex.OWNERSHIP_CONTROLLER

    def test_a_refused_launch_writes_no_record_and_starts_nothing(
        self, kanban_home, workroot, policy, monkeypatch
    ):
        """Policy runs before anything durable or live exists."""
        started = []
        monkeypatch.setattr(
            ex, "_start_process", lambda *a, **k: started.append(a) or None
        )
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex.run_supervised(
                command_class="shell.argv",
                spec=_py("pass"),
                cwd=str(Path(kanban_home).parent),  # outside allowed roots
                policy=policy,
            )
        assert exc.value.code == "cwd_outside_allowed_roots"
        assert started == []
        with kb.connect_closing() as conn:
            assert ex.list_executions(conn) == []


# ---------------------------------------------------------------------------
# 2. Exit capture
# ---------------------------------------------------------------------------


class TestExitCapture:
    def test_normal_exit_is_captured(self, kanban_home, workroot, policy):
        result = ex.run_supervised(
            command_class="shell.argv",
            spec=_py("print('hello')"),
            cwd=str(workroot),
            policy=policy,
        )
        assert result.status == ex.STATUS_COMPLETED
        assert result.exit_code == 0
        assert result.succeeded
        assert "hello" in result.stdout
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
        assert record.status == ex.STATUS_COMPLETED
        assert record.exit_code == 0
        assert record.ended_at is not None

    def test_nonzero_exit_is_captured_and_is_not_success(
        self, kanban_home, workroot, policy
    ):
        result = ex.run_supervised(
            command_class="shell.argv",
            spec=_py("import sys; sys.exit(3)"),
            cwd=str(workroot),
            policy=policy,
        )
        assert result.status == ex.STATUS_FAILED
        assert result.exit_code == 3
        assert not result.succeeded
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
        assert record.status == ex.STATUS_FAILED
        assert record.termination_reason == "nonzero_exit"

    def test_a_child_that_cannot_start_still_leaves_a_settled_record(
        self, kanban_home, workroot, policy
    ):
        """A refused-to-start execution is a fact worth keeping, not a gap."""
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [str(workroot / "does-not-exist")]},
            cwd=str(workroot),
            policy=policy,
        )
        assert result.status == ex.STATUS_FAILED
        assert result.termination_reason == "spawn_failed"
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
        assert record.is_terminal


# ---------------------------------------------------------------------------
# 3. THE bug: a synchronous timeout cannot leave an unmanaged child
# ---------------------------------------------------------------------------


class TestTimeoutLeavesNothingUnmanaged:
    def test_timeout_terminates_the_child(self, kanban_home, workroot, policy):
        policy = ex.ExecutionPolicy(
            **{**policy.__dict__, "max_runtime_seconds": 1, "sync_ceiling_seconds": 60}
        )
        pids = {}
        real_start = ex._start_process

        def spy(argv, **kwargs):
            proc = real_start(argv, **kwargs)
            pids["pid"] = proc.pid
            return proc

        import unittest.mock as mock

        with mock.patch.object(ex, "_start_process", spy):
            result = ex.run_supervised(
                command_class="shell.argv",
                spec=_py("import time; time.sleep(120)"),
                cwd=str(workroot),
                policy=policy,
            )
        try:
            assert result.status == ex.STATUS_TIMED_OUT
            assert result.timed_out is True
            assert not result.succeeded
            # The point of the whole module: the process is GONE, verified by
            # re-reading it, not inferred from the signal having been sent.
            assert _wait_gone(pids["pid"]), "child survived its controller's timeout"
            with kb.connect_closing() as conn:
                record = ex.get_execution(conn, result.execution_id)
            assert record.is_terminal
        finally:
            _reap(pids.get("pid"))

    def test_timeout_kills_the_whole_group_not_just_the_direct_child(
        self, kanban_home, workroot, policy
    ):
        """The grandchild is the process that actually escaped in the incident.

        ``subprocess.run(timeout=...)`` kills the direct child only, so a
        grandchild is reparented to init and keeps running. The child here
        spawns one and writes its pid out; after the timeout, BOTH must be
        gone.
        """
        policy = ex.ExecutionPolicy(
            **{**policy.__dict__, "max_runtime_seconds": 2, "sync_ceiling_seconds": 60}
        )
        pidfile = workroot / "grandchild.pid"
        code = (
            "import subprocess, sys, time, pathlib\n"
            f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)'])\n"
            f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
            "time.sleep(120)\n"
        )
        result = ex.run_supervised(
            command_class="shell.argv",
            spec={"argv": [sys.executable, "-c", code]},
            cwd=str(workroot),
            policy=policy,
        )
        assert result.status == ex.STATUS_TIMED_OUT
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists(), "grandchild never recorded its pid"
        grandchild = int(pidfile.read_text().strip())
        try:
            assert _wait_gone(grandchild), (
                "grandchild survived the controller's timeout — this is the "
                "exact orphan the supervisor exists to prevent"
            )
        finally:
            _reap(grandchild)

    def test_a_controller_exception_does_not_leave_the_child_running(
        self, kanban_home, workroot, policy, monkeypatch
    ):
        """Any exit path, not just the one the caller planned for."""
        pids = {}
        real_start = ex._start_process

        def spy(argv, **kwargs):
            proc = real_start(argv, **kwargs)
            pids["pid"] = proc.pid
            return proc

        monkeypatch.setattr(ex, "_start_process", spy)

        real_attach = ex._attach_process

        def boom(*args, **kwargs):
            real_attach(*args, **kwargs)
            raise KeyboardInterrupt("operator pressed ctrl-c")

        monkeypatch.setattr(ex, "_attach_process", boom)

        with pytest.raises(KeyboardInterrupt):
            ex.run_supervised(
                command_class="shell.argv",
                spec=_py("import time; time.sleep(120)"),
                cwd=str(workroot),
                policy=policy,
            )
        try:
            assert _wait_gone(pids["pid"]), "child outlived an interrupted controller"
            with kb.connect_closing() as conn:
                records = ex.list_executions(conn)
            assert len(records) == 1
            assert records[0].is_terminal
        finally:
            _reap(pids.get("pid"))

    def test_no_execution_is_ever_left_returned_but_unowned(
        self, kanban_home, workroot, policy
    ):
        """The invariant stated directly: no third ownership state.

        After every kind of ending, a row is either terminal or explicitly
        supervisor-owned. There is deliberately no row that is non-terminal
        and controller-owned once its controller has returned.
        """
        policy_fast = ex.ExecutionPolicy(
            **{**policy.__dict__, "max_runtime_seconds": 1, "sync_ceiling_seconds": 60}
        )
        ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), policy=policy,
        )
        ex.run_supervised(
            command_class="shell.argv", spec=_py("import sys; sys.exit(2)"),
            cwd=str(workroot), policy=policy,
        )
        ex.run_supervised(
            command_class="shell.argv", spec=_py("import time; time.sleep(60)"),
            cwd=str(workroot), policy=policy_fast,
        )
        with kb.connect_closing() as conn:
            for record in ex.list_executions(conn):
                assert record.is_terminal or record.ownership == ex.OWNERSHIP_SUPERVISOR, (
                    f"{record.id} is {record.status} and owned by "
                    f"{record.ownership} after its controller returned"
                )


# ---------------------------------------------------------------------------
# 4. Long work is background work from the start
# ---------------------------------------------------------------------------


class TestSynchronousCeiling:
    def test_a_request_over_the_ceiling_is_supervisor_owned_at_launch(
        self, kanban_home, workroot, policy
    ):
        """Not a longer wait — a different owner.

        The failure was an accidental orphan after a synchronous timeout. Work
        that could exceed the ceiling never depends on the caller in the first
        place, so losing the caller is a recorded no-op instead.
        """
        long_policy = ex.ExecutionPolicy(
            **{**policy.__dict__, "max_runtime_seconds": 300, "sync_ceiling_seconds": 5}
        )
        result = ex.run_supervised(
            command_class="shell.argv",
            spec=_py("pass"),
            cwd=str(workroot),
            timeout=120,
            policy=long_policy,
        )
        assert result.ownership == ex.OWNERSHIP_SUPERVISOR
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
        assert record.ownership == ex.OWNERSHIP_SUPERVISOR

    def test_background_launch_is_supervisor_owned_and_returns_immediately(
        self, kanban_home, workroot, policy
    ):
        record = ex.launch_background(
            command_class="shell.argv",
            spec=_py("import time; time.sleep(0.2)"),
            cwd=str(workroot),
            policy=policy,
        )
        try:
            assert record.ownership == ex.OWNERSHIP_SUPERVISOR
            assert record.status == ex.STATUS_RUNNING
            assert record.pid
            # Reconciliation, not the launcher, is what ends it.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with kb.connect_closing() as conn:
                    result = ex.reconcile(conn, policy=policy)
                    refreshed = ex.get_execution(conn, record.id)
                if refreshed.is_terminal:
                    break
                time.sleep(0.1)
            assert refreshed.status == ex.STATUS_RECOVERED
            assert refreshed.exit_code == 0
        finally:
            _reap(record.pid)

    def test_the_caller_cannot_raise_its_own_runtime_cap(
        self, kanban_home, workroot, policy
    ):
        """A caller may only ever ask for less than policy allows."""
        capped = ex.ExecutionPolicy(**{**policy.__dict__, "max_runtime_seconds": 30})
        assert capped.resolve_max_runtime("shell", 9999) == 30
        assert capped.resolve_max_runtime("shell", 5) == 5
        assert capped.resolve_max_runtime("shell", None) == 30


# ---------------------------------------------------------------------------
# 5. Reconciliation
# ---------------------------------------------------------------------------


class TestReconciliation:
    def test_dead_executor_with_dead_controller_is_controller_lost(
        self, kanban_home, workroot, policy
    ):
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn,
                executor_type="shell",
                command_class="shell.argv",
                cwd=str(workroot),
                controller_pid=999_999,          # never existed
                controller_key="linux:1:(gone)",
                ownership=ex.OWNERSHIP_CONTROLLER,
                max_runtime_s=60,
            )
            ex._attach_process(
                conn, record.id, pid=999_998, pgid=999_998,
                proc_key="linux:1:(also-gone)",
            )
            result = ex.reconcile(conn, policy=policy)
            refreshed = ex.get_execution(conn, record.id)
        assert record.id in result.controller_lost
        assert refreshed.status == ex.STATUS_CONTROLLER_LOST
        assert refreshed.termination_reason == "controller_and_executor_both_gone"

    def test_a_live_orphan_is_terminated_not_merely_logged(
        self, kanban_home, workroot, policy
    ):
        """The headline reconciliation case.

        A live child whose controller is gone is the split-brain condition.
        It is terminated (default policy) and the record says so; it is never
        left running with a log line about it.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=str(workroot),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn,
                    executor_type="shell",
                    command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=999_999,
                    controller_key="linux:1:(gone)",
                    ownership=ex.OWNERSHIP_CONTROLLER,
                    max_runtime_s=600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid,
                    pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                result = ex.reconcile(conn, policy=policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.controller_lost
            assert refreshed.status == ex.STATUS_CONTROLLER_LOST
            assert _wait_gone(proc.pid), "orphan was left alive"
        finally:
            _reap(proc.pid)

    def test_a_live_orphan_can_be_explicitly_adopted_instead(
        self, kanban_home, workroot, policy
    ):
        """The other deterministic option. Never 'neither'."""
        adopt_policy = ex.ExecutionPolicy(
            **{**policy.__dict__, "orphan_policy": ex.ORPHAN_POLICY_ADOPT}
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot), controller_pid=999_999,
                    controller_key="linux:1:(gone)",
                    ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                result = ex.reconcile(conn, policy=adopt_policy)
                refreshed = ex.get_execution(conn, record.id)
                events = ex.execution_events(conn, record.id)
            assert record.id in result.adopted
            assert refreshed.ownership == ex.OWNERSHIP_SUPERVISOR
            assert refreshed.status == ex.STATUS_RUNNING
            assert ex.process_identity(proc.pid) is not None, "adoption killed it"
            assert any(e["kind"] == "adopted" for e in events)
        finally:
            _reap(proc.pid)

    def test_runtime_cap_is_enforced_on_a_live_execution(
        self, kanban_home, workroot, policy
    ):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR,
                    max_runtime_s=1,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                # Age the row rather than sleeping: the cap is arithmetic on
                # started_at, so moving it back is exactly equivalent and
                # deterministic.
                conn.execute(
                    "UPDATE executions SET started_at = started_at - 600 WHERE id = ?",
                    (record.id,),
                )
                conn.commit()
                result = ex.reconcile(conn, policy=policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.timed_out
            assert refreshed.status == ex.STATUS_TIMED_OUT
            assert "runtime_cap_exceeded" in refreshed.termination_reason
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_stale_heartbeat_is_detected_and_reconciled(
        self, kanban_home, workroot, policy
    ):
        """The liveness rule still reaps — within its own authority.

        Rewritten twice on 2026-09-03, and the second rewrite is the one that
        matters. The original pinned ``max_runtime_s=3600`` against a 10 s
        liveness window and asserted the reap on an execution that had never
        heartbeated — i.e. it asserted the inversion that killed t_aef6bbe1 at
        half its authorized runtime. The first rewrite dodged that by shrinking
        the cap below the window, which made the fixture pass but described a
        situation the rule can no longer reach.

        What the rule means now: a heartbeat WAS recorded (by
        ``ex.LivenessPump``, after proving the process against /proc) and then
        stopped. That is genuine evidence of a lost owner, and reaping on it is
        the level-3 window doing its actual job rather than shadowing the
        level-1 cap. So the fixture writes one real heartbeat, ages it past the
        window, and leaves the authorized cap far away.

        The precedence half — never-heartbeated work surviving to its own cap —
        is pinned in ``test_exec_timeout_hierarchy.py`` and
        ``test_exec_heartbeat_liveness.py``.
        """
        stale_policy = ex.ExecutionPolicy(
            **{**policy.__dict__, "stale_heartbeat_seconds": 10}
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR,
                    # Comfortably above the aged runtime below, so Rule 3
                    # (the authorized cap) does not preempt and this really
                    # does exercise Rule 5.
                    max_runtime_s=3600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                # Staleness is now "a heartbeat existed and then stopped",
                # not "the row is old": ``ex.stale_reap_permitted`` refuses to
                # reap a live process that was never heartbeated at all, since
                # ``heartbeat_at == started_at`` is seeded, not observed. So
                # age ``started_at`` and leave ``heartbeat_at`` strictly after
                # it — one real heartbeat, 500 s ago, under a 10 s window.
                # Same rule, same expectations; only the fixture now describes
                # a situation that can actually occur.
                conn.execute(
                    "UPDATE executions SET started_at = started_at - 600, "
                    "heartbeat_at = heartbeat_at - 500 WHERE id = ?",
                    (record.id,),
                )
                conn.commit()
                assert ex.has_recorded_heartbeat(ex.get_execution(conn, record.id))
                result = ex.reconcile(conn, policy=stale_policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.stale
            assert refreshed.status == ex.STATUS_STALE
            assert "stale_heartbeat" in refreshed.termination_reason
        finally:
            _reap(proc.pid)

    def test_reconcile_never_advances_the_heartbeat(
        self, kanban_home, workroot, policy
    ):
        """Otherwise abandoned work looks freshest exactly when it is worst."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                before = ex.get_execution(conn, record.id).heartbeat_at
                ex.reconcile(conn, policy=policy)
                after = ex.get_execution(conn, record.id)
            assert after.heartbeat_at == before
            assert after.last_observed_at is not None
            # An explicit heartbeat DOES advance it — that is the difference
            # between "someone is watching" and "the reconciler ran".
            with kb.connect_closing() as conn:
                assert ex.heartbeat(conn, record.id, now=before + 50)
                assert ex.get_execution(conn, record.id).heartbeat_at == before + 50
        finally:
            _reap(proc.pid)

    def test_a_record_that_never_attached_a_process_is_settled(
        self, kanban_home, workroot, policy
    ):
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), controller_pid=999_999,
                controller_key="linux:1:(gone)",
                ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=60,
            )
            result = ex.reconcile(conn, policy=policy)
            refreshed = ex.get_execution(conn, record.id)
        assert record.id in result.stale
        assert refreshed.termination_reason == "never_started"

    def test_a_healthy_owned_execution_is_left_alone(
        self, kanban_home, workroot, policy
    ):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=3600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                result = ex.reconcile(conn, policy=policy)
            assert record.id in result.untouched
            assert ex.process_identity(proc.pid) is not None
        finally:
            _reap(proc.pid)


# ---------------------------------------------------------------------------
# 6. PID reuse
# ---------------------------------------------------------------------------


class TestPidReuse:
    def test_a_recycled_pid_is_not_mistaken_for_the_original_executor(
        self, kanban_home, workroot, policy
    ):
        """A live, unrelated process must never be adopted OR signalled.

        The recorded fingerprint is deliberately wrong for the live process at
        this pid — exactly the state PID reuse produces. The reconciler must
        classify the execution as gone and leave the innocent process running.
        """
        innocent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                )
                ex._attach_process(
                    conn, record.id, pid=innocent.pid,
                    pgid=os.getpgid(innocent.pid),
                    proc_key="linux:1:(a-different-process)",
                )
                result = ex.reconcile(conn, policy=policy)
                refreshed = ex.get_execution(conn, record.id)
            assert record.id in result.stale
            assert refreshed.termination_reason == "pid_reused"
            assert ex.process_identity(innocent.pid) is not None, (
                "reconciliation killed an unrelated process that had inherited "
                "the pid"
            )
        finally:
            _reap(innocent.pid)

    def test_terminate_refuses_to_signal_a_recycled_pid(
        self, kanban_home, workroot
    ):
        innocent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            outcome = ex.terminate_process_group(
                pid=innocent.pid,
                pgid=os.getpgid(innocent.pid),
                proc_key="linux:1:(someone-else)",
                grace_seconds=1,
            )
            assert outcome.signalled is False
            assert "pid reused" in outcome.detail
            assert ex.process_identity(innocent.pid) is not None
        finally:
            _reap(innocent.pid)

    def test_identity_is_refused_when_it_cannot_be_established(self):
        """No fingerprint means no match, so nothing gets signalled on a guess."""
        assert ex.identity_matches(os.getpid(), None) is False
        assert ex.identity_matches(None, "linux:1:(x)") is False
        assert ex.identity_matches(os.getpid(), ex.process_identity(os.getpid()))


def test_linux_process_identity_allows_exec_comm_change_but_not_starttime_change(monkeypatch):
    recorded = "linux:12345:python3"
    monkeypatch.setattr(ex, "process_identity", lambda pid: "linux:12345:node")
    assert ex.identity_matches(999, recorded) is True
    monkeypatch.setattr(ex, "process_identity", lambda pid: "linux:12346:node")
    assert ex.identity_matches(999, recorded) is False


def test_reconcile_does_not_mark_same_starttime_exec_as_pid_reused(monkeypatch, tmp_path):
    assert ex._same_process_key("linux:777:node", "linux:777:python3") is True
    assert ex._same_process_key("linux:778:node", "linux:777:python3") is False


# ---------------------------------------------------------------------------
# 7. Policy
# ---------------------------------------------------------------------------


def test_default_allowed_roots_include_canonical_project_repo_root(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    roots = ex.default_allowed_roots()
    assert str(tmp_path / "hermes" / "repos") in roots
    assert str(tmp_path) not in roots


class TestPolicy:
    def test_unauthorized_working_root_is_rejected(self, kanban_home, tmp_path, policy):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex.run_supervised(
                command_class="shell.argv", spec=_py("pass"),
                cwd=str(outside), policy=policy,
            )
        assert exc.value.code == "cwd_outside_allowed_roots"

    def test_a_symlink_cannot_smuggle_a_path_out_of_an_allowed_root(
        self, kanban_home, workroot, tmp_path, policy
    ):
        """The check runs on the real destination, not the name."""
        outside = tmp_path / "outside"
        outside.mkdir()
        link = workroot / "escape"
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            policy.resolve_root(str(link))
        assert exc.value.code == "cwd_outside_allowed_roots"

    def test_unauthorized_executor_type_is_rejected(self, kanban_home, workroot):
        narrow = ex.ExecutionPolicy(
            allowed_executors=("shell",), allowed_roots=(str(workroot),)
        )
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex.run_supervised(
                command_class="claude.headless", spec={"prompt": "hi"},
                cwd=str(workroot), policy=narrow,
            )
        assert exc.value.code == "executor_not_allowed"

    def test_an_unregistered_command_class_has_no_way_in(
        self, kanban_home, workroot, policy
    ):
        """There is no generic 'run this argv' endpoint."""
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex.run_supervised(
                command_class="totally.made.up", spec={"argv": ["/bin/true"]},
                cwd=str(workroot), policy=policy,
            )
        assert exc.value.code == "command_class_unregistered"

    def test_command_class_allowlist_narrows_further(self, kanban_home, workroot):
        narrow = ex.ExecutionPolicy(
            allowed_executors=("shell",),
            allowed_roots=(str(workroot),),
            allowed_command_classes=("shell.gate",),
        )
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            narrow.check_command_class("shell.argv")
        assert exc.value.code == "command_class_not_allowed"
        narrow.check_command_class("shell.gate")  # permitted

    def test_sudo_eligible_launchers_are_off_unless_enabled(
        self, kanban_home, workroot, monkeypatch
    ):
        launcher = ex.Launcher(
            "shell.sudo_probe", "shell",
            lambda spec: ["/bin/true"], requires_sudo=True,
        )
        monkeypatch.setitem(ex.LAUNCHERS, launcher.name, launcher)
        denied = ex.ExecutionPolicy(
            allowed_executors=("shell",), allowed_roots=(str(workroot),)
        )
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            denied.check_command_class("shell.sudo_probe")
        assert exc.value.code == "sudo_not_permitted"
        allowed = ex.ExecutionPolicy(
            allowed_executors=("shell",),
            allowed_roots=(str(workroot),),
            allow_sudo=True,
        )
        allowed.check_command_class("shell.sudo_probe")

    def test_an_empty_root_list_denies_rather_than_permits(self, kanban_home, workroot):
        """Deny-by-default has to survive a missing config, not invert."""
        empty = ex.ExecutionPolicy(allowed_executors=("shell",), allowed_roots=())
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            empty.resolve_root(str(workroot))
        assert exc.value.code == "cwd_outside_allowed_roots"

    def test_a_registered_allowed_execution_succeeds(
        self, kanban_home, workroot, policy
    ):
        result = ex.run_supervised(
            command_class="shell.argv", spec=_py("print('ok')"),
            cwd=str(workroot), policy=policy,
        )
        assert result.succeeded
        assert "ok" in result.stdout

    def test_a_gate_command_may_not_contain_shell_operators(self):
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex.gate_argv("pytest -q && rm -rf /")
        assert exc.value.code == "shell_operators_forbidden"
        assert ex.gate_argv("pytest -q tests/foo.py") == [
            "pytest", "-q", "tests/foo.py"
        ]


# ---------------------------------------------------------------------------
# 8. Secrets
# ---------------------------------------------------------------------------


class TestSecrets:
    def test_a_prompt_is_never_stored_in_the_execution_record(
        self, kanban_home, workroot, policy, monkeypatch
    ):
        """The record names the launcher, never the argv.

        A prompt is the most likely place a credential travels, so this walks
        the whole row and both audit tables rather than checking one field.
        """
        secret = "sk-ant-do-not-persist-me-0123456789"
        # Stand in for the real claude launcher, keeping its NAME and executor
        # type: the assertion is about what the record stores for a launch
        # whose argv carried a credential, not about running Claude.
        monkeypatch.setitem(
            ex.LAUNCHERS, "claude.headless",
            ex.Launcher(
                "claude.headless", "claude",
                lambda spec: [sys.executable, "-c", "pass"],
            ),
        )
        result = ex.run_supervised(
            command_class="claude.headless",
            spec={"prompt": f"here is a token: {secret}"},
            cwd=str(workroot),
            policy=policy,
        )
        with kb.connect_closing() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE id = ?", (result.execution_id,)
            ).fetchone()
            blob = json.dumps({k: row[k] for k in row.keys()})
            events = json.dumps(ex.execution_events(conn, result.execution_id))
        assert secret not in blob
        assert secret not in events
        assert row["command_class"] == "claude.headless"
        # Stronger than a substring scan (which matched pytest's own tmp_path,
        # named after this test): the record's columns are a CLOSED set with no
        # field that carries argv, an environment, or a prompt. A future column
        # that could hold one has to fail here first.
        assert set(row.keys()) == {
            "id", "task_id", "executor_type", "command_class", "cwd",
            "pid", "pgid", "proc_key", "nonce", "controller_pid",
            "controller_key", "controller_token", "ownership",
            "max_runtime_s", "started_at", "heartbeat_at", "last_observed_at",
            "ended_at", "status", "exit_code", "termination_reason",
            "rollback_ref", "route_task", "created_at",
        }

    def test_a_credential_shaped_field_is_refused_at_write_time(
        self, kanban_home, workroot
    ):
        with pytest.raises(ex.ExecutionPolicyError) as exc:
            ex._assert_record_is_secret_free(
                {"rollback_ref": "authorization: Bearer abc123"}
            )
        assert exc.value.code == "secret_in_record"

    def test_the_operator_surface_prints_no_environment(
        self, kanban_home, workroot, policy
    ):
        result = ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), policy=policy,
        )
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
            described = ex.describe(conn, record)
        assert "env" not in described
        assert "argv" not in described
        assert "environment" not in json.dumps(described).lower()


# ---------------------------------------------------------------------------
# 9. Gauntlet integration
# ---------------------------------------------------------------------------


def _gauntlet_task(conn, *, tenant="acme", title="supervised work") -> str:
    """Create a Gauntlet-enforced card and drive it to EXECUTING."""
    tid = kb.create_task(
        conn, title=title, assignee="default", tenant=tenant, gauntlet=True,
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    return tid


class TestGauntletIntegration:
    def test_a_clean_executor_exit_does_not_complete_the_task(
        self, kanban_home, workroot, policy
    ):
        """The single most important assertion in this file.

        An executor exiting 0 is a claim, not a verdict. It hands the card to
        VERIFICATION_PENDING; only a verifier's PASS can complete it.
        """
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, policy=policy,
        )
        assert result.succeeded
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
        assert task.status != "done", "executor exit completed a Gauntlet card"
        assert task.status == "review"
        assert task.verification_state == kb.VERIFICATION_PENDING

    def test_execution_completion_routes_into_verification(
        self, kanban_home, workroot, policy
    ):
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, policy=policy,
        )
        with kb.connect_closing() as conn:
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                    (tid,),
                )
            ]
            task = kb.get_task(conn, tid)
        assert ex.EVENT_EXECUTION_FINISHED in kinds
        assert "execution_created" in kinds
        assert task.verification_state == kb.VERIFICATION_PENDING
        assert result.execution_id

    def test_executor_failure_routes_into_recovery_not_verification(
        self, kanban_home, workroot, policy
    ):
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        ex.run_supervised(
            command_class="shell.argv", spec=_py("import sys; sys.exit(1)"),
            cwd=str(workroot), task_id=tid, policy=policy,
        )
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            kinds = [
                r["kind"] for r in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ?", (tid,)
                )
            ]
        assert ex.EVENT_EXECUTION_FAILED in kinds
        assert task.status != "done"
        assert task.verification_state != kb.VERIFICATION_VERIFIED
        assert task.consecutive_failures >= 1

    def test_a_lost_controller_is_never_treated_as_success(
        self, kanban_home, workroot, policy
    ):
        """A timeout or a lost controller cannot be laundered into progress."""
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
            record = ex.create_execution(
                conn, task_id=tid, executor_type="shell",
                command_class="shell.argv", cwd=str(workroot),
                controller_pid=999_999, controller_key="linux:1:(gone)",
                ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=60,
            )
            ex._attach_process(
                conn, record.id, pid=999_998, pgid=999_998,
                proc_key="linux:1:(gone-too)",
            )
            ex.reconcile(conn, policy=policy)
            task = kb.get_task(conn, tid)
            refreshed = ex.get_execution(conn, record.id)
        assert refreshed.status == ex.STATUS_CONTROLLER_LOST
        assert task.status != "done"
        assert task.verification_state != kb.VERIFICATION_VERIFIED

    def test_route_task_false_leaves_the_handoff_to_the_caller(
        self, kanban_home, workroot, policy
    ):
        """The recovery lane's arrangement: it gates, then hands off itself."""
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, route_task=False, policy=policy,
        )
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, tid)
            record = ex.list_executions(conn, task_id=tid)[0]
        # Still recorded against the card — the operator surface must be able
        # to answer "what task does this belong to".
        assert record.task_id == tid
        assert record.route_task is False
        # ...but the card was not moved by the supervisor.
        assert task.status == "running"
        assert task.status != "done"

    def test_a_material_repair_still_needs_regression_evidence(
        self, kanban_home, workroot, policy
    ):
        """The supervisor changes who owns the process, not the chain."""
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, policy=policy,
        )
        with kb.connect_closing() as conn:
            kb.record_verification(
                conn, tid, passed=False, verifier="reviewer",
                reason="did not actually fix it",
            )
            assert kb.get_task(conn, tid).regression_required is True

            # Repair leg: re-run the work under supervision, hand it back.
            repaired = kb.claim_task(conn, tid)
            assert repaired is not None
            assert repaired.regression_required is True

        ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, policy=policy,
        )

        with kb.connect_closing() as conn:
            # A PASS with no regression proof is refused, exactly as it is
            # without the supervisor in the picture. The supervisor changes
            # WHO owns the process, never what the chain demands.
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"note": "looks fine now"},
            )
            assert ok is False
            assert "regression evidence is required" in detail
            task = kb.get_task(conn, tid)
            assert task.regression_required is True
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="shipping anyway")


# ---------------------------------------------------------------------------
# 10. Operator surface
# ---------------------------------------------------------------------------


class TestOperatorSurface:
    def test_describe_answers_the_operator_questions(
        self, kanban_home, workroot, policy
    ):
        with kb.connect_closing() as conn:
            tid = _gauntlet_task(conn)
        result = ex.run_supervised(
            command_class="shell.argv", spec=_py("pass"),
            cwd=str(workroot), task_id=tid, route_task=False, policy=policy,
        )
        with kb.connect_closing() as conn:
            record = ex.get_execution(conn, result.execution_id)
            data = ex.describe(conn, record)
        for key in (
            "status", "ownership", "task_id", "runtime_seconds",
            "seconds_since_heartbeat", "controller_alive", "within_policy",
            "exit_code", "executor_type",
        ):
            assert key in data, f"operator surface cannot answer {key!r}"
        assert data["task_id"] == tid

    def test_terminate_execution_settles_the_record(
        self, kanban_home, workroot, policy
    ):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            cwd=str(workroot), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with kb.connect_closing() as conn:
                record = ex.create_execution(
                    conn, executor_type="shell", command_class="shell.argv",
                    cwd=str(workroot),
                    controller_pid=os.getpid(),
                    controller_key=ex.process_identity(os.getpid()),
                    ownership=ex.OWNERSHIP_SUPERVISOR, max_runtime_s=3600,
                )
                ex._attach_process(
                    conn, record.id, pid=proc.pid, pgid=os.getpgid(proc.pid),
                    proc_key=ex.process_identity(proc.pid),
                )
                settled = ex.terminate_execution(
                    conn, record.id, reason="operator_test", policy=policy,
                )
            assert settled.status == ex.STATUS_TERMINATED
            assert "operator_test" in settled.termination_reason
            assert _wait_gone(proc.pid)
        finally:
            _reap(proc.pid)

    def test_terminate_unknown_execution_raises(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(ex.ExecutionNotFound):
                ex.terminate_execution(conn, "x_nope")

    def test_settling_is_a_compare_and_swap(self, kanban_home, workroot):
        """Two racing settlers cannot both write a terminal state."""
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), ownership=ex.OWNERSHIP_SUPERVISOR,
            )
            assert ex._settle(conn, record.id, status=ex.STATUS_COMPLETED, exit_code=0)
            assert not ex._settle(
                conn, record.id, status=ex.STATUS_FAILED, exit_code=9
            )
            refreshed = ex.get_execution(conn, record.id)
        assert refreshed.status == ex.STATUS_COMPLETED
        assert refreshed.exit_code == 0


# ---------------------------------------------------------------------------
# 11. Dispatcher integration
# ---------------------------------------------------------------------------


class TestDispatchIntegration:
    def test_the_tick_reconciles_executions(self, kanban_home, workroot):
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), controller_pid=999_999,
                controller_key="linux:1:(gone)",
                ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=60,
            )
            result = kb.dispatch_once(conn, max_spawn=0)
            refreshed = ex.get_execution(conn, record.id)
        assert result.executions_reconciled
        assert result.executions_reconciled["checked"] == 1
        assert refreshed.is_terminal

    def test_supervisor_refusal_events_do_not_count_as_freshness_progress(self):
        """An alert that recurs while nothing happens is not progress."""
        for kind in ("execution_refused", "execution_reconcile_failed"):
            assert kind in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS
        # Settling events genuinely mean something happened to the card.
        for kind in (ex.EVENT_EXECUTION_FINISHED, ex.EVENT_EXECUTION_FAILED):
            assert kind not in kb._GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS


# ---------------------------------------------------------------------------
# 12. Migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_a_board_without_the_tables_gains_them(self, kanban_home):
        with kb.connect_closing() as conn:
            conn.execute("DROP TABLE executions")
            conn.execute("DROP TABLE execution_events")
            conn.commit()
        kb.init_db()
        with kb.connect_closing() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(executions)")
            }
            assert {
                "id", "task_id", "executor_type", "command_class", "cwd",
                "pid", "pgid", "proc_key", "nonce", "controller_pid",
                "controller_key", "controller_token", "ownership",
                "max_runtime_s", "started_at", "heartbeat_at",
                "last_observed_at", "ended_at", "status", "exit_code",
                "termination_reason", "rollback_ref", "route_task",
                "created_at",
            } <= cols
            assert ex.list_executions(conn) == []

    def test_migration_is_idempotent(self, kanban_home, workroot):
        with kb.connect_closing() as conn:
            record = ex.create_execution(
                conn, executor_type="shell", command_class="shell.argv",
                cwd=str(workroot), ownership=ex.OWNERSHIP_SUPERVISOR,
            )
        kb.init_db()
        kb.init_db()
        with kb.connect_closing() as conn:
            assert ex.get_execution(conn, record.id) is not None
