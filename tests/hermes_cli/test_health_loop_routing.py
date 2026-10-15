from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")

_CONTROLLER = Path(__file__).resolve().parents[2] / "norcal/health/system_health_controller.py"
_CONFIG = _CONTROLLER.with_name("health-controller.json")


def _load_controller():
    name = "norcal_health_loop_routing_under_test"
    spec = importlib.util.spec_from_file_location(name, _CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


shc = _load_controller()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class CronEpisode(shc.Invariant):
    name = "critical_cron_jobs_healthy"
    tier = shc.TIER_LIGHT
    source = "fixture cron ledger"
    failure = "fixture job is unhealthy"
    evidence = "two signatures for one subject"

    def __init__(self):
        self.unhealthy = True

    def check(self, ctx):
        if not self.unhealthy:
            return []
        return [
            shc.Finding(self.name, "nuclear_backup", "last_status:error"),
            shc.Finding(self.name, "nuclear_backup", "failure_streak"),
        ]


def _ctx(home: Path, config: dict):
    return shc.Context(
        state_dir=home / "state" / "system-health-controller",
        hermes_home=home,
        config=config,
        kanban=kb.connect_closing,
        send_alert=lambda subject, text: (True, "fixture"),
        run_command=lambda argv, timeout: (0, ""),
        create_card=shc.kanban_repair_card,
    )


def test_shipped_policy_routes_one_runnable_gauntlet_card_and_resolves_fresh_evidence(isolated_home):
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))
    # The production-only adoption points at a card absent from this isolated DB,
    # so this proves the normal automatic creation path under shipped policy.
    episode = CronEpisode()
    controller = shc.Controller(_ctx(isolated_home, config), [episode])

    first = controller.run(shc.TIER_LIGHT)
    assert len(first.escalated) == 2
    with kb.connect_closing() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ?",
            ("health:critical_cron_jobs_healthy:nuclear_backup",),
        ).fetchall()
        assert len(rows) == 1
        card = kb.get_task(conn, rows[0]["id"])
        assert card.status == "ready"
        assert card.assignee == "default"
        assert card.executor_lane == kb.EXECUTOR_LANE_CLAUDE
        assert card.gauntlet_enforced is True
        assert card.tenant is None
        assert "independent verification" in (card.body or "")
        card_id = card.id

    # Re-observation does not create another card or run.
    controller.run(shc.TIER_LIGHT)
    with kb.connect_closing() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            ("health:critical_cron_jobs_healthy:nuclear_backup",),
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (card_id,)).fetchone()[0] == 0

    # The controller, not worker assertion, resolves the actual finding from a
    # fresh normal pass and dispositions the now-overtaken repair card.
    episode.unhealthy = False
    cleared = controller.run(shc.TIER_LIGHT)
    assert len(cleared.resolved) == 2
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, card_id).status == "archived"


def test_automatic_card_requires_independent_verdict_and_fail_returns_repair(isolated_home):
    config = {
        "kanban_card_invariants": ["critical_cron_jobs_healthy"],
        "automatic_shared_repair_invariants": ["critical_cron_jobs_healthy"],
    }
    episode = CronEpisode()
    shc.Controller(_ctx(isolated_home, config), [episode]).run(shc.TIER_LIGHT)

    with kb.connect_closing() as conn:
        subject = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            ("health:critical_cron_jobs_healthy:nuclear_backup",),
        ).fetchone()["id"]
        claimed = kb.claim_task(conn, subject)
        assert claimed is not None
        kb.add_attachment(
            conn, subject, filename="worker-readback.md",
            stored_path=f"/tmp/{subject}/worker-readback.md", size=512,
            uploaded_by="claude-lane",
        )
        assert kb.request_review(
            conn, subject, summary="fresh worker readback attached",
            expected_run_id=claimed.current_run_id,
        ) is True
        pending = kb.get_task(conn, subject)
        assert pending.status == "review"
        assert pending.verification_state == kb.VERIFICATION_PENDING
        verifier = kb._open_verifier_child(conn, subject)
        assert verifier is not None
        verifier_run = kb.claim_task(conn, verifier)
        assert verifier_run is not None
        with kb.write_txn(conn):
            kb._append_event(
                conn, verifier, "codex_verifier_started", {"executor": "codex"},
                run_id=verifier_run.current_run_id,
            )
        assert kb.complete_task(
            conn, verifier,
            summary="VERDICT: FAIL\nFresh controller evidence is still unhealthy.",
            expected_run_id=verifier_run.current_run_id,
        ) is True
        returned = kb.get_task(conn, subject)
        assert returned.status in {"ready", "todo"}
        assert returned.verification_state is None
        assert returned.regression_required is True
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            ("health:critical_cron_jobs_healthy:nuclear_backup",),
        ).fetchone()[0] == 1


def test_explicit_adoption_requires_durable_manual_provenance_and_gauntlet(isolated_home):
    finding = shc.Finding("critical_cron_jobs_healthy", "nuclear_backup", "last_status:error")
    with kb.connect_closing() as conn:
        existing = kb.create_task(
            conn,
            title="bounded maintenance",
            body="No title or body matching is used for adoption.",
            assignee="default",
            executor_lane=kb.EXECUTOR_LANE_CLAUDE,
            gauntlet=True,
            provenance=kb.ActorProvenance(
                kind=kb.ACTOR_KIND_HUMAN_INTERACTIVE,
                actor_id="interactive-relay",
                cause=kb.CREATION_CAUSE_MANUAL_RELAY,
            ),
        )
    config = {
        "automatic_shared_repair_invariants": ["critical_cron_jobs_healthy"],
        "health_repair_adoptions": {
            "critical_cron_jobs_healthy:nuclear_backup": existing,
        },
    }
    adopted, created = shc.kanban_repair_card(
        _ctx(isolated_home, config), finding, {"attempts": 0}, shc.ESCALATION_NOT_RECOVERABLE,
    )
    assert (adopted, created) == (existing, False)
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_company_and_human_waits_never_enter_shared_automatic_lane(isolated_home):
    config = {
        "kanban_card_invariants": ["critical_cron_jobs_healthy"],
        "automatic_shared_repair_invariants": ["critical_cron_jobs_healthy"],
    }

    class HeldOrCompany(shc.Invariant):
        name = "critical_cron_jobs_healthy"
        source = failure = evidence = "fixture"

        def check(self, ctx):
            return [
                shc.Finding(self.name, "ENT-private", "down", route=shc.ROUTE_COMPANY),
                shc.Finding(self.name, "mfa", "authorization", awaiting_human=True),
            ]

    result = shc.Controller(_ctx(isolated_home, config), [HeldOrCompany()]).run(shc.TIER_LIGHT)
    assert len(result.awaiting_human) == 1
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
