"""Fix (b), incident 2026-10-12: a verifier of the card that caused the light
finding is named as the recovery task, so it is not refused by the not-ready
boot that its own subject caused (boot constraint 15)."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import recovery_lane

# Real values from the incident (state.json fingerprint ed9bbac9931c63db).
SUBJECT = "t_0e17b647"
RESOLVED = "2026-10-12T20:28:27.568520+00:00"
RESOLVED_TS = recovery_lane._iso_ts(RESOLVED)


@pytest.fixture
def health(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    d = home / "state" / "system-health-controller"
    d.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        kb, "relation_targets",
        lambda conn, tid, rel: [SUBJECT] if rel == kb.RELATION_VERIFIES and tid == "t_verify" else [],
    )

    def write(status, resolved_at, beat_ts, tier="light", subject=SUBJECT):
        rec = {"tier": tier, "subject": subject, "status": status, "resolved_at": resolved_at,
               "invariant": "verifier_route_open", "signature": "no_route:evidence_missing"}
        (d / "state.json").write_text(json.dumps({"version": 1, "fingerprints": {"ed9bbac9931c63db": rec}}))
        (d / "heartbeat-light.json").write_text(json.dumps({"finished_ts": beat_ts, "status": "ESCALATED"}))
    return write


def test_incident_replay_resolved_after_stale_heartbeat_names_recovery(health):
    # t_303041ae: heartbeat 295 s old, finding cleared moments after.
    health("resolved", RESOLVED, RESOLVED_TS - 295)
    scope = recovery_lane._verifier_recovery_scope(None, "t_verify")
    assert scope.startswith("RECOVERY SCOPE")
    assert "verifier_route_open (no_route:evidence_missing) on t_0e17b647" in scope
    assert "READY:" not in scope  # never mimic live-state syntax (incident 2)


@pytest.mark.parametrize("status", ["open", "escalated", "held"])
def test_unresolved_finding_on_subject_names_recovery(health, status):
    health(status, None, 1.0)
    assert recovery_lane._verifier_recovery_scope(None, "t_verify")


def test_resolved_before_heartbeat_adds_nothing(health):
    # t_69a32f8c: the heartbeat it read was already green.
    health("resolved", RESOLVED, RESOLVED_TS + 60)
    assert recovery_lane._verifier_recovery_scope(None, "t_verify") == ""


def test_other_subject_or_tier_or_card_adds_nothing(health):
    health("escalated", None, 1.0, subject="t_other")
    assert recovery_lane._verifier_recovery_scope(None, "t_verify") == ""
    health("escalated", None, 1.0, tier="deep")
    assert recovery_lane._verifier_recovery_scope(None, "t_verify") == ""
    health("escalated", None, 1.0)
    assert recovery_lane._verifier_recovery_scope(None, "t_unrelated") == ""


def test_missing_or_corrupt_state_fails_closed(health, tmp_path):
    assert recovery_lane._verifier_recovery_scope(None, "t_verify") == ""  # no files yet
    health("escalated", None, 1.0)
    (tmp_path / ".hermes/state/system-health-controller/state.json").write_text("{not json")
    assert recovery_lane._verifier_recovery_scope(None, "t_verify") == ""


def test_prompt_carries_scope_only_when_given():
    task = type("T", (), {"id": "t_v", "title": "x", "tenant": None, "workspace_path": None, "body": "b"})()
    plain = recovery_lane._build_codex_verifier_prompt(task)
    assert "RECOVERY SCOPE" not in plain
    scoped = recovery_lane._build_codex_verifier_prompt(task, "RECOVERY SCOPE (test)")
    assert scoped.splitlines()[2] == "RECOVERY SCOPE (test)"
    assert scoped.replace("RECOVERY SCOPE (test)\n", "") == plain
