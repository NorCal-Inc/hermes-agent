"""The objective attempt ceiling must not strand an in-flight independent verifier's verdict.

Reproduces the 2026-09-15 production defect on ``t_b8d62378`` (F6, events 158974-158993): the
independent ``codex_verify`` child was claimed by the dispatcher as the objective's FINAL allowed
attempt (11/11). In the same tick the review loop called ``claim_review_task`` on the subject,
whose ceiling check blocked it for ``attempt_budget_exhausted``; the verifier's FAIL then returned
``recorded: false`` because verification is only valid from the review lane. A PASS would have been
stranded the same way.

Every transition here goes through the REAL dispatcher loop (``dispatch_once``, which also runs
stale supervision) — the only stand-ins are the process spawn (a recorder, so nothing runs) and the
verifier worker's own actions (the launcher attestation event and ``complete_task``), which is what
a real ``codex_verify`` worker does. A dispatcher tick separates every claim from every completion.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

PASS_WITH_REGRESSION = ("REGRESSION: pytest -q tests/hermes_cli/test_example.py -> exit 0, 12 passed\n"
                        "ACCEPTANCE: PASS\nVERDICT: PASS\nATLAS_VERDICT: PASS")
FAIL = "ACCEPTANCE: FAIL\nVERDICT: FAIL\nATLAS_VERDICT: FAIL"
REFUSED_PASS = "Looks complete.\n\nVERDICT: PASS\nATLAS_VERDICT: PASS"   # owes REGRESSION after a FAIL
DEFERRED_EVENT = "objective_attempt_ceiling_verdict_pending"


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


class Board:
    """The production board seen only through the dispatcher and worker-side calls."""

    def __init__(self, conn):
        self.conn = conn

    def tick(self) -> list[str]:
        spawned: list[str] = []

        def fake_spawn(task, workspace, board=None):
            spawned.append(task.id)
            return None                      # no process: nothing can crash or be reaped

        kb.dispatch_once(self.conn, spawn_fn=fake_spawn, max_spawn=8)
        return spawned

    def task(self, tid):
        return kb.get_task(self.conn, tid)

    def attempts(self, tid) -> int:
        return kb.gauntlet_objective_attempts(self.conn, tid)

    def limit(self, tid) -> int:
        return kb.effective_objective_attempt_limit(self.conn, tid)

    def events(self, tid, kind) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]

    def run_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM task_runs ORDER BY id")]

    def running_verifiers(self, subject) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT c.id FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? AND c.executor_lane = ? AND c.status = 'running'",
            (subject, kb.EXECUTOR_LANE_CODEX_VERIFY))]

    # --- worker-side actions -------------------------------------------------------------

    def implement_and_hand_off(self, tid, n) -> str:
        spawned = self.tick()
        assert tid in spawned, f"dispatcher did not claim the implementation run: {spawned}"
        run_id = self.task(tid).current_run_id
        kb.add_attachment(self.conn, tid, filename=f"EVIDENCE-{n}.md", stored_path=f"/tmp/{tid}/EVIDENCE-{n}.md",
                          size=512, uploaded_by="implementer")
        assert kb.request_review(self.conn, tid, summary=f"cycle {n}", expected_run_id=run_id,
                                 metadata={"changed_files": ["hermes_cli/example.py"], "verification": "focused"}) is True
        child = kb._open_verifier_child(self.conn, tid)
        assert child is not None, "request_review did not open the independent verifier route"
        return child

    def dispatcher_claims_verifier(self, subject, child) -> int:
        spawned = self.tick()
        assert child in spawned, f"dispatcher did not claim the verifier: {spawned}"
        task = self.task(child)
        assert task.status == "running"
        with kb.write_txn(self.conn):             # what the codex_verify launcher records on start
            kb._append_event(self.conn, child, "codex_verifier_started", {"executor": "codex"},
                             run_id=task.current_run_id)
        return task.current_run_id

    def verifier_returns(self, child, run_id, summary) -> None:
        assert kb.complete_task(self.conn, child, summary=summary, expected_run_id=run_id) is True


def _history(board: Board, tid: str, rows: int) -> None:
    """Earlier timed-out attempts on the lineage (as t_b8d62378 carried). They must stay counted."""
    with kb.write_txn(board.conn):
        for i in range(rows):
            board.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'default', 'timed_out', ?, ?, 'timed_out')", (tid, 100 + i, 101 + i))


def final_attempt_is_verifier(board: Board) -> tuple[str, str, int]:
    """Drive a lineage (prior FAIL cycle, like production) until the verifier claim is the last allowed attempt."""
    tid = kb.create_task(board.conn, title="objective under verification", assignee="default", gauntlet=True)
    limit = board.limit(tid)
    _history(board, tid, limit - 4)          # + impl1, verifier1, impl2, verifier2 == limit
    child1 = board.implement_and_hand_off(tid, 1)
    run1 = board.dispatcher_claims_verifier(tid, child1)
    board.tick()                              # a tick between claim and completion
    board.verifier_returns(child1, run1, FAIL)
    assert board.task(tid).regression_required
    child2 = board.implement_and_hand_off(tid, 2)
    assert board.attempts(tid) == limit - 1
    assert board.task(tid).status == "review"
    run2 = board.dispatcher_claims_verifier(tid, child2)
    assert board.attempts(tid) == limit, "the verifier claim must be the final allowed attempt"
    return tid, child2, run2


# 5 -----------------------------------------------------------------------------------------------

def test_in_flight_verifier_keeps_subject_in_review_after_ceiling_is_numerically_reached(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, _run = final_attempt_is_verifier(board)
        # The claim tick itself is where production blocked the subject.
        assert board.task(tid).status == "review", board.task(tid).block_kind
        assert board.task(tid).verification_state == kb.VERIFICATION_PENDING
        for _ in range(3):
            assert board.tick() == []
        subject = board.task(tid)
        assert subject.status == "review" and subject.block_kind != kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.running_verifiers(tid) == [child]
        assert board.attempts(tid) == board.limit(tid)
        assert len(board.events(tid, DEFERRED_EVENT)) == 1, "deferral is recorded once per phase, not per tick"


# 1 -----------------------------------------------------------------------------------------------

def test_final_attempt_verifier_pass_is_recorded_and_subject_completes(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        board.tick()
        board.verifier_returns(child, run, PASS_WITH_REGRESSION)
        subject = board.task(tid)
        assert subject.verification_state == kb.VERIFICATION_VERIFIED
        assert subject.status == "done"
        returned = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'verifier_verdict_returned' "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
        assert '"recorded": true' in returned
        assert board.tick() == []
        assert board.task(tid).status == "done"


# 2 + 4 -------------------------------------------------------------------------------------------

def test_final_attempt_verifier_fail_is_recorded_and_no_run_beyond_the_ceiling(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        failed_before = len(board.events(tid, "verification_failed"))
        board.tick()
        board.verifier_returns(child, run, FAIL)
        assert len(board.events(tid, "verification_failed")) == failed_before + 1, "the FAIL must be recorded"
        runs_after_verdict = board.run_ids()
        for _ in range(3):
            assert board.tick() == [], "no rework or verifier run may start beyond the ceiling"
        subject = board.task(tid)
        assert subject.status == "blocked"
        assert subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.run_ids() == runs_after_verdict
        assert board.attempts(tid) == board.limit(tid)
        # 4: an operator unblock without a grant still cannot buy an attempt beyond the ceiling.
        assert kb.unblock_task(conn, tid)
        assert board.tick() == []
        assert board.task(tid).status == "blocked"
        assert board.run_ids() == runs_after_verdict
        assert kb.claim_task(conn, tid) is None


# 3 -----------------------------------------------------------------------------------------------

def test_final_attempt_verifier_refused_pass_stops_bounded(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        board.tick()
        board.verifier_returns(child, run, REFUSED_PASS)
        assert board.task(tid).verification_state != kb.VERIFICATION_VERIFIED
        runs_after_verdict = board.run_ids()
        for _ in range(3):
            assert board.tick() == []
        subject = board.task(tid)
        assert subject.status == "blocked" and subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.running_verifiers(tid) == []
        assert board.run_ids() == runs_after_verdict
        assert board.attempts(tid) == board.limit(tid)


def test_in_flight_verifier_that_ends_without_a_verdict_releases_the_ceiling_block(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, _run = final_attempt_is_verifier(board)
        assert kb.reclaim_task(conn, child, reason="verifier died", signal_fn=lambda *a, **k: None)
        runs = board.run_ids()
        for _ in range(3):
            assert board.tick() == []
        subject = board.task(tid)
        assert subject.status == "blocked" and subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.run_ids() == runs


# 6 -----------------------------------------------------------------------------------------------

def test_unrelated_objectives_are_unaffected(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        fresh = kb.create_task(conn, title="unrelated fresh objective", assignee="default", gauntlet=True)
        exhausted = kb.create_task(conn, title="unrelated exhausted objective", assignee="default", gauntlet=True)
        _history(board, exhausted, board.limit(exhausted))
        spawned = board.tick()
        assert fresh in spawned and exhausted not in spawned
        assert board.attempts(fresh) == 1 and board.limit(fresh) == board.limit(exhausted)
        assert board.task(exhausted).status == "blocked"
        assert board.task(exhausted).block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.events(fresh, DEFERRED_EVENT) == [] and board.events(exhausted, DEFERRED_EVENT) == []
        assert board.task(tid).status == "review"
        board.verifier_returns(child, run, PASS_WITH_REGRESSION)
        assert board.task(tid).status == "done"
        assert board.task(exhausted).status == "blocked"


# 7 -----------------------------------------------------------------------------------------------

def test_historical_attempts_remain_counted_and_nothing_is_reset(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        history = board.run_ids()
        limit = board.limit(tid)
        board.tick()
        board.verifier_returns(child, run, FAIL)
        board.tick()
        assert set(history) <= set(board.run_ids()), "no attempt row may be removed"
        assert board.attempts(tid) >= limit
        assert board.limit(tid) == limit, "no implicit grant"
        assert board.events(tid, kb.OBJECTIVE_ATTEMPT_GRANT_EVENT) == []
        timed_out = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ? AND status = 'timed_out'",
                                 (tid,)).fetchone()[0]
        assert timed_out == limit - 4


# 8 -----------------------------------------------------------------------------------------------

def test_no_verifier_of_verifier_and_a_verifier_cannot_claim_the_exemption(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        for _ in range(2):
            board.tick()
        # The running verifier is itself at the objective ceiling; it is never an exempt "subject".
        assert kb._in_flight_verifier_for_subject(conn, child) is None
        assert kb._in_flight_verifier_for_subject(conn, tid) == (child, run)
        board.verifier_returns(child, run, REFUSED_PASS)
        for _ in range(2):
            board.tick()
        nested = conn.execute(
            "SELECT c.id FROM task_links l JOIN tasks p ON p.id = l.parent_id JOIN tasks c ON c.id = l.child_id "
            "WHERE p.executor_lane = ? AND c.executor_lane = ?",
            (kb.EXECUTOR_LANE_CODEX_VERIFY, kb.EXECUTOR_LANE_CODEX_VERIFY)).fetchall()
        assert nested == []
        assert kb._in_flight_verifier_for_subject(conn, tid) is None


# --- Real dispatcher-loop replay of the production F6 cycle ---------------------------------------

from tests.hermes_cli.test_t_b8d62378_lifecycle_replay import OPERATOR, _QuietLedger, replay_live_history  # noqa: E402


@pytest.mark.parametrize("verdict", ["pass", "fail", "refused_pass"])
def test_t_b8d62378_f6_cycle_through_the_dispatcher(kanban_home, verdict):
    """Live 9/9 -> +2 grant -> F6 operator handoff -> dispatcher-claimed verifier (11/11) -> verdict."""
    summaries = {"pass": PASS_WITH_REGRESSION, "fail": FAIL, "refused_pass": REFUSED_PASS}
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid = replay_live_history(conn, _QuietLedger())
        assert (board.attempts(tid), board.limit(tid), board.task(tid).status) == (9, 9, "blocked")
        history = board.run_ids()
        kb.grant_objective_attempts(conn, tid, added_attempts=2, authorized_by="Christopher",
                                    reason="replay of event 158917", env=OPERATOR)
        assert (board.attempts(tid), board.limit(tid), board.task(tid).status) == (9, 11, "blocked")
        # F6 handoff exactly as run in production (one operator process).
        kb.add_attachment(conn, tid, filename="t_b8d62378-F6-facts.json", stored_path=f"/tmp/{tid}/facts.json",
                          size=2048, uploaded_by="claude-code (operator relay for Christopher)")
        kb.add_attachment(conn, tid, filename="T_B8D62378-F6-EVIDENCE-PACKET.md", stored_path=f"/tmp/{tid}/packet.md",
                          size=4096, uploaded_by="claude-code (operator relay for Christopher)")
        kb.add_comment(conn, tid, "claude-code (operator relay for Christopher)", "F6 final verification handoff")
        assert kb.unblock_task(conn, tid)
        ok, reason = kb.request_review(conn, tid, summary="F6 evidence packet attached", with_reason=True,
                                       metadata={"changed_files": ["norcal/health/system_health_controller.py"],
                                                 "verification": "full gate"})
        assert ok, reason
        assert (board.attempts(tid), board.task(tid).status) == (10, "review")
        child = kb._open_verifier_child(conn, tid)
        assert child is not None
        run = board.dispatcher_claims_verifier(tid, child)          # production tick 09:08:16
        assert board.attempts(tid) == 11
        assert board.task(tid).status == "review", "the claim tick must not block the subject"
        assert board.tick() == []                                    # verifier still running
        board.verifier_returns(child, run, summaries[verdict])      # production 09:09:18
        returned = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'verifier_verdict_returned' "
            "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0]
        for _ in range(3):
            assert board.tick() == [], "nothing may run beyond the ceiling"
        subject = board.task(tid)
        assert set(history) <= set(board.run_ids())
        assert board.limit(tid) == 11
        assert board.running_verifiers(tid) == []
        if verdict == "pass":
            assert '"recorded": true' in returned
            assert (subject.status, subject.verification_state) == ("done", kb.VERIFICATION_VERIFIED)
        elif verdict == "fail":
            assert '"recorded": true' in returned
            assert subject.status == "blocked" and subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
            assert board.attempts(tid) == 11
        else:
            assert subject.verification_state != kb.VERIFICATION_VERIFIED
            assert subject.status == "blocked" and subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
            assert board.attempts(tid) == 11


# 4b ----------------------------------------------------------------------------------------------

def test_no_new_claim_in_the_lineage_while_the_final_verifier_is_in_flight(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, _run = final_attempt_is_verifier(board)
        extra = kb.create_task(conn, title="second verifier for the same phase", assignee="atlas", parents=[tid])
        kb.recompute_ready(conn)
        assert kb._objective_lineage_root(conn, extra) == tid
        assert board.task(extra).status == "ready", "the extra card must be claimable for this test to bite"
        runs = board.run_ids()
        spawned = board.tick()
        assert extra not in spawned
        assert board.run_ids() == runs, "no attempt beyond the ceiling"
        assert board.task(extra).status == "blocked", "the extra lineage card is blocked, not exempt"
        assert board.task(extra).block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert board.task(tid).status == "review", "the in-flight verifier's subject stays in the review lane"
        assert board.running_verifiers(tid) == [child]


# Exemption narrowness -----------------------------------------------------------------------------
#
# Each case starts from the real dispatcher-built state (final verifier claimed and running at the
# ceiling), breaks exactly ONE eligibility condition, and requires the exemption to be refused and the
# ceiling to block the subject as before. These are states the lifecycle should not produce together
# with an in-flight verifier; the exemption must not depend on that.

def _break_subject_is_a_verifier(conn, tid, child, run):
    conn.execute("UPDATE tasks SET executor_lane = ? WHERE id = ?", (kb.EXECUTOR_LANE_CODEX_VERIFY, tid))


def _break_subject_not_in_review(conn, tid, child, run):
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))


def _break_verdict_not_pending(conn, tid, child, run):
    conn.execute("UPDATE tasks SET verification_state = 'failed' WHERE id = ?", (tid,))


def _break_run_closed(conn, tid, child, run):
    conn.execute("UPDATE task_runs SET ended_at = started_at + 1 WHERE id = ?", (run,))


def _break_run_from_earlier_phase(conn, tid, child, run):
    phase = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'review_requested'",
                         (tid,)).fetchone()[0]
    conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (phase - 10, run))


def _break_child_not_running(conn, tid, child, run):
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (child,))


def _break_child_not_a_verifier(conn, tid, child, run):
    conn.execute("UPDATE tasks SET executor_lane = 'claude' WHERE id = ?", (child,))


@pytest.mark.parametrize("breaker", [
    _break_subject_is_a_verifier, _break_subject_not_in_review, _break_verdict_not_pending, _break_run_closed,
    _break_run_from_earlier_phase, _break_child_not_running, _break_child_not_a_verifier,
], ids=lambda f: f.__name__[len("_break_"):])
def test_exemption_requires_every_condition(kanban_home, breaker):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid, child, run = final_attempt_is_verifier(board)
        assert kb._in_flight_verifier_for_subject(conn, tid) == (child, run)       # positive control
        with kb.write_txn(conn):
            breaker(conn, tid, child, run)
        assert kb._in_flight_verifier_for_subject(conn, tid) is None
        with kb.write_txn(conn):
            kb._block_objective_attempt_ceiling(conn, tid)
        subject = board.task(tid)
        # Moving the child off the codex_verify lane also removes its run from the lineage count, so the
        # ceiling may no longer be reached; the block is only owed while it still is.
        if kb._objective_attempt_ceiling_reached(conn, tid)[0] and subject.status not in ("done", "archived", "failed", "cancelled"):
            assert subject.status == "blocked" and subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
