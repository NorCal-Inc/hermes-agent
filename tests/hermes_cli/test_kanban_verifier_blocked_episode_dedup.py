"""Regression cover for the SECOND duplicate-verifier-spawn code path
(2026-11-04, observed live on t_bd45babc and t_bb79d28c).

Distinct from ``test_kanban_verifier_dispatch_dedup_retry.py``'s fix (b)
(bounded retry inside ``run_codex_verifier``/``run_claude_verifier`` when the
verifier's OWN execution tool crashes mid-attempt). That fix only prevents
``block_task`` from firing on the first crash; it does nothing once a
verifier child has already reached a genuine terminal ``blocked`` state
(exhausted retry budget, a Codex plan-usage-limit wall that fails every
attempt near-instantly, ``INVALID_VERIFIER_LINKAGE``, or an operator hold).

Once a child IS blocked, some OTHER caller keeps re-entering
``_ensure_independent_verifier_child`` for the same subject on roughly a
70-90 second cadence (the periodic review/dispatch recheck, not a fresh
human ``request_review``) and, before this fix, minted a brand-new sibling
every single time instead of recognizing the existing blocked child as the
same, still-open review episode: 9 Codex verifier children in 9 minutes on
t_bd45babc, 25 (later 42+) on t_bb79d28c.

The fix is ``_blocked_verifier_in_current_episode`` (kanban_db.py), wired
into ``_ensure_independent_verifier_child`` right after the existing
``_verifier_respawn_unchanged`` guard: when the newest verifier child for the
subject is BLOCKED, on the SAME lane, and was created after the subject's
*latest* ``review_requested`` event (i.e. still inside the episode that
spawned it), no new child is minted -- the stuck one is surfaced via a single
``verifier_respawn_refused_blocked`` event instead of being multiplied.

This must NOT regress the pre-existing, deliberately-kept contract in
``test_kanban_self_review_recovery.py::TestVerifierChildReuseIntegrity``:
once a genuinely NEW review episode opens (``request_review``/
``reopen_review_task`` again), a blocked child from the OLD episode is
still correctly superseded by a fresh one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def registry(monkeypatch):
    from hermes_cli import profiles

    known = {"default", "alice", "bob"}

    def _set(*names):
        known.clear()
        known.update(names)

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in known)
    return _set


def _parked_subject(conn, *, implementer="alice"):
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


def _block_the_verifier(conn, verifier_id):
    """Drive a live verifier child straight to a terminal ``blocked`` state,
    the way an exhausted crash-retry budget (or a plan-usage-limit wall that
    fails every attempt near-instantly) would."""
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, verifier_id)
    assert claimed is not None and claimed.status == "running"
    ok = kb.block_task(
        conn, verifier_id, reason="Codex verifier failed or timed out.",
        kind="needs_input", expected_run_id=claimed.current_run_id,
    )
    assert ok is True
    assert kb.get_task(conn, verifier_id).status == "blocked"


class TestBlockedVerifierInSameEpisodeIsNotMultiplied:
    def test_single_recheck_after_block_does_not_mint_a_sibling(
        self, kanban_home, registry,
    ):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            assert verifier is not None
            _block_the_verifier(conn, verifier)

            again = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )

            assert again is None, (
                "a blocked verifier still inside its own review episode "
                "must be surfaced, not replaced"
            )
            verifier_children = [
                c for c in kb.child_ids(conn, subject)
                if kb.get_task(conn, c).executor_lane in kb.INDEPENDENT_VERIFIER_LANES
            ]
            assert verifier_children == [verifier]
            assert kb.get_task(conn, verifier).status == "blocked"

    def test_repeated_periodic_rechecks_mint_zero_additional_children(
        self, kanban_home, registry,
    ):
        """Reproduces the live incident shape directly: the same recheck
        call fired many times in a row (standing in for a dispatcher tick
        every ~70-90s) must never grow the sibling count."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            _block_the_verifier(conn, verifier)

            for _ in range(10):
                result = kb._ensure_independent_verifier_child(
                    conn, subject, implementer="alice",
                )
                assert result is None

            verifier_children = [
                c for c in kb.child_ids(conn, subject)
                if kb.get_task(conn, c).executor_lane in kb.INDEPENDENT_VERIFIER_LANES
            ]
            assert len(verifier_children) == 1, (
                f"expected exactly 1 verifier child after 10 rechecks, "
                f"got {len(verifier_children)}: {verifier_children}"
            )

    def test_refusal_event_is_recorded_once_not_once_per_recheck(
        self, kanban_home, registry,
    ):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            _block_the_verifier(conn, verifier)

            for _ in range(5):
                kb._ensure_independent_verifier_child(
                    conn, subject, implementer="alice",
                )

            refusals = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? "
                "AND kind = 'verifier_respawn_refused_blocked'",
                (subject,),
            ).fetchone()[0]
            assert refusals == 1, (
                "the refusal is durable evidence the stuck verifier was "
                "surfaced -- repeating it once per tick would just teach "
                "an operator to ignore the channel"
            )


class TestNewReviewEpisodeStillReplacesABlockedChild:
    """Must not regress TestVerifierChildReuseIntegrity's contract: a
    genuinely new episode still gets a fresh verifier."""

    def test_request_review_again_after_block_mints_a_fresh_child(
        self, kanban_home, registry,
    ):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            _block_the_verifier(conn, verifier)

            # Same-episode recheck: correctly refused.
            assert kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            ) is None

            # A genuinely new review handoff -- e.g. the implementer added a
            # comment and resubmitted -- opens a new episode.
            assert kb.reopen_review_task(conn, subject) is True
            claimed = kb.claim_task(conn, subject)
            assert claimed is not None
            assert kb.request_review(
                conn, subject, summary="addressed feedback",
                expected_run_id=claimed.current_run_id,
            ) is True

            fresh = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            assert fresh is not None and fresh != verifier, (
                "a new review episode must still get its own verifier even "
                "though the old one is blocked"
            )

            # The old blocked child is untouched history, not multiplied.
            assert kb.get_task(conn, verifier).status == "blocked"
            verifier_children = {
                c for c in kb.child_ids(conn, subject)
                if kb.get_task(conn, c).executor_lane in kb.INDEPENDENT_VERIFIER_LANES
            }
            assert verifier_children == {verifier, fresh}
