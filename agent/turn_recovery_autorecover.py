"""Bounded post-exhaustion auto-recovery ladder for a pre-delivery turn (#85426, #107307).

Runs from the retry loop in ``agent/conversation_loop.py`` once ``max_retries`` is spent AND the
fallback chain has nothing left to move to (fallback stays first). While the provider is only
*temporarily* away (5xx, overloaded/529, connect/read timeouts) and no answer text has reached the
user yet, the turn parks with a visible countdown instead of ending in "API failed after N
retries", then re-enters the ordinary retry loop. Cycles: ``agent.auto_recovery_cycles`` (default
5) with a jittered 15/30/60/60/60 s schedule; a provider ``Retry-After`` wins up to 120 s.
Non-retryable classes (auth, format, content policy, billing, entitlement) never enter, and an
interrupt cancels the wait cleanly. Overload-class errors use this same schedule — there is no
separate overload backoff path.

Fork note: upstream drives the wait through ``agent.turn_recovery.interruptible_backoff_sleep``;
this fork keeps its interruptible retry wait inline in ``conversation_loop``, so the loop owns the
wait and this module owns eligibility, schedule, bookkeeping and the user-facing notice.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from agent.error_classifier import FailoverReason

logger = logging.getLogger("agent.conversation_loop")

# Transient transport verdicts: the provider is expected back. Everything else (auth, format,
# content policy, billing, entitlement, overflow, model-not-found ...) is deterministic for this
# request and stays out.
_LADDER_REASONS = frozenset({FailoverReason.overloaded, FailoverReason.server_error, FailoverReason.timeout})

_LADDER_BASE_DELAY_S = 15.0
_LADDER_CAP_S = 60.0
# A provider that names its own cooldown knows better than the schedule, within reason.
_RETRY_AFTER_CAP_S = 120.0

# How the user stops the wait on this surface; the ladder text must say so plainly.
_STOP_HINTS = {
    "cli": "press Esc to stop",
    "tui": "press Esc to stop",
    "desktop": "press Esc to stop",
    "api_server": "cancel the request to stop",
    "cron": "",
}
_DEFAULT_STOP_HINT = "send /stop to cancel"


def auto_recovery_cycles(agent: Any) -> int:
    """Configured ``agent.auto_recovery_cycles`` (0 disables the ladder)."""
    return max(int(getattr(agent, "_auto_recovery_cycles", 0) or 0), 0)


def _retry_after_seconds(api_error: Any) -> Optional[float]:
    """Provider-declared cooldown from the ``Retry-After`` header or a ``retry_after`` body field."""
    from agent.retry_utils import parse_retry_after_seconds
    value = parse_retry_after_seconds(getattr(getattr(api_error, "response", None), "headers", None))
    if value is None:
        body = getattr(api_error, "body", None)
        if isinstance(body, dict):
            nested = body.get("error")
            value = parse_retry_after_seconds((nested if isinstance(nested, dict) else body).get("retry_after"))
    return value if value is not None and value > 0 else None


def ladder_wait_seconds(cycle: int, api_error: Any) -> float:
    """Wait before recovery ``cycle`` (1-based): jittered 15/30/60/60/60 s, or the provider's
    ``Retry-After`` when present (honoured past the 60 s cap, up to 120 s)."""
    from agent.retry_utils import jittered_backoff
    retry_after = _retry_after_seconds(api_error)
    if retry_after is not None:
        return min(retry_after, _RETRY_AFTER_CAP_S)
    return jittered_backoff(cycle, base_delay=_LADDER_BASE_DELAY_S, max_delay=_LADDER_CAP_S, jitter_ratio=0.2)


def ladder_eligible(agent: Any, classified: Any) -> bool:
    """True when the ladder may engage: cycles configured, a transient transport verdict, and no
    answer text streamed to the user yet (delivered text is never replayed by this path)."""
    if auto_recovery_cycles(agent) <= 0 or classified.reason not in _LADDER_REASONS:
        return False
    streamed = getattr(agent, "_current_streamed_assistant_text", "") or ""
    return not agent._has_content_after_think_block(streamed)


def ladder_notice(agent: Any, *, wait_s: float, cycle: int, total: int) -> str:
    hint = _STOP_HINTS.get(str(getattr(agent, "platform", "") or "").lower(), _DEFAULT_STOP_HINT)
    text = f"⏳ Provider temporarily unavailable — retrying automatically in {wait_s:.0f}s (cycle {cycle}/{total})"
    return f"{text}; {hint}" if hint else text


def start_recovery_cycle(agent: Any, api_error: Any, classified: Any, _retry: Any) -> Optional[Dict[str, Any]]:
    """Begin one recovery cycle after retries + fallback are exhausted.

    Returns ``{"wait_s", "cycle", "total"}`` when the caller should wait (interruptibly) and then
    re-enter the retry loop with ``retry_count = 0``; ``None`` when the ladder does not apply or is
    spent (the caller falls through to the terminal "API failed" path). The notice goes to the
    status rail AND the live wait line, plus a log line for cron.
    """
    if not ladder_eligible(agent, classified):
        return None
    total = auto_recovery_cycles(agent)
    used = int(getattr(_retry, "auto_recovery_cycles_used", 0) or 0)
    if used >= total:
        agent._emit_status(
            f"⏳ Automatic recovery gave up after {total} cycles — the provider is still unavailable."
        )
        return None
    cycle = used + 1
    _retry.auto_recovery_cycles_used = cycle
    wait_s = ladder_wait_seconds(cycle, api_error)
    notice = ladder_notice(agent, wait_s=wait_s, cycle=cycle, total=total)
    # Durable line on every surface (CLI print, TUI status, gateway bubble) plus the live wait
    # line (spinner / thinking.delta / activity heartbeat).
    agent._emit_status(notice)
    agent._emit_wait_notice(notice)
    logger.warning(
        "%sProvider unavailable (%s) — auto-recovery cycle %d/%d, retrying in %.0fs %s",
        agent.log_prefix, classified.reason.value, cycle, total, wait_s, agent._client_log_context(),
    )
    return {"wait_s": wait_s, "cycle": cycle, "total": total}
