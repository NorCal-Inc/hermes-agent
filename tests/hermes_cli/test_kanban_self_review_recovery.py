"""Self-review recovery and the blocked-parent return path.

Regression cover for the live reproduction chain ``t_00690780 -> t_db0af7e0``
(board ``dashboard-v20-home``, 2026-09-03). The recorded event stream is the
specification these tests encode:

    9857  review_requested                  implementer=default
    9865  verification_blocked_self_review  verifier=default implementer=default
    9867  verification_blocked_self_review  (same, run 1624)
    9872  verification_blocked_self_review  (same, run 1624)
    9873  crashed                           exit_code=1  retry_status=review
    9874  claimed                           run 1625, source_status=review
    9879  verification_blocked_self_review  (same identity, again)
    9882  verification_blocked_self_review
    9883  crashed                           retry_status=review
    9884  gave_up                           failures=2 effective_limit=2
    9886  linked_task_gave_up  -> t_db0af7e0, which was parked in 'todo'

Five separate defects are visible in those 12 lines, and each has a test here:

  1. the same identity was re-selected for review after being refused;
  2. correct refusals were charged to the ordinary implementation retry budget
     and tripped the breaker at 2/2;
  3. the failure was written onto a dependent card that could not run, so its
     owner had no mechanical route to a recovery decision;
  4. the independent ``codex_verify`` lane had no return path — a verdict could
     be produced and the subject would never receive it;
  5. end to end, a subject whose implementation succeeded could not reach a
     verdict at all.

Nothing here reruns or re-derives the original 55 KB audit matrix; the fixture
stands in for the evidence packet with a single attachment, which is all the
dependency gate and the verifier lane actually read.
"""

from __future__ import annotations

import json
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


def _kinds(conn, tid):
    return [k for k, _ in _events(conn, tid)]


def _subject_awaiting_verification(conn, *, assignee="default", title="subject"):
    """Drive a gauntlet task to VERIFICATION_PENDING with an evidence packet.

    This is t_00690780 at event 9857: implementation done, four artefacts
    attached, parked in the review lane. Everything after this point is the
    part that was broken.
    """
    tid = kb.create_task(conn, title=title, assignee=assignee, gauntlet=True)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    kb.add_attachment(
        conn, tid,
        filename="EXECUTION-EVIDENCE-PACKET.md",
        stored_path=f"/tmp/{tid}/EXECUTION-EVIDENCE-PACKET.md",
        size=8549, uploaded_by="claude-lane",
    )
    assert kb.request_review(
        conn, tid, summary="audit matrix complete; 4 artefacts attached",
        expected_run_id=claimed.current_run_id,
    ) is True
    task = kb.get_task(conn, tid)
    assert task.status == "review"
    assert task.verification_state == kb.VERIFICATION_PENDING
    return tid


def _self_review_refused(conn, tid):
    """Replay one reviewer run that is refused for self-review, then crashes.

    Returns the reviewer run id. Mirrors runs 1624/1625 exactly: claim from the
    review lane with the same assignee, attempt completion, get refused, exit
    non-zero into the crash path.
    """
    review = kb.claim_review_task(conn, tid)
    assert review is not None, "review claim was expected to succeed here"
    assert kb.complete_task(
        conn, tid, summary="approving own work",
        expected_run_id=review.current_run_id,
    ) is False
    assert ("verification_blocked_self_review", ) in [
        (k,) for k in _kinds(conn, tid)
    ]
    kb._record_task_failure(
        conn, tid, "pid 3325366 not alive",
        outcome="crashed", failure_limit=2,
        release_claim=True, end_run=True,
    )
    return review.current_run_id


# ---------------------------------------------------------------------------
# Defect 1 — repeated same-identity review selection
# ---------------------------------------------------------------------------


class TestSameIdentityReviewSelection:
    def test_refused_identity_cannot_claim_review_again(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            assert kb.get_task(conn, tid).status == "review"

            # This is the live loop: the dispatcher comes back next tick and
            # re-selects the identical assignee. It must now be refused at
            # selection instead of manufacturing the same verdict refusal.
            assert kb.claim_review_task(conn, tid) is None

            rejected = _events(conn, tid, "review_claim_rejected_self_review")
            assert len(rejected) == 1
            assert rejected[0][1]["candidate"] == "default"
            assert rejected[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

            # Refused, not stranded: still parked in the lane an independent
            # verifier reads from, with its verdict still outstanding.
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert task.claim_lock is None

    def test_rejection_event_is_emitted_once_not_once_per_tick(
        self, kanban_home
    ):
        """The dispatcher re-evaluates every parked task on every tick."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            for _ in range(25):
                assert kb.claim_review_task(conn, tid) is None
            assert len(_events(conn, tid, "review_claim_rejected_self_review")) == 1

    def test_independent_identity_may_still_claim_the_review(self, kanban_home):
        """The exclusion is of one identity, not of the review lane."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = 'atlas' WHERE id = ?", (tid,)
                )
            review = kb.claim_review_task(conn, tid)
            assert review is not None
            assert review.status == "running"

    def test_exclusion_is_scoped_to_the_current_verification_phase(
        self, kanban_home
    ):
        """A later phase starts clean.

        After ``request_changes`` routes the work back and the implementer
        re-submits, the previous phase's refusal must not permanently bar the
        only identity on the board from ever reviewing it again.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            assert kb.claim_review_task(conn, tid) is None

            # Route back for repair, then hand off again -> new phase.
            assert kb.reopen_review_task(conn, tid) is True
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="repaired and resubmitted",
                expected_run_id=claimed.current_run_id,
            ) is True

            assert kb._self_review_refused_implementers(conn, tid) == set()
            assert kb.claim_review_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Defect 2 — retry exhaustion caused by a correct self-review refusal
# ---------------------------------------------------------------------------


class TestSelfReviewRefusalRetryBudget:
    def test_refusal_does_not_consume_implementation_retry_budget(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            before = kb.get_task(conn, tid)
            assert before.consecutive_failures == 0

            _self_review_refused(conn, tid)

            after = kb.get_task(conn, tid)
            assert after.consecutive_failures == 0, (
                "a correct governance refusal was charged to the subject's "
                "ordinary implementation retry budget"
            )
            assert after.status == "review"
            assert "gave_up" not in _kinds(conn, tid)

            not_counted = _events(conn, tid, "self_review_refusal_not_counted")
            assert len(not_counted) == 1
            assert not_counted[0][1]["retry_status"] == "review"
            assert not_counted[0][1]["refused_identities"] == ["default"]

            required = _events(conn, tid, "independent_verification_required")
            assert len(required) == 1
            assert required[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

    def test_two_refusals_do_not_trip_the_breaker(self, kanban_home):
        """The exact live shape: failure_limit=2, two refused reviewer runs.

        On the live board this produced ``gave_up`` (failures=2,
        effective_limit=2) and drove t_00690780 to blocked/needs_input.
        Events 9873 and 9883 were both crash-path accountings
        (``release_claim=False, end_run=False`` — what ``detect_crashed_workers``
        calls), so the second one is replayed here in that exact mode.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            kb._record_task_failure(
                conn, tid, "pid 3325366 not alive",
                outcome="crashed", failure_limit=2,
            )

            task = kb.get_task(conn, tid)
            assert task.consecutive_failures == 0
            assert task.status == "review"
            assert task.block_kind != "needs_input"
            assert "gave_up" not in _kinds(conn, tid)

    def test_counter_is_preserved_not_reset(self, kanban_home):
        """History is preserved. A prior real failure keeps its weight."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
                    (tid,),
                )
            _self_review_refused(conn, tid)
            assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_ordinary_implementation_crash_still_counts(self, kanban_home):
        """The carve-out is not a hole in the circuit breaker."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="ordinary work", assignee="default", gauntlet=True,
            )
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            kb._record_task_failure(
                conn, tid, "boom", outcome="crashed", failure_limit=2,
                release_claim=True, end_run=True,
            )
            assert kb.get_task(conn, tid).consecutive_failures == 1

    def test_review_crash_without_a_refusal_still_counts(self, kanban_home):
        """A genuinely broken reviewer run is still a failure."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            review = kb.claim_review_task(conn, tid)
            assert review is not None
            # No self-review refusal on record for this phase.
            assert kb._self_review_refused_implementers(conn, tid) == set()
            kb._record_task_failure(
                conn, tid, "reviewer OOM", outcome="crashed", failure_limit=2,
                release_claim=True, end_run=True,
            )
            assert kb.get_task(conn, tid).consecutive_failures == 1


# ---------------------------------------------------------------------------
# Defect 3 — a dependency's terminal failure notified a card that cannot run
# ---------------------------------------------------------------------------


class TestBlockedDependencyNotifiesGovernableParent:
    def test_dependent_parked_in_todo_becomes_governable(self, kanban_home):
        """The t_db0af7e0 shape.

        The governance card was linked as the CHILD of its execution card and
        sat in 'todo' behind an unsatisfiable dependency. ``linked_task_gave_up``
        landed on a card ``recompute_ready`` would never promote and no
        dispatcher would ever claim.
        """
        with kb.connect_closing() as conn:
            # Live ordering (t_db0af7e0 events 9837-9845): the governance card
            # was already running when it spawned and linked its execution
            # half, then parked itself in dependency_wait.
            owner = kb.create_task(
                conn, title="governance half", assignee="erika",
            )
            assert kb.claim_task(conn, owner) is not None
            dep = kb.create_task(
                conn, title="execution half", assignee="default",
            )
            kb.link_tasks(conn, dep, owner)
            assert kb.block_task(
                conn, owner, reason="delegated to child", kind="dependency",
            ) is True
            assert kb.get_task(conn, owner).status == "todo"

            claimed = kb.claim_task(conn, dep)
            assert claimed is not None
            assert kb._record_task_failure(
                conn, dep, "pid not alive", outcome="crashed",
                failure_limit=1, release_claim=True, end_run=True,
            ) is True

            # Still notified, exactly as before...
            assert _events(conn, owner, "linked_task_gave_up")
            # ...but now on a card its owner can actually act on.
            woken = kb.get_task(conn, owner)
            assert woken.status == "blocked"
            assert woken.block_kind == "needs_input"

            evt = _events(conn, owner, "dependency_failure_needs_decision")
            assert len(evt) == 1
            assert evt[0][1]["task_id"] == dep
            assert evt[0][1]["from_status"] == "todo"
            assert evt[0][1]["from_block_kind"] == "dependency"

            # ``unblock`` is a real exit from that state — the governor has a
            # mechanical route to a recovery decision.
            assert kb.unblock_task(conn, owner) is True

    def test_running_dependent_is_not_disturbed(self, kanban_home):
        """The wake only touches cards that are genuinely parked."""
        with kb.connect_closing() as conn:
            dep = kb.create_task(conn, title="dep", assignee="default")
            owner = kb.create_task(conn, title="owner", assignee="erika")
            kb.link_tasks(conn, dep, owner)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'running', "
                    "claim_lock = 'host:1' WHERE id = ?", (owner,),
                )
            claimed = kb.claim_task(conn, dep)
            assert claimed is not None
            kb._record_task_failure(
                conn, dep, "boom", outcome="crashed", failure_limit=1,
                release_claim=True, end_run=True,
            )
            still = kb.get_task(conn, owner)
            assert still.status == "running"
            assert still.claim_lock == "host:1"
            assert _events(conn, owner, "dependency_failure_needs_decision") == []

    def test_completed_dependent_is_not_reopened(self, kanban_home):
        with kb.connect_closing() as conn:
            dep = kb.create_task(conn, title="dep", assignee="default")
            owner = kb.create_task(conn, title="owner", assignee="erika")
            kb.link_tasks(conn, dep, owner)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (owner,),
                )
            claimed = kb.claim_task(conn, dep)
            assert claimed is not None
            kb._record_task_failure(
                conn, dep, "boom", outcome="crashed", failure_limit=1,
                release_claim=True, end_run=True,
            )
            assert kb.get_task(conn, owner).status == "done"


# ---------------------------------------------------------------------------
# Defect 4 — independent verifier verdict return path
# ---------------------------------------------------------------------------


def _verifier_child(conn, subject_id, title="independent codex verification"):
    return kb.create_task(
        conn, title=title, assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
        parents=[subject_id],
    )


def _run_verifier(conn, subject_id, summary):
    """Dispatch and complete an independent verifier child. Returns its id."""
    cid = _verifier_child(conn, subject_id)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, cid).status == "ready"
    claimed = kb.claim_task(conn, cid)
    assert claimed is not None and claimed.status == "running"
    assert kb.complete_task(
        conn, cid, summary=summary, expected_run_id=claimed.current_run_id,
    ) is True
    return cid


class TestIndependentVerifierReturnPath:
    def test_pass_returns_a_verified_verdict_to_the_subject(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)

            cid = _run_verifier(
                conn, tid,
                "Reviewed the preserved evidence packet.\n\n**VERDICT: PASS**\n"
                "All 4 artefacts present; no rerun required.",
            )

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_VERIFIED

            returned = _events(conn, tid, "verifier_verdict_returned")
            assert len(returned) == 1
            assert returned[0][1]["verdict"] == "PASS"
            assert returned[0][1]["recorded"] is True
            assert returned[0][1]["verifier_task"] == cid

            # The verdict was attributed to the verifier, not the implementer.
            row = conn.execute(
                "SELECT verifier FROM task_verifications WHERE task_id = ? "
                "AND state = ? ORDER BY id DESC LIMIT 1",
                (tid, kb.VERIFICATION_VERIFIED),
            ).fetchone()
            assert row["verifier"] == f"{kb.EXECUTOR_LANE_CODEX_VERIFY}:{cid}"
            assert row["verifier"] != "default"

    def test_fail_returns_and_routes_the_subject_back_for_repair(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)

            _run_verifier(
                conn, tid,
                "VERDICT: FAIL\nProfile matrix omits 5 of 32 profiles.",
            )

            subject = kb.get_task(conn, tid)
            # Routed back for repair: the spent verdict leaves the head (see
            # TestFailedVerification in test_kanban_gauntlet_lifecycle) but the
            # ledger keeps it, and the task is nowhere near done.
            assert subject.status in ("ready", "todo")
            assert subject.verification_state is None
            assert kb.verification_history(conn, tid)[-1]["state"] == (
                kb.VERIFICATION_FAILED
            )
            assert len(_events(conn, tid, "verification_failed")) == 1
            returned = _events(conn, tid, "verifier_verdict_returned")
            assert returned[0][1]["verdict"] == "FAIL"
            assert returned[0][1]["recorded"] is True

    def test_blocker_writes_no_verdict_and_asks_for_a_decision(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)

            _run_verifier(
                conn, tid,
                "VERDICT: BLOCKER\nSandbox cannot read the attachment store.",
            )

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert subject.status == "review"
            blocker = _events(conn, tid, "verification_blocker_returned")
            assert len(blocker) == 1
            assert blocker[0][1]["verdict"] == "BLOCKER"
            assert _events(conn, tid, "verifier_verdict_returned") == []

    def test_missing_or_contradictory_verdict_fails_closed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            _run_verifier(conn, tid, "Looks fine to me, shipping it.")

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert len(_events(conn, tid, "verifier_verdict_unreadable")) == 1

        with kb.connect_closing() as conn:
            tid2 = _subject_awaiting_verification(conn, title="second subject")
            _self_review_refused(conn, tid2)
            _run_verifier(conn, tid2, "VERDICT: PASS\nlater...\nVERDICT: FAIL")
            assert (
                kb.get_task(conn, tid2).verification_state
                == kb.VERIFICATION_PENDING
            )
            assert len(_events(conn, tid2, "verifier_verdict_unreadable")) == 1

    def test_ordinary_task_completion_has_no_return_path(self, kanban_home):
        """Only the codex_verify lane returns verdicts to its parents."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            child = kb.create_task(
                conn, title="ordinary child", assignee="default",
                parents=[tid],
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ?", (child,)
                )
            claimed = kb.claim_task(conn, child)
            if claimed is not None:
                kb.complete_task(
                    conn, child, summary="VERDICT: PASS",
                    expected_run_id=claimed.current_run_id,
                )
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )
            assert _events(conn, tid, "verifier_verdict_returned") == []


# ---------------------------------------------------------------------------
# Defect 5 — the whole chain, end to end
# ---------------------------------------------------------------------------


class TestEndToEndRecovery:
    def test_refused_subject_recovers_through_the_independent_lane(
        self, kanban_home
    ):
        """Replay t_00690780 with the repair in place.

        Same inputs that produced ``gave_up`` at 2/2 and a non-governable
        dependent; the subject now reaches COMPLETED through an independent
        verdict, and the dependent is released by its dependency completing
        rather than by being told about a failure it could not act on.
        """
        with kb.connect_closing() as conn:
            owner = kb.create_task(
                conn, title="t_db0af7e0 shape", assignee="erika",
            )
            assert kb.claim_task(conn, owner) is not None
            tid = _subject_awaiting_verification(conn, title="t_00690780 shape")
            kb.link_tasks(conn, tid, owner)
            assert kb.block_task(
                conn, owner, reason="delegated", kind="dependency",
            ) is True

            # 1. self-review is refused, twice, and costs no retry budget
            _self_review_refused(conn, tid)
            assert kb.claim_review_task(conn, tid) is None
            assert kb.get_task(conn, tid).consecutive_failures == 0
            assert "gave_up" not in _kinds(conn, tid)

            # 2. an independent verifier is dispatchable off the preserved
            #    evidence, with the subject still un-verified
            cid = _verifier_child(conn, tid)
            kb.recompute_ready(conn)
            assert kb.get_task(conn, cid).status == "ready"
            assert kb.get_task(conn, tid).status == "review"

            # 3. its verdict returns to the subject automatically
            verifier_run = kb.claim_task(conn, cid)
            assert verifier_run is not None
            assert kb.complete_task(
                conn, cid,
                summary="VERDICT: PASS\nVerified against preserved artefacts.",
                expected_run_id=verifier_run.current_run_id,
            ) is True
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_VERIFIED
            )

            # 4. the subject can now legally complete
            assert kb.complete_task(
                conn, tid, result="audit evidence verified independently",
            ) is True
            assert kb.get_task(conn, tid).status == "done"

            # 5. and the dependent is governable again
            kb.recompute_ready(conn)
            owner_task = kb.get_task(conn, owner)
            assert owner_task.status in {"ready", "todo", "blocked"}
            assert kb._parents_satisfied(conn, owner) is True
            assert owner_task.status == "ready"
