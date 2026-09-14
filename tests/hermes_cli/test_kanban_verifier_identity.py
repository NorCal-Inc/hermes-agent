"""Verifier identity: registry validation, anonymous verdicts, and independence.

Refusing ``verifier == implementer`` is necessary but was never sufficient.
Three holes around that guard were all exercised on the live board, and this
module pins each one shut (defect D4 remainder, evidence packet for
``t_db0af7e0``):

(a) REGISTRY — the verifier string was checked against nothing at all. Any
    token that merely DIFFERED from the implementer rendered a binding verdict.
    That is how the unregistered identity ``chatgpt-systems`` closed
    ``t_023d2af6`` and ``t_616527d7`` (a live Logos Covenant production
    deployment). A verdict signed by an identity the system cannot resolve is
    not attributable, so it cannot be shown to be independent of anyone.

(b) ANONYMOUS — a verifier of ``None`` skipped the comparison entirely, because
    the guard read ``if verifier and implementer and ...``. "Pass no verifier"
    was a complete bypass of the independence check.

(c) ASSIGNEE — independence was tested against the ``review_requested``
    implementer only, never against the task's own ``assignee``.

The registry check is deliberately not a bare ``profile_exists`` call:
``codex_verify`` is an executor LANE, not a profile directory, and the
codex_verify return path signs verdicts as ``codex_verify:<verifier_task_id>``.
A lane NAME is still not an identity — see
``TestBareLaneNameIsNotAVerifierIdentity`` (the t_e48487e5 false VERIFIED).

The assignee arm of (c) carries a carve-out that is load-bearing rather than a
softening: the sanctioned independence recovery is to REASSIGN a parked task to
an independent reviewer, after which the reviewer legitimately IS the assignee.
``complete_task``'s implicit approval path even derives its reviewer identity
FROM ``tasks.assignee``, so a flat ``verifier != assignee`` rule would refuse
every review-run approval on the board — including the correct recovery run on
``t_4edb5874``. Both shapes are pinned below.
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


@pytest.fixture
def registry(monkeypatch):
    """Control which names count as existing Hermes profiles.

    Returns a setter; the default registry is ``{"default", "alice", "bob"}``.
    Patching the module attribute (rather than the imported name) is what the
    production code reads: ``_verifier_identity_status`` imports
    ``profile_exists`` at call time precisely so it stays patchable.
    """
    from hermes_cli import profiles

    known = {"default", "alice", "bob"}

    def _set(*names):
        known.clear()
        known.update(names)

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name in known)
    return _set


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = []
    for r in rows:
        if kind is not None and r["kind"] != kind:
            continue
        try:
            payload = json.loads(r["payload"]) if r["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        out.append((r["kind"], payload))
    return out


def _parked_for_review(conn, *, implementer="alice", reviewer=None):
    """Drive a gauntlet task to VERIFICATION_PENDING. Returns the task id."""
    tid = kb.create_task(
        conn, title="implementation", assignee=implementer, gauntlet=True,
    )
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None and claimed.status == "running"
    assert kb.request_review(
        conn, tid, summary="implemented X", reviewer=reviewer,
        expected_run_id=claimed.current_run_id,
    ) is True
    assert kb.get_task(conn, tid).verification_state == kb.VERIFICATION_PENDING
    return tid


# ---------------------------------------------------------------------------
# (a) The verifier identity must resolve against the registry
# ---------------------------------------------------------------------------

class TestUnregisteredVerifierIsRefused:
    def test_unregistered_identity_cannot_render_a_verdict(
        self, kanban_home, registry
    ):
        """The live ``chatgpt-systems`` case, reproduced.

        The identity is not the implementer, so every pre-existing guard let it
        through — and it closed a production deployment card.
        """
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="chatgpt-systems",
            )
            assert ok is False
            assert "not a registered identity" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_refusal_is_auditable(self, kanban_home, registry):
        """A refusal that leaves no event is a refusal the board cannot act on."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn)
            kb.record_verification(
                conn, tid, passed=True, verifier="chatgpt-systems",
            )
            blocked = _events(conn, tid, "verification_blocked_unknown_verifier")
            assert len(blocked) == 1
            payload = blocked[0][1]
            assert payload["verifier"] == "chatgpt-systems"
            assert payload["source"] == "record_verification"
            assert payload["detail"]

    def test_a_failing_verdict_from_an_unknown_identity_is_refused_too(
        self, kanban_home, registry
    ):
        """A FAIL is not harmless: it routes the task back and arms the
        regression gate. An unattributable identity may not do that either."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn)
            ok, _ = kb.record_verification(
                conn, tid, passed=False, verifier="chatgpt-systems",
                reason="looks wrong to me",
            )
            assert ok is False
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_registered_profile_verifies_normally(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="bob",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)


def _attested_verifier(conn, subject_id, *, attest=True, parents=None):
    """Run a codex_verify card to completion the way the real lane does.

    ``attest`` writes the ``codex_verifier_started`` event that
    ``recovery_lane._claim_codex_verifier_attempt`` records for the run it
    actually executes; leaving it out reproduces an unscoped agent completing
    the card. The summary carries no verdict line, so the return path records
    nothing and the verdict under test is the explicit one.
    """
    if not kb.list_attachments(conn, subject_id):
        kb.add_attachment(
            conn, subject_id, filename="EVIDENCE.md",
            stored_path=f"/tmp/{subject_id}/EVIDENCE.md", size=64,
            uploaded_by="claude-lane",
        )
    cid = kb.create_task(
        conn, title="independent codex verification", assignee="default",
        executor_lane=kb.EXECUTOR_LANE_CODEX_VERIFY,
        parents=list(parents) if parents is not None else [subject_id],
        gauntlet=True,
    )
    kb.recompute_ready(conn)
    claimed = kb.claim_task(conn, cid)
    assert claimed is not None and claimed.status == "running"
    if attest:
        with kb.write_txn(conn):
            kb._append_event(
                conn, cid, "codex_verifier_started", {"executor": "codex"},
                run_id=claimed.current_run_id,
            )
    assert kb.complete_task(
        conn, cid, summary="fixture verifier run without a verdict line",
        expected_run_id=claimed.current_run_id,
    ) is True
    assert kb.get_task(conn, cid).status == "done"
    return cid


class TestBareLaneNameIsNotAVerifierIdentity:
    """Regression for the t_e48487e5 false VERIFIED (2026-09-14 12:19:13).

    The allow-list used to match the bare lane root, so typing a lane name
    rendered a binding "independent" verdict with no verifier having run.
    A lane name is not an identity: only an attested codex_verify verifier
    bound to the subject may sign.
    """

    @pytest.mark.parametrize(
        "verifier",
        [
            "codex_verify",
            "atlas",
            "claude",
            "claude_recovery",
            "codex_verify:",
            "codex_verify:t_fb23ac0a",  # scoped, but resolves to nothing
            "claude:t_fb23ac0a",
            "atlas:t_fb23ac0a",
        ],
    )
    def test_lane_names_and_unresolvable_lane_identities_are_refused(
        self, kanban_home, registry, verifier
    ):
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier=verifier,
            )
            assert ok is False, detail
            assert "not a registered identity" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )
            blocked = _events(conn, tid, "verification_blocked_unknown_verifier")
            assert [p["verifier"] for _, p in blocked] == [verifier.strip().lower()]

    def test_the_t_e48487e5_sequence_cannot_close_the_task(
        self, kanban_home, registry
    ):
        """Refused as an unknown identity, then retried with the lane name."""
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            first, _ = kb.record_verification(
                conn, tid, passed=True, verifier="chatgpt-auditor",
            )
            second, detail = kb.record_verification(
                conn, tid, passed=True, verifier="codex_verify",
            )
            assert (first, second) == (False, False)
            assert "bare lane name" in detail
            task = kb.get_task(conn, tid)
            assert task.status == "review"
            assert task.verification_state == kb.VERIFICATION_PENDING
            assert not _events(conn, tid, "verification_passed")
            assert len(_events(conn, tid, "verification_blocked_unknown_verifier")) == 2

    def test_attested_linked_verifier_signs_normally(self, kanban_home, registry):
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            cid = _attested_verifier(conn, tid)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier=f"codex_verify:{cid}",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED), detail

    def test_unattested_verifier_card_cannot_sign(self, kanban_home, registry):
        """The run-2817 shape: a codex_verify card completed by something
        other than the codex_verify lane."""
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            cid = _attested_verifier(conn, tid, attest=False)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier=f"codex_verify:{cid}",
            )
            assert ok is False
            assert "executed by the codex_verify lane" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_attested_verifier_of_another_subject_cannot_sign(
        self, kanban_home, registry
    ):
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            other = _parked_for_review(conn, implementer="alice")
            cid = _attested_verifier(conn, other)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier=f"codex_verify:{cid}",
            )
            assert ok is False
            assert f"is not a verifier of {tid}" in detail

    def test_completed_codex_verify_execution_on_the_subject_signs(
        self, kanban_home, registry
    ):
        registry("default", "alice")
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            other = _parked_for_review(conn, implementer="alice")

            def _execution(eid, task_id, *, command_class="codex.verify",
                           status="completed", exit_code=0):
                with kb.write_txn(conn):
                    conn.execute(
                        "INSERT INTO executions (id, task_id, executor_type, "
                        "command_class, cwd, nonce, controller_token, ownership, "
                        "started_at, heartbeat_at, status, exit_code, created_at) "
                        "VALUES (?, ?, 'codex', ?, '/tmp', 'n', 'tok', "
                        "'supervisor', 1, 1, ?, ?, 1)",
                        (eid, task_id, command_class, status, exit_code),
                    )

            _execution("x_foreign", other)
            _execution("x_failed", tid, status="failed", exit_code=1)
            _execution("x_good", tid)
            for bad in ("x_foreign", "x_failed"):
                ok, _ = kb.record_verification(
                    conn, tid, passed=True, verifier=f"codex_verify:{bad}",
                )
                assert ok is False
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="codex_verify:x_good",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED), detail

    def test_lane_identity_without_a_subject_is_unknown(self):
        status, detail = kb._verifier_identity_status("codex_verify:t_30e58898")
        assert status == kb.VERIFIER_IDENTITY_UNKNOWN
        assert "cannot be resolved" in detail

    def test_a_lookalike_prefix_is_not_a_lane(self, registry):
        status, detail = kb._verifier_identity_status("codex_verifyish")
        assert status == kb.VERIFIER_IDENTITY_UNKNOWN
        assert "codex_verify" in detail  # the message names the real lane


class TestUnreadableRegistry:
    """An unreadable registry must not become a false accusation OR a free pass."""

    def test_lookup_failure_is_recorded_but_does_not_refuse(
        self, kanban_home, monkeypatch
    ):
        from hermes_cli import profiles

        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="default")

            def _boom(name):
                raise OSError("profiles root is unreadable")

            monkeypatch.setattr(profiles, "profile_exists", _boom)
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="somebody-else",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED)
            unchecked = _events(conn, tid, "verifier_identity_unchecked")
            assert len(unchecked) == 1
            assert unchecked[0][1]["verifier"] == "somebody-else"


# ---------------------------------------------------------------------------
# (b) An anonymous verdict fails closed
# ---------------------------------------------------------------------------

class TestAnonymousVerdict:
    def test_no_verifier_is_refused(self, kanban_home, registry):
        """This used to be a documented concession to "untracked/legacy
        callers" — and therefore a complete bypass of the whole guard."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            ok, detail = kb.record_verification(conn, tid, passed=True)
            assert ok is False
            assert "requires a named verifier" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_no_verifier_refusal_is_auditable(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            kb.record_verification(conn, tid, passed=True)
            blocked = _events(conn, tid, "verification_blocked_unknown_verifier")
            assert len(blocked) == 1
            assert blocked[0][1]["verifier"] is None

    def test_the_bypass_cannot_be_used_to_bless_the_implementers_own_work(
        self, kanban_home, registry
    ):
        """The exact escalation the hole allowed: name yourself and you are
        refused, name nobody and the same verdict landed."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            named_ok, _ = kb.record_verification(
                conn, tid, passed=True, verifier="alice",
            )
            anon_ok, _ = kb.record_verification(conn, tid, passed=True)
            assert named_ok is False and anon_ok is False
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )


# ---------------------------------------------------------------------------
# (c) Independence is tested against the assignee as well as the implementer
# ---------------------------------------------------------------------------

class TestIndependenceAgainstAssignee:
    def test_assignee_cannot_verify_when_never_installed_as_reviewer(
        self, kanban_home, registry
    ):
        """``review_requested`` recorded someone else as implementer, but the
        row's assignee is the verifier and nothing auditable ever put them in
        the reviewer seat."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            # Rewrite the handoff's implementer so the implementer arm of the
            # guard cannot fire — this is the shape a stale or mis-recorded
            # handoff leaves behind, and previously nothing else checked.
            with kb.write_txn(conn):
                row = conn.execute(
                    "SELECT id, payload FROM task_events WHERE task_id = ? "
                    "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                payload = json.loads(row["payload"])
                payload["implementer"] = "bob"
                conn.execute(
                    "UPDATE task_events SET payload = ? WHERE id = ?",
                    (json.dumps(payload), row["id"]),
                )
            assert kb.get_task(conn, tid).assignee == "alice"

            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="alice",
            )
            assert ok is False
            assert "task's own assignee" in detail
            assert kb.get_task(conn, tid).verification_state == (
                kb.VERIFICATION_PENDING
            )

    def test_refusal_names_the_assignee_as_the_conflict_source(
        self, kanban_home, registry
    ):
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            with kb.write_txn(conn):
                row = conn.execute(
                    "SELECT id, payload FROM task_events WHERE task_id = ? "
                    "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                payload = json.loads(row["payload"])
                payload["implementer"] = "bob"
                conn.execute(
                    "UPDATE task_events SET payload = ? WHERE id = ?",
                    (json.dumps(payload), row["id"]),
                )
            kb.record_verification(conn, tid, passed=True, verifier="alice")
            blocked = _events(conn, tid, "verification_blocked_self_review")
            assert blocked, "refusal must leave the durable event the board reads"
            assert blocked[-1][1]["conflict_source"] == "assignee"
            assert blocked[-1][1]["conflict_identity"] == "alice"

    def test_reviewer_installed_by_request_review_may_verify(
        self, kanban_home, registry
    ):
        """The primary sanctioned handoff: ``request_review(reviewer=B)``
        reassigns the row to B in the same transaction and emits no separate
        ``assigned`` event. B is the assignee AND the legitimate verifier."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice", reviewer="bob")
            assert kb.get_task(conn, tid).assignee == "bob"
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="bob",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED), detail

    def test_reviewer_installed_by_recovery_reassignment_may_verify(
        self, kanban_home, registry
    ):
        """The t_4edb5874 recovery shape, 2026-09-03: 'default' was correctly
        blocked from reviewing itself, so the parked task was reclaimed and
        reassigned to an independent reviewer, who then verified it. That is
        the CORRECT outcome and a flat assignee comparison would have refused
        it."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            assert kb.assign_task(conn, tid, "bob") is True
            assert kb.get_task(conn, tid).assignee == "bob"
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="bob",
            )
            assert (ok, detail) == (True, kb.VERIFICATION_VERIFIED), detail

    def test_implementer_arm_still_fires_and_keeps_its_wording(
        self, kanban_home, registry
    ):
        """The pre-existing guard is untouched: this change only ADDS refusals."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            ok, detail = kb.record_verification(
                conn, tid, passed=True, verifier="alice",
            )
            assert ok is False
            assert "matches the implementer" in detail


# ---------------------------------------------------------------------------
# The implicit review-run approval path carries the same three gates
# ---------------------------------------------------------------------------

class TestReviewRunApprovalPath:
    def test_unregistered_reviewer_cannot_approve_inline(
        self, kanban_home, registry
    ):
        """``complete_task``'s implicit path derives its reviewer from
        ``tasks.assignee``. Before this change it applied no registry check at
        all, so an unregistered assignee could bless the task by completing
        it."""
        registry("default", "alice")  # 'ghost' is deliberately absent
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            # Put an unregistered identity in the reviewer seat the way an
            # unvalidated write path would, then let it claim the review run.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = 'ghost' WHERE id = ?", (tid,),
                )
            # Selection is now stricter than the historical inline-verdict
            # guard: an identity that was never installed for this phase is
            # rejected before it can open a review run.
            assert kb.claim_review_task(conn, tid) is None
            assert kb.get_task(conn, tid).status == "review"
            refused = _events(conn, tid, "review_claim_rejected_self_review")
            assert len(refused) == 1
            assert refused[0][1]["candidate"] == "ghost"
            assert refused[0][1]["conflict_source"] == "assignee"

    def test_assignee_never_installed_as_reviewer_cannot_approve_inline(
        self, kanban_home, registry
    ):
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice")
            # A registered profile arrives in `assignee` with no auditable
            # reviewer installation behind it (the shape a dispatcher-side
            # rewrite or a direct row edit leaves).
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET assignee = 'bob' WHERE id = ?", (tid,),
                )
            assert kb.claim_review_task(conn, tid) is None
            assert kb.get_task(conn, tid).status == "review"
            refused = _events(conn, tid, "review_claim_rejected_self_review")
            assert refused
            assert refused[-1][1]["candidate"] == "bob"
            assert refused[-1][1]["conflict_source"] == "assignee"

    def test_properly_installed_reviewer_still_approves_inline(
        self, kanban_home, registry
    ):
        """The path has to keep working — it is how the review lane records
        the overwhelming majority of its verdicts."""
        with kb.connect_closing() as conn:
            tid = _parked_for_review(conn, implementer="alice", reviewer="bob")
            assert kb.claim_review_task(conn, tid) is not None

            assert kb.complete_task(conn, tid, summary="approved") is True
            task = kb.get_task(conn, tid)
            assert task.status == "done"
            assert task.verification_state == kb.VERIFICATION_VERIFIED
            passed = _events(conn, tid, "verification_passed")
            assert passed[-1][1]["verifier"] == "bob"


# ---------------------------------------------------------------------------
# Unit coverage for the classifier itself
# ---------------------------------------------------------------------------

class TestVerifierIdentityStatus:
    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_is_unknown(self, value):
        status, _ = kb._verifier_identity_status(value)
        assert status == kb.VERIFIER_IDENTITY_UNKNOWN

    def test_existing_profile_is_a_profile(self, registry):
        status, detail = kb._verifier_identity_status("alice")
        assert (status, detail) == (kb.VERIFIER_IDENTITY_PROFILE, "alice")

    def test_missing_profile_is_unknown(self, registry):
        status, _ = kb._verifier_identity_status("nobody")
        assert status == kb.VERIFIER_IDENTITY_UNKNOWN

    def test_import_failure_is_unchecked_not_unknown(self, monkeypatch):
        """Distinguishing "we could not look" from "we looked and it is not
        there" is the whole point — one is a gap to record, the other is an
        accusation to act on."""
        from hermes_cli import profiles

        def _boom(name):
            raise RuntimeError("registry offline")

        monkeypatch.setattr(profiles, "profile_exists", _boom)
        status, detail = kb._verifier_identity_status("alice")
        assert status == kb.VERIFIER_IDENTITY_UNCHECKED
        assert "registry offline" in detail

    def test_only_the_independent_verification_lane_may_sign(self):
        """Implementation and recovery lanes are never verifier identities."""
        assert kb.VERIFIER_LANE_IDENTITIES == frozenset({kb.EXECUTOR_LANE_CODEX_VERIFY})
        assert kb.EXECUTOR_LANE_CLAUDE not in kb.VERIFIER_LANE_IDENTITIES
        assert kb.EXECUTOR_LANE_CLAUDE_RECOVERY not in kb.VERIFIER_LANE_IDENTITIES
