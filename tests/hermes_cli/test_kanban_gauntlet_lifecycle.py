"""Gauntlet lifecycle: an execution claim cannot become a completion.

The contract these tests pin down is the required chain

    EXECUTING            -> status 'running'
    VERIFICATION_PENDING -> status 'review',  verification_state 'pending'
    VERIFIED             -> verification_state 'verified'
    COMPLETED            -> status 'done'

and its one forbidden shortcut: ``running -> done`` with no verification in
between. The enforcement lives in ``complete_task``'s ``UPDATE ... WHERE``, so
these tests deliberately also exercise the path where the Python pre-check is
bypassed — a guard that only exists above the SQL is not mechanical.

Enforcement is per-task (``gauntlet_enforced``) or board-wide (the
``kanban.gauntlet_enforcement`` config key). Both are off by default, so the
classic lifecycle every existing caller relies on is covered here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _executing(conn, *, gauntlet=True, title="gauntlet task"):
    """Create a task and drive it to EXECUTING. Returns (task_id, run_id)."""
    tid = kb.create_task(conn, title=title, assignee="default", gauntlet=gauntlet)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    return tid, claimed.current_run_id


# ---------------------------------------------------------------------------
# The forbidden transition
# ---------------------------------------------------------------------------

class TestForbiddenDirectCompletion:
    def test_running_to_done_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            with pytest.raises(kb.VerificationRequiredError) as exc:
                kb.complete_task(conn, tid, summary="done, trust me")
            assert exc.value.task_id == tid
            assert exc.value.status == "running"
            assert exc.value.verification_state is None
            # No state change at all: the task is still in flight and its run
            # is still open, so the worker can take the legal next step.
            task = kb.get_task(conn, tid)
            assert task.status == "running"
            assert task.completed_at is None
            assert task.current_run_id == run_id

    def test_refusal_is_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="shipped it")
            blocked = _events(conn, tid, kind="completion_blocked_unverified")
            assert len(blocked) == 1
            payload = blocked[0][1]
            assert payload["status"] == "running"
            assert payload["verification_state"] is None
            assert payload["summary_preview"] == "shipped it"

    def test_sql_guard_holds_when_the_precheck_is_bypassed(self, kanban_home):
        """The guard must be on the transition, not only above it.

        Simulates the race the pre-check cannot cover: enforcement becomes
        true after the pre-check read it as false. Patching ``gauntlet_required``
        to answer False once and True thereafter lets the call reach the
        ``UPDATE`` — which must still refuse.
        """
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            calls = {"n": 0}
            real = kb.gauntlet_required

            def _flaky(c, task_id):
                calls["n"] += 1
                return False if calls["n"] == 1 else real(c, task_id)

            kb.gauntlet_required = _flaky
            try:
                assert kb.complete_task(conn, tid, summary="raced") is False
            finally:
                kb.gauntlet_required = real
            assert calls["n"] >= 2, "the in-txn re-check never ran"
            assert kb.get_task(conn, tid).status == "running"

    def test_executor_cannot_verify_its_own_run(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            ok, reason = kb.record_verification(
                conn, tid, passed=True, verifier="default",
                evidence={"claim": "I checked it myself"},
            )
            assert ok is False
            assert "cannot verify its own work" in reason
            assert kb.get_task(conn, tid).verification_state is None

    @pytest.mark.parametrize("status", ["ready", "blocked"])
    def test_unexecuted_statuses_also_require_verification(
        self, kanban_home, status
    ):
        """A gauntlet task cannot be closed from outside the chain either.

        ``ready``/``blocked`` -> ``done`` is the manual-CLI shortcut. Under
        enforcement it is still a completion with nothing verified behind it.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="unexecuted", assignee="default", gauntlet=True
            )
            if status == "blocked":
                assert kb.block_task(conn, tid, reason="waiting") is True
            assert kb.get_task(conn, tid).status == status
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="closing it out")
            assert kb.get_task(conn, tid).status == status


# ---------------------------------------------------------------------------
# The valid chain
# ---------------------------------------------------------------------------

class TestValidChain:
    def test_full_chain_executing_pending_verified_completed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)

            # EXECUTING -> VERIFICATION_PENDING
            assert kb.request_review(
                conn, tid, summary="implemented X", reviewer="default",
                expected_run_id=run_id,
            ) is True
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_PENDING

            # Still not completable — pending is not verified.
            with pytest.raises(kb.VerificationRequiredError) as exc:
                kb.complete_task(conn, tid, summary="early")
            assert exc.value.verification_state == kb.VERIFICATION_PENDING
            assert kb.get_task(conn, tid).status == "review"

            # VERIFICATION_PENDING -> VERIFIED
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"command": "pytest -q", "exit_code": 0, "passed": 12},
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            task = kb.get_task(conn, tid)
            # VERIFIED is a distinct durable state from COMPLETED: the task is
            # still parked in review until someone actually completes it.
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_VERIFIED

            # VERIFIED -> COMPLETED
            assert kb.complete_task(conn, tid, summary="shipped") is True
            task = kb.get_task(conn, tid)
            assert task.status == "done"
            assert task.verification_state == kb.VERIFICATION_VERIFIED

    def test_ledger_is_durable_and_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"command": "pytest -q", "exit_code": 0},
                reason="suite green",
            )
            kb.complete_task(conn, tid, summary="shipped")

        # Re-open the DB: the ledger has to survive the connection, not just
        # live in the completing process's memory.
        with kb.connect_closing() as conn:
            history = kb.verification_history(conn, tid)
            assert [h["state"] for h in history] == [
                kb.VERIFICATION_PENDING,
                kb.VERIFICATION_VERIFIED,
            ]
            verdict = history[-1]
            assert verdict["verifier"] == "reviewer"
            assert verdict["evidence"] == {"command": "pytest -q", "exit_code": 0}
            assert verdict["reason"] == "suite green"
            assert verdict["created_at"] is not None
            assert len(_events(conn, tid, kind="verification_passed")) == 1

    def test_reviewer_run_approval_counts_as_the_verdict(self, kanban_home):
        """The existing agent review lane keeps working end to end.

        A run claimed from the ``review`` lane is structurally a second party,
        so its approval is recorded as the VERIFIED step rather than refused.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            reviewer_run = kb.claim_review_task(conn, tid)
            assert reviewer_run is not None
            assert reviewer_run.status == "running"
            assert reviewer_run.current_run_id != run_id

            assert kb.complete_task(conn, tid, summary="approved") is True
            task = kb.get_task(conn, tid)
            assert task.status == "done"
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            verdict = kb.verification_history(conn, tid)[-1]
            assert verdict["evidence"]["source"] == "review_run_approval"
            assert verdict["run_id"] == reviewer_run.current_run_id


# ---------------------------------------------------------------------------
# Failed verification never completes
# ---------------------------------------------------------------------------

class TestFailedVerification:
    def test_failure_routes_to_rework_and_stays_incomplete(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            ok, detail = kb.record_verification(
                conn, tid, passed=False, verifier="reviewer",
                reason="3 tests fail", evidence={"exit_code": 1},
            )
            assert ok is True
            assert detail == "rework"

            task = kb.get_task(conn, tid)
            assert task.status in ("ready", "todo")
            # The verdict is spent: it described the run that just failed, and
            # the head must not linger into the repair run.
            assert task.verification_state is None
            # But the ledger keeps it.
            assert kb.verification_history(conn, tid)[-1]["state"] == (
                kb.VERIFICATION_FAILED
            )
            assert len(_events(conn, tid, kind="verification_failed")) == 1

            # And the task is nowhere near done.
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="ignoring the failure")
            assert kb.get_task(conn, tid).status in ("ready", "todo")

    def test_failure_without_routing_stays_non_complete(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            ok, detail = kb.record_verification(
                conn, tid, passed=False, verifier="reviewer",
                reason="gate red", route_on_failure=False,
            )
            assert (ok, detail) == (True, kb.VERIFICATION_FAILED)
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_FAILED
            with pytest.raises(kb.VerificationRequiredError) as exc:
                kb.complete_task(conn, tid, summary="anyway")
            assert exc.value.verification_state == kb.VERIFICATION_FAILED

    def test_failing_verdict_requires_a_reason(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            ok, reason = kb.record_verification(conn, tid, passed=False)
            assert ok is False
            assert "reason is required" in reason
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_repair_then_reverify_completes(self, kanban_home):
        """The whole loop: fail, repair, re-review, pass, complete.

        The repaired PASS carries regression evidence — after a failing
        verdict that is not optional (see TestRegressionAfterRepair).
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=False, verifier="reviewer", reason="red",
            )
            # Repair run.
            repaired = kb.claim_task(conn, tid)
            assert repaired is not None and repaired.status == "running"
            kb.request_review(
                conn, tid, summary="fixed", reviewer="default",
                expected_run_id=repaired.current_run_id,
            )
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"exit_code": 0},
                regression_evidence={"command": "pytest -q", "exit_code": 0},
            )
            assert kb.complete_task(conn, tid, summary="shipped") is True
            assert kb.get_task(conn, tid).status == "done"
            assert [h["state"] for h in kb.verification_history(conn, tid)] == [
                kb.VERIFICATION_PENDING,
                kb.VERIFICATION_FAILED,
                kb.VERIFICATION_PENDING,
                kb.VERIFICATION_REGRESSION,
                kb.VERIFICATION_VERIFIED,
            ]


# ---------------------------------------------------------------------------
# A verdict is spent on the run it covered
# ---------------------------------------------------------------------------

class TestVerdictStaleness:
    def test_new_implementation_run_invalidates_a_verified_verdict(
        self, kanban_home
    ):
        """VERIFIED certifies one run's work, not the card forever."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"exit_code": 0},
            )
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_VERIFIED
            )

            # Reopened for more work — the old verdict never saw it.
            assert kb.reopen_review_task(conn, tid) is True
            assert kb.get_task(conn, tid).verification_state is None
            reclaimed = kb.claim_task(conn, tid)
            assert reclaimed is not None
            assert reclaimed.verification_state is None

            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="reusing the old verdict")
            assert kb.get_task(conn, tid).status == "running"

    def test_request_changes_clears_the_head_but_keeps_the_ledger(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            reviewer_run = kb.claim_review_task(conn, tid)
            ok, implementer = kb.request_changes(
                conn, tid, reason="needs tests",
                expected_run_id=reviewer_run.current_run_id,
            )
            assert ok is True
            assert implementer == "default"
            assert kb.get_task(conn, tid).verification_state is None
            assert [h["state"] for h in kb.verification_history(conn, tid)] == [
                kb.VERIFICATION_PENDING
            ]


# ---------------------------------------------------------------------------
# A material repair must be regression-tested before it can be blessed
# ---------------------------------------------------------------------------
#
# A failing verdict makes everything that follows a repair. The Gauntlet
# contract says a repair is regression-tested, so the PASS that closes it has
# to carry proof the checks were re-run — not another assurance from the same
# source that already got it wrong once.

REGRESSION_PROOF = {
    "command": "pytest -q tests/hermes_cli/test_thing.py",
    "exit_code": 0,
    "passed": 12,
}


def _fail_then_repair(conn, tid, run_id, *, summary="fixed"):
    """Fail verification, then run the repair up to the re-handoff.

    Returns the repair run's id. The task is left parked in ``review`` with
    the requirement armed and a fresh verification phase open.
    """
    kb.request_review(
        conn, tid, summary="impl", reviewer="default", expected_run_id=run_id,
    )
    kb.record_verification(
        conn, tid, passed=False, verifier="reviewer", reason="3 tests fail",
    )
    repaired = kb.claim_task(conn, tid)
    assert repaired is not None and repaired.status == "running"
    assert kb.request_review(
        conn, tid, summary=summary, reviewer="default",
        expected_run_id=repaired.current_run_id,
    ) is True
    return repaired.current_run_id


class TestRegressionAfterRepair:
    def test_failed_verdict_arms_the_requirement(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            assert kb.get_task(conn, tid).regression_required is False
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=False, verifier="reviewer", reason="red",
            )
            task = kb.get_task(conn, tid)
            assert task.regression_required is True
            assert kb.regression_evidence_required(conn, tid) is True
            assert _events(conn, tid, kind="verification_failed")[0][1][
                "regression_required"
            ] is True

            # It survives the rework: the repair run is exactly the work the
            # proof will have to cover.
            repaired = kb.claim_task(conn, tid)
            assert repaired.regression_required is True
            assert repaired.verification_state is None

    def test_pass_after_repair_without_evidence_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)

            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"note": "looks fine to me"},
            )
            assert ok is False
            assert "regression evidence is required" in detail

            # No verdict was written: the head is still the open phase, so the
            # task is not verified and cannot complete.
            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert task.regression_required is True
            assert kb.VERIFICATION_VERIFIED not in [
                h["state"] for h in kb.verification_history(conn, tid)
            ]
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="shipping anyway")
            assert kb.get_task(conn, tid).status == "review"

    def test_refusal_is_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            kb.record_verification(conn, tid, passed=True, verifier="reviewer")
            blocked = _events(
                conn, tid, kind="verification_blocked_no_regression"
            )
            assert len(blocked) == 1
            assert blocked[0][1]["verifier"] == "reviewer"
            assert "regression evidence is required" in blocked[0][1]["detail"]

    def test_evidence_must_name_the_check_that_was_rerun(self, kanban_home):
        """"It works now" is a claim. A command with an exit code is a fact."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            for bad in ({}, {"note": "re-tested"}, {"command": ""}, "pytest"):
                ok, detail = kb.record_verification(
                    conn, tid, passed=True, verifier="reviewer",
                    regression_evidence=bad,
                )
                assert ok is False, bad
                assert "regression evidence must" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_pass_with_evidence_verifies_and_completes(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            repair_run = _fail_then_repair(conn, tid, run_id)

            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"exit_code": 0},
                regression_evidence=REGRESSION_PROOF,
                reason="regression suite green",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)

            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            # The proof is spent by the verdict it backs.
            assert task.regression_required is False

            history = kb.verification_history(conn, tid)
            proof = [h for h in history if h["kind"] == kb.LEDGER_KIND_REGRESSION]
            assert len(proof) == 1
            assert proof[0]["state"] == kb.VERIFICATION_REGRESSION
            assert proof[0]["evidence"] == REGRESSION_PROOF
            assert proof[0]["verifier"] == "reviewer"
            # Bound to the phase that covered the repair run, both ways round.
            phase = [
                h for h in history
                if h["state"] == kb.VERIFICATION_PENDING
            ][-1]
            assert proof[0]["covers_phase_id"] == phase["id"]
            assert proof[0]["run_id"] == repair_run
            assert history[-1]["evidence"]["regression_proof_id"] == proof[0]["id"]

            assert kb.complete_task(conn, tid, summary="shipped") is True
            assert kb.get_task(conn, tid).status == "done"

    def test_proof_is_never_a_verdict(self, kanban_home):
        """A regression row is an input to a verdict, not one itself."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence=REGRESSION_PROOF,
            )
        with kb.connect_closing() as conn:
            # The head only ever holds a verdict state — 'regression' is not
            # one, so it can never be mistaken for a completed verification.
            states = [
                r["verification_state"]
                for r in conn.execute(
                    "SELECT verification_state FROM tasks WHERE id = ?", (tid,)
                )
            ]
            assert states == [kb.VERIFICATION_VERIFIED]
            assert kb.VERIFICATION_REGRESSION not in kb.VALID_VERIFICATION_STATES

    def test_regression_evidence_is_rejected_on_a_failing_verdict(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            ok, detail = kb.record_verification(
                conn, tid, passed=False, verifier="reviewer", reason="red",
                regression_evidence=REGRESSION_PROOF,
            )
            assert ok is False
            assert "belongs on a passing verdict" in detail
            # Nothing recorded either way: still just the open phase.
            assert [h["state"] for h in kb.verification_history(conn, tid)] == [
                kb.VERIFICATION_PENDING
            ]

    def test_first_pass_task_needs_no_regression_evidence(self, kanban_home):
        """Criterion: only a repair owes a proof. A clean run does not."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            assert kb.regression_evidence_required(conn, tid) is False
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                evidence={"command": "pytest -q", "exit_code": 0},
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            assert kb.complete_task(conn, tid, summary="shipped") is True
            assert kb.get_task(conn, tid).status == "done"
            assert [
                h for h in kb.verification_history(conn, tid)
                if h["kind"] == kb.LEDGER_KIND_REGRESSION
            ] == []

    def test_unprompted_evidence_is_still_recorded(self, kanban_home):
        """A verifier who re-ran the checks anyway gets it on the ledger."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence=REGRESSION_PROOF,
            )
            proofs = [
                h for h in kb.verification_history(conn, tid)
                if h["kind"] == kb.LEDGER_KIND_REGRESSION
            ]
            assert len(proofs) == 1


class TestStaleRegressionProof:
    def test_proof_from_an_earlier_phase_cannot_be_spent(self, kanban_home):
        """Fresh implementation after a failure needs its OWN proof.

        The first repair is proved and blessed; the card is then reopened,
        reworked, and fails again. The proof from the first repair describes
        code two runs old — it must not carry the second one.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence=REGRESSION_PROOF,
            )
            first_proof = [
                h for h in kb.verification_history(conn, tid)
                if h["kind"] == kb.LEDGER_KIND_REGRESSION
            ][-1]

            # Reopened for more work, which fails verification in its turn.
            assert kb.reopen_review_task(conn, tid) is True
            second = kb.claim_task(conn, tid)
            second_run = _fail_then_repair(
                conn, tid, second.current_run_id, summary="fixed again",
            )
            assert kb.get_task(conn, tid).regression_required is True

            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
            )
            assert ok is False
            assert "regression evidence is required" in detail
            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert task.regression_required is True

            # A proof for THIS phase clears it.
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence={
                    "command": "pytest -q tests/hermes_cli",
                    "exit_code": 0,
                },
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            proofs = [
                h for h in kb.verification_history(conn, tid)
                if h["kind"] == kb.LEDGER_KIND_REGRESSION
            ]
            assert len(proofs) == 2
            assert proofs[-1]["covers_phase_id"] != first_proof["covers_phase_id"]
            assert proofs[-1]["run_id"] == second_run
            assert kb.complete_task(conn, tid, summary="shipped") is True

    def test_proof_predating_the_failure_cannot_be_spent(self, kanban_home):
        """A proof must answer the failure, not precede it.

        Same verification phase throughout, so the phase binding alone would
        accept the earlier proof — it is refused because it was recorded
        before the verdict that armed the requirement.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            # Unprompted proof on a first-pass verdict: recorded, phase-bound.
            kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence=REGRESSION_PROOF,
            )
            # A second verifier then fails the same parked card.
            ok, detail = kb.record_verification(
                conn, tid, passed=False, verifier="reviewer2",
                reason="regression in another module",
                route_on_failure=False,
            )
            assert (ok, detail) == (True, kb.VERIFICATION_FAILED)
            assert kb.get_task(conn, tid).regression_required is True

            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
            )
            assert ok is False
            assert "regression evidence is required" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_FAILED
            )

    def test_head_verified_but_proof_outstanding_still_cannot_complete(
        self, kanban_home
    ):
        """The kernel guard, not just the Python one.

        Simulates the state a bypassed pre-check (or a raced arm) could leave
        behind: a VERIFIED head on a task that still owes a proof. The
        completion UPDATE tests both columns, so the write cannot land.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            conn.execute(
                "UPDATE tasks SET verification_state = 'verified' WHERE id = ?",
                (tid,),
            )
            conn.commit()
            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            assert task.regression_required is True

            # The pre-check sees a verified head and lets it through; the
            # UPDATE's predicate is what refuses.
            assert kb.complete_task(conn, tid, summary="raced") is False
            assert kb.get_task(conn, tid).status == "review"


class TestReviewerRunApproval:
    """The agent review lane approves inline via complete_task."""

    def test_inline_approval_of_a_repair_needs_the_proof(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            reviewer_run = kb.claim_review_task(conn, tid)
            assert reviewer_run is not None

            with pytest.raises(kb.VerificationRequiredError) as exc:
                kb.complete_task(conn, tid, summary="looks good to me")
            assert exc.value.regression_required is True
            assert "regression evidence is required" in exc.value.regression_detail
            blocked = _events(
                conn, tid, kind="completion_blocked_no_regression"
            )
            assert len(blocked) == 1
            assert blocked[0][1]["run_id"] == reviewer_run.current_run_id

            task = kb.get_task(conn, tid)
            assert task.status == "running"     # reviewer run still in flight
            assert task.completed_at is None
            assert kb.VERIFICATION_VERIFIED not in [
                h["state"] for h in kb.verification_history(conn, tid)
            ]

    def test_inline_approval_with_the_proof_completes(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            reviewer_run = kb.claim_review_task(conn, tid)

            assert kb.complete_task(
                conn, tid, summary="verified the fix",
                regression_evidence=REGRESSION_PROOF,
            ) is True
            task = kb.get_task(conn, tid)
            assert task.status == "done"
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            assert task.regression_required is False

            history = kb.verification_history(conn, tid)
            proof = [h for h in history if h["kind"] == kb.LEDGER_KIND_REGRESSION]
            assert len(proof) == 1
            assert proof[0]["evidence"] == REGRESSION_PROOF
            verdict = history[-1]
            assert verdict["state"] == kb.VERIFICATION_VERIFIED
            assert verdict["evidence"]["regression_proof_id"] == proof[0]["id"]
            assert verdict["run_id"] == reviewer_run.current_run_id

    def test_inline_approval_rejects_a_malformed_proof(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn)
            _fail_then_repair(conn, tid, run_id)
            kb.claim_review_task(conn, tid)
            with pytest.raises(kb.VerificationRequiredError) as exc:
                kb.complete_task(
                    conn, tid, summary="approved",
                    regression_evidence={"note": "re-tested, honest"},
                )
            assert exc.value.regression_required is True
            assert kb.get_task(conn, tid).status == "running"


# ---------------------------------------------------------------------------
# Compatibility: nothing changes unless enforcement is on
# ---------------------------------------------------------------------------

class TestCompatibility:
    def test_classic_task_still_completes_straight_from_running(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn, gauntlet=False, title="classic")
            task = kb.get_task(conn, tid)
            assert task.gauntlet_enforced is False
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"

    def test_review_lane_still_records_pending_for_classic_tasks(
        self, kanban_home
    ):
        """The ledger is written for every task so enabling enforcement later
        does not leave in-flight reviews without a phase start."""
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn, gauntlet=False, title="classic")
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )
            # ...but it gates nothing without enforcement.
            assert kb.complete_task(conn, tid, summary="approved") is True
            assert kb.get_task(conn, tid).status == "done"

    def test_board_wide_config_enforces_without_a_backfill(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn, gauntlet=False, title="pre-existing")
            assert kb.get_task(conn, tid).gauntlet_enforced is False

            monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: True)
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="no")
            assert kb.get_task(conn, tid).status == "running"

    def test_set_gauntlet_enforced_toggles_and_is_audited(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn, gauntlet=False, title="toggle")
            assert kb.set_gauntlet_enforced(conn, tid, True) is True
            assert kb.get_task(conn, tid).gauntlet_enforced is True
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="no")

            assert kb.set_gauntlet_enforced(conn, tid, False) is True
            assert kb.complete_task(conn, tid, summary="yes") is True
            assert kb.get_task(conn, tid).status == "done"
            assert len(_events(conn, tid, kind="gauntlet_enforcement")) == 2

    def test_set_gauntlet_enforced_on_unknown_task(self, kanban_home):
        with kb.connect_closing() as conn:
            assert kb.set_gauntlet_enforced(conn, "t_nope", True) is False

    def test_classic_task_completes_even_with_the_requirement_armed(
        self, kanban_home
    ):
        """A non-enforced board keeps its old lifecycle end to end.

        The verdict ledger is written for every task, so a classic card can
        acquire an armed requirement — it still must not change what a bare
        ``complete_task`` does when nothing is enforced.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn, gauntlet=False, title="classic")
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            kb.record_verification(
                conn, tid, passed=False, verifier="reviewer", reason="red",
            )
            assert kb.get_task(conn, tid).regression_required is True
            repaired = kb.claim_task(conn, tid)
            assert repaired is not None
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"

    def test_parent_gating_still_precedes_the_verification_gate(
        self, kanban_home
    ):
        """An unsatisfied parent is still a plain False, not a verification
        error — the two gates must not shadow each other."""
        with kb.connect_closing() as conn:
            parent = kb.create_task(conn, title="parent", assignee="default")
            child = kb.create_task(
                conn, title="child", assignee="default",
                parents=(parent,), gauntlet=True,
            )
            assert kb.get_task(conn, child).status == "todo"
            assert kb.complete_task(conn, child, summary="jumping the gun") is False


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_legacy_board_gains_the_columns_and_ledger(self, kanban_home):
        """A DB predating this feature must open and behave classically."""
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn, gauntlet=False, title="legacy")
            conn.execute("DROP TABLE task_verifications")
            conn.commit()

        kb.init_db()
        with kb.connect_closing() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert {"gauntlet_enforced", "verification_state"} <= cols
            assert kb.verification_history(conn, tid) == []
            # Legacy rows are not retro-flagged.
            assert kb.get_task(conn, tid).gauntlet_enforced is False
            assert kb.complete_task(conn, tid, summary="ok") is True

    def test_first_release_board_gains_the_regression_columns(self, kanban_home):
        """A board written before regression evidence existed must upgrade.

        Drops exactly what the first Gauntlet release did not have — the
        task row flag and the two ledger discriminators — with a verdict row
        already on the ledger, then re-opens it.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _executing(conn, gauntlet=False, title="pre-upgrade")
            kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            )
            conn.execute("ALTER TABLE tasks DROP COLUMN regression_required")
            conn.execute("ALTER TABLE task_verifications DROP COLUMN kind")
            conn.execute(
                "ALTER TABLE task_verifications DROP COLUMN covers_phase_id"
            )
            conn.commit()

        kb.init_db()
        with kb.connect_closing() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert "regression_required" in cols
            vcols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(task_verifications)")
            }
            assert {"kind", "covers_phase_id"} <= vcols

            # The pre-existing phase row reads as an ordinary verdict, and
            # nothing was retro-armed.
            history = kb.verification_history(conn, tid)
            assert [h["state"] for h in history] == [kb.VERIFICATION_PENDING]
            assert history[0]["kind"] == kb.LEDGER_KIND_VERDICT
            assert history[0]["covers_phase_id"] is None
            assert kb.get_task(conn, tid).regression_required is False
            assert kb.regression_evidence_required(conn, tid) is False

            # And the chain still works on the upgraded board: the phase that
            # survived the migration is the one a proof binds to.
            kb.set_gauntlet_enforced(conn, tid, True)
            kb.record_verification(
                conn, tid, passed=False, verifier="reviewer", reason="red",
                route_on_failure=False,
            )
            assert kb.get_task(conn, tid).regression_required is True
            ok, _ = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence={"command": "pytest -q", "exit_code": 0},
            )
            assert ok is True
            assert kb.complete_task(conn, tid, summary="ok") is True
            assert kb.get_task(conn, tid).status == "done"
