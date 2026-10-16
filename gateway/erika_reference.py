"""Deterministic Erika / NorCal_Hermes Discord command reference.

Answers "list commands" and obvious equivalents on Discord without an LLM
turn, so the reply comes from live source/config instead of model memory.
Everything advertised is read at call time: gateway slash commands from the
command registry, schedules from enabled cron jobs that deliver to Discord,
channels from ``discord/channels.json``.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

# Slash commands worth advertising. Each is advertised only if the registry
# resolves it and it is available on the gateway right now.
_SLASH_ORDER = (
    "help", "commands", "status", "whoami", "new", "stop",
    "model", "profile", "background", "queue", "kanban",
)

_MENTION_RE = re.compile(r"<@[!&]?\d+>")
_EQUIV_RES = tuple(
    re.compile(p)
    for p in (
        r"^(?:(?:please|pls|erika|hey erika)\s+)*"
        r"(?:list|show|display|give|print|send|tell)(?:\s+me)?"
        r"(?:\s+(?:all|your|the|available|erika s|erikas|slash))*\s+commands?"
        r"(?:\s+(?:please|pls))?$",
        r"^(?:(?:please|pls|erika)\s+)*what\s+(?:commands|can\s+you\s+do)"
        r"(?:\s+(?:do\s+you\s+have|are\s+available|can\s+i\s+(?:use|run)))?$",
        r"^(?:(?:please|pls|erika)\s+)*(?:commands?|command\s+list|command\s+reference|help)$",
    )
)


def is_commands_request(text: Optional[str]) -> bool:
    """True for ``list commands`` and obvious equivalents (plain text only)."""
    if not text:
        return False
    t = _MENTION_RE.sub(" ", text).lower().replace("'", "")
    t = re.sub(r"[^a-z0-9/ ]+", " ", t)
    t = " ".join(t.split())
    if not t or t.startswith("/"):
        return False
    return any(r.match(t) for r in _EQUIV_RES)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _slash_lines() -> list[str]:
    from hermes_cli.commands import _is_gateway_available, _resolve_config_gates, resolve_command

    overrides = _resolve_config_gates()
    out = []
    for name in _SLASH_ORDER:
        cmd = resolve_command(name)
        if cmd is None or not _is_gateway_available(cmd, overrides):
            continue
        args = f" {cmd.args_hint}" if cmd.args_hint else ""
        out.append(f"`/{cmd.name}{args}` - {cmd.description}")
    return out


def _channel_names(home: Path) -> dict[str, str]:
    data = _load_json(home / "discord" / "channels.json")
    return {str(v): str(k) for k, v in data.items()} if isinstance(data, dict) else {}


def _discord_targets(deliver: Any, names: dict[str, str]) -> list[str]:
    out = []
    for part in str(deliver or "").split(","):
        part = part.strip()
        if part.startswith("discord:"):
            cid = part.split(":", 1)[1]
            out.append(f"#{names[cid]}" if cid in names else "a Discord channel")
    return out


def enabled_discord_schedules(home: Path) -> list[tuple[str, str, str]]:
    """(name, schedule, destinations) for enabled cron jobs delivering to Discord."""
    names = _channel_names(home)
    paths = [home / "cron" / "jobs.json"] + sorted(home.glob("profiles/*/cron/jobs.json"))
    rows = []
    for p in paths:
        data = _load_json(p)
        jobs = data.get("jobs", []) if isinstance(data, dict) else (data or [])
        for j in jobs:
            if not isinstance(j, dict) or not j.get("enabled") or j.get("paused_at"):
                continue
            dest = _discord_targets(j.get("deliver"), names)
            if dest:
                rows.append((str(j.get("name") or j.get("id")),
                             str(j.get("schedule_display") or j.get("schedule")),
                             ", ".join(dest)))
    return sorted(set(rows))


def _daily_reports_enabled() -> bool:
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return any("discord_daily_reports.py" in ln and not ln.lstrip().startswith("#")
               for ln in out.splitlines())


def build_reference(home: Path, *, free_response: str = "") -> str:
    lines = ["**Erika - NorCal_Hermes command reference**", ""]
    lines.append("**Talk to me**")
    lines.append("- @Erika in any channel (Christopher only). Ask `list commands` any time for this reference.")
    if free_response:
        lines.append("- #erika is a direct chat: no @ needed.")
    slash = _slash_lines()
    if slash:
        lines += ["", "**Slash commands**"] + [f"- {s}" for s in slash]
    names = _channel_names(home)
    if names:
        lines += ["", "**Discord channels** (" + ", ".join(f"#{n}" for n in names.values()) + ")"]
        lines.append("- #alerts is the home channel. Company channels carry only that company's reports; a channel existing does not mean the entity is formed or monitored.")
    sched = enabled_discord_schedules(home)
    daily = _daily_reports_enabled()
    if sched or daily:
        lines += ["", "**Enabled schedules that post to Discord**"]
        if daily:
            lines.append("- Daily company reports - 23:55 CT - each company's channel")
        lines += [f"- {n} - {s} -> {d}" for n, s, d in sched]
    lines += ["", "**Guardrails**",
              "- Financial and production actions require Christopher's explicit authorization.",
              "- Company data stays inside its own company channel."]
    return "\n".join(lines)


def commands_reference() -> str:
    """Reference text for the running gateway."""
    from hermes_constants import get_hermes_home

    free = ""
    try:
        import yaml
        cfg = yaml.safe_load((get_hermes_home() / "config.yaml").read_text(encoding="utf-8")) or {}
        free = str((cfg.get("discord") or {}).get("free_response_channels") or "")
    except Exception:
        pass
    return build_reference(get_hermes_home(), free_response=free)
