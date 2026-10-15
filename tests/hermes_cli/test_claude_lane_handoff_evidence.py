"""Incident 2026-10-01..15: six claude-lane cards (drift repairs) finished
their run, could not self-complete under Gauntlet enforcement, and handed off
to review with their evidence posted only as a comment. The verifier dispatch
gate counts only attachments as evidence, so no verifier was ever opened and
each card sat in review indefinitely (INDEPENDENT_VERIFICATION_UNROUTABLE).
The handoff now attaches the same evidence before requesting review.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run(monkeypatch, tid):
    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, t, r: True)
    monkeypatch.setattr(
        recovery_lane, "_invoke_claude",
        lambda prompt, cwd, timeout, *, task_id=None: recovery_lane.AttemptResult(
            "claude", 0, '{"result":"Drift repaired; parity check now green."}', "",
            execution_id="x_fake_claude",
            execution_status=recovery_lane.ex.STATUS_COMPLETED,
        ),
    )
    return recovery_lane.run_claude_executor(tid)


def _gauntlet_card(conn, title):
    tid = kb.create_task(
        conn, title=title, assignee="default",
        body="Drift repair. Re-run the parity check and report.",
        executor_lane=kb.EXECUTOR_LANE_CLAUDE,
    )
    conn.execute("UPDATE tasks SET gauntlet_enforced = 1 WHERE id = ?", (tid,))
    conn.commit()
    root = kb.resolve_workspace(kb.get_task(conn, tid))
    kb.set_workspace_path(conn, tid, str(root))
    assert kb.claim_task(conn, tid)
    return tid


def test_fileless_handoff_attaches_evidence_so_verifier_can_route(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _gauntlet_card(conn, "Repair drift: example ledger")

    assert _run(monkeypatch, tid) == 0

    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review"
        names = [a.filename for a in kb.list_attachments(conn, tid)]
        assert names == ["handoff-evidence.md"]
        assert kb.subject_has_evidence(conn, tid)
        unroutable = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? "
            "AND kind='independent_verification_unroutable'", (tid,),
        ).fetchone()[0]
        assert unroutable == 0


def test_handoff_does_not_duplicate_existing_evidence(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid = _gauntlet_card(conn, "Repair drift: second ledger")
        kb.store_attachment_bytes(
            conn, tid, "report.md", b"existing evidence\n",
            content_type="text/markdown", uploaded_by="test",
        )

    assert _run(monkeypatch, tid) == 0

    with kb.connect_closing() as conn:
        names = [a.filename for a in kb.list_attachments(conn, tid)]
        assert names == ["report.md"]
