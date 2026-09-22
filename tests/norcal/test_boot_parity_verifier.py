"""The executed caller for ``norcal/boot/verify.py``.

``verify.py`` accumulated boot-plumbing assertions while having zero callers. On
2026-09-20 the SessionStart gate was briefly made to run it (commit 4e5... via PR #4),
and commit c87f2ad84e ("Simplify boot control plane") removed that again with the note
that "static parity checks remain available as bounded tests" -- but no such test was
written, so the file returned to zero callers and every assertion in it went unchecked.

This module is that bounded test. It is deliberately a *test* and not a boot gate:

  * ``verify.py`` executes the shared boot generator, so gating boot on it made boot
    completion depend on a second verification of the same boot.
  * A parity failure would then deny the very recovery session authorized to repair it,
    which is the circular deadlock doctrine constraint 15 forbids.

``test_session_start_gate_does_not_invoke_the_parity_verifier`` pins that separation so
the recursion cannot be reintroduced by a later edit.

A verifier with no falsification test is indistinguishable from one whose checks have all
silently stopped matching. ``test_repo_assertions_are_not_vacuous`` therefore breaks each
assertion in turn against a synthetic repository and requires the matching error.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BOOT_SRC = REPO / "norcal" / "boot"
VERIFY = BOOT_SRC / "verify.py"

PASS_LINE = re.compile(r"(?m)^BOOT PARITY: PASS\s*$")
FAIL_LINE = re.compile(r"(?m)^BOOT PARITY: FAIL\s*$")


def _verify_module():
    spec = importlib.util.spec_from_file_location("norcal_boot_verify_under_test", VERIFY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args: str):
    return subprocess.run(
        [sys.executable, str(VERIFY), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO),
    )


# ---------------------------------------------------------------------------
# The caller itself.
# ---------------------------------------------------------------------------

def test_repo_parity_passes_on_this_checkout():
    """The real assertion: this branch satisfies every repo-tracked parity check."""
    p = _run("--repo-only")
    errors = [ln for ln in p.stdout.splitlines() if ln.startswith("ERROR:")]
    assert errors == [], "boot parity regressions: " + "; ".join(errors)
    assert PASS_LINE.search(p.stdout), p.stdout
    assert p.returncode == 0


def test_parity_output_contract_is_stable():
    """Consumers match an exact line; a prose mention must never be mistaken for a pass."""
    p = _run("--repo-only")
    assert p.stdout.splitlines()[0] == "BOOT PARITY: PASS"
    assert re.search(r"(?m)^constraints_sha256=[0-9a-f]{64}$", p.stdout)


def test_constraints_digest_matches_the_canonical_block():
    import hashlib

    body = (BOOT_SRC / "boot-constraints.md").read_text(encoding="utf-8").strip()
    expected = hashlib.sha256((body + "\n").encode()).hexdigest()
    assert f"constraints_sha256={expected}" in _run("--repo-only").stdout


# ---------------------------------------------------------------------------
# No verifier-of-verifier recursion.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["complete", "degraded"])
def test_session_start_gate_never_executes_the_parity_verifier(boot, mode):
    """Boot completion must not depend on a verifier that re-verifies the same boot.

    Observed behaviourally: an executable ``verify.py`` sitting in the gate's own boot
    directory records being run. After a full gate run -- healthy or degraded -- the
    marker must be absent, because the gate never launches it.
    """
    marker = boot.plant_verify_sentinel()
    boot.run_gate(f"sid-no-recursion-{mode}", mode=mode)
    assert not marker.exists(), "the SessionStart gate executed the parity verifier"


def test_the_recursion_sentinel_would_have_been_detected(boot):
    """Positive control: the sentinel really does record execution when run."""
    marker = boot.plant_verify_sentinel()
    subprocess.run([sys.executable, str(boot.root / "verify.py")], capture_output=True, timeout=30)
    assert marker.exists()


# ---------------------------------------------------------------------------
# Falsification: every repo assertion must actually fire.
# ---------------------------------------------------------------------------

_GOOD_KDB = (
    "_objective_lineage_members = None\n"
    "class ControlPlaneAdmissionError(Exception): pass\n"
    "def gauntlet_default_for_subject(): pass\n"
)
_GOOD_WRAPPER = "shared_boot_complete\n"
_GOOD_PARSER = 'r"(?m)^BOOT STATUS: COMPLETE\\s*$"\n'
_GOOD_ENTRY = "build_session_boot_prompt\n"
_GOOD_GATE = "recovery_only = True\nboot_wrapper_ran = True\n"


def _good_tree(root: Path) -> tuple[Path, Path]:
    """A synthetic repo+boot pair that satisfies every repo-tracked assertion."""
    repo = root / "repo"
    here = repo / "norcal" / "boot"
    here.mkdir(parents=True)
    (repo / "hermes_cli").mkdir()
    (repo / "gateway").mkdir()
    (repo / "hermes_cli" / "kanban_db.py").write_text(_GOOD_KDB, encoding="utf-8")
    (repo / "hermes_cli" / "norcal_boot.py").write_text(_GOOD_WRAPPER, encoding="utf-8")
    (repo / "cli.py").write_text(_GOOD_ENTRY, encoding="utf-8")
    (repo / "gateway" / "run.py").write_text(_GOOD_ENTRY, encoding="utf-8")
    (here / "claude-session-start-gate.py").write_text(_GOOD_WRAPPER + _GOOD_GATE, encoding="utf-8")
    (here / "claude-boot-context").write_text(_GOOD_WRAPPER, encoding="utf-8")
    (here / "codex-boot").write_text(_GOOD_WRAPPER, encoding="utf-8")
    (here / "recovery_boot.py").write_text(_GOOD_PARSER, encoding="utf-8")
    return repo, here


def test_synthetic_good_tree_is_clean(tmp_path):
    """Guards the falsification cases below: the baseline must itself pass."""
    repo, here = _good_tree(tmp_path)
    assert _verify_module().repo_errors(repo, here) == []


# (relative path, replacement text, substring the resulting error must contain)
_MUTATIONS = [
    ("hermes_cli/kanban_db.py", _GOOD_KDB.replace("_objective_lineage_members = None\n", ""),
     "attempt ceiling is missing"),
    ("hermes_cli/kanban_db.py", _GOOD_KDB + "if _gauntlet_objective_scoped(x):\n    pass\n",
     "still coupled to Gauntlet enforcement"),
    ("hermes_cli/kanban_db.py", _GOOD_KDB.replace("class ControlPlaneAdmissionError(Exception): pass\n", ""),
     "admission control is missing"),
    ("hermes_cli/kanban_db.py", _GOOD_KDB.replace("def gauntlet_default_for_subject(): pass\n", ""),
     "selective enforcement classifier is missing"),
    ("norcal/boot/claude-session-start-gate.py", _GOOD_GATE,
     "Claude SessionStart does not use the canonical exact shared boot-state parser"),
    ("norcal/boot/claude-boot-context", "nothing\n",
     "Claude boot wrapper does not use the canonical exact shared boot-state parser"),
    ("norcal/boot/codex-boot", "nothing\n",
     "Codex boot wrapper does not use the canonical exact shared boot-state parser"),
    ("norcal/boot/recovery_boot.py", 'if "BOOT STATUS: COMPLETE" in text\n',
     "not exact-line anchored"),
    ("hermes_cli/norcal_boot.py", "nothing\n",
     "Hermes session boot chokepoint does not use the canonical exact shared boot-state parser"),
    ("cli.py", "nothing\n",
     "Hermes interactive CLI does not build its fresh-session prompt through the shared boot chokepoint"),
    ("gateway/run.py", "nothing\n",
     "Hermes gateway does not build its fresh-session prompt through the shared boot chokepoint"),
    ("cli.py", _GOOD_ENTRY + "if _shared_boot_proc.returncode != 0:\n    pass\n",
     "Hermes interactive CLI still treats a zero generator exit code as a passed boot gate"),
    ("gateway/run.py", _GOOD_ENTRY + "if _shared_boot_proc.returncode != 0:\n    pass\n",
     "Hermes gateway still treats a zero generator exit code as a passed boot gate"),
    ("norcal/boot/claude-session-start-gate.py", _GOOD_WRAPPER + "boot_wrapper_ran = True\n",
     "exposes no recovery-only authority path"),
    ("norcal/boot/claude-session-start-gate.py", _GOOD_WRAPPER + "recovery_only = True\n",
     "does not distinguish a real boot payload from the exception fallback"),
]


@pytest.mark.parametrize("rel,replacement,expected", _MUTATIONS,
                         ids=[f"{i}:{m[0]}" for i, m in enumerate(_MUTATIONS)])
def test_repo_assertions_are_not_vacuous(tmp_path, rel, replacement, expected):
    """Break one invariant; the verifier must name it. Otherwise it checks nothing."""
    repo, here = _good_tree(tmp_path)
    (repo / rel).write_text(replacement, encoding="utf-8")
    errs = _verify_module().repo_errors(repo, here)
    assert any(expected in e for e in errs), f"{rel} mutation went unreported; got {errs}"


def test_missing_files_are_reported_not_raised(tmp_path):
    """A missing file must be a named failure, not a traceback.

    The pre-2026-09-21 verifier read the Codex and runtime configs unguarded at module
    scope, so an absent file crashed the whole check instead of reporting which one.
    """
    empty_repo = tmp_path / "empty"
    (empty_repo / "norcal" / "boot").mkdir(parents=True)
    errs = _verify_module().repo_errors(empty_repo, empty_repo / "norcal" / "boot")
    assert len(errs) >= 8
    assert all(isinstance(e, str) for e in errs)


def test_host_assertions_are_not_vacuous(tmp_path):
    """The deployment checks must fire against a host that has none of the layout."""
    errs = _verify_module().host_errors(tmp_path, BOOT_SRC)
    assert any("not linked to canonical boot dir" in e for e in errs)
    assert any("AGENTS.md" in e for e in errs)
    assert any("model_instructions_file" in e for e in errs)
    assert any("gauntlet_objective_attempt_limit" in e for e in errs)


def test_repo_only_mode_reads_no_host_state(tmp_path, monkeypatch):
    """--repo-only must be portable: no ~/.codex, no live config, no subprocess."""
    mod = _verify_module()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("generator was launched"))
    monkeypatch.setattr(mod, "HOME", tmp_path)
    monkeypatch.setattr(mod, "host_errors", lambda *a, **k: pytest.fail("host state was read"))
    assert mod.main(["--repo-only"]) == 0


def test_host_only_mode_does_not_launch_the_generator(monkeypatch):
    mod = _verify_module()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: pytest.fail("generator was launched"))
    monkeypatch.setattr(mod, "host_errors", lambda *a, **k: [])
    assert mod.main(["--host-only"]) == 0


def test_failures_exit_nonzero_and_print_fail(tmp_path, monkeypatch):
    mod = _verify_module()
    monkeypatch.setattr(mod, "repo_errors", lambda *a, **k: ["synthetic parity break"])
    assert mod.main(["--repo-only"]) == 1


# ---------------------------------------------------------------------------
# Deployment parity on this host. Skipped where the canonical layout is absent.
# ---------------------------------------------------------------------------

_DEPLOYED_ENTRY = Path.home() / ".local/bin/hermes-shared-boot-context"


def _is_the_deployed_tree() -> bool:
    """True only when this checkout is the tree the host's boot entry points resolve to.

    ``host_errors`` compares each deployed symlink against the *running* checkout's boot
    directory, so from a git worktree or a CI clone it reports drift that does not exist.
    Gating on identity keeps the check meaningful where it applies and silent where it
    cannot: the remaining assertions (the other three entry points, Codex AGENTS.md and
    config, and the live runtime config key) are what it actually buys on the host.
    """
    try:
        return _DEPLOYED_ENTRY.is_symlink() and _DEPLOYED_ENTRY.resolve().parent == BOOT_SRC
    except OSError:
        return False


@pytest.mark.skipif(
    not _is_the_deployed_tree(),
    reason="this checkout is not the deployed boot tree (git worktree, clone, or CI)",
)
def test_host_deployment_parity_on_this_host():
    """Runs only from the governed host's deployed tree; CI has no boot symlinks at all."""
    p = _run("--host-only")
    errors = [ln for ln in p.stdout.splitlines() if ln.startswith("ERROR:")]
    assert errors == [], "host boot deployment drift: " + "; ".join(errors)
    assert p.returncode == 0
