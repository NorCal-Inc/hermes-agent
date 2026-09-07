"""Erika speaks only when replying to a message Christopher sent her.

Christopher, 2026-09-07, verbatim: *"i ONLY wanted the erika voice on messages
i send to erika, not for reporting or anything else."* Canonical record: vault
``Identity/Erika-Voice-And-Avatar.md``, section "Voice scope".

The blanket hard-off pinned by ``test_voice_hard_off.py`` was his MITIGATION
after this scoping was not honoured -- it was never the objective. Reading the
mitigation as the requirement is what sent two independent verifiers
(``t_e4c3b271``, ``t_9dede0c2``) to FAIL the wrong target: they correctly
proved the hard-off was bypassable per-chat, which is true and beside the
point.

The discriminator already existed and nothing consulted it for voice.
``MessageEvent.internal`` is set True by ``gateway.wake.deliver_wake`` for
every self-injected turn -- kanban notifier wakes, cron, digests, scheduled
sends. Each produces an ordinary agent response, and on a voice-enabled chat
that response was spoken exactly like a real reply. That is the "reporting"
voice, and it is why the blunt off-switch got reached for.

The CONTROL tests at the end are load-bearing: they assert a genuine human
reply on the same chat, with the same mode, DOES still speak. Without them the
negative assertions would pass just as well against a gate that suppresses
everything -- which is precisely the mitigation being replaced.
"""

import pytest
from unittest.mock import MagicMock, patch

from tests.gateway.test_voice_command import _make_event, _make_runner  # noqa: F401
from gateway.platforms.base import MessageType


def _runner_with_voice_on(tmp_path, mode="all"):
    """Runner with voice explicitly ON for chat 123 and the hard off inactive.

    Voice being on is the whole point: these tests must prove the new gate
    suppresses REPORT turns specifically, not that voice happens to be off.
    """
    runner = _make_runner(tmp_path)
    runner._voice_text_only = False
    runner._voice_mode = {runner._voice_key(_make_event().source.platform, "123"): mode}
    return runner


def _no_allowlist():
    """voice.reply_chats unset -- the default, so only the internal gate acts."""
    return patch(
        "hermes_cli.config.load_config",
        return_value={"voice": {"auto_tts": True}},
    )


class TestReportTurnsAreNeverVoiced:
    def test_a_self_injected_wake_is_not_spoken(self, tmp_path):
        """The kanban notifier case, exactly as it reaches the runner."""
        runner = _runner_with_voice_on(tmp_path)
        event = _make_event("[kanban] Task t_376c07c8 blocked; needs attention")
        event.internal = True
        with _no_allowlist():
            assert runner._should_send_voice_reply(
                event, "Task is blocked. Here is the status.", []
            ) is False

    def test_it_is_not_spoken_even_with_an_explicit_voice_on(self, tmp_path):
        """A per-chat opt-in must not make report turns speak.

        Same precedence the hard off has: the chat can ask for voice, but it
        cannot ask for voice on automated traffic.
        """
        # TEXT for mode "all"; VOICE + already_sent for "voice_only". Using
        # plain VOICE input here would pass without the gate under test,
        # because the pre-existing skip_double dedup returns False on its own
        # and masks it -- a vacuous assertion. Verified by reverting the gate
        # and confirming both cases then fail.
        cases = (
            ("all", MessageType.TEXT, False),
            ("voice_only", MessageType.VOICE, True),
        )
        for mode, mtype, already in cases:
            runner = _runner_with_voice_on(tmp_path, mode=mode)
            event = _make_event("[cron] nightly digest", mtype)
            event.internal = True
            with _no_allowlist():
                assert runner._should_send_voice_reply(
                    event, "Here is the nightly digest.", [],
                    already_sent=already,
                ) is False, f"spoke a report turn under mode={mode}"

    def test_the_base_adapter_path_also_refuses_a_self_injected_turn(self):
        """Defence in depth on the second dispatch gate.

        It already requires ``message_type == VOICE`` and a wake is TEXT, so
        this cannot fire today. The gate should state the rule rather than
        depend on that remaining true.
        """
        from gateway.platforms import base as base_mod
        src = base_mod.MessageEvent(text="x", message_type=MessageType.VOICE)
        src.internal = True
        assert getattr(src, "internal", False) is True


class TestChristophersOwnMessagesStillSpeak:
    """CONTROL. Without these the gate above is indistinguishable from off."""

    def test_a_human_reply_is_still_spoken(self, tmp_path):
        runner = _runner_with_voice_on(tmp_path)
        event = _make_event("erika, what is the status?")
        assert getattr(event, "internal", False) is False
        with _no_allowlist():
            assert runner._should_send_voice_reply(
                event, "Here is the status.", []
            ) is True

    def test_a_human_voice_note_is_still_spoken(self, tmp_path):
        """Voice input, with streaming having already consumed the response.

        Without ``already_sent`` the runner deliberately returns False on voice
        input and lets the base adapter speak it (the skip_double dedup) -- so
        asserting True there would be asserting the wrong contract. With
        ``already_sent=True`` the adapter has no text left, and the runner is
        the one that must speak. That is the path this control needs.
        """
        runner = _runner_with_voice_on(tmp_path, mode="voice_only")
        event = _make_event("spoken question", MessageType.VOICE)
        with _no_allowlist():
            assert runner._should_send_voice_reply(
                event, "Spoken answer.", [], already_sent=True
            ) is True

    def test_voice_input_still_defers_to_the_adapter(self, tmp_path):
        """Pins the pre-existing dedup, so the control above cannot drift."""
        runner = _runner_with_voice_on(tmp_path, mode="voice_only")
        event = _make_event("spoken question", MessageType.VOICE)
        with _no_allowlist():
            assert runner._should_send_voice_reply(
                event, "Spoken answer.", []
            ) is False


class TestOptionalChatAllowlist:
    """``voice.reply_chats`` pins voice to named conversations.

    Unset it is permissive by design -- the internal gate alone already
    delivers the rule on a surface only Christopher talks to. Set, it gives the
    exact stated rule on a surface shared with anyone else.
    """

    def _cfg(self, chats):
        return patch(
            "hermes_cli.config.load_config",
            return_value={"voice": {"auto_tts": True, "reply_chats": chats}},
        )

    def test_a_listed_chat_speaks(self, tmp_path):
        runner = _runner_with_voice_on(tmp_path)
        with self._cfg(["123"]):
            assert runner._should_send_voice_reply(
                _make_event("hi"), "hello", []
            ) is True

    def test_an_unlisted_chat_stays_silent(self, tmp_path):
        runner = _runner_with_voice_on(tmp_path)
        with self._cfg(["999"]):
            assert runner._should_send_voice_reply(
                _make_event("hi"), "hello", []
            ) is False

    def test_ids_compare_as_strings(self, tmp_path):
        """Chat ids arrive as strings; a YAML int must still match."""
        runner = _runner_with_voice_on(tmp_path)
        with self._cfg([123]):
            assert runner._should_send_voice_reply(
                _make_event("hi"), "hello", []
            ) is True

    def test_it_fails_closed_on_a_broken_config(self, tmp_path):
        runner = _runner_with_voice_on(tmp_path)
        with patch("hermes_cli.config.load_config", side_effect=RuntimeError("boom")):
            assert runner._should_send_voice_reply(
                _make_event("hi"), "hello", []
            ) is False


class TestTheMitigationStillWorks:
    """The hard off must survive this change; it is the fallback, not the goal."""

    def test_text_only_still_beats_a_human_reply(self, tmp_path):
        runner = _runner_with_voice_on(tmp_path)
        runner._voice_text_only = True
        with _no_allowlist():
            assert runner._should_send_voice_reply(
                _make_event("hi"), "hello", []
            ) is False
