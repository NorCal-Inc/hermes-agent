"""``swap_verifier_lane_once`` must be a one-shot per SUBJECT, not per card.

Independent verifier t_9c691a23 (2026-10-25, t_ab80ef2a) returned FAIL on this
exact gap: the durable ``verifier_lane_swapped`` event used to be written only
on the verifier card that just failed. The brand-new sibling it creates has no
such event, so if THAT sibling later also hits a capacity failure, nothing
stops a second swap -- Claude and Codex could alternate back and forth on the
same subject indefinitely instead of falling back exactly once.

The fix scopes the "already swapped" check to the subject's fallback chain as
a whole, keyed off the durable ``verifier_lane_swap_dispatched`` event that
:func:`swap_verifier_lane_once` writes on the SUBJECT (not the verifier card)
every time it actually dispatches a sibling. That event is written once, on
the subject, the first time any card in the chain swaps -- so a second
capacity failure anywhere in the chain (the original card retried, or the new
sibling it swapped to) finds that event already present and declines.
"""

from __future__ import annotations

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
    kb.init_db()
    return home


@pytest.fixture
def registry(monkeypatch):
    """Control which names count as existing Hermes profiles.

    Mirrors the fixture of the same name in test_kanban_verifier_identity.py:
    patches the module attribute (not the imported name) so
    ``_assert_assignee_dispatchable``'s call-time import stays patchable.
    """
    from hermes_cli import profiles

    known = {"default", "alice", "bob"}

    def _set(*names):
        known.clear()
        known.update(names)

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in known)
    return _set


def _parked_subject(conn, *, implementer="alice"):
    """A gauntlet implementation task parked for independent verification.

    ``_ensure_independent_verifier_child`` checks for falsifiable evidence
    FIRST and refuses to create a child otherwise (see its docstring, the
    t_cbc133db un-dispatchable-verifier regression) -- so the fixture must
    attach evidence the same way ``_attested_verifier`` does in
    test_kanban_verifier_identity.py.
    """
    tid = kb.create_task(
        conn, title="implementation", assignee=implementer, gauntlet=True,
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.request_review(
        conn, tid, summary="implemented X",
        expected_run_id=claimed.current_run_id,
    ) is True
    kb.add_attachment(
        conn, tid, filename="EVIDENCE.md",
        stored_path=f"/tmp/{tid}/EVIDENCE.md", size=64,
        uploaded_by=implementer,
    )
    return tid


def _open_verifier_for(conn, subject_id):
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status NOT IN ('done', 'archived') "
        "AND executor_lane IN (?, ?) ORDER BY created_at DESC",
        (kb.EXECUTOR_LANE_CLAUDE_VERIFY, kb.EXECUTOR_LANE_CODEX_VERIFY),
    ).fetchall()
    for r in rows:
        if subject_id in kb.verifier_subject_ids(conn, r["id"]):
            return r["id"]
    return None


class TestVerifierLaneSwapIsOncePerSubjectChain:
    def test_default_claude_child_is_created(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            assert verifier is not None
            task = kb.get_task(conn, verifier)
            assert task.executor_lane == kb.EXECUTOR_LANE_CLAUDE_VERIFY

    def test_first_capacity_failure_swaps_lane(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            original = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            assert kb.get_task(conn, original).executor_lane == (
                kb.EXECUTOR_LANE_CLAUDE_VERIFY
            )

            sibling = kb.swap_verifier_lane_once(
                conn, original, reason="claude usage limit exceeded",
            )

            assert sibling is not None
            assert sibling != original
            assert kb.get_task(conn, sibling).executor_lane == (
                kb.EXECUTOR_LANE_CODEX_VERIFY
            )
            # The original is retired (blocked), not left open to double-fire.
            assert kb.get_task(conn, original).status == "blocked"
            # The dispatch record lives on the SUBJECT, where the chain-wide
            # guard reads it from.
            dispatched = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? "
                "AND kind = 'verifier_lane_swap_dispatched'",
                (subject,),
            ).fetchone()
            assert dispatched is not None

    def test_second_capacity_failure_on_the_new_sibling_does_not_swap_back(
        self, kanban_home, registry,
    ):
        """The exact gap the independent verifier found (t_9c691a23 FAIL).

        A capacity failure on the SIBLING that the first swap created must
        not be allowed to swap a second time -- that would let Claude and
        Codex alternate on the same subject forever instead of falling back
        exactly once.
        """
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            original = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            sibling = kb.swap_verifier_lane_once(
                conn, original, reason="claude usage limit exceeded",
            )
            assert sibling is not None

            # The sibling itself now hits a capacity failure.
            second_swap = kb.swap_verifier_lane_once(
                conn, sibling, reason="codex usage limit exceeded",
            )

            assert second_swap is None
            # The sibling is left open/untouched -- declining to swap again
            # must not also silently block the only remaining open verifier.
            assert kb.get_task(conn, sibling).status not in (
                "blocked", "done", "archived",
            )

    def test_second_capacity_failure_on_the_original_card_does_not_swap_again(
        self, kanban_home, registry,
    ):
        """A retried capacity failure on the ORIGINAL card, after it already
        swapped once, must also decline -- not just the sibling path."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            original = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            first = kb.swap_verifier_lane_once(
                conn, original, reason="claude usage limit exceeded",
            )
            assert first is not None

            # Something re-reports a capacity failure against the ORIGINAL
            # (already-blocked) card id -- must still decline deterministically.
            again = kb.swap_verifier_lane_once(
                conn, original, reason="claude usage limit exceeded (retry)",
            )
            assert again is None

    def test_swap_reverses_direction_codex_to_claude(self, kanban_home, registry):
        """The same one-shot rule applies in the other direction too."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            codex_verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
                preferred_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
            )
            assert kb.get_task(conn, codex_verifier).executor_lane == (
                kb.EXECUTOR_LANE_CODEX_VERIFY
            )

            sibling = kb.swap_verifier_lane_once(
                conn, codex_verifier, reason="codex rate limit exceeded",
            )
            assert sibling is not None
            assert kb.get_task(conn, sibling).executor_lane == (
                kb.EXECUTOR_LANE_CLAUDE_VERIFY
            )

            second = kb.swap_verifier_lane_once(
                conn, sibling, reason="claude usage limit exceeded",
            )
            assert second is None
