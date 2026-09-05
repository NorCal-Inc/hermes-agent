"""The 600 s recheck has a driver in the running control plane.

``test_kanban_observation_timer.py`` proves the *timer* is a mechanism: durable
across restart, exactly 600 s, verdict once, keeps observing, cannot vanish
silently. Every one of those tests calls ``run_observation_cycle`` itself.

That left the gap this file closes. Until 2026-09-03 ``run_observation_cycle``
had **no production caller at all** — a grep over the tree found it only in its
own test module. A timer that is perfect on disk and is driven only when a
model or a human remembers to drive it is still a prompt-dependent convention,
which is the thing the ruling replaced. The mechanism has to be attached to
something that is already running and does not need reminding.

That something is the dispatcher tick: ``kanban_db.dispatch_once``, called on a
timer by ``gateway/kanban_watchers.py::_kanban_dispatcher_watcher`` for as long
as the gateway is up, and by ``hermes kanban dispatch`` otherwise.

The tests below never assert that a watcher reported anything, never look at a
process, and never read wall-clock time they did not inject. They run the real
dispatcher tick against a controlled clock and read the persisted rows it left.
"""

from __future__ import annotations

import json
import sqlite3
import time as _real_time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


T0 = 1_756_900_000
INTERVAL = kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS


class _Clock:
    """A stand-in for ``kanban_db``'s ``time`` module with a settable now.

    Only ``time()`` is overridden; everything else (``sleep``, ``monotonic``,
    ``strftime``) falls through to the real module, so patching this in place
    of ``kanban_db.time`` changes what the tick *believes the hour is* and
    nothing else. Scoped to ``kanban_db``'s namespace — no other module's clock
    moves.
    """

    def __init__(self, now: int) -> None:
        self.now = int(now)

    def time(self) -> float:
        return float(self.now)

    def advance(self, seconds: int) -> int:
        self.now += int(seconds)
        return self.now

    def __getattr__(self, name):
        return getattr(_real_time, name)


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


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock(T0)
    monkeypatch.setattr(kb, "time", c)
    return c


def _tick(conn, **kwargs):
    """One real dispatcher tick that can never spawn anything.

    ``spawn_fn`` returning a PID keeps the spawn path inert; the observation
    pass under test runs before the spawn loop either way.
    """
    kwargs.setdefault("spawn_fn", lambda *a, **k: 4242)
    return kb.dispatch_once(conn, **kwargs)


def _task(conn, title: str = "observed work") -> str:
    return kb.create_task(
        conn, title=title, assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
    )


def _parked_ownerless(conn, title: str = "stripped bare") -> str:
    """A card with no resolvable owner, parked out of the spawn path.

    Parked because the dispatcher does NOT gate on ownership: a ready
    ownerless card is claimed and spawned by the same tick that reports it
    (see ``test_an_ownerless_ready_card_is_still_dispatched``), which would
    otherwise make it impossible to tell the census's writes from the spawn
    loop's.
    """
    tid = _task(conn, title)
    conn.execute(
        "UPDATE tasks SET actor_id = NULL, assignee = NULL, created_by = NULL, "
        "recovery_owner = NULL, status = 'blocked' WHERE id = ?",
        (tid,),
    )
    conn.commit()
    return tid


def _timer_row(conn, timer_id) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM observation_timers WHERE id = ?", (timer_id,)
    ).fetchone()


def _tick_rows(conn, timer_id) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM observation_ticks WHERE timer_id = ? ORDER BY seq",
        (timer_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# The tick fires the timer. Nothing else has to.
# ---------------------------------------------------------------------------

class TestTheDispatcherDrivesTheTimer:
    def test_nothing_is_emitted_before_the_boundary(self, kanban_home, clock):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            clock.advance(INTERVAL - 1)
            assert _tick(conn).observation_ticks == []
            assert _tick_rows(conn, timer.id) == []
            # And the timer is untouched, not consumed.
            assert _timer_row(conn, timer.id)["next_due_at"] == T0 + INTERVAL

    def test_the_boundary_tick_emits_the_verdict(self, kanban_home, clock):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            clock.advance(INTERVAL)
            result = _tick(conn)

            assert len(result.observation_ticks) == 1
            emitted = result.observation_ticks[0]
            assert emitted["task_id"] == tid
            assert emitted["seq"] == 1
            assert emitted["verdict"] is True
            assert emitted["due_at"] == T0 + INTERVAL
            assert emitted["missed"] == 0

            # ... and it is on disk, not merely in the return value.
            rows = _tick_rows(conn, timer.id)
            assert [r["seq"] for r in rows] == [1]
            assert rows[0]["due_at"] == T0 + INTERVAL

    def test_the_tick_keeps_observing_after_the_verdict(
        self, kanban_home, clock
    ):
        """Failure shape 1: the observation ended at its own emission."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            clock.advance(INTERVAL)
            _tick(conn)

            row = _timer_row(conn, timer.id)
            assert row["state"] == kb.OBSERVATION_STATE_OBSERVING
            assert row["closed_at"] is None
            assert row["next_due_at"] == T0 + 2 * INTERVAL

    def test_successive_ticks_emit_on_the_600_second_grid(
        self, kanban_home, clock
    ):
        """The interval is 600 across many dispatcher ticks, not just one."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            for _ in range(5):
                clock.advance(INTERVAL)
                _tick(conn)

            dues = [r["due_at"] for r in _tick_rows(conn, timer.id)]
            assert dues == [T0 + n * INTERVAL for n in range(1, 6)]
            assert set(b - a for a, b in zip(dues, dues[1:])) == {INTERVAL}

    def test_only_the_first_emission_is_a_verdict(self, kanban_home, clock):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            for _ in range(4):
                clock.advance(INTERVAL)
                _tick(conn)

            verdicts = [r["verdict"] for r in _tick_rows(conn, timer.id)]
            assert verdicts == [1, 0, 0, 0]
            row = _timer_row(conn, timer.id)
            assert row["verdict_emitted_at"] == T0 + INTERVAL

    def test_two_ticks_at_the_same_instant_emit_once(self, kanban_home, clock):
        """A dispatcher that runs twice in the same second is not a duplicate
        recheck. The CAS in ``emit_observation_tick`` carries this; the test
        proves the tick does not route around it."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            clock.advance(INTERVAL)
            first = _tick(conn)
            second = _tick(conn)

            assert len(first.observation_ticks) == 1
            assert second.observation_ticks == []
            assert len(_tick_rows(conn, timer.id)) == 1

    def test_a_dispatcher_outage_emits_once_and_records_what_was_missed(
        self, kanban_home, clock
    ):
        """The gateway being down for 40 minutes must not produce a burst of
        four rechecks, and must not silently re-phase the grid onto whenever
        the gateway happened to come back."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            clock.advance(4 * INTERVAL)  # nothing dispatched in between
            result = _tick(conn)

            assert len(result.observation_ticks) == 1
            emitted = result.observation_ticks[0]
            assert emitted["seq"] == 1
            assert emitted["due_at"] == T0 + INTERVAL   # the ORIGINAL boundary
            assert emitted["missed"] == 3
            # Still on the original grid, not on now + 600.
            assert _timer_row(conn, timer.id)["next_due_at"] == T0 + 5 * INTERVAL

    def test_the_tick_never_closes_a_timer(self, kanban_home, clock):
        """Failure shape 2: the observation disappeared and its absence read
        as 'nothing to report'."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)

            for _ in range(3):
                clock.advance(INTERVAL)
                _tick(conn)

            row = _timer_row(conn, timer.id)
            assert row["state"] == kb.OBSERVATION_STATE_OBSERVING
            assert row["closed_reason"] is None
            assert kb.task_observation_timers(
                conn, tid, state=kb.OBSERVATION_STATE_OBSERVING
            )

    def test_a_closed_timer_is_not_resurrected_by_the_tick(
        self, kanban_home, clock
    ):
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer = kb.arm_observation_timer(conn, tid, now=clock.now)
            kb.close_observation_timer(
                conn, timer.id, reason="subject verified", now=clock.now
            )

            clock.advance(10 * INTERVAL)
            assert _tick(conn).observation_ticks == []
            assert _tick_rows(conn, timer.id) == []
            assert _timer_row(conn, timer.id)["state"] == (
                kb.OBSERVATION_STATE_CLOSED
            )

    def test_the_card_gets_an_append_only_event_per_recheck(
        self, kanban_home, clock
    ):
        """Evidence a later auditor can read without the dispatcher's return
        value, a log line, or anyone's recollection."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            kb.arm_observation_timer(conn, tid, now=clock.now)

            for _ in range(2):
                clock.advance(INTERVAL)
                _tick(conn)

            payloads = [
                json.loads(r["payload"])
                for r in conn.execute(
                    "SELECT payload FROM task_events WHERE task_id = ? "
                    "AND kind = 'observation_tick' ORDER BY id",
                    (tid,),
                ).fetchall()
            ]
            assert [p["seq"] for p in payloads] == [1, 2]
            assert [p["verdict"] for p in payloads] == [True, False]
            # Named a recheck, not a declaration — the ruling's actual wording.
            assert {p["emission"] for p in payloads} == {"recheck"}


# ---------------------------------------------------------------------------
# The driver does not depend on the process, session or session-local identity
# that armed the timer.
# ---------------------------------------------------------------------------

class TestTheDriverIsSessionIndependent:
    def test_a_timer_armed_in_a_dead_session_still_fires(
        self, kanban_home, clock, monkeypatch
    ):
        """Failure shape 3 inverted: the timer must not need its originator.

        The card is armed inside a session with a full env identity; the
        session is then destroyed — env cleared, connection closed, a brand
        new connection opened, exactly as a restarted gateway would see it.
        The recheck still lands.
        """
        monkeypatch.setenv("HERMES_SESSION_ID", "20260903_103059_b4cd01")
        monkeypatch.setenv(kb.ENV_ACTOR_KIND, kb.ACTOR_KIND_HUMAN_INTERACTIVE)
        monkeypatch.setenv(kb.ENV_ACTOR_ID, "christopher")

        with kb.connect_closing() as conn:
            tid = _task(conn, "armed inside a session")
            timer_id = kb.arm_observation_timer(conn, tid, now=clock.now).id
            armed_owner = _timer_row(conn, timer_id)["owner"]

        # The session ends: identity gone, process gone, connection gone.
        for var in ("HERMES_SESSION_ID", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID):
            monkeypatch.delenv(var, raising=False)

        clock.advance(INTERVAL)
        with kb.connect_closing() as conn:
            result = _tick(conn)
            assert len(result.observation_ticks) == 1
            assert result.observation_ticks[0]["task_id"] == tid
            # The owner recorded at arming is still the owner. It was resolved
            # from persisted columns, so nothing about it moved when the
            # session that supplied it was destroyed.
            assert _timer_row(conn, timer_id)["owner"] == armed_owner
            assert kb.resolve_task_ownership(conn, tid).owner == armed_owner

    def test_dueness_survives_a_process_restart_mid_grid(
        self, kanban_home, clock
    ):
        """Emit, drop the connection entirely, reconnect, emit again."""
        with kb.connect_closing() as conn:
            tid = _task(conn)
            timer_id = kb.arm_observation_timer(conn, tid, now=clock.now).id

        clock.advance(INTERVAL)
        with kb.connect_closing() as conn:
            assert len(_tick(conn).observation_ticks) == 1

        clock.advance(INTERVAL)
        with kb.connect_closing() as conn:  # a different process would see this
            assert len(_tick(conn).observation_ticks) == 1
            rows = _tick_rows(conn, timer_id)
            assert [r["seq"] for r in rows] == [1, 2]
            assert [r["verdict"] for r in rows] == [1, 0]


# ---------------------------------------------------------------------------
# Ownership census on the tick
# ---------------------------------------------------------------------------

class TestTheTickReportsOwnershipLoss:
    def test_a_healthy_board_reports_nothing_unowned(self, kanban_home, clock):
        with kb.connect_closing() as conn:
            _task(conn, "a")
            _task(conn, "b")
            assert _tick(conn).unowned == []

    def test_an_ownerless_card_is_surfaced_by_the_tick(
        self, kanban_home, clock
    ):
        with kb.connect_closing() as conn:
            keep = _task(conn, "owned")
            lost = _task(conn, "stripped bare")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, assignee = NULL, "
                "created_by = NULL, recovery_owner = NULL WHERE id = ?",
                (lost,),
            )
            conn.commit()

            result = _tick(conn)

            assert result.unowned == [lost]
            assert keep not in result.unowned

    def test_the_census_reads_no_live_state(
        self, kanban_home, clock, monkeypatch
    ):
        """Same answer with the whole session identity torn out of the env.

        Deliberately ONE tick. Running the tick twice would not test this — the
        first tick's spawn loop assigns the card and gives it an owner, so a
        second tick legitimately reports nothing, and the test would be
        measuring the dispatcher rather than the census.
        """
        monkeypatch.setenv("HERMES_SESSION_ID", "20260903_103059_b4cd01")
        monkeypatch.setenv(kb.ENV_ACTOR_KIND, kb.ACTOR_KIND_HUMAN_INTERACTIVE)
        monkeypatch.setenv(kb.ENV_ACTOR_ID, "christopher")

        with kb.connect_closing() as conn:
            lost = _parked_ownerless(conn)
            in_session = kb.unowned_tasks(conn)

        # The session ends: env identity gone, connection gone.
        for var in ("HERMES_SESSION_ID", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID):
            monkeypatch.delenv(var, raising=False)

        with kb.connect_closing() as conn:
            assert _tick(conn).unowned == in_session == [lost]

    def test_the_census_does_not_rewrite_ownership(self, kanban_home, clock):
        """Observability only: the census reports, it does not backfill.

        Asserted on the provenance columns specifically, not on the whole row.
        Other passes of the same tick legitimately move ``status``,
        ``claim_lock``, ``current_run_id`` and ``assignee`` (the dispatcher
        owns routing and applies its own assignee fallback), and folding those
        in would make this assert the dispatcher rather than the census. A
        census that "helpfully" filled in provenance would be fabricating
        attribution for a card whose session is gone — the exact failure being
        closed.
        """
        cols = (
            "created_by", "recovery_owner", "actor_id", "actor_run_id",
            "creation_cause", "session_id",
        )
        sql = f"SELECT {', '.join(cols)} FROM tasks WHERE id = ?"
        with kb.connect_closing() as conn:
            lost = _parked_ownerless(conn)
            before = dict(conn.execute(sql, (lost,)).fetchone())

            result = _tick(conn)

            after = dict(conn.execute(sql, (lost,)).fetchone())

        assert result.unowned == [lost]
        # The ownership-source columns are the ones a backfill would touch.
        assert before["created_by"] is None
        assert before["recovery_owner"] is None
        assert before["actor_id"] is None
        assert after == before

    def test_an_ownerless_ready_card_is_still_dispatched(
        self, kanban_home, clock
    ):
        """The census is not a gate, and this proves it rather than asserting
        it in a comment.

        A ready card with no resolvable owner is reported in ``unowned`` and is
        ALSO claimed and spawned by the same tick. That is deliberate: making
        ownership a dispatch precondition would strand every card created
        before the provenance columns existed. The report is the control; the
        refusal lives at :func:`require_resolvable_ownership`, on the paths
        that create new obligations.
        """
        with kb.connect_closing() as conn:
            lost = _task(conn, "ownerless but ready")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, assignee = NULL, "
                "created_by = NULL, recovery_owner = NULL WHERE id = ?",
                (lost,),
            )
            conn.commit()

            result = _tick(conn)

            assert lost in result.unowned
            assert conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (lost,)
            ).fetchone()["status"] == "running"


# ---------------------------------------------------------------------------
# Neither pass may take a dispatcher tick down
# ---------------------------------------------------------------------------

class TestThePassesAreDefensive:
    def test_an_exploding_observation_cycle_does_not_kill_the_tick(
        self, kanban_home, clock, monkeypatch
    ):
        def _boom(*a, **k):
            raise RuntimeError("observation cycle blew up")

        monkeypatch.setattr(kb, "run_observation_cycle", _boom)
        with kb.connect_closing() as conn:
            tid = _task(conn)
            result = _tick(conn)
            assert result.observation_ticks == []
            # The rest of the tick still ran.
            assert result.promoted >= 0
            assert conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,)
            ).fetchone()["status"] in {"ready", "todo", "running"}

    def test_an_exploding_ownership_census_does_not_kill_the_tick(
        self, kanban_home, clock, monkeypatch
    ):
        def _boom(*a, **k):
            raise RuntimeError("census blew up")

        monkeypatch.setattr(kb, "unowned_tasks", _boom)
        with kb.connect_closing() as conn:
            tid = _task(conn)
            kb.arm_observation_timer(conn, tid, now=clock.now)
            clock.advance(INTERVAL)
            result = _tick(conn)

        # The census failed; the recheck it runs alongside still landed.
        assert result.unowned == []
        assert len(result.observation_ticks) == 1


# ---------------------------------------------------------------------------
# The wiring itself
# ---------------------------------------------------------------------------

def test_the_dispatcher_is_the_production_caller(kanban_home, clock):
    """Guards the regression this file exists for.

    ``run_observation_cycle`` shipped with no production caller. If a future
    edit removes the call from ``dispatch_once`` again, every behavioural test
    above would still pass against a hand-driven cycle — so assert the wiring
    directly.
    """
    import inspect

    # ``dispatch_once`` is the lock wrapper; ``_dispatch_once_locked`` is the
    # tick body it delegates to. The driver has to live in the body — a call
    # in the wrapper would run even on a tick that lost the board lock and did
    # no writes.
    src = inspect.getsource(kb._dispatch_once_locked)
    assert "run_observation_cycle(conn)" in src
    assert "unowned_tasks(conn)" in src
    assert "run_observation_cycle" not in inspect.getsource(kb.dispatch_once)


def test_a_dispatch_result_carries_both_fields_by_default():
    result = kb.DispatchResult()
    assert result.observation_ticks == []
    assert result.unowned == []
