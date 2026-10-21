"""Bounded post-exhaustion auto-recovery ladder (#85426, #107307): fallback first, transient
outages only, visible on every surface, interrupt cancels.

Adapted from upstream: the fork has no ``settle_unrecovered_error`` seam, so the module is tested
directly and the loop seam is driven end-to-end through ``run_conversation``.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.error_classifier import classify_api_error
from agent.turn_recovery_autorecover import ladder_wait_seconds, start_recovery_cycle
from agent.turn_retry_state import TurnRetryState


class _Err(Exception):
    def __init__(self, status_code, message, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.response = SimpleNamespace(headers=headers or {})
        self.body = None


class _Agent:
    log_prefix = ""
    provider = "custom"
    _current_streamed_assistant_text = ""

    def __init__(self, *, cycles=5, platform="cli"):
        self.statuses, self.waits = [], []
        self._auto_recovery_cycles = cycles
        self.platform = platform

    def _has_content_after_think_block(self, text):
        return bool(text.strip())

    def _emit_status(self, text):
        self.statuses.append(str(text))

    def _emit_wait_notice(self, text):
        self.waits.append(str(text))

    def _client_log_context(self):
        return ""


def _start(agent, err, retry=None):
    retry = retry or TurnRetryState(primary_recovery_attempted=True)
    classified = classify_api_error(err, provider=agent.provider)
    return start_recovery_cycle(agent, err, classified, retry), retry


@pytest.mark.parametrize("status,message", [(503, "Service temporarily unavailable"), (529, "Overloaded")])
def test_transient_exhaustion_enters_ladder(status, message):
    agent = _Agent()
    cycle, retry = _start(agent, _Err(status, message))
    assert cycle is not None and cycle["cycle"] == 1 and cycle["total"] == 5
    assert retry.auto_recovery_cycles_used == 1
    assert agent.statuses == agent.waits and len(agent.statuses) == 1
    line = agent.statuses[0]
    assert "retrying automatically in" in line and "(cycle 1/5)" in line and "press Esc to stop" in line


def test_ladder_is_bounded_and_shuns_nonretryable_and_delivered_text():
    agent = _Agent()
    spent = TurnRetryState(primary_recovery_attempted=True, auto_recovery_cycles_used=5)
    cycle, _ = _start(agent, _Err(503, "down"), retry=spent)
    assert cycle is None
    assert any("gave up after 5 cycles" in s for s in agent.statuses)

    auth = _Agent()
    cycle, retry = _start(auth, _Err(401, "Incorrect API key provided"))
    assert cycle is None and retry.auto_recovery_cycles_used == 0 and auth.statuses == []

    streamed = _Agent()
    streamed._current_streamed_assistant_text = "Here is the first half of the answer"
    cycle, retry = _start(streamed, _Err(503, "down"))
    assert cycle is None and retry.auto_recovery_cycles_used == 0

    off = _Agent(cycles=0)
    cycle, retry = _start(off, _Err(503, "down"))
    assert (cycle, retry.auto_recovery_cycles_used, off.statuses) == (None, 0, [])


def test_ladder_schedule_honours_retry_after_and_platform_stop_hint(monkeypatch):
    import agent.retry_utils as retry_utils
    calls = []

    def _recording_backoff(attempt, *, base_delay, max_delay, jitter_ratio):
        calls.append((attempt, base_delay, max_delay))
        return min(base_delay * 2 ** (attempt - 1), max_delay)
    monkeypatch.setattr(retry_utils, "jittered_backoff", _recording_backoff)

    plain = _Err(503, "down")
    assert [ladder_wait_seconds(c, plain) for c in (1, 2, 3, 4, 5)] == [15, 30, 60, 60, 60]
    assert calls[0] == (1, 15.0, 60.0)
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "4"})) == 4
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "90"})) == 90
    assert ladder_wait_seconds(1, _Err(529, "Overloaded", {"Retry-After": "900"})) == 120

    tg = _Agent(platform="telegram")
    _start(tg, plain)
    assert "send /stop to cancel" in tg.statuses[0]
    cron = _Agent(platform="cron")
    _start(cron, plain)
    assert cron.statuses[0].endswith("(cycle 1/5)")


# ---------------------------------------------------------------- loop seam


def _loop_agent(cycles):
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1
    agent._auto_recovery_cycles = cycles
    agent._fallback_chain = []
    return agent


def _fast_time():
    mock_time = MagicMock()
    _t = [1000.0]

    def _advancing_time():
        _t[0] += 500.0
        return _t[0]

    mock_time.time.side_effect = _advancing_time
    mock_time.sleep = MagicMock()
    mock_time.monotonic.return_value = 12345.0
    return mock_time


def _run(agent, side_effect):
    from agent import conversation_loop as _conv_loop
    from tests.run_agent.test_run_agent import _mock_response

    agent.client.chat.completions.create.side_effect = [
        _mock_response(content=s, finish_reason="stop") if isinstance(s, str) else s
        for s in side_effect
    ]
    statuses = []
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch.object(agent, "_emit_status", side_effect=statuses.append),
        patch("run_agent.time", _fast_time()),
        patch.object(_conv_loop, "time", _fast_time()),
        patch.object(_conv_loop, "jittered_backoff", lambda *a, **k: 0.0),
    ):
        result = agent.run_conversation("hello")
    return result, statuses


def test_loop_recovers_after_exhaustion_on_transient_outage():
    agent = _loop_agent(cycles=5)
    result, statuses = _run(agent, [_Err(503, "Service temporarily unavailable"), "recovered"])
    assert result.get("final_response") == "recovered"
    assert any("retrying automatically" in s and "(cycle 1/5)" in s for s in statuses)


def test_loop_gives_up_when_ladder_spent():
    agent = _loop_agent(cycles=1)
    result, statuses = _run(agent, [_Err(503, "down"), _Err(503, "down")])
    assert result.get("completed") is False
    assert any("(cycle 1/1)" in s for s in statuses)
    assert any("gave up after 1 cycles" in s for s in statuses)


def test_loop_ladder_disabled_ends_turn_immediately():
    agent = _loop_agent(cycles=0)
    result, statuses = _run(agent, [_Err(503, "down")])
    assert result.get("completed") is False
    assert not any("retrying automatically" in s for s in statuses)
    assert agent.client.chat.completions.create.call_count == 1
