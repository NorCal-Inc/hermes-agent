"""Incident 2026-10-12: test boards must never project into the live
dashboard ledger (127.0.0.1:5130)."""
from __future__ import annotations

import sqlite3
import urllib.request

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def no_network(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("ledger POST attempted")))
    return calls


def _board(path):
    conn = sqlite3.connect(str(path))
    conn.execute("create table t(x)")
    return conn


def test_temp_board_is_never_production(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert kb._production_ledger_sync_allowed(_board(tmp_path / "kanban.db")) is False


def test_in_memory_board_is_never_production(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert kb._production_ledger_sync_allowed(sqlite3.connect(":memory:")) is False


def test_production_path_allowed_outside_tests_only(tmp_path, monkeypatch):
    prod = tmp_path / "prod" / "kanban.db"
    prod.parent.mkdir()
    conn = _board(prod)
    monkeypatch.setattr(kb, "_production_board_path", lambda: prod)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert kb._production_ledger_sync_allowed(conn) is True
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert kb._production_ledger_sync_allowed(conn) is False


def test_production_path_ignores_home_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert not str(kb._production_board_path()).startswith(str(tmp_path))


def test_sync_helper_sends_nothing_from_a_test_board(tmp_path, monkeypatch, no_network):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    kb._sync_production_ledger(_board(tmp_path / "kanban.db"), "t_leak", status="ready", phase="create")
    assert no_network == []


def test_guard_errors_fail_closed(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    class Broken:
        def execute(self, *a):
            raise sqlite3.OperationalError("boom")
    assert kb._production_ledger_sync_allowed(Broken()) is False
