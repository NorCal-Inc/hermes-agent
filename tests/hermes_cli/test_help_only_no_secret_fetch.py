"""Regression coverage for t_67d782d6.

Reported symptom: a plain help-only invocation such as
``hermes kanban --help`` performed 4 live 1Password reads before this fix,
because ``hermes_cli/main.py`` unconditionally called
``load_hermes_dotenv(load_external_secrets=True)`` at CLI startup — before
argparse even sees the ``--help`` flag and prints usage/exits. Any
subcommand depth (``hermes --help``, ``hermes kanban --help``,
``hermes kanban show --help``, ...) triggered the same eager fetch.

This test is offline/static only: it never shells out to a real `op`
binary or network. It subprocess-imports ``hermes_cli.main`` with a
config.yaml that has 1Password enabled and asserts the external
secret-source apply path is never reached for help-only argv, while
confirming it's still reached for an ordinary (non-help) invocation —
so the fix doesn't silently disable secret loading altogether.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import _startup_fast

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Unit coverage for the new, conservative argv classifier.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["-h"],
        ["kanban", "--help"],
        ["kanban", "show", "--help"],
        ["gateway", "status", "-h"],
    ],
)
def test_is_help_only_argv_true_for_trailing_help_flag(argv):
    assert _startup_fast.is_help_only_argv(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["kanban"],
        ["kanban", "show"],
        ["chat"],
        ["update"],
        # --help is not the LAST token: a real subcommand follows it, so
        # argparse would NOT exit on help alone.
        ["--help", "kanban"],
        # A value that merely contains "--help"-like text but isn't the
        # trailing token / isn't a bare flag must not misfire.
        ["kanban", "comment", "--help-wanted"],
    ],
)
def test_is_help_only_argv_false_otherwise(argv):
    assert _startup_fast.is_help_only_argv(argv) is False


# ---------------------------------------------------------------------------
# End-to-end: subprocess-import main.py with 1Password enabled and assert
# the external-secret apply path is (or isn't) reached, by monkeypatching
# the one chokepoint (`agent.secret_sources.onepassword.find_op`) that would
# otherwise shell out to a real `op` binary — so this stays fully offline.
# ---------------------------------------------------------------------------


def _run_probe(tmp_path: Path, argv: list[str]) -> dict:
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        """\
secrets:
  onepassword:
    enabled: true
    env:
      SOME_TEST_API_KEY: "op://vault/item/field"
""",
        encoding="utf-8",
    )

    probe = f"""
import json, sys
sys.argv = {argv!r}

calls = {{"find_op": 0, "op_read": 0}}

# Patch the exact chokepoint that would shell out to a real `op` binary,
# BEFORE hermes_cli.main runs any startup code, so this test can never
# perform a live 1Password call regardless of fix correctness.
import agent.secret_sources.onepassword as _opmod

def _fake_find_op(binary_path=""):
    calls["find_op"] += 1
    return None  # pretend `op` is absent; a real call would still count above

_opmod.find_op = _fake_find_op

import hermes_cli.main as hermes_main
hermes_main.cmd_update = lambda _args: 0
try:
    hermes_main.main()
except SystemExit:
    pass

print("PROBE_RESULT=" + json.dumps(calls))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO_ROOT,
        env={**os.environ, "HERMES_HOME": str(home)},
    )
    assert result.returncode == 0, (
        f"probe failed rc={result.returncode}\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    line = next(
        line for line in result.stdout.splitlines() if line.startswith("PROBE_RESULT=")
    )
    return json.loads(line.removeprefix("PROBE_RESULT="))


def test_help_only_invocation_never_touches_onepassword(tmp_path):
    calls = _run_probe(tmp_path, ["hermes", "kanban", "--help"])
    assert calls["find_op"] == 0, (
        "hermes kanban --help must not attempt a 1Password fetch "
        f"(find_op called {calls['find_op']} times)"
    )


def test_bare_help_invocation_never_touches_onepassword(tmp_path):
    calls = _run_probe(tmp_path, ["hermes", "--help"])
    assert calls["find_op"] == 0


def test_normal_subcommand_still_resolves_onepassword(tmp_path):
    """Guard against an over-broad fix that disables secret loading outright."""
    calls = _run_probe(tmp_path, ["hermes", "kanban", "list"])
    assert calls["find_op"] >= 1, (
        "a normal (non-help) invocation must still attempt 1Password "
        "resolution so the fix doesn't regress real credential loading"
    )
