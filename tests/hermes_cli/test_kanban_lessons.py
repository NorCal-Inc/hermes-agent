"""Verified lessons: only verified work becomes canon, and it binds future work.

The last link of the Gauntlet contract — "only verified lessons may become
durable canonical learning; failed or unresolved attempts remain
observations/candidates" — expressed as three mechanical properties rather
than as guidance:

  * PROVENANCE — ``promote_lesson`` refuses any source task that is not
    durably VERIFIED, and refuses a repaired one whose regression proof was
    never spent.
  * SCOPE — a lesson never leaves its tenant's lane; an untenanted source
    produces nothing at all without an explicit operator flag.
  * CONSUMPTION — ``build_worker_context`` (the one funnel every dispatched
    worker reads its task through) injects the active, in-scope, exactly
    applicable lessons as binding constraints, with their provenance.

Matching is exact string equality on a closed selector grammar. There is no
inference here on purpose: these tests pin down that a selector either matches
or does not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


REGRESSION_PROOF = {"command": "pytest -q tests/hermes_cli", "exit_code": 0}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
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
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _executing(conn, *, assignee="default", title="task", tenant=None):
    """Create a Gauntlet task and drive it to EXECUTING."""
    tid = kb.create_task(
        conn, title=title, assignee=assignee, gauntlet=True, tenant=tenant,
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    return tid, claimed.current_run_id


def _pending(conn, **kw):
    """Drive a task to VERIFICATION_PENDING (parked in review)."""
    tid, run_id = _executing(conn, **kw)
    assert kb.request_review(
        conn, tid, summary="impl", reviewer=kw.get("assignee", "default"),
        expected_run_id=run_id,
    ) is True
    return tid, run_id


def _verified(conn, **kw):
    """Drive a task all the way to a durable VERIFIED head."""
    tid, _run_id = _pending(conn, **kw)
    ok, detail = kb.record_verification(
        conn, tid, passed=True, verifier="reviewer",
        evidence={"command": "pytest -q", "exit_code": 0},
    )
    assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
    return tid


def _promote(conn, tid, **kw):
    kw.setdefault("lesson", "Always re-run the integration suite after "
                            "touching the claim lock.")
    kw.setdefault("applicability", "all")
    kw.setdefault("actor", "operator")
    return kb.promote_lesson(conn, tid, **kw)


# ---------------------------------------------------------------------------
# Provenance: only a durably verified task may promote
# ---------------------------------------------------------------------------

class TestOnlyVerifiedMayPromote:
    def test_unverified_task_cannot_promote(self, kanban_home):
        """Never entered the chain: an executing claim is not a verdict."""
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "not_verified"
            assert kb.list_lessons(conn) == []

    def test_pending_task_cannot_promote(self, kanban_home):
        """Handed off but not yet ruled on — there is nothing verified yet."""
        with kb.connect_closing() as conn:
            tid, _ = _pending(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "not_verified"
            assert "awaiting a verdict" in str(exc.value)
            assert kb.list_lessons(conn) == []

    def test_failed_task_cannot_promote(self, kanban_home):
        """A failed attempt stays an observation, never canon."""
        with kb.connect_closing() as conn:
            tid, _ = _pending(conn)
            ok, _ = kb.record_verification(
                conn, tid, passed=False, verifier="reviewer",
                reason="3 tests fail", route_on_failure=False,
            )
            assert ok is True
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "not_verified"
            assert "FAILED verification" in str(exc.value)
            assert kb.list_lessons(conn) == []

    def test_verified_task_can_promote(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            lesson = _promote(
                conn, tid,
                lesson="Claim the lock before writing the run row.",
                applicability="assignee:default",
                allow_global=True,
            )
            assert lesson["source_task_id"] == tid
            assert lesson["applicability"] == "assignee:default"
            assert lesson["active"] is True
            assert lesson["verification_id"] is not None
            assert lesson["evidence"]["verifier"] == "reviewer"
            assert lesson["evidence"]["verification_state"] == "verified"

        # Durable across the connection, not just in the promoting process.
        with kb.connect_closing() as conn:
            stored = kb.list_lessons(conn)
            assert len(stored) == 1
            assert stored[0]["lesson"] == (
                "Claim the lock before writing the run row."
            )

    def test_stale_verdict_cannot_promote(self, kanban_home):
        """A re-claim invalidates the verdict, and with it the right to promote.

        The head is cleared because new implementation work started that the
        verifier never saw — exactly the case where a lesson drawn from the
        card would not be backed by anyone.
        """
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            assert kb.reopen_review_task(conn, tid) is True
            reclaimed = kb.claim_task(conn, tid)
            assert reclaimed is not None
            assert kb.get_task(conn, tid).verification_state is None
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "not_verified"

    def test_completed_verified_task_still_promotes(self, kanban_home):
        """The normal case: the lesson is written after the card closes."""
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            assert kb.complete_task(conn, tid, summary="shipped") is True
            lesson = _promote(conn, tid, allow_global=True)
            assert lesson["evidence"]["source_status"] == "done"

    def test_unknown_task_cannot_promote(self, kanban_home):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, "t_nope", allow_global=True)
            assert exc.value.code == "unknown_task"

    def test_refusal_is_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            with pytest.raises(kb.LessonPromotionError):
                _promote(conn, tid, allow_global=True)
            blocked = _events(conn, tid, kind="lesson_promotion_blocked")
            assert len(blocked) == 1
            assert blocked[0][1]["code"] == "not_verified"
            assert blocked[0][1]["applicability"] == "all"

    def test_promotion_is_auditable(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            lesson = _promote(conn, tid, allow_global=True)
            promoted = _events(conn, tid, kind="lesson_promoted")
            assert len(promoted) == 1
            assert promoted[0][1]["lesson_id"] == lesson["id"]
            assert promoted[0][1]["scope"] == kb.LESSON_SCOPE_GLOBAL
            assert promoted[0][1]["actor"] == "operator"


# ---------------------------------------------------------------------------
# Provenance: a repaired task must have actually proved its repair
# ---------------------------------------------------------------------------

class TestRepairedSourceNeedsItsProof:
    def _fail_then_repair(self, conn, tid, run_id):
        kb.record_verification(
            conn, tid, passed=False, verifier="reviewer", reason="3 tests fail",
        )
        repaired = kb.claim_task(conn, tid)
        assert repaired is not None
        assert kb.request_review(
            conn, tid, summary="fixed", reviewer="default",
            expected_run_id=repaired.current_run_id,
        ) is True

    def test_regression_debt_outstanding_cannot_promote(self, kanban_home):
        """Verified head, unpaid regression debt — the same pair completion tests.

        Simulates the state a raced arm or a bypassed pre-check could leave:
        the head reads verified while ``regression_required`` is still set.
        Promotion must refuse it for the same reason completion does.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _pending(conn)
            self._fail_then_repair(conn, tid, run_id)
            conn.execute(
                "UPDATE tasks SET verification_state = 'verified' WHERE id = ?",
                (tid,),
            )
            conn.commit()
            task = kb.get_task(conn, tid)
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            assert task.regression_required is True

            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "regression_outstanding"
            assert kb.list_lessons(conn) == []

    def test_repaired_and_proved_can_promote(self, kanban_home):
        """The positive control: proof re-run, verdict spent it, lesson allowed."""
        with kb.connect_closing() as conn:
            tid, run_id = _pending(conn)
            self._fail_then_repair(conn, tid, run_id)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="reviewer",
                regression_evidence=REGRESSION_PROOF,
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)

            lesson = _promote(conn, tid, allow_global=True)
            assert lesson["regression_proof_id"] is not None
            assert lesson["evidence"]["source_ever_failed_verification"] is True
            # The proof named on the lesson is a real regression row.
            proof = [
                h for h in kb.verification_history(conn, tid)
                if h["kind"] == kb.LEDGER_KIND_REGRESSION
            ]
            assert len(proof) == 1
            assert proof[0]["id"] == lesson["regression_proof_id"]

    def test_verdict_without_a_named_proof_cannot_promote(self, kanban_home):
        """A previously-failed card blessed by a bare verdict is still refused.

        Reaches past ``record_verification`` and writes the head the way a
        legacy board or a bypassed gate would: the ledger shows a failure and
        then a pass that names no proof. The repair was never demonstrated, so
        it cannot become canonical learning.
        """
        with kb.connect_closing() as conn:
            tid, run_id = _pending(conn)
            self._fail_then_repair(conn, tid, run_id)
            conn.execute(
                "INSERT INTO task_verifications "
                "(task_id, run_id, state, verifier, evidence, reason, "
                " created_at, kind, covers_phase_id) "
                "VALUES (?, NULL, 'verified', 'reviewer', NULL, NULL, "
                "        strftime('%s','now'), 'verdict', NULL)",
                (tid,),
            )
            conn.execute(
                "UPDATE tasks SET verification_state = 'verified', "
                "regression_required = 0 WHERE id = ?",
                (tid,),
            )
            conn.commit()

            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "regression_proof_missing"
            assert kb.list_lessons(conn) == []


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestPromotionValidation:
    def test_empty_lesson_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                kb.promote_lesson(
                    conn, tid, lesson="   ", applicability="all",
                    allow_global=True,
                )
            assert exc.value.code == "empty_lesson"
            assert kb.list_lessons(conn) == []

    def test_empty_applicability_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                kb.promote_lesson(
                    conn, tid, lesson="do the thing", applicability="",
                    allow_global=True,
                )
            assert exc.value.code == "empty_applicability"
            assert kb.list_lessons(conn) == []

    @pytest.mark.parametrize(
        "selector",
        ["assignee", "assignee:", "vibes:good", "anything at all", ":coder"],
    )
    def test_uninterpretable_selectors_are_refused(self, kanban_home, selector):
        """A selector the kernel can't evaluate is refused, not silently inert."""
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                kb.promote_lesson(
                    conn, tid, lesson="rule", applicability=selector,
                    allow_global=True,
                )
            assert exc.value.code in {
                "invalid_applicability", "empty_applicability",
            }

    def test_selectors_are_normalised_at_write_time(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            lesson = _promote(
                conn, tid, applicability="  Assignee: Default  ",
                allow_global=True,
            )
            assert lesson["applicability"] == "assignee:default"

    def test_oversized_lesson_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                kb.promote_lesson(
                    conn, tid, lesson="x" * (kb.LESSON_MAX_CHARS + 1),
                    applicability="all", allow_global=True,
                )
            assert exc.value.code == "lesson_too_long"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

class TestTenantScope:
    def test_tenant_source_creates_a_tenant_lesson(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn, tenant="acme")
            lesson = _promote(conn, tid)
            assert lesson["scope"] == kb.LESSON_SCOPE_TENANT
            assert lesson["tenant"] == "acme"

    def test_tenant_source_cannot_go_global(self, kanban_home):
        """Even with the operator flag: a lane's learning stays in its lane."""
        with kb.connect_closing() as conn:
            tid = _verified(conn, tenant="acme")
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid, allow_global=True)
            assert exc.value.code == "tenant_cannot_go_global"
            assert kb.list_lessons(conn) == []

    def test_untenanted_source_is_scoped_safe_by_default(self, kanban_home):
        """No tenant and no flag = no lesson. The default never broadcasts."""
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            with pytest.raises(kb.LessonPromotionError) as exc:
                _promote(conn, tid)
            assert exc.value.code == "global_not_authorised"
            assert kb.list_lessons(conn) == []

    def test_untenanted_source_goes_global_only_on_the_flag(self, kanban_home):
        with kb.connect_closing() as conn:
            tid = _verified(conn)
            lesson = _promote(conn, tid, allow_global=True)
            assert lesson["scope"] == kb.LESSON_SCOPE_GLOBAL
            assert lesson["tenant"] is None

    def test_a_lesson_never_crosses_into_another_tenant(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, tenant="acme", title="acme source")
            _promote(conn, source, lesson="Acme bills in arrears.")

            same, _ = _executing(conn, tenant="acme", title="acme work")
            other, _ = _executing(conn, tenant="globex", title="globex work")
            none, _ = _executing(conn, title="untenanted work")

            assert [le["lesson"] for le in kb.lessons_for_task(conn, same)] == [
                "Acme bills in arrears."
            ]
            assert kb.lessons_for_task(conn, other) == []
            # An untenanted task is in no lane, so it does not get to read
            # every lane's learning either.
            assert kb.lessons_for_task(conn, none) == []

            ctx = kb.build_worker_context(conn, other)
            assert "Acme bills in arrears" not in ctx

    def test_a_global_lesson_reaches_every_lane(self, kanban_home):
        """What the operator flag actually buys — and only the operator can."""
        with kb.connect_closing() as conn:
            source = _verified(conn, title="infra source")
            _promote(
                conn, source, lesson="Never rebind a service port.",
                allow_global=True,
            )
            tenant_task, _ = _executing(conn, tenant="acme", title="acme work")
            plain, _ = _executing(conn, title="plain work")
            for tid in (tenant_task, plain):
                assert [le["lesson"] for le in kb.lessons_for_task(conn, tid)] == [
                    "Never rebind a service port."
                ]

    def test_list_lessons_tenant_filter_matches_visibility(self, kanban_home):
        with kb.connect_closing() as conn:
            acme = _verified(conn, tenant="acme", title="acme source")
            _promote(conn, acme, lesson="acme rule")
            globex = _verified(conn, tenant="globex", title="globex source")
            _promote(conn, globex, lesson="globex rule")
            shared = _verified(conn, title="shared source")
            _promote(conn, shared, lesson="global rule", allow_global=True)

            visible = {
                le["lesson"] for le in kb.list_lessons(conn, tenant="acme")
            }
            assert visible == {"acme rule", "global rule"}


# ---------------------------------------------------------------------------
# Exact applicability matching
# ---------------------------------------------------------------------------

class TestApplicabilityMatching:
    def test_exact_selector_matches_and_near_misses_do_not(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="coder", title="source")
            _promote(
                conn, source, lesson="Coders run the linter first.",
                applicability="assignee:coder", allow_global=True,
            )
            match, _ = _executing(conn, assignee="coder", title="more coding")
            miss, _ = _executing(conn, assignee="reviewer", title="reviewing")

            assert len(kb.lessons_for_task(conn, match)) == 1
            assert kb.lessons_for_task(conn, miss) == []

    def test_all_selector_matches_every_task_in_scope(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            _promote(
                conn, source, lesson="Write the checkpoint.",
                applicability="all", allow_global=True,
            )
            a, _ = _executing(conn, assignee="coder", title="a")
            b, _ = _executing(conn, assignee="reviewer", title="b")
            assert len(kb.lessons_for_task(conn, a)) == 1
            assert len(kb.lessons_for_task(conn, b)) == 1

    def test_workspace_dimension_matches_on_the_task_row(
        self, kanban_home, tmp_path
    ):
        tmp_dir = tmp_path / "workdir"
        tmp_dir.mkdir()
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            _promote(
                conn, source, lesson="Scratch workspaces are wiped nightly.",
                applicability="workspace:scratch", allow_global=True,
            )
            scratch, _ = _executing(conn, title="scratch work")
            assert kb.get_task(conn, scratch).workspace_kind == "scratch"
            assert len(kb.lessons_for_task(conn, scratch)) == 1

            dir_id = kb.create_task(
                conn, title="dir work", assignee="default",
                workspace_kind="dir", workspace_path=str(tmp_dir),
            )
            assert kb.get_task(conn, dir_id).workspace_kind == "dir"
            assert kb.lessons_for_task(conn, dir_id) == []

    def test_task_selectors_are_the_closed_set_read_off_the_row(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn, assignee="coder", tenant="acme")
            task = kb.get_task(conn, tid)
            assert set(kb.task_lesson_selectors(task)) == {
                "all",
                "assignee:coder",
                "workspace:scratch",
                "tenant:acme",
            }

    def test_matching_is_case_insensitive_via_normalisation_both_sides(
        self, kanban_home
    ):
        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="Coder", title="source")
            _promote(
                conn, source, lesson="rule",
                applicability="ASSIGNEE:CODER", allow_global=True,
            )
            match, _ = _executing(conn, assignee="Coder", title="more")
            assert len(kb.lessons_for_task(conn, match)) == 1


# ---------------------------------------------------------------------------
# Consumption: the worker context is where this becomes mechanical
# ---------------------------------------------------------------------------

class TestWorkerContextInjection:
    def test_applicable_task_gets_the_lesson_marked_binding(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="coder", title="the source card")
            lesson = _promote(
                conn, source,
                lesson="Always re-run tests/hermes_cli after touching claims.",
                applicability="assignee:coder", allow_global=True,
            )
            tid, _ = _executing(conn, assignee="coder", title="new work")
            ctx = kb.build_worker_context(conn, tid)

            assert "## Binding verified lessons" in ctx
            assert "BINDING constraints" in ctx
            assert (
                "Always re-run tests/hermes_cli after touching claims." in ctx
            )
            # Provenance travels with it: which card, which verdict, who.
            assert source in ctx
            assert f"Lesson {lesson['id']}" in ctx
            assert f"verification ledger #{lesson['verification_id']}" in ctx
            assert "assignee:coder" in ctx

    def test_non_applicable_task_gets_nothing(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="coder", title="the source card")
            _promote(
                conn, source, lesson="Coders run the linter first.",
                applicability="assignee:coder", allow_global=True,
            )
            other, _ = _executing(conn, assignee="reviewer", title="review job")
            ctx = kb.build_worker_context(conn, other)
            assert "Binding verified lessons" not in ctx
            assert "Coders run the linter first." not in ctx

    def test_a_board_with_no_lessons_renders_unchanged(self, kanban_home):
        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
            assert "Binding verified lessons" not in kb.build_worker_context(
                conn, tid
            )

    def test_injection_is_bounded(self, kanban_home):
        """A rulebook-sized board must not swallow the worker's prompt."""
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            for i in range(kb._CTX_MAX_LESSONS + 3):
                _promote(
                    conn, source, lesson=f"rule number {i}",
                    applicability="all", allow_global=True,
                )
            tid, _ = _executing(conn, title="new work")
            ctx = kb.build_worker_context(conn, tid)
            assert ctx.count("### Lesson ") == kb._CTX_MAX_LESSONS
            assert "3 earlier lessons omitted" in ctx


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

class TestRetirement:
    def test_retirement_stops_injection_but_keeps_the_history(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            lesson = _promote(
                conn, source, lesson="Deploy on Fridays.", allow_global=True,
            )
            tid, _ = _executing(conn, title="later work")
            assert "Deploy on Fridays." in kb.build_worker_context(conn, tid)

            ok, detail = kb.retire_lesson(
                conn, lesson["id"], actor="operator",
                reason="policy changed 2026-08-30",
            )
            assert (ok, detail) == (True, None)

            assert kb.lessons_for_task(conn, tid) == []
            assert "Deploy on Fridays." not in kb.build_worker_context(conn, tid)
            assert kb.list_lessons(conn) == []

            # History survives: the row, its reason, and the audit event.
            archived = kb.list_lessons(conn, active_only=False)
            assert len(archived) == 1
            assert archived[0]["active"] is False
            assert archived[0]["retired_by"] == "operator"
            assert archived[0]["retired_reason"] == "policy changed 2026-08-30"
            assert archived[0]["retired_at"] is not None
            assert archived[0]["lesson"] == "Deploy on Fridays."
            retired_events = _events(conn, source, kind="lesson_retired")
            assert len(retired_events) == 1
            assert retired_events[0][1]["lesson_id"] == lesson["id"]
            # The promotion event is untouched — it still records that this
            # rule once governed.
            assert len(_events(conn, source, kind="lesson_promoted")) == 1

    def test_retiring_twice_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            lesson = _promote(conn, source, allow_global=True)
            assert kb.retire_lesson(conn, lesson["id"])[0] is True
            ok, detail = kb.retire_lesson(conn, lesson["id"])
            assert ok is False
            assert "already retired" in detail

    def test_retiring_an_unknown_lesson_is_refused(self, kanban_home):
        with kb.connect_closing() as conn:
            ok, detail = kb.retire_lesson(conn, 4242)
            assert ok is False
            assert "not found" in detail


# ---------------------------------------------------------------------------
# Operator + worker surfaces
# ---------------------------------------------------------------------------

class TestCliSurface:
    def test_promote_list_and_retire_round_trip(self, kanban_home):
        from hermes_cli import kanban as kc

        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="coder", title="source")

        out = kc.run_slash(
            f"lesson-promote {source} --lesson 'Run the linter first' "
            f"--applicability assignee:coder --global"
        )
        assert "Promoted lesson" in out
        assert "assignee:coder" in out

        listed = json.loads(kc.run_slash("lessons --json"))
        assert len(listed) == 1
        lesson_id = listed[0]["id"]
        assert listed[0]["lesson"] == "Run the linter first"
        assert listed[0]["scope"] == kb.LESSON_SCOPE_GLOBAL

        with kb.connect_closing() as conn:
            target, _ = _executing(conn, assignee="coder", title="next")
        scoped = json.loads(kc.run_slash(f"lessons --task {target} --json"))
        assert [le["id"] for le in scoped] == [lesson_id]

        assert "Retired lesson" in kc.run_slash(
            f"lesson-retire {lesson_id} --reason 'superseded'"
        )
        assert json.loads(kc.run_slash("lessons --json")) == []
        archived = json.loads(kc.run_slash("lessons --all --json"))
        assert archived[0]["active"] is False
        assert archived[0]["retired_reason"] == "superseded"

    def test_cli_refuses_an_unverified_source(self, kanban_home):
        from hermes_cli import kanban as kc

        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
        out = kc.run_slash(
            f"lesson-promote {tid} --lesson 'nope' --applicability all --global"
        )
        assert "cannot promote a lesson" in out
        with kb.connect_closing() as conn:
            assert kb.list_lessons(conn) == []


class TestToolSurface:
    def test_tool_promotes_from_a_verified_task(self, kanban_home, monkeypatch):
        from tools import kanban_tools as kt

        with kb.connect_closing() as conn:
            source = _verified(conn, assignee="coder", title="source")
        monkeypatch.setenv("HERMES_KANBAN_TASK", source)

        payload = json.loads(kt._handle_promote_lesson({
            "lesson": "Re-read the ledger before claiming a verdict.",
            "applicability": "assignee:coder",
            "allow_global": True,
        }))
        assert payload["ok"] is True
        assert payload["source_task_id"] == source
        assert payload["applicability"] == "assignee:coder"

        with kb.connect_closing() as conn:
            assert len(kb.list_lessons(conn)) == 1

    def test_tool_refuses_and_explains_an_unverified_source(
        self, kanban_home, monkeypatch
    ):
        from tools import kanban_tools as kt

        with kb.connect_closing() as conn:
            tid, _ = _executing(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)

        payload = json.loads(kt._handle_promote_lesson({
            "lesson": "trust me",
            "applicability": "all",
            "allow_global": True,
        }))
        assert payload.get("error")
        assert "not_verified" in payload["error"]
        with kb.connect_closing() as conn:
            assert kb.list_lessons(conn) == []

    def test_tool_rejects_an_empty_lesson(self, kanban_home, monkeypatch):
        from tools import kanban_tools as kt

        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
        monkeypatch.setenv("HERMES_KANBAN_TASK", source)

        payload = json.loads(kt._handle_promote_lesson({
            "lesson": "  ",
            "applicability": "all",
            "allow_global": True,
        }))
        assert payload.get("error")
        assert "empty_lesson" in payload["error"]


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_legacy_board_without_the_table_upgrades_in_place(self, kanban_home):
        """A DB predating lessons must open, keep working, and gain the table."""
        with kb.connect_closing() as conn:
            tid = _verified(conn, title="pre-lessons")
            conn.execute("DROP TABLE task_lessons")
            conn.commit()
            # The board still functions without the table for everything that
            # predates it.
            assert kb.complete_task(conn, tid, summary="ok") is True

        kb.init_db()
        with kb.connect_closing() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(task_lessons)")
            }
            assert {
                "source_task_id", "tenant", "scope", "applicability", "lesson",
                "evidence", "verification_id", "regression_proof_id",
                "created_by", "created_at", "active", "retired_at",
                "retired_by", "retired_reason",
            } <= cols
            assert kb.list_lessons(conn) == []
            # And the upgraded board can promote and inject immediately.
            lesson = _promote(
                conn, tid, lesson="Migrated boards work.", allow_global=True,
            )
            later, _ = _executing(conn, title="after the migration")
            assert "Migrated boards work." in kb.build_worker_context(
                conn, later
            )
            assert lesson["id"] > 0

    def test_migration_is_idempotent_and_preserves_rows(self, kanban_home):
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            lesson = _promote(conn, source, allow_global=True)

        kb.init_db()
        kb.init_db()

        with kb.connect_closing() as conn:
            stored = kb.list_lessons(conn)
            assert len(stored) == 1
            assert stored[0]["id"] == lesson["id"]
            assert stored[0]["lesson"] == lesson["lesson"]

    def test_partial_table_gains_missing_columns(self, kanban_home):
        """An intermediate build's table is upgraded, not replaced."""
        with kb.connect_closing() as conn:
            source = _verified(conn, title="source")
            _promote(conn, source, allow_global=True)
            conn.execute("ALTER TABLE task_lessons DROP COLUMN retired_reason")
            conn.execute("ALTER TABLE task_lessons DROP COLUMN retired_by")
            conn.commit()

        kb.init_db()
        with kb.connect_closing() as conn:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(task_lessons)")
            }
            assert {"retired_by", "retired_reason"} <= cols
            stored = kb.list_lessons(conn)
            assert len(stored) == 1
            assert stored[0]["retired_reason"] is None
