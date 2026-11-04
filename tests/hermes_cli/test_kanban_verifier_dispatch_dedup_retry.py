"""Regression tests for the Gauntlet verifier-dispatch incident (2026-11-04,
t_c645f27d / t_820c2104 and their verifier children t_062a5850, t_93c18b08,
t_7e3938d2, t_f67dd1ab, t_a153932a, t_f1d3dbff).

Diagnosis corrected an initial hypothesis -- recorded here because the first
instinct (centralize a "reuse a BLOCKED verifier child too" check inside
``_ensure_independent_verifier_child``) turned out to be wrong and was
reverted; see the fix log for the full trail:

(a) "Duplicate spawn" re-examined: ``_ensure_independent_verifier_child``'s
    existing ``_open_verifier_child`` check (live = todo/ready/running) was
    ALREADY correct and is confirmed here by
    ``TestNonTerminalVerifierChildIsReusedNotDuplicated``. Walking the real
    timestamps on t_c645f27d's four verifier children showed each one was
    created only AFTER its predecessor reached a terminal state (FAIL
    verdict -> done, or crash -> blocked) -- legitimate sequential
    re-verification, not a race. Minting a FRESH replacement once the only
    candidate is ``blocked`` is itself deliberate, pre-existing, tested
    behavior (``test_kanban_self_review_recovery.py::
    TestVerifierChildReuseIntegrity``) for verifiers that can never recover
    (e.g. invalid linkage) -- changing that to always reuse/await a blocked
    child broke that contract, so it was NOT changed.

(b) No bounded retry on verifier-tool crash -- the real, confirmed defect:
    ``run_codex_verifier`` / ``run_claude_verifier`` called ``kb.block_task``
    unconditionally on the FIRST implementation-class execution crash (error
    signature ``[codex] execution ended failed (rc=1)
    [failure_class=implementation]``), with zero automatic retries, even
    though the verifier TOOL crashing produces no verdict at all (not a
    FAIL). Because blocked verifiers are legitimately replaced on the next
    resubmission (see (a) above), every tooling crash was silently
    amplifying into an orphaned blocked card AND a fresh sibling -- exactly
    the "multiple verifier children in succession" shape observed live.
    Fixing (b) removes most of that amplification at the source: a crash
    now retries in place up to ``VERIFIER_EXECUTION_CRASH_RETRY_LIMIT``
    times before ``block_task`` is ever reached.

Neither fix touches verdict (PASS/FAIL/BLOCKER) semantics, self-cert
prevention, or independent-verifier identity -- both are exercised
unchanged by ``test_kanban_verifier_identity.py`` and
``test_kanban_verifier_lane_swap.py`` alongside this file.
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
    """A gauntlet implementation task parked for independent verification,
    carrying evidence so the verifier-child dependency gate will dispatch it.
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


class TestNonTerminalVerifierChildIsReusedNotDuplicated:
    """(a) Re-requesting a verifier route for the same subject while a
    LIVE (todo/ready/running) verifier child exists must reuse it, never
    mint a second one beside it. Confirms the existing
    ``_open_verifier_child`` dedup already does this correctly -- the
    "multiple children" seen live were sequential, not concurrent
    duplicates (see module docstring)."""

    def test_first_call_creates_one_child(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            assert verifier is not None
            children = kb.child_ids(conn, subject)
            assert children == [verifier]

    def test_second_call_in_quick_succession_does_not_duplicate(
        self, kanban_home, registry,
    ):
        """Simulates the live incident: two rapid calls into the verifier-
        route helper for the SAME subject (e.g. two ``request_review`` or
        ``claim_review_task`` entries landing close together) must not
        produce two verifier children."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)

            first = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            second = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )

            assert first is not None
            assert first == second, (
                "a second call against the same subject must reuse the "
                "existing (live) verifier child, not mint a new one"
            )
            children = kb.child_ids(conn, subject)
            verifier_children = [
                c for c in children
                if kb.get_task(conn, c).executor_lane in kb.INDEPENDENT_VERIFIER_LANES
            ]
            assert len(verifier_children) == 1


class TestVerifierExecutionCrashBoundedRetry:
    """(b) A verifier execution that crashes (implementation/tooling-class
    failure, no verdict) must retry automatically up to
    ``VERIFIER_EXECUTION_CRASH_RETRY_LIMIT`` attempts before the caller's
    normal ``block_task`` escalation is allowed to fire.
    """

    def test_limit_is_explicit_and_bounded(self):
        assert kb.VERIFIER_EXECUTION_CRASH_RETRY_LIMIT in (2, 3)

    def test_first_crash_retries_instead_of_blocking(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            kb.recompute_ready(conn)
            claimed = kb.claim_task(conn, verifier)
            assert claimed is not None and claimed.status == "running"

            retried = kb.verifier_execution_crash_retry(
                conn, verifier, expected_run_id=claimed.current_run_id,
                reason="[codex] execution ended failed (rc=1) "
                       "[failure_class=implementation]",
                infrastructure=False,
            )

            assert retried is True
            task = kb.get_task(conn, verifier)
            assert task.status in ("ready", "review")
            assert task.consecutive_failures == 1

    def test_crash_retries_up_to_the_bound_then_falls_back_to_block(
        self, kanban_home, registry,
    ):
        """Drive the SAME verifier card through repeated crashes: every
        attempt below the bound must auto-retry; the attempt that reaches
        the bound must refuse (``False``), handing control back to the
        caller's existing ``block_task`` escalation -- exactly once, and
        only then."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            kb.recompute_ready(conn)
            limit = kb.VERIFIER_EXECUTION_CRASH_RETRY_LIMIT
            reason = ("[codex] execution ended failed (rc=1) "
                       "[failure_class=implementation]")

            retried_flags = []
            for attempt_num in range(1, limit + 1):
                claimed = kb.claim_task(conn, verifier)
                assert claimed is not None and claimed.status == "running", (
                    f"attempt {attempt_num}: verifier must be claimable again "
                    "after a retry put it back in the queue"
                )
                retried = kb.verifier_execution_crash_retry(
                    conn, verifier, expected_run_id=claimed.current_run_id,
                    reason=reason, infrastructure=False,
                )
                retried_flags.append(retried)

            # Every attempt except the last stayed below the bound and
            # auto-retried; the final one hit the bound and refused.
            assert retried_flags[:-1] == [True] * (limit - 1)
            assert retried_flags[-1] is False

            task = kb.get_task(conn, verifier)
            assert task.consecutive_failures == limit
            # Caller is expected to call block_task itself once this
            # returns False -- simulate that and confirm it still works
            # (this fix must not interfere with the existing terminal path).
            ok = kb.block_task(
                conn, verifier, reason="Codex verifier failed or timed out.",
                kind="needs_input",
                event_payload_extra={"failure_class": "implementation"},
            )
            assert ok is True
            assert kb.get_task(conn, verifier).status == "blocked"

    def test_infrastructure_crash_never_counts_against_the_budget(
        self, kanban_home, registry,
    ):
        """A control-plane termination (runtime cap, orphan kill, operator
        terminate) is not the verifier's fault and must always retry without
        spending the implementation-crash budget -- mirrors
        ``_record_task_failure``'s own infrastructure carve-out."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            kb.recompute_ready(conn)
            limit = kb.VERIFIER_EXECUTION_CRASH_RETRY_LIMIT

            # A few iterations are enough to demonstrate the counter never
            # moves for infrastructure terminations. Not looped far past the
            # bound: beyond that an unrelated, correct safety net --
            # repeated identical-error detection -- would kick in and is not
            # what this test is about.
            for i in range(limit):
                claimed = kb.claim_task(conn, verifier)
                assert claimed is not None
                retried = kb.verifier_execution_crash_retry(
                    conn, verifier, expected_run_id=claimed.current_run_id,
                    reason="runtime cap exceeded", infrastructure=True,
                )
                assert retried is True

            assert kb.get_task(conn, verifier).consecutive_failures == 0
            assert kb.get_task(conn, verifier).status in ("ready", "review")

    def test_verdict_path_is_never_touched_by_the_retry_helper(
        self, kanban_home, registry,
    ):
        """A PASS/FAIL verdict must complete exactly as before -- this fix
        only inserts itself on the crash (``attempt.ok is False``) branch,
        never on a genuine verdict."""
        with kb.connect_closing() as conn:
            subject = _parked_subject(conn)
            verifier = kb._ensure_independent_verifier_child(
                conn, subject, implementer="alice",
            )
            kb.recompute_ready(conn)
            claimed = kb.claim_task(conn, verifier)
            # What the real codex_verify/claude_verify launcher records on
            # start, before completion -- required for the verdict
            # return-path to recognize this as an attested verifier run.
            with kb.write_txn(conn):
                kb._append_event(
                    conn, verifier, kb._attest_kind(conn, verifier),
                    {"executor": "codex"}, run_id=claimed.current_run_id,
                )
            ok = kb.complete_task(
                conn, verifier,
                summary="ACCEPTANCE: FAIL\nVERDICT: FAIL\nATLAS_VERDICT: FAIL",
                expected_run_id=claimed.current_run_id,
            )
            assert ok is True
            assert kb.get_task(conn, verifier).status == "done"
            # The FAIL verdict routed back to the subject and reopened it
            # for rework (regression evidence will be owed on the next
            # PASS) -- the ordinary, unmodified verdict-return path. This
            # fix touches none of it: it is never invoked when
            # ``attempt.ok`` is true.
            subject_task = kb.get_task(conn, subject)
            assert subject_task.status == "ready"
            assert subject_task.regression_required is True
