"""Regression test for kanban t_5807aa7f: the rate-limit provider-error
notice must post once per chat per cooldown window, not twice per event.

Repro before the fix: a single rate-limited request could surface the same
"please wait" envelope through both the mid-run status callback
(_prepare_gateway_status_message) and the final-response path
(_sanitize_gateway_final_response) for the same chat, posting the identical
notice twice. Every other error category (auth, policy, connection, generic)
must be completely unaffected by this change.
"""

import gateway.run as run


RATE_LIMIT_TEXT = "Rate limited after 3 retries"


def _reset_dedupe_state():
    run._gateway_rate_limit_notice_last_sent.clear()


class TestRateLimitNoticeDedupe:
    def setup_method(self):
        _reset_dedupe_state()

    def test_second_call_same_key_within_cooldown_is_suppressed(self):
        first = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:123")
        second = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:123")
        assert first == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT
        assert second == ""

    def test_posts_again_after_cooldown_elapses(self, monkeypatch):
        first = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:123")
        assert first == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

        # Simulate cooldown elapsing by moving the recorded timestamp into the past.
        run._gateway_rate_limit_notice_last_sent["telegram:123"] -= (
            run._GATEWAY_RATE_LIMIT_NOTICE_COOLDOWN_SECONDS + 1
        )
        third = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:123")
        assert third == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

    def test_different_chats_each_get_their_own_notice(self):
        reply_a = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:111")
        reply_b = run._gateway_provider_error_reply(RATE_LIMIT_TEXT, dedupe_key="telegram:222")
        assert reply_a == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT
        assert reply_b == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

    def test_no_dedupe_key_always_posts(self):
        """Callers that cannot resolve a chat id keep the old always-post behavior."""
        first = run._gateway_provider_error_reply(RATE_LIMIT_TEXT)
        second = run._gateway_provider_error_reply(RATE_LIMIT_TEXT)
        assert first == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT
        assert second == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

    def test_non_rate_limit_categories_unaffected_by_dedupe(self):
        """Auth/policy/connection/generic replies are never suppressed, even with
        the same dedupe_key repeated, because only the rate-limit branch dedupes."""
        key = "telegram:999"
        auth1 = run._gateway_provider_error_reply("provider authentication failed", dedupe_key=key)
        auth2 = run._gateway_provider_error_reply("provider authentication failed", dedupe_key=key)
        assert auth1 == auth2
        assert "authentication" in auth1.lower()

        conn1 = run._gateway_provider_error_reply("Errno 111 Connection refused", dedupe_key=key)
        conn2 = run._gateway_provider_error_reply("Errno 111 Connection refused", dedupe_key=key)
        assert conn1 == conn2
        assert "not running or is unreachable" in conn1

    def test_status_callback_and_final_response_share_one_notice_per_chat(self):
        """End-to-end: the two real call sites (status callback path and final
        response path) must collapse into a single notice for the same chat."""
        status_notice = run._prepare_gateway_status_message(
            "telegram", "tool.error", RATE_LIMIT_TEXT, chat_id="chat-42",
        )
        final_notice = run._sanitize_gateway_final_response(
            "telegram", RATE_LIMIT_TEXT, chat_id="chat-42",
        )
        assert status_notice == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT
        # Second notice for the same chat within the cooldown is suppressed.
        # _sanitize_gateway_final_response returns "" for the empty redacted text.
        assert final_notice == ""

    def test_status_callback_and_final_response_differ_across_chats(self):
        status_notice = run._prepare_gateway_status_message(
            "telegram", "tool.error", RATE_LIMIT_TEXT, chat_id="chat-A",
        )
        final_notice = run._sanitize_gateway_final_response(
            "telegram", RATE_LIMIT_TEXT, chat_id="chat-B",
        )
        assert status_notice == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT
        assert final_notice == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

    def test_real_empty_final_response_fallback_does_not_resurrect_deduped_notice(self):
        """Review finding (t_6569186b, FAIL): the early-return branch in
        GatewayRunner.run_sync converts an empty _sanitize_gateway_final_response
        result back into a raw f"⚠️ {result['error']}" string. Before the fix,
        that fallback ran unconditionally and could re-post the exact rate-limit
        notice text the cooldown dedupe had just suppressed -- defeating item 3.
        This exercises the EXACT extracted function used by that call site
        (_finalize_empty_gateway_response), not just the sanitizer helpers.
        """
        result = {"failed": True, "error": "Rate limited after 3 retries", "api_calls": 1}

        first = run._finalize_empty_gateway_response(
            result, history_len=5, platform="telegram", chat_id="chat-dedupe-1",
        )
        assert first == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

        # Second call for the SAME chat within the cooldown: the dedupe
        # suppresses the sanitizer's notice, and the fallback must NOT
        # resurrect it via the raw result['error'] text.
        second = run._finalize_empty_gateway_response(
            result, history_len=5, platform="telegram", chat_id="chat-dedupe-1",
        )
        assert second == ""

        # A different chat is unaffected and still gets its own notice.
        third = run._finalize_empty_gateway_response(
            result, history_len=5, platform="telegram", chat_id="chat-dedupe-2",
        )
        assert third == run._GATEWAY_RATE_LIMIT_NOTICE_TEXT

    def test_real_empty_final_response_fallback_still_surfaces_non_rate_limit_errors(self):
        """Non-rate-limit failures must still fall back to the raw error text
        when normalization/sanitization produce nothing usable -- the dedupe
        carve-out must not swallow unrelated failures."""
        result = {"failed": False, "error": None, "api_calls": 0, "interrupted": False, "partial": False}
        # api_calls==0, not failed, not interrupted, not partial:
        # _normalize_empty_agent_response returns its generic retry-hint
        # text (non-empty), which doesn't match the provider-error shape,
        # so the sanitizer passes it through untouched.
        out = run._finalize_empty_gateway_response(
            result, history_len=0, platform="telegram", chat_id="chat-generic",
        )
        assert out != ""
        assert "wasn't processed" in out or "processed" in out.lower()

