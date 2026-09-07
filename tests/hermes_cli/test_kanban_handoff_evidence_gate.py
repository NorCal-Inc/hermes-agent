"""The handoff edge: a gauntlet card may not enter review with nothing to check.

This is the edge whose absence killed ``t_376c07c8`` on 2026-09-07. The
implementer finished and called ``request_review``; the summary it had just
written -- 3,325 characters, with file:line references and acceptance-criteria
mapping -- stayed on the run row and was never attached to the card. The
independent-verifier pre-flight then applied ``_subject_has_evidence_sql``,
correctly declined to create a verifier child it could never dispatch, and
failed closed, leaving the subject in ``review``/``pending`` with, per that
function's own docstring, "no route by which anything can bless it".

Nothing surfaced that. The stall detector armed an
``independent_verification_recheck`` timer (102 ticks), then a
``lifecycle_stall_recheck`` timer to observe the first one not progressing
(53 ticks). Eight and a half hours, zero verifiers, zero state change.
Attaching the evidence by hand closed both timers within one dispatcher tick,
with reasons ``evidence_present`` and ``progress_resumed`` -- the system had
known the blocker the whole time and had no way to say so.

The fix is plumbing, not a gate: the evidence already existed, so move it.
Refusal is reserved for the case where there is genuinely nothing to move.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# Synthetic identities are not profile directories on disk; the registry gate
# (D8) needs the same fixture every other kanban test module declares.
pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def conn(tmp_path: Path):
    db = kb.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _attach(conn, tid, filename="evidence.md"):
    kb.add_attachment(
        conn, tid, filename=filename,
        stored_path=f"/tmp/{tid}/{filename}",
        size=4096, uploaded_by="executor-lane",
    )


def _gauntlet_task_ready_to_hand_off(conn, *, title="work"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.set_gauntlet_enforced(conn, tid, True) is True
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    return tid, claimed


def _kinds(conn, tid):
    return [
        r["kind"] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]


class TestHandoffAlwaysOpensAVerificationRoute:
    def test_the_summary_becomes_the_evidence_packet(self, conn, kanban_home):
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        assert kb.subject_has_evidence(conn, tid) is False

        assert kb.request_review(
            conn, tid, summary="implemented X; ran the suite; 12 passed",
            expected_run_id=claimed.current_run_id,
        ) is True

        # The route the pre-flight needs is now open.
        assert kb.subject_has_evidence(conn, tid) is True
        assert kb.get_task(conn, tid).status == "review"
        assert "evidence_auto_materialised" in _kinds(conn, tid)

    def test_the_materialised_packet_holds_the_real_summary(
        self, conn, kanban_home
    ):
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        kb.request_review(
            conn, tid, summary="the actual claim under test",
            expected_run_id=claimed.current_run_id,
        )
        [att] = kb.list_attachments(conn, tid)
        assert att.filename == kb.HANDOFF_EVIDENCE_FILENAME
        body = Path(att.stored_path).read_text(encoding="utf-8")
        assert "the actual claim under test" in body
        # It must not read as a verdict.
        assert "not a verdict" in body
        assert "not been independently verified" in body

    def test_existing_evidence_is_never_overwritten(self, conn, kanban_home):
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        _attach(conn, tid, filename="real-artefacts.tar.gz")
        kb.request_review(
            conn, tid, summary="prose",
            expected_run_id=claimed.current_run_id,
        )
        names = [a.filename for a in kb.list_attachments(conn, tid)]
        assert names == ["real-artefacts.tar.gz"]
        assert "evidence_auto_materialised" not in _kinds(conn, tid)

    def test_nothing_to_materialise_is_refused_with_an_actionable_message(
        self, conn, kanban_home
    ):
        """The one case that must still fail closed, and say why."""
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        ok, reason = kb.request_review(
            conn, tid, summary=None,
            expected_run_id=claimed.current_run_id, with_reason=True,
        )
        assert ok is False
        assert "no path to any verdict" in reason
        assert "hermes kanban attach" in reason
        assert kb.get_task(conn, tid).status == "running"

    def test_a_non_gauntlet_handoff_is_untouched(self, conn, kanban_home):
        """The change must not widen into ordinary review work."""
        tid = kb.create_task(conn, title="ordinary", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="done",
            expected_run_id=claimed.current_run_id,
        ) is True
        assert kb.list_attachments(conn, tid) == []
        assert "evidence_auto_materialised" not in _kinds(conn, tid)


class TestPredicateIsNotACopy:
    """The check must agree with the pre-flight on every input.

    ``_subject_has_evidence_sql`` accepts INHERITED evidence from a
    done/archived + verified source parent. A hand-written "has an attachment"
    check here would disagree with the pre-flight, which is the drift that
    helper's docstring explicitly warns about.

    ``test_own_attachment_counts`` also pins the alias: unaliased, the
    predicate emits ``a.task_id = id``, SQLite binds the bare ``id`` to the
    INNER table -- ``task_attachments`` has one -- and the whole gate silently
    reads false for every subject. It did, until these tests caught it.
    """

    def test_own_attachment_counts(self, conn):
        tid = kb.create_task(conn, title="s", assignee="worker")
        assert kb.subject_has_evidence(conn, tid) is False
        _attach(conn, tid)
        assert kb.subject_has_evidence(conn, tid) is True

    def test_inherited_evidence_from_a_verified_parent_counts(self, conn):
        parent = kb.create_task(conn, title="source", assignee="worker")
        child = kb.create_task(conn, title="derived", assignee="worker")
        _attach(conn, parent, filename="source-evidence.md")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, child),
            )
            conn.execute(
                "UPDATE tasks SET status = 'done', verification_state = ? "
                "WHERE id = ?", (kb.VERIFICATION_VERIFIED, parent),
            )
        assert kb.subject_has_evidence(conn, child) is True

    def test_an_unverified_parents_evidence_does_not_count(self, conn):
        parent = kb.create_task(conn, title="source", assignee="worker")
        child = kb.create_task(conn, title="derived", assignee="worker")
        _attach(conn, parent, filename="source-evidence.md")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (parent, child),
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?", (parent,)
            )
        assert kb.subject_has_evidence(conn, child) is False


class TestDispatchReportsTheLock:
    """A lock loss must not read as an empty queue.

    ``dispatch_once`` returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` when another dispatcher holds the board lock. The
    CLI omitted the field entirely, so a tick that did nothing because the
    gateway held the lock printed exactly what a genuinely idle board prints --
    while a ``--dry-run`` a second earlier had promised a spawn, because dry
    runs do not take the lock at all.
    """

    def test_json_output_carries_the_field(self, kanban_home):
        from hermes_cli import kanban as kc

        out = json.loads(kc.run_slash("dispatch --json --dry-run"))
        assert "skipped_locked" in out
        assert out["skipped_locked"] is False

    def test_dry_run_help_warns_it_does_not_take_the_lock(self, kanban_home):
        from hermes_cli import kanban as kc

        assert "does not take the board lock" in kc.run_slash("dispatch -h")
