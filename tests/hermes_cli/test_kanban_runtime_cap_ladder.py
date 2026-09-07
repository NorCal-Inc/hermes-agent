"""Per-card runtime cap escalation ladder.

Christopher's 2026-09-07 ruling:

    "the raised cap is only for cards hitting 300. not a new default. if cards
     hit 300 twice then adjust THAT card to 600. if it hits 600 twice then 900.
     after 900 stop that card until i see it."

with one refinement: Gauntlet verifier children stop at 600 rather than 900.

Two failures this sits between. A cap set too low turns work that misses by one
second into an unbounded respawn (t_9a90c33a; 153 executors across t_0ce21cbe
and t_db21ca59). A cap raised globally removes the brake for everything, which
is the opposite of the 2026-09-06 remediation. So: time is granted only to a
card that has demonstrated twice that it needs it, one rung at a time, finitely.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_EXECUTION_ID",
        "HERMES_SESSION_ID", "HERMES_EXECUTOR_LANE", "HERMES_PROFILE",
        "HERMES_PROFILE_NAME", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID,
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


def _card(conn, *, cap=300, lane=None, status="todo"):
    tid = kb.create_task(
        conn, title="a long job", body="", assignee="default",
        max_runtime_seconds=cap,
    )
    tid = tid if isinstance(tid, str) else tid["id"]
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET executor_lane=?, status=? WHERE id=?",
            (lane, status, tid),
        )
    return tid


def _timeout(conn, tid, cap, n=1):
    """Record ``n`` timeouts at ``cap``, as ``enforce_max_runtime`` would."""
    with kb.write_txn(conn):
        for _ in range(n):
            kb._append_event(conn, tid, "timed_out", {
                "elapsed_seconds": cap + 2,
                "limit_seconds": cap,
            })


def _cap(conn, tid):
    return conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id=?", (tid,)
    ).fetchone()["max_runtime_seconds"]


def _status(conn, tid):
    r = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    return r["status"], r["block_kind"]


class TestTheRungs:
    def test_one_timeout_does_not_buy_a_rung(self, kanban_home):
        """One timeout is an incident. Two is the card telling you the cap is wrong."""
        conn = kb.connect()
        tid = _card(conn, cap=300)
        _timeout(conn, tid, 300, n=1)
        kb._runtime_cap_ladder_step(conn, tid, cap=300)
        assert _cap(conn, tid) == 300

    def test_two_timeouts_at_300_raises_that_card_to_600(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=300)
        _timeout(conn, tid, 300, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=300)
        assert _cap(conn, tid) == 600

    def test_two_timeouts_at_600_raises_to_900(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=600)
        _timeout(conn, tid, 600, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=600)
        assert _cap(conn, tid) == 900

    def test_the_tally_is_per_rung_not_lifetime(self, kanban_home):
        """A promoted card starts its new rung at zero.

        Otherwise the second rung would be spent the instant it was granted,
        and 600 and 900 would never actually be tried.
        """
        conn = kb.connect()
        tid = _card(conn, cap=600)
        _timeout(conn, tid, 300, n=2)   # spent at the PREVIOUS rung
        _timeout(conn, tid, 600, n=1)
        kb._runtime_cap_ladder_step(conn, tid, cap=600)
        assert _cap(conn, tid) == 600

    def test_the_raise_is_recorded_with_provenance(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=300)
        _timeout(conn, tid, 300, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=300)
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?", (tid,)
            )
        ]
        assert "runtime_cap_raised" in kinds


class TestTheTerminus:
    def test_after_900_the_card_stops_for_a_human(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=900)
        _timeout(conn, tid, 900, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=900)
        status, kind = _status(conn, tid)
        assert status == "blocked"
        assert kind == kb.BLOCK_KIND_RUNTIME_CAP_EXHAUSTED
        assert _cap(conn, tid) == 900, "the cap must not climb past the terminus"

    def test_the_terminus_is_distinguishable_from_a_spent_attempt_budget(
        self, kanban_home
    ):
        """Different remedies, so an operator must not have to read prose."""
        assert (
            kb.BLOCK_KIND_RUNTIME_CAP_EXHAUSTED
            != kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        )
        assert kb.BLOCK_KIND_RUNTIME_CAP_EXHAUSTED in kb.VALID_BLOCK_KINDS

    def test_a_finished_card_is_never_resurrected(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=900, status="done")
        _timeout(conn, tid, 900, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=900)
        assert _status(conn, tid)[0] == "done"


class TestVerifierChildrenStopEarly:
    def test_a_verifier_gets_the_one_rung_that_mattered(self, kanban_home):
        """301-302s misses against a 300s cap were the historical failure."""
        conn = kb.connect()
        tid = _card(conn, cap=300, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
        _timeout(conn, tid, 300, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=300)
        assert _cap(conn, tid) == 600

    def test_a_verifier_stops_at_600_rather_than_climbing_to_900(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=600, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
        _timeout(conn, tid, 600, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=600)
        assert _cap(conn, tid) == 600
        status, kind = _status(conn, tid)
        assert status == "blocked"
        assert kind == kb.BLOCK_KIND_RUNTIME_CAP_EXHAUSTED

    def test_an_ordinary_card_at_600_still_climbs(self, kanban_home):
        """The contrast that proves the verifier ceiling is lane-specific."""
        conn = kb.connect()
        tid = _card(conn, cap=600, lane=None)
        _timeout(conn, tid, 600, n=2)
        kb._runtime_cap_ladder_step(conn, tid, cap=600)
        assert _cap(conn, tid) == 900


class TestItLeavesDeliberateCapsAlone:
    def test_a_bespoke_cap_is_never_laddered(self, kanban_home):
        """7200 was set by someone who knew what they were doing."""
        conn = kb.connect()
        tid = _card(conn, cap=7200)
        _timeout(conn, tid, 7200, n=5)
        kb._runtime_cap_ladder_step(conn, tid, cap=7200)
        assert _cap(conn, tid) == 7200
        assert _status(conn, tid)[0] != "blocked"

    def test_it_is_not_a_new_default(self, kanban_home):
        """A card that never times out is never touched."""
        conn = kb.connect()
        tid = _card(conn, cap=300)
        kb._runtime_cap_ladder_step(conn, tid, cap=300)
        assert _cap(conn, tid) == 300


class TestTheVerifierPinNoLongerRevertsARaise:
    """The pin's property is "never uncapped", not "exactly 300".

    Forcing equality would have silently reverted every ladder raise on a
    verifier card -- 77 of the 90 cards historically sitting at the 300s cap,
    i.e. the ladder would have been a no-op exactly where it was needed most.
    """

    def test_a_raised_verifier_cap_survives_normalisation(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=600, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = ? "
                "WHERE id = ? AND (max_runtime_seconds IS NULL "
                "OR max_runtime_seconds < ?)",
                (kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS, tid,
                 kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS),
            )
        assert _cap(conn, tid) == 600

    def test_an_uncapped_verifier_is_still_given_the_floor(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=300, lane=kb.EXECUTOR_LANE_CODEX_VERIFY)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = NULL WHERE id=?", (tid,)
            )
            conn.execute(
                "UPDATE tasks SET max_runtime_seconds = ? "
                "WHERE id = ? AND (max_runtime_seconds IS NULL "
                "OR max_runtime_seconds < ?)",
                (kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS, tid,
                 kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS),
            )
        assert _cap(conn, tid) == kb.MANDATORY_OBSERVATION_INTERVAL_SECONDS


class TestTheCounter:
    def test_it_counts_only_timeouts_at_the_given_cap(self, kanban_home):
        conn = kb.connect()
        tid = _card(conn, cap=600)
        _timeout(conn, tid, 300, n=3)
        _timeout(conn, tid, 600, n=1)
        assert kb.runtime_cap_timeouts_at(conn, tid, 300) == 3
        assert kb.runtime_cap_timeouts_at(conn, tid, 600) == 1
        assert kb.runtime_cap_timeouts_at(conn, tid, 900) == 0
