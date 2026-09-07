"""The verified-learning loop's write half must fire from the verdict itself.

``extract_lesson`` calls itself "the required post-VERIFIED step" and had zero
non-test callers, so the loop's read half (``build_worker_context`` injecting
binding lessons into every worker) ran against a table nothing ever wrote to:
the live board carried 403 verifications and 0 lessons.

These pin the wiring that closes it and -- at least as importantly -- pin that
it can never take a verdict down with it. A lesson is a by-product of a
verdict, never a condition of one.

Setup helpers mirror ``test_kanban_lessons.py`` deliberately: the lifecycle
they drive is the same, and a second, subtly different way to reach VERIFIED
would be testing a path production does not use.
"""

import json
from pathlib import Path

import pytest

import hermes_cli.kanban_db as kb


@pytest.fixture(autouse=True)
def _synthetic_identities_are_registered(monkeypatch):
    """Treat this module's stand-in names as registered profiles.

    Every assignee and verifier here ("default", "reviewer") is synthetic with
    no profile directory behind it; the D8 assignee gate and the verifier
    identity gate both refuse unregistered names. Without this the module would
    be testing the registry rather than the extraction wiring it is about.
    """
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


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [(r["kind"], json.loads(r["payload"]) if r["payload"] else None) for r in rows]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _pending(conn, *, assignee="default", title="task", tenant=None):
    """Drive a task to VERIFICATION_PENDING (parked in review)."""
    tid = kb.create_task(
        conn, title=title, assignee=assignee, gauntlet=True, tenant=tenant
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert (
        kb.request_review(
            conn,
            tid,
            summary="impl",
            reviewer=assignee,
            expected_run_id=claimed.current_run_id,
        )
        is True
    )
    return tid


LESSON = "Read a service's startup log to completion before concluding anything from a port check."


class TestExtractionFiresOnPass:
    def test_narrow_tenanted_lesson_is_written_and_binds(self, kanban_home):
        """The whole point: a PASS now produces a lesson with no one asking."""
        with kb.connect_closing() as conn:
            tid = _pending(conn, tenant="acme")
            ok, detail = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={
                    "command": "pytest -q",
                    "exit_code": 0,
                    kb.LESSON_EVIDENCE_KEY: LESSON,
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "assignee:default",
                },
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)

            extracted = _events(conn, tid, "lesson_extracted")
            assert extracted, [k for k, _ in _events(conn, tid)]
            assert extracted[0][1]["state"] == kb.LESSON_STATE_ACTIVE
            assert extracted[0][1]["auto_promoted"] is True

            rows = conn.execute(
                "SELECT state, active FROM task_lessons WHERE source_task_id = ?",
                (tid,),
            ).fetchall()
            assert len(rows) == 1
            assert rows[0]["active"] == 1
            # And it actually binds: the read half of the loop can see it.
            assert kb.list_lessons(conn)

    def test_unbounded_lesson_is_written_as_a_non_binding_candidate(
        self, kanban_home
    ):
        """Automation must not turn 'applies to everything' into canon.

        ``applicability='all'`` is a structural refusal in
        ``lesson_promotion_eligibility``. Firing extraction on every PASS makes
        that gate matter more, not less: the row is kept, attributable and
        auditable, and binds nothing until an operator approves it.
        """
        with kb.connect_closing() as conn:
            tid = _pending(conn, tenant="acme")
            ok, _ = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={
                    kb.LESSON_EVIDENCE_KEY: LESSON,
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "all",
                },
            )
            assert ok is True

            extracted = _events(conn, tid, "lesson_extracted")
            assert extracted
            assert extracted[0][1]["state"] == kb.LESSON_STATE_CANDIDATE
            assert extracted[0][1]["eligibility_reason"] == (
                "unbounded_applicability_needs_operator"
            )
            rows = conn.execute(
                "SELECT active FROM task_lessons WHERE source_task_id = ?", (tid,)
            ).fetchall()
            assert len(rows) == 1 and rows[0]["active"] == 0
            # Binding nothing until approved.
            assert kb.list_lessons(conn) == []

    def test_no_lesson_offered_is_counted_not_silent(self, kanban_home):
        """The gap must be countable. Silence is what hid 403-to-0."""
        with kb.connect_closing() as conn:
            tid = _pending(conn)
            ok, _ = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={"command": "pytest -q", "exit_code": 0},
            )
            assert ok is True

            skipped = _events(conn, tid, "lesson_extraction_skipped")
            assert skipped, [k for k, _ in _events(conn, tid)]
            assert skipped[0][1]["reason"] == "no_lesson_offered"
            assert kb.list_lessons(conn) == []

    def test_evidence_none_does_not_crash_the_verdict(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _pending(conn)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer", evidence=None
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            assert _events(conn, tid, "lesson_extraction_skipped")

    def test_lesson_without_applicability_is_declined_with_a_reason(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _pending(conn)
            ok, _ = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={kb.LESSON_EVIDENCE_KEY: LESSON},
            )
            assert ok is True
            skipped = _events(conn, tid, "lesson_extraction_skipped")
            assert skipped and skipped[0][1]["reason"] == "no_applicability"
            assert kb.list_lessons(conn) == []


    def test_untenanted_source_cannot_become_a_global_lesson(self, kanban_home):
        """The scope gate must hold through the NEW automatic path too.

        ``promote_lesson`` refuses to turn an untenanted source into a lesson
        every lane receives unless an operator says so explicitly. Wiring
        extraction to fire on every PASS must not become a way around that --
        automation is exactly where such a gate quietly stops being enforced.
        """
        with kb.connect_closing() as conn:
            tid = _pending(conn)  # no tenant
            ok, detail = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={
                    kb.LESSON_EVIDENCE_KEY: LESSON,
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "all",
                },
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            blocked = _events(conn, tid, "lesson_extraction_blocked")
            assert blocked, [k for k, _ in _events(conn, tid)]
            assert kb.list_lessons(conn) == []


class TestExtractionNeverBreaksTheVerdict:
    def test_extraction_exception_does_not_unwind_the_pass(
        self, kanban_home, monkeypatch
    ):
        with kb.connect_closing() as conn:
            tid = _pending(conn)

            def _boom(*a, **kw):
                raise RuntimeError("extraction exploded")

            monkeypatch.setattr(kb, "extract_lesson", _boom)

            ok, detail = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={
                    kb.LESSON_EVIDENCE_KEY: LESSON,
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "all",
                },
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED), (
                "a broken extractor took a passing verdict down with it"
            )
            assert _events(conn, tid, "lesson_extraction_error")

    def test_gate_refusal_is_recorded_as_blocked(self, kanban_home, monkeypatch):
        with kb.connect_closing() as conn:
            tid = _pending(conn)

            def _refuse(*a, **kw):
                raise kb.LessonPromotionError("scope_too_broad", "refused by the gate")

            monkeypatch.setattr(kb, "extract_lesson", _refuse)

            ok, _ = kb.record_verification(
                conn,
                tid,
                passed=True,
                verifier="reviewer",
                evidence={
                    kb.LESSON_EVIDENCE_KEY: LESSON,
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "all",
                },
            )
            assert ok is True
            blocked = _events(conn, tid, "lesson_extraction_blocked")
            assert blocked, [k for k, _ in _events(conn, tid)]
            assert blocked[0][1]["code"] == "scope_too_broad"


class TestExtractionDoesNotFireOnFailure:
    def test_failing_verdict_never_extracts(self, kanban_home):
        """An unverified finding must not become canon. The gate would refuse
        it anyway; not calling at all is the cheaper guarantee."""
        with kb.connect_closing() as conn:
            tid = _pending(conn)
            ok, _ = kb.record_verification(
                conn,
                tid,
                passed=False,
                verifier="reviewer",
                reason="3 tests fail",
                route_on_failure=False,
                evidence={
                    kb.LESSON_EVIDENCE_KEY: "a rule drawn from a failure",
                    kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "all",
                },
            )
            assert ok is True
            kinds = [k for k, _ in _events(conn, tid)]
            assert not any(k.startswith("lesson_") for k in kinds), kinds
            assert kb.list_lessons(conn) == []


class TestVerifierLessonDeclaration:
    """The independent-verifier return path is the route that actually runs.

    ``_extract_lesson_on_verify`` reads the lesson off the evidence dict, and
    that path builds its own — so without a parser the wiring could only ever
    record ``no_lesson_offered`` in production, which is the same inert-loop
    failure this whole change exists to remove.
    """

    def test_both_declarations_are_parsed(self):
        got = kb._parse_verifier_lesson(
            "VERDICT: PASS\n"
            "LESSON: do not treat a provider 429 as an implementation failure\n"
            "LESSON_APPLICABILITY: assignee:coder\n"
        )
        assert got == {
            kb.LESSON_EVIDENCE_KEY: (
                "do not treat a provider 429 as an implementation failure"
            ),
            kb.LESSON_APPLICABILITY_EVIDENCE_KEY: "assignee:coder",
        }

    def test_markdown_emphasis_is_tolerated(self):
        got = kb._parse_verifier_lesson(
            "**LESSON:** be careful\n**LESSON_APPLICABILITY:** project:api"
        )
        assert got[kb.LESSON_EVIDENCE_KEY] == "be careful"
        assert got[kb.LESSON_APPLICABILITY_EVIDENCE_KEY] == "project:api"

    def test_applicability_line_is_not_captured_as_the_lesson(self):
        """The LESSON: pattern also matches 'LESSON_APPLICABILITY:'.

        Without stripping applicability lines first, a report declaring only a
        selector would yield lesson='_APPLICABILITY: assignee:coder' — a
        garbage rule promoted from a verifier that never wrote one.
        """
        assert kb._parse_verifier_lesson("LESSON_APPLICABILITY: assignee:coder") is None

    @pytest.mark.parametrize(
        "text",
        [
            "VERDICT: PASS",
            "LESSON: a rule with no selector",
            "LESSON:   \nLESSON_APPLICABILITY: assignee:coder",
            "",
            None,
        ],
    )
    def test_fails_closed_on_incomplete_declarations(self, text):
        assert kb._parse_verifier_lesson(text) is None

    def test_declaration_order_does_not_matter(self):
        got = kb._parse_verifier_lesson(
            "LESSON_APPLICABILITY: project:api\nLESSON: be careful"
        )
        assert got[kb.LESSON_EVIDENCE_KEY] == "be careful"
