"""Incident 2026-10-14: Codex verifiers t_c8171572 / t_d794a8e8 / t_04544c5b were
told only the subject's id and returned BLOCKER when they could not guess how
to read it. The launcher now inlines the subject: body, latest handoff, recent
comments and attachment paths."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane


@pytest.fixture(autouse=True)
def _synthetic_identities_are_registered(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    c = kb.connect()
    try:
        yield c
    finally:
        c.close()


def _pair(conn, body="Confirm the drift against live state; record what you did."):
    sid = kb.create_task(conn, title="Repair drift: vault", body=body, assignee="default")
    vid = kb.create_task(conn, title=f"Independent verification: {sid}",
                         body=f"Independent Gauntlet verification of {sid}.", parents=[sid])
    kb.add_task_relation(conn, vid, sid, kb.RELATION_VERIFIES)
    return sid, vid


def test_incident_replay_subject_body_comments_and_attachments_reach_the_verifier(conn, tmp_path):
    sid, vid = _pair(conn)
    evidence = tmp_path / "evidence-vault-drift.txt"
    evidence.write_text("HEAD=origin/main fc844792, 0/0, clean")
    kb.add_attachment(conn, sid, filename=evidence.name, stored_path=str(evidence), size=37)
    kb.add_comment(conn, sid, "claude-code", "Live evidence attached: fresh fetch, 0/0, clean.")
    other, _ = _pair(conn, body="UNRELATED CARD BODY")

    ctx = recovery_lane._verifier_subject_context(conn, vid)

    assert f"--- SUBJECT {sid} ---" in ctx
    assert "Confirm the drift against live state" in ctx
    assert "[claude-code] Live evidence attached" in ctx
    assert f"- {evidence} (37 bytes)" in ctx
    assert "UNRELATED CARD BODY" not in ctx and other not in ctx


def test_no_subject_or_missing_subject_gives_nothing(conn):
    lone = kb.create_task(conn, title="no relation", body="b")
    assert recovery_lane._verifier_subject_context(conn, lone) == ""
    sid, vid = _pair(conn)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM tasks WHERE id = ?", (sid,))
    assert recovery_lane._verifier_subject_context(conn, vid) == ""


def test_board_error_fails_closed_to_empty(conn, monkeypatch):
    _, vid = _pair(conn)

    def boom(*a, **k):
        raise RuntimeError("board unreadable")

    monkeypatch.setattr(kb, "get_task", boom)
    assert recovery_lane._verifier_subject_context(conn, vid) == ""


def test_long_body_is_clipped(conn):
    _, vid = _pair(conn, body="x" * (recovery_lane._SUBJECT_TEXT_CAP + 500))
    ctx = recovery_lane._verifier_subject_context(conn, vid)
    assert "[... truncated, 500 more characters]" in ctx


def test_prompt_carries_subject_context_after_brief_only_when_given():
    task = type("T", (), {"id": "t_v", "title": "x", "tenant": None, "workspace_path": None, "body": "b"})()
    plain = recovery_lane._build_codex_verifier_prompt(task)
    assert "SUBJECT CONTEXT" not in plain
    full = recovery_lane._build_codex_verifier_prompt(task, "", "SUBJECT CONTEXT (test)")
    lines = full.splitlines()
    i = lines.index("SUBJECT CONTEXT (test)")
    assert lines[i - 2] == "b" and lines[i - 1] == ""
    assert full.replace("\n\nSUBJECT CONTEXT (test)", "", 1) == plain
    assert lines[-1].startswith("The FINAL line must be")
