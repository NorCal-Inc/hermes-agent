"""Quota wall -> worker sidecar -> run metadata -> dispatcher respawn guard.

Uses a captured, redacted error-shaped fixture (no live API calls).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.agent_runtime_helpers import extract_api_error_context
from agent.error_classifier import build_quota_hold, classify_api_error
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


class _ProviderError(Exception):
    """Shape of an SDK APIStatusError: .status_code + parsed .body."""

    def __init__(self, status_code, body):
        super().__init__(json.dumps(body))
        self.status_code = status_code
        self.body = body


def _usage_limit_error(resets_in=2874, resets_at=None):
    err = {
        "type": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "plan_type": "redacted",
        "resets_in_seconds": resets_in,
    }
    if resets_at is not None:
        err["resets_at"] = resets_at
    return _ProviderError(429, {"error": err})


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    return h


def _wall(conn, monkeypatch, tid, hold, pid, *, task_env=True, claim=None):
    """Claim, let the worker emit `hold`, reap its EX_TEMPFAIL exit."""
    host = kb._claimer_id().split(":", 1)[0]
    (claim or kb.claim_task)(conn, tid, claimer=f"{host}:w{pid}")
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id=?", (tid,)
    ).fetchone()[0]
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    if hold is not None:
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        assert kb.write_worker_quota_hold(hold)
    monkeypatch.setattr(kb, "_pid_alive", lambda _p: False)
    kb._record_worker_exit(pid, kb.KANBAN_RATE_LIMIT_EXIT_CODE << 8)
    kb.detect_crashed_workers(conn)
    assert tid in kb.detect_crashed_workers._last_rate_limited


def _hold_from_error(exc):
    classified = classify_api_error(exc, provider="openai-codex", model="gpt-5.6-sol")
    # The retry loop passes the structured context it extracted from the error.
    return classified, build_quota_hold(
        classified, error_context=extract_api_error_context(exc),
    )


def test_fixture_is_classified_hard_with_reset():
    _, hold = _hold_from_error(_usage_limit_error())
    assert hold["kind"] == "hard"
    assert 2800 < hold["reset_at"] - time.time() < 2900
    assert extract_api_error_context(_usage_limit_error())["reset_at"]


def test_plain_429_is_temporary():
    exc = _ProviderError(429, {"error": {"type": "rate_limit_exceeded",
                                         "message": "Too many requests, try again"}})
    _, hold = _hold_from_error(exc)
    assert hold["kind"] == "temporary"


@pytest.mark.parametrize("bad", [None, "", "garbage", -5, 1.0, float("inf"),
                                 time.time() + 10 * 86400])
def test_invalid_or_stale_reset_is_none(bad):
    from agent.error_classifier import coerce_quota_reset_epoch
    assert coerce_quota_reset_epoch(bad) is None


def test_reset_hold_defers_until_reset_no_tight_retry(home, monkeypatch):
    _, hold = _hold_from_error(_usage_limit_error())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="q", assignee="a")
        _wall(conn, monkeypatch, tid, hold, 71001)
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        # Even with the fixed cooldown disabled / elapsed, hold applies.
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
        assert kb.check_respawn_guard(conn, tid) == "quota_reset_hold"
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "1")
        real = time.time
        monkeypatch.setattr(kb.time, "time", lambda: real() + 600)
        assert kb.check_respawn_guard(conn, tid) == "quota_reset_hold"
        # After the reset the same route may probe again.
        monkeypatch.setattr(kb.time, "time", lambda: real() + 3000)
        assert kb.check_respawn_guard(conn, tid) is None


def test_ready_phase_preserved(home, monkeypatch):
    _, hold = _hold_from_error(_usage_limit_error())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="r", assignee="a")
        _wall(conn, monkeypatch, tid, hold, 71002)
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id=? ORDER BY id DESC",
            (tid,),
        ).fetchone()
        meta = json.loads(run["metadata"])
        assert meta["retry_status"] == "ready"
        assert meta["quota_hold"]["kind"] == "hard"
        assert meta["quota_hold"]["route"]["assignee"] == "a"


def test_changed_provider_or_executor_does_not_inherit_hold(home, monkeypatch):
    _, hold = _hold_from_error(_usage_limit_error())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="s", assignee="a")
        _wall(conn, monkeypatch, tid, hold, 71003)
        assert kb.check_respawn_guard(conn, tid) == "quota_reset_hold"
        kb.set_model_override(conn, tid, model="other-model", provider="other")
        assert kb.check_respawn_guard(conn, tid) is None
        conn.execute("UPDATE tasks SET model_override=NULL, provider_override=NULL WHERE id=?", (tid,))
        assert kb.check_respawn_guard(conn, tid) == "quota_reset_hold"
        conn.execute("UPDATE tasks SET assignee='b' WHERE id=?", (tid,))
        assert kb.check_respawn_guard(conn, tid) is None


def test_missing_sidecar_falls_back_to_cooldown(home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="m", assignee="a")
        _wall(conn, monkeypatch, tid, None, 71004)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"


def test_hard_without_reset_uses_bounded_cooldown(home, monkeypatch):
    exc = _ProviderError(429, {"error": {"type": "usage_limit_reached",
                                         "resets_at": "not-a-time"}})
    _, hold = _hold_from_error(exc)
    assert hold["kind"] == "hard" and hold["reset_at"] is None
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="i", assignee="a")
        _wall(conn, monkeypatch, tid, hold, 71005)
        assert kb.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "0")
        assert kb.check_respawn_guard(conn, tid) is None


def test_sidecar_from_other_run_is_ignored(home, monkeypatch):
    _, hold = _hold_from_error(_usage_limit_error())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="f", assignee="a")
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
        assert kb.write_worker_quota_hold(hold)
        assert kb._consume_worker_quota_hold(tid, 1) is None


def test_review_phase_preserved_and_held(home, monkeypatch):
    _, hold = _hold_from_error(_usage_limit_error())
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rv", assignee="a")
        kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        assert kb.request_review(conn, tid, summary="done", reviewer="rev",
                                 expected_run_id=run_id)
        assert kb.get_task(conn, tid).status == "review"
        _wall(conn, monkeypatch, tid, hold, 71004, claim=kb.claim_review_task)
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.consecutive_failures == 0
        assert kb.check_respawn_guard(conn, tid) == "quota_reset_hold"
