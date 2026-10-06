"""Worker tool surface (2026-10-05): eager MCP servers + no execute_code where it is always blocked."""
from __future__ import annotations

import pytest

from tools import tool_search as ts


def _td(name):
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {"type": "object", "properties": {}}}}


def test_config_parses_eager_servers():
    cfg = ts.ToolSearchConfig.from_raw({"eager_servers": ["Playwright", " playwright ", "", "x"]})
    assert cfg.eager_servers == ("playwright", "x")
    assert ts.ToolSearchConfig.from_raw({"eager_servers": "playwright"}).eager_servers == ("playwright",)
    assert ts.ToolSearchConfig.from_raw({}).eager_servers == ()
    assert ts.ToolSearchConfig.from_raw(None).eager_servers == ()
    assert ts.ToolSearchConfig.from_raw(True).eager_servers == ()


def test_eager_server_tools_stay_visible(monkeypatch):
    monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda n: n.startswith("mcp__"))
    defs = [_td("terminal"), _td("mcp__playwright__browser_click"), _td("mcp__cloudflare__dns_list")]
    visible, deferrable = ts.classify_tools(defs, eager_servers=("playwright",))
    assert [d["function"]["name"] for d in visible] == ["terminal", "mcp__playwright__browser_click"]
    assert [d["function"]["name"] for d in deferrable] == ["mcp__cloudflare__dns_list"]


def test_no_eager_servers_is_unchanged(monkeypatch):
    monkeypatch.setattr(ts, "is_deferrable_tool_name", lambda n: n.startswith("mcp__"))
    defs = [_td("terminal"), _td("mcp__playwright__browser_click"), _td("mcp__cloudflare__dns_list")]
    visible, deferrable = ts.classify_tools(defs, eager_servers=())
    assert [d["function"]["name"] for d in visible] == ["terminal"]
    assert len(deferrable) == 2


@pytest.mark.parametrize("single,mode,expected", [
    (True, "deny", True), (True, "approve", False), (False, "deny", False),
])
def test_execute_code_hidden_only_when_always_blocked(monkeypatch, single, mode, expected):
    import model_tools
    import tools.approval as ap
    monkeypatch.setattr(ap, "_is_single_query_approval_context", lambda: single)
    monkeypatch.setattr(ap, "_get_single_query_approval_mode", lambda: mode)
    assert model_tools._single_query_execute_code_denied() is expected
