"""GUI capability follows the SESSION's client, not the backend's process env.

The desktop app is a client. It can drive a backend that Electron spawned
locally, one reached over SSH, one behind a plain URL+token, or Hermes Cloud —
and only the first two run with ``HERMES_DESKTOP=1`` in their environment.
Gating the pane/browser/reaction tools on that env var therefore stripped every
one of them from URL and cloud gateways, while the same backend still told the
model "You are chatting inside the Hermes desktop app".

These tests pin the contract that replaced it: eligibility is resolved from the
session's own ``source`` (``session.create``'s ``source: 'desktop'``), so the
answer is identical on every connection topology.
"""

import pytest

import tui_gateway.server as server
from toolsets import TOOLSETS, resolve_toolset

GUI_TOOLS = {
    "close_terminal",
    "focus_pane",
    "open_preview",
    "read_preview",
    "read_terminal",
    "read_window_below",
    "react_to_message",
    "setup_mcp",
    "tour",
}


@pytest.fixture
def no_desktop_env(monkeypatch):
    """A backend nobody told about the desktop — i.e. every remote gateway."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    return monkeypatch


class TestDesktopUiToolset:
    def test_holds_exactly_the_gui_affordances(self):
        assert set(resolve_toolset("desktop_ui")) == GUI_TOOLS

    def test_stays_off_the_core_tool_list(self):
        """Core ships on every API call — a GUI-only tool must not be there."""
        from toolsets import _HERMES_CORE_TOOLS

        assert GUI_TOOLS.isdisjoint(_HERMES_CORE_TOOLS)

    def test_no_platform_bundle_carries_it(self):
        """Messaging/CLI bundles must not pick these up by listing them."""
        for name, spec in TOOLSETS.items():
            if name == "desktop_ui":
                continue
            assert GUI_TOOLS.isdisjoint(set(spec.get("tools") or ())), name


class TestSurfaceResolution:
    def test_desktop_session_gets_them_with_no_desktop_env(self, no_desktop_env):
        """THE regression: a desktop client on a remote/cloud backend."""
        assert "desktop_ui" in server._gui_surface_toolsets("desktop")

    def test_tui_session_does_not(self, no_desktop_env):
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_desktop_env_alone_does_not_grant_them(self, no_desktop_env):
        """A desktop-spawned backend serving a TUI session stays clean.

        The embedded terminal pane runs `hermes --tui` against this same
        backend; env-keyed gating handed it GUI tools it cannot answer.
        """
        no_desktop_env.setenv("HERMES_DESKTOP", "1")
        assert "desktop_ui" not in server._gui_surface_toolsets("tui")

    def test_project_tools_ride_on_every_gui_surface(self, no_desktop_env):
        for platform in ("desktop", "tui"):
            assert "project" in server._gui_surface_toolsets(platform)


class TestResolverPlumbing:
    def test_posture_path_folds_in_the_session_surface(self, no_desktop_env):
        """Focus-mode returns early — the surface toolsets must survive it."""
        import agent.coding_context as cc

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: ["coding"])

        assert server._load_enabled_toolsets("desktop") == [
            "coding",
            "desktop_ui",
            "project",
        ]
        assert server._load_enabled_toolsets("tui") == ["coding", "project"]

    def test_config_path_folds_in_the_session_surface(self, no_desktop_env):
        import agent.coding_context as cc
        import hermes_cli.config as config_mod

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)
        no_desktop_env.setattr(
            config_mod, "load_config", lambda: {"platform_toolsets": {"cli": ["memory"]}}
        )

        desktop = server._load_enabled_toolsets("desktop")
        tui = server._load_enabled_toolsets("tui")

        assert desktop is not None and tui is not None
        assert "desktop_ui" in desktop
        assert "desktop_ui" not in tui

    def test_explicit_env_pin_still_wins(self, no_desktop_env):
        """HERMES_TUI_TOOLSETS is an operator override; surface can't re-add."""
        no_desktop_env.setenv("HERMES_TUI_TOOLSETS", "web,memory")

        assert server._load_enabled_toolsets("desktop") == ["web", "memory"]


class TestExplicitEmptySelection:
    """#82010: an explicitly saved empty platform_toolsets list is a zero-tool state.

    ``platform_toolsets.cli: []`` used to fall open — the resolver returned an empty set and
    the gateway converted it to None, which downstream means 'no restriction' (every
    toolset). Only an ABSENT key means that; the explicit empty list must return [].
    """

    @staticmethod
    def _config(no_desktop_env, platform_toolsets):
        import agent.coding_context as cc
        import hermes_cli.config as config_mod

        no_desktop_env.setattr(cc, "coding_selection", lambda **_: None)
        no_desktop_env.setattr(
            config_mod, "load_config", lambda: {"platform_toolsets": platform_toolsets}
        )

    def test_explicit_empty_list_yields_no_toolsets(self, no_desktop_env):
        # Fork note: the NorCal fork's non-configurable ``executive`` toolset is recovered
        # by _get_platform_tools even for ``cli: []``, so pin the resolver to the upstream
        # zero-set result to exercise the gateway's fail-closed branch directly.
        import hermes_cli.tools_config as tools_config

        self._config(no_desktop_env, {"cli": []})
        no_desktop_env.setattr(tools_config, "_get_platform_tools", lambda *a, **k: set())

        assert server._load_enabled_toolsets("desktop") == []
        assert server._load_enabled_toolsets("tui") == []

    def test_explicit_empty_list_never_falls_open(self, no_desktop_env):
        # With the real resolver, ``cli: []`` must still be a restriction (never None = all).
        self._config(no_desktop_env, {"cli": []})

        result = server._load_enabled_toolsets("tui")
        assert result is not None
        assert "terminal" not in result

    def test_absent_key_keeps_the_default_selection(self, no_desktop_env):
        self._config(no_desktop_env, {})

        result = server._load_enabled_toolsets("tui")
        assert result  # the platform default applies — not the zero state, not None-as-all

    def test_named_selection_is_honored(self, no_desktop_env):
        self._config(no_desktop_env, {"cli": ["memory"]})

        result = server._load_enabled_toolsets("tui")
        assert result is not None
        assert "memory" in result
        assert "terminal" not in result
