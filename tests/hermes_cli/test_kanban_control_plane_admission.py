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
    """#2 -- govern deliverables, not investigations. Classified on MUTATION.

    The topic-vs-mutation distinction is the load-bearing one. A topic list is
    gameable: an agent avoiding verification labels its card an
    "investigation". Keying on what the work DOES closes that.
    """

    @pytest.mark.parametrize("title", [
        "Deploy the Orion pricing page to production",
        "Rotate the Stripe billing credential",
        "Schema migration for the customer table",
        "Update the refund policy change for LCASS",
        "Cross-entity governance change for company routing",
        "Ship the customer-facing onboarding deliverable",
    ])
    def test_mutating_work_is_governed(self, title):
        assert kb.gauntlet_default_for_subject(title) is True

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

    def test_mutation_beats_investigation_when_both_present(self):
        """A root-cause discovery that patches production IS a production change."""
        assert kb.gauntlet_default_for_subject(
            "Investigate the billing bug and deploy the fix to production"
        ) is True

    def test_undecidable_subject_falls_through_to_board_config(self, monkeypatch):
        assert kb.gauntlet_default_for_subject("Tidy the notes") is None
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: True)
        assert kb._resolve_gauntlet_default("Tidy the notes") is True
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        assert kb._resolve_gauntlet_default("Tidy the notes") is False

    def test_classification_overrides_a_permissive_board_default(
        self, kanban_home, monkeypatch
    ):
        """Board says off; a deployment is still governed."""
        monkeypatch.setattr(kb, "gauntlet_enforcement_default", lambda: False)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Deploy the Orion pricing page to production",
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
