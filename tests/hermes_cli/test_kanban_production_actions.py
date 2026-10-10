"""Rule 6 (production authority), segment 6c: the ``production_actions``
authorization field on cards.

Design 3A (approved 2026-10-07): a card "explicitly permits" a production
action through a STRUCTURED field, never body text. Ruling 2026-10-09
(norcal/security/production-roles/): every role is christopher_only;
production actions are executed only after Christopher authorizes that exact
action on the card. These tests pin the field's four properties:

* validated against the installed registry (unknown role → refused);
* settable only at creation, by an interactive operator — never by a
  dispatcher-owned worker (``HERMES_KANBAN_TASK``) nor the ``kanban_create``
  tool;
* recorded as a ``production_action_authorized`` event;
* immutable afterwards (storage-layer trigger, same pass as the rule 8
  history triggers).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A test registry shaped like the installed one, pointed at via the
    documented override env var."""
    reg = tmp_path / "production-roles.json"
    reg.write_text(json.dumps({
        "version": 1,
        "schema": "norcal.production-roles.v1",
        "roles": {
            "stripe_live": {"christopher_only": True, "profiles": []},
            "deploy": {"christopher_only": True, "profiles": []},
            "dns": {"christopher_only": True, "profiles": []},
        },
    }), encoding="utf-8")
    monkeypatch.setenv(kb.PRODUCTION_ROLES_REGISTRY_ENV, str(reg))
    return reg


def _create_ns(**overrides):
    ns = argparse.Namespace(
        title="prod card", body=None, assignee="worker",
        created_by="christopher", workspace="scratch", tenant=None,
        priority=0, parent=None, triage=False,
        idempotency_key=None, max_runtime=None, skills=None,
        production_actions=[], json=True,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _events(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


# ---------------------------------------------------------------------------
# Registry resolution
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_default_path_is_installed_registry(self, kanban_home, monkeypatch):
        monkeypatch.delenv(kb.PRODUCTION_ROLES_REGISTRY_ENV, raising=False)
        assert kb.production_roles_registry_path() == (
            Path.home() / ".hermes" / "security" / "production-roles"
            / "production-roles.json"
        )

    def test_missing_registry_fails_closed(self, kanban_home, monkeypatch, tmp_path):
        monkeypatch.setenv(
            kb.PRODUCTION_ROLES_REGISTRY_ENV, str(tmp_path / "absent.json")
        )
        with pytest.raises(kb.ProductionActionError, match="not found"):
            kb.validate_production_actions(["deploy"])

    def test_blank_request_needs_no_registry(self, kanban_home, monkeypatch, tmp_path):
        monkeypatch.setenv(
            kb.PRODUCTION_ROLES_REGISTRY_ENV, str(tmp_path / "absent.json")
        )
        assert kb.validate_production_actions(None) == []
        assert kb.validate_production_actions(["", "  "]) == []


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_adds_column_on_existing_board_without_touching_rows(self, kanban_home):
        with kb.connect_closing() as conn:
            t1 = kb.create_task(conn, title="old one", assignee="worker", body="b1")
            t2 = kb.create_task(conn, title="old two", assignee="worker", priority=3)
        db_path = kb.kanban_db_path(board="default")
        # Rewind the board to its pre-6c shape: drop the trigger (it names
        # the column) and then the column itself.
        raw = sqlite3.connect(str(db_path))
        raw.row_factory = sqlite3.Row
        raw.execute(f"DROP TRIGGER IF EXISTS {kb.PRODUCTION_ACTIONS_TRIGGER_NAME}")
        raw.execute("ALTER TABLE tasks DROP COLUMN production_actions")
        raw.commit()
        cols = {r["name"] for r in raw.execute("PRAGMA table_info(tasks)")}
        assert "production_actions" not in cols
        before = {
            r["id"]: dict(r)
            for r in raw.execute("SELECT * FROM tasks ORDER BY id")
        }
        raw.close()

        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        kb.init_db()

        with kb.connect_closing() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
            assert "production_actions" in cols
            after = {
                r["id"]: dict(r)
                for r in conn.execute("SELECT * FROM tasks ORDER BY id")
            }
            assert set(after) == {t1, t2}
            for tid, row in after.items():
                assert row.pop("production_actions") == "[]"
                assert row == before[tid]
            assert kb.get_task(conn, t1).production_actions == []
            triggers = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            assert kb.PRODUCTION_ACTIONS_TRIGGER_NAME in triggers
            assert kb.PRODUCTION_ACTIONS_TRIGGER_NAME in kb.TASK_HISTORY_TRIGGER_NAMES


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

class TestCreate:
    def test_valid_role_is_stored_and_recorded(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="deploy it", assignee="worker",
                created_by="christopher",
                production_actions=["deploy", "dns", "deploy"],
            )
            task = kb.get_task(conn, tid)
            assert task.production_actions == ["deploy", "dns"]
            ev = _events(conn, tid, kb.PRODUCTION_ACTION_AUTHORIZED_EVENT)
            assert len(ev) == 1
            assert ev[0].payload == {
                "roles": ["deploy", "dns"], "created_by": "christopher",
            }

    def test_ordinary_card_is_empty_and_records_no_grant(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="plain", assignee="worker")
            assert kb.get_task(conn, tid).production_actions == []
            assert _events(conn, tid, kb.PRODUCTION_ACTION_AUTHORIZED_EVENT) == []

    def test_unknown_role_refused_and_nothing_created(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            with pytest.raises(kb.ProductionActionError, match="unknown production role"):
                kb.create_task(
                    conn, title="nope", assignee="worker",
                    production_actions=["deploy", "rm_rf_prod"],
                )
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    def test_worker_environment_refused(self, kanban_home, registry, monkeypatch):
        with kb.connect_closing() as conn:
            parent = kb.create_task(conn, title="worker's own card", assignee="worker")
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
            with pytest.raises(kb.ProductionActionError, match="dispatcher-owned worker"):
                kb.create_task(
                    conn, title="escalation", assignee="worker",
                    production_actions=["deploy"],
                )
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
            # A worker creating an ordinary card is unaffected.
            tid = kb.create_task(conn, title="ordinary child", assignee="worker")
            assert kb.get_task(conn, tid).production_actions == []


# ---------------------------------------------------------------------------
# CLI: hermes kanban create / show
# ---------------------------------------------------------------------------

class TestCli:
    def test_create_flag_stores_roles(self, kanban_home, registry, capsys):
        rc = kb_cli._cmd_create(_create_ns(production_actions=["stripe_live"]))
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["production_actions"] == ["stripe_live"]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, out["id"]).production_actions == ["stripe_live"]
            ev = _events(conn, out["id"], kb.PRODUCTION_ACTION_AUTHORIZED_EVENT)
            assert ev and ev[0].payload["created_by"] == "christopher"

    def test_create_unknown_role_refused(self, kanban_home, registry, capsys):
        with kb.connect_closing() as conn:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        rc = kb_cli._cmd_create(_create_ns(production_actions=["firewall"]))
        assert rc == 2
        err = capsys.readouterr().err
        assert "unknown production role(s): firewall" in err
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    def test_create_from_worker_refused(self, kanban_home, registry, capsys, monkeypatch):
        with kb.connect_closing() as conn:
            parent = kb.create_task(conn, title="worker's own card", assignee="worker")
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        rc = kb_cli._cmd_create(_create_ns(production_actions=["deploy"]))
        assert rc == 2
        assert "dispatcher-owned worker" in capsys.readouterr().err
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    def test_show_prints_production_actions_line(self, kanban_home, registry, capsys):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="shown", assignee="worker",
                production_actions=["deploy", "dns"],
            )
            plain = kb.create_task(conn, title="plain", assignee="worker")
        rc = kb_cli._cmd_show(argparse.Namespace(
            task_id=tid, json=False, state_type=None, state_name=None,
        ))
        assert rc == 0
        assert "  production actions: deploy, dns\n" in capsys.readouterr().out
        rc = kb_cli._cmd_show(argparse.Namespace(
            task_id=plain, json=False, state_type=None, state_name=None,
        ))
        assert rc == 0
        assert "production actions:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# kanban_create tool
# ---------------------------------------------------------------------------

class TestTool:
    @pytest.mark.parametrize("key", ["production_actions", "production_action"])
    def test_tool_cannot_set_it(self, kanban_home, registry, key):
        from tools import kanban_tools as kt

        with kb.connect_closing() as conn:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        out = json.loads(kt._handle_create({
            "title": "tool grant", "assignee": "worker", key: ["deploy"],
        }))
        assert "error" in out
        assert "production_actions cannot be set through kanban_create" in out["error"]
        with kb.connect_closing() as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    def test_tool_without_it_still_creates(self, kanban_home, registry):
        from tools import kanban_tools as kt

        out = json.loads(kt._handle_create({"title": "ordinary", "assignee": "worker"}))
        assert out["ok"] is True
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, out["task_id"]).production_actions == []


# ---------------------------------------------------------------------------
# Immutability trigger
# ---------------------------------------------------------------------------

class TestImmutable:
    def test_update_rejected_by_trigger(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="locked", assignee="worker",
                production_actions=["deploy"],
            )
            for new_value in ('["deploy", "stripe_live"]', "[]"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    with kb.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET production_actions = ? WHERE id = ?",
                            (new_value, tid),
                        )
            assert kb.get_task(conn, tid).production_actions == ["deploy"]

    def test_empty_card_cannot_be_granted_later(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = kb.create_task(conn, title="plain", assignee="worker")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                with kb.write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET production_actions = '[\"deploy\"]' WHERE id = ?",
                        (tid,),
                    )
            assert kb.get_task(conn, tid).production_actions == []

    def test_same_value_rewrite_and_other_columns_still_update(self, kanban_home, registry):
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn, title="locked", assignee="worker",
                production_actions=["deploy"],
            )
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET production_actions = production_actions, "
                    "priority = 7 WHERE id = ?",
                    (tid,),
                )
            task = kb.get_task(conn, tid)
            assert task.priority == 7
            assert task.production_actions == ["deploy"]


# ---------------------------------------------------------------------------
# Repair (Codex t_7e126eb3 / t_2f9a1fd4): the worker marker counts when it is
# PRESENT, whatever its value - empty and blank values must still refuse.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("marker", ["", "   "])
def test_db_refuses_when_worker_marker_present_but_blank(kanban_home, registry, monkeypatch, marker):
    with kb.connect_closing() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        monkeypatch.setenv("HERMES_KANBAN_TASK", marker)
        with pytest.raises(kb.ProductionActionError, match="dispatcher-owned worker"):
            kb.create_task(conn, title="blank marker", assignee="worker",
                           production_actions=["deploy"])
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


@pytest.mark.parametrize("marker", ["", "   "])
def test_cli_refuses_when_worker_marker_present_but_blank(kanban_home, registry, capsys, monkeypatch, marker):
    with kb.connect_closing() as conn:
        before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    monkeypatch.setenv("HERMES_KANBAN_TASK", marker)
    rc = kb_cli._cmd_create(_create_ns(production_actions=["deploy"]))
    assert rc == 2
    assert "dispatcher-owned worker" in capsys.readouterr().err
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


def test_marker_absent_still_allows_interactive_grant(kanban_home, registry, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="interactive grant", assignee="worker",
                             production_actions=["deploy"])
        assert kb.get_task(conn, tid).production_actions == ["deploy"]
