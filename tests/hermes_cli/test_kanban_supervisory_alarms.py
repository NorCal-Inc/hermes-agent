"""Verifier subject linkage, orphan-verifier watchdog, lifecycle-stall alarms.

Three related holes in the Gauntlet control plane, all reproduced against the
live shared board on 2026-09-03.

**Defect A' — orphan verifier creation.** ``_return_verifier_verdict_to_subjects``
resolves the tasks a verdict belongs to through ``parent_ids``. A
``codex_verify`` card created with no parent therefore runs, renders a correct
PASS/FAIL, completes — and the return path takes its ``if not subjects: return``
branch in silence. Seven of the fourteen ``codex_verify`` cards on the live board
are that shape, two of which closed a production deployment.

**Defect C — nothing noticed.** Those seven stranded verdicts were found by a
human reading the table, not by the control plane. There was no census, no
alarm, and no route to the governor.

**Defect D — the stale scan talked to nobody.** ``detect_stale_gauntlet_work``
has appended a ``gauntlet_stale`` event since it was written and nothing ever
consumed it. An audit event with no subscriber is a diary: the board recorded
that work had been abandoned, and the recording was abandoned too.

Historical cards are evidence. Nothing here backfills, reinterprets, retries or
repairs one — the guard tests assert the NEW refusal on NEW cards, and the
watchdog tests assert that a pre-existing orphan is REPORTED with its status,
counters and events left exactly as they were.
"""

from __future__ import annotations

import json
import sqlite3
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
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_EXECUTION_ID",
        "HERMES_SESSION_ID",
        "HERMES_EXECUTOR_LANE",
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
        kb.ENV_ACTOR_KIND,
        kb.ENV_ACTOR_ID,
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def routed(monkeypatch: pytest.MonkeyPatch):
    """Pin supervisory routing so a test never depends on the host's config."""
    monkeypatch.setattr(
        kb, "_supervisory_routing",
        lambda: {
            "supervisor": "erika",
            "chat_id": "-100999",
            "thread_id": None,
            "chat_type": "group",
        },
    )


@pytest.fixture
def unrouted(monkeypatch: pytest.MonkeyPatch):
    """Supervisory routing with no executive channel configured."""
    monkeypatch.setattr(
        kb, "_supervisory_routing",
        lambda: {
            "supervisor": "erika",
            "chat_id": "",
            "thread_id": None,
            "chat_type": "dm",
        },
    )


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


def _make_orphan_verifier(conn, *, title="stranded verifier") -> str:
    """Mint a parentless codex_verify card the way the board used to.

    The creation guard now refuses this, which is the point — so the historical
    shape is reconstructed by writing the row directly, exactly as the seven
    live orphans exist today. Reproducing the defect is not the same as being
    allowed to create it.
    """
    tid = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET executor_lane = ? WHERE id = ?",
            (kb.EXECUTOR_LANE_CODEX_VERIFY, tid),
        )
    return tid


# ---------------------------------------------------------------------------
# Defect A' — a verifier is refused unless its verdict has a destination
# ---------------------------------------------------------------------------

class TestVerifierCreationLinkage:
    def test_orphan_verifier_creation_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
            with pytest.raises(kb.VerifierLinkageError) as exc:
                kb.create_task(
                    conn,
                    title="Independent verification of nothing in particular",
                    executor_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
                )
            assert "subject" in str(exc.value)
            # Refused BEFORE any row was written, not rolled back after.
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"] == before

    def test_atlas_shorthand_is_refused_on_the_same_rule(self, kanban_home):
        """``assignee='atlas'`` normalises INTO the verifier lane.

        The shorthand is the door most callers actually use, so a guard that
        only saw the explicit ``executor_lane`` kwarg would be trivially
        bypassed by the spelling everyone prefers.
        """
        with kb.connect_closing() as conn:
            with pytest.raises(kb.VerifierLinkageError):
                kb.create_task(conn, title="verify it", assignee="atlas")

    def test_linked_verifier_is_created_and_reachable(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = kb.create_task(conn, title="subject", assignee="default")
            verifier = kb.create_task(
                conn,
                title=f"Independent verification of {subject}",
                assignee="atlas",
                parents=[subject],
            )
            assert kb.verifier_subject_ids(conn, verifier) == [subject]
            assert kb.missing_verifier_linkage(conn, verifier) == []
            assert kb.orphaned_verifier_tasks(conn) == []

    def test_linkage_is_read_back_from_the_board_not_the_argument(
        self, kanban_home, monkeypatch,
    ):
        """A parent that does not land must unwind the card.

        ``INSERT OR IGNORE`` returns cleanly when it inserts nothing, so a
        non-empty ``parents`` argument is not evidence that the edge exists.
        Simulating that by making the read-back report the truth is what
        distinguishes "we called the linker" from "the board holds the link".
        """
        with kb.connect_closing() as conn:
            subject = kb.create_task(conn, title="subject", assignee="default")
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"]
            monkeypatch.setattr(
                kb, "missing_verifier_linkage", lambda c, t: ["subject"],
            )
            with pytest.raises(kb.VerifierLinkageError):
                kb.create_task(
                    conn, title="verify", assignee="atlas", parents=[subject],
                )
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM tasks"
            ).fetchone()["n"] == before

    def test_non_verifier_cards_are_unaffected(self, kanban_home):
        """The guard is scoped to the verifier lane and nothing else."""
        with kb.connect_closing() as conn:
            ordinary = kb.create_task(conn, title="ordinary parentless work")
            claude = kb.create_task(
                conn, title="claude-lane parentless work", assignee="claude",
            )
            assert kb.get_task(conn, ordinary) is not None
            assert kb.get_task(conn, claude) is not None


# ---------------------------------------------------------------------------
# Defect C — orphan-verifier watchdog
# ---------------------------------------------------------------------------

class TestOrphanVerifierWatchdog:
    def _pending_subject(self, conn, *, title="historical subject"):
        tid = kb.create_task(
            conn, title=title, assignee="default", gauntlet=True,
            max_runtime_seconds=300,
        )
        assert kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id=?", (tid,)
        ).fetchone()["current_run_id"]
        ok = kb.request_review(
            conn, tid, summary="implementation complete; awaiting Atlas",
            expected_run_id=run_id,
        )
        assert ok is True or (isinstance(ok, tuple) and ok[0] is True)
        return tid

    def test_legacy_orphan_pass_is_consumed_and_closes_exact_pending_subject(
        self, kanban_home, routed,
    ):
        with kb.connect_closing() as conn:
            subject = self._pending_subject(conn)
            phase = conn.execute(
                "SELECT id, created_at FROM task_verifications "
                "WHERE task_id=? AND state=? ORDER BY id DESC LIMIT 1",
                (subject, kb.VERIFICATION_PENDING),
            ).fetchone()
            verifier = _make_orphan_verifier(
                conn, title=f"Independent verification: {subject}"
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', result='VERDICT: PASS', "
                    "created_at=?, completed_at=? WHERE id=?",
                    (int(phase["created_at"]) + 1, int(phase["created_at"]) + 2, verifier),
                )

            # The consumer spends the PASS before the orphan alarm path.
            assert kb.sweep_orphan_verifiers(conn) == []
            row = kb.get_task(conn, subject)
            assert row.status == "done"
            assert row.verification_state == kb.VERIFICATION_VERIFIED
            assert row.terminal_disposition == kb.DISPOSITION_COMPLETED
            consumed = _events(conn, subject, "orphan_verifier_verdict_consumed")
            assert len(consumed) == 1
            assert consumed[0][1]["verifier_task"] == verifier
            assert consumed[0][1]["verdict"] == "PASS"
            assert consumed[0][1]["recorded"] is True
            assert "orphan_verifier_reconciled" in _kinds(conn, verifier)
            assert kb.ORPHAN_VERIFIER_RESULT_EVENT not in _kinds(conn, verifier)
            # Idempotent: a second sweep neither reapplies nor alarms it.
            assert kb.sweep_orphan_verifiers(conn) == []
            assert len(_events(conn, subject, "orphan_verifier_verdict_consumed")) == 1

    def test_legacy_orphan_fail_is_consumed_and_routes_subject_to_rework(
        self, kanban_home, routed,
    ):
        with kb.connect_closing() as conn:
            subject = self._pending_subject(conn, title="historical failing subject")
            phase = conn.execute(
                "SELECT created_at FROM task_verifications "
                "WHERE task_id=? AND state=? ORDER BY id DESC LIMIT 1",
                (subject, kb.VERIFICATION_PENDING),
            ).fetchone()
            verifier = _make_orphan_verifier(
                conn, title=f"Independent verification: {subject}"
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', result='VERDICT: FAIL', "
                    "created_at=?, completed_at=? WHERE id=?",
                    (int(phase["created_at"]) + 1, int(phase["created_at"]) + 2, verifier),
                )
            assert kb.sweep_orphan_verifiers(conn) == []
            row = kb.get_task(conn, subject)
            assert row.status in ("ready", "todo")
            assert row.verification_state is None
            assert kb.verification_history(conn, subject)[-1]["state"] == kb.VERIFICATION_FAILED
            assert "orphan_verifier_reconciled" in _kinds(conn, verifier)

    def test_legacy_orphan_recovery_refuses_ambiguous_deleted_or_wrong_phase(
        self, kanban_home, routed,
    ):
        with kb.connect_closing() as conn:
            subject = self._pending_subject(conn, title="current subject")
            other = kb.create_task(conn, title="other context task", assignee="default")
            phase = conn.execute(
                "SELECT created_at FROM task_verifications "
                "WHERE task_id=? AND state=? ORDER BY id DESC LIMIT 1",
                (subject, kb.VERIFICATION_PENDING),
            ).fetchone()
            ambiguous = _make_orphan_verifier(
                conn, title=f"Independent verification: {subject} context {other}"
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', result='VERDICT: PASS', "
                    "created_at=?, completed_at=? WHERE id=?",
                    (int(phase["created_at"]) + 1, int(phase["created_at"]) + 2, ambiguous),
                )
            assert kb.sweep_orphan_verifiers(conn) == [ambiguous]
            assert kb.get_task(conn, subject).status == "review"

            # Deleted/non-existent subjects are never reconstructed from prose.
            deleted = _make_orphan_verifier(
                conn, title="Independent verification: t_b0ba7338"
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', result='VERDICT: PASS' WHERE id=?",
                    (deleted,),
                )
            assert kb.sweep_orphan_verifiers(conn) == [deleted]
            assert "orphan_verifier_reconciled" not in _kinds(conn, deleted)

    def test_finished_orphan_is_escalated_with_its_stranded_verdict(
        self, kanban_home, routed,
    ):
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done', "
                    "result = 'VERDICT: PASS' WHERE id = ?",
                    (tid,),
                )
            assert kb.orphaned_verifier_tasks(conn) == [tid]
            assert kb.sweep_orphan_verifiers(conn) == [tid]

            alarms = _events(conn, tid, kb.ORPHAN_VERIFIER_RESULT_EVENT)
            assert len(alarms) == 1
            payload = alarms[0][1]
            assert payload["verdict"] == "PASS"
            assert payload["verdict_source"] == "tasks.result"
            assert payload["supervisor"] == "erika"
            assert payload["executive_channel"] is True
            assert kb.SUPERVISORY_ALARM_ROUTED_EVENT in _kinds(conn, tid)

    def test_escalation_subscribes_the_supervisor_before_the_alarm(
        self, kanban_home, routed,
    ):
        """Cursor ordering is the whole difference between told and not told.

        ``add_notify_sub`` snaps a new subscriber to the current max event id.
        Subscribing after the alarm would mark the alarm as already consumed
        and deliver silence.
        """
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,),
                )
            kb.sweep_orphan_verifiers(conn)
            sub = conn.execute(
                "SELECT notifier_profile, chat_id, last_event_id "
                "FROM kanban_notify_subs WHERE task_id = ?",
                (tid,),
            ).fetchone()
            assert sub["notifier_profile"] == "erika"
            assert sub["chat_id"] == "-100999"
            alarm_id = conn.execute(
                "SELECT id FROM task_events WHERE task_id = ? AND kind = ?",
                (tid, kb.ORPHAN_VERIFIER_RESULT_EVENT),
            ).fetchone()["id"]
            assert int(sub["last_event_id"]) < int(alarm_id)

    def test_alarm_is_durable_without_an_executive_channel(
        self, kanban_home, unrouted,
    ):
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,),
                )
            assert kb.sweep_orphan_verifiers(conn) == [tid]
            kinds = _kinds(conn, tid)
            assert kb.ORPHAN_VERIFIER_RESULT_EVENT in kinds
            assert kb.SUPERVISORY_ALARM_UNDELIVERABLE_EVENT in kinds
            assert kb.SUPERVISORY_ALARM_ROUTED_EVENT not in kinds

    def test_unfinished_orphan_is_not_alarmed(self, kanban_home, routed):
        """No result has been produced, so no result has been stranded.

        The creation guard covers this card's real defect, at the boundary
        where it could still be refused.
        """
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            assert kb.sweep_orphan_verifiers(conn) == []
            assert kb.ORPHAN_VERIFIER_RESULT_EVENT not in _kinds(conn, tid)

    def test_linked_verifier_is_never_alarmed(self, kanban_home, routed):
        with kb.connect_closing() as conn:
            subject = kb.create_task(conn, title="subject", assignee="default")
            verifier = kb.create_task(
                conn, title="verify", assignee="atlas", parents=[subject],
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?",
                    (verifier,),
                )
            assert kb.sweep_orphan_verifiers(conn) == []

    def test_alarm_fires_once_ever(self, kanban_home, routed):
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,),
                )
            assert kb.sweep_orphan_verifiers(conn) == [tid]
            assert kb.sweep_orphan_verifiers(conn) == []
            assert kb.sweep_orphan_verifiers(conn) == []
            assert len(_events(conn, tid, kb.ORPHAN_VERIFIER_RESULT_EVENT)) == 1

    def test_historical_orphan_state_is_preserved_exactly(
        self, kanban_home, routed,
    ):
        """The watchdog reports; it does not repair, retry or reclassify."""
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done', consecutive_failures = 2,"
                    " result = 'VERDICT: FAIL', terminal_disposition = NULL "
                    "WHERE id = ?",
                    (tid,),
                )
            before = dict(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,))
                .fetchone()
            )
            before_events = len(_events(conn, tid))
            kb.sweep_orphan_verifiers(conn)
            after = dict(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,))
                .fetchone()
            )
            assert after == before, "watchdog mutated a historical card"
            # Append-only: events were added, none removed or rewritten.
            assert len(_events(conn, tid)) > before_events
            assert kb.orphaned_verifier_tasks(conn) == [tid]

    def test_verdict_recovered_from_the_run_when_absent_on_the_task(
        self, kanban_home, routed,
    ):
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,),
                )
                conn.execute(
                    "INSERT INTO task_runs "
                    "(task_id, profile, status, started_at, summary) "
                    "VALUES (?, 'default', 'done', 0, ?)",
                    (tid, "**VERDICT: BLOCKER**\nsandbox unavailable"),
                )
            kb.sweep_orphan_verifiers(conn)
            payload = _events(conn, tid, kb.ORPHAN_VERIFIER_RESULT_EVENT)[0][1]
            assert payload["verdict"] == "BLOCKER"
            assert payload["verdict_source"] == "task_runs"

    def test_unreadable_verdict_still_escalates(self, kanban_home, routed):
        """Silence about an orphan is the failure; a null verdict is a fact."""
        with kb.connect_closing() as conn:
            tid = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done', result = 'looks fine' "
                    "WHERE id = ?",
                    (tid,),
                )
            assert kb.sweep_orphan_verifiers(conn) == [tid]
            payload = _events(conn, tid, kb.ORPHAN_VERIFIER_RESULT_EVENT)[0][1]
            assert payload["verdict"] is None


# ---------------------------------------------------------------------------
# Defect D — no-lifecycle-progress alarms
# ---------------------------------------------------------------------------

def _park_stale(conn, *, age: int, now: int, status: str = "review") -> str:
    """A gauntlet-enforced card whose last durable event is ``age`` old."""
    tid = kb.create_task(conn, title="parked mid-chain", assignee="default",
                         gauntlet=True)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = ?, verification_state = ?, "
            "created_at = ?, started_at = ? WHERE id = ?",
            (status, kb.VERIFICATION_PENDING, now - age, now - age, tid),
        )
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (now - age, tid),
        )
    return tid


class TestLifecycleStallAlarms:
    def test_stale_finding_is_routed_to_the_supervisor(
        self, kanban_home, routed,
    ):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            assert [s.task_id for s in stale] == [tid]

            assert kb.sweep_lifecycle_stall_alarms(conn, stale, now=now) == [tid]
            alarms = _events(conn, tid, kb.NO_LIFECYCLE_PROGRESS_EVENT)
            assert len(alarms) == 1
            payload = alarms[0][1]
            assert payload["supervisor"] == "erika"
            assert payload["executive_channel"] is True
            assert payload["reasons"] == ["verification_pending"]
            assert payload["age_seconds"] >= 14400
            assert kb.SUPERVISORY_ALARM_ROUTED_EVENT in _kinds(conn, tid)

            sub = conn.execute(
                "SELECT notifier_profile FROM kanban_notify_subs "
                "WHERE task_id = ?",
                (tid,),
            ).fetchone()
            assert sub["notifier_profile"] == "erika"

    def test_alarm_arms_a_ten_minute_evidence_recheck(
        self, kanban_home, routed,
    ):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)

            timers = [
                t for t in kb.task_observation_timers(
                    conn, tid, state=kb.OBSERVATION_STATE_OBSERVING,
                )
                if t.kind == kb.LIFECYCLE_STALL_TIMER_KIND
            ]
            assert len(timers) == 1
            timer = timers[0]
            assert timer.interval_seconds == 300
            assert timer.interval_seconds == (
                kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS
            )
            assert timer.next_due_at == now + 300
            payload = _events(conn, tid, kb.NO_LIFECYCLE_PROGRESS_EVENT)[0][1]
            assert payload["recheck_timer_id"] == timer.id
            assert payload["recheck_interval_seconds"] == 300

            # The recheck actually fires, and keeps observing afterwards.
            ticks = kb.run_observation_cycle(conn, now=now + 601)
            assert [t.timer_id for t in ticks] == [timer.id]
            assert kb.get_observation_timer(conn, timer.id).observing

    def test_alarm_events_do_not_reset_the_staleness_clock(
        self, kanban_home, routed,
    ):
        """The trap this design had to avoid.

        The alarm, its routing record and the recheck timer are all writes to
        ``task_events`` on the card being alarmed about. If any of them counted
        as durable lifecycle progress, the card would look freshly touched for
        exactly as long as nobody was touching it, and the next episode would
        never open.
        """
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            kb.run_observation_cycle(conn, now=now + 601)

            after = kb._last_meaningful_event(conn, tid)
            # Every event written since the alarm is non-progress chatter, so
            # the newest meaningful event is still the original one.
            assert after is None or int(after["created_at"]) <= now - 20_000

    def test_ownerless_card_still_alarms_and_records_the_refusal(
        self, kanban_home, routed,
    ):
        """An unowned stalled card is exactly one a governor must hear about.

        ``arm_observation_timer`` fails closed on an ownerless card, correctly
        — a timer nobody owns is an alarm nobody answers. That refusal must
        degrade the recheck, never the escalation.
        """
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = NULL, created_by = NULL, "
                    "actor_id = NULL, recovery_owner = NULL WHERE id = ?",
                    (tid,),
                )
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            assert kb.sweep_lifecycle_stall_alarms(conn, stale, now=now) == [tid]
            payload = _events(conn, tid, kb.NO_LIFECYCLE_PROGRESS_EVENT)[0][1]
            assert payload["recheck_timer_id"] is None
            assert payload["recheck_error"]

    def test_inactive_card_clears_the_alarm(self, kanban_home, routed):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ?", (tid,),
                )
            assert kb.clear_resolved_lifecycle_stalls(conn, now=now + 10) == [tid]
            assert _stall_timers(conn, tid) == []
            cleared = _events(conn, tid, kb.LIFECYCLE_STALL_CLEARED_EVENT)[0][1]
            assert cleared["resolution"] == "task_inactive"
            assert cleared["lifecycle"] == "inactive"

    def test_resumed_progress_clears_the_alarm(self, kanban_home, routed):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            # Real durable progress: somebody commented on the card.
            kb.add_comment(conn, tid, "erika", "picking this up")
            assert kb.clear_resolved_lifecycle_stalls(conn, now=now + 10) == [tid]
            cleared = _events(conn, tid, kb.LIFECYCLE_STALL_CLEARED_EVENT)[0][1]
            assert cleared["resolution"] == "progress_resumed"
            assert cleared["lifecycle"] == "active"

    def test_still_stalled_card_keeps_its_alarm(self, kanban_home, routed):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            assert kb.clear_resolved_lifecycle_stalls(conn, now=now + 10) == []
            assert len(_stall_timers(conn, tid)) == 1

    def test_one_recheck_timer_per_card(self, kanban_home, routed):
        """A second alarm on the same card must not stack a second timer."""
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now + 1)
            assert len(_stall_timers(conn, tid)) == 1

    def test_alarm_changes_no_workflow_state(self, kanban_home, routed):
        now = 2_000_000_000
        with kb.connect_closing() as conn:
            tid = _park_stale(conn, age=20_000, now=now)
            before = dict(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,))
                .fetchone()
            )
            stale = kb.detect_stale_gauntlet_work(
                conn, stale_timeout_seconds=14400, realert_seconds=0, now=now,
            )
            kb.sweep_lifecycle_stall_alarms(conn, stale, now=now)
            after = dict(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,))
                .fetchone()
            )
            assert after == before


def _stall_timers(conn, tid):
    return [
        t for t in kb.task_observation_timers(
            conn, tid, state=kb.OBSERVATION_STATE_OBSERVING,
        )
        if t.kind == kb.LIFECYCLE_STALL_TIMER_KIND
    ]


# ---------------------------------------------------------------------------
# Dispatcher wiring — the passes must actually run on a tick
# ---------------------------------------------------------------------------

class TestDispatchWiring:
    def test_tick_escalates_stalls_and_orphan_verifiers(
        self, kanban_home, routed, monkeypatch,
    ):
        now = 2_000_000_000
        monkeypatch.setattr(kb.time, "time", lambda: float(now))
        with kb.connect_closing() as conn:
            stalled = _park_stale(conn, age=20_000, now=now)
            orphan = _make_orphan_verifier(conn)
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = 'done', "
                    "result = 'VERDICT: PASS' WHERE id = ?",
                    (orphan,),
                )
        with kb.connect_closing() as conn:
            result = kb.dispatch_once(
                conn,
                max_spawn=0,
                gauntlet_stale_timeout_seconds=14400,
                gauntlet_stale_realert_seconds=0,
            )
        assert stalled in result.gauntlet_stale
        assert stalled in result.lifecycle_alarms
        assert orphan in result.orphan_verifiers
        with kb.connect_closing() as conn:
            assert kb.NO_LIFECYCLE_PROGRESS_EVENT in _kinds(conn, stalled)
            assert kb.ORPHAN_VERIFIER_RESULT_EVENT in _kinds(conn, orphan)

    def test_tick_does_not_alarm_card_resolved_by_same_stale_disposition(
        self, kanban_home, monkeypatch,
    ):
        now = 2_000_000_000
        monkeypatch.setattr(kb.time, "time", lambda: float(now))
        with kb.connect_closing() as conn:
            stalled = _park_stale(conn, age=20_000, now=now)

        seen = []

        def _resolved(_conn, entries):
            ids = [entry.task_id for entry in entries]
            assert stalled in ids
            return {
                "reconciled": [],
                "infrastructure_released": [stalled],
                "verifier_routed": [],
                "unresolved": [],
            }

        def _alarms(_conn, entries):
            seen.extend(entry.task_id for entry in entries)
            return [entry.task_id for entry in entries]

        monkeypatch.setattr(kb, "resolve_stale_gauntlet_dispositions", _resolved)
        monkeypatch.setattr(kb, "sweep_lifecycle_stall_alarms", _alarms)
        with kb.connect_closing() as conn:
            result = kb.dispatch_once(
                conn,
                max_spawn=0,
                gauntlet_stale_timeout_seconds=14400,
                gauntlet_stale_realert_seconds=0,
            )
        assert stalled in result.gauntlet_stale
        assert stalled not in seen
        assert stalled not in result.lifecycle_alarms

    def test_routing_failure_does_not_take_the_tick_down(
        self, kanban_home, monkeypatch,
    ):
        """Detection is durable with or without a channel; a tick is not."""
        now = 2_000_000_000
        monkeypatch.setattr(kb.time, "time", lambda: float(now))
        with kb.connect_closing() as conn:
            stalled = _park_stale(conn, age=20_000, now=now)

        def _boom(*a, **kw):
            raise RuntimeError("messaging down")

        monkeypatch.setattr(kb, "sweep_lifecycle_stall_alarms", _boom)
        monkeypatch.setattr(kb, "sweep_orphan_verifiers", _boom)
        with kb.connect_closing() as conn:
            result = kb.dispatch_once(
                conn,
                max_spawn=0,
                gauntlet_stale_timeout_seconds=14400,
                gauntlet_stale_realert_seconds=0,
            )
        # The scan still ran and still recorded the finding.
        assert stalled in result.gauntlet_stale
        assert result.lifecycle_alarms == []
        assert result.orphan_verifiers == []

class TestStaleDispositionActuator:
    def _review_pending_subject(self, conn):
        tid = kb.create_task(
            conn,
            title="stale implementation awaiting independent review",
            assignee="default",
            gauntlet=True,
            max_runtime_seconds=300,
        )
        assert kb.claim_task(conn, tid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()["current_run_id"]
        ok = kb.request_review(
            conn,
            tid,
            summary="implementation complete; independent verification required",
            expected_run_id=run_id,
        )
        assert ok is True or (isinstance(ok, tuple) and ok[0] is True)
        row = conn.execute(
            "SELECT status, verification_state FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
        assert row["status"] == "review"
        assert row["verification_state"] == kb.VERIFICATION_PENDING
        return tid

    def _entry(self, tid):
        return kb.GauntletStaleTask(
            task_id=tid,
            status="review",
            verification_state=kb.VERIFICATION_PENDING,
            regression_required=False,
            age_seconds=600,
            reasons=["verification_pending"],
            last_event_id=None,
            last_event_at=None,
            last_event_kind=None,
        )

    def test_stale_pending_with_late_evidence_routes_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = self._review_pending_subject(conn)
            assert kb._open_verifier_child(conn, tid) is None

            kb.store_attachment_bytes(
                conn,
                tid,
                "evidence.txt",
                b"commit abc123\nfocused tests: pass\n",
                content_type="text/plain",
                uploaded_by="test",
            )

            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [self._entry(tid)], now=1_700_000_000,
            )
            assert result["verifier_routed"] == [tid]
            verifier = kb._open_verifier_child(conn, tid)
            assert verifier is not None
            assert kb.verifier_subject_ids(conn, verifier) == [tid]

            vrow = conn.execute(
                "SELECT status, executor_lane, gauntlet_enforced, max_runtime_seconds "
                "FROM tasks WHERE id = ?",
                (verifier,),
            ).fetchone()
            assert vrow["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert bool(vrow["gauntlet_enforced"]) is True
            assert vrow["max_runtime_seconds"] == 300
            assert vrow["status"] in ("ready", "todo")

            events = _events(conn, tid, "gauntlet_stale_disposition")
            assert len(events) == 1
            assert events[0][1]["action"] == "independent_verifier_routed"
            assert events[0][1]["verifier_task"] == verifier

    def test_stale_pending_without_evidence_stays_fail_closed(self, kanban_home, unrouted):
        with kb.connect_closing() as conn:
            tid = self._review_pending_subject(conn)
            assert kb._open_verifier_child(conn, tid) is None

            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [self._entry(tid)], now=1_700_000_000,
            )
            assert result["verifier_routed"] == []
            assert result["unresolved"] == [tid]
            assert kb._open_verifier_child(conn, tid) is None

            row = conn.execute(
                "SELECT status, verification_state FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["status"] == "review"
            assert row["verification_state"] == kb.VERIFICATION_PENDING
            assert "independent_verification_unroutable" in _kinds(conn, tid)

    def test_stale_subject_with_verified_completed_repair_is_reconciled(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = kb.create_task(
                conn,
                title="old stale repair generation",
                assignee="default",
                gauntlet=True,
                max_runtime_seconds=300,
            )
            assert kb.claim_task(conn, subject)
            assert kb.block_task(
                conn,
                subject,
                reason="historical defect awaiting newer repair",
                kind="needs_input",
            )

            repair = kb.create_task(
                conn,
                title="new verified repair generation",
                assignee="default",
                gauntlet=True,
                max_runtime_seconds=300,
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', verification_state=?, "
                    "terminal_disposition='completed', completed_at=? WHERE id=?",
                    (kb.VERIFICATION_VERIFIED, 1_700_000_100, repair),
                )
            kb.add_task_relation(
                conn, repair, subject, "repairs", created_by="test"
            )
            relation_event = conn.execute(
                "SELECT id, created_at FROM task_events WHERE task_id=? "
                "AND kind='relation_received' ORDER BY id DESC LIMIT 1",
                (subject,),
            ).fetchone()
            assert relation_event is not None

            entry = kb.GauntletStaleTask(
                task_id=subject,
                status="blocked",
                verification_state=None,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=int(relation_event["id"]),
                last_event_at=int(relation_event["created_at"]),
                last_event_kind="relation_received",
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=1_700_000_200,
            )

            assert result["reconciled"] == [subject]
            row = conn.execute(
                "SELECT status, terminal_disposition, disposition_reason "
                "FROM tasks WHERE id=?",
                (subject,),
            ).fetchone()
            assert row["status"] == "archived"
            assert row["terminal_disposition"] == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
            assert repair in row["disposition_reason"]
            kinds = _kinds(conn, subject)
            assert "reconciled_overtaken_by_events" in kinds


    def test_stale_needs_input_from_infrastructure_timeout_is_released(self, kanban_home):
        from hermes_cli import exec_supervisor as ex

        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="infrastructure-stuck repair",
                assignee="default",
                gauntlet=True,
                max_runtime_seconds=300,
            )
            assert kb.claim_task(conn, tid)
            execution = ex.create_execution(
                conn,
                executor_type="claude",
                command_class="claude.headless",
                cwd=str(kanban_home),
                task_id=tid,
                max_runtime_s=300,
                now=1_700_000_000,
            )
            assert kb.block_task(
                conn,
                tid,
                reason="control-plane timeout; no human decision required",
                kind="needs_input",
                event_payload_extra={
                    "execution_id": execution.id,
                    "execution_status": "timed_out",
                    "failure_class": "infrastructure",
                },
            )
            block = conn.execute(
                "SELECT created_at FROM task_events WHERE task_id=? AND kind='blocked' "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            assert block is not None
            block_at = int(block["created_at"])
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE executions SET status='timed_out', ended_at=? WHERE id=?",
                    (block_at, execution.id),
                )

            entry = kb.GauntletStaleTask(
                task_id=tid,
                status="blocked",
                verification_state=None,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=None,
                last_event_at=None,
                last_event_kind=None,
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=1_700_000_600,
            )

            assert result["infrastructure_released"] == [tid]
            assert result["unresolved"] == []
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["status"] == "ready"
            events = _events(conn, tid, "gauntlet_stale_disposition")
            assert any(
                payload["action"] == "infrastructure_recovery_released"
                and payload["execution_id"] == execution.id
                and payload["execution_status"] == "timed_out"
                for _, payload in events
            )

    def test_genuine_needs_input_without_infrastructure_execution_stays_blocked(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="real owner decision",
                assignee="default",
                gauntlet=True,
                max_runtime_seconds=300,
            )
            assert kb.claim_task(conn, tid)
            assert kb.block_task(
                conn,
                tid,
                reason="owner must choose credential source of truth",
                kind="needs_input",
            )
            entry = kb.GauntletStaleTask(
                task_id=tid,
                status="blocked",
                verification_state=None,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=None,
                last_event_at=None,
                last_event_kind=None,
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=1_700_000_600,
            )
            assert result["infrastructure_released"] == []
            assert result["unresolved"] == [tid]
            assert conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()["status"] == "blocked"


    def test_existing_open_gauntlet_verifier_missing_runtime_is_normalized(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = self._review_pending_subject(conn)
            kb.store_attachment_bytes(
                conn,
                tid,
                "evidence-existing.txt",
                b"commit abc123\nfocused tests: pass\n",
                content_type="text/plain",
                uploaded_by="test",
            )
            verifier = kb._ensure_independent_verifier_child(
                conn, tid, implementer="default"
            )
            assert verifier is not None
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET max_runtime_seconds=NULL WHERE id=?",
                    (verifier,),
                )
            assert conn.execute(
                "SELECT max_runtime_seconds FROM tasks WHERE id=?", (verifier,)
            ).fetchone()[0] is None

            again = kb._ensure_independent_verifier_child(
                conn, tid, implementer="default"
            )
            assert again == verifier
            assert conn.execute(
                "SELECT max_runtime_seconds FROM tasks WHERE id=?", (verifier,)
            ).fetchone()[0] == 300
            assert "gauntlet_runtime_inherited" in _kinds(conn, verifier)


    def test_stale_current_self_review_block_routes_independent_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = self._review_pending_subject(conn)
            kb.store_attachment_bytes(
                conn,
                tid,
                "self-review-evidence.txt",
                b"candidate sha and passing focused tests\n",
                content_type="text/plain",
                uploaded_by="test",
            )
            phase = kb._current_verification_phase(conn, tid)
            assert phase is not None
            now = 1_700_000_000
            with kb.write_txn(conn):
                kb._append_event(
                    conn,
                    tid,
                    "verification_blocked_self_review",
                    {"verifier": "default", "implementer": "default"},
                    run_id=phase[1],
                )
                conn.execute(
                    "UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?",
                    (tid,),
                )
                kb._append_event(
                    conn,
                    tid,
                    "blocked",
                    {
                        "reason": "same-identity review rejected",
                        "kind": "needs_input",
                        "source_status": "review",
                    },
                    run_id=phase[1],
                )

            entry = kb.GauntletStaleTask(
                task_id=tid,
                status="blocked",
                verification_state=kb.VERIFICATION_PENDING,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=None,
                last_event_at=None,
                last_event_kind=None,
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=now + 600,
            )
            assert result["self_review_released"] == [tid]
            assert result["verifier_routed"] == [tid]
            row = conn.execute(
                "SELECT status, verification_state FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["status"] == "review"
            assert row["verification_state"] == kb.VERIFICATION_PENDING
            verifier = kb._open_verifier_child(conn, tid)
            assert verifier is not None
            assert conn.execute(
                "SELECT max_runtime_seconds FROM tasks WHERE id=?", (verifier,)
            ).fetchone()[0] == 300
            events = _events(conn, tid, "gauntlet_stale_disposition")
            assert any(p["action"] == "self_review_routed_independent" for _, p in events)


    def test_old_verified_repair_cannot_archive_newer_same_second_episode(self, kanban_home):
        with kb.connect_closing() as conn:
            subject = kb.create_task(
                conn, title="subject later reopened", assignee="default",
                gauntlet=True, max_runtime_seconds=300,
            )
            assert kb.claim_task(conn, subject)
            assert kb.block_task(conn, subject, reason="first episode", kind="needs_input")

            repair = kb.create_task(
                conn, title="repair for first episode", assignee="default",
                gauntlet=True, max_runtime_seconds=300,
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status='done', verification_state=?, "
                    "terminal_disposition='completed', completed_at=? WHERE id=?",
                    (kb.VERIFICATION_VERIFIED, 1_700_000_100, repair),
                )
            kb.add_task_relation(conn, repair, subject, "repairs", created_by="test")
            rel_event = conn.execute(
                "SELECT id, created_at FROM task_events WHERE task_id=? "
                "AND kind='relation_received' ORDER BY id DESC LIMIT 1",
                (subject,),
            ).fetchone()
            assert rel_event is not None

            # A later meaningful event in the SAME second creates a new episode.
            # Timestamp comparison cannot distinguish it; event identity can.
            with kb.write_txn(conn):
                kb._append_event(conn, subject, "commented", {"author":"test","len":1})
                later = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                conn.execute(
                    "UPDATE task_events SET created_at=? WHERE id=?",
                    (int(rel_event["created_at"]), int(later)),
                )
            entry = kb.GauntletStaleTask(
                task_id=subject,
                status="blocked",
                verification_state=None,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=int(later),
                last_event_at=int(rel_event["created_at"]),
                last_event_kind="commented",
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=int(rel_event["created_at"]) + 600,
            )
            assert result["reconciled"] == []
            assert result["unresolved"] == [subject]
            row = conn.execute(
                "SELECT status, terminal_disposition FROM tasks WHERE id=?",
                (subject,),
            ).fetchone()
            assert row["status"] == "blocked"
            assert row["terminal_disposition"] is None

    def test_historical_infrastructure_timeout_cannot_clear_new_human_block(self, kanban_home):
        from hermes_cli import exec_supervisor as ex

        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="current credential decision", assignee="default",
                gauntlet=True, max_runtime_seconds=300,
            )
            assert kb.claim_task(conn, tid)
            assert kb.block_task(
                conn, tid,
                reason="owner must choose current credential source",
                kind="needs_input",
            )
            block = conn.execute(
                "SELECT created_at FROM task_events WHERE task_id=? AND kind='blocked' "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            assert block is not None
            block_at = int(block["created_at"])

            # Execution history terminates in the exact same second as the
            # human block. Timestamp correlation would clear it; the missing
            # structured execution_id/failure_class linkage must keep it blocked.
            execution = ex.create_execution(
                conn,
                executor_type="claude",
                command_class="claude.headless",
                cwd=str(kanban_home),
                task_id=tid,
                max_runtime_s=300,
                now=block_at - 1200,
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE executions SET status='timed_out', ended_at=? WHERE id=?",
                    (block_at, execution.id),
                )

            entry = kb.GauntletStaleTask(
                task_id=tid,
                status="blocked",
                verification_state=None,
                regression_required=False,
                age_seconds=600,
                reasons=["parked_blocked"],
                last_event_id=None,
                last_event_at=block_at,
                last_event_kind="blocked",
            )
            result = kb.resolve_stale_gauntlet_dispositions(
                conn, [entry], now=block_at + 600,
            )
            assert result["infrastructure_released"] == []
            assert result["unresolved"] == [tid]
            assert conn.execute(
                "SELECT status FROM tasks WHERE id=?", (tid,)
            ).fetchone()["status"] == "blocked"
