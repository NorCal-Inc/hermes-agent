"""Regression (t_f7d75bb7 runs 5088/5100): a Kanban worker dispatched under
the shared ``default`` profile must not take the root home's SOUL.md
(the executive persona) as its identity, while chat sessions and named
profile workers keep their SOUL.md.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import KANBAN_SHARED_WORKER_IDENTITY, build_system_prompt_parts

PERSONA = "# Hermes Agent Persona — Erika (executive; governs, does not execute)"


def _agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _parts(agent):
    seen = {}

    def fake_context_files(cwd=None, skip_soul=False, **_kw):
        seen["skip_soul"] = skip_soul
        return "" if skip_soul else PERSONA

    with (
        patch("run_agent.load_soul_md", return_value=PERSONA),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        parts = build_system_prompt_parts(agent)
    return parts, seen


def _full(parts):
    return "\n".join(str(v) for v in parts.values())


def test_shared_default_worker_gets_worker_identity_not_persona():
    parts, seen = _parts(_agent(_kanban_shared_worker=True))
    assert KANBAN_SHARED_WORKER_IDENTITY in parts["stable"]
    assert PERSONA not in _full(parts)
    assert seen["skip_soul"] is True


def test_chat_session_keeps_soul_persona():
    parts, _ = _parts(_agent())
    assert PERSONA in parts["stable"]
    assert KANBAN_SHARED_WORKER_IDENTITY not in _full(parts)


def test_named_profile_worker_keeps_its_soul():
    parts, _ = _parts(_agent(_kanban_shared_worker=False, _kanban_worker_guidance="KANBAN"))
    assert PERSONA in parts["stable"]
    assert KANBAN_SHARED_WORKER_IDENTITY not in _full(parts)


def _flag(monkeypatch, task, profile):
    import agent.agent_init as ai
    src = open(ai.__file__, encoding="utf-8").read()
    start = src.index("    agent._kanban_shared_worker = ")
    end = src.index("    )\n", start) + len("    )\n")
    snippet = "\n".join(l[4:] for l in src[start:end].splitlines())
    for k, v in (("HERMES_KANBAN_TASK", task), ("HERMES_PROFILE", profile)):
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    import os
    ns = {"os": os, "agent": SimpleNamespace(),
          "_kanban_task_id": (os.environ.get("HERMES_KANBAN_TASK") or "").strip()}
    exec(snippet, ns)
    return ns["agent"]._kanban_shared_worker


def test_flag_only_for_kanban_task_under_default(monkeypatch):
    assert _flag(monkeypatch, "t_x", "default") is True
    assert _flag(monkeypatch, "t_x", "paris_worker") is False
    assert _flag(monkeypatch, None, "default") is False
    assert _flag(monkeypatch, None, None) is False
