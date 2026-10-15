"""A confirmed hard ``usage_limit_reached`` must not be repeated inside one
worker spawn; a temporary 429 keeps its normal backoff retries.

Drives the real ``run_conversation`` retry loop with a stubbed transport and
counts provider requests. Error body is a redacted provider-shaped fixture.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


class _ProviderError(Exception):
    def __init__(self, status_code, body):
        super().__init__(json.dumps(body))
        self.status_code = status_code
        self.body = body
        self.response = SimpleNamespace(headers={})


def _usage_limit_error():
    return _ProviderError(429, {"error": {
        "type": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "plan_type": "redacted",
        "resets_in_seconds": 2874,
    }})


def _plain_429():
    return _ProviderError(429, {"error": {"message": "rate limit exceeded"}})


def _agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="redacted-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._api_max_retries = 3
    return agent


def _run(agent, errors):
    calls = []

    def fake_api_call(api_kwargs):
        calls.append(1)
        if errors:
            raise errors.pop(0)
        msg = SimpleNamespace(content="Recovered", tool_calls=None)
        choice = SimpleNamespace(message=msg, finish_reason="stop")
        return SimpleNamespace(choices=[choice], model="m", usage=None)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.time.sleep"),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.conversation_loop.time.sleep"),
    ):
        result = agent.run_conversation("hello")
    return result, len(calls)


def test_hard_quota_makes_exactly_one_request_and_yields_hold():
    result, n = _run(_agent(), [_usage_limit_error() for _ in range(5)])
    assert n == 1
    assert result["completed"] is False
    hold = result["quota_hold"]
    assert hold["kind"] == "hard"
    assert hold["reset_at"] is not None


def test_temporary_429_still_retries_with_backoff():
    result, n = _run(_agent(), [_plain_429(), _plain_429()])
    assert n == 3  # two throttles then success: retry behavior unchanged
    assert result["completed"] is True
    assert result["final_response"] == "Recovered"


def test_temporary_429_exhaustion_uses_full_retry_budget():
    result, n = _run(_agent(), [_plain_429() for _ in range(10)])
    assert n == 3
    assert result["completed"] is False
    assert result["quota_hold"]["kind"] == "temporary"
