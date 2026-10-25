"""A ceiling block taken while the subject waits for its verifier must resume to review.

Reproduces the 2026-10-13 production defect on ``t_6cc051f2``: the implementer's hand-off
spent the objective's final allowed attempt, so the verifier spawn hit the ceiling and the
subject was blocked straight out of ``review``. ``_block_objective_attempt_ceiling`` wrote its
``blocked`` event without ``source_status`` (every other block records it), so after an
operator grant ``unblock_task`` resumed the subject to ``ready``. The dispatcher then claimed
a redundant implementation run, the run re-requested review, and the ceiling blocked it again
before any verifier could run. Twice in a row, with nothing changed.

Every transition goes through the REAL dispatcher loop, as in
``test_kanban_ceiling_inflight_verifier``.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_ceiling_inflight_verifier import Board, _history, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_objective_attempt_grant import OPERATOR

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

FIRST_PASS = "ACCEPTANCE: PASS\nVERDICT: PASS\nATLAS_VERDICT: PASS"


def _hand_off_on_final_attempt(board: Board) -> str:
    conn = board.conn
    tid = kb.create_task(conn, title="objective whose hand-off spends the last attempt",
                         assignee="default", gauntlet=True)
    limit = board.limit(tid)
    _history(board, tid, limit - 1)
    assert tid in board.tick(), "dispatcher did not claim the implementation run"
    run_id = board.task(tid).current_run_id
    assert board.attempts(tid) == limit, "the implementation run must be the final allowed attempt"
    kb.add_attachment(conn, tid, filename="EVIDENCE.md", stored_path=f"/tmp/{tid}/EVIDENCE.md",
                      size=512, uploaded_by="implementer")
    assert kb.request_review(conn, tid, summary="handoff", expected_run_id=run_id,
                             metadata={"changed_files": ["hermes_cli/example.py"],
                                       "verification": "focused"}) is True
    board.tick()
    return tid


def _last_blocked_payload(conn, tid) -> dict:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(row[0])


def test_ceiling_block_from_review_records_source_status(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid = _hand_off_on_final_attempt(board)
        subject = board.task(tid)
        assert subject.status == "blocked"
        assert subject.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert _last_blocked_payload(conn, tid).get("source_status") == "review"


def test_grant_and_unblock_after_review_ceiling_runs_verifier_not_implementer(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid = _hand_off_on_final_attempt(board)
        assert board.task(tid).status == "blocked"
        kb.grant_objective_attempts(conn, tid, added_attempts=1, authorized_by="Christopher",
                                    reason="one verifier for the unjudged hand-off", env=OPERATOR)
        assert kb.unblock_task(conn, tid)
        assert board.task(tid).status == "review", "unblock must resume the verification phase"

        spawned = board.tick()
        assert tid not in spawned, "no redundant implementation run may be claimed"
        child = kb._open_verifier_child(conn, tid)
        assert child is not None
        if child not in spawned:
            spawned = board.tick()
            assert tid not in spawned
            assert child in spawned, f"dispatcher did not claim the verifier: {spawned}"
        verifier = board.task(child)
        assert verifier.status == "running"
        with kb.write_txn(conn):
            kb._append_event(conn, child, kb._attest_kind(conn, child), {"executor": "codex"},
                             run_id=verifier.current_run_id)
        assert board.attempts(tid) == board.limit(tid), "the verifier is the one granted attempt"
        board.tick()
        assert kb.complete_task(conn, child, summary=FIRST_PASS,
                                expected_run_id=verifier.current_run_id) is True
        subject = board.task(tid)
        assert subject.verification_state == kb.VERIFICATION_VERIFIED
        assert subject.status == "done"


def test_unblock_without_grant_still_buys_nothing(kanban_home):
    with kb.connect_closing() as conn:
        board = Board(conn)
        tid = _hand_off_on_final_attempt(board)
        runs = board.run_ids()
        assert kb.unblock_task(conn, tid)
        for _ in range(3):
            assert board.tick() == []
        assert board.task(tid).status == "blocked"
        assert board.run_ids() == runs
