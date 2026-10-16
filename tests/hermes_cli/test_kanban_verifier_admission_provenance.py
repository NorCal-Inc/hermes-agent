"""2026-10-15 gate audit (Christopher: fix it, full authority).

1. A control-plane subject admitted under a named defect/regression/requirement
   got a verifier with no authority, so admission refused the verifier the
   verification gate demands (157 refusals 2026-09-24..10-15). Its verifier now
   carries ``requirement:independent-verification:<subject>``, never operator:.
2. The verifier card took its provenance from the caller's env; the gateway has
   no governed-run env, so 221 automated verifier cards in 21 days were
   recorded human_interactive/manual_relay, which is_human_relayed() and
   task_admission() treat as delegated operator authority. Now: system.
3. The respawn guard wrote one identical event per card per tick (1,572 rows in
   three days on three cards).
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_verifier_brief_admission import (  # noqa: F401
    _attach_evidence,
    _auto,
    _failed,
    _human,
    automated_home,
)


@pytest.fixture
def gateway_env(automated_home, monkeypatch):
    """The gateway dispatcher: no governed-run env and no declared kind."""
    monkeypatch.delenv(kb.ENV_ACTOR_KIND, raising=False)
    monkeypatch.delenv(kb.ENV_ACTOR_ID, raising=False)
    return automated_home


def _row(conn, tid):
    return conn.execute(
        "SELECT control_plane, control_plane_authority, actor_kind, creation_cause "
        "FROM tasks WHERE id=?", (tid,),
    ).fetchone()


def _defect_subject(conn):
    tid = kb.create_task(
        conn, title="Repair Gauntlet verifier return paths",
        assignee="default", provenance=_auto(),
        control_plane_authority="defect:verifier t_x recorded 24/26 regression",
    )
    _attach_evidence(conn, tid)
    return tid


class TestDefectAdmittedSubject:
    def test_verifier_is_admitted_on_first_try(self, automated_home):
        with kb.connect_closing() as conn:
            tid = _defect_subject(conn)
            child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
            assert child is not None
            assert _failed(conn, tid) == 0
            row = _row(conn, child)
            assert row["control_plane"] == 1
            assert row["control_plane_authority"] == f"requirement:independent-verification:{tid}"

    def test_inherited_authority_delegates_no_operator_power(self, automated_home):
        with kb.connect_closing() as conn:
            tid = _defect_subject(conn)
            child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
            assert not kb.task_admission(conn, child)["control_plane_authority"].startswith("operator:")
            assert kb.is_human_relayed(conn, child) is False

    def test_subject_with_no_recorded_authority_is_still_refused(self, automated_home):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Update shared boot consumer checkpoint pin",
                assignee="default", provenance=_human(),
            )
            _attach_evidence(conn, tid)
            # A legacy automated control-plane card admitted before admission
            # control existed: no authority, not human-relayed.
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET actor_kind=?, control_plane_authority=NULL WHERE id=?",
                    (kb.ACTOR_KIND_GOVERNED_AUTOMATION, tid),
                )
            assert kb._ensure_independent_verifier_child(conn, tid, implementer="default") is None
            assert _failed(conn, tid) == 1

    def test_new_automated_control_plane_work_is_still_refused(self, automated_home):
        with kb.connect_closing() as conn:
            with pytest.raises(kb.ControlPlaneAdmissionError):
                kb.create_task(
                    conn, title="Repair Gauntlet verifier return paths",
                    assignee="default", provenance=_auto(),
                )


class TestVerifierProvenance:
    def test_gateway_created_verifier_is_system_not_human(self, gateway_env):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Draft NCASS monthly research summary",
                assignee="default", provenance=_auto(),
            )
            _attach_evidence(conn, tid)
            # Sanity: in this env the old default really was human.
            assert kb.resolve_actor_provenance().kind == kb.ACTOR_KIND_HUMAN_INTERACTIVE
            child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
            row = _row(conn, child)
            assert row["actor_kind"] == kb.ACTOR_KIND_SYSTEM
            assert row["creation_cause"] == kb.CREATION_CAUSE_AUTOMATED
            assert kb.is_human_relayed(conn, child) is False

    def test_human_subject_verifier_keeps_operator_parent_authority(self, gateway_env):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="Update shared boot consumer checkpoint pin",
                assignee="default", provenance=_human(),
            )
            _attach_evidence(conn, tid)
            child = kb._ensure_independent_verifier_child(conn, tid, implementer="default")
            row = _row(conn, child)
            assert row["control_plane_authority"] == f"operator:human-interactive-parent:{tid}"
            assert row["actor_kind"] == kb.ACTOR_KIND_SYSTEM


class TestRespawnGuardChatter:
    def _count(self, conn, tid):
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='respawn_guarded'",
            (tid,),
        ).fetchone()[0]

    def test_same_reason_recorded_once_per_window(self, automated_home, monkeypatch):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="verify", assignee="default", provenance=_auto())
            real = time.time
            for minute in range(0, 29, 5):   # loop test: 6 ticks inside the window
                monkeypatch.setattr(time, "time", lambda m=minute: real() + m * 60)
                kb._record_respawn_guarded(conn, tid, "quota_reset_hold")
            assert self._count(conn, tid) == 1
            kb._record_respawn_guarded(conn, tid, "rate_limit_cooldown")
            assert self._count(conn, tid) == 2          # a new reason is news
            monkeypatch.setattr(time, "time", lambda: real() + 3 * 3600)
            kb._record_respawn_guarded(conn, tid, "rate_limit_cooldown")
            assert self._count(conn, tid) == 3          # still guarded hours later
