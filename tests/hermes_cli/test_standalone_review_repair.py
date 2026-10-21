"""Standalone-review-FAIL routing: repair the parent, requeue, never block.

``request_changes`` is legal only for a run claimed FROM the review lane
(``claimed`` event payload ``source_status='review'``). A STANDALONE review
card — an ordinary task whose job is reviewing its PARENT's output via a
plain ``task_links`` edge, without itself ever having been claimed from the
review column — can never satisfy that guard. Before this change its only
legal move was ``kanban_block(reason=needs_input)``, parking a
machine-fixable defect for a human (the live t_e54d47a7 example). These
tests pin down the replacement path: :func:`kb.route_standalone_review_fail`
creates a repair card for the parent, gates the review card on it, and the
review auto-promotes back to ``ready`` once the repair is done, verified and
completed — never touching the ``needs_input``/block machinery.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _row(conn, tid):
    return conn.execute(
        "SELECT status, block_kind, block_recurrences, current_run_id "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _make_standalone_review(conn, *, subject_title="implementation", reviewer="reviewer"):
    """Build a subject task plus a STANDALONE review card linked to it.

    "Standalone" here means: the review task is a plain ``task_links``
    child of the subject (``kb.link_tasks(subject_id, review_id)``), and its
    active run was claimed as an ordinary task (NOT via
    ``claim_review_task``/``source_status='review'``). That is exactly the
    shape ``request_changes`` refuses with "active run was not claimed from
    review".
    """
    subject_id = kb.create_task(conn, title=subject_title, assignee="worker")
    kb.claim_task(conn, subject_id)
    kb.complete_task(conn, subject_id, summary="implemented", result="done")

    review_id = kb.create_task(
        conn, title=f"Independent review: {subject_id}", assignee=reviewer,
    )
    kb.link_tasks(conn, subject_id, review_id)
    claimed = kb.claim_task(conn, review_id)
    assert claimed is not None
    return subject_id, review_id


# ---------------------------------------------------------------------------
# request_changes refuses a standalone review card (pins the gap this closes)
# ---------------------------------------------------------------------------


def test_request_changes_refuses_standalone_review_run(kanban_home: Path) -> None:
    with kb.connect() as conn:
        _subject_id, review_id = _make_standalone_review(conn)
        run_id = kb.get_task(conn, review_id).current_run_id

        ok, detail = kb.request_changes(
            conn, review_id, reason="found a defect", expected_run_id=run_id,
        )
        assert ok is False
        assert detail is not None and "not claimed from review" in detail


# ---------------------------------------------------------------------------
# route_standalone_review_fail: the replacement path
# ---------------------------------------------------------------------------


def test_standalone_review_fail_creates_repair_and_gates_review(
    kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        subject_id, review_id = _make_standalone_review(conn)

        ok, repair_id = kb.route_standalone_review_fail(
            conn, review_id,
            reason="missing required field X in the deliverable",
            findings="add field X with value Y",
            owner="erika",
        )
        assert ok is True
        assert repair_id

        # The repair card exists, points at the subject (RELATION_REPAIRS)
        # and at the durable umbrella (RELATION_UMBRELLA) — the same
        # relations create_repair_task enforces for every caller.
        relations = {r["relation"]: r["to_task_id"] for r in kb.task_relations(conn, repair_id)}
        assert relations.get(kb.RELATION_REPAIRS) == subject_id
        assert kb.RELATION_UMBRELLA in relations

        # The umbrella is the one durable "Operational Error Repairs" card.
        umbrella_id = relations[kb.RELATION_UMBRELLA]
        umbrella = kb.get_task(conn, umbrella_id)
        assert umbrella.title == kb.OPERATIONAL_ERROR_REPAIRS_TITLE
        # Same card the kernel auto-repair routes use (one umbrella, closed
        # container, never an open dispatchable card).
        assert umbrella_id == kb.ensure_operational_repairs_umbrella(conn)
        assert umbrella.status == "done"

        # The review card is back to 'todo', gated on the repair via the
        # SAME task_links dependency mechanism _parents_satisfied/
        # recompute_ready already use everywhere else.
        row = _row(conn, review_id)
        assert row["status"] == "todo"
        assert row["current_run_id"] is None
        assert repair_id in kb.parent_ids(conn, review_id)

        # Never a block/needs_input — this is the whole point of the route.
        assert row["block_kind"] is None
        assert (row["block_recurrences"] or 0) == 0
        assert _events(conn, review_id, kind="blocked") == []
        assert _events(conn, review_id, kind="gave_up") == []

        # recompute_ready leaves the review NOT ready while the repair is open.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, review_id).status == "todo"

        routed = _events(conn, review_id, kind="standalone_review_fail_routed_to_repair")
        assert len(routed) == 1
        assert routed[0][1]["repair_id"] == repair_id
        assert routed[0][1]["subject_id"] == subject_id


def test_standalone_review_fail_ambiguous_parent_is_refused_without_mutation(
    kanban_home: Path,
) -> None:
    """Zero or multiple task_links parents is refused, not guessed."""
    with kb.connect() as conn:
        review_id = kb.create_task(conn, title="orphan review", assignee="reviewer")
        claimed = kb.claim_task(conn, review_id)
        assert claimed is not None
        run_id = kb.get_task(conn, review_id).current_run_id

        ok, detail = kb.route_standalone_review_fail(
            conn, review_id, reason="found a defect",
        )
        assert ok is False
        assert detail is not None and "parent" in detail

        # Nothing mutated: the review card's run/claim/status are untouched.
        row = _row(conn, review_id)
        assert row["status"] == "running"
        assert row["current_run_id"] == run_id


def test_repair_completion_promotes_review_back_to_ready(kanban_home: Path) -> None:
    """Once the repair reaches done+verified+completed, the gated review
    auto-promotes back to ready via the same repairs-relation +
    done+verified+completed check resolve_stale_gauntlet_dispositions
    already relies on."""
    with kb.connect() as conn:
        _subject_id, review_id = _make_standalone_review(conn)

        ok, repair_id = kb.route_standalone_review_fail(
            conn, review_id, reason="missing field X",
        )
        assert ok is True
        assert kb.get_task(conn, review_id).status == "todo"

        # Repair is worked and passes independent verification.
        kb.claim_task(conn, repair_id)
        kb.request_review(
            conn, repair_id, summary="fixed",
            expected_run_id=kb.get_task(conn, repair_id).current_run_id,
        )
        claimed_review = kb.claim_review_task(conn, repair_id)
        assert claimed_review is not None
        verified_ok, verified_state = kb.record_verification(
            conn, repair_id, passed=True, verifier="verifier-profile",
        )
        assert verified_ok is True
        assert verified_state == kb.VERIFICATION_VERIFIED
        completed = kb.complete_task(
            conn, repair_id, summary="repair verified", result="done",
        )
        assert completed is True

        repair_row = conn.execute(
            "SELECT status, verification_state, terminal_disposition, "
            "completed_at FROM tasks WHERE id = ?",
            (repair_id,),
        ).fetchone()
        assert repair_row["status"] == "done"
        assert repair_row["verification_state"] == kb.VERIFICATION_VERIFIED
        assert repair_row["terminal_disposition"] == "completed"
        assert repair_row["completed_at"] is not None

        # The review card's only parent (the repair) is now terminal ->
        # _parents_satisfied is true -> recompute_ready promotes it.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, review_id).status == "ready"


# ---------------------------------------------------------------------------
# In-place review path (claimed from review) is completely unchanged
# ---------------------------------------------------------------------------


def test_in_place_request_changes_path_unchanged(kanban_home: Path) -> None:
    """A run actually claimed FROM the review lane still goes through
    request_changes exactly as before — this new routing path is additive,
    not a replacement."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="builder")
        kb.claim_task(conn, tid)
        kb.request_review(
            conn, tid, summary="done", reviewer="reviewer",
            expected_run_id=kb.get_task(conn, tid).current_run_id,
        )
        review = kb.claim_review_task(conn, tid)
        assert review is not None

        ok, implementer = kb.request_changes(
            conn, tid, reason="fix the thing",
            expected_run_id=review.current_run_id,
        )
        assert ok is True
        assert implementer == "builder"
        assert kb.get_task(conn, tid).status in ("ready", "todo")
