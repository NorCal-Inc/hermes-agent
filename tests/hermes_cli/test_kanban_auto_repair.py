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
            # Decision B: every auto-repair is Erika's, under the ONE durable
            # "Operational Error Repairs" umbrella.
            assert repair.recovery_owner == "erika"
            [umbrella_id] = kb.relation_targets(conn, repair_id, kb.RELATION_UMBRELLA)
            umbrella = kb.get_task(conn, umbrella_id)
            assert umbrella.title == "Operational Error Repairs"
            assert umbrella.status == "done"
            # Decision D: a real gate, not the no-op 'true'.
            assert repair.recovery_gate_cmd != "true"
            assert repair.recovery_gate_cmd.endswith(f"kanban repair-gate {tid}")
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

            # Decision C: no gave_up event/state for an ordinary failure.
            assert _events(conn, tid, "gave_up") == []
            assert len(_events(conn, tid, "failure_routed_to_repair")) == 1
            # But the human-decision comment is gone.
            comments = kb.list_comments(conn, tid)
            assert not any("human decision" in c.body for c in comments)
            assert not any(
                e.kind == "repair_start_failed" for e in _events(conn, tid)
            )

    def test_second_and_third_trips_each_open_a_fresh_repair(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            repair_ids = []
            for error in ("first failure", "second failure", "second failure"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status='running' WHERE id=?", (tid,)
                    )
                kb._record_task_failure(
                    conn, tid, error, outcome="crashed", failure_limit=1,
                )
                [repair_id] = [
                    r for r in kb._repair_ids_for_subject(conn, tid)
                    if r not in repair_ids
                ]
                repair_ids.append(repair_id)
                # Real repair cards cannot close without a recorded change.
                kb.record_auto_repair(
                    conn, repair_id, root_cause="r",
                    change=f"change {len(repair_ids)}", check="c",
                )
                # The repair ran and closed; the subject reran and failed again.
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET status='done' WHERE id=?", (repair_id,)
                    )

            assert len(set(repair_ids)) == 3
            started = _events(conn, tid, "repair_started")
            assert [s.payload["attempt"] for s in started] == [1, 2, 3]
            assert [s.payload["same_error_as_previous"] for s in started] == [
                False, False, True,
            ]
            umbrellas = {
                kb.relation_targets(conn, r, kb.RELATION_UMBRELLA)[0]
                for r in repair_ids
            }
            assert len(umbrellas) == 1
            # The repeated fingerprint is flagged; the fresh one is not.
            assert "SAME ERROR AGAIN" in kb.get_task(conn, repair_ids[2]).body
            assert "SAME ERROR AGAIN" not in kb.get_task(conn, repair_ids[1]).body
            assert _events(conn, tid, "gave_up") == []
            assert kb.get_task(conn, tid).block_kind != "needs_input"

    def test_trip_does_not_push_dependents_to_needs_input(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            dep = kb.create_task(
                conn, title="waits on the failing task", assignee="default",
                parents=[tid],
            )
            kb._record_task_failure(
                conn, tid, "boom", outcome="crashed", failure_limit=1,
            )
            dep_task = kb.get_task(conn, dep)
            assert dep_task.status == "todo"
            assert dep_task.block_kind != "needs_input"
            assert _events(conn, dep, "linked_task_gave_up") == []
            assert not any(
                "human decision" in c.body for c in kb.list_comments(conn, dep)
            )


class TestOperationalUmbrella:
    def test_umbrella_is_created_once(self, kanban_home):
        with kb.connect_closing() as conn:
            first = kb.ensure_operational_repairs_umbrella(conn)
            second = kb.ensure_operational_repairs_umbrella(conn)
            assert first == second
            rows = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = ?",
                ("Operational Error Repairs",),
            ).fetchone()[0]
            assert rows == 1


class TestRepairGate:
    def _tripped(self, conn, error="boom"):
        tid = _running_task(conn)
        kb._record_task_failure(conn, tid, error, outcome="crashed", failure_limit=1)
        return tid, kb.open_repair_for_subject(conn, tid)

    def test_gate_red_until_a_repair_is_recorded(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, repair_id = self._tripped(conn)
            ok, reason = kb.auto_repair_gate(conn, tid)
            assert not ok and "no repair recorded" in reason
            with pytest.raises(ValueError):
                kb.record_auto_repair(conn, tid, root_cause="x", change="", check="y")
            assert kb.record_auto_repair(
                conn, tid, root_cause="stale lock", change="clear lock on start",
                check="reran the job",
            ) == repair_id
            ok, reason = kb.auto_repair_gate(conn, tid)
            assert ok, reason

    def test_gate_red_when_change_repeats_previous_repair(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            kb.record_auto_repair(
                conn, first, root_cause="a", change="Clear the lock", check="c",
            )
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
            kb._record_task_failure(conn, tid, "boom", outcome="crashed", failure_limit=1)
            second = kb.open_repair_for_subject(conn, tid)
            assert second and second != first
            assert "Clear the lock" in kb.get_task(conn, second).body
            kb.record_auto_repair(
                conn, second, root_cause="a", change="clear  the LOCK", check="c",
            )
            ok, reason = kb.auto_repair_gate(conn, tid)
            assert not ok and "same as previous repair" in reason
            kb.record_auto_repair(
                conn, second, root_cause="lock never released",
                change="release the lock in the finally block", check="c",
            )
            ok, _ = kb.auto_repair_gate(conn, tid)
            assert ok

    def test_red_gate_after_both_attempts_opens_next_repair_not_human(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            kb.record_auto_repair(
                conn, first, root_cause="a", change="clear the lock", check="c",
            )
            nxt = kb.supersede_auto_repair(conn, first, reason="gate still red")
            assert nxt and nxt != first
            assert kb.get_task(conn, first).status == "archived"
            subject = kb.get_task(conn, tid)
            # Never released in between: still parked, now behind the new card.
            assert subject.status == "todo"
            assert nxt in kb.parent_ids(conn, tid)
            assert first not in kb.parent_ids(conn, tid)
            assert kb.get_task(conn, nxt).block_kind != "needs_input"
            started = _events(conn, tid, "repair_started")
            assert [e.payload["attempt"] for e in started] == [1, 2]
            assert kb._REPAIR_NEW_APPROACH_PREFIX in kb.get_task(conn, nxt).body

    def test_supersede_ignores_cards_that_are_not_auto_repairs(self, kanban_home):
        with kb.connect_closing() as conn:
            other = kb.create_task(conn, title="plain card", assignee="default")
            assert kb.supersede_auto_repair(conn, other, reason="x") is None


class TestRepairLoopNoNesting:
    """2026-10-23: 26 nested "Repair: Repair: ..." cards on t_cd5937cc."""

    def _tripped(self, conn):
        tid = _running_task(conn)
        kb._record_task_failure(conn, tid, "boom", outcome="crashed", failure_limit=1)
        return tid, kb.open_repair_for_subject(conn, tid)

    def test_failing_repair_card_is_superseded_not_repaired(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            kb.record_auto_repair(
                conn, first, root_cause="a", change="clear the lock", check="c",
            )
            nxt = kb._start_verifier_fail_repair(conn, first, "verifier: still broken")
            assert nxt and nxt != first
            assert kb.relation_targets(conn, nxt, kb.RELATION_REPAIRS) == [tid]
            assert kb._repair_ids_for_subject(conn, first) == []
            assert kb.get_task(conn, first).status == "archived"
            assert not kb.get_task(conn, nxt).title.startswith("Repair: Repair:")
            started = _events(conn, tid, "repair_started")
            assert [e.payload["attempt"] for e in started] == [1, 2]

    def test_repair_with_no_change_goes_to_diagnosis_not_new_card(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            out = kb._start_verifier_fail_repair(conn, first, "verifier: still broken")
            assert out is None
            assert kb.open_repair_for_subject(conn, tid) is None
            assert len(kb._repair_ids_for_subject(conn, tid)) == 1
            assert kb.get_task(conn, first).status == "archived"
            assert first not in kb.parent_ids(conn, tid)
            subject = kb.get_task(conn, tid)
            assert subject.status == "triage"
            assert kb.block_loop_repair_pending(conn, tid)

    def test_supersede_with_no_change_reports_routed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            assert kb.supersede_auto_repair(conn, first, reason="gate red") == tid
            assert kb.get_task(conn, tid).status == "triage"

    def test_diagnosis_exit_is_loop_repair(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            kb._start_verifier_fail_repair(conn, first, "still broken")
            with pytest.raises(kb.BlockLoopRepairRequired):
                kb.specify_triage_task(conn, tid, author="claude")
            kb.record_block_loop_repair(
                conn, tid, root_cause="r", change="c", check="k", author="claude",
            )
            assert kb.specify_triage_task(conn, tid, author="claude")
            assert kb.get_task(conn, tid).status in ("todo", "ready")

    def test_gate_green_when_subject_reconciled_overtaken(self, kanban_home):
        # t_0adeb90a: the repair closed its subject as overtaken; gate stayed red.
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            ok, why = kb.reconcile_overtaken_by_events(
                conn, tid, actor="claude", reason="superseded",
                evidence="other card", superseded_by=None,
            )
            assert ok, why
            ok, reason = kb.auto_repair_gate(conn, tid)
            assert ok and "overtaken" in reason

    def test_after_loop_repair_next_failure_gets_a_repair_card(self, kanban_home):
        # Review finding 1: diagnosis must not switch the repair lane off for good.
        with kb.connect_closing() as conn:
            tid, first = self._tripped(conn)
            kb._start_verifier_fail_repair(conn, first, "still broken")
            kb.record_block_loop_repair(
                conn, tid, root_cause="r", change="new fix", check="k", author="claude",
            )
            kb.specify_triage_task(conn, tid, author="claude")
            assert kb._last_repair_brought_new_change(conn, tid)
            nxt = kb._open_auto_repair(conn, tid, cause="fails again", trigger="t")
            assert nxt and nxt != first
            assert kb.get_task(conn, tid).status != "triage"

    def test_no_loop_flag_when_subject_is_running(self, kanban_home):
        # Review finding 2: a claimed subject is not routed, so no flag is left.
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            assert kb._route_repair_loop_to_diagnosis(
                conn, tid, cause="x", trigger="t",
            ) is False
            assert not kb.block_loop_repair_pending(conn, tid)
            assert kb.get_task(conn, tid).status == "running"


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
            (tid, kb.DEFAULT_INDEPENDENT_VERIFIER_LANE),
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
            # 2026-10-23: an identical change is no new evidence, so the
            # subject goes to diagnosis (triage + loop-repair), not a third card.
            assert _open_repair(conn, tid) is None
            assert kb.get_task(conn, tid).status == "triage"
            assert kb.block_loop_repair_pending(conn, tid)
            _no_needs_input(conn, tid, first, second)

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


class TestNoRepairAgainstTerminalSubject:
    """t_ae87c65e root-cause fix: a subject already closed with an
    IRREVERSIBLE_DISPOSITIONS terminal state (e.g. OVERTAKEN_BY_EVENTS) must
    never get a fresh repair card opened against it.

    Before this fix, ``_open_auto_repair`` only checked for an existing open
    repair and whether the previous repair brought a new change -- it never
    checked whether the subject itself was already terminally closed. A
    repair opened against an archived/OVERTAKEN_BY_EVENTS subject is
    unwinnable by construction: ``repair-gate`` refuses to resume an
    archived subject, so the repair card can only ever end in "recovery
    attempts exhausted; escalated for human review" even though the
    underlying decision was already made and recorded as the disposition
    itself (live incident: t_0adeb90a/t_68332405 nested against archived
    t_1ee44487, 2026-10-23).
    """

    def test_open_auto_repair_skips_terminal_subject(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            assert kb.block_task(
                conn, tid, reason="obsolete", kind="needs_input",
            )
            ok, reason = kb.reconcile_overtaken_by_events(
                conn, tid, actor="erika", reason="superseded",
                evidence="superseded by a later decision",
            )
            assert ok, reason
            task = kb.get_task(conn, tid)
            assert task.status == "archived"
            assert task.terminal_disposition == kb.DISPOSITION_OVERTAKEN_BY_EVENTS

            repair_id = kb._open_auto_repair(
                conn, tid, cause="boom", trigger="verification_failed",
            )
            assert repair_id is None

            skipped = _events(conn, tid, "repair_skipped_terminal_subject")
            assert len(skipped) == 1
            assert (
                skipped[0].payload["terminal_disposition"]
                == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
            )
            # No repair card was linked against the terminal subject.
            assert kb.task_relations(
                conn, tid, relation=kb.RELATION_REPAIRS, direction="to",
            ) == []

    def test_supersede_auto_repair_also_refuses_terminal_subject(self, kanban_home):
        """Same guard applies via the supersede path (repeated gate-red repair)."""
        with kb.connect_closing() as conn:
            tid = _running_task(conn)
            repair_id = kb._open_auto_repair(
                conn, tid, cause="boom", trigger="verification_failed",
            )
            assert repair_id is not None

            # Force the subject into 'blocked' so reconcile_overtaken_by_events
            # (which refuses 'running') will accept it, mirroring how the
            # live incident card reached OVERTAKEN_BY_EVENTS.
            assert kb.block_task(
                conn, tid, reason="obsolete", kind="needs_input",
            )
            ok, reason = kb.reconcile_overtaken_by_events(
                conn, tid, actor="erika", reason="superseded",
                evidence="superseded by a later decision",
            )
            assert ok, reason

            result = kb.supersede_auto_repair(
                conn, repair_id, reason="gate still red",
            )
            # Neither a new repair id nor a diagnosis routing -- the subject
            # is terminal, so the superseding attempt yields nothing to do.
            assert result is None
