"""Loop stops, 2026-10-15 (Christopher: no resource-draining loops).

1. Repeated independent-verification FAIL: t_7b593c5f failed nine times in
   20 minutes on one missing workstation file; each FAIL reopened a paid
   implementer run and then opened a new verifier.
2. Codex verifier quota wall: t_70087f1d / t_7d9452d0 probed every 5m20s for
   90+ minutes although Codex said "try again at 7:16 PM".
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane as rl
from tests.hermes_cli.test_kanban_self_review_recovery import (  # noqa: F401
    _events,
    _repair_and_hand_off,
    _run_verifier,
    _subject_awaiting_verification,
    kanban_home,
)

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

FAIL = "Workstation evidence file missing.\n\nVERDICT: FAIL"


def _verifier_children(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_links l JOIN tasks c ON c.id = l.child_id "
        "WHERE l.parent_id = ? AND c.executor_lane = ?",
        (tid, kb.EXECUTOR_LANE_CODEX_VERIFY),
    ).fetchone()[0]


@pytest.fixture
def advisory_budget(monkeypatch):
    monkeypatch.setattr(kb, "objective_attempt_budget_enforced", lambda: False)


@pytest.mark.usefixtures("advisory_budget")
class TestRepeatedFailHold:
    def test_first_fail_still_routes_to_rework(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(conn, tid, FAIL)
            assert kb.get_task(conn, tid).status == "ready"
            assert kb._repeated_fail_anchor(conn, tid) is None

    def test_second_consecutive_fail_holds_without_rework_or_verifier(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(conn, tid, FAIL)
            _repair_and_hand_off(conn, tid)
            _run_verifier(conn, tid, FAIL)

            task = kb.get_task(conn, tid)
            assert task.status == "review"            # not reopened: no paid rework run
            assert kb.claim_task(conn, tid) is None
            assert kb.verifier_respawn_guard_holds(conn, tid) is True
            children = _verifier_children(conn, tid)
            # The "loop test": many dispatcher ticks open nothing new.
            for _ in range(20):
                assert kb._ensure_independent_verifier_child(conn, tid, implementer="default") is None
            assert _verifier_children(conn, tid) == children
            assert len(_events(conn, tid, "verifier_respawn_refused_unchanged")) == 1
            held = [c for c in kb.list_comments(conn, tid) if c.author == "verifier-respawn-guard"]
            assert len(held) == 1 and "failed 2 times in a row" in held[0].body

    def test_executor_comment_does_not_release(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(conn, tid, FAIL)
            _repair_and_hand_off(conn, tid)
            _run_verifier(conn, tid, FAIL)
            kb.add_comment(conn, tid, "claude-lane", "re-attached the same evidence")
            kb.add_comment(conn, tid, "default", "still waiting on workstation file")
            assert kb._ensure_independent_verifier_child(conn, tid, implementer="default") is None

    def test_person_comment_releases_one_attempt(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            _run_verifier(conn, tid, FAIL)
            _repair_and_hand_off(conn, tid)
            _run_verifier(conn, tid, FAIL)
            kb.add_comment(conn, tid, "Christopher", "Changed: workstation file pushed")
            assert kb._repeated_fail_anchor(conn, tid) is None
            assert kb._ensure_independent_verifier_child(conn, tid, implementer="default") is not None

    def test_pass_resets_the_count(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _subject_awaiting_verification(conn)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {})
                kb._append_event(conn, tid, "verification_passed", {})
                kb._append_event(conn, tid, "verification_failed", {})
            assert kb._repeated_fail_anchor(conn, tid) is None


class TestCodexResetTime:
    NOW = datetime(2026, 10, 15, 18, 0).astimezone().timestamp()

    @pytest.mark.parametrize("text, expect", [
        ("...credits or try again at 7:16 pm.", datetime(2026, 10, 15, 19, 16)),
        ("You've hit your usage limit. try again at 5:00 AM.", datetime(2026, 10, 16, 5, 0)),
        ("try again at oct 17th, 2026 3:00 pm.", datetime(2026, 10, 17, 15, 0)),
        ("try again at September 3rd, 2026 1:05 AM", datetime(2026, 9, 3, 1, 5)),
    ])
    def test_parses_codex_messages(self, text, expect):
        got = rl._parse_codex_reset_at(text, now=self.NOW)
        assert got == int(expect.astimezone().timestamp())

    @pytest.mark.parametrize("text", ["rate limit", "try again later", "", "try again at 25:99 PM"])
    def test_unparseable_keeps_fixed_cooldown(self, text):
        assert rl._parse_codex_reset_at(text, now=self.NOW) is None

    def _defer(self, conn, reset_at):
        tid = kb.create_task(conn, title="verify", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert kb.defer_rate_limited_task(
            conn, tid, reason="Codex provider capacity exhausted",
            expected_run_id=claimed.current_run_id, reset_at=reset_at,
        )
        return tid

    def test_reset_time_holds_past_the_fixed_cooldown(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            reset = int(time.time()) + 3600
            tid = self._defer(conn, reset)
            meta = json.loads(conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ?", (tid,),
            ).fetchone()[0])
            assert meta["quota_hold"]["reset_at"] == reset
            # Loop test: across the next 55 minutes of ticks, never respawns.
            real = time.time
            for minutes in range(0, 56, 5):
                monkeypatch.setattr(time, "time", lambda m=minutes: real() + m * 60)
                assert kb.check_respawn_guard(conn, tid) in ("quota_reset_hold", "rate_limit_cooldown")
            monkeypatch.setattr(time, "time", lambda: real() + 3700)
            assert kb.check_respawn_guard(conn, tid) is None   # one probe after reset

    def test_no_reset_time_keeps_existing_cooldown(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            tid = self._defer(conn, None)
            meta = json.loads(conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ?", (tid,),
            ).fetchone()[0])
            assert "quota_hold" not in meta
            assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
            real = time.time
            monkeypatch.setattr(time, "time", lambda: real() + 400)
            assert kb.check_respawn_guard(conn, tid) is None


def test_enforced_budget_keeps_the_attempt_ceiling_as_the_brake(kanban_home):
    with kb.connect_closing() as conn:
        assert kb.objective_attempt_budget_enforced() is True
        tid = _subject_awaiting_verification(conn)
        _run_verifier(conn, tid, FAIL)
        _repair_and_hand_off(conn, tid)
        _run_verifier(conn, tid, FAIL)
        assert kb._repeated_fail_anchor(conn, tid) is None
        assert kb.get_task(conn, tid).status == "ready"   # existing rework routing
