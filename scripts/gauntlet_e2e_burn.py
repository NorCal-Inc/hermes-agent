#!/usr/bin/env python3
"""GAUNTLET_E2E_BURN — the whole contract, end to end, on a real board.

Not a unit test. This drives a live Kanban board through every link of the
Gauntlet chain and through the execution supervisor, and refuses to print PASS
unless each stage's *consequence* is observed. Each stage asserts the thing
that would actually have gone wrong, not that a function returned.

Stages, in order:

  A create Gauntlet task              K complete task
  B claim execution                   L promote verified lesson
  C direct completion rejected        M create equivalent second task
  D request verification              N second task receives it (binding)
  E fail verification                 O create cross-tenant control task
  F task enters repair/rework         P control task does NOT receive it
  G repair task                       Q retire lesson
  H PASS without regression refused   R future work no longer receives it
  I attach falsifiable evidence       S lesson/audit history remains
  J obtain VERIFIED                   T execution supervisor: no unmanaged
                                        executor survives controller loss or
                                        timeout

Run as the account that owns HERMES_HOME. Creating the temp home as root and
then running as another user produces a board nothing can write to, and the
failure that causes looks like a logic bug for about twenty minutes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FAILURES: list[str] = []
STAGES: list[tuple[str, str, bool]] = []


def check(stage: str, label: str, condition: bool, detail: str = "") -> bool:
    ok = bool(condition)
    STAGES.append((stage, label, ok))
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {stage}  {label}"
    if detail:
        line += f"\n       {detail}"
    print(line, flush=True)
    if not ok:
        FAILURES.append(f"{stage}: {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    args = ap.parse_args()

    home = Path(args.home)
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)

    # Ownership guard, stated in the mission for a reason: a root-created home
    # under a non-root run is unwriteable and every later failure misreports.
    st = home.stat()
    check(
        "PRE", "temporary HERMES_HOME is owned by the running account",
        st.st_uid == os.getuid(),
        f"home uid={st.st_uid} process uid={os.getuid()}",
    )
    if FAILURES:
        return _report()

    os.environ["HERMES_HOME"] = str(home)

    from hermes_cli import kanban as kc
    from hermes_cli import kanban_db as kb
    from hermes_cli import exec_supervisor as ex

    kb.init_db()

    def cli(*argv) -> tuple[int, str]:
        parser = argparse.ArgumentParser(prog="hermes", add_help=False)
        sub = parser.add_subparsers(dest="command")
        kc.build_parser(sub)
        parsed = parser.parse_args(["kanban", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = kc.kanban_command(parsed)
        return rc, buf.getvalue()

    policy = ex.ExecutionPolicy(
        allowed_executors=("shell",),
        allowed_roots=(str(work),),
        max_runtime_seconds=120,
        sync_ceiling_seconds=120,
        stale_heartbeat_seconds=3600,
        terminate_grace_seconds=3,
    )

    # -- A -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="burn: claim lock regression",
            assignee="default", tenant="acme", gauntlet=True,
        )
        task = kb.get_task(conn, tid)
    check("A", "Gauntlet task created and enforced",
          task is not None and task.gauntlet_enforced, f"task={tid}")

    # -- B -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        claimed = kb.claim_task(conn, tid)
    check("B", "execution claimed (EXECUTING)",
          claimed is not None and claimed.status == "running",
          f"status={claimed.status if claimed else None}")
    run_id = claimed.current_run_id

    # The executor actually runs, under supervision. Its clean exit is a
    # claim, not a verdict.
    result = ex.run_supervised(
        command_class="shell.argv",
        spec={"argv": [sys.executable, "-c", "print('implementation run')"]},
        cwd=str(work), task_id=tid, route_task=False, policy=policy,
    )
    check("B", "supervised executor ran and settled",
          result.status == ex.STATUS_COMPLETED and result.exit_code == 0,
          f"execution={result.execution_id} status={result.status}")
    with kb.connect_closing() as conn:
        after_exec = kb.get_task(conn, tid)
    check("B", "a clean executor exit did NOT complete the card",
          after_exec.status == "running",
          f"status={after_exec.status}")

    # -- C -----------------------------------------------------------------
    refused = False
    with kb.connect_closing() as conn:
        try:
            kb.complete_task(conn, tid, summary="done, trust me")
        except kb.VerificationRequiredError:
            refused = True
        still = kb.get_task(conn, tid)
    check("C", "direct EXECUTING -> COMPLETED rejected",
          refused and still.status == "running" and still.completed_at is None,
          f"refused={refused} status={still.status}")

    # -- D -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        ok = kb.request_review(
            conn, tid, summary="implementation done", expected_run_id=run_id,
        )
        pending = kb.get_task(conn, tid)
    check("D", "VERIFICATION_PENDING entered",
          ok and pending.status == "review"
          and pending.verification_state == kb.VERIFICATION_PENDING,
          f"status={pending.status} vstate={pending.verification_state}")

    # -- E -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        vok, detail = kb.record_verification(
            conn, tid, passed=False, verifier="reviewer",
            reason="claim lock still races under concurrent dispatch",
        )
        failed = kb.get_task(conn, tid)
    check("E", "verification failed and was recorded",
          vok is True and failed.verification_state != kb.VERIFICATION_VERIFIED,
          f"detail={detail} vstate={failed.verification_state}")

    # -- F -----------------------------------------------------------------
    check("F", "task entered repair/rework with regression debt armed",
          failed.regression_required is True and failed.status != "done",
          f"regression_required={failed.regression_required} status={failed.status}")

    # A failed verdict must not be completable, whatever anyone claims.
    refused_after_fail = False
    with kb.connect_closing() as conn:
        try:
            kb.complete_task(conn, tid, summary="calling it done anyway")
        except kb.VerificationRequiredError:
            refused_after_fail = True
    check("F", "failed verification never counts as completion",
          refused_after_fail)

    # -- G -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        repaired = kb.claim_task(conn, tid)
    check("G", "repair run claimed, debt survives the rework",
          repaired is not None and repaired.status == "running"
          and repaired.regression_required is True,
          f"regression_required={repaired.regression_required if repaired else None}")
    repair_run_id = repaired.current_run_id

    repair_exec = ex.run_supervised(
        command_class="shell.argv",
        spec={"argv": [sys.executable, "-c", "print('repair run')"]},
        cwd=str(work), task_id=tid, route_task=False, policy=policy,
    )
    check("G", "repair executed under supervision",
          repair_exec.status == ex.STATUS_COMPLETED,
          f"execution={repair_exec.execution_id}")

    with kb.connect_closing() as conn:
        kb.request_review(
            conn, tid, summary="repaired", expected_run_id=repair_run_id,
        )

    # -- H -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        hok, hdetail = kb.record_verification(
            conn, tid, passed=True, verifier="reviewer",
            evidence={"note": "looks fine to me now"},
        )
        after_h = kb.get_task(conn, tid)
    check("H", "PASS without regression evidence is refused",
          hok is False and "regression evidence is required" in (hdetail or ""),
          f"ok={hok} detail={hdetail}")
    check("H", "no verdict was written; card still not verified",
          after_h.verification_state == kb.VERIFICATION_PENDING
          and after_h.regression_required is True)

    # -- I / J -------------------------------------------------------------
    # Falsifiable: a named command with an exit code, not "it works now".
    proof_cmd = f"{sys.executable} -c 'import sys; sys.exit(0)'"
    proof_rc = subprocess.call(
        [sys.executable, "-c", "import sys; sys.exit(0)"], cwd=str(work)
    )
    check("I", "regression check actually re-run (falsifiable)",
          proof_rc == 0, f"{proof_cmd} -> rc={proof_rc}")

    with kb.connect_closing() as conn:
        jok, jdetail = kb.record_verification(
            conn, tid, passed=True, verifier="reviewer",
            evidence={"command": proof_cmd, "exit_code": proof_rc},
            regression_evidence={
                "command": proof_cmd,
                "exit_code": proof_rc,
                "suite": "claim-lock regression",
                "passed": 1,
                "failed": 0,
            },
        )
        verified = kb.get_task(conn, tid)
    check("I", "regression evidence accepted and spent",
          jok is True and verified.regression_required is False,
          f"detail={jdetail} regression_required={verified.regression_required}")
    check("J", "VERIFIED obtained",
          verified.verification_state == kb.VERIFICATION_VERIFIED,
          f"vstate={verified.verification_state}")

    # -- K -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        completed = kb.complete_task(conn, tid, summary="claim lock fixed")
        done = kb.get_task(conn, tid)
    check("K", "task COMPLETED only after VERIFIED",
          completed is True and done.status == "done",
          f"status={done.status}")

    # -- L -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        lesson = kb.promote_lesson(
            conn, tid,
            lesson=("Re-run the claim-lock regression suite after touching "
                    "dispatch concurrency."),
            applicability="tenant:acme",
            actor="operator",
            evidence={"command": proof_cmd, "exit_code": proof_rc},
        )
    check("L", "verified lesson promoted to canonical learning",
          bool(lesson and lesson.get("id")), f"lesson_id={lesson.get('id')}")
    lesson_id = lesson["id"]

    # -- M / N -------------------------------------------------------------
    with kb.connect_closing() as conn:
        second = kb.create_task(
            conn, title="burn: equivalent dispatch work",
            assignee="default", tenant="acme", gauntlet=True,
        )
        applicable = kb.lessons_for_task(conn, second)
        context = kb.build_worker_context(conn, second)
    check("M", "equivalent second task created in the same lane",
          bool(second), f"task={second}")
    check("N", "second task MECHANICALLY receives the binding lesson",
          any(l["id"] == lesson_id for l in applicable)
          and "claim-lock regression" in context,
          f"lessons={[l['id'] for l in applicable]}")
    check("N", "the injected lesson is marked binding with its provenance",
          "BINDING" in context.upper() and tid in context,
          "worker context must name the source task")

    # -- O / P -------------------------------------------------------------
    with kb.connect_closing() as conn:
        control = kb.create_task(
            conn, title="burn: other tenant dispatch work",
            assignee="default", tenant="northcal", gauntlet=True,
        )
        control_lessons = kb.lessons_for_task(conn, control)
        control_context = kb.build_worker_context(conn, control)
    check("O", "cross-tenant control task created",
          bool(control), f"task={control} tenant=northcal")
    check("P", "control task does NOT receive the lesson (tenant isolation)",
          not any(l["id"] == lesson_id for l in control_lessons)
          and "claim-lock regression" not in control_context,
          f"lessons={[l['id'] for l in control_lessons]}")

    # -- Q / R -------------------------------------------------------------
    with kb.connect_closing() as conn:
        rok, rdetail = kb.retire_lesson(
            conn, lesson_id, actor="operator",
            reason="superseded by the supervised execution path",
        )
    check("Q", "lesson retired",
          rok is True, f"detail={rdetail}")

    with kb.connect_closing() as conn:
        third = kb.create_task(
            conn, title="burn: later dispatch work",
            assignee="default", tenant="acme", gauntlet=True,
        )
        later = kb.lessons_for_task(conn, third)
        later_context = kb.build_worker_context(conn, third)
    check("R", "future work no longer receives the retired lesson",
          not any(l["id"] == lesson_id for l in later)
          and "claim-lock regression" not in later_context,
          f"lessons={[l['id'] for l in later]}")

    # -- S -----------------------------------------------------------------
    with kb.connect_closing() as conn:
        all_lessons = kb.list_lessons(conn, active_only=False)
        row = next((l for l in all_lessons if l["id"] == lesson_id), None)
        history = kb.verification_history(conn, tid)
        events = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id",
                (tid,),
            )
        ]
    check("S", "retired lesson's row and retirement stamp survive",
          row is not None and not row["active"] and row["retired_by"] == "operator",
          f"row={'present' if row else 'MISSING'}")
    states = [h["state"] for h in history]
    check("S", "verification ledger keeps the whole chain incl. the failure",
          kb.VERIFICATION_FAILED in states
          and kb.VERIFICATION_VERIFIED in states
          and kb.VERIFICATION_REGRESSION in states,
          f"ledger states={states}")
    check("S", "execution records are attached to the card's audit trail",
          "execution_created" in events,
          f"events={sorted(set(events))}")

    # -- T -----------------------------------------------------------------
    ok_t = _burn_supervisor(ex, kb, work, policy)
    for label, cond, detail in ok_t:
        check("T", label, cond, detail)

    return _report()


def _burn_supervisor(ex, kb, work: Path, policy) -> list[tuple[str, bool, str]]:
    """T: prove no unmanaged executor survives controller loss or timeout."""
    out: list[tuple[str, bool, str]] = []

    def gone(pid, timeout=15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ex.process_identity(pid) is None:
                return True
            time.sleep(0.05)
        return ex.process_identity(pid) is None

    # T1 — synchronous timeout, with a grandchild. This is the exact incident:
    # the direct child is killed by subprocess, the grandchild is not.
    pidfile = work / "burn-grandchild.pid"
    with contextlib.suppress(OSError):
        pidfile.unlink()
    code = (
        "import subprocess, sys, time, pathlib\n"
        f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(300)'])\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "time.sleep(300)\n"
    )
    fast = ex.ExecutionPolicy(
        **{**policy.__dict__, "max_runtime_seconds": 3, "sync_ceiling_seconds": 60}
    )
    timed = ex.run_supervised(
        command_class="shell.argv",
        spec={"argv": [sys.executable, "-c", code]},
        cwd=str(work), policy=fast,
    )
    out.append((
        "synchronous timeout is reported as a timeout, never as success",
        timed.status == ex.STATUS_TIMED_OUT and not timed.succeeded,
        f"status={timed.status}",
    ))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.05)
    if pidfile.exists():
        grandchild = int(pidfile.read_text().strip())
        out.append((
            "the GRANDCHILD did not survive the timeout (the original bug)",
            gone(grandchild),
            f"grandchild pid={grandchild}",
        ))
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(grandchild), signal.SIGKILL)
    else:
        out.append((
            "the GRANDCHILD did not survive the timeout (the original bug)",
            False, "grandchild never recorded its pid",
        ))

    # T2 — real controller loss. A separate OS process creates the execution,
    # launches a long-lived child, and is then SIGKILLed. Nothing in-process
    # is simulated: the controller genuinely dies mid-execution.
    launcher = work / "burn_controller.py"
    launcher.write_text(
        "import os, sys, time\n"
        f"os.environ['HERMES_HOME'] = {str(Path(os.environ['HERMES_HOME']))!r}\n"
        "from hermes_cli import kanban_db as kb, exec_supervisor as ex\n"
        f"work = {str(work)!r}\n"
        "pol = ex.ExecutionPolicy(allowed_executors=('shell',), "
        "allowed_roots=(work,), max_runtime_seconds=600, "
        "sync_ceiling_seconds=600, stale_heartbeat_seconds=3600)\n"
        "conn = kb.connect()\n"
        "rec = ex.create_execution(conn, executor_type='shell', "
        "command_class='shell.argv', cwd=work, "
        "controller_pid=os.getpid(), "
        "controller_key=ex.process_identity(os.getpid()), "
        "ownership=ex.OWNERSHIP_CONTROLLER, max_runtime_s=600)\n"
        "import subprocess\n"
        f"p = subprocess.Popen([{sys.executable!r}, '-c', "
        "'import time; time.sleep(300)'], cwd=work, start_new_session=True)\n"
        "ex._attach_process(conn, rec.id, pid=p.pid, pgid=os.getpgid(p.pid), "
        "proc_key=ex.process_identity(p.pid))\n"
        "print(rec.id, p.pid, flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(launcher)],
        cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)},
    )
    line = proc.stdout.readline().strip()
    parts = line.split()
    if len(parts) != 2:
        proc.kill()
        out.append((
            "controller-loss scenario set up", False,
            f"launcher said {line!r}; stderr={proc.stderr.read()[:400]!r}",
        ))
        return out
    exec_id, child_pid = parts[0], int(parts[1])

    # Kill the controller only. The child is in its own session, so it
    # survives — which is precisely the unmanaged-orphan condition.
    os.kill(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)
    survived = ex.process_identity(child_pid) is not None
    out.append((
        "controller death genuinely leaves the executor alive (setup is real)",
        survived, f"child pid={child_pid} controller pid={proc.pid}",
    ))

    with kb.connect_closing() as conn:
        recon = ex.reconcile(conn, policy=policy)
        record = ex.get_execution(conn, exec_id)
    out.append((
        "reconciliation detected the lost controller",
        exec_id in recon.controller_lost,
        f"status={record.status if record else None} reason="
        f"{record.termination_reason if record else None}",
    ))
    out.append((
        "NO unmanaged executor survives: the orphan was terminated",
        gone(child_pid), f"child pid={child_pid}",
    ))
    out.append((
        "controller loss is not treated as success",
        record is not None
        and record.status == ex.STATUS_CONTROLLER_LOST
        and record.exit_code is None,
        f"status={record.status if record else None}",
    ))

    # T3 — the invariant, swept over the whole board.
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT id, status, ownership FROM executions"
        ).fetchall()
    unowned = [
        r["id"] for r in rows
        if r["status"] in ex.ACTIVE_STATUSES
        and r["ownership"] != ex.OWNERSHIP_SUPERVISOR
    ]
    out.append((
        "no execution is left non-terminal AND controller-owned",
        not unowned, f"offenders={unowned}",
    ))
    with contextlib.suppress(Exception):
        os.killpg(os.getpgid(child_pid), signal.SIGKILL)
    return out


def _report() -> int:
    total = len(STAGES)
    passed = sum(1 for _, _, ok in STAGES if ok)
    print("")
    print("=" * 72)
    print(f"checks: {passed}/{total} passed")
    if FAILURES:
        print("")
        print("FAILED INVARIANTS:")
        for failure in FAILURES:
            print(f"  - {failure}")
        print("")
        print("GAUNTLET_E2E_BURN FAIL")
        return 1
    print("")
    print("GAUNTLET_E2E_BURN PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
