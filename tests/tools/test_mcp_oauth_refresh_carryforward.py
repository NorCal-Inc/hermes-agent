"""NorCal port of upstream #106185 to the SECOND refresh handler copy (tools/mcp_oauth.py,
reached via build_oauth_auth() from mcp_tool.py). Upstream's tests only cover the manager copy.

RFC 6749 §6: a refresh response may omit refresh_token (non-rotating AS) and scope; the handler
must carry both forward instead of storing the response verbatim (which erased the only refresh
token and forced a browser re-auth at the next expiry). Hermetic: fake context/storage, no network.
"""
import asyncio

import pytest

from mcp.shared.auth import OAuthToken


class _Storage:
    def __init__(self):
        self.saved = None

    async def set_tokens(self, tokens):
        self.saved = tokens


class _Context:
    def __init__(self, tokens):
        self.current_tokens = tokens
        self.storage = _Storage()

    def update_token_expiry(self, tokens):
        pass

    def clear_tokens(self):
        self.current_tokens = None


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    async def aread(self):
        return self._body


def _provider(prior):
    from tools.mcp_oauth import _get_hermes_oauth_provider_class
    cls = _get_hermes_oauth_provider_class()
    if cls is None:
        pytest.skip("MCP OAuth SDK not available")
    p = cls.__new__(cls)  # exercise the handler only; no network/browser construction
    p.context = _Context(prior)
    return p


def _run(coro):
    return asyncio.run(coro)


def test_non_rotating_refresh_keeps_refresh_token_and_scope():
    prior = OAuthToken(access_token="at-1", token_type="Bearer", expires_in=3600,
                       refresh_token="rt-keep", scope="read")
    p = _provider(prior)
    ok = _run(p._handle_refresh_response(
        _Resp(200, b'{"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600}')))
    assert ok is True
    assert p.context.current_tokens.access_token == "at-2"
    assert p.context.current_tokens.refresh_token == "rt-keep"
    assert p.context.current_tokens.scope == "read"
    assert p.context.storage.saved.refresh_token == "rt-keep"


def test_rotating_refresh_replaces_refresh_token():
    prior = OAuthToken(access_token="at-1", token_type="Bearer", expires_in=3600, refresh_token="rt-old")
    p = _provider(prior)
    ok = _run(p._handle_refresh_response(
        _Resp(200, b'{"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "rt-new"}')))
    assert ok is True
    assert p.context.current_tokens.refresh_token == "rt-new"
    assert p.context.storage.saved.refresh_token == "rt-new"


def test_error_refresh_still_clears_tokens():
    prior = OAuthToken(access_token="at-1", token_type="Bearer", expires_in=3600, refresh_token="rt-x")
    p = _provider(prior)
    assert _run(p._handle_refresh_response(_Resp(400, b"{}"))) is False
    assert p.context.current_tokens is None
