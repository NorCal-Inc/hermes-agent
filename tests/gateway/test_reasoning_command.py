"""Tests for gateway /reasoning command and hot reload behavior."""

import asyncio
import inspect
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource, _session_key_namespace


def _make_event(text="/reasoning", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    return runner


def _run_agent_capturing_toolsets(tmp_path, monkeypatch, profile):
    """Drive ``_run_agent`` once and return the toolsets the agent was built with.

    ``platform_toolsets`` and two ``mcp_servers`` are configured so the caller
    can distinguish "resolved toolsets reached the agent" from "the
    thin-executive ceiling replaced them".
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "platform_toolsets:\n"
        "  cli: [web, memory]\n"
        "mcp_servers:\n"
        "  exa:\n"
        "    url: https://mcp.exa.ai/mcp\n"
        "  web-search-prime:\n"
        "    url: https://api.z.ai/api/mcp/web_search_prime/mcp\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_env_path", hermes_home / ".env")
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
        },
    )
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    _CapturingAgent.last_init = None
    runner = _make_runner()

    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="cli",
        chat_name="CLI",
        chat_type="dm",
        user_id="user-1",
        profile=profile,
    )

    result = asyncio.run(
        runner._run_agent(
            message="ping",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            # Use the real namespace resolver rather than an f-string: it
            # collapses both ``None`` and the literal ``"default"`` to
            # ``agent:main``, which a hand-rolled key would get wrong.
            session_key=f"{_session_key_namespace(profile)}:local:dm",
        )
    )

    assert result["final_response"] == "ok"
    assert _CapturingAgent.last_init is not None
    return list(_CapturingAgent.last_init["enabled_toolsets"])


class _CapturingAgent:
    """Fake agent that records init kwargs for assertions."""

    last_init = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message: str, conversation_history=None, task_id=None):
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
        }


class TestReasoningCommand:


    def test_parse_reasoning_command_args_accepts_ascii_and_smart_global_flags(self):
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("high --global") == ("high", True)
        assert gateway_run.GatewayRunner._parse_reasoning_command_args("—global xhigh") == ("xhigh", True)

    @pytest.mark.asyncio
    async def test_reasoning_command_reloads_current_state_from_config(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text(
            "agent:\n  reasoning_effort: none\ndisplay:\n  show_reasoning: true\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        runner._reasoning_config = {"enabled": True, "effort": "xhigh"}
        runner._show_reasoning = False

        result = await runner._handle_reasoning_command(_make_event("/reasoning"))

        assert "**Effort:** `none (disabled)`" in result
        assert "**Display:** on ✓" in result
        assert runner._reasoning_config == {"enabled": False}
        assert runner._show_reasoning is True


    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["max", "ultra"])
    async def test_handle_reasoning_command_accepts_extended_efforts(
        self, tmp_path, monkeypatch, effort
    ):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "agent:\n  reasoning_effort: medium\n", encoding="utf-8"
        )
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_event(f"/reasoning {effort}")
        session_key = runner._session_key_for_source(event.source)

        await runner._handle_reasoning_command(event)

        assert runner._session_reasoning_overrides[session_key] == {
            "enabled": True,
            "effort": effort,
        }


    def test_resolve_session_reasoning_prefers_session_override(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("agent:\n  reasoning_effort: low\n", encoding="utf-8")

        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        source = _make_event("/reasoning").source
        session_key = runner._session_key_for_source(source)
        runner._session_reasoning_overrides[session_key] = {"enabled": True, "effort": "xhigh"}

        assert runner._resolve_session_reasoning_config(source=source) == {"enabled": True, "effort": "xhigh"}


    def test_run_agent_includes_enabled_mcp_servers_in_gateway_toolsets(self, tmp_path, monkeypatch):
        # Runs on a *named* profile on purpose. The thin-executive ceiling in
        # ``_run_agent`` replaces the resolved toolsets with ``["executive"]``
        # for the default/Erika profile, so a default-profile source measures
        # that ceiling instead of the MCP toolset resolution this test names.
        # See ``test_thin_executive_ceiling_*`` below, which lock the ceiling.
        enabled_toolsets = set(
            _run_agent_capturing_toolsets(tmp_path, monkeypatch, profile="team-leader")
        )
        assert "web" in enabled_toolsets
        assert "memory" in enabled_toolsets
        assert "exa" in enabled_toolsets
        assert "web-search-prime" in enabled_toolsets

    def test_thin_executive_ceiling_caps_default_profile_toolsets(self, tmp_path, monkeypatch):
        """Default/Erika profile is capped to the thin ``executive`` surface.

        The ceiling is a deliberate capability boundary (2d92fcf007): on an
        ordinary default-profile turn the agent orchestrates governed work and
        does not receive research, terminal, code-execution, memory or MCP
        tools — even when ``platform_toolsets`` and ``mcp_servers`` are
        configured, as they are in this fixture's config.

        Scope: this pins the ceiling in ``TurnRunner._run_agent`` only, which
        is where every ``_thin_executive_mode`` branch lives. The
        ``/background`` path (``_run_background_task_inner``) resolves
        toolsets independently and is *not* capped; that gap predates this
        test and is not asserted here either way.
        """
        toolsets = _run_agent_capturing_toolsets(tmp_path, monkeypatch, profile=None)
        assert toolsets == ["executive"]
        # Spelled out so the boundary, not just the literal, is what fails:
        # a legitimate widening of the executive surface should still keep
        # these off a default-profile turn.
        for denied in ("terminal", "code_execution", "web", "memory", "exa"):
            assert denied not in set(toolsets)

    def test_thin_executive_ceiling_does_not_apply_to_named_profiles(self, tmp_path, monkeypatch):
        """Named Team Leader / worker profiles keep their configured toolsets.

        Pairs with the test above so the boundary is pinned from both sides:
        tightening the ceiling to cover every profile, or dropping it
        entirely, fails one of the two.
        """
        enabled_toolsets = set(
            _run_agent_capturing_toolsets(tmp_path, monkeypatch, profile="team-leader")
        )
        assert "web" in enabled_toolsets


class TestLoadShowReasoningCoercion:
    """Regression: display.show_reasoning must be coerced, not bool()'d."""

    def _load_with_config(self, tmp_path, monkeypatch, yaml_body: str) -> bool:
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(yaml_body, encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
        return gateway_run.GatewayRunner._load_show_reasoning()

    def test_quoted_false_is_false(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: "false"\n',
        ) is False


    def test_bare_true_is_true(self, tmp_path, monkeypatch):
        assert self._load_with_config(
            tmp_path, monkeypatch,
            'display:\n  show_reasoning: true\n',
        ) is True

