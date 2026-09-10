"""D5: shorthand-lane normalization is a property of ASSIGNMENT, not of status.

``assignee='atlas'`` and ``assignee='claude'`` name executor LANES, not Hermes
profiles. ``~/.hermes/profiles/atlas/`` does not exist and
``profile_exists('atlas')`` is False, so a row that keeps the raw token is
unclaimable in every lane: both dispatch loops resolve the assignee through
``profile_exists`` and bucket it as ``skipped_nonspawnable``.

The translation used to live in exactly two places — ``create_task`` and the
ready-queue dispatch loop, the latter guarded by ``status = 'ready'``. So a
task moved onto a shorthand while in any OTHER status never normalized, and
stalled permanently with no exception and no dispatcher signal.

Live casualties at evidence-collection time (defect D5, evidence packet for
t_db0af7e0, independently verified PASS by t_fb23ac0a):

  * ``t_6b7d5845`` — 'review', assignee='atlas', stalled 35.7h, 8 consecutive
    ``gauntlet_stale`` events.
  * ``t_06e046f1`` — 'review', assignee='atlas', stalled 16.8h, 4 events.
    It reached the dead end while doing the RIGHT thing: routing to an
    independent verifier to escape an Erika-reviews-Erika loop. The mechanism
    swallowed the exact manoeuvre that breaks a self-review deadlock.

Neither casualty went through ``assign_task``; both were written by
``request_review(reviewer='atlas')``, which puts an arbitrary reviewer string
straight into ``assignee`` on a task entering 'review' — the one status the
lazy ready-queue fixup can never see. Both writers are pinned below.
"""

from __future__ import annotations

import json
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


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (tid, kind),
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def _park_in_review(conn, *, title="implementation"):
    """Drive a task to status 'review' the way a real implementer does."""
    tid = kb.create_task(conn, title=title, assignee="default")
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    ok, detail = kb.request_review(
        conn, tid, summary="implementation done", force=True, with_reason=True,
    )
    assert ok is True, detail
    assert kb.get_task(conn, tid).status == "review"
    return tid


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

class TestNormalizeShorthandLane:
    @pytest.mark.parametrize(
        "assignee,lane,expected",
        [
            ("atlas", None, ("default", kb.EXECUTOR_LANE_CODEX_VERIFY, "atlas")),
            ("claude", None, ("default", kb.EXECUTOR_LANE_CLAUDE, "claude")),
            # Not a shorthand: passes through completely untouched.
            ("erika", None, ("erika", None, None)),
            (None, None, (None, None, None)),
            # Already normalized — nothing to translate, so no event should be
            # emitted by callers that key on ``from_token``.
            ("default", kb.EXECUTOR_LANE_CODEX_VERIFY,
             ("default", kb.EXECUTOR_LANE_CODEX_VERIFY, None)),
            # A real profile on a carrier lane still gets moved onto the
            # carrier: these two lanes only ride 'default'.
            ("erika", kb.EXECUTOR_LANE_CLAUDE,
             ("default", kb.EXECUTOR_LANE_CLAUDE, "erika")),
        ],
    )
    def test_translation_table(self, assignee, lane, expected):
        assert kb._normalize_shorthand_lane(assignee, lane) == expected

    def test_claude_recovery_lane_is_not_a_carrier_lane(self):
        """``claude_recovery`` runs as itself and is not folded onto 'default'.

        Pinned because the carrier set is deliberately the two lanes that ride
        cli.py's pre-agent bypass, not "every non-null lane".
        """
        assert kb._normalize_shorthand_lane(
            "erika", kb.EXECUTOR_LANE_CLAUDE_RECOVERY
        ) == ("erika", kb.EXECUTOR_LANE_CLAUDE_RECOVERY, None)


# ---------------------------------------------------------------------------
# assign_task — the named gap: normalization ran at create/ready only
# ---------------------------------------------------------------------------

class TestAssignTaskNormalizes:
    @pytest.mark.parametrize(
        "token,lane",
        [("atlas", kb.EXECUTOR_LANE_CODEX_VERIFY),
         ("claude", kb.EXECUTOR_LANE_CLAUDE)],
    )
    @pytest.mark.parametrize("status", ["todo", "ready", "review", "blocked"])
    def test_normalizes_in_every_status(self, kanban_home, token, lane, status):
        """The regression, stated directly: status must not gate translation."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="implementation", assignee="default")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", (status, tid)
                )

            assert kb.assign_task(conn, tid, token) is True

            task = kb.get_task(conn, tid)
            assert task.assignee == "default"
            assert task.executor_lane == lane
            # Status is not a side-effect of assignment.
            assert task.status == status

    def test_emits_the_same_audit_event_the_dispatcher_emits(self, kanban_home):
        """A translation that leaves no event is one the board cannot audit."""
        with kb.connect_closing() as conn:
            tid = _park_in_review(conn)
            assert kb.assign_task(conn, tid, "atlas") is True

            payloads = _events(conn, tid, "executor_lane_normalized")
            assert len(payloads) == 1
            assert payloads[0]["from_assignee"] == "atlas"
            assert payloads[0]["assignee"] == "default"
            assert payloads[0]["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert payloads[0]["source"] == "assign_task"
            assert payloads[0]["status"] == "review"

            # The ordinary 'assigned' event still fires, and records what the
            # caller actually asked for alongside what landed.
            assigned = _events(conn, tid, "assigned")
            assert assigned[-1]["assignee"] == "default"
            assert assigned[-1]["requested"] == "atlas"
            assert assigned[-1]["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY

    def test_non_shorthand_assignment_leaves_lane_untouched(self, kanban_home):
        """Only a shorthand token writes executor_lane. Nothing else does."""
        with kb.connect_closing() as conn:
            # The subject parent is mandatory: "atlas" resolves to
            # ``codex_verify``, and a verifier card with no subject is refused
            # by the mandatory-linkage guard (criterion A) because its verdict
            # could be returned to nobody. Lane normalization is what is under
            # test here, so the fixture satisfies the linkage rule rather than
            # the rule being relaxed to admit an orphan verifier.
            subject = kb.create_task(conn, title="subject under review")
            tid = kb.create_task(
                conn, title="verify", assignee="atlas", parents=(subject,),
            )
            assert kb.get_task(conn, tid).executor_lane == (
                kb.EXECUTOR_LANE_CODEX_VERIFY
            )
            # Reassigning to a real profile must not silently clear the lane —
            # clearing it is a separate decision with its own semantics, and
            # inventing one here would change dispatch for existing rows.
            assert kb.assign_task(conn, tid, "default") is True
            task = kb.get_task(conn, tid)
            assert task.assignee == "default"
            assert task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert _events(conn, tid, "executor_lane_normalized") == []

    def test_lane_change_on_same_carrier_resets_the_failure_streak(
        self, kanban_home
    ):
        """assignee='default' -> assignee='default' + a NEW lane is a real
        reassignment: the incoming executor must not inherit the outgoing
        one's ``consecutive_failures``. Comparing the profile string alone
        would miss it, because the carrier name is unchanged.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="implementation", assignee="default")
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = 2, "
                    "last_failure_error = 'boom' WHERE id = ?",
                    (tid,),
                )
            assert kb.assign_task(conn, tid, "atlas") is True
            row = conn.execute(
                "SELECT assignee, executor_lane, consecutive_failures, "
                "last_failure_error FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["assignee"] == "default"
            assert row["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert row["consecutive_failures"] == 0
            assert row["last_failure_error"] is None

    def test_reassign_task_inherits_the_normalization(self, kanban_home):
        """``reassign_task`` delegates to ``assign_task`` — pin that it still
        does, so the recovery path can't drift back to writing a raw token."""
        with kb.connect_closing() as conn:
            tid = _park_in_review(conn)
            assert kb.reassign_task(conn, tid, "atlas") is True
            task = kb.get_task(conn, tid)
            assert (task.assignee, task.executor_lane) == (
                "default", kb.EXECUTOR_LANE_CODEX_VERIFY,
            )


# ---------------------------------------------------------------------------
# request_review(reviewer=...) — the writer that produced both casualties
# ---------------------------------------------------------------------------

class TestRequestReviewNormalizes:
    def test_reviewer_shorthand_normalizes_on_handoff(self, kanban_home):
        """The exact live scenario: hand off to an independent Codex verifier.

        Reproduces t_6b7d5845 / t_06e046f1. Before the fix this parked the card
        on assignee='atlas' in 'review' — nonspawnable in both dispatch loops,
        with nothing on the timeline to say so.
        """
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="implementation", assignee="default")
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None

            ok, detail = kb.request_review(
                conn, tid, summary="ready for independent verification",
                reviewer="atlas", force=True, with_reason=True,
            )
            assert ok is True, detail

            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.assignee == "default"
            assert task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY

            payloads = _events(conn, tid, "executor_lane_normalized")
            assert len(payloads) == 1
            assert payloads[0]["from_assignee"] == "atlas"
            assert payloads[0]["source"] == "request_review"

    def test_reviewer_omitted_leaves_assignee_and_lane_alone(self, kanban_home):
        """No reviewer => no assignee write => no lane write. Unchanged path."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="implementation", assignee="default")
            assert kb.claim_task(conn, tid) is not None
            ok, detail = kb.request_review(
                conn, tid, summary="done", force=True, with_reason=True,
            )
            assert ok is True, detail
            task = kb.get_task(conn, tid)
            assert task.assignee == "default"
            assert task.executor_lane is None

    def test_real_reviewer_profile_is_not_given_a_lane(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="implementation", assignee="default")
            assert kb.claim_task(conn, tid) is not None
            ok, detail = kb.request_review(
                conn, tid, summary="done", reviewer="default",
                force=True, with_reason=True,
            )
            assert ok is True, detail
            task = kb.get_task(conn, tid)
            assert task.assignee == "default"
            assert task.executor_lane is None
            assert _events(conn, tid, "executor_lane_normalized") == []


# ---------------------------------------------------------------------------
# The point of the whole thing: a normalized review card is dispatchable
# ---------------------------------------------------------------------------

class TestNormalizedReviewCardIsSpawnable:
    def test_raw_atlas_token_is_nonspawnable_but_normalized_row_is_not(
        self, kanban_home
    ):
        """Both halves in one test, because the delta IS the defect.

        A row carrying the raw token fails ``profile_exists`` and is bucketed
        ``skipped_nonspawnable``; the same row after normalization rides the
        real 'default' profile and is claimable. ``has_spawnable_review``
        is the same ``profile_exists`` resolution the review dispatch loop
        performs, so it reads the difference the dispatcher would.
        """
        with kb.connect_closing() as conn:
            stuck = _park_in_review(conn, title="stuck the old way")
            # Simulate a pre-fix row: written by a legacy surface, direct SQL.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = 'atlas', executor_lane = NULL "
                    "WHERE id = ?",
                    (stuck,),
                )
            assert kb.has_spawnable_review(conn) is False

            # The repaired write path, same task, same status.
            assert kb.assign_task(conn, stuck, "atlas") is True
            assert kb.has_spawnable_review(conn) is True
            assert kb.claim_review_task(conn, stuck) is not None


# ---------------------------------------------------------------------------
# Self-review guard: normalization must not weaken it
# ---------------------------------------------------------------------------

class TestSelfReviewGuardStillHolds:
    def test_normalization_does_not_bypass_the_self_review_refusal(
        self, kanban_home
    ):
        """Routing to 'atlas' must not launder an identity past the guard.

        The carrier profile is 'default', so an identity already refused as a
        self-reviewer this phase is still refused after normalization —
        ``claim_review_task`` reads ``assignee``, and the constraint on this
        repair was explicitly not to weaken that. The refusal is now VISIBLE
        (a ``review_claim_rejected_self_review`` event) instead of the silent
        nonspawnable stall the raw token produced.
        """
        with kb.connect_closing() as conn:
            tid = _park_in_review(conn)
            # Put a refusal for 'default' on record for this phase, the way
            # record_verification's guard does.
            with kb.write_txn(conn):
                kb._append_event(
                    conn, tid, "verification_blocked_self_review",
                    {"verifier": "default", "implementer": "default",
                     "source": "test"},
                )
            assert "default" in kb._self_review_refused_implementers(conn, tid)

            assert kb.assign_task(conn, tid, "atlas") is True
            task = kb.get_task(conn, tid)
            assert task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY

            # Guard still fires: the claim is refused, not silently granted.
            assert kb.claim_review_task(conn, tid) is None
            assert _events(conn, tid, "review_claim_rejected_self_review")


class TestGauntletAtlasRoutingRegression:
    def test_gauntlet_reviewer_atlas_preserves_subject_and_opens_child(self, kanban_home):
        """t_a0e4c47b regression: Atlas is a child lane, never the subject."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="gauntlet subject", assignee="default", gauntlet=True)
            claimed = kb.claim_task(conn, tid)
            assert claimed is not None
            kb.add_attachment(
                conn, tid, filename="evidence.md",
                stored_path=f"/tmp/{tid}/evidence.md", size=100, uploaded_by="test",
            )
            ok, detail = kb.request_review(
                conn, tid, summary="ready", reviewer="atlas",
                expected_run_id=claimed.current_run_id, with_reason=True,
            )
            assert ok is True, detail
            subject = kb.get_task(conn, tid)
            assert subject.status == "review"
            assert subject.verification_state == kb.VERIFICATION_PENDING
            assert subject.executor_lane is None
            rows = conn.execute(
                "SELECT c.id,c.executor_lane FROM task_links l JOIN tasks c ON c.id=l.child_id "
                "WHERE l.parent_id=? AND c.executor_lane=?",
                (tid, kb.EXECUTOR_LANE_CODEX_VERIFY),
            ).fetchall()
            assert len(rows) == 1, [tuple(r) for r in conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
            assert _events(conn, tid, "independent_verifier_lane_requested")

    def test_orphan_codex_lane_does_not_bypass_gauntlet_completion(self, kanban_home):
        """Defense in depth: an unlinked verifier-labelled subject remains gated."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="subject", assignee="default", gauntlet=True)
            assert kb.assign_task(conn, tid, "atlas") is True
            task = kb.get_task(conn, tid)
            assert task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert kb.gauntlet_required(conn, tid) is True
            try:
                kb.complete_task(conn, tid, result="VERDICT: FAIL\nATLAS_VERDICT: FAIL")
            except kb.VerificationRequiredError:
                pass
            else:
                raise AssertionError("unlinked codex_verify-labelled Gauntlet subject completed without verification")
            task = kb.get_task(conn, tid)
            assert task.status != "done"
            assert task.terminal_disposition is None

    def test_orphan_codex_lane_is_gated_even_without_gauntlet_flag(self, kanban_home):
        """Verifier-artifact exemption requires an actual subject link."""
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="orphan verifier", assignee="default")
            assert kb.assign_task(conn, tid, "atlas") is True
            task = kb.get_task(conn, tid)
            assert task.executor_lane == kb.EXECUTOR_LANE_CODEX_VERIFY
            assert kb.gauntlet_required(conn, tid) is True
            try:
                kb.complete_task(conn, tid, result="VERDICT: FAIL\\nATLAS_VERDICT: FAIL")
            except kb.VerificationRequiredError:
                pass
            else:
                raise AssertionError("orphan codex_verify card bypassed completion gate")
            assert kb.get_task(conn, tid).status != "done"
