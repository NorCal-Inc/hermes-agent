"""Explicit in-body verification requirements are enforced, not advisory.

t_eed363cc (2026-09-16): a control-plane change whose own closing comment said
"the card still needs an independent codex_verify PASS before it closes" was
marked done on self-report. gauntlet_enforced was 0, the board default was off,
and the subject classifier has no notion of an in-body requirement.

Pinned here:
(a) the canonical phrase in a body stamps gauntlet_enforced at creation, even
    when neither subject regex matches (or the investigation regex says no);
(b) a completion whose own summary admits verification is still owed is
    refused and lands in review, with gauntlet_enforced unset beforehand;
(c) keyword-classified and ordinary tasks are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane


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
    kb.init_db()
    return home


def _enforced(conn, tid) -> bool:
    return bool(conn.execute(
        "SELECT gauntlet_enforced FROM tasks WHERE id = ?", (tid,)
    ).fetchone()["gauntlet_enforced"])


def _event_kinds(conn, tid):
    return [r["kind"] for r in conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
    )]


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrase", kb.CANONICAL_VERIFICATION_REQUIREMENT_PHRASES)
def test_every_canonical_phrase_is_recognised(phrase):
    assert kb.explicit_verification_requirement(phrase)


@pytest.mark.parametrize("text", [
    "Independent codex_verify PASS required before this closes (yes, "
    "genuinely enforced this time)",
    "the card still needs an independent codex_verify PASS before it closes.",
    "Awaiting independent verification.",
    "Not yet independently verified.",
    "Must not close without independent verification",
    "This requires an independent verdict before completion.",
])
def test_requirement_and_admission_wordings_match(text):
    assert kb.explicit_verification_requirement(text)


@pytest.mark.parametrize("text", [
    "",
    None,
    "Fix the typo in the README.",
    "No independent verification required; docs-only change.",
    "Independent review is not required for this card.",
    "This does not require an independent verdict.",
    "Independent codex_verify PASS was already recorded (t_9046e4a5).",
    "Run the unit tests and report.",
])
def test_non_requirements_do_not_match(text):
    assert not kb.explicit_verification_requirement(text)


def test_negation_in_one_clause_does_not_cancel_another():
    assert kb.explicit_verification_requirement(
        "Lint fixes: no review needed. Independent verification required "
        "before closing for the kernel change."
    )


# ---------------------------------------------------------------------------
# (a) creation
# ---------------------------------------------------------------------------

def test_canonical_body_phrase_enforces_at_creation(kanban_home):
    body = (
        "Adjust verifier-child reuse. Root cause analysis attached.\n"
        "Independent verification required before closing."
    )
    # Neither subject regex decides True here; the investigation regex even
    # says False ("root cause", "analysis").
    assert kb.gauntlet_default_for_subject("Adjust reuse logic", body) is False
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="Adjust reuse logic", body=body, assignee="default",
        )
        assert _enforced(conn, tid)
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
            (tid,),
        ).fetchone()["payload"])
        assert payload["gauntlet_source"] == "explicit_body_requirement"


def test_body_requirement_beats_explicit_gauntlet_false(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="small change", assignee="default", gauntlet=False,
            body="Gauntlet PASS required before closing.",
        )
        assert _enforced(conn, tid)


# ---------------------------------------------------------------------------
# (b) completion
# ---------------------------------------------------------------------------

_ADMISSION = (
    "Implemented the reuse fix and ran the suite. The card still needs an "
    "independent codex_verify PASS before it closes."
)


def test_self_reported_unverified_completion_is_refused(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="default")
        assert not _enforced(conn, tid)
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(kb.VerificationRequiredError):
            kb.complete_task(
                conn, tid, summary=_ADMISSION, result=_ADMISSION,
                expected_run_id=claimed.current_run_id,
            )
        task = kb.get_task(conn, tid)
        assert task.status == "running"
        # Escalated, so a retry without the admission is refused too.
        assert _enforced(conn, tid)
        assert "completion_blocked_self_reported_unverified" in _event_kinds(conn, tid)
        with pytest.raises(kb.VerificationRequiredError):
            kb.complete_task(
                conn, tid, summary="done", expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, tid).status != "done"


def test_admission_only_in_result_with_blank_summary_is_refused(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="default")
        claimed = kb.claim_task(conn, tid)
        with pytest.raises(kb.VerificationRequiredError):
            kb.complete_task(
                conn, tid, summary="   ", result=_ADMISSION,
                expected_run_id=claimed.current_run_id,
            )
        assert kb.get_task(conn, tid).status != "done"


def test_direct_claude_lane_routes_admission_to_review(kanban_home, monkeypatch):
    """End to end through the executor wrapper that closed t_eed363cc."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(
            conn, title="reuse logic", assignee="default",
            body="Change the reuse logic.", executor_lane=kb.EXECUTOR_LANE_CLAUDE,
        )
        root = kb.resolve_workspace(kb.get_task(conn, tid))
        kb.set_workspace_path(conn, tid, str(root))
        assert kb.claim_task(conn, tid)
        assert not _enforced(conn, tid)

    def fake_invoke(prompt, cwd, timeout, *, task_id=None):
        (root / "evidence.md").write_text("pytest: 12 passed\n")
        return recovery_lane.AttemptResult(
            "claude", 0, json.dumps({"result": _ADMISSION}), "",
            execution_id="x_fake_claude",
            execution_status=recovery_lane.ex.STATUS_COMPLETED,
        )

    monkeypatch.setattr(recovery_lane, "_claim_direct_claude_attempt", lambda c, t, r: True)
    monkeypatch.setattr(recovery_lane, "_invoke_claude", fake_invoke)
    assert recovery_lane.run_claude_executor(tid) == 0

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review"
        assert task.status != "done"
        assert _enforced(conn, tid)
        assert "completion_blocked_self_reported_unverified" in _event_kinds(conn, tid)


def test_verified_task_may_mention_the_requirement(kanban_home):
    """A requirement already met is not an admission."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid,
            summary="Independent codex_verify PASS was already recorded.",
            expected_run_id=claimed.current_run_id,
        )
        assert kb.get_task(conn, tid).status == "done"


# ---------------------------------------------------------------------------
# (c) no regression
# ---------------------------------------------------------------------------

def test_keyword_classification_uses_risk_not_generic_mutation(kanban_home):
    with kb.connect_closing() as conn:
        deploy = kb.create_task(conn, title="deploy the API", assignee="default")
        credential = kb.create_task(
            conn, title="rotate the API credential", assignee="default",
        )
        investigate = kb.create_task(
            conn, title="investigate slow queries", assignee="default",
        )
        plain = kb.create_task(conn, title="rename a variable", assignee="default")
        assert not _enforced(conn, deploy)
        assert _enforced(conn, credential)
        assert not _enforced(conn, investigate)
        assert not _enforced(conn, plain)
        for tid in (deploy, investigate, plain):
            payload = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created'",
                (tid,),
            ).fetchone()["payload"])
            assert "gauntlet_source" not in payload


def test_ordinary_completion_unchanged(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="rename a variable", assignee="default")
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn, tid, summary="Renamed; tests pass.",
            expected_run_id=claimed.current_run_id,
        )
        assert kb.get_task(conn, tid).status == "done"
        assert not _enforced(conn, tid)
