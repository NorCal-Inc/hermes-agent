"""Per-company kanban worker sandbox: bubblewrap argv builder (phase C1.1).

Design: ``vault/Business/Systems/2026-10-11-worker-sandbox-design.md``.

This module only *builds* the ``bwrap`` command prefix and decides whether a
spawn should be wrapped. It is not wired into the dispatcher here (C1.3 does
that by calling :func:`wrap` once in ``kanban_db._default_spawn`` before the
scoped spawn). Nothing in this module creates directories or writes files.

Policy summary
--------------
* ``kanban.worker_sandbox.mode`` is ``off`` (default) or ``enforce``.
* ``kanban.worker_sandbox.lanes`` lists the company lanes that are opted in.
* The lane of a worker is ``profiles.profile_company(assignee)``; ``shared``
  or an untagged profile is never sandboxed.
* Executor lanes (``tasks.executor_lane`` set: claude, codex_verify,
  claude_recovery) are the privileged maintenance lane and are never wrapped.
* The lane manifest (``~/.hermes/security/worker-sandbox/lanes.yaml``) says
  which paths each lane may see. A *missing* manifest disables the sandbox
  (logged once). A *malformed* manifest, or a lane that is enforced in config
  but absent from the manifest, raises :class:`SandboxConfigError` so the
  worker is NOT spawned unsandboxed by accident (fail closed).

Manifest schema (paths only, never secrets)::

    version: 1
    sandbox_home_root: /home/chris/.hermes/security/worker-sandbox/homes
    shared_ro: [<dir or file bound read-only into every lane>, ...]
    credentials_ro: [<exact FILE bound read-only into every lane>, ...]
    skill_roots_ro: [<skill-trust root bound read-only into every lane>, ...]
    groups:
      <group>: [<lane>, <lane>]      # lanes in a group see each other's repos/vault
    lanes:
      <lane>:
        llm_source: codex | claude_code     # default codex (C1.2d)
        repos: [<dir>, ...]
        vault: [<dir>, ...]
        browser_profiles: [<dir>, ...]
        extra_ro: [<path>, ...]
        extra_rw: [<path>, ...]

LLM credential (C1.2d). The ROOT store ``~/.hermes/auth.json`` is never bound
into a sandbox, even when ``credentials_ro`` lists it. A ``codex`` lane gets
the gateway-materialised, trimmed, per-lane copy
``~/.hermes/security/worker-sandbox/creds/<lane>/auth.json`` bound read-only
AT ``~/.hermes/auth.json`` (fail closed when that file is missing), plus
``HERMES_SANDBOX_NO_REFRESH=1`` so it never rotates the token itself. A
``claude_code`` lane gets no root-store view at all; it keeps only the exact
``credentials_ro`` files (``~/.claude/.credentials.json``).

``skill_roots_ro`` (C1.2c) exists so the skill supply-chain gate
(``~/.hermes/security/skill-trust/verify.py``, run by boot-context) can see
the roots it hashes. The key must be absent/``None`` or a *list of strings*;
any other type (a scalar string, a mapping, a number, a bool, a list holding
a non-string) raises :class:`SandboxConfigError` (fail closed, never "treated
as empty"). Entries are validated against a fixed allow-list of exactly three
paths (:data:`_SKILL_ROOTS_ALLOWED_REL`); anything else, in particular any
other path under ``~/.claude`` or ``~/.codex``, raises
:class:`SandboxConfigError`. It does not loosen ``credentials_ro`` or any
forbidden path.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_ENFORCE = "enforce"
MANIFEST_RELPATH = Path(".hermes") / "security" / "worker-sandbox" / "lanes.yaml"

UNSHARE_FLAGS: List[str] = [
    "--unshare-user",
    "--unshare-pid",
    "--unshare-ipc",
    "--unshare-uts",
    "--unshare-cgroup-try",
    "--share-net",
    "--die-with-parent",
    "--new-session",
]

SYSTEM_RO_DIRS: List[str] = ["/usr", "/etc", "/bin", "/lib", "/lib64", "/sbin"]
RESOLVER_DIR = "/run/systemd/resolve"

# Relative to $HOME. Never bound, in any mode, by any lane. ``.claude`` and
# ``.codex`` admit exactly one *file* each, and only when the manifest lists
# it under ``credentials_ro``.
_FORBIDDEN_HOME_REL: List[str] = [
    ".hermes/secrets",
    ".ssh",
    ".config/op",
    ".hermes/cache/op_cache.json",
    ".claude",
    ".codex",
]
_FORBIDDEN_ABS: List[str] = ["/var/run/docker.sock", "/run/docker.sock", "/run/user"]
_CREDENTIAL_FILE_PARENTS_REL: List[str] = [".claude", ".codex"]

# The ONLY paths ``skill_roots_ro`` may name (relative to $HOME), with the
# kind each must be. These are the roots the skill-trust gate hashes; nothing
# else under ``.claude``/``.codex`` is ever admitted through this key.
_SKILL_ROOTS_ALLOWED_REL: Dict[str, str] = {
    ".claude/plugins": "dir",
    ".claude/settings.json": "file",
    ".agents": "dir",
}

# C1.2d: LLM credential source of a lane and the per-lane trimmed codex store.
LLM_SOURCE_CODEX = "codex"
LLM_SOURCE_CLAUDE_CODE = "claude_code"
LLM_SOURCES = (LLM_SOURCE_CODEX, LLM_SOURCE_CLAUDE_CODE)
ROOT_AUTH_STORE_REL = Path(".hermes") / "auth.json"
LANE_CREDS_RELPATH = Path(".hermes") / "security" / "worker-sandbox" / "creds"
# Append-only company records (vault Decisions/2026-10-11-company-daily-records-append-only.md):
# with manifest ``vault_readonly: true`` a lane's vault paths are bound read-only and the
# lane writes new records to its own submissions dir, which the company daily ledger
# (outside the sandbox) files into the vault as new files, never overwriting.
SUBMISSIONS_RELPATH = Path(".hermes") / "company-submissions"
SANDBOX_NO_REFRESH_ENV = "HERMES_SANDBOX_NO_REFRESH"

_warned_missing_manifest = False


class SandboxConfigError(RuntimeError):
    """The manifest or config would produce an unsafe or incomplete sandbox."""


# --------------------------------------------------------------------------
# Config / manifest
# --------------------------------------------------------------------------

def default_manifest_path(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / MANIFEST_RELPATH


def load_manifest(path: Optional[os.PathLike] = None) -> Optional[Dict[str, Any]]:
    """Return the parsed lane manifest, or ``None`` when the file is absent.

    Absence is logged once per process and means "sandbox not configured":
    :func:`wrap` returns the command unchanged. A present-but-unparseable
    manifest raises :class:`SandboxConfigError` (fail closed).
    """
    global _warned_missing_manifest
    p = Path(path) if path is not None else default_manifest_path()
    if not p.is_file():
        if not _warned_missing_manifest:
            logger.warning("worker sandbox: manifest %s not found; sandbox disabled", p)
            _warned_missing_manifest = True
        return None
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise SandboxConfigError(f"worker sandbox: cannot parse manifest {p}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        raise SandboxConfigError(f"worker sandbox: manifest {p} must be a mapping with a 'lanes' mapping")
    return data


def sandbox_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``{"mode": str, "lanes": list[str]}`` from the effective kanban config.

    ``cfg`` may be an already-resolved ``kanban:`` section (tests, dispatcher
    passing its own); otherwise the board+process merge from
    ``kanban_db._kanban_cfg`` is used (lazy import: kanban_db will import this
    module in C1.3).
    """
    if cfg is None:
        try:
            from hermes_cli.kanban_db import _kanban_cfg
            cfg = _kanban_cfg()
        except Exception:  # noqa: BLE001
            try:
                from hermes_cli.config import load_config_readonly
                cfg = (load_config_readonly() or {}).get("kanban") or {}
            except Exception:  # noqa: BLE001
                cfg = {}
    raw = cfg.get("worker_sandbox") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or MODE_OFF).strip().lower()
    if mode not in (MODE_OFF, MODE_ENFORCE):
        raise SandboxConfigError(f"kanban.worker_sandbox.mode must be off|enforce, got {mode!r}")
    lanes_raw = raw.get("lanes") or []
    if isinstance(lanes_raw, str):
        lanes_raw = [lanes_raw]
    lanes = [str(x).strip().casefold() for x in lanes_raw if str(x).strip()]
    return {"mode": mode, "lanes": lanes}


def lane_for(profile_name: Optional[str]) -> Optional[str]:
    """Company lane of a profile; ``None`` for shared/untagged/unknown."""
    if not profile_name:
        return None
    try:
        from hermes_cli.profiles import SHARED_LANE, profile_company
        lane = profile_company(profile_name)
    except Exception:  # noqa: BLE001
        return None
    if lane is None or lane == SHARED_LANE:
        return None
    return lane


# --------------------------------------------------------------------------
# Pure argv builder
# --------------------------------------------------------------------------

def _norm(p: os.PathLike | str) -> str:
    return os.path.normpath(str(Path(p).expanduser()))


def _is_within(path: str, root: str) -> bool:
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def forbidden_roots(home: Path) -> List[str]:
    return [_norm(home / rel) for rel in _FORBIDDEN_HOME_REL] + list(_FORBIDDEN_ABS)


def _check_allowed(src: str, home: Path, *, allow_credential_file: bool = False) -> None:
    """Raise unless ``src`` is outside every forbidden root.

    ``allow_credential_file`` admits one exact file directly under a
    credential parent (``~/.claude/<file>``, ``~/.codex/<file>``); it never
    admits a directory, a nested path, or anything under the other roots.
    """
    s = _norm(src)
    for root in forbidden_roots(home):
        if not _is_within(s, root):
            continue
        if allow_credential_file:
            parent_ok = any(
                _norm(Path(s).parent) == _norm(home / rel) for rel in _CREDENTIAL_FILE_PARENTS_REL
            )
            if parent_ok and s != root:
                return
        raise SandboxConfigError(f"worker sandbox: refusing to bind forbidden path {s} (under {root})")


def skill_roots_allowed(home: Path) -> Dict[str, str]:
    """``{normalised absolute path: "dir"|"file"}`` of the admissible skill roots."""
    return {_norm(home / rel): kind for rel, kind in _SKILL_ROOTS_ALLOWED_REL.items()}


def _skill_roots_entries(manifest: Dict[str, Any]) -> List[str]:
    """Type gate for ``skill_roots_ro``: absent/``None`` -> ``[]``; a list of
    strings -> that list; anything else raises (fail closed).

    A scalar string is NOT a one-element list here (unlike the lenient
    ``kanban.worker_sandbox.lanes`` config key): a manifest typo must stop the
    spawn, not silently bind or silently skip.
    """
    raw = manifest.get("skill_roots_ro")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SandboxConfigError(
            f"worker sandbox: skill_roots_ro must be a list of paths, got {type(raw).__name__}"
        )
    out: List[str] = []
    for i, p in enumerate(raw):
        if not isinstance(p, str) or not p.strip():
            raise SandboxConfigError(
                f"worker sandbox: skill_roots_ro[{i}] must be a non-empty path string, got {type(p).__name__}"
            )
        out.append(p)
    return out


def _check_skill_root(src: str, home: Path, exists: Callable[[str], bool]) -> str:
    """Validate one ``skill_roots_ro`` entry; return its normalised path.

    Fail closed: the entry must spell one of the allow-listed roots (no ``..``
    components), must not be or sit behind a symlink (so it cannot point
    outside its own location), must exist, and must have the expected kind.
    """
    raw = str(src)
    if ".." in Path(raw).parts:
        raise SandboxConfigError(f"worker sandbox: skill_roots_ro entry {raw} contains a '..' component")
    n = _norm(raw)
    allowed = skill_roots_allowed(home)
    if n not in allowed:
        raise SandboxConfigError(
            f"worker sandbox: skill_roots_ro entry {n} is not an allowed skill root "
            f"(allowed: {', '.join(sorted(allowed))})"
        )
    rel = os.path.relpath(n, _norm(home))
    if os.path.realpath(n) != os.path.join(os.path.realpath(str(home)), rel):
        raise SandboxConfigError(f"worker sandbox: skill_roots_ro entry {n} is a symlink; refusing to follow it")
    if not exists(n):
        raise SandboxConfigError(f"worker sandbox: skill_roots_ro entry {n} does not exist")
    kind = allowed[n]
    if os.path.lexists(n):
        if kind == "dir" and not os.path.isdir(n):
            raise SandboxConfigError(f"worker sandbox: skill_roots_ro entry {n} must be a directory")
        if kind == "file" and not os.path.isfile(n):
            raise SandboxConfigError(f"worker sandbox: skill_roots_ro entry {n} must be a regular file")
    return n


def _skill_roots(manifest: Dict[str, Any], home: Path, exists: Callable[[str], bool]) -> List[str]:
    out: List[str] = []
    for p in _skill_roots_entries(manifest):
        n = _check_skill_root(p, home, exists)
        if n not in out:
            out.append(n)
    return out


def _lane_paths(manifest: Dict[str, Any], lane: str, key: str) -> List[str]:
    """Paths for ``key`` of ``lane`` plus, for repos/vault, its group peers."""
    lanes = manifest.get("lanes") or {}
    members = [lane]
    if key in ("repos", "vault"):
        for _gname, glanes in (manifest.get("groups") or {}).items():
            glanes = [str(x).casefold() for x in (glanes or [])]
            if lane in glanes:
                members.extend(x for x in glanes if x not in members)
    out: List[str] = []
    for m in members:
        spec = lanes.get(m) or {}
        for p in spec.get(key) or []:
            n = _norm(p)
            if n not in out:
                out.append(n)
    return out


def lane_llm_source(manifest: Dict[str, Any], lane: str) -> str:
    """``llm_source`` of a lane (``codex`` default). Unknown values fail closed."""
    spec = (manifest.get("lanes") or {}).get(str(lane).strip().casefold()) or {}
    raw = str(spec.get("llm_source") or LLM_SOURCE_CODEX).strip().casefold()
    if raw not in LLM_SOURCES:
        raise SandboxConfigError(f"worker sandbox: lane {lane!r} llm_source must be one of {LLM_SOURCES}, got {raw!r}")
    return raw


def vault_readonly(manifest: Dict[str, Any]) -> bool:
    """``vault_readonly`` must be absent/None or a bool (fail closed on anything else)."""
    raw = manifest.get("vault_readonly")
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise SandboxConfigError(f"worker sandbox: vault_readonly must be true/false, got {raw!r}")
    return raw


def lane_submissions_dir(lane: str, home: Path) -> str:
    """Host path where ``lane`` drops new records for the company daily ledger."""
    return _norm(home / SUBMISSIONS_RELPATH / str(lane).strip().casefold())


def lane_creds_file(lane: str, home: Path) -> str:
    """Host path of the gateway-materialised trimmed codex store for ``lane``."""
    return _norm(home / LANE_CREDS_RELPATH / str(lane).strip().casefold() / "auth.json")


def runtime_code_paths(home: Path) -> List[str]:
    """Read-only code the worker needs: repo checkout + venv, node, ~/.local/bin, playwright browsers."""
    repo_root = Path(__file__).resolve().parents[1]
    return [
        str(repo_root),
        _norm(home / ".hermes" / "node"),
        _norm(home / ".local" / "bin"),
        _norm(home / ".cache" / "ms-playwright"),
    ]


def build_bwrap_argv(
    lane: str,
    profile: str,
    profile_home: os.PathLike | str,
    workspace: os.PathLike | str,
    manifest: Dict[str, Any],
    *,
    kanban_db_dir: Optional[os.PathLike | str] = None,
    home: Optional[Path] = None,
    exists: Optional[Callable[[str], bool]] = None,
    bwrap_path: str = "bwrap",
) -> List[str]:
    """Pure function: the ``bwrap`` prefix for one worker (no trailing ``--``).

    Mount order matters in bwrap (later binds overlay earlier ones): system
    read-only, ``/dev`` ``/proc``, tmpfs over ``/tmp`` ``/run`` and ``$HOME``,
    then the allow-list is re-bound on top of the empty home.
    """
    home = Path(home) if home is not None else Path.home()
    exists = exists or os.path.exists
    lane = str(lane).strip().casefold()
    lanes = manifest.get("lanes") or {}
    if lane not in lanes:
        raise SandboxConfigError(f"worker sandbox: lane {lane!r} is not in the manifest")
    profile_home_s = _norm(profile_home)
    workspace_s = _norm(workspace)
    hermes_root = _norm(home / ".hermes")
    if profile_home_s == hermes_root or _is_within(hermes_root, profile_home_s):
        raise SandboxConfigError("worker sandbox: profile home must be a named profile directory, not the hermes root")
    for p in (profile_home_s, workspace_s):
        _check_allowed(p, home)

    argv: List[str] = [bwrap_path, *UNSHARE_FLAGS]

    for d in SYSTEM_RO_DIRS:
        if exists(d):
            argv += ["--ro-bind", d, d]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    argv += ["--tmpfs", "/tmp", "--tmpfs", "/run"]
    if exists(RESOLVER_DIR):
        argv += ["--ro-bind", RESOLVER_DIR, RESOLVER_DIR]
    argv += ["--tmpfs", str(home)]

    for p in runtime_code_paths(home):
        if exists(p):
            _check_allowed(p, home)
            argv += ["--ro-bind", p, p]

    for p in manifest.get("shared_ro") or []:
        n = _norm(p)
        _check_allowed(n, home)
        argv += ["--ro-bind", n, n]

    for n in _skill_roots(manifest, home, exists):
        argv += ["--ro-bind", n, n]

    root_auth = _norm(home / ROOT_AUTH_STORE_REL)
    for p in manifest.get("credentials_ro") or []:
        n = _norm(p)
        if n == root_auth:
            # The root store is the whole provider pool; it is never bound as-is.
            # A codex lane gets the trimmed per-lane copy below instead.
            continue
        if n.endswith("/") or (exists(n) and os.path.isdir(n)):
            raise SandboxConfigError(f"worker sandbox: credentials_ro entry {n} is a directory; exact files only")
        _check_allowed(n, home, allow_credential_file=True)
        argv += ["--ro-bind", n, n]

    llm_source = lane_llm_source(manifest, lane)
    if llm_source == LLM_SOURCE_CODEX:
        trimmed = lane_creds_file(lane, home)
        if not exists(trimmed) or os.path.isdir(trimmed):
            raise SandboxConfigError(
                f"worker sandbox: lane {lane!r} is codex-sourced but the trimmed credential "
                f"{trimmed} is missing (the gateway codex lane refresher materialises it); refusing to spawn"
            )
        _check_allowed(trimmed, home)
        argv += ["--ro-bind", trimmed, root_auth]

    argv += ["--bind", profile_home_s, profile_home_s]
    argv += ["--bind", workspace_s, workspace_s]
    if kanban_db_dir:
        kd = _norm(kanban_db_dir)
        _check_allowed(kd, home)
        argv += ["--bind", kd, kd]

    vault_ro = vault_readonly(manifest)
    for key in ("repos", "vault", "browser_profiles", "extra_rw"):
        for p in _lane_paths(manifest, lane, key):
            _check_allowed(p, home)
            argv += ["--ro-bind" if (vault_ro and key == "vault") else "--bind", p, p]
    if vault_ro:
        sub = lane_submissions_dir(lane, home)
        if not exists(sub):
            raise SandboxConfigError(
                f"worker sandbox: lane {lane!r} has vault_readonly but no submissions dir {sub}; refusing to spawn"
            )
        _check_allowed(sub, home)
        argv += ["--bind", sub, sub]
    for p in _lane_paths(manifest, lane, "extra_ro"):
        _check_allowed(p, home)
        argv += ["--ro-bind", p, p]

    root = manifest.get("sandbox_home_root") or str(home / ".hermes" / "security" / "worker-sandbox" / "homes")
    lane_home = _norm(Path(root) / lane)
    _check_allowed(lane_home, home)
    argv += ["--bind", lane_home, lane_home]

    argv += ["--setenv", "HOME", lane_home]
    argv += ["--setenv", "HERMES_ENV_SOURCED", "1"]
    # Token rotation is owned by the gateway (hermes_cli.codex_lane_creds);
    # hermes_cli.auth honours this flag and never consumes the refresh token.
    argv += ["--setenv", SANDBOX_NO_REFRESH_ENV, "1"]
    argv += ["--chdir", workspace_s]
    return argv


# --------------------------------------------------------------------------
# Spawn-time decision
# --------------------------------------------------------------------------

def wrap(
    cmd: Sequence[str],
    *,
    task: Any,
    profile: str,
    profile_home: os.PathLike | str,
    workspace: os.PathLike | str,
    env: Dict[str, str],
    cfg: Optional[Dict[str, Any]] = None,
    manifest: Optional[Dict[str, Any]] = None,
    manifest_path: Optional[os.PathLike] = None,
) -> List[str]:
    """Return ``cmd`` unchanged, or ``bwrap <prefix> -- cmd`` when the lane is enforced.

    Unchanged when: mode is off; ``task.executor_lane`` is set; the profile is
    shared/untagged; the lane is not opted in; or the manifest is missing.
    """
    cmd = list(cmd)
    sc = sandbox_config(cfg)
    if sc["mode"] != MODE_ENFORCE:
        return cmd
    if getattr(task, "executor_lane", None):
        return cmd
    lane = lane_for(profile)
    if lane is None or lane not in sc["lanes"]:
        return cmd
    if manifest is None:
        manifest = load_manifest(manifest_path)
    if manifest is None:
        return cmd
    db = (env or {}).get("HERMES_KANBAN_DB", "").strip()
    kanban_db_dir = str(Path(db).parent) if db else None
    prefix = build_bwrap_argv(
        lane, profile, profile_home, workspace, manifest, kanban_db_dir=kanban_db_dir
    )
    return prefix + ["--"] + cmd


# --------------------------------------------------------------------------
# Probe (C1.2a): run a check table INSIDE the exact sandbox a lane would get
# --------------------------------------------------------------------------
#
# ``python -m hermes_cli.worker_sandbox probe --profile <name> --manifest PATH``
# builds the bwrap prefix with :func:`build_bwrap_argv` and runs
# :data:`PROBE_SCRIPT` inside it. The script receives the check table as JSON
# on argv and prints a JSON result list on stdout. Output is names and
# PASS/FAIL only: no file contents, no env values, no tokens.
#
# Check kinds (``target`` is a path, a list of paths, a pid, a hostname, a
# URL or an env var name):
#   hidden        every listed path must not exist inside the sandbox
#   readonly      creating a temp file under the path must fail (EROFS/EACCES)
#   rw            create + unlink a temp file under the path must succeed
#   kanban_lock   ``BEGIN IMMEDIATE; ROLLBACK`` on <dir>/kanban.db must succeed
#   pid_hidden    /proc/<pid> of a process outside the sandbox must not exist.
#                 With an ``identity`` (starttime + cmdline hash recorded
#                 OUTSIDE, see :func:`outside_processes`) the pid only counts
#                 as visible when the process at that number inside the
#                 sandbox has the same identity: pid numbers are reused inside
#                 a fresh pid namespace, so a bare number is not evidence.
#   dns           getaddrinfo(host) must return at least one address
#   https_head    HEAD <url> must return any HTTP status (status only)
#   env_absent    the variable must not be in the environment
#   env_equals    the variable must equal the given value

PROBE_SECRET_FILES: List[str] = [
    "Orion.env",
    "op-service-account-northcaledonia_lead.env",
]
# Directories that are read-only when bound and must otherwise be hidden.
PROBE_RO_IF_BOUND_REL: List[str] = [".hermes/skills", ".npm"]
PROBE_DNS_HOST = "chatgpt.com"
PROBE_HTTPS_URL = "https://chatgpt.com"

EXIT_PROBE_PASS = 0
EXIT_PROBE_FAIL = 1
EXIT_PROBE_CONFIG = 2
EXIT_PROBE_RUNNER = 3


def process_identity(pid: int, proc: str = "/proc") -> Optional[Dict[str, str]]:
    """``{"starttime": <stat field 22>, "cmdline_sha256": <hex>}`` of ``pid``, or ``None``.

    Values only, never content: the start time is a clock tick count and the
    command line is reduced to a hash, so the record can travel in the check
    table without carrying argv (which may hold tokens).
    """
    import hashlib
    try:
        with open(f"{proc}/{int(pid)}/stat", "rb") as fh:
            stat = fh.read()
        # comm (field 2) may contain spaces/parens; everything after the last
        # ')' is fields 3.. so field 22 (starttime) is index 19 there.
        _head, _sep, rest = stat.rpartition(b")")
        fields = rest.split()
        starttime = fields[19].decode("ascii")
        with open(f"{proc}/{int(pid)}/cmdline", "rb") as fh:
            cmdline = fh.read()
    except (OSError, IndexError, ValueError, UnicodeDecodeError):
        return None
    return {"starttime": starttime, "cmdline_sha256": hashlib.sha256(cmdline).hexdigest()}


def outside_processes(candidates: Iterable[Optional[int]], proc: str = "/proc") -> List[Dict[str, Any]]:
    """Identity records for processes that live OUTSIDE the sandbox about to be built.

    Call this *before* launching bwrap, in the probe's own pid namespace. Pids
    1 and 2 are never chosen (inside the sandbox they are bwrap and the probed
    command, and when the probe itself runs inside a pid namespace they are
    its own ancestors), and a pid whose identity cannot be read is dropped.
    Each record is ``{"pid": int, "identity": {...}}``.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for pid in candidates:
        try:
            p = int(pid or 0)
        except (TypeError, ValueError):
            continue
        if p <= 2 or p in seen:
            continue
        ident = process_identity(p, proc)
        if ident is None:
            continue
        seen.add(p)
        out.append({"pid": p, "identity": ident})
    return out


def _manifest_bound_paths(manifest: Dict[str, Any], lane: str) -> Dict[str, List[str]]:
    """Paths the lane would get, split by bind mode (normalised)."""
    ro = [_norm(p) for p in (manifest.get("shared_ro") or [])]
    ro += [_norm(p) for p in _skill_roots_entries(manifest)]
    ro += _lane_paths(manifest, lane, "extra_ro")
    rw: List[str] = []
    vault_ro = vault_readonly(manifest)
    for key in ("repos", "vault", "browser_profiles", "extra_rw"):
        (ro if (vault_ro and key == "vault") else rw).extend(_lane_paths(manifest, lane, key))
    return {"ro": ro, "rw": rw}


def _group_peers(manifest: Dict[str, Any], lane: str) -> List[str]:
    peers = [lane]
    for _g, glanes in (manifest.get("groups") or {}).items():
        glanes = [str(x).casefold() for x in (glanes or [])]
        if lane in glanes:
            peers.extend(x for x in glanes if x not in peers)
    return peers


def probe_checks(
    lane: str,
    profile: str,
    profile_home: os.PathLike | str,
    workspace: os.PathLike | str,
    manifest: Dict[str, Any],
    *,
    kanban_db_dir: Optional[os.PathLike | str] = None,
    home: Optional[Path] = None,
    uid: Optional[int] = None,
    outside_pids: Iterable[Any] = (),
    exists: Optional[Callable[[str], bool]] = None,
    listdir: Optional[Callable[[str], List[str]]] = None,
    network: bool = True,
) -> List[Dict[str, Any]]:
    """Pure function: the check table for one lane (names and paths only).

    ``outside_pids`` holds bare ints (legacy, number-only comparison) and/or
    :func:`outside_processes` records; the latter carry the identity the
    in-sandbox check must match before it may report "visible".
    """
    home = Path(home) if home is not None else Path.home()
    exists = exists or os.path.exists
    listdir = listdir or (lambda d: sorted(os.listdir(d)))
    uid = os.getuid() if uid is None else uid
    lane = str(lane).strip().casefold()
    profile_home_s = _norm(profile_home)
    workspace_s = _norm(workspace)
    bound = _manifest_bound_paths(manifest, lane)
    checks: List[Dict[str, Any]] = []

    def add(name: str, kind: str, target: Any, **extra: Any) -> None:
        checks.append({"name": name, "kind": kind, "target": target, **extra})

    # --- MUST BE HIDDEN -------------------------------------------------
    secrets_dir = _norm(home / ".hermes" / "secrets")
    add("secrets_dir_hidden", "hidden", [secrets_dir])
    for fn in PROBE_SECRET_FILES:
        add(f"secrets_{fn}_hidden", "hidden", [f"{secrets_dir}/{fn}"])
    peers = _group_peers(manifest, lane)
    other_lane_paths: List[str] = []
    for other, spec in (manifest.get("lanes") or {}).items():
        if str(other).casefold() in peers:
            continue
        for key in ("repos", "vault"):
            for p in (spec or {}).get(key) or []:
                n = _norm(p)
                if n not in other_lane_paths and n not in bound["ro"] and n not in bound["rw"]:
                    other_lane_paths.append(n)
    add("other_lane_repos_and_vault_hidden", "hidden", other_lane_paths)
    add("docker_sock_hidden", "hidden", ["/var/run/docker.sock", "/run/docker.sock"])
    add("user_bus_hidden", "hidden", [f"/run/user/{uid}/bus", f"/run/user/{uid}"])
    for rel, name in (
        (".ssh", "ssh_dir_hidden"),
        (".claude.json", "claude_json_hidden"),
        (".codex", "codex_dir_hidden"),
        (".hermes/cache/op_cache.json", "op_cache_hidden"),
        (".hermes/.env", "hermes_root_env_hidden"),
    ):
        add(name, "hidden", [_norm(home / rel)])
    profiles_root = _norm(home / ".hermes" / "profiles")
    others: List[str] = []
    if exists(profiles_root):
        try:
            for child in listdir(profiles_root):
                n = _norm(Path(profiles_root) / child)
                if n != profile_home_s:
                    others.append(n)
        except OSError:
            pass
    add("other_profiles_hidden", "hidden", others)
    for entry in outside_pids:
        if isinstance(entry, dict):
            pid = int(entry["pid"])
            ident = entry.get("identity") or {}
            add(f"outside_pid_{pid}_environ_hidden", "pid_hidden", pid,
                identity={"starttime": str(ident["starttime"]), "cmdline_sha256": str(ident["cmdline_sha256"])})
        else:
            add(f"outside_pid_{int(entry)}_environ_hidden", "pid_hidden", int(entry))

    # --- MUST BE READ-ONLY ----------------------------------------------
    repo_root = runtime_code_paths(home)[0]
    add("runtime_repo_readonly", "readonly", repo_root)
    for rel in PROBE_RO_IF_BOUND_REL:
        n = _norm(home / rel)
        label = rel.replace("/", "_").strip("._")
        if n in bound["rw"]:
            continue
        if n in bound["ro"]:
            add(f"{label}_readonly", "readonly", n)
        else:
            add(f"{label}_hidden", "hidden", [n])
    root_auth = _norm(home / ROOT_AUTH_STORE_REL)
    for p in manifest.get("credentials_ro") or []:
        n = _norm(p)
        if n == root_auth:
            continue  # never bound as-is; covered by the trimmed-store check below
        add(f"credential_{Path(n).name}_readonly", "readonly", n, file=True)
    allowed_roots = skill_roots_allowed(home)
    for n in _skill_roots(manifest, home, exists):
        label = os.path.relpath(n, _norm(home)).replace("/", "_").strip("._")
        add(f"skill_root_{label}_readonly", "readonly", n, file=(allowed_roots[n] == "file"))
    if vault_readonly(manifest):
        for p in _lane_paths(manifest, lane, "vault"):
            label = Path(p).name.replace(".", "_")
            add(f"own_vault_{label}_readonly", "readonly", p, file=p.endswith(".md"))
    if lane_llm_source(manifest, lane) == LLM_SOURCE_CODEX:
        add("trimmed_codex_store_readonly", "readonly", root_auth, file=True)
    else:
        add("root_auth_store_hidden", "hidden", [root_auth])

    # --- MUST WORK ------------------------------------------------------
    add("profile_dir_rw", "rw", profile_home_s)
    add("workspace_rw", "rw", workspace_s)
    if vault_readonly(manifest):
        add("submissions_dir_rw", "rw", lane_submissions_dir(lane, home))
    if kanban_db_dir:
        add("kanban_db_begin_immediate", "kanban_lock", _norm(kanban_db_dir))
    if network:
        add("dns_resolve", "dns", PROBE_DNS_HOST)
        add("https_head", "https_head", PROBE_HTTPS_URL)
    add("op_service_account_token_absent", "env_absent", "OP_SERVICE_ACCOUNT_TOKEN")
    add("hermes_env_sourced_set", "env_equals", "HERMES_ENV_SOURCED", value="1")
    add("sandbox_no_refresh_set", "env_equals", SANDBOX_NO_REFRESH_ENV, value="1")
    return checks


# Runs inside the sandbox. Self-contained; stdlib only; prints JSON results.
PROBE_SCRIPT = r'''
import errno, hashlib, json, os, socket, sqlite3, sys, tempfile, http.client, urllib.parse

def _errname(e):
    return errno.errorcode.get(getattr(e, "errno", None) or -1, type(e).__name__)

def hidden(c):
    seen = [p for p in c["target"] if os.path.lexists(p)]
    return (not seen, "visible: %d path(s)" % len(seen) if seen else "")

def readonly(c):
    t = c["target"]
    try:
        if c.get("file"):
            fd = os.open(t, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
        else:
            fd, p = tempfile.mkstemp(prefix=".sandbox-probe-", dir=t)
            os.close(fd); os.unlink(p)
        return (False, "write succeeded")
    except OSError as e:
        if e.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
            return (True, _errname(e))
        if e.errno == errno.ENOENT:
            return (False, "ENOENT (not bound)")
        return (False, _errname(e))

def rw(c):
    try:
        fd, p = tempfile.mkstemp(prefix=".sandbox-probe-", dir=c["target"])
        os.write(fd, b"probe\n"); os.close(fd); os.unlink(p)
        return (True, "")
    except OSError as e:
        return (False, _errname(e))

def kanban_lock(c):
    db = os.path.join(c["target"], "kanban.db")
    try:
        if not os.path.isdir(c["target"]):
            return (False, "dir not visible")
        con = sqlite3.connect(db, timeout=5.0)
        try:
            con.execute("BEGIN IMMEDIATE"); con.execute("ROLLBACK")
        finally:
            con.close()
        return (True, "")
    except sqlite3.Error as e:
        return (False, type(e).__name__)

def _identity(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat = fh.read()
        rest = stat.rpartition(b")")[2].split()
        starttime = rest[19].decode("ascii")
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            cmdline = fh.read()
    except (OSError, IndexError, ValueError, UnicodeDecodeError):
        return None
    return {"starttime": starttime, "cmdline_sha256": hashlib.sha256(cmdline).hexdigest()}

def pid_hidden(c):
    pid = c["target"]
    if not os.path.exists("/proc/%d" % pid):
        return (True, "")
    want = c.get("identity")
    if not want:
        return (False, "visible")          # legacy number-only check
    have = _identity(pid)
    if have is None:
        return (True, "pid vanished while checking")
    if have.get("starttime") == want.get("starttime") and have.get("cmdline_sha256") == want.get("cmdline_sha256"):
        return (False, "visible (identity matches outside process)")
    return (True, "pid number reused inside namespace; identity differs")

def dns(c):
    try:
        r = socket.getaddrinfo(c["target"], 443, proto=socket.IPPROTO_TCP)
        return (len(r) > 0, "%d address(es)" % len(r))
    except OSError as e:
        return (False, _errname(e))

def https_head(c):
    u = urllib.parse.urlsplit(c["target"])
    try:
        con = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=20)
        con.request("HEAD", u.path or "/", headers={"User-Agent": "hermes-sandbox-probe"})
        st = con.getresponse().status
        con.close()
        return (True, "status %d" % st)
    except Exception as e:  # noqa: BLE001
        return (False, _errname(e))

def env_absent(c):
    return (c["target"] not in os.environ, "present" if c["target"] in os.environ else "")

def env_equals(c):
    ok = os.environ.get(c["target"]) == c["value"]
    return (ok, "" if ok else ("unset" if c["target"] not in os.environ else "different value"))

KINDS = dict(hidden=hidden, readonly=readonly, rw=rw, kanban_lock=kanban_lock,
             pid_hidden=pid_hidden, dns=dns, https_head=https_head,
             env_absent=env_absent, env_equals=env_equals)

def main():
    checks = json.loads(sys.argv[1])
    out = []
    for c in checks:
        fn = KINDS.get(c["kind"])
        if fn is None:
            out.append({"name": c["name"], "ok": False, "detail": "unknown kind"}); continue
        try:
            ok, detail = fn(c)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, type(e).__name__
        out.append({"name": c["name"], "ok": bool(ok), "detail": detail})
    sys.stdout.write(json.dumps(out))

main()
'''


def _probe_python(home: Path) -> str:
    """Interpreter visible inside the sandbox: the venv python when it lives under a bound runtime path."""
    import sys
    exe = _norm(sys.executable)
    for p in runtime_code_paths(home):
        if _is_within(exe, p):
            return exe
    return "/usr/bin/python3"


def _probe_env() -> Dict[str, str]:
    """Minimal environment for the probe process (never the caller's secrets)."""
    keep = ("PATH", "LANG", "LC_ALL", "TERM")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def run_probe(
    prefix: Sequence[str],
    checks: Sequence[Dict[str, Any]],
    *,
    runner: Optional[Callable[..., Any]] = None,
    python: str = "/usr/bin/python3",
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Run ``checks`` inside ``prefix`` (a bwrap argv) and return a result dict.

    ``runner`` has the ``subprocess.run`` signature (injected by tests). The
    result is ``{"ok": bool, "exit_code": int, "results": [...], "error": str}``;
    ``exit_code`` is 0 only when every check passed.
    """
    import json
    import subprocess

    runner = runner or subprocess.run
    argv = list(prefix) + ["--", python, "-I", "-c", PROBE_SCRIPT, json.dumps(list(checks))]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=timeout, env=_probe_env())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_code": EXIT_PROBE_RUNNER, "results": [],
                "error": f"probe runner failed: {type(exc).__name__}"}
    stdout = getattr(proc, "stdout", "") or ""
    try:
        results = json.loads(stdout)
        if not isinstance(results, list):
            raise ValueError("not a list")
    except Exception:  # noqa: BLE001
        tail = (getattr(proc, "stderr", "") or "").strip().splitlines()[-1:] or [""]
        return {"ok": False, "exit_code": EXIT_PROBE_RUNNER, "results": [],
                "error": f"sandbox exited {getattr(proc, 'returncode', '?')} without results: {tail[0][:200]}"}
    expected = [c["name"] for c in checks]
    got = {r.get("name"): r for r in results}
    ordered = [got.get(n, {"name": n, "ok": False, "detail": "no result"}) for n in expected]
    ok = bool(expected) and all(bool(r.get("ok")) for r in ordered)
    return {"ok": ok, "exit_code": EXIT_PROBE_PASS if ok else EXIT_PROBE_FAIL, "results": ordered, "error": ""}


def format_probe(result: Dict[str, Any]) -> str:
    lines = []
    for r in result.get("results", []):
        tag = "PASS" if r.get("ok") else "FAIL"
        detail = f"  ({r['detail']})" if r.get("detail") else ""
        lines.append(f"{tag}  {r['name']}{detail}")
    if result.get("error"):
        lines.append(f"ERROR  {result['error']}")
    n = len(result.get("results", []))
    passed = sum(1 for r in result.get("results", []) if r.get("ok"))
    lines.append(f"{'PASS' if result.get('ok') else 'FAIL'}: {passed}/{n} checks passed")
    return "\n".join(lines)


def _gateway_pid() -> Optional[int]:
    import subprocess
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "hermes-gateway.service", "-p", "MainPID", "--value"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        pid = int(out or 0)
        return pid or None
    except Exception:  # noqa: BLE001
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json
    import shutil
    import sys

    ap = argparse.ArgumentParser(prog="python -m hermes_cli.worker_sandbox")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="run the sandbox self-check inside a lane's sandbox")
    p.add_argument("--profile", required=True, help="profile name (lane = profile_company(profile))")
    p.add_argument("--lane", help="override the lane (default: profile_company(profile))")
    p.add_argument("--manifest", help="lane manifest path (default: ~/.hermes/security/worker-sandbox/lanes.yaml)")
    p.add_argument("--profile-home", help="profile directory (default: ~/.hermes/profiles/<profile>)")
    p.add_argument("--workspace", required=True, help="scratch workspace directory (bound rw, written to)")
    p.add_argument("--kanban-db-dir", help="directory holding kanban.db (default: $HERMES_KANBAN_DB's dir)")
    p.add_argument("--create-lane-home", action="store_true",
                   help="mkdir <sandbox_home_root>/<lane> if missing (otherwise it must exist)")
    p.add_argument("--skip-network", action="store_true", help="omit the DNS and HTTPS checks")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)

    home = Path.home()
    manifest_path = Path(args.manifest) if args.manifest else default_manifest_path(home)
    if not manifest_path.is_file():
        print(f"probe: manifest {manifest_path} not found; pass --manifest", file=sys.stderr)
        return EXIT_PROBE_CONFIG
    try:
        manifest = load_manifest(manifest_path) or {}
    except SandboxConfigError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return EXIT_PROBE_CONFIG
    lane = (args.lane or lane_for(args.profile) or "").strip().casefold()
    if not lane:
        print(f"probe: profile {args.profile!r} has no company lane; pass --lane", file=sys.stderr)
        return EXIT_PROBE_CONFIG
    profile_home = Path(args.profile_home) if args.profile_home else home / ".hermes" / "profiles" / args.profile
    workspace = Path(args.workspace)
    if not profile_home.is_dir() or not workspace.is_dir():
        print("probe: --profile-home and --workspace must be existing directories", file=sys.stderr)
        return EXIT_PROBE_CONFIG
    kd = args.kanban_db_dir
    if not kd:
        db = os.environ.get("HERMES_KANBAN_DB", "").strip()
        kd = str(Path(db).parent) if db else None
    if shutil.which("bwrap") is None:
        print("probe: bwrap not installed", file=sys.stderr)
        return EXIT_PROBE_RUNNER
    try:
        prefix = build_bwrap_argv(lane, args.profile, profile_home, workspace, manifest,
                                  kanban_db_dir=kd, home=home)
    except SandboxConfigError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return EXIT_PROBE_CONFIG
    lane_home = Path(prefix[prefix.index("HOME") + 1])
    if not lane_home.is_dir():
        if not args.create_lane_home:
            print(f"probe: lane home {lane_home} missing (pass --create-lane-home)", file=sys.stderr)
            return EXIT_PROBE_CONFIG
        lane_home.mkdir(parents=True, exist_ok=True)
    # Recorded here, outside bwrap, by identity: inside the sandbox's fresh pid
    # namespace these numbers may be reused by the probe's own processes.
    outside = outside_processes([os.getpid(), os.getppid(), _gateway_pid()])
    checks = probe_checks(lane, args.profile, profile_home, workspace, manifest,
                          kanban_db_dir=kd, home=home, outside_pids=outside, network=not args.skip_network)
    result = run_probe(prefix, checks, python=_probe_python(home))
    result["lane"] = lane
    result["profile"] = args.profile
    print(json.dumps(result, indent=2) if args.json else format_probe(result))
    return int(result["exit_code"])


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
