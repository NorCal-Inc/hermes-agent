"""One Gauntlet objective, start to finish, with nobody relaying anything.

This is the acceptance test for the whole control plane, and its subject is
not any single function — it is the *absence of a human in the loop*. The
failure it exists to catch is the one that kept happening in production: every
individual mechanism worked, and the objective still did not close, because
some step required a person to read a verdict in one place and retype it in
another. A control plane that needs a copy/paste relay to advance is not
automated; it is a person with extra steps.

So the discipline here is what the test is NOT allowed to do:

* It never calls ``record_verification`` on a subject. The ONLY way a verdict
  may reach a subject is by the independent verifier child completing with its
  verdict line, and the return path carrying it. Any assertion that passes
  because the test wrote the verdict itself would be proving nothing.
* It never calls ``complete_task`` on a subject. Finalization has to come from
  the verified state, on its own.
* It never hand-creates a verifier child, and never promotes one. If the
  dependency gate will not dispatch the child, the run stalls — which is the
  correct outcome to observe, not something to route around.

"Christopher does nothing after intake" is therefore mechanical rather than
aspirational: after the objective card exists, every subsequent write in this
test is one an autonomous role would have made.

The recovery leg is not optional decoration. An objective that only closes
when the first attempt happens to pass is not a governed lifecycle — the
first verdict here is deliberately FAIL, and the run has to detect it, repair
it, re-verify it and close anyway, with no relay at either verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kinds(conn, tid):
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]


def _attach(conn, tid, filename, *, by="executor-lane"):
    """The evidence packet. Without one there is nothing falsifiable to check."""
    kb.add_attachment(
        conn, tid, filename=filename,
        stored_path=f"/tmp/{tid}/{filename}",
        size=4096, uploaded_by=by,
    )


def _executor_does_the_work(conn, tid, *, summary, evidence_file):
    """Claim, produce evidence, hand off. Exactly what a worker does."""
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    _attach(conn, tid, evidence_file)
    assert kb.request_review(
        conn, tid, summary=summary, expected_run_id=claimed.current_run_id,
    ) is True
    return claimed


def _independent_verifier_reports(conn, subject_id, report):
    """Dispatch the verifier child the control plane opened, and report.

    Deliberately does not create or promote the child: if the handoff did not
    open a dispatchable route, this fails, which is the point.
    """
    cid = kb._open_verifier_child(conn, subject_id)
    assert cid is not None, (
        "the handoff must open an independent verification route"
    )
    assert kb.get_task(conn, cid).status == "ready", (
        "the verifier child must be dispatchable, not parked behind its own "
        "subject's completion"
    )
    claimed = kb.claim_task(conn, cid)
    assert claimed is not None and claimed.status == "running"
    # The verifier's ONLY output is its report. Nothing else in this test
    # touches the subject's verification state.
    assert kb.complete_task(
        conn, cid, summary=report, expected_run_id=claimed.current_run_id,
    ) is True
    return cid


class TestObjectiveClosesWithNoHumanRelay:
    def test_intake_to_closure_including_a_recovered_failure(self, kanban_home):
        with kb.connect_closing() as conn:
            # -- 1. Intake. The last thing Christopher does. ----------------
            objective = kb.create_task(
                conn,
                title="Objective: prove the Gauntlet closes unattended",
                # The governor role is narrative; the isolated board only
                # registers "default", and criterion F's dispatchability gate
                # correctly refuses an assignee it cannot spawn. Leaving the
                # objective unassigned is the honest encoding of "Erika holds
                # it" here, and it exercises the None arm of that gate.
                assignee=None,
                gauntlet=True,
                created_by="christopher",
            )

            # -- 2. Erika accepts and delegates a phase to an executor. -----
            #    The dependency runs phase -> objective, not the other way
            #    round: a child waits for a TERMINAL parent, so making the
            #    phase a child of the open objective would gate the work
            #    behind the very card it is meant to advance. That is exactly
            #    the shape that stranded t_cbc133db's four delegated children
            #    in todo. The objective is what becomes reachable once the
            #    phase closes.
            phase = kb.create_task(
                conn,
                title="Phase 1: implement and evidence the change",
                assignee="default",
                gauntlet=True,
                created_by="erika",
            )
            kb.link_tasks(conn, phase, objective)

            # -- 3. The executor works and hands off. ----------------------
            _executor_does_the_work(
                conn, phase,
                summary="implemented; evidence attached",
                evidence_file="EXECUTION-EVIDENCE-PACKET.md",
            )

            parked = kb.get_task(conn, phase)
            assert parked.status == "review"
            assert parked.verification_state == kb.VERIFICATION_PENDING, (
                "a clean executor exit is a claim, not a verdict"
            )

            # -- 4. Independent verifier runs and returns FAIL. -------------
            #    The recoverable failure. Nobody relays this anywhere.
            first_verifier = _independent_verifier_reports(
                conn, phase,
                "Checked the packet against the diff.\n\nVERDICT: FAIL",
            )

            # -- 5. The verdict came back on its own. ----------------------
            after_fail = kb.get_task(conn, phase)
            assert "verifier_verdict_returned" in _kinds(conn, phase)
            assert "verification_failed" in _kinds(conn, phase)
            assert after_fail.verification_state != kb.VERIFICATION_VERIFIED
            assert after_fail.status != "done", (
                "a failed verdict must never leave the subject completable"
            )
            assert after_fail.regression_required is True, (
                "the repair gate arms itself; the next PASS owes re-run proof"
            )

            # -- 6. Governed recovery: the repair leg. ---------------------
            _executor_does_the_work(
                conn, phase,
                summary="repaired the defect the verifier named; re-ran checks",
                evidence_file="REPAIR-EVIDENCE-PACKET.md",
            )

            # -- 7. Re-verification. A NEW independent verifier child. -----
            second_verifier = _independent_verifier_reports(
                conn, phase,
                "Re-ran the suite after the repair: 356 passed.\n\n"
                "REGRESSION: pytest tests/hermes_cli/ -> exit 0, 356 passed\n"
                "VERDICT: PASS",
            )
            assert second_verifier != first_verifier, (
                "re-verification must be a fresh independent run, not a "
                "re-open of the verifier that already rendered a verdict"
            )

            # -- 8. The PASS returned and closed the phase, unattended. -----
            closed = kb.get_task(conn, phase)
            assert closed.verification_state == kb.VERIFICATION_VERIFIED, (
                "the independent PASS must reach the subject with no relay"
            )
            assert closed.regression_required is False, (
                "the repair leg's PASS must spend a real regression proof"
            )
            assert closed.status in ("done", "archived"), (
                "a verified subject finalizes from its verified state; "
                "requiring a human to call complete_task IS the relay"
            )

            # -- 9. The result is available to Christopher on the objective.
            #    Not a chat message someone pasted: board state, reachable
            #    from the card he created.
            assert objective in kb.child_ids(conn, phase)
            assert kb._parents_satisfied(conn, objective) is True, (
                "the closed, verified phase must release the objective — "
                "this is the result reaching Christopher as board state "
                "rather than as a message someone had to forward"
            )
            obj_after = kb.get_task(conn, objective)
            assert obj_after.status in ("ready", "todo")

    def test_the_test_itself_never_relayed_a_verdict(self, kanban_home):
        """Guard the guard.

        Every verdict on the subject must be signed by the codex_verify lane.
        If a future edit to this file quietly recorded a verdict directly, the
        run above would still go green while proving nothing — so the
        provenance of each verdict is asserted rather than assumed.
        """
        with kb.connect_closing() as conn:
            phase = kb.create_task(
                conn, title="phase", assignee="default", gauntlet=True,
            )
            _executor_does_the_work(
                conn, phase, summary="done", evidence_file="EVIDENCE.md",
            )
            _independent_verifier_reports(conn, phase, "VERDICT: PASS")

            # Only RENDERED verdicts. request_review opens the phase with a
            # `pending` row of the same kind; that is a phase marker rather
            # than a verdict, and is unsigned by design.
            signers = [
                r["verifier"] for r in conn.execute(
                    "SELECT verifier FROM task_verifications "
                    "WHERE task_id = ? AND kind = ? AND state IN (?, ?)",
                    (
                        phase, kb.LEDGER_KIND_VERDICT,
                        kb.VERIFICATION_VERIFIED, kb.VERIFICATION_FAILED,
                    ),
                )
            ]
            assert signers, "the subject must carry a durable verdict"
            for signer in signers:
                assert signer is not None
                assert signer.startswith(f"{kb.EXECUTOR_LANE_CODEX_VERIFY}:"), (
                    f"verdict signed by {signer!r} — every verdict on the "
                    f"subject must come from the independent verifier lane"
                )
