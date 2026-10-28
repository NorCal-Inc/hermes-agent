"""Each company lane gets only its own 1Password service-account file, read-only."""
from pathlib import Path

from hermes_cli import worker_sandbox as ws


def _manifest():
    return {"lanes": {"orion": {}, "logos": {}}, "sandbox_home_root": "/home/u/.hermes/security/worker-sandbox/homes"}


def _argv(lane, present):
    home = Path("/home/u")
    def exists(p):
        p = str(p)
        if p.endswith("/op.env"):
            return p in present
        return not p.endswith("auth.json") and "company-submissions" not in p or True
    return ws.build_bwrap_argv(lane, "p", "/home/u/.hermes/profiles/p", "/home/u/.hermes/kanban/workspaces/t",
                               _manifest(), home=home, exists=exists)


def test_lane_gets_own_op_env_readonly_and_var():
    f = "/home/u/.hermes/security/worker-sandbox/creds/orion/op.env"
    argv = _argv("orion", {f})
    i = argv.index(f)
    assert argv[i - 1] == "--ro-bind" and argv[i + 1] == f
    j = argv.index(ws.LANE_OP_ENV_VAR)
    assert argv[j - 1] == "--setenv" and argv[j + 1] == f
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in argv
    assert not any("/creds/logos/" in a for a in argv)


def test_no_op_env_no_bind():
    argv = _argv("logos", set())
    assert ws.LANE_OP_ENV_VAR not in argv
    assert not any(a.endswith("/op.env") for a in argv)
