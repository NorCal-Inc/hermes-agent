"""Control-plane admission control (#3).

Makes the 2026-08-31 Gauntlet closure boundary mechanical:

    "The Gauntlet project is closed as construction work. Future operational
     tasks are governed BY the Gauntlet; they must not reopen Gauntlet
     construction merely because a later task fails."

251 control-plane cards were created after that closure because the rule lived
only in a vault note. These tests are the rule.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB.

    Every provenance-derivation env var is cleared, so a test never inherits
    the provenance of the executor running pytest — which matters more here
    than anywhere else, since this suite's whole subject is who created a card.
    """
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


def _auto():
    return kb.ActorProvenance(
        kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION,
        actor_id="worker",
        cause=kb.CREATION_CAUSE_AUTOMATED,
    )


def _human():
    return kb.ActorProvenance(
        kind=kb.ACTOR_KIND_HUMAN_INTERACTIVE,
        actor_id="christopher",
        cause=kb.CREATION_CAUSE_MANUAL_RELAY,
    )


class TestControlPlaneClassifier:
    @pytest.mark.parametrize("title", [
        "Repair Gauntlet control plane: verifier return paths",
        "Implement controlled calibration canary for Gauntlet observer",
        "Integrate verified self-review parent/child release repair",
        "Gauntlet circuit breaker: fresh-session checkpoint on timeout",
        "Repair: unvalidated observation timer",
        "Bind shared boot record to canonical checkpoint",
        "Update boot context consumer",
    ])
    def test_control_plane_subjects_are_recognised(self, title):
        assert kb.control_plane_subject(title) is True

    @pytest.mark.parametrize("title", [
        "Digest and file 7 newly detected Jared Rhodenizer videos",
        "Orion compliance audit - fresh acceptance run",
        "finance-agent: inbox scans + stripe checks",
        # An infrastructure investigation is NOT control-plane construction.
        # This is the distinction the 2026-09-06 analysis turned on: the
        # largest recursion driver was infra work, not self-reference.
        "Investigate Hermes host-memory watchdog missed resolution trigger",
    ])
    def test_ordinary_work_is_not_control_plane(self, title):
        assert kb.control_plane_subject(title) is False


class TestControlPlaneAdmission:
    def test_automation_cannot_create_control_plane_work(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.ControlPlaneAdmissionError) as ei:
                kb.create_task(
                    conn, title="Repair Gauntlet verifier routing",
                    assignee="default", provenance=_auto(),
                )
            assert "closed as construction work" in str(ei.value)

    def test_authority_must_name_its_justification(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.ControlPlaneAdmissionError) as ei:
                kb.create_task(
                    conn, title="Repair Gauntlet verifier routing",
                    assignee="default", provenance=_auto(),
                    control_plane_authority="because I said so",
                )
            assert "must start with one of" in str(ei.value)

    @pytest.mark.parametrize("authority", [
        "operator:christopher",
        "regression:v_1234",
        "requirement:REQ-9",
        "defect:D12 verifier drops evidence",
    ])
    def test_named_authority_admits_and_is_recorded(self, kanban_home, authority):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Repair Gauntlet verifier routing",
                assignee="default", provenance=_auto(),
                control_plane_authority=authority,
            )
            row = conn.execute(
                "SELECT control_plane, control_plane_authority FROM tasks WHERE id=?",
                (tid,),
            ).fetchone()
            assert row["control_plane"] == 1
            assert row["control_plane_authority"] == authority

    def test_human_may_create_control_plane_work(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Gauntlet closure gate review",
                assignee="default", provenance=_human(),
            )
            row = conn.execute(
                "SELECT control_plane FROM tasks WHERE id=?", (tid,)
            ).fetchone()
            assert row["control_plane"] == 1

    def test_ordinary_automated_work_is_untouched(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Digest Jared videos", assignee="default",
                provenance=_auto(),
            )
            row = conn.execute(
                "SELECT control_plane, control_plane_authority FROM tasks WHERE id=?",
                (tid,),
            ).fetchone()
            assert row["control_plane"] == 0
            assert row["control_plane_authority"] is None

    def test_human_control_plane_subject_opens_required_verifier_first_try(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Update shared boot consumer checkpoint pin",
                assignee="default",
                provenance=_human(),
            )
            kb.store_attachment_bytes(
                conn, tid, "acceptance.txt", b"boot status complete\n",
                content_type="text/plain", uploaded_by="test",
            )
            child = kb._ensure_independent_verifier_child(
                conn, tid, implementer="default"
            )
            assert child is not None
            row = conn.execute(
                "SELECT control_plane, control_plane_authority, executor_lane "
                "FROM tasks WHERE id=?", (child,),
            ).fetchone()
            assert row["control_plane"] == 1
            assert row["control_plane_authority"] == (
                f"operator:human-interactive-parent:{tid}"
            )
            assert row["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            failed = conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE task_id=? AND kind='independent_verifier_child_failed'",
                (tid,),
            ).fetchone()[0]
            assert failed == 0


class TestControlPlaneInheritance:
    def test_child_inherits_classification_from_parent(self, kanban_home):
        """A blandly-titled child cannot launder a control-plane chain clean."""
        with kb.connect_closing() as conn:
            parent = kb.create_task(
                conn, title="Gauntlet closure gate review",
                assignee="default", provenance=_human(),
            )
            with pytest.raises(kb.ControlPlaneAdmissionError):
                kb.create_task(
                    conn, title="Follow-up step two", assignee="default",
                    parents=[parent], provenance=_auto(),
                )

    def test_child_of_ordinary_parent_is_unaffected(self, kanban_home):
        with kb.connect_closing() as conn:
            parent = kb.create_task(
                conn, title="Digest Jared videos", assignee="default",
                provenance=_human(),
            )
            tid = kb.create_task(
                conn, title="Follow-up step two", assignee="default",
                parents=[parent], provenance=_auto(),
            )
            assert conn.execute(
                "SELECT control_plane FROM tasks WHERE id=?", (tid,)
            ).fetchone()["control_plane"] == 0


class TestSecondGenerationControlPlane:
    """A control-plane repair may not automatically create another one.

    This is the chain that ran 2026-09-01..06: repair -> verifier -> recovery
    -> another verifier, each generation minting the next without a human.
    """

    def _control_plane_card_with_run(self, conn):
        tid = kb.create_task(
            conn, title="Repair Gauntlet control plane",
            assignee="default", provenance=_human(),
        )
        with kb.write_txn(conn):
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at) "
                "VALUES (?, 'default', 'running', 1)", (tid,),
            )
            run_id = cur.lastrowid
        return tid, run_id

    def test_control_plane_card_cannot_spawn_another_automatically(self, kanban_home):
        with kb.connect_closing() as conn:
            spawner, run_id = self._control_plane_card_with_run(conn)
            prov = kb.ActorProvenance(
                kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION, actor_id="worker",
                run_id=str(run_id), cause=kb.CREATION_CAUSE_AUTOMATED,
            )
            # Even a named defect is not enough at the second generation.
            with pytest.raises(kb.ControlPlaneAdmissionError) as ei:
                kb.create_task(
                    conn, title="Repair the Gauntlet verifier again",
                    assignee="default", provenance=prov,
                    control_plane_authority="defect:D13",
                )
            assert "second-generation" in str(ei.value)
            assert spawner in str(ei.value)

    def test_operator_authority_admits_the_second_generation(self, kanban_home):
        with kb.connect_closing() as conn:
            _spawner, run_id = self._control_plane_card_with_run(conn)
            prov = kb.ActorProvenance(
                kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION, actor_id="worker",
                run_id=str(run_id), cause=kb.CREATION_CAUSE_AUTOMATED,
            )
            tid = kb.create_task(
                conn, title="Repair the Gauntlet verifier again",
                assignee="default", provenance=prov,
                control_plane_authority="operator:christopher",
            )
            assert conn.execute(
                "SELECT control_plane FROM tasks WHERE id=?", (tid,)
            ).fetchone()["control_plane"] == 1

    def test_ordinary_card_may_still_spawn_ordinary_work(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Digest Jared videos", assignee="default",
                provenance=_human(),
            )
            with kb.write_txn(conn):
                cur = conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, started_at) "
                    "VALUES (?, 'default', 'running', 1)", (tid,),
                )
                run_id = cur.lastrowid
            prov = kb.ActorProvenance(
                kind=kb.ACTOR_KIND_GOVERNED_AUTOMATION, actor_id="worker",
                run_id=str(run_id), cause=kb.CREATION_CAUSE_AUTOMATED,
            )
            child = kb.create_task(
                conn, title="Second half of the digest", assignee="default",
                provenance=prov,
            )
            assert child


class TestSelectiveGauntletEnforcement:
    """Independent verification is reserved for high-risk mutations.

    Ordinary deploy/publish/customer-facing work uses task-local deterministic
    proof unless the author explicitly requests independent verification.
    """

    @pytest.mark.parametrize("title", [
        "Rotate the Stripe billing credential",
        "Schema migration for the customer table",
        "Issue a customer refund",
        "Cross-entity data migration for company routing",
        "Change the production firewall port binding",
        "Update shared boot consumer checkpoint pin",
    ])
    def test_high_risk_mutating_work_is_governed(self, title):
        assert kb.gauntlet_default_for_subject(title) is True

    @pytest.mark.parametrize("title", [
        "Deploy the Orion pricing page to production",
        "Publish the customer-facing onboarding page",
        "Ship the onboarding deliverable",
    ])
    def test_ordinary_mutation_falls_through_to_board_policy(self, title):
        assert kb.gauntlet_default_for_subject(title) is None

    @pytest.mark.parametrize("title", [
        "Investigate Hermes host-memory watchdog missed resolution trigger",
        "Diagnose the dispatcher stall",
        "Read-only audit of the profile matrix",
        "Root cause analysis for the gateway restart",
        "Exploratory debug of the worker spawn path",
        "Triage the overnight incident",
    ])
    def test_investigation_is_not_governed(self, title):
        assert kb.gauntlet_default_for_subject(title) is False

    def test_high_risk_mutation_beats_investigation_when_both_present(self):
        assert kb.gauntlet_default_for_subject(
            "Investigate the credential leak and rotate the credential"
        ) is True

    # -- Gauntlet defect A (2026-10-12): a look-only word must not switch
    #    verification off on a card that changes state. ------------------

    _T_C73A6B89_TITLE = (
        "Live execution: digest BidVantage transcript and verify dashboard closure"
    )
    _T_C73A6B89_BODY = (
        "Execute the full bounded LifeWiki digest workflow authorized by Erika "
        "for dashboard row TASK-20261004-003231876033-f8411e.\n\n"
        "Exact input (and only input in whisper_output you may touch): "
        "/home/chris/.hermes/video-intelligence/incoming/whisper_output/"
        "bidvantage-7689492264346832158.txt (expected 1,231 bytes). Scope is "
        "shared/internal LifeWiki intake only; no company-private data. Do not "
        "touch any other whisper_output file.\n\n"
        "Requirements:\n"
        "1. Inspect the named input and complete canonical "
        "filing/tagging/synthesis/indexing, using POST /api/ingest where "
        "applicable. Delete the named input only after verified authoritative "
        "filing.\n"
        "2. Read back the resulting authoritative LifeWiki state and "
        "dashboard/API row. Do not fabricate evidence or merely mark verified.\n"
        "3. Update dashboard row TASK-20261004-003231876033-f8411e so it leaves "
        "FAILED and becomes closed+VERIFIED with real values for: "
        "filed_note_paths, entity_tags, synthesis_path, updated_index_paths, "
        "deleted_media_count, reclaimed_bytes, ambiguous_items_flagged, "
        "report_delivered_to, report_message_id.\n"
        "4. Return exact artifact paths and authoritative dashboard/API "
        "read-back evidence. Touch no other whisper_output file.\n"
        "5. Trust-score restoration and Christopher notification remain "
        "Erika/independent-monitor responsibilities.\n\n"
        "Acceptance: row closed+VERIFIED with all required real evidence; input "
        "deleted only after verified filing; exact artifact/read-back evidence "
        "returned; other whisper files untouched. Block rather than invent "
        "unavailable reporting values.\n\n"
        "Coordination: report your result on this card and also comment the "
        "complete evidence handoff onto orchestrator card t_83804c18 so it can "
        "close with acceptance proof."
    )

    def test_defect_a_the_exact_t_c73a6b89_card_is_governed(self):
        """The card that closed on self-report with zero task_verifications.

        Its only investigation word is "Inspect"; it deletes a file, files
        notes, updates a dashboard row and marks it verified.
        """
        assert kb._GAUNTLET_INVESTIGATION_RE.search(self._T_C73A6B89_BODY)
        assert kb.gauntlet_default_for_subject(
            self._T_C73A6B89_TITLE, self._T_C73A6B89_BODY
        ) is True

    def test_defect_a_stamps_gauntlet_enforced_against_a_permissive_board(
        self, kanban_home, monkeypatch
    ):
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title=self._T_C73A6B89_TITLE, body=self._T_C73A6B89_BODY,
                assignee="default", provenance=_human(),
            )
            assert conn.execute(
                "SELECT gauntlet_enforced FROM tasks WHERE id=?", (tid,)
            ).fetchone()["gauntlet_enforced"] == 1

    @pytest.mark.parametrize("title, body", [
        ("Inspect the intake folder and delete the stale inputs", None),
        ("Investigate the digest stall", "Then update the dashboard row to VERIFIED."),
        ("Diagnose the index drift", "Write the corrected notes and file them into the vault."),
        ("Triage the notify backlog", "Send the report to Erika when done."),
        ("Research the worktree layout", "Move the orphaned notes under Business/Operations."),
        ("Root cause the failed import", "Close the task once the row is marked verified."),
        ("Live execution: run the bounded digest", "Inspect the input first."),
        ("Analysis of the gateway", "Restart the gateway and commit the config."),
    ])
    def test_state_changing_card_is_governed_despite_investigation_words(
        self, title, body
    ):
        assert kb._GAUNTLET_INVESTIGATION_RE.search(f"{title}\n{body or ''}")
        assert kb.gauntlet_default_for_subject(title, body) is True

    @pytest.mark.parametrize("title, body", [
        ("Investigate why the digest stalled",
         "Investigate why X, report findings, change nothing."),
        ("Root cause analysis for the gateway restart", None),
        ("Diagnose the dashboard row that was moved to FAILED",
         "Read-only: report which process updated it and when. Do not modify anything."),
        ("Triage the backlog", "Inspect the source file and report on its state."),
        # t_df2a1e7a / t_6384fb98: a prohibition is not an action, and
        # third-person description of what another actor will do is not one
        # either.
        ("Erika check: Phase 1 inventory result before Phase 2/3 restart",
         "Read-only. Reply CHECK: PASS or CHECK: FAIL. Then complete this card. "
         "Do not edit any file. Never delete anything, don't send messages, "
         "and do not merely mark verified. Claude Code restarts them on PASS."),
    ])
    def test_pure_investigation_stays_ungoverned(self, title, body):
        assert kb.gauntlet_default_for_subject(title, body) is False

    def test_describing_a_past_change_is_not_making_one(self):
        """Nouns and past participles describe history; only imperatives act."""
        assert kb.gauntlet_default_for_subject(
            "Investigate the data that was deleted, moved and updated last night"
        ) is False
        assert kb.gauntlet_default_for_subject("verified, closed, filed") is None

    def test_noun_use_of_a_listed_verb_is_not_an_imperative(self):
        """The t_887d70e9-shaped fixture in test_kanban_overtaken_by_events:
        "restart" is a noun here; the card is neither classified governed nor
        ungoverned by subject, so board policy decides as before."""
        assert kb.gauntlet_default_for_subject(
            "load and validate",
            "Post-verification live-load validation for commit deadbee.\n"
            "1. Confirm the repository is clean and HEAD equals origin.\n"
            "2. Confirm no unrelated active workers would be disrupted.\n"
            "3. Perform exactly one controlled restart to load the commit.\n",
        ) is None
        assert kb.gauntlet_default_for_subject(
            "Sandbox smoke test", "Do no company work, create no cards, send no messages."
        ) is None

    def test_title_ending_in_a_noun_does_not_borrow_the_body(self):
        """t_df2a1e7a: "... before Phase 2/3 restart" + a body on the next line
        read as "restart Erika" when the object could cross the newline."""
        assert kb.gauntlet_default_for_subject(
            "Erika check: Phase 1 inventory result before Phase 2/3 restart",
            "Erika, review the inventory findings and report whether Phase 2 may start.",
        ) is False

    def test_undecidable_subject_falls_through_to_board_config(self, monkeypatch):
        assert kb.gauntlet_default_for_subject("Tidy the notes") is None
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: True)
        assert kb._resolve_gauntlet_default("Tidy the notes") is True
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        assert kb._resolve_gauntlet_default("Tidy the notes") is False

    def test_high_risk_classification_overrides_a_permissive_board_default(
        self, kanban_home, monkeypatch
    ):
        """Board says off; a credential rotation is still governed."""
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Rotate the Orion production credential",
                assignee="default", provenance=_human(),
            )
            assert conn.execute(
                "SELECT gauntlet_enforced FROM tasks WHERE id=?", (tid,)
            ).fetchone()["gauntlet_enforced"] == 1

    def test_classification_overrides_a_strict_board_default(
        self, kanban_home, monkeypatch
    ):
        """Board says on; an investigation is still ungoverned.

        This is the change that stops a non-converging infra question becoming
        a verification chain -- t_0ce21cbe produced 59 verifier cards.
        """
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: True)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Investigate host-memory watchdog trigger",
                assignee="default", provenance=_human(),
            )
            assert conn.execute(
                "SELECT gauntlet_enforced FROM tasks WHERE id=?", (tid,)
            ).fetchone()["gauntlet_enforced"] == 0

    def test_explicit_argument_still_wins_over_classification(
        self, kanban_home, monkeypatch
    ):
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        with kb.connect_closing() as conn:
            forced_on = kb.create_task(
                conn, title="Investigate something", assignee="default",
                gauntlet=True, provenance=_human(),
            )
            forced_off = kb.create_task(
                conn, title="Deploy to production", assignee="default",
                gauntlet=False, provenance=_human(),
            )
            assert conn.execute(
                "SELECT gauntlet_enforced FROM tasks WHERE id=?", (forced_on,)
            ).fetchone()["gauntlet_enforced"] == 1
            assert conn.execute(
                "SELECT gauntlet_enforced FROM tasks WHERE id=?", (forced_off,)
            ).fetchone()["gauntlet_enforced"] == 0
