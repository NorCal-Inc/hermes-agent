"""North Caledonia fresh-session startup invariant for Hermes/Erika sessions.

Every fresh Erika/Hermes agent session must mechanically run the canonical shared
boot generator, evaluate its required gates with the *same* exact-line parser the
Claude Code and GPT/Codex entry points use, and carry a non-optional startup
directive into the session prompt. None of that may depend on the operator
saying "boot".

Before this module existed, ``cli.py`` and ``gateway/run.py`` each carried their
own copy of the injection block and judged boot success by
``returncode != 0 or not stdout``. The canonical generator only exits non-zero
when it is invoked with ``--gate-exit-code``; a normal invocation exits 0 even
when it emits ``BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION``. Both Erika
paths therefore treated a degraded boot as a successful one and left the stop
decision entirely to model discretion, while Claude/Codex failed closed on the
same payload. This module is the single shared chokepoint that closes that gap.

Role-neutral by design: it makes no authority, doctrine, company-scope, or task
decision. It runs the canonical generator, classifies the result, and emits the
prompt text.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from typing import Callable, NamedTuple

# Deployed canonical entry point. It is a symlink to norcal/boot/hermes-shared-boot-context
# and is the same path the Claude and Codex wrappers consume.
CANONICAL_GENERATOR = "/home/chris/.local/bin/hermes-shared-boot-context"

# The deterministic gate command used by the claude_recovery lane. Named here so
# the degraded directive tells the session exactly what to file, with no guessing.
RECOVERY_GATE_CMD = f"{CANONICAL_GENERATOR} --gate-exit-code"

BOOT_TIMEOUT_SECONDS = 45

# Canonical exact-line parser. Single source of truth, shared with the Claude
# SessionStart gate and the Claude/Codex boot wrappers; never re-implement the
# regex here. norcal/ is not an installed package, so it is loaded by path.
_PARSER_PATH = Path(__file__).resolve().parents[1] / "norcal" / "boot" / "recovery_boot.py"


class SessionBoot(NamedTuple):
    """Result of the fresh-session boot for one Erika/Hermes agent."""

    prompt: str
    complete: bool
    failure: str
    returncode: "int | None"


def _load_shared_boot_complete() -> Callable[[str], bool]:
    """Return the canonical ``shared_boot_complete``; fail closed if unloadable."""
    try:
        spec = importlib.util.spec_from_file_location(
            "norcal_recovery_boot_runtime", _PARSER_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.shared_boot_complete
    except Exception:
        # No canonical parser means the gate cannot be evaluated, and an
        # unevaluated gate is a failed gate.
        return lambda _text: False


def startup_directive(complete: bool, failure: str = "") -> str:
    """Mandatory fresh-session directive appended after the canonical payload.

    Deliberately never emits a bare ``BOOT STATUS: COMPLETE`` line: that exact
    line is the machine gate for every downstream consumer, and a directive that
    printed it would let this wrapper spoof the gate it is enforcing.
    """
    if complete:
        return (
            "<norcal-session-startup>\n"
            "FRESH SESSION STARTUP — MANDATORY AND NOT OPERATOR-TRIGGERED.\n"
            "SHARED BOOT GATE: COMPLETE\n"
            "The canonical boot generator already ran for this session and every required "
            "gate above has been evaluated. Its verbatim output precedes this block.\n"
            "Before answering, routing, delegating, or acting on any Kanban task in this "
            "session, execute the canonical five-step startup protocol against that payload: "
            "(1) confirm boot state, boot time, doctrine version, source fingerprint, runtime "
            "version, and required-chain failures; (2) apply the injected doctrine set in "
            "precedence order; (3) load the shared vault continuity and CURRENT KANBAN "
            "CONTINUITY as the first source of truth for current-task state; (4) read the gate "
            "results already included above rather than repeating them; (5) surface the state.\n"
            "Do not wait to be told to boot. Do not re-run the generator; its output is "
            "already present.\n"
            "</norcal-session-startup>"
        )
    return (
        "<norcal-session-startup>\n"
        "FRESH SESSION STARTUP — MANDATORY AND NOT OPERATOR-TRIGGERED.\n"
        "SHARED BOOT GATE: DEGRADED — STOP BEFORE TASK EXECUTION\n"
        f"FAILURE: {failure or 'shared boot gate did not report a complete boot state'}\n"
        "Ordinary task execution is blocked. Do not act on, claim, advance, verify, or "
        "complete any Kanban task, and do not execute the operator's original request "
        "through the failed gate. Do not ask whether to repair it.\n"
        "Park the original request and use the Jarvis recovery override: create exactly one "
        'recovery task with assignee="default", executor_lane="claude_recovery", and '
        f'recovery_gate_cmd="{RECOVERY_GATE_CMD}", with the original request as its '
        "dependent continuation. Do not diagnose the gate first and do not poll.\n"
        "Ordinary execution resumes only once a fresh session reports SHARED BOOT GATE: "
        "COMPLETE.\n"
        "</norcal-session-startup>"
    )


def build_session_boot_prompt(
    runner: "Callable[..., object] | None" = None,
    generator: str = CANONICAL_GENERATOR,
    timeout: int = BOOT_TIMEOUT_SECONDS,
) -> SessionBoot:
    """Run the canonical boot generator for a fresh session and classify it.

    ``runner`` is injectable so regression tests can drive every branch without
    executing the live generator. Production always uses ``subprocess.run``.
    """
    run = runner or subprocess.run
    failure = ""
    payload = ""
    returncode: "int | None" = None

    try:
        proc = run(
            [generator],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        returncode = getattr(proc, "returncode", None)
        payload = (
            (getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or "")
        ).strip()
    except Exception as exc:
        failure = f"shared boot generator exception: {type(exc).__name__}: {exc}"

    if not failure:
        if returncode != 0:
            failure = f"shared boot generator failed rc={returncode}"
        elif not payload:
            failure = "shared boot generator produced no output"

    complete = False
    if not failure:
        # The generator exits 0 on a degraded boot unless --gate-exit-code is
        # passed, so a zero exit is necessary but never sufficient. The exact
        # canonical state line is the gate.
        complete = bool(_load_shared_boot_complete()(payload))
        if not complete:
            failure = (
                "shared boot payload does not carry the canonical "
                "complete boot-state line (degraded or unparseable boot)"
            )

    directive = startup_directive(complete, failure)
    prompt = "\n\n".join(part for part in (payload, directive) if part)
    return SessionBoot(
        prompt=prompt, complete=complete, failure=failure, returncode=returncode
    )
