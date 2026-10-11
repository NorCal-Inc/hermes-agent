"""Execution contract rule 6 (production authority), segment 6d: REFUSE mode
in the production-surface gate.

Pins: (a) refuse mode + unauthorized match -> refusal string, exactly one
production_action_refused event per matched role, payload and refusal text
carry no command text / path / content; (b) refuse mode + non-production
call -> None, no event; (c) report-only is unchanged (unauthorized event,
None, same payload shape as before); (d) refuse mode with no
HERMES_KANBAN_TASK -> refusal + one WARNING line, no event; (e) an unknown
mode, or a missing / unparseable registry, allows and records
production_action_unauthorized with the registry state in the payload;
(f) a gate exception never breaks the tool call, even in refuse mode;
(g) through the dispatcher a refused call never reaches tool dispatch.

The registry is always a temp file (``registry_path=`` or a monkeypatched
``REGISTRY_PATH``); the installed registry is never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import production_authority_gate as gate
from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.usefixtures("all_assignees_spawnable")


ROLES_BLOCK = {r: {"christopher_only": True, "profiles": []} for r in gate.ROLES}


def _registry(mode):
    return {
        "version": 1,
        "mode": mode,
        "christopher_authorized_executors": ["claude", "codex"],
        "roles": ROLES_BLOCK,
    }


@pytest.fixture
def refuse_registry(tmp_path, monkeypatch):
    p = tmp_path / "production-roles.json"
    p.write_text(json.dumps(_registry("refuse")), encoding="utf-8")
    monkeypatch.setattr(gate, "REGISTRY_PATH", p)
    return p


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, refuse_registry):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # Never resolve to the operator's live board.
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
    return kb.create_task(conn, title="prod gate refuse", assignee="w")


def _events(conn, task_id, kind):
    return [e for e in kb.list_events(conn, task_id) if e.kind == kind]


def _write_registry(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# (a) refuse mode + unauthorized match
# ---------------------------------------------------------------------------


def test_refuse_mode_returns_refusal_and_records_one_refused_event(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setenv(gate.ENV_RUN_ID, "11")
    secret_cmd = "sudo ufw allow from 203.0.113.9 to any port 5100 # token=SEKRET-VALUE"
    out = gate.production_authority_refusal("terminal", {"command": secret_cmd})
    assert isinstance(out, str)
    assert "firewall" in out
    assert "Claude Code" in out and "Codex" in out
    for leak in ("SEKRET", "203.0.113.9", "ufw allow", "5100"):
        assert leak not in out, leak
    refused = _events(conn, tid, gate.EVENT_REFUSED)
    assert len(refused) == 1
    assert _events(conn, tid, gate.EVENT_UNAUTHORIZED) == []
    ev = refused[0]
    payload = ev.payload
    assert payload["role"] == "firewall"
    assert payload["tool"] == "terminal"
    assert payload["matcher"] == "firewall.ufw"
    assert payload["executor_lane"] == gate.HERMES_NATIVE_EXECUTOR
    assert payload["authorized"] is False
    assert payload["registry_state"] == "ok"
    assert payload["registry_mode"] == "refuse"
    assert payload["action"] == "refused"
    assert ev.run_id == 11
    raw = json.dumps(payload)
    for leak in ("SEKRET", "203.0.113.9", "ufw allow", "5100", "command"):
        assert leak not in raw, leak


def test_refuse_mode_write_payload_and_text_have_no_path_or_content(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    out = gate.production_authority_refusal(
        "write_file",
        {"path": "/home/chris/services/secretapp/.env", "content": "PORT=5100\nAPI_KEY=hunter2\n"},
    )
    assert isinstance(out, str) and "ports" in out
    for leak in ("secretapp", "hunter2", "5100", "/home/chris"):
        assert leak not in out, leak
    refused = _events(conn, tid, gate.EVENT_REFUSED)
    assert len(refused) == 1
    raw = json.dumps(refused[0].payload)
    for leak in ("secretapp", "hunter2", "5100", "/home/chris"):
        assert leak not in raw, leak


def test_refuse_mode_records_one_refused_event_per_matched_role(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    out = gate.production_authority_refusal("write_file", {
        "path": "/etc/systemd/system/app.service",
        "content": "[Service]\nEnvironment=PORT=5100\nEnvironment=STRIPE=sk_live_x1\n",
    })
    assert isinstance(out, str)
    for role in ("ports", "service_units", "stripe_live"):
        assert role in out
    refused = _events(conn, tid, gate.EVENT_REFUSED)
    assert sorted(e.payload["role"] for e in refused) == ["ports", "service_units", "stripe_live"]
    assert _events(conn, tid, gate.EVENT_UNAUTHORIZED) == []
    assert "sk_live" not in out and "sk_live" not in json.dumps([e.payload for e in refused])


def test_refuse_mode_card_production_actions_still_refuse_hermes_native_worker(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    monkeypatch.setattr(gate, "_card_production_actions", lambda _tid: ("firewall",))
    out = gate.production_authority_refusal("terminal", {"command": "ufw allow 443"})
    assert isinstance(out, str)
    refused = _events(conn, tid, gate.EVENT_REFUSED)
    assert len(refused) == 1
    assert "not a christopher_authorized_executor" in refused[0].payload["reason"]


# ---------------------------------------------------------------------------
# (b) refuse mode + non-production call -> None, no event
# ---------------------------------------------------------------------------


def test_refuse_mode_non_production_call_is_allowed_without_event(conn, monkeypatch):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal("terminal", {"command": "ls -la"}) is None
    assert gate.production_authority_refusal("terminal", {"command": "ufw status verbose"}) is None
    assert gate.production_authority_refusal("terminal", {"command": "systemctl --user status x"}) is None
    assert gate.production_authority_refusal("terminal", {"command": "git push --dry-run production main"}) is None
    assert gate.production_authority_refusal("read_file", {"path": "/etc/systemd/system/x.service"}) is None
    assert gate.production_authority_refusal("write_file", {"path": "/tmp/notes.md", "content": "PORT=1\n"}) is None
    assert gate.production_authority_refusal("terminal", {}) is None
    assert gate.production_authority_refusal("terminal", None) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    assert _events(conn, tid, gate.EVENT_UNAUTHORIZED) == []


def test_refuse_mode_authorized_decision_records_nothing():
    registry = gate.RegistryState(
        state="ok", mode="refuse",
        authorized_executors=frozenset({"claude", "codex"}), roles=frozenset(gate.ROLES),
    )
    m = gate.SurfaceMatch("deploy", "deploy.script")
    ok = gate.decide(m, executor_lane="claude", task_id="t_1", registry=registry, card_actions=("deploy",))
    assert ok.authorized and ok.refuse
    no = gate.decide(m, executor_lane="hermes", task_id="t_1", registry=registry, card_actions=("deploy",))
    assert not no.authorized and no.refuse


# ---------------------------------------------------------------------------
# (c) report-only unchanged
# ---------------------------------------------------------------------------


def test_report_only_mode_is_unchanged(conn, monkeypatch, tmp_path):
    p = _write_registry(tmp_path, "report-only.json", _registry("report-only"))
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal(
        "terminal", {"command": "ufw allow 22"}, registry_path=p
    ) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    events = _events(conn, tid, gate.EVENT_UNAUTHORIZED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["action"] == "allowed_report_only"
    assert payload["registry_mode"] == "report-only"
    assert payload["registry_state"] == "ok"
    assert payload["authorized"] is False
    assert set(payload) == {
        "tool", "source", "role", "surface_role", "matcher", "executor_lane",
        "registry_state", "registry_mode", "authorized", "reason", "action",
    }


def test_report_only_no_card_still_logs_warning_only(monkeypatch, tmp_path, caplog, kanban_home):
    p = _write_registry(tmp_path, "report-only.json", _registry("report-only"))
    monkeypatch.delenv(gate.ENV_TASK, raising=False)
    with caplog.at_level("WARNING", logger=gate.__name__):
        assert gate.production_authority_refusal(
            "terminal", {"command": "systemctl --user restart NorCal_Hermes.service"}, registry_path=p
        ) is None
    msgs = [r.getMessage() for r in caplog.records if gate.EVENT_UNAUTHORIZED in r.getMessage()]
    assert len(msgs) == 1 and "(report-only)" in msgs[0]
    assert not any(gate.EVENT_REFUSED in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# (d) refuse mode with no card -> refusal + one WARNING, no event
# ---------------------------------------------------------------------------


def test_refuse_mode_no_card_refuses_and_logs_one_warning(conn, monkeypatch, caplog):
    monkeypatch.delenv(gate.ENV_TASK, raising=False)
    with caplog.at_level("WARNING", logger=gate.__name__):
        out = gate.production_authority_refusal(
            "terminal", {"command": "systemctl --user restart NorCal_Hermes.service"}
        )
    assert isinstance(out, str) and "service_units" in out and "NorCal_Hermes" not in out
    msgs = [r.getMessage() for r in caplog.records if gate.EVENT_REFUSED in r.getMessage()]
    assert len(msgs) == 1
    assert "service_units" in msgs[0] and "NorCal_Hermes" not in msgs[0]
    assert [r.levelname for r in caplog.records if gate.EVENT_REFUSED in r.getMessage()] == ["WARNING"]
    assert not any(gate.EVENT_UNAUTHORIZED in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# (e) unknown mode / missing / unparseable registry -> allow + record state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["REFUSE", "Refuse", " refuse", "refuse ", "enforce", "block", "", None, 1])
def test_other_modes_allow_and_record_unauthorized_with_registry_state(conn, monkeypatch, tmp_path, mode):
    p = _write_registry(tmp_path, "other.json", _registry(mode))
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal(
        "terminal", {"command": "git push production main"}, registry_path=p
    ) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    events = _events(conn, tid, gate.EVENT_UNAUTHORIZED)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["registry_state"] == "ok"
    assert payload["registry_mode"] == (str(mode) if mode else "report-only")
    assert payload["action"] == "allowed_report_only"


def test_missing_registry_allows_and_records_state(conn, monkeypatch, tmp_path):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    assert gate.production_authority_refusal(
        "terminal", {"command": "ufw allow 22"}, registry_path=tmp_path / "nope.json"
    ) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    payload = _events(conn, tid, gate.EVENT_UNAUTHORIZED)[0].payload
    assert payload["registry_state"] == "missing" and payload["role"] == "unknown"


def test_unparseable_registry_allows_and_records_state(conn, monkeypatch, tmp_path):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    bad = _write_registry(tmp_path, "bad.json", '{"mode": "refuse", not json')
    assert gate.production_authority_refusal(
        "terminal", {"command": "ufw allow 22"}, registry_path=bad
    ) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    payload = _events(conn, tid, gate.EVENT_UNAUTHORIZED)[0].payload
    assert payload["registry_state"] == "unparseable" and payload["role"] == "unknown"


def test_invalid_registry_with_refuse_mode_allows_and_records_state(conn, monkeypatch, tmp_path):
    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    bad = _write_registry(tmp_path, "invalid.json", {"mode": "refuse", "roles": "not-a-dict"})
    assert gate.production_authority_refusal(
        "terminal", {"command": "ufw allow 22"}, registry_path=bad
    ) is None
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
    payload = _events(conn, tid, gate.EVENT_UNAUTHORIZED)[0].payload
    assert payload["registry_state"] == "invalid" and payload["role"] == "unknown"


# ---------------------------------------------------------------------------
# (f) gate exception never breaks the tool call; (g) dispatcher integration
# ---------------------------------------------------------------------------


def _stub_contract_gate(monkeypatch):
    from agent import task_contract_gate

    monkeypatch.setattr(task_contract_gate, "task_contract_refusal", lambda *_a, **_k: None)


def test_gate_exception_in_refuse_mode_never_breaks_tool_call(conn, monkeypatch):
    import model_tools

    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    _stub_contract_gate(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("registry read blew up")

    monkeypatch.setattr(gate, "load_registry", boom)
    monkeypatch.setattr(model_tools.registry, "dispatch", lambda name, args, **kw: json.dumps({"output": "stubbed"}))
    out = model_tools.handle_function_call("terminal", {"command": "ufw allow 443"})
    assert json.loads(out) == {"output": "stubbed"}
    assert _events(conn, tid, gate.EVENT_REFUSED) == []


def test_refuse_mode_through_dispatcher_blocks_tool_dispatch(conn, monkeypatch):
    """End to end: in refuse mode a firewall-shaped command from a worker
    card is turned into a tool error by the dispatcher and never reaches
    registry dispatch."""
    import model_tools

    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    _stub_contract_gate(monkeypatch)
    dispatched = []

    def fake_dispatch(name, args, **kw):
        dispatched.append(name)
        return json.dumps({"output": "stubbed"})

    monkeypatch.setattr(model_tools.registry, "dispatch", fake_dispatch)
    out = model_tools.handle_function_call("terminal", {"command": "echo && ufw allow 443 # SEKRET"})
    assert dispatched == []
    result = json.loads(out)
    assert "error" in result
    assert "firewall" in result["error"] and "SEKRET" not in out
    assert len(_events(conn, tid, gate.EVENT_REFUSED)) == 1


def test_refuse_mode_through_dispatcher_passes_non_production_call(conn, monkeypatch):
    import model_tools

    tid = _task(conn)
    monkeypatch.setenv(gate.ENV_TASK, tid)
    _stub_contract_gate(monkeypatch)
    dispatched = []

    def fake_dispatch(name, args, **kw):
        dispatched.append(name)
        return json.dumps({"output": "stubbed"})

    monkeypatch.setattr(model_tools.registry, "dispatch", fake_dispatch)
    out = model_tools.handle_function_call("terminal", {"command": "ufw status"})
    assert json.loads(out) == {"output": "stubbed"}
    assert dispatched == ["terminal"]
    assert _events(conn, tid, gate.EVENT_REFUSED) == []
