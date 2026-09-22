#!/usr/bin/env python3
"""Boot parity verifier.

Asserts the boot plumbing itself: that the deployed entry points still resolve
into the canonical boot dir, that every boot-state parser is exact-line
anchored, that Erika's two session paths judge boot through the shared
chokepoint rather than an exit code, and that the live attempt ceiling /
admission control / selective-enforcement classifier are present and decoupled.

Two classes of assertion live here, and the distinction is what makes the file
callable from a test at all:

``repo_errors``
    Deterministic checks against files tracked in this repository. Portable, no
    host state, safe in CI.
``host_errors`` / ``generator_errors``
    Deployment parity: symlinks under ~/.local/bin, the Codex configuration,
    the live runtime config, and one real execution of the shared generator.
    These describe *this host* and cannot pass on a clean CI checkout.

Modes::

    verify.py               repo + host + generator (full deployment parity)
    verify.py --repo-only   repo-tracked assertions only (portable, CI-safe)
    verify.py --host-only    host filesystem parity only, no generator subprocess

The output contract is stable across modes: an exact ``BOOT PARITY: PASS`` or
``BOOT PARITY: FAIL`` line, a ``constraints_sha256=`` line, one ``ERROR:`` line
per failure, and exit status 1 on any failure.

This verifier is invoked by ``tests/norcal/test_boot_parity_verifier.py``. It is
deliberately NOT a term in the SessionStart gate's ``complete`` decision: it
executes the shared boot generator, so gating boot on it made boot completion
depend on a second verification of the same boot, and a parity failure could
then deny the very recovery session authorized to repair it.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HOME = Path.home()

CONSTRAINTS = (HERE / "boot-constraints.md").read_text(encoding="utf-8").strip()

DEPLOYED_ENTRY_POINTS = (
    ".local/bin/hermes-shared-boot-context",
    ".local/bin/boot-context",
    ".local/bin/claude-session-start-gate.py",
    ".local/bin/codex",
)


def _read(path: Path) -> str:
    """Return file text, or '' when absent.

    Absence is reported as a named assertion failure by the caller rather than
    as a traceback. The pre-2026-09-21 version read the Codex and runtime
    configs unguarded at module scope, so the whole verifier crashed instead of
    reporting which file was missing.
    """
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def repo_errors(repo: Path = REPO, here: Path = HERE) -> list[str]:
    """Assertions about files tracked in this repository."""
    errs: list[str] = []

    # Selective enforcement (2026-09-07). Board-wide `gauntlet_enforcement: true`
    # is no longer the contract: investigations and diagnostics are deliberately
    # ungoverned, because governing them turned every non-converging question
    # into a verifier chain (t_0ce21cbe produced 59 verifier cards). What must
    # hold instead is stricter than the flag ever was -- the flag guaranteed
    # verification, never brakes.
    kdb = _read(repo / "hermes_cli/kanban_db.py")
    if "_objective_lineage_members" not in kdb:
        errs.append(
            "universal objective attempt ceiling is missing: ungoverned work would retry without bound"
        )
    if "if _gauntlet_objective_scoped(" in kdb:
        errs.append(
            "attempt ceiling is still coupled to Gauntlet enforcement; exempting work from verification would exempt it from having brakes"
        )
    if "ControlPlaneAdmissionError" not in kdb:
        errs.append(
            "control-plane admission control is missing: automation could reopen Gauntlet construction unsupervised"
        )
    if "gauntlet_default_for_subject" not in kdb:
        errs.append(
            "selective enforcement classifier is missing: deliverables would inherit the board default instead of being governed on their own merits"
        )

    for name, rel in (
        ("Claude SessionStart", "claude-session-start-gate.py"),
        ("Claude boot wrapper", "claude-boot-context"),
        ("Codex boot wrapper", "codex-boot"),
    ):
        if "shared_boot_complete" not in _read(here / rel):
            errs.append(f"{name} does not use the canonical exact shared boot-state parser")

    if "^BOOT STATUS: COMPLETE\\s*$" not in _read(here / "recovery_boot.py"):
        errs.append("canonical shared boot-state parser is not exact-line anchored")

    # Hermes/Erika parity. The two Erika session-creation paths must build their
    # fresh-session prompt through the shared chokepoint, which reuses the same
    # exact-line parser above. They previously judged boot success by exit code
    # alone, and the generator exits 0 on a degraded boot unless
    # --gate-exit-code is passed, so Erika alone failed open.
    if "shared_boot_complete" not in _read(repo / "hermes_cli/norcal_boot.py"):
        errs.append(
            "Hermes session boot chokepoint does not use the canonical exact shared boot-state parser"
        )
    for name, rel in (("Hermes interactive CLI", "cli.py"), ("Hermes gateway", "gateway/run.py")):
        src = _read(repo / rel)
        if "build_session_boot_prompt" not in src:
            errs.append(
                f"{name} does not build its fresh-session prompt through the shared boot chokepoint"
            )
        if "_shared_boot_proc.returncode != 0" in src:
            errs.append(f"{name} still treats a zero generator exit code as a passed boot gate")

    # Boot-gate recovery must stay reachable. Doctrine constraint 15 requires the
    # recovery executor not to depend on the failed gate being green, and the
    # boot wrapper exits non-zero on every DEGRADED boot -- so a returncode term
    # in ``recovery_only`` makes recovery unreachable exactly when it is needed.
    gate_src = _read(here / "claude-session-start-gate.py")
    if "recovery_only" not in gate_src:
        errs.append("SessionStart gate exposes no recovery-only authority path")
    if "boot_wrapper_ran" not in gate_src:
        errs.append(
            "SessionStart gate does not distinguish a real boot payload from the exception fallback; "
            "recovery authority would key off the failed gate's exit status"
        )

    return errs


def host_errors(home: Path = HOME, here: Path = HERE) -> list[str]:
    """Deployment parity for this host's filesystem. No subprocesses."""
    errs: list[str] = []

    for rel in DEPLOYED_ENTRY_POINTS:
        p = home / rel
        if not p.is_symlink() or p.resolve().parent != here:
            errs.append(f"{p}: not linked to canonical boot dir")

    agents = _read(home / ".codex/AGENTS.md")
    if not agents.startswith(CONSTRAINTS + "\n"):
        errs.append("Codex AGENTS.md does not start with canonical constraints")

    cfg = _read(home / ".codex/config.toml")
    if 'model_instructions_file = "/home/chris/.codex/norcal-boot-current.md"' not in cfg:
        errs.append("Codex model_instructions_file not configured")

    runtime = _read(home / ".hermes/config.yaml")
    if "gauntlet_objective_attempt_limit" not in runtime:
        errs.append("North Caledonia live config sets no kanban.gauntlet_objective_attempt_limit")

    return errs


def generator_errors(here: Path = HERE) -> list[str]:
    """One real execution of the shared generator under its gate exit code."""
    errs: list[str] = []
    try:
        p = subprocess.run(
            [str(here / "hermes-shared-boot-context"), "--gate-exit-code"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return [f"shared generator did not run: {type(exc).__name__}: {exc}"]
    body = (p.stdout or p.stderr or "").strip()
    if p.returncode != 0:
        errs.append(f"shared generator gate rc={p.returncode}")
    if not body.startswith(CONSTRAINTS):
        errs.append("shared generator does not start with canonical constraints")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--repo-only",
        action="store_true",
        help="run only the repo-tracked assertions (portable, CI-safe)",
    )
    mode.add_argument(
        "--host-only",
        action="store_true",
        help="run only host filesystem deployment parity (no generator subprocess)",
    )
    args = ap.parse_args(argv)

    if args.repo_only:
        errs = repo_errors()
    elif args.host_only:
        errs = host_errors()
    else:
        errs = repo_errors() + host_errors() + generator_errors()

    print("BOOT PARITY: PASS" if not errs else "BOOT PARITY: FAIL")
    print("constraints_sha256=" + hashlib.sha256((CONSTRAINTS + "\n").encode()).hexdigest())
    for e in errs:
        print("ERROR:", e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
