"""Failure-fingerprint-aware attempt ceiling (learning-loop fix #4, t_c574be85).

The objective attempt ceiling counted capacity only, so "six runs still failing
the same way" and "six runs spent before corrected evidence existed" produced
the identical ATTEMPT_BUDGET_EXHAUSTED block. Live cases: ``t_318800fe`` was
granted 6 -> 9 -> 12 with nothing asking whether its failure had changed, and
``t_fe66575d`` (zero runs of its own) was blocked by a budget spent a week
earlier on a subject whose corrected evidence no verifier had yet judged.

These pin both halves: the ceiling still blocks every case exactly as before,
and the block reason now says which case the operator is looking at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb

OPERATOR = {kb.ENV_ACTOR_KIND: kb.ACTOR_KIND_HUMAN_INTERACTIVE, kb.ENV_ACTOR_ID: "christopher"}


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
    for var in (*kb._GOVERNED_RUN_ENV_MARKERS, "HERMES_SESSION_ID", "HERMES_PROFILE",
                "HERMES_PROFILE_NAME", kb.ENV_ACTOR_KIND, kb.ENV_ACTOR_ID):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


def _hand_off(conn, tid, summary="impl"):
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.request_review(
        conn, tid, summary=summary, reviewer="default",
        expected_run_id=claimed.current_run_id,
    ) is True


def _fail(conn, tid, reason):
    ok, detail = kb.record_verification(
        conn, tid, passed=False, verifier="reviewer", reason=reason,
    )
    assert ok is True, detail


def _spend_to_ceiling(conn, tid):
    used = kb.gauntlet_objective_attempts(conn, tid)
    with kb.write_txn(conn):
        for i in range(kb.GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT - used):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome) "
                "VALUES (?, 'default', 'timed_out', ?, ?, 'timed_out')",
                (tid, 100 + i, 101 + i),
            )
    assert kb.gauntlet_objective_attempts(conn, tid) == kb.GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT


def _ceiling_event(conn, tid):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'objective_attempt_ceiling_reached' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    return json.loads(row["payload"])


SAME = "No-change reconciliation produces false mutations in the skill ledger"


# (a) -------------------------------------------------------------------------
def test_identical_unresolved_failure_still_hits_the_ceiling(kanban_home):
    """The guard must not become toothless: a repeat stays blocked, labelled as a repeat."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="objective", assignee="default", gauntlet=True)
        _hand_off(conn, tid)
        _fail(conn, tid, SAME)
        _hand_off(conn, tid, summary="retry, same approach")
        _fail(conn, tid, SAME + " (run 2)")
        _spend_to_ceiling(conn, tid)

        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert task.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert task.last_failure_error.startswith("ATTEMPT_BUDGET_EXHAUSTED:")
        assert kb.FAILURE_BASIS_REPEATED in task.last_failure_error
        basis = _ceiling_event(conn, tid)["failure_basis"]
        assert basis["basis"] == kb.FAILURE_BASIS_REPEATED
        assert basis["failed_verdicts"] == 2 and basis["repeated"] == 1 and basis["open"] == 1

        # Still no way through: unblock re-blocks at the next claim.
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid) is None
        assert kb.get_task(conn, tid).block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED


# (b) -------------------------------------------------------------------------
def test_corrected_evidence_after_failure_is_reported_distinctly_but_still_blocked(kanban_home):
    """The t_fe66575d shape: budget spent, then corrected evidence handed off, unjudged."""
    with kb.connect_closing() as conn:
        repeat = kb.create_task(conn, title="repeat", assignee="default", gauntlet=True)
        _hand_off(conn, repeat)
        _fail(conn, repeat, SAME)
        _hand_off(conn, repeat)
        _fail(conn, repeat, SAME)
        _spend_to_ceiling(conn, repeat)
        assert kb.claim_task(conn, repeat) is None

        corrected = kb.create_task(conn, title="corrected", assignee="default", gauntlet=True)
        _hand_off(conn, corrected)
        _fail(conn, corrected, SAME)
        _hand_off(conn, corrected)
        _fail(conn, corrected, SAME)
        _hand_off(conn, corrected, summary="isolated synthetic-mutation run attached")
        _spend_to_ceiling(conn, corrected)
        assert kb.get_task(conn, corrected).status == "review"
        assert kb.claim_review_task(conn, corrected) is None

        a, b = kb.get_task(conn, repeat), kb.get_task(conn, corrected)
        # Same brake, same numbers ...
        assert a.status == b.status == "blocked"
        assert a.block_kind == b.block_kind == kb.BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED
        assert kb.gauntlet_objective_attempts(conn, repeat) == kb.gauntlet_objective_attempts(conn, corrected)
        # ... but no longer silently identical.
        assert kb.FAILURE_BASIS_EVIDENCE_UNVERIFIED in b.last_failure_error
        assert kb.FAILURE_BASIS_REPEATED not in b.last_failure_error
        assert "may grant on that basis" in b.last_failure_error
        basis = _ceiling_event(conn, corrected)["failure_basis"]
        assert basis["basis"] == kb.FAILURE_BASIS_EVIDENCE_UNVERIFIED
        assert basis["superseded"] == basis["failed_verdicts"] == 2
        assert basis["refailed"] == 1 and basis["evidence_pending"] == 1 and basis["open"] == 0
        # The repeat that preceded the correction is still disclosed, not hidden.
        assert basis["repeated"] == 1


def test_open_failure_elsewhere_in_lineage_outranks_corrected_evidence(kanban_home):
    """Conservative ordering: one unanswered failure keeps the lineage FAILURE_UNRESOLVED."""
    with kb.connect_closing() as conn:
        root = kb.create_task(conn, title="root", assignee="default", gauntlet=True)
        _hand_off(conn, root)
        _fail(conn, root, "Timer is not enabled")
        _hand_off(conn, root, summary="timer enabled")
        repair = kb.create_repair_task(
            conn, title="repair", subject_id=root, owner="erika",
            umbrella_id=kb.create_task(conn, title="umbrella", assignee="default"),
            assignee="default", gauntlet=True,
        )
        _hand_off(conn, repair)
        _fail(conn, repair, "Request-review path never mints the replacement verifier")
        basis = kb.objective_failure_basis(conn, repair)
        assert basis["basis"] == kb.FAILURE_BASIS_UNRESOLVED
        assert basis["evidence_pending"] == 1 and basis["open"] == 1


def test_no_failed_verdicts_is_its_own_basis(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="objective", assignee="default")
        _spend_to_ceiling(conn, tid)
        assert kb.claim_task(conn, tid) is None
        assert kb.FAILURE_BASIS_NONE in kb.get_task(conn, tid).last_failure_error


def test_resolved_failure_is_reported_as_resolved(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="objective", assignee="default", gauntlet=True)
        _hand_off(conn, tid)
        _fail(conn, tid, "Created-by spoof regression is missing")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_verifications (task_id, state, verifier, reason, created_at) "
                "VALUES (?, ?, 'reviewer', '', 1)", (tid, kb.VERIFICATION_VERIFIED),
            )
        assert kb.objective_failure_basis(conn, tid)["basis"] == kb.FAILURE_BASIS_RESOLVED


def test_grant_records_the_failure_basis_it_was_made_on(kanban_home):
    """A later grant can be read against whether anything changed since the last one."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="objective", assignee="default", gauntlet=True)
        _hand_off(conn, tid)
        _fail(conn, tid, SAME)
        _spend_to_ceiling(conn, tid)
        payload = kb.grant_objective_attempts(
            conn, tid, added_attempts=3, authorized_by="Christopher",
            reason="operator decision", env=OPERATOR,
        )
        assert payload["failure_basis"]["basis"] == kb.FAILURE_BASIS_UNRESOLVED
        assert kb.effective_objective_attempt_limit(conn, tid) == 9


def test_fail_time_candidate_lesson_carries_the_same_fingerprint(kanban_home):
    """Fix #2's candidate row snapshots the fingerprint the ceiling later derives."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="objective", assignee="default", gauntlet=True)
        _hand_off(conn, tid)
        _fail(conn, tid, SAME)
        (row,) = conn.execute(
            "SELECT evidence FROM task_lessons WHERE source_task_id = ?", (tid,)
        ).fetchall()
        prov = json.loads(row["evidence"])
        (entry,) = kb.objective_failure_ledger(conn, tid)
        assert prov["failure_fingerprint"] == entry["fingerprint"] != ""


def test_fingerprint_parses_failing_criteria_from_verifier_reports():
    """Shapes taken from the live t_69440ff2 verifier reports (t_fa3d6af3, t_8b66792e)."""
    first = kb.failure_fingerprint(
        "## Independent Verification\n\n"
        "- Central ledger source exists: PASS (`tools/skill_ledger.py`).\n"
        "- Timer enabled/active: BLOCKED; could not connect to the user bus.\n"
        "- Synthetic external mutation detection: FAIL — no falsifiable run output.\n"
        "- No-change reconciliation produces zero false mutations: FAIL — no output.\n\n"
        "ACCEPTANCE: FAIL\n\nVERDICT: FAIL\n"
    )
    second = kb.failure_fingerprint(
        "- Central ledger exists: **PASS**\n"
        "- **PASS/FAIL verdict routing after dependency unlink: PASS.**\n"
        "- Synthetic external-mutation detection: **FAIL** — only implementer-reported\n"
        "- No-change reconciliation yields zero false mutations: **FAIL** — same gap\n"
        "- Targeted regression: **INSUFFICIENT** — named suite does not test it\n"
        "VERDICT: FAIL\n"
    )
    assert first["source"] == second["source"] == "criteria"
    assert [c["label"] for c in first["criteria"]] == [
        "Timer enabled/active",
        "Synthetic external mutation detection",
        "No-change reconciliation produces zero false mutations",
    ]
    assert len(second["criteria"]) == 3        # PASS/FAIL routing line is a PASS
    assert first["fingerprint"] != second["fingerprint"]
    matched = [
        c["label"] for c in second["criteria"]
        if any(kb._criteria_match(c["terms"], p["terms"]) for p in first["criteria"])
    ]
    assert matched == [
        "Synthetic external-mutation detection",
        "No-change reconciliation yields zero false mutations",
    ]
    # Stable across ids/paths/counts.
    assert kb.failure_fingerprint("- Suite: FAIL 2 passed in t_aaaaaaaa")["fingerprint"] == \
        kb.failure_fingerprint("- Suite: FAIL 4 passed in t_bbbbbbbb")["fingerprint"]
