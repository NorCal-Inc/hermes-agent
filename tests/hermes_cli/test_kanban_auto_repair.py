"""Breaker trip creates a repair card instead of a human hold (t_bf453ced).

Part 1/3 of the t_5d7e889e kernel decomposition. Christopher's directive is
explicit and has no attempt cap: an ORDINARY task failure that trips the
circuit breaker in ``_record_task_failure`` must create a governed repair
card (via ``create_repair_task``, on the ``claude_recovery`` executor lane so
the existing recovery lane in ``hermes_cli.recovery_lane`` runs it) and park
the subject in ``todo`` behind it -- never ``blocked``/``needs_input`` for a
human to triage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, task_id, kind=None):
    rows = kb.list_events(conn, task_id)
    if kind is None:
        return rows
    return [r for r in rows if r.kind == kind]


def _running_task(conn, assignee="default"):
    tid = kb.create_task(conn, title="ordinary work that keeps failing", assignee=assignee)
    claimed = kb.claim_task(conn, tid, claimer=assignee)
    assert claimed is not None
    return tid


class TestBreakerTripCreatesRepair:
    def test_trip_creates_linked_repair_not_needs_input(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)

            blocked = kb._record_task_failure(
                conn, tid, "boom: subprocess exited 1",
                outcome="crashed", failure_limit=1,
            )
            assert blocked is True

            task = kb.get_task(conn, tid)
            # Never a human hold for an ordinary failure.
            assert task.block_kind != "needs_input"
            # Parked in todo, gated on the repair, not left 'blocked'.
            assert task.status == "todo"

            incoming_repairs = kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )
            assert len(incoming_repairs) == 1
            repair_id = incoming_repairs[0]["from_task_id"]

            repair = kb.get_task(conn, repair_id)
            assert repair.executor_lane == kb.EXECUTOR_LANE_CLAUDE_RECOVERY
            assert repair.recovery_owner == "default"
            assert kb.missing_repair_relations(conn, repair_id) == []
            assert kb.relation_targets(conn, repair_id, kb.RELATION_REPAIRS) == [tid]

            # Dependency edge actually gates the subject on the repair.
            assert repair_id in kb.parent_ids(conn, tid)

            started = _events(conn, tid, "repair_started")
            assert len(started) == 1
            payload = started[0].payload
            assert payload["repair_id"] == repair_id
            assert payload["subject_id"] == tid
            assert payload["attempt"] == 1
            assert "boom" in payload["cause"]

            # History is preserved: gave_up still fires exactly as before.
            assert len(_events(conn, tid, "gave_up")) == 1
            # But the human-decision comment is gone.
            comments = kb.list_comments(conn, tid)
            assert not any("human decision" in c.body for c in comments)
            assert not any(
                e.kind == "repair_start_failed" for e in _events(conn, tid)
            )

    def test_second_trip_on_same_subject_increments_attempt(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            kb._record_task_failure(
                conn, tid, "first failure",
                outcome="crashed", failure_limit=1,
            )
            first_repairs = kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )
            assert len(first_repairs) == 1

            # Simulate the subject tripping again (e.g. requeued and failed
            # once more) -- no attempt cap per Christopher's directive.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='running' WHERE id=?", (tid,)
                )
            kb._record_task_failure(
                conn, tid, "second failure",
                outcome="crashed", failure_limit=1,
            )

            second_repairs = kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )
            assert len(second_repairs) == 2

            started = _events(conn, tid, "repair_started")
            assert [s.payload["attempt"] for s in started] == [1, 2]


class TestRecoveryLinkageFailureLeavesSubjectUntouched:
    def test_linkage_error_emits_repair_start_failed_and_preserves_state(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)

            def _boom(*a, **k):
                raise kb.RecoveryLinkageError("forced: subject vanished mid-link")

            monkeypatch.setattr(kb, "create_repair_task", _boom)

            blocked = kb._record_task_failure(
                conn, tid, "boom again",
                outcome="crashed", failure_limit=1,
            )
            assert blocked is True

            task = kb.get_task(conn, tid)
            # Prior state (set by the trip branch itself) is left exactly as
            # it was -- no repair, no needs_input human hold either.
            assert task.status == "blocked"
            assert task.block_kind != "needs_input"
            assert kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            ) == []

            failed = _events(conn, tid, "repair_start_failed")
            assert len(failed) == 1
            assert failed[0].payload["subject_id"] == tid
            assert "forced" in failed[0].payload["error"]
            assert _events(conn, tid, "repair_started") == []


class TestRepairCompletionResumesSubject:
    """Part 2/3 (t_7b99f87c): completing the repair resumes the subject."""

    @staticmethod
    def _drive_repair_to_done(conn, repair_id):
        """Full Gauntlet chain: claim -> request_review -> independent PASS.

        ``create_repair_task`` always sets ``gauntlet_enforced=True`` on a
        governed repair card (the ``declares_repair`` branch in
        ``create_task``), so a bare ``complete_task`` is refused until the
        card has gone through review and collected a VERIFIED verdict from
        an identity independent of its own assignee/implementer.
        """
        claimed = kb.claim_task(conn, repair_id)
        assert claimed is not None and claimed.status == "running"
        ok = kb.request_review(
            conn, repair_id, summary="Repair complete: fixed the subject.",
            reviewer="independent-reviewer",
            expected_run_id=claimed.current_run_id,
        )
        assert ok is True
        ok, detail = kb.record_verification(
            conn, repair_id, passed=True, verifier="independent-reviewer",
            evidence={"command": "pytest -q", "exit_code": 0},
        )
        assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
        ok = kb.complete_task(
            conn, repair_id, summary="Repair complete: fixed the subject.",
        )
        assert ok is True

    def test_completing_repair_moves_subject_todo_to_ready_and_emits_notice(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)

            blocked = kb._record_task_failure(
                conn, tid, "boom: subprocess exited 1",
                outcome="crashed", failure_limit=1,
            )
            assert blocked is True

            subject = kb.get_task(conn, tid)
            assert subject.status == "todo"

            incoming_repairs = kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )
            assert len(incoming_repairs) == 1
            repair_id = incoming_repairs[0]["from_task_id"]

            self._drive_repair_to_done(conn, repair_id)

            subject_after = kb.get_task(conn, tid)
            assert subject_after.status == "ready"

            resumed = _events(conn, tid, "repair_completed_subject_resumed")
            assert len(resumed) == 1
            payload = resumed[0].payload
            assert payload["repair_id"] == repair_id
            assert payload["subject_id"] == tid
            assert payload["resumed_status"] == "ready"

    def test_completing_repair_when_subject_still_gated_emits_no_notice(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)

            kb._record_task_failure(
                conn, tid, "boom: subprocess exited 1",
                outcome="crashed", failure_limit=1,
            )
            incoming_repairs = kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )
            repair_id = incoming_repairs[0]["from_task_id"]

            # Add a second, unrelated parent dependency that is NOT done, so
            # the subject stays gated even after the repair completes.
            other_parent = kb.create_task(
                conn, title="unrelated blocking dependency", assignee="default",
            )
            kb.link_tasks(conn, other_parent, tid)

            self._drive_repair_to_done(conn, repair_id)

            subject_after = kb.get_task(conn, tid)
            # Still gated by the other undone parent -- must not have been
            # force-promoted, and no false "resumed" notice should fire.
            assert subject_after.status == "todo"
            assert _events(conn, tid, "repair_completed_subject_resumed") == []


# ---------------------------------------------------------------------------
# Part 3/3 (t_6450901b): repeated verifier FAIL creates a repair card, and a
# repair that records a change releases exactly one new verifier.
# ---------------------------------------------------------------------------

from tests.hermes_cli.test_kanban_self_review_recovery import (  # noqa: E402
    _repair_and_hand_off,
    _run_verifier,
    _subject_awaiting_verification,
)

_FAIL = "Workstation evidence file missing.\n\nVERDICT: FAIL"


def _open_verifiers(conn, tid):
    return [
        r["id"] for r in conn.execute(
            "SELECT c.id FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? AND c.executor_lane = ? "
            "AND c.status NOT IN ('done', 'archived')",
            (tid, kb.EXECUTOR_LANE_CODEX_VERIFY),
        ).fetchall()
    ]


def _open_repair(conn, tid):
    for rel in kb.task_relations(
        conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
    ):
        if kb.get_task(conn, rel["from_task_id"]).status not in ("done", "archived"):
            return rel["from_task_id"]
    return None


def _no_needs_input(conn, *ids):
    for i in ids:
        assert kb.get_task(conn, i).block_kind != "needs_input"


@pytest.fixture
def _advisory_budget(monkeypatch):
    monkeypatch.setattr(kb, "objective_attempt_budget_enforced", lambda: False)


def _subject_failed_twice(conn):
    tid = _subject_awaiting_verification(conn)
    _run_verifier(conn, tid, _FAIL)
    _repair_and_hand_off(conn, tid)
    _run_verifier(conn, tid, _FAIL)
    return tid


@pytest.mark.usefixtures("_advisory_budget")
class TestVerifierFailCreatesRepair:
    """Part 3/3 (t_6450901b)."""

    def test_two_fails_create_linked_repair_not_needs_input(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_failed_twice(conn)
            repair = _open_repair(conn, tid)
            assert repair is not None
            assert tid in kb.relation_targets(conn, repair, kb.RELATION_REPAIRS)
            assert repair in kb.parent_ids(conn, tid)
            started = _events(conn, tid, "repair_started")
            assert len(started) == 1
            assert started[0].payload["repair_id"] == repair
            assert started[0].payload["trigger"] == "verification_failed"
            assert "Workstation evidence file missing" in kb.get_task(conn, repair).body
            assert kb.get_task(conn, tid).status == "review"
            # More ticks never duplicate the repair.
            for _ in range(5):
                kb._ensure_independent_verifier_child(conn, tid, implementer="default")
            assert len(kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            )) == 1
            _no_needs_input(conn, tid, repair)

    def test_repair_with_change_releases_one_claimable_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_failed_twice(conn)
            repair = _open_repair(conn, tid)
            assert _open_verifiers(conn, tid) == []
            TestRepairCompletionResumesSubject._drive_repair_to_done(conn, repair)

            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_PENDING
            released = _events(conn, tid, "verifier_released_after_repair")
            assert len(released) == 1
            assert released[0].payload["repair_id"] == repair
            verifiers = _open_verifiers(conn, tid)
            assert len(verifiers) == 1
            assert kb._repeated_fail_anchor(conn, tid) is None
            # Claimable: the dependency gate lets it through.
            assert kb.get_task(conn, verifiers[0]).status == "ready"
            claimed = kb.claim_task(conn, verifiers[0])
            assert claimed is not None and claimed.status == "running"
            _no_needs_input(conn, tid, repair)

    def test_identical_change_starts_new_repair_not_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_failed_twice(conn)
            first = _open_repair(conn, tid)
            TestRepairCompletionResumesSubject._drive_repair_to_done(conn, first)
            # The released verifier fails again, twice -> next repair.
            _run_verifier(conn, tid, _FAIL)
            _repair_and_hand_off(conn, tid)
            _run_verifier(conn, tid, _FAIL)
            second = _open_repair(conn, tid)
            assert second is not None and second != first
            # Same summary text as the first repair -> identical change.
            TestRepairCompletionResumesSubject._drive_repair_to_done(conn, second)

            assert len(_events(conn, tid, "verifier_released_after_repair")) == 1
            assert len(_events(conn, tid, "repair_needs_new_approach")) == 1
            assert _open_verifiers(conn, tid) == []
            third = _open_repair(conn, tid)
            assert third is not None and third not in (first, second)
            assert kb.get_task(conn, third).body.startswith(
                kb._REPAIR_NEW_APPROACH_PREFIX
            )
            _no_needs_input(conn, tid, first, second, third)

    def test_empty_change_starts_new_repair_not_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_failed_twice(conn)
            repair = _open_repair(conn, tid)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "repair_change_recorded", {"fingerprint": "x"})
            assert kb._release_verifier_after_repair(
                conn, tid, repair_id=repair, change_text="   ",
            ) is None
            assert _events(conn, tid, "verifier_released_after_repair") == []
            assert len(_events(conn, tid, "repair_needs_new_approach")) == 1
            assert kb.get_task(conn, tid).verification_state == kb.VERIFICATION_FAILED
            assert _open_verifiers(conn, tid) == []
            _no_needs_input(conn, tid)

    def test_repair_link_failure_records_event_not_needs_input(
        self, kanban_home, monkeypatch
    ):
        def _boom(*a, **k):
            raise kb.RecoveryLinkageError("forced link failure")

        monkeypatch.setattr(kb, "create_repair_task", _boom)
        with kb.connect_closing() as conn:
            tid = _subject_failed_twice(conn)
            assert _open_repair(conn, tid) is None
            failed = _events(conn, tid, "repair_start_failed")
            assert len(failed) == 1 and failed[0].payload["trigger"] == "verification_failed"
            _no_needs_input(conn, tid)
