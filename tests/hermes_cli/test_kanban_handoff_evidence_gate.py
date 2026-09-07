"""A gauntlet card that enters review with no evidence must SAY SO.

This is the edge whose absence killed ``t_376c07c8`` on 2026-09-07. The
implementer handed off with a prose summary and no evidence packet. The
independent-verifier pre-flight applied ``_subject_has_evidence_sql``,
correctly declined to create a verifier child it could never dispatch, and
failed closed -- leaving the subject, per that function's own docstring, with
"no route by which anything can bless it".

Every part of that is the design working. The defect is that it was SILENT.
The stall detector armed an ``independent_verification_recheck`` timer (102
ticks), then a ``lifecycle_stall_recheck`` timer to observe the first one not
progressing (53 ticks). Eight and a half hours, zero verifiers, zero state
change. The instant evidence was attached by hand, both timers closed inside
one dispatcher tick with reasons ``evidence_present`` and ``progress_resumed``
-- the system had known the blocker the whole time and had nowhere to say it.

So this fix records the fact and changes nothing else. Two things it must NOT
do, both of which I implemented first and had to back out:

  * It must not refuse the handoff. That converts a silent death into a
    blocked queue, and broke ~59 existing tests.
  * It must not manufacture an evidence packet from the summary.
    ``request_review`` already materialises evidence for a STRUCTURED handoff
    and deliberately not for prose -- prose is not falsifiable. Nine tests in
    ``test_kanban_gauntlet_lifecycle`` pin that gate; defeating it broke them.
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


def _payload(conn, tid, kind):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (tid, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else {}


class TestTheDeadStateIsRecorded:
    def test_an_evidenceless_gauntlet_handoff_records_the_unopened_route(
        self, conn
    ):
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        assert kb.subject_has_evidence(conn, tid) is False

        assert kb.request_review(
            conn, tid, summary="implemented X",
            expected_run_id=claimed.current_run_id,
        ) is True

        assert "verification_route_unopened" in _kinds(conn, tid)
        payload = _payload(conn, tid, "verification_route_unopened")
        # The record must be actionable, not merely present.
        assert "hermes kanban attach" in payload["remedy"]
        assert "no evidence packet" in payload["reason"]

    def test_the_handoff_still_succeeds(self, conn):
        """Recording must not become blocking. A blocked queue is not better
        than a silent one; it is the same failure with a louder failure mode.
        """
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        assert kb.request_review(
            conn, tid, summary="implemented X",
            expected_run_id=claimed.current_run_id,
        ) is True
        assert kb.get_task(conn, tid).status == "review"

    def test_no_evidence_is_manufactured(self, conn):
        """The prose summary must NOT become an evidence packet.

        ``request_review`` materialises evidence for a structured handoff and
        deliberately not for prose. Fabricating one here would release a
        verifier against an unfalsifiable claim.
        """
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        kb.request_review(
            conn, tid, summary="just prose",
            expected_run_id=claimed.current_run_id,
        )
        assert kb.list_attachments(conn, tid) == []
        assert kb.subject_has_evidence(conn, tid) is False

    def test_a_subject_with_evidence_records_nothing(self, conn):
        tid, claimed = _gauntlet_task_ready_to_hand_off(conn)
        _attach(conn, tid)
        kb.request_review(
            conn, tid, summary="implemented X",
            expected_run_id=claimed.current_run_id,
        )
        assert "verification_route_unopened" not in _kinds(conn, tid)

    def test_a_non_gauntlet_handoff_records_nothing(self, conn):
        """The record must not widen into ordinary review work."""
        tid = kb.create_task(conn, title="ordinary", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="done",
            expected_run_id=claimed.current_run_id,
        )
        assert "verification_route_unopened" not in _kinds(conn, tid)


class TestPredicateIsNotACopy:
    """The check must agree with the pre-flight on every input.

    ``_subject_has_evidence_sql`` accepts INHERITED evidence from a
    done/archived + verified source parent. A hand-written "has an attachment"
    check would disagree with the pre-flight -- the drift that helper's own
    docstring warns about.

    ``test_own_attachment_counts`` also pins the alias. Unaliased, the
    predicate emits ``a.task_id = id``; SQLite binds that bare ``id`` to the
    INNER table, and ``task_attachments`` has an ``id`` of its own, so the
    correlation silently became ``a.task_id = a.id`` and the whole predicate
    read false for every subject. It did, until this test caught it.
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
    while a ``--dry-run`` a second earlier promised a spawn, because dry runs
    never take the lock at all.
    """

    def test_json_output_carries_the_field(self, kanban_home):
        from hermes_cli import kanban as kc

        out = json.loads(kc.run_slash("dispatch --json --dry-run"))
        assert "skipped_locked" in out
        assert out["skipped_locked"] is False

    def test_dry_run_help_warns_it_does_not_take_the_lock(self, kanban_home):
        from hermes_cli import kanban as kc

        assert "does not take the board lock" in kc.run_slash("dispatch -h")
