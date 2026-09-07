"""The same error, over and over, stops the loop.

Christopher's design: an agent hits an error, the loop looks up whether it is
known, fixes it, logs it, and the agent retries. **Only a NEW error opens a new
loop.** That bound is what stops the recursion — but it needs two brakes, not
one, and only one existed.

* The **objective attempt ceiling** already existed and works. It counts
  ``task_runs`` across the objective lineage, which is append-only and so
  cannot be cleared by ``review_reopened`` or
  ``infrastructure_failure_not_counted``. Live proof on 2026-09-07: the voice
  objective read attempts=6 against limit=6.

* It cannot see *which* error. Six attempts against six different errors is
  progress. Six against the **same** error is a wrong fix being retried, and
  that should stop long before the objective budget is spent. That is this
  module.

The reset-proof property is the load-bearing one. `consecutive_failures` reads
0 on a card that has failed repeatedly, because reopening clears it — a bound
built on that counter would inherit the exact defect that made the documented
three-retry ceiling unreachable in practice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


SAME = "sqlite3.OperationalError: no such column: id at /srv/a.py:{n}"
OTHER = [
    "TimeoutError: worker exceeded 300s budget",
    "ConnectionResetError: telegram polling dropped",
    "KeyError: 'assignee' while building the dispatch payload",
]


def _failed_run(conn, tid, error):
    with kb.write_txn(conn):
        kb._synthesize_ended_run(conn, tid, outcome="crashed", error=error)


def _ready_task(conn, title="work"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


class TestTheSameErrorStopsTheLoop:
    def test_it_blocks_once_the_error_recurs_to_the_limit(self, conn):
        tid = _ready_task(conn)
        limit = kb.repeated_error_limit()
        for i in range(limit):
            _failed_run(conn, tid, SAME.format(n=i))

        assert kb.claim_task(conn, tid) is None

        row = conn.execute(
            "SELECT status, block_kind, last_failure_error FROM tasks WHERE id=?",
            (tid,),
        ).fetchone()
        assert row["status"] == "blocked"
        assert row["block_kind"] == kb.BLOCK_KIND_ERROR_RECURRED
        assert "ERROR_RECURRED" in row["last_failure_error"]

    def test_it_does_not_block_below_the_limit(self, conn):
        tid = _ready_task(conn)
        for i in range(kb.repeated_error_limit() - 1):
            _failed_run(conn, tid, SAME.format(n=i))
        assert kb.claim_task(conn, tid) is not None

    def test_different_errors_are_progress_not_recursion(self, conn):
        """Six attempts against six different errors is the loop working."""
        tid = _ready_task(conn)
        for err in OTHER:
            _failed_run(conn, tid, err)
        reached, _sig, count, _limit, _root = kb.repeated_error_ceiling_reached(
            conn, tid
        )
        assert reached is False
        assert count == 1

    def test_paths_and_line_numbers_do_not_reset_the_count(self, conn):
        """The same failure at different call sites is still the same failure.

        If it were counted as novel, this brake would never fire — which is how
        a bounded rule decays into an unbounded one.
        """
        tid = _ready_task(conn)
        for path, line in (("/a/x.py", 1), ("/b/y.py", 4242), ("/c/z.py", 7)):
            _failed_run(
                conn, tid,
                f"sqlite3.OperationalError: no such column: id at {path}:{line}",
            )
        reached, _sig, count, limit, _root = kb.repeated_error_ceiling_reached(
            conn, tid
        )
        assert count == 3 >= limit
        assert reached is True


class TestItCannotBeReset:
    def test_clearing_consecutive_failures_does_not_clear_it(self, conn):
        """The property the existing counters do not have.

        `consecutive_failures` is cleared on reopen, which is why the
        documented three-retry ceiling was unreachable in practice. This bound
        counts append-only run rows instead.
        """
        tid = _ready_task(conn)
        for i in range(kb.repeated_error_limit()):
            _failed_run(conn, tid, SAME.format(n=i))
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET consecutive_failures=0, status='ready' "
                "WHERE id=?", (tid,),
            )
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is True
        assert kb.claim_task(conn, tid) is None


class TestItTellsTheAgentWhereToLook:
    def test_the_block_reason_points_at_the_lesson_lookup(self, conn):
        """Blocking without saying what to do next is how a card dies quietly."""
        tid = _ready_task(conn)
        for i in range(kb.repeated_error_limit()):
            _failed_run(conn, tid, SAME.format(n=i))
        kb.claim_task(conn, tid)
        reason = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id=?", (tid,)
        ).fetchone()["last_failure_error"]
        assert "hermes kanban lessons --error" in reason
        assert "the fix is wrong or the cause is elsewhere" in reason

    def test_it_records_an_event(self, conn):
        tid = _ready_task(conn)
        for i in range(kb.repeated_error_limit()):
            _failed_run(conn, tid, SAME.format(n=i))
        kb.claim_task(conn, tid)
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?", (tid,)
            )
        ]
        assert "repeated_error_ceiling_reached" in kinds


class TestTheTwoBrakesAreIndependent:
    #: Genuinely different failures. An earlier version of this test used
    #: "distinct failure number {i}", which differs only by a digit — and
    #: digits are stripped before matching, so all of them normalised to one
    #: signature and the repeated-error brake fired. The normaliser was right
    #: and the test was wrong: if numbering made errors look novel, the brake
    #: would never fire in production either.
    GENUINELY_DISTINCT = [
        "TimeoutError: worker exceeded its runtime budget",
        "ConnectionResetError: telegram polling dropped mid-update",
        "KeyError: assignee missing while building the dispatch payload",
        "PermissionError: refusing to write outside the workspace root",
        "JSONDecodeError: worker emitted pretty-printed output, not one line",
        "ImportError: plugin module vanished during a concurrent build",
    ]

    def test_the_objective_ceiling_still_fires_on_distinct_errors(self, conn):
        """Different errors escape THIS brake and must still hit the budget."""
        tid = _ready_task(conn)
        limit = kb.gauntlet_objective_attempt_limit()
        assert len(self.GENUINELY_DISTINCT) >= limit
        for err in self.GENUINELY_DISTINCT[:limit]:
            _failed_run(conn, tid, err)
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is False
        assert kb._objective_attempt_ceiling_reached(conn, tid)[0] is True
        assert kb.claim_task(conn, tid) is None
        assert conn.execute(
            "SELECT block_kind FROM tasks WHERE id=?", (tid,)
        ).fetchone()["block_kind"] == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED


class TestInfrastructureFailuresAreNotCharged:
    """A rate limit is not a wrong fix.

    Caught by `test_rate_limit_exit_requeues_without_counting_failure`: the
    first version of this bound counted every run error, so a recurring
    provider 429 blocked the card. Blocking work because the provider was busy
    is the opposite of this bound's purpose.

    The codebase already states the principle for the retry counter — an
    infrastructure termination "ends a process that may have been making
    perfect progress, nothing about it is evidence that the work is wrong".
    The same carve-out applies here.
    """

    RATE = "HTTP 429 rate limited by the provider, retry after 30s"

    def _run(self, conn, tid, outcome, error):
        with kb.write_txn(conn):
            kb._synthesize_ended_run(conn, tid, outcome=outcome, error=error)

    def test_rate_limited_runs_do_not_count(self, conn):
        tid = _ready_task(conn)
        for _ in range(kb.repeated_error_limit() + 2):
            self._run(conn, tid, "rate_limited", self.RATE)
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is False
        assert kb.claim_task(conn, tid) is not None

    def test_timeouts_and_reaps_do_not_count(self, conn):
        tid = _ready_task(conn)
        for outcome in ("timed_out", "stale", "reclaimed", "spawn_failed"):
            for _ in range(kb.repeated_error_limit()):
                self._run(conn, tid, outcome, "the control plane ended this run")
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is False

    def test_a_real_crash_still_counts(self, conn):
        """Control: the carve-out must not swallow genuine failures."""
        tid = _ready_task(conn)
        for _ in range(kb.repeated_error_limit()):
            self._run(conn, tid, "crashed", SAME.format(n=1))
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is True

    def test_infrastructure_noise_does_not_mask_a_real_recurrence(self, conn):
        """Interleaved infra failures must not hide the real one."""
        tid = _ready_task(conn)
        for i in range(kb.repeated_error_limit()):
            self._run(conn, tid, "rate_limited", self.RATE)
            self._run(conn, tid, "crashed", SAME.format(n=i))
        assert kb.repeated_error_ceiling_reached(conn, tid)[0] is True
