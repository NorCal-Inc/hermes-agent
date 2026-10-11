"""Gateway-owned Codex refresh + per-lane trimmed credential files (C1.2d).

Design: ``vault/Business/Systems/2026-10-11-worker-sandbox-design.md``.

Problem: 28/32 profiles have no ``openai-codex`` state of their own and fall
through to the ROOT store (``<root>/auth.json``), which is the whole provider
credential pool. A refresh performed by any of them writes only the PROFILE
store, and Codex refresh tokens are single-use, so N profiles race one global
refresh token. Binding the root store read-only into a company sandbox also
exposes every provider key to that lane.

This module makes the gateway the single owner of Codex rotation for the
root store and materialises, per sandboxed lane, a TRIMMED read-only copy
that contains only the ``openai-codex`` provider state:

    ~/.hermes/security/worker-sandbox/creds/<lane>/auth.json   (mode 600)

:mod:`hermes_cli.worker_sandbox` binds that file read-only AT
``<root>/auth.json`` inside the lane's sandbox, and sets
``HERMES_SANDBOX_NO_REFRESH=1`` so the worker never rotates tokens itself
(see ``resolve_codex_runtime_credentials`` in :mod:`hermes_cli.auth`).

Write strategy for the lane file: created atomically (O_EXCL + rename via
``_save_auth_store``); subsequent updates are written IN PLACE on the same
inode. bwrap binds an inode, not a path, so a rename on the host would leave
every already-running sandbox looking at the pre-refresh file. An in-place
rewrite is visible to running workers immediately; the reader side
(``_load_global_auth_store``) tolerates the microsecond partial-read window
by treating unparsable JSON as "no state" for that single call.

Nothing here ever reads a profile store, performs a refresh outside the
root-store lock, or logs a token value.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

PROVIDER_ID = "openai-codex"
CREDS_RELPATH = Path(".hermes") / "security" / "worker-sandbox" / "creds"
# Refresh the root token well before the worker-side skew (120s): a sandboxed
# worker can never refresh, so the gateway must hand it a token that outlives
# the gap until the next gateway tick plus the worker's own skew.
ROOT_REFRESH_SKEW_SECONDS = 900
DEFAULT_TICK_SECONDS = 60.0

RefreshFn = Callable[[str, str], Dict[str, Any]]


class CodexLaneCredsError(RuntimeError):
    """The root store has no usable openai-codex state or the lane file is unsafe."""


def default_creds_root(home: Optional[Path] = None) -> Path:
    return (home or Path.home()) / CREDS_RELPATH


def lane_creds_path(lane: str, *, creds_root: Optional[Path] = None, home: Optional[Path] = None) -> Path:
    lane = str(lane).strip().casefold()
    if not lane or "/" in lane or lane in (".", ".."):
        raise CodexLaneCredsError(f"invalid lane name {lane!r}")
    return (creds_root or default_creds_root(home)) / lane / "auth.json"


def codex_lanes(manifest: Dict[str, Any]) -> List[str]:
    """Lanes whose LLM credential is Codex-sourced (manifest ``llm_source``, default codex)."""
    from hermes_cli.worker_sandbox import lane_llm_source, LLM_SOURCE_CODEX

    out: List[str] = []
    for lane in (manifest.get("lanes") or {}):
        if lane_llm_source(manifest, str(lane)) == LLM_SOURCE_CODEX:
            out.append(str(lane).strip().casefold())
    return out


# --------------------------------------------------------------------------
# (1a) root-store refresher — one owner, under the root store's own lock
# --------------------------------------------------------------------------

def _root_codex_state(auth_store: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    providers = auth_store.get("providers")
    if not isinstance(providers, dict):
        return None
    state = providers.get(PROVIDER_ID)
    return dict(state) if isinstance(state, dict) else None


def refresh_root_codex_if_expiring(
    root_path: Path,
    *,
    skew_seconds: int = ROOT_REFRESH_SKEW_SECONDS,
    refresh_fn: Optional[RefreshFn] = None,
    timeout_seconds: float = 20.0,
) -> str:
    """Refresh the ROOT store's openai-codex token pair when inside the skew window.

    Returns ``"absent"`` (no codex state in the root store), ``"fresh"``
    (not inside the skew window, nothing written) or ``"refreshed"`` (new
    pair persisted to ``root_path`` with the store's atomic writer). The
    whole read-decide-refresh-write sequence runs under the root store's own
    lock (``_auth_store_lock(target_path=root_path)``) so it serialises with
    any other writer of that file (``hermes auth``, Nous/xAI write-throughs).
    Never touches a profile store.
    """
    from hermes_cli import auth as _auth

    if refresh_fn is None:
        def refresh_fn(access: str, refresh: str) -> Dict[str, Any]:  # pragma: no cover - network
            return _auth.refresh_codex_oauth_pure(access, refresh, timeout_seconds=timeout_seconds)

    with _auth._auth_store_lock(timeout_seconds=max(15.0, timeout_seconds + 5.0), target_path=root_path):
        auth_store = _auth._load_auth_store(root_path)
        state = _root_codex_state(auth_store)
        if not state:
            return "absent"
        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        access = str(tokens.get("access_token", "") or "").strip()
        refresh = str(tokens.get("refresh_token", "") or "").strip()
        if not access or not refresh:
            return "absent"
        if not _auth._codex_access_token_is_expiring(access, skew_seconds):
            return "fresh"
        refreshed = refresh_fn(access, refresh)
        new_tokens = dict(tokens)
        new_tokens["access_token"] = refreshed["access_token"]
        new_tokens["refresh_token"] = refreshed.get("refresh_token") or refresh
        last_refresh = (
            refreshed.get("last_refresh")
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        previous = state.get("tokens") if isinstance(state.get("tokens"), dict) else None
        state["tokens"] = new_tokens
        state["last_refresh"] = last_refresh
        state["auth_mode"] = "chatgpt"
        auth_store.setdefault("providers", {})[PROVIDER_ID] = state
        # Keep the root credential_pool aliases of the singleton in step
        # (same helper the profile-side save uses; operates on this dict only).
        _auth._sync_codex_pool_entries(auth_store, new_tokens, last_refresh, previous_singleton_tokens=previous)
        _auth._save_auth_store(auth_store, target_path=root_path)
        logger.info("codex lane creds: refreshed root openai-codex token pair")
        return "refreshed"


# --------------------------------------------------------------------------
# (1b) trimmed per-lane writer
# --------------------------------------------------------------------------

def trimmed_store(root_store: Dict[str, Any]) -> Dict[str, Any]:
    """The ONLY content a Codex lane may see: the openai-codex singleton."""
    state = _root_codex_state(root_store)
    if not state:
        raise CodexLaneCredsError("root auth store has no openai-codex state")
    return {"version": root_store.get("version", 1), "active_provider": PROVIDER_ID,
            "providers": {PROVIDER_ID: state}}


def _write_in_place(path: Path, payload: str) -> None:
    """Rewrite an existing file on its current inode (see module docstring)."""
    data = payload.encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_NOFOLLOW)
    try:
        os.write(fd, data)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)


def materialize_lane_file(lane: str, root_store: Dict[str, Any], *, creds_root: Path) -> Path:
    """Write ``creds/<lane>/auth.json`` (mode 600, dir 700) with only the codex state.

    First write is atomic (O_EXCL temp + rename); later writes are in place on
    the same inode. Unchanged content is not rewritten (no mtime churn, so the
    sandbox reader's mtime cache stays valid). Refuses symlinks and group/other
    readable files.
    """
    from hermes_cli import auth as _auth

    path = lane_creds_path(lane, creds_root=creds_root)
    doc = trimmed_store(root_store)
    doc["updated_at"] = root_store.get("updated_at") or datetime.now(timezone.utc).isoformat()
    payload = json.dumps(doc, indent=2) + "\n"
    if path.is_symlink():
        raise CodexLaneCredsError(f"lane creds path {path} is a symlink")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(stat.S_IRWXU)
        _auth._save_auth_store(json.loads(payload), target_path=path)
        return path
    st = path.stat()
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CodexLaneCredsError(f"lane creds file {path} is group/other accessible")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        current = None
    if isinstance(current, dict):
        same = {k: v for k, v in current.items() if k != "updated_at"} == {k: v for k, v in doc.items() if k != "updated_at"}
        if same:
            return path
    _write_in_place(path, payload)
    return path


def materialize_lanes(lanes: Iterable[str], *, root_path: Path, creds_root: Path) -> Dict[str, str]:
    """Regenerate every lane file from the current root store. Returns lane -> outcome."""
    from hermes_cli import auth as _auth

    with _auth._auth_store_lock(target_path=root_path):
        root_store = _auth._load_auth_store(root_path)
    out: Dict[str, str] = {}
    for lane in lanes:
        try:
            materialize_lane_file(lane, root_store, creds_root=creds_root)
            out[lane] = "ok"
        except CodexLaneCredsError as exc:
            out[lane] = f"error: {exc}"
            logger.warning("codex lane creds: lane %s not materialised: %s", lane, exc)
    return out


# --------------------------------------------------------------------------
# one tick (gateway watcher body) — refresh, then regenerate the lane files
# --------------------------------------------------------------------------

def run_once(
    *,
    manifest: Optional[Dict[str, Any]] = None,
    root_path: Optional[Path] = None,
    creds_root: Optional[Path] = None,
    home: Optional[Path] = None,
    refresh_fn: Optional[RefreshFn] = None,
    skew_seconds: int = ROOT_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    """One gateway tick. Returns ``{"refresh": str, "lanes": {lane: outcome}}``.

    No-op (``{"refresh": "skipped", "lanes": {}}``) when the sandbox is not
    enforced or no manifest exists, so an un-configured gateway never writes.
    """
    from hermes_cli import worker_sandbox as ws

    home = Path(home) if home is not None else Path.home()
    if manifest is None:
        if ws.sandbox_config()["mode"] != ws.MODE_ENFORCE:
            return {"refresh": "skipped", "lanes": {}}
        manifest = ws.load_manifest(ws.default_manifest_path(home))
        if manifest is None:
            return {"refresh": "skipped", "lanes": {}}
    lanes = codex_lanes(manifest)
    if not lanes:
        return {"refresh": "skipped", "lanes": {}}
    if root_path is None:
        from hermes_constants import get_default_hermes_root
        root_path = get_default_hermes_root() / "auth.json"
    creds_root = creds_root or default_creds_root(home)
    outcome = refresh_root_codex_if_expiring(root_path, skew_seconds=skew_seconds, refresh_fn=refresh_fn)
    if outcome == "absent":
        return {"refresh": outcome, "lanes": {lane: "error: root store has no openai-codex state" for lane in lanes}}
    return {"refresh": outcome, "lanes": materialize_lanes(lanes, root_path=root_path, creds_root=creds_root)}
