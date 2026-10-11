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
    groups:
      <group>: [<lane>, <lane>]      # lanes in a group see each other's repos/vault
    lanes:
      <lane>:
        repos: [<dir>, ...]
        vault: [<dir>, ...]
        browser_profiles: [<dir>, ...]
        extra_ro: [<path>, ...]
        extra_rw: [<path>, ...]
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

    for p in manifest.get("credentials_ro") or []:
        n = _norm(p)
        if n.endswith("/") or (exists(n) and os.path.isdir(n)):
            raise SandboxConfigError(f"worker sandbox: credentials_ro entry {n} is a directory; exact files only")
        _check_allowed(n, home, allow_credential_file=True)
        argv += ["--ro-bind", n, n]

    argv += ["--bind", profile_home_s, profile_home_s]
    argv += ["--bind", workspace_s, workspace_s]
    if kanban_db_dir:
        kd = _norm(kanban_db_dir)
        _check_allowed(kd, home)
        argv += ["--bind", kd, kd]

    for key in ("repos", "vault", "browser_profiles", "extra_rw"):
        for p in _lane_paths(manifest, lane, key):
            _check_allowed(p, home)
            argv += ["--bind", p, p]
    for p in _lane_paths(manifest, lane, "extra_ro"):
        _check_allowed(p, home)
        argv += ["--ro-bind", p, p]

    root = manifest.get("sandbox_home_root") or str(home / ".hermes" / "security" / "worker-sandbox" / "homes")
    lane_home = _norm(Path(root) / lane)
    _check_allowed(lane_home, home)
    argv += ["--bind", lane_home, lane_home]

    argv += ["--setenv", "HOME", lane_home]
    argv += ["--setenv", "HERMES_ENV_SOURCED", "1"]
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
