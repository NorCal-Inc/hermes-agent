"""yt_source delivery waits are neither block loops nor stale work (2026-10-26).

A worker parked on the yt_source queue re-blocks after each failed capture and
re-request. On 2026-10-26 the second wait sent t_56f5852d to triage, where
nothing could resume it, one minute before the transcript arrived.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

WAIT = "waiting for yt_source delivery of UGVz615AQLA"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="t"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("ready", tid))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _make_running_again(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("ready", tid))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def test_repeated_yt_source_waits_never_route_to_triage(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="yt wait")
        for suffix in ("", " (retry 1)", " (retry 2)"):
            assert kb.block_task(conn, tid, reason=WAIT + suffix, kind="needs_input")
            assert kb.get_task(conn, tid).status == "blocked"
            kb.unblock_task(conn, tid)
            _make_running_again(conn, tid)
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "block_loop_detected"]


def test_other_same_cause_blocks_still_break_the_loop(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        tid = _running_task(conn, title="real loop")
        kb.block_task(conn, tid, reason="need input", kind="needs_input")
        kb.unblock_task(conn, tid)
        _make_running_again(conn, tid)
        kb.block_task(conn, tid, reason="need input", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"


def _stale_ids(conn):
    later = int(time.time()) + 3600
    return {s.task_id for s in kb.detect_stale_gauntlet_work(
        conn, stale_timeout_seconds=60, realert_seconds=0, now=later)}


def test_yt_source_wait_is_not_reported_stale(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        waiting = _running_task(conn, title="yt wait")
        kb.block_task(conn, waiting, reason=WAIT, kind="needs_input")
        other = _running_task(conn, title="human wait")
        kb.block_task(conn, other, reason="need input", kind="needs_input")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET gauntlet_enforced=1")
        stale = _stale_ids(conn)
        assert waiting not in stale
        assert other in stale
