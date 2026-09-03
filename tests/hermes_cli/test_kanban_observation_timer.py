"""The 600-second recheck, as a mechanism rather than a convention.

Christopher, 2026-09-03 03:34:05, verbatim:

    "A 10-minute stall timer is reasonable here, but it should mean **recheck,
    not declare stalled**."

and the correcting session's own summary of its error:
**"a clock produced a verdict instead of a question."**

That ruling went into ``vault/Skills/ops/claude-session-hygiene.md`` as prose,
and prose is what failed. Three distinct shapes, all observed on 2026-09-03:

1. **The timer ended at its own emission.** A watcher whose contract was a
   recheck every 600 s emitted once and returned. The second recheck never
   existed and nothing on disk recorded that one was owed.
2. **The timer silently disappeared.** A watcher was stopped with no
   replacement armed. The observation stopped, and its absence read exactly
   like "nothing to report".
3. **A stopped watcher kept running.** ``TaskStop`` reported success while PIDs
   18339 and 22963 stayed live; one consumed a half-corrected probe and emitted
   a false CONTINUITY-OK. Process presence was not evidence of observation and
   process absence was not evidence of its end.

Every test below is deterministic clock-and-state evidence: the time is
injected, the assertions read persisted rows, and no test anywhere trusts a
process, a watcher's report, or a model's claim about what a timer did.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


T0 = 1_756_900_000  # a fixed epoch; every due time below is derived from it
INTERVAL = kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _task(conn, title: str = "observed work") -> str:
    return kb.create_task(
        conn, title=title, assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
    )


def _armed(conn, **kwargs) -> kb.ObservationTimer:
    tid = kwargs.pop("task_id", None) or _task(conn)
    kwargs.setdefault("now", T0)
    return kb.arm_observation_timer(conn, tid, **kwargs)


def _events(conn, task_id, kind):
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id",
            (task_id, kind),
        ).fetchall()
    ]


# ---------------------------------------------------------------------------
# The interval is 600. Not configurable, not negotiable.
# ---------------------------------------------------------------------------

class TestIntervalIsEnforced:
    def test_the_constant_is_600(self):
        assert kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS == 600

    def test_arming_uses_600_and_is_owed_one_interval_later(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
        assert timer.interval_seconds == 600
        assert timer.created_at == T0
        # Owed one full interval after arming, never immediately.
        assert timer.next_due_at == T0 + 600
        assert timer.state == kb.OBSERVATION_STATE_OBSERVING
        assert timer.tick_count == 0
        assert timer.verdict_emitted is False

    @pytest.mark.parametrize("bad", [0, 60, 300, 599, 601, 900, 1800, 14400])
    def test_any_other_interval_is_refused(self, kanban_home, bad):
        """"Not 900, not a number reasoned out from a dispatcher tick."

        A silently-accepted 900 would be indistinguishable at read time from a
        correctly armed timer, which is how "use 600" decays back into a
        convention.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            with pytest.raises(kb.ObservationIntervalError):
                kb.arm_observation_timer(
                    conn, tid, interval_seconds=bad, now=T0,
                )
            # Refused means nothing was written.
            assert kb.task_observation_timers(conn, tid) == []

    def test_arming_fails_closed_on_an_ownerless_card(self, kanban_home):
        """A timer nobody owns is an alarm nobody answers."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, assignee = NULL, "
                "created_by = NULL, recovery_owner = NULL WHERE id = ?",
                (tid,),
            )
            conn.commit()
            with pytest.raises(kb.OwnershipUnresolvable):
                kb.arm_observation_timer(conn, tid, now=T0)
            assert kb.task_observation_timers(conn, tid) == []

    def test_arming_on_an_unknown_card_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.OwnershipUnresolvable):
                kb.arm_observation_timer(conn, "t_nope", now=T0)

    def test_owner_defaults_to_the_resolved_accountable_identity(
        self, kanban_home
    ):
        """Ownership comes from persisted state, so the timer outlives its armer."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=T0)
            expected = kb.resolve_task_ownership(conn, tid).owner
        assert timer.owner == expected and timer.owner is not None


# ---------------------------------------------------------------------------
# Due-time handling and the exact 600s cadence
# ---------------------------------------------------------------------------

class TestDueTime:
    def test_nothing_is_owed_before_the_boundary(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            for t in (T0, T0 + 1, T0 + 599):
                assert kb.due_observation_timers(conn, now=t) == []
                assert kb.emit_observation_tick(conn, timer.id, now=t) is None
            assert kb.observation_ticks(conn, timer.id) == []

    def test_the_boundary_itself_is_due(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            assert [t.id for t in kb.due_observation_timers(
                conn, now=T0 + 600)] == [timer.id]
            tick = kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
        assert tick is not None
        assert tick.due_at == T0 + 600 and tick.emitted_at == T0 + 600
        assert tick.missed == 0 and tick.late_seconds == 0

    def test_the_cadence_is_exactly_600_seconds_over_many_ticks(
        self, kanban_home
    ):
        """The interval is asserted from the ledger, not from a report.

        Five consecutive ticks, each emitted at its own boundary. The due
        instants must be T0+600k with no drift — a timer that re-phases off
        ``now`` would drift by however late each emission happened to be.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            for k in range(1, 6):
                # Deliberately emit LATE by a few seconds each time. Phase must
                # come from the due instant, never from the emission instant.
                kb.emit_observation_tick(conn, timer.id, now=T0 + 600 * k + 7)
            ticks = kb.observation_ticks(conn, timer.id)
            final = kb.get_observation_timer(conn, timer.id)

        assert [t.seq for t in ticks] == [1, 2, 3, 4, 5]
        assert [t.due_at for t in ticks] == [T0 + 600 * k for k in range(1, 6)]
        gaps = [b.due_at - a.due_at for a, b in zip(ticks, ticks[1:])]
        assert gaps == [600, 600, 600, 600]
        assert final.next_due_at == T0 + 600 * 6
        assert final.tick_count == 5

    def test_a_long_outage_emits_once_and_records_what_was_missed(
        self, kanban_home
    ):
        """An observer down for ~33 minutes does not fire a burst.

        It emits ONE tick, discharging the oldest owed boundary, records the
        three intervals that passed unobserved, and stays on the original
        grid. A burst would flood the card; a silent skip would erase the
        evidence that observation lapsed.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            tick = kb.emit_observation_tick(conn, timer.id, now=T0 + 2000)
            after = kb.get_observation_timer(conn, timer.id)
            all_ticks = kb.observation_ticks(conn, timer.id)

        assert len(all_ticks) == 1
        assert tick.due_at == T0 + 600  # the oldest owed boundary
        assert tick.emitted_at == T0 + 2000
        assert tick.missed == 2  # T0+1200 and T0+1800 passed unobserved
        assert tick.late_seconds == 1400
        # Phase preserved: still on the T0+600k grid, next boundary in future.
        assert after.next_due_at == T0 + 2400
        assert (after.next_due_at - T0) % 600 == 0
        assert after.next_due_at > T0 + 2000


# ---------------------------------------------------------------------------
# The verdict is emitted once; observation continues past it
# ---------------------------------------------------------------------------

class TestVerdictOnceThenKeepObserving:
    def test_the_first_tick_is_the_verdict(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            tick = kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            after = kb.get_observation_timer(conn, timer.id)
        assert tick.seq == 1 and tick.verdict is True
        assert after.verdict_emitted is True
        assert after.verdict_emitted_at == T0 + 600

    def test_the_timer_keeps_observing_after_the_verdict(self, kanban_home):
        """Failure shape 1, pinned.

        The emission must not be the end of the timer. State stays
        ``observing`` and the next boundary is armed in the same transaction
        that wrote the verdict.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            after = kb.get_observation_timer(conn, timer.id)

            assert after.state == kb.OBSERVATION_STATE_OBSERVING
            assert after.observing is True
            assert after.next_due_at == T0 + 1200
            assert after.closed_at is None

            second = kb.emit_observation_tick(conn, timer.id, now=T0 + 1200)
            third = kb.emit_observation_tick(conn, timer.id, now=T0 + 1800)

        assert second.seq == 2 and second.verdict is False
        assert third.seq == 3 and third.verdict is False

    def test_no_second_verdict_is_ever_emitted(self, kanban_home):
        """Exactly one ``verdict=1`` row, however many ticks are emitted."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            for k in range(1, 11):
                kb.emit_observation_tick(conn, timer.id, now=T0 + 600 * k)
            ticks = kb.observation_ticks(conn, timer.id)
            after = kb.get_observation_timer(conn, timer.id)

        assert len(ticks) == 10
        assert sum(1 for t in ticks if t.verdict) == 1
        assert ticks[0].verdict is True
        assert after.verdict_emitted_at == T0 + 600  # never rewritten

    def test_a_retry_at_the_same_due_instant_emits_nothing(self, kanban_home):
        """One emitter, retried — the shape a crashed-and-restarted observer has.

        This exits through the due-time check (``next_due_at`` was already
        advanced past T0+600 by the first emission), not through the CAS. It is
        recorded that way deliberately: the guarantee is "one tick per due
        instant", and this is the path that carries it for a sequential retry.
        The genuinely concurrent case is
        :meth:`TestConcurrentEmitters.test_only_one_emitter_discharges_a_due_instant`.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            first = kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            again = kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            ticks = kb.observation_ticks(conn, timer.id)

        assert first is not None and first.seq == 1
        assert again is None
        assert len(ticks) == 1

    def test_the_tick_ledger_is_keyed_and_cannot_hold_a_duplicate(
        self, kanban_home
    ):
        """The suppression is a database constraint, not caller discipline."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_ticks "
                    "(timer_id, seq, due_at, emitted_at, missed, verdict) "
                    "VALUES (?, 1, ?, ?, 0, 1)",
                    (timer.id, T0 + 600, T0 + 600),
                )

    def test_the_emission_is_labelled_a_recheck_not_a_declaration(
        self, kanban_home
    ):
        """"A clock produced a verdict instead of a question."

        The event a governor reads must say ``recheck``. A label like
        ``DEFECT-ADVISORY`` on a 600 s tick is the same mistake wearing a
        different word.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=T0)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            payloads = _events(conn, tid, "observation_tick")

        assert len(payloads) == 1
        assert payloads[0]["emission"] == "recheck"
        assert payloads[0]["seq"] == 1
        assert payloads[0]["next_due_at"] == T0 + 1200
        # The tick states the facts; it does not adjudicate them.
        assert "stalled" not in json.dumps(payloads[0]).lower()


# ---------------------------------------------------------------------------
# Recovery after process / session interruption
# ---------------------------------------------------------------------------

class TestSurvivesInterruption:
    def test_a_fresh_process_resumes_the_same_timer(self, kanban_home):
        """Each ``connect_closing`` block is a separate process's view.

        Nothing is carried in memory between them: the timer's entire state is
        the row.
        """
        with kb.connect_closing() as conn:
            timer_id = _armed(conn).id

        # Process 1 emits the verdict, then dies.
        with kb.connect_closing() as conn:
            first = kb.emit_observation_tick(conn, timer_id, now=T0 + 600)

        # Process 2 knows nothing about process 1 except what is on disk.
        with kb.connect_closing() as conn:
            resumed = kb.get_observation_timer(conn, timer_id)
            second = kb.emit_observation_tick(conn, timer_id, now=T0 + 1200)

        with kb.connect_closing() as conn:
            ticks = kb.observation_ticks(conn, timer_id)

        assert first.seq == 1 and first.verdict is True
        assert resumed.tick_count == 1
        assert resumed.next_due_at == T0 + 1200
        assert resumed.state == kb.OBSERVATION_STATE_OBSERVING
        assert second.seq == 2 and second.verdict is False
        assert [t.seq for t in ticks] == [1, 2]

    def test_a_crash_between_emissions_replays_nothing(self, kanban_home):
        """The restarted process must not re-emit the verdict.

        This is the concrete regression for "the watcher restarted and
        re-announced": ``verdict_emitted_at`` is persisted, so seq 1 cannot
        happen twice however many times the observer is restarted.
        """
        with kb.connect_closing() as conn:
            timer_id = _armed(conn).id
            kb.emit_observation_tick(conn, timer_id, now=T0 + 600)

        for restart in range(4):
            with kb.connect_closing() as conn:
                # A restarting observer that re-runs the cycle at a time when
                # nothing new is owed must emit nothing at all.
                assert kb.run_observation_cycle(conn, now=T0 + 900) == []

        with kb.connect_closing() as conn:
            ticks = kb.observation_ticks(conn, timer_id)
        assert [t.seq for t in ticks] == [1]

    def test_due_ness_does_not_depend_on_a_live_process(self, kanban_home):
        """Failure shape 3, pinned.

        Neither presence nor absence of a process is consulted, so a watcher
        that was believed dead but kept running, and one that was believed
        alive but had exited, are both irrelevant to what the board says is
        owed. Enforced by making a process probe raise.
        """
        with kb.connect_closing() as conn:
            timer_id = _armed(conn).id

        def _no_process(*_a, **_k):  # pragma: no cover - must not be reached
            raise AssertionError("the timer probed a live process")

        with kb.connect_closing() as conn:
            original = os.kill
            os.kill = _no_process
            try:
                due = kb.due_observation_timers(conn, now=T0 + 600)
                tick = kb.emit_observation_tick(conn, timer_id, now=T0 + 600)
            finally:
                os.kill = original

        assert [t.id for t in due] == [timer_id]
        assert tick is not None and tick.seq == 1

    def test_an_orphaned_timer_is_still_found_by_the_due_scan(self, kanban_home):
        """The armer is gone; the observation is not.

        A timer armed inside a session that has since ended has no process
        behind it and no session to resolve. It is returned by the due scan
        exactly like any other, which is what stops an observation from
        disappearing with the thing that started it.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="armed in a dead session", assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE,
                session_id="20260903_103059_b4cd01",
            )
            timer_id = kb.arm_observation_timer(conn, tid, now=T0).id

        with kb.connect_closing() as conn:
            due = kb.due_observation_timers(conn, now=T0 + 600)
            ownership = kb.resolve_task_ownership(conn, tid)

        assert [t.id for t in due] == [timer_id]
        assert ownership.resolved is True


# ---------------------------------------------------------------------------
# A timer never disappears silently
# ---------------------------------------------------------------------------

class TestNoSilentDisappearance:
    def test_emitting_never_closes_a_timer(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            for k in range(1, 4):
                kb.emit_observation_tick(conn, timer.id, now=T0 + 600 * k)
            after = kb.get_observation_timer(conn, timer.id)
        assert after.state == kb.OBSERVATION_STATE_OBSERVING
        assert after.closed_at is None and after.closed_reason is None

    def test_the_cycle_driver_closes_nothing(self, kanban_home):
        """Failure shape 1 again, at the driver.

        An observer that runs one cycle and exits must leave every timer armed
        for its next interval.
        """
        with kb.connect_closing() as conn:
            a = _armed(conn)
            b = _armed(conn)
            ticks = kb.run_observation_cycle(conn, now=T0 + 600)
            states = [
                kb.get_observation_timer(conn, t.id) for t in (a, b)
            ]
        assert sorted(t.timer_id for t in ticks) == sorted([a.id, b.id])
        assert all(s.observing for s in states)
        assert all(s.next_due_at == T0 + 1200 for s in states)

    def test_a_cycle_run_twice_at_the_same_instant_emits_once(self, kanban_home):
        with kb.connect_closing() as conn:
            _armed(conn)
            _armed(conn)
            first = kb.run_observation_cycle(conn, now=T0 + 600)
            second = kb.run_observation_cycle(conn, now=T0 + 600)
        assert len(first) == 2
        assert second == []

    def test_closing_requires_an_explicit_reason(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            for blank in ("", "   ", None):
                with pytest.raises(ValueError):
                    kb.close_observation_timer(conn, timer.id, reason=blank)
            still = kb.get_observation_timer(conn, timer.id)
        assert still.observing is True

    def test_closing_records_the_reason_on_the_card(self, kanban_home):
        """The end of an observation is as discoverable as its start."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=T0)
            closed = kb.close_observation_timer(
                conn, timer.id, reason="verified; observation complete",
                now=T0 + 1200,
            )
            after = kb.get_observation_timer(conn, timer.id)
            armed_events = _events(conn, tid, "observation_timer_armed")
            close_events = _events(conn, tid, "observation_timer_closed")

        assert closed is True
        assert after.state == kb.OBSERVATION_STATE_CLOSED
        assert after.closed_at == T0 + 1200
        assert after.closed_reason == "verified; observation complete"
        assert len(armed_events) == 1 and len(close_events) == 1
        assert close_events[0]["reason"] == "verified; observation complete"

    def test_a_closed_timer_is_not_due_and_emits_nothing(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.close_observation_timer(conn, timer.id, reason="done", now=T0 + 10)
            assert kb.due_observation_timers(conn, now=T0 + 100_000) == []
            assert kb.emit_observation_tick(
                conn, timer.id, now=T0 + 100_000) is None
            assert kb.run_observation_cycle(conn, now=T0 + 100_000) == []

    def test_closing_twice_is_reported_not_silently_accepted(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            assert kb.close_observation_timer(
                conn, timer.id, reason="first", now=T0 + 10) is True
            assert kb.close_observation_timer(
                conn, timer.id, reason="second", now=T0 + 20) is False
            after = kb.get_observation_timer(conn, timer.id)
        # The first reason stands; a second close does not overwrite history.
        assert after.closed_reason == "first" and after.closed_at == T0 + 10

    def test_an_unknown_timer_raises_rather_than_reporting_nothing_owed(
        self, kanban_home
    ):
        """"No such timer" must not be reportable as "nothing was due"."""
        with kb.connect_closing() as conn:
            with pytest.raises(kb.ObservationTimerNotFound):
                kb.emit_observation_tick(conn, "obs_missing", now=T0 + 600)
            with pytest.raises(kb.ObservationTimerNotFound):
                kb.close_observation_timer(conn, "obs_missing", reason="x")
            with pytest.raises(kb.ObservationTimerNotFound):
                kb.observation_ticks(conn, "obs_missing")
            assert kb.get_observation_timer(conn, "obs_missing") is None


# ---------------------------------------------------------------------------
# Genuine concurrency
# ---------------------------------------------------------------------------

class TestConcurrentEmitters:
    def test_only_one_emitter_discharges_a_due_instant(self, kanban_home):
        """Eight real threads, eight connections, one due instant, one tick.

        The scenario the 2026-09-03 incident actually produced: a watcher
        believed stopped kept running alongside its replacement, and both
        consumed the same probe. Here both racers are real — separate
        connections, started together on a barrier — and the board is what
        decides which one emits.
        """
        import threading

        with kb.connect_closing() as conn:
            timer_id = _armed(conn).id

        racers = 8
        barrier = threading.Barrier(racers)
        results: list[object] = [None] * racers
        errors: list[BaseException] = []

        def race(i: int) -> None:
            try:
                with kb.connect_closing() as conn:
                    barrier.wait(timeout=30)
                    results[i] = kb.emit_observation_tick(
                        conn, timer_id, now=T0 + 600,
                    )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=race, args=(i,)) for i in range(racers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, f"emitters raised: {errors!r}"
        emitted = [r for r in results if r is not None]

        with kb.connect_closing() as conn:
            ticks = kb.observation_ticks(conn, timer_id)
            after = kb.get_observation_timer(conn, timer_id)

        # Exactly one emitter won; the other seven correctly emitted nothing.
        assert len(emitted) == 1
        assert len(ticks) == 1 and ticks[0].seq == 1 and ticks[0].verdict is True
        assert after.tick_count == 1
        # And the losers did not end the observation on their way out.
        assert after.observing is True and after.next_due_at == T0 + 1200

    def test_concurrent_emitters_across_two_due_instants_stay_ordered(
        self, kanban_home
    ):
        """A backlog of two boundaries yields two ticks, in sequence, no gaps."""
        import threading

        with kb.connect_closing() as conn:
            timer_id = _armed(conn).id

        def emit_at(when: int) -> None:
            with kb.connect_closing() as conn:
                for _ in range(4):
                    kb.emit_observation_tick(conn, timer_id, now=when)

        threads = [
            threading.Thread(target=emit_at, args=(when,))
            for when in (T0 + 600, T0 + 1200, T0 + 600, T0 + 1200)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        with kb.connect_closing() as conn:
            ticks = kb.observation_ticks(conn, timer_id)

        with kb.connect_closing() as conn:
            after = kb.get_observation_timer(conn, timer_id)

        # Both boundaries are accounted for, and the shape depends on who won
        # the race — which is the one thing this test must NOT pin.
        #
        #   * a ``T0+600`` racer first  → two ticks, missed=0 each;
        #   * a ``T0+1200`` racer first → ONE tick, ``missed=1``, because
        #     collapsing a backlog into a single emission that records what it
        #     swallowed is the contracted outage behaviour (see
        #     ``test_a_long_outage_emits_once_and_records_what_was_missed``).
        #
        # Asserting ``len(ticks) == 2`` pinned the first ordering and failed on
        # the second roughly 15% of the time — an A/B over 40 rounds measured
        # 7/40 before the 2026-09-03 trigger work and 6/40 after, so the race
        # is in these four threads, not in the mechanism.
        #
        # What is invariant under either ordering, and is what the test was
        # always trying to say: every due instant is discharged exactly once
        # and none is silently dropped. ``1 + missed`` is the number of
        # boundaries a tick accounts for, so the sum below is the total, and it
        # is strictly stronger than the ``<= 2`` an earlier draft used — that
        # draft passed on a single tick with ``missed=0``, i.e. on a genuinely
        # dropped boundary, which this arithmetic rejects.
        assert sum(1 + t.missed for t in ticks) == 2
        assert [t.seq for t in ticks] == list(range(1, len(ticks) + 1))
        assert ticks[0].due_at == T0 + 600
        assert sum(1 for t in ticks if t.verdict) == 1 and ticks[0].verdict
        assert len({t.due_at for t in ticks}) == len(ticks)
        # And the timer is re-armed on the original grid past both boundaries,
        # under either ordering. A dropped boundary would leave this at T0+1200.
        assert after.next_due_at == T0 + 1800
        assert after.observing


# ---------------------------------------------------------------------------
# Independence between timers, and the evidence surface
# ---------------------------------------------------------------------------

class TestMultipleTimers:
    def test_timers_are_independent(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            a = kb.arm_observation_timer(conn, tid, kind="evidence", now=T0)
            b = kb.arm_observation_timer(
                conn, tid, kind="verifier-return", now=T0 + 300,
            )
            due = kb.due_observation_timers(conn, now=T0 + 600)
            kb.emit_observation_tick(conn, a.id, now=T0 + 600)
            b_after = kb.get_observation_timer(conn, b.id)

        assert [t.id for t in due] == [a.id]
        assert b.next_due_at == T0 + 900
        assert b_after.tick_count == 0 and b_after.verdict_emitted is False

    def test_a_tick_snapshot_is_json_serialisable(self, kanban_home):
        """Ticks have to survive being written into an evidence packet."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            tick = kb.emit_observation_tick(
                conn, timer.id, now=T0 + 600,
                payload={"probe": "regression suite", "exit_code": 0},
            )
            reread = kb.observation_ticks(conn, timer.id)[0]

        round_tripped = json.loads(json.dumps(tick.as_dict()))
        assert round_tripped["seq"] == 1
        assert round_tripped["payload"]["exit_code"] == 0
        # The payload is persisted, not just returned to the caller.
        assert reread.payload == {"probe": "regression suite", "exit_code": 0}

    def test_the_timer_snapshot_is_immutable(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
        with pytest.raises((AttributeError, TypeError)):
            timer.next_due_at = 0  # type: ignore[misc]


class TestTheDatabaseEnforcesTheInvariants:
    """The Python guards bind callers that use them. These bind everyone.

    Every test here bypasses the module API entirely and writes raw SQL, which
    is the shape independent ``codex_verify`` review used to defeat the first
    draft: with the 600 s check living only in ``arm_observation_timer``, a
    direct ``INSERT`` could mint a 900-second "observation timer" that reads at
    query time exactly like a correctly armed one.
    """

    def test_the_database_rejects_a_non_600_interval_on_insert(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            for bad in (60, 900, 1800):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO observation_timers "
                        "(id, task_id, kind, interval_seconds, created_at, "
                        " next_due_at, tick_count, state) "
                        "VALUES (?, ?, 'smuggled', ?, ?, ?, 0, 'observing')",
                        (f"obs_bad{bad}", tid, bad, T0, T0 + bad),
                    )
            assert kb.task_observation_timers(conn, tid) == []

    def test_the_database_rejects_widening_an_armed_interval(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE observation_timers SET interval_seconds = 3600 "
                    "WHERE id = ?",
                    (timer.id,),
                )
            assert kb.get_observation_timer(
                conn, timer.id).interval_seconds == 600

    def test_the_python_constant_and_the_sql_literal_agree(self, kanban_home):
        """The trigger hard-codes 600; the module exports it. Pin them together.

        SQL cannot read the Python constant, so this is the only thing standing
        between the two definitions and a silent divergence.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            # The constant's value is accepted by the trigger...
            conn.execute(
                "INSERT INTO observation_timers "
                "(id, task_id, kind, interval_seconds, created_at, next_due_at, "
                " tick_count, state) VALUES ('obs_probe', ?, 'probe', ?, ?, ?, "
                " 0, 'observing')",
                (tid, INTERVAL, T0, T0 + INTERVAL),
            )
            # ...and one second either side of it is not.
            for off in (-1, 1):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE observation_timers SET interval_seconds = ? "
                        "WHERE id = 'obs_probe'",
                        (INTERVAL + off,),
                    )

    def test_the_verdict_stamp_cannot_be_cleared_or_rewritten(self, kanban_home):
        """A restarted observer must not be able to re-announce.

        Clearing the stamp is the direct-SQL route to a second verdict, so the
        column is append-once at the storage layer, not only in the emission
        path.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            for attempt in (None, T0 + 9999):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE observation_timers SET verdict_emitted_at = ? "
                        "WHERE id = ?",
                        (attempt, timer.id),
                    )
            after = kb.get_observation_timer(conn, timer.id)
        assert after.verdict_emitted_at == T0 + 600

    def test_a_closed_timer_cannot_be_reopened_by_raw_sql(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.close_observation_timer(
                conn, timer.id, reason="observation complete", now=T0 + 10,
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE observation_timers SET state = 'observing' "
                    "WHERE id = ?",
                    (timer.id,),
                )
            after = kb.get_observation_timer(conn, timer.id)
        assert after.state == kb.OBSERVATION_STATE_CLOSED

    def test_an_emitted_tick_cannot_be_rewritten(self, kanban_home):
        """A tick is what was reported. It is not editable after the fact."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            for sql, params in (
                ("UPDATE observation_ticks SET verdict = 0 WHERE timer_id = ?",
                 (timer.id,)),
                ("UPDATE observation_ticks SET due_at = ? WHERE timer_id = ?",
                 (T0, timer.id)),
                ("UPDATE observation_ticks SET missed = 99 WHERE timer_id = ?",
                 (timer.id,)),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
            tick = kb.observation_ticks(conn, timer.id)[0]
        assert tick.verdict is True and tick.due_at == T0 + 600 and tick.missed == 0

    def test_deleting_a_task_takes_its_timers_with_it(self, kanban_home):
        """A timer must not outlive the card it observes.

        The mirror image of the failure this mechanism fixes: an orphaned timer
        would be due forever and point at nothing, and the due scan would keep
        surfacing it.
        """
        with kb.connect_closing() as conn:
            keep = _task(conn, "survivor")
            doomed = _task(conn, "to be deleted")
            kept_timer = kb.arm_observation_timer(conn, keep, now=T0)
            doomed_timer = kb.arm_observation_timer(conn, doomed, now=T0)
            kb.emit_observation_tick(conn, doomed_timer.id, now=T0 + 600)

            assert kb.delete_task(conn, doomed) is True

            assert kb.get_observation_timer(conn, doomed_timer.id) is None
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM observation_ticks WHERE timer_id = ?",
                (doomed_timer.id,),
            ).fetchone()["n"] == 0
            # The survivor is untouched and still due on its own schedule.
            assert [t.id for t in kb.due_observation_timers(
                conn, now=T0 + 600)] == [kept_timer.id]


class TestTheRawSQLHolesFoundByReview:
    """The holes the 2026-09-03 independent ``codex_verify`` review found.

    Every invariant above was real, and every one of these was a way around a
    *different* invariant that the emission path upheld and the storage layer
    did not. The review's finding, restated: a guarantee that only holds for
    callers who use the API is not a guarantee, it is a convention with a
    docstring — which is the exact thing this card exists to replace.

    Each test writes raw SQL against a real board, exactly as the review did.
    """

    def test_a_second_verdict_tick_cannot_be_inserted_at_another_seq(
        self, kanban_home
    ):
        """``PRIMARY KEY (timer_id, seq)`` deduplicates; it does not limit.

        The review's route: seq 1 legitimately carries the verdict, so mint a
        seq 2 that also carries one. Both rows read as "the verdict" at query
        time and nothing in the schema preferred either.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_ticks "
                    "(timer_id, seq, due_at, emitted_at, missed, verdict) "
                    "VALUES (?, 2, ?, ?, 0, 1)",
                    (timer.id, T0 + 1200, T0 + 1200),
                )
            verdicts = [t for t in kb.observation_ticks(conn, timer.id) if t.verdict]
        assert len(verdicts) == 1 and verdicts[0].seq == 1

    def test_a_verdict_cannot_ride_any_sequence_but_the_first(self, kanban_home):
        """Even on a timer that has never emitted, only seq 1 may declare."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_ticks "
                    "(timer_id, seq, due_at, emitted_at, missed, verdict) "
                    "VALUES (?, 7, ?, ?, 0, 1)",
                    (timer.id, T0 + 600, T0 + 600),
                )
            assert kb.observation_ticks(conn, timer.id) == []

    def test_a_non_verdict_tick_is_still_insertable_at_any_seq(self, kanban_home):
        """The guard constrains verdicts, not the ledger.

        Pinned because a trigger that over-blocks would break outage replay,
        which legitimately writes a later ``seq`` with ``verdict = 0``.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            conn.execute(
                "INSERT INTO observation_ticks "
                "(timer_id, seq, due_at, emitted_at, missed, verdict) "
                "VALUES (?, 4, ?, ?, 0, 0)",
                (timer.id, T0 + 2400, T0 + 2400),
            )
            assert [t.seq for t in kb.observation_ticks(conn, timer.id)] == [4]

    @pytest.mark.parametrize(
        "column, value",
        [
            ("tick_count", 9),
            ("last_tick_at", T0),
            ("verdict_emitted_at", T0),
            ("closed_at", T0),
            ("closed_reason", "already done"),
        ],
    )
    def test_a_timer_cannot_be_born_having_already_done_work(
        self, kanban_home, column, value
    ):
        """A forged timer that reads as though it has already reported.

        The review's point: with only the interval constrained, a direct
        INSERT could mint a row carrying a spent verdict stamp and a tick
        count, which at read time is indistinguishable from a timer that
        genuinely emitted — and, worse, is no longer owed its verdict.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_timers "
                    f"(id, task_id, kind, interval_seconds, created_at, "
                    f" next_due_at, state, {column}) "
                    "VALUES ('obs_forged', ?, 'forged', 600, ?, ?, "
                    "'observing', ?)",
                    (tid, T0, T0 + 600, value),
                )
            assert kb.task_observation_timers(conn, tid) == []

    def test_a_timer_cannot_be_born_off_the_600_second_grid(self, kanban_home):
        """Interval 600 with a due time that is not one interval out.

        Passes the interval trigger and still schedules the first recheck
        wherever the forger liked — including immediately, or never.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            for bad_due in (T0, T0 + 1, T0 + 599, T0 + 601, T0 + 86_400):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO observation_timers "
                        "(id, task_id, kind, interval_seconds, created_at, "
                        " next_due_at, tick_count, state) "
                        "VALUES ('obs_offgrid', ?, 'offgrid', 600, ?, ?, 0, "
                        "'observing')",
                        (tid, T0, bad_due),
                    )
            assert kb.task_observation_timers(conn, tid) == []

    def test_a_timer_cannot_be_born_closed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_timers "
                    "(id, task_id, kind, interval_seconds, created_at, "
                    " next_due_at, tick_count, state) "
                    "VALUES ('obs_dead', ?, 'dead', 600, ?, ?, 0, 'closed')",
                    (tid, T0, T0 + 600),
                )
            assert kb.task_observation_timers(conn, tid) == []

    def test_arming_through_the_api_still_satisfies_the_shape_guard(
        self, kanban_home
    ):
        """The guard constrains forgery, not the legitimate path.

        Without this, the previous six tests would also pass against a trigger
        that simply refused every INSERT.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            assert timer.state == kb.OBSERVATION_STATE_OBSERVING
            assert timer.tick_count == 0
            assert timer.verdict_emitted_at is None
            assert timer.next_due_at == timer.created_at + INTERVAL

    @pytest.mark.parametrize("reason", [None, "", "   "])
    def test_a_timer_cannot_be_closed_without_a_recorded_reason(
        self, kanban_home, reason
    ):
        """Failure shape 2, reached by UPDATE instead of by silence.

        ``close_observation_timer`` demanded a reason; raw SQL did not, so an
        observation could stop with nothing on the row saying why — which
        reads, later, exactly like an observation that had nothing to report.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE observation_timers "
                    "SET state = 'closed', closed_at = ?, closed_reason = ? "
                    "WHERE id = ?",
                    (T0 + 10, reason, timer.id),
                )
            after = kb.get_observation_timer(conn, timer.id)
        assert after.observing and after.closed_reason is None

    def test_a_timer_cannot_be_closed_without_a_closed_at(self, kanban_home):
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE observation_timers SET state = 'closed', "
                    "closed_reason = 'quietly' WHERE id = ?",
                    (timer.id,),
                )
            assert kb.get_observation_timer(conn, timer.id).observing

    def test_a_recorded_closure_reason_cannot_be_erased_afterwards(
        self, kanban_home
    ):
        """The same hole reached in two statements instead of one."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.close_observation_timer(
                conn, timer.id, reason="verification returned", now=T0 + 10,
            )
            for sql, params in (
                ("UPDATE observation_timers SET closed_reason = NULL "
                 "WHERE id = ?", (timer.id,)),
                ("UPDATE observation_timers SET closed_reason = '' "
                 "WHERE id = ?", (timer.id,)),
                ("UPDATE observation_timers SET closed_at = NULL "
                 "WHERE id = ?", (timer.id,)),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
            after = kb.get_observation_timer(conn, timer.id)
        assert after.closed_reason == "verification returned"
        assert after.closed_at == T0 + 10

    def test_an_observing_timer_cannot_simply_be_deleted(self, kanban_home):
        """The most direct disappearance of all, and it was unguarded.

        ``DELETE FROM observation_timers`` ended a live observation leaving no
        row, no reason and no event — indistinguishable from a timer that was
        never armed.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM observation_timers WHERE id = ?", (timer.id,)
                )
            assert kb.get_observation_timer(conn, timer.id).observing

    def test_the_ledger_of_an_observing_timer_cannot_be_purged(self, kanban_home):
        """Deleting the ticks would erase the evidence of what was reported."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM observation_ticks WHERE timer_id = ?",
                    (timer.id,),
                )
            assert len(kb.observation_ticks(conn, timer.id)) == 1

    def test_ledger_retention_works_once_the_timer_is_properly_closed(
        self, kanban_home
    ):
        """Purging an old LEDGER stays legitimate maintenance once closed.

        The timer row itself does not go with it — see the next test. The split
        is deliberate: the ledger is bulk (one row per 600 s, forever), while
        the timer row is the single place the closure reason lives.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            kb.close_observation_timer(
                conn, timer.id, reason="observation complete", now=T0 + 700,
            )
            conn.execute(
                "DELETE FROM observation_ticks WHERE timer_id = ?", (timer.id,)
            )
            assert kb.observation_ticks(conn, timer.id) == []
            after = kb.get_observation_timer(conn, timer.id)
        assert after.closed_reason == "observation complete"

    def test_closing_then_deleting_cannot_erase_the_reason_it_just_recorded(
        self, kanban_home
    ):
        """The second review's objection to the delete guard, closed.

        Its point was exact: demanding a reason before deletion is hollow if the
        very next statement deletes the row holding it. So a timer now cannot be
        deleted at all while the card it observes is still on the board. The
        only way a timer row leaves is as a cascade of its own card's deletion —
        a destructive operation an operator explicitly asked for, which takes
        the card's whole history with it by design.

        The case this closes is the other one: ending an observation quietly
        while its subject is still live.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.close_observation_timer(
                conn, timer.id, reason="looked fine to me", now=T0 + 10,
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM observation_timers WHERE id = ?", (timer.id,)
                )
            after = kb.get_observation_timer(conn, timer.id)
        assert after.closed_reason == "looked fine to me"

    def test_only_one_observing_timer_per_task_and_kind(self, kanban_home):
        """The second review's other finding: verdict-once was only per TIMER.

        Nothing stopped a second 'recheck' timer being armed on the same card,
        and each would emit its own seq-1 verdict. Two live observations of the
        same kind on one card are not redundancy — they are two clocks that
        disagree, with no way for a reader to tell which one the contract meant.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            first = kb.arm_observation_timer(conn, tid, now=T0)
            with pytest.raises(sqlite3.IntegrityError):
                kb.arm_observation_timer(conn, tid, now=T0)
            observing = kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )
            assert [t.id for t in observing] == [first.id]

    def test_a_different_kind_is_still_allowed_on_the_same_card(self, kanban_home):
        """The index is scoped to (task, kind), which is what ``kind`` is for."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            a = kb.arm_observation_timer(conn, tid, kind="recheck", now=T0)
            b = kb.arm_observation_timer(conn, tid, kind="verification", now=T0)
            observing = kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )
        assert sorted(t.id for t in observing) == sorted([a.id, b.id])

    def test_a_timer_cannot_be_re_homed_onto_another_card_or_nothing(
        self, kanban_home
    ):
        """The move that defeated the delete guard, found by the third review.

        ``trg_obs_timer_no_delete_while_task_lives`` asks whether ``task_id``
        names a live card. So point ``task_id`` at a card that does not exist
        and the guard waves the DELETE through, taking the only durable copy of
        the closure reason with it. The same edit re-homes a live observation
        onto an unrelated card, or slips past the (task_id, kind) uniqueness
        index by changing either half of the key.
        """
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            other = _task(conn, "an unrelated card")
            for sql, params in (
                ("UPDATE observation_timers SET task_id = 't_nonexistent' "
                 "WHERE id = ?", (timer.id,)),
                ("UPDATE observation_timers SET task_id = ? WHERE id = ?",
                 (other, timer.id)),
                ("UPDATE observation_timers SET kind = 'something else' "
                 "WHERE id = ?", (timer.id,)),
                ("UPDATE observation_timers SET id = 'obs_stolen' WHERE id = ?",
                 (timer.id,)),
                ("UPDATE observation_timers SET created_at = 0 WHERE id = ?",
                 (timer.id,)),
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql, params)
            after = kb.get_observation_timer(conn, timer.id)
            # And the delete guard therefore still holds.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "DELETE FROM observation_timers WHERE id = ?", (timer.id,)
                )
        assert after.task_id == timer.task_id and after.kind == timer.kind
        assert after.created_at == timer.created_at

    def test_emitting_and_closing_still_work_under_the_identity_guard(
        self, kanban_home
    ):
        """The guard freezes identity, not the columns a timer legitimately writes."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            assert kb.emit_observation_tick(conn, timer.id, now=T0 + 600) is not None
            assert kb.close_observation_timer(
                conn, timer.id, reason="done", now=T0 + 700,
            ) is True

    @pytest.mark.parametrize("noisy", ["Recheck", "  recheck  ", "RECHECK"])
    def test_kind_is_normalised_so_it_cannot_forge_a_second_observation(
        self, kanban_home, noisy
    ):
        """Free-text ``kind`` was half of a uniqueness key — third review's find.

        'recheck', 'Recheck' and ' recheck ' named three different observations
        of the same thing and each got its own verdict, with the index none the
        wiser.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            first = kb.arm_observation_timer(conn, tid, kind="recheck", now=T0)
            with pytest.raises(sqlite3.IntegrityError):
                kb.arm_observation_timer(conn, tid, kind=noisy, now=T0)
            observing = kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )
        assert [t.id for t in observing] == [first.id]
        assert first.kind == "recheck"

    @pytest.mark.parametrize("variant", ["Recheck", " recheck", "RECHECK  "])
    def test_raw_sql_cannot_forge_a_duplicate_with_a_case_variant_of_kind(
        self, kanban_home, variant
    ):
        """Normalising in Python bound callers; it did not bind the database.

        The fourth review's finding: a raw ``INSERT`` with ``kind='Recheck'``
        alongside an existing ``'recheck'`` produced two logically identical
        observations of one card, each owed its own verdict, with the uniqueness
        index none the wiser. The index now keys on ``LOWER(TRIM(kind))``, which
        puts the normalisation inside the constraint where raw SQL cannot reach
        around it.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            kb.arm_observation_timer(conn, tid, kind="recheck", now=T0)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO observation_timers "
                    "(id, task_id, kind, interval_seconds, created_at, "
                    " next_due_at, tick_count, state) "
                    "VALUES ('obs_variant', ?, ?, 600, ?, ?, 0, 'observing')",
                    (tid, variant, T0, T0 + 600),
                )
            observing = kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )
        assert len(observing) == 1

    def test_the_uniqueness_migration_refuses_to_skip_enforcement_silently(
        self, kanban_home
    ):
        """A swallowed error would leave the constraint absent but believed present.

        The migration used to catch ``sqlite3.OperationalError`` broadly and log
        it, which meant a board could finish ``connect()``, be cached as
        initialised, and run for the life of the process with no uniqueness
        constraint — enforcement that reads as present and is not. Only a board
        with no ``observation_timers`` table is now tolerated, and it is
        detected rather than caught.
        """
        with kb.connect_closing() as conn:
            # No table: a legitimate no-op, and it must not raise.
            conn.execute("DROP INDEX IF EXISTS idx_obs_timer_one_observing_per_task_kind")
            conn.execute("DROP TABLE IF EXISTS observation_ticks")
            conn.execute("DROP TABLE IF EXISTS observation_timers")
            kb._migrate_observation_timer_uniqueness(conn)

        with kb.connect_closing() as conn:
            # Table present but unwritable-to for another reason: propagate.
            conn.executescript(kb.SCHEMA_SQL)
            conn.execute("DROP INDEX IF EXISTS idx_obs_timer_one_observing_per_task_kind")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS idx_obs_timer_one_observing_per_task_kind"
                " (blocker INTEGER)"
            )
            with pytest.raises(sqlite3.OperationalError):
                kb._migrate_observation_timer_uniqueness(conn)

    def test_the_uniqueness_index_upgrades_a_board_that_already_has_duplicates(
        self, kanban_home
    ):
        """The migration hazard: this hardening must not break the boards it fixes.

        Two concurrent observing timers of one kind were legal until this
        change, so a board upgraded from before it can hold them — and
        ``CREATE UNIQUE INDEX`` inside ``executescript(SCHEMA_SQL)`` would abort
        the entire schema pass on exactly those boards. Duplicates are closed
        with a recorded reason, oldest kept, never deleted.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            conn.execute("DROP INDEX IF EXISTS idx_obs_timer_one_observing_per_task_kind")
            for n, created in ((1, T0 + 50), (2, T0), (3, T0 + 100)):
                conn.execute(
                    "INSERT INTO observation_timers "
                    "(id, task_id, kind, interval_seconds, created_at, "
                    " next_due_at, tick_count, state, owner) "
                    "VALUES (?, ?, 'recheck', 600, ?, ?, 0, 'observing', 'default')",
                    (f"obs_dupe{n}", tid, created, created + 600),
                )
            conn.commit()
            assert len(kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING)) == 3

            kb._migrate_observation_timer_uniqueness(conn)
            conn.commit()

            survivors = kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )
            everything = kb.task_observation_timers(conn, tid)
            # The oldest observation survives; the others are closed, not lost.
            assert [t.id for t in survivors] == ["obs_dupe2"]
            assert len(everything) == 3
            assert {t.closed_reason for t in everything if not t.observing} == {
                "superseded_by_uniqueness_migration"
            }
            # ...and the index now exists and holds.
            with pytest.raises(sqlite3.IntegrityError):
                kb.arm_observation_timer(conn, tid, now=T0 + 200)
            # Idempotent on an already-migrated board.
            kb._migrate_observation_timer_uniqueness(conn)
            assert len(kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING)) == 1

    def test_re_arming_after_a_recorded_closure_is_still_allowed(self, kanban_home):
        """Closed timers are exempt, and that is the point of the partial index.

        A new observation is a new question and gets its own verdict; the
        previous one's closure reason stays on its own row, so the history reads
        as two observations rather than as one that mysteriously restarted.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn)
            first = kb.arm_observation_timer(conn, tid, now=T0)
            kb.close_observation_timer(
                conn, first.id, reason="first question answered", now=T0 + 10,
            )
            second = kb.arm_observation_timer(conn, tid, now=T0 + 20)
            all_timers = kb.task_observation_timers(conn, tid)
        assert len(all_timers) == 2 and second.id != first.id
        assert {t.state for t in all_timers} == {"closed", "observing"}
        assert [t.closed_reason for t in all_timers if not t.observing] == [
            "first question answered"
        ]

    def test_the_task_delete_cascade_closes_before_it_deletes(self, kanban_home):
        """The cascade obeys the same rule it would otherwise have bypassed.

        The review's finding: ``_delete_observation_timers_for_task`` dropped
        timers outright without ``close_observation_timer`` or any reason. It
        now closes with ``task_deleted`` first — and the delete trigger is what
        holds it to that, rather than this function remembering to.
        """
        with kb.connect_closing() as conn:
            doomed = _task(conn, "to be deleted")
            timer = kb.arm_observation_timer(conn, doomed, now=T0)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)
            assert kb.delete_task(conn, doomed) is True
            assert kb.get_observation_timer(conn, timer.id) is None
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM observation_ticks WHERE timer_id = ?",
                (timer.id,),
            ).fetchone()["n"] == 0

    def test_the_archived_delete_cascade_obeys_the_same_rule(self, kanban_home):
        """``delete_archived_task`` is the second caller and the same hole."""
        with kb.connect_closing() as conn:
            tid = _task(conn, "archived then purged")
            timer = kb.arm_observation_timer(conn, tid, now=T0)
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,)
            )
            assert kb.delete_archived_task(conn, tid) is True
            assert kb.get_observation_timer(conn, timer.id) is None


class TestLegacyBoardMigration:
    def test_a_board_without_the_tables_gains_them_and_keeps_its_work(
        self, kanban_home
    ):
        """Additive migration. Existing cards, events and runs are untouched.

        The live board carries preserved failure evidence (``t_aef6bbe1``,
        ``t_f9b3b48b``, ``t_c5c2929d``). A migration that rewrote or dropped
        anything would destroy exactly what those cards are kept for, so the
        test asserts the pre-existing rows survive byte-for-byte alongside the
        new tables.
        """
        with kb.connect_closing() as conn:
            tid = _task(conn, "predates observation timers")
            before = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            before_row = dict(before)
            before_events = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?",
                (tid,),
            ).fetchone()["n"]
            # Rewind the board to its pre-migration shape.
            conn.execute("DROP TABLE observation_ticks")
            conn.execute("DROP TABLE observation_timers")
            conn.commit()
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'observation_timers'"
            ).fetchone() is None

        kb.init_db()

        with kb.connect_closing() as conn:
            after_row = dict(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
            )
            after_events = conn.execute(
                "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ?",
                (tid,),
            ).fetchone()["n"]
            # The tables are back and usable on the legacy card.
            timer = kb.arm_observation_timer(conn, tid, now=T0)
            tick = kb.emit_observation_tick(conn, timer.id, now=T0 + 600)

        assert after_row == before_row
        assert after_events == before_events
        assert timer.interval_seconds == 600
        assert tick.seq == 1 and tick.verdict is True

    def test_init_is_idempotent_and_preserves_armed_timers(self, kanban_home):
        """Re-running init must not re-arm, reset or drop a live timer."""
        with kb.connect_closing() as conn:
            timer = _armed(conn)
            kb.emit_observation_tick(conn, timer.id, now=T0 + 600)

        kb.init_db()
        kb.init_db()

        with kb.connect_closing() as conn:
            after = kb.get_observation_timer(conn, timer.id)
            ticks = kb.observation_ticks(conn, timer.id)

        assert after.tick_count == 1
        assert after.next_due_at == T0 + 1200
        assert after.verdict_emitted_at == T0 + 600
        assert [t.seq for t in ticks] == [1]
