"""Terminal disposition OVERTAKEN_BY_EVENTS.

Some cards stop being work without ever being done: the objective is satisfied
by verified intervening events while the card sits safely blocked. Before this
disposition existed there were only two ways to record that, and both lied —
mark it ``done`` (fabricating an execution that never happened) or leave it
``blocked`` forever (where dependency gating, hygiene sweeps, dashboards and
the dispatcher all keep treating dead work as live).

These tests pin the four properties that make the third option trustworthy:

* **redispatch prevention** — the card can never run again, and the guard is a
  SQL predicate on ``terminal_disposition``, not merely a consequence of the
  status it landed in. Tests that force the status back by hand are the point,
  not paranoia: a guard that only holds while every other writer behaves is
  not a guard.
* **dependency terminality** — a disposed parent stops gating its children
  immediately, so reconciling a card cannot strand the work behind it.
* **history immutability** — title, body (including every step that is now
  historically obsolete), result, completion timestamp, runs, verdicts,
  comments, attachments, links, counters and prior events all survive
  untouched, and no run is ever marked ``completed``.
* **reporting semantics** — "we completed this" and "this was overtaken" are
  two separately readable facts, and an overtaken card leaves active-work
  views entirely.

Plus the negative half: ordinary done/blocked/archived cards must behave
exactly as they did before this column existed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


# The shape of the real card this feature was built for (t_887d70e9): a
# multi-step contract, parked in ``blocked`` with a typed human blocker,
# overtaken while it waited.
_OBSOLETE_BODY = (
    "Post-verification live-load validation for commit deadbee.\n"
    "1. Confirm the repository is clean and HEAD equals origin.\n"
    "2. Confirm no unrelated active workers would be disrupted.\n"
    "3. Perform exactly one controlled restart to load the commit.\n"
)


def _blocked_card(conn, *, title="load and validate", body=_OBSOLETE_BODY):
    """A card parked in ``blocked`` with a typed blocker and real history."""
    tid = kb.create_task(conn, title=title, body=body, assignee="default")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.block_task(
        conn, tid, reason="waiting on independent verifier", kind="needs_input",
    )
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _reconcile(conn, tid, **kw):
    kw.setdefault("actor", "claude")
    kw.setdefault("reason", "gateway already loaded a later commit")
    kw.setdefault("evidence", "systemctl show --property=ExecMainStartTimestamp")
    return kb.reconcile_overtaken_by_events(conn, tid, **kw)


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


# ---------------------------------------------------------------------------
# Reconciliation records the disposition without fabricating success
# ---------------------------------------------------------------------------

def test_reconcile_sets_disposition_and_parks_card_terminal(conn):
    tid = _blocked_card(conn)

    ok, err = _reconcile(
        conn, tid,
        reason="the restart happened during unrelated authorized maintenance",
        evidence="gateway ExecMainStartTimestamp is newer than the commit",
        superseded_by=["t_deadbeef", "96e20a6"],
    )
    assert (ok, err) == (True, None)

    task = kb.get_task(conn, tid)
    assert task.status == "archived"
    assert task.terminal_disposition == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
    assert task.disposition_at is not None
    # Rationale AND evidence are both retained, plus the supersession refs.
    assert "unrelated authorized maintenance" in task.disposition_reason
    assert "ExecMainStartTimestamp" in task.disposition_reason
    assert "t_deadbeef" in task.disposition_reason


def test_reconcile_never_fabricates_execution_success(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)

    task = kb.get_task(conn, tid)
    # ``result`` and ``completed_at`` both assert "this executor produced this
    # outcome". Neither is true, so neither may be written.
    assert task.result is None
    assert task.completed_at is None
    # No run may claim a completion either.
    outcomes = [
        r["outcome"]
        for r in conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ?", (tid,)
        )
    ]
    assert "completed" not in outcomes
    # The audit event states the negative claim outright rather than leaving
    # consumers to infer it from a NULL result.
    events = _events(conn, tid, "reconciled_overtaken_by_events")
    assert len(events) == 1
    payload = events[0][1]
    assert payload["executed"] is False
    assert payload["prior_status"] == "blocked"
    assert payload["disposition"] == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
    # No supersession refs were passed, so none are invented.
    assert "superseded_by" not in payload


def test_reason_and_evidence_are_mandatory(conn):
    tid = _blocked_card(conn)

    ok, err = kb.reconcile_overtaken_by_events(
        conn, tid, actor="claude", reason="   ", evidence="x",
    )
    assert ok is False and "reason is required" in err

    ok, err = kb.reconcile_overtaken_by_events(
        conn, tid, actor="claude", reason="x", evidence="",
    )
    assert ok is False and "evidence is required" in err

    ok, err = kb.reconcile_overtaken_by_events(
        conn, tid, actor="", reason="x", evidence="y",
    )
    assert ok is False and "actor is required" in err

    # None of the refusals touched the card.
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.terminal_disposition is None


def test_dry_run_validates_without_mutating(conn):
    tid = _blocked_card(conn)
    before = _events(conn, tid)

    ok, err = _reconcile(conn, tid, dry_run=True)
    assert (ok, err) == (True, None)

    task = kb.get_task(conn, tid)
    assert task.status == "blocked"
    assert task.terminal_disposition is None
    assert _events(conn, tid) == before


def test_running_and_done_cards_are_refused(conn):
    running = kb.create_task(conn, title="live", assignee="default")
    assert kb.claim_task(conn, running) is not None
    ok, err = _reconcile(conn, running)
    assert ok is False
    assert "running" in err and "worker" in err
    assert kb.get_task(conn, running).status == "running"

    finished = kb.create_task(conn, title="real work", assignee="default")
    kb.claim_task(conn, finished)
    assert kb.complete_task(conn, finished, result="did the work")
    ok, err = _reconcile(conn, finished)
    assert ok is False
    # A real completion must never be relabelled as satisfied-elsewhere:
    # that would destroy the very distinction the disposition exists to make.
    assert "already" in err
    task = kb.get_task(conn, finished)
    assert task.terminal_disposition == kb.DISPOSITION_COMPLETED
    assert task.result == "did the work"


def test_double_reconcile_is_refused(conn):
    tid = _blocked_card(conn)
    assert _reconcile(conn, tid)[0] is True
    ok, err = _reconcile(conn, tid)
    assert ok is False and "already has terminal disposition" in err
    # Exactly one audit event; the refusal wrote nothing.
    assert len(_events(conn, tid, "reconciled_overtaken_by_events")) == 1


# ---------------------------------------------------------------------------
# Redispatch prevention
# ---------------------------------------------------------------------------

def test_reconciled_card_cannot_be_reclaimed_even_if_status_is_forced(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)

    # Simulate a racing or hand-written writer putting the card back in the
    # ready column. Status alone must not be enough to make it runnable.
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
        (tid,),
    )
    assert kb.claim_task(conn, tid) is None
    assert kb.get_task(conn, tid).status == "ready"  # claim refused, not demoted
    # And the dispatcher never even offers it a slot.
    assert kb.has_spawnable_ready(conn) is False
    assert kb.check_respawn_guard(conn, tid) == "terminal_disposition"


def test_reconciled_card_cannot_be_reclaimed_from_the_review_lane(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)
    conn.execute(
        "UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = ?",
        (tid,),
    )
    assert kb.claim_review_task(conn, tid) is None
    assert kb.has_spawnable_review(conn) is False


def test_reconciled_card_cannot_be_unblocked_promoted_or_reopened(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)

    ok, err = kb.promote_task(conn, tid, actor="op")
    assert ok is False and "terminal" in err
    # --force bypasses dependency gating, not terminality.
    ok, err = kb.promote_task(conn, tid, actor="op", force=True)
    assert ok is False and "terminal" in err

    # A cron reaching for the ordinary blocked-card recovery path finds
    # nothing to recover, whatever status the row is wearing.
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    assert kb.unblock_task(conn, tid) is False
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    assert kb.reopen_review_task(conn, tid) is False


def test_reconciled_card_cannot_be_completed_afterwards(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

    assert kb.complete_task(conn, tid, result="pretending we ran it") is False
    task = kb.get_task(conn, tid)
    assert task.terminal_disposition == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
    assert task.result is None


def test_recompute_ready_never_promotes_a_reconciled_card(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (tid,))

    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "todo"


def test_dispatcher_ready_query_skips_reconciled_cards(conn):
    tid = _blocked_card(conn)
    _reconcile(conn, tid)
    conn.execute(
        "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
        (tid,),
    )
    spawned: list[str] = []
    result = kb.dispatch_once(
        conn, dry_run=True, spawn_fn=lambda *a, **k: spawned.append(a),
    )
    assert spawned == []
    assert [t for (t, *_rest) in result.spawned] == []
    assert tid not in result.skipped_unassigned


# ---------------------------------------------------------------------------
# Dependency terminality
# ---------------------------------------------------------------------------

def test_reconciled_parent_stops_gating_its_children(conn):
    parent = _blocked_card(conn, title="overtaken parent")
    child = kb.create_task(
        conn, title="downstream", parents=[parent], assignee="default",
    )
    assert kb.get_task(conn, child).status == "todo"
    assert kb._parents_satisfied(conn, child) is False

    _reconcile(conn, parent)

    # reconcile_overtaken_by_events runs recompute_ready itself, so the child
    # is already freed — a disposed parent must not strand the work behind it.
    assert kb._parents_satisfied(conn, child) is True
    assert kb.get_task(conn, child).status == "ready"
    assert kb.claim_task(conn, child) is not None


def test_reconciled_parent_is_terminal_for_manual_promotion_too(conn):
    parent = _blocked_card(conn, title="overtaken parent")
    child = kb.create_task(
        conn, title="downstream", parents=[parent], assignee="default",
    )
    _reconcile(conn, parent)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))

    ok, err = kb.promote_task(conn, child, actor="op")
    assert (ok, err) == (True, None)


# ---------------------------------------------------------------------------
# History immutability
# ---------------------------------------------------------------------------

def test_reconciliation_preserves_the_original_contract_and_history(
    conn, tmp_path,
):
    def tmp_evidence(task_id):
        p = tmp_path / f"{task_id}-evidence.txt"
        p.write_bytes(b"packet")
        return p

    tid = _blocked_card(conn)
    parent = kb.create_task(conn, title="upstream")
    kb.link_tasks(conn, parent, tid)
    kb.add_comment(conn, tid, author="christopher", body="hold for the verifier")
    packet = tmp_evidence(tid)
    kb.add_attachment(
        conn, tid, filename="evidence.txt", stored_path=str(packet),
        size=packet.stat().st_size,
    )
    # Ledger row written directly: the point here is that reconciliation does
    # not disturb the verdict history, not how the row got there.
    conn.execute(
        "INSERT INTO task_verifications (task_id, state, verifier, reason, "
        "created_at, kind) VALUES (?, 'failed', 'atlas', 'not yet', 1, 'verdict')",
        (tid,),
    )
    conn.execute("UPDATE tasks SET consecutive_failures = 2 WHERE id = ?", (tid,))

    before_task = kb.get_task(conn, tid)
    before_events = _events(conn, tid)
    before_runs = conn.execute(
        "SELECT id, outcome, summary FROM task_runs WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    before_verifications = conn.execute(
        "SELECT id, kind, state FROM task_verifications WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()

    assert _reconcile(conn, tid)[0] is True

    after = kb.get_task(conn, tid)
    # The original contract is untouched — including the numbered steps that
    # are now historically obsolete. They stay visible and are NOT marked as
    # executed; the disposition explains why they were never run.
    assert after.title == before_task.title
    assert after.body == before_task.body
    assert "3. Perform exactly one controlled restart" in after.body
    # Counters are history, not bookkeeping to tidy on the way out.
    assert after.consecutive_failures == before_task.consecutive_failures
    assert after.block_kind == before_task.block_kind
    assert after.verification_state == before_task.verification_state
    assert after.regression_required == before_task.regression_required

    # Runs, verdicts, attachments and dependency links survive verbatim.
    after_runs = conn.execute(
        "SELECT id, outcome, summary FROM task_runs WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    assert [tuple(r) for r in after_runs] == [tuple(r) for r in before_runs]
    after_verifications = conn.execute(
        "SELECT id, kind, state FROM task_verifications WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    assert (
        [tuple(r) for r in after_verifications]
        == [tuple(r) for r in before_verifications]
    )
    assert [a.filename for a in kb.list_attachments(conn, tid)] == ["evidence.txt"]
    assert kb.parent_ids(conn, tid) == [parent]

    # Prior events are appended to, never rewritten.
    after_events = _events(conn, tid)
    assert after_events[: len(before_events)] == before_events
    assert after_events[-1][0] == "reconciled_overtaken_by_events"

    # The prose audit trail says the same thing the machine-readable field
    # does, for a human reading the card cold.
    bodies = [c.body for c in kb.list_comments(conn, tid)]
    assert "hold for the verifier" in bodies
    assert any("NOT performed" in b for b in bodies)


# ---------------------------------------------------------------------------
# Reporting semantics
# ---------------------------------------------------------------------------

def test_reporting_distinguishes_completed_from_overtaken(conn):
    done_id = kb.create_task(conn, title="real work", assignee="default")
    kb.claim_task(conn, done_id)
    assert kb.complete_task(conn, done_id, result="shipped")
    overtaken_id = _blocked_card(conn, title="overtaken work")
    _reconcile(conn, overtaken_id)

    assert (
        kb.get_task(conn, done_id).terminal_disposition
        == kb.DISPOSITION_COMPLETED
    )

    stats = kb.board_stats(conn)
    # Active-work views drop the overtaken card entirely...
    assert "archived" not in stats["by_status"]
    assert stats["by_status"].get("done") == 1
    # ...while the disposition report keeps both facts, separately readable.
    assert stats["by_terminal_disposition"] == {
        kb.DISPOSITION_COMPLETED: 1,
        kb.DISPOSITION_OVERTAKEN_BY_EVENTS: 1,
    }

    # list_tasks: out of the default (active) view, findable on demand.
    active = [t.id for t in kb.list_tasks(conn)]
    assert overtaken_id not in active and done_id in active
    overtaken = kb.list_tasks(
        conn,
        include_archived=True,
        terminal_disposition=kb.DISPOSITION_OVERTAKEN_BY_EVENTS,
    )
    assert [t.id for t in overtaken] == [overtaken_id]

    with pytest.raises(ValueError):
        kb.list_tasks(conn, terminal_disposition="made_up")


def test_hygiene_style_stale_scan_no_longer_sees_the_card(conn):
    """The hygiene sweep selects blocked/ready/todo/triage candidates.

    Reconciliation is what takes a permanently-stale card out of that scan
    without deleting it — the sweep stops re-proposing work nobody will ever
    do, and the card is still there to read.
    """
    tid = _blocked_card(conn)
    candidates = lambda: [  # noqa: E731 - inline for readability
        r["id"]
        for r in conn.execute(
            "SELECT id FROM tasks "
            "WHERE status IN ('blocked','ready','todo','triage')"
        )
    ]
    assert tid in candidates()
    _reconcile(conn, tid)
    assert tid not in candidates()
    assert kb.get_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Ordinary cards keep their existing semantics
# ---------------------------------------------------------------------------

def test_ordinary_done_blocked_and_archived_semantics_are_unchanged(conn):
    # blocked -> unblock still works when no disposition is involved.
    blocked = _blocked_card(conn, title="ordinary block")
    assert kb.unblock_task(conn, blocked) is True
    assert kb.get_task(conn, blocked).status in ("ready", "todo")
    assert kb.get_task(conn, blocked).terminal_disposition is None

    # A plain archive is still a plain archive — no disposition invented.
    archived = kb.create_task(conn, title="noise", assignee="default")
    assert kb.archive_task(conn, archived) is True
    task = kb.get_task(conn, archived)
    assert task.status == "archived"
    assert task.terminal_disposition is None

    # An ordinary archived parent still satisfies dependency gating.
    child = kb.create_task(
        conn, title="after noise", parents=[archived], assignee="default",
    )
    assert kb._parents_satisfied(conn, child) is True

    # done still carries a real result and completion timestamp.
    done = kb.create_task(conn, title="ships", assignee="default")
    kb.claim_task(conn, done)
    assert kb.complete_task(conn, done, result="shipped")
    finished = kb.get_task(conn, done)
    assert finished.status == "done"
    assert finished.result == "shipped"
    assert finished.completed_at is not None


def test_completed_is_a_reversible_ending_overtaken_is_not(conn):
    """The distinction the whole guard set turns on.

    ``done`` is reversible here — reviewers request changes, ancestors get
    reopened, operators re-queue. So a 'completed' stamp must never bar
    re-entry, or every reopened card freezes. 'overtaken_by_events' must,
    because there is nothing left to redo. These reopens use direct UPDATEs on
    purpose: the guards have to hold for callers that skipped the Python
    paths, which is the same reason they are SQL predicates in the first
    place.
    """
    assert kb.DISPOSITION_COMPLETED not in kb.IRREVERSIBLE_DISPOSITIONS
    assert kb.DISPOSITION_OVERTAKEN_BY_EVENTS in kb.IRREVERSIBLE_DISPOSITIONS

    reopened = kb.create_task(conn, title="reopened work", assignee="default")
    kb.claim_task(conn, reopened)
    assert kb.complete_task(conn, reopened, result="first pass")
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (reopened,))
    # Everything a reopened card needs still works, stale stamp and all.
    assert kb.check_respawn_guard(conn, reopened) != "terminal_disposition"
    assert kb.claim_task(conn, reopened) is not None
    assert kb.complete_task(conn, reopened, result="second pass")
    assert kb.get_task(conn, reopened).result == "second pass"

    # A reopened-but-then-overtaken card is still reconcilable: the stale
    # 'completed' stamp is overwritten by the honest ending.
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (reopened,))
    ok, err = _reconcile(conn, reopened)
    assert (ok, err) == (True, None)
    assert (
        kb.get_task(conn, reopened).terminal_disposition
        == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
    )


def test_completed_parent_that_is_reopened_gates_its_children_again(conn):
    """A 'completed' parent must not read as permanently satisfied.

    If the disposition were treated as terminal for dependency resolution
    regardless of reversibility, a child would run against a parent whose work
    is actively being redone.
    """
    parent = kb.create_task(conn, title="parent", assignee="default")
    child = kb.create_task(
        conn, title="child", parents=[parent], assignee="default",
    )
    kb.claim_task(conn, parent)
    assert kb.complete_task(conn, parent, result="done")
    assert kb._parents_satisfied(conn, child) is True

    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (parent,))
    assert kb._parents_satisfied(conn, child) is False


def test_ancestor_reopen_clears_a_completed_disposition_so_work_can_rerun(conn):
    """Un-completing a card must un-say "this executor finished it".

    A card going back out for rework is not a completed card, so leaving the
    stamp behind would put a phantom completion in the disposition counts for
    work that is actively being redone. Cleared alongside ``completed_at``,
    and scoped to 'completed' only.
    """
    parent = kb.create_task(conn, title="ancestor", assignee="default")
    child = kb.create_task(
        conn, title="descendant", parents=[parent], assignee="default",
    )
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    kb.recompute_ready(conn)
    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="first pass")
    assert kb.get_task(conn, child).terminal_disposition == kb.DISPOSITION_COMPLETED

    kb.invalidate_descendants_for_parent_reopen(conn, parent, author="op")

    reopened = kb.get_task(conn, child)
    assert reopened.status == "todo"
    assert reopened.terminal_disposition is None
    assert reopened.disposition_at is None
    # And it really can run again.
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "ready"
    assert kb.claim_task(conn, child) is not None


# ---------------------------------------------------------------------------
# Migration + surfaces
# ---------------------------------------------------------------------------

def test_legacy_board_without_the_columns_migrates_cleanly(conn, kanban_home):
    """A board created before this feature gains the columns on next open,
    with every existing row left as NULL — nothing is retro-stamped."""
    tid = kb.create_task(conn, title="legacy", assignee="default")
    kb.claim_task(conn, tid)
    kb.complete_task(conn, tid, result="done long ago")
    # The index has to go first — SQLite refuses to drop a column an index
    # still references. init_db recreates both.
    conn.execute("DROP INDEX IF EXISTS idx_tasks_terminal_disposition")
    for col in ("terminal_disposition", "disposition_reason", "disposition_at"):
        conn.execute(f"ALTER TABLE tasks DROP COLUMN {col}")
    conn.commit()

    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()

    with kb.connect() as fresh:
        cols = {r["name"] for r in fresh.execute("PRAGMA table_info(tasks)")}
        assert {
            "terminal_disposition", "disposition_reason", "disposition_at",
        } <= cols
        # Not backfilled: the column asserts a fact about how a card ended,
        # and inventing that for unobserved history is the same fabrication
        # this feature exists to prevent.
        assert fresh.execute(
            "SELECT COUNT(*) FROM tasks WHERE terminal_disposition IS NOT NULL"
        ).fetchone()[0] == 0
        # Still usable afterwards.
        assert kb.get_task(fresh, tid).status == "done"


def _ns(task_id, **kw):
    return argparse.Namespace(
        task_id=task_id,
        reason=kw.get("reason", "gateway already loaded a later commit"),
        evidence=kw.get("evidence", "ExecMainStartTimestamp newer than commit"),
        superseded_by=kw.get("superseded_by"),
        dry_run=kw.get("dry_run", False),
        json=kw.get("json", False),
    )


def test_cli_reconcile_overtaken_round_trip(conn, kanban_home, capsys):
    tid = _blocked_card(conn)
    conn.commit()

    assert kb_cli._cmd_reconcile_overtaken(_ns(tid, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reconciled"] is True
    assert payload["executed"] is False
    assert payload["status"] == "archived"
    assert payload["terminal_disposition"] == kb.DISPOSITION_OVERTAKEN_BY_EVENTS

    # Second attempt is refused with a non-zero exit so a script notices.
    assert kb_cli._cmd_reconcile_overtaken(_ns(tid, json=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["reconciled"] is False
    assert "already has terminal disposition" in payload["error"]


def test_cli_show_surfaces_the_disposition(conn, kanban_home, capsys):
    tid = _blocked_card(conn)
    _reconcile(conn, tid, reason="restart already happened")
    conn.commit()

    rc = kb_cli._cmd_show(
        argparse.Namespace(
            task_id=tid, json=False, state_type=None, state_name=None,
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERTAKEN_BY_EVENTS" in out
    assert "NOT executed" in out
    assert "restart already happened" in out
    # The original obsolete steps are still there to read.
    assert "3. Perform exactly one controlled restart" in out
