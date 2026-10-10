"""Execution contract rule 6 (production authority), segment 6b: the
REPORT-ONLY production-surface gate.

Pins: (a) each role matches its named mutating forms and ignores the
read-only forms; (b) non-production tool calls produce no event; (c) a
match on a worker card records exactly one production_action_unauthorized
event whose payload carries no command text / path / content; (d) a card
with production_actions does not make a Hermes-native worker authorized;
(e) a gate exception never breaks the tool call; (f) report-only never
returns a refusal.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import production_authority_gate as gate
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


REGISTRY = {
    "version": 1,
    "mode": "report-only",
    "christopher_authorized_executors": ["claude", "codex"],
    "roles": {r: {"christopher_only": True, "profiles": []} for r in gate.ROLES},
}


@pytest.fixture
def registry_path(tmp_path, monkeypatch):
    p = tmp_path / "production-roles.json"
    p.write_text(json.dumps(REGISTRY), encoding="utf-8")
    monkeypatch.setattr(gate, "REGISTRY_PATH", p)
    return p


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, registry_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # Never resolve to the operator's live board (see test_task_contract_gate).
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", gate.ENV_TASK, gate.ENV_RUN_ID):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    yield home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _task(conn):
    return kb.create_task(conn, title="prod gate", assignee="w")


def _events(conn, task_id):
    return [e for e in kb.list_events(conn, task_id) if e.kind == gate.EVENT_UNAUTHORIZED]


def _roles(tool, args):
    return sorted(m.role for m in gate.match_surfaces(tool, args))


# ---------------------------------------------------------------------------
# (a) surface matcher: mutating forms match, read-only forms do not
# ---------------------------------------------------------------------------

MUTATING = [
    ("firewall", "terminal", {"command": "sudo ufw allow 443/tcp"}),
    ("firewall", "terminal", {"command": "ufw deny 5100"}),
    ("firewall", "terminal", {"command": "iptables -A INPUT -p tcp --dport 22 -j ACCEPT"}),
    ("firewall", "terminal", {"command": "sudo ip6tables -F"}),
    ("firewall", "terminal", {"command": "nft add rule inet filter input drop"}),
    ("firewall", "terminal", {"command": "nft -f /etc/nftables.conf"}),
    ("firewall", "terminal", {"command": "iptables-restore < rules.v4"}),
    ("service_units", "terminal", {"command": "systemctl --user restart NorCal_Hermes.service"}),
    ("service_units", "terminal", {"command": "sudo systemctl stop nginx"}),
    ("service_units", "terminal", {"command": "systemctl daemon-reload"}),
    ("service_units", "terminal", {"command": "systemctl --user enable --now foo.service"}),
    ("service_units", "terminal", {"command": "systemctl --user edit foo.service"}),
    ("service_units", "terminal", {"command": "ls && systemctl --user mask foo"}),
    ("service_units", "execute_code", {"code": "systemctl --user reload foo"}),
    ("service_units", "write_file", {"path": "/home/x/.config/systemd/user/foo.service", "content": "[Unit]\n"}),
    ("service_units", "patch", {"path": "/etc/systemd/system/foo.service", "old_string": "a", "new_string": "b"}),
    ("service_units", "write_file", {"path": "~/.config/systemd/user/foo.service", "content": "[Unit]\n"}),
    ("dns", "terminal", {"command": 'curl -X POST https://api.cloudflare.com/client/v4/zones/z/dns_records -d "{}"'}),
    ("dns", "terminal", {"command": "curl --request DELETE https://api.cloudflare.com/client/v4/zones/z/dns_records/r"}),
    ("dns", "execute_code", {"code": "requests.put('https://api.cloudflare.com/client/v4/zones/z/dns_records/r', json=body)"}),
    ("dns", "terminal", {"command": "nsupdate -k key.private update.txt"}),
    ("deploy", "terminal", {"command": "./deploy.sh"}),
    ("deploy", "terminal", {"command": "bash scripts/deploy-site.sh"}),
    ("deploy", "terminal", {"command": "python3 ops/deploy_api.py --env prod"}),
    ("deploy", "terminal", {"command": "make deploy"}),
    ("deploy", "terminal", {"command": "npm run deploy:prod"}),
    ("deploy", "terminal", {"command": "git push production main"}),
    ("deploy", "terminal", {"command": "git push --force live HEAD:main"}),
    ("deploy", "terminal", {"command": "cd repo && git push prod main"}),
    ("stripe_live", "terminal", {"command": "curl -u sk_live_abc123: https://api.stripe.com/v1/charges"}),
    ("stripe_live", "terminal", {"command": "stripe --live customers list"}),
    ("stripe_live", "execute_code", {"code": "stripe.api_key = 'rk_live_xyz9'"}),
    ("stripe_live", "write_file", {"path": "/tmp/.env", "content": "STRIPE_KEY=sk_live_abc\n"}),
    ("ports", "write_file", {"path": "/home/chris/services/app/.env", "content": "PORT=5100\n"}),
    ("ports", "patch", {"path": "/home/chris/services/app/config.yml", "old_string": "x", "new_string": "host: 0.0.0.0\n"}),
    ("ports", "write_file", {"path": "/etc/systemd/system/app.service", "content": "[Service]\nEnvironment=PORT=5100\n"}),
    ("ports", "write_file", {"path": "/etc/systemd/system/app.socket", "content": "[Socket]\nListenStream=0.0.0.0:8080\n"}),
    ("ports", "write_file", {"path": "/home/chris/services/app/run.sh", "content": "exec uvicorn app --port 5100\n"}),
]


@pytest.mark.parametrize("role,tool,args", MUTATING)
def test_mutating_forms_match_their_role(role, tool, args):
    assert role in _roles(tool, args), (role, tool, args)


READ_ONLY = [
    ("terminal", {"command": "ufw status verbose"}),
    ("terminal", {"command": "sudo ufw status"}),
    ("terminal", {"command": "ufw --version"}),
    ("terminal", {"command": "iptables -L -n -v"}),
    ("terminal", {"command": "iptables -S"}),
    ("terminal", {"command": "ip6tables -t nat -L"}),
    ("terminal", {"command": "nft list ruleset"}),
    ("terminal", {"command": "systemctl --user status NorCal_Hermes.service"}),
    ("terminal", {"command": "systemctl is-active nginx"}),
    ("terminal", {"command": "systemctl --user show foo -p ActiveState"}),
    ("terminal", {"command": "systemctl --user cat foo.service | grep -c Environment"}),
    ("terminal", {"command": "systemctl list-units --type=service"}),
    ("terminal", {"command": "curl https://api.cloudflare.com/client/v4/zones/z/dns_records"}),
    ("terminal", {"command": "curl -X GET https://api.cloudflare.com/client/v4/zones/z/dns_records"}),
    ("terminal", {"command": "git push --dry-run production main"}),
    ("terminal", {"command": "git push -n live main"}),
    ("terminal", {"command": "git push origin main"}),
    ("terminal", {"command": "git fetch production"}),
    ("terminal", {"command": "ls deploy/ && cat deploy.sh"}),
    ("terminal", {"command": "stripe customers list"}),
    ("terminal", {"command": "echo sk_test_abc"}),
    ("terminal", {"command": "ss -tln"}),
    ("terminal", {"command": "pytest -q tests/agent"}),
    ("execute_code", {"code": "print('hello')"}),
    ("write_file", {"path": "/home/chris/services/app/README.md", "content": "# PORTABLE notes\nHOSTNAME=x\n"}),
    ("write_file", {"path": "/tmp/notes.md", "content": "PORT=5100\n"}),
    ("patch", {"path": "/home/chris/repo/app.py", "old_string": "a", "new_string": "b"}),
    ("read_file", {"path": "/etc/systemd/system/foo.service"}),
    ("terminal", {"command": ""}),
    ("terminal", {}),
    ("terminal", None),
]


@pytest.mark.parametrize("tool,args", READ_ONLY)
def test_read_only_and_non_production_forms_do_not_match(tool, args):
    assert _roles(tool, args) == []


def test_one_match_per_role_even_when_several_matchers_fire():
    roles = _roles("write_file", {
        "path": "/etc/systemd/system/app.service",
        "content": "[Service]\nEnvironment=PORT=5100\nEnvironment=STRIPE=sk_live_x1\n",
    })
    assert roles == ["ports", "service_units", "stripe_live"]
    assert len(gate.match_surfaces("terminal", {"command": "ufw allow 80; ufw allow 443"})) == 1


# ---------------------------------------------------------------------------
# (b) non-production tool calls produce no event
# ---------------------------------------------------------------------------


def test_non_production_call_records_nothing(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal("terminal", {"command": "ls -la"}) is None
    assert gate.production_authority_refusal("terminal", {"command": "systemctl --user status x"}) is None
    assert gate.production_authority_refusal("read_file", {"path": "/etc/systemd/system/x.service"}) is None
    assert _events(conn, tid) == []


# ---------------------------------------------------------------------------
# (c) a match on a worker card records exactly one event, redacted
# ---------------------------------------------------------------------------


def test_match_on_worker_card_records_exactly_one_redacted_event(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setenv(gate.ENV_RUN_ID, "7")
    secret_cmd = "sudo ufw allow from 203.0.113.9 to any port 5100 # token=SEKRET-VALUE"
    assert gate.production_authority_refusal("terminal", {"command": secret_cmd}) is None
    events = _events(conn, tid)
    assert len(events) == 1
    ev = events[0]
    payload = ev.payload
    assert payload["role"] == "firewall"
    assert payload["tool"] == "terminal"
    assert payload["matcher"] == "firewall.ufw"
    assert payload["executor_lane"] == gate.HERMES_NATIVE_EXECUTOR
    assert payload["authorized"] is False
    assert payload["registry_state"] == "ok"
    assert ev.run_id == 7
    raw = json.dumps(ev.payload)
    for leak in ("SEKRET", "203.0.113.9", "ufw allow", "5100", "command"):
        assert leak not in raw, leak


def test_write_match_payload_has_no_path_or_content(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    path = "/home/chris/services/secretapp/.env"
    assert gate.production_authority_refusal(
        "write_file", {"path": path, "content": "PORT=5100\nAPI_KEY=hunter2\n"}
    ) is None
    events = _events(conn, tid)
    assert len(events) == 1
    raw = json.dumps(events[0].payload)
    assert "secretapp" not in raw and "hunter2" not in raw and "5100" not in raw
    assert events[0].payload["role"] == "ports"


def test_no_card_logs_warning_instead_of_event(conn, monkeypatch, caplog):
    monkeypatch.delenv(gate.ENV_TASK, raising=False)
    with caplog.at_level("WARNING", logger=gate.__name__):
        assert gate.production_authority_refusal(
            "terminal", {"command": "systemctl --user restart NorCal_Hermes.service"}
        ) is None
    msgs = [r.getMessage() for r in caplog.records if gate.EVENT_UNAUTHORIZED in r.getMessage()]
    assert len(msgs) == 1
    assert "service_units" in msgs[0] and "NorCal_Hermes" not in msgs[0]


# ---------------------------------------------------------------------------
# (d) card authorization never makes a Hermes-native worker authorized
# ---------------------------------------------------------------------------


def test_card_production_actions_do_not_authorize_hermes_native_worker(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setattr(gate, "_card_production_actions", lambda _tid: ("firewall",))
    assert gate.production_authority_refusal("terminal", {"command": "ufw allow 443"}) is None
    events = _events(conn, tid)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["authorized"] is False
    assert "not a christopher_authorized_executor" in payload["reason"]


def test_decision_requires_both_executor_and_card_authorization():
    registry = gate.RegistryState(
        state="ok", mode="report-only",
        authorized_executors=frozenset({"claude", "codex"}), roles=frozenset(gate.ROLES),
    )
    m = gate.SurfaceMatch("deploy", "deploy.script")
    ok = gate.decide(m, executor_lane="claude", task_id="t_1", registry=registry, card_actions=("deploy",))
    assert ok.authorized
    assert not gate.decide(m, executor_lane="hermes", task_id="t_1", registry=registry, card_actions=("deploy",)).authorized
    assert not gate.decide(m, executor_lane="claude", task_id="t_1", registry=registry, card_actions=("dns",)).authorized
    assert not gate.decide(m, executor_lane="claude", task_id=None, registry=registry, card_actions=("deploy",)).authorized
    assert not gate.decide(m, executor_lane="claude", task_id="t_1", registry=gate.RegistryState(state="missing"), card_actions=("deploy",)).authorized


def test_absent_production_actions_field_means_no_authorization(conn):
    tid = _task(conn)
    # Task dataclass at this base has no production_actions attribute (card 6c builds it).
    assert gate._card_production_actions(tid) == ()
    assert gate._card_production_actions("t_nope") == ()


# ---------------------------------------------------------------------------
# registry missing / unparseable: role=unknown, still allowed
# ---------------------------------------------------------------------------


def test_missing_registry_records_role_unknown_and_allows(conn, monkeypatch, tmp_path):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setattr(gate, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    assert gate.production_authority_refusal("terminal", {"command": "git push production main"}) is None
    events = _events(conn, tid)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["role"] == "unknown"
    assert payload["surface_role"] == "deploy"
    assert payload["registry_state"] == "missing"


def test_unparseable_registry_records_role_unknown_and_allows(conn, monkeypatch, tmp_path):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert gate.production_authority_refusal(
        "terminal", {"command": "ufw allow 22"}, registry_path=bad
    ) is None
    payload = _events(conn, tid)[0].payload
    assert payload["role"] == "unknown" and payload["registry_state"] == "unparseable"


def test_registry_without_executor_list_authorizes_nobody(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"version": 1, "mode": "report-only", "roles": REGISTRY["roles"]}))
    reg = gate.load_registry(p)
    assert reg.state == "ok" and reg.authorized_executors == frozenset()


# ---------------------------------------------------------------------------
# (e) gate exception never breaks the tool call; (f) report-only never refuses
# ---------------------------------------------------------------------------


def test_gate_exception_never_breaks_tool_call(conn, monkeypatch):
    import model_tools
    from agent import task_contract_gate

    monkeypatch.setattr(task_contract_gate, "task_contract_refusal", lambda *_a, **_k: None)

    def boom(*_a, **_k):
        raise RuntimeError("gate blew up")

    monkeypatch.setattr(gate, "production_authority_refusal", boom)
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda name, args, **kw: json.dumps({"output": "stubbed"}))
    out = model_tools.handle_function_call("terminal", {"command": "ufw allow 443"})
    assert json.loads(out) == {"output": "stubbed"}


def test_report_only_never_returns_refusal_through_dispatcher(conn, monkeypatch):
    """End to end: a firewall-shaped command from a worker card passes the
    gate (event recorded) and reaches tool dispatch; no refusal text. The
    registry dispatch is stubbed so nothing is actually executed."""
    import model_tools
    from agent import task_contract_gate

    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setattr(task_contract_gate, "task_contract_refusal", lambda *_a, **_k: None)
    dispatched = []

    def fake_dispatch(name, args, **kw):
        dispatched.append(name)
        return json.dumps({"output": "stubbed"})

    monkeypatch.setattr(model_tools.registry, "dispatch", fake_dispatch)
    out = model_tools.handle_function_call("terminal", {"command": "echo && ufw allow 443"})
    assert json.loads(out) == {"output": "stubbed"}
    assert dispatched == ["terminal"]
    assert len(_events(conn, tid)) == 1


@pytest.mark.parametrize("role,tool,args", MUTATING)
def test_report_only_returns_none_for_every_matching_form(conn, monkeypatch, role, tool, args):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal(tool, args) is None
    assert len(_events(conn, tid)) >= 1


def test_refuse_mode_registry_is_still_report_only_in_this_segment(conn, monkeypatch, tmp_path):
    p = tmp_path / "refuse.json"
    p.write_text(json.dumps({**REGISTRY, "mode": "refuse"}))
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal("terminal", {"command": "ufw allow 22"}, registry_path=p) is None
    payload = _events(conn, tid)[0].payload
    assert payload["registry_mode"] == "refuse" and payload["action"] == "allowed_report_only"


# ---------------------------------------------------------------------------
# Repair (Codex t_3cb4a9cb / t_150b247f): ports on generic *_PORT and
# bind-address; targets named only inside a patch payload are inspected.
# ---------------------------------------------------------------------------

_V4A_UNIT = "*** Begin Patch\n*** Update File: /home/chris/.config/systemd/user/app.service\n@@\n-Environment=PORT=8080\n+Environment=PORT=9090\n*** End Patch\n"
_UDIFF_SERVICE = "--- a/home/chris/services/app/.env\n+++ b/home/chris/services/app/.env\n@@ -1 +1 @@\n-APP_PORT=8000\n+APP_PORT=9000\n"


@pytest.mark.parametrize("tool,args,expected", [
    ("write_file", {"path": "/home/chris/.config/systemd/user/app.service", "content": "[Service]\nEnvironment=PORT=9000\n"}, ["ports", "service_units"]),
    ("write_file", {"path": "/home/chris/services/app/.env", "content": "APP_PORT=9000\n"}, ["ports"]),
    ("write_file", {"path": "/home/chris/services/db/my.cnf", "content": "[mysqld]\nbind-address = 0.0.0.0\n"}, ["ports"]),
    ("write_file", {"path": "/home/chris/services/app/.env", "content": "LISTEN_PORT=9000\n"}, ["ports"]),
    ("patch", {"mode": "patch", "patch": _V4A_UNIT}, ["ports", "service_units"]),
    ("patch", {"mode": "patch", "patch": _UDIFF_SERVICE}, ["ports"]),
])
def test_repair_ports_and_patch_payload_targets(tool, args, expected):
    assert _roles(tool, args) == expected


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "/home/chris/services/app/report.txt", "content": "REPORT=weekly\nIMPORT=csv\n"}),
    ("write_file", {"path": "/home/chris/scratch/notes.env", "content": "APP_PORT=9000\n"}),
    ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: /home/chris/scratch/x.py\n@@\n-a\n+b\n*** End Patch\n"}),
])
def test_repair_non_production_targets_still_do_not_match(tool, args):
    assert _roles(tool, args) == []
