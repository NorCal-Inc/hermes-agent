"""Regression tests: global text-only mode is a hard off for outgoing TTS.

Christopher's requirement is that *all* outgoing voice comments stay disabled.
``voice.auto_tts: false`` used to express only a **default**: an explicit
per-chat ``/voice on`` or ``/voice tts`` beat it and re-enabled outgoing voice
for that chat, persisting in ``gateway_voice_mode.json``. Independent verifier
``t_e4c3b271`` (run 2581) proved that bypass live and returned FAIL with:

    CONFIG_FALSE_EXPLICIT_OPT_IN_BASE_GATE=True
    CONFIG_FALSE_EXPLICIT_OPT_IN_RUNNER_GATE=True

Those two names are the two dispatch gates this module pins shut:

  * **base gate** — ``BasePlatformAdapter._should_auto_tts_for_chat()``
    (``gateway/platforms/base.py``), which fronts adapter auto-TTS on voice
    input *and* the ``StreamingTTSConsumer`` setup in ``gateway/run.py``.
  * **runner gate** — ``GatewayRunner._should_send_voice_reply()``
    (``gateway/run.py``), the whole-response auto voice reply.

Both must return False while text-only is active, no matter what per-chat mode
the user set. ``/voice on`` sets mode ``voice_only``; ``/voice tts`` sets mode
``all`` (see ``gateway/slash_commands.py::_handle_voice_command``) — both are
exercised here through the real command handler, not by writing the mode in.

Nothing here synthesizes audio, calls a TTS provider, or sends a message: every
assertion is on a pure predicate. Inbound STT/transcription is untouched by the
gates under test and is asserted unchanged at the bottom.

The CONTROL tests at the end are load-bearing. They assert the same paths *do*
fire once text-only is off, which is what makes the negative assertions above
falsifiable rather than vacuous.
"""

import pytest
from unittest.mock import MagicMock, patch

from tests.gateway.test_voice_command import _make_event, _make_runner  # noqa: F401
from gateway.platforms.base import BasePlatformAdapter, MessageType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hard_off_runner(tmp_path):
    """Runner in global text-only mode, with voice-mode persistence redirected.

    ``_make_runner`` points ``_VOICE_MODE_PATH`` at ``tmp_path`` so running
    ``/voice on`` here can never touch the live ``~/.hermes/gateway_voice_mode.json``.
    """
    runner = _make_runner(tmp_path)
    runner._voice_text_only = True
    return runner


def _bare_adapter(*, default: bool, enabled=(), disabled=()):
    """Adapter carrying only the three attributes the base gate reads."""
    adapter = MagicMock()
    adapter._auto_tts_default = default
    adapter._auto_tts_enabled_chats = set(enabled)
    adapter._auto_tts_disabled_chats = set(disabled)
    # Bind the *real* predicate, not the MagicMock's auto-attribute.
    adapter._should_auto_tts_for_chat = (
        lambda chat_id: BasePlatformAdapter._should_auto_tts_for_chat(adapter, chat_id)
    )
    return adapter


async def _run_voice_command(runner, event, args: str):
    """Drive the real /voice slash-command handler for ``args``."""
    event.get_command_args = lambda: args
    with patch("gateway.slash_commands.t", side_effect=lambda key, **kw: key):
        return await runner._handle_voice_command(event)


# ---------------------------------------------------------------------------
# Runner gate — CONFIG_FALSE_EXPLICIT_OPT_IN_RUNNER_GATE
# ---------------------------------------------------------------------------

class TestRunnerGateHardOff:
    """``/voice on`` and ``/voice tts`` cannot make the runner speak."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command,expected_mode", [("on", "voice_only"), ("tts", "all")])
    @pytest.mark.parametrize("message_type", [MessageType.TEXT, MessageType.VOICE])
    @pytest.mark.parametrize("already_sent", [False, True])
    async def test_explicit_opt_in_cannot_reenable_runner(
        self, tmp_path, command, expected_mode, message_type, already_sent
    ):
        """The bypass verifier t_e4c3b271 found, pinned shut.

        ``already_sent=True`` with VOICE input is the worst case: it defeats
        the ``skip_double`` dedup, so nothing but the hard off stops the send.
        """
        runner = _hard_off_runner(tmp_path)
        event = _make_event(message_type=message_type)
        adapter = _bare_adapter(default=False)
        runner.adapters[event.source.platform] = adapter

        await _run_voice_command(runner, event, command)

        # The preference is still recorded — it just cannot fire.
        assert runner._voice_mode["telegram:123"] == expected_mode

        assert runner._should_send_voice_reply(
            event, "hello", [], already_sent=already_sent
        ) is False

    @pytest.mark.asyncio
    async def test_discord_voice_channel_mode_cannot_reenable_runner(self, tmp_path):
        """The Discord ``/voice channel`` join also writes mode ``all``."""
        runner = _hard_off_runner(tmp_path)
        event = _make_event(message_type=MessageType.VOICE)
        runner._voice_mode["telegram:123"] = "all"
        runner.adapters[event.source.platform] = _bare_adapter(default=False)

        assert runner._should_send_voice_reply(
            event, "hello", [], already_sent=True
        ) is False

    @pytest.mark.asyncio
    async def test_hard_off_beats_an_adapter_that_reports_true(self, tmp_path):
        """A stale/rogue adapter answering True must not reopen the runner path."""
        runner = _hard_off_runner(tmp_path)
        event = _make_event(message_type=MessageType.VOICE)
        adapter = MagicMock()
        adapter._should_auto_tts_for_chat = MagicMock(return_value=True)
        runner.adapters[event.source.platform] = adapter

        assert runner._should_send_voice_reply(
            event, "hello", [], already_sent=True
        ) is False


# ---------------------------------------------------------------------------
# Base/adapter gate — CONFIG_FALSE_EXPLICIT_OPT_IN_BASE_GATE
# ---------------------------------------------------------------------------

class TestBaseGateHardOff:
    """The predicate behind adapter auto-TTS and StreamingTTSConsumer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["on", "tts"])
    async def test_explicit_opt_in_cannot_reenable_base_gate(self, tmp_path, command):
        """``/voice on`` / ``/voice tts`` populate the opt-in set but never fire."""
        runner = _hard_off_runner(tmp_path)
        event = _make_event()
        adapter = _bare_adapter(default=False)
        runner.adapters[event.source.platform] = adapter

        await _run_voice_command(runner, event, command)

        # The command really did record the opt-in on the adapter...
        assert "123" in adapter._auto_tts_enabled_chats
        # ...and the gate still refuses.
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is False

    def test_opt_in_and_opt_out_both_lose_to_hard_off(self):
        adapter = _bare_adapter(default=False, enabled={"123"}, disabled={"123"})
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is False

    def test_unset_default_fails_closed(self):
        """An adapter that never received the config push stays silent.

        ``_auto_tts_default`` initialises to False in ``__init__``, so a chat
        that opted in before any sync cannot fire.
        """
        adapter = _bare_adapter(default=False, enabled={"123"})
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is False

    @pytest.mark.parametrize("bad_default", [None, 0, ""])
    def test_falsy_non_bool_default_fails_closed(self, bad_default):
        adapter = _bare_adapter(default=bad_default, enabled={"123"})
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is False


# ---------------------------------------------------------------------------
# _voice_hard_off() resolution
# ---------------------------------------------------------------------------

class TestVoiceHardOffResolution:
    """The runner reads config live, and fails closed on anything unexpected."""

    def _runner(self, tmp_path):
        runner = _make_runner(tmp_path)
        runner._voice_text_only = None  # resolve from config, as in production
        return runner

    @pytest.mark.parametrize("cfg,expected", [
        ({"voice": {"auto_tts": False}}, True),
        ({"voice": {"auto_tts": True}}, False),
        ({"voice": {}}, True),        # key absent -> text-only
        ({}, True),                   # no voice block -> text-only
        ({"voice": None}, True),      # null block -> text-only
    ])
    def test_resolves_from_live_config(self, tmp_path, cfg, expected):
        runner = self._runner(tmp_path)
        with patch("hermes_cli.config.load_config", return_value=cfg):
            assert runner._voice_hard_off() is expected

    def test_config_error_fails_closed(self, tmp_path):
        runner = self._runner(tmp_path)
        with patch("hermes_cli.config.load_config", side_effect=OSError("boom")):
            assert runner._voice_hard_off() is True

    def test_missing_attribute_resolves_from_config(self, tmp_path):
        """A runner built before this field existed must not crash or fail open."""
        runner = self._runner(tmp_path)
        del runner._voice_text_only
        with patch("hermes_cli.config.load_config", return_value={"voice": {"auto_tts": False}}):
            assert runner._voice_hard_off() is True

    def test_config_change_takes_effect_without_reconnect(self, tmp_path):
        """Turning voice off mid-session must not wait for an adapter reconnect."""
        runner = self._runner(tmp_path)
        with patch("hermes_cli.config.load_config", return_value={"voice": {"auto_tts": True}}):
            assert runner._voice_hard_off() is False
        with patch("hermes_cli.config.load_config", return_value={"voice": {"auto_tts": False}}):
            assert runner._voice_hard_off() is True


# ---------------------------------------------------------------------------
# Inbound audio is untouched
# ---------------------------------------------------------------------------

class TestInboundUnaffected:
    """The hard off gates *outgoing* TTS only. STT/transcription is unchanged."""

    def test_stt_transcript_echo_still_follows_its_own_config(self, tmp_path):
        runner = _hard_off_runner(tmp_path)
        runner.config = MagicMock()
        runner.config.stt_echo_transcripts = True
        assert runner._should_echo_stt_transcripts() is True
        runner.config.stt_echo_transcripts = False
        assert runner._should_echo_stt_transcripts() is False

    @pytest.mark.asyncio
    async def test_voice_command_still_records_state_under_hard_off(self, tmp_path):
        """Preferences survive so they apply if voice is ever re-enabled."""
        runner = _hard_off_runner(tmp_path)
        event = _make_event()
        runner.adapters[event.source.platform] = _bare_adapter(default=False)

        await _run_voice_command(runner, event, "tts")
        assert runner._voice_mode["telegram:123"] == "all"
        await _run_voice_command(runner, event, "off")
        assert runner._voice_mode["telegram:123"] == "off"


# ---------------------------------------------------------------------------
# CONTROLS — prove the assertions above are falsifiable
# ---------------------------------------------------------------------------

class TestControlsNotVacuous:
    """Without these, "no voice" could equally mean "the test never looked"."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command,message_type", [
        ("on", MessageType.VOICE),   # voice_only fires on voice input
        ("tts", MessageType.TEXT),   # all fires on any input
    ])
    async def test_runner_fires_when_hard_off_inactive(self, tmp_path, command, message_type):
        runner = _make_runner(tmp_path)          # _voice_text_only = False
        event = _make_event(message_type=message_type)
        runner.adapters[event.source.platform] = _bare_adapter(default=True)

        await _run_voice_command(runner, event, command)

        assert runner._should_send_voice_reply(
            event, "hello", [], already_sent=True
        ) is True

    def test_base_gate_fires_when_default_true(self):
        adapter = _bare_adapter(default=True)
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is True

    def test_base_gate_still_honours_explicit_off(self):
        """``/voice off`` keeps working under a True global default."""
        adapter = _bare_adapter(default=True, disabled={"123"})
        assert BasePlatformAdapter._should_auto_tts_for_chat(adapter, "123") is False
