"""Erika Discord ``list commands`` reference: deterministic, source-derived."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.erika_reference import build_reference, is_commands_request
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource

FORBIDDEN = ("NorCal-OpsBot", "NorCal Ops", "#morning-briefing", "morning-briefing",
             "Telegram", "telegram")


def _home(tmp_path):
    (tmp_path / "discord").mkdir()
    (tmp_path / "discord" / "channels.json").write_text(json.dumps({"alerts": "11", "erika": "22"}))
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"name": "on-job", "enabled": True, "schedule_display": "0 9 * * 1", "deliver": "discord:11"},
        {"name": "off-job", "enabled": False, "schedule_display": "0 1 * * *", "deliver": "discord:11"},
        {"name": "paused-job", "enabled": True, "paused_at": "x", "schedule_display": "0 2 * * *", "deliver": "discord:11"},
        {"name": "local-job", "enabled": True, "schedule_display": "0 3 * * *", "deliver": "local"},
    ]}))
    return tmp_path


def test_reference_lists_only_enabled_discord_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.erika_reference._daily_reports_enabled", lambda: False)
    text = build_reference(_home(tmp_path), free_response="22")
    assert "on-job - 0 9 * * 1 -> #alerts" in text
    for absent in ("off-job", "paused-job", "local-job"):
        assert absent not in text
    assert "**Discord channels** (#alerts, #erika)" in text
    assert "/help" in text and "/commands" in text
    assert "explicit authorization" in text
    assert "does not mean the entity is formed or monitored" in text
    for bad in FORBIDDEN:
        assert bad not in text


@pytest.mark.parametrize("phrase", [
    "list commands", "List commands", "<@123> list commands", "list commands please",
    "show me your commands", "what commands do you have?", "commands", "help",
    "what can you do",
])
def test_equivalents_match(phrase):
    assert is_commands_request(phrase)


@pytest.mark.parametrize("phrase", [
    "", "/commands", "list the invoices", "please run the commands in the script",
    "help me draft an email", "commands for deploying production",
])
def test_non_requests_do_not_match(phrase):
    assert not is_commands_request(phrase)


def _runner(platform):
    from gateway.run import GatewayRunner

    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True)})
    r.adapters = {platform: MagicMock(send=AsyncMock())}
    r._voice_mode = {}
    r.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    r.session_store = MagicMock()
    r._running_agents = {}
    r._session_run_generation = {}
    r._pending_messages = {}
    r._pending_approvals = {}
    r._is_user_authorized = lambda _s: True
    r._set_session_env = lambda _c: None
    r._run_agent = AsyncMock(side_effect=AssertionError("LLM path must not run"))
    return r


def _event(text, platform):
    src = SessionSource(platform=platform, user_id="u1", chat_id="c1", user_name="t", chat_type="group")
    return MessageEvent(text=text, source=src, message_id="m1")


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["list commands", "show me your commands"])
async def test_real_request_path_returns_reference_without_llm(phrase, monkeypatch):
    monkeypatch.setattr("gateway.erika_reference._daily_reports_enabled", lambda: False)
    out = await _runner(Platform.DISCORD)._handle_message(_event(phrase, Platform.DISCORD))
    assert out.startswith("**Erika - NorCal_Hermes command reference**")
    assert "`/help`" in out
    for bad in FORBIDDEN:
        assert bad not in out


@pytest.mark.asyncio
async def test_other_platforms_unaffected():
    r = _runner(Platform.TELEGRAM)
    r.session_store.get_or_create_session.side_effect = RuntimeError("reached agent path")
    try:
        out = await r._handle_message(_event("list commands", Platform.TELEGRAM))
    except Exception:
        return
    assert not (out or "").startswith("**Erika")
