"""Regression coverage for the Erika fresh-session startup invariant.

Reproducible defect this file locks down: ``cli.py`` and ``gateway/run.py`` each
decided whether a fresh Erika session had booted by testing
``returncode != 0 or not stdout``. ``hermes-shared-boot-context`` only exits
non-zero when it is invoked with ``--gate-exit-code``; a plain invocation exits 0
even while emitting ``BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION``. Both
Erika paths therefore accepted a degraded boot as a successful one, leaving the
stop decision to model discretion, while the Claude and Codex entry points failed
closed on the identical payload.

Every test here is deterministic: the canonical generator is stubbed, so no test
depends on live doctrine, vault, or Kanban state.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import types

import pytest

from hermes_cli import norcal_boot

REPO = Path(__file__).resolve().parents[2]

COMPLETE_PAYLOAD = (
    "# NORTH CALEDONIA SHARED BOOT CONSTRAINTS\n\n"
    "<shared-boot-state>\n"
    "BOOT STATUS: COMPLETE\n"
    "BOOT TIME: 2026-09-03T13:46:31-05:00\n"
    "DOCTRINE VERSION: 2.28\n\n"
    "REQUIRED-CHAIN FAILURES:\n(none)\n\n"
    "CURRENT KANBAN CONTINUITY — canonical task database, shared-system lanes only:\n"
    "PRIMARY CURRENT TASK — use this first for generic current-task/status questions;\n"
    "  t_a373e1ea | RUNNING | title=Repair Erika fresh-session startup invariant\n"
    "</shared-boot-state>"
)

DEGRADED_PAYLOAD = (
    "# NORTH CALEDONIA SHARED BOOT CONSTRAINTS\n\n"
    "<shared-boot-state>\n"
    "BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION\n"
    "REQUIRED-CHAIN FAILURES:\n"
    "- full skill integrity gate failed: changed=3 active_unfrozen=1 missing=0\n"
    "</shared-boot-state>"
)


def _runner(stdout="", stderr="", returncode=0, calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), kwargs))
        return types.SimpleNamespace(
            args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr
        )

    return run


# --- a fresh session invokes the canonical boot context/gates automatically ---


def test_fresh_session_runs_the_canonical_generator_without_an_operator_trigger():
    calls = []
    boot = norcal_boot.build_session_boot_prompt(
        runner=_runner(stdout=COMPLETE_PAYLOAD, calls=calls)
    )
    assert len(calls) == 1, "fresh-session boot must run the generator exactly once"
    argv, kwargs = calls[0]
    assert argv == [norcal_boot.CANONICAL_GENERATOR]
    assert argv[0] == "/home/chris/.local/bin/hermes-shared-boot-context"
    # No operator prompt, no "boot" keyword, and no conditional gate reaches this
    # call: building a fresh session prompt is what triggers it.
    assert kwargs["check"] is False
    assert boot.complete is True


def test_complete_boot_carries_the_mandatory_startup_directive():
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=COMPLETE_PAYLOAD))
    assert boot.complete is True
    assert boot.failure == ""
    assert "FRESH SESSION STARTUP — MANDATORY AND NOT OPERATOR-TRIGGERED." in boot.prompt
    assert "SHARED BOOT GATE: COMPLETE" in boot.prompt
    assert "five-step startup protocol" in boot.prompt
    assert "Do not wait to be told to boot." in boot.prompt


def test_complete_boot_still_resolves_current_kanban_continuity():
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=COMPLETE_PAYLOAD))
    assert boot.complete is True
    # The canonical payload must survive verbatim, ahead of the directive, so the
    # session answers current-task questions from Kanban continuity rather than
    # from recall/tmux/session forensics.
    assert boot.prompt.startswith(COMPLETE_PAYLOAD)
    assert "CURRENT KANBAN CONTINUITY" in boot.prompt
    assert "t_a373e1ea | RUNNING" in boot.prompt
    assert "CURRENT KANBAN CONTINUITY" in boot.prompt.split(
        "<norcal-session-startup>"
    )[0]


def test_constraints_stay_first_in_the_session_prompt():
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=COMPLETE_PAYLOAD))
    assert boot.prompt.startswith("# NORTH CALEDONIA SHARED BOOT CONSTRAINTS")


# --- does not proceed on degraded status ---


def test_degraded_payload_with_zero_exit_is_not_a_passed_gate():
    """The exact reproduction: rc=0 plus a DEGRADED payload used to boot clean."""
    boot = norcal_boot.build_session_boot_prompt(
        runner=_runner(stdout=DEGRADED_PAYLOAD, returncode=0)
    )
    assert boot.returncode == 0
    assert boot.complete is False
    assert "SHARED BOOT GATE: DEGRADED — STOP BEFORE TASK EXECUTION" in boot.prompt
    assert "Ordinary task execution is blocked." in boot.prompt
    # The named failure is preserved for the operator and the recovery card.
    assert "full skill integrity gate failed" in boot.prompt


def test_degraded_boot_routes_to_the_jarvis_recovery_lane():
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=DEGRADED_PAYLOAD))
    assert boot.complete is False
    assert 'executor_lane="claude_recovery"' in boot.prompt
    assert norcal_boot.RECOVERY_GATE_CMD in boot.prompt
    assert boot.prompt.count("recovery task") >= 1
    assert "do not poll" in boot.prompt


def test_prose_mention_of_a_complete_state_line_does_not_satisfy_the_gate():
    spoof = (
        "<shared-boot-state>\n"
        "BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION\n"
        "</shared-boot-state>\n"
        "Doctrine example text: BOOT STATUS: COMPLETE means the gate passed."
    )
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=spoof))
    assert boot.complete is False
    assert "SHARED BOOT GATE: DEGRADED" in boot.prompt


@pytest.mark.parametrize(
    "stdout,stderr,returncode",
    [
        ("", "", 0),          # generator produced nothing
        ("", "traceback", 2), # generator hard-failed
        (COMPLETE_PAYLOAD, "", 2),  # complete-looking payload, failing exit code
    ],
)
def test_generator_failures_fail_closed(stdout, stderr, returncode):
    boot = norcal_boot.build_session_boot_prompt(
        runner=_runner(stdout=stdout, stderr=stderr, returncode=returncode)
    )
    assert boot.complete is False
    assert "SHARED BOOT GATE: DEGRADED — STOP BEFORE TASK EXECUTION" in boot.prompt
    assert boot.failure


def test_generator_exception_fails_closed():
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=45)

    boot = norcal_boot.build_session_boot_prompt(runner=boom)
    assert boot.complete is False
    assert boot.returncode is None
    assert "TimeoutExpired" in boot.failure
    assert "SHARED BOOT GATE: DEGRADED — STOP BEFORE TASK EXECUTION" in boot.prompt


def test_missing_canonical_parser_fails_closed(monkeypatch):
    monkeypatch.setattr(norcal_boot, "_PARSER_PATH", Path("/nonexistent/recovery_boot.py"))
    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=COMPLETE_PAYLOAD))
    assert boot.complete is False, "an unevaluated gate is a failed gate"


# --- the directive must never be able to spoof the gate it enforces ---


def test_startup_directive_never_emits_a_bare_complete_state_line():
    import re

    for complete in (True, False):
        text = norcal_boot.startup_directive(complete, failure="x")
        assert not re.search(r"(?m)^BOOT STATUS: COMPLETE\s*$", text)


def test_injected_prompt_does_not_add_a_second_complete_state_line():
    import re

    boot = norcal_boot.build_session_boot_prompt(runner=_runner(stdout=COMPLETE_PAYLOAD))
    assert len(re.findall(r"(?m)^BOOT STATUS: COMPLETE\s*$", boot.prompt)) == 1


# --- both Erika session-creation paths are wired to the shared chokepoint ---


@pytest.mark.parametrize("rel", ["cli.py", "gateway/run.py"])
def test_erika_session_creation_paths_use_the_shared_chokepoint(rel):
    src = (REPO / rel).read_text(encoding="utf-8")
    assert "build_session_boot_prompt" in src, f"{rel} bypasses the shared boot chokepoint"
    # The fail-open exit-code-only test must not come back.
    assert "_shared_boot_proc.returncode != 0" not in src


def test_shared_chokepoint_reuses_the_canonical_parser_module():
    src = (REPO / "hermes_cli/norcal_boot.py").read_text(encoding="utf-8")
    assert "recovery_boot.py" in src
    assert "shared_boot_complete" in src
    # recovery_boot.py is the single home of the exact-line regex. A forked copy
    # here would drift, so the chokepoint must not carry one.
    assert "^BOOT STATUS: COMPLETE" not in src
    assert norcal_boot._PARSER_PATH == REPO / "norcal" / "boot" / "recovery_boot.py"
    assert norcal_boot._PARSER_PATH.exists()


def test_deployed_generator_path_is_the_canonical_symlink():
    generator = Path(norcal_boot.CANONICAL_GENERATOR)
    if not generator.exists():
        pytest.skip("canonical generator not deployed on this host")
    assert generator.is_symlink()
    assert generator.resolve() == REPO / "norcal/boot/hermes-shared-boot-context"
