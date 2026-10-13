"""``kanban_supersede`` — terminally disposition an exhausted card as
superseded, through the governed kernel path, without fabricating success.

The kernel already had the honest ending for a card whose objective was
satisfied elsewhere: ``reconcile_overtaken_by_events`` (CLI
``hermes kanban reconcile-overtaken``) — status ``archived``, disposition
``overtaken_by_events``, ``superseded_by`` refs, ``executed: false``, no
result and no completion timestamp. What was missing was a tool-surface
operation a task session could call: a worker's own card cannot close
another card (worker-scope guard), and doctrine forbids a direct SQLite write
or shelling out to the CLI. 2026-10-12, t_af33e54a: 12 attempts consumed by
boot-gate parks, zero mutations, replaced by t_83804c18 which completed the
identical scope; the owner (Erika) had no supported way to end it.

These tests pin:

* **authority** — the card's owner (creator acting as itself) or a session
  with operator authority; an automation-admitted worker that is neither is
  refused, and a worker never supersedes its own running card.
* **replacement evidence** — the replacement must be on the same tenant and
  record a real completion (``done`` + ``completed``) or a VERIFIED head;
  anything less returns a precise blocker and changes nothing.
* **no falsified execution** — the event says ``executed: false``, the
  result and completion timestamp stay NULL, runs are never ``completed``.
* **kernel refusals pass through** — running / done / already-disposed
  cards come back as the kernel's own blocker text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture(autouse=True)
def _synthetic_identities_are_registered(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _provenance(kind, actor_id):
    return kb.resolve_actor_provenance(kind=kind, actor_id=actor_id, env={})


def _blocked_card(conn, *, created_by="erika", assignee="lifewiki_lead", tenant=None):
    """The shape of t_af33e54a: created by Erika (governed automation) for a
    Team Leader, parked in ``blocked`` after its attempt budget ran out."""
    tid = kb.create_task(
        conn, title="Complete failed digest with real closure evidence",
        body="1. ingest\n2. file\n3. delete input after verified filing\n",
        assignee=assignee, created_by=created_by, tenant=tenant,
        provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, created_by),
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.block_task(
        conn, tid, reason="ATTEMPT_BUDGET_EXHAUSTED", kind="needs_input",
    )
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _completed_card(conn, *, created_by="erika", assignee="lifewiki_lead", tenant=None):
    """The shape of t_83804c18: the replacement that really did the work."""
    tid = kb.create_task(
        conn, title="Digest transcript (replacement)", assignee=assignee,
        created_by=created_by, tenant=tenant,
        provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, created_by),
    )
    assert kb.claim_task(conn, tid) is not None
    assert kb.complete_task(conn, tid, summary="closed+VERIFIED on the dashboard") is True
    task = kb.get_task(conn, tid)
    assert (task.status, task.terminal_disposition) == ("done", kb.DISPOSITION_COMPLETED)
    return tid


def _erika_task_session(conn, monkeypatch, *, tenant=None):
    """A worker session for Erika on a card a human relayed to her."""
    card = kb.create_task(
        conn, title="Erika: work the lesson candidate queue", assignee="erika",
        created_by="claude-code", tenant=tenant,
        provenance=_provenance(kb.ACTOR_KIND_HUMAN_INTERACTIVE, "claude-code"),
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", card)
    monkeypatch.setenv("HERMES_PROFILE", "erika")
    return card


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def _supersede(**args):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_supersede(args))


# ---------------------------------------------------------------------------
# The owner, dispatched as a task, ends its exhausted card honestly
# ---------------------------------------------------------------------------

class TestOwnerSupersedes:
    def test_owner_worker_supersedes_with_read_back(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
            before = kb.get_task(conn, source)

        out = _supersede(
            task_id=source, replacement_task_id=replacement,
            reason="budget consumed by boot-gate parks; replacement completed the identical scope",
            evidence="dashboard row CLOSED/VERIFIED, verified_at=2026-10-12T21:01:24",
        )
        assert out.get("ok") is True, out
        assert out["task_id"] == source
        assert out["prior_status"] == "blocked"
        assert out["status"] == "archived"
        assert out["terminal_disposition"] == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
        assert out["disposition_at"] is not None
        assert out["executed"] is False
        assert out["result_written"] is False
        assert out["completed_at"] is None
        assert out["superseded_by"] == [replacement]
        assert out["replacement"]["task_id"] == replacement
        assert out["replacement"]["status"] == "done"
        assert out["replacement"]["terminal_disposition"] == kb.DISPOSITION_COMPLETED
        assert out["authority_basis"] == "card_owner"
        assert out["actor"] == "erika"
        payload = out["event"]["payload"]
        assert out["event"]["kind"] == "reconciled_overtaken_by_events"
        assert payload["executed"] is False
        assert payload["superseded_by"] == [replacement]
        assert payload["actor"] == "erika"
        assert payload["prior_status"] == "blocked"
        assert "dashboard row CLOSED/VERIFIED" in payload["evidence"]
        assert f"replacement task {replacement}" in payload["evidence"]

        with kb.connect_closing() as conn:
            after = kb.get_task(conn, source)
            assert after.status == "archived"
            assert after.terminal_disposition == kb.DISPOSITION_OVERTAKEN_BY_EVENTS
            # Nothing fabricated, nothing lost.
            assert after.result is None
            assert after.completed_at is None
            assert (after.title, after.body) == (before.title, before.body)
            assert f"Superseded by: {replacement}" in after.disposition_reason
            runs = conn.execute(
                "SELECT status FROM task_runs WHERE task_id = ?", (source,)
            ).fetchall()
            assert runs and all(r["status"] != "completed" for r in runs)
            comments = conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id",
                (source,),
            ).fetchall()
            assert "OVERTAKEN_BY_EVENTS" in comments[-1]["body"]
            assert "NOT performed" in comments[-1]["body"]
            # The replacement is untouched.
            rep = kb.get_task(conn, replacement)
            assert (rep.status, rep.terminal_disposition) == ("done", kb.DISPOSITION_COMPLETED)

    def test_second_supersede_is_refused_as_already_disposed(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
        assert _supersede(
            task_id=source, replacement_task_id=replacement, reason="r",
        ).get("ok") is True
        again = _supersede(
            task_id=source, replacement_task_id=replacement, reason="r again",
        )
        assert again.get("ok") is not True
        assert "already has terminal disposition" in again["error"]
        with kb.connect_closing() as conn:
            assert len(_events(conn, source, "reconciled_overtaken_by_events")) == 1

    def test_verified_replacement_is_also_evidence(self, kanban_home, monkeypatch):
        """A Gauntlet replacement with a VERIFIED head counts even before its
        completion lands."""
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            rid = kb.create_task(
                conn, title="verified replacement", assignee="lifewiki_lead",
                created_by="erika", gauntlet=True,
                provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, "erika"),
            )
            claimed = kb.claim_task(conn, rid)
            assert kb.request_review(
                conn, rid, summary="impl", reviewer="lifewiki_lead",
                expected_run_id=claimed.current_run_id,
            ) is True
            ok, detail = kb.record_verification(
                conn, rid, passed=True, verifier="reviewer",
                evidence={"command": "pytest -q", "exit_code": 0},
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            assert kb.get_task(conn, rid).status != "done"
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=source, replacement_task_id=rid, reason="r")
        assert out.get("ok") is True, out
        assert out["replacement"]["verification_state"] == kb.VERIFICATION_VERIFIED

    def test_orchestrator_session_supersedes_with_operator_basis(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            replacement = _completed_card(conn)
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.setenv("HERMES_PROFILE", "claude")
        out = _supersede(task_id=source, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is True, out
        assert out["authority_basis"] == "orchestrator_session"
        assert out["actor"] == "claude"


# ---------------------------------------------------------------------------
# Replacement evidence: a precise blocker, never a silent close
# ---------------------------------------------------------------------------

class TestReplacementEvidence:
    def test_replacement_without_real_completion_is_a_blocker(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            not_done = _blocked_card(conn)   # blocked, no disposition
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=source, replacement_task_id=not_done, reason="r")
        assert out.get("ok") is not True
        assert "blocked:" in out["error"]
        assert "'blocked'" in out["error"]
        assert "must record a real completion" in out["error"]
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, source)
            assert (task.status, task.terminal_disposition) == ("blocked", None)
            assert _events(conn, source, "reconciled_overtaken_by_events") == []

    def test_missing_replacement_is_a_blocker(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=source, replacement_task_id="t_nope", reason="r")
        assert out.get("ok") is not True
        assert "not on this board" in out["error"]

    def test_self_replacement_and_missing_fields_are_refused(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
        assert "cannot supersede itself" in _supersede(
            task_id=source, replacement_task_id=source, reason="r",
        )["error"]
        assert "reason is required" in _supersede(
            task_id=source, replacement_task_id=replacement,
        )["error"]
        assert "replacement_task_id is required" in _supersede(
            task_id=source, reason="r",
        )["error"]
        assert "task_id is required" in _supersede(
            replacement_task_id=replacement, reason="r",
        )["error"]

    def test_cross_tenant_replacement_is_refused(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            source = _blocked_card(conn, tenant="acme")
            replacement = _completed_card(conn, tenant="globex")
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=source, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "Company lanes are sealed" in out["error"]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, source).status == "blocked"


# ---------------------------------------------------------------------------
# Authority
# ---------------------------------------------------------------------------

class TestAuthority:
    def test_automation_admitted_non_owner_is_refused(self, kanban_home, monkeypatch):
        """A Team Leader's worker (card came from Erika's automation) can
        neither own nor operate on a sibling card."""
        with kb.connect_closing() as conn:
            source = _blocked_card(conn, created_by="erika")
            replacement = _completed_card(conn)
            worker = kb.create_task(
                conn, title="sibling", assignee="lifewiki_lead", created_by="erika",
                provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, "erika"),
            )
        monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
        monkeypatch.setenv("HERMES_PROFILE", "lifewiki_lead")
        out = _supersede(task_id=source, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "is not the owner" in out["error"]
        assert "automation chain" in out["error"]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, source).status == "blocked"

    def test_owner_must_act_as_itself(self, kanban_home, monkeypatch):
        """Claiming ``actor='erika'`` from a card assigned to someone else is
        impersonation, even though erika does own the target."""
        with kb.connect_closing() as conn:
            source = _blocked_card(conn, created_by="erika")
            replacement = _completed_card(conn)
            worker = kb.create_task(
                conn, title="sibling", assignee="lifewiki_lead", created_by="erika",
                provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, "erika"),
            )
        monkeypatch.setenv("HERMES_KANBAN_TASK", worker)
        monkeypatch.setenv("HERMES_PROFILE", "lifewiki_lead")
        out = _supersede(
            task_id=source, replacement_task_id=replacement, reason="r", actor="erika",
        )
        assert out.get("ok") is not True
        assert "is not the assignee" in out["error"]

    def test_worker_cannot_supersede_its_own_card(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            replacement = _completed_card(conn)
            own = _erika_task_session(conn, monkeypatch)
            assert kb.claim_task(conn, own) is not None
        out = _supersede(task_id=own, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "own running card" in out["error"]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, own).status == "running"

    def test_delegated_child_is_refused(self, kanban_home, monkeypatch):
        from tools import kanban_tools as kt

        with kb.connect_closing() as conn:
            source = _blocked_card(conn)
            replacement = _completed_card(conn)
        monkeypatch.setattr(kt, "_is_delegated_child_context", lambda: True)
        out = _supersede(task_id=source, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "delegate_task child" in out["error"]


# ---------------------------------------------------------------------------
# Kernel refusals pass through as blockers
# ---------------------------------------------------------------------------

class TestKernelRefusals:
    def test_running_source_is_a_blocker(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            running = kb.create_task(
                conn, title="live", assignee="lifewiki_lead", created_by="erika",
                provenance=_provenance(kb.ACTOR_KIND_GOVERNED_AUTOMATION, "erika"),
            )
            assert kb.claim_task(conn, running) is not None
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=running, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "stop or reclaim the worker first" in out["error"]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, running).status == "running"

    def test_done_source_is_a_blocker(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            done = _completed_card(conn)
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id=done, replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "already records a real completion" in out["error"]
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, done)
            assert (task.status, task.terminal_disposition) == ("done", kb.DISPOSITION_COMPLETED)

    def test_unknown_source_is_reported(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            replacement = _completed_card(conn)
            _erika_task_session(conn, monkeypatch)
        out = _supersede(task_id="t_nope", replacement_task_id=replacement, reason="r")
        assert out.get("ok") is not True
        assert "not found" in out["error"]


# ---------------------------------------------------------------------------
# Gate membership: a dead contract cannot supersede either
# ---------------------------------------------------------------------------

def test_supersede_is_board_mutating_for_the_task_contract_gate():
    from agent import task_contract_gate as gate

    assert "kanban_supersede" in gate.BOARD_MUTATING_KANBAN_TOOLS
    assert gate._is_gated_tool("kanban_supersede")
