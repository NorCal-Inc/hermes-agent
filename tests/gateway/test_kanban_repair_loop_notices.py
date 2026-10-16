"""#alerts repair-loop notices (t_2bc18772).

Christopher, 2026-10-16: exactly ONE "error caught, beginning repair loop"
message once a repair card exists, exactly ONE "repaired, restarted/passed"
message when the subject resumes, a "could NOT start ... Needs you" message
if the repair could not be opened, and the old raw verification-FAILED /
gave-up line must not ALSO fire for an error the repair loop now owns.
"""

import asyncio

import pytest

from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_notifier import (
    RecordingAdapter,
    _make_runner,
    _run_one_notifier_tick,
)

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "repair-notices.db"))
    kb.init_db()


def _subscribed_task(title="nightly sync"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        return tid
    finally:
        conn.close()


def _emit(tid, *events):
    conn = kb.connect()
    try:
        for kind, payload in events:
            kb._append_event(conn, tid, kind, payload)
    finally:
        conn.close()


def _tick(monkeypatch):
    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    return [m["text"] for m in adapter.sent]


def test_repair_started_posts_one_start_message(board, monkeypatch):
    tid = _subscribed_task()
    _emit(tid, ("repair_started", {
        "repair_id": "t_rep1", "subject_id": tid, "attempt": 3,
        "cause": "boom: subprocess exited 1\nsecond line",
    }))
    sent = _tick(monkeypatch)
    assert len(sent) == 1
    assert "error caught, beginning repair loop" in sent[0]
    assert tid in sent[0] and "t_rep1" in sent[0]
    assert "(attempt 3)" in sent[0]
    assert " of " not in sent[0].split("attempt", 1)[1]
    assert "second line" not in sent[0]
    # Fires once: the next tick has nothing new.
    assert _tick(monkeypatch) == []


def test_standalone_review_fail_posts_start_message(board, monkeypatch):
    review = _subscribed_task("review of subject")
    _emit(review, ("standalone_review_fail_routed_to_repair", {
        "repair_id": "t_rep2", "subject_id": "t_subj", "umbrella_id": "t_umb",
        "reason": "tests missing",
    }))
    sent = _tick(monkeypatch)
    assert len(sent) == 1
    assert "error caught, beginning repair loop" in sent[0]
    assert "t_subj" in sent[0] and "t_rep2" in sent[0] and "tests missing" in sent[0]


def test_repair_completed_posts_one_repaired_message(board, monkeypatch):
    tid = _subscribed_task()
    _emit(tid, ("repair_completed_subject_resumed", {
        "repair_id": "t_rep1", "subject_id": tid, "resumed_status": "ready",
    }))
    sent = _tick(monkeypatch)
    assert len(sent) == 1
    assert "repaired, restarted/passed" in sent[0] and "t_rep1" in sent[0]
    assert _tick(monkeypatch) == []


def test_repair_start_failed_posts_needs_you(board, monkeypatch):
    tid = _subscribed_task()
    _emit(tid, ("repair_start_failed", {"subject_id": tid, "error": "subject vanished"}))
    sent = _tick(monkeypatch)
    assert len(sent) == 1
    assert "repair loop could NOT start" in sent[0]
    assert "subject vanished" in sent[0] and "Needs you." in sent[0]


def test_raw_verification_failed_is_replaced_by_repair_notice(board, monkeypatch):
    tid = _subscribed_task()
    _emit(
        tid,
        ("verification_failed", {"reason": "missing file", "verifier": "codex"}),
        ("repair_started", {"repair_id": "t_rep3", "subject_id": tid, "attempt": 1,
                            "cause": "missing file"}),
    )
    sent = _tick(monkeypatch)
    assert len(sent) == 1
    assert "verification FAILED" not in sent[0]
    assert "error caught, beginning repair loop" in sent[0]


def test_raw_gave_up_is_replaced_by_repair_notice(board, monkeypatch):
    tid = _subscribed_task()
    _emit(
        tid,
        ("gave_up", {"error": "boom"}),
        ("repair_started", {"repair_id": "t_rep4", "subject_id": tid, "attempt": 1,
                            "cause": "boom"}),
    )
    sent = _tick(monkeypatch)
    assert len(sent) == 1 and "gave up" not in sent[0]


def test_unrouted_verification_failed_still_notifies(board, monkeypatch):
    # A first FAIL routes to ordinary rework, not the repair loop: keep it.
    tid = _subscribed_task()
    _emit(tid, ("verification_failed", {"reason": "missing file", "verifier": "codex"}))
    sent = _tick(monkeypatch)
    assert len(sent) == 1 and "verification FAILED" in sent[0]


def test_approval_required_notice_unaffected(board, monkeypatch):
    tid = _subscribed_task()
    _emit(tid, ("blocked", {"kind": "approval_required", "reason": "spend money"}))
    sent = _tick(monkeypatch)
    assert len(sent) == 1 and "needs Christopher approval" in sent[0]


def test_new_kinds_are_registered():
    for kind in ("repair_started", "repair_start_failed",
                 "repair_completed_subject_resumed",
                 "standalone_review_fail_routed_to_repair"):
        assert kind in kb.KANBAN_NOTIFY_EVENT_KINDS
    assert "repair_start_failed" in kb.KANBAN_ACTIVE_WAKE_EVENT_KINDS
