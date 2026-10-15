"""A ``dependency`` block must be backed by an unsatisfied parent (t_3e640c71).

Live evidence 2026-10-15 (t_4c41cdde, runs 4094-4097): the card had ZERO
``task_links`` parents, yet every run ended with ``kind="dependency"`` on the
same operational-health condition. ``block_task`` routed each one to ``todo``
and returned before the ``block_recurrences`` loop accounting, so
``recompute_ready`` promoted it on the next tick and the dispatcher spawned
another paid worker ~1 minute later, 13 times, until the attempt ceiling.

The repair: the dependency routing reads the actual graph state through the
same predicate ``recompute_ready`` uses (``_parents_satisfied``). Only a
genuinely unsatisfied parent justifies the auto-resuming ``todo`` park. With
no unsatisfied parent the block is a real blocker and goes through the
existing loop breaker -> ``record_block_loop_repair`` path. No new gate, no
ceiling change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.execution_effects import effect_states, mark_applied, prepare_effect

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect_closing() as c:
        yield c


def _claim(conn, tid) -> int:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready' WHERE id=? AND status IN ('todo','ready')",
            (tid,),
        )
    task = kb.claim_task(conn, tid, claimer="worker")
    assert task is not None and task.current_run_id is not None
    return int(task.current_run_id)


def _running(conn, title="orphan dependency"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    run_id = _claim(conn, tid)
    return tid, run_id


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


REASON = "dependency: operational health must be green before publishing"


# ---------------------------------------------------------------------------
# The live defect
# ---------------------------------------------------------------------------


def test_orphan_dependency_block_cannot_auto_promote(conn) -> None:
    """No parent at all -> the card may not park in the auto-resume lane."""
    tid, run_id = _running(conn)
    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run_id,
    )
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.block_kind == "dependency"
    assert task.block_recurrences == 1
    assert task.claim_lock is None and task.current_run_id is None

    # Nothing changed on the (empty) prerequisite side, so the dispatcher
    # tick must not hand this card to another paid worker.
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.claim_task(conn, tid, claimer="worker") is None
    assert _events(conn, tid, "promoted") == []
    assert _events(conn, tid, "dependency_wait") == []

    blocked = _events(conn, tid, "blocked")
    assert len(blocked) == 1
    payload = blocked[0].payload or {}
    assert payload.get("kind") == "dependency"
    assert payload.get("unsatisfied_parents") == 0


def test_second_unchanged_orphan_dependency_block_reaches_loop_repair(conn) -> None:
    """Runs 4094 -> 4095: the same report twice must land in the repair loop."""
    tid, run_id = _running(conn)
    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run_id,
    )
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"
    run2 = _claim(conn, tid)
    assert run2 != run_id

    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run2,
    )
    task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    loop = _events(conn, tid, "block_loop_detected")
    assert len(loop) == 1
    assert (loop[0].payload or {}).get("kind") == "dependency"

    # The existing accountable repair gate owns the card from here.
    assert kb.block_loop_repair_pending(conn, tid)
    with pytest.raises(kb.BlockLoopRepairRequired):
        kb.specify_triage_task(conn, tid, author="claude")
    assert kb.get_task(conn, tid).status == "triage"
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, tid).status == "triage"

    kb.record_block_loop_repair(
        conn, tid,
        root_cause="orphan dependency report with no linked prerequisite",
        change="worker links the real prerequisite or blocks needs_input",
        check="card no longer re-spawns on an unchanged graph",
        author="claude",
    )
    assert kb.specify_triage_task(conn, tid, author="claude") is True
    assert kb.get_task(conn, tid).status in ("todo", "ready")


def test_satisfied_parents_do_not_justify_dependency_parking(conn) -> None:
    """A done parent is not something to wait for."""
    parent = kb.create_task(conn, title="parent", assignee="worker")
    _claim(conn, parent)
    assert kb.complete_task(conn, parent, result="done")

    child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
    assert kb.get_task(conn, child).status == "ready"
    run_id = _claim(conn, child)
    assert kb.block_task(
        conn, child, reason="dependency: waiting on parent", kind="dependency",
        expected_run_id=run_id,
    )
    task = kb.get_task(conn, child)
    assert task.status == "blocked"
    assert task.block_recurrences == 1
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "blocked"
    assert _events(conn, child, "dependency_wait") == []


def test_archived_parent_counts_as_satisfied(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker", parents=[parent])
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (parent,))
    assert kb.recompute_ready(conn) == 1
    run_id = _claim(conn, child)
    assert kb.block_task(
        conn, child, reason="dependency: parent", kind="dependency",
        expected_run_id=run_id,
    )
    assert kb.get_task(conn, child).status == "blocked"
    assert kb.recompute_ready(conn) == 0


# ---------------------------------------------------------------------------
# Preserved behaviour: genuine waits, review phase, effects, run-id fencing
# ---------------------------------------------------------------------------


def test_genuine_unsatisfied_parent_waits_and_resumes_without_loop_accounting(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    child_run = _claim(conn, child)
    # Link while running: the live "owner delegates to a child" shape.
    kb.link_tasks(conn, parent_id=parent, child_id=child)

    assert kb.block_task(
        conn, child, reason="dependency: waiting on parent", kind="dependency",
        expected_run_id=child_run,
    )
    task = kb.get_task(conn, child)
    assert task.status == "todo"
    assert task.block_kind == "dependency"
    assert task.block_recurrences == 0
    assert len(_events(conn, child, "dependency_wait")) == 1
    assert _events(conn, child, "blocked") == []

    # Still parked while the parent is open.
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "todo"

    # Parent completion is the changed prerequisite that releases it.
    _claim(conn, parent)
    assert kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"


def test_repeated_genuine_waits_never_trip_the_loop_breaker(conn) -> None:
    """Waiting on real, still-open work is not a recurrence."""
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    for _ in range(kb.BLOCK_RECURRENCE_LIMIT + 1):
        # ``claim_task`` itself refuses a child with an open parent, so each
        # round reproduces the live shape: the owner is already running when
        # it links the work it is waiting on.
        kb.unlink_tasks(conn, parent_id=parent, child_id=child)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        run_id = _claim(conn, child)
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        assert kb.block_task(
            conn, child, reason="dependency: parent", kind="dependency",
            expected_run_id=run_id,
        )
        assert kb.get_task(conn, child).status == "todo"
    assert kb.get_task(conn, child).block_recurrences == 0
    assert _events(conn, child, "block_loop_detected") == []


def test_orphan_dependency_from_review_phase_resumes_in_review(conn) -> None:
    tid = kb.create_task(conn, title="reviewed", assignee="worker")
    impl_run = _claim(conn, tid)
    assert kb.request_review(
        conn, tid, summary="ready", reviewer="reviewer", expected_run_id=impl_run,
    )
    review = kb.claim_review_task(conn, tid)
    assert review is not None and review.current_run_id is not None

    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency",
        expected_run_id=int(review.current_run_id),
    )
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    blocked = _events(conn, tid, "blocked")[-1]
    assert (blocked.payload or {}).get("source_status") == "review"

    assert kb.recompute_ready(conn) == 0
    assert kb.unblock_task(conn, tid)
    assert kb.get_task(conn, tid).status == "review"


def test_orphan_dependency_holds_applied_effects_until_adopted(conn, tmp_path) -> None:
    """Effects a dependency-blocked run already applied are held, not rolled back."""
    tid, run1 = _running(conn)
    path = tmp_path / f"{tid}.txt"
    eid = prepare_effect(
        conn,
        task_id=tid,
        run_id=run1,
        effect_type="test_text_write",
        resource=str(path),
        before_state={"exists": False},
        after_state={"exists": True, "text": "owned"},
        inverse_action="restore_text_file",
        inverse_payload={"existed": False, "text": ""},
    )
    path.write_text("owned", encoding="utf-8")
    mark_applied(conn, eid)

    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run1,
    )
    assert kb.get_task(conn, tid).status == "blocked"
    assert effect_states(conn, task_id=tid, run_id=run1) == ["held"]
    assert path.exists()
    assert kb.reconcile_execution_effect_rollbacks(conn) == (0, 0)
    assert path.read_text(encoding="utf-8") == "owned"

    # The replacement run (after a deliberate unblock) adopts the held work.
    assert kb.unblock_task(conn, tid)
    task2 = kb.claim_task(conn, tid, claimer="worker")
    assert task2 is not None and task2.current_run_id is not None
    run2 = int(task2.current_run_id)
    assert run2 != run1
    assert effect_states(conn, task_id=tid, run_id=run2) == ["applied"]
    row = conn.execute(
        "SELECT origin_run_id FROM execution_effects WHERE task_id=?", (tid,)
    ).fetchone()
    assert int(row["origin_run_id"]) == run1


def test_stale_expected_run_id_changes_nothing(conn) -> None:
    tid, run_id = _running(conn)
    before_events = len(kb.list_events(conn, tid))
    before_runs = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", (tid,)
    ).fetchone()["n"]

    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run_id + 1,
    ) is False

    task = kb.get_task(conn, tid)
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert task.block_kind is None
    assert task.block_recurrences == 0
    assert len(kb.list_events(conn, tid)) == before_events
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs WHERE task_id=?", (tid,)
    ).fetchone()["n"] == before_runs
    assert kb.recompute_ready(conn) == 0


def test_stale_expected_run_id_with_real_parent_changes_nothing(conn) -> None:
    parent = kb.create_task(conn, title="parent", assignee="worker")
    child = kb.create_task(conn, title="child", assignee="worker")
    run_id = _claim(conn, child)
    kb.link_tasks(conn, parent_id=parent, child_id=child)
    before_events = len(kb.list_events(conn, child))
    assert kb.block_task(
        conn, child, reason="dependency: parent", kind="dependency",
        expected_run_id=run_id + 1,
    ) is False
    task = kb.get_task(conn, child)
    assert task.status == "running"
    assert task.current_run_id == run_id
    assert len(kb.list_events(conn, child)) == before_events


def test_completion_resets_dependency_loop_memory(conn) -> None:
    tid, run_id = _running(conn)
    assert kb.block_task(
        conn, tid, reason=REASON, kind="dependency", expected_run_id=run_id,
    )
    assert kb.unblock_task(conn, tid)
    run2 = _claim(conn, tid)
    assert kb.complete_task(conn, tid, result="done", expected_run_id=run2)
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    assert task.block_recurrences == 0
