"""Every writer of ``tasks.assignee`` applies the same existence gate (D8).

``assign_task`` has refused undispatchable assignees since t_500a3503. The two
other paths that write the column did not, and that asymmetry is the defect:

    hermes kanban assign <id> w   -> ValueError
    hermes kanban add --assignee w -> created a row nothing can ever spawn

Five distinct non-existent assignees reached the live board across 8 rows
through the unguarded paths (``w`` x3, ``reviewer`` x1, ``erika_lead`` x1,
``paris`` x1 — defect D8, evidence packet for t_db0af7e0). The failure is
silent by construction: the dispatcher resolves the profile at spawn time,
counts the row as ``skipped_nonspawnable``, and the card sits in ``ready`` or
``review`` forever with no exception and no operator signal (t_820fca96:
claimed -> gave_up "worker crashed"/spawn_failed -> promoted, on repeat).

These tests pin the gate at each write, and — just as importantly — pin what
must still be ACCEPTED: ``None`` (a deliberately unassigned triage card) and
the two legacy shorthand tokens, which are lanes rather than profiles and are
translated into (assignee="default", executor_lane=...) before the gate sees
them.

Note that ``kanban_home`` repoints ``HERMES_HOME`` at an empty tmp dir, so
``profile_exists`` is False for every name except the special ``default``.
That makes these assertions independent of whichever profiles happen to exist
on the machine running the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB and no named profiles."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class TestCreateTaskAssigneeGate:
    """``create_task`` refuses what ``assign_task`` has always refused."""

    def test_rejects_nonexistent_profile(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(ValueError, match="w"):
                kb.create_task(conn, title="phantom", assignee="w")
            # Nothing was written: the refusal happens before the insert.
            assert kb.list_tasks(conn, include_archived=True) == []

    def test_rejects_executor_lane_identifier(self, kanban_home):
        """An executor lane belongs in ``executor_lane``, never ``assignee``."""
        with kb.connect_closing() as conn:
            with pytest.raises(ValueError, match="codex_verify"):
                kb.create_task(conn, title="lane token", assignee="codex_verify")

    def test_accepts_real_profile(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="real", assignee="default")
            assert kb.get_task(conn, tid).assignee == "default"

    def test_accepts_unassigned_card(self, kanban_home):
        """Triage cards are created with no assignee — still legal."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="triage", assignee=None)
            assert kb.get_task(conn, tid).assignee is None

    @pytest.mark.parametrize(
        "shorthand,lane",
        [("claude", kb.EXECUTOR_LANE_CLAUDE),
         ("atlas", kb.EXECUTOR_LANE_CODEX_VERIFY)],
    )
    def test_accepts_legacy_shorthand_and_still_normalizes(
        self, kanban_home, shorthand, lane
    ):
        """The gate must not break the two documented shorthand tokens.

        They are lanes, not profiles: ``profile_exists('atlas')`` is False and
        always was. They are translated to the spawnable carrier profile before
        the gate runs, so this asserts both halves at once.

        The ``atlas`` case carries a subject parent because that shorthand
        resolves to ``codex_verify``, and a verifier card with no subject is
        refused by the mandatory-linkage guard (``VerifierLinkageError``).
        Refusing an orphan verifier is criterion (A) — its verdict could be
        returned to nobody — so the fixture supplies the subject rather than the
        assignee gate being relaxed to admit one.
        """
        with kb.connect_closing() as conn:
            parents: tuple[str, ...] = ()
            if lane == kb.EXECUTOR_LANE_CODEX_VERIFY:
                parents = (kb.create_task(conn, title="subject under review"),)
            tid = kb.create_task(
                conn, title="shorthand", assignee=shorthand, parents=parents,
            )
            task = kb.get_task(conn, tid)
            assert task.assignee == "default"
            assert task.executor_lane == lane

    def test_gate_honours_a_patched_profile_registry(
        self, kanban_home, all_assignees_spawnable
    ):
        """The check is late-bound, so the dispatcher-test fixture works.

        Without this, every existing test that creates cards for synthetic
        assignees would have to be rewritten instead of declaring the fixture
        that already exists for exactly this purpose.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="synthetic", assignee="alice")
            assert kb.get_task(conn, tid).assignee == "alice"


class TestRequestReviewReviewerGate:
    """``request_review`` writes ``reviewer`` straight into ``assignee``."""

    def _running(self, conn, **kw):
        tid = kb.create_task(conn, title="impl", assignee="default", **kw)
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None and claimed.status == "running"
        return tid, claimed.current_run_id

    def test_refuses_phantom_reviewer_and_leaves_the_card_alone(
        self, kanban_home
    ):
        """t_f853abd8's exact shape: reviewer='reviewer', a string nobody is.

        The refusal is a transition failure rather than an exception because
        that is ``request_review``'s contract — and because a caller handed a
        reason can retry with a real reviewer, whereas the phantom write
        succeeded loudly and then failed silently forever.
        """
        with kb.connect_closing() as conn:
            tid, run_id = self._running(conn)
            ok, reason = kb.request_review(
                conn, tid, summary="ruling made", reviewer="reviewer",
                expected_run_id=run_id, with_reason=True,
            )
            assert ok is False
            assert "reviewer" in reason
            task = kb.get_task(conn, tid)
            # No half-transition: still the implementer's, still running.
            assert task.assignee == "default"
            assert task.status == "running"

    def test_refuses_executor_lane_identifier_as_reviewer(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = self._running(conn)
            ok, reason = kb.request_review(
                conn, tid, summary="impl", reviewer="codex_verify",
                expected_run_id=run_id, with_reason=True,
            )
            assert ok is False
            assert "codex_verify" in reason

    def test_accepts_real_reviewer(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, run_id = self._running(conn)
            assert kb.request_review(
                conn, tid, summary="impl", reviewer="default",
                expected_run_id=run_id,
            ) is True
            assert kb.get_task(conn, tid).status == "review"

    def test_accepts_handoff_with_no_reviewer(self, kanban_home):
        """Omitting ``reviewer`` keeps the current assignee — unchanged path."""
        with kb.connect_closing() as conn:
            tid, run_id = self._running(conn)
            assert kb.request_review(
                conn, tid, summary="impl", expected_run_id=run_id,
            ) is True
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.assignee == "default"
