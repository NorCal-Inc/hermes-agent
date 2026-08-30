"""Deterministic execution-control boundary for governed external executors.

The failure this module exists to eliminate
-------------------------------------------

A Claude Code invocation was launched synchronously. The caller hit its
execution timeout and reported EXECUTION FAILED. The Claude child survived,
kept running outside any controller, and later produced valid commits.

That is a split-brain: the controller believes execution ended while the
executor is still mutating state. ``subprocess.run(timeout=...)`` cannot
prevent it — on ``TimeoutExpired`` it kills only the *direct* child, so every
grandchild (a CLI's own daemon, a spawned node process) is reparented to init
and continues, invisibly, forever.

The invariants
--------------

1. **Record before process.** The durable :data:`executions` row is INSERTed
   and committed before ``Popen`` is called. A process the supervisor did not
   record cannot exist, so there is no window in which a live executor has no
   durable identity.

2. **Ownership is explicit and total.** ``executions.ownership`` is either
   ``controller`` (a synchronous caller is waiting; losing it terminates the
   process group) or ``supervisor`` (a durable background job reconciled by
   :func:`reconcile`). There is deliberately no third value, so "the caller
   returned and nobody owns this" is unrepresentable rather than merely
   unlikely. Every exit path from :func:`run_supervised` — return, exception,
   ``KeyboardInterrupt``, ``SystemExit`` — passes through the same settle
   step in a ``finally``.

3. **Long work is background work from the start.** A requested timeout above
   the synchronous ceiling (``execution.sync_ceiling_seconds``) does not get a
   longer synchronous wait: the execution is created ``supervisor``-owned at
   launch, atomically, so losing the caller is a no-op rather than an orphan.
   The accidental-orphan-after-timeout path is not made less likely; it is
   removed.

4. **Reconciliation is deterministic.** Every non-terminal row resolves to one
   of the terminal statuses in :data:`TERMINAL_STATUSES`, or is explicitly
   adopted. A live orphan is terminated or adopted — never logged and left.

5. **PID is not identity.** Signalling is gated on a fingerprint captured at
   launch (Linux: the boot-relative start time and ``comm`` from
   ``/proc/<pid>/stat``). A recycled PID fails the comparison, is classified
   ``stale``/``pid_reused``, and is *never* signalled. Killing an innocent
   unrelated process is a worse failure than leaking a record.

6. **Deny by default.** :class:`ExecutionPolicy` gates executor class, working
   root, command class and runtime *before* the record exists. There is no
   generic "run this argv" entry point: callers name a launcher registered in
   :data:`LAUNCHERS`, and the launcher builds argv. This is an execution
   boundary, not a root shell.

Secrets
-------

The execution record persists ``command_class`` — the launcher's name — and
never argv, environment, prompts or tokens. Prompts and credentials are passed
to the child in memory and are not written to the database, to
``execution_events``, or to any operator surface. See
:func:`_assert_record_is_secret_free`, which is asserted in tests.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from hermes_cli import kanban_db as kb

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

STATUS_LAUNCHING = "launching"
STATUS_RUNNING = "running"

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMED_OUT = "timed_out"
STATUS_CONTROLLER_LOST = "controller_lost"
STATUS_STALE = "stale"
STATUS_TERMINATED = "terminated"
STATUS_RECOVERED = "recovered"

ACTIVE_STATUSES = (STATUS_LAUNCHING, STATUS_RUNNING)
TERMINAL_STATUSES = (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_TIMED_OUT,
    STATUS_CONTROLLER_LOST,
    STATUS_STALE,
    STATUS_TERMINATED,
    STATUS_RECOVERED,
)
#: Terminal statuses that mean "the executor finished the work it was asked to
#: do". Everything else is a non-success, and a non-success must never be
#: allowed to look like a completion to the Gauntlet lifecycle.
SUCCESS_STATUSES = (STATUS_COMPLETED,)

OWNERSHIP_CONTROLLER = "controller"
OWNERSHIP_SUPERVISOR = "supervisor"
VALID_OWNERSHIP = (OWNERSHIP_CONTROLLER, OWNERSHIP_SUPERVISOR)

#: What reconciliation does with a live executor whose controller is gone.
#: ``terminate`` (the default) kills the process group; ``adopt`` transfers
#: ownership to the supervisor, which then holds it to the runtime cap. Both
#: are deterministic and recorded; "leave it running unowned" is not offered.
ORPHAN_POLICY_TERMINATE = "terminate"
ORPHAN_POLICY_ADOPT = "adopt"
VALID_ORPHAN_POLICIES = (ORPHAN_POLICY_TERMINATE, ORPHAN_POLICY_ADOPT)


# ---------------------------------------------------------------------------
# Defaults (all overridable under the ``execution`` config key)
# ---------------------------------------------------------------------------

#: Longest a caller may wait synchronously before the execution is created as
#: a supervisor-owned background job instead. Chosen to sit just above a
#: normal gate/test run and well below an agentic repair attempt, which is
#: exactly the class of work that produced the orphan.
DEFAULT_SYNC_CEILING_SECONDS = 900
#: Hard ceiling applied to every execution when the caller names none.
DEFAULT_MAX_RUNTIME_SECONDS = 3600
#: A live executor whose owner has not reported progress for this long is
#: called stale and reconciled. Generous: an agentic executor can legitimately
#: be silent for a long time, and a false positive kills real work.
DEFAULT_STALE_HEARTBEAT_SECONDS = 1800
#: Grace period between SIGTERM and SIGKILL when terminating a process group.
DEFAULT_TERMINATE_GRACE_SECONDS = 10

DEFAULT_ALLOWED_EXECUTORS = ("claude", "codex", "shell")

#: Bound on captured child output kept in memory and handed back to callers.
#: Full transcripts belong in the executor's own session logs, not here.
OUTPUT_MAX_CHARS = 20000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExecutionPolicyError(RuntimeError):
    """A launch was refused by the policy layer.

    Raised BEFORE any durable record is written and before any process is
    created, so a refused launch leaves the board and the host untouched.
    ``code`` is the stable part — callers and tools branch on it rather than
    parsing the message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ExecutionNotFound(LookupError):
    """No execution row with that id on this board."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _exec_config() -> dict:
    """Read the ``execution`` config block live.

    Live rather than import-captured for the same reason
    ``gauntlet_enforcement_default`` is: an operator retuning a runtime cap or
    an allow-list must not have to restart the gateway, and a policy that can
    only be widened by a restart tends to get widened permanently instead.
    """
    try:
        from hermes_cli.config import load_config

        block = (load_config() or {}).get("execution", {})
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _cfg_int(block: dict, key: str, default: int) -> int:
    raw = block.get(key, default)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        _log.warning("execution: invalid execution.%s=%r; using %d", key, raw, default)
        return default


def _cfg_str_list(block: dict, key: str, default: Iterable[str]) -> tuple[str, ...]:
    raw = block.get(key)
    if raw is None:
        return tuple(default)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        _log.warning("execution: invalid execution.%s=%r; using default", key, raw)
        return tuple(default)
    return tuple(str(item) for item in raw if str(item).strip())


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionPolicy:
    """Deny-by-default execution scope.

    An execution boundary, not unlimited authority: this decides *whether* a
    class of process may be started and where, and nothing else. It grants no
    filesystem permissions, relaxes no ownership, and has no notion of
    escalating privilege beyond the explicit :attr:`allow_sudo` opt-in, which
    is off unless an operator turns it on in config.
    """

    allowed_executors: tuple[str, ...] = DEFAULT_ALLOWED_EXECUTORS
    #: Absolute directory prefixes an execution's cwd may fall under. Empty
    #: means "nothing is allowed", not "everything is allowed" — the
    #: deny-by-default posture has to survive a missing/empty config.
    allowed_roots: tuple[str, ...] = ()
    allowed_command_classes: tuple[str, ...] = ()
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS
    per_executor_max_runtime: dict = field(default_factory=dict)
    sync_ceiling_seconds: int = DEFAULT_SYNC_CEILING_SECONDS
    stale_heartbeat_seconds: int = DEFAULT_STALE_HEARTBEAT_SECONDS
    terminate_grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS
    orphan_policy: str = ORPHAN_POLICY_TERMINATE
    allow_sudo: bool = False

    # -- checks ----------------------------------------------------------

    def check_executor(self, executor_type: str) -> None:
        if executor_type not in self.allowed_executors:
            raise ExecutionPolicyError(
                "executor_not_allowed",
                f"executor type {executor_type!r} is not in "
                f"execution.allowed_executors ({', '.join(self.allowed_executors) or 'none'})",
            )

    def check_command_class(self, command_class: str) -> None:
        if command_class not in LAUNCHERS:
            raise ExecutionPolicyError(
                "command_class_unregistered",
                f"command class {command_class!r} has no registered launcher; "
                "the supervisor never accepts raw argv",
            )
        if (
            self.allowed_command_classes
            and command_class not in self.allowed_command_classes
        ):
            raise ExecutionPolicyError(
                "command_class_not_allowed",
                f"command class {command_class!r} is not in "
                "execution.allowed_command_classes",
            )
        if LAUNCHERS[command_class].requires_sudo and not self.allow_sudo:
            raise ExecutionPolicyError(
                "sudo_not_permitted",
                f"command class {command_class!r} is sudo-eligible and "
                "execution.allow_sudo is false",
            )

    def resolve_root(self, cwd: str | os.PathLike) -> Path:
        """Return the resolved cwd, or refuse it.

        Resolved with ``strict=True`` before comparison so a symlink cannot
        point a permitted-looking path at a directory outside every allowed
        root — the check has to run on the real destination, not the name.
        """
        try:
            resolved = Path(cwd).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ExecutionPolicyError(
                "cwd_unresolvable", f"working directory {str(cwd)!r} is unusable: {exc}"
            ) from exc
        if not resolved.is_dir():
            raise ExecutionPolicyError(
                "cwd_not_a_directory", f"working directory {str(resolved)!r} is not a directory"
            )
        for root in self.allowed_roots:
            try:
                root_resolved = Path(root).resolve()
            except (OSError, RuntimeError):
                continue
            if resolved == root_resolved or root_resolved in resolved.parents:
                return resolved
        raise ExecutionPolicyError(
            "cwd_outside_allowed_roots",
            f"working directory {str(resolved)!r} is outside every root in "
            f"execution.allowed_roots ({', '.join(self.allowed_roots) or 'none'})",
        )

    def resolve_max_runtime(self, executor_type: str, requested: Optional[int]) -> int:
        """Effective runtime cap: the tightest of request, per-executor, global.

        A caller may only ever ask for *less* than the policy allows. Asking
        for more is not an error — it is silently clamped — because the point
        is a bound that holds regardless of what the caller believed.
        """
        candidates = [self.max_runtime_seconds]
        per = self.per_executor_max_runtime.get(executor_type)
        if per:
            try:
                candidates.append(max(1, int(per)))
            except (TypeError, ValueError):
                pass
        if requested:
            try:
                candidates.append(max(1, int(requested)))
            except (TypeError, ValueError):
                pass
        return min(c for c in candidates if c and c > 0)


def default_allowed_roots() -> tuple[str, ...]:
    """Roots permitted when the operator has configured none.

    HERMES_HOME and the board's workspaces directory are where governed work
    legitimately happens. Notably absent: ``/``, ``$HOME`` and the process
    cwd — an execution boundary whose default is "wherever the caller happens
    to be" is not a boundary.
    """
    roots: list[str] = []
    try:
        from hermes_constants import get_hermes_home

        roots.append(str(get_hermes_home()))
    except Exception:
        pass
    try:
        roots.append(str(kb.workspaces_root()))
    except Exception:
        pass
    # De-duplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for root in roots:
        if root and root not in seen:
            seen.add(root)
            out.append(root)
    return tuple(out)


def load_policy() -> ExecutionPolicy:
    """Build the live policy from config, deny-by-default where unset."""
    block = _exec_config()
    per_exec_raw = block.get("max_runtime_seconds_by_executor") or {}
    per_exec = per_exec_raw if isinstance(per_exec_raw, dict) else {}

    orphan_policy = str(block.get("orphan_policy", ORPHAN_POLICY_TERMINATE))
    if orphan_policy not in VALID_ORPHAN_POLICIES:
        _log.warning(
            "execution: invalid execution.orphan_policy=%r; using %r",
            orphan_policy,
            ORPHAN_POLICY_TERMINATE,
        )
        orphan_policy = ORPHAN_POLICY_TERMINATE

    roots = _cfg_str_list(block, "allowed_roots", ())
    if not roots:
        roots = default_allowed_roots()

    return ExecutionPolicy(
        allowed_executors=_cfg_str_list(
            block, "allowed_executors", DEFAULT_ALLOWED_EXECUTORS
        ),
        allowed_roots=roots,
        allowed_command_classes=_cfg_str_list(block, "allowed_command_classes", ()),
        max_runtime_seconds=_cfg_int(
            block, "max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS
        ),
        per_executor_max_runtime=per_exec,
        sync_ceiling_seconds=_cfg_int(
            block, "sync_ceiling_seconds", DEFAULT_SYNC_CEILING_SECONDS
        ),
        stale_heartbeat_seconds=_cfg_int(
            block, "stale_heartbeat_seconds", DEFAULT_STALE_HEARTBEAT_SECONDS
        ),
        terminate_grace_seconds=_cfg_int(
            block, "terminate_grace_seconds", DEFAULT_TERMINATE_GRACE_SECONDS
        ),
        orphan_policy=orphan_policy,
        allow_sudo=bool(block.get("allow_sudo", False)),
    )


# ---------------------------------------------------------------------------
# Registered launchers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Launcher:
    """A named, bounded way to build argv.

    The whole point of routing every launch through a registry: the durable
    record names the launcher, never the argv, so a prompt or a credential
    passed as an argument cannot reach the database. ``build`` receives a
    plain dict of parameters and returns argv; anything it does not
    understand is ignored rather than forwarded.
    """

    name: str
    executor_type: str
    build: Callable[[dict], list[str]]
    requires_sudo: bool = False


def _require_str(spec: dict, key: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise ExecutionPolicyError(
            "spec_invalid", f"launcher spec requires a non-empty string {key!r}"
        )
    return value


def _build_claude_headless(spec: dict) -> list[str]:
    prompt = _require_str(spec, "prompt")
    binary = shutil.which("claude") or "claude"
    return [
        binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--permission-mode",
        "acceptEdits",
    ]


def _build_codex_exec(spec: dict) -> list[str]:
    prompt = _require_str(spec, "prompt")
    binary = shutil.which("codex") or "codex"
    argv = [
        binary,
        "exec",
        prompt,
        "--json",
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
    ]
    last_message_path = spec.get("last_message_path")
    if last_message_path:
        argv += ["-o", str(last_message_path)]
    return argv


#: Shell metacharacters a gate command may not contain. A gate is a bounded
#: check, so it is parsed with ``shlex`` and executed with ``shell=False``:
#: there is no interpreter to inject into. Refusing the operators outright
#: (rather than quoting them) keeps that property obvious at the boundary.
_SHELL_OPERATORS = ("|", ";", "&&", "||", ">", "<", "`", "$(", "\n", "\r")


def gate_argv(gate_cmd: str) -> list[str]:
    """Parse a gate command without invoking a shell."""
    if any(token in gate_cmd for token in _SHELL_OPERATORS):
        raise ExecutionPolicyError(
            "shell_operators_forbidden",
            "gate command may not contain shell operators or redirection",
        )
    argv = shlex.split(gate_cmd)
    if not argv:
        raise ExecutionPolicyError("spec_invalid", "gate command is empty")
    return argv


def _build_shell_gate(spec: dict) -> list[str]:
    return gate_argv(_require_str(spec, "command"))


def _build_shell_argv(spec: dict) -> list[str]:
    """Explicit argv for tests and operator-registered checks.

    Still not a generic root endpoint: it is a *registered* class, subject to
    ``execution.allowed_command_classes``, the executor allow-list and the
    working-root check like every other. It exists so a caller with a genuine
    vector of arguments does not have to round-trip through a string that
    would then need quoting rules.
    """
    argv = spec.get("argv")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ExecutionPolicyError(
            "spec_invalid", "launcher spec requires a non-empty 'argv' list"
        )
    out = [str(part) for part in argv]
    if any(not part for part in out):
        raise ExecutionPolicyError("spec_invalid", "argv contains an empty element")
    return out


LAUNCHERS: dict[str, Launcher] = {
    "claude.headless": Launcher("claude.headless", "claude", _build_claude_headless),
    "codex.exec": Launcher("codex.exec", "codex", _build_codex_exec),
    "shell.gate": Launcher("shell.gate", "shell", _build_shell_gate),
    "shell.argv": Launcher("shell.argv", "shell", _build_shell_argv),
}


def register_launcher(launcher: Launcher) -> None:
    """Register an additional bounded launcher.

    Deliberately explicit and additive: there is no way to register "run
    whatever argv the caller supplies at the time of the call", because that
    is the generic endpoint this whole layer exists to not have.
    """
    if launcher.name in LAUNCHERS and LAUNCHERS[launcher.name] is not launcher:
        raise ValueError(f"launcher {launcher.name!r} is already registered")
    LAUNCHERS[launcher.name] = launcher


# ---------------------------------------------------------------------------
# Process identity — PID is not identity
# ---------------------------------------------------------------------------

_PROC_STAT_RE = re.compile(r"^(\d+)\s+\((.*)\)\s+(.*)$", re.DOTALL)


def process_identity(pid: Optional[int]) -> Optional[str]:
    """Fingerprint of the live process at ``pid``, or None if there is none.

    On Linux this is the boot-relative start time (``/proc/<pid>/stat`` field
    22) plus ``comm``. Start time is what makes it safe against PID reuse: the
    kernel will hand the same number out again, but not with the same start
    tick, so a recycled PID produces a different fingerprint and fails the
    comparison in :func:`identity_matches`.

    Returns ``None`` when the process does not exist. Raises nothing — an
    unreadable ``/proc`` entry is treated as "cannot confirm", and every
    caller that would *signal* on the strength of this treats "cannot
    confirm" as "do not signal".
    """
    if not pid or pid <= 0:
        return None
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError:
        return None
    match = _PROC_STAT_RE.match(raw.strip())
    if not match:
        return None
    comm = match.group(2)
    rest = match.group(3).split()
    # Fields from ``state`` (index 0 here) onward; ``starttime`` is field 22
    # of the whole line, i.e. index 19 of this remainder.
    if len(rest) < 20:
        return None
    state = rest[0]
    if state == "Z":
        # A zombie is an exit status waiting to be collected, not a running
        # executor. Its /proc entry survives — same start time, same comm — so
        # a naive fingerprint match reports a killed-but-unreaped child as
        # ALIVE, forever. That inverts this module's central question: the
        # termination path would conclude the process refused to die and hand
        # a corpse to the supervisor as a live orphan. Treating Z as gone is
        # what makes "confirmed dead" mean what it says.
        return None
    starttime = rest[19]
    return f"linux:{starttime}:{comm}"


def _fallback_alive(pid: Optional[int]) -> bool:
    """Existence probe for platforms with no ``/proc``.

    Signal 0 only reports existence, never identity, so a caller relying on
    this alone must never terminate on its say-so. :func:`identity_matches`
    encodes that: with no fingerprint it refuses to claim a match.
    """
    if not pid or pid <= 0:
        return False
    # Note: signal 0 also succeeds against a zombie, which this probe cannot
    # distinguish. That is why it is the fallback and why identity_matches
    # refuses to claim a match without a real fingerprint.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else. Existence is all this probe claims.
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def identity_matches(pid: Optional[int], recorded_key: Optional[str]) -> bool:
    """Whether the live process at ``pid`` is still the recorded executor.

    False when the process is gone, when the fingerprint differs (PID reuse),
    and — deliberately — when identity cannot be established at all. The cost
    of a false negative is a record classified ``stale``; the cost of a false
    positive is signalling an unrelated process that happens to have inherited
    the number. Those are not symmetric, so this errs to False.
    """
    if not pid or pid <= 0:
        return False
    live = process_identity(pid)
    if live is None:
        return False
    if not recorded_key:
        return False
    return live == recorded_key


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class ExecutionRecord:
    id: str
    task_id: Optional[str]
    executor_type: str
    command_class: str
    cwd: str
    pid: Optional[int]
    pgid: Optional[int]
    proc_key: Optional[str]
    nonce: str
    controller_pid: Optional[int]
    controller_key: Optional[str]
    controller_token: str
    ownership: str
    max_runtime_s: Optional[int]
    started_at: int
    heartbeat_at: int
    last_observed_at: Optional[int]
    ended_at: Optional[int]
    status: str
    exit_code: Optional[int]
    termination_reason: Optional[str]
    rollback_ref: Optional[str]
    route_task: bool
    created_at: int

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status in SUCCESS_STATUSES and (self.exit_code == 0)

    def runtime_seconds(self, now: Optional[int] = None) -> int:
        end = self.ended_at or (now if now is not None else int(time.time()))
        return max(0, int(end) - int(self.started_at))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "executor_type": self.executor_type,
            "command_class": self.command_class,
            "cwd": self.cwd,
            "pid": self.pid,
            "pgid": self.pgid,
            "nonce": self.nonce,
            "controller_pid": self.controller_pid,
            "controller_token": self.controller_token,
            "ownership": self.ownership,
            "max_runtime_s": self.max_runtime_s,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "last_observed_at": self.last_observed_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "exit_code": self.exit_code,
            "termination_reason": self.termination_reason,
            "rollback_ref": self.rollback_ref,
            "route_task": self.route_task,
            "created_at": self.created_at,
        }


def _row_to_record(row: sqlite3.Row) -> ExecutionRecord:
    keys = row.keys()

    def get(name, default=None):
        return row[name] if name in keys else default

    return ExecutionRecord(
        id=row["id"],
        task_id=get("task_id"),
        executor_type=get("executor_type", ""),
        command_class=get("command_class", ""),
        cwd=get("cwd", ""),
        pid=get("pid"),
        pgid=get("pgid"),
        proc_key=get("proc_key"),
        nonce=get("nonce", ""),
        controller_pid=get("controller_pid"),
        controller_key=get("controller_key"),
        controller_token=get("controller_token", ""),
        ownership=get("ownership", OWNERSHIP_SUPERVISOR),
        max_runtime_s=get("max_runtime_s"),
        started_at=get("started_at", 0),
        heartbeat_at=get("heartbeat_at", 0),
        last_observed_at=get("last_observed_at"),
        ended_at=get("ended_at"),
        status=get("status", STATUS_STALE),
        exit_code=get("exit_code"),
        termination_reason=get("termination_reason"),
        rollback_ref=get("rollback_ref"),
        route_task=bool(get("route_task", 1)),
        created_at=get("created_at", 0),
    )


#: Substrings that must never appear in a persisted execution record. Asserted
#: at write time rather than only in tests: a record is an operator surface and
#: an audit artefact, and a credential that reaches one has to be rotated.
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "sk-ant-",
    "ghp_",
    "github_pat_",
    "bearer ",
    "authorization:",
    "password",
    "secret",
    "token=",
    "access_token",
    "-----begin",
)


def _assert_record_is_secret_free(values: dict) -> None:
    """Refuse to persist a record whose free-text fields smell of a credential.

    Structural defence, not a filter: the fields written here are an id, a
    launcher name, a path and a status vocabulary, none of which has any
    legitimate reason to contain a credential. If one does, the caller is
    doing something the launcher registry exists to prevent, and failing loudly
    is much cheaper than a rotation.
    """
    for key in ("command_class", "executor_type", "cwd", "termination_reason", "rollback_ref"):
        value = values.get(key)
        if not value:
            continue
        lowered = str(value).lower()
        for marker in _SECRET_MARKERS:
            if marker in lowered:
                raise ExecutionPolicyError(
                    "secret_in_record",
                    f"refusing to persist execution field {key!r}: it contains "
                    f"{marker!r}, which looks like a credential",
                )


def _now() -> int:
    return int(time.time())


def _new_execution_id() -> str:
    return f"x_{secrets.token_hex(8)}"


def controller_identity() -> tuple[int, Optional[str], str]:
    """(pid, identity fingerprint, ownership token) of the calling controller.

    The token is per-call rather than per-process: two sequential launches from
    the same session are two distinct ownerships, so a stale record can never
    be mistaken for the live one just because the PID matches.
    """
    pid = os.getpid()
    return pid, process_identity(pid), secrets.token_hex(12)


def _append_execution_event(
    conn: sqlite3.Connection,
    execution_id: str,
    kind: str,
    payload: Optional[dict] = None,
) -> None:
    """Append one audit row. Called from inside an open transaction."""
    conn.execute(
        "INSERT INTO execution_events (execution_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (
            execution_id,
            kind,
            json.dumps(payload, ensure_ascii=False) if payload else None,
            _now(),
        ),
    )


def get_execution(conn: sqlite3.Connection, execution_id: str) -> Optional[ExecutionRecord]:
    row = conn.execute(
        "SELECT * FROM executions WHERE id = ?", (execution_id,)
    ).fetchone()
    return _row_to_record(row) if row is not None else None


def list_executions(
    conn: sqlite3.Connection,
    *,
    status: Optional[str] = None,
    active_only: bool = False,
    task_id: Optional[str] = None,
    limit: int = 50,
) -> list[ExecutionRecord]:
    clauses: list[str] = []
    params: list[Any] = []
    if active_only:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        clauses.append(f"status IN ({placeholders})")
        params.extend(ACTIVE_STATUSES)
    elif status:
        clauses.append("status = ?")
        params.append(status)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    rows = conn.execute(
        f"SELECT * FROM executions {where} ORDER BY started_at DESC, rowid DESC LIMIT ?",
        params,
    ).fetchall()
    return [_row_to_record(row) for row in rows]


def execution_events(
    conn: sqlite3.Connection, execution_id: str, *, limit: int = 100
) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, payload, created_at FROM execution_events "
        "WHERE execution_id = ? ORDER BY id ASC LIMIT ?",
        (execution_id, max(1, int(limit))),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except (TypeError, ValueError):
            payload = None
        out.append(
            {"kind": row["kind"], "payload": payload, "created_at": row["created_at"]}
        )
    return out


def heartbeat(conn: sqlite3.Connection, execution_id: str, *, now: Optional[int] = None) -> bool:
    """Record that an owner has just observed this execution making progress.

    Never advanced by :func:`reconcile` — a reconciler that refreshed the
    heartbeat would make every abandoned job look permanently fresh, which is
    exactly the invisible-parking failure the freshness work exists to stop.
    """
    ts = now if now is not None else _now()
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with kb.write_txn(conn):
        cur = conn.execute(
            f"UPDATE executions SET heartbeat_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (ts, execution_id, *ACTIVE_STATUSES),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Creating the durable record — always before the process
# ---------------------------------------------------------------------------


def create_execution(
    conn: sqlite3.Connection,
    *,
    executor_type: str,
    command_class: str,
    cwd: str,
    task_id: Optional[str] = None,
    controller_pid: Optional[int] = None,
    controller_key: Optional[str] = None,
    controller_token: Optional[str] = None,
    ownership: str = OWNERSHIP_CONTROLLER,
    max_runtime_s: Optional[int] = None,
    rollback_ref: Optional[str] = None,
    route_task: bool = True,
    now: Optional[int] = None,
) -> ExecutionRecord:
    """INSERT and COMMIT the execution row. No process exists yet.

    Separated from :func:`_start_process` so the ordering invariant is visible
    at the call site and testable on its own: if this raises, nothing was
    started; if it returns, the executor that is about to start already has a
    durable identity and an owner.
    """
    if ownership not in VALID_OWNERSHIP:
        raise ValueError(f"invalid ownership {ownership!r}")
    ts = now if now is not None else _now()
    execution_id = _new_execution_id()
    values = {
        "command_class": command_class,
        "executor_type": executor_type,
        "cwd": str(cwd),
        "termination_reason": None,
        "rollback_ref": rollback_ref,
    }
    _assert_record_is_secret_free(values)
    nonce = secrets.token_hex(16)
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO executions ("
            "  id, task_id, executor_type, command_class, cwd, pid, pgid,"
            "  proc_key, nonce, controller_pid, controller_key, controller_token,"
            "  ownership, max_runtime_s, started_at, heartbeat_at,"
            "  last_observed_at, ended_at, status, exit_code, termination_reason,"
            "  rollback_ref, route_task, created_at"
            ") VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?,"
            "          NULL, NULL, ?, NULL, NULL, ?, ?, ?)",
            (
                execution_id,
                task_id,
                executor_type,
                command_class,
                str(cwd),
                nonce,
                controller_pid,
                controller_key,
                controller_token or secrets.token_hex(12),
                ownership,
                max_runtime_s,
                ts,
                ts,
                STATUS_LAUNCHING,
                rollback_ref,
                1 if route_task else 0,
                ts,
            ),
        )
        _append_execution_event(
            conn,
            execution_id,
            "created",
            {
                "executor_type": executor_type,
                "command_class": command_class,
                "ownership": ownership,
                "task_id": task_id,
                "max_runtime_s": max_runtime_s,
            },
        )
        if task_id:
            kb._append_event(
                conn,
                task_id,
                "execution_created",
                {
                    "execution_id": execution_id,
                    "executor_type": executor_type,
                    "command_class": command_class,
                    "ownership": ownership,
                },
            )
    record = get_execution(conn, execution_id)
    assert record is not None  # just inserted and committed
    return record


def _attach_process(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    pid: int,
    pgid: Optional[int],
    proc_key: Optional[str],
    now: Optional[int] = None,
) -> None:
    """Bind the started process to its already-durable record."""
    ts = now if now is not None else _now()
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE executions SET pid = ?, pgid = ?, proc_key = ?, status = ?, "
            "heartbeat_at = ? WHERE id = ? AND status = ?",
            (pid, pgid, proc_key, STATUS_RUNNING, ts, execution_id, STATUS_LAUNCHING),
        )
        _append_execution_event(
            conn, execution_id, "started", {"pid": pid, "pgid": pgid}
        )


def _settle(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    status: str,
    exit_code: Optional[int] = None,
    reason: Optional[str] = None,
    now: Optional[int] = None,
) -> bool:
    """Move a non-terminal execution to a terminal status, exactly once.

    The ``status IN (active)`` predicate is inside the UPDATE, so two racing
    settlers (the synchronous waiter and a reconciliation pass) cannot both
    write a terminal state: the loser's rowcount is 0 and it reports False.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"{status!r} is not a terminal execution status")
    _assert_record_is_secret_free({"termination_reason": reason})
    ts = now if now is not None else _now()
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with kb.write_txn(conn):
        cur = conn.execute(
            f"UPDATE executions SET status = ?, exit_code = ?, "
            f"termination_reason = ?, ended_at = ?, last_observed_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (status, exit_code, reason, ts, ts, execution_id, *ACTIVE_STATUSES),
        )
        if cur.rowcount:
            _append_execution_event(
                conn,
                execution_id,
                "settled",
                {"status": status, "exit_code": exit_code, "reason": reason},
            )
    return cur.rowcount > 0


def _transfer_to_supervisor(
    conn: sqlite3.Connection, execution_id: str, *, reason: str
) -> bool:
    """Atomically hand a live controller-owned execution to the supervisor.

    This is the second half of invariant 2. When a controller loses a child it
    cannot kill, the choice is not between "terminate" and "leave it"; it is
    between "terminate" and "record that the supervisor now owns it, in the
    same transaction the controller gives up". The row never passes through a
    state in which nobody owns it.
    """
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))
    with kb.write_txn(conn):
        cur = conn.execute(
            f"UPDATE executions SET ownership = ? "
            f"WHERE id = ? AND ownership = ? AND status IN ({placeholders})",
            (OWNERSHIP_SUPERVISOR, execution_id, OWNERSHIP_CONTROLLER, *ACTIVE_STATUSES),
        )
        if cur.rowcount:
            _append_execution_event(
                conn, execution_id, "adopted", {"reason": reason}
            )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Termination — always the group, never a bare PID
# ---------------------------------------------------------------------------


@dataclass
class TerminationOutcome:
    signalled: bool
    dead: bool
    detail: str


def terminate_process_group(
    *,
    pid: Optional[int],
    pgid: Optional[int],
    proc_key: Optional[str],
    grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> TerminationOutcome:
    """SIGTERM then SIGKILL the executor's process *group*.

    The group, because that is the difference between killing the executor and
    orphaning its children: the child is started with ``start_new_session=True``
    so it leads its own session and group, and every process it spawns inherits
    that group unless it deliberately leaves.

    Refuses to signal anything whose identity does not match the recorded
    fingerprint. A recycled PID is not this executor, and killing whatever now
    owns the number is a far worse outcome than leaving a record to be
    classified ``stale``.
    """
    if not pid or pid <= 0:
        return TerminationOutcome(False, True, "no pid recorded")
    if proc_key and not identity_matches(pid, proc_key):
        live = process_identity(pid)
        if live is None:
            return TerminationOutcome(False, True, "process already gone")
        return TerminationOutcome(
            False, True, "pid reused by an unrelated process; not signalled"
        )
    if proc_key is None and not _fallback_alive(pid):
        return TerminationOutcome(False, True, "process already gone")

    target_group = pgid if pgid and pgid > 0 else None

    def _signal(sig: int) -> bool:
        try:
            if target_group is not None:
                os.killpg(target_group, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            return False
        except OSError:
            return False
        return True

    signalled = _signal(signal.SIGTERM)
    deadline = time.monotonic() + max(0, grace_seconds)
    while time.monotonic() < deadline:
        if not _still_alive(pid, proc_key):
            return TerminationOutcome(signalled, True, "terminated on SIGTERM")
        time.sleep(0.05)

    if _still_alive(pid, proc_key):
        signalled = _signal(signal.SIGKILL) or signalled
        # SIGKILL is not instantaneous: the kernel still has to tear the
        # process down and the parent still has to reap it. Confirm rather
        # than assume — "we sent SIGKILL" is not evidence of death, and this
        # module's entire purpose is not to confuse the two.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not _still_alive(pid, proc_key):
                return TerminationOutcome(signalled, True, "terminated on SIGKILL")
            time.sleep(0.05)

    if _still_alive(pid, proc_key):
        return TerminationOutcome(signalled, False, "still alive after SIGKILL")
    return TerminationOutcome(signalled, True, "terminated")


def _still_alive(pid: Optional[int], proc_key: Optional[str]) -> bool:
    if proc_key:
        return identity_matches(pid, proc_key)
    return _fallback_alive(pid)


# ---------------------------------------------------------------------------
# Synchronous supervised execution
# ---------------------------------------------------------------------------


@dataclass
class SupervisedResult:
    """What the caller gets back. Never a bare exit code.

    ``status`` is the authority. ``succeeded`` is deliberately narrow: only a
    ``completed`` execution with exit code 0 counts, so a timeout or a lost
    controller can never be read as success by a caller that only checks a
    boolean.
    """

    execution_id: str
    status: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    termination_reason: Optional[str]
    ownership: str
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_COMPLETED and self.exit_code == 0

    @property
    def evidence(self) -> str:
        parts = [f"[execution {self.execution_id}] status={self.status}"]
        if self.exit_code is not None:
            parts[0] += f" rc={self.exit_code}"
        if self.termination_reason:
            parts[0] += f" reason={self.termination_reason}"
        if self.error:
            parts.append(f"error: {self.error}")
        if self.stdout.strip():
            parts.append(f"stdout:\n{_truncate(self.stdout)}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{_truncate(self.stderr)}")
        return "\n".join(parts)


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) <= OUTPUT_MAX_CHARS:
        return text
    return text[:OUTPUT_MAX_CHARS] + f"\n... [truncated, {len(text)} chars total]"


def _child_env(execution_id: str, nonce: str, extra: Optional[dict] = None) -> dict:
    """Environment for the child: the parent's, plus correlation ids.

    Nothing is stripped and nothing new that is secret is added. The two
    injected values are an execution id and a non-credential nonce, both of
    which are already in the durable record, so a child that logs its own
    environment leaks nothing the operator surface does not already show.
    """
    env = dict(os.environ)
    env["HERMES_EXECUTION_ID"] = execution_id
    env["HERMES_EXECUTION_NONCE"] = nonce
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _start_process(
    argv: list[str], *, cwd: str, env: dict, capture_output: bool
) -> subprocess.Popen:
    """Popen in a NEW SESSION so the whole tree is signalable as one group.

    ``start_new_session=True`` is the load-bearing argument in this module.
    Without it the executor shares the controller's process group, killing the
    child leaves grandchildren behind, and the orphan this module exists to
    prevent walks straight out the door.
    """
    kwargs: dict = {
        "cwd": cwd,
        "env": env,
        "start_new_session": True,
    }
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"] = True
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    return subprocess.Popen(argv, **kwargs)


def _drain(proc: subprocess.Popen, timeout: Optional[float]) -> tuple[str, str]:
    """Collect whatever output is available without blocking forever.

    ``communicate`` on a killed process can still block until every inherited
    pipe writer closes — which, when a grandchild survived, is never. The
    bounded second wait is the difference between reporting a timeout and
    hanging on one.
    """
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", ""
    except (ValueError, OSError):
        return "", ""
    return (out or ""), (err or "")


def run_supervised(
    *,
    command_class: str,
    spec: dict,
    cwd: str,
    task_id: Optional[str] = None,
    timeout: Optional[int] = None,
    rollback_ref: Optional[str] = None,
    route_task: bool = True,
    conn: Optional[sqlite3.Connection] = None,
    policy: Optional[ExecutionPolicy] = None,
    capture_output: bool = True,
    env_extra: Optional[dict] = None,
) -> SupervisedResult:
    """Run a registered executor under durable ownership.

    Ordering, which is the whole contract:

    1. Policy check. Refusal happens before anything durable or live exists.
    2. Durable record, committed. The executor now has an identity and an
       owner before it has a PID.
    3. ``Popen`` in a new session; the PID, PGID and identity fingerprint are
       bound to the record.
    4. Bounded wait.
    5. ``finally``: settle. Every exit path — normal, timeout, exception,
       ``KeyboardInterrupt``, ``SystemExit`` — reaches this, so the record is
       terminal or explicitly supervisor-owned before the call returns. There
       is no path on which the caller returns and the child is unowned.

    A ``timeout`` above the policy's synchronous ceiling does not extend the
    wait: the execution is created ``supervisor``-owned instead, so the work
    that is *most* likely to outlive its caller is the work that never depended
    on the caller in the first place.
    """
    policy = policy or load_policy()
    launcher = LAUNCHERS.get(command_class)
    if launcher is None:
        raise ExecutionPolicyError(
            "command_class_unregistered",
            f"command class {command_class!r} has no registered launcher",
        )
    policy.check_executor(launcher.executor_type)
    policy.check_command_class(command_class)
    resolved_cwd = policy.resolve_root(cwd)
    argv = launcher.build(spec)
    effective_timeout = policy.resolve_max_runtime(launcher.executor_type, timeout)

    # Invariant 3. Work that could outlive the synchronous ceiling is created
    # supervisor-owned, so losing the caller is a recorded no-op rather than
    # the orphan-producing timeout path.
    ownership = (
        OWNERSHIP_SUPERVISOR
        if effective_timeout > policy.sync_ceiling_seconds
        else OWNERSHIP_CONTROLLER
    )

    controller_pid, controller_key, controller_token = controller_identity()

    owns_conn = conn is None
    conn = conn or kb.connect()
    try:
        record = create_execution(
            conn,
            executor_type=launcher.executor_type,
            command_class=command_class,
            cwd=str(resolved_cwd),
            task_id=task_id,
            controller_pid=controller_pid,
            controller_key=controller_key,
            controller_token=controller_token,
            ownership=ownership,
            max_runtime_s=effective_timeout,
            rollback_ref=rollback_ref,
            route_task=route_task,
        )
        execution_id = record.id

        proc: Optional[subprocess.Popen] = None
        proc_key: Optional[str] = None
        stdout = ""
        stderr = ""
        settled_status: Optional[str] = None
        settled_reason: Optional[str] = None
        exit_code: Optional[int] = None
        timed_out = False
        error: Optional[str] = None

        try:
            try:
                proc = _start_process(
                    argv,
                    cwd=str(resolved_cwd),
                    env=_child_env(execution_id, record.nonce, env_extra),
                    capture_output=capture_output,
                )
            except (OSError, ValueError) as exc:
                # The process never existed. The record does, and it settles
                # as a failure rather than being deleted: a refused-to-start
                # execution is a fact worth keeping.
                error = str(exc)
                settled_status = STATUS_FAILED
                settled_reason = "spawn_failed"
                raise _SpawnAborted() from exc

            proc_key = process_identity(proc.pid)
            try:
                pgid = os.getpgid(proc.pid)
            except (ProcessLookupError, OSError):
                pgid = proc.pid
            _attach_process(
                conn, execution_id, pid=proc.pid, pgid=pgid, proc_key=proc_key
            )

            try:
                stdout, stderr = proc.communicate(timeout=effective_timeout)
                stdout, stderr = (stdout or ""), (stderr or "")
                exit_code = proc.returncode
                settled_status = (
                    STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
                )
                settled_reason = None if exit_code == 0 else "nonzero_exit"
            except subprocess.TimeoutExpired:
                timed_out = True
                settled_status = STATUS_TIMED_OUT
                settled_reason = "timeout"
        except _SpawnAborted:
            pass
        except BaseException as exc:  # noqa: BLE001 - deliberate
            # Anything at all: an exception in the caller's own thread, a
            # KeyboardInterrupt, a SystemExit. The controller is losing
            # ownership, so the child must not be left running under it.
            error = f"{type(exc).__name__}: {exc}"
            settled_status = STATUS_CONTROLLER_LOST
            settled_reason = "controller_exception"
            raise
        finally:
            final = _finalise_controller_execution(
                conn,
                execution_id,
                proc=proc,
                proc_key=proc_key,
                status=settled_status,
                reason=settled_reason,
                exit_code=exit_code,
                policy=policy,
            )
            if final is not None:
                settled_status, settled_reason, exit_code, extra_out, extra_err = final
                stdout = stdout or extra_out
                stderr = stderr or extra_err

        return SupervisedResult(
            execution_id=execution_id,
            status=settled_status or STATUS_STALE,
            exit_code=exit_code,
            stdout=_truncate(stdout),
            stderr=_truncate(stderr),
            termination_reason=settled_reason,
            ownership=ownership,
            timed_out=timed_out,
            error=error,
        )
    finally:
        if owns_conn:
            with contextlib.suppress(Exception):
                conn.close()


class _SpawnAborted(Exception):
    """Internal: the child never started; skip straight to settling."""


def _finalise_controller_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    proc: Optional[subprocess.Popen],
    proc_key: Optional[str],
    status: Optional[str],
    reason: Optional[str],
    exit_code: Optional[int],
    policy: ExecutionPolicy,
) -> Optional[tuple[Optional[str], Optional[str], Optional[int], str, str]]:
    """The single place a controller-owned execution stops being owned.

    Reached from ``finally``, so it runs on every exit path including the ones
    the caller did not plan for. Two rules:

    * If the child is still alive, it is terminated as a group. Confirmed
      dead, not merely signalled.
    * If it cannot be confirmed dead, ownership transfers to the supervisor in
      the same transaction — the record is never left saying a controller owns
      a process the controller has stopped watching.
    """
    extra_out = extra_err = ""
    record = get_execution(conn, execution_id)
    if record is None:  # pragma: no cover - the row was just written
        return status, reason, exit_code, extra_out, extra_err

    if record.is_terminal:
        return record.status, record.termination_reason, record.exit_code, "", ""

    still_running = proc is not None and proc.poll() is None

    if still_running and record.ownership == OWNERSHIP_SUPERVISOR:
        # The caller was only ever a watcher here — the record has said
        # ``supervisor`` since before the process existed (invariant 3). Its
        # wait ending is not the job ending, so nothing is terminated and
        # nothing is settled; reconcile still holds the runtime cap. Returning
        # a non-terminal status is correct and is NOT an unowned state: the
        # owner is named in the row.
        _drain_out, _drain_err = "", ""
        _log.info(
            "execution %s: synchronous wait ended while the job is still "
            "running; it remains supervisor-owned",
            execution_id,
        )
        return (
            status or STATUS_RUNNING,
            "left_to_supervisor",
            None,
            _drain_out,
            _drain_err,
        )

    if still_running:
        outcome = terminate_process_group(
            pid=proc.pid,
            pgid=record.pgid,
            proc_key=proc_key or record.proc_key,
            grace_seconds=policy.terminate_grace_seconds,
        )
        extra_out, extra_err = _drain(proc, timeout=5.0)
        if not outcome.dead:
            # Could not confirm death. The controller is returning either way,
            # so the only acceptable move is to hand the live process to the
            # supervisor rather than let the caller walk away from it.
            if record.ownership == OWNERSHIP_CONTROLLER:
                _transfer_to_supervisor(
                    conn,
                    execution_id,
                    reason=f"controller could not confirm termination: {outcome.detail}",
                )
            _log.error(
                "execution %s: could not confirm termination (%s); ownership "
                "transferred to the supervisor for reconciliation",
                execution_id,
                outcome.detail,
            )
            settled = get_execution(conn, execution_id)
            return (
                settled.status if settled else STATUS_RUNNING,
                "adopted_by_supervisor",
                None,
                extra_out,
                extra_err,
            )
        reason = reason or "terminated_by_controller"
        if status is None:
            status = STATUS_TERMINATED
    elif proc is not None and status is None:
        # The child ended while we were unwinding; capture what it did rather
        # than inventing a status for it.
        exit_code = proc.returncode
        status = STATUS_COMPLETED if exit_code == 0 else STATUS_FAILED
        reason = None if exit_code == 0 else "nonzero_exit"

    _settle(
        conn,
        execution_id,
        status=status or STATUS_STALE,
        exit_code=exit_code,
        reason=reason,
    )
    settled = get_execution(conn, execution_id)
    if settled is not None and settled.task_id:
        route_task_from_execution(conn, settled)
    return status, reason, exit_code, extra_out, extra_err


# ---------------------------------------------------------------------------
# Tracked background execution
# ---------------------------------------------------------------------------

#: Wrapper that runs the real argv and records its exit status in a sidecar
#: file. Without it a background job reparented to init leaves no exit code
#: behind and reconciliation can only ever call it ``stale``; with it the
#: reconciler can report what actually happened (``recovered``). Deliberately
#: tiny, no shell, and it never sees a credential the child would not.
_BACKGROUND_WRAPPER = r"""
import os, subprocess, sys
sidecar = sys.argv[1]
argv = sys.argv[2:]
rc = 127
try:
    rc = subprocess.call(argv)
finally:
    try:
        tmp = sidecar + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(str(rc))
        os.replace(tmp, sidecar)
    except Exception:
        pass
sys.exit(rc)
"""


def _sidecar_path(execution_id: str) -> Path:
    root = kb.kanban_db_path().parent / "executions"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{execution_id}.exit"


def read_sidecar_exit(execution_id: str) -> Optional[int]:
    path = _sidecar_path(execution_id)
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def launch_background(
    *,
    command_class: str,
    spec: dict,
    cwd: str,
    task_id: Optional[str] = None,
    max_runtime: Optional[int] = None,
    rollback_ref: Optional[str] = None,
    route_task: bool = True,
    conn: Optional[sqlite3.Connection] = None,
    policy: Optional[ExecutionPolicy] = None,
    env_extra: Optional[dict] = None,
) -> ExecutionRecord:
    """Start a supervisor-owned job and return immediately.

    The caller is explicitly NOT the owner: the record says ``supervisor``
    from the moment it is written, so there is no handover to get wrong and
    nothing about the job's fate depends on the launching process surviving.
    This is the sanctioned route for anything that might exceed the
    synchronous ceiling — not a synchronous call with a bigger number.
    """
    policy = policy or load_policy()
    launcher = LAUNCHERS.get(command_class)
    if launcher is None:
        raise ExecutionPolicyError(
            "command_class_unregistered",
            f"command class {command_class!r} has no registered launcher",
        )
    policy.check_executor(launcher.executor_type)
    policy.check_command_class(command_class)
    resolved_cwd = policy.resolve_root(cwd)
    argv = launcher.build(spec)
    effective_max = policy.resolve_max_runtime(launcher.executor_type, max_runtime)

    controller_pid, controller_key, controller_token = controller_identity()

    owns_conn = conn is None
    conn = conn or kb.connect()
    try:
        record = create_execution(
            conn,
            executor_type=launcher.executor_type,
            command_class=command_class,
            cwd=str(resolved_cwd),
            task_id=task_id,
            controller_pid=controller_pid,
            controller_key=controller_key,
            controller_token=controller_token,
            ownership=OWNERSHIP_SUPERVISOR,
            max_runtime_s=effective_max,
            rollback_ref=rollback_ref,
            route_task=route_task,
        )
        sidecar = _sidecar_path(record.id)
        with contextlib.suppress(OSError):
            sidecar.unlink()
        wrapped = [sys.executable, "-c", _BACKGROUND_WRAPPER, str(sidecar), *argv]
        try:
            proc = _start_process(
                wrapped,
                cwd=str(resolved_cwd),
                env=_child_env(record.id, record.nonce, env_extra),
                capture_output=False,
            )
        except (OSError, ValueError) as exc:
            _settle(
                conn,
                record.id,
                status=STATUS_FAILED,
                exit_code=None,
                reason="spawn_failed",
            )
            raise ExecutionPolicyError(
                "spawn_failed", f"could not start background execution: {exc}"
            ) from exc
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = proc.pid
        _attach_process(
            conn,
            record.id,
            pid=proc.pid,
            pgid=pgid,
            proc_key=process_identity(proc.pid),
        )
        refreshed = get_execution(conn, record.id)
        return refreshed or record
    finally:
        if owns_conn:
            with contextlib.suppress(Exception):
                conn.close()


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """What one reconciliation pass did. Every id appears in exactly one list."""

    checked: int = 0
    completed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    controller_lost: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)

    @property
    def settled(self) -> list[str]:
        return (
            self.completed
            + self.failed
            + self.timed_out
            + self.controller_lost
            + self.stale
            + self.terminated
            + self.recovered
        )

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "completed": self.completed,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "controller_lost": self.controller_lost,
            "stale": self.stale,
            "terminated": self.terminated,
            "recovered": self.recovered,
            "adopted": self.adopted,
            "untouched": self.untouched,
        }


def _controller_alive(record: ExecutionRecord) -> bool:
    """Whether the controller that owned this execution still exists.

    Same identity discipline as the executor: with a recorded fingerprint the
    comparison is exact, so a controller PID recycled by an unrelated process
    reads as dead (which it is) rather than alive (which would strand the
    child).
    """
    if not record.controller_pid:
        return False
    if record.controller_key:
        return identity_matches(record.controller_pid, record.controller_key)
    return _fallback_alive(record.controller_pid)


def reconcile(
    conn: Optional[sqlite3.Connection] = None,
    *,
    policy: Optional[ExecutionPolicy] = None,
    now: Optional[int] = None,
) -> ReconcileResult:
    """Resolve every non-terminal execution deterministically.

    Run at supervisor startup and on the dispatcher's normal tick. The rules,
    in order, for each ``launching``/``running`` row:

    1. **Identity mismatch** — the PID now belongs to something else. The
       executor is gone and was never observed exiting: ``stale`` /
       ``pid_reused``. The live process is deliberately NOT signalled.
    2. **Process gone** — with a sidecar exit code, ``recovered`` carrying
       that code; without one, ``controller_lost`` if the controller owned it
       and is also gone, else ``stale``.
    3. **Runtime cap exceeded** — terminate the group, ``timed_out``.
    4. **Controller-owned, controller dead** — a live orphan. Per
       ``execution.orphan_policy``: terminate it (``controller_lost``) or adopt
       it into supervisor ownership. Never left as it was.
    5. **Stale heartbeat** — no owner has reported progress within
       ``execution.stale_heartbeat_seconds``: terminate the group, ``stale``.
    6. Otherwise the execution is healthy and owned; only
       ``last_observed_at`` is stamped. The heartbeat is never advanced here.
    """
    policy = policy or load_policy()
    ts = now if now is not None else _now()
    result = ReconcileResult()

    owns_conn = conn is None
    conn = conn or kb.connect()
    try:
        placeholders = ",".join("?" * len(ACTIVE_STATUSES))
        rows = conn.execute(
            f"SELECT * FROM executions WHERE status IN ({placeholders}) "
            f"ORDER BY started_at ASC",
            ACTIVE_STATUSES,
        ).fetchall()
        for row in rows:
            record = _row_to_record(row)
            result.checked += 1
            _reconcile_one(conn, record, policy=policy, now=ts, result=result)
        return result
    finally:
        if owns_conn:
            with contextlib.suppress(Exception):
                conn.close()


def _observe(conn: sqlite3.Connection, execution_id: str, ts: int) -> None:
    """Stamp that reconciliation looked at this row. Never the heartbeat."""
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE executions SET last_observed_at = ? WHERE id = ?",
            (ts, execution_id),
        )


def _finish(
    conn: sqlite3.Connection,
    record: ExecutionRecord,
    result: ReconcileResult,
    *,
    status: str,
    reason: str,
    exit_code: Optional[int] = None,
    now: Optional[int] = None,
) -> None:
    if not _settle(
        conn, record.id, status=status, exit_code=exit_code, reason=reason, now=now
    ):
        # Another writer settled it first. That is the CAS working, not an
        # error: report it as untouched rather than double-counting.
        result.untouched.append(record.id)
        return
    getattr(result, status).append(record.id)
    settled = get_execution(conn, record.id)
    if settled is not None and settled.task_id:
        route_task_from_execution(conn, settled)


def _reconcile_one(
    conn: sqlite3.Connection,
    record: ExecutionRecord,
    *,
    policy: ExecutionPolicy,
    now: int,
    result: ReconcileResult,
) -> None:
    pid = record.pid

    if record.status == STATUS_LAUNCHING and pid is None:
        # A record written but never attached to a process. If the writer is
        # gone, nothing will ever attach one.
        if _controller_alive(record):
            _observe(conn, record.id, now)
            result.untouched.append(record.id)
        else:
            _finish(
                conn,
                record,
                result,
                status=STATUS_STALE,
                reason="never_started",
                now=now,
            )
        return

    live_identity = process_identity(pid) if pid else None
    if record.proc_key and live_identity is not None and live_identity != record.proc_key:
        # Rule 1. The number was recycled. Do not touch whatever owns it now.
        _finish(
            conn, record, result, status=STATUS_STALE, reason="pid_reused", now=now
        )
        return

    alive = _still_alive(pid, record.proc_key)
    if not alive:
        # Rule 2.
        sidecar_rc = read_sidecar_exit(record.id)
        if sidecar_rc is not None:
            _finish(
                conn,
                record,
                result,
                status=STATUS_RECOVERED,
                reason="exit_recovered_from_sidecar",
                exit_code=sidecar_rc,
                now=now,
            )
            return
        if record.ownership == OWNERSHIP_CONTROLLER and not _controller_alive(record):
            _finish(
                conn,
                record,
                result,
                status=STATUS_CONTROLLER_LOST,
                reason="controller_and_executor_both_gone",
                now=now,
            )
            return
        _finish(
            conn, record, result, status=STATUS_STALE, reason="process_gone", now=now
        )
        return

    # From here the executor is confirmed alive and confirmed to be ours.
    runtime = record.runtime_seconds(now)
    cap = record.max_runtime_s or policy.max_runtime_seconds
    if cap and runtime > cap:
        # Rule 3.
        outcome = terminate_process_group(
            pid=pid,
            pgid=record.pgid,
            proc_key=record.proc_key,
            grace_seconds=policy.terminate_grace_seconds,
        )
        _finish(
            conn,
            record,
            result,
            status=STATUS_TIMED_OUT,
            reason=(
                f"runtime_cap_exceeded ({runtime}s > {cap}s); {outcome.detail}"
            ),
            now=now,
        )
        return

    if record.ownership == OWNERSHIP_CONTROLLER and not _controller_alive(record):
        # Rule 4 — a live orphan. Terminate or adopt; never both, never neither.
        if policy.orphan_policy == ORPHAN_POLICY_ADOPT:
            if _transfer_to_supervisor(
                conn, record.id, reason="controller_dead_adopted_by_policy"
            ):
                result.adopted.append(record.id)
                _observe(conn, record.id, now)
            else:
                result.untouched.append(record.id)
            return
        outcome = terminate_process_group(
            pid=pid,
            pgid=record.pgid,
            proc_key=record.proc_key,
            grace_seconds=policy.terminate_grace_seconds,
        )
        if not outcome.dead:
            # Refused to die. Adopt rather than leave it unowned; the runtime
            # cap will come back for it on a later pass.
            if _transfer_to_supervisor(
                conn,
                record.id,
                reason=f"orphan survived termination: {outcome.detail}",
            ):
                result.adopted.append(record.id)
            else:
                result.untouched.append(record.id)
            _observe(conn, record.id, now)
            return
        _finish(
            conn,
            record,
            result,
            status=STATUS_CONTROLLER_LOST,
            reason=f"controller_dead; {outcome.detail}",
            now=now,
        )
        return

    stale_after = policy.stale_heartbeat_seconds
    if stale_after and (now - (record.heartbeat_at or record.started_at)) > stale_after:
        # Rule 5.
        outcome = terminate_process_group(
            pid=pid,
            pgid=record.pgid,
            proc_key=record.proc_key,
            grace_seconds=policy.terminate_grace_seconds,
        )
        if not outcome.dead:
            if _transfer_to_supervisor(
                conn, record.id, reason=f"stale executor survived termination: {outcome.detail}"
            ):
                result.adopted.append(record.id)
            else:
                result.untouched.append(record.id)
            _observe(conn, record.id, now)
            return
        _finish(
            conn,
            record,
            result,
            status=STATUS_STALE,
            reason=f"stale_heartbeat; {outcome.detail}",
            now=now,
        )
        return

    # Rule 6: healthy and owned.
    _observe(conn, record.id, now)
    result.untouched.append(record.id)


def terminate_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    reason: str = "operator_request",
    policy: Optional[ExecutionPolicy] = None,
) -> ExecutionRecord:
    """Operator termination. Ends the group and settles the record."""
    policy = policy or load_policy()
    record = get_execution(conn, execution_id)
    if record is None:
        raise ExecutionNotFound(f"no execution {execution_id!r} on this board")
    if record.is_terminal:
        return record
    outcome = terminate_process_group(
        pid=record.pid,
        pgid=record.pgid,
        proc_key=record.proc_key,
        grace_seconds=policy.terminate_grace_seconds,
    )
    if not outcome.dead:
        _transfer_to_supervisor(
            conn, execution_id, reason=f"operator terminate failed: {outcome.detail}"
        )
        refreshed = get_execution(conn, execution_id)
        return refreshed or record
    _settle(
        conn,
        execution_id,
        status=STATUS_TERMINATED,
        exit_code=None,
        reason=f"{reason}; {outcome.detail}",
    )
    settled = get_execution(conn, execution_id)
    if settled is not None and settled.task_id:
        route_task_from_execution(conn, settled)
    return settled or record


# ---------------------------------------------------------------------------
# Gauntlet integration
# ---------------------------------------------------------------------------

#: Recorded on the task when an execution ends. Both are lifecycle progress in
#: the freshness scan's sense — something genuinely happened to the card — so
#: neither belongs on the non-progress denylist.
EVENT_EXECUTION_FINISHED = "execution_finished"
EVENT_EXECUTION_FAILED = "execution_failed"


def route_task_from_execution(
    conn: sqlite3.Connection, record: ExecutionRecord
) -> Optional[str]:
    """Feed a settled execution into the Gauntlet lifecycle.

    The one rule that matters: **an executor finishing is not a completion.**
    A clean exit hands the card to VERIFICATION_PENDING and nothing here ever
    calls ``complete_task``; only a verifier's PASS verdict can do that, and on
    a repaired card only with regression evidence. Every non-success —
    ``failed``, ``timed_out``, ``controller_lost``, ``stale``, ``terminated`` —
    routes into recovery/rework, so a lost controller can never be laundered
    into progress.

    Returns the routing decision as a short string, or None when the execution
    has no task to route.
    """
    if not record.task_id:
        return None
    if not record.route_task:
        # The launching caller owns this card's Gauntlet handoff (see the
        # ``route_task`` note in the schema). Routing here as well would race
        # it out of 'running' and refuse the richer handoff it is about to
        # make. Nothing is skipped: the completion guard is in the kernel.
        return "routing_owned_by_caller"
    task = kb.get_task(conn, record.task_id)
    if task is None:
        return None

    if record.status in SUCCESS_STATUSES and record.exit_code == 0:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                record.task_id,
                EVENT_EXECUTION_FINISHED,
                {
                    "execution_id": record.id,
                    "executor_type": record.executor_type,
                    "exit_code": record.exit_code,
                    "status": record.status,
                    "rollback_ref": record.rollback_ref,
                },
            )
        if not kb.gauntlet_required(conn, record.task_id):
            # Non-Gauntlet card: the executor's exit is recorded and the
            # existing lifecycle owns what happens next. Deliberately no
            # completion from here either — this module never closes a card.
            return "recorded"
        if task.status == "running":
            ok = kb.request_review(
                conn,
                record.task_id,
                summary=(
                    f"Execution {record.id} ({record.executor_type}) exited 0."
                ),
                metadata={
                    "execution_id": record.id,
                    "executor_type": record.executor_type,
                    "command_class": record.command_class,
                    "exit_code": record.exit_code,
                    "rollback_ref": record.rollback_ref,
                },
                expected_run_id=task.current_run_id,
                force=task.current_run_id is None,
            )
            return "verification_pending" if ok else "handoff_refused"
        return "recorded"

    # Every non-success. Recorded first so the reason survives whatever the
    # failure machinery decides to do with the card.
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            record.task_id,
            EVENT_EXECUTION_FAILED,
            {
                "execution_id": record.id,
                "executor_type": record.executor_type,
                "status": record.status,
                "exit_code": record.exit_code,
                "termination_reason": record.termination_reason,
            },
        )
    outcome_map = {
        STATUS_TIMED_OUT: "timed_out",
        STATUS_CONTROLLER_LOST: "crashed",
        STATUS_STALE: "crashed",
        STATUS_TERMINATED: "crashed",
        STATUS_FAILED: "crashed",
        STATUS_RECOVERED: "crashed",
    }
    outcome = outcome_map.get(record.status, "crashed")
    try:
        kb._record_task_failure(
            conn,
            record.task_id,
            f"execution {record.id} ended {record.status}"
            + (f" ({record.termination_reason})" if record.termination_reason else ""),
            outcome=outcome,
            event_payload_extra={
                "execution_id": record.id,
                "execution_status": record.status,
            },
        )
    except Exception:
        _log.exception(
            "execution %s: could not route task %s into recovery",
            record.id,
            record.task_id,
        )
        return "recovery_routing_failed"
    return "recovery"


# ---------------------------------------------------------------------------
# Operator surface helpers
# ---------------------------------------------------------------------------


def describe(conn: sqlite3.Connection, record: ExecutionRecord, *, now: Optional[int] = None) -> dict:
    """The operator's questions, answered from the record.

    Deliberately answers "whether the controller is alive" and "whether it is
    inside policy" as computed facts rather than stored ones: a stored answer
    to a liveness question is stale by definition.
    """
    ts = now if now is not None else _now()
    policy = load_policy()
    cap = record.max_runtime_s or policy.max_runtime_seconds
    runtime = record.runtime_seconds(ts)
    within_policy = True
    policy_notes: list[str] = []
    try:
        policy.check_executor(record.executor_type)
    except ExecutionPolicyError as exc:
        within_policy = False
        policy_notes.append(exc.code)
    try:
        policy.check_command_class(record.command_class)
    except ExecutionPolicyError as exc:
        within_policy = False
        policy_notes.append(exc.code)
    try:
        policy.resolve_root(record.cwd)
    except ExecutionPolicyError as exc:
        within_policy = False
        policy_notes.append(exc.code)
    if cap and runtime > cap and not record.is_terminal:
        within_policy = False
        policy_notes.append("runtime_cap_exceeded")

    data = record.to_dict()
    data.update(
        {
            "runtime_seconds": runtime,
            "seconds_since_heartbeat": (
                max(0, ts - record.heartbeat_at) if record.heartbeat_at else None
            ),
            "controller_alive": _controller_alive(record) if not record.is_terminal else False,
            "executor_alive": (
                _still_alive(record.pid, record.proc_key) if not record.is_terminal else False
            ),
            "within_policy": within_policy,
            "policy_notes": policy_notes,
            "terminal": record.is_terminal,
        }
    )
    return data
