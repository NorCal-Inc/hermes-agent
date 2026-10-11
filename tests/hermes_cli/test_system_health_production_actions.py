"""Rule 6 segment 6e: the health controller alerts on refused production actions.

Every scenario runs on an isolated HERMES_HOME board and an isolated controller
state dir. The gate (agent/production_authority_gate.py) records redacted
``production_action_refused`` / ``production_action_unauthorized`` events on
the worker card; the controller must page on a refusal, keep report-only
matches visible without paging, stay silent otherwise, and never touch the card.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

_CONTROLLER_PATH = Path(__file__).resolve().parents[2] / "norcal/health/system_health_controller.py"


def _load_controller():
    name = "norcal_system_health_controller_production_actions_under_test"
    spec = importlib.util.spec_from_file_location(name, _CONTROLLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shc = _load_controller()

COMMAND_TEXT = "stripe charges create --live sk_live_SECRET"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class Clock:
    def __init__(self) -> None:
        self.t = time.time() + 3600

    def __call__(self) -> float:
        return self.t


class Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def __call__(self, subject: str, text: str):
        self.sent.append((subject, text))
        return True, "fake-sender"


def _ctx(home: Path, *, clock=None, alerts=None, config=None, kanban=None):
    # The live model (norcal/health/health-controller.json): governed exceptions, no entries.
    test_config = {
        "kanban_card_invariants": ["company_isolation"],
        "state_model": shc.STATE_MODEL_GOVERNED,
        "governed_exceptions": {
            "temporary": True, "authorized_by": "Christopher, 2026-10-11 (test)",
            "purpose": "segment 6e test", "revert": "delete state_model", "entries": [],
        },
    }
    test_config.update(config or {})
    return shc.Context(
        state_dir=home / "state" / "system-health-controller",
        hermes_home=home,
        config=test_config,
        kanban=kanban or kb.connect_closing,
        send_alert=alerts if alerts is not None else Alerts(),
        run_command=lambda argv, timeout: (0, ""),
        create_card=shc.kanban_repair_card,
        now=clock or Clock(),
    )


def _payload(role="stripe_live", tool="terminal"):
    # Shape of ProductionDecision.event_payload() plus the tool name the gate adds.
    return {
        "source": "production_authority_gate", "tool": tool, "role": role, "surface_role": role,
        "matcher": "stripe_live_key", "executor_lane": "hermes", "registry_state": "ok",
        "registry_mode": "refuse", "authorized": False,
        "reason": "card does not carry production_actions authorization", "action": "refused",
    }


def _event(conn, task_id, kind, created_at, payload=None):
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, ?, ?, ?)",
            (task_id, kind, json.dumps(payload or _payload()), int(created_at)),
        )


def _card(conn, title="worker card"):
    return kb.create_task(conn, title=title, assignee="worker")


def _snapshot(conn, tid):
    return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())


class TestProductionActionsRefused:
    def test_refused_event_in_window_escalates_with_redacted_evidence(self, kanban_home):
        clock = Clock()
        alerts = Alerts()
        with kb.connect_closing() as conn:
            tid = _card(conn)
            _event(conn, tid, shc.PRODUCTION_ACTION_REFUSED_EVENT, clock() - 600)
            _event(conn, tid, shc.PRODUCTION_ACTION_REFUSED_EVENT, clock() - 300)   # same card+role: one fingerprint
            before = _snapshot(conn, tid)
        inv = shc.ProductionActionsRefused()
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts)

        findings = inv.check(ctx)
        assert len(findings) == 1
        (finding,) = findings
        assert finding.subject == tid and finding.signature == "refused:stripe_live" and finding.unsafe
        assert finding.fingerprint == shc.Finding(inv.name, tid, "refused:stripe_live").fingerprint
        assert set(finding.detail) == {"role", "tool", "matcher", "executor_lane", "event_at"}
        assert finding.detail["event_at"] == shc._iso(int(clock() - 300))
        assert COMMAND_TEXT not in json.dumps(finding.detail)
        assert "command" not in json.dumps(finding.detail)

        result = shc.Controller(ctx, [inv]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_ESCALATED
        assert result.escalated == [finding.fingerprint] and result.recovered == []
        assert len(alerts.sent) == 1
        subject, text = alerts.sent[0]
        assert inv.name in subject and tid in text and "reason: unsafe" in text
        assert COMMAND_TEXT not in text and "sk_live" not in text
        with kb.connect_closing() as conn:
            assert _snapshot(conn, tid) == before                       # the card itself is untouched

    def test_refused_event_outside_window_is_silent(self, kanban_home):
        clock = Clock()
        alerts = Alerts()
        with kb.connect_closing() as conn:
            tid = _card(conn)
            _event(conn, tid, shc.PRODUCTION_ACTION_REFUSED_EVENT,
                   clock() - shc.PRODUCTION_ACTION_WINDOW_SECONDS - 60)
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts)
        assert shc.ProductionActionsRefused().check(ctx) == []
        result = shc.Controller(ctx, [shc.ProductionActionsRefused()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_GREEN and alerts.sent == []

    def test_unauthorized_only_is_held_visible_and_never_paged(self, kanban_home):
        clock = Clock()
        alerts = Alerts()
        with kb.connect_closing() as conn:
            tid = _card(conn)
            payload = _payload()
            payload.update({"registry_mode": "report-only", "action": "allowed_report_only"})
            _event(conn, tid, shc.PRODUCTION_ACTION_UNAUTHORIZED_EVENT, clock() - 60, payload)
            before = _snapshot(conn, tid)
        inv = shc.ProductionActionsRefused()
        ctx = _ctx(kanban_home, clock=clock, alerts=alerts)

        (finding,) = inv.check(ctx)
        assert finding.signature == "unauthorized_report_only:stripe_live"
        assert finding.informational and not finding.unsafe
        assert finding.signature != "refused:stripe_live"

        controller = shc.Controller(ctx, [inv])
        result = controller.run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_GREEN_WITH_HOLDS
        assert result.escalated == [] and result.held == [finding.fingerprint]
        assert alerts.sent == []
        rec = shc.StateStore(ctx.state_dir).load()["fingerprints"][finding.fingerprint]
        assert rec["status"] == shc.STATUS_HELD and rec["held_by"] == shc.HELD_INFORMATIONAL
        assert rec["card_id"] is None
        # Later passes: still visible, still never paged.
        again = controller.run(shc.TIER_LIGHT)
        assert again.held == [finding.fingerprint] and alerts.sent == []
        with kb.connect_closing() as conn:
            assert _snapshot(conn, tid) == before
            assert conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key LIKE 'health:%'").fetchone()[0] == 0

    def test_no_events_pass(self, kanban_home):
        alerts = Alerts()
        with kb.connect_closing() as conn:
            _card(conn)
        ctx = _ctx(kanban_home, alerts=alerts)
        assert shc.ProductionActionsRefused().check(ctx) == []
        result = shc.Controller(ctx, [shc.ProductionActionsRefused()]).run(shc.TIER_LIGHT)
        assert result.status == shc.AGGREGATE_GREEN and result.findings == 0 and alerts.sent == []

    def test_unreadable_db_is_a_check_error_like_every_db_invariant(self, kanban_home):
        def broken():
            raise sqlite3.OperationalError("unable to open database file")

        alerts = Alerts()
        ctx = _ctx(kanban_home, alerts=alerts, kanban=broken)
        with pytest.raises(sqlite3.OperationalError):                 # same as SubjectReviewRegressed
            shc.ProductionActionsRefused().check(ctx)
        with pytest.raises(sqlite3.OperationalError):
            shc.SubjectReviewRegressed().check(ctx)
        result = shc.Controller(ctx, [shc.ProductionActionsRefused()]).run(shc.TIER_LIGHT)
        assert len(result.escalated) == 1
        assert "check_error:OperationalError" in alerts.sent[0][1]

    def test_registered_detect_only_and_listed(self, capsys):
        names = [inv.name for inv in shc.default_invariants()]
        assert names.count("production_actions_refused") == 1
        assert "production_actions_refused" in shc.ESCALATE_ONLY_INVARIANTS
        assert shc.ProductionActionsRefused in shc.F1_DETECT_ONLY
        assert shc.ProductionActionsRefused.recover is shc.Invariant.recover
        assert shc.main(["invariants"]) == 0
        listed = {row["name"]: row for row in json.loads(capsys.readouterr().out)}
        row = listed["production_actions_refused"]
        assert row["tier"] == "light" and row["detect_only"]
        assert "production_action_refused" in row["source"] and row["failure"].strip()
