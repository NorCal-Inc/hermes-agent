"""Isolated replay of t_b8d62378's final review lifecycle (F4, Christopher 2026-09-15).

Before any attempt grant, the exact run accounting of the real lifecycle is replayed on a
throwaway board with the deployed Kanban code. The history is first rebuilt and must reproduce
production exactly (exhausted at 6, +3 grant, Phase D handoff, verifier FAIL, automatic rework
run, hard stop at 9/9 — events 153466..153630). Then, from that exact state, every grant size is
tried against every verdict. ``pytest -s`` prints the lineage accounting after every transition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

OPERATOR = {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "christopher"}
REGRESSION_PASS = ("REGRESSION: pytest -q tests/hermes_cli/test_system_health_controller.py -> exit 0, 207 passed\n"
                   "ACCEPTANCE: PASS\nVERDICT: PASS")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (*kb._GOVERNED_RUN_ENV_MARKERS, "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


class Ledger:
    def __init__(self, label):
        self.label = label
        self.rows = []

    def mark(self, conn, tid, step):
        task = kb.get_task(conn, tid)
        row = {
            "step": step,
            "attempts": kb.gauntlet_objective_attempts(conn, tid),
            "limit": kb.effective_objective_attempt_limit(conn, tid),
            "status": task.status,
            "block": task.block_kind,
            "verification": task.verification_state,
            "regression_required": int(bool(task.regression_required)),
        }
        self.rows.append(row)
        print(f"[{self.label}] {step:<58} attempts={row['attempts']}/{row['limit']} status={row['status']:<8} "
              f"block={row['block']} verification={row['verification']} regression={row['regression_required']}")
        return row


def _spawned(conn):
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return None                       # no process: nothing can crash or be reaped

    kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=8)
    return spawned


def _verifier(conn, subject):
    return kb._open_verifier_child(conn, subject)


def _attest(conn, cid, run_id):
    with kb.write_txn(conn):
        kb._append_event(conn, cid, "codex_verifier_started", {"executor": "codex"}, run_id=run_id)


def _run_verifier(conn, subject, summary):
    cid = _verifier(conn, subject)
    claimed = kb.claim_task(conn, cid)
    if claimed is None:
        return cid, False
    _attest(conn, cid, claimed.current_run_id)
    assert kb.complete_task(conn, cid, summary=summary, expected_run_id=claimed.current_run_id) is True
    return cid, True


def _implementation_cycle(conn, tid, ledger, n):
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    ledger.mark(conn, tid, f"history {n}: implementation run claimed")
    kb.add_attachment(conn, tid, filename=f"EVIDENCE-{n}.md", stored_path=f"/tmp/{tid}/EVIDENCE-{n}.md",
                      size=512, uploaded_by="claude-code (operator relay for Christopher)")
    assert kb.request_review(conn, tid, summary=f"cycle {n}", expected_run_id=claimed.current_run_id,
                             metadata={"changed_files": ["norcal/health/system_health_controller.py"],
                                       "verification": "focused suite"}) is True
    ledger.mark(conn, tid, f"history {n}: review requested")
    _cid, ran = _run_verifier(conn, tid, "ACCEPTANCE: FAIL\nVERDICT: FAIL")
    assert ran
    ledger.mark(conn, tid, f"history {n}: verifier FAIL returned")


def _operator_handoff(conn, tid, ledger, label):
    kb.add_comment(conn, tid, "claude-code (operator relay for Christopher)", "evidence packet attached")
    assert kb.unblock_task(conn, tid)
    ledger.mark(conn, tid, f"{label}: operator unblock")
    kb.add_attachment(conn, tid, filename="PHASE-EVIDENCE-PACKET.md", stored_path=f"/tmp/{tid}/PHASE.md",
                      size=4096, uploaded_by="claude-code (operator relay for Christopher)")
    ok = kb.request_review(conn, tid, summary="implementation complete; evidence packet attached",
                           metadata={"changed_files": ["norcal/health/system_health_controller.py"],
                                     "verification": "full gate + focused suite + mutation + injection"})
    row = ledger.mark(conn, tid, f"{label}: operator handoff (request_review)")
    return ok, row


def replay_live_history(conn, ledger):
    """Rebuild production up to the 9/9 hard stop. Every figure must match the live board."""
    tid = kb.create_task(conn, title="Build recursive self-healing system health controller",
                         assignee="default", gauntlet=True)
    _implementation_cycle(conn, tid, ledger, 1)          # runs 2819-shape card + verifier (t_d7b74477)
    _implementation_cycle(conn, tid, ledger, 2)          # card + verifier (t_c0ebb2f6)
    with kb.write_txn(conn):
        for i in range(2):                               # the remaining timed-out/stale-released card runs
            conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                         "VALUES (?, 'default', 'timed_out', ?, ?, 'timed_out')", (tid, 100 + i, 101 + i))
    ledger.mark(conn, tid, "history: remaining timed-out card runs recorded")
    assert kb.claim_task(conn, tid) is None
    row = ledger.mark(conn, tid, "history: claim refused, objective exhausted")
    assert (row["attempts"], row["limit"], row["block"]) == (6, 6, kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED)
    kb.grant_objective_attempts(conn, tid, added_attempts=3, authorized_by="Christopher",
                                reason="replay of event 153466", env=OPERATOR)
    ledger.mark(conn, tid, "history: +3 grant (event 153466)")
    ok, row = _operator_handoff(conn, tid, ledger, "history Phase D")
    assert ok and row["attempts"] == 7
    _cid, ran = _run_verifier(conn, tid, "ACCEPTANCE: FAIL\nVERDICT: FAIL")
    assert ran
    ledger.mark(conn, tid, "history Phase D: verifier t_302f62c8 FAIL returned")
    spawned = _spawned(conn)
    ledger.mark(conn, tid, f"history Phase D: dispatcher tick spawned {spawned == [tid]}")
    assert spawned == [tid]                               # the automatic rework run 2829
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    kb.request_review(conn, tid, summary="I didn't change anything this run", expected_run_id=run)
    row = ledger.mark(conn, tid, "history: run 2829 review request -> hard stop")
    assert (row["attempts"], row["limit"], row["status"], row["block"]) == (
        9, 9, "blocked", kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED)
    return tid


def test_replay_reproduces_the_live_hard_stop(kanban_home):
    with kb.connect_closing() as conn:
        ledger = Ledger("history")
        tid = replay_live_history(conn, ledger)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))]
        assert kinds.count(kb.OBJECTIVE_ATTEMPT_GRANT_EVENT) == 1
        assert "objective_attempt_ceiling_reached" in kinds


VERDICTS = {
    "pass": REGRESSION_PASS,
    "fail": "ACCEPTANCE: FAIL\nVERDICT: FAIL",
    "refused_pass": "Looks complete.\n\nVERDICT: PASS",
}
OUTCOMES: dict = {}


@pytest.mark.parametrize("grant", [1, 2, 3, 4])
@pytest.mark.parametrize("verdict", list(VERDICTS))
def test_one_verification_cycle_per_grant(kanban_home, grant, verdict):
    label = f"grant+{grant}/{verdict}"
    with kb.connect_closing() as conn:
        tid = replay_live_history(conn, _QuietLedger())
        ledger = Ledger(label)
        start = ledger.mark(conn, tid, "start (live state)")
        kb.grant_objective_attempts(conn, tid, added_attempts=grant, authorized_by="Christopher",
                                    reason="replay sizing", env=OPERATOR)
        ledger.mark(conn, tid, f"grant +{grant}")
        ok, handoff = _operator_handoff(conn, tid, ledger, "cycle")
        child = _verifier(conn, tid)
        ledger.mark(conn, tid, f"verifier child created={child is not None}")
        outcome = {"handoff_accepted": bool(ok), "verifier_created": child is not None,
                   "verifier_ran": False, "subject_verified": False, "automatic_rework_run": False,
                   "second_verifier_claimable": None}
        if child is not None:
            _cid, ran = _run_verifier(conn, tid, VERDICTS[verdict])
            outcome["verifier_ran"] = ran
            ledger.mark(conn, tid, f"verifier claimed+completed={ran} ({verdict})")
            if ran:
                before = kb.gauntlet_objective_attempts(conn, tid)
                spawned = _spawned(conn)
                row = ledger.mark(conn, tid, f"dispatcher tick after verdict spawned={spawned}")
                outcome["automatic_rework_run"] = tid in spawned
                outcome["runs_spent_by_tick"] = row["attempts"] - before
                task = kb.get_task(conn, tid)
                outcome["subject_verified"] = task.verification_state == kb.VERIFICATION_VERIFIED
                if verdict == "refused_pass":
                    second = kb._open_verifier_child(conn, tid)
                    outcome["second_verifier_claimable"] = (
                        second is not None and second != _cid and kb.claim_task(conn, second) is not None)
                    ledger.mark(conn, tid, f"second verifier claimable={outcome['second_verifier_claimable']}")
        final = ledger.rows[-1]
        outcome.update({"attempts_start": start["attempts"], "attempts_end": final["attempts"],
                        "limit": final["limit"], "status_end": final["status"], "block_end": final["block"]})
        OUTCOMES[(grant, verdict)] = outcome
        print(f"[{label}] OUTCOME {outcome}")

        # Observed on the deployed Kanban code and pinned (2026-09-15). From the live 9/9 state:
        assert start["attempts"] == 9
        assert handoff["attempts"] == 10                                  # the handoff itself is a counted row
        if grant == 1:                                                    # handoff spends the only slot
            assert not outcome["verifier_created"] and outcome["status_end"] == "blocked"
            return
        assert outcome["verifier_created"] and outcome["verifier_ran"]    # verifier claim is the next counted row
        if verdict == "pass":                                             # completion row is not gated
            assert outcome["subject_verified"] and outcome["status_end"] == "done"
            assert outcome["attempts_end"] == 12
        elif verdict == "fail":
            # +2 stops at the ceiling for a decision; +3 and above let the dispatcher spend an automatic
            # rework run (the run 2829 pattern).
            assert outcome["automatic_rework_run"] is (grant >= 3)
            assert outcome["status_end"] == ("blocked" if grant == 2 else "running")
        else:
            # +2 leaves the refused PASS at the ceiling; +3 and above make a second verifier claimable.
            assert outcome["second_verifier_claimable"] is (grant >= 3)
            assert not outcome["subject_verified"]


class _QuietLedger(Ledger):
    def __init__(self):
        super().__init__("quiet")

    def mark(self, conn, tid, step):
        task = kb.get_task(conn, tid)
        row = {"attempts": kb.gauntlet_objective_attempts(conn, tid),
               "limit": kb.effective_objective_attempt_limit(conn, tid),
               "status": task.status, "block": task.block_kind}
        self.rows.append(row)
        return row
