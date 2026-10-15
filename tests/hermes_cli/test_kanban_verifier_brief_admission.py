"""Incident 2026-10-04..15: the automated independent-verifier card for ORDINARY
work was refused by control-plane admission 134 times across 69 subjects.

The verifier brief itself said "Gauntlet" and "control-plane", create_task
classifies a card from its own title+body, so every automated verifier of
ordinary work looked like new control-plane construction and was refused. The
subject could then never complete. The brief now carries those words only when
the subject is itself control-plane (where the verdict path enforces
ACCEPTANCE: PASS). No authority is granted to make this work.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def automated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated kanban DB whose ambient creator is governed automation,
    which is what a live worker calling request_review looks like."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_EXECUTION_ID",
        "HERMES_SESSION_ID", "HERMES_EXECUTOR_LANE", "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(kb.ENV_ACTOR_KIND, kb.ACTOR_KIND_GOVERNED_AUTOMATION)
    monkeypatch.setenv(kb.ENV_ACTOR_ID, "worker")
    kb.init_db()
    return home


def _auto():
    return kb.ActorProvenance(
        kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION, actor_id="worker",
        cause=kb.CREATION_CAUSE_AUTOMATED,
    )


def _human():
    return kb.ActorProvenance(
        kind=kb.ACTOR_KIND_HUMAN_INTERACTIVE, actor_id="christopher",
        cause=kb.CREATION_CAUSE_MANUAL_RELAY,
    )


def _failed(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events "
        "WHERE task_id=? AND kind='independent_verifier_child_failed'", (tid,),
    ).fetchone()[0]


def _attach_evidence(conn, tid):
    kb.store_attachment_bytes(
        conn, tid, "evidence.txt", b"work product\n",
        content_type="text/plain", uploaded_by="test",
    )


def test_ordinary_subject_gets_its_verifier_without_any_authority(automated_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="Draft NCASS monthly research summary",
            assignee="default", provenance=_auto(),
        )
        _attach_evidence(conn, tid)
        child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
        assert child is not None
        assert _failed(conn, tid) == 0
        row = conn.execute(
            "SELECT title, body, control_plane, control_plane_authority "
            "FROM tasks WHERE id=?", (child,),
        ).fetchone()
        assert row["control_plane"] == 0
        assert row["control_plane_authority"] is None
        assert not kb.control_plane_subject(row["title"], row["body"])
        assert "ACCEPTANCE: PASS" not in row["body"]


def test_control_plane_subject_verifier_keeps_acceptance_and_operator_authority(automated_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="Update shared boot consumer checkpoint pin",
            assignee="default", provenance=_human(),
        )
        _attach_evidence(conn, tid)
        child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
        assert child is not None
        row = conn.execute(
            "SELECT body, control_plane, control_plane_authority FROM tasks WHERE id=?",
            (child,),
        ).fetchone()
        assert row["control_plane"] == 1
        assert row["control_plane_authority"] == f"operator:human-interactive-parent:{tid}"
        assert "ACCEPTANCE: PASS" in row["body"]


def test_automated_control_plane_work_is_still_refused(automated_home):
    with kb.connect_closing() as conn:
        with pytest.raises(kb.ControlPlaneAdmissionError):
            kb.create_task(
                conn, title="Repair Gauntlet verifier return paths",
                assignee="default", provenance=_auto(),
            )
