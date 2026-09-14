"""Regression 2026-09-14: a dispatcher worker whose Kanban scope env was
stripped in transit must fail closed instead of running an unscoped agent."""

from types import SimpleNamespace

import pytest

import cli as cli_mod


def _install_fake_cli(monkeypatch, calls):
    class FakeCLI:
        def __init__(self, **_kwargs):
            self.console = SimpleNamespace(print=lambda *_a, **_kw: None)
            self.session_id = "sq-tripwire"
            self.agent = SimpleNamespace(session_id="sq-tripwire", platform="cli")

        def _claim_active_session(self, surface, *, stderr=False):
            return True

        def _show_security_advisories(self):
            pass

        def chat(self, query, images=None):
            calls.append(("chat", query))
            return "done"

        def _print_exit_summary(self, clear_screen=True):
            pass

    monkeypatch.setattr(cli_mod, "HermesCLI", FakeCLI)
    monkeypatch.setattr(cli_mod.atexit, "register", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_mod, "_finalize_single_query", lambda _cli: None)


def test_worker_prompt_without_kanban_scope_exits_before_agent(monkeypatch, capsys):
    calls = []
    _install_fake_cli(monkeypatch, calls)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(query="work kanban task t_3883034a", quiet=False, toolsets="terminal")

    assert exc.value.code == 2
    assert calls == []
    assert "t_3883034a started without HERMES_KANBAN_TASK" in capsys.readouterr().err


def test_ordinary_query_is_not_affected_by_worker_tripwire(monkeypatch):
    calls = []
    _install_fake_cli(monkeypatch, calls)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    cli_mod.main(query="please work kanban task t_3883034a later", quiet=False, toolsets="terminal")

    assert calls == [("chat", "please work kanban task t_3883034a later")]


def test_dispatcher_worker_query_matches_only_the_exact_spawn_prompt():
    from hermes_cli import kanban_db as kb

    assert kb.dispatcher_worker_query_task_id("work kanban task t_ab12cd34") == "t_ab12cd34"
    assert kb.dispatcher_worker_query_task_id(f"{kb.KANBAN_WORKER_QUERY_PREFIX}t_9f") == "t_9f"
    assert kb.dispatcher_worker_query_task_id("work kanban task t_ab12; rm -rf /") is None
    assert kb.dispatcher_worker_query_task_id("hello") is None
    assert kb.dispatcher_worker_query_task_id(None) is None
