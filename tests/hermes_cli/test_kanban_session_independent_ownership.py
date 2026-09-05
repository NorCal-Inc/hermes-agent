"""Ownership must survive the session that created it.

``tasks.session_id`` names the interactive session a card was created in. It is
useful provenance and it is not an ownership key: the session ends, the SSH
connection drops, the executor process is reaped, and the string keeps pointing
at something that no longer exists. Anything that answers "who owns this?" by
resolving a session — or by checking whether a process is still alive — returns
"nobody" the moment the session ends, and an ownerless card is one no recovery
lane routes and no governor chases.

These regressions pin the property that makes that impossible:
:func:`hermes_cli.kanban_db.resolve_task_ownership` reads persisted rows and
nothing else. The tests therefore do not merely assert that resolution "still
works" after a session ends — an assertion a lucky implementation could pass —
they assert that the environment and the process table are never consulted at
all, by making any access to either raise.

The live cards this repair was written from are preserved untouched:
``t_d8c9baff`` (this closure gate) and ``t_c5c2929d`` (the umbrella repair) both
carry ``session_id='20260903_103059_b4cd01'``, a session that has since ended.
Nothing here backfills or reinterprets them.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SESSION = "20260903_103059_b4cd01"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_EXECUTION_ID",
        "HERMES_SESSION_ID",
        "HERMES_EXECUTOR_LANE",
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
        kb.ENV_ACTOR_KIND,
        kb.ENV_ACTOR_ID,
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


class _ExplodingEnv(dict):
    """A mapping that fails loudly on any read.

    Substituted for ``os.environ`` so "resolution does not consult the
    environment" is enforced rather than asserted in prose. A resolver that
    reads one variable — including a harmless-looking fallback — fails here.
    """

    def __getitem__(self, key):  # pragma: no cover - the point is not reaching it
        raise AssertionError(f"ownership resolution read os.environ[{key!r}]")

    def get(self, key, default=None):  # pragma: no cover - same
        raise AssertionError(f"ownership resolution read os.environ.get({key!r})")


def _end_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the originating session terminating or disconnecting.

    Everything the session put in the environment is gone. The card's
    ``session_id`` now points at nothing, which is the state every card on the
    live board reaches within a day of being created.
    """
    for var in (
        "HERMES_SESSION_ID",
        "HERMES_EXECUTION_ID",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_TASK",
        "HERMES_EXECUTOR_LANE",
        "HERMES_PROFILE",
        "HERMES_PROFILE_NAME",
    ):
        monkeypatch.delenv(var, raising=False)


@contextlib.contextmanager
def _no_live_state(monkeypatch: pytest.MonkeyPatch):
    """Make any read of the environment or the process table raise.

    Scoped tightly around the *resolution* call. Opening the board is
    legitimately environment-dependent (``HERMES_KANBAN_DB`` selects which
    board), and conflating "finding the database" with "resolving ownership"
    would make this guard prove nothing about either.
    """

    def _no_process(*_a, **_k):  # pragma: no cover - the point is not reaching it
        raise AssertionError(
            "ownership resolution probed a live process; ownership must come "
            "from persisted state"
        )

    with monkeypatch.context() as m:
        m.setattr(os, "environ", _ExplodingEnv())
        m.setattr(os, "kill", _no_process)
        yield


def _create_in_session(conn, title: str, **kwargs) -> str:
    """Create a card as an interactive session would."""
    kwargs.setdefault("session_id", SESSION)
    kwargs.setdefault("assignee", "default")
    kwargs.setdefault("executor_lane", kb.EXECUTOR_LANE_CLAUDE)
    return kb.create_task(conn, title=title, **kwargs)


# ---------------------------------------------------------------------------
# The core property: the session was never an input
# ---------------------------------------------------------------------------

class TestResolutionIgnoresLiveState:
    def test_ownership_is_byte_identical_after_the_session_ends(
        self, kanban_home, monkeypatch
    ):
        """The whole claim, in one assertion.

        Resolve while the session is live, destroy the session, resolve again
        from a *fresh connection* (a new process would get one), and compare.
        Equality is the evidence: if the session had contributed anything, the
        second reading could not match the first.
        """
        monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
        monkeypatch.setenv("HERMES_PROFILE", "claude-code")
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "work started inside a session")
            live = kb.resolve_task_ownership(conn, tid).as_dict()

        assert live["resolved"] is True
        assert live["session_id"] == SESSION

        _end_the_session(monkeypatch)

        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                after = kb.resolve_task_ownership(conn, tid).as_dict()

        assert after == live

    def test_resolution_never_reads_the_environment_or_a_process(
        self, kanban_home, monkeypatch
    ):
        """Enforced, not asserted: both sources raise if touched."""
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "no live state may be consulted")

        _end_the_session(monkeypatch)

        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                ownership = kb.resolve_task_ownership(conn, tid)
                guarded = kb.require_resolvable_ownership(conn, tid)
                unowned = kb.unowned_tasks(conn)

        assert ownership.resolved is True
        # Owned through ``assignee`` — the control plane wrote it, so it is
        # structured and passes the enforced boundary, but it names where the
        # card RUNS rather than who is accountable for it. The second
        # independent review (2026-09-03) was right that those are different
        # claims, so the record now says so instead of reporting clean
        # ownership. Advisory, never blocking: all 34 live cards resolve this
        # way, so refusing it would strand the board.
        assert ownership.attributable is True
        assert ownership.blocking_reasons == ()
        assert ownership.degraded_reasons == ("owner_only_from_assignee",)
        assert ownership.governable is False
        assert guarded == ownership
        assert unowned == []

    def test_a_dangling_session_id_does_not_make_a_card_ownerless(
        self, kanban_home, monkeypatch
    ):
        """A session id pointing at nothing is provenance, not a broken link."""
        with kb.connect_closing() as conn:
            tid = _create_in_session(
                conn, "session that never existed", session_id="s_gone_forever"
            )

        _end_the_session(monkeypatch)

        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                ownership = kb.resolve_task_ownership(conn, tid)

        assert ownership.session_id == "s_gone_forever"
        assert ownership.resolved is True
        assert ownership.attributable is True
        # The dangling session is NOT among the reasons — that is the assertion
        # this test exists for. The only degradation is the routing-identity
        # one every assignee-owned card carries.
        assert ownership.degraded_reasons == ("owner_only_from_assignee",)

    def test_missing_card_resolves_to_none_not_to_an_ownerless_card(
        self, kanban_home
    ):
        """"Gone" and "owned badly" are different answers."""
        with kb.connect_closing() as conn:
            assert kb.resolve_task_ownership(conn, "t_nope") is None
            with pytest.raises(kb.OwnershipUnresolvable):
                kb.require_resolvable_ownership(conn, "t_nope")


# ---------------------------------------------------------------------------
# Owner resolution itself
# ---------------------------------------------------------------------------

class TestOwnerResolution:
    def test_source_precedence_is_recovery_owner_first(self, kanban_home):
        """A repair's accountable identity is its owner, not its dispatch target."""
        with kb.connect_closing() as conn:
            umbrella = _create_in_session(conn, "umbrella")
            subject = _create_in_session(conn, "subject")
            repair = kb.create_repair_task(
                conn,
                title="repair",
                subject_id=subject,
                umbrella_id=umbrella,
                owner="erika",
                assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
                recovery_gate_cmd="pytest -q tests/hermes_cli",
                session_id=SESSION,
            )
            ownership = kb.resolve_task_ownership(conn, repair)

        assert ownership.owner == "erika"
        assert ownership.owner_source == "recovery_owner"
        assert ownership.structurally_owned is True

    def test_created_by_alone_is_resolvable_but_reported_degraded(
        self, kanban_home
    ):
        """The free-text string resolves; it never counts as clean ownership.

        ``created_by`` is the profile name Defect A proved cannot separate a
        human relay from automation. A card owned only through it is reported
        with ``owner_only_from_created_by`` so the weakness is visible instead
        of being laundered into a confident-looking owner.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "legacy-shaped")
            # Strip the structured sources, leaving the legacy string. This is
            # the shape of every card created before the provenance columns.
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, actor_kind = NULL, "
                "assignee = NULL, created_by = 'claude-code' WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, tid)

        assert ownership.owner == "claude-code"
        assert ownership.owner_source == "created_by"
        assert ownership.structurally_owned is False
        assert ownership.resolved is True
        assert "owner_only_from_created_by" in ownership.degraded_reasons
        assert "provenance_not_recorded" in ownership.degraded_reasons
        # Degraded is not governable: a governor that routes this card is
        # acting on an attribution the data does not support.
        assert ownership.governable is False

    def test_identical_created_by_still_yields_different_ownership(
        self, kanban_home, monkeypatch
    ):
        """Two cards, same author string, genuinely different actors.

        The false-attribution case. Ownership must not collapse them, and it
        must still not collapse them once both sessions are gone.
        """
        monkeypatch.setenv("HERMES_PROFILE", "claude-code")
        with kb.connect_closing() as conn:
            relayed = _create_in_session(
                conn,
                "relayed by a human",
                provenance=kb.ActorProvenance(
                    kind=kb.ACTOR_KIND_HUMAN_INTERACTIVE,
                    actor_id="christopher",
                    cause=kb.CREATION_CAUSE_MANUAL_RELAY,
                ),
            )
            automated = _create_in_session(
                conn,
                "filed by automation",
                provenance=kb.ActorProvenance(
                    kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION,
                    actor_id="kanban-worker",
                    run_id="x_deadbeef",
                    cause=kb.CREATION_CAUSE_AUTOMATED,
                ),
            )
            conn.execute("UPDATE tasks SET created_by = 'claude-code'")
            conn.commit()

        _end_the_session(monkeypatch)

        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                a = kb.resolve_task_ownership(conn, relayed)
                b = kb.resolve_task_ownership(conn, automated)

        assert a.owner == "christopher" and b.owner == "kanban-worker"
        assert a.actor_kind == kb.ACTOR_KIND_HUMAN_INTERACTIVE
        assert b.actor_kind == kb.ACTOR_KIND_GOVERNED_AUTOMATION
        assert b.actor_run_id == "x_deadbeef"

    def test_an_ownerless_card_fails_closed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "stripped bare")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, assignee = NULL, "
                "created_by = NULL, recovery_owner = NULL WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, tid)
            assert ownership.resolved is False
            assert ownership.owner_source is None
            assert "no_owner_recorded" in ownership.degraded_reasons
            with pytest.raises(kb.OwnershipUnresolvable):
                kb.require_resolvable_ownership(conn, tid)
            assert tid in kb.unowned_tasks(conn)

    def test_a_degraded_card_still_passes_the_boundary_guard(self, kanban_home):
        """Degradation is carried forward, not used to strand legacy work.

        Refusing to route every pre-migration card would abandon exactly the
        history this repair preserves. The guard fails closed on *ownerless*,
        not on *imperfect*.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "legacy")
            conn.execute(
                "UPDATE tasks SET actor_kind = NULL, actor_id = NULL WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.require_resolvable_ownership(conn, tid)

        assert "provenance_not_recorded" in ownership.degraded_reasons
        assert ownership.resolved is True

    def test_an_untrustworthy_attribution_fails_closed(self, kanban_home):
        """The hole the 2026-09-03 independent review found in this guard.

        The boundary used to admit *every* degraded record, including one whose
        only owner came from ``created_by`` — the string the test two cases up
        documents as unable to tell a human relay from automation. A guard that
        admits an owner it has just finished describing as unreliable is not a
        guard, and the paths behind it (arming a timer, routing a recovery,
        returning a verdict) are exactly where a wrong actor does damage.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "owned only by free text")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, actor_kind = NULL, "
                "assignee = NULL, recovery_owner = NULL, "
                "created_by = 'claude-code' WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, tid)
            # It still RESOLVES — "owned badly" and "gone" stay different
            # answers, and the census still reports it as owned...
            assert ownership.resolved is True
            assert tid not in kb.unowned_tasks(conn)
            # ...but it is not something a governor may act on.
            assert ownership.attributable is False
            assert ownership.blocking_reasons == ("owner_only_from_created_by",)
            with pytest.raises(kb.OwnershipUnresolvable) as excinfo:
                kb.require_resolvable_ownership(conn, tid)
        assert "trustworthy" in str(excinfo.value)

    def test_arming_a_timer_refuses_an_untrustworthy_owner(self, kanban_home):
        """The boundary is enforced where it matters, not merely defined.

        ``arm_observation_timer`` is the one production caller of the guard, so
        this is the test that would fail if the tightening were reverted while
        the guard's own unit test kept passing.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "free-text owner")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, actor_kind = NULL, "
                "assignee = NULL, recovery_owner = NULL, "
                "created_by = 'claude-code' WHERE id = ?",
                (tid,),
            )
            conn.commit()
            with pytest.raises(kb.OwnershipUnresolvable):
                kb.arm_observation_timer(conn, tid)
            assert kb.task_observation_timers(conn, tid) == []

    def test_a_dispatch_target_is_reported_as_routing_not_as_attribution(
        self, kanban_home
    ):
        """The second review's remaining ownership objection, answered honestly.

        ``assignee`` is structured — the control plane writes it, unlike the
        free-text ``created_by`` — but it answers "where does this run?", not
        "who is accountable for it?". The review was right that treating the two
        as the same claim is a category error.

        It is reported and NOT blocking, and the asymmetry with ``created_by``
        is decided by the same census that decided everything else here: all 34
        live cards resolve through ``assignee``, so blocking it strands the
        entire board, while blocking ``created_by`` strands none of it. The
        available honest move is to stop calling it clean — which is what this
        pins — not to pretend the board can operate without it.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "dispatched, not attributed")
            conn.execute(
                "UPDATE tasks SET actor_id = NULL, recovery_owner = NULL "
                "WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, tid)
            # Still passes the enforced boundary — the board keeps working...
            assert kb.require_resolvable_ownership(conn, tid) == ownership

        assert ownership.owner_source == "assignee"
        assert ownership.structurally_owned is True
        assert ownership.attributable is True
        # ...but the record no longer claims this is a clean accountable owner.
        assert "owner_only_from_assignee" in ownership.degraded_reasons
        assert ownership.governable is False
        assert "owner_only_from_assignee" not in kb.ATTRIBUTION_BLOCKING_REASONS

    def test_a_structured_actor_outranks_the_dispatch_target(self, kanban_home):
        """And a card that DOES record its actor is not tarred with that brush.

        Without this, the previous test would also pass against an
        implementation that degraded every card unconditionally.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(
                conn,
                "properly attributed",
                provenance=kb.ActorProvenance(
                    kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION,
                    actor_id="kanban-worker",
                    run_id="x_deadbeef",
                    cause=kb.CREATION_CAUSE_AUTOMATED,
                ),
            )
            ownership = kb.resolve_task_ownership(conn, tid)
        assert ownership.owner_source == "actor_id"
        assert ownership.degraded_reasons == ()
        assert ownership.governable is True

    @pytest.mark.parametrize(
        "reason",
        ["provenance_not_recorded", "no_executor_lane", "owner_only_from_assignee"],
    )
    def test_incompleteness_is_reported_but_never_blocking(
        self, kanban_home, reason
    ):
        """The half of the review's finding that was NOT adopted, and why.

        Enforcing completeness as well as trustworthiness was rejected on
        evidence, not preference: a read-only census of the live board on
        2026-09-03 found 33 of 34 live cards carrying
        ``provenance_not_recorded`` and 10 carrying ``no_executor_lane``, so
        blocking on either would strand nearly the whole board — including the
        preserved failure evidence this card is forbidden to restart from zero.
        Zero cards resolved through ``created_by``, which is why *that* one
        could be promoted to blocking at no cost.
        """
        assert reason not in kb.ATTRIBUTION_BLOCKING_REASONS
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "incomplete but honest")
            if reason == "provenance_not_recorded":
                conn.execute(
                    "UPDATE tasks SET actor_kind = NULL WHERE id = ?", (tid,)
                )
            else:
                conn.execute(
                    "UPDATE tasks SET executor_lane = NULL WHERE id = ?", (tid,)
                )
            conn.commit()
            ownership = kb.require_resolvable_ownership(conn, tid)
        assert reason in ownership.degraded_reasons
        assert ownership.blocking_reasons == ()
        assert ownership.attributable is True
        # Advisory, not clean: the record still says it is not fully governable.
        assert ownership.governable is False

    def test_the_ownership_record_publishes_the_boundary_it_is_judged_by(
        self, kanban_home
    ):
        """A reader gets the verdict, not just the raw reasons to re-derive it.

        Without this, every consumer would reimplement the blocking/advisory
        split from ``degraded_reasons``, and they would drift.
        """
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "published")
            payload = kb.resolve_task_ownership(conn, tid).as_dict()
        assert payload["attributable"] is True
        assert payload["blocking_reasons"] == []
        assert set(kb.ATTRIBUTION_BLOCKING_REASONS) == {
            "no_owner_recorded", "owner_only_from_created_by",
        }


# ---------------------------------------------------------------------------
# The four routes that must not lose their owner: task, recovery, verifier,
# parent
# ---------------------------------------------------------------------------

class TestRoutesSurviveSessionTermination:
    @pytest.fixture
    def graph(self, kanban_home, monkeypatch):
        """An umbrella, its subject, a repair, and a verifier child.

        Built inside a live session, which is then destroyed. Every assertion
        below runs against a board whose creating session no longer exists.
        """
        monkeypatch.setenv("HERMES_SESSION_ID", SESSION)
        with kb.connect_closing() as conn:
            umbrella = _create_in_session(conn, "umbrella: control-plane repair")
            subject = _create_in_session(conn, "subject: the defective path")
            repair = kb.create_repair_task(
                conn,
                title="repair: the defective path",
                subject_id=subject,
                umbrella_id=umbrella,
                owner="erika",
                assignee="default",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE_RECOVERY,
                recovery_gate_cmd="pytest -q tests/hermes_cli",
                session_id=SESSION,
            )
            verifier = _create_in_session(
                conn,
                "independent verification (codex_verify, read-only)",
                parents=[repair],
                executor_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
                provenance=kb.ActorProvenance(
                    kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION,
                    actor_id="kanban-dispatcher",
                    lane=kb.EXECUTOR_LANE_CLAUDE,
                    run_id="x_abc123",
                    cause=kb.CREATION_CAUSE_VERIFICATION,
                ),
            )
        _end_the_session(monkeypatch)
        return {
            "umbrella": umbrella,
            "subject": subject,
            "repair": repair,
            "verifier": verifier,
        }

    def test_task_ownership_survives(self, graph, monkeypatch):
        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                ownership = kb.resolve_task_ownership(conn, graph["subject"])
        assert ownership.attributable is True
        assert ownership.blocking_reasons == ()
        assert ownership.owner is not None

    def test_recovery_routing_survives(self, graph, monkeypatch):
        """The repair still knows its owner, its subject and its umbrella."""
        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                ownership = kb.resolve_task_ownership(conn, graph["repair"])

        assert ownership.owner == "erika"
        assert ownership.owner_source == "recovery_owner"
        assert ownership.creation_cause == kb.CREATION_CAUSE_RECOVERY
        assert ownership.repairs == (graph["subject"],)
        assert ownership.umbrella == (graph["umbrella"],)
        assert ownership.executor_lane == kb.EXECUTOR_LANE_CLAUDE_RECOVERY
        assert ownership.governable is True

    def test_verifier_return_path_survives(self, graph, monkeypatch):
        """The verifier can still find the card it must return its verdict to.

        The return path is the dependency parent, which lives in
        ``task_links`` — persisted, and unrelated to the session.
        """
        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                ownership = kb.resolve_task_ownership(conn, graph["verifier"])
                parent = kb.resolve_task_ownership(conn, ownership.parent_ids[0])

        assert ownership.parent_ids == (graph["repair"],)
        assert ownership.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
        # The creating lane and the running lane differ; conflating them would
        # lose the relay from the dispatcher to the verifier.
        assert ownership.actor_lane == kb.EXECUTOR_LANE_CLAUDE
        assert ownership.creation_cause == kb.CREATION_CAUSE_VERIFICATION
        assert parent.governable is True

    def test_parent_remains_governable(self, graph, monkeypatch):
        """The umbrella can still enumerate and govern what sits under it."""
        with kb.connect_closing() as conn:
            with _no_live_state(monkeypatch):
                umbrella = kb.resolve_task_ownership(conn, graph["umbrella"])
                inbound = kb.task_relations(
                    conn, graph["umbrella"], relation=kb.RELATION_UMBRELLA,
                    direction="to",
                )

        assert umbrella.attributable is True
        assert umbrella.blocking_reasons == ()
        assert [r["from_task_id"] for r in inbound] == [graph["repair"]]

    def test_a_repair_that_lost_its_linkage_is_reported_not_hidden(
        self, graph
    ):
        """An orphaned repair surfaces as an ownership degradation.

        Creation makes this state unreachable, so it is forced here to prove
        the *detection* works — the diagnostic has to hold for the pre-repair
        cards preserved on the live board, which no guard can retroactively
        fix.
        """
        with kb.connect_closing() as conn:
            conn.execute(
                "DELETE FROM task_relations WHERE from_task_id = ? "
                "AND relation = ?",
                (graph["repair"], kb.RELATION_REPAIRS),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, graph["repair"])

        assert "repair_missing_repairs" in ownership.degraded_reasons
        assert ownership.governable is False
        # Still owned — losing the linkage costs governability, not the owner.
        assert ownership.owner == "erika"


# ---------------------------------------------------------------------------
# Provenance is recorded, never invented
# ---------------------------------------------------------------------------

class TestNothingIsFabricated:
    def test_a_pre_migration_row_is_not_backfilled(self, kanban_home):
        """A legacy card answers "not recorded", which is the truth."""
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "predates provenance")
            conn.execute(
                "UPDATE tasks SET actor_kind = NULL, actor_id = NULL, "
                "actor_lane = NULL, actor_run_id = NULL, "
                "creation_cause = NULL WHERE id = ?",
                (tid,),
            )
            conn.commit()
            ownership = kb.resolve_task_ownership(conn, tid)
            # Resolution must not write anything back.
            row = conn.execute(
                "SELECT actor_kind, creation_cause FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()

        assert ownership.actor_kind is None
        assert ownership.creation_cause is None
        assert row["actor_kind"] is None and row["creation_cause"] is None
        assert "provenance_not_recorded" in ownership.degraded_reasons

    def test_every_new_card_records_an_actor_kind(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "new card")
            ownership = kb.resolve_task_ownership(conn, tid)
        assert ownership.actor_kind in kb.VALID_ACTOR_KINDS

    def test_ownership_snapshot_is_json_serialisable(self, kanban_home):
        """The record has to survive being written into an evidence packet."""
        with kb.connect_closing() as conn:
            tid = _create_in_session(conn, "serialisable")
            payload = kb.resolve_task_ownership(conn, tid).as_dict()
        assert json.loads(json.dumps(payload))["task_id"] == tid


def test_the_ownership_record_is_immutable(kanban_home):
    """Frozen: a caller cannot "correct" a degraded reading in memory."""
    with kb.connect_closing() as conn:
        tid = _create_in_session(conn, "frozen")
        ownership = kb.resolve_task_ownership(conn, tid)
    with pytest.raises((AttributeError, TypeError)):
        ownership.owner = "someone-else"  # type: ignore[misc]


def test_resolution_is_read_only_against_the_board(kanban_home):
    """No resolution path may write. Enforced by a read-only connection."""
    with kb.connect_closing() as conn:
        tid = _create_in_session(conn, "read-only probe")
        path = conn.execute("PRAGMA database_list").fetchone()["file"]

    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        ownership = kb.resolve_task_ownership(ro, tid)
        assert ownership.resolved is True
        assert kb.unowned_tasks(ro) == []
    finally:
        ro.close()
