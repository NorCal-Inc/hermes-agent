"""Structured actor provenance, and atomic repair->subject/owner/umbrella linkage.

Two defects, reproduced live on the shared Hermes control plane on 2026-09-03,
are pinned here.

**Defect A — actor provenance ambiguity.** ``tasks.created_by`` holds a profile
name. ``hermes_cli/kanban.py`` writes ``_profile_author()`` and
``tools/kanban_tools.py`` writes ``HERMES_PROFILE`` or ``"worker"``, so an
interactive session relaying Christopher's instruction and a governed automated
executor running under the same profile produce the *same string*. Any
acceptance rule that infers "a human relayed this" from that string is
asserting something the data does not contain. ``t_fb23ac0a`` carries
``created_by='claude-code'`` and was relayed by a human; nothing about the
string says so.

**Defect B — orphan governed repair creation.** ``t_aef6bbe1`` and
``t_f9b3b48b`` are both governed repair cards whose subject exists only as
prose in the body. Neither is reachable from the thing it repairs by any query.
Beyond discoverability, creating the card and linking it in two separate
transactions leaves a window in which an orphan exists; a crash, a lock
timeout, or a killed executor inside that window strands it permanently.

Both defect cards are preserved untouched as evidence. Nothing here backfills
or reinterprets them — the tests assert the NEW behaviour on NEW cards, and
one test asserts explicitly that an unrecorded provenance stays unrecorded.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Every provenance-derivation env var this repair reads is cleared, so a
    # test never inherits the provenance of the executor running pytest.
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


def _task_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


# ---------------------------------------------------------------------------
# Defect A — structured provenance separation and retention
# ---------------------------------------------------------------------------

class TestProvenanceSeparation:
    def test_identical_created_by_is_separable_by_actor_kind(self, kanban_home):
        """The live reproduction, in miniature.

        Two cards, byte-identical ``created_by``, genuinely different actors.
        The string cannot tell them apart; the structured fields can.
        """
        with kb.connect_closing() as conn:
            human = kb.create_task(
                conn,
                title="relayed by Christopher through an interactive session",
                created_by="claude-code",
                provenance=kb.resolve_actor_provenance(
                    env={"HERMES_PROFILE": "claude-code"},
                ),
            )
            automated = kb.create_task(
                conn,
                title="filed by a governed executor mid-run",
                created_by="claude-code",
                provenance=kb.resolve_actor_provenance(
                    env={
                        "HERMES_PROFILE": "claude-code",
                        # Injected into every dispatched executor by
                        # build_worker_env, and by nothing else.
                        "HERMES_KANBAN_TASK": "t_subject01",
                        "HERMES_KANBAN_RUN_ID": "4242",
                    },
                ),
            )

            # The defect, asserted rather than described: the identity string
            # is identical, so any rule reading it decides both cards the same
            # way. This assertion is the reason the rest of the file exists.
            assert (
                kb.get_task(conn, human).created_by
                == kb.get_task(conn, automated).created_by
                == "claude-code"
            )

            assert kb.is_human_relayed(conn, human) is True
            assert kb.is_governed_automation(conn, human) is False
            assert kb.is_human_relayed(conn, automated) is False
            assert kb.is_governed_automation(conn, automated) is True

    def test_all_structured_fields_are_retained(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="governed automation files follow-on work",
                created_by="worker",
                executor_lane=kb.EXECUTOR_LANE_CLAUDE,
                provenance=kb.resolve_actor_provenance(
                    env={
                        "HERMES_KANBAN_TASK": "t_parent01",
                        "HERMES_EXECUTION_ID": "x_abc123",
                        "HERMES_EXECUTOR_LANE": kb.EXECUTOR_LANE_CODEX_VERIFY,
                        "HERMES_PROFILE": "atlas",
                    },
                ),
            )
            prov = kb.task_provenance(conn, tid)
            assert prov["actor_kind"] == kb.ACTOR_KIND_GOVERNED_AUTOMATION
            assert prov["actor_id"] == "atlas"
            assert prov["creation_cause"] == kb.CREATION_CAUSE_AUTOMATED
            # Most specific run identifier wins: an execution id pins one
            # supervised process.
            assert prov["actor_run_id"] == "x_abc123"
            # The creating actor's lane and the created card's lane are
            # different facts, and both survive. Collapsing them would lose
            # the relay: a codex_verify run filed work for a claude lane.
            assert prov["actor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert prov["executor_lane"] == kb.EXECUTOR_LANE_CLAUDE

            task = kb.get_task(conn, tid)
            assert task.actor_kind == kb.ACTOR_KIND_GOVERNED_AUTOMATION
            assert task.actor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert task.actor_run_id == "x_abc123"
            assert task.creation_cause == kb.CREATION_CAUSE_AUTOMATED

    def test_provenance_is_on_the_append_only_event_log(self, kanban_home):
        """The row can be UPDATEd; the event log cannot. Audit reads the log."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="provenance survives a later row mutation",
                provenance=kb.resolve_actor_provenance(
                    env={"HERMES_KANBAN_TASK": "t_x", "HERMES_PROFILE": "worker"},
                ),
            )
            # A hand-written UPDATE that launders the row, exactly the kind of
            # out-of-band mutation kanban_db cannot prevent.
            conn.execute(
                "UPDATE tasks SET actor_kind = ?, created_by = ? WHERE id = ?",
                (kb.ACTOR_KIND_HUMAN_INTERACTIVE, "christopher", tid),
            )
            conn.commit()

            created = _events(conn, tid, kind="created")
            assert len(created) == 1
            assert created[0][1]["actor_kind"] == kb.ACTOR_KIND_GOVERNED_AUTOMATION
            assert created[0][1]["creation_cause"] == kb.CREATION_CAUSE_AUTOMATED

    def test_explicit_env_declaration_wins_but_junk_is_ignored(self, kanban_home):
        declared = kb.resolve_actor_provenance(
            env={
                kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_SYSTEM,
                "HERMES_KANBAN_TASK": "t_x",
            },
        )
        assert declared.kind == kb.ACTOR_KIND_SYSTEM

        # A junk declaration must not be stored — a bogus provenance is worse
        # than a derived one, because it looks authoritative. Derivation wins.
        junk = kb.resolve_actor_provenance(
            env={kb.ENV_ACTOR_KIND: "definitely-a-human", "HERMES_KANBAN_TASK": "t_x"},
        )
        assert junk.kind == kb.ACTOR_KIND_GOVERNED_AUTOMATION

    def test_invalid_kind_or_cause_is_refused_at_construction(self, kanban_home):
        with pytest.raises(ValueError):
            kb.ActorProvenance(kind="human-ish")
        with pytest.raises(ValueError):
            kb.ActorProvenance(
                kind=kb.ACTOR_KIND_SYSTEM, cause="because-i-said-so"
            )


class TestProvenanceBasedAcceptance:
    """Acceptance decisions read the enum, never the identity string."""

    def test_human_sounding_identity_does_not_satisfy_manual_relay(
        self, kanban_home
    ):
        """The adversarial case string inference gets wrong.

        A governed automated executor configured with a human-sounding
        identity. Every string heuristic ("does it look like a person?")
        passes it. The structured check refuses it, because a fact about the
        process — it is running inside a governed run — is not overridable by
        a name.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="automation wearing a human name",
                created_by="christopher",
                provenance=kb.resolve_actor_provenance(
                    env={
                        "HERMES_PROFILE": "christopher",
                        "HERMES_KANBAN_TASK": "t_running01",
                    },
                ),
            )
            assert kb.get_task(conn, tid).created_by == "christopher"
            assert kb.is_human_relayed(conn, tid) is False

    def test_unrecorded_provenance_fails_closed(self, kanban_home):
        """A pre-migration row answers "not recorded", and that is not a human.

        This is the rule that protects the preserved evidence cards. Their
        provenance was never recorded, so no acceptance decision may treat
        them as human-relayed on the strength of ``created_by`` alone.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="legacy-shaped card")
            # Simulate a row written before the columns existed.
            conn.execute(
                "UPDATE tasks SET actor_kind = NULL, actor_id = NULL, "
                "creation_cause = NULL, created_by = 'claude-code' WHERE id = ?",
                (tid,),
            )
            conn.commit()

            prov = kb.task_provenance(conn, tid)
            # Every key present, so "not recorded" is distinguishable from a
            # recorded 'unknown'.
            assert prov["actor_kind"] is None
            assert prov["created_by"] == "claude-code"
            assert kb.is_human_relayed(conn, tid) is False
            assert kb.is_governed_automation(conn, tid) is False

    def test_missing_task_is_not_a_human_relay(self, kanban_home):
        with kb.connect_closing() as conn:
            assert kb.task_provenance(conn, "t_nonexistent") is None
            assert kb.is_human_relayed(conn, "t_nonexistent") is False

    def test_default_derivation_records_a_kind_on_every_card(self, kanban_home):
        """No creation path can produce a card with no recorded actor kind."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="no provenance argument at all")
            task = kb.get_task(conn, tid)
            assert task.actor_kind in kb.VALID_ACTOR_KINDS
            assert task.creation_cause in kb.VALID_CREATION_CAUSES
            # Bare pytest env: no governed run around it, so this is the
            # interactive branch.
            assert task.actor_kind == kb.ACTOR_KIND_HUMAN_INTERACTIVE
            assert task.creation_cause == kb.CREATION_CAUSE_MANUAL_RELAY


# ---------------------------------------------------------------------------
# Defect B — atomic subject/owner/umbrella linkage, fail closed
# ---------------------------------------------------------------------------

def _subject_and_umbrella(conn):
    subject = kb.create_task(conn, title="subject: the thing being repaired")
    umbrella = kb.create_task(conn, title="umbrella: the recovery programme")
    return subject, umbrella


class TestAtomicRepairLinkage:
    def test_creation_establishes_subject_owner_and_umbrella(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            evidence = kb.create_task(conn, title="preserved evidence card")

            repair = kb.create_repair_task(
                conn,
                title="Repair: the named defect",
                body="prose is still allowed; it is just no longer the only record",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
                evidence_ids=[evidence],
            )

            assert kb.missing_repair_relations(conn, repair) == []
            assert kb.relation_targets(conn, repair, kb.RELATION_REPAIRS) == [subject]
            assert kb.relation_targets(conn, repair, kb.RELATION_UMBRELLA) == [umbrella]
            assert kb.relation_targets(conn, repair, kb.RELATION_EVIDENCE) == [evidence]

            task = kb.get_task(conn, repair)
            assert task.recovery_owner == "erika"
            assert task.creation_cause == kb.CREATION_CAUSE_RECOVERY

            # The question a governor actually asks: "what is repairing this?"
            # Answerable from the subject's side, which is the whole point.
            incoming = kb.task_relations(
                conn, subject, relation=kb.RELATION_REPAIRS, direction="to"
            )
            assert [r["from_task_id"] for r in incoming] == [repair]

    def test_relations_do_not_gate_readiness(self, kanban_home):
        """A repair is not blocked on the subject it repairs.

        This is why the relations live outside ``task_links``: the dependency
        graph means "parent must finish first", and the subject usually cannot
        finish until the repair lands. Expressing the relation as a dependency
        would deadlock every repair by construction.
        """
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            repair = kb.create_repair_task(
                conn,
                title="Repair: must be runnable while its subject is open",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
            )
            assert kb.get_task(conn, subject).status != "done"
            assert kb.get_task(conn, repair).status == "ready"
            assert kb.parent_ids(conn, repair) == []

    def test_both_ends_can_discover_the_relation(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            repair = kb.create_repair_task(
                conn,
                title="Repair: discoverable from either side",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
            )
            assert _events(conn, repair, kind="relation_added")
            assert _events(conn, subject, kind="relation_received")
            assert _events(conn, umbrella, kind="relation_received")
            linked = _events(conn, repair, kind="repair_linked")
            assert len(linked) == 1
            assert linked[0][1]["subject"] == subject
            assert linked[0][1]["umbrella"] == umbrella
            assert linked[0][1]["owner"] == "erika"

    def test_evidence_pointer_does_not_rewrite_the_cited_card(self, kanban_home):
        """A citation must not re-scope what it cites."""
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            cited = kb.create_task(
                conn, title="existing umbrella card", body="original scope text"
            )
            before = kb.get_task(conn, cited)

            kb.create_repair_task(
                conn,
                title="Repair: cites without editing",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
                evidence_ids=[cited],
            )
            after = kb.get_task(conn, cited)
            assert after.title == before.title
            assert after.body == before.body
            assert after.status == before.status
            assert after.priority == before.priority
            # Discoverable all the same.
            assert kb.task_relations(
                conn, cited, relation=kb.RELATION_EVIDENCE, direction="to"
            )


class TestFailClosedOrphanPrevention:
    @pytest.mark.parametrize(
        "kwargs, why",
        [
            ({"subject_id": "t_doesnotexist"}, "unknown subject"),
            ({"umbrella_id": "t_doesnotexist"}, "unknown umbrella"),
            ({"subject_id": ""}, "blank subject"),
            ({"umbrella_id": ""}, "blank umbrella"),
            ({"owner": ""}, "blank owner"),
            ({"owner": "   "}, "whitespace owner"),
        ],
    )
    def test_unestablishable_linkage_creates_no_card(
        self, kanban_home, kwargs, why
    ):
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            before = _task_count(conn)

            call = {
                "title": f"Repair that must not exist ({why})",
                "subject_id": subject,
                "owner": "erika",
                "umbrella_id": umbrella,
            }
            call.update(kwargs)

            with pytest.raises(kb.RecoveryLinkageError):
                kb.create_repair_task(conn, **call)

            # Fail CLOSED: not a card in a bad state — no card.
            assert _task_count(conn) == before
            assert kb.orphaned_repair_tasks(conn) == []

    def test_a_failure_between_insert_and_linkage_leaves_no_orphan(
        self, kanban_home, monkeypatch
    ):
        """The window this repair closes, forced open deliberately.

        The card is inserted, then linking raises — a crash, a lock timeout, a
        killed executor. Before this repair that stranded a permanently
        orphaned card that looked like real work. Both operations now share
        one transaction, so the insert unwinds with the failure.
        """
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            before = _task_count(conn)

            real = kb.add_task_relation
            calls: list[str] = []

            def exploding(conn_, frm, to, relation, **kw):
                calls.append(relation)
                if relation == kb.RELATION_UMBRELLA:
                    raise sqlite3.OperationalError("database is locked")
                return real(conn_, frm, to, relation, **kw)

            monkeypatch.setattr(kb, "add_task_relation", exploding)

            with pytest.raises(sqlite3.OperationalError):
                kb.create_repair_task(
                    conn,
                    title="Repair interrupted mid-linkage",
                    subject_id=subject,
                    owner="erika",
                    umbrella_id=umbrella,
                )

            # It really did get as far as inserting and linking the subject.
            assert calls == [kb.RELATION_REPAIRS, kb.RELATION_UMBRELLA]
            # And none of it survived.
            assert _task_count(conn) == before
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM task_relations"
            ).fetchone()["n"] == 0
            assert kb.orphaned_repair_tasks(conn) == []

    def test_silently_missing_relation_is_caught_by_readback(
        self, kanban_home, monkeypatch
    ):
        """A linker that reports success without landing a row is refused.

        ``INSERT OR IGNORE`` returns cleanly when it inserts nothing, so a
        caller's in-memory success flag is not evidence. The read-back inside
        the transaction is.
        """
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            before = _task_count(conn)

            real = kb.add_task_relation

            def skips_umbrella(conn_, frm, to, relation, **kw):
                if relation == kb.RELATION_UMBRELLA:
                    return False  # claims success, writes nothing
                return real(conn_, frm, to, relation, **kw)

            monkeypatch.setattr(kb, "add_task_relation", skips_umbrella)

            with pytest.raises(kb.RecoveryLinkageError) as exc:
                kb.create_repair_task(
                    conn,
                    title="Repair whose umbrella link never landed",
                    subject_id=subject,
                    owner="erika",
                    umbrella_id=umbrella,
                )
            assert kb.RELATION_UMBRELLA in str(exc.value)
            assert _task_count(conn) == before
            assert kb.orphaned_repair_tasks(conn) == []


class TestCreateTaskBoundaryIsTheGuard:
    """The bypass independent review found, and the reason the guard moved.

    An earlier draft enforced subject/owner/umbrella only inside
    ``create_repair_task``. ``codex_verify`` (execution ``x_f38029eabd6342bf``)
    refuted the non-orphaning claim in one step: call ``create_task``
    directly with a ``recovery`` creation cause and the helper is simply not
    involved. ``tools/kanban_tools.py`` and the CLI both reach ``create_task``
    that way, so the hole was on the path real callers use.
    """

    def test_recovery_cause_without_linkage_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            before = _task_count(conn)
            with pytest.raises(kb.RecoveryLinkageError):
                kb.create_task(
                    conn,
                    title="orphan recovery card via the direct path",
                    provenance=kb.ActorProvenance(
                        kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION,
                        cause=kb.CREATION_CAUSE_RECOVERY,
                    ),
                )
            assert _task_count(conn) == before
            assert kb.orphaned_repair_tasks(conn) == []

    @pytest.mark.parametrize(
        "partial",
        [
            {"recovery_owner": "erika"},
            {"repairs_task_id": "SUBJECT"},
            {"umbrella_task_id": "UMBRELLA"},
            {"recovery_owner": "erika", "repairs_task_id": "SUBJECT"},
            {"repairs_task_id": "SUBJECT", "umbrella_task_id": "UMBRELLA"},
        ],
    )
    def test_a_partial_repair_declaration_is_refused(self, kanban_home, partial):
        """Any one field arms the requirement; a partial set cannot dodge it."""
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            resolved = {
                k: {"SUBJECT": subject, "UMBRELLA": umbrella}.get(v, v)
                for k, v in partial.items()
            }
            before = _task_count(conn)
            with pytest.raises(kb.RecoveryLinkageError):
                kb.create_task(
                    conn, title="partially declared repair", **resolved
                )
            assert _task_count(conn) == before
            assert kb.orphaned_repair_tasks(conn) == []

    def test_full_declaration_through_create_task_is_linked(self, kanban_home):
        """The direct path is not forbidden — it is required to be complete."""
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            tid = kb.create_task(
                conn,
                title="repair created without the helper",
                recovery_owner="erika",
                repairs_task_id=subject,
                umbrella_task_id=umbrella,
            )
            assert kb.missing_repair_relations(conn, tid) == []
            assert kb.relation_targets(conn, tid, kb.RELATION_REPAIRS) == [subject]
            assert kb.relation_targets(conn, tid, kb.RELATION_UMBRELLA) == [umbrella]
            # Declaring linkage normalises the cause, so cause-based queries
            # (orphan sweeps, reporting) cannot miss the card.
            assert kb.get_task(conn, tid).creation_cause == kb.CREATION_CAUSE_RECOVERY
            assert kb.orphaned_repair_tasks(conn) == []

    def test_unknown_linkage_target_creates_no_card(self, kanban_home):
        with kb.connect_closing() as conn:
            _, umbrella = _subject_and_umbrella(conn)
            before = _task_count(conn)
            with pytest.raises(kb.RecoveryLinkageError):
                kb.create_task(
                    conn,
                    title="repair pointing at a ghost",
                    recovery_owner="erika",
                    repairs_task_id="t_ghost",
                    umbrella_task_id=umbrella,
                )
            assert _task_count(conn) == before

    def test_ordinary_cards_are_unaffected(self, kanban_home):
        """The guard must not arm on anything that is not a repair."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="an ordinary card")
            task = kb.get_task(conn, tid)
            assert task.recovery_owner is None
            assert task.creation_cause != kb.CREATION_CAUSE_RECOVERY
            assert kb.task_relations(conn, tid, direction="both") == []


class TestRelationApiGuards:
    def test_unknown_relation_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a")
            b = kb.create_task(conn, title="b")
            with pytest.raises(ValueError):
                kb.add_task_relation(conn, a, b, "supersedes")

    def test_self_relation_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a")
            with pytest.raises(ValueError):
                kb.add_task_relation(conn, a, a, kb.RELATION_REPAIRS)

    def test_unknown_endpoint_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a")
            with pytest.raises(ValueError):
                kb.add_task_relation(conn, a, "t_ghost", kb.RELATION_REPAIRS)

    def test_relation_is_idempotent(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a")
            b = kb.create_task(conn, title="b")
            assert kb.add_task_relation(conn, a, b, kb.RELATION_EVIDENCE) is True
            assert kb.add_task_relation(conn, a, b, kb.RELATION_EVIDENCE) is False
            assert len(kb.task_relations(conn, a)) == 1
            # One relation_added event, not two.
            assert len(_events(conn, a, kind="relation_added")) == 1

    def test_relations_cascade_on_delete(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            repair = kb.create_repair_task(
                conn,
                title="Repair to be deleted",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
            )
            assert kb.delete_task(conn, repair) is True
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM task_relations"
            ).fetchone()["n"] == 0

    def test_relations_cascade_on_archived_delete_too(self, kanban_home):
        """The second deletion path. Covering only one leaves dangling rows.

        Flagged by independent review: ``delete_task`` and
        ``delete_archived_task`` are separate functions with separate cascade
        lists, so testing one says nothing about the other.
        """
        with kb.connect_closing() as conn:
            subject, umbrella = _subject_and_umbrella(conn)
            repair = kb.create_repair_task(
                conn,
                title="Repair to be archived then purged",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
            )
            conn.execute(
                "UPDATE tasks SET status = 'archived' WHERE id = ?", (repair,)
            )
            conn.commit()
            assert kb.delete_archived_task(conn, repair) is True
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM task_relations"
            ).fetchone()["n"] == 0


class TestLegacyBoardMigration:
    def test_migration_is_additive_and_backfills_nothing(
        self, tmp_path, monkeypatch
    ):
        """A board written before this repair keeps its history untouched.

        The preserved orphan reproductions depend on this: retro-stamping a
        provenance onto ``t_aef6bbe1``/``t_f9b3b48b`` would destroy the very
        evidence they are kept for.

        The board is built with a genuine PRE-migration schema — the columns
        and the ``task_relations`` table physically do not exist — rather than
        by creating a current-schema row and nulling its fields. Independent
        review called the weaker version out, and correctly: nulling a column
        that already exists never exercises ``ALTER TABLE`` at all, so it
        would pass even if the migration were missing entirely.
        """
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        db_path = kb.kanban_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            """
            CREATE TABLE tasks (
                id             TEXT PRIMARY KEY,
                title          TEXT NOT NULL,
                body           TEXT,
                assignee       TEXT,
                status         TEXT NOT NULL,
                priority       INTEGER DEFAULT 0,
                created_by     TEXT,
                created_at     INTEGER NOT NULL,
                started_at     INTEGER,
                completed_at   INTEGER,
                workspace_kind TEXT NOT NULL DEFAULT 'scratch',
                workspace_path TEXT,
                claim_lock     TEXT,
                claim_expires  INTEGER
            );
            CREATE TABLE task_links (
                parent_id TEXT NOT NULL,
                child_id  TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );
            CREATE TABLE task_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        raw.execute(
            "INSERT INTO tasks (id, title, status, created_by, created_at, "
            "workspace_kind) VALUES ('t_legacy01', 'legacy card', 'ready', "
            "'claude-code', 1, 'scratch')"
        )
        raw.commit()
        # Prove the starting point really is pre-migration.
        cols = {r[1] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "actor_kind" not in cols
        assert not raw.execute(
            "SELECT name FROM sqlite_master WHERE name = 'task_relations'"
        ).fetchall()
        raw.close()

        # Opening the board runs the migration.
        kb.init_db()

        with kb.connect_closing() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert {
                "actor_kind", "actor_id", "actor_lane", "actor_run_id",
                "creation_cause", "recovery_owner",
            } <= cols
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'task_relations'"
            ).fetchone() is not None

            # The pre-existing row survived and gained nothing invented.
            prov = kb.task_provenance(conn, "t_legacy01")
            assert prov["created_by"] == "claude-code"
            assert prov["actor_kind"] is None
            assert prov["creation_cause"] is None
            assert prov["recovery_owner"] is None
            assert kb.is_human_relayed(conn, "t_legacy01") is False
            assert kb.task_relations(conn, "t_legacy01", direction="both") == []
            assert kb.get_task(conn, "t_legacy01").title == "legacy card"

            # And the migrated board is fully functional for new work.
            subject = kb.create_task(conn, title="new subject")
            umbrella = kb.create_task(conn, title="new umbrella")
            repair = kb.create_repair_task(
                conn,
                title="repair on a migrated board",
                subject_id=subject,
                owner="erika",
                umbrella_id=umbrella,
            )
            assert kb.missing_repair_relations(conn, repair) == []
