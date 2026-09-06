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

Six separate defects are visible in those 12 lines, and each has a test here:

  0. ROUTING — ``request_review(reviewer=None)`` left the implementer as the
     review assignee, so the dispatcher selected it for its own review and the
     refusal at 9865 was manufactured rather than avoided. Re-reproduced on E2E
     governor t_cbc133db (2026-09-03 22:50) with ``assignee='erika'``, which is
     what this module's first class now pins: the independent lane is chosen at
     the handoff, BEFORE any same-identity claim is attempted;
  1. the same identity was re-selected for review after being refused;
  2. correct refusals were charged to the ordinary implementation retry budget
     and tripped the breaker at 2/2;
  3. the failure was written onto a dependent card that could not run, so its
     owner had no mechanical route to a recovery decision;
  4. the independent ``codex_verify`` lane had no return path — a verdict could
     be produced and the subject would never receive it;
  5. end to end, a subject whose implementation succeeded could not reach a
     verdict at all;
  6. GATE — one link earlier in the same return path, the verifier child's
     START gate keyed on terminal parent completion, so a subject that had
     produced a complete evidence packet and entered verification-pending
     still could not hand it to anyone. Verifier dependencies must key on
     evidence readiness / verification-pending, never on terminal parent
     completion; see the last section of this module.

Defect 0's repair changes how the later defects are REACHED, not whether their
guards still hold. The dispatcher can no longer put the implementer into a
review run of its own work, so the verdict-time refusal those guards feed on is
now reached the only way that survives: a review run held by an independent
reviewer that signs the verdict with the implementer's identity (a
``--verifier`` mistake, or a legacy board whose refusals predate this repair).
The guards themselves are unchanged and still pinned here.

Nothing here reruns or re-derives the original 55 KB audit matrix; the fixture
stands in for the evidence packet with a single attachment, which is all the
dependency gate and the verifier lane actually read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# D8 (defect packet t_db0af7e0): every path that writes ``tasks.assignee``
# now checks it against the profile registry, not just ``assign_task``. The
# identities below ("worker", "alice", "reviewer") are synthetic and are not
# profile directories on disk, so this module declares the registry-patching
# fixture that already exists for exactly that reason. The gate itself is
# pinned in test_kanban_assignee_validation.py.
pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


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


def _independent_review_run(conn, tid, *, reviewer="reviewer"):
    """Install an independent reviewer on the open phase and claim its run.

    The sanctioned recovery route (reclaim + assign, used live on t_4edb5874):
    an ``assigned`` event after the phase opened installs the identity as this
    phase's reviewer, which is what makes it independent of the implementer for
    the selection guard and the verdict gate alike.
    """
    assert kb.assign_task(conn, tid, reviewer) is True
    review = kb.claim_review_task(conn, tid)
    assert review is not None, "an independent reviewer must be able to claim"
    return review


def _self_review_refused(conn, tid, *, review=None, implementer="default"):
    """Replay one reviewer run refused for self-review, then crash it.

    Returns the reviewer run id. Events 9865-9873 with one substitution the
    routing repair forces: the run is opened by an independent reviewer rather
    than by the implementer, because the dispatcher can no longer select the
    implementer for its own review at all (see ``TestRoutingAtHandoff``). What
    is replayed unchanged is the part these guards read — a
    ``verification_blocked_self_review`` on the phase, followed by the reviewer
    run exiting non-zero into the crash path.
    """
    review = review if review is not None else _independent_review_run(conn, tid)
    ok, _reason = kb.record_verification(
        conn, tid, passed=True, verifier=implementer,
    )
    assert ok is False, "a verdict signed by the implementer must be refused"
    assert "verification_blocked_self_review" in _kinds(conn, tid)
    kb._record_task_failure(
        conn, tid, "pid 3325366 not alive",
        outcome="crashed", failure_limit=2,
        release_claim=True, end_run=True,
    )
    return review.current_run_id


# ---------------------------------------------------------------------------
# Defect 0 — routing chosen at the handoff, before any same-identity claim
# ---------------------------------------------------------------------------


class TestRoutingAtHandoff:
    """The E2E shape reproduced on t_cbc133db at 22:50.

    ``request_review`` with ``reviewer`` omitted — what every autonomous caller
    passes — left the implementer as the effective review assignee, and the
    review dispatcher then selected it for its own review. The refusal that
    followed was correct and useless: the loop had already been entered.
    """

    def test_implementer_is_never_selected_for_its_own_review(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn, assignee="erika")

            # The dispatcher's FIRST review tick, with nothing on record yet.
            assert kb.claim_review_task(conn, tid) is None

            # The distinguishing assertion: the guard that used to fire here
            # never had to. No same-identity claim was opened, so no verdict
            # was manufactured to be refused.
            assert "verification_blocked_self_review" not in _kinds(conn, tid)

            rejected = _events(conn, tid, "review_claim_rejected_self_review")
            assert len(rejected) == 1
            assert rejected[0][1]["candidate"] == "erika"
            assert rejected[0][1]["conflict_source"] == "implementer"
            assert rejected[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

            # Refused, not stranded: still parked in the lane an independent
            # verifier reads from, with its verdict still outstanding.
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert task.claim_lock is None

    def test_handoff_opens_the_independent_verifier_route(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn, assignee="erika")

            required = _events(conn, tid, "independent_verification_required")
            assert len(required) == 1
            assert required[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert required[0][1]["conflict_source"] == "implementer"
            assert required[0][1]["source"] == "request_review"

            created = _events(conn, tid, "independent_verifier_child_created")
            assert len(created) == 1
            cid = created[0][1]["verifier_task"]
            assert required[0][1]["verifier_task"] == cid

            # Actionable, not merely recorded: the child is in the independent
            # lane, parented to the subject, and already claimable off the
            # subject's evidence packet rather than waiting for a 'done' parent
            # the subject cannot reach without this verdict.
            child = kb.get_task(conn, cid)
            assert child.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert child.assignee == "default"
            assert kb.parent_ids(conn, cid) == [tid]
            assert child.status == "ready"

    def test_route_is_not_duplicated_across_ticks_or_phases(self, kanban_home):
        """One verifier child per open phase, never a second.

        The architecture already represents verifier work as a child, so a
        duplicate would put two independent verdicts on one phase.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn, assignee="erika")
            first = kb._open_verifier_child(conn, tid)
            assert first is not None

            # Re-request while the existing child is still live.
            assert kb.reopen_review_task(conn, tid) is True
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="resubmitted",
                expected_run_id=claimed.current_run_id,
            ) is True

            assert kb._open_verifier_child(conn, tid) == first
            assert len(_events(conn, tid, "independent_verifier_child_created")) == 1
            assert len(kb.child_ids(conn, tid)) == 1

    def test_explicit_independent_reviewer_is_left_alone(self, kanban_home):
        """``reviewer=`` was already a safe independent routing rule."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="explicitly routed", assignee="default",
                gauntlet=True,
            )
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="over to you",
                reviewer="reviewer",
                expected_run_id=claimed.current_run_id,
            ) is True

            assert _events(conn, tid, "independent_verifier_child_created") == []
            review = kb.claim_review_task(conn, tid)
            assert review is not None
            assert review.status == "running"

    def test_dispatcher_tick_spawns_the_verifier_not_the_self_review(
        self, kanban_home
    ):
        """The live loop, driven by the real dispatcher rather than by hand.

        Every tick on t_cbc133db spawned a reviewer worker for the subject
        under the implementer's own identity. The tick must now spawn the
        independent verifier child instead, and must not let the unclaimable
        review row consume the slot the review lane reserves for itself.
        """
        spawned: list[str] = []

        def fake_spawn(task, workspace, board=None):
            spawned.append(task.id)
            return 4242

        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn, assignee="erika")
            cid = _verifier_child(conn, tid)

            result = kb.dispatch_once(conn, spawn_fn=fake_spawn, max_spawn=4)

            assert cid in spawned
            assert tid not in spawned
            assert "verification_blocked_self_review" not in _kinds(conn, tid)
            assert kb.get_task(conn, tid).status == "review"
            assert result.skipped_locked is False


class TestOrdinaryReviewIsUnchanged:
    """``reviewer=None`` on a non-Gauntlet board keeps its existing behaviour.

    Pre-emption is scoped to Gauntlet-enforced subjects because only there is a
    same-identity verdict refused at verdict time. Applying it to an ordinary
    board would strand review work that has always been allowed to close.
    """

    def test_non_gauntlet_reviewer_none_still_claims_as_before(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="ordinary review", assignee="default",
                gauntlet=False,
            )
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="please review",
                expected_run_id=claimed.current_run_id,
            ) is True

            assert _events(conn, tid, "independent_verifier_child_created") == []
            assert _events(conn, tid, "independent_verification_required") == []
            review = kb.claim_review_task(conn, tid)
            assert review is not None
            assert review.status == "running"


# ---------------------------------------------------------------------------
# Defect 1 — repeated same-identity review selection
# ---------------------------------------------------------------------------


class TestSameIdentityReviewSelection:
    def test_refused_identity_cannot_claim_review_again(self, kanban_home):
        """The unconditional exclusion, which every board still gets.

        A refusal already on the ledger bars that identity from opening a
        review run for the phase, whatever put it back in the assignee seat —
        a legacy board's history, or a manual reassignment like the one below.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            assert kb.get_task(conn, tid).status == "review"

            assert kb.assign_task(conn, tid, "default") is True
            assert kb.claim_review_task(conn, tid) is None

            rejected = _events(conn, tid, "review_claim_rejected_self_review")
            assert len(rejected) == 1
            assert rejected[0][1]["candidate"] == "default"
            assert rejected[0][1]["conflict_source"] == "refused"
            assert rejected[0][1]["refused_identities"] == ["default"]
            assert rejected[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

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
            for _ in range(25):
                assert kb.claim_review_task(conn, tid) is None
            assert len(_events(conn, tid, "review_claim_rejected_self_review")) == 1

    def test_independent_identity_may_still_claim_the_review(self, kanban_home):
        """The exclusion is of one identity, not of the review lane."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            review = kb.claim_review_task(conn, tid)
            assert review is not None
            assert review.status == "running"

    def test_exclusion_is_scoped_to_the_current_verification_phase(
        self, kanban_home
    ):
        """A later phase starts clean.

        The refusal record must not outlive the phase it happened in: after
        ``request_changes`` routes the work back and the implementer
        re-submits, the previous phase's exclusions stop applying and an
        independent identity claims the new phase without inheriting them.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _self_review_refused(conn, tid)
            assert kb._self_review_refused_implementers(conn, tid) == {"default"}

            # Route back for repair, then hand off again -> new phase.
            assert kb.reopen_review_task(conn, tid) is True
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="repaired and resubmitted",
                expected_run_id=claimed.current_run_id,
            ) is True

            assert kb._self_review_refused_implementers(conn, tid) == set()
            assert _independent_review_run(conn, tid) is not None


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
            # Seeded after the reviewer is installed: ``assign_task`` clears
            # the streak on a genuine reassignment, which is its own behaviour
            # and not what this test is about.
            review = _independent_review_run(conn, tid)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = 1 WHERE id = ?",
                    (tid,),
                )
            _self_review_refused(conn, tid, review=review)
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
            review = _independent_review_run(conn, tid)
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


def _verifier_child(conn, subject_id):
    """The verifier child the handoff already opened for this subject.

    Deliberately does NOT create one: ``request_review`` opens the route, and a
    test that made its own would prove the return path against a card the
    dispatcher never produces.
    """
    cid = kb._open_verifier_child(conn, subject_id)
    assert cid is not None, "request_review must open the independent route"
    return cid


def _run_verifier(conn, subject_id, summary):
    """Dispatch and complete the independent verifier child. Returns its id."""
    cid = _verifier_child(conn, subject_id)
    assert kb.get_task(conn, cid).status == "ready"
    claimed = kb.claim_task(conn, cid)
    assert claimed is not None and claimed.status == "running"
    assert kb.complete_task(
        conn, cid, summary=summary, expected_run_id=claimed.current_run_id,
    ) is True
    return cid


class TestVerifiedReplayFinalization:
    def test_persisted_verified_review_finalizes_on_replay_and_releases_child(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            subject = _subject_awaiting_verification(
                conn, title="verified before gateway restart"
            )
            child = kb.create_task(
                conn, title="dependent after restart", assignee="default",
                parents=[subject],
            )
            # Simulate a PASS that was durably recorded by the old process,
            # before the auto-finalize actuator was loaded.
            ok, detail = kb.record_verification(
                conn, subject, passed=True, verifier="codex_verify:t_fixture",
                evidence={"source": "restart-boundary-fixture"},
                route_on_failure=False,
            )
            assert ok is True and detail == kb.VERIFICATION_VERIFIED
            before = kb.get_task(conn, subject)
            assert before.status == "review"
            assert before.verification_state == kb.VERIFICATION_VERIFIED
            assert kb.get_task(conn, child).status == "todo"

            assert kb.finalize_stranded_verified_reviews(conn) == 1

            after = kb.get_task(conn, subject)
            assert after.status == "done"
            assert after.terminal_disposition == kb.DISPOSITION_COMPLETED
            # complete_task performs the ordinary dependent recompute itself.
            assert kb.get_task(conn, child).status == "ready"


class TestIndependentVerifierReturnPath:
    def test_pass_returns_a_verified_verdict_to_the_subject(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            cid = _run_verifier(
                conn, tid,
                "Reviewed the preserved evidence packet.\n\n**VERDICT: PASS**\n"
                "All 4 artefacts present; no rerun required.",
            )

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_VERIFIED
            assert subject.status == "done"
            assert subject.terminal_disposition == kb.DISPOSITION_COMPLETED

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
            _run_verifier(conn, tid, "Looks fine to me, shipping it.")

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert len(_events(conn, tid, "verifier_verdict_unreadable")) == 1

        with kb.connect_closing() as conn:
            tid2 = _subject_awaiting_verification(conn, title="second subject")
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
    def test_subject_reaches_a_verdict_without_any_self_review_attempt(
        self, kanban_home
    ):
        """Replay t_00690780 / t_cbc133db with the routing repair in place.

        Same inputs that produced ``gave_up`` at 2/2 and a non-governable
        dependent; the implementer is never selected for its own review at all,
        the subject reaches COMPLETED through an independent verdict, and the
        dependent is released by its dependency completing rather than by being
        told about a failure it could not act on.
        """
        with kb.connect_closing() as conn:
            owner = kb.create_task(
                conn, title="t_db0af7e0 shape", assignee="erika",
            )
            assert kb.claim_task(conn, owner) is not None
            tid = _subject_awaiting_verification(
                conn, assignee="erika", title="t_00690780 shape",
            )
            kb.link_tasks(conn, tid, owner)
            assert kb.block_task(
                conn, owner, reason="delegated", kind="dependency",
            ) is True

            # 1. the implementer is refused at SELECTION, every tick, without
            #    ever opening a review run — so no verdict is manufactured to
            #    be refused, and no retry budget is spent
            for _ in range(3):
                assert kb.claim_review_task(conn, tid) is None
            assert "verification_blocked_self_review" not in _kinds(conn, tid)
            assert kb.get_task(conn, tid).consecutive_failures == 0
            assert "gave_up" not in _kinds(conn, tid)

            # 2. the independent verifier the handoff opened is dispatchable
            #    off the preserved evidence, with the subject still un-verified
            cid = _verifier_child(conn, tid)
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
            # 4. PASS is itself the final actuator: no model/human relay is
            #    needed to call complete_task after the durable verdict.
            assert kb.get_task(conn, tid).status == "done"

            # 5. and the dependent is governable again
            kb.recompute_ready(conn)
            owner_task = kb.get_task(conn, owner)
            assert owner_task.status in {"ready", "todo", "blocked"}
            assert kb._parents_satisfied(conn, owner) is True
            assert owner_task.status == "ready"


# ---------------------------------------------------------------------------
# Defect S1 — a refused review claim must OPEN the route it demands
#
# ``request_review`` opens the independent route at the handoff. That covers
# every card parked by the repaired code and nothing else. A subject can be
# sitting in ``review`` without ever having been through it:
#
#   * it was parked by a ``request_review`` that ran BEFORE the routing repair
#     existed — every such card on the live board is permanently in this shape,
#     and no future call will fix it because the handoff has already happened;
#   * it was blocked and then unblocked back into ``review``, which restores the
#     status without re-running the handoff.
#
# On those cards ``claim_review_task`` refused the same-identity claim on every
# dispatcher tick and left nothing that could ever produce a verdict. Observed
# live on t_cbc133db: ``review_claim_rejected_self_review`` at 23:31:20 on
# 2026-09-03 and then total silence, with its four delegated children gated
# behind it. The refusal is correct; being the END of the path was the defect.
# ---------------------------------------------------------------------------


def _legacy_parked_subject(conn, *, assignee="erika"):
    """A subject parked in ``review`` with NO independent route opened.

    Reproduces a card handed off by the pre-repair code. The handoff artefacts
    are removed rather than never created because current code cannot produce
    this state any more — which is precisely why the cards that already carry
    it can only be rescued from the claim path.
    """
    tid = _subject_awaiting_verification(conn, assignee=assignee)
    cid = kb._open_verifier_child(conn, tid)
    assert cid is not None
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_links WHERE child_id = ?", (cid,))
        conn.execute("DELETE FROM tasks WHERE id = ?", (cid,))
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind IN "
            "('independent_verification_required', "
            " 'independent_verifier_child_created')",
            (tid,),
        )
    assert kb._open_verifier_child(conn, tid) is None
    assert _events(conn, tid, "independent_verification_required") == []
    return tid


class TestRefusedClaimOpensTheRoute:
    def test_legacy_parked_subject_gets_a_verifier_on_refusal(
        self, kanban_home
    ):
        """The live t_cbc133db shape, end to end."""
        with kb.connect_closing() as conn:
            tid = _legacy_parked_subject(conn)

            assert kb.claim_review_task(conn, tid) is None

            # the refusal is still recorded, unchanged
            rejected = _events(conn, tid, "review_claim_rejected_self_review")
            assert len(rejected) == 1
            assert rejected[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

            # and it now also opened the route it demands
            required = _events(conn, tid, "independent_verification_required")
            assert len(required) == 1
            assert required[0][1]["source"] == "claim_review_task"
            assert required[0][1]["required_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            cid = required[0][1]["verifier_task"]
            assert cid is not None

            child = kb.get_task(conn, cid)
            assert child.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert kb.parent_ids(conn, cid) == [tid]
            # actionable on this tick, not merely recorded
            assert child.status == "ready"

            # the subject itself is untouched by the rescue
            subject = kb.get_task(conn, tid)
            assert subject.status == "review"
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert subject.claim_lock is None
            assert subject.consecutive_failures == 0

    def test_the_opened_route_actually_closes_the_loop(self, kanban_home):
        """A rescued subject reaches VERIFIED with no operator relay."""
        with kb.connect_closing() as conn:
            tid = _legacy_parked_subject(conn)
            assert kb.claim_review_task(conn, tid) is None

            _run_verifier(conn, tid, "VERDICT: PASS\nChecked the packet.")

            subject = kb.get_task(conn, tid)
            assert subject.verification_state == kb.VERIFICATION_VERIFIED
            assert subject.status == "done"

    def test_route_is_not_duplicated_by_repeated_refusals(self, kanban_home):
        """Every tick refuses; only the first tick may open a route."""
        with kb.connect_closing() as conn:
            tid = _legacy_parked_subject(conn)

            for _ in range(4):
                assert kb.claim_review_task(conn, tid) is None

            assert len(_events(conn, tid, "independent_verification_required")) == 1
            assert len(
                _events(conn, tid, "independent_verifier_child_created")
            ) == 1
            children = conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (tid,),
            ).fetchall()
            assert len(children) == 1

    def test_existing_live_verifier_is_reused_not_replaced(self, kanban_home):
        """A card parked by the repaired handoff already has its route."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn, assignee="erika")
            opened = kb._open_verifier_child(conn, tid)

            assert kb.claim_review_task(conn, tid) is None

            assert kb._open_verifier_child(conn, tid) == opened
            assert len(
                _events(conn, tid, "independent_verifier_child_created")
            ) == 1
            # the handoff's event stands; the claim path does not overwrite it
            required = _events(conn, tid, "independent_verification_required")
            assert len(required) == 1
            assert required[0][1]["source"] == "request_review"

    def test_non_gauntlet_refusal_opens_no_verifier(self, kanban_home):
        """The codex_verify child is a Gauntlet artefact, so it is Gauntlet-scoped.

        ``_review_claim_conflict`` answers "refused" for an unenforced card too,
        on the strength of a refusal already on its ledger. That must bar the
        claim without manufacturing a Gauntlet verifier on a board that never
        asked for one.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="unenforced", assignee="default", gauntlet=False,
            )
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            assert kb.request_review(
                conn, tid, summary="done",
                expected_run_id=claimed.current_run_id,
            ) is True
            with kb.write_txn(conn):
                kb._append_event(
                    conn, tid, "verification_blocked_self_review",
                    {"verifier": "default", "implementer": "default",
                     "conflict_identity": "default",
                     "conflict_source": "implementer"},
                )

            assert kb.claim_review_task(conn, tid) is None

            assert _events(conn, tid, "independent_verification_required") == []
            assert kb._open_verifier_child(conn, tid) is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_links WHERE parent_id = ?", (tid,),
            ).fetchone()[0] == 0

    def test_refusal_rescue_consumes_no_retry_budget(self, kanban_home):
        """Governance denial, not an implementation failure."""
        with kb.connect_closing() as conn:
            tid = _legacy_parked_subject(conn)
            before = kb.get_task(conn, tid)

            for _ in range(3):
                assert kb.claim_review_task(conn, tid) is None

            after = kb.get_task(conn, tid)
            assert after.consecutive_failures == before.consecutive_failures == 0
            assert "gave_up" not in _kinds(conn, tid)
            assert after.terminal_disposition is None


# ---------------------------------------------------------------------------
# Defect E3 — a child that cannot be dispatched is not a route
#
# The handoff opened the independent route for ANY conflicted subject, without
# asking whether that subject carried evidence the dependency gate would
# accept. For an evidence-less subject the gate then refuses to promote the
# child forever: the board shows an open verifier, the lifecycle shows nothing
# happening, and neither ever resolves. Observed live on t_cbc133db (zero rows
# in task_attachments, one pending verdict row, four delegated children gated
# behind it in todo, 78 minutes with no event of any kind).
#
# The evidence gate is NOT loosened here -- with nothing attached there is
# nothing falsifiable to verify. What changes is that the control plane stops
# manufacturing a card it will never run, and says so immediately instead of
# waiting out the four-hour staleness clock.
# ---------------------------------------------------------------------------


def _evidenceless_parked_subject(conn, *, assignee="default", title="subject"):
    """A gauntlet subject parked in ``review`` carrying NO attachment.

    The live t_cbc133db shape. A delegation card's deliverable is board state
    rather than a file, so it reaches the review lane with an empty evidence
    packet -- which is exactly the case the dispatch gate refuses.
    """
    tid = kb.create_task(conn, title=title, assignee=assignee, gauntlet=True)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.request_review(
        conn, tid, summary="delegated work complete; nothing to attach",
        expected_run_id=claimed.current_run_id,
    ) is True
    task = kb.get_task(conn, tid)
    assert task.status == "review"
    assert task.verification_state == kb.VERIFICATION_PENDING
    assert conn.execute(
        "SELECT COUNT(*) FROM task_attachments WHERE task_id = ?", (tid,),
    ).fetchone()[0] == 0
    return tid


class TestUndispatchableVerifierIsNeverCreated:
    def test_evidenceless_subject_gets_no_child_it_cannot_run(
        self, kanban_home
    ):
        """The defect itself: no permanently un-dispatchable verifier."""
        with kb.connect_closing() as conn:
            tid = _evidenceless_parked_subject(conn)

            assert kb._open_verifier_child(conn, tid) is None
            assert conn.execute(
                "SELECT COUNT(*) FROM task_links WHERE parent_id = ?", (tid,),
            ).fetchone()[0] == 0

    def test_it_says_so_immediately_rather_than_going_quiet(self, kanban_home):
        """The stall is announced at the handoff, not four hours later."""
        with kb.connect_closing() as conn:
            tid = _evidenceless_parked_subject(conn)

            assert _events(conn, tid, "independent_verification_unroutable")
            # route_supervisory_alarm puts its kind on the ledger whether or
            # not a delivery channel is configured -- that durable record is
            # the part a governor can act on.
            assert "INDEPENDENT_VERIFICATION_UNROUTABLE" in _kinds(conn, tid)

    def test_the_subject_stays_parked_and_unblessed(self, kanban_home):
        """Fails CLOSED. Nothing about this bypasses verification."""
        with kb.connect_closing() as conn:
            tid = _evidenceless_parked_subject(conn)

            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert task.terminal_disposition is None

    def test_no_retry_budget_is_consumed(self, kanban_home):
        """Unroutable is a governance outcome, not an implementation failure."""
        with kb.connect_closing() as conn:
            tid = _evidenceless_parked_subject(conn)

            for _ in range(3):
                assert kb.claim_review_task(conn, tid) is None

            after = kb.get_task(conn, tid)
            assert after.consecutive_failures == 0
            assert "gave_up" not in _kinds(conn, tid)

    def test_a_subject_with_evidence_still_gets_its_verifier(self, kanban_home):
        """The negative control. The ordinary route is untouched."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)

            cid = kb._open_verifier_child(conn, tid)
            assert cid is not None
            assert _events(conn, tid, "independent_verification_unroutable") == []

    def test_every_child_that_is_created_is_actually_dispatchable(
        self, kanban_home
    ):
        """The property the repair is really asserting.

        Not merely "sometimes we skip creation" but: if a verifier child
        exists, the dependency gate will promote it. This is what makes
        create-but-never-dispatch unrepresentable rather than merely rarer.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            cid = _verifier_child(conn, tid)

            assert kb._parents_satisfied(conn, cid) is True
            assert kb.get_task(conn, cid).status == "ready"
            claimed = kb.claim_task(conn, cid)
            assert claimed is not None and claimed.status == "running"

    def test_the_precheck_and_the_dispatch_gate_agree(self, kanban_home):
        """They are the same predicate; prove it on both answers.

        If these two ever drift the control plane silently resumes creating
        undispatchable children, so the agreement is pinned rather than
        trusted to the shared helper staying shared.
        """
        with kb.connect_closing() as conn:
            with_evidence = _subject_awaiting_verification(conn)
            without = _evidenceless_parked_subject(conn, title="no evidence")

            assert kb._subject_evidence_is_dispatchable(conn, with_evidence)
            assert not kb._subject_evidence_is_dispatchable(conn, without)

            cid = _verifier_child(conn, with_evidence)
            assert kb._parents_satisfied(conn, cid) is True


# ---------------------------------------------------------------------------
# The repair leg's PASS must return automatically too
#
# After a FAIL, regression_required is armed and the next PASS owes re-run
# proof. The return path had no way to supply it, so an automatic PASS on a
# repair leg was always refused -- with a message instructing a human to run
# `hermes kanban verify <id> --pass --regression-evidence '{...}'`. The
# lifecycle therefore closed itself on a first-pass verdict and demanded a
# copy/paste relay on every RECOVERED one, which is both the case the Gauntlet
# exists for and the point at which the run had already been hardest.
#
# The verifier re-ran the checks, so the verifier declares them, on an anchored
# line parsed exactly like the verdict. The gate is unchanged: these tests pin
# that a PASS with no declaration is still refused.
# ---------------------------------------------------------------------------


def _armed_for_repair(conn):
    """A subject whose first independent verdict was FAIL. regression armed."""
    tid = _subject_awaiting_verification(conn)
    _run_verifier(conn, tid, "VERDICT: FAIL")
    task = kb.get_task(conn, tid)
    assert task.regression_required is True
    return tid


def _repair_and_hand_off(conn, tid):
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    kb.add_attachment(
        conn, tid, filename="REPAIR-EVIDENCE.md",
        stored_path=f"/tmp/{tid}/REPAIR-EVIDENCE.md",
        size=2048, uploaded_by="claude-lane",
    )
    assert kb.request_review(
        conn, tid, summary="repaired", expected_run_id=claimed.current_run_id,
    ) is True


class TestRepairLegPassReturnsWithoutRelay:
    def test_declared_regression_closes_the_repair_leg(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _armed_for_repair(conn)
            _repair_and_hand_off(conn, tid)
            _run_verifier(
                conn, tid,
                "Re-ran the suite.\n"
                "REGRESSION: pytest -q tests/hermes_cli/ -> exit 0, 356 passed\n"
                "VERDICT: PASS",
            )

            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            assert task.regression_required is False
            assert task.status in ("done", "archived")

    def test_the_proof_is_durable_and_names_the_check(self, kanban_home):
        """A claim is not a proof. What was re-run is on the ledger."""
        with kb.connect_closing() as conn:
            tid = _armed_for_repair(conn)
            _repair_and_hand_off(conn, tid)
            _run_verifier(
                conn, tid,
                "REGRESSION: pytest -q tests/hermes_cli/ -> exit 0, 356 passed\n"
                "VERDICT: PASS",
            )

            rows = [
                r["evidence"] for r in conn.execute(
                    "SELECT evidence FROM task_verifications "
                    "WHERE task_id = ? AND kind = ?",
                    (tid, kb.LEDGER_KIND_REGRESSION),
                )
            ]
            assert rows, "the spent regression proof must stay on the ledger"
            assert "pytest -q tests/hermes_cli/" in rows[0]

    def test_a_pass_with_no_declaration_is_still_refused(self, kanban_home):
        """The gate is NOT loosened. This is the whole safety property."""
        with kb.connect_closing() as conn:
            tid = _armed_for_repair(conn)
            _repair_and_hand_off(conn, tid)
            _run_verifier(conn, tid, "Looks fine now.\n\nVERDICT: PASS")

            task = kb.get_task(conn, tid)
            assert task.verification_state != kb.VERIFICATION_VERIFIED
            assert task.regression_required is True
            assert task.status not in ("done", "archived")
            assert "verification_blocked_no_regression" in _kinds(conn, tid)

    def test_a_first_pass_verdict_owes_no_regression_proof(self, kanban_home):
        """Unchanged: nothing extra is demanded of work that never failed."""
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(conn, tid, "VERDICT: PASS")

            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            assert task.status in ("done", "archived")

    def test_a_failing_verdict_never_carries_regression_evidence(
        self, kanban_home
    ):
        """A FAIL declaring a re-run must not be refused for declaring it.

        record_verification rejects a failing verdict that arrives with
        regression evidence, so the return path must withhold it on FAIL
        rather than pass it through and turn a valid FAIL into a no-op.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(
                conn, tid,
                "REGRESSION: pytest -q tests/hermes_cli/ -> exit 1, 3 failed\n"
                "VERDICT: FAIL",
            )

            assert "verification_failed" in _kinds(conn, tid)
            assert kb.get_task(conn, tid).regression_required is True


class TestVerifierRegressionParsing:
    @pytest.mark.parametrize("text,expected", [
        ("REGRESSION: pytest -q", ["pytest -q"]),
        ("**REGRESSION:** pytest -q", ["pytest -q"]),
        ("  regression = pytest -q  ", ["pytest -q"]),
        ("> REGRESSION: pytest -q", ["pytest -q"]),
    ])
    def test_anchored_forms_codex_actually_emits(self, text, expected):
        assert kb._parse_verifier_regression(text) == {"commands": expected}

    def test_absent_declaration_is_none_not_an_empty_claim(self):
        assert kb._parse_verifier_regression("VERDICT: PASS") is None
        assert kb._parse_verifier_regression(None) is None
        assert kb._parse_verifier_regression("") is None

    def test_several_declarations_are_all_kept(self):
        parsed = kb._parse_verifier_regression(
            "REGRESSION: pytest -q tests/a.py\nREGRESSION: ruff check\n"
        )
        assert parsed == {"commands": ["pytest -q tests/a.py", "ruff check"]}

    def test_it_never_invents_a_check(self):
        """No declaration must never become a passing proof."""
        assert kb._parse_verifier_regression("REGRESSION:") is None
        assert kb._parse_verifier_regression("REGRESSION:   ") is None


# ---------------------------------------------------------------------------
# Defect 6 — the verifier's START gate keyed on terminal parent completion
#
# The return path above only matters if the verifier can begin. Ordinary
# dependency gating demanded a terminal ('done'/'archived') parent, which is
# circular for a gauntlet-enforced subject: completion requires an independent
# VERIFIED verdict, the verdict requires an independent verifier, and the
# verifier could not start until the parent had completed. Every gate behaved
# correctly and the chain still could not terminate. Observed live on
# t_cda2da6b -- 18-artefact evidence packet, verification_state='pending',
# parked after a correctly refused same-identity review -- whose codex_verify
# child t_0c9e2a80 sat with nothing in the system able to promote it.
#
# The durable rule this section pins, stated once so a later refactor cannot
# lose it: **a verifier child's start gate keys on the parent's evidence
# readiness (verification-pending + an open ledger phase + an evidence
# packet), never on terminal parent completion.** The mechanism lives in
# ``_EVIDENCE_READY_PARENT_SQL``; ``TestEvidenceReadyVerifierDependency`` in
# test_kanban_gauntlet_lifecycle.py pins the predicate's own edges. What is
# pinned HERE is the property this return path depends on: the deadlock class
# cannot reappear one link upstream of the verdict route, and the carve-out
# stays lane-scoped so nothing else is released early.
# ---------------------------------------------------------------------------


class TestVerifierGateKeysOnEvidenceNotCompletion:
    def test_verifier_starts_while_the_subject_is_still_pending(
        self, kanban_home
    ):
        """The t_cda2da6b / t_0c9e2a80 shape on this module's fixtures.

        The verifier becomes dispatchable and the subject is NOT dragged
        terminal to make that happen -- it is still awaiting the verdict the
        verifier exists to produce.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            cid = _verifier_child(conn, tid)

            assert kb._parents_satisfied(conn, cid) is True
            kb.recompute_ready(conn)
            assert kb.get_task(conn, cid).status == "ready"

            subject = kb.get_task(conn, tid)
            assert subject.status == "review"
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert subject.completed_at is None
            assert subject.terminal_disposition is None

            # ...and it actually runs: promotion the claim invariant would
            # demote straight back is not a route.
            claimed = kb.claim_task(conn, cid)
            assert claimed is not None and claimed.status == "running"
            assert _events(conn, cid, "claim_rejected") == []

    def test_an_ordinary_child_of_the_same_subject_still_waits(
        self, kanban_home
    ):
        """The carve-out is lane-scoped, not a general loosening.

        Same evidence-ready subject, two children. Only the ``codex_verify``
        one moves; the ordinary one waits for genuine terminal completion and
        is released only by the verdict.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            verifier = _verifier_child(conn, tid)
            ordinary = kb.create_task(
                conn, title="follow-on implementation", assignee="default",
                parents=[tid],
            )

            kb.recompute_ready(conn)
            assert kb.get_task(conn, verifier).status == "ready"
            assert kb._parents_satisfied(conn, ordinary) is False
            assert kb.get_task(conn, ordinary).status == "todo"

            # The verdict -- not the carve-out -- is what frees it.
            _run_verifier(
                conn, tid,
                summary="VERDICT: PASS\nVerified against the evidence packet.",
            )
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_VERIFIED
            )
            assert kb.get_task(conn, tid).status == "done"
            kb.recompute_ready(conn)
            assert kb.get_task(conn, ordinary).status == "ready"

    def test_evidence_is_what_opens_the_gate_not_the_pending_state(
        self, kanban_home
    ):
        """Fail-closed control. Verification-pending alone is not enough.

        An evidence-less subject in the identical review/pending state leaves
        a verifier child gated, so the gate is keyed on something falsifiable
        rather than merely on the parent having left the running lane.
        """
        with kb.connect_closing() as conn:
            tid = _evidenceless_parked_subject(conn, title="nothing attached")
            cid = kb.create_task(
                conn, title="independent codex verification",
                assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
                parents=[tid], gauntlet=True,
            )

            assert kb._parents_satisfied(conn, cid) is False
            kb.recompute_ready(conn)
            assert kb.get_task(conn, cid).status == "todo"
            assert kb.claim_task(conn, cid) is None

    def test_completion_and_independence_gates_are_untouched(
        self, kanban_home
    ):
        """Nothing about an eligible verifier weakens the two hard gates.

        The subject still cannot go EXECUTING -> COMPLETED on its own, and a
        same-identity verdict is still refused, while its verifier is running.
        """
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            cid = _verifier_child(conn, tid)
            kb.recompute_ready(conn)
            assert kb.claim_task(conn, cid) is not None

            # The completion gate refuses loudly rather than returning a
            # falsy value a caller could ignore, so pin the raise itself.
            with pytest.raises(kb.VerificationRequiredError):
                kb.complete_task(conn, tid, summary="done anyway")
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="default",
            )
            assert ok is False
            assert "self" in detail.lower() or "independent" in detail.lower()

            subject = kb.get_task(conn, tid)
            assert subject.status == "review"
            assert subject.verification_state == kb.VERIFICATION_PENDING
