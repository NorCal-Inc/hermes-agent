"""A named secondary profile whose home is missing/removed must fail CLOSED.

Two multiplex handler factories resolve a named secondary profile's home and
then run a body under ``_profile_runtime_scope``:

* ``_make_profile_platform_event_handler`` — observer events (repaired by
  ``bc12fc7048`` / ``b09a62bb80``).
* ``_make_profile_message_handler`` — real inbound messages.

Both are only ever called for a NAMED secondary profile, so an unresolvable
home is never "the launch profile's own work". Running the body with no scope
installed makes ``_auth_env`` / ``_platform_gate_env`` fall through to
``os.environ``, which under multiplexing holds the LAUNCH profile's values — so
one lane's allowlist and ``*_ALLOW_ALL_USERS`` flag decide another lane's
traffic. That is a fail-OPEN cross-lane admission decision.

The contract asserted here, for BOTH handlers:

1. a name with no directory behind it is reported and every call is dropped;
2. a home that vanishes after the factory ran is reported once and dropped;
3. ``_profile_runtime_scope`` is never entered for a home that is not there.

Assertion 3 is the load-bearing one: it fails for a handler that merely skips
dispatch but still scopes, and for one that scopes a nonexistent path (which
installs an EMPTY secret scope and points ``HERMES_HOME`` at nothing).

Upstream: ``4aa9baf139`` (principle). NorCal card ``t_ab4016a8``.
"""
import asyncio
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run import GatewayRunner


class _Source:
    def __init__(self):
        self.profile = None
        self.platform = None
        self.chat_id = "c1"


class _Event:
    def __init__(self):
        self.source = _Source()


def _event_handler(runner, name):
    """Build the platform-event handler and adapt it to a 1-arg call."""
    dispatch = AsyncMock()
    runner._handle_gateway_platform_event = dispatch
    handler = runner._make_profile_platform_event_handler(name)

    async def _call(event):
        return await handler({"event_type": "reaction"}, event.source)

    return _call, dispatch


def _message_handler(runner, name):
    dispatch = AsyncMock()
    runner._handle_message = dispatch
    return runner._make_profile_message_handler(name), dispatch


# (factory, the noun the warning uses)
HANDLERS = [
    pytest.param(_event_handler, "platform events", id="platform-event"),
    pytest.param(_message_handler, "inbound messages", id="inbound-message"),
]


def _isolate_profiles_root(monkeypatch, tmp_path) -> Path:
    """Point profile resolution at a temp profiles root and return it."""
    root = tmp_path / ".hermes"
    (root / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return root / "profiles"


@pytest.mark.parametrize("build,noun", HANDLERS)
class TestProfileHomeFailsClosed:
    def test_name_with_no_directory_is_reported_and_dropped(
        self, build, noun, monkeypatch, tmp_path, caplog,
    ):
        """Real resolution: ``get_profile_dir`` does NOT raise for a deleted profile."""
        from hermes_cli.profiles import get_profile_dir

        _isolate_profiles_root(monkeypatch, tmp_path)

        # Premise check. If real resolution ever starts raising for this case,
        # the assertions below stop proving anything.
        resolved = get_profile_dir("vanishedlane")
        assert not resolved.exists(), "fixture must model a profile with no directory"

        runner = object.__new__(GatewayRunner)
        with caplog.at_level("WARNING", logger="gateway.run"):
            handler, dispatch = build(runner, "vanishedlane")
        assert any(
            "does not resolve" in r.message and noun in r.message
            for r in caplog.records
        ), "an unresolvable profile home must be reported, not silently used"

        with patch("gateway.run._profile_runtime_scope") as scope:
            result = asyncio.run(handler(_Event()))

        assert result is None
        assert not scope.called, "a nonexistent home must never be entered as a scope"
        dispatch.assert_not_awaited()

    def test_home_removed_after_install_is_reported_once_and_dropped(
        self, build, noun, monkeypatch, tmp_path, caplog,
    ):
        """The factory runs once at adapter install; the home can go away later.

        ``profiles_to_serve`` only yields directories that exist at startup, so
        the realistic failure is a profile removed or renamed while its adapter
        is up — which a factory-time-only check cannot see.
        """
        from hermes_cli.profiles import get_profile_dir

        profiles = _isolate_profiles_root(monkeypatch, tmp_path)
        home = profiles / "worklane"
        home.mkdir()
        assert get_profile_dir("worklane") == home

        runner = object.__new__(GatewayRunner)
        handler, dispatch = build(runner, "worklane")

        with patch("gateway.run._profile_runtime_scope",
                   side_effect=lambda _home: nullcontext()):
            asyncio.run(handler(_Event()))
        dispatch.assert_awaited_once()

        home.rmdir()
        with caplog.at_level("WARNING", logger="gateway.run"):
            with patch("gateway.run._profile_runtime_scope") as scope:
                first = asyncio.run(handler(_Event()))
                second = asyncio.run(handler(_Event()))

        assert first is None and second is None
        scope.assert_not_called()
        dispatch.assert_awaited_once()  # still just the pre-removal dispatch
        vanished = [r for r in caplog.records if "disappeared" in r.message]
        assert len(vanished) == 1, (
            "a home vanishing mid-run is reported once, then dropped quietly")
        assert noun in vanished[0].message

    def test_resolution_raising_is_reported_and_dropped(
        self, build, noun, monkeypatch, caplog,
    ):
        """An invalid profile name genuinely raises; that must not read as launch-owned."""
        def _gone(_name):
            raise FileNotFoundError("profile deleted mid-run")

        monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", _gone)

        runner = object.__new__(GatewayRunner)
        with caplog.at_level("WARNING", logger="gateway.run"):
            handler, dispatch = build(runner, "work")
        assert any("does not resolve" in r.message for r in caplog.records)

        with patch("gateway.run._profile_runtime_scope") as scope:
            result = asyncio.run(handler(_Event()))

        assert result is None
        assert not scope.called
        dispatch.assert_not_awaited()

    def test_existing_home_is_scoped_and_dispatched(
        self, build, noun, monkeypatch, tmp_path,
    ):
        """The fail-closed guard must not break the healthy path it guards."""
        profiles = _isolate_profiles_root(monkeypatch, tmp_path)
        home = profiles / "goodlane"
        home.mkdir()

        runner = object.__new__(GatewayRunner)
        handler, dispatch = build(runner, "goodlane")

        entered = []
        with patch("gateway.run._profile_runtime_scope",
                   side_effect=lambda h: (entered.append(Path(h)), nullcontext())[1]):
            asyncio.run(handler(_Event()))

        assert entered == [home], "the lane's own home is the scope that gets entered"
        dispatch.assert_awaited_once()
