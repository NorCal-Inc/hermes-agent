"""``abandoned`` records a card that closed without completing.

On 2026-09-07 a reconciliation found **316 closed cards with no terminal
disposition** -- 172 archived, 144 done -- so two-thirds of the board could not
distinguish finished from abandoned.

For the 144 ``done`` cards that was only an unfilled column: every one had
``completed_at`` and a ``completed`` event, and none ended on a failed verdict.

The 172 archived cards were the real defect: **there was no value that told the
truth about them.** Only 10 ever completed; 14 ended on a FAILED verdict, 16
gave up, 35 never started. ``completed`` was false, and
``overtaken_by_events`` asserts the objective is SATISFIED -- untrue, and
irreversible, so it would also have permanently barred re-entry and could have
satisfied dependencies off abandoned work.

The distinction these tests protect is the reason the value exists: an
abandoned objective is not a finished one.
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


class TestTheValueExistsAndIsUsable:
    def test_it_is_a_valid_terminal_disposition(self):
        assert kb.DISPOSITION_ABANDONED in kb.VALID_TERMINAL_DISPOSITIONS

    def test_list_tasks_accepts_it_as_a_filter(self, conn):
        """Before this, filtering for abandoned work raised ValueError."""
        tid = kb.create_task(conn, title="abandoned work", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='archived', terminal_disposition=? "
                "WHERE id=?", (kb.DISPOSITION_ABANDONED, tid),
            )
        found = kb.list_tasks(
            conn,
            terminal_disposition=kb.DISPOSITION_ABANDONED,
            include_archived=True,
        )
        assert [t.id for t in found] == [tid]

    def test_an_unknown_disposition_is_still_rejected(self, conn):
        with pytest.raises(ValueError):
            kb.list_tasks(
                conn, terminal_disposition="not_a_real_disposition",
                include_archived=True,
            )


class TestAbandonedIsNotFinished:
    """The load-bearing property. If this ever flips, the value is a lie."""

    def test_it_is_reversible(self):
        """An abandoned objective may legitimately be picked up again.

        ``overtaken_by_events`` is irreversible because the objective is
        satisfied and there is nothing left to redo. Abandonment is the
        opposite claim, so it must not bar re-entry.
        """
        assert kb.DISPOSITION_ABANDONED not in kb.IRREVERSIBLE_DISPOSITIONS

    def test_overtaken_by_events_is_still_irreversible(self):
        """Control: the distinction only means something if the other half holds."""
        assert kb.DISPOSITION_OVERTAKEN_BY_EVENTS in kb.IRREVERSIBLE_DISPOSITIONS

    def test_it_does_not_leak_into_the_irreversible_sql_predicate(self):
        """The SQL list is derived from IRREVERSIBLE_DISPOSITIONS, not written
        out per call site, so this is really a regression guard on that
        derivation staying derived.
        """
        assert "abandoned" not in kb._IRREVERSIBLE_DISPOSITION_SQL_LIST
        assert "overtaken_by_events" in kb._IRREVERSIBLE_DISPOSITION_SQL_LIST

    def test_completed_is_still_reversible_too(self):
        """`completed` was always an assertion about the LAST completion."""
        assert kb.DISPOSITION_COMPLETED not in kb.IRREVERSIBLE_DISPOSITIONS
