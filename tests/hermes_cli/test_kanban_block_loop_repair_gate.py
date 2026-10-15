"""A block-loop card cannot leave triage until its repair is recorded.

Christopher, 2026-10-14: "twice blocked is for you to triage and fix via the
gauntlet loop". t_4c41cdde was bounced from triage to todo with a
status-only specify and its root cause unrepaired; nothing refused it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_specify as spec

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _rerun(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None


def _looped_task(conn):
    tid = kb.create_task(conn, title="loops", assignee="worker")
    _rerun(conn, tid)
    kb.block_task(conn, tid, reason="socket path too long", kind="needs_input")
    kb.unblock_task(conn, tid)
    _rerun(conn, tid)
    kb.block_task(conn, tid, reason="socket path too long", kind="needs_input")
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _repair(conn, tid, **over):
    kw = dict(root_cause="AF_UNIX path 113 > 107", change="shorter socket dir",
              check="daemon started in sandbox", author="claude")
    kw.update(over)
    kb.record_block_loop_repair(conn, tid, **kw)


def test_status_only_specify_is_refused_for_block_loop_card(kanban_home):
    with kb.connect_closing() as conn:
        tid = _looped_task(conn)
        with pytest.raises(kb.BlockLoopRepairRequired):
            kb.specify_triage_task(conn, tid, author="claude")
        assert kb.get_task(conn, tid).status == "triage"


def test_llm_specify_refuses_before_spending(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _looped_task(conn)
    import agent.auxiliary_client as aux
    monkeypatch.setattr(aux, "call_llm", lambda *a, **k: pytest.fail("LLM called"))
    out = spec.specify_task(tid, author="claude")
    assert out.ok is False and "loop-repair" in out.reason


@pytest.mark.parametrize("blank", ["root_cause", "change", "check", "author"])
def test_repair_needs_every_field(kanban_home, blank):
    with kb.connect_closing() as conn:
        tid = _looped_task(conn)
        with pytest.raises(ValueError):
            _repair(conn, tid, **{blank: "  "})
        assert kb.block_loop_repair_pending(conn, tid)


def test_recorded_repair_releases_card_and_arms_gauntlet(kanban_home):
    with kb.connect_closing() as conn:
        tid = _looped_task(conn)
        _repair(conn, tid)
        task = kb.get_task(conn, tid)
        assert task.gauntlet_enforced
        assert any("Root cause" in c.body for c in kb.list_comments(conn, tid))
        assert kb.specify_triage_task(conn, tid, author="claude") is True
        assert kb.get_task(conn, tid).status in ("todo", "ready")


def test_repair_does_not_carry_over_to_a_new_loop(kanban_home):
    with kb.connect_closing() as conn:
        tid = _looped_task(conn)
        _repair(conn, tid)
        kb.specify_triage_task(conn, tid, author="claude")
        _rerun(conn, tid)
        kb.block_task(conn, tid, reason="socket path too long", kind="needs_input")
        assert kb.get_task(conn, tid).status == "triage"
        with pytest.raises(kb.BlockLoopRepairRequired):
            kb.specify_triage_task(conn, tid, author="claude")


def test_repair_refused_when_no_loop(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain", triage=True)
        with pytest.raises(ValueError):
            _repair(conn, tid)
        assert kb.specify_triage_task(conn, tid, title="ok", body="b") is True
