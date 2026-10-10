"""Execution-contract rule 6 (production authority), segment 6b: the
REPORT-ONLY production-surface gate.

Doctrine basis: hierarchy.md 2.5 "Production authority". Design approved by
Christopher 2026-10-07 (options note t_92521910, gate-rule6-OPTIONS.md):
1C production-tagged credentials plus a SHORT named surface list; 2B a pinned
registry; 3A a structured ``production_actions`` task field; report-only
first. Ruling 2026-10-09 (``norcal/security/production-roles/``): EVERY role
is ``christopher_only``; no Hermes profile can hold one. A production action
is legitimate only when

  (a) the executor lane is in the registry's
      ``christopher_authorized_executors`` (``claude``, ``codex``), AND
  (b) the card carries Christopher's authorization for that exact role in
      its structured ``production_actions`` field (built by card 6c; read
      here with ``getattr(task, "production_actions", None) or []`` so an
      absent field means "no authorization").

This module is the 1C *B half* (surface matching). It runs in the Hermes
tool dispatcher (``model_tools.handle_function_call``) right after the
rule 1-2 task-contract hook. A process that reaches this hook is a
Hermes-native agent, which is never one of the authorized executors (Claude
Code and Codex are separate processes that do not dispatch through
``model_tools``). So every surface match seen here is, by construction,
unauthorized -- and that is exactly what this segment is for: to show, on
the card, where production-shaped actions are happening today before the
gate is flipped to refuse.

What the gate does on a match, in ``mode: report-only`` (the only mode this
segment implements):

* records ONE ``production_action_unauthorized`` event per matched role on
  the card named by ``HERMES_KANBAN_TASK`` (payload: role, tool name, matcher
  id, executor lane, registry state -- NEVER the command text, code, path or
  file content, which can contain secrets);
* when there is no card (interactive session, no ``HERMES_KANBAN_TASK``),
  logs one WARNING line with the same redacted fields;
* returns ``None`` -- the tool call is ALLOWED. Always. Nothing in this file
  refuses anything.

What refuse mode will do later (NOT in this segment): the same
``ProductionDecision`` is computed; when ``decision.authorized`` is False and
the registry says ``mode: refuse``, the hook returns a refusal string (the
dispatcher turns it into a tool error, exactly like the rule 1-2 gate) and
records ``production_action_refused`` instead. Until that segment lands, a
registry whose ``mode`` is anything other than ``report-only`` is still
treated as report-only here, and the registry state is recorded in the event
so the mismatch is visible.

The credential half (1C, A half: ``hermes-run`` refusing to load
production-tagged secrets into an unauthorized profile) is NOT here.

Surface list (one entry per role; read-only forms deliberately do not match):

``firewall``
    ``ufw``, ``nft``, ``iptables``, ``ip6tables``, ``iptables-restore``,
    ``ip6tables-restore`` as the command of a shell segment (after stripping
    ``sudo``/``env``/``VAR=val`` prefixes). Read-only, no match: ``ufw
    status|show|version|help``; ``iptables``/``ip6tables`` without any of the
    chain-mutating flags (``-A -I -D -R -F -X -P -N -E -Z`` and their long
    forms), so ``-L``/``-S`` listings pass; ``nft list|get|describe|monitor``.

``ports``
    ``write_file``/``patch`` whose target path is under a systemd directory
    (``/etc/systemd/``, ``~/.config/systemd/``) or under
    ``/home/chris/services/``, AND whose written text (``content`` or
    ``new_string``/``patch``) contains a listen-port/bind-address line:
    ``PORT=``, ``LISTEN_PORT=``, ``BIND=``, ``BIND_ADDR(ESS)=``, ``HOST=``,
    ``LISTEN_HOST=``, ``ListenStream=``, ``ListenDatagram=``, ``listen ``
    (nginx), optionally prefixed by ``Environment=``; or ``--port``,
    ``--bind``, ``--host``, ``--listen`` followed by a value. Terminal-side
    port edits (``sed -i`` on a unit) are NOT matched by this role; a unit
    edit through ``systemctl edit`` is caught by ``service_units``.

``service_units``
    ``systemctl`` (with or without ``--user``) whose subcommand is one of
    ``start stop restart reload reload-or-restart try-restart
    try-reload-or-restart enable disable mask unmask daemon-reload
    daemon-reexec edit kill set-property revert link preset isolate``.
    ``status``, ``is-active``, ``is-enabled``, ``show``, ``cat``,
    ``list-units`` etc. do not match. Also any ``write_file``/``patch``
    whose target path is under ``/etc/systemd/`` or ``~/.config/systemd/``.

``dns``
    Command/code text that names ``api.cloudflare.com`` AND ``/dns_records``
    together with a mutating marker (``-X``/``--request`` ``POST|PUT|PATCH|
    DELETE``, ``-d``/``--data*``/``--json``, ``.post(``/``.put(``/
    ``.patch(``/``.delete(``, ``method="POST"`` etc.). A plain GET listing
    does not match. ``nsupdate`` as a segment command always matches.

``deploy``
    A shell segment whose command basename starts with ``deploy`` (``./
    deploy.sh``, ``deploy-prod``), or an interpreter (``bash sh zsh python
    python3 node``) whose script basename starts with ``deploy``, or ``make
    deploy*`` / ``npm|pnpm|yarn run deploy*``; or ``git push <remote>`` where
    ``<remote>`` is ``production``, ``prod`` or ``live`` and neither
    ``--dry-run`` nor ``-n`` is present.

``stripe_live``
    ``sk_live_`` or ``rk_live_`` followed by a key character anywhere in the
    command, code, content, ``new_string`` or ``patch`` text; or the
    ``stripe`` CLI with ``--live``.

Matchers are regular expressions over text and are knowingly incomplete; a
miss is a missed report, never a wrongful refusal, because this segment
refuses nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENV_TASK = "HERMES_KANBAN_TASK"
ENV_RUN_ID = "HERMES_KANBAN_RUN_ID"

EVENT_UNAUTHORIZED = "production_action_unauthorized"

#: The lane this hook executes under. ``model_tools.handle_function_call``
#: only ever runs inside a Hermes-native agent process; Claude Code and Codex
#: executors never pass through it. Deliberately NOT read from
#: ``HERMES_EXECUTOR_LANE``: that variable describes the card's lane, and a
#: Hermes-native process does not become Claude Code because its card says
#: ``claude``.
HERMES_NATIVE_EXECUTOR = "hermes"

#: Installed registry (root-owned, sha256-pinned by the sibling verify.py).
#: Override via ``production_authority_refusal(..., registry_path=...)`` or by
#: monkeypatching this constant in tests. Not overridable from the environment.
REGISTRY_PATH = Path.home() / ".hermes" / "security" / "production-roles" / "production-roles.json"

ROLES = ("stripe_live", "deploy", "dns", "firewall", "ports", "service_units")

COMMAND_TOOLS = frozenset({"terminal", "execute_code"})
WRITE_TOOLS = frozenset({"write_file", "patch"})
GATED_TOOLS = COMMAND_TOOLS | WRITE_TOOLS

_SYSTEMD_DIR_PATTERNS = ("/etc/systemd/", "/.config/systemd/")
_SERVICES_DIR = "/home/chris/services/"

_FIREWALL_BINS = frozenset(
    {"ufw", "nft", "iptables", "ip6tables", "iptables-restore", "ip6tables-restore"}
)
_UFW_READ_ONLY = frozenset({"status", "show", "version", "--version", "help", "--help"})
_NFT_READ_ONLY = frozenset({"list", "get", "describe", "monitor", "--version", "-v", "--help", "-h"})
_IPTABLES_MUTATING_FLAGS = frozenset(
    {
        "-A", "--append", "-I", "--insert", "-D", "--delete", "-R", "--replace",
        "-F", "--flush", "-X", "--delete-chain", "-P", "--policy", "-N", "--new-chain",
        "-E", "--rename-chain", "-Z", "--zero",
    }
)

_SYSTEMCTL_MUTATING = frozenset(
    {
        "start", "stop", "restart", "reload", "reload-or-restart", "try-restart",
        "try-reload-or-restart", "enable", "disable", "mask", "unmask",
        "daemon-reload", "daemon-reexec", "edit", "kill", "set-property", "revert",
        "link", "preset", "isolate",
    }
)

_PREFIX_SKIP = frozenset({"sudo", "env", "nohup", "command", "exec"})
_INTERPRETERS = frozenset({"bash", "sh", "zsh", "dash", "python", "python3", "node"})
_DEPLOY_REMOTES = frozenset({"production", "prod", "live"})

_PORT_LINE_RE = re.compile(
    r"(?im)^[+\-]?\s*(?:Environment=[\"']?)?"
    # (?:[A-Za-z0-9_]*_)?PORT covers PORT, LISTEN_PORT, APP_PORT ... but not
    # REPORT/IMPORT; BIND[-_]ADDR covers bind-address / bind_address (Codex t_3cb4a9cb, t_150b247f).
    r"(?:(?:(?:[A-Za-z0-9_]*_)?PORT|BIND|BIND[-_]ADDR(?:ESS)?|HOST|LISTEN_HOST|ListenStream|ListenDatagram)"
    r"\s*[=:]\s*\S|listen\s+\S)"
)
_PORT_FLAG_RE = re.compile(r"(?i)--(?:port|bind|host|listen)[= ]\S")
_STRIPE_LIVE_KEY_RE = re.compile(r"\b[sr]k_live_[A-Za-z0-9]")
_DNS_HOST_RE = re.compile(r"api\.cloudflare\.com")
_DNS_RECORDS_RE = re.compile(r"/dns_records")
_DNS_MUTATING_RE = re.compile(
    r"(?i)(?:-X\s*|--request[= ]\s*)[\"']?(?:POST|PUT|PATCH|DELETE)\b"
    r"|(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|--json)(?:\s|=)"
    r"|\.(?:post|put|patch|delete)\("
    r"|method\s*[=:]\s*[\"'](?:POST|PUT|PATCH|DELETE)[\"']"
)
_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|[;|\n]")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class SurfaceMatch:
    role: str
    matcher: str  # stable id, e.g. "firewall.ufw"; never carries user text


@dataclass(frozen=True)
class RegistryState:
    state: str  # "ok" | "missing" | "unparseable" | "invalid"
    mode: str = "report-only"
    authorized_executors: frozenset = field(default_factory=frozenset)
    roles: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class ProductionDecision:
    match: SurfaceMatch
    executor_lane: str
    task_id: Optional[str]
    registry: RegistryState
    card_actions: tuple
    authorized: bool
    reason: str

    def event_payload(self) -> dict:
        """Redacted, card-safe payload. No command text, code, path or content."""
        role = self.match.role if self.registry.state == "ok" else "unknown"
        return {
            "source": "production_authority_gate",
            "role": role,
            "surface_role": self.match.role,
            "matcher": self.match.matcher,
            "executor_lane": self.executor_lane,
            "registry_state": self.registry.state,
            "registry_mode": self.registry.mode,
            "authorized": self.authorized,
            "reason": self.reason,
            "action": "allowed_report_only",
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def load_registry(path: Optional[Path] = None) -> RegistryState:
    """Read the installed registry. Never raises; a missing or broken registry
    is a state the caller records, not an exception that breaks a tool call.
    (Hash pinning is verify.py's job at boot, not this hook's.)"""
    reg_path = Path(path) if path is not None else REGISTRY_PATH
    try:
        raw = reg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return RegistryState(state="missing")
    except Exception:
        return RegistryState(state="missing")
    try:
        reg = json.loads(raw)
    except Exception:
        return RegistryState(state="unparseable")
    if not isinstance(reg, dict) or not isinstance(reg.get("roles"), dict):
        return RegistryState(state="invalid")
    mode = str(reg.get("mode") or "report-only")
    execs = reg.get("christopher_authorized_executors")
    if not isinstance(execs, list):
        # Registry predates the 2026-10-09 ruling: no executor may be
        # authorized through it. Fails closed on the decision; still
        # report-only on the action.
        execs = []
    return RegistryState(
        state="ok",
        mode=mode,
        authorized_executors=frozenset(str(e) for e in execs),
        roles=frozenset(str(r) for r in reg["roles"]),
    )


# ---------------------------------------------------------------------------
# Surface matching
# ---------------------------------------------------------------------------


def _segments(text: str) -> list[list[str]]:
    """Split shell text into command segments and tokenize each, dropping
    ``sudo``/``env``/``VAR=val`` prefixes so ``tokens[0]`` is the command."""
    out: list[list[str]] = []
    for raw in _SEGMENT_SPLIT_RE.split(text or ""):
        raw = raw.strip()
        if not raw:
            continue
        try:
            toks = shlex.split(raw, posix=True)
        except ValueError:
            toks = raw.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            if t in _PREFIX_SKIP or _ENV_ASSIGN_RE.match(t):
                i += 1
                # skip sudo's own short options (-u user, -E, -n ...)
                while t == "sudo" and i < len(toks) and toks[i].startswith("-"):
                    if toks[i] in ("-u", "--user", "-g", "--group") and i + 1 < len(toks):
                        i += 1
                    i += 1
                continue
            break
        toks = toks[i:]
        if toks:
            out.append(toks)
    return out


def _base(tok: str) -> str:
    return os.path.basename(tok)


def _first_positional(args: list[str]) -> Optional[str]:
    for a in args:
        if not a.startswith("-"):
            return a
    return None


def _match_firewall(segs: list[list[str]]) -> list[SurfaceMatch]:
    found = []
    for toks in segs:
        b = _base(toks[0])
        if b not in _FIREWALL_BINS:
            continue
        args = toks[1:]
        if b == "ufw":
            sub = _first_positional(args)
            if sub is None or sub in _UFW_READ_ONLY:
                continue
            found.append(SurfaceMatch("firewall", "firewall.ufw"))
        elif b == "nft":
            sub = _first_positional(args)
            if sub is not None and sub in _NFT_READ_ONLY and "-f" not in args:
                continue
            found.append(SurfaceMatch("firewall", "firewall.nft"))
        elif b in ("iptables", "ip6tables"):
            if not any(a in _IPTABLES_MUTATING_FLAGS for a in args):
                continue
            found.append(SurfaceMatch("firewall", f"firewall.{b}"))
        else:  # *-restore always rewrites the ruleset
            found.append(SurfaceMatch("firewall", f"firewall.{b}"))
    return found


def _match_service_units_cmd(segs: list[list[str]]) -> list[SurfaceMatch]:
    found = []
    for toks in segs:
        if _base(toks[0]) != "systemctl":
            continue
        sub = _first_positional(toks[1:])
        if sub in _SYSTEMCTL_MUTATING:
            found.append(SurfaceMatch("service_units", f"service_units.systemctl.{sub}"))
    return found


def _match_dns(text: str, segs: list[list[str]]) -> list[SurfaceMatch]:
    found = []
    if _DNS_HOST_RE.search(text) and _DNS_RECORDS_RE.search(text) and _DNS_MUTATING_RE.search(text):
        found.append(SurfaceMatch("dns", "dns.cloudflare_dns_records"))
    if any(_base(t[0]) == "nsupdate" for t in segs):
        found.append(SurfaceMatch("dns", "dns.nsupdate"))
    return found


def _match_deploy(segs: list[list[str]]) -> list[SurfaceMatch]:
    found = []
    for toks in segs:
        b = _base(toks[0])
        if b.startswith("deploy"):
            found.append(SurfaceMatch("deploy", "deploy.script"))
            continue
        if b in _INTERPRETERS and len(toks) > 1:
            script = _first_positional(toks[1:])
            if script and _base(script).startswith("deploy"):
                found.append(SurfaceMatch("deploy", "deploy.script"))
                continue
        if b == "make" and any(_base(a).startswith("deploy") for a in toks[1:]):
            found.append(SurfaceMatch("deploy", "deploy.make"))
            continue
        if b in ("npm", "pnpm", "yarn") and "run" in toks[1:]:
            idx = toks.index("run")
            if idx + 1 < len(toks) and toks[idx + 1].startswith("deploy"):
                found.append(SurfaceMatch("deploy", "deploy.package_script"))
                continue
        if b == "git" and "push" in toks[1:]:
            args = toks[toks.index("push") + 1 :]
            if "--dry-run" in args or "-n" in args:
                continue
            remote = _first_positional(args)
            if remote in _DEPLOY_REMOTES:
                found.append(SurfaceMatch("deploy", "deploy.git_push_production_remote"))
    return found


def _match_stripe_live(text: str, segs: list[list[str]]) -> list[SurfaceMatch]:
    found = []
    if _STRIPE_LIVE_KEY_RE.search(text or ""):
        found.append(SurfaceMatch("stripe_live", "stripe_live.key_literal"))
    if any(_base(t[0]) == "stripe" and "--live" in t[1:] for t in segs):
        found.append(SurfaceMatch("stripe_live", "stripe_live.cli_live"))
    return found


def _is_systemd_path(path: str) -> bool:
    p = os.path.expanduser(path or "")
    return any(pat in p for pat in _SYSTEMD_DIR_PATTERNS)


def _is_services_path(path: str) -> bool:
    return os.path.expanduser(path or "").startswith(_SERVICES_DIR)


_PATCH_PATH_RE = re.compile(
    r"(?m)^(?:\*\*\* (?:Update|Add|Delete) File:|\*\*\* Move to:|\+\+\+ (?:b/)?|--- (?:a/)?)[ \t]*(\S[^\t\n]*?)[ \t]*$"
)


def _patch_paths(text: str) -> list:
    """Target paths named inside a patch payload (V4A ``*** Update File:`` /
    ``*** Add File:`` / ``*** Delete File:`` / ``*** Move to:`` headers and
    unified-diff ``+++ b/`` / ``--- a/`` headers). /dev/null is ignored."""
    out = []
    for m in _PATCH_PATH_RE.finditer(text or ""):
        p = m.group(1).strip()
        if p.startswith("home/"):
            p = "/" + p
        if p and p != "/dev/null" and p not in out:
            out.append(p)
    return out


def _written_text(tool_name: str, args: dict) -> str:
    parts = []
    for key in ("content", "new_string", "patch"):
        v = args.get(key)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def match_surfaces(tool_name: str, args: Any) -> list[SurfaceMatch]:
    """Return every production surface the call touches (deduplicated by
    role, first matcher wins). Pure: no IO, no side effects, never raises
    for odd inputs."""
    if tool_name not in GATED_TOOLS or not isinstance(args, dict):
        return []
    matches: list[SurfaceMatch] = []
    if tool_name in COMMAND_TOOLS:
        text = args.get("command") if tool_name == "terminal" else args.get("code")
        if not isinstance(text, str):
            text = args.get("command") or args.get("code") or ""
            if not isinstance(text, str):
                text = ""
        segs = _segments(text)
        matches += _match_firewall(segs)
        matches += _match_service_units_cmd(segs)
        matches += _match_dns(text, segs)
        matches += _match_deploy(segs)
        matches += _match_stripe_live(text, segs)
    else:
        path = args.get("path") or ""
        if not isinstance(path, str):
            path = ""
        text = _written_text(tool_name, args)
        # Every target: the explicit path plus any path named inside a patch payload.
        targets = [path] + [p for p in _patch_paths(args.get("patch") if isinstance(args.get("patch"), str) else "") if p != path]
        port_text = bool(_PORT_LINE_RE.search(text) or _PORT_FLAG_RE.search(text))
        if any(_is_systemd_path(p) for p in targets):
            matches.append(SurfaceMatch("service_units", f"service_units.write.{tool_name}"))
        if port_text and any(_is_systemd_path(p) or _is_services_path(p) for p in targets):
            matches.append(SurfaceMatch("ports", f"ports.write.{tool_name}"))
        matches += _match_stripe_live(text, [])
    seen: set = set()
    out: list[SurfaceMatch] = []
    for m in matches:
        if m.role not in seen:
            seen.add(m.role)
            out.append(m)
    return out


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _card_production_actions(task_id: str) -> tuple:
    """The card's structured ``production_actions`` (card 6c). Absent field,
    missing card or unreadable board all mean: no authorization."""
    try:
        from hermes_cli import kanban_db as kb

        with kb.connect() as conn:
            task = kb.get_task(conn, task_id)
    except Exception:
        return ()
    if task is None:
        return ()
    actions = getattr(task, "production_actions", None) or []
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except Exception:
            actions = [actions]
    try:
        return tuple(str(a) for a in actions)
    except TypeError:
        return ()


def decide(
    match: SurfaceMatch,
    *,
    executor_lane: str,
    task_id: Optional[str],
    registry: RegistryState,
    card_actions: tuple,
) -> ProductionDecision:
    """(a) executor lane in christopher_authorized_executors AND (b) role in
    the card's production_actions. Both are required; the order of checks is
    only for the reason text."""
    if registry.state != "ok":
        reason = f"registry {registry.state}: no executor can be authorized"
        authorized = False
    elif executor_lane not in registry.authorized_executors:
        reason = f"executor lane {executor_lane!r} is not a christopher_authorized_executor"
        authorized = False
    elif not task_id:
        reason = "no card: production authorization is carried only by a card"
        authorized = False
    elif match.role not in card_actions:
        reason = f"card does not carry production_actions authorization for role {match.role!r}"
        authorized = False
    else:
        reason = "executor and card authorization both present"
        authorized = True
    return ProductionDecision(
        match=match,
        executor_lane=executor_lane,
        task_id=task_id or None,
        registry=registry,
        card_actions=tuple(card_actions),
        authorized=authorized,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def _record(decision: ProductionDecision, tool_name: str, environ) -> None:
    payload = {"tool": tool_name, **decision.event_payload()}
    task_id = decision.task_id
    if task_id:
        run_raw = (environ.get(ENV_RUN_ID) or "").strip()
        try:
            run_id: Optional[int] = int(run_raw) if run_raw else None
        except ValueError:
            run_id = None
        try:
            from hermes_cli import kanban_db as kb

            with kb.connect() as conn:
                with kb.write_txn(conn):
                    kb._append_event(conn, task_id, EVENT_UNAUTHORIZED, payload, run_id=run_id)
            return
        except Exception as exc:
            logger.warning(
                "production authority gate: could not record %s on %s (%s)",
                EVENT_UNAUTHORIZED, task_id, type(exc).__name__,
            )
    logger.warning(
        "production authority gate (report-only): %s tool=%s role=%s matcher=%s executor=%s "
        "registry=%s reason=%s",
        EVENT_UNAUTHORIZED, tool_name, payload["role"], payload["matcher"],
        payload["executor_lane"], payload["registry_state"], payload["reason"],
    )


# ---------------------------------------------------------------------------
# Dispatcher entry point
# ---------------------------------------------------------------------------


def production_authority_refusal(
    tool_name: str,
    args: Any,
    *,
    environ=None,
    executor_lane: str = HERMES_NATIVE_EXECUTOR,
    registry_path: Optional[Path] = None,
) -> Optional[str]:
    """Rule 6 entry point for the tool dispatcher. REPORT-ONLY: always
    returns ``None`` (allow). Side effects only: on a production-surface
    match, one ``production_action_unauthorized`` event per matched role on
    the ``HERMES_KANBAN_TASK`` card, or a WARNING log line when there is no
    card.

    Refuse mode (a later segment) will return a refusal string from the same
    ``ProductionDecision`` when ``authorized`` is False and the registry's
    ``mode`` is ``refuse``.
    """
    matches = match_surfaces(tool_name, args)
    if not matches:
        return None
    env = os.environ if environ is None else environ
    task_id = (env.get(ENV_TASK) or "").strip() or None
    registry = load_registry(registry_path)
    card_actions = _card_production_actions(task_id) if task_id else ()
    for match in matches:
        decision = decide(
            match,
            executor_lane=executor_lane,
            task_id=task_id,
            registry=registry,
            card_actions=card_actions,
        )
        if decision.authorized:
            continue
        _record(decision, tool_name, env)
    return None
