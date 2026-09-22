"""Boot-gate authority: what a DEGRADED boot must allow, and what it must never allow.

Two doctrine constraints are under test.

Constraint 14 -- "If a required boot gate is DEGRADED, ordinary task execution is blocked.
Only narrowly scoped recovery of the named failed gate is permitted."

Constraint 15 -- "Every binding boot gate must fail closed for ordinary work and remain
mechanically recoverable through a distinct recovery-only executor path. The recovery
executor must not require the failed gate to be green before it can repair that same gate."

The 2026-09-21 boot control-plane redesign (commit c87f2ad84e, "Simplify boot control
plane") satisfied the first and broke the second. ``recovery_only`` carried a
``proc.returncode == 0`` term, and ``norcal/boot/claude-boot-context`` exits 1 on every
DEGRADED boot -- so ``recovery_only`` could only be true when ``complete`` was already
true, making the ``recovery_only and not complete`` branch unreachable and leaving a
degraded session with the HARD GATE and no way out. Recovery authority was granted
(``recovery_authority_reason: "authorized recovery launcher"``) and then discarded.

The same commit introduced the ``NORCAL_RECOVERY_AUTHORIZED=1`` launcher grant, which
short-circuits ``validate_recovery_task`` before it reads Kanban. That short-circuit is
intentional and necessary -- the board can be the failed gate, and constraint 15 forbids
making recovery depend on it -- so these tests pin its *boundary* rather than its absence:
it may skip the Kanban lookup, and nothing else.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

BOOT_SRC = Path(__file__).resolve().parents[2] / "norcal" / "boot"
GRANT = {"NORCAL_RECOVERY_AUTHORIZED": "1", "NORCAL_RECOVERY_TASK_ID": "t_repair"}


def _recovery_boot():
    path = BOOT_SRC / "recovery_boot.py"
    spec = importlib.util.spec_from_file_location("norcal_recovery_boot_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Constraint 15: a DEGRADED boot must stay recoverable through the named gate.
# ---------------------------------------------------------------------------

def test_degraded_boot_grants_recovery_only_and_names_the_failed_gate(boot):
    """The regression test for the unreachable recovery branch.

    Before the fix this produced ``recovery_only: false`` with ``boot_returncode: 1``
    and a HARD GATE, despite recovery authority having been granted.
    """
    r = boot.run_gate("sid-degraded-recovery", mode="degraded", env=GRANT)

    assert r.state["boot_returncode"] == 1, "the wrapper must still report the failed gate"
    assert r.state["recovery_only"] is True
    assert r.state["recovery_task_id"] == "t_repair"
    assert r.recovery_only_block is True
    assert r.hard_gate_block is False

    # The gate must be *named*, so the recovery session has a scope.
    assert r.named_gates == ["shared boot state is DEGRADED"]
    assert r.state["failed_gates"] == ["shared boot state is DEGRADED"]


def test_recovery_authority_survives_every_named_gate_failure(boot):
    """Recovery must not get rarer as the boot gets worse."""
    r = boot.run_gate("sid-two-failures", mode="two_failures", env=GRANT)
    assert r.state["recovery_only"] is True
    assert r.state["complete"] is False
    assert r.state["failed_gates"] == [
        "shared boot state is DEGRADED",
        "canonical CLAUDE.md is missing or unreadable",
    ]
    assert r.named_gates == ["shared boot state is DEGRADED; canonical CLAUDE.md is missing or unreadable"]


def test_recovery_only_session_may_use_tools(boot):
    """The recovery lane's authority has to survive as far as the tool guard."""
    boot.write_card("t_repair")
    boot.run_gate("sid-recovery-tools", mode="degraded", env=GRANT)
    assert boot.run_guard("sid-recovery-tools", env=GRANT).allowed is True


# ---------------------------------------------------------------------------
# Constraint 14: ordinary work fails closed, at both the gate and the tool guard.
# ---------------------------------------------------------------------------

def test_degraded_boot_fails_closed_for_ordinary_lanes(boot):
    r = boot.run_gate("sid-degraded-ordinary", mode="degraded")
    assert r.state["complete"] is False
    assert r.state["recovery_only"] is False
    assert r.state["recovery_task_id"] is None
    assert r.hard_gate_block is True
    assert r.recovery_only_block is False
    # Even a closed gate must say which gate closed.
    assert r.named_gates == ["shared boot state is DEGRADED"]

    assert boot.run_guard("sid-degraded-ordinary").denied is True


def test_complete_boot_allows_ordinary_work_and_names_nothing(boot):
    r = boot.run_gate("sid-complete", mode="complete")
    assert r.state["complete"] is True
    assert r.state["failed_gates"] == []
    assert r.hard_gate_block is False
    assert r.recovery_only_block is False
    assert boot.run_guard("sid-complete").allowed is True


def test_tool_guard_denies_a_session_with_no_gate_record(boot):
    assert boot.run_guard("sid-never-booted").denied is True


# ---------------------------------------------------------------------------
# The NORCAL_RECOVERY_AUTHORIZED boundary: it may skip Kanban, and nothing else.
# ---------------------------------------------------------------------------

def test_launcher_grant_never_confers_ordinary_task_authority(boot):
    """The whole point of a recovery lane is that it is not ordinary authority."""
    r = boot.run_gate("sid-grant-not-ordinary", mode="degraded", env=GRANT)
    assert r.state["complete"] is False
    assert r.state["claude_boot_complete"] is False
    assert r.state["shared_boot_complete"] is False
    # Recovery-scoped, and explicitly told so.
    assert "Normal execution remains blocked" in r.ctx


def test_launcher_grant_still_requires_a_named_card(boot):
    """A grant with no card id names no gate, so it authorizes nothing."""
    for task_id in ("", "   "):
        r = boot.run_gate(
            f"sid-grant-empty-{len(task_id)}",
            mode="degraded",
            env={"NORCAL_RECOVERY_AUTHORIZED": "1", "NORCAL_RECOVERY_TASK_ID": task_id},
        )
        assert r.state["recovery_only"] is False
        assert r.state["recovery_task_id"] is None
        assert r.hard_gate_block is True


@pytest.mark.parametrize("value", ["", "0", "2", "true", "TRUE", "yes", "on", "1 1", "01", "one"])
def test_launcher_grant_only_honours_the_exact_value_1(value, boot):
    """Anything but ``1`` must fall through to Kanban validation, which rejects the card.

    The card here is deliberately in the ordinary ``claude`` lane, so a grant that leaked
    through on a truthy-looking value would be visible as recovery authority over an
    ordinary task.
    """
    boot.write_card("t_repair", lane="claude")
    r = boot.run_gate(
        f"sid-grant-{abs(hash(value))}",
        mode="degraded",
        env={"NORCAL_RECOVERY_AUTHORIZED": value, "NORCAL_RECOVERY_TASK_ID": "t_repair"},
    )
    assert r.state["recovery_only"] is False
    assert "not in claude_recovery lane" in r.state["recovery_authority_reason"]


def test_launcher_grant_does_not_bypass_the_shared_constraint_payload(boot):
    """Recovery still requires evidence the shared doctrine block was actually loaded."""
    r = boot.run_gate("sid-grant-no-shared", mode="no_shared", env=GRANT)
    assert r.state["recovery_only"] is False
    assert r.hard_gate_block is True


def test_launcher_grant_does_not_bypass_claude_rule_synchronisation(boot):
    """A session holding stale role rules is not made safe by a recovery grant."""
    r = boot.run_gate("sid-grant-stale-rules", mode="no_rules", env=GRANT)
    assert r.state["recovery_only"] is False
    assert r.hard_gate_block is True


def test_launcher_grant_does_not_survive_a_wrapper_that_never_ran(boot):
    """No payload means no evidence of anything, grant or not."""
    r = boot.run_gate("sid-grant-crash", mode="crash", env=GRANT)
    assert r.state["boot_wrapper_ran"] is True  # the wrapper ran and exited 3
    assert r.state["recovery_only"] is False
    assert r.hard_gate_block is True


def test_launcher_grant_cannot_promote_an_ordinary_session_at_tool_time(boot):
    """The tool guard reads recovery status from the state file, not from its own env.

    Otherwise a degraded ordinary session could export the variable and unblock itself.
    """
    boot.run_gate("sid-ordinary-then-grant", mode="degraded")  # booted WITHOUT the grant
    assert boot.run_guard("sid-ordinary-then-grant", env=GRANT).denied is True


def test_tool_guard_ignores_an_environment_supplied_gate_record(boot, tmp_path):
    """A session must not be able to point the guard at a gate record it wrote itself.

    The decoy directory holds a forged ``complete: true`` record for this session. Every
    plausible override name is exported at once; the guard must still deny, because it
    resolves the record location itself.
    """
    sid = "sid-forged-record"
    decoy = tmp_path / "decoy-gates"
    boot.forge_state(decoy, sid)

    overrides = {name: str(decoy) for name in (
        "NORCAL_SESSION_GATE_STATE_DIR", "STATE_DIR", "SESSION_GATES_DIR",
        "CLAUDE_SESSION_GATES", "NORCAL_STATE_DIR",
    )}
    assert boot.run_guard(sid, env=overrides).denied is True

    # Positive control: the identical record in the directory the guard *does* read
    # allows the call, so the forgery above failed on location, not on content.
    boot.forge_state(boot.state_dir, sid)
    assert boot.run_guard(sid).allowed is True


# ---------------------------------------------------------------------------
# The Kanban fallback still validates lane, status and gate when no grant is present.
# ---------------------------------------------------------------------------

def test_kanban_fallback_enforces_lane_status_and_gate(boot, monkeypatch):
    monkeypatch.delenv("NORCAL_RECOVERY_AUTHORIZED", raising=False)
    rb = _recovery_boot()

    boot.write_card("t_repair")
    assert rb.validate_recovery_task("t_repair", boot.db_path)[0] is True

    for kwargs, expected in (
        ({"lane": "claude"}, "not in claude_recovery lane"),
        ({"status": "done"}, "is not actively running"),
        ({"run_id": None}, "is not actively running"),
        ({"gate": ""}, "has no deterministic gate"),
    ):
        boot.write_card("t_repair", **kwargs)
        ok, reason = rb.validate_recovery_task("t_repair", boot.db_path)
        assert ok is False, f"{kwargs} must not authorize recovery"
        assert expected in reason

    assert rb.validate_recovery_task("t_absent", boot.db_path)[0] is False
    assert rb.validate_recovery_task("", boot.db_path)[0] is False


def test_launcher_grant_is_the_only_path_that_skips_the_database(boot, monkeypatch):
    """Pin the intentional exception itself: unreachable DB must fail without a grant."""
    rb = _recovery_boot()
    missing = boot.db_path.parent / "no-such-board.db"

    monkeypatch.delenv("NORCAL_RECOVERY_AUTHORIZED", raising=False)
    ok, reason = rb.validate_recovery_task("t_repair", missing)
    assert ok is False and "database unavailable" in reason

    # This is why the grant exists: the board itself can be the failed gate.
    monkeypatch.setenv("NORCAL_RECOVERY_AUTHORIZED", "1")
    ok, reason = rb.validate_recovery_task("t_repair", missing)
    assert ok is True and reason == "authorized recovery launcher"
