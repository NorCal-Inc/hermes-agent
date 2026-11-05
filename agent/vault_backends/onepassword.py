"""1Password Login items as a vault backend (``op`` CLI).

Unlock: ``op signin --raw`` with the master password on stdin (desktop-app
integration or account-level auth) mints an ``OP_SESSION_<account>`` token.
A configured service-account token skips the prompt entirely (headless).
List: ``op item list --categories Login --format json`` → title, urls,
username, vault. Resolve: ``op item get <id> --vault <vault-id> ...`` (service
accounts reject an item read without a vault query).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from agent.secret_sources.base import run_cli
from agent.secret_sources.onepassword import _OP_ENV_ALLOWLIST, _scrub, find_op
from agent.vault_backends.base import LoginBackend, UnlockRequired, run_with_stdin_secret
from agent.vault_backends import unlock as _unlock
from agent.vault_store import VaultItemMeta, normalize_origin

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def _token_from_file(env_name: str, path: Optional[str]) -> str:
    """``env_name``'s value from a dotenv-format service-account file, or "".

    Parsed, never sourced. Refuses a file that group/other can read: a token file
    anyone else can open is already a leak, not a credential source.
    """
    if not path:
        return ""
    f = Path(os.path.expanduser(str(path)))
    try:
        if not f.is_file():
            return ""
        if f.stat().st_mode & 0o077:
            logger.warning("1Password token file %s is group/other-readable; ignoring it", f)
            return ""
        text = f.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == env_name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


class OnePasswordLoginBackend(LoginBackend):
    name = "onepassword"
    display_name = "1Password"
    prefix = "op:"
    needs_unlock = True

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        from agent.secret_scope import get_secret
        env_name = str(self.cfg.get("service_account_token_env") or "OP_SERVICE_ACCOUNT_TOKEN")
        self._service_token = get_secret(env_name, "") or ""
        if not self._service_token:
            # Workers never carry the token in their env (shared profiles have no .env;
            # the sandbox strips it on purpose), so the in-process browser_vault saw
            # "1Password locked" forever -- Paris's Blogger publish, 2026-11-04,
            # t_49332b78. Read it from the same 0600 file hermes-run --company or the
            # sandbox's lane op.env already uses; it stays in this backend and only
            # reaches op's own child env (_env), never os.environ.
            self._service_token = _token_from_file(
                env_name,
                self.cfg.get("service_account_token_file") or os.environ.get("HERMES_LANE_OP_ENV"),
            )
        self._item_vaults: dict[str, str] = {}  # item id -> vault id from the latest ``item list``

    # ── auth ────────────────────────────────────────────────────────────────

    def _op(self) -> Path:
        op = find_op(str(self.cfg.get("binary_path") or ""))
        if op is None:
            raise RuntimeError("1Password CLI (op) not found — install it or set vault.onepassword.binary_path")
        return op

    def _env(self, session_token: Optional[str]) -> dict[str, str]:
        from agent.secret_scope import get_secret
        env = {k: os.environ[k] for k in _OP_ENV_ALLOWLIST if k in os.environ and not k.startswith("OP_CONNECT_")}
        # Connect credentials outrank OP_SERVICE_ACCOUNT_TOKEN inside op, so they must come from the
        # profile's own secret scope like the service token does — never from the launch environment.
        for k in ("OP_CONNECT_HOST", "OP_CONNECT_TOKEN"):
            if v := get_secret(k, ""):
                env[k] = v
        env["NO_COLOR"] = "1"
        account = str(self.cfg.get("account") or "")
        if account:
            env["OP_ACCOUNT"] = account
        if self._service_token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = self._service_token
        elif session_token:
            # op signin --raw prints the bare token; the env var name carries the account shorthand,
            # which op also accepts as plain OP_SESSION for the default account.
            env[f"OP_SESSION_{account}" if account else "OP_SESSION"] = session_token
        return env

    def is_unlocked(self) -> bool:
        return bool(self._service_token) or _unlock.is_unlocked(self.name)

    def unlock(self, master_password: str) -> None:
        """Mint a session token from the master password (consumed on stdin, never argv)."""
        generation = _unlock.begin_unlock(self.name)
        cmd = [str(self._op()), "signin", "--raw"]
        if account := str(self.cfg.get("account") or ""):
            cmd += ["--account", account]
        proc = run_with_stdin_secret(cmd, env=self._env(None), secret=master_password, timeout=_TIMEOUT, label="op")
        token = (proc.stdout or "").strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"1Password unlock failed: {_scrub(proc.stderr or '')[:200] or 'no session token'}")
        if not _unlock.store_session_token(self.name, token, generation):
            raise RuntimeError("1Password was locked while unlocking; try again")

    def _run(self, *args: str) -> str:
        token = None if self._service_token else _unlock.get_session_token(self.name)
        if not self._service_token and not token:
            raise UnlockRequired(self)
        proc = run_cli([str(self._op()), *args], env=self._env(token), timeout=_TIMEOUT, label="op",
                       timeout_message="op timed out", stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            err = _scrub(proc.stderr or "")
            # A service account never holds a session, so an auth failure there is not a lock to undo:
            # surface op's own error instead of an unlock prompt that reports "already unlocked".
            if not self._service_token and any(w in err.lower() for w in ("session", "sign in", "not signed in")):
                _unlock.lock(self.name)
                raise UnlockRequired(self)
            raise RuntimeError(f"op failed: {err[:200]}")
        return proc.stdout or ""

    # ── backend contract ───────────────────────────────────────────────────
    def list_items(self) -> list[VaultItemMeta]:
        if not self.is_unlocked():
            return []
        raw = json.loads(self._run("item", "list", "--categories", "Login", "--format", "json") or "[]")
        out: list[VaultItemMeta] = []
        self._item_vaults = {}
        for item in raw if isinstance(raw, list) else []:
            vault = item.get("vault") or {}
            if item.get("id"):
                self._item_vaults[str(item["id"])] = str(vault.get("id") or "") if isinstance(vault, dict) else ""
            urls = [str(u["href"]) for u in item.get("urls") or [] if isinstance(u, dict) and u.get("href")]
            origins = _all_origins(urls)
            if not origins:
                continue
            username = str(item.get("additional_information") or "").strip() or None
            out.append(VaultItemMeta(
                id=f"{self.prefix}{item.get('id')}", kind="login", label=str(item.get("title") or origins[0]),
                origin=origins[0], created_at=str(item.get("created_at") or ""),
                identifier_type="username" if username else None, identifier=username,
                allowed_origins=_web_origins(origins)))
        return out

    def get_meta(self, handle: str) -> Optional[VaultItemMeta]:
        return next((m for m in self.list_items() if m.id == handle), None)

    def _item_selector(self, handle: str) -> list[str]:
        """``op item get`` target for a listed login. A service account must name the item's vault
        (``--vault``) and ``item list`` reports it, so the vault comes from the same listing that
        proved the handle is a Login item; a handle nothing listed is refused before any secret read."""
        item_id = handle[len(self.prefix):]
        if item_id not in self._item_vaults:
            self.list_items()
        if item_id not in self._item_vaults:
            raise RuntimeError("1Password login not found; list logins again")
        vault_id = self._item_vaults[item_id]
        return [item_id, "--vault", vault_id] if vault_id else [item_id]

    def resolve_password(self, handle: str) -> str:
        return self._run("item", "get", *self._item_selector(handle),
                         "--fields", "label=password", "--reveal").rstrip("\r\n")

    def resolve_otp(self, handle: str) -> Optional[str]:
        # `--otp` mints the current TOTP from the item's one-time-password field; items without one error out.
        try:
            code = self._run("item", "get", *self._item_selector(handle), "--otp").strip()
        except Exception:
            return None
        return code if code.isdigit() else None


def _web_origins(origins: list[str]) -> tuple:
    """Fill targets are browser pages, so app URIs (``androidapp://`` etc.) never
    widen the fill set; an item whose only URI is an app URI keeps its single
    (unfillable-from-a-page) origin exactly as before."""
    web = tuple(o for o in origins if o.startswith(("http://", "https://")))
    return web or (origins[0],)


def _all_origins(urls: list[str]) -> list[str]:
    """Every normalized origin saved on the item, deduped, order preserved.

    A 1Password Login item can carry several websites; each of them is a place the
    user told 1Password the credential belongs, so all of them are valid fill targets.
    """
    out: list[str] = []
    for u in urls:
        try:
            origin = normalize_origin(u)
        except Exception:
            continue
        if origin not in out:
            out.append(origin)
    return out
