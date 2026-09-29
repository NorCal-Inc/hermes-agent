"""2026-09-29 verification-loop closure.

Two gaps kept VERIFICATION SYSTEM from passing the operational gauntlet:

* ``record_error_lesson`` (the error-keyed write half of retrieval-on-error)
  had no automatic caller, so 0 of 233 lessons carried a signature.
* ``unlink_tasks`` could delete the only route a hand-created verifier had to
  its subject, stranding its verdict (2026-09-15 x4, 2026-09-20 ``t_fd296c4a``).
"""

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


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


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else None
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
            (tid, kind),
        )
    ]


def _review(conn, tid):
    run = kb.claim_task(conn, tid)
    assert run is not None and run.status == "running"
    assert kb.request_review(
        conn, tid, summary="impl", reviewer="default",
        expected_run_id=run.current_run_id,
    ) is True


FAILED_REPORT = (
    "VERDICT: FAIL\n"
    "- **Antenna double-count in executive total:** **FAIL.** both paths summed\n"
    "- **Triplexer price freshness:** **FAIL.** snapshot labelled current\n"
    "- **Mast pricing absent:** PASS\n"
)


def _fail_then_pass(conn, *, tenant="acme", reason=FAILED_REPORT, evidence=None):
    tid = kb.create_task(conn, title="t", assignee="default", gauntlet=True, tenant=tenant)
    _review(conn, tid)
    ok, _ = kb.record_verification(
        conn, tid, passed=False, verifier="reviewer", reason=reason,
        route_on_failure=False,
    )
    assert ok is True
    ok, detail = kb.record_verification(
        conn, tid, passed=True, verifier="reviewer",
        reason="recomputed single-path total; live price cited",
        evidence=evidence or {"exit_code": 0},
        regression_evidence={"command": "pytest -q", "exit_code": 0},
    )
    assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
    return tid


def _error_rows(conn, tid):
    return conn.execute(
        "SELECT * FROM task_lessons WHERE source_task_id = ? "
        "AND error_signature IS NOT NULL ORDER BY id",
        (tid,),
    ).fetchall()


class TestAutomaticErrorLessons:
    def test_pass_after_fail_keys_one_lesson_per_failing_criterion(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(conn)
            rows = _error_rows(conn, tid)
            assert len(rows) == 2  # the PASS criterion is not an error
            for row in rows:
                assert row["state"] == kb.LESSON_STATE_CANDIDATE
                assert row["active"] == 0
                assert row["tenant"] == "acme"
                assert row["created_by"] == kb.ERROR_LESSON_AUTO_ACTOR
                assert "recomputed single-path total" in row["lesson"]
            assert len(_events(conn, tid, "error_lesson_recorded")) == 2

    def test_recorded_fix_is_retrievable_by_the_error(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(conn)
            hits = kb.lessons_for_error(
                conn, "Antenna double-count in executive total", tenant="acme"
            )
            assert any(tid in str(dict(h).get("lesson", "")) for h in hits)

    def test_generic_one_term_criterion_is_not_keyed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(
                conn,
                reason="- **Overall:** **FAIL.** see above\n"
                "- **Database connectivity:** **FAIL.** refused\n",
            )
            rows = _error_rows(conn, tid)
            assert len(rows) == 1
            assert "overall" not in rows[0]["error_signature"].split()

    def test_clean_pass_records_nothing(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="t", assignee="default", gauntlet=True)
            _review(conn, tid)
            ok, _ = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer", evidence={"exit_code": 0},
            )
            assert ok is True
            assert _error_rows(conn, tid) == []
            assert _events(conn, tid, "error_lesson_skipped") == []

    def test_unparsed_failure_prose_is_not_keyed(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(conn, reason="independent verification failed")
            assert _error_rows(conn, tid) == []
            skipped = _events(conn, tid, "error_lesson_skipped")
            assert skipped and skipped[0]["reason"] == "no_parsed_failing_criteria"

    def test_idempotent_per_task_and_signature(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(conn)
            again = kb._record_error_lessons_on_resolution(
                conn, tid, verifier="reviewer", reason="x", evidence={},
            )
            assert again == []
            assert len(_error_rows(conn, tid)) == 2

    def test_failure_inside_the_hook_never_unwinds_the_pass(self, kanban_home, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("fingerprint exploded")

        monkeypatch.setattr(kb, "failure_fingerprint", boom)
        with kb.connect_closing() as conn:
            tid = _fail_then_pass(conn)
            assert kb.get_task(conn, tid).verification_state == kb.VERIFICATION_VERIFIED
            errs = _events(conn, tid, "error_lesson_error")
            assert errs and errs[0]["error"] == "RuntimeError"


def _verifier_pair(conn):
    subject = kb.create_task(conn, title="subject", assignee="default")
    verifier = kb.create_task(conn, title="verifier", assignee="default")
    kb.link_tasks(conn, subject, verifier)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET executor_lane = ? WHERE id = ?",
            (kb.EXECUTOR_LANE_CODEX_VERIFY, verifier),
        )
    return subject, verifier


class TestUnlinkCannotStrandAVerifier:
    def test_open_hand_made_verifier_link_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, verifier = _verifier_pair(conn)
            with pytest.raises(kb.VerifierLinkProtected):
                kb.unlink_tasks(conn, subject, verifier)
            assert kb.parent_ids(conn, verifier) == [subject]
            assert _events(conn, verifier, "unlinked") == []

    def test_durable_relation_makes_unlink_safe(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, verifier = _verifier_pair(conn)
            kb.add_task_relation(conn, verifier, subject, kb.RELATION_VERIFIES)
            assert kb.unlink_tasks(conn, subject, verifier) is True
            assert subject in kb.verifier_subject_ids(conn, verifier)

    def test_archived_verifier_can_be_detached(self, kanban_home):
        with kb.connect_closing() as conn:
            subject, verifier = _verifier_pair(conn)
            assert kb.archive_task(conn, verifier) is True
            assert kb.unlink_tasks(conn, subject, verifier) is True

    def test_ordinary_dependency_links_are_unaffected(self, kanban_home):
        with kb.connect_closing() as conn:
            a = kb.create_task(conn, title="a", assignee="default")
            b = kb.create_task(conn, title="b", assignee="default")
            kb.link_tasks(conn, a, b)
            assert kb.unlink_tasks(conn, a, b) is True
