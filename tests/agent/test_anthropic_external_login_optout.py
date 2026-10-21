"""``auth.adopt_external_logins: false`` keeps Hermes off the Claude Code login.

Hand-ported from upstream 6c7f693473. Claude Code's OAuth refresh token is single-use: once Hermes
borrows and refreshes it, Hermes and Claude Code hold one token family and whichever refreshes first
logs the other out. With the opt-out set, Hermes must neither read nor refresh
``~/.claude/.credentials.json``, must drop the pool row an earlier adopting process persisted, and
must say so in ``hermes auth list``. The default (no ``auth`` section) is unchanged.

The fork reads the Claude Code file from ``Path.home()/.claude`` (no CLAUDE_CONFIG_DIR), so the
test points HOME at a temp directory.
"""
from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from agent import anthropic_adapter as ac
from agent import credential_sources
from agent.credential_pool import load_pool
from hermes_cli.auth_commands import auth_list_command


def _write_config(hermes_home, adopt, arm):
    body = "model:\n  provider: anthropic\n  model: claude-sonnet-4-5\n"
    if adopt is not None:
        body += f"auth:\n  adopt_external_logins: {'true' if adopt else 'false'}\n"
    body += f"# arm {arm}\n"  # distinct size so the mtime/size config cache always invalidates
    (hermes_home / "config.yaml").write_text(body)


@pytest.mark.skipif(sys.platform == "darwin", reason="Keychain is a second Claude Code source on macOS")
def test_opt_out_never_reads_claude_code_login(tmp_path, monkeypatch):
    hermes_home, home = tmp_path / "hermes", tmp_path / "home"
    hermes_home.mkdir()
    (home / ".claude").mkdir(parents=True)
    (hermes_home / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(home))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(credential_sources, "_notice_logged", False, raising=False)
    cred_file = home / ".claude" / ".credentials.json"
    cred_file.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-valid", "refreshToken": "sk-ant-ort01-cc",
        "expiresAt": int(time.time() * 1000) + 3_600_000, "scopes": ["user:inference"]}}))
    original_bytes = cred_file.read_bytes()

    # Arm A (default): today's behaviour — the borrowed login is read and seeded into the pool.
    _write_config(hermes_home, adopt=None, arm="a")
    assert ac.read_claude_code_credentials() is not None
    assert "claude_code" in [e.source for e in load_pool("anthropic").entries()]

    # Arm B (opt-out): no read, the persisted row is dropped, the file is untouched, list explains why.
    _write_config(hermes_home, adopt=False, arm="b")
    assert ac.read_claude_code_credentials() is None
    assert "claude_code" not in [e.source for e in load_pool("anthropic").entries()]
    assert cred_file.read_bytes() == original_bytes
    out = io.StringIO()
    with redirect_stdout(out):
        auth_list_command(SimpleNamespace(provider=None))
    assert credential_sources.EXTERNAL_LOGINS_NOT_ADOPTED_NOTICE in out.getvalue()

    # Arm A again: flipping back adopts exactly as before and the notice disappears.
    _write_config(hermes_home, adopt=True, arm="c")
    assert ac.read_claude_code_credentials() is not None
    assert "claude_code" in [e.source for e in load_pool("anthropic").entries()]
    out = io.StringIO()
    with redirect_stdout(out):
        auth_list_command(SimpleNamespace(provider=None))
    assert credential_sources.EXTERNAL_LOGINS_NOT_ADOPTED_NOTICE not in out.getvalue()
