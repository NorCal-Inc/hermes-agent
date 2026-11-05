"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` (pins the
file path) > ``<root>/kanban/current`` > ``default`` — but only for unfenced callers; the dispatcher
injects these into workers, and dispatched workers (``HERMES_KANBAN_TASK``), delegated children and
board-enumerating machine flows (gateway notifier/watcher/dispatcher, ``pin_first_board_resolution``)
always resolve through the pin, so workers physically cannot see other boards.
One guard on ``HERMES_KANBAN_DB``: a pin under the platform-native root is ignored when the
active kanban home resolves outside that root, so an inherited dispatcher pin cannot drag a
deliberately re-homed process back to the live board (incident 2026-10-12; see
:func:`_inherited_db_pin_is_foreign`).
Concurrency: WAL + ``BEGIN IMMEDIATE`` + compare-and-swap on ``tasks.status``/``claim_lock`` —
SQLite serializes writers so one claimer wins, losers see zero rows (no retries, no distributed
locks). Schema: tasks, task_links, task_comments, task_events, task_runs, attachments, notify subs.
"""
from __future__ import annotations
import contextlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from hermes_cli.kanban_workflow import DEFAULT_STATUSES as VALID_STATUSES
from toolsets import get_toolset_names
import errno
import hashlib
import random
import shutil
import tempfile
import threading
from dataclasses import field
from typing import Mapping
from hermes_cli.sqlite_util import add_column_if_missing as _add_column_if_missing
_log = logging.getLogger(__name__)


def _lossy_text(value: Any) -> Any:
    """``bytes`` -> ``str`` with U+FFFD for undecodable sequences; anything else passes through.

    Installed as every board connection's ``text_factory`` (a TEXT cell holding
    invalid UTF-8 otherwise aborts the whole ``fetchall`` with "Could not decode
    to UTF-8") and applied to BLOB-typed cells in the ``from_row`` constructors
    (task, comment, event, run), so one
    corrupt row degrades to replacement characters instead of taking the board
    listing down."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _row_get(row: Any, col: str, default: Any = None) -> Any:
    """``row[col]`` tolerant of the column being absent from the SELECT / schema."""
    if row is None or col not in row.keys():
        return default
    return row[col]


def _json_or(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column; any decode failure or empty value yields ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    """Decode a JSON text column that must be an object; anything else yields ``{}``."""
    parsed = _json_or(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer env override: absent/empty/non-integer/below ``minimum`` falls back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return default
        if parsed >= minimum:
            return parsed
    return default


def _git_out(cwd: Path, *args: str, timeout: int = 30) -> Optional[str]:
    """Run ``git -C cwd args`` and return stripped stdout, or ``None`` on any failure / empty output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


VALID_INITIAL_STATUSES = {"running", "blocked"}
KANBAN_VERIFIER_RETURN_ATTENTION_EVENT_KINDS = (
    "verifier_verdict_unattested",
    "verification_blocker_returned",
    "verifier_verdict_unreadable",
    "verified_completion_deferred",
)
KANBAN_NOTIFY_EVENT_KINDS = (
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "status", "archived", "unblocked", "block_loop_detected",
    "review_requested", "linked_task_gave_up", "verification_failed",
    "notification_delivery_failed", "verification_acceptance_missing",
) + KANBAN_VERIFIER_RETURN_ATTENTION_EVENT_KINDS + (
    # Repair loop (t_2bc18772): one "error caught, beginning repair loop"
    # notice when a repair card exists, one "repaired" notice when the
    # subject resumes, and a needs-you notice if the repair could not start.
    "repair_started", "repair_start_failed",
    "repair_completed_subject_resumed",
    "standalone_review_fail_routed_to_repair",
)
KANBAN_ACTIVE_WAKE_EVENT_KINDS = frozenset({
    "completed", "blocked", "gave_up", "crashed", "timed_out",
    "block_loop_detected", "review_requested", "linked_task_gave_up",
    "verification_failed", "notification_delivery_failed",
    "verification_acceptance_missing", "repair_start_failed",
}) | frozenset(KANBAN_VERIFIER_RETURN_ATTENTION_EVENT_KINDS)
KANBAN_REPAIR_SUPERSEDED_EVENT_KINDS = frozenset({"verification_failed", "gave_up"})
KANBAN_REPAIR_LOOP_START_EVENT_KINDS = ("repair_started", "repair_start_failed")
assert KANBAN_ACTIVE_WAKE_EVENT_KINDS.issubset(KANBAN_NOTIFY_EVENT_KINDS)
VERIFICATION_PENDING = "pending"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_FAILED = "failed"
VALID_VERIFICATION_STATES = {
    VERIFICATION_PENDING,
    VERIFICATION_VERIFIED,
    VERIFICATION_FAILED,
}
LEDGER_KIND_VERDICT = "verdict"
LEDGER_KIND_REGRESSION = "regression"
VERIFICATION_REGRESSION = "regression"
VALID_LEDGER_STATES = VALID_VERIFICATION_STATES | {VERIFICATION_REGRESSION}
REGRESSION_EVIDENCE_KEYS = (
    "command",
    "commands",
    "test",
    "tests",
    "suite",
    "suites",
    "check",
    "checks",
)
LESSON_SCOPE_TENANT = "tenant"
LESSON_SCOPE_GLOBAL = "global"
VALID_LESSON_SCOPES = {LESSON_SCOPE_TENANT, LESSON_SCOPE_GLOBAL}
LESSON_SELECTOR_ALL = "all"
LESSON_SELECTOR_DIMENSIONS = {
    # dimension  -> Task attribute it is read from
    "assignee": "assignee",
    "project": "project_id",
    "workspace": "workspace_kind",
    "lane": "executor_lane",
    "template": "workflow_template_id",
    "tenant": "tenant",
}
_CTX_MAX_LESSONS = 25
_CTX_MAX_LESSON_BYTES = 2 * 1024
LESSON_MAX_CHARS = 4000
GAUNTLET_STALE_STATUSES = ("review", "ready", "todo", "blocked")
YT_SOURCE_WAIT_PREFIX = "waiting for yt_source delivery of "
GAUNTLET_STALE_EVENT = "gauntlet_stale"
RESPAWN_GUARD_EVENT_REPEAT_SECONDS = 1800


def _record_respawn_guarded(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    last = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? "
        "AND kind = 'respawn_guarded' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if last is not None and int(time.time()) - int(last["created_at"] or 0) < RESPAWN_GUARD_EVENT_REPEAT_SECONDS:
        try:
            previous = json.loads(last["payload"] or "{}").get("reason")
        except (ValueError, AttributeError):
            previous = None
        if previous == reason:
            return
    with write_txn(conn):
        _append_event(conn, task_id, "respawn_guarded", {"reason": reason})


_GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS = (
    GAUNTLET_STALE_EVENT,
    "respawn_guarded",
    "reclaim_deferred",
    "claim_rejected",
    "completion_blocked_unverified",
    "completion_blocked_no_regression",
    "completion_blocked_hallucination",
    "verification_blocked_no_regression",
    "lesson_promotion_blocked",
    "suspected_hallucinated_references",
    # Execution-supervisor refusals. A launch the policy refused, or an
    # execution the reconciler could not settle, recurs for exactly as long as
    # nothing is being fixed — counting it as progress would make an
    # abandoned card look freshest at the moment it is most abandoned. The
    # supervisor's *settling* events (``execution_finished`` /
    # ``execution_failed``) are deliberately absent from this list: those mean
    # something really happened to the card.
    "execution_refused",
    "execution_reconcile_failed",
    # Append-only history refusal (execution contract rule 8): a delete the
    # gate turned away is not someone working the card.
    "history_delete_refused",
    # Same rule, attachment surface: an ``attach-rm`` the gate turned away
    # because the card is still live is a refusal, not progress.
    "attachment_delete_refused",
    # Company-lane refusal (execution contract rule 3). The dispatcher
    # re-evaluates a misrouted card every tick; the record is written once
    # per distinct conflict, but a refusal is still not someone working
    # the card.
    "claim_refused_tenant_conflict",
    # Supervisory alarm bookkeeping. Every one of these is written BY the
    # watchdogs that read this clock, so counting them as progress would make
    # each alarm silence its own successor: the card would look freshly touched
    # for exactly as long as nobody was touching it. ``observation_timer_armed``
    # and ``observation_tick`` are here for the same reason — arming the
    # 300-second recheck that watches a stalled card must not reset the
    # staleness the recheck exists to watch.
    "NO_LIFECYCLE_PROGRESS",
    "ORPHAN_VERIFIER_RESULT",
    "supervisory_alarm_routed",
    "supervisory_alarm_undeliverable",
    "lifecycle_stall_cleared",
    "observation_timer_armed",
    "observation_tick",
    # Stale supervision declining to release an owner-held park changes
    # nothing on the card, so it must not make the card look freshly moved.
    "infrastructure_release_withheld",
    # Supervision's own refusal/alarm/timer bookkeeping (2026-09-14). At a
    # 300-second evaluation cadence these were written on every tick for an
    # evidence-less review card, each one reset the stall episode, and the
    # card re-alarmed every five minutes whatever the re-alert window said.
    "independent_verification_unroutable",
    "INDEPENDENT_VERIFICATION_UNROUTABLE",
    "observation_timer_closed",
    "supervisory_alarm_delivery_failed",
)
ORPHAN_VERIFIER_RESULT_EVENT = "ORPHAN_VERIFIER_RESULT"
NO_LIFECYCLE_PROGRESS_EVENT = "NO_LIFECYCLE_PROGRESS"
SUPERVISORY_ALARM_ROUTED_EVENT = "supervisory_alarm_routed"
SUPERVISORY_ALARM_UNDELIVERABLE_EVENT = "supervisory_alarm_undeliverable"
LIFECYCLE_STALL_CLEARED_EVENT = "lifecycle_stall_cleared"
LIFECYCLE_STALL_TIMER_KIND = "lifecycle_stall_recheck"
UNROUTABLE_VERIFICATION_TIMER_KIND = "independent_verification_recheck"
SUPERVISORY_PROFILE_DEFAULT = "erika"
DEFAULT_GAUNTLET_STALE_TIMEOUT_SECONDS = 14400
DEFAULT_GAUNTLET_STALE_REALERT_SECONDS = 14400
INFRASTRUCTURE_AUTO_RELEASE_WINDOW_SECONDS = 900
AUTOMATION_COMMENT_AUTHORS = frozenset({"stale-supervision", "system-health-controller"})
BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
BLOCK_KIND_ERROR_RECURRED = "error_recurred"
REPEATED_ERROR_LIMIT_DEFAULT = 3
IMPLEMENTATION_FAILURE_OUTCOMES = frozenset({"crashed", "gave_up"})
VALID_BLOCK_KINDS = {
    "dependency", "needs_input", "approval_required", "capability", "transient",
    BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED,
    "runtime_cap_exhausted",
}
BLOCK_RECURRENCE_LIMIT = 2
# Bounded automatic retry for an independent verifier whose own execution tool
# crashed (implementation/tooling-class failure -- no verdict produced, not a
# FAIL). Total attempts allowed before the card is parked ``blocked`` for an
# operator; see ``verifier_execution_crash_retry`` (2026-11-04, t_c645f27d /
# t_820c2104: every crash previously went straight to ``blocked`` on the FIRST
# attempt with zero retries).
VERIFIER_EXECUTION_CRASH_RETRY_LIMIT = 3
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
EXECUTOR_LANE_CLAUDE = "claude"
EXECUTOR_LANE_CODEX_VERIFY = "codex_verify"
EXECUTOR_LANE_CLAUDE_VERIFY = "claude_verify"
EXECUTOR_LANE_CLAUDE_RECOVERY = "claude_recovery"
VALID_EXECUTOR_LANES = {
    EXECUTOR_LANE_CLAUDE,
    EXECUTOR_LANE_CODEX_VERIFY,
    EXECUTOR_LANE_CLAUDE_VERIFY,
    EXECUTOR_LANE_CLAUDE_RECOVERY,
}
INDEPENDENT_VERIFIER_LANES: frozenset[str] = frozenset(
    {EXECUTOR_LANE_CLAUDE_VERIFY, EXECUTOR_LANE_CODEX_VERIFY}
)
DEFAULT_INDEPENDENT_VERIFIER_LANE = EXECUTOR_LANE_CLAUDE_VERIFY
ACTOR_KIND_HUMAN_INTERACTIVE = "human_interactive"
ACTOR_KIND_GOVERNED_AUTOMATION = "governed_automation"
ACTOR_KIND_SYSTEM = "system"
ACTOR_KIND_UNKNOWN = "unknown"
VALID_ACTOR_KINDS = {
    ACTOR_KIND_HUMAN_INTERACTIVE,
    ACTOR_KIND_GOVERNED_AUTOMATION,
    ACTOR_KIND_SYSTEM,
    ACTOR_KIND_UNKNOWN,
}
HUMAN_IN_LOOP_ACTOR_KINDS = frozenset({ACTOR_KIND_HUMAN_INTERACTIVE})
CREATION_CAUSE_MANUAL_RELAY = "manual_relay"
CREATION_CAUSE_AUTOMATED = "automated"
CREATION_CAUSE_RECOVERY = "recovery"
CREATION_CAUSE_VERIFICATION = "verification"
CREATION_CAUSE_UNKNOWN = "unknown"
CREATION_CAUSE_RECOVERY_PENDING = "recovery_pending"
VALID_CREATION_CAUSES = {
    CREATION_CAUSE_MANUAL_RELAY,
    CREATION_CAUSE_AUTOMATED,
    CREATION_CAUSE_RECOVERY,
    CREATION_CAUSE_RECOVERY_PENDING,
    CREATION_CAUSE_VERIFICATION,
    CREATION_CAUSE_UNKNOWN,
}
ENV_ACTOR_KIND = "HERMES_ACTOR_KIND"
ENV_ACTOR_ID = "HERMES_ACTOR_ID"
RELATION_REPAIRS = "repairs"
RELATION_UMBRELLA = "umbrella"
RELATION_EVIDENCE = "evidence"
RELATION_VERIFIES = "verifies"
VALID_TASK_RELATIONS = {
    RELATION_REPAIRS,
    RELATION_UMBRELLA,
    RELATION_EVIDENCE,
    RELATION_VERIFIES,
}
REQUIRED_REPAIR_RELATIONS = (RELATION_REPAIRS, RELATION_UMBRELLA)
VALIDATION_LOOP_TIMER_KIND = "validation_loop_recheck"
MANDATORY_OBSERVATION_INTERVAL_SECONDS = 300
RUNTIME_CAP_LADDER = (300, 600, 900)
RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG = 2
RUNTIME_CAP_LADDER_VERIFIER_CEILING = 600
BLOCK_KIND_RUNTIME_CAP_EXHAUSTED = "runtime_cap_exhausted"
OBSERVATION_STATE_OBSERVING = "observing"
OBSERVATION_STATE_CLOSED = "closed"
DISPOSITION_COMPLETED = "completed"
DISPOSITION_OVERTAKEN_BY_EVENTS = "overtaken_by_events"
DISPOSITION_ABANDONED = "abandoned"
VALID_TERMINAL_DISPOSITIONS = {
    DISPOSITION_COMPLETED,
    DISPOSITION_OVERTAKEN_BY_EVENTS,
    DISPOSITION_ABANDONED,
}
IRREVERSIBLE_DISPOSITIONS = frozenset({DISPOSITION_OVERTAKEN_BY_EVENTS})
_IRREVERSIBLE_DISPOSITION_SQL_LIST = ", ".join(
    f"'{d}'" for d in sorted(IRREVERSIBLE_DISPOSITIONS)
)


def _not_irreversibly_disposed_sql(alias: str = "") -> str:
    """SQL predicate: this row may still enter (or re-enter) work.

    ``alias`` qualifies the column for queries that join ``tasks`` under a
    name (``"p"`` for the parent-gating queries). The ``IS NULL`` arm is
    required, not decorative: under SQL three-valued logic a bare
    ``NOT IN`` over a NULL column evaluates to NULL, which would silently
    drop every ordinary card from the result.
    """
    col = f"{alias}.terminal_disposition" if alias else "terminal_disposition"
    return (
        f"({col} IS NULL "
        f"OR {col} NOT IN ({_IRREVERSIBLE_DISPOSITION_SQL_LIST}))"
    )


RECONCILABLE_STATUSES = {
    "triage", "todo", "scheduled", "ready", "blocked", "review", "archived",
}


def normalize_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """``VALID_REASONING_EFFORTS`` or ``"none"`` (thinking off), case-insensitive;
    empty/None = inherit the profile's own effort (NULL). Anything else raises —
    a typo'd level must not quietly hand the task back to the profile default."""
    from hermes_constants import VALID_REASONING_EFFORTS

    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "none" or value in VALID_REASONING_EFFORTS:
        return value
    allowed = ", ".join(("none", *VALID_REASONING_EFFORTS))
    raise ValueError(f"reasoning_effort must be one of {allowed}, got {effort!r}")


KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # one cap for dashboard, tools and CLI


def _assert_not_delegated_child_mutation(path: str | Path | None = None) -> None:
    """Reject Kanban mutations from ``delegate_task`` child contexts.

    The tool/CLI fast-fail guards are UX, not a trust boundary (a child can shell
    out or import this module); the invariant lives here so every ``write_txn``
    user and board-metadata mutator fails closed before touching durable state.
    *path* is the board DB / metadata root being mutated; ``None`` means the
    lineage's own board (``kanban_home()``).
    """
    from agent.delegation_context import kanban_path_is_fenced

    if kanban_path_is_fenced(kanban_home() if path is None else path):
        raise PermissionError("delegate_task child contexts cannot mutate Kanban tasks or boards")


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Best-effort lifecycle hook. Call AFTER the write txn commits (plugins never
    run under the SQLite write lock, always see durable state); failures are
    swallowed so an observer can never break a transition."""
    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


def _fire_task_hook(event: str, task: Optional[Task], task_id: str, run_id: Optional[int], **fields: Any) -> None:
    """Lifecycle hook for a task transition; ``assignee`` from the (possibly missing) row."""
    _fire_kanban_lifecycle_hook(
        event, task_id, board=get_current_board(),
        assignee=task.assignee if task else None, run_id=run_id, **fields,
    )


def _hook_profile_name() -> str:
    """Active profile for hook payloads; ``"default"`` when it cannot be resolved."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return get_active_profile_name()
    except Exception:
        return "default"


def _kanban_observer_consumed(event: str) -> bool:
    """Hot-path short-circuit: skip payload assembly when nothing subscribes.
    Inspection failure counts as unconsumed (dropping an observer is always safe)."""
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook(event)
    except Exception:  # pragma: no cover - defensive
        return False


def _fire_worker_spawned_hook(
    conn: sqlite3.Connection, task: Task, workspace_path: str, pid: Optional[int], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_worker_spawned`` AFTER the PID is durably persisted; best-effort."""
    if not _kanban_observer_consumed("on_kanban_worker_spawned"):
        return
    try:
        _fire_kanban_lifecycle_hook(
            "on_kanban_worker_spawned", task.id, board=board or get_current_board(),
            assignee=task.assignee, run_id=_current_run_id(conn, task.id),
            worker_pid=int(pid) if pid else None, workspace_path=str(workspace_path),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban worker spawned hook failed: %s", exc)


def notify_task_updated(
    conn: sqlite3.Connection, task_id: str, changed_fields: Iterable[str], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_task_updated`` AFTER a non-lifecycle task mutation commits
    (also for direct-SQL surfaces like dashboard field editors).
    ``changed_fields`` carries field NAMES only, never values."""
    if not _kanban_observer_consumed("on_kanban_task_updated"):
        return
    try:
        row = conn.execute(
            "SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        _fire_kanban_lifecycle_hook(
            "on_kanban_task_updated", task_id, board=board or get_current_board(),
            assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, changed_fields=list(changed_fields),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban task updated hook failed: %s", exc)


_TICK_ACTIVITY_FIELDS = (
    "spawned", "reclaimed", "promoted", "reconciled_orphans", "reaped_terminal_workers", "crashed", "stale",
    "gauntlet_stale",  # fork: Gauntlet stale-observation sweep (DispatchResult.gauntlet_stale)
    "timed_out", "auto_blocked", "rate_limited", "auto_assigned_default",
    "respawn_guarded", "skipped_per_profile_capped", "skipped_unassigned",
    "skipped_nonspawnable",
)


def _fire_dispatch_tick_hook(
    result: DispatchResult, *, board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """``on_kanban_dispatch_tick`` — strictly AFTER ``_dispatch_tick_lock`` is
    released so a slow subscriber cannot stall a sibling dispatcher.

    Re-port of PR #56066 per the #64231 batch disposition: renamed to the taxonomy form and called by
    ``dispatch_once`` strictly AFTER ``_dispatch_tick_lock`` has been released — the original fired inside
    the lock, so a slow subscriber could extend the single-writer critical section and stall a sibling
    dispatcher's tick. Observer-only and fully best-effort: any subscriber failure is swallowed.
    """
    if not _kanban_observer_consumed("on_kanban_dispatch_tick"):
        return
    try:
        from hermes_cli.lifecycle import invoke_hook

        profile_name = _hook_profile_name()
        if board is None:
            try:
                board = get_current_board()
            except Exception:
                board = None
        outcome = "ok"
        if result.skipped_locked:
            outcome = "skipped_locked"
        elif not any(getattr(result, f, None) for f in _TICK_ACTIVITY_FIELDS):
            outcome = "idle"
        invoke_hook(
            "on_kanban_dispatch_tick", board=board, profile_name=profile_name,
            dry_run=bool(dry_run), outcome=outcome, result=result,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban dispatch tick hook failed: %s", exc)


DEFAULT_CLAIM_TTL_SECONDS = 15 * 60
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Explicit ``ttl_seconds`` > ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` > default."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    return _env_int("HERMES_KANBAN_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1)


DEFAULT_CRASH_GRACE_SECONDS = 30
KANBAN_RATE_LIMIT_EXIT_CODE = 75


def _quota_hold_path(task_id: str) -> Path:
    return worker_logs_dir() / f"{task_id}.quota_hold.json"


def write_worker_quota_hold(hold: dict) -> bool:
    """Worker side: record the provider's quota verdict for the dispatcher.

    The exit code carries only "rate limited"; this sidecar (task + run
    fenced, owner-only) carries the reset time/kind. Best-effort.
    """
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    if not task_id or not run_id or not isinstance(hold, dict):
        return False
    try:
        path = _quota_hold_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"run_id": int(run_id), **hold}, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _consume_worker_quota_hold(task_id: str, run_id: Optional[int]) -> Optional[dict]:
    """Dispatcher side: read and delete the sidecar; None unless run matches."""
    try:
        path = _quota_hold_path(task_id)
        data = json.loads(path.read_text())
    except Exception:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(data, dict) or run_id is None:
        return None
    if data.get("run_id") != int(run_id):
        return None
    kind = data.get("kind")
    if kind not in ("hard", "temporary"):
        return None
    reset_at = data.get("reset_at")
    if isinstance(reset_at, bool) or not isinstance(reset_at, (int, float)):
        reset_at = None
    return {
        "kind": kind,
        "reset_at": int(reset_at) if reset_at else None,
        "provider": str(data.get("provider") or "")[:80] or None,
        "model": str(data.get("model") or "")[:120] or None,
    }


KANBAN_TERMINAL_PROVIDER_EXIT_CODE = 78


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override", default=None,
)
_PIN_FIRST_BOARD_RESOLUTION: ContextVar[bool] = ContextVar(
    "hermes_kanban_pin_first_board_resolution", default=False,
)


@contextlib.contextmanager
def pin_first_board_resolution():
    """Resolve board paths env-pin-first for machine flows that enumerate boards.

    The gateway notifier, per-subscription cursor writes and the embedded
    dispatcher poll every board slug from ``list_boards()`` — the slug is not
    caller intent, it is an iteration variable. On a box whose environment pins
    ``HERMES_KANBAN_DB`` (the dispatcher default) every slug must map to that
    one pinned file: the notifier dedupes resolved DB paths, and per-slug
    paths read empty boards nobody writes, silently killing wake
    notifications. Explicit cross-board intent (CLI ``--board``, a model
    tool's ``board=``) is a USER property and must never run inside this
    context; with no pin set this context changes nothing.
    """
    token = _PIN_FIRST_BOARD_RESOLUTION.set(True)
    try:
        yield
    finally:
        _PIN_FIRST_BOARD_RESOLUTION.reset(token)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)


_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def _board_kanban_cfg() -> dict:
    """Read board-authoritative rules, independent of the active worker profile."""
    try:
        import yaml
        cfg_path = kanban_home() / "config.yaml"
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        section = data.get("kanban") if isinstance(data, dict) else None
        return dict(section) if isinstance(section, dict) else {}
    except Exception:
        return {}


def _kanban_cfg(loader=None) -> dict:
    """The effective ``kanban:`` config section for board rules.

    Base = the BOARD's config (``kanban_home()/config.yaml``), which is shared by every
    profile; overlay = the current process config's ``kanban:`` section (``loader``,
    default ``load_config_readonly``). Workers run with a profile HERMES_HOME whose
    config has no ``kanban:`` section, so without the base they silently used code
    defaults (attempt limit 6 instead of the board's 12, 2026-10-05).
    """
    base = _board_kanban_cfg()
    overlay: dict = {}
    try:
        if loader is None:
            from hermes_cli.config import load_config_readonly as loader
        k = (loader() or {}).get("kanban")
        if isinstance(k, dict):
            overlay = k
    except Exception:
        overlay = {}
    return {**base, **overlay}


def _slug_or_default(board: Optional[str]) -> str:
    return _normalize_board_slug(board) or DEFAULT_BOARD


def _require_slug(slug: str) -> str:
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    return normed


def kanban_home() -> Path:
    """``HERMES_KANBAN_HOME`` else ``get_default_hermes_root()``. Shared across
    profiles BY DESIGN: resolving through the active profile's HERMES_HOME would
    fork the board per profile and break the dispatcher/worker handoff."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """``<root>/kanban/boards`` — parent of the *additional* named boards.
    ``default`` is deliberately not here (its DB stays at ``<root>/kanban.db``)."""
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """``<root>/kanban/current`` — one-line slug written by ``boards switch``; absent = ``default``."""
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Active slug: context override -> ``HERMES_KANBAN_BOARD`` -> ``<root>/kanban/current``
    (only while that board exists) -> ``DEFAULT_BOARD``. A malformed/stale slug
    falls through — the dispatcher must never crash on a hand-edited file."""
    def _existing(candidate: str) -> Optional[str]:
        if not candidate:
            return None
        try:
            normed = _normalize_board_slug(candidate)
        except ValueError:
            return None
        if not (normed and board_exists(normed)):
            return None
        # An archived board leaves a tombstone board.json; a stale `current`
        # file or HERMES_KANBAN_BOARD pointing at one falls through to
        # default rather than resurrecting it (#43243).
        if read_board_metadata(normed).get("archived"):
            return None
        return normed

    for candidate in (
        (_CURRENT_BOARD_OVERRIDE.get() or "").strip(),
        os.environ.get("HERMES_KANBAN_BOARD", "").strip(),
    ):
        found = _existing(candidate)
        if found:
            return found
    try:
        f = current_board_path()
        if f.exists():
            # utf-8-sig read fix (ours): tolerate BOM-persisted current-board files.
            val = f.read_text(encoding="utf-8-sig").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board; returns the file written. Does NOT
    check the board exists — callers do (so ``boards switch <typo>`` errors)."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    with contextlib.suppress(FileNotFoundError):
        current_board_path().unlink()


def board_dir(board: Optional[str] = None) -> Path:
    """``<root>/kanban/boards/<slug>/``. For ``default`` this holds metadata
    only (board.json, workspaces/, logs/) — its DB stays at ``<root>/kanban.db``
    for back-compat (:func:`kanban_db_path`).
    """
    return boards_root() / _slug_or_default(board)


def board_exists(board: Optional[str] = None) -> bool:
    """Board has a ``board.json`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))


def _path_is_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to ``root`` or somewhere beneath it.

    Symlinks are followed on both sides (the live board is reached through a
    ``kanban.db`` symlink on at least one install). Any resolution error
    reads as "not within" so the caller falls back to its historical
    behaviour rather than guessing.
    """
    try:
        path.expanduser().resolve().relative_to(root.expanduser().resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


_FOREIGN_PIN_MEMO: Optional[tuple] = None
_FOREIGN_PIN_WARNED: set = set()


def _inherited_db_pin_is_foreign(pinned: Path) -> bool:
    """True when ``HERMES_KANBAN_DB`` points back at the platform-native
    Hermes root while the active kanban home has been pointed elsewhere.

    Incident 2026-10-12 22:02 (card t_6cc051f2, audit EVT-PROBECLEAN-20261012):
    a worker's probe script set ``HERMES_HOME`` to a temp folder and called
    ``init_db()`` / ``create_task()``. The worker environment still carried the
    dispatcher's ``HERMES_KANBAN_DB`` pin, which this module honoured ahead of
    ``HERMES_HOME``, so five probe cards landed on the live board and were
    dispatched. ``tests/conftest.py`` strips the pin for pytest; ad-hoc scripts
    had no such protection.

    The rule is deliberately narrow so the dispatcher→worker handoff is
    untouched:

    * A pin **outside** the native root (``~/.hermes`` on POSIX) is always
      honoured — custom / Docker roots, and tests that pin a tempfile.
    * A pin **inside** the native root is honoured whenever the active kanban
      home also resolves inside it — the normal install, every
      ``<root>/profiles/<name>`` worker, and an explicit
      ``HERMES_KANBAN_HOME`` pointing at the root.
    * Only a pin inside the native root combined with a kanban home **outside**
      it is foreign: the process deliberately re-homed itself somewhere else
      and the pin can only have been inherited. ``HERMES_KANBAN_HOME=<root>``
      is the documented way to use the live board from such a process on
      purpose.
    """
    global _FOREIGN_PIN_MEMO
    from hermes_constants import _get_platform_default_hermes_home

    native = _get_platform_default_hermes_home()
    key = (
        str(pinned),
        os.environ.get("HERMES_HOME", ""),
        os.environ.get("HERMES_KANBAN_HOME", ""),
        str(native),
    )
    if _FOREIGN_PIN_MEMO is not None and _FOREIGN_PIN_MEMO[0] == key:
        return _FOREIGN_PIN_MEMO[1]

    if not _path_is_within(pinned, native):
        verdict = False
    else:
        home = kanban_home()
        verdict = not (_path_is_within(home, native) or _path_is_within(pinned, home))
    _FOREIGN_PIN_MEMO = (key, verdict)
    return verdict


def _warn_foreign_db_pin_once(pinned: Path) -> None:
    key = str(pinned)
    if key in _FOREIGN_PIN_WARNED:
        return
    _FOREIGN_PIN_WARNED.add(key)
    _log.warning(
        "ignoring inherited HERMES_KANBAN_DB=%s: it names a board under the "
        "platform Hermes root while HERMES_HOME resolves kanban paths to %s. "
        "A re-homed process must not write to the live board through an "
        "inherited dispatcher pin (incident 2026-10-12). Set "
        "HERMES_KANBAN_HOME to that root to use the pinned board on purpose.",
        pinned, kanban_home(),
    )


def _explicit_board_slug(board: Optional[str]) -> Optional[str]:
    """Explicit caller intent: a direct ``board=`` argument, else the scoped
    ``--board`` context (CLI ``hermes kanban --board``, dashboard plugin_api);
    ``None`` when the caller expressed neither."""
    if board is not None:
        return _normalize_board_slug(board)
    # A caller-scoped board (CLI `hermes kanban --board B ...`, dashboard
    # plugin_api) is explicit intent just like a direct board= argument —
    # without this a worker-pinned HERMES_KANBAN_DB silently outranks --board
    # (os-reviewer P1 on PR#107195 / t_11c4afd8).
    ctx = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if ctx:
        try:
            return _normalize_board_slug(ctx)
        except ValueError:
            return None
    return None


def _explicit_board_intent_pinned() -> bool:
    """Whether an explicit ``board=`` (or scoped ``--board``) must still resolve
    through the ``HERMES_KANBAN_DB``-style env pins instead of its own board dir.

    True for machine flows wrapped in :func:`pin_first_board_resolution` and
    for every execution the dispatcher fences: its own workers (they carry
    ``HERMES_KANBAN_TASK``) and delegated children / descendants (the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker). The pins ARE the "workers
    physically cannot see other boards" isolation (5ec6baa), and
    ``agent.delegation_context.kanban_path_is_fenced`` checks the pinned path /
    fenced root — an explicit board that resolved elsewhere would also escape
    that fence."""
    if _PIN_FIRST_BOARD_RESOLUTION.get():
        return True
    from agent.delegation_context import explicit_board_intent_is_pinned
    return explicit_board_intent_is_pinned()


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver. An explicit ``board=`` argument — or the scoped
    ``--board`` context (:func:`scoped_current_board`) — outranks the ``env_var``
    pin ONLY where no fence applies (see :func:`_explicit_board_intent_pinned`):
    user-facing cross-board intent (CLI ``--board``, a model tool's ``board=``)
    is honored, but machine flows that enumerate boards (gateway notifier /
    watcher / dispatcher ticks) and dispatched or delegated workers keep
    resolving through the pin. Without explicit intent the ``env_var`` override
    pins the file, else legacy ``<root>/<default_parts>`` for the ``default``
    board, else ``board_dir(slug)/leaf``."""
    pin = os.environ.get(env_var, "").strip() if env_var else ""
    slug = _explicit_board_slug(board)
    if pin and (slug is None or _explicit_board_intent_pinned()):
        return Path(pin).expanduser()
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def kanban_db_path(board: Optional[str] = None) -> Path:
    """``kanban.db`` path: ``HERMES_KANBAN_DB`` pins it (injected into workers);
    ``default`` -> ``<root>/kanban.db`` (back-compat), else the board dir.

    One guard (fork): a pin under the platform-native Hermes root is ignored
    (with one warning) when the active kanban home resolves outside that root,
    so an inherited dispatcher pin cannot drag a deliberately re-homed process
    back to the live board (incident 2026-10-12; see
    :func:`_inherited_db_pin_is_foreign`)."""
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        pinned = Path(override).expanduser()
        if _inherited_db_pin_is_foreign(pinned):
            _warn_foreign_db_pin_once(pinned)
            # Resolve as if the foreign pin were absent.
            return _board_path(None, board, ("kanban.db",), "kanban.db")
    return _board_path("HERMES_KANBAN_DB", board, ("kanban.db",), "kanban.db")


def workspaces_root(board: Optional[str] = None) -> Path:
    """Per-board scratch workspace root (``HERMES_KANBAN_WORKSPACES_ROOT`` wins);
    ``default`` keeps the legacy ``<root>/kanban/workspaces/``."""
    return _board_path("HERMES_KANBAN_WORKSPACES_ROOT", board, ("kanban", "workspaces"), "workspaces")


def attachments_root(board: Optional[str] = None) -> Path:
    """Per-board attachments root (``HERMES_KANBAN_ATTACHMENTS_ROOT`` wins). Workers
    read attachments by absolute path, so remote terminal backends must mount it."""
    return _board_path("HERMES_KANBAN_ATTACHMENTS_ROOT", board, ("kanban", "attachments"), "attachments")


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _board_path(None, board, ("kanban", "logs"), "logs")


@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: Optional[int]  # None only for an unparseable stamp
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Column semantics: see SCHEMA_SQL.
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    skills: Optional[list] = None            # None = defaults only; [] = explicitly none
    model_override: Optional[str] = None
    provider_override: Optional[str] = None  # provider ``model_override`` belongs to
    reasoning_effort: Optional[str] = None   # VALID_REASONING_EFFORTS | "none"; NULL = profile's
    # Breaker trip count; None -> ``kanban.failure_limit`` -> DEFAULT_FAILURE_LIMIT.
    max_retries: Optional[int] = None
    # ``/goal``-style loop: a judge re-checks each turn IN THE SAME SESSION until
    # done / budget exhausted (-> kanban_block); ``goal_max_turns`` None -> goals default.
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard
    # VALID_BLOCK_KINDS or None (legacy); kept across unblock so a same-kind re-block reads as a loop.
    block_kind: Optional[str] = None
    block_recurrences: int = 0               # unblock-loop counter, see BLOCK_RECURRENCE_LIMIT
    completion_contract: Optional[str] = None
    # Explicit executor lane (one of VALID_EXECUTOR_LANES, or None for the
    # normal Hermes worker). See the module-level comment above
    # VALID_EXECUTOR_LANES.
    executor_lane: Optional[str] = None
    # Shell command the recovery lane runs (mechanically, never via LLM
    # self-report) to decide whether the gate is green. Required at creation
    # when executor_lane == EXECUTOR_LANE_CLAUDE_RECOVERY; otherwise ignored.
    recovery_gate_cmd: Optional[str] = None
    # True when this task may only reach 'done' through the full Gauntlet
    # chain (running -> review/pending -> verified -> done). See the column
    # comment in SCHEMA_SQL and :func:`gauntlet_required`.
    gauntlet_enforced: bool = False
    # Head of the task_verifications ledger: None | 'pending' | 'verified' |
    # 'failed'. Only 'verified' satisfies the completion guard.
    verification_state: Optional[str] = None
    # True once a failing verdict has made the next round of work a material
    # repair: the next PASS must carry an explicit regression proof bound to
    # the current verification phase. Survives the repair run; cleared only by
    # such a PASS. See the REGRESSION EVIDENCE comment near
    # VERIFICATION_REGRESSION.
    regression_required: bool = False
    # How this task stopped, when it stopped for a reason worth naming:
    # None (ordinary/live card) | 'completed' (this executor did the work) |
    # 'overtaken_by_events' (the objective was satisfied elsewhere and the
    # work must not be run). See VALID_TERMINAL_DISPOSITIONS.
    terminal_disposition: Optional[str] = None
    # Mandatory rationale + evidence recorded alongside a non-'completed'
    # disposition. Never a substitute for ``result``: ``result`` means "this
    # executor produced this outcome", which is exactly the claim an
    # overtaken card must not make.
    disposition_reason: Optional[str] = None
    # When the disposition was recorded. Deliberately NOT ``completed_at`` —
    # an overtaken card was never completed, and conflating the two would put
    # fabricated completions into every time-to-complete metric.
    disposition_at: Optional[int] = None
    # Structured actor provenance — see the ACTOR PROVENANCE block near
    # VALID_ACTOR_KINDS. ``None`` means "not recorded" (a pre-migration row),
    # which is NOT the same as ACTOR_KIND_UNKNOWN ("recorded, and the creating
    # path could not establish a kind"). Never infer any of these from
    # ``created_by``; that inference is the defect.
    actor_kind: Optional[str] = None
    actor_id: Optional[str] = None
    # Lane of the actor that CREATED this card, not the lane it runs in.
    actor_lane: Optional[str] = None
    actor_run_id: Optional[str] = None
    creation_cause: Optional[str] = None
    # Identity accountable for this card's recovery decision. Set by
    # ``create_repair_task``; None on ordinary cards.
    recovery_owner: Optional[str] = None
    # Rule 6 (production authority), design 3A: the production roles this
    # card EXPLICITLY permits, as a structured list of role names from the
    # installed registry (``production_roles_registry_path``). Never read
    # from body text. Set only at creation, by an interactive operator —
    # never by a dispatcher-owned worker or the ``kanban_create`` tool — and
    # immutable afterwards (``trg_tasks_production_actions_immutable``):
    # changing an authorization means a new card. Empty on ordinary cards.
    production_actions: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        g = lambda col, default=None: _lossy_text(_row_get(row, col, default))
        parsed = _json_or(g("skills"))
        skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
        parsed_pa = _json_or(g("production_actions"))
        production_actions_value: list[str] = (
            [str(r) for r in parsed_pa if r] if isinstance(parsed_pa, list) else []
        )
        disposition_at_raw = g("disposition_at")
        return cls(
            **{col: _lossy_text(row[col]) for col in _TASK_REQUIRED_COLUMNS},
            **{col: _to_epoch(g(col)) for col in _TASK_EPOCH_COLUMNS},  # outside writers may store text: epoch or None
            **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS},
            **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
            # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
            # reachable on a DB never opened since the rename migration landed.
            consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
            last_failure_error=g("last_failure_error", g("last_spawn_error")),
            skills=skills_value,
            goal_mode=bool(g("goal_mode")),
            block_recurrences=int(g("block_recurrences") or 0),
            # Fork (Gauntlet) columns: NULL/"" read as "not set".
            executor_lane=g("executor_lane") or None,
            recovery_gate_cmd=g("recovery_gate_cmd") or None,
            gauntlet_enforced=bool(g("gauntlet_enforced")),
            verification_state=g("verification_state") or None,
            regression_required=bool(g("regression_required")),
            terminal_disposition=g("terminal_disposition") or None,
            disposition_reason=g("disposition_reason") or None,
            disposition_at=int(disposition_at_raw) if disposition_at_raw is not None else None,
            actor_kind=g("actor_kind") or None,
            actor_id=g("actor_id") or None,
            actor_lane=g("actor_lane") or None,
            actor_run_id=g("actor_run_id") or None,
            creation_cause=g("creation_cause") or None,
            recovery_owner=g("recovery_owner") or None,
            production_actions=production_actions_value,
        )


_TASK_REQUIRED_COLUMNS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by",
    "workspace_kind", "workspace_path", "claim_lock",
)
_TASK_EPOCH_COLUMNS = ("created_at", "started_at", "completed_at", "claim_expires", "last_heartbeat_at")
_TASK_OPTIONAL_COLUMNS = (
    "branch_name", "project_id", "tenant", "result", "idempotency_key", "worker_pid",
    "max_runtime_seconds", "current_run_id", "workflow_template_id",
    "current_step_key", "max_retries", "session_id", "completion_contract",
)
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
)


@dataclass
class Run:
    """One attempt at a task (``task_runs`` row): opened on claim, closed on
    complete/block/crash/timeout/reclaim; carries the handoff summary."""

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(
            **{
                col: _lossy_text(row[col]) for col in (
                    "task_id", "profile", "step_key", "status", "claim_lock",
                    "worker_pid", "max_runtime_seconds", "outcome", "summary", "error",
                )
            },
            **{col: _to_epoch(row[col]) for col in ("claim_expires", "last_heartbeat_at", "ended_at")},
            id=int(row["id"]), started_at=_to_epoch(row["started_at"]) or 0,
            metadata=_json_or(_lossy_text(row["metadata"])),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Comment:
        return cls(
            id=r["id"], task_id=r["task_id"], author=_lossy_text(r["author"]),
            body=_lossy_text(r["body"]), created_at=_to_epoch(r["created_at"]) or 0,
        )


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Attachment:
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=_to_epoch(r["created_at"]) or 0,
        )


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        run_id = _row_get(row, "run_id")
        return cls(
            id=row["id"], task_id=row["task_id"], kind=_lossy_text(row["kind"]),
            payload=_json_or(_lossy_text(row["payload"])), created_at=_to_epoch(row["created_at"]) or 0,
            run_id=_opt_int(run_id),
        )


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Restart-stable fingerprint of worker_pid ("<boot/instantiation epoch>|<start time>",
    -- kanban_db_dispatch._process_fingerprint) recorded at spawn: liveness and kills require pid
    -- AND fingerprint to agree, so a PID recycled after a reboot is never read as our worker or
    -- signalled. NULL = legacy row (pre-fingerprint spawn); 'unverified' = capture failed at
    -- spawn (held while live, never signalled). Column keeps its INTEGER affinity for the
    -- start-time-only integer values older rows carry.
    worker_started_at    INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    -- Per-task reasoning effort for the worker (minimal|low|medium|high|
    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher
    -- passes --reasoning <level> so the worker runs at that depth regardless
    -- of the profile's agent.reasoning_effort. NULL = profile setting.
    reasoning_effort     TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var, and for an id with no ``sessions`` row in this
    -- profile's state.db (kanban_create verifies before stamping). Indexed
    -- so per-session list queries stay cheap on larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0,
    -- Explicit executor lane (see VALID_EXECUTOR_LANES). NULL (the default)
    -- is the normal Hermes worker; "claude_recovery" mechanically bypasses
    -- the Hermes agent/tool loop for this task's dispatched worker process.
    executor_lane        TEXT,
    -- Shell command the recovery lane runs to verify the gate is green.
    -- Required (validated in create_task) when executor_lane is set.
    recovery_gate_cmd    TEXT,
    -- Gauntlet lifecycle enforcement. When 1, this task may only reach
    -- 'done' through the full chain
    --   running -> review (verification_state='pending')
    --           -> verification_state='verified' -> done
    -- The guard is in complete_task's UPDATE ... WHERE, so a direct
    -- running -> done write is impossible at the kernel boundary rather
    -- than merely discouraged. 0 (the default) preserves the classic
    -- lifecycle for every pre-existing caller. The live config key
    -- ``kanban.gauntlet_enforcement`` raises the floor board-wide without
    -- needing a per-row backfill.
    gauntlet_enforced    INTEGER NOT NULL DEFAULT 0,
    -- Materialised head of the task_verifications ledger:
    -- NULL (no verification phase entered) | 'pending' | 'verified' |
    -- 'failed'. Denormalised onto the task so the completion guard can be
    -- expressed as a single atomic SQL predicate. Cleared whenever a new
    -- implementation run starts, so a stale verdict can never be spent on
    -- work it did not cover.
    verification_state   TEXT,
    -- Armed by a failing verdict: the work that follows is a material repair,
    -- so the next PASS verdict must carry an explicit regression proof (a
    -- task_verifications row with kind='regression' bound to the current
    -- verification phase). Survives the repair run and the re-handoff;
    -- cleared only by a PASS that spends such a proof. complete_task tests it
    -- in the same UPDATE ... WHERE as verification_state, so a repaired card
    -- with no regression evidence cannot reach 'done' even when every Python
    -- pre-check is bypassed.
    regression_required  INTEGER NOT NULL DEFAULT 0,
    -- Terminal disposition: NULL for every ordinary/live card,
    -- 'completed' when THIS executor did the work (stamped by
    -- complete_task's own guarded UPDATE), 'overtaken_by_events' when the
    -- objective was satisfied by verified intervening events and the work
    -- must never run. Non-NULL is tested directly in the WHERE clause of
    -- every transition that could put the card back in flight, so
    -- terminality survives a status change made by a racing or
    -- hand-written writer. See VALID_TERMINAL_DISPOSITIONS.
    terminal_disposition TEXT,
    -- Mandatory rationale + evidence for a non-'completed' disposition.
    -- Kept separate from ``result`` on purpose: ``result`` asserts this
    -- executor produced the outcome, which an overtaken card must not claim.
    disposition_reason   TEXT,
    -- When the disposition was recorded. Deliberately not ``completed_at``:
    -- an overtaken card was never completed and must not enter
    -- time-to-complete metrics as if it had been.
    disposition_at       INTEGER,
    -- Structured actor provenance, written once at creation and never
    -- re-derived. NULL on every row that predates the columns; nothing is
    -- backfilled. See the ACTOR PROVENANCE block near VALID_ACTOR_KINDS for
    -- why ``created_by`` cannot answer these questions.
    actor_kind           TEXT,
    actor_id             TEXT,
    -- Executor lane of the CREATING actor, not of this task. ``executor_lane``
    -- above is the lane this card will run in; the two differ whenever one
    -- lane files work for another.
    actor_lane           TEXT,
    actor_run_id         TEXT,
    creation_cause       TEXT,
    -- Identity accountable for this card's recovery decision, recorded
    -- structurally so "who owns this repair?" is answerable without reading
    -- the body. Required by ``create_repair_task``; NULL elsewhere.
    recovery_owner       TEXT,
    -- Rule 6 production authority (design 3A): JSON list of production role
    -- names this card explicitly permits. Structured, never body text. Set
    -- only at creation and immutable afterwards (see
    -- trg_tasks_production_actions_immutable). '[]' on ordinary cards.
    production_actions   TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

-- Governance relations. Deliberately separate from ``task_links``: this table
-- never participates in readiness/promotion, so a repair can point at the
-- subject it fixes without being gated on that subject finishing. See the
-- GOVERNANCE RELATIONS block near VALID_TASK_RELATIONS.
CREATE TABLE IF NOT EXISTS task_relations (
    from_task_id TEXT NOT NULL,
    to_task_id   TEXT NOT NULL,
    relation     TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    created_by   TEXT,
    PRIMARY KEY (from_task_id, to_task_id, relation)
);

-- Durable observation timers. The 300-second recheck was a CONVENTION carried
-- in prompts and watcher scripts, which means it lived exactly as long as the
-- process holding it: a watcher that exited, was hot-swapped, or emitted its
-- verdict and returned took the timer with it, and nothing on disk knew a
-- recheck was ever owed. These two tables move the timer into the same durable
-- store as the work it observes, so due-ness is a fact about the clock and the
-- board rather than about whether some process is still alive.
--
-- ``next_due_at`` is the whole mechanism: it is written before any tick is
-- emitted and re-armed in the same transaction, so an emitter that dies
-- immediately after emitting leaves a timer that is still armed, and an
-- emitter that dies before emitting leaves one that is still due.
CREATE TABLE IF NOT EXISTS observation_timers (
    id                 TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    -- Free-text label for WHAT is being observed. Never consulted by the
    -- due-time logic; it exists so a board can carry more than one timer per
    -- card without them being confusable.
    kind               TEXT NOT NULL,
    -- Always MANDATORY_OBSERVATION_INTERVAL_SECONDS. Stored per-row anyway so
    -- an existing timer keeps its contracted interval if the constant is ever
    -- changed, rather than silently re-phasing every armed timer on a board.
    interval_seconds   INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    -- The next instant at which a tick is OWED. Monotonically advanced, never
    -- recomputed from "now" — see advance semantics in emit_observation_tick.
    next_due_at        INTEGER NOT NULL,
    -- Count of ticks emitted. Also the seq of the NEXT tick minus one, so the
    -- first emission is seq 1 and is the contracted verdict.
    tick_count         INTEGER NOT NULL DEFAULT 0,
    last_tick_at       INTEGER,
    -- Set exactly once, by a conditional UPDATE that requires it to be NULL.
    -- That predicate is what makes "the verdict is emitted once" a database
    -- constraint rather than a caller's promise.
    verdict_emitted_at INTEGER,
    -- 'observing' or 'closed'. A timer NEVER transitions to closed as a side
    -- effect of emitting; only close_observation_timer does it, and only with
    -- an explicit reason.
    state              TEXT NOT NULL DEFAULT 'observing',
    closed_at          INTEGER,
    closed_reason      TEXT,
    -- Accountable identity, resolved from persisted ownership at arm time.
    owner              TEXT
);

-- Append-only tick ledger. ``PRIMARY KEY (timer_id, seq)`` is the duplicate
-- suppression: two racing emitters, or one emitter retried after a crash that
-- happened between the INSERT and the commit, cannot both land seq N.
CREATE TABLE IF NOT EXISTS observation_ticks (
    timer_id   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    -- The due instant this tick discharges, not the instant it was emitted.
    -- They differ whenever the observer was down; the gap is the evidence.
    due_at     INTEGER NOT NULL,
    emitted_at INTEGER NOT NULL,
    -- Intervals whose due time passed with no observer running. 0 in normal
    -- operation; non-zero is a recorded outage, not a silently skipped tick.
    missed     INTEGER NOT NULL DEFAULT 0,
    -- 1 on seq 1 only: the contracted one-time verdict emission.
    verdict    INTEGER NOT NULL DEFAULT 0,
    payload    TEXT,
    PRIMARY KEY (timer_id, seq)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Append-only verification ledger for the Gauntlet lifecycle. One row per
-- verification-phase transition: the 'pending' row written by
-- request_review, then a terminal 'verified'/'failed' row written by
-- record_verification. ``evidence`` is a JSON blob of falsifiable facts
-- (commands run, exit codes, test counts, artefact paths). The task row's
-- ``verification_state`` is the materialised head of this ledger; the
-- ledger itself is what makes a completion auditable after the fact.
CREATE TABLE IF NOT EXISTS task_verifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    state      TEXT NOT NULL,
    verifier   TEXT,
    evidence   TEXT,
    reason     TEXT,
    created_at INTEGER NOT NULL,
    -- 'verdict' (a phase start or a pass/fail verdict; the historical shape of
    -- this table) or 'regression' (a regression proof for a material repair).
    -- Only 'verdict' rows materialise tasks.verification_state.
    kind       TEXT NOT NULL DEFAULT 'verdict',
    -- Regression rows only: the id of the VERIFICATION_PENDING row whose
    -- verification phase this proof was produced against. A later
    -- request_review opens a new phase with a new id, so a proof gathered
    -- against earlier work stops matching and cannot be spent on it.
    covers_phase_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_task_verifications_task
    ON task_verifications(task_id, id);

-- Promoted lessons: what a VERIFIED task established, in a form future work
-- is made to consume. Written only by ``promote_lesson``, which refuses any
-- source task that is not durably verified (and, if it ever failed, has not
-- spent its material-repair proof). Rows are never deleted or rewritten;
-- retirement stamps the three ``retired_*`` columns and flips ``active``, so
-- a lesson that no longer governs is still visible as one that once did.
CREATE TABLE IF NOT EXISTS task_lessons (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_task_id      TEXT NOT NULL,
    -- Tenant lane this lesson belongs to. NULL only when ``scope='global'``,
    -- which an operator has to ask for explicitly: a tenant-scoped source can
    -- never produce a global lesson, and an untenanted source produces
    -- nothing at all unless the operator says otherwise.
    tenant              TEXT,
    scope               TEXT NOT NULL,  -- 'tenant' | 'global'
    -- Exact-match selector, normalised at write time: 'all' or
    -- '<dimension>:<value>' over LESSON_SELECTOR_DIMENSIONS. No inference —
    -- a receiving task either produces this exact string among its own
    -- selectors or the lesson does not fire for it.
    applicability       TEXT NOT NULL,
    -- The rule itself. Injected verbatim into applicable workers' context as
    -- a binding constraint, so it is bounded (LESSON_MAX_CHARS) at write time.
    lesson              TEXT NOT NULL,
    -- JSON provenance: free-form facts the promoter recorded, plus the
    -- kernel-derived source snapshot (verified_at, verifier, ...).
    evidence            TEXT,
    -- Ledger ids the promotion was authorised by: the VERIFIED verdict row,
    -- and the regression proof it spent when the source had ever failed.
    verification_id     INTEGER,
    regression_proof_id INTEGER,
    created_by          TEXT,
    created_at          INTEGER NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1,
    retired_at          INTEGER,
    retired_by          TEXT,
    retired_reason      TEXT,
    -- 'candidate' | 'active' | 'retired'. Only 'active' binds work; the
    -- binding gate itself remains ``active = 1``, which this mirrors.
    state               TEXT NOT NULL DEFAULT 'active',
    -- When this lesson must be re-justified. A lesson binds by exact match,
    -- so one that has stopped being true steers work silently.
    review_by           INTEGER,
    -- The named condition under which this lesson should be retired.
    retire_condition    TEXT
);

-- NOTE: the two indexes this table needs are NOT created here. One of them
-- covers ``active``, a column an intermediate build's table may not have yet,
-- and a CREATE INDEX in this script runs *before* the additive column pass —
-- so on such a board the whole schema script would abort with "no such column:
-- active" and the DB would fail to open at all. They are created in
-- ``_migrate_add_optional_columns`` instead, after the columns are backfilled.

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    -- Spawn-time start fingerprint of worker_pid (see tasks.worker_started_at). Retained with
    -- worker_pid after the run ends so a worker that outlives its terminal transition can
    -- still be found and reaped; NULL = legacy row, never signalled.
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

-- ---------------------------------------------------------------------
-- Execution supervisor: durable ownership for governed external processes
-- ---------------------------------------------------------------------
--
-- The failure this table exists to make impossible: a synchronous caller
-- launches an external executor (claude, codex, a gate command), the call
-- hits its timeout, the caller reports EXECUTION FAILED — and the child
-- survives, reparented to init, still mutating the working tree. The
-- controller believes the work ended; the executor believes it is running.
-- Nobody owns it, and it later produces commits nobody authorised.
--
-- ``subprocess.run(timeout=...)`` cannot prevent that on its own: it kills
-- only the direct child, so any grandchild (a CLI's own daemon, a spawned
-- node process) is orphaned silently. The fix is not a bigger timeout, it
-- is an ownership record that exists BEFORE the process does and has no
-- state meaning "the caller returned and nobody owns this".
--
-- Every row is therefore written and committed before ``Popen``. Ownership
-- is explicit in ``ownership``: 'controller' (a synchronous caller is
-- waiting, and losing it terminates the process group) or 'supervisor' (a
-- durable background job, reconciled by :func:`exec_supervisor.reconcile`).
-- There is no third value, so "returned but unowned" is unrepresentable
-- rather than merely unlikely.
--
-- Deliberately NOT stored: argv, environment, prompts, tokens. Only
-- ``command_class`` — the name of a registered launcher — is persisted, so
-- an execution record can never become a place a secret leaks to. See
-- ``exec_supervisor.LAUNCHERS``.
CREATE TABLE IF NOT EXISTS executions (
    id                 TEXT PRIMARY KEY,
    -- Optional: a governed executor usually belongs to a Gauntlet card, but
    -- a gate command or an ad-hoc operator run legitimately has no task.
    task_id            TEXT,
    -- Registered executor class (claude / codex / shell / ...). Checked
    -- against the policy's allow-list BEFORE the record is written.
    executor_type      TEXT NOT NULL,
    -- Name of the registered launcher that built argv. Never the argv.
    command_class      TEXT NOT NULL,
    cwd                TEXT NOT NULL,
    pid                INTEGER,
    -- Process-group id. Populated because the child is started with
    -- start_new_session=True: signalling the GROUP is what makes
    -- termination cover grandchildren instead of orphaning them.
    pgid               INTEGER,
    -- Process identity fingerprint captured at launch (Linux: boot-relative
    -- start time + comm from /proc/<pid>/stat). PID alone is not identity —
    -- a recycled PID belonging to an unrelated process must never be
    -- mistaken for this executor and must never be signalled. Reconciliation
    -- compares the live fingerprint against this one.
    proc_key           TEXT,
    -- Durable, per-execution random identity. Survives PID reuse and is
    -- exported to the child as HERMES_EXECUTION_ID for correlation. Not a
    -- credential: it authorises nothing.
    nonce              TEXT NOT NULL,
    controller_pid     INTEGER,
    controller_key     TEXT,
    -- Ownership token of the controlling session. Opaque and non-secret;
    -- it identifies WHICH controller owns the row so a second controller
    -- cannot silently adopt a live execution.
    controller_token   TEXT NOT NULL,
    -- 'controller' | 'supervisor'. See the note above.
    ownership          TEXT NOT NULL,
    max_runtime_s      INTEGER,
    started_at         INTEGER NOT NULL,
    -- Last LIVENESS PROOF from an owner actively watching the child.
    -- Advanced only by ``exec_supervisor.LivenessPump``, which re-reads this
    -- row and re-matches the recorded pid against /proc before every write,
    -- and NEVER by reconciliation — a reconciler that refreshed it would make
    -- every abandoned job look permanently fresh.
    --
    -- Seeded equal to ``started_at``, so equality means "never heartbeated",
    -- not "0 s old". Between the column's introduction and 2026-09-03 that was
    -- true of every row on every board: ``heartbeat()`` had no call site, and
    -- the staleness rule that reads this column was therefore measuring age.
    -- ``exec_supervisor.has_recorded_heartbeat`` is the only correct way to
    -- ask whether a value here is a real observation.
    heartbeat_at       INTEGER NOT NULL,
    -- Last time reconciliation observed this row. Separate from the
    -- heartbeat precisely so "the supervisor is running" and "the executor
    -- is making progress" stay independently visible.
    last_observed_at   INTEGER,
    ended_at           INTEGER,
    -- launching | running | completed | failed | timed_out | controller_lost
    -- | stale | terminated | recovered
    status             TEXT NOT NULL,
    exit_code          INTEGER,
    termination_reason TEXT,
    -- Optional pointer to the rollback / pre-change artefact this execution
    -- can be undone with. Read-only jobs legitimately have none.
    rollback_ref       TEXT,
    -- Whether settling this execution should drive the Gauntlet lifecycle of
    -- ``task_id`` (hand a clean exit to VERIFICATION_PENDING, route every
    -- non-success into recovery). 1 for the ordinary case.
    --
    -- 0 for a caller that already owns that routing and would otherwise be
    -- raced by it — the Claude recovery lane runs its own gate command AFTER
    -- the executor exits and hands off with that gate's evidence attached, so
    -- a supervisor handoff firing first would move the card out of 'running'
    -- and leave the lane's richer handoff refused. Setting it to 0 buys no
    -- freedom to skip the chain: completion is still gated in
    -- ``complete_task``'s UPDATE ... WHERE, which no caller can talk its way
    -- past. It only says WHICH owner performs the handoff.
    route_task         INTEGER NOT NULL DEFAULT 1,
    created_at         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(status, started_at);
CREATE INDEX IF NOT EXISTS idx_executions_task   ON executions(task_id, started_at);

-- Append-only audit trail for executions. Separate from ``task_events``
-- because an execution may have no task, and because an execution's
-- ownership transitions have to stay auditable even when the card is
-- later archived.
CREATE TABLE IF NOT EXISTS execution_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload      TEXT,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_events_exec
    ON execution_events(execution_id, id);

CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_relations_from        ON task_relations(from_task_id, relation);
CREATE INDEX IF NOT EXISTS idx_relations_to          ON task_relations(to_task_id, relation);
-- The due-scan's only index. Ordered state-first so the scan reads just the
-- armed timers; a closed timer must never cost anything to skip.
CREATE INDEX IF NOT EXISTS idx_obs_timers_due        ON observation_timers(state, next_due_at);
CREATE INDEX IF NOT EXISTS idx_obs_timers_task       ON observation_timers(task_id, state);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);

-- Durable ownership ledger for reversible external side effects. Effects are
-- routed by the EXISTING task-run lifecycle; this is not a second scheduler.
CREATE TABLE IF NOT EXISTS execution_effects (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id               TEXT NOT NULL,
    run_id                INTEGER,
    origin_run_id         INTEGER,
    effect_type           TEXT NOT NULL,
    resource              TEXT NOT NULL,
    before_state          TEXT,
    after_state           TEXT,
    inverse_action        TEXT NOT NULL,
    inverse_payload       TEXT,
    state                 TEXT NOT NULL DEFAULT 'prepared',
    hold_reason           TEXT,
    created_at            INTEGER NOT NULL,
    applied_at            INTEGER,
    held_at               INTEGER,
    rollback_requested_at INTEGER,
    reverted_at           INTEGER,
    conflict_at           INTEGER,
    committed_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_execution_effects_run
    ON execution_effects(task_id, run_id, state, id);
CREATE INDEX IF NOT EXISTS idx_execution_effects_state
    ON execution_effects(state, task_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);

-- ---------------------------------------------------------------------------
-- Observation-timer invariants, enforced by the database itself
-- ---------------------------------------------------------------------------
--
-- The Python guards in ``arm_observation_timer`` and ``emit_observation_tick``
-- only bind callers that go through them. Independent ``codex_verify`` review
-- of this repair's first draft made the point precisely: with the check living
-- only in Python, a direct ``INSERT`` could mint a 900-second "observation
-- timer" that reads, at query time, exactly like a correctly armed one — which
-- is the convention-shaped failure this whole mechanism replaces, re-entering
-- through the back door.
--
-- Triggers rather than ``CHECK`` constraints on purpose: a CHECK can only be
-- attached when the table is created, so it would silently skip every board
-- that already has ``observation_timers``. ``CREATE TRIGGER IF NOT EXISTS``
-- applies to existing boards on the next init, which is where the live rows
-- are.

-- 300 is written as a literal because SQL cannot read the Python constant.
-- MANDATORY_OBSERVATION_INTERVAL_SECONDS must match it.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_interval_insert
BEFORE INSERT ON observation_timers
FOR EACH ROW WHEN NEW.interval_seconds != 300
BEGIN
    SELECT RAISE(ABORT, 'observation interval is fixed at 300 seconds');
END;

CREATE TRIGGER IF NOT EXISTS trg_obs_timer_interval_update
BEFORE UPDATE OF interval_seconds ON observation_timers
FOR EACH ROW WHEN NEW.interval_seconds != 300
BEGIN
    SELECT RAISE(ABORT, 'observation interval is fixed at 300 seconds');
END;

-- The verdict is emitted once, for the lifetime of the timer. Clearing or
-- rewriting the stamp is how a restarted observer would get to re-announce,
-- so the column is append-once at the storage layer and not merely in the
-- emission path.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_verdict_once
BEFORE UPDATE OF verdict_emitted_at ON observation_timers
FOR EACH ROW WHEN OLD.verdict_emitted_at IS NOT NULL
                  AND NEW.verdict_emitted_at IS NOT OLD.verdict_emitted_at
BEGIN
    SELECT RAISE(ABORT, 'the observation verdict may only be emitted once');
END;

-- A closed timer is terminal. Re-opening one would resurrect an observation
-- whose end was already recorded and reasoned about, and it would do so
-- without a new ``observation_timer_armed`` event to make it discoverable.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_no_reopen
BEFORE UPDATE OF state ON observation_timers
FOR EACH ROW WHEN OLD.state = 'closed' AND NEW.state != 'closed'
BEGIN
    SELECT RAISE(ABORT, 'a closed observation timer cannot be re-opened');
END;

-- A tick is a statement about what was reported at a due instant. Rewriting
-- one would let an emitter change what it already said.
CREATE TRIGGER IF NOT EXISTS trg_obs_ticks_append_only_update
BEFORE UPDATE ON observation_ticks
BEGIN
    SELECT RAISE(ABORT, 'observation ticks are append-only');
END;

-- The five triggers below close the raw-SQL holes the 2026-09-03 independent
-- codex_verify review found. Each one had the same shape: the *emission path*
-- upheld an invariant that the *storage layer* did not, so the guarantee held
-- only for callers who went through the API. A control that a direct INSERT
-- can walk around is a convention with a docstring, which is the exact class
-- of defect this whole card exists to close.

-- (1) The verdict is emitted once PER TIMER, not once per emitter's good
-- intentions. ``PRIMARY KEY (timer_id, seq)`` alone permitted a second
-- verdict row at any other sequence number. Both halves are enforced: a
-- verdict may only ride seq 1, and only one may exist.
CREATE TRIGGER IF NOT EXISTS trg_obs_ticks_verdict_once_insert
BEFORE INSERT ON observation_ticks
FOR EACH ROW WHEN NEW.verdict != 0
                  AND (NEW.seq != 1
                       OR EXISTS (SELECT 1 FROM observation_ticks t
                                  WHERE t.timer_id = NEW.timer_id
                                    AND t.verdict != 0))
BEGIN
    SELECT RAISE(ABORT,
        'the observation verdict is exactly one tick, at seq 1, per timer');
END;

-- (2) A timer is inserted ARMED or not at all. Without this, a row could be
-- born already carrying a tick count, a spent verdict stamp, a closed state
-- or a due time off the 300 s grid — a timer that looks like it has already
-- done work it never did. ``arm_observation_timer`` writes exactly this shape,
-- so the trigger constrains forgery, not the legitimate path.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_arm_shape
BEFORE INSERT ON observation_timers
FOR EACH ROW WHEN NEW.state != 'observing'
                  OR NEW.tick_count != 0
                  OR NEW.last_tick_at IS NOT NULL
                  OR NEW.verdict_emitted_at IS NOT NULL
                  OR NEW.closed_at IS NOT NULL
                  OR NEW.closed_reason IS NOT NULL
                  OR NEW.next_due_at != NEW.created_at + 300
BEGIN
    SELECT RAISE(ABORT,
        'an observation timer is inserted armed: observing, 0 ticks, no verdict, no closure, first tick due one full interval out');
END;

-- (3) Closing carries its reason at the storage layer. ``state='closed'`` with
-- a null ``closed_at`` or a blank reason is a timer that stopped and did not
-- say why — failure shape 2, arrived at by UPDATE instead of by silence.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_closure_reasoned
BEFORE UPDATE OF state ON observation_timers
FOR EACH ROW WHEN NEW.state = 'closed'
                  AND (NEW.closed_at IS NULL
                       OR TRIM(COALESCE(NEW.closed_reason, '')) = '')
BEGIN
    SELECT RAISE(ABORT,
        'closing an observation timer requires closed_at and a reason');
END;

-- (4) …and the reason cannot be erased afterwards, which is the same hole
-- reached in two statements instead of one.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_closure_reason_immutable
BEFORE UPDATE OF closed_at, closed_reason ON observation_timers
FOR EACH ROW WHEN OLD.state = 'closed'
                  AND (NEW.closed_at IS NULL
                       OR TRIM(COALESCE(NEW.closed_reason, '')) = '')
BEGIN
    SELECT RAISE(ABORT,
        'a closed observation timer keeps its closed_at and reason');
END;

-- (5) An observing timer cannot be deleted, and its ledger cannot be purged
-- out from under it. DELETE was previously left open on the reasoning that
-- purging an old ledger is legitimate maintenance — true, but it also meant
-- ``DELETE FROM observation_timers`` made a live observation vanish with no
-- record anywhere, which is precisely the disappearance the 300 s control was
-- built to make impossible. Retention still works: close the timer first, with
-- a reason, and both deletes are permitted. The task-deletion cascade in
-- ``_delete_observation_timers_for_task`` does exactly that.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_no_delete_while_observing
BEFORE DELETE ON observation_timers
FOR EACH ROW WHEN OLD.state != 'closed'
BEGIN
    SELECT RAISE(ABORT,
        'close an observation timer with a reason before deleting it');
END;

CREATE TRIGGER IF NOT EXISTS trg_obs_ticks_no_delete_while_observing
BEFORE DELETE ON observation_ticks
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM observation_timers
                          WHERE id = OLD.timer_id AND state != 'closed')
BEGIN
    SELECT RAISE(ABORT,
        'the ledger of an observing timer is not purgeable');
END;

-- The two below close what the SECOND independent codex_verify review found
-- (2026-09-03, execution x_4662551c5e9a293f) after the six above were added.

-- (7) ONE observing timer per (task, kind). The verdict-once trigger above is
-- scoped to a timer, so "the verdict is emitted once" was only ever true per
-- timer — and nothing stopped a second timer being armed on the same card with
-- the same kind, each emitting its own seq-1 verdict. Two live 'recheck'
-- observations on one card are not redundancy; they are two clocks that
-- disagree, and a reader cannot tell which one the contract meant.
--
-- A partial unique index rather than a trigger: it is declarative, it is
-- enforced on every writer including raw SQL, and scoping it to
-- state='observing' leaves any number of CLOSED timers in the history, which
-- is what makes re-arming after a recorded closure still legal. Re-arming is
-- deliberately still allowed to produce a new verdict: a new observation is a
-- new question, and the previous one's closure reason is on its row.
--
-- The index is NOT created here. It is created by
-- ``_migrate_observation_timer_uniqueness`` after that function has resolved
-- any pre-existing duplicates, because duplicates were legal until this change
-- and `CREATE UNIQUE INDEX` inside ``executescript(SCHEMA_SQL)`` would abort
-- the whole schema pass on a board that has them — turning a hardening into an
-- init failure. Found by the third independent review.

-- (8) A timer cannot be deleted while the card it observes still exists. The
-- review's objection to the delete guard was that "close with a reason, then
-- delete" erases the very reason it just demanded. That is now impossible for a
-- live card: the only way a timer row leaves the board is as a cascade of its
-- own card's deletion, which removes the card's whole history by design and by
-- authorization. Deleting a card is a destructive operation the operator asked
-- for; ending an observation quietly while its subject is still on the board is
-- not, and that is the case this closes.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_no_delete_while_task_lives
BEFORE DELETE ON observation_timers
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)
BEGIN
    SELECT RAISE(ABORT,
        'an observation timer outlives everything but its own card');
END;

-- (9) A timer's identity is immutable. Found by the THIRD independent
-- codex_verify review (2026-09-03, execution x_66018ca656f32298), and it
-- defeated the guard directly above it: repoint ``task_id`` at a card that does
-- not exist, and the "no delete while its card lives" check passes, taking the
-- only durable copy of the closure reason with it. The same move re-homes a
-- timer onto an unrelated card, or slips it past the (task_id, kind) uniqueness
-- index by changing either key.
--
-- Every field named here identifies WHICH observation this is. None of them is
-- something a legitimate caller edits: arming writes them once, emitting
-- touches only the tick counters, and closing touches only the closure columns.
CREATE TRIGGER IF NOT EXISTS trg_obs_timer_identity_immutable
BEFORE UPDATE OF id, task_id, kind, created_at ON observation_timers
FOR EACH ROW WHEN NEW.id IS NOT OLD.id
                  OR NEW.task_id IS NOT OLD.task_id
                  OR NEW.kind IS NOT OLD.kind
                  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT,
        'an observation timer cannot be re-homed or re-identified');
END;
"""
_INIT_LOCK_UNAVAILABLE_ERRNOS = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})
_INIT_LOCK_UNAVAILABLE_WARNED: set[str] = set()


def _write_corrupt_backup_evidence(candidate: Path) -> None:
    """Best-effort: record quick_check output for a fresh quarantine copy.

    Runs against the *copy* (``immutable=1``, so no -wal/-shm is created and the
    live file's locks are never touched) and writes ``<backup>.evidence.txt``
    next to it. Gives every future corruption a failing-check capture taken at
    quarantine time instead of relying on later reconstruction.
    """
    evidence = candidate.with_name(candidate.name + ".evidence.txt")
    if evidence.exists():
        return
    lines = [f"backup={candidate.name}", f"size={candidate.stat().st_size}"]
    try:
        probe = sqlite3.connect(f"file:{candidate}?immutable=1", uri=True)
        try:
            lines.append("quick_check:")
            lines.extend(
                f"  {r[0]}" for r in probe.execute("PRAGMA quick_check").fetchall()[:200]
            )
        finally:
            probe.close()
    except (sqlite3.Error, OSError) as exc:
        lines.append(f"quick_check failed: {exc}")
    try:
        evidence.write_text("\n".join(lines) + "\n")
    except OSError:
        pass


def _schema_objects(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every user object (``(type, name)``) plus every table column
    (``("column", "<table>.<col>")``) visible on ``conn``."""
    objects: set[tuple[str, str]] = set()
    rows = conn.execute(
        "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in rows:
        kind, name = str(row[0]), str(row[1])
        objects.add((kind, name))
        if kind == "table":
            quoted = name.replace('"', '""')
            for col in conn.execute(f'PRAGMA table_info("{quoted}")').fetchall():
                objects.add(("column", f"{name}.{col[1]}"))
    return objects


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    """Whether first-connect init would be a no-op on ``conn``'s database.

    Meant for a read-only open (board directory not writable), where the
    schema script and the additive migrations cannot run. Rather than
    hand-maintain a list of every column and index those passes create, run
    them against a throwaway in-memory database and require every object and
    column they produce to already exist on disk. The passes are additive, so
    superset on disk means they would have changed nothing.
    """
    if not _schema_is_present(conn):
        return False
    reference = sqlite3.connect(":memory:", isolation_level=None)
    try:
        reference.row_factory = sqlite3.Row
        reference.executescript(SCHEMA_SQL)
        _migrate_add_optional_columns(reference)
        _migrate_fork_schema(reference)
        expected = _schema_objects(reference)
    finally:
        reference.close()
    return expected <= _schema_objects(conn)


_CORRUPT_LATCH_SUFFIX = ".corrupt-latch"


def _corrupt_latch_path(resolved: Path) -> Path:
    return resolved.with_name(resolved.name + _CORRUPT_LATCH_SUFFIX)


def _set_corrupt_latch(resolved: Path) -> None:
    """Best-effort: mark ``resolved`` (by inode) as corrupt for all processes."""
    try:
        ino = resolved.stat().st_ino
        _corrupt_latch_path(resolved).write_text(str(ino), encoding="utf-8")
    except OSError:
        pass


def _clear_corrupt_latch(resolved: Path) -> None:
    try:
        _corrupt_latch_path(resolved).unlink(missing_ok=True)
    except OSError:
        pass


def _corrupt_latch_active(resolved: Path) -> bool:
    """True when a latch exists for the file currently at ``resolved``."""
    try:
        recorded = _corrupt_latch_path(resolved).read_text(encoding="utf-8").strip()
        return recorded == str(resolved.stat().st_ino)
    except OSError:
        return False


_WAL_RESET_WRITER_LOGGED = False


def _wal_reset_writer_blocked() -> bool:
    """True when this process links an SQLite with the WAL-reset bug.

    The live board is a WAL database written by processes linked to a fixed
    SQLite (3.51.3+). A writer/checkpointer linked to an affected build
    (3.7.0-3.51.2 except the 3.50.7/3.44.6 backports, e.g. the 3.50.4
    ``.venv`` or the system 3.45.1 python) can race those writers on WAL
    frames and corrupt the file; ``hermes_state`` deliberately keeps WAL
    for an already-WAL file and only warns. Make such a connection
    read-only instead. Override: ``HERMES_KANBAN_ALLOW_WAL_RESET_WRITER=1``.
    """
    global _WAL_RESET_WRITER_LOGGED
    if os.environ.get("HERMES_KANBAN_ALLOW_WAL_RESET_WRITER") == "1":
        return False
    from hermes_state import is_sqlite_wal_reset_vulnerable

    if not is_sqlite_wal_reset_vulnerable():
        return False
    if not _WAL_RESET_WRITER_LOGGED:
        _WAL_RESET_WRITER_LOGGED = True
        _log.error(
            "kanban.db opened query_only: linked SQLite %s has the WAL-reset "
            "bug; writes refused to protect the shared WAL database",
            sqlite3.sqlite_version,
        )
    return True


def _migrate_observation_timer_uniqueness(conn: sqlite3.Connection) -> None:
    """Resolve duplicate observing timers, then enforce one per (task, kind).

    Runs after ``SCHEMA_SQL``, not inside it. Two concurrent observing timers of
    the same kind on one card were legal until 2026-09-03 — that is the defect
    the index closes — so a board upgraded from before then can legitimately
    contain them. ``CREATE UNIQUE INDEX`` on such a board raises, and inside
    ``executescript(SCHEMA_SQL)`` that aborts the entire schema pass: a
    hardening would present as a board that no longer opens. The third
    independent review found this, and it is the more dangerous kind of bug,
    because it fires on exactly the boards that had the problem.

    Duplicates are **closed, oldest kept**, with an explicit reason — never
    deleted. The surviving timer is the one that has been observing longest, so
    the resolution keeps the earliest continuous observation rather than the
    most recent arming, and the closure reason says which migration ended the
    others. Idempotent: on a clean board both statements are no-ops.

    The index keys on ``LOWER(TRIM(kind))``, not on ``kind``. Normalising in
    ``arm_observation_timer`` binds callers of the API; a raw
    ``INSERT ... kind='Recheck'`` alongside an existing ``'recheck'`` was still
    two logically identical observations, each owed its own verdict — found by
    the fourth independent review. An expression index moves the normalisation
    into the constraint, where raw SQL cannot get around it.

    **Nothing here is caught and shrugged off.** Only a genuinely absent
    ``observation_timers`` table is tolerated (a board whose schema pass has not
    run); every other failure propagates, because an ``OperationalError``
    swallowed here would mark the path initialised with the uniqueness
    constraint silently absent for the life of the process — enforcement that
    reads as present and is not, which is the exact failure class this whole
    card exists to close.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'observation_timers'"
    ).fetchone():
        return
    conn.execute(
        "UPDATE observation_timers SET state = ?, closed_at = ?, "
        "    closed_reason = 'superseded_by_uniqueness_migration' "
        "WHERE state = ? AND id NOT IN ("
        "    SELECT id FROM ("
        "        SELECT id, MIN(created_at) FROM observation_timers "
        "        WHERE state = ? "
        "        GROUP BY task_id, LOWER(TRIM(kind))))",
        (
            OBSERVATION_STATE_CLOSED, int(time.time()),
            OBSERVATION_STATE_OBSERVING, OBSERVATION_STATE_OBSERVING,
        ),
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_obs_timer_one_observing_per_task_kind "
        "ON observation_timers (task_id, LOWER(TRIM(kind))) "
        "WHERE state = 'observing'"
    )


TASK_HISTORY_TRIGGERS_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_task_comments_append_only_update
BEFORE UPDATE ON task_comments
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: comments cannot be edited');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_comments_no_delete_while_task_lives
BEFORE DELETE ON task_comments
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: comments cannot be deleted while their task exists');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_events_append_only_update
BEFORE UPDATE ON task_events
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: events cannot be edited');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_events_no_delete_while_task_lives
BEFORE DELETE ON task_events
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: events cannot be deleted while their task exists');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_runs_identity_immutable
BEFORE UPDATE OF task_id, started_at ON task_runs
FOR EACH ROW WHEN NEW.task_id IS NOT OLD.task_id
               OR NEW.started_at IS NOT OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: a run cannot be re-homed or re-dated');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_runs_no_delete_while_task_lives
BEFORE DELETE ON task_runs
FOR EACH ROW WHEN EXISTS (SELECT 1 FROM tasks WHERE id = OLD.task_id)
BEGIN
    SELECT RAISE(ABORT, 'task history is append-only: runs cannot be deleted while their task exists');
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_production_actions_immutable
BEFORE UPDATE OF production_actions ON tasks
FOR EACH ROW WHEN NEW.production_actions IS NOT OLD.production_actions
BEGIN
    SELECT RAISE(ABORT, 'production_actions is immutable after creation: changing a production authorization means a new card');
END;
"""
PRODUCTION_ACTIONS_TRIGGER_NAME = "trg_tasks_production_actions_immutable"
TASK_HISTORY_TRIGGER_NAMES: tuple[str, ...] = (
    "trg_task_comments_append_only_update",
    "trg_task_comments_no_delete_while_task_lives",
    "trg_task_events_append_only_update",
    "trg_task_events_no_delete_while_task_lives",
    "trg_task_runs_identity_immutable",
    "trg_task_runs_no_delete_while_task_lives",
    PRODUCTION_ACTIONS_TRIGGER_NAME,
)


def _ensure_task_history_append_only(conn: sqlite3.Connection) -> None:
    """Install the append-only history triggers (idempotent).

    Each trigger is created only when its table exists: the migration pass
    is also run directly against partial legacy databases (tests do this,
    and so does ``hermes kanban repair`` on a damaged board), and a trigger
    on a missing table is a hard error rather than a no-op.
    """
    present = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('task_comments', 'task_events', 'task_runs', 'tasks')"
        )
    }
    # The production_actions trigger names its column, so a partial legacy
    # ``tasks`` table that predates the column must not get it until the
    # additive migration above has added the column.
    task_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
    } if "tasks" in present else set()
    for statement in TASK_HISTORY_TRIGGERS_SQL.split("END;"):
        statement = statement.strip()
        if not statement:
            continue
        table = statement.split(" ON ", 1)[1].split()[0]
        if table == "tasks" and "production_actions" not in task_cols:
            continue
        if table in present:
            conn.execute(statement + "\nEND;")


def _new_task_id() -> str:
    """``t_`` + 4 hex bytes (collision ~1e-3 at 100k tasks; 2 bytes would hit 50%
    by 10k). Idempotency belongs to ``idempotency_key``, not id uniqueness."""
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


PROFILE_ASSIGNEE_ALIASES: dict[str, str] = {
    "paris": "paris_worker",
}


def _host_prefix() -> str:
    """``"<host>:"`` prefix shared by every claim lock issued from this host."""
    return f"{_claimer_id().split(':', 1)[0]}:"


def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Strip both; a provider without a model is rejected (a bare ``--provider``
    would re-resolve the profile's model against another backend — exactly
    the mismatch the override exists to kill)."""
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    if provider and not model:
        raise ValueError("provider_override requires a model_override")
    return model, provider


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Normalize assignee names and resolve stable operator-facing aliases."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    normalized = normalize_profile_name(assignee)
    return PROFILE_ASSIGNEE_ALIASES.get(normalized, normalized)


TENANT_SHARED = "shared"


def _normalize_lane(value: object) -> Optional[str]:
    from hermes_cli.profiles import normalize_company_lane

    return normalize_company_lane(value)


def _profile_lane(assignee: Optional[str]) -> Optional[str]:
    """Company lane of an assignee token; ``None`` when untagged/unknown."""
    if assignee is None:
        return None
    if assignee in (EXECUTOR_LANE_CLAUDE, "atlas"):
        # Legacy shorthand for the direct executor lanes — they ride the
        # shared ``default`` carrier (see _normalize_shorthand_lane).
        return TENANT_SHARED
    # Imported inside the function so a monkeypatched
    # ``profiles.profile_company`` is honoured at call time.
    from hermes_cli.profiles import profile_company

    try:
        return _normalize_lane(profile_company(assignee))
    except Exception:
        return None


def tenant_claim_conflict(
    task_tenant: Optional[str], assignee: Optional[str],
) -> Optional[dict[str, Any]]:
    """Return a conflict record when *assignee* may not execute a task in
    *task_tenant*, or ``None`` when the lanes are compatible.

    Pure on its inputs apart from the profile-lane lookup, so every door can
    ask the same question and get the same answer.
    """
    lane = _normalize_lane(task_tenant)
    if lane is None or assignee is None:
        return None
    profile_lane = _profile_lane(assignee)
    base = {
        "assignee": assignee,
        "task_tenant": lane,
        "profile_company": profile_lane,
    }
    if profile_lane == TENANT_SHARED:
        return None
    if profile_lane is None:
        return {"reason": "tenant_unknown_profile", **base}
    if lane == TENANT_SHARED:
        return {"reason": "shared_task_company_profile", **base}
    if lane != profile_lane:
        return {"reason": "tenant_mismatch", **base}
    return None


def _assert_assignee_in_lane(
    task_tenant: Optional[str], assignee: Optional[str], *, context: str,
) -> None:
    """Raise ``ValueError`` when assigning *assignee* would cross a company lane."""
    conflict = tenant_claim_conflict(task_tenant, assignee)
    if conflict is None:
        return
    raise ValueError(
        f"{context}: profile {assignee!r} (company lane "
        f"{conflict['profile_company'] or 'untagged'}) may not execute a task "
        f"in lane {conflict['task_tenant']!r} ({conflict['reason']}). One task, "
        f"one company; uncertain scope is forbidden. Tag the profile's "
        f"company in its profile.yaml, or route the task to a profile in that "
        f"lane or in the shared lane."
    )


def _emit_unless_last_identical(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: dict,
) -> bool:
    """Append ``kind`` unless the most recent event of that kind on the card
    already carries the same payload. One record per distinct refusal, not
    one per dispatcher tick."""
    last = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if last is not None and last["payload"]:
        try:
            if json.loads(last["payload"]) == payload:
                return False
        except (TypeError, ValueError):
            pass
    _append_event(conn, task_id, kind, payload)
    return True


def _assert_assignee_dispatchable(assignee: Optional[str], *, context: str) -> None:
    """Raise ``ValueError`` unless *assignee* names something that can dispatch.

    ONE vocabulary for every path that writes the ``assignee`` column: an
    existing Hermes profile, or one of the two recognized legacy shorthand
    tokens ("claude", "atlas"). ``None`` (deliberately unassigned) passes.

    This gate lived only in :func:`assign_task` until D8. The asymmetry was
    the defect: ``hermes kanban assign <id> w`` raised, while
    ``hermes kanban add --assignee w`` succeeded and minted a row that the
    dispatcher can never spawn — it fails profile resolution at spawn time,
    is counted as ``skipped_nonspawnable``, and the card then sits in
    ``ready``/``todo`` forever with no error anyone reads (t_820fca96:
    claimed -> gave_up "worker crashed"/spawn_failed -> promoted, repeat).
    Five distinct non-existent assignees reached the live board across 8
    rows this way. Validating at the write is the only place the caller is
    still around to be told.

    ``context`` names the calling operation so the message points at the
    argument the caller actually passed.
    """
    if assignee is None:
        return
    if assignee in (EXECUTOR_LANE_CLAUDE, "atlas"):
        return
    # Imported inside the function so a monkeypatched ``profiles.profile_exists``
    # (the ``all_assignees_spawnable`` test fixture) is honoured at call time,
    # matching how ``assign_task`` has always resolved it.
    from hermes_cli.profiles import profile_exists as _profile_exists

    if not _profile_exists(assignee):
        raise ValueError(
            f"{context}: {assignee!r} is not an existing Hermes profile and "
            f"not a recognized shorthand ({EXECUTOR_LANE_CLAUDE!r} or "
            f"'atlas'). A task assigned to a non-existent profile can never "
            f"be spawned. Run 'hermes profiles list' for valid names; "
            f"executor-lane identifiers (e.g. {EXECUTOR_LANE_CODEX_VERIFY!r}, "
            f"{EXECUTOR_LANE_CLAUDE_RECOVERY!r}) must be set via "
            f"executor_lane, never assignee."
        )


SHORTHAND_ASSIGNEE_LANES: dict[str, str] = {
    EXECUTOR_LANE_CLAUDE: EXECUTOR_LANE_CLAUDE,
    "atlas": EXECUTOR_LANE_CODEX_VERIFY,
}


def _normalize_shorthand_lane(
    assignee: Optional[str], executor_lane: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Translate a shorthand assignee token into (carrier profile, lane).

    Returns ``(assignee, executor_lane, from_token)``; ``from_token`` is the
    shorthand that was translated, or ``None`` when nothing changed.

    ``assignee='atlas'`` and ``assignee='claude'`` name LANES, not Hermes
    profiles — ``~/.hermes/profiles/atlas/`` does not exist and
    ``profile_exists('atlas')`` is False. So a row left carrying the raw token
    is unclaimable in *every* lane: the ready loop skips it as
    ``skipped_nonspawnable`` and so does the review loop. Before this helper
    the translation lived inline in exactly two places — ``create_task`` and
    the ready-queue dispatch loop, the latter guarded by ``status = 'ready'``
    — so a task moved to a shorthand while in any OTHER status (``review``
    being the live case) never normalized and stalled permanently with no
    exception and no dispatcher signal. Live casualties: t_6b7d5845 (35.7h in
    'review', 8 consecutive ``gauntlet_stale`` events) and t_06e046f1 (16.8h,
    4 events) — the latter stalled while correctly trying to escape an
    Erika-reviews-Erika loop, i.e. the dead end swallowed the very manoeuvre
    that breaks a self-review deadlock. Defect D5, evidence packet for
    t_db0af7e0.

    Every write that can put an assignee on a row calls this, so normalization
    is a property of the ASSIGNMENT rather than of whichever status the row
    happens to be sitting in. ``_assert_assignee_dispatchable`` above is the
    companion gate: it decides whether a token may be written at all, this
    decides what it means once it is.
    """
    lane = SHORTHAND_ASSIGNEE_LANES.get(assignee or "")
    from_token: Optional[str] = None
    if lane is not None and executor_lane is None:
        from_token = assignee
        executor_lane = lane
    if executor_lane in {
        EXECUTOR_LANE_CLAUDE, EXECUTOR_LANE_CODEX_VERIFY, EXECUTOR_LANE_CLAUDE_VERIFY,
    }:
        # All direct-executor lanes ride the spawnable default profile as a
        # carrier, only far enough to reach cli.py's pre-agent executor bypass.
        if assignee != "default":
            from_token = from_token or assignee
            assignee = "default"
    return assignee, executor_lane, from_token


@dataclass(frozen=True)
class ActorProvenance:
    """Structured record of WHO created a card and WHY.

    Immutable on purpose: provenance is a fact about a moment, and a mutable
    provenance object invites exactly the after-the-fact reinterpretation that
    made ``created_by`` useless.
    """

    kind: str = ACTOR_KIND_UNKNOWN
    #: Identity within ``kind``. For a human relay this is the human (or the
    #: session relaying for them); for automation, the executor identity.
    actor_id: Optional[str] = None
    #: Executor lane of the CREATING actor (not of the card being created).
    lane: Optional[str] = None
    #: Run / execution / session id of the creating actor's own run.
    run_id: Optional[str] = None
    cause: str = CREATION_CAUSE_UNKNOWN

    def __post_init__(self) -> None:
        if self.kind not in VALID_ACTOR_KINDS:
            raise ValueError(
                f"actor kind must be one of {sorted(VALID_ACTOR_KINDS)}, "
                f"got {self.kind!r}"
            )
        if self.cause not in VALID_CREATION_CAUSES:
            raise ValueError(
                f"creation cause must be one of {sorted(VALID_CREATION_CAUSES)}, "
                f"got {self.cause!r}"
            )

    @property
    def human_in_loop(self) -> bool:
        """Whether a human was mechanically in the loop for this creation.

        The single predicate every acceptance decision should use. It reads a
        recorded enum, so it cannot be satisfied by an identity string that
        merely looks human.
        """
        return self.kind in HUMAN_IN_LOOP_ACTOR_KINDS

    def as_dict(self) -> dict:
        return {
            "actor_kind": self.kind,
            "actor_id": self.actor_id,
            "actor_lane": self.lane,
            "actor_run_id": self.run_id,
            "creation_cause": self.cause,
        }


def resolve_actor_provenance(
    *,
    kind: Optional[str] = None,
    actor_id: Optional[str] = None,
    lane: Optional[str] = None,
    run_id: Optional[str] = None,
    cause: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ActorProvenance:
    """Derive provenance from explicit arguments, falling back to the env.

    The env fallback is mechanical, not a guess about identity:

    * ``HERMES_KANBAN_TASK`` is injected by ``build_worker_env`` into every
      dispatched executor and by nothing else. Its presence means "this
      process IS a governed run", which is a fact about the process, not an
      inference from a name — so it is the discriminator, and the reason a
      governed automation cannot masquerade as a human relay simply by being
      configured with a human-sounding profile.
    * ``HERMES_ACTOR_KIND`` lets a caller assert its own kind explicitly and
      wins over the derivation. A value outside :data:`VALID_ACTOR_KINDS` is
      IGNORED rather than stored: a junk provenance is worse than a derived
      one, because it looks authoritative.

    An interactive surface with no governed-run env is recorded as
    ``human_interactive``/``manual_relay`` — the case ``t_fb23ac0a`` is: a
    card Christopher relayed through an interactive Claude session, which
    ``created_by='claude-code'`` cannot distinguish from automation running
    under the same profile.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    governed_task = (environ.get("HERMES_KANBAN_TASK") or "").strip()
    declared = (environ.get(ENV_ACTOR_KIND) or "").strip()

    if kind is None:
        if declared in VALID_ACTOR_KINDS:
            kind = declared
        elif governed_task:
            kind = ACTOR_KIND_GOVERNED_AUTOMATION
        else:
            kind = ACTOR_KIND_HUMAN_INTERACTIVE

    if actor_id is None:
        actor_id = (
            (environ.get(ENV_ACTOR_ID) or "").strip()
            or (environ.get("HERMES_PROFILE_NAME") or "").strip()
            or (environ.get("HERMES_PROFILE") or "").strip()
            or None
        )

    if lane is None:
        # The creating run's own lane. Only a governed run has one; an
        # interactive surface correctly records None rather than borrowing
        # the lane of the card it is about to create.
        lane = (environ.get("HERMES_EXECUTOR_LANE") or "").strip() or None

    if run_id is None:
        # Most specific identifier first. An execution id pins one supervised
        # process; a run id pins one attempt at a task; a session id pins the
        # conversation. All three are stable and machine-readable, unlike the
        # profile name.
        for key in (
            "HERMES_EXECUTION_ID",
            "HERMES_KANBAN_RUN_ID",
            "HERMES_SESSION_ID",
        ):
            candidate = (environ.get(key) or "").strip()
            if candidate:
                run_id = candidate
                break

    if cause is None:
        cause = (
            CREATION_CAUSE_MANUAL_RELAY
            if kind == ACTOR_KIND_HUMAN_INTERACTIVE
            else CREATION_CAUSE_AUTOMATED
        )

    return ActorProvenance(
        kind=kind,
        actor_id=actor_id,
        lane=lane,
        run_id=run_id,
        cause=cause,
    )


def task_provenance(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """Structured provenance for ``task_id``, or ``None`` if the card is gone.

    Always returns every key, so a caller can distinguish "not recorded"
    (values ``None``, a pre-migration row) from a recorded ``unknown``.
    ``created_by`` is included for audit continuity only — no caller should
    branch on it, and the regressions assert that branching on it fails.
    """
    row = conn.execute(
        "SELECT created_by, actor_kind, actor_id, actor_lane, actor_run_id, "
        "creation_cause, recovery_owner, executor_lane, session_id "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "created_by": row["created_by"],
        "actor_kind": row["actor_kind"],
        "actor_id": row["actor_id"],
        "actor_lane": row["actor_lane"],
        "actor_run_id": row["actor_run_id"],
        "creation_cause": row["creation_cause"],
        "recovery_owner": row["recovery_owner"],
        "executor_lane": row["executor_lane"],
        "session_id": row["session_id"],
    }


def is_human_relayed(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether a human was in the loop when this card was created.

    Fails CLOSED on an unrecorded provenance: a pre-migration row returns
    ``False``, because "we did not record it" is not evidence a human relayed
    it. The alternative — inferring from ``created_by`` — is the defect.
    """
    prov = task_provenance(conn, task_id)
    if not prov:
        return False
    return prov.get("actor_kind") in HUMAN_IN_LOOP_ACTOR_KINDS


def is_governed_automation(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether a governed automated executor created this card."""
    prov = task_provenance(conn, task_id)
    if not prov:
        return False
    return prov.get("actor_kind") == ACTOR_KIND_GOVERNED_AUTOMATION


PRODUCTION_ROLES_REGISTRY_ENV = "HERMES_PRODUCTION_ROLES_REGISTRY"
PRODUCTION_ACTION_AUTHORIZED_EVENT = "production_action_authorized"


class ProductionActionError(ValueError):
    """A ``production_actions`` grant was refused (unknown role, missing
    registry, or an actor that may not grant production authority)."""


def production_roles_registry_path() -> Path:
    """Path of the installed production-roles registry.

    ``HERMES_PRODUCTION_ROLES_REGISTRY`` overrides it (tests, staging a
    registry for review); otherwise the installed copy under the user's
    ``~/.hermes/security/production-roles/``.
    """
    override = (os.environ.get(PRODUCTION_ROLES_REGISTRY_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "security" / "production-roles" / "production-roles.json"


def load_production_roles(path: Optional[Path] = None) -> dict[str, dict]:
    """Return the ``roles`` map of the installed registry.

    Raises :class:`ProductionActionError` when the registry is missing or
    malformed. Callers that merely want to know whether any registry exists
    should catch it; ``create_task`` deliberately does not, because a grant
    against an unverifiable registry is exactly what rule 6 forbids.
    """
    reg_path = Path(path) if path is not None else production_roles_registry_path()
    try:
        raw = json.loads(reg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProductionActionError(
            f"production-roles registry not found at {reg_path}; refusing to "
            "grant production authority without an installed registry"
        ) from None
    except (OSError, ValueError) as exc:
        raise ProductionActionError(
            f"production-roles registry at {reg_path} is unreadable: {exc}"
        ) from exc
    roles = raw.get("roles") if isinstance(raw, dict) else None
    if not isinstance(roles, dict) or not roles:
        raise ProductionActionError(
            f"production-roles registry at {reg_path} declares no roles"
        )
    return roles


def validate_production_actions(
    roles: Optional[Iterable[str]],
    *,
    registry_path: Optional[Path] = None,
) -> list[str]:
    """Normalise and validate a requested ``production_actions`` list.

    Returns the de-duplicated role names in request order (empty list for
    ``None`` / no non-blank entries). Any name not present in the installed
    registry raises :class:`ProductionActionError` naming the offending
    role and the roles that do exist.
    """
    if roles is None:
        return []
    if isinstance(roles, str):
        roles = [roles]
    cleaned: list[str] = []
    for r in roles:
        name = str(r or "").strip()
        if name and name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        return []
    known = load_production_roles(registry_path)
    unknown = [name for name in cleaned if name not in known]
    if unknown:
        raise ProductionActionError(
            f"unknown production role(s): {', '.join(unknown)}; the installed "
            f"registry defines: {', '.join(sorted(known))}"
        )
    return cleaned


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


def create_task(
    conn: sqlite3.Connection, *, title: str, body: Optional[str] = None,
    assignee: Optional[str] = None, created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None, workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None, tenant: Optional[str] = None, priority: int = 0,
    parents: Iterable[str] = (), triage: bool = False, idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None, skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None, model_override: Optional[str] = None,
    provider_override: Optional[str] = None, reasoning_effort: Optional[str] = None,
    goal_mode: bool = False, goal_max_turns: Optional[int] = None, initial_status: str = "running",
    session_id: Optional[str] = None, board: Optional[str] = None, project_id: Optional[str] = None,
    project_source_task_id: Optional[str] = None,
    creator_task_id: Optional[str] = None,
    completion_contract: Optional[str] = None,
    executor_lane: Optional[str] = None,
    recovery_gate_cmd: Optional[str] = None,
    gauntlet: Optional[bool] = None,
    provenance: Optional[ActorProvenance] = None,
    recovery_owner: Optional[str] = None,
    repairs_task_id: Optional[str] = None,
    umbrella_task_id: Optional[str] = None,
    control_plane_authority: Optional[str] = None,
    production_actions: Optional[Iterable[str]] = None,
) -> str:
    """Create a task (optionally under ``parents``); returns its id.

    Status: ``ready`` unless a parent is not ``done`` (``todo``); ``triage=True``
    forces ``triage``; ``initial_status="blocked"`` parks it for human ops.
    ``idempotency_key``: an existing non-archived task with the key is returned
    instead of a duplicate. ``max_runtime_seconds``: cap before the dispatcher
    SIGTERMs and re-queues. ``model_override``/``provider_override`` pin the
    worker model (provider requires model); ``reasoning_effort`` is independent.
    ``creator_task_id``: inherit durable session/subscriptions independently of
    dependency edges; an explicit ``session_id`` still wins.
    ``project_source_task_id``: cross-profile fallback when ``project_id`` is not
    in the active profile's projects.db — see ``_resolve_project_link``.
    ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.

    ``provenance`` records WHO created this card and WHY, structurally. When
    omitted it is derived from the process environment by
    :func:`resolve_actor_provenance` — deriving is safe because the derivation
    reads mechanical facts about the process (is there a governed run around
    it?), never the identity string. It is stored alongside, and never
    instead of, ``created_by``: the string stays for audit continuity, but
    nothing may branch on it. See the ACTOR PROVENANCE block.

    ``recovery_owner`` names the identity accountable for this card's recovery
    decision, and ``repairs_task_id``/``umbrella_task_id`` are the subject and
    umbrella a governed repair card must point at.

    **A card that declares itself a repair is refused unless all three are
    present, and the relations are established in this same transaction.**
    Anything that marks the card as a repair arms the requirement — a
    ``recovery`` creation cause, a ``recovery_owner``, or either linkage id.
    The guard lives HERE, at the creation boundary, rather than only in
    :func:`create_repair_task`, because a guard that only one helper honours
    is not a guard: ``tools/kanban_tools.py`` and every other caller reaches
    ``create_task`` directly, so a recovery-tagged card could otherwise be
    minted with no subject and no umbrella just by not using the helper. That
    bypass was found by independent ``codex_verify`` review of this repair's
    first draft, which created exactly such an orphan.
    """
    from hermes_cli.kanban_db_graph import initial_task_state, inherit_creator_origin
    from hermes_cli.kanban_pr_acceptance import validate_contract

    completion_contract = validate_contract(completion_contract)
    model_override, provider_override = _validate_model_override(model_override, provider_override)
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    # Rule 6: validate the production authorization before anything is
    # written, and refuse it from a dispatcher-owned worker. The env check
    # comes first so a worker learns it may not grant authority at all, not
    # merely that it named a role wrong.
    production_actions_list: list[str] = []
    if production_actions is not None:
        requested = (
            [production_actions] if isinstance(production_actions, str)
            else [str(r or "").strip() for r in production_actions]
        )
        if any(requested):
            # Presence, not value: an empty or blank HERMES_KANBAN_TASK still marks
            # a worker environment (Codex t_7e126eb3 / t_2f9a1fd4).
            if "HERMES_KANBAN_TASK" in os.environ:
                raise ProductionActionError(
                    "a dispatcher-owned worker cannot grant production "
                    "authority to another card (HERMES_KANBAN_TASK is set); "
                    "production_actions may only be set from an interactive "
                    "session"
                )
            production_actions_list = validate_production_actions(requested)
    # Resolve before any validation that can raise, so a rejected creation
    # never half-populates provenance state, and so every successful creation
    # carries it — there is no path through this function that produces a card
    # with no recorded actor kind.
    if provenance is None:
        provenance = resolve_actor_provenance()
    recovery_owner = (recovery_owner or "").strip() or None
    repairs_task_id = (repairs_task_id or "").strip() or None
    umbrella_task_id = (umbrella_task_id or "").strip() or None
    # Any one of these declares "this card is a governed repair", and each is
    # sufficient on its own: a caller that names an owner but no subject is as
    # much an orphan as one that names a subject but no owner. Arming on the
    # union means the requirement cannot be dodged by supplying a partial set.
    declares_repair = (
        provenance.cause == CREATION_CAUSE_RECOVERY
        or recovery_owner is not None
        or repairs_task_id is not None
        or umbrella_task_id is not None
    )
    if declares_repair:
        # Normalise the cause so the card is queryable as recovery work
        # however it was declared — otherwise ``orphaned_repair_tasks`` and
        # every other cause-based query would miss it.
        if provenance.cause != CREATION_CAUSE_RECOVERY:
            provenance = ActorProvenance(
                kind=provenance.kind,
                actor_id=provenance.actor_id,
                lane=provenance.lane,
                run_id=provenance.run_id,
                cause=CREATION_CAUSE_RECOVERY,
            )
        for label, value in (
            ("recovery_owner", recovery_owner),
            ("repairs_task_id", repairs_task_id),
            ("umbrella_task_id", umbrella_task_id),
        ):
            if not value:
                raise RecoveryLinkageError(
                    f"a governed repair task requires {label}; refusing to "
                    f"create an unlinked recovery card "
                    f"(cause={provenance.cause!r})"
                )
    # ── Control-plane admission control ────────────────────────────────────
    # The 2026-08-31 closure declared the Gauntlet closed as construction work.
    # That rule lived in a vault note nothing read, so 251 control-plane cards
    # were created after it. It is enforced here, at the creation boundary, for
    # the same reason declares_repair is: every caller reaches create_task, so
    # a guard anywhere else is optional.
    control_plane_authority = (control_plane_authority or "").strip() or None
    is_control_plane = control_plane_subject(title, body)
    parent_ids_for_admission = [str(p) for p in (parents or ()) if p]
    if not is_control_plane and parent_ids_for_admission:
        # Inheritance: a generically-titled child of control-plane work is
        # control-plane work. Without this a chain launders itself clean by
        # the second card simply by not naming its subject.
        marks = ",".join("?" * len(parent_ids_for_admission))
        try:
            inherited = conn.execute(
                f"SELECT 1 FROM tasks WHERE id IN ({marks}) AND control_plane = 1 LIMIT 1",
                tuple(parent_ids_for_admission),
            ).fetchone()
        except sqlite3.Error:
            inherited = None
        is_control_plane = inherited is not None

    if is_control_plane:
        creator_is_human = provenance.kind == ACTOR_KIND_HUMAN_INTERACTIVE
        if control_plane_authority and not control_plane_authority.startswith(
            CONTROL_PLANE_AUTHORITY_PREFIXES
        ):
            raise ControlPlaneAdmissionError(
                f"control_plane_authority must start with one of "
                f"{list(CONTROL_PLANE_AUTHORITY_PREFIXES)}; got "
                f"{control_plane_authority!r}. The closure requires the "
                f"justification be NAMED, not asserted."
            )
        if not creator_is_human and not control_plane_authority:
            raise ControlPlaneAdmissionError(
                f"refusing automated creation of control-plane work "
                f"({title.strip()[:60]!r}). The Gauntlet is closed as "
                f"construction work; reopening it requires a named "
                f"falsifiable defect, new requirement, or verified regression. "
                f"Pass control_plane_authority='operator:<who>' / "
                f"'regression:<id>' / 'requirement:<id>' / 'defect:<evidence>'."
            )
        if not creator_is_human:
            spawner = _creating_actor_control_plane_task(conn, provenance)
            if spawner is not None and not control_plane_authority.startswith(
                "operator:"
            ):
                raise ControlPlaneAdmissionError(
                    f"refusing second-generation control-plane work: card "
                    f"{spawner} is itself control-plane and may not "
                    f"automatically create another. This is the recursion that "
                    f"consumed 44% of board compute 2026-09-01..06. Only "
                    f"'operator:<who>' authority admits it."
                )

    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
    # A project-scoped board anchors every new task to its project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    # An explicit ``scratch`` (or ``project_id=""``) is a request for no project:
    # it must not be upgraded to a worktree in the board's repo (#106342).
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (_board_meta_for(board).get("project_id") or "").strip() or None
        except Exception:
            pass
    if workspace_kind is None:
        workspace_kind = "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    executor_lane = (executor_lane or "").strip() or None
    recovery_gate_cmd = (recovery_gate_cmd or "").strip() or None
    # ``None`` (the default) inherits the board-wide config so operators who
    # enabled enforcement get it stamped on new rows; an explicit True/False
    # from the caller always wins.
    gauntlet_enforced = (
        _resolve_gauntlet_default(title, body) if gauntlet is None
        else bool(gauntlet)
    )
    # A governed repair is never an ordinary task, even when a caller reaches
    # this kernel boundary directly with ``gauntlet=False``.  The validation
    # loop is part of the repair contract, not an opt-in convention.
    if declares_repair:
        gauntlet_enforced = True
    # Simple operator/Erika shorthand: assignee="claude" means the direct
    # Claude executor lane, not a Hermes profile named "claude". Persist the
    # spawnable default profile as the dispatcher carrier; cli.py exits into
    # Claude before any Hermes agent/tool loop is built.
    assignee, executor_lane, _ = _normalize_shorthand_lane(assignee, executor_lane)

    # An explicit in-body verification requirement is binding, not advisory.
    # Additive to the classifier above and not overridable by gauntlet=False:
    # the body is the author's own statement of the contract. Verifier cards
    # are exempt -- their bodies describe the requirement they fulfil.
    explicit_verification_required = False
    if (
        not gauntlet_enforced
        and executor_lane not in INDEPENDENT_VERIFIER_LANES
        and explicit_verification_requirement(title, body)
    ):
        gauntlet_enforced = True
        explicit_verification_required = True

    if executor_lane is not None and executor_lane not in VALID_EXECUTOR_LANES:
        raise ValueError(
            f"executor_lane must be one of {sorted(VALID_EXECUTOR_LANES)} or None, "
            f"got {executor_lane!r}"
        )
    if executor_lane == EXECUTOR_LANE_CLAUDE_RECOVERY and not recovery_gate_cmd:
        raise ValueError(
            "recovery_gate_cmd is required when "
            f"executor_lane={EXECUTOR_LANE_CLAUDE_RECOVERY!r}"
        )
    if recovery_gate_cmd and executor_lane != EXECUTOR_LANE_CLAUDE_RECOVERY:
        raise ValueError(
            "recovery_gate_cmd is only valid with "
            f"executor_lane={EXECUTOR_LANE_CLAUDE_RECOVERY!r}"
        )
    # A verifier with no subject is a verdict with no destination. The whole
    # return path (``_return_verifier_verdict_to_subjects``) resolves its
    # subjects through ``parent_ids``, so a ``codex_verify`` card created with
    # no parent cannot deliver a PASS, a FAIL or a BLOCKER to anyone — it runs,
    # renders a correct verdict, completes, and the work it judged stays parked
    # forever. That is not hypothetical: on the live board seven of fourteen
    # verifier cards are exactly this shape, including two that closed a
    # production deployment.
    #
    # Refused HERE, at the creation boundary, and read back again inside the
    # creating transaction below — for the reason the repair-linkage guard
    # already learned the hard way: a rule only ``create_repair_task`` honoured
    # was bypassed by the first independent review that called ``create_task``
    # directly. Every door, or it is not a rule.
    if executor_lane in INDEPENDENT_VERIFIER_LANES and not [
        p for p in parents if p
    ]:
        raise VerifierLinkageError(
            "an independent verifier task requires at least one parent naming "
            "the subject it verifies; refusing to create an orphan "
            f"{executor_lane!r} card whose verdict could not be "
            "returned to anyone"
        )
    # D8: the same gate ``assign_task`` has always applied. Deliberately AFTER
    # the shorthand -> executor_lane rewrite above, so "claude"/"atlas" still
    # create the (assignee="default", executor_lane=...) pair they always did,
    # and only genuinely undispatchable names are refused. An unassigned card
    # (assignee=None) is still legal — triage cards are created that way.
    _assert_assignee_dispatchable(assignee, context="cannot create task")
    # Company isolation (rule 3): the same lane check every claim path applies,
    # asked here while the caller is still around to be told.
    _assert_assignee_in_lane(tenant, assignee, context="cannot create task")

    project_id, project_obj, project_repo, workspace_kind = _resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path
    )
    parents = tuple(p for p in parents if p)
    skills_list = _normalize_task_skills(skills)

    # Idempotency check BEFORE the write txn (no lock held); a concurrent-create
    # race may insert twice, the next lookup stabilises on the newest.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1", (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    now = int(time.time())

    # Only persistent kinds inherit the board ``default_workdir``: a scratch
    # task inheriting it would point cleanup at the user's source tree.
    if workspace_path is None and project_repo is None and workspace_kind in {"dir", "worktree"}:
        board_default = _board_meta_for(board).get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            # allow_nested: graph builders compose create_task under one outer
            # commit so the dispatcher never sees a half-built graph.
            with write_txn(conn, allow_nested=True):
                task_status, tenant = initial_task_state(conn, parents, initial_status, triage, tenant)
                # Project worktree: fresh dir under the repo + deterministic
                # branch, instead of the random ``wt/<id>`` worker fallback.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(project_repo, ".worktrees", task_id)
                    if not branch_name:
                        branch_name = _project_branch_name(project_obj, task_id, title)

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        reasoning_effort,
                        goal_mode, goal_max_turns, session_id, completion_contract,
                        executor_lane, recovery_gate_cmd,
                        gauntlet_enforced,
                        actor_kind, actor_id, actor_lane, actor_run_id,
                        creation_cause, recovery_owner,
                        control_plane, control_plane_authority,
                        production_actions
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, title.strip(), body, assignee, task_status, priority,
                        created_by, now, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        _opt_int(max_runtime_seconds),
                        json.dumps(skills_list) if skills_list is not None else None,
                        _opt_int(max_retries), model_override, provider_override, reasoning_effort,
                        1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,
                        executor_lane,
                        recovery_gate_cmd,
                        1 if gauntlet_enforced else 0,
                        provenance.kind,
                        provenance.actor_id,
                        provenance.lane,
                        provenance.run_id,
                        provenance.cause,
                        recovery_owner,
                        1 if is_control_plane else 0,
                        control_plane_authority,
                        json.dumps(production_actions_list),
                    ),
                )
                for pid in parents:
                    _link(conn, pid, task_id)
                if declares_repair:
                    # Same transaction as the INSERT above. When this runs
                    # standalone the enclosing write_txn is a real
                    # BEGIN IMMEDIATE/COMMIT; when composed under
                    # create_repair_task it is a savepoint inside that outer
                    # transaction. Either way a raise below unwinds the card
                    # with the linkage, so there is no committed state in
                    # which the card exists unlinked.
                    for relation, target in (
                        (RELATION_REPAIRS, repairs_task_id),
                        (RELATION_UMBRELLA, umbrella_task_id),
                    ):
                        try:
                            add_task_relation(
                                conn, task_id, target, relation,
                                created_by=recovery_owner,
                            )
                        except ValueError as exc:
                            # An unknown or self-referential endpoint. Re-raise
                            # as the linkage error so every fail-closed path
                            # out of this function has one type.
                            raise RecoveryLinkageError(
                                f"cannot establish {relation} linkage to "
                                f"{target!r}: {exc}"
                            ) from exc
                    # Read back from the table, not from the calls above. An
                    # INSERT OR IGNORE that inserted nothing returns cleanly,
                    # so a caller's own success flag is not evidence that the
                    # board holds the link.
                    still_missing = missing_repair_relations(conn, task_id)
                    if still_missing:
                        raise RecoveryLinkageError(
                            f"repair task {task_id} is missing required "
                            f"relation(s) {still_missing}; rolling back so no "
                            f"orphan card is created"
                        )
                    # Arm the existing durable recheck before the transaction
                    # can expose the repair as dispatchable.  This closes the
                    # administrative bypass where a repair was unlinked,
                    # promoted, and claimed without supervisory state.
                    arm_observation_timer(
                        conn, task_id, kind=VALIDATION_LOOP_TIMER_KIND,
                        owner=recovery_owner, now=now,
                    )
                if executor_lane in INDEPENDENT_VERIFIER_LANES:
                    # Read the linkage back from ``task_links``, in the same
                    # transaction that wrote it and before it can commit. The
                    # early refusal above checked the caller's ARGUMENT; this
                    # checks the BOARD. ``INSERT OR IGNORE`` above returns
                    # cleanly when it inserted nothing (a parent id that does
                    # not exist, a FK that did not hold), so the argument being
                    # non-empty is not evidence that the edge landed.
                    #
                    # Owner is checked here too, and for a different failure:
                    # a linked verifier whose accountable identity cannot be
                    # resolved produces a verdict that reaches a subject with
                    # nobody answerable for acting on it. Both are refused as
                    # one error type so every fail-closed exit from this
                    # function stays diagnosable.
                    missing_verifier = missing_verifier_linkage(conn, task_id)
                    if missing_verifier:
                        raise VerifierLinkageError(
                            f"verifier task {task_id} is missing required "
                            f"linkage {missing_verifier}; rolling back so no "
                            f"orphan verifier is created"
                        )
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "creator_task_id": creator_task_id,
                        "tenant": tenant,
                        "workspace_kind": workspace_kind,
                        "workspace_path": workspace_path,
                        "branch_name": branch_name,
                        "project_id": project_id,
                        "skills": list(skills_list) if skills_list else None,
                        "goal_mode": bool(goal_mode) or None,
                        "model_override": model_override,
                        "provider_override": provider_override,
                        "executor_lane": executor_lane,
                        # Provenance goes on the event too, not only the row.
                        # The row can be UPDATEd; the event log is append-only,
                        # so this is the copy that survives a later mutation
                        # and the one an audit reads.
                        **provenance.as_dict(),
                        "recovery_owner": recovery_owner,
                        **(
                            {"gauntlet_source": "explicit_body_requirement"}
                            if explicit_verification_required else {}
                        ),
                    },
                )
                if production_actions_list:
                    # Rule 6: the grant lives in append-only history too, not
                    # only on the (trigger-protected) row.
                    _append_event(
                        conn,
                        task_id,
                        PRODUCTION_ACTION_AUTHORIZED_EVENT,
                        {
                            "roles": list(production_actions_list),
                            "created_by": created_by,
                        },
                    )
                if task_status == "blocked":
                    _append_event(
                        conn,
                        task_id,
                        "blocked",
                        {"reason": "initial_status", "status": "blocked", "actor": created_by or "user"},
                    )
                if task_status == "todo":
                    # Parked behind an open parent: record why, exactly as
                    # link_tasks does, so the board never shows an unexplained todo.
                    gating = [p for p in parents if _task_status(conn, p) not in ("done", "archived")]
                    if gating:
                        _append_event(
                            conn,
                            task_id,
                            "dependency_wait",
                            {"reason": "parent_not_done", "parent": gating[0]},
                        )
                # ACK-edge: the originating channel hears a child BLOCK, not just the fan-in.
                inherit_creator_origin(conn, task_id, creator_task_id, created_at=now)
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            # Production ledger projection (post-commit, best-effort). See
            # _sync_production_ledger docstring. Moved here from the MCP
            # tool-layer wrapper (tools/kanban_tools.py) so every direct
            # caller of create_task (CLI, recovery_lane.py, kanban_swarm.py)
            # gets the same projection the MCP tool always had, instead of
            # only the MCP path — closing the gap identified in
            # t_da07457d's FAIL verdict on t_faf6e26a (single authoritative
            # path requirement).
            _sync_production_ledger(conn, task_id, status=task_status, phase="create")
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def _board_meta_for(board: Optional[str]) -> dict:
    return read_board_metadata(board if board else get_current_board())


def _project_branch_name(project_obj: Any, task_id: str, title: Optional[str]) -> Optional[str]:
    from hermes_cli import projects_db as _pdb

    try:
        return _pdb.branch_name_for(project_obj, task_id, title=title or "")
    except Exception:
        return None


def _link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )


def _missing_task_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Subset of ``ids`` (order kept) with no ``tasks`` row."""
    ids = list(ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in ids if p not in present]


def _inherit_notify_subs(
    conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *,
    created_at: Optional[int] = None,
) -> None:
    """Copy parents' notify subscriptions to a child, cursor caught up to the
    child's current event so a late ``link_tasks`` never replays history.

    Single owner of inheritance (create_task, link_tasks, decompose). It must
    copy EVERY routing/delivery column: dropping ``chat_type`` made DM-originated
    completions wake a fresh group session instead of the originating DM.

    Omitting columns here silently degrades routing: a DM-originated child completion falls back to
    chat_type='group' and wakes a fresh group-scoped session instead of the originating DM (issue #73030).
    """
    parent_ids = tuple(dict.fromkeys(p for p in parents if p))
    if not parent_ids:
        return
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?", (child_id,),
    ).fetchone()
    cursor = int(row["cursor"] if row is not None else 0)
    placeholders = ",".join("?" * len(parent_ids))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,
             chat_type, notifier_profile, delivery_mode, delivery_metadata,
             created_at, last_event_id)
        SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,
               COALESCE(chat_type, 'dm'), notifier_profile,
               COALESCE(delivery_mode, 'notify'), delivery_metadata, ?, ?
          FROM kanban_notify_subs
         WHERE task_id IN ({placeholders})
        """,
        (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids),
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


def task_admission(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """How a card was admitted: who created it, as what kind of actor, and
    under which named control-plane authority (if any).

    Read-only. This is the provenance the tool surface consults when a
    dispatcher-owned worker asks to exercise an *operator* decision (retiring
    a global lesson, superseding another card): the kernel records at creation
    whether a human relayed the card (``actor_kind``) and whether a named
    ``operator:`` justification admitted it (``control_plane_authority``), and
    those facts — not the worker's own claim about itself — decide whether the
    authority was delegated. ``Task`` deliberately does not carry
    ``control_plane_authority``, so it is read here instead of by callers
    reaching into the row.
    """
    # ``SELECT *`` on purpose: like ``get_task``, this must open a legacy board
    # that predates the provenance columns rather than fail on the read.
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    keys = set(row.keys())
    return {
        "task_id": row["id"],
        "assignee": row["assignee"],
        "created_by": row["created_by"] if "created_by" in keys else None,
        "tenant": row["tenant"] if "tenant" in keys else None,
        "actor_kind": row["actor_kind"] if "actor_kind" in keys else None,
        "actor_id": row["actor_id"] if "actor_id" in keys else None,
        "control_plane": bool(row["control_plane"]) if "control_plane" in keys else False,
        "control_plane_authority": (
            row["control_plane_authority"]
            if "control_plane_authority" in keys else None
        ),
    }


VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
    "completed-desc": "completed_at DESC NULLS LAST, id DESC",
}


def list_tasks(
    conn: sqlite3.Connection, *, assignee: Optional[str] = None, status: Optional[str] = None,
    tenant: Optional[str] = None, session_id: Optional[str] = None, include_archived: bool = False,
    limit: Optional[int] = None, order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None, current_step_key: Optional[str] = None,
    terminal_disposition: Optional[str] = None,
) -> list[Task]:
    """List tasks. ``terminal_disposition`` filters on how cards ended (one of
    :data:`VALID_TERMINAL_DISPOSITIONS`); pass it with ``include_archived=True``
    to report on overtaken cards, which are archived by construction.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    if terminal_disposition is not None and terminal_disposition not in VALID_TERMINAL_DISPOSITIONS:
        raise ValueError(
            "terminal_disposition must be one of "
            f"{sorted(VALID_TERMINAL_DISPOSITIONS)}"
        )
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    for col, val in (
        ("assignee", _canonical_assignee(assignee)), ("status", status), ("tenant", tenant),
        ("session_id", session_id), ("workflow_template_id", workflow_template_id),
        ("current_step_key", current_step_key), ("terminal_disposition", terminal_disposition),
    ):
        if val is not None:
            query += f" AND {col} = ?"
            params.append(val)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}")
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.

    Raises ``ValueError`` if ``profile`` is neither an existing Hermes
    profile nor one of the two recognized legacy shorthand tokens
    ("claude", "atlas"). Executor-lane identifiers such as
    ``EXECUTOR_LANE_CODEX_VERIFY`` ("codex_verify") or
    ``EXECUTOR_LANE_CLAUDE_RECOVERY`` belong in the ``executor_lane``
    column, never in ``assignee``.

    The two legacy shorthands ARE accepted and are normalized here, in the
    same call, to (assignee="default", executor_lane=...) — see
    :func:`_normalize_shorthand_lane`. That normalization used to happen only
    at create time and lazily during ready-queue dispatch (``status='ready'``
    rows only), so a task reassigned to "atlas"/"claude" while already in
    another status — 'review' in every observed case — kept the raw token,
    failed ``profile_exists`` in both dispatch loops, and stalled forever with
    no exception and no dispatcher signal (t_500a3503 / t_f2d639a3 packet;
    t_6b7d5845 and t_06e046f1 as live casualties, defect D5). The
    ``profile_exists`` gate already ran on every status; the translation now
    does too.
    """
    requested = _canonical_assignee(profile)
    # Shared with create_task and request_review since D8 — one vocabulary for
    # every writer of the assignee column, so the gate cannot be true on one
    # path and absent on another (that asymmetry WAS the defect).
    _assert_assignee_dispatchable(requested, context=f"cannot assign {task_id!r}")
    # An explicit shorthand assignment is unambiguous lane intent (this
    # signature has no executor_lane parameter to conflict with), so it sets
    # the lane outright rather than deferring to whatever lane the row carries.
    profile, lane, from_token = _normalize_shorthand_lane(requested, None)
    preserved_subject_implementer: Optional[str] = None
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee, executor_lane, "
            "gauntlet_enforced, verification_state, tenant "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        # Company isolation (rule 3), at the assignment boundary.
        _assert_assignee_in_lane(
            row["tenant"], profile, context=f"cannot assign {task_id!r}",
        )
        # Same carve-out request_review applies: on a Gauntlet subject that is
        # awaiting verification, "atlas" means "open the independent verifier
        # route", never "make the subject its own verification artifact". On
        # 2026-09-14 ``reassign t_3883034a atlas`` relabelled the parked subject
        # to executor_lane=codex_verify (event 151185).
        preserve_subject = (
            lane == EXECUTOR_LANE_CODEX_VERIFY
            and from_token is not None
            and row["executor_lane"] != EXECUTOR_LANE_CODEX_VERIFY
            and bool(row["gauntlet_enforced"] or gauntlet_enforcement_default())
            and (
                row["verification_state"] == VERIFICATION_PENDING
                or row["status"] in ("review", "triage")
            )
        )
        if preserve_subject:
            _append_event(
                conn, task_id, "independent_verifier_lane_requested",
                {
                    "from_reviewer": from_token,
                    "required_lane": EXECUTOR_LANE_CODEX_VERIFY,
                    "subject_executor_lane_preserved": True,
                    "source": "assign_task",
                    "status": row["status"],
                },
            )
            preserved_subject_implementer = _review_requested_implementer(
                conn, task_id, row["assignee"],
            ) or ""
        else:
            # Explicit assignment to a real Hermes profile is authoritative
            # ownership of execution. A stale direct executor lane must not
            # survive that reassignment, otherwise the dispatcher can rewrite
            # the card back onto default/claude or default/codex_verify.
            # Only the legacy shorthand tokens (claude/atlas) explicitly set
            # a direct executor lane.
            if from_token is None:
                lane = None
                lane_sql = ", executor_lane = NULL"
                lane_params: tuple[Any, ...] = ()
            else:
                lane_sql = ", executor_lane = ?"
                lane_params = (lane,)
            # Compare the whole executor identity, not just the profile string: a
            # task already on assignee='default' that is being moved onto the
            # codex_verify lane IS a reassignment, and must not inherit the
            # previous executor's failure streak just because the carrier profile
            # name happens to be unchanged.
            changed = row["assignee"] != profile or (
                from_token is not None and row["executor_lane"] != lane
            )
            if changed:
                # The retry guard is scoped to the task/profile combination. A
                # human reassigning the task is an explicit recovery action, so the
                # new profile should not inherit the previous profile's streak.
                conn.execute(
                    "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                    "last_failure_error = NULL" + lane_sql + " WHERE id = ?",
                    (profile, *lane_params, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET assignee = ?" + lane_sql + " WHERE id = ?",
                    (profile, *lane_params, task_id),
                )
            if from_token is not None:
                # Same event kind the ready-queue dispatcher emits, so the audit
                # trail reads identically whichever path did the translation.
                _append_event(
                    conn, task_id, "executor_lane_normalized",
                    {
                        "from_assignee": from_token,
                        "assignee": profile,
                        "executor_lane": lane,
                        "source": "assign_task",
                        "status": row["status"],
                    },
                )
            # ``from`` lets the respawn guard tell a real handoff (dev→closer)
            # from a no-op re-assign or an unassign, which must not lift
            # ``active_pr``.
            _append_event(
                conn, task_id, "assigned",
                {
                    "assignee": profile, "from": row["assignee"],
                    "requested": requested, "executor_lane": lane,
                }
                if from_token is not None
                else {"assignee": profile, "from": row["assignee"]},
            )
    if preserved_subject_implementer is not None:
        # Post-commit, like request_review's routing block: the preserved
        # subject is durable first, and opening the route is evidence-gated
        # and fail-closed inside the helper.
        if gauntlet_required(conn, task_id):
            _ensure_independent_verifier_child(
                conn, task_id, implementer=preserved_subject_implementer or None,
                # assignee='atlas' is an explicit Codex-lane request.
                preferred_lane=EXECUTOR_LANE_CODEX_VERIFY,
            )
        return True
    # Observer fires AFTER commit so subscribers see durable state.
    notify_task_updated(
        conn,
        task_id,
        ("assignee", "executor_lane") if from_token is not None else ("assignee",),
    )
    return True


def restore_relabelled_subject_lane(
    conn: sqlite3.Connection, task_id: str, *, actor: str,
) -> tuple[bool, Optional[str]]:
    """Undo a shorthand relabel that put a Gauntlet subject on codex_verify.

    The repair for damage :func:`assign_task` no longer produces but live
    boards still carry (t_3883034a, event 151185). Restores ``executor_lane``
    to the value the card was created with, and nothing else: status,
    assignee, verification ledger and history are untouched, and an
    append-only ``subject_executor_lane_restored`` event records the repair.

    Refuses (returns ``(False, None)``) unless every condition that identifies
    the damage holds: the card is on codex_verify, is not a dependency child
    (a real verifier card always has its subject as parent), was handed off
    for review, carries an ``executor_lane_normalized`` relabel event, is not
    claimed, and was not itself created on codex_verify.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT executor_lane, claim_lock FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if (
            row is None
            or row["executor_lane"] != EXECUTOR_LANE_CODEX_VERIFY
            or row["claim_lock"] is not None
        ):
            return False, None
        if conn.execute(
            "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task_id,),
        ).fetchone() is not None:
            return False, None
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'review_requested' LIMIT 1",
            (task_id,),
        ).fetchone() is None:
            return False, None
        relabel = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "AND kind = 'executor_lane_normalized' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if relabel is None:
            return False, None
        created = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
            "ORDER BY id ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        try:
            created_payload = json.loads(created["payload"]) if created and created["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            return False, None
        original = created_payload.get("executor_lane") if isinstance(created_payload, dict) else None
        if original == EXECUTOR_LANE_CODEX_VERIFY:
            return False, None
        cur = conn.execute(
            "UPDATE tasks SET executor_lane = ? WHERE id = ? AND executor_lane = ? "
            "AND claim_lock IS NULL",
            (original, task_id, EXECUTOR_LANE_CODEX_VERIFY),
        )
        if cur.rowcount != 1:
            return False, None
        _append_event(
            conn, task_id, "subject_executor_lane_restored",
            {
                "from": EXECUTOR_LANE_CODEX_VERIFY,
                "to": original,
                "relabel_event": int(relabel["id"]),
                "actor": actor,
            },
        )
    return True, original


def set_model_override(
    conn: sqlite3.Connection, task_id: str, model: Optional[str], provider: Optional[str] = None,
) -> bool:
    """Set (empty ``model`` clears BOTH) the per-task model/provider override.
    Allowed while ``running``: it applies on the NEXT dispatch, which is the
    rate-limit-recovery flow (set, then reclaim/retry)."""
    model, provider = _validate_model_override(model, provider)
    return _set_task_override(
        conn, task_id,
        "UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?", (model, provider),
        "model_override_set", {"model": model, "provider": provider},
        ("model_override", "provider_override"), archived_msg="cannot set model override",
    )


def _set_task_override(
    conn: sqlite3.Connection, task_id: str, sql: str, params: tuple, event_kind: str, payload: dict,
    changed_fields: tuple[str, ...], *, archived_msg: str,
) -> bool:
    """Per-task override write: refuse archived tasks, record ``event_kind``,
    then fire the task-updated observer AFTER commit (RFC #58548)."""
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"{archived_msg} on archived task {task_id}")
        conn.execute(sql, (*params, task_id))
        _append_event(conn, task_id, event_kind, payload)
    notify_task_updated(conn, task_id, changed_fields)
    return True


def set_reasoning_effort(conn: sqlite3.Connection, task_id: str, effort: Optional[str]) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = normalize_reasoning_effort(effort)
    return _set_task_override(
        conn, task_id, "UPDATE tasks SET reasoning_effort = ? WHERE id = ?", (effort,),
        "reasoning_effort_set", {"reasoning_effort": effort},
        ("reasoning_effort",), archived_msg="cannot set reasoning effort",
    )


def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    expected_child_run_id: Optional[int] = None,
) -> bool:
    """Link ``parent_id -> child_id``. Returns True when the link gated a
    ``ready`` child back to ``todo`` (the new parent is not yet terminal), so
    callers can surface the demotion instead of a silent status flip.

    A running child cannot normally be gated retroactively, so reject the edge
    rather than record a dependency that did not constrain the active run. The
    owning worker may link its own active run for a subsequent dependency-block
    handoff by supplying its trusted ``expected_child_run_id``.
    """
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gated = False
    with write_txn(conn):
        missing = _missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        child = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
        if child["status"] == "running" and (
            expected_child_run_id is None
            or child["current_run_id"] != expected_child_run_id
        ):
            raise ValueError(f"cannot link {parent_id} -> {child_id}: child is already running")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        _link(conn, parent_id, child_id)
        # If child was ready but parent is not yet terminal, demote child to todo
        # (archived counts as terminal, matching _parents_satisfied/recompute_ready).
        if _task_status(conn, parent_id) not in ("done", "archived"):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            gated = cur.rowcount == 1
            if gated:
                _append_event(
                    conn,
                    child_id,
                    "dependency_wait",
                    {"reason": "parent_not_done", "demoted": True, "parent": parent_id},
                )
        _append_event(
            conn,
            child_id,
            "linked",
            {"parent": parent_id, "child": child_id},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))
    return gated


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """True iff ``parent_id`` is already a descendant of ``child_id``."""
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


class VerifierLinkProtected(ValueError):
    """Unlinking would leave a live verifier's verdict with nowhere to go."""


def _refuse_stranding_live_verifier(
    conn: sqlite3.Connection, parent_id: str, child_id: str
) -> None:
    """Refuse to delete the only route a live verifier has to its subject.

    ``verifier_subject_ids`` resolves a verdict's destination from the live
    ``task_links`` edge OR a durable ``task_relations`` ``verifies`` row.
    Auto-created verifiers get the durable row (``fbdf0d564d``); a verifier
    made by hand has only the edge. Deleting that edge while the verifier is
    still open is how four real verdicts were stranded on 2026-09-15 and how
    ``t_fd296c4a`` lost its route on 2026-09-20. Refused only in exactly that
    shape: open ``codex_verify`` child, edge present, no ``verifies`` row.
    Archive the verifier first to detach it deliberately.
    """
    child = conn.execute(
        "SELECT executor_lane, status, terminal_disposition FROM tasks WHERE id = ?",
        (child_id,),
    ).fetchone()
    if child is None or child["executor_lane"] not in INDEPENDENT_VERIFIER_LANES:
        return
    if child["status"] in ("done", "archived") or child["terminal_disposition"] is not None:
        return
    if conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
        (parent_id, child_id),
    ).fetchone() is None:
        return
    if conn.execute(
        "SELECT 1 FROM task_relations WHERE from_task_id = ? AND to_task_id = ? "
        "AND relation = ?",
        (child_id, parent_id, RELATION_VERIFIES),
    ).fetchone() is not None:
        return
    raise VerifierLinkProtected(
        f"refusing to unlink {parent_id} -> {child_id}: {child_id} is an open "
        f"codex_verify verifier whose only route to {parent_id} is this link, so "
        f"its verdict would be stranded. Archive {child_id} first to detach it."
    )


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        _refuse_stranding_live_verifier(conn, parent_id, child_id)
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _append_event(conn, child_id, "unlinked", {"parent": parent_id, "child": child_id})
    if removed:
        # Re-gate the child now (as complete_task/unblock_task do) instead of
        # leaving it in todo until the next tick.
        recompute_ready(conn)
    return removed


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "parent_id", "child_id", task_id)


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "child_id", "parent_id", task_id)


_CONTROL_PLANE_SUBJECT_RE = re.compile(
    r"\b("
    r"gauntlet"
    r"|control[ _-]?plane"
    r"|kanban[ _-]?lifecycle"
    r"|verifier[ _-]?(routing|identity|linkage|return)"
    r"|verification[ _-]?lifecycle"
    r"|recovery[ _-]?lane"
    r"|dispatch(er)?[ _-]?(lock|loop|tick|routing)"
    r"|retry[ _-]?breaker"
    r"|attempt[ _-]?(ceiling|budget)"
    r"|observation[ _-]?timer"
    r"|governance[ _-]?enforcement"
    r"|self[ _-]?review"
    r"|review[ _-]?lane"
    r"|objective[ _-]?budget"
    r"|boot[ _-]?(gate|record|context|checkpoint|consumer|surface)"
    r"|shared[ _-]?boot"
    r")\b",
    re.IGNORECASE,
)
CONTROL_PLANE_AUTHORITY_PREFIXES = (
    "operator:",      # explicit human authorisation
    "regression:",    # a verified regression against the contract
    "requirement:",   # a new requirement
    "defect:",        # falsifiable defect evidence
)
_GAUNTLET_MUTATION_RE = re.compile(
    r"\b("
    r"(schema|data|database|db)[ _-]?migrat(e|ion)"
    r"|schema[ _-]?change|backfill|data[ _-]?deletion|purge"
    r"|payment|refund|payroll|tax[ _-]?filing"
    r"|credential|secret|token[ _-]?rotation|key[ _-]?rotation|access[ _-]?control"
    r"|permission[ _-]?change|firewall|port[ _-]?binding"
    r"|legal[ _-]?filing|compliance[ _-]?change"
    r"|cross[ _-]?(company|entity)"
    r"|company[ _-]?isolation|data[ _-]?isolation"
    r"|control[ _-]?plane"
    r"|boot[ _-]?(gate|record|context|checkpoint|consumer|surface)"
    r"|shared[ _-]?boot"
    r")\b",
    re.IGNORECASE,
)
_GAUNTLET_STATE_CHANGE_RE = re.compile(
    r"(?:^|[.;:!?]|\d+[.)]|[-*]"
    r"|\b(?:then|and|or|please|also|must|should|may|will|can|shall|to|you))"
    r"[ \t]*"
    r"("
    r"live[ _-]?execution"
    r"|(?:delet|remov|writ|mov|updat)(?:e|ing)[ \t]+\w"
    r"|(?:edit|install|uninstall|restart|commit|push|send)(?:ing|ting)?[ \t]+\w"
    r"|filing\b|file[ \t]+(?:\w+[ \t]+){0,3}?(?:into|under)\b"
    r"|close[ \t]+(?:out[ \t]+)?(?:the[ \t]+|this[ \t]+|a[ \t]+|every[ \t]+|all[ \t]+)?"
    r"(?:task|tasks|card|cards|row|rows|ticket|tickets|incident|loop|loops)\b"
    r"|mark(?:ing)?[ \t]+(?:it[ \t]+|them[ \t]+|the[ \t]+\w+[ \t]+|as[ \t]+)?"
    r"(?:verified|closed|done|complete|completed|resolved)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_GAUNTLET_INVESTIGATION_RE = re.compile(
    r"\b("
    r"investigat(e|ion)|diagnos(e|is|tic)|debug|explor(e|atory)|research"
    r"|read[ _-]?only|information[ _-]?gathering|triage|root[ _-]?cause"
    r"|analys(is|e)|survey|inspect|reconnaissance|findings|report on"
    r")\b",
    re.IGNORECASE,
)


def gauntlet_default_for_subject(
    title: Optional[str], body: Optional[str] = None
) -> Optional[bool]:
    """Whether this subject warrants independent verification by default.

    ``True`` govern, ``False`` do not, ``None`` undecidable -- fall through to
    the board-wide ``kanban.gauntlet_enforcement`` config.

    Mutation is tested FIRST and wins outright. A card that both investigates
    and deploys is a deployment. That ordering is the whole point: classifying
    on topic would let "investigation" become the label everything wears to
    avoid verification.

    Ordinary state changes (``_GAUNTLET_STATE_CHANGE_RE``) are tested next and
    also win over investigation, for the same reason: a card that deletes,
    files, writes, updates records or sends messages is a state change and is
    Gauntlet-verified, whatever investigation words it also contains.
    """
    blob = f"{title or ''}\n{body or ''}"
    if _GAUNTLET_MUTATION_RE.search(blob):
        return True
    if _GAUNTLET_STATE_CHANGE_RE.search(blob):
        return True
    if _GAUNTLET_INVESTIGATION_RE.search(blob):
        return False
    return None


CANONICAL_VERIFICATION_REQUIREMENT_PHRASES = (
    "Independent verification required before closing.",
    "Independent codex_verify PASS required before this closes.",
    "Gauntlet PASS required before closing.",
    # The executor-side admission, in a run summary or result:
    "Still needs independent verification.",
)
_VERIFICATION_REQ_SUBJECT_RE = re.compile(
    r"\b(?:"
    r"independent(?:ly)?[ _-]+(?:codex[ _-]?verif\w*|verif\w*|review\w*"
    r"|pass|verdict|sign[ _-]?off|check\w*)"
    r"|gauntlet[ _-]+(?:pass|verif\w*|verdict|sign[ _-]?off)"
    r"|codex[ _-]?verify[ _-]+(?:pass|verdict|sign[ _-]?off)"
    r")",
    re.IGNORECASE,
)
_VERIFICATION_REQ_DEMAND_RE = re.compile(
    r"\b(?:required|requires?|mandatory|must|needs?|needed|owed|owes"
    r"|before\s+(?:it|this|the\s+(?:card|task|work))?\s*"
    r"(?:closes|closing|close|closure|is\s+closed|done|completes|completion))\b",
    re.IGNORECASE,
)
_VERIFICATION_REQ_PENDING_RE = re.compile(
    r"\b(?:still|pending|outstanding|awaiting|awaits|not\s+yet|yet\s+to"
    r"|unverified)\b",
    re.IGNORECASE,
)
_VERIFICATION_REQ_NEGATION_RE = re.compile(
    r"\b(?:not|no\s+longer|never)\s+(?:be\s+)?(?:required|needed|mandatory|necessary)\b"
    r"|\b(?:does|do|did)(?:\s+not|n't)\s+(?:require|need)\b"
    r"|\boptional\b",
    re.IGNORECASE,
)
_VERIFICATION_REQ_PRECEDING_NEGATION_RE = re.compile(
    r"\b(?:no|not|never)\s+(?:(?:an?|any|further|additional|separate|more)\s+){0,2}$",
    re.IGNORECASE,
)
_VERIFICATION_REQ_SATISFIED_RE = re.compile(
    r"\b(?:was|were|has\s+been|have\s+been|already|now)\s+(?:been\s+)?"
    r"(?:satisfied|obtained|received|recorded|granted|completed|passed|returned|met)\b",
    re.IGNORECASE,
)


def explicit_verification_requirement(*texts: Optional[str]) -> bool:
    """True when the text states that independent verification is owed.

    Judged per clause, so a requirement in one sentence is not cancelled by an
    unrelated "not required" in another. A clause counts when it names an
    independent verification (see ``_VERIFICATION_REQ_SUBJECT_RE``) and either
    demands it or reports it outstanding, unless it is explicitly negated or
    reported satisfied. "still"/"pending"/"not yet" beat both exceptions: an
    admission that the work is unverified is never read as its opposite.

    Deliberately biased toward matching. A false positive costs one verifier
    run; a false negative is t_eed363cc.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return False
    for clause in re.split(r"[.;!?\n]+", blob):
        subject = _VERIFICATION_REQ_SUBJECT_RE.search(clause)
        if subject is None:
            continue
        pending = bool(_VERIFICATION_REQ_PENDING_RE.search(clause))
        if not pending and not _VERIFICATION_REQ_DEMAND_RE.search(clause):
            continue
        if not pending and (
            _VERIFICATION_REQ_NEGATION_RE.search(clause)
            or _VERIFICATION_REQ_PRECEDING_NEGATION_RE.search(
                clause[: subject.start()]
            )
            or _VERIFICATION_REQ_SATISFIED_RE.search(clause)
        ):
            continue
        return True
    return False


class ControlPlaneAdmissionError(RuntimeError):
    """Automated creation of control-plane work without a named authority.

    Not a failure of the work -- a refusal to let the control system enlarge
    its own remit unsupervised.
    """


def control_plane_subject(
    title: Optional[str], body: Optional[str] = None
) -> bool:
    """True when this card's SUBJECT is the control plane itself."""
    return bool(_CONTROL_PLANE_SUBJECT_RE.search(f"{title or ''}\n{body or ''}"))


def _creating_actor_control_plane_task(
    conn: sqlite3.Connection, provenance: ActorProvenance
) -> Optional[str]:
    """The creating actor's own card, but only when it is control-plane work.

    This is what makes the second-generation rule enforceable: a control-plane
    repair must not automatically spawn another control-plane repair, which is
    precisely the chain that ran 2026-09-01..06.
    """
    run_id = getattr(provenance, "run_id", None)
    if not run_id:
        return None
    try:
        row = conn.execute(
            "SELECT t.id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.id = ? AND t.control_plane = 1",
            (int(run_id),),
        ).fetchone()
    except (TypeError, ValueError, sqlite3.Error):
        return None
    return str(row["id"]) if row is not None else None


class RecoveryLinkageError(RuntimeError):
    """A governed repair card could not be linked to its required relations.

    Raised from inside :func:`create_repair_task`'s single write transaction,
    which is the entire point: the exception unwinds the transaction that also
    inserted the card, so a repair that cannot be linked is never created at
    all. Callers see either a fully-linked repair or no repair.
    """


def add_task_relation(
    conn: sqlite3.Connection,
    from_task_id: str,
    to_task_id: str,
    relation: str,
    *,
    created_by: Optional[str] = None,
) -> bool:
    """Record a governance relation ``from -> to``. Returns True if new.

    Safe to compose under a caller's transaction (``allow_nested=True``), so a
    creation path can insert the card and its relations under ONE commit. That
    composability is load-bearing, not a convenience: it is what makes the
    orphan window in :func:`create_repair_task` structurally absent rather
    than merely narrow.

    Never touches ``task_links``. Only ``verifies`` is read by readiness, and
    only to release a verifier child that is ALREADY a dependency child of its
    subject (see :data:`RELATION_VERIFIES`); every other relation can never
    change readiness, promotion or dependency gating for either card. That is
    what lets an evidence pointer be added to a live card (``t_c5c2929d``)
    without rewriting or re-scoping it.
    """
    if relation not in VALID_TASK_RELATIONS:
        raise ValueError(
            f"relation must be one of {sorted(VALID_TASK_RELATIONS)}, "
            f"got {relation!r}"
        )
    if from_task_id == to_task_id:
        raise ValueError("a task cannot hold a governance relation to itself")
    with write_txn(conn, allow_nested=True):
        missing = _find_missing_parents(conn, [from_task_id, to_task_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if relation == RELATION_VERIFIES and conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (to_task_id, from_task_id),
        ).fetchone() is None:
            raise ValueError(
                f"{from_task_id} can only verify {to_task_id} through an existing "
                f"dependency link ({to_task_id} -> {from_task_id}); a verifier "
                f"whose verdict has no destination is an orphan"
            )
        cur = conn.execute(
            "INSERT OR IGNORE INTO task_relations "
            "(from_task_id, to_task_id, relation, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (from_task_id, to_task_id, relation, int(time.time()), created_by),
        )
        added = cur.rowcount > 0
        if added:
            payload = {
                "from": from_task_id,
                "to": to_task_id,
                "relation": relation,
            }
            # Both ends get the event. A relation only one side can see is how
            # a governance edge goes unnoticed — the subject and the umbrella
            # must be able to discover the repair from their own event log,
            # not only by being queried for.
            _append_event(conn, from_task_id, "relation_added", payload)
            _append_event(
                conn, to_task_id, "relation_received", dict(payload),
            )
        return added


def task_relations(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    relation: Optional[str] = None,
    direction: str = "from",
) -> list[dict]:
    """Governance relations for ``task_id``.

    ``direction='from'`` returns edges this card asserts; ``'to'`` returns
    edges pointed AT it; ``'both'`` returns the union.
    """
    if direction not in ("from", "to", "both"):
        raise ValueError("direction must be 'from', 'to' or 'both'")
    clauses = []
    params: list[Any] = []
    if direction in ("from", "both"):
        clauses.append("from_task_id = ?")
        params.append(task_id)
    if direction in ("to", "both"):
        clauses.append("to_task_id = ?")
        params.append(task_id)
    sql = (
        "SELECT from_task_id, to_task_id, relation, created_at, created_by "
        "FROM task_relations WHERE (" + " OR ".join(clauses) + ")"
    )
    if relation is not None:
        sql += " AND relation = ?"
        params.append(relation)
    sql += " ORDER BY created_at, relation, to_task_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def relation_targets(
    conn: sqlite3.Connection, task_id: str, relation: str
) -> list[str]:
    """The ``to`` ids this card points at with ``relation``."""
    return [
        r["to_task_id"]
        for r in task_relations(conn, task_id, relation=relation)
    ]


def missing_repair_relations(
    conn: sqlite3.Connection, task_id: str
) -> list[str]:
    """Required repair relations this card does NOT have. Empty == linked.

    Read back from the table rather than trusted from the caller's own
    bookkeeping — an INSERT that silently did nothing (``INSERT OR IGNORE``
    against a row that was concurrently deleted, a partially-applied
    statement) is exactly the failure mode a caller's in-memory success flag
    cannot see.
    """
    present = {
        r["relation"]
        for r in task_relations(conn, task_id)
        if r["relation"] in REQUIRED_REPAIR_RELATIONS
    }
    return [rel for rel in REQUIRED_REPAIR_RELATIONS if rel not in present]


class VerifierLinkageError(RuntimeError):
    """An independent verifier card could not be linked to its subject/owner.

    The verifier analogue of :class:`RecoveryLinkageError`, and raised the same
    way: from inside the write transaction that also inserted the card, so the
    exception unwinds the card along with the linkage. Callers see either a
    verifier whose verdict has somewhere to go, or no verifier at all.
    """


def verifier_subject_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Subjects a verifier card renders its verdict on.

    A thin, NAMED alias for :func:`parent_ids`, and worth its own name because
    the parent edge means something different on a verifier than it does
    anywhere else on the board. Everywhere else a parent is a dependency the
    child waits for; on a ``codex_verify`` card the parent is the SUBJECT under
    review — which is why ``_EVIDENCE_READY_PARENT_SQL`` lets a verifier child
    run against a parent that is not finished. Reading the linkage through this
    function keeps the guard, the watchdog and
    :func:`_return_verifier_verdict_to_subjects` anchored to one definition.
    """
    # The dependency edge is a scheduling mechanism and may be removed before
    # the verifier is dispatched.  The durable ``verifies`` relation is the
    # governance attribution and must remain the source of truth for verdict
    # return.  Keep the live edge as a compatibility fallback for legacy rows
    # created before the relation ledger existed, and deduplicate deterministically.
    subject_ids = set(parent_ids(conn, task_id))
    rows = conn.execute(
        "SELECT to_task_id FROM task_relations "
        "WHERE from_task_id = ? AND relation = ?",
        (task_id, RELATION_VERIFIES),
    ).fetchall()
    subject_ids.update(row["to_task_id"] for row in rows)
    return sorted(subject_ids)


def missing_verifier_linkage(
    conn: sqlite3.Connection, task_id: str
) -> list[str]:
    """Required verifier linkage this card does NOT have. Empty == linked.

    ``"subject"`` — no parent edge, so a verdict has no destination.
    ``"owner"``   — ownership does not resolve, so a returned verdict has
    nobody accountable for acting on it. Both are read back from persisted
    state; neither trusts the caller's arguments.
    """
    missing: list[str] = []
    if not verifier_subject_ids(conn, task_id):
        missing.append("subject")
    ownership = resolve_task_ownership(conn, task_id)
    if ownership is None or not ownership.owner:
        missing.append("owner")
    return missing


def orphaned_verifier_tasks(conn: sqlite3.Connection) -> list[str]:
    """Independent-verifier cards that name no subject.

    Diagnostic, not a guard, and deliberately so: every verifier created before
    this repair that predates the creation-boundary check is expected to appear
    here and is preserved EXACTLY as it is. Rewriting history to make the
    census look clean would destroy the only evidence that the defect was real
    — on the live board this returns seven of fourteen verifier cards, two of
    which closed a production deployment with a verdict nothing consumed.
    """
    rows = conn.execute(
        "SELECT id FROM tasks WHERE executor_lane IN (?, ?) ORDER BY created_at, id",
        tuple(INDEPENDENT_VERIFIER_LANES),
    ).fetchall()
    return [r["id"] for r in rows if not verifier_subject_ids(conn, r["id"])]


def orphaned_repair_tasks(conn: sqlite3.Connection) -> list[str]:
    """Repair/recovery cards that carry no required governance relation.

    Diagnostic, not a guard: cards created BEFORE this repair are expected to
    appear here and are preserved exactly as they are. It exists so the
    orphan population is countable instead of anecdotal.
    """
    rows = conn.execute(
        "SELECT id FROM tasks WHERE creation_cause = ? ORDER BY created_at",
        (CREATION_CAUSE_RECOVERY,),
    ).fetchall()
    return [
        r["id"] for r in rows if missing_repair_relations(conn, r["id"])
    ]


_REPAIR_NEW_APPROACH_PREFIX = (
    "Previous repair recorded no change or an identical change; "
    "take a different approach."
)
AUTO_REPAIR_OWNER = "erika"
AUTO_REPAIR_CREATED_BY = "auto-repair"
AUTO_REPAIR_UMBRELLA_TITLE = "Operational Error Repairs"
AUTO_REPAIR_UMBRELLA_KEY = "auto-repair:umbrella:operational-error-repairs"
AUTO_REPAIR_RECORDED_EVENT = "repair_recorded"


def ensure_operational_repairs_umbrella(conn: sqlite3.Connection) -> str:
    """Return the ONE durable "Operational Error Repairs" umbrella, creating it once.

    Decision B (t_5d7e889e). The lookup and the insert share one write
    transaction (BEGIN IMMEDIATE), so concurrent callers cannot mint two; the
    idempotency key is the durable identity. The card is a container, not
    work: it is closed in the same transaction that creates it, so it is never
    dispatched and never counts as an open task. Must be called outside any
    open write transaction.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "ORDER BY created_at, id LIMIT 1",
            (AUTO_REPAIR_UMBRELLA_KEY,),
        ).fetchone()
        if row is not None:
            return row["id"]
        umbrella_id = create_task(
            conn,
            title=AUTO_REPAIR_UMBRELLA_TITLE,
            body=(
                "Container for the automatic repair cards opened when an "
                "ordinary task error or a repeated failed check happens. It "
                "holds no work itself; each repair card links here."
            ),
            created_by=AUTO_REPAIR_CREATED_BY,
            idempotency_key=AUTO_REPAIR_UMBRELLA_KEY,
            initial_status="blocked",
            gauntlet=False,
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', block_kind = NULL, "
            "completed_at = ?, result = ? WHERE id = ?",
            (
                int(time.time()),
                "Umbrella container for automatic repair cards; no work.",
                umbrella_id,
            ),
        )
        _append_event(
            conn, umbrella_id, "umbrella_created",
            {"title": AUTO_REPAIR_UMBRELLA_TITLE},
        )
    return umbrella_id


def _auto_repair_gate_cmd(subject_id: str) -> str:
    """The repair card's gate: a real recorded repair, checked by the kernel."""
    return f"{sys.executable} -m hermes_cli.main kanban repair-gate {subject_id}"


def _repair_ids_for_subject(conn: sqlite3.Connection, subject_id: str) -> list:
    """Every repair card ever linked to ``subject_id``, oldest first."""
    rows = conn.execute(
        "SELECT r.from_task_id AS id FROM task_relations r "
        "JOIN tasks t ON t.id = r.from_task_id "
        "WHERE r.to_task_id = ? AND r.relation = ? "
        "ORDER BY t.created_at, t.id",
        (subject_id, RELATION_REPAIRS),
    ).fetchall()
    return [r["id"] for r in rows]


def open_repair_for_subject(
    conn: sqlite3.Connection, subject_id: str, *, exclude: Iterable[str] = (),
) -> Optional[str]:
    """The subject's repair card that is still open (not done/archived)."""
    skip = set(exclude)
    for repair_id in reversed(_repair_ids_for_subject(conn, subject_id)):
        if repair_id in skip:
            continue
        st = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (repair_id,),
        ).fetchone()
        if st is not None and st["status"] not in ("done", "archived"):
            return repair_id
    return None


def _latest_event_payload(
    conn: sqlite3.Connection, task_id: str, kind: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _latest_repair_record(conn: sqlite3.Connection, repair_id: str) -> Optional[dict]:
    return _latest_event_payload(conn, repair_id, AUTO_REPAIR_RECORDED_EVENT)


def _norm_change(text: Optional[str]) -> str:
    return " ".join((text or "").lower().split())


def _previous_recorded_change(
    conn: sqlite3.Connection, subject_id: str, repair_id: str,
) -> Optional[tuple]:
    """(repair_id, change) of the newest OTHER repair that recorded one."""
    for prev in reversed(_repair_ids_for_subject(conn, subject_id)):
        if prev == repair_id:
            continue
        rec = _latest_repair_record(conn, prev)
        if rec and (rec.get("change") or "").strip():
            return prev, rec["change"]
    return None


def resolve_repair_card(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """``task_id`` itself when it is a repair card, else its open repair."""
    if relation_targets(conn, task_id, RELATION_REPAIRS):
        return task_id
    return open_repair_for_subject(conn, task_id)


def record_auto_repair(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_cause: str,
    change: str,
    check: str,
    author: Optional[str] = None,
) -> str:
    """Record what a repair actually did. ``task_id`` is the repair or its subject.

    This is what the repair card's gate reads: no recorded root cause, change
    and check means the gate stays red and the card cannot close.
    """
    repair_id = resolve_repair_card(conn, task_id)
    if repair_id is None:
        raise ValueError(
            f"{task_id} is not a repair card and has no open repair card"
        )
    fields = {
        "root_cause": (root_cause or "").strip()[:2000],
        "change": (change or "").strip()[:2000],
        "check": (check or "").strip()[:2000],
    }
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise ValueError(f"empty {', '.join(missing)}")
    with write_txn(conn):
        _append_event(
            conn, repair_id, AUTO_REPAIR_RECORDED_EVENT,
            {**fields, "author": author},
        )
        add_comment(
            conn, repair_id, author or "repair-worker",
            f"Repair recorded.\nRoot cause: {fields['root_cause']}\n"
            f"Change: {fields['change']}\nCheck: {fields['check']}",
        )
    return repair_id


def auto_repair_gate(conn: sqlite3.Connection, subject_id: str) -> tuple:
    """(ok, reason) for the open repair of ``subject_id``.

    Decision D (t_5d7e889e). Green only when the open repair recorded a root
    cause, a change and a check, and the change is not the same as the
    previous repair's. The subject is gated on this card, so its own rerun
    cannot run before the card closes; that rerun plus its independent
    verifier is the acceptance downstream, and a further failure opens the
    next repair card automatically (no cap).
    """
    subj = conn.execute(
        "SELECT status, terminal_disposition FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if subj is None:
        return False, f"subject {subject_id} not found"
    if subj["status"] == "archived":
        # A repair that reconciled its subject as overtaken by events met the
        # objective; a red gate here only bought another paid repair (t_0adeb90a).
        if subj["terminal_disposition"] == DISPOSITION_OVERTAKEN_BY_EVENTS:
            return True, (
                f"subject {subject_id} was reconciled as overtaken by events; "
                "nothing left to repair"
            )
        return False, f"subject {subject_id} is archived; nothing to resume"
    repair_id = open_repair_for_subject(conn, subject_id)
    if repair_id is None:
        return False, f"no open repair card for {subject_id}"
    rec = _latest_repair_record(conn, repair_id)
    if not rec or not all(
        (rec.get(k) or "").strip() for k in ("root_cause", "change", "check")
    ):
        return False, (
            f"{repair_id}: no repair recorded yet. Fix it, then run: hermes "
            f"kanban repair-record {subject_id} --root-cause '...' "
            "--change '...' --check '...'"
        )
    prev = _previous_recorded_change(conn, subject_id, repair_id)
    if prev and _norm_change(prev[1]) == _norm_change(rec["change"]):
        return False, (
            f"{repair_id}: the recorded change is the same as previous repair "
            f"{prev[0]}; take a different approach"
        )
    return True, (
        f"{repair_id}: root cause, change and check recorded. {subject_id} "
        "now reruns and must pass its own check and independent "
        "verification; a further failure opens the next repair."
    )


def _last_repair_brought_new_change(
    conn: sqlite3.Connection, subject_id: str,
) -> bool:
    """True when the subject's newest repair recorded a change that differs
    from the one before it. No repair yet also counts as True (first attempt).
    """
    ids = _repair_ids_for_subject(conn, subject_id)
    if not ids:
        return True
    # created_at ties within one second, so take the newest from the event log.
    started = _latest_event_payload(conn, subject_id, "repair_started") or {}
    last = started.get("repair_id") if started.get("repair_id") in ids else ids[-1]
    # A loop-repair diagnosis recorded after the last repair started is new evidence.
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'block_loop_repaired' "
        "AND id > COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ? "
        "AND kind = 'repair_started'), 0) LIMIT 1",
        (subject_id, subject_id),
    ).fetchone():
        return True
    released = _latest_event_payload(
        conn, subject_id, "verifier_released_after_repair",
    )
    if released and released.get("repair_id") == last:
        # _release_verifier_after_repair already judged its change new.
        return True
    rec = _latest_repair_record(conn, last)
    change = ((rec or {}).get("change") or "").strip()
    if not change:
        return False
    prev = _previous_recorded_change(conn, subject_id, last)
    return not (prev and _norm_change(prev[1]) == _norm_change(change))


#: Christopher, 2026-11-04 (Erika desktop session): a hard maximum of FIVE
#: investigation/repair attempts per incident (subject card), counted across
#: replacement/descendant cards. The count is the subject's own repair history
#: (``_repair_ids_for_subject``), so a renamed replacement card or a verifier
#: rejection cannot reset it. At the cap the loop stops opening another paid
#: repair and records an honest "historical cause unresolved; deferred pending
#: recurrence" disposition, then waits for a fresh occurrence. It never
#: fabricates a root cause, marks an unproven repair VERIFIED, or disables
#: monitoring.
#:
#: Enforced only while the board's attempt-budget mode is ``enforce``
#: (``objective_attempt_budget_enforced``); while the board is in its current
#: ``advisory`` mode the cap is recorded but does not refuse, per the
#: operations.md gate-activation policy (unproved non-security workflow
#: controls stay advisory until proved). This control does not set READY to NO,
#: lock tools, or block the recovery path: a genuinely fresh occurrence is a
#: new subject card and starts its own bounded attempt count.
REPAIR_ATTEMPT_CAP_DEFAULT = 5


def repair_attempt_cap() -> int:
    """Board-scoped maximum repair attempts per incident (subject card)."""
    try:
        raw = _board_kanban_cfg().get(
            "repair_attempt_cap", REPAIR_ATTEMPT_CAP_DEFAULT
        )
        value = int(raw)
        return value if value >= 1 else REPAIR_ATTEMPT_CAP_DEFAULT
    except Exception:
        return REPAIR_ATTEMPT_CAP_DEFAULT


def _defer_repair_pending_recurrence(
    conn: sqlite3.Connection, subject_id: str, *,
    fingerprint: str, attempts: int, cap: int, cause: str,
) -> None:
    """Record the five-attempt cap disposition and stop the paid repair loop.

    Christopher, 2026-11-04: at the cap we stop digging, log
    "not reproduced / historical cause unresolved; deferred pending
    recurrence", and wait for the next occurrence to add evidence. The subject
    is left exactly as it is (not closed, not fabricated, not marked VERIFIED);
    monitoring stays active. Only the specific action this control guards is
    refused -- opening one more paid repair for this incident. Recorded once
    per subject, so a dispatcher that keeps re-evaluating does not spam it.
    """
    already = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'repair_deferred_pending_recurrence' LIMIT 1",
        (subject_id,),
    ).fetchone()
    if already is not None:
        return
    with write_txn(conn):
        _append_event(
            conn, subject_id, "repair_deferred_pending_recurrence",
            {
                "fingerprint": fingerprint,
                "attempts": attempts,
                "cap": cap,
                "cause": (cause or "").strip()[:500],
                "disposition":
                    "not reproduced / historical cause unresolved; "
                    "deferred pending recurrence",
                "rule": "Christopher 2026-11-04 five-attempt repair cap",
                "source": "auto_repair",
            },
        )
        add_comment(
            conn, subject_id, "recovery-lane",
            f"Repair loop stopped at the {cap}-attempt cap for this incident "
            f"({attempts} attempt(s) already spent). Not reproduced / "
            "historical cause unresolved; deferred pending recurrence. No "
            "further paid repair will open for this incident from unchanged "
            "evidence. A fresh occurrence starts a new incident and opens a "
            "new investigation under the normal path. Monitoring stays "
            "active; nothing is marked fixed and nothing is waived.",
        )


def _route_repair_loop_to_diagnosis(
    conn: sqlite3.Connection, subject_id: str, *, cause: str, trigger: str,
) -> bool:
    """Send a subject whose last repair changed nothing to Claude diagnosis.

    Christopher 2026-10-23: a repeat failure with no new change or evidence is
    not handed to another paid repair card. It reuses the block-loop triage
    path; the only exit back to work is ``hermes kanban loop-repair`` with a
    recorded root cause, change and check. This is not the attempt cap: a
    repair that records a new change still gets its next card until the
    separate five-attempt-per-fingerprint cap (Christopher, 2026-11-04) is
    reached.
    """
    reason = (
        "Repair loop: the last repair recorded no new change, so another "
        "repair card would repeat it. Diagnose before any further attempt. "
        f"Latest failure: {(cause or '').strip()[:1500]}"
    )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'triage', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, block_kind = ? "
            "WHERE id = ? AND status NOT IN ('running', 'done', 'archived')",
            ("repair_loop", subject_id),
        )
        if cur.rowcount != 1:
            # Claimed or closed meanwhile: not routed, so leave no loop flag.
            return False
        _append_event(
            conn, subject_id, "block_loop_detected",
            {
                "reason": reason[:2000],
                "kind": "repair_loop",
                "trigger": trigger,
                "source": "auto_repair",
            },
        )
        add_comment(
            conn, subject_id, "recovery-lane",
            reason + chr(10) * 2 + f"After fixing: hermes kanban loop-repair {subject_id} "
            "--root-cause '...' --change '...' --check '...'",
        )
    return True


def _open_auto_repair(
    conn: sqlite3.Connection,
    subject_id: str,
    *,
    cause: str,
    trigger: str,
    evidence: Optional[str] = None,
    prefix: Optional[str] = None,
    exclude: Iterable[str] = (),
) -> Optional[str]:
    """Open the next repair card for ``subject_id`` (shared by every auto-repair path).

    Decisions A-D (t_5d7e889e): ``create_repair_task`` on the
    ``claude_recovery`` lane, owner ``erika``, under the durable "Operational
    Error Repairs" umbrella, gated by ``hermes kanban repair-gate``; the
    subject is parked behind it (``task_links`` repair -> subject) and a
    ``repair_started`` event fires. When the failure's fingerprint matches the
    previous repair's, the body says so and demands a different approach. An
    already-open repair is reused, never duplicated. Must be called outside
    any open write transaction. Returns the repair id, or None when the repair
    could not be linked (``repair_start_failed`` is recorded).
    """
    row = conn.execute(
        "SELECT title, assignee, terminal_disposition FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if row is None:
        return None
    if row["terminal_disposition"] in IRREVERSIBLE_DISPOSITIONS:
        # The subject already has a durable, evidence-backed terminal close
        # (e.g. OVERTAKEN_BY_EVENTS) that a repair cannot and must not
        # reopen -- repair-gate refuses to resume an archived/terminal
        # subject by design (see the gate check further down this module),
        # so a repair card opened here is guaranteed to end in
        # "recovery attempts exhausted; escalated for human review" even
        # though no human decision is actually pending: Christopher's
        # decision already landed as the terminal disposition itself.
        # Root-caused 2026-10-23 (t_ae87c65e) from t_0adeb90a/t_68332405,
        # three nested zombie repair cards against an archived t_1ee44487.
        _append_event(
            conn, subject_id, "repair_skipped_terminal_subject",
            {
                "terminal_disposition": row["terminal_disposition"],
                "trigger": trigger,
            },
        )
        return None
    existing = open_repair_for_subject(conn, subject_id, exclude=exclude)
    if existing is not None:
        return existing
    if not _last_repair_brought_new_change(conn, subject_id):
        _route_repair_loop_to_diagnosis(
            conn, subject_id, cause=cause, trigger=trigger,
        )
        return None
    attempt = len(_repair_ids_for_subject(conn, subject_id)) + 1
    cause_text = (cause or "").strip()[:4000] or "task failed"
    fingerprint = _error_fingerprint(cause_text)
    # Christopher, 2026-11-04: hard five-attempt cap per incident. `attempt`
    # counts every repair card ever linked to the subject, so a renamed
    # replacement or a verifier rejection cannot reset it. Applies in every
    # attempt-budget mode (advisory included): Christopher's explicit rule.
    _cap = repair_attempt_cap()
    if attempt > _cap:
        _defer_repair_pending_recurrence(
            conn, subject_id, fingerprint=fingerprint,
            attempts=attempt - 1, cap=_cap, cause=cause_text,
        )
        return None
    prev_start = _latest_event_payload(conn, subject_id, "repair_started")
    same_error = bool(
        prev_start and prev_start.get("fingerprint") == fingerprint
    )
    parts = []
    if prefix:
        parts.append(prefix)
    if same_error:
        parts.append(
            f"SAME ERROR AGAIN: this failure has the same fingerprint "
            f"({fingerprint}) as the one repair {prev_start.get('repair_id')} "
            "handled, so that repair did not fix it. Change the approach or "
            "the executor; do not repeat the previous change."
        )
    prev_change = _previous_recorded_change(conn, subject_id, "")
    if prev_change:
        parts.append(
            f"Previous repair {prev_change[0]} recorded this change: "
            f"{prev_change[1][:1000]}"
        )
    parts.append(evidence or cause_text)
    parts.append(f"Error fingerprint: {fingerprint}")
    parts.append(
        f"Repair attempt {attempt} for {subject_id} (attempt {attempt} of a "
        f"{_cap}-attempt cap for this error fingerprint). Find "
        "the root cause and fix it. When the fix is in place, record it: "
        f"hermes kanban repair-record {subject_id} --root-cause '<why it "
        "failed>' --change '<what you changed>' --check '<how you checked "
        f"it>'. This card's gate (hermes kanban repair-gate {subject_id}) "
        "passes only once a root cause, change and check are recorded and "
        "the change differs from the previous repair's. When this card "
        f"closes, {subject_id} reruns for real and must pass its own check "
        "and independent verification; if it fails again a new repair card "
        "opens automatically. Never parked for a human: block only for a "
        "decision only Christopher can make."
    )
    body = "\n\n".join(parts)
    umbrella_id = ensure_operational_repairs_umbrella(conn)
    try:
        repair_id = create_repair_task(
            conn,
            title=f"Repair: {(row['title'] or subject_id)[:80]}",
            subject_id=subject_id,
            owner=AUTO_REPAIR_OWNER,
            umbrella_id=umbrella_id,
            body=body,
            assignee=(row["assignee"] or "").strip() or "default",
            created_by=AUTO_REPAIR_CREATED_BY,
            executor_lane=EXECUTOR_LANE_CLAUDE_RECOVERY,
            recovery_gate_cmd=_auto_repair_gate_cmd(subject_id),
        )
    except (RecoveryLinkageError, ControlPlaneAdmissionError) as exc:
        # t_e9b417b0 (2026-10-25): an automated repair's title/body inherits
        # the subject's own title verbatim ("Repair: {subject title}"), and a
        # subject whose title merely happens to contain a control-plane
        # keyword (e.g. an ordinary project called "... gauntlet ...", not
        # Hermes's own Gauntlet governance machinery) trips
        # ControlPlaneAdmissionError here. Left uncaught, that exception
        # propagated out of this automated-repair helper, through
        # record_verification, into _return_verifier_verdict_to_subjects'
        # broad except -- silently losing not just this repair card but the
        # verdict-return path's own audit event and finding comment for the
        # subject, with only a WARNING in the log to show for it. Recording
        # it the same way as a linkage failure keeps the held-comment's
        # promise honest: a repair opens automatically when the admission
        # gate allows it, and the refusal is durable and diagnosable
        # (not silently lost) when it does not. The admission gate itself is
        # NOT weakened or bypassed -- no control_plane_authority is invented
        # here.
        with write_txn(conn):
            _append_event(
                conn, subject_id, "repair_start_failed",
                {
                    "subject_id": subject_id,
                    "error": str(exc)[:500],
                    "trigger": trigger,
                    "control_plane_admission_refused": isinstance(
                        exc, ControlPlaneAdmissionError,
                    ),
                },
            )
        return None
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'todo', block_kind = NULL "
            "WHERE id = ? AND status = 'blocked'",
            (subject_id,),
        )
    # Dependency edge (repair -> subject), separate from the non-gating
    # RELATION_REPAIRS governance relation: this is what parks the subject.
    link_tasks(conn, repair_id, subject_id)
    with write_txn(conn):
        _append_event(
            conn, subject_id, "repair_started",
            {
                "repair_id": repair_id,
                "subject_id": subject_id,
                "attempt": attempt,
                "cause": cause_text[:500],
                "trigger": trigger,
                "fingerprint": fingerprint,
                "same_error_as_previous": same_error,
            },
        )
    return repair_id


def supersede_auto_repair(
    conn: sqlite3.Connection, repair_id: str, *, reason: str,
) -> Optional[str]:
    """Replace an auto-repair card whose gate stayed red with the next one.

    Decision C: no exhausted-to-human state for this failure class. The next
    repair is opened and linked to the subject BEFORE the old one is unlinked
    and archived, so the subject is never released in between. Returns the
    new repair id; the subject id when the subject was routed to diagnosis
    instead (the last repair changed nothing); or None when ``repair_id`` is
    not an auto-repair card or the next one could not be opened (the caller
    keeps its old escalation).
    """
    row = conn.execute(
        "SELECT created_by FROM tasks WHERE id = ?", (repair_id,),
    ).fetchone()
    if row is None or row["created_by"] != AUTO_REPAIR_CREATED_BY:
        return None
    targets = relation_targets(conn, repair_id, RELATION_REPAIRS)
    if not targets:
        return None
    subject_id = targets[0]
    new_id = _open_auto_repair(
        conn, subject_id,
        cause=(
            f"Previous repair {repair_id} could not turn its gate green. "
            f"{(reason or '').strip()[:3000]}"
        ),
        trigger="repair_gate_red",
        prefix=_REPAIR_NEW_APPROACH_PREFIX,
        exclude=(repair_id,),
    )
    if new_id is None:
        if not block_loop_repair_pending(conn, subject_id):
            return None
        # Routed to diagnosis: retire this card so it no longer holds the subject.
        with contextlib.suppress(Exception):
            unlink_tasks(conn, repair_id, subject_id)
        with write_txn(conn):
            _append_event(
                conn, repair_id, "repair_superseded",
                {"next_repair_id": None, "subject_id": subject_id,
                 "routed_to": "diagnosis"},
            )
        archive_task(conn, repair_id)
        return subject_id
    with contextlib.suppress(Exception):
        unlink_tasks(conn, repair_id, subject_id)
    with write_txn(conn):
        _append_event(
            conn, repair_id, "repair_superseded",
            {"next_repair_id": new_id, "subject_id": subject_id},
        )
        add_comment(
            conn, repair_id, "recovery-lane",
            f"Gate still red after the bounded attempts; superseded by the "
            f"next repair {new_id} (within the five-attempt-per-fingerprint "
            f"cap).",
        )
    archive_task(conn, repair_id)
    return new_id


def _start_verifier_fail_repair(
    conn: sqlite3.Connection,
    subject_id: str,
    findings: str,
    *,
    prefix: Optional[str] = None,
) -> Optional[str]:
    """Open a governed repair card for a subject that keeps FAILING verification.

    Kernel part 3 (t_6450901b), via the shared :func:`_open_auto_repair`.
    Never ``needs_input``. Must be called outside any open write transaction.
    """
    finding_text = (findings or "").strip()[:4000] or "independent verification failed"
    # A repair card that fails verification is replaced by the next repair on
    # its root subject, never given a repair of its own (2026-10-23: 26 nested
    # "Repair: Repair: ..." cards, each a fresh attempt 1, so no guard fired).
    _subj = conn.execute(
        "SELECT created_by FROM tasks WHERE id = ?", (subject_id,),
    ).fetchone()
    _roots = (
        relation_targets(conn, subject_id, RELATION_REPAIRS)
        if _subj is not None and _subj["created_by"] == AUTO_REPAIR_CREATED_BY
        else []
    )
    if _roots:
        nxt = supersede_auto_repair(conn, subject_id, reason=finding_text)
        return None if nxt in (None, _roots[0]) else nxt
    evidence = (
        f"Independent verification of {subject_id} failed "
        f"{VERIFIER_REPEATED_FAIL_LIMIT}+ times in a row. Verifier finding:\n\n"
        f"{finding_text}\n\n"
        "Fix what the finding names. A non-empty, new recorded change "
        "releases one new independent verifier on the subject; an empty or "
        "repeated change starts another repair instead."
    )
    return _open_auto_repair(
        conn, subject_id,
        cause=finding_text,
        trigger="verification_failed",
        evidence=evidence,
        prefix=prefix,
    )


def _release_verifier_after_repair(
    conn: sqlite3.Connection,
    subject_id: str,
    *,
    repair_id: str,
    change_text: str,
) -> Optional[str]:
    """After a repair completes, release one verifier only if it changed something.

    Kernel part 3 (t_6450901b). Applies only to a subject whose verification
    head is ``failed``. The repair's completion text is fingerprinted and
    compared with the previous repair's (``repair_change_recorded``). A
    non-empty new change sets the subject back to ``pending`` with a pending
    verdict row -- both required by ``_EVIDENCE_READY_PARENT_SQL`` or the
    verifier could never claim -- and opens one independent verifier. An
    empty or identical change starts the next repair instead. Returns the
    verifier id when one was released.
    """
    row = conn.execute(
        "SELECT assignee, verification_state FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if row is None or row["verification_state"] != VERIFICATION_FAILED:
        return None
    recorded = _latest_repair_record(conn, repair_id)
    text = ((recorded or {}).get("change") or change_text or "").strip()
    if not text:
        try:
            run_row = conn.execute(
                "SELECT summary FROM task_runs WHERE task_id = ? "
                "AND summary IS NOT NULL AND TRIM(summary) != '' "
                "ORDER BY id DESC LIMIT 1",
                (repair_id,),
            ).fetchone()
        except sqlite3.Error:
            run_row = None
        text = (run_row["summary"] or "").strip() if run_row else ""
    fingerprint = (
        hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
    )
    prior = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'repair_change_recorded' ORDER BY id DESC LIMIT 1",
        (subject_id,),
    ).fetchone()
    try:
        prior_fp = json.loads(prior["payload"] or "{}").get("fingerprint") if prior else None
    except (ValueError, AttributeError):
        prior_fp = None
    released = bool(text) and fingerprint != prior_fp
    with write_txn(conn):
        _append_event(
            conn, subject_id, "repair_change_recorded",
            {"repair_id": repair_id, "fingerprint": fingerprint, "empty": not text},
        )
        if released:
            _record_verification_row(
                conn, subject_id,
                state=VERIFICATION_PENDING,
                verifier="repair-release",
                evidence={
                    "repair_id": repair_id,
                    "change_fingerprint": fingerprint,
                    "change": text[:2000],
                },
                reason="repair completed with a recorded change; one new "
                       "independent verification released",
            )
            _append_event(
                conn, subject_id, "verifier_released_after_repair",
                {
                    "subject_id": subject_id,
                    "repair_id": repair_id,
                    "fingerprint": fingerprint,
                },
            )
        else:
            _append_event(
                conn, subject_id, "repair_needs_new_approach",
                {"subject_id": subject_id, "repair_id": repair_id},
            )
    if released:
        verifier_id = _ensure_independent_verifier_child(
            conn, subject_id, implementer=row["assignee"],
        )
        if recompute_ready(conn):
            signal_dispatcher_wake()
        return verifier_id
    last = conn.execute(
        "SELECT reason FROM task_verifications WHERE task_id = ? AND kind = ? "
        "AND state = ? ORDER BY id DESC LIMIT 1",
        (subject_id, LEDGER_KIND_VERDICT, VERIFICATION_FAILED),
    ).fetchone()
    _start_verifier_fail_repair(
        conn, subject_id,
        (last["reason"] if last and last["reason"] else "") or "independent verification failed",
        prefix=_REPAIR_NEW_APPROACH_PREFIX,
    )
    return None


def create_repair_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    subject_id: str,
    owner: str,
    umbrella_id: str,
    body: Optional[str] = None,
    evidence_ids: Iterable[str] = (),
    provenance: Optional[ActorProvenance] = None,
    **create_kwargs: Any,
) -> str:
    """Create a governed repair card, atomically linked or not created at all.

    The defect this closes: a repair card whose subject, owner and umbrella
    exist only as prose in its body. ``t_aef6bbe1`` and ``t_f9b3b48b`` are
    both that shape — each states in words what it repairs, and neither is
    reachable from its subject by any query. Nothing is wrong with either
    card's text; the problem is that a governor cannot ask the board "what is
    repairing this?" and get an answer, so a repair can be lost simply by
    nobody remembering to look for it.

    Prose is insufficient for a second, sharper reason. A relation added in a
    *later* call than the insert has a window — however short — in which the
    card exists and is unlinked. A crash, a lock timeout, a killed executor,
    or an exception in the linking code leaves a permanently orphaned card
    that looks like real work. Both the insert and every required relation
    therefore run inside ONE ``write_txn``, and the linkage is READ BACK from
    the table before that transaction commits. Any failure — a missing
    subject, a blank owner, a relation that did not land — raises
    :class:`RecoveryLinkageError` and unwinds the whole transaction, including
    the card. The orphan window is not narrowed; it does not exist.

    That enforcement lives in :func:`create_task`, not here, and this function
    is a convenience over it rather than the thing that makes it safe. The
    distinction matters: an earlier draft enforced only in this helper, and
    independent review immediately minted an orphan by calling ``create_task``
    directly with ``creation_cause='recovery'``. A rule only one door honours
    is not a rule.

    ``evidence_ids`` add non-governing :data:`RELATION_EVIDENCE` pointers.
    They are best-effort by design and do NOT gate creation: an evidence
    pointer is a citation, and failing to create a repair because a citation
    target is gone would be the fail-closed rule applied where it does not
    belong.
    """
    if provenance is None:
        # A repair card's cause is 'recovery' whoever asked for it. The KIND
        # still comes from the environment — a human-relayed repair and an
        # automation-generated one are genuinely different provenance, and
        # this is the distinction Defect A exists to preserve.
        base = resolve_actor_provenance()
        provenance = ActorProvenance(
            kind=base.kind,
            actor_id=base.actor_id,
            lane=base.lane,
            run_id=base.run_id,
            cause=CREATION_CAUSE_RECOVERY,
        )

    owner = (owner or "").strip()
    evidence = tuple(e for e in evidence_ids if e)

    # ONE transaction. Everything below either commits together or leaves the
    # board exactly as it was.
    #
    # The subject/owner/umbrella requirement and the linkage itself are NOT
    # re-implemented here — ``create_task`` enforces them at the creation
    # boundary, so the guarantee holds for every caller rather than only for
    # users of this helper. This function adds what is specific to a repair
    # card: the non-governing evidence citations and the ``repair_linked``
    # summary event.
    with write_txn(conn):
        task_id = create_task(
            conn,
            title=title,
            body=body,
            provenance=provenance,
            recovery_owner=owner or None,
            repairs_task_id=subject_id,
            umbrella_task_id=umbrella_id,
            **create_kwargs,
        )
        for evidence_id in evidence:
            # Citations, not governance. A dud id is dropped rather than
            # allowed to veto a repair the board actually needs.
            with contextlib.suppress(ValueError):
                add_task_relation(
                    conn, task_id, evidence_id, RELATION_EVIDENCE,
                    created_by=owner,
                )
        # Read back, inside the transaction, before it can commit. This is the
        # difference between "we called the linker" and "the board holds the
        # link" — only the second one is evidence.
        still_missing = missing_repair_relations(conn, task_id)
        if still_missing:
            raise RecoveryLinkageError(
                f"repair task {task_id} is missing required relation(s) "
                f"{still_missing}; rolling back so no orphan card is created"
            )
        _append_event(
            conn,
            task_id,
            "repair_linked",
            {
                "subject": subject_id,
                "umbrella": umbrella_id,
                "owner": owner,
                "evidence": list(evidence),
                "relations": [
                    r["relation"] for r in task_relations(conn, task_id)
                ],
            },
        )
    return task_id


OPERATIONAL_ERROR_REPAIRS_IDEMPOTENCY_KEY = AUTO_REPAIR_UMBRELLA_KEY
OPERATIONAL_ERROR_REPAIRS_TITLE = AUTO_REPAIR_UMBRELLA_TITLE


def ensure_operational_error_repairs_umbrella(
    conn: sqlite3.Connection,
    *,
    owner: str = AUTO_REPAIR_OWNER,
) -> str:
    """Return the id of the one durable "Operational Error Repairs" umbrella.

    Thin alias of :func:`ensure_operational_repairs_umbrella` so the
    standalone-review FAIL route and every kernel auto-repair route share one
    closed container card (never an open, dispatchable look-alike). ``owner``
    is kept for call-site compatibility; repairs carry the owner themselves.
    Must be called outside any open write transaction.
    """
    del owner
    return ensure_operational_repairs_umbrella(conn)


class StandaloneReviewRoutingError(RuntimeError):
    """A standalone review card's FAIL could not be routed to a repair.

    Raised (never silently swallowed) when the review card's parent cannot
    be resolved unambiguously, or when :func:`create_repair_task` itself
    refuses (``RecoveryLinkageError``). Either way the review card's own
    state is left exactly as it was -- no partial mutation -- so a caller
    that catches this can report "the repair loop could NOT start" instead
    of leaving the board in an ambiguous half-routed state.
    """


def route_standalone_review_fail(
    conn: sqlite3.Connection,
    review_task_id: str,
    *,
    reason: str,
    findings: Optional[str] = None,
    owner: str = "erika",
    provenance: Optional[ActorProvenance] = None,
) -> tuple[bool, Optional[str]]:
    """Route a FAIL on a STANDALONE review card to a repair of its parent.

    The gap this closes: :func:`request_changes` is legal only for a run
    claimed FROM the review lane (``claimed`` event payload
    ``source_status='review'`` -- see that function's docstring). A
    standalone review card -- an ordinary task whose job is reviewing its
    PARENT's output, linked via a plain ``task_links`` edge rather than
    having itself been parked in ``status='review'`` -- can never satisfy
    that, so it has no legal route back and the only remaining move used to
    be ``kanban_block(reason=needs_input)``, parking it for a human. Live
    example: ``t_e54d47a7`` did exactly that and a human had to manually fix
    the parent's content and unblock it. Christopher's standing directive
    (operations.md 2a, "fix it, restart it, do not park it") forbids that
    for a machine-fixable defect.

    This is an ADDITIONAL route, not a change to :func:`request_changes` --
    the in-place ``claimed from review`` path and its guard are completely
    untouched by this function.

    Steps, all inside one write transaction so a partial failure leaves the
    review card exactly as it was:

    1. Resolve the review card's single ``task_links`` parent (the subject
       being reviewed). Zero or more-than-one parents is refused rather than
       guessed -- a human should pick the right one, this is not that case
       (see ``StandaloneReviewRoutingError``).
    2. Resolve/create the durable "Operational Error Repairs" umbrella via
       :func:`ensure_operational_error_repairs_umbrella`, in its own
       transaction (``create_task`` manages that itself).
    3. Create a repair card for the PARENT via :func:`create_repair_task`,
       carrying the reviewer's exact findings/required-changes verbatim as
       its body. This also runs in its own transaction -- ``create_repair_task``
       already opens ``write_txn(conn)`` internally without
       ``allow_nested=True``, so it must be called from OUTSIDE any
       transaction this function holds open, not nested inside one (nesting
       would raise ``RuntimeError: write_txn: already inside a
       transaction``). If it raises ``RecoveryLinkageError`` no repair/link
       was created (its own transaction already rolled that back) and this
       function re-raises as :class:`StandaloneReviewRoutingError` before
       touching the review card at all, so the review card's run/claim state
       is provably untouched.
    4. Link the repair -> review-card as a ``task_links`` dependency edge
       (parent_id=repair, child_id=review_task_id): the SAME mechanism
       ``_parents_satisfied``/``recompute_ready`` already use everywhere
       else in this codebase for "don't run until this other task finishes,
       then promote automatically". No second gating mechanism is invented.
       ``link_tasks`` also manages its own transaction -- called here after
       the repair exists, also outside any transaction this function holds.
    5. Reset the review card's run/claim state (mirrors the pattern
       :func:`request_changes` already uses at its own claim-reset: closes
       the active run, clears ``claim_lock``/``claim_expires``/
       ``worker_pid``) and set its status to ``'todo'`` so it is gated on
       the repair rather than immediately re-runnable. ``_end_run`` and
       ``_append_event`` do NOT manage their own transactions (both are
       documented as "called from within an already-open txn"), so THIS is
       the one step that legitimately needs this function's own
       ``write_txn(conn)`` block -- grouping the run-close, status update,
       and event emission as a single atomic commit.
    6. Call :func:`recompute_ready` (outside the transaction, per that
       function's contract) so the review card is correctly NOT ready until
       the repair resolves.
    7. Emit a durable ``standalone_review_fail_routed_to_repair`` event on
       the review card (inside the same transaction as step 5).

    Note on atomicity: steps 2-4 (umbrella, repair, link) and step 5 (review
    card reset) are now THREE separate commits rather than one, because
    ``create_repair_task`` and ``link_tasks`` are reused verbatim (unmodified,
    so the kernel-sibling repair path -- t_5d7e889e -- converges on the exact
    same primitives) and neither accepts ``allow_nested``. The repair card
    existing without yet having gated the review card is a safe intermediate
    state (worst case on a crash between steps 4 and 5: an extra repair card
    points at the subject but the review card is still ``running`` with its
    original claim -- recoverable, not a lost-state hazard), which is the
    tradeoff accepted here rather than changing ``create_repair_task``/
    ``link_tasks`` (both relied on elsewhere) to support nesting.

    Never produces ``block_kind='needs_input'`` or any gave-up-to-human
    state -- this stays entirely inside the no-cap repair loop.

    Returns ``(True, repair_task_id)`` on success, or ``(False, detail)`` on
    a refusal where nothing was mutated (e.g. ambiguous parent count).
    """
    reason_text = str(redact_review_value(reason or "")).strip()
    if not reason_text:
        return False, "reason is required"
    findings_text = str(redact_review_value(findings or "")).strip()

    review_row = conn.execute(
        "SELECT id, title FROM tasks WHERE id = ?", (review_task_id,),
    ).fetchone()
    if review_row is None:
        return False, "review task not found"

    parents = parent_ids(conn, review_task_id)
    if len(parents) != 1:
        return False, (
            f"review card {review_task_id!r} has {len(parents)} task_links "
            f"parent(s); routing a standalone review FAIL requires exactly "
            f"one unambiguous parent (the subject being reviewed)"
        )
    subject_id = parents[0]
    subject_row = conn.execute(
        "SELECT id, title FROM tasks WHERE id = ?", (subject_id,),
    ).fetchone()
    if subject_row is None:
        return False, f"review parent {subject_id!r} not found"

    body = (
        f"Repair requested by independent review {review_task_id} "
        f"(\"{review_row['title']}\") of this task's output.\n\n"
        f"Reviewer finding (verbatim):\n{reason_text}"
        + (f"\n\nRequired changes:\n{findings_text}" if findings_text else "")
    )

    # Steps 2-4 each manage their own transaction (create_task/
    # create_repair_task/link_tasks all open write_txn(conn) internally
    # without allow_nested=True) -- calling them from inside a second
    # write_txn here would raise "already inside a transaction". Run them
    # sequentially, outside any transaction this function holds open; see
    # the "Note on atomicity" above for the accepted intermediate-state
    # tradeoff.
    umbrella_id = ensure_operational_error_repairs_umbrella(conn, owner=owner)
    try:
        repair_id = create_repair_task(
            conn,
            title=f"Repair: {subject_row['title']}",
            subject_id=subject_id,
            owner=owner,
            umbrella_id=umbrella_id,
            body=body,
            evidence_ids=[review_task_id],
            provenance=provenance,
        )
    except RecoveryLinkageError as exc:
        # create_repair_task's own transaction already unwound (no orphan
        # card), so the review card's state (run, claim, status) is provably
        # unchanged -- nothing partial survives.
        raise StandaloneReviewRoutingError(
            f"could not create a repair for {subject_id!r}: {exc}"
        ) from exc

    # Gate the review card on the repair using the SAME dependency
    # mechanism every other "wait for X, then auto-promote" relationship
    # in this codebase already uses.
    link_tasks(conn, repair_id, review_task_id)

    # _end_run/_append_event do not manage their own transactions (both are
    # documented as "called from within an already-open txn"), so this is
    # the one block that legitimately needs this function's own write_txn.
    with write_txn(conn):
        run_id = _end_run(
            conn,
            review_task_id,
            outcome="standalone_review_fail_routed",
            status="todo",
            summary=reason_text,
        )
        conn.execute(
            "UPDATE tasks SET status = 'todo', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (review_task_id,),
        )
        _append_event(
            conn,
            review_task_id,
            "standalone_review_fail_routed_to_repair",
            {
                "repair_id": repair_id,
                "subject_id": subject_id,
                "umbrella_id": umbrella_id,
                "reason": reason_text,
            },
            run_id=run_id,
        )
    recompute_ready(conn)
    return True, repair_id


OWNERSHIP_SOURCE_ORDER = ("recovery_owner", "actor_id", "assignee", "created_by")
STRUCTURED_OWNERSHIP_SOURCES = frozenset({"recovery_owner", "actor_id", "assignee"})
ATTRIBUTION_BLOCKING_REASONS = frozenset({
    "no_owner_recorded",
    "owner_only_from_created_by",
})


@dataclass(frozen=True)
class TaskOwnership:
    """Everything needed to govern a card, resolved from persisted state alone.

    Frozen because an ownership answer is a reading of the board at a moment;
    a mutable one invites a caller to "correct" a degraded resolution in
    memory and then act as though the board said so.
    """

    task_id: str
    #: The accountable identity, or ``None`` when nothing on the row answers.
    owner: Optional[str]
    #: Which column in :data:`OWNERSHIP_SOURCE_ORDER` supplied ``owner``.
    #: ``None`` when unowned. Callers that care about attribution quality test
    #: this rather than the string, for the same reason acceptance tests read
    #: ``actor_kind`` rather than ``created_by``.
    owner_source: Optional[str]
    #: Lane this card RUNS in.
    executor_lane: Optional[str]
    #: Lane of the actor that CREATED it. Differs whenever one lane files work
    #: for another — a codex_verify run filing a claude repair, for instance.
    actor_lane: Optional[str]
    actor_kind: Optional[str]
    actor_id: Optional[str]
    actor_run_id: Optional[str]
    creation_cause: Optional[str]
    #: Provenance only. Present in the record so an auditor can see which
    #: session a card came from; never consulted to resolve ``owner``.
    session_id: Optional[str]
    #: Dependency parents (``task_links``) — the return path a child's result
    #: must travel back up.
    parent_ids: tuple[str, ...]
    #: Governance relations (``task_relations``).
    repairs: tuple[str, ...]
    umbrella: tuple[str, ...]
    evidence: tuple[str, ...]
    #: Reasons ownership is not cleanly resolvable. Empty == clean.
    degraded_reasons: tuple[str, ...] = ()

    @property
    def resolved(self) -> bool:
        """Whether an accountable identity exists at all."""
        return self.owner is not None

    @property
    def structurally_owned(self) -> bool:
        """Whether the owner came from structured provenance, not free text."""
        return self.owner_source in STRUCTURED_OWNERSHIP_SOURCES

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        """Degradations that make the owner untrustworthy rather than thin.

        See :data:`ATTRIBUTION_BLOCKING_REASONS`. Empty means the recorded
        owner may be acted on; non-empty means acting on it would be a guess
        wearing a name.
        """
        return tuple(
            r for r in self.degraded_reasons if r in ATTRIBUTION_BLOCKING_REASONS
        )

    @property
    def attributable(self) -> bool:
        """Whether the owner can be acted on without risking false attribution.

        Weaker than :attr:`governable` (which additionally wants a lane and a
        complete provenance record) and stronger than :attr:`resolved` (which
        only wants some string). This is the predicate the enforced boundary
        uses, because it is the one that separates "we do not know enough
        about this card" from "we might name the wrong actor".
        """
        return self.resolved and not self.blocking_reasons

    @property
    def governable(self) -> bool:
        """Whether a governor can act on this card without guessing.

        Requires an owner AND a lane to route to AND no recorded degradation —
        an owner with nowhere to run is not governable, it just looks it.

        **Advisory, not a gate.** Every card created before the provenance
        columns reads ``False`` here (``provenance_not_recorded``), and on the
        live board as of 2026-09-03 that is 33 of its 34 live cards. Wiring
        this into a routing or dispatch guard would therefore strand almost the
        whole existing board, including the preserved failure evidence.

        The *enforced* boundary is :func:`require_resolvable_ownership`, which
        sits between this property and :attr:`resolved`: it fails closed on an
        ownerless card **and** on an untrustworthy attribution, and passes a
        merely incomplete one. See :attr:`attributable`.
        """
        return (
            self.resolved
            and self.executor_lane is not None
            and not self.degraded_reasons
        )

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "owner": self.owner,
            "owner_source": self.owner_source,
            "executor_lane": self.executor_lane,
            "actor_lane": self.actor_lane,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "actor_run_id": self.actor_run_id,
            "creation_cause": self.creation_cause,
            "session_id": self.session_id,
            "parent_ids": list(self.parent_ids),
            "repairs": list(self.repairs),
            "umbrella": list(self.umbrella),
            "evidence": list(self.evidence),
            "degraded_reasons": list(self.degraded_reasons),
            "resolved": self.resolved,
            "structurally_owned": self.structurally_owned,
            "blocking_reasons": list(self.blocking_reasons),
            "attributable": self.attributable,
            "governable": self.governable,
        }


class OwnershipUnresolvable(RuntimeError):
    """A card could not be given an accountable owner from persisted state."""


def resolve_task_ownership(
    conn: sqlite3.Connection, task_id: str
) -> Optional[TaskOwnership]:
    """Resolve ownership for ``task_id`` from persisted state only.

    Returns ``None`` if the card does not exist. Never raises for a merely
    degraded card: "this is owned badly" and "this card is gone" are different
    answers and collapsing them loses the one a governor can act on.

    **Reads no live state.** Not the process table, not a session registry,
    not the environment. That is not an implementation detail — it is the
    guarantee. The resolution a caller gets while the originating session is
    live and the one it gets an hour after that session was destroyed are the
    same bytes, because the session was never an input.
    """
    row = conn.execute(
        "SELECT id, assignee, created_by, session_id, executor_lane, "
        "actor_kind, actor_id, actor_lane, actor_run_id, creation_cause, "
        "recovery_owner "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    candidates = {
        "recovery_owner": row["recovery_owner"],
        "actor_id": row["actor_id"],
        "assignee": row["assignee"],
        "created_by": row["created_by"],
    }
    owner: Optional[str] = None
    owner_source: Optional[str] = None
    for source in OWNERSHIP_SOURCE_ORDER:
        value = (candidates.get(source) or "").strip()
        if value:
            owner, owner_source = value, source
            break

    degraded: list[str] = []
    if owner is None:
        degraded.append("no_owner_recorded")
    elif owner_source not in STRUCTURED_OWNERSHIP_SOURCES:
        # Resolvable, but only through the string that cannot distinguish an
        # actor kind. Say so rather than reporting a clean owner.
        degraded.append("owner_only_from_created_by")
    elif owner_source == "assignee":
        # Structured — the control plane writes it — but it answers "where does
        # this run?" rather than "who is accountable for it?". The second
        # independent review (2026-09-03) was right that treating a dispatch
        # target as an accountable actor is a category error, and it is a
        # separate one from the ``created_by`` free-text problem.
        #
        # It is reported and NOT blocking, on the same evidence that made
        # ``created_by`` blocking: all 34 live cards resolve through
        # ``assignee``, so refusing it strands the entire board. The honest
        # position is therefore to stop calling it clean, not to pretend the
        # board can do without it. See ATTRIBUTION_BLOCKING_REASONS.
        degraded.append("owner_only_from_assignee")
    if row["actor_kind"] is None:
        # A pre-migration card. Nothing is backfilled — inventing a kind is
        # the fabrication the provenance columns exist to prevent — so the
        # degradation is reported and the card stays exactly as it is.
        degraded.append("provenance_not_recorded")
    if not (row["executor_lane"] or "").strip():
        degraded.append("no_executor_lane")

    cause = row["creation_cause"]
    rel = {
        r: tuple(relation_targets(conn, task_id, r))
        for r in (RELATION_REPAIRS, RELATION_UMBRELLA, RELATION_EVIDENCE)
    }
    if cause == CREATION_CAUSE_RECOVERY:
        # A repair card whose subject or umbrella is missing cannot be
        # returned to anything. This is the orphan condition, restated as an
        # ownership fact so one predicate covers both.
        for relation in REQUIRED_REPAIR_RELATIONS:
            if not rel[relation]:
                degraded.append(f"repair_missing_{relation}")

    return TaskOwnership(
        task_id=row["id"],
        owner=owner,
        owner_source=owner_source,
        executor_lane=(row["executor_lane"] or None),
        actor_lane=(row["actor_lane"] or None),
        actor_kind=(row["actor_kind"] or None),
        actor_id=(row["actor_id"] or None),
        actor_run_id=(row["actor_run_id"] or None),
        creation_cause=(cause or None),
        session_id=(row["session_id"] or None),
        parent_ids=tuple(parent_ids(conn, task_id)),
        repairs=rel[RELATION_REPAIRS],
        umbrella=rel[RELATION_UMBRELLA],
        evidence=rel[RELATION_EVIDENCE],
        degraded_reasons=tuple(degraded),
    )


def require_resolvable_ownership(
    conn: sqlite3.Connection, task_id: str
) -> TaskOwnership:
    """Ownership for ``task_id``, or raise. Fails closed.

    The boundary form, for the paths where proceeding without an accountable
    identity is what creates the orphan in the first place: arming an
    observation timer, routing a recovery, returning a verifier verdict.

    It refuses two things, and passes a third:

    * an **unowned** card — nothing on the row names anyone;
    * an **untrustworthy** one — the only thing naming an owner is the
      free-text ``created_by``, which cannot distinguish a human relay from
      automation running under the same profile, so routing on it risks
      attributing the card to the wrong actor entirely;
    * a merely **incomplete** one still passes, carrying its degradation
      forward in :attr:`TaskOwnership.degraded_reasons`.

    The middle case used to pass, and the 2026-09-03 independent review was
    right that it hollowed the boundary out: a guard that admits an owner it
    has just finished describing as unreliable is not a guard. See
    :data:`ATTRIBUTION_BLOCKING_REASONS` for why the third case is treated
    differently and for the live-board census showing this costs nothing.
    """
    ownership = resolve_task_ownership(conn, task_id)
    if ownership is None:
        raise OwnershipUnresolvable(f"unknown task {task_id!r}")
    if not ownership.resolved:
        raise OwnershipUnresolvable(
            f"task {task_id!r} has no accountable owner in persisted state "
            f"(checked {', '.join(OWNERSHIP_SOURCE_ORDER)}); refusing to "
            f"proceed with an ownerless card"
        )
    if ownership.blocking_reasons:
        raise OwnershipUnresolvable(
            f"task {task_id!r} has no trustworthy accountable owner "
            f"({', '.join(ownership.blocking_reasons)}); refusing to proceed "
            f"on an attribution that may name the wrong actor"
        )
    return ownership


def unowned_tasks(conn: sqlite3.Connection) -> list[str]:
    """Live cards for which :func:`resolve_task_ownership` finds no owner.

    Diagnostic, like :func:`orphaned_repair_tasks`: it makes the ownerless
    population countable rather than anecdotal, and it deliberately does not
    repair anything it finds.
    """
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status NOT IN ('done', 'archived') "
        "ORDER BY created_at"
    ).fetchall()
    out = []
    for r in rows:
        ownership = resolve_task_ownership(conn, r["id"])
        if ownership is not None and not ownership.resolved:
            out.append(r["id"])
    return out


class ObservationIntervalError(ValueError):
    """An observation timer was armed at an interval other than 300 seconds."""


class ObservationTimerNotFound(KeyError):
    """No such observation timer on this board."""


def _new_observation_timer_id() -> str:
    return "obs_" + secrets.token_hex(4)


def _delete_observation_timers_for_task(
    conn: sqlite3.Connection, task_id: str
) -> None:
    """Cascade a task deletion into its timers and their tick ledgers.

    Called from inside the caller's transaction, like every other cascade in
    the delete paths. Without it a deleted card leaves an armed timer behind
    that is due forever and points at nothing — the exact "observation
    outlives the thing observed" shape, arrived at from the other direction.

    **The timers are CLOSED, with a reason, before they are deleted.** They
    used to be dropped outright, which meant a live observation could end with
    no recorded cause anywhere — the disappearance this control exists to
    prevent, reached through the delete path instead of the emit path. The
    close is not decorative: ``trg_obs_timer_no_delete_while_observing``
    refuses the DELETE until it has happened, so the ordering below is
    enforced by the database rather than by this function remembering to.

    No ``observation_timer_closed`` event is appended, and that is deliberate
    rather than an omission: both callers delete the card's whole
    ``task_events`` history in the same transaction, so the event would be
    written into a log that ceases to exist a few statements later. The
    durable record of a purge is the caller's own deletion path, not a
    tombstone on a row that is going away.
    """
    conn.execute(
        "UPDATE observation_timers "
        "SET state = ?, closed_at = ?, closed_reason = 'task_deleted' "
        "WHERE task_id = ? AND state != ?",
        (
            OBSERVATION_STATE_CLOSED, int(time.time()), task_id,
            OBSERVATION_STATE_CLOSED,
        ),
    )
    conn.execute(
        "DELETE FROM observation_ticks WHERE timer_id IN "
        "(SELECT id FROM observation_timers WHERE task_id = ?)",
        (task_id,),
    )
    conn.execute("DELETE FROM observation_timers WHERE task_id = ?", (task_id,))


@dataclass(frozen=True)
class ObservationTimer:
    """One armed (or retired) observation timer, read from the board."""

    id: str
    task_id: str
    kind: str
    interval_seconds: int
    created_at: int
    next_due_at: int
    tick_count: int
    last_tick_at: Optional[int]
    verdict_emitted_at: Optional[int]
    state: str
    closed_at: Optional[int]
    closed_reason: Optional[str]
    owner: Optional[str]

    @property
    def observing(self) -> bool:
        return self.state == OBSERVATION_STATE_OBSERVING

    @property
    def verdict_emitted(self) -> bool:
        return self.verdict_emitted_at is not None

    def is_due(self, now: Optional[int] = None) -> bool:
        """Whether a tick is owed as of ``now``.

        A function of the clock and this row. Deliberately not a function of
        whether any observer process exists — that conflation is failure shape
        3 (a stopped watcher that kept running, and a running watcher believed
        stopped), and it is why process presence is never consulted here.
        """
        return self.observing and (
            (int(time.time()) if now is None else int(now)) >= self.next_due_at
        )

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "interval_seconds": self.interval_seconds,
            "created_at": self.created_at,
            "next_due_at": self.next_due_at,
            "tick_count": self.tick_count,
            "last_tick_at": self.last_tick_at,
            "verdict_emitted_at": self.verdict_emitted_at,
            "state": self.state,
            "closed_at": self.closed_at,
            "closed_reason": self.closed_reason,
            "owner": self.owner,
        }


@dataclass(frozen=True)
class ObservationTick:
    """One emitted recheck."""

    timer_id: str
    task_id: str
    seq: int
    #: The due instant discharged, not the instant of emission.
    due_at: int
    emitted_at: int
    #: Whole intervals whose due time passed with nothing running. Non-zero is
    #: a recorded observer outage; it is never a silently dropped recheck.
    missed: int
    #: True on the contracted one-time verdict emission only.
    verdict: bool
    payload: Optional[dict]

    @property
    def late_seconds(self) -> int:
        return max(0, self.emitted_at - self.due_at)

    def as_dict(self) -> dict:
        return {
            "timer_id": self.timer_id,
            "task_id": self.task_id,
            "seq": self.seq,
            "due_at": self.due_at,
            "emitted_at": self.emitted_at,
            "missed": self.missed,
            "verdict": self.verdict,
            "late_seconds": self.late_seconds,
            "payload": self.payload,
        }


def _observation_timer_from_row(row: sqlite3.Row) -> ObservationTimer:
    return ObservationTimer(
        id=row["id"],
        task_id=row["task_id"],
        kind=row["kind"],
        interval_seconds=int(row["interval_seconds"]),
        created_at=int(row["created_at"]),
        next_due_at=int(row["next_due_at"]),
        tick_count=int(row["tick_count"]),
        last_tick_at=row["last_tick_at"],
        verdict_emitted_at=row["verdict_emitted_at"],
        state=row["state"],
        closed_at=row["closed_at"],
        closed_reason=row["closed_reason"],
        owner=row["owner"],
    )


def arm_observation_timer(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    kind: str = "recheck",
    interval_seconds: int = MANDATORY_OBSERVATION_INTERVAL_SECONDS,
    owner: Optional[str] = None,
    now: Optional[int] = None,
) -> ObservationTimer:
    """Arm a durable observation timer on ``task_id``.

    ``interval_seconds`` must equal
    :data:`MANDATORY_OBSERVATION_INTERVAL_SECONDS`. Any other value raises
    :class:`ObservationIntervalError` — the parameter exists so the refusal is
    explicit and testable, not so the interval can be tuned. "A number reasoned
    out from a dispatcher tick" is precisely what the ruling forbade, and a
    silently-accepted 900 would be indistinguishable at read time from a
    correctly armed timer.

    ``owner`` defaults to the card's resolved accountable identity. Arming
    fails closed on an ownerless card: a timer nobody owns is an alarm nobody
    answers, which is worse than no timer because it looks like coverage.

    The timer is armed for ``now + interval``, so the first tick is owed one
    full interval after arming rather than immediately.
    """
    # Normalised before it becomes half of a uniqueness key. Free-text 'kind'
    # let 'recheck', 'Recheck' and ' recheck ' name three different observations
    # of the same thing and slip past the (task_id, kind) index — found by the
    # third independent review. Normalising at the one place timers are created
    # is the fix; the column stays free-text because 'kind' is a label, not an
    # enum, and constraining its vocabulary is a different decision.
    kind = (kind or "").strip().lower() or "recheck"
    interval = int(interval_seconds)
    if interval != MANDATORY_OBSERVATION_INTERVAL_SECONDS:
        raise ObservationIntervalError(
            f"observation interval is fixed at "
            f"{MANDATORY_OBSERVATION_INTERVAL_SECONDS}s by the 2026-09-03 "
            f"ruling; refusing {interval}s"
        )
    now = int(time.time()) if now is None else int(now)
    # Fails closed on an unknown or ownerless card, before any row is written.
    ownership = require_resolvable_ownership(conn, task_id)
    owner = (owner or "").strip() or ownership.owner
    timer_id = _new_observation_timer_id()
    with write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO observation_timers "
            "(id, task_id, kind, interval_seconds, created_at, next_due_at, "
            " tick_count, state, owner) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                timer_id, task_id, kind, interval, now, now + interval,
                OBSERVATION_STATE_OBSERVING, owner,
            ),
        )
        _append_event(
            conn,
            task_id,
            "observation_timer_armed",
            {
                "timer_id": timer_id,
                "kind": kind,
                "interval_seconds": interval,
                "next_due_at": now + interval,
                "owner": owner,
            },
        )
    result = get_observation_timer(conn, timer_id)
    assert result is not None  # just inserted, inside a committed txn
    return result


def get_observation_timer(
    conn: sqlite3.Connection, timer_id: str
) -> Optional[ObservationTimer]:
    row = conn.execute(
        "SELECT * FROM observation_timers WHERE id = ?", (timer_id,)
    ).fetchone()
    return None if row is None else _observation_timer_from_row(row)


def task_observation_timers(
    conn: sqlite3.Connection, task_id: str, *, state: Optional[str] = None
) -> list[ObservationTimer]:
    sql = "SELECT * FROM observation_timers WHERE task_id = ?"
    params: list[Any] = [task_id]
    if state is not None:
        sql += " AND state = ?"
        params.append(state)
    sql += " ORDER BY created_at, id"
    return [
        _observation_timer_from_row(r) for r in conn.execute(sql, params).fetchall()
    ]


def validation_loop_entry_valid(conn: sqlite3.Connection, task_id: str) -> tuple[bool, str]:
    """Check the durable admission state for a governed repair.

    Administrative paths may legally change status, assignee, or dependency
    edges, but none of those writes is a substitute for the validation loop.
    The claim boundary calls this helper, so direct dispatcher/recovery paths
    receive the same fail-closed decision as CLI/dashboard transitions.
    """
    repair = conn.execute(
        "SELECT 1 FROM task_relations WHERE from_task_id = ? AND relation = ? LIMIT 1",
        (task_id, RELATION_REPAIRS),
    ).fetchone()
    if repair is None:
        return True, "not a governed repair"
    row = conn.execute(
        "SELECT gauntlet_enforced, terminal_disposition FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False, "task not found"
    if not bool(row["gauntlet_enforced"] or gauntlet_enforcement_default()):
        return False, "governed repair is not Gauntlet-enforced"
    if row["terminal_disposition"] in IRREVERSIBLE_DISPOSITIONS:
        return False, "repair has an irreversible terminal disposition"
    timer = conn.execute(
        "SELECT 1 FROM observation_timers WHERE task_id = ? AND kind = ? "
        "AND state = ? LIMIT 1",
        (task_id, VALIDATION_LOOP_TIMER_KIND, OBSERVATION_STATE_OBSERVING),
    ).fetchone()
    if timer is None:
        return False, "validation-loop recheck timer is absent or closed"
    return True, "validation-loop state valid"


def due_observation_timers(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[ObservationTimer]:
    """Every armed timer whose next tick is owed as of ``now``.

    The scan is over persisted rows and the clock. A timer armed by a process
    that has since died is returned here exactly like one whose armer is still
    running, because the row is the timer — that equivalence is the point.
    """
    now = int(time.time()) if now is None else int(now)
    rows = conn.execute(
        "SELECT * FROM observation_timers "
        "WHERE state = ? AND next_due_at <= ? "
        "ORDER BY next_due_at, id",
        (OBSERVATION_STATE_OBSERVING, now),
    ).fetchall()
    return [_observation_timer_from_row(r) for r in rows]


def emit_observation_tick(
    conn: sqlite3.Connection,
    timer_id: str,
    *,
    payload: Optional[dict] = None,
    now: Optional[int] = None,
) -> Optional[ObservationTick]:
    """Emit the owed recheck for ``timer_id`` and RE-ARM in the same statement.

    Returns the tick, or ``None`` when nothing was owed — the timer is not due,
    is closed, or another emitter already discharged this due instant.

    Three properties, each carried by a database constraint rather than by a
    caller's discipline:

    * **Continues observing.** ``next_due_at`` is advanced to the next future
      boundary and ``state`` is left untouched, inside the transaction that
      writes the tick. There is no code path here that closes a timer, so an
      emitter cannot end observation by emitting — failure shape 1.
    * **Verdict exactly once.** The CAS below matches on ``tick_count``, so
      only one emitter can ever produce ``seq == 1``; ``verdict_emitted_at`` is
      additionally written only where it ``IS NULL``.
    * **Duplicates suppressed.** ``observation_ticks`` is keyed
      ``(timer_id, seq)``, and the guarded UPDATE means two racing emitters
      compute different ``seq`` values only if one of them already committed.
      The loser's ``rowcount`` is 0 and it emits nothing.

    Phase is preserved across an observer outage: the next due time is derived
    from the due instant being discharged, never from ``now``. So an observer
    that was down for 40 minutes emits ONE tick recording ``missed=3`` and
    stays on its original 300 s boundaries, rather than emitting a burst of
    four or silently re-phasing to a new grid.
    """
    now = int(time.time()) if now is None else int(now)
    with write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT * FROM observation_timers WHERE id = ?", (timer_id,)
        ).fetchone()
        if row is None:
            raise ObservationTimerNotFound(timer_id)
        if row["state"] != OBSERVATION_STATE_OBSERVING:
            return None
        due_at = int(row["next_due_at"])
        if now < due_at:
            return None
        interval = int(row["interval_seconds"])
        observed_ticks = int(row["tick_count"])
        # Full intervals that elapsed BEYOND the one being discharged.
        missed = (now - due_at) // interval
        next_due = due_at + (missed + 1) * interval
        seq = observed_ticks + 1
        # The verdict is owed while none has been recorded. Reading the column
        # rather than testing ``seq == 1`` keeps the guarantee anchored to
        # persisted state.
        verdict = row["verdict_emitted_at"] is None
        cur = conn.execute(
            "UPDATE observation_timers "
            "SET next_due_at = ?, tick_count = ?, last_tick_at = ?, "
            "    verdict_emitted_at = CASE "
            "        WHEN verdict_emitted_at IS NULL AND ? = 1 THEN ? "
            "        ELSE verdict_emitted_at END "
            "WHERE id = ? AND state = ? AND next_due_at = ? AND tick_count = ?",
            (
                next_due, seq, now,
                1 if verdict else 0, now,
                timer_id, OBSERVATION_STATE_OBSERVING, due_at, observed_ticks,
            ),
        )
        if cur.rowcount == 0:
            # Another emitter discharged this due instant between the SELECT
            # and the UPDATE. It emitted; this call must not.
            return None
        conn.execute(
            "INSERT INTO observation_ticks "
            "(timer_id, seq, due_at, emitted_at, missed, verdict, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                timer_id, seq, due_at, now, int(missed), 1 if verdict else 0,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )
        _append_event(
            conn,
            row["task_id"],
            "observation_tick",
            {
                "timer_id": timer_id,
                "seq": seq,
                "due_at": due_at,
                "emitted_at": now,
                "missed": int(missed),
                "verdict": verdict,
                "next_due_at": next_due,
                # Named for what it is. The ruling's own wording: a 600s tick
                # re-gathers evidence and restates the open question. Labelling
                # it anything that declares — 'stalled', 'DEFECT-ADVISORY' — is
                # the same mistake wearing a different word.
                "emission": "recheck",
            },
        )
    return ObservationTick(
        timer_id=timer_id,
        task_id=row["task_id"],
        seq=seq,
        due_at=due_at,
        emitted_at=now,
        missed=int(missed),
        verdict=verdict,
        payload=payload,
    )


def run_observation_cycle(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[ObservationTick]:
    """Discharge every owed recheck once. Closes nothing.

    The driver an observer process calls on each pass. It is deliberately
    idempotent per due instant and deliberately incapable of retiring a timer:
    an observer that runs this and then exits leaves every timer armed for its
    next interval, which is what makes the observation survive the observer.
    """
    now = int(time.time()) if now is None else int(now)
    ticks: list[ObservationTick] = []
    for timer in due_observation_timers(conn, now=now):
        tick = emit_observation_tick(conn, timer.id, now=now)
        if tick is not None:
            ticks.append(tick)
    return ticks


def close_observation_timer(
    conn: sqlite3.Connection,
    timer_id: str,
    *,
    reason: str,
    now: Optional[int] = None,
) -> bool:
    """Retire a timer. Returns False if it was already closed.

    ``reason`` is mandatory and must be non-empty. A timer that can be retired
    without saying why is a timer that can vanish quietly, which is failure
    shape 2 — the observation stopped and its absence read as "nothing to
    report". The reason is written to the row and to the card's append-only
    event log, so the end of an observation is as discoverable as its start.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError(
            "closing an observation timer requires an explicit reason; a "
            "timer must never be able to disappear silently"
        )
    now = int(time.time()) if now is None else int(now)
    with write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT task_id, state FROM observation_timers WHERE id = ?",
            (timer_id,),
        ).fetchone()
        if row is None:
            raise ObservationTimerNotFound(timer_id)
        cur = conn.execute(
            "UPDATE observation_timers SET state = ?, closed_at = ?, "
            "closed_reason = ? WHERE id = ? AND state = ?",
            (
                OBSERVATION_STATE_CLOSED, now, reason, timer_id,
                OBSERVATION_STATE_OBSERVING,
            ),
        )
        if cur.rowcount == 0:
            return False
        _append_event(
            conn,
            row["task_id"],
            "observation_timer_closed",
            {"timer_id": timer_id, "reason": reason, "closed_at": now},
        )
    return True


def observation_ticks(
    conn: sqlite3.Connection, timer_id: str
) -> list[ObservationTick]:
    """Every tick this timer has emitted, oldest first."""
    timer_row = conn.execute(
        "SELECT task_id FROM observation_timers WHERE id = ?", (timer_id,)
    ).fetchone()
    if timer_row is None:
        raise ObservationTimerNotFound(timer_id)
    rows = conn.execute(
        "SELECT * FROM observation_ticks WHERE timer_id = ? ORDER BY seq",
        (timer_id,),
    ).fetchall()
    return [
        ObservationTick(
            timer_id=timer_id,
            task_id=timer_row["task_id"],
            seq=int(r["seq"]),
            due_at=int(r["due_at"]),
            emitted_at=int(r["emitted_at"]),
            missed=int(r["missed"]),
            verdict=bool(r["verdict"]),
            payload=json.loads(r["payload"]) if r["payload"] else None,
        )
        for r in rows
    ]


def task_graph_contexts(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict]:
    """Bulk-load compact direct graph state for graph-aware diagnostics."""
    ordered_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    contexts = {task_id: {"parents": [], "children": []} for task_id in ordered_ids}
    if not ordered_ids:
        return contexts

    placeholders = ",".join("?" for _ in ordered_ids)
    for bucket, own, other in (("parents", "child_id", "parent_id"), ("children", "parent_id", "child_id")):
        for row in conn.execute(
            f"SELECT l.{own} AS owner_id, t.id, t.title, t.status "
            f"FROM task_links l JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} IN ({placeholders}) ORDER BY l.{own}, t.id", tuple(ordered_ids),
        ).fetchall():
            contexts[row["owner_id"]][bucket].append(
                {"id": row["id"], "title": row["title"], "status": row["status"]}
            )
    return contexts


def task_graph_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """Return compact direct parent/child state for one task."""
    return task_graph_contexts(conn, [task_id])[task_id]


def add_comment(conn: sqlite3.Connection, task_id: str, author: str, body: str) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    # ``allow_nested=True``: graph builders (kanban_swarm blackboard seeding)
    # compose comment writes under one outer commit.
    with write_txn(conn, allow_nested=True):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)", (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    return [Comment.from_row(r) for r in _task_rows(conn, "task_comments", task_id, "created_at ASC")]


def list_comments_after(
    conn: sqlite3.Connection, task_id: str, *, after_id: int = 0
) -> list[Comment]:
    """Comments with ``id > after_id`` — keyed on rowid, not ``created_at``, so a
    same-second burst is never skipped (live worker comment bridge)."""
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC", (task_id, int(after_id)),
    ).fetchall()
    return [Comment.from_row(r) for r in rows]


class AttachmentTooLarge(ValueError):
    """Attachment over the size cap. A ``ValueError`` so generic 400 handlers
    still catch it while the tool/CLI can give a 413-style message."""


def _safe_attachment_name(raw: str) -> str:
    """Client filename -> safe basename: strip directories (both separators),
    control chars and leading dots (no dotfiles, no traversal); ValueError when
    nothing usable remains. Only ever joined under the per-task attachments dir."""
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\x00").strip()
    name = name.lstrip(".").strip()
    if not name:
        raise ValueError("invalid attachment filename")
    return name[:200]


def _collision_free_path(dest_dir: Path, safe_name: str) -> Path:
    """``foo.pdf`` -> ``foo.pdf``, ``foo (1).pdf``, ... first one that doesn't exist."""
    stem, dot, ext = safe_name.partition(".")
    candidate = safe_name
    n = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem} ({n}){dot}{ext}"
        n += 1
    return dest_dir / candidate


def store_attachment_bytes(
    conn: sqlite3.Connection, task_id: str, filename: str, data: bytes, *,
    content_type: Optional[str] = None, uploaded_by: Optional[str] = None,
    board: Optional[str] = None, max_bytes: Optional[int] = None,
) -> int:
    """Single attachment write path (dashboard, tools, CLI): size cap, safe
    basename, collision-free blob under :func:`task_attachments_dir`, then the
    metadata row. Raises :class:`AttachmentTooLarge` / ``ValueError``; a blob
    whose row insert fails is removed before re-raising. Returns the new id."""
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise


def add_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: Optional[str] = None, size: int = 0, uploaded_by: Optional[str] = None,
) -> int:
    """Record the metadata row (+ ``attached`` event) for a blob the caller already wrote."""
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    # ``request_review`` composes this canonical attachment write into the
    # same transaction when it materialises a structured run handoff.
    with write_txn(conn, allow_nested=True):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename.strip(), stored_path, content_type, int(size), uploaded_by, now),
        )
        _append_event(
            conn, task_id, "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    return [Attachment.from_row(r) for r in _task_rows(conn, "task_attachments", task_id, "created_at ASC, id ASC")]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return None if r is None else Attachment.from_row(r)


class AttachmentDeleteRefused(PermissionError):
    """Raised when an attachment's owning card is not archived.

    Attachments are evidence (execution contract rule 8, same footing as
    comments, events and runs): a card must be archived — a visible and
    reversible step — before anything on it can be purged. Carries the
    attachment row and the card's current status so callers can print an
    actionable "archive the card first" message.
    """

    def __init__(self, attachment: Attachment, status: Optional[str]):
        self.attachment = attachment
        self.status = status
        super().__init__(
            f"attachment {attachment.id} ({attachment.filename}) belongs to "
            f"{attachment.task_id} which is {status!r}; archive the card "
            "before deleting its attachments"
        )


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete an attachment row and its on-disk blob. Returns the removed row.

    Returns ``None`` when no row matched. The blob is removed best-effort
    (a missing file is not an error, and a blob still referenced by another
    row is kept); the metadata row is the source of truth for whether an
    attachment "exists".

    Only an attachment on an ARCHIVED card can be deleted. Until this gate
    ``hermes kanban attach-rm`` (and the dashboard DELETE) erased the row
    and the blob on a live card in one call, while rule 8 already protected
    the card's comments, events and runs. The status check is in the
    DELETE's WHERE clause, not a prior SELECT, so a card un-archived between
    the two cannot lose an attachment to a stale read. A refused delete
    raises :class:`AttachmentDeleteRefused` after appending an
    ``attachment_delete_refused`` event so the attempt itself is on the
    record; the blob is never touched on refusal.
    """
    refused: Optional[AttachmentDeleteRefused] = None
    has_remaining_blob_reference = False
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        cur = conn.execute(
            "DELETE FROM task_attachments WHERE id = ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status = 'archived')",
            (attachment_id,),
        )
        if cur.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (att.task_id,)
            ).fetchone()
            status = row["status"] if row is not None else None
            _append_event(
                conn, att.task_id, "attachment_delete_refused",
                {
                    "reason": "task_not_archived",
                    "status": status,
                    "attachment_id": att.id,
                    "filename": att.filename,
                    "source": "delete_attachment",
                },
            )
            # Raise AFTER the txn commits — raising inside ``write_txn``
            # would roll the refusal record back along with everything else.
            refused = AttachmentDeleteRefused(att, status)
        else:
            has_remaining_blob_reference = conn.execute(
                "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
                (att.stored_path,),
            ).fetchone() is not None
            _append_event(
                conn, att.task_id, "attachment_removed", {"filename": att.filename}
            )
    if refused is not None:
        raise refused
    if not has_remaining_blob_reference:
        with contextlib.suppress(OSError):
            p = Path(att.stored_path)
            if p.is_file():
                p.unlink()
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    return [Event.from_row(r) for r in _task_rows(conn, "task_events", task_id, "created_at ASC, id ASC")]


def _insert_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, created_at: int,
) -> None:
    """Raw comment INSERT for callers already inside a write txn (``add_comment``
    opens its own txn and emits ``commented``)."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)", (task_id, author, body, created_at),
    )


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> None:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)", (task_id, run_id, kind, _json_or_null(payload), int(time.time())),
    )


def _end_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None, status: Optional[str] = None,
    effect_policy: Optional[str] = None,
) -> Optional[int]:
    """Close the active run (``status`` defaults to ``outcome``) and clear
    ``current_run_id``; None when no run was active (never-claimed task).

    ``worker_pid`` / ``worker_started_at`` / ``claim_lock`` stay on the closed
    row: they are the only evidence left of the OS process once the task row
    is wiped, and :func:`kanban_db_dispatch.reap_terminal_workers` needs them
    to end a worker that survived its own terminal transition.

    ``effect_policy`` (fork) is normally inferred from the outcome:
    success/review handoff commits execution effects, provider waits hold
    them, and ownership-loss outcomes request rollback. Dependency waits pass
    ``effect_policy='hold'`` explicitly because their run outcome remains the
    legacy value ``blocked``."""
    now = int(time.time())
    run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return None
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (status or outcome, outcome, summary, error, _json_or_null(metadata), now, run_id),
    )
    # Route durable external side effects through the SAME lifecycle decision
    # that just closed the run. This only changes ledger state here; actual
    # rollback I/O is reconciled after the write transaction commits.
    if effect_policy is None:
        if outcome in {"completed", "review_requested"}:
            effect_policy = "commit"
        elif outcome == "rate_limited":
            effect_policy = "hold"
        else:
            effect_policy = "rollback"
    try:
        from hermes_cli.execution_effects import route_run_effects
        route_run_effects(
            conn, task_id=task_id, run_id=run_id, policy=effect_policy,
            reason=outcome, commit=False,
        )
    except sqlite3.OperationalError as exc:
        # A pre-migration/third-party DB without the ledger must not make the
        # legacy lifecycle unusable. connect() creates the table on normal boards.
        if "execution_effects" not in str(exc):
            raise
    conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,))
    return run_id


def _first_line(text: Optional[str], limit: int) -> str:
    """First non-blank-stripped line of ``text`` capped at ``limit`` chars; "" when empty."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _opt_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` when ``value`` is ``None`` (NULL column passthrough)."""
    return int(value) if value is not None else None


def _json_or_null(obj: Any) -> Optional[str]:
    """JSON text for a payload/metadata column; falsy -> NULL."""
    return json.dumps(obj, ensure_ascii=False) if obj else None


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Current ``tasks.status`` for ``task_id``, or ``None`` when no such row."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


_UNSET: Any = object()


def _end_or_synthesize_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, status: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None, synthesize: bool,
    profile: Any = _UNSET,
) -> Optional[int]:
    """:func:`_end_run`; when no run was active and ``synthesize`` holds, record a
    zero-duration run instead so the handoff fields survive in attempt history.
    ``profile`` overrides the profile read off the task row for the synthesized
    run — transitions that reassign the task (e.g. review handoff) pass the
    acting profile captured before the rewrite."""
    run_id = _end_run(conn, task_id, outcome=outcome, status=status, summary=summary, metadata=metadata)
    if run_id is None and synthesize:
        run_id = _synthesize_ended_run(conn, task_id, outcome=outcome, summary=summary, metadata=metadata, profile=profile)
    return run_id


def _synthesize_ended_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None,
    profile: Any = _UNSET,
) -> int:
    """Zero-duration closed run for a terminal transition on a never-claimed
    task, so the handoff fields aren't silently dropped (``_end_run`` is a
    no-op then). ``started_at == ended_at`` keeps elapsed stats honest. Does
    NOT touch the tasks row.

    ``profile`` overrides the profile read off the task row: transitions that
    reassign the task (e.g. review handoff) pass the acting profile captured
    before the rewrite, so the run names the actor, not the new assignee."""
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if profile is _UNSET:
        profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key, outcome, outcome, summary, error, _json_or_null(metadata),
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


def reconcile_execution_effect_rollbacks(conn: sqlite3.Connection) -> tuple[int, int]:
    """Revert ownership-lost side effects and surface conflicts as existing blocks.

    This is an actuator inside the ordinary dispatcher tick, not a watchdog or
    repair-card generator. A conflict means current state no longer matches
    either side of the recorded mutation, so Hermes refuses to overwrite it and
    moves the existing task to the existing ``blocked/needs_input`` lane.
    """
    try:
        from hermes_cli.execution_effects import reconcile_pending_rollbacks
        reverted, conflicts = reconcile_pending_rollbacks(conn, commit=True)
    except sqlite3.OperationalError as exc:
        if "execution_effects" in str(exc):
            return 0, 0
        raise
    if not conflicts:
        return len(reverted), 0

    placeholders = ",".join("?" for _ in conflicts)
    rows = conn.execute(
        f"SELECT id, task_id, resource FROM execution_effects WHERE id IN ({placeholders})",
        tuple(int(x) for x in conflicts),
    ).fetchall()
    by_task: dict[str, list[str]] = {}
    for row in rows:
        by_task.setdefault(str(row["task_id"]), []).append(str(row["resource"]))
    with write_txn(conn):
        for tid, resources in by_task.items():
            current = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            if current is None or current["status"] in ("done", "archived"):
                continue
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind='needs_input', "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL "
                "WHERE id=?",
                (tid,),
            )
            conflict_payload = {
                "reason": "reversible execution effect conflicts with current state",
                "kind": "needs_input",
                "resources": resources[:20],
                "count": len(resources),
                "status": "blocked",
                "block_kind": "needs_input",
            }
            _append_event(conn, tid, "execution_effect_conflict", conflict_payload)
            # Reuse the existing sticky-block contract so recompute_ready cannot
            # silently release an unresolved ownership conflict next tick.
            _append_event(conn, tid, "blocked", conflict_payload)
    return len(reverted), len(conflicts)


def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the newest ``blocked``/``unblocked``/``gave_up`` event says the
    block must wait for an operator: an explicit ``kanban_block`` (#28712), or a
    breaker trip ``_record_task_failure`` stamped ``sticky`` — the clean-exit
    protocol-violation budget or a systemic same-error wave. Those trip on a
    policy independent of ``consecutive_failures``, so ``recompute_ready``'s
    counter check cannot see them — without this the trip is promoted back to
    ``ready`` in the same tick and the card respawns forever. A plain
    (unified-budget) ``gave_up`` carries no marker and is judged by the counter,
    so raising ``failure_limit`` or ``assign_task`` to a fresh profile still
    releases it; a task with no such event at all (direct DB edit) auto-recovers.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row and row["kind"] == "blocked":
        return True
    trip = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'gave_up' AND id > COALESCE("
        "  (SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'unblocked'), 0) "
        "ORDER BY id DESC LIMIT 1", (task_id, task_id),
    ).fetchone()
    return bool(trip) and bool(_json_dict(trip["payload"]).get("sticky"))


def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    """Newest ``task_events`` row of ``kind`` (optionally scoped to one run)."""
    sql = "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?"
    params: tuple[Any, ...] = (task_id, kind)
    if run_id is not None:
        sql += " AND run_id = ?"
        params = (*params, int(run_id))
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', "
        "'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited'"
        ") ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"


def finalize_stranded_verified_reviews(conn: sqlite3.Connection) -> int:
    """Replay-safe finalizer for Gauntlet subjects already VERIFIED in review.

    A verifier PASS can be durable before the gateway process that knows how to
    auto-finalize it is loaded (restart/deploy boundary).  Such a card must not
    require another model/human relay.  On each dispatcher tick, finish only
    cards whose persisted state already authorizes ordinary guarded completion.
    ``complete_task`` re-checks parents, Gauntlet verification, regression, and
    terminal-disposition invariants, so this helper adds no bypass.
    """
    rows = conn.execute(
        "SELECT id FROM tasks "
        "WHERE status = 'review' "
        "AND verification_state = ? "
        "AND regression_required = 0 "
        "AND terminal_disposition IS NULL",
        (VERIFICATION_VERIFIED,),
    ).fetchall()
    completed = 0
    for row in rows:
        task_id = row["id"]
        if not gauntlet_required(conn, task_id):
            continue
        if complete_task(
            conn, task_id,
            summary=(
                "Replay finalization: persisted independent VERIFIED verdict "
                "already authorizes guarded Gauntlet completion."
            ),
        ):
            completed += 1
    return completed


DISPATCH_WAKE_FILENAME = ".dispatcher.wake"


def dispatcher_wake_path() -> Path:
    """Machine-global wake signal consumed by the gateway dispatcher."""
    return kanban_home() / "kanban" / DISPATCH_WAKE_FILENAME


def signal_dispatcher_wake() -> None:
    """Best-effort nudge: wake the embedded dispatcher after work becomes ready.

    The signal carries no task data or authority; it only shortens the wait to
    the next ordinary dispatcher policy pass. Failure is intentionally non-fatal:
    the periodic dispatcher remains the recovery path.
    """
    try:
        path = dispatcher_wake_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except Exception as exc:
        _log.warning("kanban dispatcher wake signal failed: %s", exc)


def recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int:
    """Promote ``todo``/``blocked`` tasks whose parents are all satisfied;
    returns the count. Opens its own IMMEDIATE txn — call OUTSIDE any write txn.

    "Satisfied" is :func:`_parents_satisfied` — normally ``done``/``archived``,
    plus the evidence-ready verifier and governed implementation-handoff
    carve-outs described at that predicate.

    ``blocked`` is skipped when sticky (explicit ``kanban_block``) or when
    ``consecutive_failures`` reached the limit (else the breaker could never
    trip). Limit order matches ``_record_task_failure``: ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``.

    1. The most recent block event was a worker-initiated ``kanban_block`` — those stay blocked until an
    explicit ``kanban_unblock`` (#28712).
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries "
            "FROM tasks WHERE status IN ('todo', 'blocked') "
            # An irreversibly disposed card is terminal; auto-promotion must
            # never pull it back into the ready column, whatever its status
            # says.
            f"AND {_not_irreversibly_disposed_sql()}"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                # Explicit human-intervention block; only ``unblock_task`` may exit it.
                continue
            if _parents_satisfied(conn, task_id):
                resume_status = _resume_status_from_events(conn, task_id)
                if cur_status == "blocked":
                    # At the breaker limit, no auto-recovery (else block ->
                    # recover -> respawn -> exhaust -> block forever). The
                    # counter is preserved so it accumulates across cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = ? "
                        "WHERE id = ? AND status = 'blocked'", (resume_status, task_id),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE id = ? AND status = 'todo'",
                        (resume_status, task_id),
                    )
                _append_event(
                    conn, task_id, "promoted",
                    {"status": resume_status} if resume_status != "ready" else None,
                )
                promoted += 1
    return promoted


def _subject_has_evidence_sql(alias: str = "") -> str:
    """SQL predicate: this subject carries evidence a verifier could check.

    Either the subject holds an attachment of its own, or it inherits one from
    a ``done``/``archived`` + ``verified`` source parent.

    Factored out of :data:`_EVIDENCE_READY_PARENT_SQL` so the dispatch gate and
    the pre-flight check in :func:`_ensure_independent_verifier_child` are the
    *same* predicate rather than two hand-copied ones. That matters more than
    tidiness: if the two ever disagree, the control plane resumes creating
    verifier children it will never dispatch — the exact defect the pre-flight
    exists to prevent — and it would do so silently.
    """
    subject = f"{alias}.id" if alias else "id"
    return f"""
    (EXISTS (
        SELECT 1 FROM task_attachments a WHERE a.task_id = {subject}
    )
    OR EXISTS (
        SELECT 1
          FROM task_links src_link
          JOIN tasks src ON src.id = src_link.parent_id
         WHERE src_link.child_id = {subject}
           AND src.status IN ('done', 'archived')
           AND src.verification_state = :verified
           AND EXISTS (
               SELECT 1 FROM task_attachments sa WHERE sa.task_id = src.id
           )
    ))
"""


def subject_has_evidence(conn: sqlite3.Connection, task_id: str) -> bool:
    """Does this subject carry evidence an independent verifier could check?

    Reuses :func:`_subject_has_evidence_sql` verbatim rather than restating
    it. That helper's own docstring explains why this matters more than
    tidiness: the dispatch gate and the verifier pre-flight must not drift
    apart, and a third hand-copied predicate here would reintroduce exactly
    the defect they exist to prevent.
    """
    # The alias is load-bearing. Unaliased, the predicate emits
    # ``a.task_id = id``; SQLite binds that bare ``id`` to the INNER
    # table, and ``task_attachments`` has an ``id`` of its own -- so the
    # correlation silently becomes ``a.task_id = a.id`` and the whole
    # gate reads false for every subject. Same failure mode the COALESCE
    # note above guards: a predicate that inverts quietly rather than
    # erroring.
    row = conn.execute(
        "SELECT (" + _subject_has_evidence_sql("t") + ") AS ok "
        "FROM tasks t WHERE t.id = :subject",
        {"subject": task_id, "verified": VERIFICATION_VERIFIED},
    ).fetchone()
    return bool(row and row[0])


_EVIDENCE_READY_PARENT_SQL = f"""
    (
        (SELECT c.executor_lane FROM tasks c WHERE c.id = :child) IN (:verify_lane, :verify_lane2)
        -- A registered-profile verifier declared by the typed relation. Never
        -- the subject's implementer: that identity cannot verify its own work,
        -- so it is not released to try (selection and verdict must agree).
        OR EXISTS (
            SELECT 1 FROM task_relations vr
             WHERE vr.from_task_id = :child
               AND vr.to_task_id = p.id
               AND vr.relation = :verifies_rel
               AND COALESCE((SELECT c.assignee FROM tasks c WHERE c.id = :child), '')
                   <> COALESCE((
                       SELECT json_extract(rq.payload, '$.implementer')
                         FROM task_events rq
                        WHERE rq.task_id = p.id
                          AND rq.kind = 'review_requested'
                          AND json_valid(rq.payload)
                        ORDER BY rq.id DESC LIMIT 1
                   ), p.assignee, '')
        )
    )
    AND p.verification_state = :pending
    AND EXISTS (
        SELECT 1 FROM task_verifications v
         WHERE v.task_id = p.id
           AND v.kind = :verdict_kind
           AND v.state = :pending
    )
    AND {_subject_has_evidence_sql("p")}
"""
_GOVERNED_IMPLEMENTATION_HANDOFF_SQL = """
    (SELECT c.executor_lane FROM tasks c WHERE c.id = :child) = :implement_lane
    AND (SELECT c.gauntlet_enforced FROM tasks c WHERE c.id = :child) = 1
    AND p.verification_state = :pending
    AND EXISTS (
        SELECT 1 FROM task_verifications pv
         WHERE pv.task_id = p.id
           AND pv.kind = :verdict_kind
           AND pv.state = :pending
           -- Only the ledger head can describe the materialised pending
           -- phase.  Older pending rows are audit history, not reusable
           -- authority for a later handoff.
           AND pv.id = (
               SELECT MAX(head.id) FROM task_verifications head
                WHERE head.task_id = p.id
                  AND head.kind = :verdict_kind
           )
           AND EXISTS (
               SELECT 1
                 FROM task_events requested
                 JOIN task_events blocked ON blocked.task_id = requested.task_id
                WHERE requested.task_id = p.id
                  AND requested.kind = 'review_requested'
                  AND requested.run_id = pv.run_id
                  -- Bind the ledger phase to its exact/latest durable
                  -- handoff, rather than merely another event for the same
                  -- implementation run.
                  AND requested.id = (
                      SELECT MAX(latest.id) FROM task_events latest
                       WHERE latest.task_id = p.id
                         AND latest.kind = 'review_requested'
                  )
                  AND blocked.kind = 'verification_blocked_self_review'
                  AND requested.id < blocked.id
                  AND (
                      -- Parked ``record_verification`` refusals are anchored
                      -- directly to the implementation run that opened pv.
                      blocked.run_id = pv.run_id
                      OR EXISTS (
                          -- Inline ``complete_task`` refusals happen in a
                          -- later run claimed from review. Bind that review
                          -- run back to this exact implementation handoff by
                          -- the ordered, durable event chain rather than by
                          -- identity or current task state.
                          SELECT 1 FROM task_events claimed
                           WHERE claimed.task_id = requested.task_id
                             AND claimed.run_id = blocked.run_id
                             AND claimed.kind = 'claimed'
                             AND json_valid(claimed.payload)
                             AND json_extract(
                                 claimed.payload, '$.source_status'
                             ) = 'review'
                             AND requested.id < claimed.id
                             AND claimed.id < blocked.id
                      )
                  )
           )
    )
    AND EXISTS (SELECT 1 FROM task_attachments pa WHERE pa.task_id = p.id)
    AND EXISTS (
        SELECT 1 FROM task_events created
         WHERE created.task_id = :child
           AND created.kind = 'created'
           AND json_valid(created.payload)
           AND json_extract(created.payload, '$.executor_lane') = :implement_lane
           AND EXISTS (
               SELECT 1 FROM json_each(created.payload, '$.parents') ep
                WHERE ep.value = p.id
           )
           AND created.id < (
               SELECT MAX(requested.id) FROM task_events requested
                WHERE requested.task_id = p.id
                  AND requested.kind = 'review_requested'
           )
    )
    AND EXISTS (
        SELECT 1
          FROM task_links vl
          JOIN tasks verifier ON verifier.id = vl.child_id
         WHERE vl.parent_id = :child
           AND verifier.executor_lane = :verify_lane
           AND verifier.gauntlet_enforced = 1
           AND EXISTS (
               SELECT 1 FROM task_events vc
                WHERE vc.task_id = verifier.id
                  AND vc.kind = 'created'
                  AND json_valid(vc.payload)
                  AND json_extract(vc.payload, '$.executor_lane') = :verify_lane
                  AND EXISTS (
                      SELECT 1 FROM json_each(vc.payload, '$.parents') vp
                       WHERE vp.value = :child
                  )
                  AND vc.id < (
                      SELECT MAX(requested.id) FROM task_events requested
                       WHERE requested.task_id = p.id
                         AND requested.kind = 'review_requested'
                  )
           )
    )
"""
_UNSATISFIED_PARENTS_SQL = f"""
    SELECT 1
      FROM task_links l
      JOIN tasks p ON p.id = l.parent_id
     WHERE l.child_id = :child
       AND p.status NOT IN ('done', 'archived')
       AND {_not_irreversibly_disposed_sql("p")}
       AND NOT COALESCE((
           ({_EVIDENCE_READY_PARENT_SQL})
           OR ({_GOVERNED_IMPLEMENTATION_HANDOFF_SQL})
       ), 0)
     LIMIT 1
"""
_PARENT_GATE_PARAMS = {
    "verifies_rel": RELATION_VERIFIES,
    "verify_lane": EXECUTOR_LANE_CODEX_VERIFY,
    "verify_lane2": EXECUTOR_LANE_CLAUDE_VERIFY,
    "implement_lane": EXECUTOR_LANE_CLAUDE,
    "pending": VERIFICATION_PENDING,
    "verdict_kind": LEDGER_KIND_VERDICT,
    "verified": VERIFICATION_VERIFIED,
}


def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return whether every direct parent is satisfied for dependency gating.

    Satisfied means terminal ('done'/'archived'), evidence-ready for an
    independent verifier child, or a self-review-blocked evidence handoff for
    a pre-wired governed implementation -> independent-verifier graph. See the
    two SQL predicates above for their fail-closed evidence requirements.

    This is the dependency-eligibility predicate: :func:`recompute_ready`,
    :func:`claim_task`, :func:`claim_review_task`, :func:`complete_task` and
    :func:`request_review` all read it from here so they cannot drift into
    disagreeing about the same chain.
    """
    return conn.execute(
        _UNSATISFIED_PARENTS_SQL, {**_PARENT_GATE_PARAMS, "child": task_id},
    ).fetchone() is None


GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT = 6
OBJECTIVE_ATTEMPT_BUDGET_MODE_ENFORCE = "enforce"
OBJECTIVE_ATTEMPT_BUDGET_MODE_ADVISORY = "advisory"


def gauntlet_objective_attempt_limit() -> int:
    """Board-authoritative implementation+review+verifier run ceiling."""
    try:
        raw = _board_kanban_cfg().get(
            "gauntlet_objective_attempt_limit", GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT
        )
        value = int(raw)
        return value if value >= 1 else GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT
    except Exception:
        return GAUNTLET_OBJECTIVE_ATTEMPT_LIMIT_DEFAULT


def objective_attempt_budget_mode() -> str:
    """Board-scoped admission mode; worker profile config cannot override it."""
    raw = str(_board_kanban_cfg().get(
        "objective_attempt_budget_mode", OBJECTIVE_ATTEMPT_BUDGET_MODE_ENFORCE
    )).strip().lower()
    if raw == OBJECTIVE_ATTEMPT_BUDGET_MODE_ADVISORY:
        return raw
    return OBJECTIVE_ATTEMPT_BUDGET_MODE_ENFORCE


_OBJECTIVE_LINEAGE_MAX_DEPTH = 16
_LINEAGE_SQL_CHUNK = 400


def _objective_lineage_root(conn: sqlite3.Connection, task_id: str) -> str:
    """Walk to the root of the objective ``task_id`` is a continuation of.

    Exactly two edge kinds continue the SAME objective:

    * a ``codex_verify`` child -> its subject: a verifier exists only to serve
      the subject's objective, so its runs are spent on that objective.
    * a card that ``repairs`` X -> X: a repair is another attempt at X.

    Deliberately NOT followed, because each would make one budget swallow work
    that is not a retry:

    * ``RELATION_UMBRELLA`` -- an umbrella is an ORGANISING parent. Its children
      are separate objectives that merely share a heading. Following it would
      give a whole project one budget of six and starve honest multi-stage work.
    * ``RELATION_EVIDENCE`` -- documented as non-governing.
    * plain ``task_links`` parents -- that edge is a dependency, i.e. legitimate
      follow-on work, not another attempt at the same thing.

    This distinction is the whole design: the budget must bound *replacements
    and retries of one objective*, never *legitimate follow-on work*.
    """
    seen: set = set()
    current = task_id
    for _ in range(_OBJECTIVE_LINEAGE_MAX_DEPTH):
        if current in seen:
            break
        seen.add(current)
        row = conn.execute(
            "SELECT executor_lane FROM tasks WHERE id = ?", (current,)
        ).fetchone()
        if row is not None and row["executor_lane"] in INDEPENDENT_VERIFIER_LANES:
            parent = conn.execute(
                "SELECT l.parent_id FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                "WHERE l.child_id=? ORDER BY p.created_at ASC LIMIT 1", (current,)
            ).fetchone()
            if parent is not None and str(parent["parent_id"]) not in seen:
                current = str(parent["parent_id"])
                continue
        repaired = conn.execute(
            "SELECT to_task_id FROM task_relations "
            "WHERE from_task_id = ? AND relation = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (current, RELATION_REPAIRS),
        ).fetchone()
        if repaired is not None and str(repaired["to_task_id"]) not in seen:
            current = str(repaired["to_task_id"])
            continue
        break
    return current


def _objective_lineage_members(conn: sqlite3.Connection, root: str) -> set:
    """Every card that spends the objective budget of ``root`` (root included).

    The inverse of :func:`_objective_lineage_root`, walked downward, so a card
    that spawned a replacement cannot escape the budget by being a new row.
    """
    members = {root}
    frontier = [root]
    for _ in range(_OBJECTIVE_LINEAGE_MAX_DEPTH):
        if not frontier:
            break
        found: list = []
        for i in range(0, len(frontier), _LINEAGE_SQL_CHUNK):
            batch = frontier[i:i + _LINEAGE_SQL_CHUNK]
            marks = ",".join("?" * len(batch))
            rows = conn.execute(
                f"SELECT l.child_id AS id FROM task_links l "
                f"JOIN tasks c ON c.id = l.child_id "
                f"WHERE l.parent_id IN ({marks}) AND c.executor_lane IN (?, ?) "
                f"UNION "
                f"SELECT r.from_task_id AS id FROM task_relations r "
                f"WHERE r.to_task_id IN ({marks}) AND r.relation = ?",
                (*batch, EXECUTOR_LANE_CODEX_VERIFY, EXECUTOR_LANE_CLAUDE_VERIFY, *batch, RELATION_REPAIRS),
            ).fetchall()
            found.extend(str(r["id"]) for r in rows)
        nxt = [i for i in dict.fromkeys(found) if i not in members]
        members.update(nxt)
        frontier = nxt
    return members


def gauntlet_objective_attempts(conn: sqlite3.Connection, task_id: str) -> int:
    """Count every run against one subject, including direct verifier children.

    Timeouts, controller loss, review runs, verifier FAILs and successful runs all
    count.  This deliberately ignores mechanism-specific failure classification:
    the budget answers only "how many times did we spend capacity on this same
    objective?".
    """
    root = _objective_lineage_root(conn, task_id)
    members = sorted(_objective_lineage_members(conn, root))
    total = 0
    for i in range(0, len(members), _LINEAGE_SQL_CHUNK):
        batch = members[i:i + _LINEAGE_SQL_CHUNK]
        marks = ",".join("?" * len(batch))
        total += int(conn.execute(
            f"SELECT COUNT(*) FROM task_runs r WHERE r.task_id IN ({marks})",
            tuple(batch),
        ).fetchone()[0])
    return total


def repeated_error_limit() -> int:
    """How many times one error may recur against a single objective.

    Config: ``kanban.repeated_error_limit``. Deliberately far below the
    objective attempt budget — the point is to catch a wrong fix while the
    objective still has room to try a different one.
    """
    try:
        from hermes_cli.config import load_config
        return max(2, int(
            _kanban_cfg(load_config).get(
                "repeated_error_limit", REPEATED_ERROR_LIMIT_DEFAULT
            )
        ))
    except Exception:
        return REPEATED_ERROR_LIMIT_DEFAULT


def objective_error_recurrences(
    conn: sqlite3.Connection, task_id: str, signature: str
) -> int:
    """How many runs in this objective's lineage failed with this signature.

    Counts ``task_runs.error`` rows, which are append-only. The existing
    counters (``consecutive_failures``, ``review_reopened``) are cleared on
    reopen, which is precisely why the three-retry ceiling was unreachable in
    practice; a bound built on them would inherit that defect.
    """
    if not signature:
        return 0
    root = _objective_lineage_root(conn, task_id)
    members = sorted(_objective_lineage_members(conn, root))
    hits = 0
    for i in range(0, len(members), _LINEAGE_SQL_CHUNK):
        batch = members[i:i + _LINEAGE_SQL_CHUNK]
        marks = ",".join("?" * len(batch))
        outcome_marks = ",".join("?" * len(IMPLEMENTATION_FAILURE_OUTCOMES))
        for row in conn.execute(
            f"SELECT error FROM task_runs WHERE task_id IN ({marks}) "
            "AND error IS NOT NULL AND error != '' "
            f"AND outcome IN ({outcome_marks})",
            tuple(batch) + tuple(sorted(IMPLEMENTATION_FAILURE_OUTCOMES)),
        ):
            if error_signature(row["error"]) == signature:
                hits += 1
    return hits


def repeated_error_ceiling_reached(
    conn: sqlite3.Connection, task_id: str
) -> tuple:
    """(reached, signature, count, limit, root) for this task's latest error.

    Universal, like the attempt ceiling: retry protection is a safety control,
    not a governance policy, so it is not gated on Gauntlet enforcement.
    """
    root = _objective_lineage_root(conn, task_id)
    outcome_marks = ",".join("?" * len(IMPLEMENTATION_FAILURE_OUTCOMES))
    row = conn.execute(
        "SELECT error FROM task_runs WHERE task_id = ? AND error IS NOT NULL "
        f"AND error != '' AND outcome IN ({outcome_marks}) "
        "ORDER BY id DESC LIMIT 1",
        (task_id,) + tuple(sorted(IMPLEMENTATION_FAILURE_OUTCOMES)),
    ).fetchone()
    if row is None:
        return (False, "", 0, repeated_error_limit(), root)
    sig = error_signature(row["error"])
    if not sig:
        return (False, "", 0, repeated_error_limit(), root)
    limit = repeated_error_limit()
    count = objective_error_recurrences(conn, task_id, sig)
    return (count >= limit, sig, count, limit, root)


def _block_repeated_error(conn: sqlite3.Connection, task_id: str) -> None:
    reached, sig, count, limit, root = repeated_error_ceiling_reached(
        conn, task_id
    )
    if not reached:
        return
    reason = (
        f"ERROR_RECURRED: the same failure has now occurred {count} times "
        f"against objective lineage {root} (limit {limit}). Signature: "
        f"{sig[:200]!r}. Retrying has not changed the outcome, so the fix is "
        f"wrong or the cause is elsewhere. This is NOT a failure verdict -- "
        f"it needs a different approach or an operator decision. Look up what "
        f"is known about this error first: "
        f'hermes kanban lessons --error "<the error text>"'
    )
    for tid in dict.fromkeys((root, task_id)):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (tid,)
        ).fetchone()
        if row is None or row["status"] in (
            "done", "archived", "failed", "cancelled"
        ):
            continue
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind=?, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "last_failure_error=? WHERE id=?",
            (BLOCK_KIND_ERROR_RECURRED, reason, tid),
        )
        _append_event(conn, tid, "repeated_error_ceiling_reached", {
            "objective": root, "signature": sig[:200],
            "count": count, "limit": limit,
        })
        _append_event(conn, tid, "blocked", {
            "reason": reason,
            "kind": BLOCK_KIND_ERROR_RECURRED,
            "source": "repeated_error_ceiling",
        })


OBJECTIVE_ATTEMPT_GRANT_EVENT = "objective_attempt_budget_granted"
OBJECTIVE_ATTEMPT_GRANT_MAX = 6
_GOVERNED_RUN_ENV_MARKERS = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_EXECUTION_ID",
    "HERMES_EXECUTOR_LANE",
)
_AUTOMATION_IDENTITIES = frozenset({
    "default", "atlas", "system", "unknown", "dispatcher", "kanban-dispatcher",
    "stale-supervision", "stale_supervision", "system-health-controller",
    *VALID_EXECUTOR_LANES,
})


class ObjectiveAttemptGrantRefused(PermissionError):
    """An attempt-budget grant was refused; nothing was recorded."""


def objective_attempt_grants(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Recorded operator grants for ``task_id``'s objective lineage, oldest first."""
    root = _objective_lineage_root(conn, task_id)
    grants: list[dict] = []
    for row in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (root, OBJECTIVE_ATTEMPT_GRANT_EVENT),
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
            added = payload.get("added_attempts")
        except Exception:
            continue
        if (
            isinstance(added, int) and not isinstance(added, bool)
            and 1 <= added <= OBJECTIVE_ATTEMPT_GRANT_MAX
            and payload.get("objective") == root
        ):
            grants.append({**payload, "event_id": int(row["id"])})
    return grants


def effective_objective_attempt_limit(conn: sqlite3.Connection, task_id: str) -> int:
    """Base board limit plus this objective's operator grants."""
    grants = objective_attempt_grants(conn, task_id)
    # A grant records the board base the human authorized. If a later worker
    # cannot read board config, never reinterpret that durable authorization
    # against the smaller code default.
    authorized_bases: list[int] = []
    for grant in grants:
        recorded_base = grant.get("base_limit")
        if (
            isinstance(recorded_base, int)
            and not isinstance(recorded_base, bool)
            and recorded_base >= 1
        ):
            authorized_bases.append(recorded_base)
    base = max([gauntlet_objective_attempt_limit(), *authorized_bases])
    return base + sum(g["added_attempts"] for g in grants)


def objective_attempt_budget_enforced() -> bool:
    return objective_attempt_budget_mode() == OBJECTIVE_ATTEMPT_BUDGET_MODE_ENFORCE


def _process_ancestry() -> list[int]:
    pids: list[int] = []
    pid = os.getpid()
    for _ in range(64):
        pids.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        pid = ppid
    return pids


def _governed_grant_context(conn: sqlite3.Connection, env: Mapping[str, str]) -> Optional[str]:
    """Why this caller is automation rather than an operator, or ``None``."""
    for marker in _GOVERNED_RUN_ENV_MARKERS:
        if (env.get(marker) or "").strip():
            return f"governed run marker {marker} is set"
    ancestry = set(_process_ancestry())
    # In PID-namespaced/sandboxed runners, /proc ancestry may not expose the
    # host-visible parent chain even though the immediate parent is recorded.
    # Check the kernel's direct parent independently before relying on /proc.
    try:
        direct_parent = os.getppid()
    except OSError:
        direct_parent = None
    try:
        own_pgid = os.getpgid(0)
    except OSError:
        own_pgid = None
    for row in conn.execute(
        "SELECT id, pid, pgid FROM executions WHERE ended_at IS NULL"
    ):
        if row["pid"] is not None and (
            int(row["pid"]) in ancestry or int(row["pid"]) == direct_parent
        ):
            return f"caller descends from live execution {row['id']}"
        if own_pgid is not None and row["pgid"] is not None and int(row["pgid"]) == own_pgid:
            return f"caller shares the process group of live execution {row['id']}"
    return None


def _automation_identity(name: str) -> bool:
    lowered = name.strip().lower()
    if lowered in _AUTOMATION_IDENTITIES or lowered.endswith("-lane"):
        return True
    try:
        return (kanban_home() / "profiles" / name.strip()).is_dir()
    except Exception:
        return False


def grant_objective_attempts(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    added_attempts: int,
    authorized_by: str,
    reason: str,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[int] = None,
) -> dict:
    """Record one finite operator grant of extra attempts for one objective.

    Only raises the objective's effective ceiling. It never unblocks, claims,
    dispatches, requests review or resets any counter. Fails closed: refused
    for any governed run or descendant of a live execution, for any provenance
    other than an explicitly declared ``human_interactive`` operator, and for
    automation identities (``default``, executor lanes, profiles, controllers).
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if isinstance(added_attempts, bool) or not isinstance(added_attempts, int) or not (
        1 <= added_attempts <= OBJECTIVE_ATTEMPT_GRANT_MAX
    ):
        raise ObjectiveAttemptGrantRefused(
            f"added_attempts must be an integer from 1 to {OBJECTIVE_ATTEMPT_GRANT_MAX}"
        )
    authorized_by = (authorized_by or "").strip()
    reason = (reason or "").strip()
    if not authorized_by or not reason:
        raise ObjectiveAttemptGrantRefused("a grant requires authorized_by and a reason")
    context = _governed_grant_context(conn, environ)
    if context is not None:
        raise ObjectiveAttemptGrantRefused(f"automation cannot grant attempts: {context}")
    declared = (environ.get(ENV_ACTOR_KIND) or "").strip()
    actor_id = (environ.get(ENV_ACTOR_ID) or "").strip()
    if declared != ACTOR_KIND_HUMAN_INTERACTIVE or not actor_id:
        raise ObjectiveAttemptGrantRefused(
            f"a grant requires explicit operator provenance: {ENV_ACTOR_KIND}="
            f"{ACTOR_KIND_HUMAN_INTERACTIVE} and a named {ENV_ACTOR_ID}"
        )
    for name in (actor_id, authorized_by):
        if _automation_identity(name):
            raise ObjectiveAttemptGrantRefused(
                f"{name!r} is an automation identity and cannot authorize a grant"
            )
    if actor_id.casefold() != "christopher" or authorized_by.casefold() != "christopher":
        raise ObjectiveAttemptGrantRefused(
            "attempt grants require explicit Christopher authorization"
        )
    now_ts = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise ObjectiveAttemptGrantRefused(f"task {task_id} not found")
        root = _objective_lineage_root(conn, task_id)
        status = conn.execute("SELECT status FROM tasks WHERE id = ?", (root,)).fetchone()
        if status is None:
            raise ObjectiveAttemptGrantRefused(f"objective {root} not found")
        if status["status"] in ("done", "archived", "failed", "cancelled"):
            # A closed repaired objective is still immutable.  The one narrow
            # continuation case is an explicitly governed, still-open repair
            # card that was created as a descendant of that objective: the
            # operator is extending the repair attempt, not reopening the
            # repaired root.  Do not infer this from identity or prose; require
            # the durable recovery cause, owner, both mandatory repair links,
            # and the direct repairs relation that made this card a descendant.
            subject = conn.execute(
                "SELECT status, creation_cause, recovery_owner FROM tasks "
                "WHERE id = ?", (task_id,)
            ).fetchone()
            is_open_repair = (
                task_id != root
                and subject is not None
                and subject["status"] not in ("done", "archived", "failed", "cancelled")
                and subject["creation_cause"] == CREATION_CAUSE_RECOVERY
                and bool((subject["recovery_owner"] or "").strip())
                and not missing_repair_relations(conn, task_id)
                and conn.execute(
                    "SELECT 1 FROM task_relations "
                    "WHERE from_task_id = ? AND relation = ? LIMIT 1",
                    (task_id, RELATION_REPAIRS),
                ).fetchone() is not None
            )
            if not is_open_repair:
                raise ObjectiveAttemptGrantRefused(f"objective {root} is not open")
        base = gauntlet_objective_attempt_limit()
        prior = effective_objective_attempt_limit(conn, root)
        payload = {
            "objective": root,
            "subject_requested": task_id,
            "added_attempts": added_attempts,
            "attempts_before": gauntlet_objective_attempts(conn, root),
            "base_limit": base,
            "prior_effective_limit": prior,
            "new_effective_limit": prior + added_attempts,
            "authorized_by": authorized_by,
            "reason": reason,
            "actor_kind": declared,
            "actor_id": actor_id,
            "authorization_source": "hermes kanban attempt-budget --grant",
            "granted_at": now_ts,
            # What the failures behind this grant amounted to when it was made,
            # so a later grant can be read against whether anything changed.
            "failure_basis": _safe_objective_failure_basis(conn, root),
        }
        _append_event(conn, root, OBJECTIVE_ATTEMPT_GRANT_EVENT, payload)
    return payload


def _objective_attempt_ceiling_reached(conn: sqlite3.Connection, task_id: str) -> tuple[bool, int, int, str]:
    """Universal. Applies to EVERY task, Gauntlet-governed or not.

    Retry protection is a safety control; Gauntlet verification is a governance
    policy. Coupling them meant exempting work from verification also exempted
    it from having any brakes, which is how an ungoverned task could retry
    without bound. The two are now independent. The limit is the board base
    plus any operator continuation grants recorded for this objective.
    """
    root = _objective_lineage_root(conn, task_id)
    limit = effective_objective_attempt_limit(conn, root)
    attempts = gauntlet_objective_attempts(conn, root)
    return attempts >= limit, attempts, limit, root


FAILURE_BASIS_REPEATED = "REPEATED_FAILURE_UNRESOLVED"
FAILURE_BASIS_UNRESOLVED = "FAILURE_UNRESOLVED"
FAILURE_BASIS_EVIDENCE_UNVERIFIED = "CORRECTED_EVIDENCE_UNVERIFIED"
FAILURE_BASIS_RESOLVED = "FAILURES_RESOLVED"
FAILURE_BASIS_NONE = "NO_FAILED_VERDICTS"
FAILURE_BASIS_UNAVAILABLE = "FAILURE_BASIS_UNAVAILABLE"
_FAILURE_CRITERION_MATCH = 0.6
_FAILING_CRITERION_RE = re.compile(
    r"[:\-—]\s*(FAIL|FAILED|BLOCKED|INSUFFICIENT)\b"
)


def failure_fingerprint(text: Optional[str]) -> dict[str, Any]:
    """Failing criteria and a stable fingerprint for one failed verdict's report.

    A failing criterion is a bullet line whose label is followed by a
    ``FAIL``/``BLOCKED``/``INSUFFICIENT`` marker, e.g.
    ``- **Live-status predicate:** **FAIL.** ...``. Summary lines such as
    ``VERDICT: FAIL`` are not bullets and are ignored. When no criterion
    parses, the whole text is the single criterion. Terms are normalised with
    :func:`_error_signature_terms`, so ids, paths and counts do not make a
    recurrence look novel.
    """
    criteria: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line.startswith(("-", "*", "•")):
            continue
        line = line.lstrip("-*• ").replace("*", "").replace("`", "")
        match = _FAILING_CRITERION_RE.search(line)
        if match is None:
            continue
        label = line[: match.start()].strip(" :-—.") or line[match.end():].strip(" :-—.")
        terms = sorted(_error_signature_terms(label))
        if terms and all(c["terms"] != terms for c in criteria):
            criteria.append({"label": label[:160], "terms": terms})
    source = "criteria"
    if not criteria:
        terms = sorted(_error_signature_terms(text or ""))
        source = "text"
        if terms:
            criteria.append({"label": (text or "").strip()[:160], "terms": terms})
    digest = hashlib.sha256(
        "\n".join(" ".join(c["terms"]) for c in sorted(criteria, key=lambda c: c["terms"]))
        .encode("utf-8")
    ).hexdigest()[:16] if criteria else ""
    return {"fingerprint": digest, "criteria": criteria, "source": source}


def _failed_verdict_report(conn: sqlite3.Connection, verdict: sqlite3.Row) -> str:
    """The failing verifier's own report for a failed verdict row, else its reason."""
    verifier_task = None
    try:
        verifier_task = (json.loads(verdict["evidence"] or "{}") or {}).get("verifier_task")
    except (TypeError, ValueError, AttributeError):
        verifier_task = None
    if not verifier_task and any(
        str(verdict["verifier"] or "").startswith(f"{lane}:")
        for lane in INDEPENDENT_VERIFIER_LANES
    ):
        verifier_task = str(verdict["verifier"]).split(":", 1)[1]
    if verifier_task:
        row = conn.execute(
            "SELECT result FROM tasks WHERE id = ?", (str(verifier_task),)
        ).fetchone()
        if row is not None and (row["result"] or "").strip():
            return str(row["result"])
        run = conn.execute(
            "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL "
            "AND summary != '' ORDER BY id DESC LIMIT 1",
            (str(verifier_task),),
        ).fetchone()
        if run is not None:
            return str(run["summary"])
    return str(verdict["reason"] or "")


def _criteria_match(a: list, b: list) -> bool:
    sa, sb = set(a), set(b)
    union = sa | sb
    return bool(union) and len(sa & sb) / len(union) >= _FAILURE_CRITERION_MATCH


def objective_failure_ledger(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    """Every failed verdict in ``task_id``'s objective lineage, oldest first.

    Each entry's ``status`` is decided by the next verdict on the same subject:

    * ``resolved``         -- the next judging verdict was VERIFIED;
    * ``refailed``         -- the next judging verdict failed again (that later
                              failure carries the current state);
    * ``evidence_pending`` -- evidence was handed off again (a later
                              ``pending`` row) and no verifier has judged it;
    * ``open``             -- nothing has happened on the subject since.

    ``repeats`` lists earlier failed verdicts in the lineage that failed on a
    matching criterion, with the matching labels.
    """
    root = _objective_lineage_root(conn, task_id)
    members = sorted(_objective_lineage_members(conn, root))
    rows: list[sqlite3.Row] = []
    for i in range(0, len(members), _LINEAGE_SQL_CHUNK):
        batch = members[i:i + _LINEAGE_SQL_CHUNK]
        marks = ",".join("?" * len(batch))
        rows.extend(conn.execute(
            f"SELECT id, task_id, state, verifier, evidence, reason, created_at "
            f"FROM task_verifications WHERE task_id IN ({marks}) AND kind = 'verdict' "
            f"ORDER BY id",
            tuple(batch),
        ).fetchall())
    rows.sort(key=lambda r: int(r["id"]))
    ledger: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        if row["state"] != VERIFICATION_FAILED:
            continue
        status = "open"
        for later in rows[idx + 1:]:
            if later["task_id"] != row["task_id"]:
                continue
            if later["state"] == VERIFICATION_VERIFIED:
                status = "resolved"
                break
            if later["state"] == VERIFICATION_FAILED:
                status = "refailed"
                break
            if later["state"] == VERIFICATION_PENDING:
                status = "evidence_pending"
        fp = failure_fingerprint(_failed_verdict_report(conn, row))
        repeats: list[dict[str, Any]] = []
        for earlier in ledger:
            labels = [
                c["label"] for c in fp["criteria"]
                if any(_criteria_match(c["terms"], e["terms"]) for e in earlier["criteria"])
            ]
            if labels:
                repeats.append({"verification_id": earlier["verification_id"], "criteria": labels})
        ledger.append({
            "verification_id": int(row["id"]),
            "subject": str(row["task_id"]),
            "verifier": row["verifier"],
            "failed_at": int(row["created_at"]),
            "status": status,
            "fingerprint": fp["fingerprint"],
            "fingerprint_source": fp["source"],
            "criteria": fp["criteria"],
            "repeats": repeats,
        })
    return ledger


def objective_failure_basis(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """What the failed verdicts behind this objective's budget currently amount to.

    Ordered most-conservative first: any still-open failure outranks corrected
    evidence elsewhere in the lineage, and an open failure that repeats an
    earlier one outranks both. Informational only -- it never admits a claim.
    """
    ledger = objective_failure_ledger(conn, task_id)
    current = [e for e in ledger if e["status"] != "refailed"]
    counts = {
        s: sum(1 for e in ledger if e["status"] == s)
        for s in ("resolved", "refailed", "evidence_pending", "open")
    }
    repeated = [e for e in ledger if e["repeats"]]
    open_ = [e for e in current if e["status"] == "open"]
    pending = [e for e in current if e["status"] == "evidence_pending"]
    if not ledger:
        basis = FAILURE_BASIS_NONE
    elif any(e["repeats"] for e in open_):
        basis = FAILURE_BASIS_REPEATED
    elif open_:
        basis = FAILURE_BASIS_UNRESOLVED
    elif pending:
        basis = FAILURE_BASIS_EVIDENCE_UNVERIFIED
    else:
        basis = FAILURE_BASIS_RESOLVED
    return {
        "basis": basis,
        "failed_verdicts": len(ledger),
        "superseded": counts["resolved"] + counts["refailed"] + counts["evidence_pending"],
        **counts,
        "repeated": len(repeated),
        "open_verifications": [e["verification_id"] for e in open_],
        "evidence_pending_verifications": [e["verification_id"] for e in pending],
        "fingerprints": [
            {k: e[k] for k in ("verification_id", "subject", "status", "fingerprint",
                               "fingerprint_source")}
            | {"criteria": [c["label"] for c in e["criteria"]],
               "repeats": [r["verification_id"] for r in e["repeats"]]}
            for e in ledger
        ],
    }


def _safe_objective_failure_basis(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """:func:`objective_failure_basis` for the claim/grant paths, which must not raise."""
    try:
        return objective_failure_basis(conn, task_id)
    except Exception as exc:
        return {"basis": FAILURE_BASIS_UNAVAILABLE, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _describe_failure_basis(summary: dict[str, Any]) -> str:
    """One operator-facing sentence distinguishing why the budget is exhausted."""
    basis = summary.get("basis")
    m = summary.get("failed_verdicts", 0)

    def _where(ids: list) -> str:
        fp_by_id = {f["verification_id"]: f for f in summary.get("fingerprints", [])}

        def _one(i):
            fp = fp_by_id.get(i)
            if fp is None:
                return f"verification {i} on ?"
            subject = fp.get("subject", "?")
            labels = [c for c in fp.get("criteria", []) if c]
            if labels:
                return f"verification {i} on {subject} (unmet: {'; '.join(labels)})"
            return f"verification {i} on {subject}"

        return ", ".join(_one(i) for i in ids)

    if basis == FAILURE_BASIS_REPEATED:
        return (
            f"Failure basis: {basis} -- the unresolved failed verdict(s) "
            f"({_where(summary['open_verifications'])}) repeat failing criteria from "
            f"earlier verdicts in this lineage, and no new evidence has been handed "
            f"off since. More attempts would repeat a known failure; change the "
            f"approach before granting."
        )
    if basis == FAILURE_BASIS_UNRESOLVED:
        return (
            f"Failure basis: {basis} -- {len(summary['open_verifications'])} of {m} "
            f"failed verdict(s) are still unresolved with no new evidence handed off "
            f"since ({_where(summary['open_verifications'])}); {summary['resolved']} "
            f"were resolved by a later VERIFIED verdict."
        )
    if basis == FAILURE_BASIS_EVIDENCE_UNVERIFIED:
        return (
            f"Failure basis: {basis} -- all {m} failed verdict(s) consuming this budget "
            f"have since been superseded ({summary['resolved']} resolved by a later "
            f"VERIFIED verdict, {summary['refailed']} by a later verdict, "
            f"{summary['evidence_pending']} by new evidence handed off after the "
            f"failure that no verifier has judged yet: failed "
            f"{_where(summary['evidence_pending_verifications'])}); "
            f"{summary['repeated']} of them repeated earlier failing criteria. The "
            f"exhaustion was spent before that evidence existed -- an operator may "
            f"grant on that basis rather than on raw exhaustion."
        )
    if basis == FAILURE_BASIS_RESOLVED:
        return (
            f"Failure basis: {basis} -- all {m} failed verdict(s) were resolved by a "
            f"later VERIFIED verdict; the exhaustion is capacity, not an open failure."
        )
    if basis == FAILURE_BASIS_NONE:
        return (
            f"Failure basis: {basis} -- no failed verdict spent this budget, so there "
            f"is no failure fingerprint to compare."
        )
    return f"Failure basis: {FAILURE_BASIS_UNAVAILABLE} -- could not be computed."


def _in_flight_verifier_for_subject(
    conn: sqlite3.Connection, subject_id: str
) -> Optional[tuple[str, int]]:
    """The independent verifier already running against ``subject_id``'s open review, if any.

    Returns ``(verifier_task_id, run_id)`` only when ALL of these hold:

    * the subject is itself not a ``codex_verify`` card (a verifier is never an
      exempt subject -- no verifier-of-verifier);
    * the subject is parked in ``review`` with ``verification_state='pending'``;
    * a ``codex_verify`` child linked to it is ``running`` on an open current run;
    * that run started during the subject's current review phase (at or after
      its latest ``review_requested``) -- a leftover from an earlier phase is not
      this phase's verifier.

    Used by :func:`_block_objective_attempt_ceiling`: the verifier's claim was the
    attempt the ceiling allowed, so its verdict must still be able to land in the
    review lane (2026-09-15, ``t_b8d62378`` events 158976-158993).
    """
    srow = conn.execute(
        "SELECT status, verification_state, executor_lane FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if (
        srow is None
        or srow["executor_lane"] in INDEPENDENT_VERIFIER_LANES
        or srow["status"] != "review"
        or srow["verification_state"] != VERIFICATION_PENDING
    ):
        return None
    phase = conn.execute(
        "SELECT MAX(created_at) AS at FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested'",
        (subject_id,),
    ).fetchone()
    if phase is None or phase["at"] is None:
        return None
    row = conn.execute(
        "SELECT c.id AS id, r.id AS run_id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        "JOIN task_runs r ON r.id = c.current_run_id "
        "WHERE l.parent_id = ? AND c.executor_lane IN (?, ?) AND c.status = 'running' "
        "AND r.ended_at IS NULL AND r.started_at >= ? "
        "ORDER BY r.started_at ASC, r.id ASC LIMIT 1",
        (subject_id, EXECUTOR_LANE_CODEX_VERIFY, EXECUTOR_LANE_CLAUDE_VERIFY, int(phase["at"])),
    ).fetchone()
    if row is None:
        return None
    return str(row["id"]), int(row["run_id"])


def _block_objective_attempt_ceiling(conn: sqlite3.Connection, task_id: str) -> None:
    reached, attempts, limit, root = _objective_attempt_ceiling_reached(conn, task_id)
    if not reached:
        return
    # Classifies the exhaustion; it never changes whether the ceiling blocks.
    failure_basis = _safe_objective_failure_basis(conn, root)
    reason = (
        f"ATTEMPT_BUDGET_EXHAUSTED: {attempts} total runs against objective "
        f"lineage {root} (limit {limit}). This is NOT a failure verdict -- "
        f"automation has spent its budget on this objective and continuing "
        f"requires an operator decision. {_describe_failure_basis(failure_basis)}"
    )
    for tid in dict.fromkeys((root, task_id)):
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row["status"] in ("done", "archived", "failed", "cancelled"):
            continue
        in_flight = _in_flight_verifier_for_subject(conn, tid)
        if in_flight is not None:
            # The ceiling still refuses every NEW claim (each caller returns
            # None after this call). What it must not do is pull a subject out
            # of the review lane while the verifier whose claim WAS the last
            # allowed attempt is running: record_verification only accepts a
            # verdict from ``review``, so blocking here strands a valid PASS or
            # FAIL. Once that verifier ends -- verdict or not -- this exemption
            # no longer matches and the next ceiling check blocks as before.
            _emit_once_this_phase(conn, tid, "objective_attempt_ceiling_verdict_pending", {
                "objective": root, "attempts": attempts, "limit": limit,
                "verifier_task": in_flight[0], "verifier_run": in_flight[1],
            })
            continue
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind=?, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, last_failure_error=? "
            "WHERE id=?", (BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED, reason, tid),
        )
        _append_event(conn, tid, "objective_attempt_ceiling_reached", {
            "objective": root, "attempts": attempts, "limit": limit,
            "failure_basis": failure_basis,
        })
        # Record the phase being left, as every other block does. Without it
        # ``_resume_status_from_events`` resumes a subject blocked out of
        # ``review`` (hand-off spent the last attempt, so the verifier spawn
        # hit the ceiling) to ``ready``: after a grant the dispatcher claimed
        # a redundant implementation run, which re-requested review and hit
        # the ceiling again before any verifier ran (t_6cc051f2, 2026-10-13).
        _append_event(conn, tid, "blocked", {
            "reason": reason,
            "kind": BLOCK_KIND_ATTEMPT_BUDGET_EXHAUSTED,
            "source": "objective_attempt_ceiling",
            "source_status": row["status"],
        })


def _runtime_cap_ladder_ceiling(conn: sqlite3.Connection, task_id: str) -> int:
    """Highest rung this card may reach before a human must look at it.

    Gauntlet verifier children stop one rung early — see
    ``RUNTIME_CAP_LADDER_VERIFIER_CEILING``.
    """
    row = conn.execute(
        "SELECT executor_lane FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    lane = (row["executor_lane"] if row is not None else None) or ""
    if lane in INDEPENDENT_VERIFIER_LANES:
        return RUNTIME_CAP_LADDER_VERIFIER_CEILING
    return RUNTIME_CAP_LADDER[-1]


def runtime_cap_timeouts_at(
    conn: sqlite3.Connection, task_id: str, cap: int
) -> int:
    """How many times this card has timed out *at this exact cap*.

    Counted from the ``timed_out`` events' own ``limit_seconds``, so a card
    that has already been promoted starts the next rung at zero rather than
    inheriting the previous rung's tally. That is what makes each rung an
    independent two-strikes test instead of a lifetime budget.
    """
    total = 0
    for row in conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'timed_out'",
        (task_id,),
    ):
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if int(payload.get("limit_seconds") or 0) == int(cap):
            total += 1
    return total


def _runtime_cap_ladder_step(
    conn: sqlite3.Connection, task_id: str, *, cap: int, run_id=None
) -> None:
    """Advance one rung, or stop the card, after a ``timed_out``.

    Called after the timeout has been recorded and the card re-queued, so the
    status written here is the one that survives the tick.

    Deliberately a no-op for any card whose cap is not itself a rung: a
    bespoke cap (7200 for a long migration, say) was set by someone who knew
    what they were doing, and inferring a ladder position for it would silently
    overwrite that judgement.
    """
    cap = int(cap or 0)
    if cap not in RUNTIME_CAP_LADDER:
        return
    if runtime_cap_timeouts_at(conn, task_id, cap) < RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG:
        return

    ceiling = _runtime_cap_ladder_ceiling(conn, task_id)
    if cap >= ceiling:
        reason = (
            f"RUNTIME_CAP_EXHAUSTED: timed out {RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG}x "
            f"at {cap}s, the highest cap granted without review. This is NOT a "
            f"failure verdict -- each attempt needs more wall-clock than "
            f"automation will grant unsupervised, and continuing requires an "
            f"operator decision."
        )
        with write_txn(conn):
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None or row["status"] in (
                "done", "archived", "failed", "cancelled"
            ):
                return
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind=?, "
                "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
                "last_failure_error=? WHERE id=?",
                (BLOCK_KIND_RUNTIME_CAP_EXHAUSTED, reason, task_id),
            )
            _append_event(conn, task_id, "runtime_cap_ladder_exhausted", {
                "cap_seconds": cap,
                "ceiling_seconds": ceiling,
                "timeouts_at_cap": RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG,
            }, run_id=run_id)
            _append_event(conn, task_id, "blocked", {
                "reason": reason,
                "kind": BLOCK_KIND_RUNTIME_CAP_EXHAUSTED,
                "source": "runtime_cap_ladder",
            }, run_id=run_id)
        return

    nxt = min(
        (r for r in RUNTIME_CAP_LADDER if r > cap),
        default=None,
    )
    if nxt is None:
        return
    nxt = min(nxt, ceiling)
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? "
            "WHERE id = ? AND max_runtime_seconds = ?",
            (nxt, task_id, cap),
        )
        if cur.rowcount:
            _append_event(conn, task_id, "runtime_cap_raised", {
                "max_runtime_seconds": nxt,
                "previous": cap,
                "ceiling_seconds": ceiling,
                "timeouts_at_previous": RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG,
                "source": "runtime_cap_ladder",
            }, run_id=run_id)


RUNTIME_CAP_APPROVAL_EVENT = "runtime_cap_raised"
RUNTIME_CAP_APPROVAL_SOURCE_LADDER = "runtime_cap_ladder"
RUNTIME_CAP_APPROVAL_SOURCE_HUMAN = "human_approval"
RUNTIME_CAP_APPROVAL_AUTHORIZATION_SOURCE = "kanban_db.approve_runtime_cap"


class RuntimeCapApprovalRefused(PermissionError):
    """A runtime-cap approval was refused; nothing was recorded."""


def _valid_ladder_raise(
    conn: sqlite3.Connection, task_id: str, event_id: int, payload: dict,
    approved: Optional[int],
) -> bool:
    """Whether ``payload`` is a raise the ladder itself would have written here.

    One rung up, within the card's ceiling, after
    ``RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG`` ``timed_out`` events at the rung
    it left, and from either the baseline rung or a cap the card already held
    with approval -- so a card created at 600 cannot ladder itself to 900.
    """
    try:
        previous = int(payload.get("previous"))
        granted = int(payload.get("max_runtime_seconds"))
    except (TypeError, ValueError):
        return False
    if previous not in RUNTIME_CAP_LADDER or granted not in RUNTIME_CAP_LADDER:
        return False
    if RUNTIME_CAP_LADDER.index(granted) != RUNTIME_CAP_LADDER.index(previous) + 1:
        return False
    if granted > _runtime_cap_ladder_ceiling(conn, task_id):
        return False
    if previous != RUNTIME_CAP_LADDER[0] and previous != approved:
        return False
    timeouts = 0
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'timed_out' AND id < ?",
        (task_id, event_id),
    ):
        try:
            limit = int(json.loads(row["payload"] or "{}").get("limit_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if limit == previous:
            timeouts += 1
    return timeouts >= RUNTIME_CAP_LADDER_TIMEOUTS_PER_RUNG


_AUTOMATION_ROLE_WORDS = (
    "worker", "lead", "agent", "manager", "bot", "automation", "dispatcher",
    "supervisor", "controller", "verifier", "swarm", "daemon", "service",
    "executor", "lane", "auditor", "atlas", "system",
)
_AUTOMATION_ROLE_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:" + "|".join(_AUTOMATION_ROLE_WORDS) + r")(?:[^a-z0-9]|$)"
)


def _automation_style_identity(name: str) -> bool:
    """Whether ``name`` is an automation identity, filesystem or not."""
    lowered = str(name or "").strip().lower()
    if not lowered:
        return True
    if _automation_identity(lowered):
        return True
    return bool(_AUTOMATION_ROLE_RE.search(lowered))


def _valid_human_approval(payload: dict) -> bool:
    """Whether ``payload`` is in the format only ``approve_runtime_cap`` writes."""
    if payload.get("source") != RUNTIME_CAP_APPROVAL_SOURCE_HUMAN:
        return False
    if payload.get("authorization_source") != RUNTIME_CAP_APPROVAL_AUTHORIZATION_SOURCE:
        return False
    if payload.get("actor_kind") != ACTOR_KIND_HUMAN_INTERACTIVE:
        return False
    if not str(payload.get("reason") or "").strip():
        return False
    for key in ("approved_by", "actor_id"):
        if _automation_style_identity(payload.get(key)):
            return False
    return True


def approved_runtime_cap(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """The card's cap if it is an approved override, else ``None``.

    Replays the card's ``runtime_cap_raised`` history in order. A valid ladder
    step or a valid human approval sets the approved cap; any other record --
    an unattributed raise, a retired-format or self-asserted "human" record, a
    ladder record without its timeouts -- clears it. The card's current cap
    counts only when it equals the approved cap, so a cap set at creation, by a
    worker's ``kanban_create`` call, or changed after its approval is not
    approved.
    """
    row = conn.execute(
        "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None or row["max_runtime_seconds"] is None:
        return None
    cap = int(row["max_runtime_seconds"])
    approved: Optional[int] = None
    for event in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
        (task_id, RUNTIME_CAP_APPROVAL_EVENT),
    ):
        try:
            payload = json.loads(event["payload"] or "{}")
            granted = int(payload.get("max_runtime_seconds"))
        except (TypeError, ValueError, AttributeError):
            approved = None
            continue
        if granted <= 0:
            approved = None
        elif payload.get("source") == RUNTIME_CAP_APPROVAL_SOURCE_LADDER:
            valid = _valid_ladder_raise(conn, task_id, int(event["id"]), payload, approved)
            approved = granted if valid else None
        elif _valid_human_approval(payload):
            approved = granted
        else:
            approved = None
    return cap if approved is not None and approved == cap else None


def _running_worker_context(conn: sqlite3.Connection) -> Optional[str]:
    """Why this caller is inside a dispatcher-spawned worker, or ``None``.

    ``_governed_grant_context`` sees env markers and supervised executions. A
    profile worker is not a supervised execution, and its terminal tool can drop
    the env markers, so also refuse any descendant (or process-group member) of
    a running card's worker process.
    """
    ancestry = set(_process_ancestry())
    try:
        own_pgid = os.getpgid(0)
    except OSError:
        own_pgid = None
    for row in conn.execute(
        "SELECT id, worker_pid FROM tasks WHERE status = 'running' AND worker_pid IS NOT NULL"
    ):
        pid = int(row["worker_pid"])
        if pid in ancestry:
            return f"caller descends from running kanban worker for {row['id']}"
        try:
            if own_pgid is not None and os.getpgid(pid) == own_pgid:
                return f"caller shares the process group of the worker for {row['id']}"
        except OSError:
            pass
    return None


def approve_runtime_cap(
    conn: sqlite3.Connection,
    task_id: str,
    seconds: int,
    *,
    approved_by: str,
    reason: str,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[int] = None,
) -> dict:
    """Record one human operator's approval of a per-card runtime cap.

    The only path besides the ladder that lets a card run longer than
    ``execution.max_runtime_seconds``. Fails closed, writing nothing: refused
    inside any board run or supervised execution (env markers, process
    ancestry, running worker processes), without explicit operator provenance
    (``HERMES_ACTOR_KIND=human_interactive`` and a named ``HERMES_ACTOR_ID``),
    for automation identities, and without an approver, reason or positive cap.
    Mirrors ``grant_objective_attempts``. Not exposed to worker tools.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    approved_by = str(approved_by or "").strip()
    reason = str(reason or "").strip()
    if not approved_by or not reason:
        raise RuntimeCapApprovalRefused("an approval requires approved_by and a reason")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise RuntimeCapApprovalRefused("an approval requires a positive integer cap")
    context = _governed_grant_context(conn, environ) or _running_worker_context(conn)
    if context is not None:
        raise RuntimeCapApprovalRefused(f"automation cannot approve a runtime cap: {context}")
    declared = (environ.get(ENV_ACTOR_KIND) or "").strip()
    actor_id = (environ.get(ENV_ACTOR_ID) or "").strip()
    if declared != ACTOR_KIND_HUMAN_INTERACTIVE or not actor_id:
        raise RuntimeCapApprovalRefused(
            f"an approval requires explicit operator provenance: {ENV_ACTOR_KIND}="
            f"{ACTOR_KIND_HUMAN_INTERACTIVE} and a named {ENV_ACTOR_ID}"
        )
    for name in (actor_id, approved_by):
        if _automation_style_identity(name):
            raise RuntimeCapApprovalRefused(
                f"{name!r} is an automation identity and cannot approve a runtime cap"
            )
    now_ts = int(time.time()) if now is None else int(now)
    with write_txn(conn):
        row = conn.execute(
            "SELECT max_runtime_seconds FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise RuntimeCapApprovalRefused(f"task {task_id} not found")
        payload = {
            "max_runtime_seconds": seconds,
            "previous": row["max_runtime_seconds"],
            "source": RUNTIME_CAP_APPROVAL_SOURCE_HUMAN,
            "authorization_source": RUNTIME_CAP_APPROVAL_AUTHORIZATION_SOURCE,
            "approved_by": approved_by,
            "actor_kind": declared,
            "actor_id": actor_id,
            "reason": reason,
            "approved_at": now_ts,
        }
        conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?",
            (seconds, task_id),
        )
        _append_event(conn, task_id, RUNTIME_CAP_APPROVAL_EVENT, payload)
    return payload


def gauntlet_canonical_ref() -> str:
    try:
        from hermes_cli.config import load_config_readonly
        raw = _kanban_cfg(load_config_readonly).get(
            "gauntlet_canonical_ref", "origin/main"
        )
        return str(raw).strip() or "origin/main"
    except Exception:
        return "origin/main"


def _gauntlet_worktree_integrated(conn: sqlite3.Connection, task_id: str) -> tuple[bool, str]:
    """Prove a Gauntlet worktree's commits are integrated in the canonical ref.

    Direct ancestry passes.  Cherry-picks also pass when ``git cherry`` marks
    every worktree-only commit as patch-equivalent (``-``). Any ``+`` is an
    unintegrated commit and fails closed.
    """
    row = conn.execute(
        "SELECT workspace_kind, workspace_path FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    if row is None or row["workspace_kind"] != "worktree":
        return True, "not a worktree task"
    path = row["workspace_path"]
    if not path:
        return False, "worktree task has no workspace_path"
    ref = gauntlet_canonical_ref()
    try:
        anc = subprocess.run(
            ["git", "-C", str(path), "merge-base", "--is-ancestor", "HEAD", ref],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if anc.returncode == 0:
            return True, f"HEAD is ancestor of {ref}"
        cherry = subprocess.run(
            ["git", "-C", str(path), "cherry", ref, "HEAD"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if cherry.returncode != 0:
            return False, (cherry.stderr or cherry.stdout or "git cherry failed").strip()[:500]
        lines = [ln.strip() for ln in cherry.stdout.splitlines() if ln.strip()]
        if all(ln.startswith("-") for ln in lines):
            return True, f"all worktree-only commits are patch-equivalent on {ref}"
        missing = [ln for ln in lines if ln.startswith("+")]
        return False, f"unintegrated commits relative to {ref}: " + ", ".join(missing[:5])
    except Exception as exc:
        return False, f"canonical integration probe failed: {type(exc).__name__}: {exc}"


def unsatisfied_parents(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """``(parent_id, status)`` for every direct parent :func:`_parents_satisfied`
    still counts as open (``done`` / ``archived`` release the child), in id
    order, so a refusal or a board view can name the blockers instead of the
    caller guessing. Read-only."""
    rows = conn.execute(
        "SELECT p.id, p.status FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') "
        "ORDER BY p.id", (task_id,),
    ).fetchall()
    return [(row["id"], row["status"]) for row in rows]


def _claim_and_open_run(
    conn: sqlite3.Connection, task_id: str, source_status: str, lock: str, expires: int, now: int,
    *, event_extra: Optional[dict] = None,
) -> Optional[int]:
    """CAS ``source_status -> running``, open a run row, emit ``claimed``; None
    when the CAS lost. Caller holds the txn."""
    cur = conn.execute(
        f"""
        UPDATE tasks
           SET status        = 'running',
               claim_lock    = ?,
               claim_expires = ?,
               started_at    = COALESCE(started_at, ?)
         WHERE id = ?
           AND status = '{source_status}'
           AND claim_lock IS NULL
        """,
        (lock, expires, now, task_id),
    )
    if cur.rowcount != 1:
        return None
    trow = conn.execute(
        "SELECT assignee, max_runtime_seconds, current_step_key "
        "FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    run_cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key, status,
            claim_lock, claim_expires, max_runtime_seconds,
            started_at
        ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
        """,
        (
            task_id, trow["assignee"] if trow else None, trow["current_step_key"] if trow else None,
            lock, expires, trow["max_runtime_seconds"] if trow else None, now,
        ),
    )
    run_id = run_cur.lastrowid
    conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    _append_event(
        conn, task_id, "claimed",
        {"lock": lock, "expires": expires, "run_id": run_id, **(event_extra or {})}, run_id=run_id,
    )
    return run_id


def claim_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        # A rollback that has not reconciled yet is unresolved ownership. Never
        # start a replacement beside it. Conflict is also fail-closed: the
        # dispatcher routes that task to needs_input during reconciliation.
        try:
            from hermes_cli.execution_effects import unresolved_effect_count
            if unresolved_effect_count(conn, task_id=task_id):
                _append_event(
                    conn, task_id, "execution_refused",
                    {"reason": "unresolved_execution_effects"},
                )
                return None
        except sqlite3.OperationalError as exc:
            if "execution_effects" not in str(exc):
                raise
        # Board policy may temporarily make this unproved non-security counter
        # advisory. Accounting still advances and the repeated-error brake below
        # remains binding.
        reached, _attempts, _limit, _root = _objective_attempt_ceiling_reached(conn, task_id)
        if reached and objective_attempt_budget_enforced():
            _block_objective_attempt_ceiling(conn, task_id)
            return None
        # Same error over and over is a wrong fix, not progress. Checked
        # alongside the attempt budget so both brakes share every call site
        # and neither can quietly become an uncalled stage.
        if repeated_error_ceiling_reached(conn, task_id)[0]:
            _block_repeated_error(conn, task_id)
            return None
        loop_ok, loop_reason = validation_loop_entry_valid(conn, task_id)
        if not loop_ok:
            _append_event(conn, task_id, "execution_refused",
                          {"reason": "validation_loop_missing", "detail": loop_reason,
                           "source": "claim_task"})
            return None
        # Company isolation (rule 3): the claim is the last door before a
        # worker runs under this profile, and the one every surface — the
        # dispatcher, a terminal lane, manual SQL that flipped status — has
        # to pass through. Fail closed here even if an upstream check was
        # skipped.
        lane_row = conn.execute(
            "SELECT assignee, tenant FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if lane_row is not None:
            conflict = tenant_claim_conflict(lane_row["tenant"], lane_row["assignee"])
            if conflict is not None:
                _append_event(
                    conn, task_id, "execution_refused",
                    {**conflict, "source": "claim_task"},
                )
                return None
        # Single enforcement point: never ready -> running with an unsatisfied
        # parent, whichever writer set 'ready'. Demote to 'todo';
        # recompute_ready re-promotes when the parents are satisfied. Reads the
        # same predicate recompute_ready promotes on: if the two disagreed, a
        # verifier child freed by the evidence-ready carve-out would be
        # promoted and then demoted straight back on every dispatch pass.
        # See RCA at kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        if not _parents_satisfied(conn, task_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'", (task_id,),
            )
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        # Double-dispatch guard (kanban.db corruption RCA, 2026-10-25): never
        # start a replacement run beside a prior worker whose PID is still
        # alive. Two live workers held claims on one task for ~2.5 min before
        # the stale one was reaped; refuse the claim until that PID is dead.
        for prior in conn.execute(
            "SELECT id, worker_pid FROM task_runs "
            "WHERE task_id = ? AND ended_at IS NULL AND worker_pid IS NOT NULL",
            (task_id,),
        ).fetchall():
            prior_pid = int(prior["worker_pid"])
            if prior_pid != os.getpid() and _pid_alive(prior_pid):
                _append_event(
                    conn, task_id, "execution_refused",
                    {"reason": "prior_worker_pid_alive",
                     "run_id": int(prior["id"]), "worker_pid": prior_pid,
                     "source": "claim_task"},
                )
                return None
        # Close a leaked prior run so the CAS below doesn't strand it.
        _reclaim_dangling_run(
            conn, task_id, statuses=("ready",), now=now, note="invariant recovery on re-claim",
        )
        # Redispatch guard. A card whose objective was satisfied elsewhere has
        # nothing left to do, ever. Testing it HERE (not only in the
        # dispatcher's SELECT) is what makes the guarantee mechanical: even if
        # some other writer flips status back to 'ready', the claim still
        # cannot succeed. 'completed' is deliberately NOT a bar — done is
        # reversible and a reopened card must be runnable again. Checked
        # inside the txn immediately before the CAS, so the shared
        # ``_claim_and_open_run`` helper never claims a disposed card.
        live = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND status = 'ready' "
            "AND claim_lock IS NULL AND " + _not_irreversibly_disposed_sql(),
            (task_id,),
        ).fetchone()
        if live is None:
            return None
        run_id = _claim_and_open_run(conn, task_id, "ready", lock, expires, now)
        if run_id is None:
            return None
        # A new implementation run invalidates any prior verdict — it covered
        # the previous run's work, not this one. Ledger rows are kept.
        _clear_verification_state(conn, task_id)
        try:
            from hermes_cli.execution_effects import adopt_held_effects
            adopted_effects = adopt_held_effects(
                conn, task_id=task_id, new_run_id=int(run_id), commit=False
            )
            if adopted_effects:
                _append_event(
                    conn, task_id, "execution_effects_adopted",
                    {"run_id": int(run_id), "count": int(adopted_effects)},
                    run_id=int(run_id),
                )
        except sqlite3.OperationalError as exc:
            if "execution_effects" not in str(exc):
                raise
        claimed = get_task(conn, task_id)
    _fire_task_hook("kanban_task_claimed", claimed, task_id, run_id)
    # Production ledger projection (post-commit, best-effort). See
    # _sync_production_ledger docstring — this closes the gap identified in
    # t_da07457d's FAIL verdict on t_faf6e26a: only create/complete/block
    # were projected before, so a card claimed (ready -> running) could show
    # stale 'Open' in Postgres while Kanban had already moved it.
    _sync_production_ledger(conn, task_id, status="running", phase="claim", run_id=run_id)
    return claimed


def claim_review_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomic ``review -> running`` (None when lost). Parents are re-checked
    (one may have reopened meanwhile) and a NEW run tracks the reviewer
    separately from the implementer."""
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        try:
            from hermes_cli.execution_effects import unresolved_effect_count
            if unresolved_effect_count(conn, task_id=task_id):
                _append_event(
                    conn, task_id, "execution_refused",
                    {"reason": "unresolved_execution_effects", "source": "claim_review_task"},
                )
                return None
        except sqlite3.OperationalError as exc:
            if "execution_effects" not in str(exc):
                raise
        # Board-scoped mode is shared with ordinary worker admission.
        reached, _attempts, _limit, _root = _objective_attempt_ceiling_reached(conn, task_id)
        if reached and objective_attempt_budget_enforced():
            _block_objective_attempt_ceiling(conn, task_id)
            return None
        # Same error over and over is a wrong fix, not progress. Checked
        # alongside the attempt budget so both brakes share every call site
        # and neither can quietly become an uncalled stage.
        if repeated_error_ceiling_reached(conn, task_id)[0]:
            _block_repeated_error(conn, task_id)
            return None
        loop_ok, loop_reason = validation_loop_entry_valid(conn, task_id)
        if not loop_ok:
            _append_event(conn, task_id, "execution_refused",
                          {"reason": "validation_loop_missing", "detail": loop_reason,
                           "source": "claim_review_task"})
            return None
        # Company isolation (rule 3): a review claim is a claim. The reviewer
        # profile must be in the task's lane or in the shared lane.
        lane_row = conn.execute(
            "SELECT assignee, tenant FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if lane_row is not None:
            conflict = tenant_claim_conflict(lane_row["tenant"], lane_row["assignee"])
            if conflict is not None:
                _append_event(
                    conn, task_id, "execution_refused",
                    {**conflict, "source": "claim_review_task"},
                )
                return None
        if not _parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL", (task_id,),
            )
            if demoted.rowcount == 1:
                _append_event(
                    conn, task_id, "dependency_wait",
                    {"reason": "parent_reopened", "source_status": "review"},
                )
            return None
        # Verifier independence, enforced at SELECTION rather than only at
        # verdict time — and, for a Gauntlet-enforced subject, BEFORE the
        # same-identity claim is attempted rather than after it has been
        # refused once. See ``_review_claim_conflict``: it answers with the
        # verdict gate's own predicate, so the review dispatcher can no longer
        # re-select an identity the verdict gate is certain to refuse
        # (t_00690780 runs 1624/1625; t_cbc133db 2026-09-03 22:50). Refusing
        # the claim is what breaks that loop and leaves the task parked in
        # ``review`` for the independent ``codex_verify`` child to pick up.
        #
        # Refusing is necessary and was not sufficient. ``request_review`` opens
        # the independent route at the handoff, but a subject can arrive in
        # ``review`` WITHOUT a fresh handoff — unblocked back into review by an
        # operator or a recovery path, or parked by a ``request_review`` that ran
        # before the routing repair existed. Those subjects were refused here on
        # every dispatcher tick with nothing that could ever verify them: the
        # live t_cbc133db shape, refused at 23:31:20 and then silent. So the
        # refusal now also OPENS the route it demands, post-commit and
        # idempotently, exactly as the handoff does.
        refused: Optional[tuple[str, str]] = None
        claim_row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ? AND status = 'review'",
            (task_id,),
        ).fetchone()
        if claim_row is not None and claim_row["assignee"]:
            conflict = _review_claim_conflict(
                conn, task_id, claim_row["assignee"]
            )
            if conflict is not None:
                candidate, conflict_source = conflict
                _emit_once_this_phase(
                    conn, task_id, "review_claim_rejected_self_review",
                    {
                        "candidate": candidate,
                        "conflict_source": conflict_source,
                        "refused_identities": sorted(
                            _self_review_refused_implementers(conn, task_id)
                        ),
                        "required_lane": EXECUTOR_LANE_CODEX_VERIFY,
                    },
                )
                refused = (candidate, conflict_source)
        if refused is None:
            # Redispatch guard; see the same predicate in claim_task. Checked
            # inside the txn immediately before the CAS, so the shared
            # ``_claim_and_open_run`` helper never claims a disposed card.
            live = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND status = 'review' "
                "AND claim_lock IS NULL AND " + _not_irreversibly_disposed_sql(),
                (task_id,),
            ).fetchone()
            if live is None:
                return None
            run_id = _claim_and_open_run(
                conn, task_id, "review", lock, expires, now, event_extra={"source_status": "review"},
            )
            if run_id is None:
                return None
            claimed_review = get_task(conn, task_id)
            _sync_production_ledger(
                conn, task_id, status="running", phase="claim_review", run_id=run_id,
            )
            return claimed_review
    # Only a refusal reaches here; every other path returned inside the
    # transaction above. The refusal is already durable, so opening the route
    # is post-commit and best-effort for the same reason ``request_review``
    # places it there: a fault while creating the verifier must not undo a
    # correctly recorded refusal.
    #
    # Gauntlet-scoped: ``_review_claim_conflict`` also answers "refused" for an
    # unenforced card, and the ``codex_verify`` child is a Gauntlet artifact.
    if gauntlet_required(conn, task_id):
        conflict_identity, conflict_source = refused
        implementer = _review_requested_implementer(conn, task_id)
        child_id = _ensure_independent_verifier_child(
            conn, task_id, implementer=implementer,
        )
        with write_txn(conn):
            _emit_once_this_phase(
                conn, task_id, "independent_verification_required",
                {
                    "required_lane": EXECUTOR_LANE_CODEX_VERIFY,
                    "conflict_identity": conflict_identity,
                    "conflict_source": conflict_source,
                    "verifier_task": child_id,
                    "source": "claim_review_task",
                },
            )
        # Promote the child off the subject's evidence packet on this tick,
        # not the next one.
        recompute_ready(conn)
    return None


def defer_rate_limited_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    expected_run_id: Optional[int] = None,
    execution_id: Optional[str] = None,
    reset_at: Optional[int] = None,
) -> bool:
    """Neutral-return a live task whose provider capacity is exhausted.

    ``reset_at`` (epoch seconds) is the provider's own "try again at" time
    when it gave one. It is stored as a hard ``quota_hold`` on the run so
    :func:`check_respawn_guard` waits for the reset instead of probing on the
    fixed cooldown (2026-10-15: two Codex verifier cards probed every 5m20s
    for 90+ minutes against a wall that said when it would lift).

    This is the in-process counterpart of the worker EX_TEMPFAIL reap path:
    preserve failure counters, close the active run as ``rate_limited``,
    release the claim back to its source phase, and stamp the quota reason so
    the respawn guard can cool the task down instead of burning retries.
    """
    text = (reason or "provider capacity exhausted").strip()[:500]
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] is None:
            return False
        run_id = int(row["current_run_id"])
        if expected_run_id is not None and run_id != int(expected_run_id):
            return False
        retry_status = _retry_status_for_run(conn, task_id, run_id)
        meta = {"retry_status": retry_status, "provider_capacity": True}
        if execution_id:
            meta["execution_id"] = execution_id
        if isinstance(reset_at, int) and not isinstance(reset_at, bool) and reset_at > 0:
            route = conn.execute(
                "SELECT assignee, provider_override, model_override FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            meta["quota_hold"] = {
                "kind": "hard",
                "reset_at": int(reset_at),
                "provider": "codex",
                "model": None,
                "route": {
                    "assignee": route["assignee"],
                    "provider_override": route["provider_override"],
                    "model_override": route["model_override"],
                },
            }
        conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, last_failure_error = ? WHERE id = ? AND status = 'running'",
            (retry_status, text, task_id),
        )
        _end_run(
            conn, task_id, outcome="rate_limited", status="rate_limited",
            error=text, metadata=meta,
        )
        _append_event(
            conn, task_id, "rate_limited",
            {"reason": text, **meta}, run_id=run_id,
        )
        return True


def _retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` — one place, so crash/timeout/reclaim can't silently turn a
    reviewer run into an implementation run."""
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _latest_event(conn, task_id, "claimed", run_id)
    payload = _json_dict(_row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"


def verifier_execution_crash_retry(
    conn: sqlite3.Connection, task_id: str, *, expected_run_id: int,
    reason: str, infrastructure: bool,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Bounded auto-retry for an independent verifier whose own execution tool
    crashed -- no verdict, no FAIL, the executor itself failed to run.

    Returns ``True`` when the card was returned to ``ready``/``review`` to try
    again (the caller must NOT also call :func:`block_task`), ``False`` when
    the retry budget (:data:`VERIFIER_EXECUTION_CRASH_RETRY_LIMIT`) is already
    exhausted, this run is no longer the card's live run (reclaimed/cancelled
    out from under it), or the row vanished -- in every ``False`` case the
    caller falls back to its own existing ``block_task`` call, unchanged.

    ``infrastructure=True`` (control plane ended the run -- runtime cap,
    orphan-policy kill, operator terminate) mirrors
    :func:`kanban_db_dispatch._record_task_failure`'s own carve-out: it never
    counts against the retry budget and always retries, because the verifier
    never actually got a chance to run.

    Deliberately independent of ``_record_task_failure``'s trip machinery,
    which opens an auto-repair CARD on exhaustion: a verifier is itself
    Gauntlet work, not a subject for auto-repair. This only inserts bounded
    retries in front of the existing "blocked, needs operator" terminal
    state -- it never changes what blocking means or who can unblock it, and
    it never touches verdict (PASS/FAIL/BLOCKER) handling at all, which lives
    entirely on the ``attempt.ok`` branch this function is never called from.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, current_run_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "running"
            or row["current_run_id"] != int(expected_run_id)
        ):
            # Not this run's card to retry any more -- let the caller's own
            # block_task do its normal (likely no-op/refused) handling.
            return False
        if infrastructure:
            failures = int(row["consecutive_failures"])
        else:
            failures = int(row["consecutive_failures"]) + 1
        if not infrastructure and failures >= VERIFIER_EXECUTION_CRASH_RETRY_LIMIT:
            # Budget exhausted -- persist the real count so the ledger/event
            # the caller's block_task writes next reflects it, then refuse.
            conn.execute(
                "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
                (failures, task_id),
            )
            return False
        retry_status = _retry_status_for_run(conn, task_id, row["current_run_id"])
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, consecutive_failures = ?, "
            "last_failure_error = ? "
            "WHERE id = ? AND status = 'running' AND current_run_id = ?",
            (retry_status, failures, reason[:500], task_id, int(expected_run_id)),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id, outcome="crashed", status="crashed", error=reason[:500],
            metadata={
                "failures": failures,
                "limit": VERIFIER_EXECUTION_CRASH_RETRY_LIMIT,
                "retry_status": retry_status,
                "infrastructure": infrastructure,
            },
        )
        payload = {
            "failures": failures,
            "limit": VERIFIER_EXECUTION_CRASH_RETRY_LIMIT,
            "retry_status": retry_status,
            "error": reason[:500],
            "infrastructure": infrastructure,
        }
        if event_payload_extra:
            payload.update(event_payload_extra)
        _append_event(conn, task_id, "verifier_execution_crash_retried", payload, run_id=run_id)
    return True


_RUN_OUTCOME_TERMINAL_STATUS = {
    "completed": "done",
    "review_requested": "review",
    "changes_requested": "changes_requested",
    "blocked": "blocked",
    "dependency_wait": "blocked",
}


def goal_run_status(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Lifecycle status as seen by ONE run: terminal handoffs bind to that run,
    any other ownership loss is ``superseded`` — otherwise an old goal loop
    would read the successor's live ``running`` and mutate it."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    if expected_run_id is not None:
        row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (int(expected_run_id), task_id),
        ).fetchone()
        outcome = str(row["outcome"]) if row and row["outcome"] is not None else None
        terminal_status = _RUN_OUTCOME_TERMINAL_STATUS.get(outcome)
        if terminal_status is not None:
            return terminal_status
        if outcome is not None or task.current_run_id != int(expected_run_id):
            return "superseded"
    if task.status in {"ready", "todo"}:
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if event and event["kind"] == "changes_requested":
            return "changes_requested"
    return task.status


def heartbeat_claim(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim; True if we still own it."""
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?", (expires, task_id, lock),
        )
        if cur.rowcount != 1:
            return False
        _extend_run_claim(conn, task_id, expires)
        return True


def _extend_run_claim(conn: sqlite3.Connection, task_id: str, expires: int) -> Optional[int]:
    """Mirror a task claim extension onto its active run row; returns that run id."""
    run_id = _current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (expires, run_id))
    return run_id


def release_stale_claims(
    conn: sqlite3.Connection, *, signal_fn=None, failure_limit: Optional[int] = None,
) -> int:
    """Reclaim ``running`` tasks whose claim expired; returns the count reclaimed.

    Every reclaim that actually releases a claim is a non-success attempt and
    is booked through ``_record_task_failure`` (#111306): a claim that expired
    without a worker ever spawning otherwise loops claim -> reclaim -> claim
    with ``consecutive_failures`` stuck at 0, so the breaker never trips.
    ``reclaim_task`` (operator path) deliberately resets the counter instead.

    A host-local worker that is still alive gets its claim *extended* instead
    (a slow model can sit longer than the TTL inside one tool-free call, so no
    heartbeat) — unless ``last_heartbeat_at`` is older than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (wedged; ``_touch_activity``
    keeps any genuinely active worker fresh). Safe to call often.

    Reclaiming a live worker mid-flight produces the spawn- then-immediately-reclaim loop seen on slow
    models that spend longer than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call (#23025):
    no tool calls means no ``kanban_heartbeat``, even though the subprocess is healthy.
    Backstop (#29747 gap 3): if the worker's PID is still alive but its ``last_heartbeat_at`` is stale by
    more than ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has been making no observable
    progress and we reclaim anyway — even if ``_pid_alive`` is still true. This catches the
    wedged-in-a-logic-loop case where the process is technically running but accomplishing nothing.
    ``_touch_activity`` (run_agent.py) bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
    so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
    ``enforce_max_runtime`` and ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = _host_prefix()
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, worker_started_at, claim_expires, last_heartbeat_at, "
        "       assignee "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?", (now,),
    ).fetchall()
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill
    for row in stale:
        if owner_reclaim_blocked(conn, row["id"]):
            continue
        host_local = (row["claim_lock"] or "").startswith(host_prefix)
        hb = _to_epoch(row["last_heartbeat_at"])
        # Backstop: a heartbeat older than the max-stale threshold means no
        # observable progress — reclaim even if the PID is alive (logic loop).
        heartbeat_stale = hb is not None and (now - hb) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        started_at = _row_get(row, "worker_started_at")
        if (host_local and row["worker_pid"] and _worker_alive(row["worker_pid"], started_at)
                and not heartbeat_stale):
            _extend_live_stale_claim(conn, row, now)
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn, started_at=started_at,
        )
        # A live worker of ours must keep its claim (else a duplicate spawns beside it).
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        with write_txn(conn):
            # Again under the write lock: an interpreter that bound since the check above
            # changes neither claim_lock nor worker_pid, so the CAS below cannot see it.
            if owner_reclaim_blocked_after_kill(conn, row["id"], signalled=bool(termination.get("termination_attempted"))):
                continue
            retry_status = _retry_status_for_run(conn, row["id"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ? "
                # A worker that registered its own pid since the SELECT keeps its claim.
                "AND worker_pid IS ?",
                (retry_status, row["id"], row["claim_lock"], now, row["worker_pid"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _record_reclaim(
                conn, row["id"], termination,
                error=f"stale_lock={row['claim_lock']}",
                payload={
                    "stale_lock": row["claim_lock"],
                    "worker_pid": _opt_int(row["worker_pid"]),
                    "claim_expires": _to_epoch(row["claim_expires"]),
                    "last_heartbeat_at": hb,
                    "now": now,
                    "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                    "retry_status": retry_status,
                },
            )
            reclaimed += 1
        # Own txn, after the reclaim commit (same shape as ``enforce_max_runtime``):
        # the run ended without a verdict, so it counts toward the breaker and a
        # trip flips the task to ``blocked`` + ``gave_up`` on top of ``reclaimed``.
        _record_task_failure(
            conn, row["id"], f"stale_lock={row['claim_lock']}",
            outcome="reclaimed", failure_limit=failure_limit,
            release_claim=False, end_run=False,
            event_payload_extra={"worker_pid": _opt_int(row["worker_pid"]), "retry_status": retry_status},
        )
        # Post-commit observer; every non-reclaim branch ``continue``d above.
        if _kanban_observer_consumed("on_kanban_worker_stale_claim"):
            _fire_kanban_lifecycle_hook(
                "on_kanban_worker_stale_claim", row["id"], board=get_current_board(),
                assignee=row["assignee"], run_id=run_id, worker_pid=_opt_int(row["worker_pid"]),
                heartbeat_stale=bool(heartbeat_stale), retry_status=retry_status,
            )
    return reclaimed


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    run_id = _end_run(
        conn, task_id, outcome="reclaimed", status="reclaimed", error=error, metadata=termination,
    )
    payload.update(termination)
    _append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id


def _extend_live_stale_claim(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """TTL-expired claim whose host-local worker is alive: extend instead of
    reclaiming (``claim_extended`` event). CAS on the same expired lock so a
    concurrent reclaimer wins cleanly."""
    new_expires = now + _resolve_claim_ttl_seconds()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "  AND claim_lock IS ? "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?", (new_expires, row["id"], row["claim_lock"], now),
        )
        if cur.rowcount != 1:
            return
        run_id = _extend_run_claim(conn, row["id"], new_expires)
        _append_event(
            conn, row["id"], "claim_extended",
            {
                "reason": "pid_alive",
                "worker_pid": int(row["worker_pid"]),
                "claim_lock": row["claim_lock"],
                "claim_expires_was": _to_epoch(row["claim_expires"]),
                "claim_expires_now": new_expires,
                "last_heartbeat_at": _to_epoch(row["last_heartbeat_at"]),
            },
            run_id=run_id,
        )


def reclaim_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None, signal_fn=None,
) -> bool:
    """Operator reclaim regardless of TTL: release the claim, restore the source
    phase, reset the failure counter. False when not running."""
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn, started_at=row["worker_started_at"])
    with write_txn(conn):
        retry_status = _retry_status_for_run(conn, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?", (retry_status, task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        _record_reclaim(
            conn, task_id, termination,
            error=f"manual_reclaim: {reason}" if reason else f"manual_reclaim lock={prev_lock}",
            payload={"manual": True, "reason": reason, "prev_lock": prev_lock, "retry_status": retry_status},
        )
    # Operator intervention = fresh retry budget (own txn, runs after commit).
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], *, reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign (None unassigns); a running task is refused unless
    ``reclaim_first`` releases its claim — the "this profile's model is broken" path."""
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom). Verified = the row
    exists AND ``created_by`` is the completing task's assignee or id, OR the
    card is linked as its child (created elsewhere, attached by the worker).
    Never mutates."""
    ordered = list(dict.fromkeys(str(x).strip() for x in (claimed_ids or []) if str(x).strip()))
    if not ordered:
        return [], []

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,)).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})", tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        trusted = created_by is not None and (
            (completing_assignee and created_by == completing_assignee)
            or created_by == completing_task_id
            or cid in linked_children
        )
        (verified if trusted else phantom).append(cid)
    return verified, phantom


_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """``t_<hex>`` references in ``text`` that don't resolve to a task (deduped; advisory)."""
    if not text:
        return []
    return _missing_task_ids(conn, dict.fromkeys(_TASK_ID_PROSE_RE.findall(text)))


class HallucinatedCardsError(ValueError):
    """``complete_task`` refused: ``created_cards`` has ids that don't exist or
    weren't created by this worker (``.phantom``). A ``ValueError`` so tool
    error handlers treat it as recoverable."""

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class VerificationRequiredError(ValueError):
    """Raised by ``complete_task`` when a Gauntlet-enforced task tries to
    reach ``done`` without a durable VERIFIED verdict.

    The task is NOT mutated: like :class:`HallucinatedCardsError` this gate
    runs before the write transaction, so the caller can fix the lifecycle
    (request review, get it verified) and retry the same completion. Kept a
    ``ValueError`` subclass so existing tool-error handlers treat it as a
    recoverable user error rather than a crash.

    ``.status`` is the task status at rejection and ``.verification_state``
    the ledger head (``None`` when no verification phase was ever entered) —
    together they tell the caller which step of the chain is missing.
    """

    def __init__(
        self,
        task_id: str,
        status: Optional[str],
        verification_state: Optional[str],
        regression_detail: Optional[str] = None,
    ):
        self.task_id = task_id
        self.status = status
        self.verification_state = verification_state
        # Set when the missing step is specifically the regression proof owed
        # for a material repair, so a caller can tell "not verified yet" apart
        # from "verified is unreachable until you re-run the checks".
        self.regression_required = regression_detail is not None
        self.regression_detail = regression_detail
        if regression_detail is not None:
            super().__init__(
                f"completion blocked: {task_id} is gauntlet-enforced and "
                f"{regression_detail} (status={status!r}, "
                f"verification_state={verification_state!r}). The required "
                f"chain is running -> review -> verified -> done, and after a "
                f"failed verification the verdict needs regression evidence."
            )
            return
        if verification_state == VERIFICATION_FAILED:
            detail = (
                "its latest verification FAILED; it must be repaired and "
                "re-verified"
            )
        elif verification_state == VERIFICATION_PENDING:
            detail = (
                "it is awaiting verification; a verifier must record a "
                "passing verdict first"
            )
        else:
            detail = (
                "no verification phase was entered; hand off with "
                "request_review, then have a verifier record the verdict"
            )
        super().__init__(
            f"completion blocked: {task_id} is gauntlet-enforced and "
            f"{detail} (status={status!r}, "
            f"verification_state={verification_state!r}). The required chain "
            f"is running -> review -> verified -> done."
        )


def _resolve_gauntlet_default(
    title: Optional[str], body: Optional[str] = None
) -> bool:
    """Per-card default: subject classification first, board config second.

    An explicit ``gauntlet=`` argument still wins over both -- this only fills
    in when the caller expressed no preference.
    """
    decided = gauntlet_default_for_subject(title, body)
    if decided is not None:
        return decided
    return gauntlet_enforcement_default()


def gauntlet_enforcement_default() -> bool:
    """Return whether every task on this board is Gauntlet-enforced.

    Per-task ``gauntlet_enforced`` is the primary switch; this config key
    (``kanban.gauntlet_enforcement``) raises the floor for the whole board
    without a backfill, and is consulted live so flipping it takes effect on
    tasks that already exist.

    Defaults to ``False``. Enforcement changes the meaning of a bare
    ``complete_task`` for every existing caller — CLI, dashboard, worker tool,
    recovery lane — so it is an operator decision, not a silent upgrade.
    """
    try:
        from hermes_cli.config import load_config
        return bool(
            _kanban_cfg(load_config).get(
                "gauntlet_enforcement", False
            )
        )
    except Exception:
        return False


def gauntlet_required(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` may only complete via a VERIFIED verdict.

    ``codex_verify`` tasks are themselves the independent verification artifact.
    Sending them through the generic review lane would recursively "verify the
    verifier" and can confuse process success with the reviewed work's verdict.
    They therefore complete with their PASS/FAIL report as evidence; the target
    task's verification ledger remains the authority on whether the target passed.
    """
    row = conn.execute(
        "SELECT gauntlet_enforced, executor_lane FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    if row["executor_lane"] in INDEPENDENT_VERIFIER_LANES:
        # The dependency edge is deliberately removed before dispatch so the
        # subject's completion state cannot gate its verifier.  The exemption
        # therefore keys on the append-only governance relation created while
        # the dependency still existed, not on the current task_links graph.
        # An arbitrary/relabelled or orphan codex card cannot mint this relation
        # because add_task_relation(RELATION_VERIFIES) requires the live edge.
        verified_subject = conn.execute(
            "SELECT 1 FROM task_relations "
            "WHERE from_task_id = ? AND relation = ? LIMIT 1",
            (task_id, RELATION_VERIFIES),
        ).fetchone()
        linked_subject = conn.execute(
            "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1", (task_id,)
        ).fetchone()
        if verified_subject is not None or linked_subject is not None:
            return False
        # No durable subject attribution means this is not a verifier artifact;
        # preserve the orphan guard regardless of the board-wide flag.
        return True
    if row["gauntlet_enforced"]:
        return True
    return gauntlet_enforcement_default()


def _record_verification_row(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    state: str,
    run_id: Optional[int] = None,
    verifier: Optional[str] = None,
    evidence: Optional[dict] = None,
    reason: Optional[str] = None,
    now: Optional[int] = None,
    kind: str = LEDGER_KIND_VERDICT,
    covers_phase_id: Optional[int] = None,
) -> int:
    """Append one row to the verification ledger and materialise its head.

    Must be called inside an open write transaction: the ledger row and the
    ``tasks.verification_state`` it materialises have to land together, or the
    completion guard could read a head with no evidence behind it.

    ``kind='regression'`` writes a regression proof instead of a verdict: it is
    appended to the same ledger and is equally durable, but it deliberately
    does NOT touch ``tasks.verification_state`` — a proof is an input to a
    verdict, never a verdict itself.
    """
    if kind not in (LEDGER_KIND_VERDICT, LEDGER_KIND_REGRESSION):
        raise ValueError(
            f"ledger kind must be {LEDGER_KIND_VERDICT!r} or "
            f"{LEDGER_KIND_REGRESSION!r}, got {kind!r}"
        )
    if kind == LEDGER_KIND_VERDICT and state not in VALID_VERIFICATION_STATES:
        raise ValueError(
            f"verification state must be one of "
            f"{sorted(VALID_VERIFICATION_STATES)}, got {state!r}"
        )
    if kind == LEDGER_KIND_REGRESSION and state != VERIFICATION_REGRESSION:
        raise ValueError(
            f"regression rows carry state {VERIFICATION_REGRESSION!r}, "
            f"got {state!r}"
        )
    ts = int(time.time()) if now is None else int(now)
    payload = None
    if evidence is not None:
        try:
            payload = json.dumps(redact_review_value(evidence))
        except (TypeError, ValueError):
            payload = json.dumps({"unserialisable": repr(evidence)[:2000]})
    cur = conn.execute(
        """
        INSERT INTO task_verifications
            (task_id, run_id, state, verifier, evidence, reason, created_at,
             kind, covers_phase_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            int(run_id) if run_id is not None else None,
            state,
            verifier,
            payload,
            (str(redact_review_value(reason)).strip() or None) if reason else None,
            ts,
            kind,
            int(covers_phase_id) if covers_phase_id is not None else None,
        ),
    )
    if kind == LEDGER_KIND_VERDICT:
        conn.execute(
            "UPDATE tasks SET verification_state = ? WHERE id = ?",
            (state, task_id),
        )
    return int(cur.lastrowid)


def _clear_verification_state(conn: sqlite3.Connection, task_id: str) -> None:
    """Drop a stale verdict when a fresh implementation run starts.

    A VERIFIED verdict certifies the run it was issued against. Re-claiming
    the task for implementation means new work the verdict never saw, so it
    must not remain spendable. The ledger rows stay — only the materialised
    head is cleared — so the audit trail of what was verified when survives.

    Deliberately NOT called by :func:`claim_review_task`: that claim *is* the
    verification phase continuing, not new implementation.
    """
    conn.execute(
        "UPDATE tasks SET verification_state = NULL "
        "WHERE id = ? AND verification_state IS NOT NULL",
        (task_id,),
    )


def regression_evidence_required(
    conn: sqlite3.Connection, task_id: str
) -> bool:
    """Return True when the next PASS on ``task_id`` must carry a proof.

    Materialised on the task by the failing verdict that made the following
    work a material repair. Read (never inferred) so the requirement survives
    a process restart, a re-claim, and a different verifier.
    """
    row = conn.execute(
        "SELECT regression_required FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    keys = set(row.keys())
    return bool("regression_required" in keys and row["regression_required"])


def _current_verification_phase(
    conn: sqlite3.Connection, task_id: str
) -> Optional[tuple[int, Optional[int]]]:
    """Return ``(phase_id, run_id)`` of the open verification phase, if any.

    The phase is the latest ``pending`` verdict row — written by
    :func:`request_review` when the implementation was handed off. Its ledger
    id is the token a regression proof binds to, which is what stops a proof
    being spent on a later, unrelated run: the next handoff writes a new
    phase row with a new id.
    """
    row = conn.execute(
        "SELECT id, run_id FROM task_verifications "
        "WHERE task_id = ? AND kind = ? AND state = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, LEDGER_KIND_VERDICT, VERIFICATION_PENDING),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"]), (int(row["run_id"]) if row["run_id"] is not None else None)


def _arming_failure_id(conn: sqlite3.Connection, task_id: str) -> int:
    """Ledger id of the failing verdict that armed the requirement (0 if none).

    A proof must post-date the failure it answers. Without this, a proof
    gathered *before* the verification failed — describing exactly the code
    that then failed — would satisfy the gate.
    """
    row = conn.execute(
        "SELECT MAX(id) AS id FROM task_verifications "
        "WHERE task_id = ? AND kind = ? AND state = ?",
        (task_id, LEDGER_KIND_VERDICT, VERIFICATION_FAILED),
    ).fetchone()
    if row is None or row["id"] is None:
        return 0
    return int(row["id"])


def _existing_regression_proof(
    conn: sqlite3.Connection, task_id: str, phase_id: int, after_id: int
) -> Optional[int]:
    """Ledger id of a live regression proof for this phase, or ``None``.

    "Live" means both bound to the current verification phase and recorded
    after the failing verdict that armed the requirement.
    """
    row = conn.execute(
        "SELECT id FROM task_verifications "
        "WHERE task_id = ? AND kind = ? AND covers_phase_id = ? AND id > ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, LEDGER_KIND_REGRESSION, int(phase_id), int(after_id)),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _validate_regression_evidence(evidence: Any) -> tuple[bool, Optional[str]]:
    """Check a regression proof against the minimal contract.

    Deliberately narrow: it must be a JSON object, and it must name at least
    one check that was actually re-run. "regressions: none" is a claim; a
    command with an exit code is a fact somebody else can re-execute.
    """
    if not isinstance(evidence, dict) or not evidence:
        return False, (
            "regression evidence must be a non-empty JSON object of "
            "falsifiable facts"
        )
    if not any(evidence.get(k) for k in REGRESSION_EVIDENCE_KEYS):
        return False, (
            "regression evidence must name the check that was re-run — "
            "include at least one of "
            f"{', '.join(REGRESSION_EVIDENCE_KEYS)} "
            "(plus exit codes / counts / artefact paths as available)"
        )
    return True, None


def _regression_gate(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    evidence: Optional[dict] = None,
    verifier: Optional[str] = None,
    reason: Optional[str] = None,
    now: Optional[int] = None,
) -> tuple[bool, Optional[str], Optional[int]]:
    """Decide whether a PASS may be recorded, recording the proof if supplied.

    Returns ``(ok, detail, proof_id)``. ``ok`` is True when the task is not in
    a repair cycle at all (the ordinary first-pass case — nothing extra is
    demanded of it), when ``evidence`` satisfies the contract and has just
    been appended to the ledger, or when a live proof for this phase was
    already recorded. Otherwise ``detail`` says exactly what is missing.

    Must run inside the caller's write transaction: the proof row and the
    verdict that spends it have to land together, or the ledger could show a
    VERIFIED repair with no proof behind it.
    """
    required = regression_evidence_required(conn, task_id)
    phase = _current_verification_phase(conn, task_id)

    if evidence is not None:
        # Recorded whether or not it was demanded. A verifier who re-ran the
        # checks unprompted has produced evidence; silently discarding it
        # because the gate happened to be open would make the ledger lie by
        # omission.
        ok, detail = _validate_regression_evidence(evidence)
        if not ok:
            return False, detail, None
        if phase is None and required:
            return False, (
                "no verification phase is open to bind regression evidence "
                "to; hand the repair off with request_review first"
            ), None
        proof_id = _record_verification_row(
            conn,
            task_id,
            state=VERIFICATION_REGRESSION,
            kind=LEDGER_KIND_REGRESSION,
            run_id=phase[1] if phase else None,
            covers_phase_id=phase[0] if phase else None,
            verifier=verifier,
            evidence=evidence,
            reason=reason or "regression evidence for a material repair",
            now=now,
        )
        return True, None, proof_id

    if not required:
        return True, None, None

    if phase is None:
        return False, (
            "no verification phase is open to bind regression evidence to; "
            "hand the repair off with request_review first"
        ), None
    phase_id, _phase_run_id = phase
    armed_after = _arming_failure_id(conn, task_id)
    existing = _existing_regression_proof(conn, task_id, phase_id, armed_after)
    if existing is not None:
        return True, None, existing

    return False, (
        "regression evidence is required: this task failed verification and "
        "the work since then is a material repair, so a passing verdict must "
        "carry proof that the checks were re-run against the repaired work. "
        "Supply a JSON object naming at least one of "
        f"{', '.join(REGRESSION_EVIDENCE_KEYS)} "
        "(e.g. `hermes kanban verify <id> --pass --regression-evidence "
        "'{\"command\": \"pytest -q tests/foo.py\", \"exit_code\": 0}'`)"
    ), None


def _spend_regression_requirement(
    conn: sqlite3.Connection, task_id: str
) -> None:
    """Disarm the repair gate once a PASS has spent a live proof.

    Only ever called in the same transaction as the VERIFIED verdict that the
    proof backs, so ``regression_required = 0`` and ``verification_state =
    'verified'`` become true together — the pair the completion UPDATE tests.
    The ledger rows stay: what was re-run, by whom and when is exactly the
    record a later reader needs.
    """
    conn.execute(
        "UPDATE tasks SET regression_required = 0 WHERE id = ?", (task_id,)
    )


def _review_run_verification(
    conn: sqlite3.Connection, task_id: str
) -> tuple[Optional[int], Optional[str]]:
    """Return ``(run_id, reviewer)`` when the active run is a reviewer run.

    A run claimed from the ``review`` lane is structurally not the
    implementer's: :func:`request_review` ends the implementation run and
    parks the task in ``review`` before :func:`claim_review_task` can open
    this one, and the claim records ``source_status=review`` on its event.
    An approval issued from inside such a run is therefore a second party's
    verdict — the executor cannot manufacture one for itself — so it counts
    as the VERIFIED step rather than a self-report.

    Returns ``(None, None)`` when there is no such run.
    """
    row = conn.execute(
        "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row["status"] != "running" or row["current_run_id"] is None:
        return None, None
    run_id = int(row["current_run_id"])
    if _retry_status_for_run(conn, task_id, run_id) != "review":
        return None, None
    return run_id, row["assignee"]


def _review_requested_implementer(
    conn: sqlite3.Connection, task_id: str, fallback_assignee: Optional[str] = None
) -> Optional[str]:
    """Return the implementer identity recorded on the phase's ``review_requested``
    handoff, falling back to the task's current assignee.

    The implementer identity lives on the ``review_requested`` event that
    parked this phase — NOT necessarily ``tasks.assignee``, which
    ``request_review``/``reopen_review_task`` may reassign to the reviewer
    while the task is parked. Reading current assignee alone would compare
    the reviewer against itself and never catch the real case (author
    reassigns nothing, e.g. a governance profile that stays its own assignee
    throughout, like t_7311348b). Shared by every caller that must refuse a
    verifier matching the implementer — see ``record_verification`` and
    ``complete_task``'s implicit reviewer-approval path.
    """
    implementer = None
    req_event = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if req_event is not None and req_event["payload"]:
        try:
            req_payload = json.loads(req_event["payload"])
        except (json.JSONDecodeError, TypeError):
            req_payload = {}
        imp = req_payload.get("implementer") if isinstance(req_payload, dict) else None
        if isinstance(imp, str) and imp.strip():
            implementer = _canonical_assignee(imp)
    if implementer is None and fallback_assignee:
        implementer = _canonical_assignee(fallback_assignee)
    return implementer


VERIFIER_LANE_IDENTITIES: frozenset[str] = frozenset(INDEPENDENT_VERIFIER_LANES)
_LANE_NAME_ROOTS: frozenset[str] = frozenset(VALID_EXECUTOR_LANES | {"atlas"})
VERIFIER_IDENTITY_LANE = "lane"
VERIFIER_IDENTITY_PROFILE = "profile"
VERIFIER_IDENTITY_UNKNOWN = "unknown"
VERIFIER_IDENTITY_UNCHECKED = "unchecked"
_VERIFIER_LANE_STARTED_EVENT_KIND: dict[str, str] = {
    EXECUTOR_LANE_CODEX_VERIFY: "codex_verifier_started",
    EXECUTOR_LANE_CLAUDE_VERIFY: "claude_verifier_started",
}


def _attest_kind(conn: sqlite3.Connection, verifier_task_id: str) -> str:
    """``*_verifier_started`` event kind for the lane a verifier card is on."""
    row = conn.execute(
        "SELECT executor_lane FROM tasks WHERE id = ?", (verifier_task_id,),
    ).fetchone()
    lane = row["executor_lane"] if row else None
    return _VERIFIER_LANE_STARTED_EVENT_KIND.get(lane, "codex_verifier_started")


def _attested_verifier_run(
    conn: sqlite3.Connection, verifier_task_id: str, lane: str,
) -> Optional[int]:
    """Return the completing run id of a *lane* verifier task the real lane ran.

    ``executor_lane`` is only a label: an unscoped agent that happens to claim
    a verifier card (2026-09-14, run 2817, codex_verify) can complete it too.
    Each lane's own launcher writes its own ``*_verifier_started`` event for
    the run it executes, so the attestation is that event on the run that
    completed the card. ``None`` when the card is not a completed task on
    *lane* or its completing run carries no such event.
    """
    started_kind = _VERIFIER_LANE_STARTED_EVENT_KIND.get(lane)
    if started_kind is None:
        return None
    row = conn.execute(
        "SELECT status, executor_lane FROM tasks WHERE id = ?", (verifier_task_id,),
    ).fetchone()
    if row is None or row["executor_lane"] != lane:
        return None
    if row["status"] != "done":
        return None
    run = conn.execute(
        "SELECT id FROM task_runs WHERE task_id = ? AND outcome = 'completed' "
        "ORDER BY id DESC LIMIT 1",
        (verifier_task_id,),
    ).fetchone()
    if run is None:
        return None
    started = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = ? AND run_id = ? LIMIT 1",
        (verifier_task_id, started_kind, run["id"]),
    ).fetchone()
    return int(run["id"]) if started is not None else None


def _attested_codex_verifier_run(
    conn: sqlite3.Connection, verifier_task_id: str,
) -> Optional[int]:
    """Return the completing run id of a codex_verify task the real lane ran.

    Thin, backward-compatible wrapper over :func:`_attested_verifier_run`
    pinned to ``EXECUTOR_LANE_CODEX_VERIFY`` -- kept because call sites that
    are specifically about the Codex fallback lane (never about whichever
    lane actually ran) read clearer naming it explicitly.
    """
    return _attested_verifier_run(conn, verifier_task_id, EXECUTOR_LANE_CODEX_VERIFY)


def _resolve_lane_verifier_identity(
    conn: sqlite3.Connection, identity: str, subject_id: str,
) -> Optional[str]:
    """Return ``None`` when a lane-scoped identity legitimately verifies *subject_id*.

    Otherwise return the refusal detail. Scoped forms are produced by the
    system itself and are the only ones accepted, one per lane in
    :data:`INDEPENDENT_VERIFIER_LANES`:

    * ``codex_verify:<verifier_task_id>`` / ``claude_verify:<verifier_task_id>``
      — a verifier card on that lane, attested by :func:`_attested_verifier_run`,
      that verifies this subject: linked as its child, or the single safe
      subject of a legacy orphan verifier
      (:func:`_historical_orphan_subject_candidate`).
    * ``codex_verify:<execution_id>`` — a completed exit-0 ``codex.verify``
      execution recorded against this subject by the execution supervisor.
      (``claude_verify`` has no execution-supervisor launcher equivalent, so
      this form only resolves for the ``codex_verify`` root.)
    """
    root, _, ref = identity.partition(":")
    root = root.strip().lower()
    ref = ref.strip()
    if root not in VERIFIER_LANE_IDENTITIES:
        return (
            f"{identity!r} names the {root!r} lane, which is not an independent "
            f"verification lane; only {'/'.join(sorted(INDEPENDENT_VERIFIER_LANES))}"
            ":<verifier> may sign"
        )
    if not ref:
        return (
            f"bare lane name {identity!r} is not a verifier identity; an "
            f"independent verdict must be signed as "
            f"{root}:<attested verifier task or execution>"
        )
    if ref == subject_id:
        return f"{identity!r} names the subject itself"
    if root == EXECUTOR_LANE_CODEX_VERIFY:
        execution = conn.execute(
            "SELECT task_id, command_class, status, exit_code FROM executions WHERE id = ?",
            (ref,),
        ).fetchone()
        if execution is not None:
            if (
                execution["task_id"] == subject_id
                and execution["command_class"] == "codex.verify"
                and execution["status"] == "completed"
                and execution["exit_code"] == 0
            ):
                return None
            return (
                f"{identity!r} is not a completed exit-0 codex.verify execution "
                f"recorded against {subject_id}"
            )
    if _attested_verifier_run(conn, ref, root) is None:
        return (
            f"{identity!r} does not resolve to a completed {root} task "
            f"whose completing run was executed by the {root} lane"
        )
    linked = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ? "
        "UNION ALL SELECT 1 FROM task_relations "
        "WHERE from_task_id = ? AND to_task_id = ? AND relation = ? LIMIT 1",
        (subject_id, ref, ref, subject_id, RELATION_VERIFIES),
    ).fetchone()
    if linked is not None:
        return None
    if _historical_orphan_subject_candidate(conn, ref) == subject_id:
        return None
    return f"{identity!r} is not a verifier of {subject_id}"


def _verifier_identity_status(
    verifier: Optional[str],
    *,
    conn: Optional[sqlite3.Connection] = None,
    subject_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Classify a verifier identity for a verdict on *subject_id*.

    Returns ``(status, detail)``:

    * ``"lane"``      — a scoped ``codex_verify:<verifier>`` identity that
      resolves, for this subject, per :func:`_resolve_lane_verifier_identity`.
      Deliberately not required to be a profile.
    * ``"profile"``   — an existing Hermes profile (``profile_exists``).
    * ``"unknown"``   — anything else, including every bare lane name and any
      lane identity that cannot be resolved (no ``conn``/``subject_id``). The
      verdict must be refused.
    * ``"unchecked"`` — the profile registry could not be consulted at all
      (import or filesystem failure). Reported as itself so an unreadable
      registry is visible rather than masquerading as either a clean pass or a
      false accusation; callers let the verdict through and record the gap.
    """
    if not verifier or not str(verifier).strip():
        return VERIFIER_IDENTITY_UNKNOWN, "no verifier identity supplied"
    identity = str(verifier).strip()
    lane_root = identity.split(":", 1)[0].strip().lower()
    if lane_root in _LANE_NAME_ROOTS:
        if conn is None or not subject_id:
            return VERIFIER_IDENTITY_UNKNOWN, (
                f"lane identity {identity!r} cannot be resolved without its subject"
            )
        refusal = _resolve_lane_verifier_identity(conn, identity, subject_id)
        if refusal is not None:
            return VERIFIER_IDENTITY_UNKNOWN, refusal
        return VERIFIER_IDENTITY_LANE, identity
    try:
        from hermes_cli.profiles import profile_exists as _profile_exists
    except Exception as exc:  # pragma: no cover - defensive
        return VERIFIER_IDENTITY_UNCHECKED, f"profile registry unavailable: {exc}"
    try:
        exists = bool(_profile_exists(identity))
    except Exception as exc:  # pragma: no cover - defensive
        return VERIFIER_IDENTITY_UNCHECKED, f"profile lookup failed: {exc}"
    if exists:
        return VERIFIER_IDENTITY_PROFILE, identity
    return VERIFIER_IDENTITY_UNKNOWN, (
        f"{identity!r} is neither an existing Hermes profile nor a scoped "
        f"verifier-lane identity ({EXECUTOR_LANE_CODEX_VERIFY}:<verifier>)"
    )


def _phase_reviewer_identities(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Identities explicitly installed as the CURRENT phase's reviewer.

    ``verifier == tasks.assignee`` is normally self-verification, but it cannot
    be refused flat, because the sanctioned independence recovery is precisely
    to REASSIGN the parked task to an independent reviewer — after which the
    reviewer legitimately IS the assignee. ``complete_task``'s implicit
    approval path even derives its reviewer identity from ``tasks.assignee``
    (see ``_review_run_verification``), so a flat comparison would refuse every
    review-run approval on the board.

    Two routes put someone in the reviewer seat on purpose, and only these two
    earn the exception:

    * ``review_requested.reviewer`` — ``request_review(reviewer=B)`` records B
      on the handoff event and reassigns the row to B inside the same
      transaction, emitting no separate ``assigned`` event.
    * an ``assigned`` event AFTER the phase opened — the manual recovery route
      (reclaim + assign) used on t_4edb5874 on 2026-09-03, when 'default' was
      correctly blocked from reviewing itself and compliance_worker was handed
      the review.

    Anything else that arrives in ``tasks.assignee`` — a dispatcher-side
    shorthand normalization, ``reopen_review_task``'s implementer restore, a
    direct row edit — is not a reviewer installation and does not earn it.

    The exception applies ONLY to the assignee-derived identity. The
    ``review_requested`` implementer is never exempted, so this is strictly
    additive: it can add refusals, never remove one.
    """
    floor = _verification_phase_floor(conn, task_id)
    identities: set[str] = set()
    if floor:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE id = ?", (floor,),
        ).fetchone()
        if row is not None and row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                payload = {}
            reviewer = payload.get("reviewer") if isinstance(payload, dict) else None
            if isinstance(reviewer, str) and reviewer.strip():
                identities.add(_canonical_assignee(reviewer))
    for row in conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'assigned' AND id > ?",
        (task_id, floor),
    ):
        if not row["payload"]:
            continue
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        assignee = payload.get("assignee") if isinstance(payload, dict) else None
        if isinstance(assignee, str) and assignee.strip():
            identities.add(_canonical_assignee(assignee))
    identities.discard(None)  # type: ignore[arg-type]
    return identities


def _verifier_independence_conflict(
    conn: sqlite3.Connection,
    task_id: str,
    verifier: Optional[str],
    *,
    implementer: Optional[str],
    assignee: Optional[str],
) -> Optional[tuple[str, str]]:
    """Return ``(conflicting_identity, source)`` when *verifier* is not independent.

    ``source`` is ``"implementer"`` (the identity recorded on the phase's
    ``review_requested`` handoff) or ``"assignee"`` (the task's own assignee,
    where that assignee was never installed as this phase's reviewer). Returns
    ``None`` when the verifier is independent of both.
    """
    if not verifier:
        return None
    verifier = _canonical_assignee(verifier)
    if implementer and verifier == implementer:
        return implementer, "implementer"
    assignee = _canonical_assignee(assignee) if assignee else None
    if assignee and verifier == assignee:
        if assignee not in _phase_reviewer_identities(conn, task_id):
            return assignee, "assignee"
    return None


def _verification_phase_floor(conn: sqlite3.Connection, task_id: str) -> int:
    """Return the event id that opens the current verification phase."""
    row = conn.execute(
        "SELECT id FROM task_events "
        "WHERE task_id = ? AND kind = 'review_requested' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return int(row["id"]) if row is not None else 0


def _self_review_refused_implementers(
    conn: sqlite3.Connection, task_id: str
) -> set[str]:
    """Identities refused as self-reviewers in the current verification phase.

    Derived from the ``verification_blocked_self_review`` audit events the
    refusal paths already emit — the historical events are the source of
    truth, so this reads correctly against boards that recorded refusals
    before this repair existed (t_00690780 among them).
    """
    floor = _verification_phase_floor(conn, task_id)
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'verification_blocked_self_review' "
        "AND id > ?",
        (task_id, floor),
    ).fetchall()
    refused: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if not isinstance(payload, dict):
            continue
        for key in ("implementer", "verifier"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                refused.add(_canonical_assignee(value))
    return refused


def _independent_verification_required(
    conn: sqlite3.Connection, task_id: str
) -> bool:
    """Whether a self-review refusal is on record for the current phase."""
    return bool(_self_review_refused_implementers(conn, task_id))


def _emit_once_this_phase(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
) -> bool:
    """Append ``kind`` only if it is not already on record for this phase.

    The review dispatcher re-evaluates every parked task on every tick. A
    refusal that emitted an event per tick would bury the real history under
    thousands of duplicates — the opposite of the durable, readable state this
    repair is for. Returns True when the event was actually written.
    """
    floor = _verification_phase_floor(conn, task_id)
    existing = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND id > ? "
        "LIMIT 1",
        (task_id, kind, floor),
    ).fetchone()
    if existing is not None:
        return False
    _append_event(conn, task_id, kind, payload, run_id=run_id)
    return True


def _review_claim_conflict(
    conn: sqlite3.Connection, task_id: str, assignee: Optional[str]
) -> Optional[tuple[str, str]]:
    """Return ``(identity, source)`` when *assignee* may not review ``task_id``.

    ``source`` is ``"refused"`` (this identity already has a
    ``verification_blocked_self_review`` on record for the phase),
    ``"implementer"`` or ``"assignee"`` (the two independence conflicts
    :func:`_verifier_independence_conflict` distinguishes). ``None`` means the
    identity may open a review run.

    Read by :func:`claim_review_task` and by the dispatcher's review-slot
    reservation, so a candidate the claim path will reject never consumes the
    slot it reserved.

    An identity deliberately installed as THIS phase's reviewer is not
    pre-empted — ``request_review(reviewer=X)`` / ``assign_task`` are a caller
    stating who reviews — UNLESS that identity is the implementer. Selection
    used to defer to the installation even then, while the verdict gate
    (``_verifier_independence_conflict``) refused it: on 2026-09-14 the CLI
    handed ``t_3883034a`` to ``--reviewer default`` for work ``default``
    implemented, selection opened review run 2816 for ``default`` (event
    151161) and the verdict gate refused it (151169), with no verifier route
    opened in between. Selection and verdict now agree: the implementer never
    reviews its own Gauntlet work, installed or not.
    """
    if not assignee:
        return None
    candidate = _canonical_assignee(assignee)
    if candidate in _self_review_refused_implementers(conn, task_id):
        return candidate, "refused"
    if not gauntlet_required(conn, task_id):
        return None
    implementer = _review_requested_implementer(conn, task_id, assignee)
    if candidate in _phase_reviewer_identities(conn, task_id):
        if implementer and candidate == implementer:
            return candidate, "implementer"
        return None
    return _verifier_independence_conflict(
        conn, task_id, candidate, implementer=implementer, assignee=assignee,
    )


def _open_verifier_child(
    conn: sqlite3.Connection, subject_id: str
) -> Optional[str]:
    """Return a valid, still-live ``codex_verify`` child of ``subject_id``.

    A child is reusable only when its durable ``verifies`` relation names this
    subject and the subject currently carries evidence the verifier dependency
    gate accepts.  The dependency edge is intentionally *not* required here:
    it may be removed before dispatch, while the durable relation remains the
    verdict-return attribution.  This rejects historical blocked/orphaned or
    otherwise unroutable cards instead of treating their mere task-link and
    lane label as an idempotency key.

    "Live" also excludes ``blocked``: a blocked verifier (circuit breaker,
    ``INVALID_VERIFIER_LINKAGE``, operator hold) is not dispatchable, so even
    with otherwise-valid linkage it is not a route to a verdict. It is left
    untouched as history and ``request_review`` mints a fresh child instead.
    """
    if not _subject_evidence_is_dispatchable(conn, subject_id):
        return None
    rows = conn.execute(
        "SELECT c.id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        "WHERE l.parent_id = ? AND c.executor_lane IN (?, ?) "
        "AND c.status NOT IN ('blocked', 'done', 'archived') "
        "AND EXISTS ("
        "  SELECT 1 FROM task_relations r "
        "  WHERE r.from_task_id = c.id AND r.to_task_id = ?"
        "    AND r.relation = ?"
        ") "
        "ORDER BY c.created_at ASC, c.id ASC",
        (
            subject_id,
            EXECUTOR_LANE_CODEX_VERIFY,
            EXECUTOR_LANE_CLAUDE_VERIFY,
            subject_id,
            RELATION_VERIFIES,
        ),
    ).fetchall()
    for row in rows:
        if not missing_verifier_linkage(conn, row["id"]):
            return row["id"]
    return None


def _blocked_verifier_child(
    conn: sqlite3.Connection, subject_id: str
) -> Optional[str]:
    """Return a BLOCKED ``codex_verify`` child still tied to ``subject_id``.

    Mirrors :func:`_open_verifier_child`'s linkage/relation scoping exactly,
    but looks for a stuck verifier (``status == 'blocked'``) instead of a
    live one. A blocked verifier is not dispatchable -- it is NOT a route to
    a verdict -- but it IS evidence that this leg already has an open
    verification episode. Treating "no live child" as "no child" let the
    stale-disposition actuator mint a fresh verifier every 5-6 minutes
    against a subject whose existing verifier was simply stuck (blocked),
    producing 22 independent-verifier children on one subject in two hours
    (t_ab80ef2a, 2026-10-25 19:47-21:52). The correct disposition for a
    stuck verifier is to surface it, not multiply it.
    """
    row = conn.execute(
        "SELECT c.id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        "WHERE l.parent_id = ? AND c.executor_lane IN (?, ?) "
        "AND c.status = 'blocked' "
        "AND EXISTS ("
        "  SELECT 1 FROM task_relations r "
        "  WHERE r.from_task_id = c.id AND r.to_task_id = ?"
        "    AND r.relation = ?"
        ") "
        "ORDER BY c.created_at DESC, c.id DESC LIMIT 1",
        (
            subject_id,
            EXECUTOR_LANE_CODEX_VERIFY,
            EXECUTOR_LANE_CLAUDE_VERIFY,
            subject_id,
            RELATION_VERIFIES,
        ),
    ).fetchone()
    return row["id"] if row is not None else None


def _blocked_verifier_in_current_episode(
    conn: sqlite3.Connection, subject_id: str, lane: str
) -> bool:
    """True (refusal recorded once) when this subject's newest verifier child
    is blocked ON THE SAME LANE and was created after the subject's latest
    review handoff. A different lane is a real change (swap_verifier_lane_once)."""
    stuck = _blocked_verifier_child(conn, subject_id)
    if stuck is None:
        return False
    stuck_lane = conn.execute(
        "SELECT executor_lane FROM tasks WHERE id = ?", (stuck,)
    ).fetchone()["executor_lane"]
    if stuck_lane != lane:
        return False
    last_review = conn.execute(
        "SELECT MAX(id) AS id FROM task_events WHERE task_id = ? "
        "AND kind = 'review_requested'",
        (subject_id,),
    ).fetchone()["id"] or 0
    created = conn.execute(
        "SELECT MAX(id) AS id FROM task_events WHERE task_id = ? "
        "AND kind = 'independent_verifier_child_created' "
        "AND json_extract(payload, '$.verifier_task') = ?",
        (subject_id, stuck),
    ).fetchone()["id"]
    if created is None or created < last_review:
        return False
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind = 'verifier_respawn_refused_blocked' LIMIT 1",
        (subject_id, created),
    ).fetchone() is None:
        with write_txn(conn):
            _append_event(
                conn, subject_id, "verifier_respawn_refused_blocked",
                {"blocked_verifier": stuck},
            )
    return True


def _verifier_stuck_alarm_is_owed(
    conn: sqlite3.Connection, subject_id: str, verifier_task_id: str
) -> bool:
    """True when ``verifier_stuck_surfaced`` has not yet fired for THIS exact
    blocked verifier child.

    Scoped to the verifier's id rather than just the subject: a NEW blocked
    child (after an old one was superseded, retired, or unblocked) is a new
    episode and must be audible again, while repeated stale ticks against
    the SAME stuck child must not re-alarm every cycle -- that per-tick
    re-alarming is the same failure shape as the duplicate-verifier defect,
    just in the notification channel instead of task creation.
    """
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'gauntlet_stale_disposition' "
        "AND json_extract(payload, '$.action') = 'verifier_stuck_surfaced' "
        "AND json_extract(payload, '$.verifier_task') = ? LIMIT 1",
        (subject_id, verifier_task_id),
    ).fetchone() is None


def _subject_evidence_is_dispatchable(
    conn: sqlite3.Connection, subject_id: str
) -> bool:
    """Whether a verifier child parented to ``subject_id`` could ever start.

    Runs the *same* evidence predicate the dependency gate runs
    (:func:`_subject_has_evidence_sql`), so a True here means the gate will
    promote the child and a False means it never will.
    """
    row = conn.execute(
        f"SELECT 1 FROM tasks t WHERE t.id = :subject "
        f"AND {_subject_has_evidence_sql('t')} LIMIT 1",
        {"subject": subject_id, "verified": VERIFICATION_VERIFIED},
    ).fetchone()
    return row is not None


def _unroutable_recheck_timer(
    conn: sqlite3.Connection, task_id: str
) -> Optional[ObservationTimer]:
    """The card's live UNROUTABLE recheck timer, if one is armed.

    Mirrors :func:`_stall_timer`; kept separate so the two observations retire
    independently.
    """
    for timer in task_observation_timers(
        conn, task_id, state=OBSERVATION_STATE_OBSERVING,
    ):
        if timer.kind == UNROUTABLE_VERIFICATION_TIMER_KIND:
            return timer
    return None


def _unroutable_alarm_is_owed(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None
) -> bool:
    """Whether the UNROUTABLE alarm may speak again for ``task_id``.

    Three cases, in the order they are decided:

    * **No timer armed** — this is a new episode. Owed. (Also the path taken
      when a previous episode was retired by :func:`_retire_unroutable_recheck`,
      which is what makes a recurrence audible instead of permanently muted.)
    * **Timer armed and due** — ten minutes have elapsed and the evidence is
      still missing. The recheck is discharged via :func:`emit_observation_tick`
      (which re-arms on the original 300 s grid rather than re-phasing to
      ``now``) and the alarm is owed again.
    * **Timer armed and not yet due** — the same unchanged fact inside the same
      window. Not owed.

    A card that cannot carry a timer at all (ownerless, so
    :func:`arm_observation_timer` fails closed) degrades to once-ever via
    :func:`_already_alarmed` rather than back to once-per-tick: losing the
    recheck cadence must never cost more than the recheck.
    """
    now_ts = int(time.time()) if now is None else int(now)
    timer = _unroutable_recheck_timer(conn, task_id)
    if timer is None:
        if _already_alarmed(
            conn, task_id, "independent_verification_unroutable"
        ):
            # Alarmed before, yet no timer survives. Either arming failed on an
            # ownerless card, or the episode was explicitly retired. Retirement
            # is the only path that deletes nothing and leaves the event behind,
            # and it is distinguishable: it writes observation_timer_closed.
            return _unroutable_episode_was_retired(conn, task_id)
        return True
    if int(timer.next_due_at) > now_ts:
        return False
    emit_observation_tick(
        conn, timer.id,
        payload={"observation": UNROUTABLE_VERIFICATION_TIMER_KIND},
        now=now_ts,
    )
    # The recheck keeps observing on its 300-second grid; repeating the same
    # unresolved alarm follows the stale re-alert window instead
    # (operations.md stale-supervision amendment, 2026-09-14).
    last = conn.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? "
        "AND kind = 'independent_verification_unroutable' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    realert = gauntlet_stale_realert_default()
    if last is not None and (realert <= 0 or now_ts - int(last["created_at"]) < realert):
        return False
    return True


def _unroutable_episode_was_retired(
    conn: sqlite3.Connection, task_id: str
) -> bool:
    """True when the last UNROUTABLE observation on ``task_id`` was retired.

    Distinguishes "this card never could carry a timer" (stay quiet after the
    single alarm already on the ledger) from "the previous episode resolved and
    a new one has begun" (speak again).
    """
    row = conn.execute(
        "SELECT state FROM observation_timers WHERE task_id = ? AND kind = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, UNROUTABLE_VERIFICATION_TIMER_KIND),
    ).fetchone()
    return row is not None and row["state"] == OBSERVATION_STATE_CLOSED


def _arm_unroutable_recheck(
    conn: sqlite3.Connection, task_id: str, *, now: Optional[int] = None
) -> tuple[Optional[str], Optional[str]]:
    """Arm the 300 s recheck for a new UNROUTABLE episode.

    Returns ``(timer_id, error)``. Best-effort and never blocks the alarm, for
    the same reason :func:`sweep_lifecycle_stall_alarms` arms best-effort: an
    ownerless card cannot carry a timer, and an ownerless card stuck with no
    routable verification is exactly one a supervisor must still hear about.
    The refusal rides on the alarm payload rather than being swallowed.
    """
    if _unroutable_recheck_timer(conn, task_id) is not None:
        return None, None
    try:
        return arm_observation_timer(
            conn, task_id,
            kind=UNROUTABLE_VERIFICATION_TIMER_KIND,
            now=now,
        ).id, None
    except Exception as exc:
        _log.warning(
            "unroutable-verification recheck could not be armed on %s: %s",
            task_id, exc,
        )
        return None, f"{type(exc).__name__}: {exc}"


def _retire_unroutable_recheck(
    conn: sqlite3.Connection, task_id: str, *, reason: str
) -> bool:
    """Close the UNROUTABLE recheck once the condition it watched resolved.

    The ACTIVE half of criterion (D)'s active/inactive handling for this alarm:
    evidence arrived, or a verifier child is open, so there is a route now and
    the observation has nothing left to watch. Best-effort — a failure to
    retire must not break the dispatch path that just succeeded.
    """
    timer = _unroutable_recheck_timer(conn, task_id)
    if timer is None:
        return False
    try:
        return close_observation_timer(conn, timer.id, reason=reason)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "unroutable-verification recheck could not be retired on %s: %s",
            task_id, exc,
        )
        return False


_VERIFIER_RESPAWN_GUARD_AUTHOR = "verifier-respawn-guard"
_VERIFIER_NO_RESULT_KINDS = ("verification_blocker_returned", "verifier_verdict_unreadable")
_VERIFIER_SYSTEM_AUTHORS = ("verifier-return-path", _VERIFIER_RESPAWN_GUARD_AUTHOR)
VERIFIER_REPEATED_FAIL_LIMIT = 2
_REPEATED_FAIL_NON_RELEASING_AUTHORS = frozenset({
    *_VERIFIER_SYSTEM_AUTHORS, "atlas-codex-lane", "default", "worker", "codex",
    "dispatcher", "recovery-lane", "orphan-verifier-watchdog", "queue-hygiene",
    "system-health-controller",
})


def _repeated_fail_anchor(conn: sqlite3.Connection, subject_id: str) -> Optional[int]:
    """Event id of the latest FAIL when the subject has failed independent
    verification ``VERIFIER_REPEATED_FAIL_LIMIT`` times in a row, else None.

    Christopher, 2026-10-15: no resource-draining loops. t_7b593c5f failed
    verification nine times in 20 minutes on one missing workstation file;
    each FAIL reopened a paid implementer run that could not produce it, then
    a new verifier. A PASS, an unblock, or a comment from a non-executor
    author (a person saying what changed) resets the count.

    Only applies while ``objective_attempt_budget_mode`` is advisory (the live
    board since 2026-10-15). In enforce mode the objective attempt ceiling is
    already the brake and keeps its own, tested semantics.
    """
    if objective_attempt_budget_enforced():
        return None
    assignee_row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (subject_id,)
    ).fetchone()
    assignee = assignee_row["assignee"] if assignee_row else None
    fails = 0
    anchor: Optional[int] = None
    for ev in conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id = ? AND kind IN "
        "('verification_failed', 'verification_passed', 'commented', 'unblocked', "
        "'verifier_released_after_repair') "
        "ORDER BY id DESC LIMIT 500",
        (subject_id,),
    ).fetchall():
        kind = ev["kind"]
        if kind == "verification_failed":
            fails += 1
            if anchor is None:
                anchor = int(ev["id"])
            continue
        if kind in (
            "verification_passed", "unblocked", "verifier_released_after_repair",
        ):
            break
        try:
            author = json.loads(ev["payload"] or "{}").get("author")
        except (ValueError, AttributeError):
            author = None
        author = str(author or "")
        if (
            author
            and author not in _REPEATED_FAIL_NON_RELEASING_AUTHORS
            and author != assignee
            and not author.startswith("claude-lane")
        ):
            break
    return anchor if fails >= VERIFIER_REPEATED_FAIL_LIMIT else None


def _verifier_respawn_guard_anchor(
    conn: sqlite3.Connection, subject_id: str
) -> Optional[int]:
    """The event id the re-queue guard is holding ``subject_id`` on, or None.

    READ-ONLY. This is the rule itself: the last no-verdict verifier return
    (BLOCKER or unreadable) with no new review handoff and no non-system
    comment after it. Both :func:`verifier_respawn_guard_holds` (observers
    such as the health controller) and :func:`_verifier_respawn_unchanged`
    (the dispatch path, which also records the refusal) answer from this one
    predicate so they can never disagree about whether the guard is holding.
    """
    repeated_fail = _repeated_fail_anchor(conn, subject_id)
    if repeated_fail is not None:
        return repeated_fail
    last = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY id DESC LIMIT 1",
        (subject_id, *_VERIFIER_NO_RESULT_KINDS),
    ).fetchone()
    if last is None:
        return None
    for ev in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind IN ('review_requested', 'commented')",
        (subject_id, last["id"]),
    ).fetchall():
        if ev["kind"] == "review_requested":
            return None
        try:
            author = json.loads(ev["payload"] or "{}").get("author")
        except ValueError:
            author = None
        if author not in _VERIFIER_SYSTEM_AUTHORS:
            return None
    return int(last["id"])


def verifier_respawn_guard_holds(conn: sqlite3.Connection, subject_id: str) -> bool:
    """True when the no-identical-re-verification guard is holding ``subject_id``.

    READ-ONLY: writes no event and no comment. A held subject is waiting on a
    human (a non-system comment saying what changed, or a new review handoff),
    not on the dispatcher; a monitor that asks this question must report it as
    held rather than count the guard's refusal as a failed recovery
    (t_e8eb2485, 2026-10-07: that miscount escalated light health and took
    boot to READY: NO).
    """
    return _verifier_respawn_guard_anchor(conn, subject_id) is not None


def _verifier_respawn_unchanged(conn: sqlite3.Connection, subject_id: str) -> bool:
    """True (and recorded once) when the last verifier produced no verdict and
    nothing has changed on the subject since.

    Christopher, 2026-10-07: no identical re-verification. A BLOCKER or an
    unreadable verdict leaves the subject exactly as it was, so another
    verifier against that state can only reproduce the same result --
    t_92521910 spent four verifier runs in 26 minutes that way. A new verifier
    is released only by a new review handoff or a non-system comment saying
    what changed. The rule lives in :func:`_verifier_respawn_guard_anchor`;
    this wrapper adds the once-per-hold refusal record.
    """
    anchor = _verifier_respawn_guard_anchor(conn, subject_id)
    if anchor is None:
        return False
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? "
        "AND kind = 'verifier_respawn_refused_unchanged' LIMIT 1",
        (subject_id, anchor),
    ).fetchone() is None:
        with write_txn(conn):
            _append_event(
                conn, subject_id, "verifier_respawn_refused_unchanged",
                {"last_no_result_event": anchor},
            )
            if _repeated_fail_anchor(conn, subject_id) == anchor:
                text = (
                    f"Held: independent verification failed "
                    f"{VERIFIER_REPEATED_FAIL_LIMIT} times in a row. No unchanged "
                    "rework run or verifier will start. A repair card is opened "
                    "against the verifier's finding; when it completes with a "
                    "recorded change, one new verifier is released automatically. "
                    "A comment saying what changed also releases one new attempt."
                )
            else:
                text = (
                    "Re-verification refused: the last verifier returned no verdict "
                    "and nothing has changed since. Fix the blocker, then post a "
                    "comment saying what changed; that comment releases one new "
                    "verifier."
                )
            add_comment(conn, subject_id, _VERIFIER_RESPAWN_GUARD_AUTHOR, text)
    return True


def _subject_implemented_on_claude(conn: sqlite3.Connection, subject_id: str) -> bool:
    """True when *subject_id* (or its still-open implementation child) ran on
    :data:`EXECUTOR_LANE_CLAUDE`.

    A ``claude_verify`` verdict cannot be independent of Claude-implemented
    work, so the default-lane selection in
    :func:`_ensure_independent_verifier_child` checks this before honouring
    ``claude_verify``. The common case is direct: the subject card itself
    carries ``executor_lane == EXECUTOR_LANE_CLAUDE``. The repair-leg case is
    indirect: the subject is a governance wrapper (e.g. the card a
    ``RELATION_REPAIRS`` edge points at) whose actual implementation happened
    on a still-open direct child instead of the wrapper card. Only an open
    child counts — a child that has already reached a terminal state
    (``done``/``archived``, or any ``terminal_disposition``) already went
    through its own independent verification and is not what is "currently
    being verified" here.
    """
    row = conn.execute(
        "SELECT executor_lane FROM tasks WHERE id = ?", (subject_id,),
    ).fetchone()
    if row is not None and row["executor_lane"] == EXECUTOR_LANE_CLAUDE:
        return True
    children = conn.execute(
        "SELECT c.executor_lane AS executor_lane, c.status AS status, "
        "c.terminal_disposition AS terminal_disposition "
        "FROM task_links l JOIN tasks c ON c.id = l.child_id "
        "WHERE l.parent_id = ?",
        (subject_id,),
    ).fetchall()
    for child in children:
        if (
            child["executor_lane"] == EXECUTOR_LANE_CLAUDE
            and child["status"] not in ("done", "archived")
            and child["terminal_disposition"] is None
        ):
            return True
    return False


def _release_failed_subject_for_verifier(
    conn: sqlite3.Connection, subject_id: str
) -> bool:
    """Reopen a FAILED subject's verdict as pending for one new verifier.

    Called only once the respawn guard has let a new verifier through, i.e. a
    person's "what changed" comment or a new handoff. Same ledger write as
    :func:`_release_verifier_after_repair`; a subject that is not ``failed``
    is left alone.
    """
    row = conn.execute(
        "SELECT verification_state FROM tasks WHERE id = ?", (subject_id,)
    ).fetchone()
    if row is None or row["verification_state"] != VERIFICATION_FAILED:
        return False
    with write_txn(conn):
        _record_verification_row(
            conn, subject_id,
            state=VERIFICATION_PENDING,
            verifier="comment-release",
            evidence={"released_by": "comment"},
            reason="a comment saying what changed released one new "
                   "independent verification",
        )
        _append_event(
            conn, subject_id, "verifier_released_after_comment",
            {"subject_id": subject_id},
        )
    return True


def _ensure_independent_verifier_child(
    conn: sqlite3.Connection, subject_id: str, *, implementer: Optional[str],
    preferred_lane: Optional[str] = None,
) -> Optional[str]:
    """Open the independent verification route for a parked Gauntlet subject.

    Returns the verifier child's id — the pre-existing one when there is one,
    otherwise a newly created verifier card parented to the subject, on
    ``preferred_lane`` (any lane in :data:`INDEPENDENT_VERIFIER_LANES`).

    ``preferred_lane`` defaults to :data:`DEFAULT_INDEPENDENT_VERIFIER_LANE`
    (``claude_verify`` — Christopher's 2026-10-25 authorization, t_ab80ef2a:
    Claude is the default independent verifier, Codex the deterministic
    fallback). It is forced to ``codex_verify`` regardless of the caller's
    request whenever a Claude verifier could not be independent of the work
    under verification: the subject (or its still-open implementation child)
    was itself implemented on ``EXECUTOR_LANE_CLAUDE``. An explicit "ask
    Atlas" request (``preferred_lane=EXECUTOR_LANE_CODEX_VERIFY``, e.g. from
    ``reviewer='atlas'``) is honoured as-is; it was already codex_verify
    before this lane became configurable and continues unchanged.

    The child is created directly on the chosen lane via ``create_task``'s
    ``executor_lane`` parameter (NOT the ``assignee='atlas'`` shorthand,
    which only ever resolves to ``codex_verify``), carried on the
    ``default`` profile exactly as every other direct-executor-lane write.
    It inherits the dependency carve-out in ``_EVIDENCE_READY_PARENT_SQL``
    (keyed on the shared ``INDEPENDENT_VERIFIER_LANES`` set): it becomes
    claimable off the subject's evidence packet rather than waiting for the
    subject to reach ``done``, which it never can without this verdict.
    Its PASS/FAIL then returns through :func:`_return_verifier_verdict_to_subjects`.

    Best-effort by construction, and called post-commit: the review handoff it
    follows is already durable, and a failure here must not un-park a subject
    whose implementation succeeded. Failing to create the child fails CLOSED —
    the subject stays in ``review`` with ``verification_state='pending'``,
    ``independent_verification_required`` on its ledger, and no route by which
    anything can bless it.

    **A child that cannot be dispatched is not a route.** The dependency gate
    promotes a verifier child only once its subject carries evidence, so
    creating one for an evidence-less subject manufactures a card that is
    permanently un-dispatchable: the board shows an open verifier, the
    lifecycle shows nothing happening, and the two never resolve. Observed
    live on ``t_cbc133db`` — refused at 23:31:20, then 78 minutes of no events
    at all, with four delegated children gated behind it.

    So the evidence is checked FIRST, against the same predicate the gate uses.
    With no acceptable evidence there is nothing falsifiable to verify: no
    child is created, ``independent_verification_unroutable`` goes on the
    ledger, and it is routed to Erika immediately rather than waiting out the
    four-hour staleness clock. The evidence gate itself is NOT loosened —
    whether an implementer's ``request_review`` summary should count as
    durable evidence changes what "falsifiable" means for delegation cards,
    and that is a governance decision, not a code bypass.
    """
    lane = preferred_lane or DEFAULT_INDEPENDENT_VERIFIER_LANE
    if lane not in INDEPENDENT_VERIFIER_LANES:
        lane = DEFAULT_INDEPENDENT_VERIFIER_LANE
    # A Claude verifier cannot be independent of Claude-implemented work. If
    # the subject itself (or its still-open implementation child, for the
    # repair-leg case where the subject card is a governance wrapper) was
    # implemented on EXECUTOR_LANE_CLAUDE, force the deterministic Codex
    # fallback regardless of what the caller asked for.
    if lane == EXECUTOR_LANE_CLAUDE_VERIFY and _subject_implemented_on_claude(
        conn, subject_id,
    ):
        lane = EXECUTOR_LANE_CODEX_VERIFY
    # Same board-scoped policy as worker and review admission. In advisory mode
    # the count remains visible but does not prevent independent verification.
    reached, _attempts, _limit, _root = _objective_attempt_ceiling_reached(conn, subject_id)
    if reached and objective_attempt_budget_enforced():
        with write_txn(conn):
            _block_objective_attempt_ceiling(conn, subject_id)
        return None
    existing = _open_verifier_child(conn, subject_id)
    if existing is not None:
        # Every Gauntlet verifier is itself Gauntlet work and therefore owns
        # the same 300-second attempt ceiling. Historically verifier children
        # inherited gauntlet_enforced through their parent but not the runtime
        # cap, producing uncapped Atlas runs. Normalize old open children here
        # and make creation below explicit so the defect cannot recur.
        if gauntlet_required(conn, subject_id):
            with write_txn(conn):
                # ``<`` rather than ``!=`` (2026-09-07). The defect this guard
                # was written for was verifier children inheriting
                # ``gauntlet_enforced`` but NOT a runtime cap, producing
                # *uncapped* Atlas runs -- so the property that matters is
                # "never above the floor unbounded", not "exactly 300".
                # Forcing equality also silently reverted any deliberate raise,
                # which would have made the cap ladder a no-op for verifier
                # cards -- 77 of the 90 cards historically sitting at 300s.
                # Lowering is still refused; only a raise survives.
                cur = conn.execute(
                    "UPDATE tasks SET max_runtime_seconds = ? "
                    "WHERE id = ? AND (max_runtime_seconds IS NULL "
                    "OR max_runtime_seconds < ?)",
                    (
                        MANDATORY_OBSERVATION_INTERVAL_SECONDS,
                        existing,
                        MANDATORY_OBSERVATION_INTERVAL_SECONDS,
                    ),
                )
                if cur.rowcount:
                    _append_event(
                        conn,
                        existing,
                        "gauntlet_runtime_inherited",
                        {
                            "max_runtime_seconds": MANDATORY_OBSERVATION_INTERVAL_SECONDS,
                            "subject": subject_id,
                        },
                    )
        if preferred_lane == EXECUTOR_LANE_CODEX_VERIFY:
            # An explicit "ask Atlas" request names the Codex lane: an unclaimed
            # default-lane (Claude) child is retargeted, never reused as-is.
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET executor_lane = ? WHERE id = ? "
                    "AND executor_lane = ? AND status IN ('todo', 'ready')",
                    (EXECUTOR_LANE_CODEX_VERIFY, existing, EXECUTOR_LANE_CLAUDE_VERIFY),
                )
                if cur.rowcount:
                    _append_event(
                        conn, existing, "verifier_lane_retargeted",
                        {
                            "from": EXECUTOR_LANE_CLAUDE_VERIFY,
                            "to": EXECUTOR_LANE_CODEX_VERIFY,
                            "reason": "explicit_atlas_request",
                            "subject": subject_id,
                        },
                    )
        _retire_unroutable_recheck(conn, subject_id, reason="verifier_child_open")
        return existing
    # No identical re-verification -- see _verifier_respawn_unchanged.
    if _verifier_respawn_unchanged(conn, subject_id):
        return None
    # A verifier BLOCKED in the current review episode is stuck, not missing.
    # Its block lives on the child card, so the subject-side guard above never
    # sees it, and every dispatcher tick minted another doomed child: 9 codex
    # verifiers in 9 minutes on t_bd45babc, 25 on t_bb79d28c (2026-11-04,
    # Codex plan usage limit). Mirrors the stale-disposition actuator's
    # _blocked_verifier_child rule: surface it, never multiply it. A new
    # review handoff (new episode) or a swap to the OTHER lane releases one
    # fresh verifier.
    if _blocked_verifier_in_current_episode(conn, subject_id, lane):
        return None
    if not _subject_evidence_is_dispatchable(conn, subject_id):
        if not _unroutable_alarm_is_owed(conn, subject_id):
            # Same unchanged fact, inside the same 10-minute window. Silence is
            # the correct output: the first alarm is already durable on the
            # ledger and already delivered, and restating it once per dispatcher
            # tick is what teaches a supervisor to ignore the channel.
            return None
        timer_id, timer_error = _arm_unroutable_recheck(conn, subject_id)
        with write_txn(conn):
            _append_event(
                conn, subject_id, "independent_verification_unroutable",
                {
                    "reason": "subject carries no evidence the dispatch gate accepts",
                    "implementer": implementer,
                    "lane": EXECUTOR_LANE_CODEX_VERIFY,
                },
            )
        try:
            route_supervisory_alarm(
                conn,
                subject_id,
                kind="INDEPENDENT_VERIFICATION_UNROUTABLE",
                payload={
                    "subject": subject_id,
                    "implementer": implementer,
                    "detail": (
                        "independent verification is required but the subject "
                        "carries no attachment the dispatch gate will accept, "
                        "so no verifier child can ever run — attach the "
                        "evidence packet or decide the card another way"
                    ),
                    "recheck_timer_id": timer_id,
                    "recheck_interval_seconds": (
                        MANDATORY_OBSERVATION_INTERVAL_SECONDS
                        if timer_id else None
                    ),
                    "recheck_error": timer_error,
                },
            )
        except Exception as exc:  # pragma: no cover - defensive
            # The unroutable event is already durable; a failed *delivery*
            # must not also lose the record of why nothing was created.
            with write_txn(conn):
                _append_event(
                    conn, subject_id, "supervisory_alarm_delivery_failed",
                    {
                        "kind": "INDEPENDENT_VERIFICATION_UNROUTABLE",
                        "error": str(exc)[:500],
                    },
                )
        return None
    # Evidence arrived. The condition the recheck was watching is resolved, so
    # the observation is retired here rather than left to keep asking a settled
    # question — and a LATER unroutable episode on the same card then gets a
    # fresh timer and a fresh first alarm instead of being suppressed by the
    # retired one.
    _retire_unroutable_recheck(conn, subject_id, reason="evidence_present")
    # A comment saying what changed released the hold on a subject whose last
    # verdict is FAIL. The verifier child below is only claimable off a pending
    # subject with a pending verdict row (_EVIDENCE_READY_PARENT_SQL), so put the
    # subject in the state _release_verifier_after_repair produces.
    _release_failed_subject_for_verifier(conn, subject_id)
    # A mandatory verifier is a continuation of the already-admitted subject,
    # not fresh control-plane construction. Preserve structured operator
    # provenance so a human-authorized control-plane subject can open its
    # required independent verification route without weakening admission
    # control or inferring authority from the legacy free-text created_by.
    verifier_control_plane_authority = None
    subject_admission = conn.execute(
        "SELECT control_plane, control_plane_authority, actor_kind "
        "FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if subject_admission is not None and subject_admission["control_plane"]:
        inherited_authority = (
            str(subject_admission["control_plane_authority"] or "").strip() or None
        )
        if inherited_authority and inherited_authority.startswith("operator:"):
            verifier_control_plane_authority = inherited_authority
        elif subject_admission["actor_kind"] == ACTOR_KIND_HUMAN_INTERACTIVE:
            verifier_control_plane_authority = (
                f"operator:human-interactive-parent:{subject_id}"
            )
        elif inherited_authority and inherited_authority.startswith(
            CONTROL_PLANE_AUTHORITY_PREFIXES
        ):
            # Admitted under a named defect/regression/requirement: the
            # verifier is that admission's own mandatory check, not new
            # construction. Never operator:, so it delegates no operator power.
            verifier_control_plane_authority = (
                f"requirement:independent-verification:{subject_id}"
            )

    # A subject on a repair leg owes regression proof with its next PASS, and
    # only the verifier that re-ran the checks can hand it over (the return
    # path parses an anchored REGRESSION: line). The brief never said so, so an
    # honest PASS on a repair leg was refused and the next verifier spent
    # another objective attempt on the same unanswerable brief (2026-09-14).
    regression_brief = (
        "\n\nThis subject is on a repair leg: an earlier verdict failed. Before any "
        "PASS, rerun the relevant regression checks yourself, and justify the PASS "
        "with one anchored line beginning exactly `REGRESSION:` that states what you "
        f"reran and whether it passed, for example `{VERIFIER_REGRESSION_BRIEF_EXAMPLE}`. "
        "A PASS without that line is refused and the subject stays unverified."
        if regression_evidence_required(conn, subject_id) else ""
    )
    # create_task classifies the card from its title+body, so this system
    # boilerplate must not carry control-plane keywords for an ordinary
    # subject: "Gauntlet"/"control-plane" here made the verifier of ordinary
    # work look like new control-plane construction, admission refused it, and
    # the subject could never complete (134 refusals, 69 subjects,
    # 2026-10-04..15). The ACCEPTANCE instruction is kept exactly where the
    # verdict return path enforces it: control_plane_subject() of the subject.
    subject_row = conn.execute(
        "SELECT title, body FROM tasks WHERE id = ?", (subject_id,)
    ).fetchone()
    subject_needs_acceptance = bool(
        (subject_admission is not None and subject_admission["control_plane"])
        or (subject_row is not None
            and control_plane_subject(subject_row["title"], subject_row["body"]))
    )
    acceptance_brief = (
        "For control-plane work, a PASS must include an anchored "
        "`ACCEPTANCE: PASS` line confirming the stated acceptance criteria were "
        "checked; missing acceptance confirmation fails closed. "
        if subject_needs_acceptance else ""
    )
    # A verifier child is the mandatory check for ITS subject's Gauntlet
    # requirement -- the same fact the "existing" branch above already uses
    # to decide whether to raise the runtime cap. Compute it once and apply
    # it consistently to both knobs: a newly-created child for a
    # Gauntlet-enforced subject must itself be gauntlet_enforced, or the
    # verifier can complete with a bare PASS/FAIL summary instead of the
    # VERIFIED chain the subject's own enforcement demands. Previously this
    # call omitted ``gauntlet=`` entirely, so the new child fell back to
    # title/body classification -- "Independent verification: <id>" never
    # matches that classifier, so every freshly created verifier child for a
    # Gauntlet subject came up gauntlet_enforced=0 (observed on live board;
    # regression test: TestStaleDispositionActuator).
    verifier_gauntlet_required = gauntlet_required(conn, subject_id)
    try:
        child_id = create_task(
            conn,
            title=f"Independent verification: {subject_id}",
            body=(
                f"Independent verification of {subject_id}.\n\n"
                f"The implementer ({implementer or 'unknown'}) cannot verify "
                f"its own work, so this card carries the verdict instead.\n\n"
                f"Review the subject task body and its evidence against every explicit "
                f"requirement / acceptance criterion. {acceptance_brief}"
                f"Finish with `VERDICT: PASS`, `VERDICT: FAIL` or `VERDICT: BLOCKER` "
                f"on its own anchored line; the verdict is returned automatically."
                f"{regression_brief}"
            ),
            # Created directly on the chosen lane (see function docstring):
            # NOT the assignee="atlas" shorthand, which _normalize_shorthand_lane
            # only ever maps to EXECUTOR_LANE_CODEX_VERIFY. Passing executor_lane
            # explicitly lets `lane` (claude_verify or codex_verify, resolved
            # above) actually take effect; assignee="default" is the spawnable
            # carrier every direct-executor lane rides.
            assignee="default",
            executor_lane=lane,
            parents=[subject_id],
            created_by="kanban:request_review",
            # The verification gate creates this card, never a person. The env
            # fallback recorded it as human_interactive whenever the caller had
            # no governed-run env (the gateway dispatcher), which
            # is_human_relayed()/task_admission() read as delegated operator
            # authority (2026-10-15: 221 such cards in 21 days).
            provenance=ActorProvenance(
                kind=ACTOR_KIND_SYSTEM,
                actor_id="kanban:request_review",
                lane=None,
                run_id=None,
                cause=CREATION_CAUSE_AUTOMATED,
            ),
            gauntlet=verifier_gauntlet_required,
            max_runtime_seconds=(
                MANDATORY_OBSERVATION_INTERVAL_SECONDS
                if verifier_gauntlet_required
                else None
            ),
            control_plane_authority=verifier_control_plane_authority,
        )
    except Exception as exc:  # pragma: no cover - defensive
        with write_txn(conn):
            _append_event(
                conn, subject_id, "independent_verifier_child_failed",
                {"error": str(exc)[:500], "lane": lane},
            )
        return None
    try:
        add_task_relation(
            conn, child_id, subject_id, RELATION_VERIFIES,
            created_by="kanban:request_review",
        )
    except Exception as exc:  # pragma: no cover - defensive
        with write_txn(conn):
            _append_event(
                conn, subject_id, "independent_verifier_relation_failed",
                {"verifier_task": child_id, "error": str(exc)[:500]},
            )
        return None
    with write_txn(conn):
        _append_event(
            conn, subject_id, "independent_verifier_child_created",
            {
                "verifier_task": child_id,
                "lane": lane,
                "implementer": implementer,
            },
        )
    return child_id


def swap_verifier_lane_once(
    conn: sqlite3.Connection, verifier_task_id: str, *, reason: str,
) -> Optional[str]:
    """Deterministically swap one usage/rate-limited verifier to the other lane.

    Christopher's 2026-10-25 authorization (t_ab80ef2a): a ``claude_verify``
    attempt that hits a usage/rate limit falls back to ``codex_verify`` once,
    automatically -- and the reverse when Codex is the one that is
    unavailable. This is a deterministic, mechanical swap (one new sibling
    verifier card, never an LLM retry/judgment call) so the behaviour is the
    same every time regardless of which model is reasoning about it.

    Only swaps a card that is still open and has not already been swapped
    once. The "once" scope is the SUBJECT's whole fallback chain, not just
    this one verifier card: a durable ``verifier_lane_swap_dispatched`` event
    on the subject is the record that a fallback already happened somewhere
    in this chain, so a second capacity failure -- whether it recurs on the
    original card or lands on the sibling it already swapped to -- declines
    instead of swapping back and forth indefinitely. Returns the new sibling
    verifier task id, or ``None`` if no swap happened (already swapped
    anywhere in the chain, unknown lane, or the subject link is gone).
    """
    row = conn.execute(
        "SELECT executor_lane, status FROM tasks WHERE id = ?",
        (verifier_task_id,),
    ).fetchone()
    if row is None or row["executor_lane"] not in INDEPENDENT_VERIFIER_LANES:
        return None
    from_lane = row["executor_lane"]
    to_lane = (
        EXECUTOR_LANE_CODEX_VERIFY
        if from_lane == EXECUTOR_LANE_CLAUDE_VERIFY
        else EXECUTOR_LANE_CLAUDE_VERIFY
    )
    subjects = verifier_subject_ids(conn, verifier_task_id)
    if not subjects:
        return None
    # Chain-wide guard: once ANY verifier card for this subject has
    # dispatched a fallback sibling, decline every further swap attempt for
    # the subject, regardless of which card (original or sibling) hit the
    # next capacity failure. This is what makes the fallback a single
    # one-shot per subject rather than a per-card allowance that can
    # alternate Claude<->Codex indefinitely.
    already_swapped_in_chain = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'verifier_lane_swap_dispatched' LIMIT 1",
        (subjects[0],),
    ).fetchone()
    if already_swapped_in_chain is not None:
        return None
    implementer = None
    try:
        implementer = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (subjects[0],),
        ).fetchone()
        implementer = implementer["assignee"] if implementer else None
    except Exception:  # pragma: no cover - defensive
        implementer = None
    with write_txn(conn):
        if conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ? AND status NOT IN "
            "('done', 'archived')",
            (verifier_task_id,),
        ).rowcount:
            _append_event(
                conn, verifier_task_id, "verifier_lane_swapped",
                {"from_lane": from_lane, "to_lane": to_lane, "reason": reason[:500]},
            )
    new_child = _ensure_independent_verifier_child(
        conn, subjects[0], implementer=implementer, preferred_lane=to_lane,
    )
    with write_txn(conn):
        _append_event(
            conn, subjects[0], "verifier_lane_swap_dispatched",
            {
                "previous_verifier": verifier_task_id,
                "from_lane": from_lane,
                "to_lane": to_lane,
                "new_verifier": new_child,
                "reason": reason[:500],
            },
        )
    return new_child


VERIFIER_VERDICT_PASS = "PASS"
VERIFIER_VERDICT_FAIL = "FAIL"
VERIFIER_VERDICT_BLOCKER = "BLOCKER"
_VERIFIER_VERDICT_RE = re.compile(
    r"^[\s>*_-]*(?:\*\*|__)?(?:ATLAS_|CLAUDE_)?VERDICT(?:\*\*|__)?\s*[:=]\s*(?:\*\*|__)?"
    r"(PASS|FAIL|BLOCKER)\b",
    re.IGNORECASE | re.MULTILINE,
)
_VERIFIER_ACCEPTANCE_RE = re.compile(
    r"^[\s>*_-]*(?:\*\*|__)?ACCEPTANCE(?:\*\*|__)?\s*[:=]\s*(?:\*\*|__)?"
    r"(PASS|FAIL)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_verifier_acceptance(*texts: Optional[str]) -> Optional[str]:
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in _VERIFIER_ACCEPTANCE_RE.finditer(str(text)):
            found.add(match.group(1).upper())
    if len(found) != 1:
        return None
    return found.pop()


_VERIFIER_LESSON_RE = re.compile(
    r"^[\s>*_-]*(?:\*\*|__)?LESSON(?:\*\*|__)?\s*[:=]\s*(?:\*\*|__)?"
    r"(?P<detail>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_VERIFIER_LESSON_APPLICABILITY_RE = re.compile(
    r"^[\s>*_-]*(?:\*\*|__)?LESSON[ _-]?APPLICABILITY(?:\*\*|__)?"
    r"\s*[:=]\s*(?:\*\*|__)?"
    r"(?P<detail>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
VERIFIER_REGRESSION_BRIEF_EXAMPLE = (
    "REGRESSION: pytest -q tests/hermes_cli/test_example.py -> exit 0, 12 passed"
)
_VERIFIER_REGRESSION_RE = re.compile(
    r"^[\s>*_-]*(?:\*\*|__)?REGRESSION(?:[ _-]?EVIDENCE)?(?:\*\*|__)?"
    r"\s*[:=]\s*(?:\*\*|__)?"
    r"(?P<detail>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REGRESSION_CHECK_MAX_DEPTH = 4


def _names_a_concrete_check(value: Any, _depth: int = 0) -> bool:
    """True when ``value`` actually names a check or command that was re-run.

    The validator's contract is deliberately minimal — an evidence key merely
    has to be truthy — and truthiness is not the same question as "did the
    verifier name something". ``{"checks": true}`` is truthy and names nothing;
    so are ``{"checks": 1}`` and ``{"checks": {"result": "PASS"}}`` once the
    booleans are dressed up. Admission is therefore decided here, on the parser
    side, where an unfalsifiable declaration can still be discarded before it
    becomes a ledger row. The validator is left exactly as it was: every
    evidence dict built by hand or passed on the CLI is held to the same
    contract it always was.

    Concrete means a non-empty string somewhere the key points at — the name of
    a command, a suite, a test, a check. Containers are searched to
    ``_REGRESSION_CHECK_MAX_DEPTH``; ``bool``, ``None``, numbers and empty
    containers name nothing at any depth — ``True`` in particular is not a
    ``str``, so it falls through to the final ``False`` like any other bare
    scalar despite being truthy.
    """
    if isinstance(value, str):
        return bool(value.strip())
    if _depth >= _REGRESSION_CHECK_MAX_DEPTH:
        return False
    if isinstance(value, (list, tuple)):
        return any(_names_a_concrete_check(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        return any(
            _names_a_concrete_check(v, _depth + 1) for v in value.values()
        )
    return False


def _parse_verifier_lesson(*texts: Optional[str]) -> Optional[dict]:
    """Extract the lesson an independent verifier declared, if any.

    Declaration shape mirrors ``REGRESSION:`` because verifiers already write
    in it::

        LESSON: do not treat a provider 429 as an implementation failure
        LESSON_APPLICABILITY: assignee:coder

    **Fails closed.** Returns ``None`` unless BOTH lines are present and
    non-empty. A lesson without a selector cannot be matched against future
    work, and inferring one would be the model guessing at scope -- the exact
    thing ``normalize_lesson_applicability``'s closed grammar exists to
    prevent. A verifier that declares nothing produces nothing, as before.

    Note the ordering hazard this avoids: ``_VERIFIER_LESSON_RE`` would also
    match a ``LESSON_APPLICABILITY:`` line (``LESSON`` then ``_APPLICABILITY``
    swallowed by ``detail``), so applicability is matched first and its lines
    are excluded from the lesson search rather than relying on regex
    precedence.
    """
    lesson = ""
    applicability = ""
    for text in texts:
        if not text:
            continue
        body = str(text)
        if not applicability:
            m = _VERIFIER_LESSON_APPLICABILITY_RE.search(body)
            if m:
                applicability = m.group("detail").strip().strip("*_`")
        if not lesson:
            # Strip applicability declarations before looking for the lesson,
            # so the broader LESSON: pattern cannot capture one.
            stripped = _VERIFIER_LESSON_APPLICABILITY_RE.sub("", body)
            m = _VERIFIER_LESSON_RE.search(stripped)
            if m:
                lesson = m.group("detail").strip().strip("*_`")
    if not lesson or not applicability:
        return None
    return {
        LESSON_EVIDENCE_KEY: lesson,
        LESSON_APPLICABILITY_EVIDENCE_KEY: applicability,
    }


def _parse_verifier_regression(*texts: Optional[str]) -> Optional[dict]:
    """Extract the re-run proof an independent verifier declared, if any.

    Returns an evidence dict shaped for :func:`_validate_regression_evidence`,
    or ``None`` when the verifier declared nothing — in which case an armed
    subject's PASS is refused exactly as before. Fails closed; never invents a
    check that was not named.

    Two declaration shapes, because verifiers use both:

    * **A named command** — ``REGRESSION: pytest -q tests/foo.py -> exit 0``.
      Wrapped as ``{"commands": [...]}``.
    * **A JSON object** — ``REGRESSION_EVIDENCE: {"checks": [...]}``. Passed
      through as the evidence itself, so the verifier's own structure survives
      onto the ledger instead of being flattened into a quoted string.

    The gate is not weakened by accepting the second shape. A JSON object is
    held to :func:`_validate_regression_evidence` on its own merits, and one
    that names no check is DISCARDED rather than re-wrapped as a command — the
    re-wrap would be a laundering path, turning ``{"result": "PASS"}`` (a claim)
    into ``{"commands": ['{"result": "PASS"}']}`` (a fact it never was). A
    truncated or malformed object fails the same way, which is why anything
    starting with ``{`` is never treated as a command string.

    Admission is stricter than the validator on one point, and only here: an
    accepted evidence key must actually name a check
    (:func:`_names_a_concrete_check`). ``{"checks": true}`` clears the
    validator's minimal truthiness test while naming nothing anyone could
    re-run, and a verifier's free-text report is precisely where such a value
    arrives unreviewed. It is discarded like any other unfalsifiable claim.
    """
    commands: list[str] = []
    structured: dict = {}
    for text in texts:
        if not text:
            continue
        for match in _VERIFIER_REGRESSION_RE.finditer(str(text)):
            detail = match.group("detail").strip().strip("*_`")
            if not detail:
                continue
            if detail.startswith("{"):
                try:
                    parsed = json.loads(detail)
                except (ValueError, TypeError):
                    continue
                ok, _ = _validate_regression_evidence(parsed)
                if not ok:
                    continue
                if not any(
                    _names_a_concrete_check(parsed.get(k))
                    for k in REGRESSION_EVIDENCE_KEYS
                ):
                    continue
                for key, value in parsed.items():
                    if key not in structured:
                        structured[key] = value
                continue
            if detail not in commands:
                commands.append(detail)
    if structured:
        if commands:
            merged = dict(structured)
            merged.setdefault("commands", commands)
            return merged
        return structured
    if not commands:
        return None
    return {"commands": commands}


def _parse_verifier_verdict(*texts: Optional[str]) -> Optional[str]:
    """Extract the single verdict declared across ``texts``.

    Fails closed on purpose: no verdict line, or two lines that disagree,
    both return ``None``. Guessing which of two contradictory verdicts the
    verifier meant is exactly the kind of inference that must never bless a
    task, so an ambiguous report is routed for a human decision instead.
    """
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for match in _VERIFIER_VERDICT_RE.finditer(str(text)):
            found.add(match.group(1).upper())
    if len(found) != 1:
        return None
    return found.pop()


_VERIFIER_FAIL_DETAIL_CHARS = 6000


def _return_verifier_verdict_to_subjects(
    conn: sqlite3.Connection,
    verifier_task_id: str,
    *,
    summary: Optional[str],
    result: Optional[str],
) -> None:
    """Return an independent verifier's verdict to the subject(s) it verified.

    Called post-commit from :func:`complete_task` for ``codex_verify`` tasks
    only. Without this the lane was a one-way street: the verifier child could
    be dispatched (``_EVIDENCE_READY_PARENT_SQL`` made that possible) and could
    complete with a correct verdict, and the subject it verified never heard
    about it — it stayed parked at ``verification_state='pending'`` forever.

    Routing, all of it fail-closed:

    * ``PASS``    -> :func:`record_verification` (which still enforces verifier
                     independence and the regression gate; a refusal there
                     leaves the subject parked and emits its own event).
    * ``FAIL``    -> :func:`record_verification` with ``passed=False``, which
                     routes the subject back for repair.
    * ``BLOCKER`` -> no verdict is written. The subject cannot be blessed and
                     must not be silently failed either; it stays in the review
                     lane with a durable event + comment for its governor.
    * missing/ambiguous -> same as ``BLOCKER``, plus a distinct event naming
                     the parse failure so the gap is diagnosable.

    Never raises into the completion path: the verifier's own completion is
    already durable by the time this runs, and a return-path fault must not
    un-complete it.
    """
    try:
        row = conn.execute(
            "SELECT executor_lane FROM tasks WHERE id = ?", (verifier_task_id,),
        ).fetchone()
        if row is None or row["executor_lane"] not in INDEPENDENT_VERIFIER_LANES:
            return
        lane = row["executor_lane"]
        subjects = verifier_subject_ids(conn, verifier_task_id)
        if not subjects:
            return
        verdict = _parse_verifier_verdict(summary, result)
        verifier_identity = f"{lane}:{verifier_task_id}"
        # The lane label is not proof the lane ran: on 2026-09-14 (run 2817) an
        # unscoped agent claimed a codex_verify card. Only a completing run the
        # task's own independent-verifier lane attested may return a verdict.
        attested_run = _attested_verifier_run(conn, verifier_task_id, lane)
        for subject_id in subjects:
            srow = conn.execute(
                "SELECT status, verification_state, title, body FROM tasks WHERE id = ?",
                (subject_id,),
            ).fetchone()
            if srow is None:
                continue
            if srow["verification_state"] != VERIFICATION_PENDING:
                # Someone already rendered a verdict for this phase. Record
                # that the return path fired and stop — never overwrite a
                # verdict that is already on the ledger.
                with write_txn(conn):
                    _append_event(
                        conn, subject_id, "verifier_verdict_skipped",
                        {
                            "verifier_task": verifier_task_id,
                            "verdict": verdict,
                            "reason": "subject is not awaiting a verdict",
                            "verification_state": srow["verification_state"],
                        },
                    )
                continue
            if verdict in (VERIFIER_VERDICT_PASS, VERIFIER_VERDICT_FAIL) and attested_run is None:
                with write_txn(conn):
                    _append_event(
                        conn, subject_id, "verifier_verdict_unattested",
                        {
                            "verifier_task": verifier_task_id,
                            "verdict": verdict,
                            "reason": (
                                "completing run carries no codex_verifier_started "
                                "attestation from the codex_verify lane"
                            ),
                        },
                    )
                    add_comment(
                        conn, subject_id, "verifier-return-path",
                        f"Verifier card {verifier_task_id} reported {verdict}, but the "
                        "codex_verify lane did not execute its completing run. No "
                        "verdict was written; this task stays in verification.",
                    )
                continue
            if verdict in (VERIFIER_VERDICT_PASS, VERIFIER_VERDICT_FAIL):
                passed = verdict == VERIFIER_VERDICT_PASS
                if passed and control_plane_subject(srow["title"], srow["body"]):
                    acceptance = _parse_verifier_acceptance(summary, result)
                    if acceptance != "PASS":
                        with write_txn(conn):
                            _append_event(
                                conn, subject_id, "verification_acceptance_missing",
                                {
                                    "verifier_task": verifier_task_id,
                                    "verdict": verdict,
                                    "acceptance": acceptance,
                                    "reason": "control-plane PASS requires ACCEPTANCE: PASS",
                                },
                            )
                            add_comment(
                                conn, subject_id, "verifier-return-path",
                                f"Independent verifier {verifier_task_id} returned PASS without "
                                "`ACCEPTANCE: PASS`. Control-plane work remains pending verification."
                            )
                        continue
                # Only a PASS may carry it: record_verification refuses a
                # failing verdict that arrives with regression evidence, and
                # rightly — there is nothing to bless, so nothing to prove.
                regression = (
                    _parse_verifier_regression(summary, result)
                    if passed else None
                )
                # Same rule as regression evidence: only a PASS may carry a
                # lesson. A finding drawn from work that failed verification is
                # an observation, and promote_lesson would refuse it anyway.
                lesson_evidence = (
                    _parse_verifier_lesson(summary, result) if passed else None
                )
                ok, detail = record_verification(
                    conn,
                    subject_id,
                    passed=passed,
                    verifier=verifier_identity,
                    evidence={
                        "source": "codex_verify_return_path",
                        "verifier_task": verifier_task_id,
                        **(lesson_evidence or {}),
                    },
                    regression_evidence=regression,
                    reason=(
                        None if passed else
                        f"independent verifier {verifier_task_id} returned FAIL"
                    ),
                )
                with write_txn(conn):
                    _append_event(
                        conn, subject_id, "verifier_verdict_returned",
                        {
                            "verifier_task": verifier_task_id,
                            "verdict": verdict,
                            "recorded": bool(ok),
                            "detail": detail,
                            "regression_declared": bool(regression),
                        },
                    )
                    if not passed:
                        # record_verification keeps only the generic reason, so
                        # the next worker would never see WHY it failed. Carry
                        # the verifier's own bounded, redacted finding on the
                        # subject's comment thread, which worker context reads.
                        finding = redact_review_value((result or summary or "").strip())
                        if finding:
                            if len(finding) > _VERIFIER_FAIL_DETAIL_CHARS:
                                finding = (
                                    finding[:_VERIFIER_FAIL_DETAIL_CHARS]
                                    + "… [truncated]"
                                )
                            add_comment(
                                conn, subject_id, "verifier-return-path",
                                f"Independent verifier {verifier_task_id} returned "
                                f"FAIL. Verifier finding (verbatim, bounded):\n\n{finding}",
                            )
                # A successful independent PASS is the final authority needed
                # by a parked Gauntlet subject. Finalize it immediately through
                # the ordinary guarded completion path instead of requiring a
                # model/human to relay the already-durable verdict back into
                # complete_task(). FAIL deliberately does not finalize: the
                # record_verification failure path routes the subject to rework.
                if passed and ok and detail == VERIFICATION_VERIFIED:
                    completed = complete_task(
                        conn,
                        subject_id,
                        summary=(
                            f"Independent verifier {verifier_task_id} returned PASS; "
                            "subject finalized from the durable verified state."
                        ),
                    )
                    if not completed:
                        with write_txn(conn):
                            _append_event(
                                conn, subject_id, "verified_completion_deferred",
                                {
                                    "verifier_task": verifier_task_id,
                                    "reason": "guarded complete_task transition refused",
                                },
                            )
                continue
            # BLOCKER, or no parseable verdict at all.
            kind = (
                "verification_blocker_returned"
                if verdict == VERIFIER_VERDICT_BLOCKER
                else "verifier_verdict_unreadable"
            )
            note = (
                f"Independent verifier {verifier_task_id} reported "
                f"{verdict or 'no machine-readable verdict'}. No verdict was "
                f"written; this task stays in verification and needs a "
                f"governance decision."
            )
            with write_txn(conn):
                _append_event(
                    conn, subject_id, kind,
                    {
                        "verifier_task": verifier_task_id,
                        "verdict": verdict,
                    },
                )
                add_comment(conn, subject_id, "verifier-return-path", note)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning(
            "verifier verdict return failed for %s: %s", verifier_task_id, exc
        )


def _supervisory_routing() -> dict:
    """Resolve where a supervisory alarm is sent. Live config, never cached.

    ``kanban.supervisory_profile``       — the governing identity (Erika).
    ``kanban.executive_telegram_chat_id``— the executive channel, falling back
    to the already-configured ``kanban.approval_telegram_chat_id`` so this
    works on the current board with no config change. Thread/type follow the
    same fallback.

    An empty chat id is a supported configuration, not an error: the alarm
    still lands durably on the ledger and records that it had no transport.
    """
    supervisor = SUPERVISORY_PROFILE_DEFAULT
    chat_id = thread_id = ""
    chat_type = "dm"
    try:
        from hermes_cli.config import load_config

        kcfg = _kanban_cfg(load_config)
        supervisor = (
            str(kcfg.get("supervisory_profile") or "").strip()
            or SUPERVISORY_PROFILE_DEFAULT
        )
        chat_id = str(
            kcfg.get("executive_telegram_chat_id")
            or kcfg.get("approval_telegram_chat_id")
            or ""
        ).strip()
        thread_id = str(
            kcfg.get("executive_telegram_thread_id")
            or kcfg.get("approval_telegram_thread_id")
            or ""
        ).strip()
        chat_type = str(
            kcfg.get("executive_telegram_chat_type")
            or kcfg.get("approval_telegram_chat_type")
            or "dm"
        ).strip() or "dm"
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("supervisory routing config unavailable: %s", exc)
    return {
        "supervisor": supervisor,
        "chat_id": chat_id,
        "thread_id": thread_id or None,
        "chat_type": chat_type,
    }


def route_supervisory_alarm(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    kind: str,
    payload: dict,
) -> bool:
    """Emit one supervisory alarm and route it to Erika + the executive channel.

    Returns True when a delivery channel was armed, False when the alarm is
    durable-only (no executive channel configured, or subscribing failed).
    Either way the ``kind`` event IS on the ledger when this returns — that is
    the guarantee, and it is the one a governor can act on.

    Order matters and is not incidental: the subscription is created BEFORE the
    alarm event is appended. ``add_notify_sub`` snaps a new subscriber's cursor
    to the task's current ``MAX(task_events.id)``, so subscribing afterwards
    would mark the alarm itself as already-consumed and deliver nothing — the
    same ordering ``_ensure_approval_telegram_subscription`` depends on.

    **Writes no comment on the subject card.** ``add_comment`` emits a
    ``commented`` event, which counts as durable lifecycle progress, so an
    alarm that commented on the card it was alarming about would reset the very
    staleness clock that produced it and silence its own successor. The alarm
    speaks in events only.
    """
    route = _supervisory_routing()
    delivered = False
    if route["chat_id"]:
        try:
            add_notify_sub(
                conn,
                task_id=task_id,
                platform="telegram",
                chat_id=route["chat_id"],
                thread_id=route["thread_id"],
                chat_type=route["chat_type"],
                notifier_profile=route["supervisor"],
                delivery_mode="notify+wake",
            )
            delivered = True
        except Exception as exc:
            # A messaging outage must never cost the durable record, and must
            # never fail a dispatcher tick. Record that delivery was not armed
            # so "nobody was told" is visible instead of assumed.
            _log.warning(
                "supervisory alarm subscribe failed for %s (%s): %s",
                task_id, kind, exc,
            )
    with write_txn(conn):
        _append_event(
            conn, task_id, kind,
            {
                **payload,
                "supervisor": route["supervisor"],
                "executive_channel": bool(route["chat_id"]),
            },
        )
        _append_event(
            conn,
            task_id,
            (
                SUPERVISORY_ALARM_ROUTED_EVENT if delivered
                else SUPERVISORY_ALARM_UNDELIVERABLE_EVENT
            ),
            {
                "alarm": kind,
                "supervisor": route["supervisor"],
                "platform": "telegram" if delivered else None,
                "reason": (
                    None if delivered
                    else "no executive channel configured or subscribe failed"
                ),
            },
        )
    return delivered


def _already_alarmed(
    conn: sqlite3.Connection, task_id: str, kind: str
) -> bool:
    """Whether ``kind`` has ever been recorded on ``task_id``.

    Unlike :func:`_emit_once_this_phase` this is not phase-scoped. An orphan
    verifier has no verification phase of its own, and its defect is permanent
    — the card will never acquire a subject — so the correct cadence is once,
    ever, not once per phase.
    """
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
        (task_id, kind),
    ).fetchone() is not None


def _verifier_reported_verdict(
    conn: sqlite3.Connection, task_id: str
) -> tuple[Optional[str], Optional[str]]:
    """Best-effort ``(verdict, source)`` recovered from a finished verifier.

    Reads the same fields the live return path parses, so a stranded verdict is
    reported in the SAME vocabulary a delivered one would have been. ``None``
    means the verifier left no machine-readable verdict — itself a fact the
    supervisor needs, and not a reason to stay silent about the orphan.
    """
    row = conn.execute(
        "SELECT result FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    task_result = row["result"] if row is not None else None
    verdict = _parse_verifier_verdict(task_result)
    if verdict:
        return verdict, "tasks.result"
    run = conn.execute(
        "SELECT summary, error FROM task_runs WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if run is not None:
        verdict = _parse_verifier_verdict(run["summary"], run["error"])
        if verdict:
            return verdict, "task_runs"
    return None, None


_HISTORICAL_VERIFIER_TASK_REF_RE = re.compile(r"\bt_[0-9a-f]{8}\b")


def _historical_orphan_subject_candidate(
    conn: sqlite3.Connection, verifier_task_id: str
) -> Optional[str]:
    """Return one mechanically safe legacy subject candidate, else ``None``.

    This is a migration/reconciliation path for verifier cards created before
    the creation boundary required ``parents=[subject]``. It is intentionally
    stricter than ordinary routing:

    * the verifier must still have no dependency parents;
    * its immutable title must contain exactly one task id other than itself;
    * that task must still exist in ``review`` with a pending verification
      phase and no terminal disposition;
    * the pending phase must pre-date the verifier card. A verifier created
      before the current phase cannot be spent on a later phase.

    The title reference supplies identity. Timestamps are used only as a
    one-way freshness rejection after identity is already exact; equality
    fails closed. No body/result prose is parsed for subject identity.
    """
    if parent_ids(conn, verifier_task_id):
        return None
    row = conn.execute(
        "SELECT title, created_at FROM tasks WHERE id = ? AND executor_lane = ?",
        (verifier_task_id, EXECUTOR_LANE_CODEX_VERIFY),
    ).fetchone()
    if row is None:
        return None
    refs = [
        ref for ref in dict.fromkeys(
            _HISTORICAL_VERIFIER_TASK_REF_RE.findall(row["title"] or "")
        )
        if ref != verifier_task_id
    ]
    if len(refs) != 1:
        return None
    subject_id = refs[0]
    subject = conn.execute(
        "SELECT status, verification_state, terminal_disposition "
        "FROM tasks WHERE id = ?",
        (subject_id,),
    ).fetchone()
    if (
        subject is None
        or subject["status"] != "review"
        or subject["verification_state"] != VERIFICATION_PENDING
        or subject["terminal_disposition"] is not None
    ):
        return None
    phase = conn.execute(
        "SELECT id, created_at FROM task_verifications "
        "WHERE task_id = ? AND kind = ? AND state = ? "
        "ORDER BY id DESC LIMIT 1",
        (subject_id, LEDGER_KIND_VERDICT, VERIFICATION_PENDING),
    ).fetchone()
    if phase is None:
        return None
    # Strictly older: same-second ambiguity is deliberately unrecoverable.
    if int(phase["created_at"]) >= int(row["created_at"]):
        return None
    return subject_id


def _consume_historical_orphan_verdict(
    conn: sqlite3.Connection, verifier_task_id: str
) -> Optional[str]:
    """Spend a legacy orphan PASS/FAIL on its one safe pending subject.

    Returns the subject id when the verdict was durably consumed. The orphan
    verifier row itself is never rewritten and no dependency edge is invented
    retroactively; append-only events record the reconciliation.
    """
    subject_id = _historical_orphan_subject_candidate(conn, verifier_task_id)
    if subject_id is None:
        return None
    verdict, verdict_source = _verifier_reported_verdict(conn, verifier_task_id)
    if verdict not in (VERIFIER_VERDICT_PASS, VERIFIER_VERDICT_FAIL):
        return None
    passed = verdict == VERIFIER_VERDICT_PASS
    verifier_identity = f"{EXECUTOR_LANE_CODEX_VERIFY}:{verifier_task_id}"
    ok, detail = record_verification(
        conn,
        subject_id,
        passed=passed,
        verifier=verifier_identity,
        evidence={
            "source": "historical_orphan_verifier_reconciliation",
            "verifier_task": verifier_task_id,
            "verdict_source": verdict_source,
        },
        reason=(
            None if passed else
            f"historical orphan verifier {verifier_task_id} returned FAIL"
        ),
    )
    with write_txn(conn):
        _append_event(
            conn, subject_id, "orphan_verifier_verdict_consumed",
            {
                "verifier_task": verifier_task_id,
                "verdict": verdict,
                "verdict_source": verdict_source,
                "recorded": bool(ok),
                "detail": detail,
            },
        )
        _append_event(
            conn, verifier_task_id, "orphan_verifier_reconciled",
            {
                "subject": subject_id,
                "verdict": verdict,
                "verdict_source": verdict_source,
                "recorded": bool(ok),
            },
        )
    if not ok:
        return None
    if passed and detail == VERIFICATION_VERIFIED:
        completed = complete_task(
            conn,
            subject_id,
            summary=(
                f"Historical orphan verifier {verifier_task_id} returned PASS; "
                "subject finalized from the durable verified state."
            ),
        )
        if not completed:
            with write_txn(conn):
                _append_event(
                    conn, subject_id, "verified_completion_deferred",
                    {
                        "verifier_task": verifier_task_id,
                        "source": "historical_orphan_verifier_reconciliation",
                        "reason": "guarded complete_task transition refused",
                    },
                )
            return subject_id
    return subject_id


def reconcile_stranded_direct_codex_verdicts(
    conn: sqlite3.Connection,
) -> list[str]:
    """Recover completed caller-owned Codex verifier results stranded off-ledger.

    Recovery-lane verifier executions intentionally use ``route_task=False``
    because their caller owns the richer post-execution handoff. If that caller
    dies or times out after Codex finishes, the execution and its task-scoped
    verdict artifact remain durable while the subject can be left in
    triage/blocked with an unchanged pending verification phase.

    Consume only an exact, fail-closed match: Gauntlet subject with a pending
    phase and no live run; completed exit-0 ``codex.verify`` execution with
    ``route_task=0``; and a verdict artifact under the subject workspace that
    contains that exact execution id and ends with ATLAS_VERDICT PASS/FAIL.
    """
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, status, verification_state, current_run_id, workspace_path, "
        "       terminal_disposition "
        "FROM tasks WHERE gauntlet_enforced = 1 AND verification_state = ? "
        "AND status IN ('review','triage','blocked') AND current_run_id IS NULL "
        "AND terminal_disposition IS NULL",
        (VERIFICATION_PENDING,),
    ).fetchall()
    for row in rows:
        task_id = row["id"]
        workspace = row["workspace_path"]
        if not workspace:
            continue
        root = Path(workspace)
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        executions = conn.execute(
            "SELECT id, ended_at FROM executions WHERE task_id = ? "
            "AND command_class = 'codex.verify' AND status = 'completed' "
            "AND exit_code = 0 AND route_task = 0 "
            "ORDER BY ended_at DESC, created_at DESC",
            (task_id,),
        ).fetchall()
        consumed = False
        for execution in executions:
            execution_id = str(execution["id"])
            candidates = sorted(
                root_resolved.glob("CODEX-VERIFY-VERDICT*.md"),
                key=lambda x: x.stat().st_mtime if x.exists() else 0,
                reverse=True,
            )
            for artifact in candidates:
                try:
                    resolved = artifact.resolve(strict=True)
                    resolved.relative_to(root_resolved)
                    text = resolved.read_text(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    continue
                if execution_id not in text:
                    continue
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                verdict_line = next(
                    (ln for ln in reversed(lines)
                     if ln in ("ATLAS_VERDICT: PASS", "ATLAS_VERDICT: FAIL")),
                    None,
                )
                if verdict_line is None:
                    continue
                passed = verdict_line.endswith("PASS")

                # Restore only the verification lane. Do not clear the pending
                # phase, failure history, block history, or evidence.
                if row["status"] != "review":
                    with write_txn(conn):
                        cur = conn.execute(
                            "UPDATE tasks SET status = 'review', block_kind = NULL "
                            "WHERE id = ? AND status IN ('triage','blocked') "
                            "AND verification_state = ? AND current_run_id IS NULL "
                            "AND terminal_disposition IS NULL",
                            (task_id, VERIFICATION_PENDING),
                        )
                        if cur.rowcount != 1:
                            continue
                        _append_event(
                            conn, task_id, "verification_lane_restored",
                            {"execution_id": execution_id,
                             "source": "stranded_direct_codex_verdict"},
                        )

                evidence = {
                    "source": "stranded_direct_codex_verdict_reconciliation",
                    "execution_id": execution_id,
                    "command_class": "codex.verify",
                    "exit_code": 0,
                    "artifact": str(resolved),
                }
                ok, detail = record_verification(
                    conn,
                    task_id,
                    passed=passed,
                    verifier=f"codex_verify:{execution_id}",
                    evidence=evidence,
                    reason=(None if passed else
                            f"direct Codex verifier {execution_id} returned FAIL"),
                )
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "direct_codex_verdict_consumed",
                        {"execution_id": execution_id,
                         "verdict": "PASS" if passed else "FAIL",
                         "artifact": str(resolved),
                         "recorded": bool(ok),
                         "detail": detail},
                    )
                if not ok:
                    consumed = True
                    break
                if passed and detail == VERIFICATION_VERIFIED:
                    complete_task(
                        conn,
                        task_id,
                        summary=(
                            f"Recovered independent Codex PASS from execution "
                            f"{execution_id}; pending subject finalized through "
                            "the verification ledger."
                        ),
                    )
                reconciled.append(task_id)
                consumed = True
                break
            if consumed:
                break
    return reconciled


def sweep_orphan_verifiers(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[str]:
    """Emit ``ORPHAN_VERIFIER_RESULT`` for finished verifiers with no subject.

    The watchdog for the failure the creation guard now prevents going forward
    and cannot undo backwards. A ``codex_verify`` card with no parent has
    already run, already rendered a verdict, and already completed — and
    :func:`_return_verifier_verdict_to_subjects` returned early at
    ``if not subjects: return``, silently. The verdict exists and nothing
    consumed it.

    Scope is deliberately narrow:

    * Only FINISHED verifiers. An orphan still sitting in ``todo``/``blocked``
      has produced no result to strand, and alarming on it would report a
      creation defect as a lost verdict. The creation guard is what covers
      that case, at the boundary where it can still be refused.
    * Once per card, ever. The defect is permanent — the card cannot acquire a
      subject retroactively without rewriting history, which is forbidden — so
      a per-tick or per-window repeat would be pure noise.

    **Mutates no historical state.** No status change, no disposition, no
    counter, no relation is written on the verifier or on anything else. The
    only writes are the append-only alarm events and one comment ON THE
    VERIFIER ITSELF (a terminal card, so no staleness clock reads it) naming
    the stranded verdict for a human reader.

    Returns the verifier ids alarmed on THIS sweep.
    """
    now_ts = int(time.time()) if now is None else int(now)
    alarmed: list[str] = []
    for task_id in orphaned_verifier_tasks(conn):
        row = conn.execute(
            "SELECT status, terminal_disposition, completed_at, title "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            continue
        finished = (
            row["status"] in ("done", "archived")
            or row["terminal_disposition"] is not None
        )
        if not finished:
            continue
        if _already_alarmed(conn, task_id, "orphan_verifier_reconciled"):
            continue
        # Legacy cards created before verifier-parent linkage was mandatory can
        # still be reconciled when one exact, current review/pending subject is
        # recoverable from the verifier's immutable title. Consume that verdict
        # before escalating it as lost. Unsafe/ambiguous history remains alarm-only.
        if _consume_historical_orphan_verdict(conn, task_id) is not None:
            continue
        if _already_alarmed(conn, task_id, ORPHAN_VERIFIER_RESULT_EVENT):
            continue
        verdict, verdict_source = _verifier_reported_verdict(conn, task_id)
        route_supervisory_alarm(
            conn, task_id,
            kind=ORPHAN_VERIFIER_RESULT_EVENT,
            payload={
                "verifier_task": task_id,
                "verdict": verdict,
                "verdict_source": verdict_source,
                "status": row["status"],
                "terminal_disposition": row["terminal_disposition"],
                "completed_at": row["completed_at"],
                "detected_at": now_ts,
                "reason": (
                    "independent verifier finished with no subject linkage; "
                    "its verdict was never returned to any task"
                ),
            },
        )
        with contextlib.suppress(Exception):
            add_comment(
                conn, task_id, "orphan-verifier-watchdog",
                (
                    f"ORPHAN_VERIFIER_RESULT: this verifier finished with no "
                    f"subject linkage, so its verdict "
                    f"({verdict or 'no machine-readable verdict'}) was never "
                    f"returned to any task. Escalated to the governing "
                    f"supervisor for a routing decision. The card and its "
                    f"history are preserved unchanged."
                ),
            )
        alarmed.append(task_id)
    return alarmed


def _stall_timer(
    conn: sqlite3.Connection, task_id: str
) -> Optional[ObservationTimer]:
    """The card's live stall recheck timer, if one is armed."""
    for timer in task_observation_timers(
        conn, task_id, state=OBSERVATION_STATE_OBSERVING,
    ):
        if timer.kind == LIFECYCLE_STALL_TIMER_KIND:
            return timer
    return None


def clear_resolved_unroutable_rechecks(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[str]:
    """Retire UNROUTABLE rechecks on cards that are inactive or gone.

    The INACTIVE half of criterion (D)'s handling for this alarm. The ACTIVE
    half lives in :func:`_ensure_independent_verifier_child`, which retires the
    timer the moment evidence appears or a verifier child opens — that is the
    call site that learns the condition resolved, so that is where it is
    recorded.

    This sweep covers the case that call site can never see: a card that goes
    terminal, or is deleted, while still unroutable. Nothing will call
    ``_ensure_independent_verifier_child`` for it again, so without this pass
    the observation would sit ``observing`` forever — a timer outliving its
    condition, which is the exact failure shape the observation-timer contract
    was built to prevent.

    Returns the task ids whose UNROUTABLE recheck was retired on this pass.
    """
    now_ts = int(time.time()) if now is None else int(now)
    cleared: list[str] = []
    rows = conn.execute(
        "SELECT task_id FROM observation_timers WHERE state = ? AND kind = ? "
        "ORDER BY created_at, id",
        (OBSERVATION_STATE_OBSERVING, UNROUTABLE_VERIFICATION_TIMER_KIND),
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        timer = _unroutable_recheck_timer(conn, task_id)
        if timer is None:
            continue
        trow = conn.execute(
            "SELECT status, terminal_disposition FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if trow is None:
            close_observation_timer(
                conn, timer.id, reason="task_gone", now=now_ts,
            )
            cleared.append(task_id)
            continue
        if (
            trow["status"] in ("done", "archived")
            or trow["terminal_disposition"] is not None
        ):
            close_observation_timer(
                conn, timer.id, reason="subject_inactive", now=now_ts,
            )
            cleared.append(task_id)
    return cleared


def clear_resolved_validation_loop_rechecks(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[str]:
    """Retire validation-loop rechecks once their repair card is terminal or gone."""
    now_ts = int(time.time()) if now is None else int(now)
    cleared: list[str] = []
    rows = conn.execute(
        "SELECT id, task_id FROM observation_timers WHERE state = ? AND kind = ? "
        "ORDER BY created_at, id",
        (OBSERVATION_STATE_OBSERVING, VALIDATION_LOOP_TIMER_KIND),
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        trow = conn.execute(
            "SELECT status, terminal_disposition FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if trow is None:
            close_observation_timer(
                conn, row["id"], reason="task_gone", now=now_ts,
            )
            cleared.append(task_id)
            continue
        if (
            trow["status"] in ("done", "archived")
            or trow["terminal_disposition"] is not None
        ):
            close_observation_timer(
                conn, row["id"], reason="subject_inactive", now=now_ts,
            )
            cleared.append(task_id)
    return cleared


def clear_resolved_lifecycle_stalls(
    conn: sqlite3.Connection, *, now: Optional[int] = None
) -> list[str]:
    """Retire stall rechecks on cards that are moving again, or are inactive.

    The ACTIVE/INACTIVE half of the alarm. A 10-minute recheck that outlives
    the condition it was watching is worse than no recheck: it keeps asking a
    settled question, and a supervisor who learns to ignore it will also ignore
    the one that matters.

    Two ways a stall resolves, and they are not the same fact:

    * **INACTIVE** — the card reached a terminal state (``done``/``archived``,
      or an irreversible disposition). There is no longer anything to make
      progress on. The alarm is closed as satisfied.
    * **ACTIVE** — durable lifecycle progress happened after the alarm fired:
      the card's last meaningful event is newer than the one the alarm was
      keyed on. Somebody or something picked it up. The alarm is closed as
      resumed.

    Runs BEFORE the alarm pass on each tick, so a card that resumed and then
    went stale again gets a fresh episode rather than being suppressed by its
    own retired timer.

    Returns the task ids whose stall alarm was cleared on this pass.
    """
    now_ts = int(time.time()) if now is None else int(now)
    cleared: list[str] = []
    rows = conn.execute(
        "SELECT task_id FROM observation_timers WHERE state = ? AND kind = ? "
        "ORDER BY created_at, id",
        (OBSERVATION_STATE_OBSERVING, LIFECYCLE_STALL_TIMER_KIND),
    ).fetchall()
    for row in rows:
        task_id = row["task_id"]
        trow = conn.execute(
            "SELECT status, terminal_disposition FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        timer = _stall_timer(conn, task_id)
        if timer is None:
            continue
        if trow is None:
            close_observation_timer(
                conn, timer.id, reason="task_gone", now=now_ts,
            )
            cleared.append(task_id)
            continue
        inactive = (
            trow["status"] in ("done", "archived")
            or trow["terminal_disposition"] is not None
        )
        resumed = False
        if not inactive:
            alarm = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                (task_id, NO_LIFECYCLE_PROGRESS_EVENT),
            ).fetchone()
            if alarm is not None:
                latest = _last_meaningful_event(conn, task_id)
                try:
                    keyed = (json.loads(alarm["payload"]) or {}).get(
                        "last_event_id"
                    )
                except (json.JSONDecodeError, TypeError):
                    keyed = None
                # Strictly newer. Equality means the alarm is still keyed on
                # the same episode and nothing has moved.
                resumed = (
                    latest is not None
                    and keyed is not None
                    and int(latest["id"]) > int(keyed)
                )
        if not (inactive or resumed):
            continue
        reason = "task_inactive" if inactive else "progress_resumed"
        close_observation_timer(conn, timer.id, reason=reason, now=now_ts)
        with write_txn(conn):
            _append_event(
                conn, task_id, LIFECYCLE_STALL_CLEARED_EVENT,
                {
                    "timer_id": timer.id,
                    "resolution": reason,
                    "lifecycle": "inactive" if inactive else "active",
                    "status": trow["status"],
                    "terminal_disposition": trow["terminal_disposition"],
                    "cleared_at": now_ts,
                },
            )
        cleared.append(task_id)
    return cleared


def _infrastructure_release_withheld(
    conn: sqlite3.Connection,
    task_id: str,
    block_event_id: int,
    execution: sqlite3.Row,
    now_ts: int,
) -> Optional[str]:
    """Why an infrastructure park must stay parked, or ``None`` to release it."""
    if now_ts - int(execution["ended_at"]) > INFRASTRUCTURE_AUTO_RELEASE_WINDOW_SECONDS:
        return "retry_window_expired"
    for row in conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'commented' AND id > ?",
        (task_id, block_event_id),
    ):
        try:
            author = str(json.loads(row["payload"] or "{}").get("author") or "")
        except Exception:
            author = ""
        if not (author.endswith("-lane") or author in AUTOMATION_COMMENT_AUTHORS):
            return "owner_engaged"
    return None


def _record_infrastructure_release_withheld(
    conn: sqlite3.Connection,
    task_id: str,
    execution: sqlite3.Row,
    reason: str,
    now_ts: int,
) -> None:
    """One record per parked execution; the card itself is not touched."""
    for row in conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'infrastructure_release_withheld'",
        (task_id,),
    ):
        try:
            if json.loads(row["payload"] or "{}").get("execution_id") == execution["id"]:
                return
        except Exception:
            continue
    with write_txn(conn):
        _append_event(
            conn,
            task_id,
            "infrastructure_release_withheld",
            {
                "reason": reason,
                "execution_id": execution["id"],
                "execution_status": execution["status"],
                "parked_seconds": now_ts - int(execution["ended_at"]),
                "detected_at": now_ts,
                "source": "stale_supervision",
            },
        )


def resolve_stale_gauntlet_dispositions(
    conn: sqlite3.Connection,
    stale: Iterable[GauntletStaleTask],
    *,
    now: Optional[int] = None,
) -> dict[str, list[str]]:
    """Act on stale Gauntlet cards where the next transition is deterministic.

    Detection without disposition is not recovery. Historically the stale
    sweep could emit NO_LIFECYCLE_PROGRESS every observation interval for days
    while a review-pending subject already had sufficient evidence for an
    independent verifier.

    This actuator is deliberately narrow and fail-closed. It only performs a
    transition already authorized by durable state: review + pending asks the
    canonical independent-verifier helper to return/create the verifier child.
    The helper still requires dispatchable evidence and preserves independence.

    Everything else is classified unresolved and left untouched.
    """
    now_ts = int(time.time()) if now is None else int(now)
    routed: list[str] = []
    reconciled: list[str] = []
    infrastructure_released: list[str] = []
    self_review_released: list[str] = []
    verifier_stuck_alarmed: list[str] = []
    unresolved: list[str] = []

    for entry in stale:
        task_id = entry.task_id
        state_row = conn.execute(
            "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        repair = None
        if (
            entry.status == "blocked"
            and state_row is not None
            and state_row["block_kind"] == "needs_input"
            and entry.last_event_id is not None
        ):
            # Exact episode binding: add_task_relation() writes a structured
            # relation_received event on the subject. The repair is authorized
            # to retire only the episode for which THAT event is still the
            # stale detector's latest meaningful event. Event IDs, not clocks,
            # make same-second reopen/reblock races fail closed.
            relation_event = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'relation_received' AND id = ?",
                (task_id, int(entry.last_event_id)),
            ).fetchone()
            if relation_event is not None:
                try:
                    relation_payload = json.loads(relation_event["payload"] or "{}")
                except Exception:
                    relation_payload = {}
                repair_id = relation_payload.get("from")
                if (
                    relation_payload.get("to") == task_id
                    and relation_payload.get("relation") == "repairs"
                    and isinstance(repair_id, str)
                    and repair_id
                ):
                    repair = conn.execute(
                        "SELECT id FROM tasks WHERE id = ? AND status = 'done' "
                        "AND verification_state = ? "
                        "AND terminal_disposition = 'completed' "
                        "AND completed_at IS NOT NULL",
                        (repair_id, VERIFICATION_VERIFIED),
                    ).fetchone()
        if repair is not None:
            repair_id = repair["id"]
            ok, _reason = reconcile_overtaken_by_events(
                conn,
                task_id,
                actor="stale-supervision",
                reason=(
                    "stale card was superseded by a newer directly-linked "
                    "repair that reached done + verified + completed"
                ),
                evidence=(
                    f"task_relations relation=repairs from {repair_id}; "
                    f"{repair_id} is durably done/verified/completed"
                ),
                superseded_by=[repair_id],
            )
            if ok:
                reconciled.append(task_id)
                continue

        # Infrastructure termination is not human input. Older executor
        # routing parked timed-out/stale/terminated runs as needs_input, which
        # made the stale observer re-alert forever even though no owner
        # decision was required. The executions ledger is the structured
        # authority here; do not infer this class from comments or summaries.
        if (
            entry.status == "blocked"
            and state_row is not None
            and state_row["block_kind"] == "needs_input"
        ):
            current_block = conn.execute(
                "SELECT id, run_id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            block_payload = {}
            if current_block is not None:
                try:
                    block_payload = json.loads(current_block["payload"] or "{}")
                except Exception:
                    block_payload = {}
            linked_execution_id = block_payload.get("execution_id")
            execution = None
            if (
                isinstance(linked_execution_id, str)
                and linked_execution_id
                and block_payload.get("failure_class") == "infrastructure"
            ):
                execution = conn.execute(
                    "SELECT id, status, ended_at FROM executions "
                    "WHERE id = ? AND task_id = ? AND ended_at IS NOT NULL LIMIT 1",
                    (linked_execution_id, task_id),
                ).fetchone()
            withheld = None
            if (
                execution is not None
                and execution["status"] in ("timed_out", "stale", "terminated")
            ):
                withheld = _infrastructure_release_withheld(
                    conn, task_id, int(current_block["id"]), execution, now_ts,
                )
            if withheld is not None:
                _record_infrastructure_release_withheld(
                    conn, task_id, execution, withheld, now_ts,
                )
            elif (
                execution is not None
                and execution["status"] in ("timed_out", "stale", "terminated")
            ):
                if unblock_task(conn, task_id):
                    with write_txn(conn):
                        _append_event(
                            conn,
                            task_id,
                            "gauntlet_stale_disposition",
                            {
                                "action": "infrastructure_recovery_released",
                                "execution_id": execution["id"],
                                "execution_status": execution["status"],
                                "detected_at": now_ts,
                                "source": "stale_supervision",
                            },
                        )
                    infrastructure_released.append(task_id)

                    # If the safe resume phase is REVIEW/PENDING, spend the
                    # same deterministic opportunity to release its independent
                    # verifier. Evidence gating remains inside the canonical
                    # helper and therefore still fails closed.
                    resumed = conn.execute(
                        "SELECT status, verification_state, assignee FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                    if (
                        resumed is not None
                        and resumed["status"] == "review"
                        and resumed["verification_state"] == VERIFICATION_PENDING
                    ):
                        implementer = _review_requested_implementer(
                            conn, task_id, fallback_assignee=resumed["assignee"],
                        )
                        child_id = _ensure_independent_verifier_child(
                            conn, task_id, implementer=implementer,
                        )
                        if child_id is not None:
                            recompute_ready(conn)
                            routed.append(task_id)
                    continue

        # A same-identity review refusal is a routing requirement, not owner
        # input. Older review flows correctly rejected self-review but then
        # parked the subject in BLOCKED, leaving the independent-verifier path
        # unopened forever. Release only the current pending review phase, and
        # only when the block is mechanically tied to that self-review event.
        if (
            entry.status == "blocked"
            and entry.verification_state == VERIFICATION_PENDING
            and _subject_evidence_is_dispatchable(conn, task_id)
        ):
            phase = _current_verification_phase(conn, task_id)
            self_review = conn.execute(
                "SELECT id, run_id, payload FROM task_events "
                "WHERE task_id = ? AND kind IN "
                "('verification_blocked_self_review', 'review_claim_rejected_self_review') "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            latest_block = conn.execute(
                "SELECT id, run_id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()

            # Bind self-review recovery to the exact CURRENT review episode.
            # Timestamps are not authority: a historical self-review refusal
            # can share the same second (or five-minute window) as a later
            # phase. The durable chain is event/run identity:
            # current phase's review_requested -> reviewer claimed from review
            # -> self-review refusal -> block on that SAME reviewer run.
            review_requested = None
            if phase is not None and phase[1] is not None:
                review_requested = conn.execute(
                    "SELECT id, run_id FROM task_events "
                    "WHERE task_id = ? AND kind = 'review_requested' AND run_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, int(phase[1])),
                ).fetchone()
            review_claim = None
            if self_review is not None and self_review["run_id"] is not None:
                review_claim = conn.execute(
                    "SELECT id, run_id, payload FROM task_events "
                    "WHERE task_id = ? AND kind = 'claimed' AND run_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (task_id, int(self_review["run_id"])),
                ).fetchone()
            try:
                block_payload = json.loads(
                    latest_block["payload"] or "{}"
                ) if latest_block is not None else {}
            except Exception:
                block_payload = {}
            try:
                claim_payload = json.loads(
                    review_claim["payload"] or "{}"
                ) if review_claim is not None else {}
            except Exception:
                claim_payload = {}

            exact_self_review_episode = bool(
                phase is not None
                and review_requested is not None
                and self_review is not None
                and self_review["run_id"] is not None
                and review_claim is not None
                and latest_block is not None
                and latest_block["run_id"] is not None
                and int(review_claim["run_id"]) == int(self_review["run_id"])
                and int(latest_block["run_id"]) == int(self_review["run_id"])
                and int(review_claim["id"]) > int(review_requested["id"])
                and int(self_review["id"]) > int(review_claim["id"])
                and int(latest_block["id"]) > int(self_review["id"])
                and claim_payload.get("source_status") == "review"
                and block_payload.get("source_status") == "review"
            )
            if exact_self_review_episode:
                if unblock_task(conn, task_id):
                    resumed = conn.execute(
                        "SELECT status, verification_state, assignee FROM tasks WHERE id = ?",
                        (task_id,),
                    ).fetchone()
                    if (
                        resumed is not None
                        and resumed["status"] == "review"
                        and resumed["verification_state"] == VERIFICATION_PENDING
                    ):
                        implementer = _review_requested_implementer(
                            conn, task_id, fallback_assignee=resumed["assignee"],
                        )
                        child_id = _ensure_independent_verifier_child(
                            conn, task_id, implementer=implementer,
                        )
                        if child_id is not None:
                            recompute_ready(conn)
                            with write_txn(conn):
                                _append_event(
                                    conn,
                                    task_id,
                                    "gauntlet_stale_disposition",
                                    {
                                        "action": "self_review_routed_independent",
                                        "verifier_task": child_id,
                                        "self_review_event_id": self_review["id"],
                                        "detected_at": now_ts,
                                        "source": "stale_supervision",
                                    },
                                )
                            self_review_released.append(task_id)
                            routed.append(task_id)
                            continue

        if (
            entry.status == "review"
            and entry.verification_state == VERIFICATION_PENDING
        ):
            # A blocked verifier child already open for this leg is an
            # existing verification episode, not an absence of one. Minting
            # another would be unreachable anyway (creation runs through the
            # same evidence-dispatchable gate the blocked child already
            # passed), so the correct disposition is to surface the stuck
            # child once, not multiply it. Checked BEFORE
            # _ensure_independent_verifier_child, which only ever sees LIVE
            # children via _open_verifier_child and would otherwise mint a
            # fresh one on every stale tick (t_ab80ef2a: 22 children in
            # ~2 hours, one every 5-6 minutes). Deterministic, no LLM retry.
            stuck_verifier = _blocked_verifier_child(conn, task_id)
            if stuck_verifier is not None:
                if _verifier_stuck_alarm_is_owed(conn, task_id, stuck_verifier):
                    with write_txn(conn):
                        _append_event(
                            conn,
                            task_id,
                            "gauntlet_stale_disposition",
                            {
                                "action": "verifier_stuck_surfaced",
                                "verifier_task": stuck_verifier,
                                "detected_at": now_ts,
                                "source": "stale_supervision",
                                "detail": (
                                    "an independent verifier child is "
                                    "already open for this verification leg "
                                    "but is blocked; no new verifier was "
                                    "created -- unblock or repair the "
                                    "existing one"
                                ),
                            },
                        )
                    verifier_stuck_alarmed.append(task_id)
                unresolved.append(task_id)
                continue
            row = conn.execute(
                "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            fallback = row["assignee"] if row is not None else None
            implementer = _review_requested_implementer(
                conn, task_id, fallback_assignee=fallback,
            )
            child_id = _ensure_independent_verifier_child(
                conn, task_id, implementer=implementer,
            )
            if child_id is not None:
                recompute_ready(conn)
                with write_txn(conn):
                    _append_event(
                        conn,
                        task_id,
                        "gauntlet_stale_disposition",
                        {
                            "action": "independent_verifier_routed",
                            "verifier_task": child_id,
                            "detected_at": now_ts,
                            "source": "stale_supervision",
                        },
                    )
                routed.append(task_id)
                continue
        unresolved.append(task_id)

    return {
        "reconciled": reconciled,
        "infrastructure_released": infrastructure_released,
        "self_review_released": self_review_released,
        "verifier_routed": routed,
        "verifier_stuck_alarmed": verifier_stuck_alarmed,
        "unresolved": unresolved,
    }


def sweep_lifecycle_stall_alarms(
    conn: sqlite3.Connection,
    stale: Iterable[GauntletStaleTask],
    *,
    now: Optional[int] = None,
) -> list[str]:
    """Route this tick's stale-Gauntlet findings to Erika and the executive.

    Consumes the result of :func:`detect_stale_gauntlet_work` rather than
    re-deriving staleness, so there is exactly one definition of "no lifecycle
    progress" on the board and exactly one anti-spam policy (that scan's
    per-episode / ``realert_seconds`` de-duplication) governing how often a
    supervisor hears about the same parked card.

    For each finding this pass:

    1. Emits ``NO_LIFECYCLE_PROGRESS`` and routes it to the governing
       supervisor and the configured executive channel.
    2. Arms a 300-second observation timer (:data:`LIFECYCLE_STALL_TIMER_KIND`)
       so the alarm is re-evidenced every ten minutes for as long as the card
       stays stuck, instead of being one message that scrolls away.
       :func:`clear_resolved_lifecycle_stalls` retires it when the card moves
       or goes terminal.

    Arming is best-effort and never blocks the alarm: an ownerless card cannot
    carry a timer (``arm_observation_timer`` fails closed on exactly that), and
    an unowned stalled card is precisely one a supervisor must still hear
    about. The refusal is recorded on the alarm's own event rather than
    swallowed.

    **Changes no workflow state.** Nothing is completed, verified, requeued,
    reassigned or failed here, for the same reason the scan it consumes does
    not: a watchdog has no evidence, and manufacturing a transition would
    defeat the chain it is watching.

    Returns the task ids alarmed on THIS sweep.
    """
    now_ts = int(time.time()) if now is None else int(now)
    alarmed: list[str] = []
    for entry in stale:
        task_id = entry.task_id
        timer_id: Optional[str] = None
        timer_error: Optional[str] = None
        if _stall_timer(conn, task_id) is None:
            try:
                timer_id = arm_observation_timer(
                    conn, task_id,
                    kind=LIFECYCLE_STALL_TIMER_KIND,
                    now=now_ts,
                ).id
            except Exception as exc:
                timer_error = f"{type(exc).__name__}: {exc}"
                _log.warning(
                    "lifecycle stall recheck could not be armed on %s: %s",
                    task_id, exc,
                )
        else:
            timer_error = None
        route_supervisory_alarm(
            conn, task_id,
            kind=NO_LIFECYCLE_PROGRESS_EVENT,
            payload={
                "age_seconds": entry.age_seconds,
                "status": entry.status,
                "verification_state": entry.verification_state,
                "regression_required": entry.regression_required,
                "reasons": entry.reasons,
                "last_event_id": entry.last_event_id,
                "last_event_at": entry.last_event_at,
                "last_event_kind": entry.last_event_kind,
                "detected_at": now_ts,
                "recheck_timer_id": timer_id,
                "recheck_interval_seconds": (
                    MANDATORY_OBSERVATION_INTERVAL_SECONDS
                    if timer_id else None
                ),
                "recheck_error": timer_error,
            },
        )
        alarmed.append(task_id)
    return alarmed


class EmptyCompletionError(ValueError):
    """``complete_task`` refused: no substantive ``result``, ``summary``, or
    stored result. A ``ValueError`` so tool error handlers treat it as
    recoverable. Review approvals are exempt (the human is the record)."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} has no result or summary evidence"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


def _production_board_path() -> Path:
    """The live production board. Resolved from the account's real home
    (not ``HOME``/``HERMES_HOME``), so a test that redirects either can never
    resolve to it."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".hermes" / "kanban.db"


def _production_ledger_sync_allowed(conn: sqlite3.Connection) -> bool:
    """Only the live production board may project into the live ledger.

    Incident 2026-10-12: every test run created tasks on a temp board and
    POSTed them to the live dashboard (127.0.0.1:5130). 69,750 open rows
    for tasks that never existed on any real board drove Erika's trust
    score to 0% from at least 2026-10-09. Fails closed: any doubt means no
    projection.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        db_file = next((row[2] for row in conn.execute("PRAGMA database_list")
                        if row[1] == "main"), "")
        if not db_file:
            return False
        return os.path.realpath(db_file) == os.path.realpath(_production_board_path())
    except Exception:
        return False


def _sync_production_ledger(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    phase: str,
    summary: Optional[str] = None,
    result: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    run_id: Optional[int] = None,
) -> None:
    """Best-effort projection of a Kanban lifecycle transition into the
    PostgreSQL operational ledger consumed by MAPS/dispatcher/verifier.

    North Caledonia control-plane projection. PostgreSQL is the authoritative
    operational ledger; Kanban is the executor queue. **Never raises.**
    Kanban's own transition (already committed by the time any caller
    reaches this helper) is durable and MUST NOT be reverted or reported as
    failed merely because the projection to Postgres hiccups (app.py
    restart, transient network blip, request timeout). Per Gauntlet
    doctrine, infrastructure/transport failures are handled separately from
    implementation failure and must never masquerade as a failed
    kanban_claim/kanban_request_review/kanban_request_changes/kanban_unblock
    — the caller already has a valid, durable transition. A sync failure is
    recorded as a first-class ``production_ledger_sync_failed`` audit event
    so it is visible and reconcilable instead of either silently dropped or
    incorrectly surfaced as a transition failure.

    ``phase`` names the calling transition (``claim``, ``claim_review``,
    ``request_review``, ``request_changes``, ``unblock``,
    ``review_reopened``, ``complete``, ``block``, ``create``) purely for the
    audit trail; it does not change behaviour. This is the single choke
    point every lifecycle-mutating function in this module calls so that
    create/claim/request_review/request_changes/unblock/complete/block all
    share one authoritative projection path instead of only the terminal
    transitions (see t_faf6e26a / t_da07457d — the prior fix covered only
    create/complete/block, leaving claim/request_review/request_changes/
    unblock unsynchronized, which is exactly the gap this closes).
    """
    if not _production_ledger_sync_allowed(conn):
        return
    try:
        task_row = get_task(conn, task_id)
    except Exception:
        task_row = None
    try:
        import urllib.request as _urlreq
        _payload = json.dumps({
            "task_id": task_id,
            "title": task_row.title if task_row else task_id,
            "assignee": task_row.assignee if task_row else "",
            "status": status,
            "summary": summary or "",
            "result": result or "",
            "blocked_reason": blocked_reason or "",
        }).encode("utf-8")
        _req = _urlreq.Request(
            "http://127.0.0.1:5130/internal/kanban-sync", data=_payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with _urlreq.urlopen(_req, timeout=5) as _resp:
            if not json.loads(_resp.read().decode("utf-8")).get("ok"):
                raise RuntimeError("production ledger rejected transition")
    except Exception as _sync_exc:
        _log.warning(
            "production ledger sync failed for task %s (phase=%s, status=%s): %s",
            task_id, phase, status, _sync_exc,
        )
        try:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "production_ledger_sync_failed",
                    {"phase": phase, "status": status, "error": str(_sync_exc)},
                    run_id=run_id,
                )
        except Exception:
            _log.debug(
                "could not record production_ledger_sync_failed event for %s",
                task_id,
            )


class LiveClaimError(ValueError):
    """``complete_task`` refused: the task is ``running`` under a live claim and
    the caller neither owns its run (``expected_run_id``) nor passed ``force``.
    Completing anyway would close the worker's run row underneath a process
    that is still executing. A ``ValueError`` so tool error handlers treat it
    as recoverable."""

    def __init__(self, task_id: str):
        super().__init__(
            f"{task_id} is running under a live worker claim; pass expected_run_id "
            "(worker ownership) or force=True (explicit operator override) instead "
            "of closing the live run"
        )


def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim still protects a run: the worker process
    it spawned exists (PID + start-time fingerprint). A claim whose worker is gone,
    or a library/CLI claim that never spawned one, has no run to protect. TTL expiry
    is deliberately not consulted: ``reclaim_stale_tasks`` extends, not reclaims, the
    claim of a live worker, so the process is the liveness authority here too."""
    return bool(
        trow["status"] == "running"
        and trow["claim_lock"] is not None
        and trow["worker_pid"]
        and _worker_alive(trow["worker_pid"], trow["worker_started_at"])
    )


def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None, expected_run_id: Optional[int] = None,
    fire_lifecycle_hook: bool = True, force: bool = False,
    regression_evidence: Optional[dict] = None,
) -> bool:
    """``running|ready|blocked|review -> done``; records ``result``.

    ``ready`` is accepted for manual CLI completion, ``review`` for human
    approval. A ``running`` task under a live claim is only completed with
    proof of ownership (``expected_run_id``) or ``force=True`` (explicit
    operator override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. With no active run the handoff fields survive via
    :func:`_synthesize_ended_run`. ``summary`` (defaults to ``result``) and
    ``metadata`` land on the closing run for :func:`build_worker_context`.
    ``created_cards`` are verified first — a phantom id raises
    :class:`HallucinatedCardsError` after an auditable event; afterwards the
    prose is scanned for unresolvable ``t_<hex>`` refs (advisory event only).
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.

    ``regression_evidence`` is only consulted on the reviewer-run approval
    path (a run claimed from ``review`` approving inline, which is how the
    agent review lane records its verdict). When that task owes a regression
    proof for a material repair, the evidence is recorded on the ledger and
    spent by the verdict this call writes; without it the approval is refused
    exactly like an unverified completion.
    """
    now = int(time.time())
    # Cheap pre-check; re-checked inside the txn to close the parent-reopen race.
    if not _parents_satisfied(conn, task_id):
        return False

    # Gate: the executor's own admission. A completion whose summary/result
    # says independent verification is still owed is refused whatever the
    # task's enforcement flag says, and the task is escalated to
    # gauntlet_enforced so the refusal holds on every later attempt too. The
    # VerificationRequiredError is what every executor wrapper already turns
    # into request_review, so the card lands in review, never done.
    # Not applied to a reviewer run's approval, a verifier card's report, or a
    # task already carrying a VERIFIED verdict: there the phrase describes
    # someone else's work or a requirement already met.
    if explicit_verification_requirement(summary, result):
        arow = conn.execute(
            "SELECT status, executor_lane, gauntlet_enforced, verification_state "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            arow is not None
            and arow["executor_lane"] not in INDEPENDENT_VERIFIER_LANES
            and arow["verification_state"] != VERIFICATION_VERIFIED
            and _review_run_verification(conn, task_id)[0] is None
        ):
            with write_txn(conn):
                if not arow["gauntlet_enforced"]:
                    conn.execute(
                        "UPDATE tasks SET gauntlet_enforced = 1 WHERE id = ?",
                        (task_id,),
                    )
                _append_event(
                    conn, task_id, "completion_blocked_self_reported_unverified",
                    {
                        "status": arow["status"],
                        "verification_state": arow["verification_state"],
                        "gauntlet_escalated": not arow["gauntlet_enforced"],
                        # First non-blank line of either field: the admission
                        # may sit only in result behind a blank summary.
                        "summary_preview": next(
                            (
                                line.strip()[:200]
                                for line in f"{summary or ''}\n{result or ''}".splitlines()
                                if line.strip()
                            ),
                            None,
                        ),
                    },
                )
            raise VerificationRequiredError(
                task_id, arow["status"], arow["verification_state"]
            )

    # Gate: the Gauntlet chain. A gauntlet-enforced task reaches 'done' only
    # from a VERIFIED verdict, so a bare running -> done completion claim by
    # the executor is refused here with a diagnosis, and refused again as a
    # SQL predicate on the transition itself further down. This pre-check
    # exists for the error message and the audit event; the predicate is what
    # makes it mechanical.
    require_verified = gauntlet_required(conn, task_id)
    if require_verified:
        integrated, integration_detail = _gauntlet_worktree_integrated(conn, task_id)
        if not integrated:
            with write_txn(conn):
                _append_event(conn, task_id, "completion_blocked_not_integrated", {
                    "canonical_ref": gauntlet_canonical_ref(),
                    "detail": integration_detail,
                })
            raise VerificationRequiredError(
                task_id, "integration_pending", None,
                regression_detail=integration_detail,
            )
        vrow = conn.execute(
            "SELECT status, verification_state FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if vrow is not None and vrow["verification_state"] != VERIFICATION_VERIFIED:
            review_run_id, _reviewer = _review_run_verification(conn, task_id)
            # A reviewer run may approve inline (see _review_run_verification);
            # anything else is the executor claiming its own work complete.
            if review_run_id is None:
                with write_txn(conn):
                    _append_event(
                        conn, task_id, "completion_blocked_unverified",
                        {
                            "status": vrow["status"],
                            "verification_state": vrow["verification_state"],
                            "summary_preview": (
                                (summary or result or "").strip().splitlines()[0][:200]
                                if (summary or result)
                                else None
                            ),
                        },
                    )
                raise VerificationRequiredError(
                    task_id, vrow["status"], vrow["verification_state"]
                )
            # Reviewer run approving a material repair: the inline verdict is
            # only legal if the regression proof it owes is available. Checked
            # here read-only (nothing is recorded on a refusal but the audit
            # event) and again, authoritatively, inside the write txn.
            if regression_evidence_required(conn, task_id):
                if regression_evidence is not None:
                    # Shape-check the supplied proof here so a malformed one is
                    # a named refusal rather than a bare False from the UPDATE.
                    ok_reg, reg_detail = _validate_regression_evidence(
                        regression_evidence
                    )
                else:
                    ok_reg, reg_detail, _ = _regression_gate(conn, task_id)
                if not ok_reg:
                    with write_txn(conn):
                        _append_event(
                            conn, task_id, "completion_blocked_no_regression",
                            {
                                "status": vrow["status"],
                                "verification_state": vrow["verification_state"],
                                "detail": reg_detail,
                                "run_id": review_run_id,
                            },
                            run_id=review_run_id,
                        )
                    raise VerificationRequiredError(
                        task_id, vrow["status"], vrow["verification_state"],
                        regression_detail=reg_detail,
                    )

    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _gate_created_cards(conn, task_id, created_cards, summary or result)
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return False
    with write_txn(conn):
        # Hard invariant even for human review approval: a parent may have
        # reopened while this task waited.
        if not _parents_satisfied(conn, task_id):
            return False
        if acceptance is not None and not record_acceptance(conn, task_id, acceptance):
            return False
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        # Re-decide enforcement inside the write txn — the config could have
        # been flipped, or the task re-flagged, since the pre-check above.
        require_verified = gauntlet_required(conn, task_id)
        if require_verified:
            integrated, _integration_detail = _gauntlet_worktree_integrated(conn, task_id)
            if not integrated:
                return False
            head = conn.execute(
                "SELECT verification_state FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if head is not None and head["verification_state"] != VERIFICATION_VERIFIED:
                review_run_id, reviewer = _review_run_verification(conn, task_id)
                if review_run_id is None:
                    # Lost the race with a re-claim/reopen, or the pre-check
                    # was bypassed entirely. Refuse without mutating.
                    return False
                # Verifier identity (D4). This implicit approval path derives
                # "reviewer" purely from which RUN claimed the review lane, not
                # from WHO is acting — the same profile can stay its own
                # assignee across the whole phase (a governance profile that
                # never reassigns, e.g. t_7311348b/t_aba8b8d8) and this path
                # would otherwise record a "VERIFIED" verdict for its own
                # decision with no identity check at all, unlike the explicit
                # `record_verification` CLI path. It carries the same three
                # gates that path does. ``return False`` (not raise) so the
                # audit event still commits — this function returns normally on
                # refusal elsewhere in this block too, only raising pre-write
                # gate errors it wants the caller to see as
                # ``VerificationRequiredError``.
                reviewer = _canonical_assignee(reviewer) if reviewer else None
                # (b) An unnamed reviewer means the review run has no assignee
                # at all. Nothing about that verdict is attributable; fail
                # closed rather than blessing it as "not the implementer".
                if not reviewer:
                    _append_event(
                        conn,
                        task_id,
                        "verification_blocked_unknown_verifier",
                        {
                            "verifier": None,
                            "detail": "review run has no assignee identity",
                            "source": "review_run_approval",
                        },
                        run_id=review_run_id,
                    )
                    return False
                # (a) The reviewer identity must resolve against the profile
                # registry or the verifier-lane allow-list.
                identity_status, identity_detail = _verifier_identity_status(
                    reviewer, conn=conn, subject_id=task_id,
                )
                if identity_status == VERIFIER_IDENTITY_UNKNOWN:
                    _append_event(
                        conn,
                        task_id,
                        "verification_blocked_unknown_verifier",
                        {
                            "verifier": reviewer,
                            "detail": identity_detail,
                            "source": "review_run_approval",
                        },
                        run_id=review_run_id,
                    )
                    return False
                if identity_status == VERIFIER_IDENTITY_UNCHECKED:
                    _append_event(
                        conn,
                        task_id,
                        "verifier_identity_unchecked",
                        {
                            "verifier": reviewer,
                            "detail": identity_detail,
                            "source": "review_run_approval",
                        },
                        run_id=review_run_id,
                    )
                # (c) Independence against the implementer AND the task's own
                # assignee. On this path the reviewer IS ``tasks.assignee`` by
                # construction, so the assignee arm only bites when nothing
                # auditable ever put that identity in the reviewer seat — see
                # ``_phase_reviewer_identities``.
                implementer = _review_requested_implementer(conn, task_id, reviewer)
                head_assignee = conn.execute(
                    "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
                ).fetchone()
                conflict = _verifier_independence_conflict(
                    conn, task_id, reviewer,
                    implementer=implementer,
                    assignee=head_assignee["assignee"] if head_assignee else None,
                )
                if conflict is not None:
                    conflict_identity, conflict_source = conflict
                    _append_event(
                        conn,
                        task_id,
                        "verification_blocked_self_review",
                        {
                            "verifier": reviewer,
                            "implementer": implementer,
                            "conflict_identity": conflict_identity,
                            "conflict_source": conflict_source,
                        },
                        run_id=review_run_id,
                    )
                    return False
                # Reviewer approving from inside their own review run: record
                # the verdict atomically with the completion it authorises, so
                # the ledger can never show a 'done' with nothing behind it.
                # A material repair owes its regression proof first — recorded
                # in this same transaction, so proof and verdict are one write.
                ok_reg, reg_detail, proof_id = _regression_gate(
                    conn, task_id,
                    evidence=regression_evidence,
                    verifier=reviewer,
                    now=now,
                )
                if not ok_reg:
                    # Armed between the pre-check and here, or the pre-check
                    # was bypassed. Refuse without mutating: the rollback of
                    # this txn also discards anything the gate wrote.
                    return False
                verdict_evidence: dict[str, Any] = {
                    "source": "review_run_approval",
                    "run_id": review_run_id,
                }
                if proof_id is not None:
                    verdict_evidence["regression_proof_id"] = proof_id
                _record_verification_row(
                    conn, task_id,
                    state=VERIFICATION_VERIFIED,
                    run_id=review_run_id,
                    verifier=reviewer,
                    evidence=verdict_evidence,
                    reason="approved by reviewer run",
                    now=now,
                )
                if proof_id is not None:
                    _spend_regression_requirement(conn, task_id)
                _append_event(
                    conn, task_id, "verification_passed",
                    {
                        "verifier": reviewer,
                        "source": "review_run_approval",
                        "regression_proof_id": proof_id,
                    },
                    run_id=review_run_id,
                )
        verified_guard = 1 if require_verified else 0
        # Refuse to close a LIVE worker's run without proof of ownership
        # (expected_run_id) or an explicit human override (force=True); see
        # _claim_is_live for what "live" means.
        if expected_run_id is None and not force and trow and _claim_is_live(trow):
            raise LiveClaimError(task_id)
        sql = """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL,
                       block_kind   = NULL,
                       block_recurrences = 0,
                       -- Positively assert "this executor did the work",
                       -- instead of leaving reporting to infer it from
                       -- status='done'. This is the other half of the
                       -- distinction 'overtaken_by_events' draws.
                       terminal_disposition = ?,
                       disposition_at = ?
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                   -- Gauntlet: EXECUTING -> COMPLETED is unreachable unless a
                   -- VERIFIED verdict is already materialised on the row, and
                   -- a repair still owing regression evidence is unreachable
                   -- even with one (only a PASS that spent a live proof
                   -- clears regression_required).
                   AND (? = 0 OR (verification_state = 'verified'
                                  AND regression_required = 0))
                   -- A card whose objective was satisfied elsewhere can
                   -- never be completed as if this executor had done it.
                   -- Re-completing a reopened 'completed' card is fine and
                   -- just re-stamps the disposition.
                   AND """ + _not_irreversibly_disposed_sql() + """
                """
        params: tuple = (result, now, DISPOSITION_COMPLETED, now, task_id, verified_guard)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        if isinstance(metadata, dict):
            _stage_completion_artifacts(conn, task_id, metadata, now)
        run_id = _end_run(
            conn, task_id, outcome="completed", status="done", summary=handoff_summary,
            metadata=metadata,
        )
        # Never-claimed task: synthesize a run so the handoff fields survive.
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata,
            )
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        _append_event(
            conn, task_id, "completed",
            _completed_event_payload(result, event_summary, verified_cards, metadata),
            run_id=run_id,
        )
    _flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    # An independent verifier's completion is only half the transaction: the
    # subject it verified has to hear the verdict. Post-commit, in its own
    # txn(s), so the verifier's own completion is durable first and a fault in
    # the return path can never roll it back. No-op for every other task.
    _return_verifier_verdict_to_subjects(
        conn, task_id, summary=summary, result=result,
    )
    # Success wipes the breaker counter (history stays on the event log).
    _clear_failure_counter(conn, task_id)
    # If this completed task itself ``repairs`` one or more subjects (minted
    # by create_repair_task — t_bf453ced), capture each subject's pre-recompute
    # status so we can tell, after the shared recompute below runs, whether
    # THIS completion is what resumed it. No separate scheduler: the subject
    # resumes through the exact same recompute_ready() every other dependent
    # uses; this only narrates the outcome it already produced.
    _repaired_subjects = relation_targets(conn, task_id, RELATION_REPAIRS)
    _repair_pre_status: dict[str, str] = {}
    for _subj_id in _repaired_subjects:
        _subj_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (_subj_id,),
        ).fetchone()
        if _subj_row is not None:
            _repair_pre_status[_subj_id] = _subj_row["status"]
    # Recompute ready status for dependents (separate txn so children see done).
    # If this completion released work, wake the gateway dispatcher immediately
    # instead of making the child wait up to dispatch_interval_seconds.
    promoted_dependents = recompute_ready(conn)
    if promoted_dependents:
        signal_dispatcher_wake()
    # Narrate a resumed subject: only when recompute_ready just moved it off
    # its gated wait (todo/blocked -> ready/review). A subject with another
    # unresolved parent/gate stays put and gets no notice — nothing resumed.
    for _subj_id, _prior_status in _repair_pre_status.items():
        if _prior_status not in ("todo", "blocked"):
            continue
        _subj_row_after = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (_subj_id,),
        ).fetchone()
        if _subj_row_after is None:
            continue
        _resumed_status = _subj_row_after["status"]
        if _resumed_status == _prior_status:
            continue
        with write_txn(conn):
            _append_event(
                conn, _subj_id, "repair_completed_subject_resumed",
                {
                    "repair_id": task_id,
                    "subject_id": _subj_id,
                    "resumed_status": _resumed_status,
                },
            )
    # Kernel part 3 (t_6450901b): a repair of a subject that FAILED
    # verification releases one new verifier when it records a change, or
    # starts the next repair when it recorded none / the same one.
    for _subj_id in _repaired_subjects:
        _release_verifier_after_repair(
            conn, _subj_id, repair_id=task_id,
            change_text=summary or result or "",
        )
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    # North Caledonia control-plane projection. PostgreSQL is the authoritative
    # operational ledger; Kanban is the executor queue. Best-effort: Kanban's
    # own completion (above) is already durable and MUST NOT be reverted or
    # reported as failed merely because the projection to Postgres hiccups
    # (app.py restart, transient network blip, request timeout). Per Gauntlet
    # doctrine, infrastructure/transport failures are handled separately from
    # implementation failure and must never masquerade as a failed
    # kanban_complete — the caller already has a valid, terminal, durable
    # result. A sync failure is recorded as a first-class audit event so it
    # is visible and reconcilable, instead of either silently dropped or
    # incorrectly surfaced as "completion failed".
    if not _production_ledger_sync_allowed(conn):
        return True
    try:
        import urllib.request as _urlreq
        _payload = json.dumps({
            "task_id": task_id,
            "title": _done_task.title if _done_task else task_id,
            "assignee": _done_task.assignee if _done_task else "",
            "status": "done",
            "summary": summary or "",
            "result": result or "",
        }).encode("utf-8")
        _req = _urlreq.Request(
            "http://127.0.0.1:5130/internal/kanban-sync", data=_payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with _urlreq.urlopen(_req, timeout=5) as _resp:
            if not json.loads(_resp.read().decode("utf-8")).get("ok"):
                raise RuntimeError("production ledger rejected completion")
    except Exception as _sync_exc:
        _log.warning(
            "production ledger sync failed for completed task %s: %s",
            task_id, _sync_exc,
        )
        try:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "production_ledger_sync_failed",
                    {"phase": "complete", "error": str(_sync_exc)},
                    run_id=run_id,
                )
        except Exception:
            _log.debug(
                "could not record production_ledger_sync_failed event for %s",
                task_id,
            )
    return True


_REVIEW_APPROVED_NOTE = "Review approved without additional evidence."


def _gate_created_cards(
    conn: sqlite3.Connection, task_id: str, created_cards: Optional[Iterable[str]], preview_text: Optional[str],
) -> list[str]:
    """Verify ``created_cards`` BEFORE the main write txn; returns the verified
    ids. A phantom id is recorded in its own tiny txn (auditable) then raised
    as :class:`HallucinatedCardsError` without touching task state."""
    if not created_cards:
        return []
    verified_cards, phantom_cards = _verify_created_cards(conn, task_id, created_cards)
    if phantom_cards:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_hallucination",
                {
                    "phantom_cards": phantom_cards,
                    "verified_cards": verified_cards,
                    "summary_preview": _first_line(preview_text, 200) or None,
                },
            )
        raise HallucinatedCardsError(phantom_cards, task_id)
    return verified_cards


def _substantive_text(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip())


def _gate_empty_completion(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
) -> None:
    """Refuse a completion that would leave the card with no evidence.

    Review approvals are exempt: a human vouches for the card and
    ``_REVIEW_APPROVED_NOTE`` is the documented record.
    """
    row = conn.execute(
        "SELECT status, result FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return
    if row["status"] == "review":
        return
    stored = row["result"]
    if _substantive_text(result) or _substantive_text(summary) or _substantive_text(stored):
        return
    with write_txn(conn):
        _append_event(
            conn, task_id, "completion_blocked_empty_result",
            {
                "result_preview": _first_line(result, 200) or None,
                "summary_preview": _first_line(summary, 200) or None,
            },
        )
    raise EmptyCompletionError(task_id)


def _stage_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict, now: int, *,
    uploaded_by: str = "kanban_complete",
) -> list[Path]:
    """Copy scratch artifacts to the attachments dir and record each as an
    attachment row; returns the copies so the caller can discard them if its
    transaction rolls back."""
    _persist_scratch_completion_artifacts(conn, task_id, metadata)
    staged = [Path(stored_path) for stored_path in metadata.pop("_staged_artifacts", [])]
    for path in staged:
        _insert_completion_attachment(
            conn, task_id, filename=path.name, stored_path=str(path),
            size=path.stat().st_size, created_at=now, uploaded_by=uploaded_by,
        )
    return staged


def _cleaned_artifact_paths(metadata: Any) -> list[str]:
    """Non-blank string paths declared in ``metadata["artifacts"]``."""
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("artifacts")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()]


def _completed_event_payload(
    result: Optional[str], event_summary: Optional[str], verified_cards: list[str], metadata: Any,
) -> dict:
    """``completed`` event payload: first summary line (400 chars) so gateway
    notifiers / dashboard WS render without a second round-trip; verified
    cards; and ``metadata["artifacts"]`` promoted so the notifier can upload
    them as native attachments without fetching the run row."""
    # Mirror CLI's _show_voice_status: include STT/TTS provider availability so the user can tell at a
    # glance *why* voice mode isn't working ("STT provider: MISSING ..." is the common case). ``record_key``
    # mirrors the configured ``voice.record_key`` so the TUI can both bind it (frontend
    # ``isVoiceToggleKey``) and display it in /voice status — previously the TUI hardcoded Ctrl+B and
    # ignored the config (#18994).
    payload: dict = {
        "result_len": len(result) if result else 0,
        "summary": _first_line(event_summary, 400) or None,
    }
    if verified_cards:
        payload["verified_cards"] = verified_cards
    if isinstance(metadata, dict):
        cleaned = _cleaned_artifact_paths(metadata)
        if cleaned:
            payload["artifacts"] = cleaned
    return payload


def _flag_phantom_prose_refs(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
    summary: Optional[str], result: Optional[str], verified_cards: list[str],
) -> None:
    """Advisory post-commit scan of summary+result for unresolvable ``t_<hex>``
    references; emits ``suspected_hallucinated_references`` in its own txn so
    the completion is already durable. Never blocks."""
    scan_text = " ".join(filter(None, [summary, result]))
    if not scan_text:
        return
    phantom_refs = [p for p in _scan_prose_for_phantom_ids(conn, scan_text) if p not in set(verified_cards)]
    if phantom_refs:
        with write_txn(conn):
            _append_event(
                conn, task_id, "suspected_hallucinated_references",
                {"phantom_refs": phantom_refs, "source": "completion_summary"}, run_id=run_id,
            )


def _merge_completion_prose_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: Optional[dict], *, summary: Optional[str],
    result: Optional[str],
) -> Optional[dict]:
    """Legacy workers named deliverables only by absolute path in prose; add
    those that exist under the scratch workspace to ``metadata["artifacts"]``
    before cleanup can erase them."""
    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return metadata
    if not _is_managed_scratch_path(workspace):
        return metadata
    text = "\n".join(part for part in (summary, result) if part)
    if not text:
        return metadata
    prefix = re.escape(str(workspace))
    discovered: list[str] = []
    for match in re.finditer(prefix + r"(?:[/\\][^\s`\"'<>]+)", text):
        raw = match.group(0).rstrip(".,;:!?)]}")
        candidate = Path(raw)
        if candidate.is_file():
            discovered.append(str(candidate))
    if not discovered:
        return metadata
    updated = dict(metadata) if isinstance(metadata, dict) else {}
    existing = updated.get("artifacts")
    merged = list(existing) if isinstance(existing, (list, tuple)) else []
    seen = {str(path) for path in merged}
    for path in discovered:
        if path not in seen:
            merged.append(path)
            seen.add(path)
    updated["artifacts"] = merged
    return updated


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        _discard_staged_copies(used_destinations, attachment_dir)

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise ArtifactPreservationError(problem)

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_capped(resolved_src, dest, artifact)
        except Exception as exc:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _discard_staged_copies(copies: Iterable[Path], attachment_dir: Path) -> None:
    """Remove staged attachment copies whose DB rows never committed; a leaked
    copy would make the retry stage ``name_1.ext`` next to an orphan."""
    for copied in copies:
        with contextlib.suppress(OSError):
            Path(copied).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        attachment_dir.rmdir()


def _copy_capped(src: Path, dest: Path, artifact: str) -> None:
    """Chunked copy that aborts if the file grows past the attachment cap mid-copy."""
    with src.open("rb") as source_file, dest.open("xb") as destination_file:
        copied = 0
        while chunk := source_file.read(1024 * 1024):
            copied += len(chunk)
            if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                raise ArtifactPreservationError(
                    f"declared scratch artifact grew beyond the size limit: {artifact}"
                )
            destination_file.write(chunk)


def _insert_completion_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str, size: int,
    created_at: int, uploaded_by: str = "kanban_complete",
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, filename, stored_path, size, uploaded_by, created_at),
    )
    _append_event(conn, task_id, "attached", {"filename": filename, "size": size, "by": uploaded_by})


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    stem, suffix = Path(safe_name).stem or "artifact", Path(safe_name).suffix
    candidate = directory / safe_name
    idx = 1
    while candidate in used or candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def _ensure_approval_telegram_subscription(conn: sqlite3.Connection, task_id: str) -> None:
    """Best-effort owner Telegram subscription for approval-required blocks.

    Called before the ``blocked`` event is appended so ``add_notify_sub``'s
    caught-up cursor points to the prior event, making the new approval event
    immediately deliverable. Empty config is an intentional no-op.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        kcfg = cfg.get("kanban") or {}
        chat_id = str(kcfg.get("approval_telegram_chat_id") or "").strip()
        if not chat_id:
            return
        thread_id = str(kcfg.get("approval_telegram_thread_id") or "").strip() or None
        chat_type = str(kcfg.get("approval_telegram_chat_type") or "group").strip() or "group"
        notifier_profile = str(kcfg.get("approval_notifier_profile") or "overall_manager").strip() or "overall_manager"
        add_notify_sub(
            conn, task_id=task_id,
            platform="telegram", chat_id=chat_id, thread_id=thread_id,
            chat_type=chat_type, notifier_profile=notifier_profile,
            delivery_mode="notify+wake",
        )
    except Exception as exc:
        # Approval state itself must remain durable even if notification setup
        # is temporarily unavailable. Existing notifier/diagnostics can repair
        # delivery; never fail the block transition for a messaging outage.
        _log.warning("approval Telegram auto-subscribe failed for %s: %s", task_id, exc)


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None,
) -> bool:
    """Edit task fields, optionally backfilling a completed task's result."""
    changed_fields = [
        field for field, value in (("title", title), ("body", body), ("priority", priority))
        if value is not None
    ]
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        assignments = []
        params = []
        for field, value in (("title", title), ("body", body), ("priority", priority)):
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if result is not None:
            assignments.append("result = ?")
            params.append(result)
            changed_fields.append("result")
        if not assignments:
            return False
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
            (*params, task_id),
        )
        if priority is not None:
            _append_event(conn, task_id, "reprioritized", {"priority": priority})
        if result is None:
            non_priority_fields = [field for field in changed_fields if field != "priority"]
            if non_priority_fields:
                _append_event(conn, task_id, "edited", {"fields": non_priority_fields})
        else:
            handoff_summary = summary if summary is not None else result
            changed_fields.append("summary")
            if metadata is not None:
                changed_fields.append("metadata")
            run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
            if run is None:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
                )
            else:
                run_id = int(run["id"])
                conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
                if metadata is not None:
                    conn.execute(
                        "UPDATE task_runs SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), run_id),
                    )
            _append_event(
                conn, task_id, "edited",
                {
                    "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                    "result_len": len(result) if result else 0,
                    "summary": _first_line(handoff_summary, 400) or None,
                },
                run_id=run_id,
            )
    notify_task_updated(conn, task_id, changed_fields, board=board)
    return True


def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """``running``/``ready`` -> ``blocked`` (or ``todo`` / ``triage``, see
    :func:`_route_block`). ``kind='dependency'`` with no incomplete parent is
    re-kinded to ``needs_input`` (sticky) so ``recompute_ready`` cannot
    promote it into a context-free respawn. ``transient`` still counts
    toward the loop breaker so a forever-flaky task escalates. True on any
    transition.

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.

    ``approval_required`` subscribes the approval Telegram route BEFORE the
    block event is emitted so the request itself is not consumed. A
    ``dependency`` report always *holds* (never rolls back) the effects the
    run already applied, for the replacement run to adopt. An
    infrastructure-class block (``event_payload_extra["failure_class"] ==
    "infrastructure"``: timeout, stale, lost controller) and a wait on the
    yt_source delivery queue are not the work re-blocking for the same cause
    and never count toward the loop breaker.
    """
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    if kind == "approval_required":
        # Subscribe BEFORE the approval event is emitted. add_notify_sub starts
        # its cursor at the current max event id, so subscribing afterward would
        # incorrectly mark the approval request itself as already consumed.
        pre = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if pre is not None and pre["status"] in {"running", "ready"}:
            _ensure_approval_telegram_subscription(conn, task_id)
    with write_txn(conn):
        cur_row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if cur_row is None:
            return False
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return False
            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1 "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, task_id),
            ).rowcount
            if classified != 1:
                return False
            _append_event(conn, task_id, "blocked", {
                "kind": kind, "reason": reason, "classified_in_place": True,
            })
            return True
        source_status = _retry_status_for_run(conn, task_id) if cur_row["status"] == "running" else "ready"
        requested_kind = kind
        rekind_reason = None
        # ``dependency`` only waits on incomplete parents. A worker filing that
        # kind with none open would park in ``todo`` and ``recompute_ready``
        # would promote+respawn it context-free on the next tick. Re-kind to
        # ``needs_input`` so it is sticky until a human unblocks.
        if kind == "dependency" and _parents_satisfied(conn, task_id):
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        prev_recurrences = int(_row_get(cur_row, "block_recurrences") or 0)
        new_status, event_kind, set_sql, params, payload = _route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=prev_recurrences,
        )
        # A dependency report (genuine wait, or an orphan re-kinded above)
        # holds the run's already-applied effects for the replacement run to
        # adopt; they are never rolled back.
        effect_policy = "hold" if requested_kind == "dependency" else None
        if new_status != "todo":
            # An infrastructure-class block (the control plane ended the
            # executor — timeout, stale, lost controller) is not the work
            # re-blocking for the same cause, and must never count toward the
            # loop. On 2026-09-14 an executor timeout (t_3883034a event 151138)
            # was counted as recurrence 1, so the agent's first real blocker
            # (151172) hit the limit and moved a subject awaiting verification
            # into triage, where nothing resumes it.
            # A worker parked on the yt_source delivery queue re-blocks after
            # each failed capture + re-request. Each is a fresh wait on an
            # external queue, not the same loop; counting it sent t_56f5852d
            # to triage on 2026-10-26 with the transcript arriving 1 minute
            # later.
            if (
                (event_payload_extra or {}).get("failure_class") == "infrastructure"
                or (reason or "").startswith(YT_SOURCE_WAIT_PREFIX)
            ):
                recurrences = prev_recurrences
                params = (kind, recurrences)
                payload["recurrences"] = recurrences
                if recurrences >= BLOCK_RECURRENCE_LIMIT:
                    new_status, event_kind = "triage", "block_loop_detected"
                    payload["limit"] = BLOCK_RECURRENCE_LIMIT
                else:
                    new_status, event_kind = "blocked", "blocked"
                    payload.pop("limit", None)
            if event_kind == "blocked" and event_payload_extra:
                payload.update(event_payload_extra)
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        sql = f"""
                UPDATE tasks
                   SET status        = '{new_status}',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL,
                       {set_sql}
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """
        params = (*params, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="blocked", status="blocked",
            summary=reason, effect_policy=effect_policy,
        )
        # Synthesize a run when blocking a never-claimed task so the
        # reason is preserved in attempt history.
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="blocked", summary=reason,
            )
        _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        blocked_task = get_task(conn, task_id)
        if kind == "dependency":
            # Historical ordering: the dependency lane fires inside the txn.
            _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
            _sync_production_ledger(
                conn, task_id, status="todo", phase="block", blocked_reason=reason,
                run_id=run_id,
            )
            return True
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    _sync_production_ledger(
        conn, task_id,
        status=(blocked_task.status if blocked_task else "blocked"),
        phase="block", blocked_reason=reason, run_id=run_id,
    )
    return True


def _route_block(
    kind: Optional[str], reason: Optional[str], source_status: str, *,
    prev_kind: Optional[str], prev_recurrences: int,
) -> tuple[str, str, str, tuple, dict]:
    """``(new_status, event_kind, set_sql, params, payload)`` for :func:`block_task`.

    ``dependency`` never enters the human ``blocked`` bucket: it waits in
    ``todo`` for ``recompute_ready``, so a cron never sees a dependency-wait
    as something to "unblock". Callers that pass ``dependency`` with no
    incomplete parent are re-kinded to ``needs_input`` before this runs
    (see :func:`block_task`). Every other kind counts unblock-loop
    recurrences: block_task only fires from running/ready (AFTER an unblock
    returned the task to the pool), so a stored ``block_kind`` equal to the
    incoming one means blocked -> unblocked -> re-block for the same cause
    (un-typed None compares equal to a prior un-typed block). At
    ``BLOCK_RECURRENCE_LIMIT`` the task routes to ``triage`` for a human.
    """
    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload


def redact_review_value(value: Any) -> Any:
    """Redact secrets at the domain boundary for durable review handoffs."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: redact_review_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_review_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_review_value(item) for item in value)
    return value


_GENERATED_REVIEW_EVIDENCE_BY = "hermes:review-handoff"


def _nonempty_string_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _qualifying_review_evidence(
    conn: sqlite3.Connection, task_id: str, metadata: Any
) -> bool:
    """Return whether run metadata is concrete enough to verify independently.

    Two evidence shapes are accepted, both fail-closed:

    * implementation evidence: non-empty ``changed_files`` and ``verification``
      lists;
    * governed orchestration-release evidence: metadata names the upstream
      verified task/verifier and a pre-wired implementation -> verifier graph,
      and every claimed edge/state is re-derived from the durable DB here.

    The second form exists for orchestration parents whose work is authorising
    a governed implementation phase rather than changing source files. Metadata
    alone never authorises release.
    """
    if not isinstance(metadata, dict):
        return False
    if (
        _nonempty_string_list(metadata.get("changed_files"))
        and _nonempty_string_list(metadata.get("verification"))
    ):
        return True
    if metadata.get("phase") != "orchestration_release":
        return False
    upstream = metadata.get("upstream_verified")
    implementation_child = metadata.get("implementation_child")
    verifier_child = metadata.get("independent_verifier_child")
    if not (
        isinstance(upstream, dict)
        and isinstance(upstream.get("task_id"), str)
        and upstream.get("task_id")
        and isinstance(upstream.get("verifier"), str)
        and upstream.get("verifier")
        and upstream.get("verdict") == "PASS"
        and isinstance(implementation_child, str)
        and implementation_child
        and isinstance(verifier_child, str)
        and verifier_child
        and _nonempty_string_list(metadata.get("performed"))
    ):
        return False

    upstream_row = conn.execute(
        "SELECT status, verification_state, terminal_disposition FROM tasks WHERE id = ?",
        (upstream["task_id"],),
    ).fetchone()
    upstream_verifier = conn.execute(
        "SELECT status, executor_lane FROM tasks WHERE id = ?",
        (upstream["verifier"],),
    ).fetchone()
    impl_row = conn.execute(
        "SELECT executor_lane, gauntlet_enforced FROM tasks WHERE id = ?",
        (implementation_child,),
    ).fetchone()
    verify_row = conn.execute(
        "SELECT executor_lane, gauntlet_enforced FROM tasks WHERE id = ?",
        (verifier_child,),
    ).fetchone()
    if not (
        upstream_row is not None
        and upstream_row["status"] in ("done", "archived")
        and upstream_row["verification_state"] == VERIFICATION_VERIFIED
        and upstream_row["terminal_disposition"] == DISPOSITION_COMPLETED
        and upstream_verifier is not None
        and upstream_verifier["status"] in ("done", "archived")
        and upstream_verifier["executor_lane"] in INDEPENDENT_VERIFIER_LANES
        and impl_row is not None
        and impl_row["executor_lane"] == EXECUTOR_LANE_CLAUDE
        and bool(impl_row["gauntlet_enforced"])
        and verify_row is not None
        and verify_row["executor_lane"] == EXECUTOR_LANE_CODEX_VERIFY
        and bool(verify_row["gauntlet_enforced"])
    ):
        return False
    edges = (
        conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (upstream["task_id"], upstream["verifier"]),
        ).fetchone()
        and conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (task_id, implementation_child),
        ).fetchone()
        and conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (implementation_child, verifier_child),
        ).fetchone()
    )
    return bool(edges)


def review_handoff_evidence_gap(
    conn: sqlite3.Connection, task_id: str, metadata: Any,
) -> Optional[str]:
    """Why a Gauntlet review handoff would carry no verifiable evidence, or ``None``.

    Read by the CLI and tool handoff surfaces before calling
    :func:`request_review`. ``None`` when the task is not Gauntlet-enforced,
    already has evidence (:func:`subject_has_evidence`), or ``metadata`` would
    materialize a packet (:func:`_qualifying_review_evidence`). A prose summary
    never counts: it is not falsifiable.
    """
    row = conn.execute(
        "SELECT gauntlet_enforced FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return None
    if not (row["gauntlet_enforced"] or gauntlet_enforcement_default()):
        return None
    if subject_has_evidence(conn, task_id):
        return None
    if _qualifying_review_evidence(conn, task_id, metadata):
        return None
    return (
        f"{task_id} is Gauntlet-enforced and this handoff carries no verifiable "
        "evidence, so no independent verifier could ever be routed. Attach it "
        f"first (hermes kanban attach {task_id} <file>) or hand off with "
        "structured metadata carrying non-empty 'changed_files' and "
        "'verification' lists."
    )


def _materialize_review_evidence(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
    summary: Optional[str],
    metadata: Any,
) -> Optional[int]:
    """Persist qualifying run evidence through the native attachment API."""
    if run_id is None or not _qualifying_review_evidence(conn, task_id, metadata):
        return None
    payload = {
        "schema": "hermes.review-handoff-evidence.v1",
        "generated": True,
        "source": {"task_id": task_id, "run_id": int(run_id)},
        "summary": summary,
        "metadata": metadata,
    }
    return store_attachment_bytes(
        conn,
        task_id,
        f"review-handoff-run-{int(run_id)}.json",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        content_type="application/vnd.hermes.review-handoff+json",
        uploaded_by=_GENERATED_REVIEW_EVIDENCE_BY,
        board=get_current_board(),
    )


def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.

    Gauntlet: entering the review lane IS entering VERIFICATION_PENDING (a
    ledger row is recorded unconditionally). ``reviewer='atlas'`` on a
    Gauntlet subject opens the independent ``codex_verify`` child route
    post-commit instead of relabelling the subject; a handoff whose review
    assignee the verdict gate is certain to refuse opens the same route.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    requested_independent_lane = False
    implementer: Optional[str] = None
    try:
        with write_txn(conn):
            if not _parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at, gauntlet_enforced FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _canonical_assignee(reviewer)
            # D8: ``reviewer`` is written straight into ``assignee`` below, so it
            # gets the same gate as every other assignee write. Before this, an
            # arbitrary string ("reviewer") landed on a live card and parked it in
            # the review lane with nothing that could ever claim it (t_f853abd8).
            # Refused as a normal transition failure rather than an exception:
            # request_review's contract is (ok, reason), and a caller that gets a
            # reason can retry with a real reviewer, whereas a phantom write
            # succeeds loudly and fails silently forever.
            try:
                _assert_assignee_dispatchable(
                    reviewer, context="cannot hand off for review"
                )
            except ValueError as exc:
                return _ret(False, str(exc))
            # D5: this is an assignee write on a task that is, by definition, about
            # to be in 'review' — the one status the ready-queue's lazy shorthand
            # normalization never sees. ``reviewer='atlas'`` therefore used to park
            # the card on a token no dispatch loop can spawn, which is exactly how
            # t_6b7d5845 and t_06e046f1 stalled (35.7h / 16.8h, 12 gauntlet_stale
            # events between them) while doing the RIGHT thing: routing to an
            # independent verifier to escape an Erika-reviews-Erika loop.
            reviewer, reviewer_lane, reviewer_from_token = _normalize_shorthand_lane(
                reviewer, None
            )
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            # On a Gauntlet subject, reviewer=atlas means "open the independent
            # Codex verifier route".  It must NEVER relabel the subject itself as
            # executor_lane=codex_verify, because that turns the subject into its
            # own verification artifact and bypasses the verified-before-done gate.
            # Preserve the subject carrier/assignee and let the post-commit routing
            # block below create the linked verifier child.
            requested_independent_lane = (
                reviewer_from_token is not None
                and reviewer_lane == EXECUTOR_LANE_CODEX_VERIFY
                and bool(trow["gauntlet_enforced"] or gauntlet_enforcement_default())
            )
            if requested_independent_lane:
                reviewer = implementer if implementer is not None else trow["assignee"]
                reviewer_lane = None
                reviewer_from_token_for_write = None
            else:
                reviewer_from_token_for_write = reviewer_from_token
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            lane_sql = ", executor_lane = ?" if reviewer_from_token_for_write is not None else ""
            assignee_sql += lane_sql
            head: tuple[Any, ...] = ()
            if reviewer is not None:
                head = (reviewer,)
                if reviewer_from_token_for_write is not None:
                    head += (reviewer_lane,)
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            params: tuple[Any, ...] = (
                *head, task_id,
                *(() if expected_run_id is None else (int(expected_run_id),)),
            )
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + run_guard,
                params,
            )
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if requested_independent_lane:
                _append_event(
                    conn, task_id, "independent_verifier_lane_requested",
                    {
                        "from_reviewer": reviewer_from_token,
                        "required_lane": EXECUTOR_LANE_CODEX_VERIFY,
                        "subject_executor_lane_preserved": True,
                        "source": "request_review",
                    },
                )
            elif reviewer_from_token_for_write is not None:
                # Same event kind the ready-queue dispatcher and assign_task emit,
                # so the audit trail reads identically whichever path translated.
                _append_event(
                    conn, task_id, "executor_lane_normalized",
                    {
                        "from_assignee": reviewer_from_token_for_write,
                        "assignee": reviewer,
                        "executor_lane": reviewer_lane,
                        "source": "request_review",
                        "status": "review",
                    },
                )
            if isinstance(metadata, dict):
                staged_copies = _stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            # Gauntlet: entering the review lane IS entering VERIFICATION_PENDING.
            # Recorded unconditionally, not just for enforced tasks, so the ledger
            # is complete if enforcement is switched on later — and so the
            # ``pending -> verified`` verdict always has a phase-start to sit
            # against. It does not gate anything on its own.
            _record_verification_row(
                conn,
                task_id,
                state=VERIFICATION_PENDING,
                run_id=run_id,
                verifier=reviewer,
                evidence={"implementer": implementer, "reviewer": reviewer},
                reason="implementation handed off for verification",
            )
            _materialize_review_evidence(conn, task_id, run_id, summary, metadata)
            # Judged only after materialization: on 2026-09-14 a handoff whose
            # metadata did qualify still logged a false "no evidence" record,
            # because this check used to run before the packet was written.
            # (The CLI and tool entry surfaces refuse such a handoff outright via
            # ``review_handoff_evidence_gap``; this core stays non-refusing.)
            if (
                (trow["gauntlet_enforced"] or gauntlet_enforcement_default())
                and not subject_has_evidence(conn, task_id)
            ):
                _append_event(
                    conn,
                    task_id,
                    "verification_route_unopened",
                    {
                        "reason": "gauntlet subject entered review carrying no "
                                  "evidence packet and inheriting none from a "
                                  "verified parent; the independent-verifier "
                                  "pre-flight will decline to open a route, so "
                                  "nothing can return a verdict on this card",
                        "remedy": "attach falsifiable evidence "
                                  "(hermes kanban attach <task_id> <file>), or "
                                  "hand off with structured run metadata",
                    },
                )
            payload: dict = {
                "summary": _first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
                "verification_state": VERIFICATION_PENDING,
            }
            staged = _cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    # Autonomous Gauntlet routing. The handoff above is durable; what follows
    # decides WHO can act on it. When the review assignee this handoff leaves
    # behind is the implementer itself — the ``reviewer=None`` default, which
    # is what every autonomous caller passes — a Gauntlet subject has been
    # parked in a lane whose only candidate the verdict gate is guaranteed to
    # refuse. That is the live t_cbc133db failure, and the point of repair is
    # that it is knowable HERE, at the handoff, rather than after a refusal.
    #
    # Post-commit on purpose (the same placement ``complete_task`` uses for the
    # verdict return path): the subject is already correctly parked, and a
    # fault while opening the independent route must not roll back an
    # implementation handoff that succeeded.
    if gauntlet_required(conn, task_id):
        assignee_now = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        effective = assignee_now["assignee"] if assignee_now is not None else None
        conflict = _review_claim_conflict(conn, task_id, effective)
        # Explicit reviewer=atlas on a Gauntlet subject is an explicit request
        # for the independent verifier CHILD, even though the preserved subject
        # assignee is also recorded as this phase's reviewer carrier. Do not let
        # that provenance suppress the child route.
        if requested_independent_lane or conflict is not None:
            if conflict is not None:
                conflict_identity, conflict_source = conflict
            else:
                conflict_identity, conflict_source = implementer or effective, "explicit_codex_lane"
            child_id = _ensure_independent_verifier_child(
                conn, task_id, implementer=implementer,
                # An explicit reviewer='atlas' request names the Codex lane
                # specifically (acceptance: "ask Atlas" still routes to
                # codex_verify/atlas exactly as now). A plain conflict-based
                # route (no explicit lane named) falls through to the
                # function's own default-lane resolution.
                preferred_lane=(
                    EXECUTOR_LANE_CODEX_VERIFY if requested_independent_lane else None
                ),
            )
            with write_txn(conn):
                _emit_once_this_phase(
                    conn, task_id, "independent_verification_required",
                    {
                        "required_lane": EXECUTOR_LANE_CODEX_VERIFY,
                        "conflict_identity": conflict_identity,
                        "conflict_source": conflict_source,
                        "verifier_task": child_id,
                        "source": "request_review",
                    },
                )
    # A pre-wired verifier OR governed implementation child must not require an
    # operator relay after the evidence-producing transition commits. The
    # normal dependency predicate remains authoritative and keeps any
    # evidence-less handoff gated.
    recompute_ready(conn)
    # Production ledger projection (post-commit, best-effort). See
    # _sync_production_ledger docstring — this closes the gap identified in
    # t_da07457d's FAIL verdict on t_faf6e26a.
    _sync_production_ledger(
        conn, task_id, status="review", phase="request_review", summary=summary,
    )
    return _ret(True)


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _json_dict(_row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def request_changes(
    conn: sqlite3.Connection, task_id: str, *, reason: str, expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Close an active reviewer run (claimed from ``review``) and hand the task
    back to the implementer from the latest ``review_requested`` event, parent
    gating reapplied. Returns ``(ok, implementer | reason)``."""
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _json_dict(_row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _nonblank_str(_json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = _canonical_assignee(_nonblank_str(task_row["assignee"]))

        new_status = _landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        )
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        # Back to the implementer: the verification phase is over and its
        # verdict (pending or failed) no longer describes the work in flight.
        # ``regression_required`` is deliberately NOT cleared here: if a
        # failing verdict armed it, the rework this routing starts is the
        # repair that owes the proof. A bare request_changes with no recorded
        # verdict does not arm it — the requirement follows the verdict, not
        # the routing, so review conversations keep their existing behaviour.
        _clear_verification_state(conn, task_id)
        run_id = _end_run(
            conn, task_id, outcome="changes_requested", status=new_status, summary=reason,
        )
        _append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
            },
            run_id=run_id,
        )
    # Production ledger projection (post-commit, best-effort). See
    # _sync_production_ledger docstring — this closes the gap identified in
    # t_da07457d's FAIL verdict on t_faf6e26a.
    _sync_production_ledger(
        conn, task_id, status=new_status, phase="request_changes",
        summary=reason, run_id=run_id,
    )
    return True, implementer


LESSON_EVIDENCE_KEY = "lesson"
LESSON_APPLICABILITY_EVIDENCE_KEY = "lesson_applicability"


def _extract_lesson_on_verify(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    verifier: Optional[str],
    evidence: Optional[dict],
    run_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Run the required post-VERIFIED extraction step after a passing verdict.

    :func:`extract_lesson` describes itself as "the required post-VERIFIED
    step" and had no caller, so the write half of the verified-learning loop
    never ran: the board carried 403 verifications and 0 lessons while
    :func:`build_worker_context` faithfully injected the empty set into every
    worker. This is the call site that closes it.

    Deliberately NOT inside :func:`record_verification`'s transaction. The
    verdict must be durable before extraction is attempted, because
    :func:`promote_lesson` refuses a source whose head is not already verified
    -- running it in the same transaction would make every extraction fail its
    own gate on state this transaction has not yet committed.

    **Never raises.** A lesson is a by-product of a verdict, never a condition
    of it; an extraction failure that could unwind a PASS would make the
    learning loop a new way for verification to break. Every outcome --
    extracted, declined, refused, errored -- is recorded as an event instead,
    so "no lesson" stops being silence and becomes a counted, auditable fact.

    Returns the lesson row when one was written, else ``None``.
    """
    payload = evidence if isinstance(evidence, dict) else {}
    text = str(payload.get(LESSON_EVIDENCE_KEY) or "").strip()
    selector = str(payload.get(LESSON_APPLICABILITY_EVIDENCE_KEY) or "").strip()

    def _event(kind: str, detail: dict[str, Any]) -> None:
        # Its own transaction: this runs after the verdict has committed, and
        # a bookkeeping failure here must not be able to touch that verdict.
        try:
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    kind,
                    {"verifier": verifier, "source": "record_verification", **detail},
                    run_id=run_id,
                )
        except Exception:
            pass

    if not text:
        # The common case today, and the one worth counting. A verifier that
        # offered no lesson is not an error -- most verdicts genuinely teach
        # nothing reusable -- but it must be visible, or a loop that never runs
        # is indistinguishable from a loop with nothing to say.
        _event("lesson_extraction_skipped", {"reason": "no_lesson_offered"})
        return None
    if not selector:
        _event(
            "lesson_extraction_skipped",
            {"reason": "no_applicability", "lesson_chars": len(text)},
        )
        return None

    try:
        row = extract_lesson(
            conn,
            task_id,
            lesson=text,
            applicability=selector,
            actor=verifier,
            evidence=payload,
        )
    except LessonPromotionError as exc:
        # A refusal is the gate working. Record the code so a verifier can see
        # which gate blocked it rather than concluding extraction is broken.
        _event(
            "lesson_extraction_blocked",
            {"code": exc.code, "detail": str(exc)[:400]},
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        _event(
            "lesson_extraction_error",
            {"error": type(exc).__name__, "detail": str(exc)[:400]},
        )
        return None

    _event(
        "lesson_extracted",
        {
            "lesson_id": row.get("id"),
            "state": row.get("state"),
            "auto_promoted": bool(row.get("auto_promoted")),
            "eligibility_reason": row.get("eligibility_reason"),
        },
    )
    return row


ERROR_LESSON_AUTO_ACTOR = "loop:auto-resolution"
ERROR_LESSON_MIN_SIGNATURE_TERMS = 2


def _record_error_lessons_on_resolution(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    verifier: Optional[str],
    reason: Optional[str],
    evidence: Optional[dict],
    run_id: Optional[int] = None,
) -> list[int]:
    """Automatic write half of retrieval-on-error (Christopher, 2026-09-29).

    Fires on a PASS that follows a FAILED verdict on the same task: the one
    moment both the error and its fix are known. Each failing criterion parsed
    from the failed verdict's report becomes an error-keyed lesson through
    :func:`record_error_lesson` -- the signature is the criterion, the lesson
    is how it was resolved.

    Deliberately narrow. Only parsed criteria (``failure_fingerprint`` source
    ``criteria``) are keyed: whole-report fallback text and verifier reason
    prose are boilerplate that would make every row match every other (the
    reason :func:`_extract_lesson_on_failure` sets no signature), and run-level
    errors are protocol/timeout noise. Rows are candidates (``active=0``) --
    retrievable on error, binding nothing (ruling 2026-09-07: lessons are never
    preloaded). Idempotent per (task, signature). **Never raises.**
    """
    recorded: list[int] = []

    def _event(kind: str, detail: dict[str, Any]) -> None:
        try:
            with write_txn(conn):
                _append_event(
                    conn, task_id, kind,
                    {"verifier": verifier, "source": "record_verification", **detail},
                    run_id=run_id,
                )
        except Exception:
            pass

    try:
        failed = conn.execute(
            "SELECT * FROM task_verifications WHERE task_id = ? AND state = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, VERIFICATION_FAILED),
        ).fetchone()
        if failed is None:
            return recorded
        fp = failure_fingerprint(_failed_verdict_report(conn, failed))
        if fp["source"] != "criteria" or not fp["criteria"]:
            _event(
                "error_lesson_skipped",
                {"reason": "no_parsed_failing_criteria",
                 "failed_verification_id": int(failed["id"])},
            )
            return recorded
        task = conn.execute(
            "SELECT tenant FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        tenant = (task["tenant"] if task is not None else None) or None
        payload = evidence if isinstance(evidence, dict) else {}
        offered = str(
            redact_review_value(payload.get(LESSON_EVIDENCE_KEY) or "")
        ).strip()
        summary = str(redact_review_value(reason or "")).strip()
        for criterion in fp["criteria"]:
            label = str(redact_review_value(criterion["label"]))
            sig = error_signature(label)
            if len(sig.split()) < ERROR_LESSON_MIN_SIGNATURE_TERMS:
                continue
            if conn.execute(
                "SELECT 1 FROM task_lessons WHERE source_task_id = ? "
                "AND error_signature = ? LIMIT 1",
                (task_id, sig),
            ).fetchone() is not None:
                continue
            text = (
                f"Resolved in task {task_id} after failed verification "
                f"{int(failed['id'])} ({label}); passed verification by "
                f"{verifier or 'unknown'}: {summary or 'no summary recorded'}"
            )
            if offered:
                text += f"\nVerifier-offered lesson: {offered}"
            if len(text) > LESSON_MAX_CHARS:
                text = text[: LESSON_MAX_CHARS - 3].rstrip() + "..."
            row = record_error_lesson(
                conn,
                error_text=label,
                lesson=text,
                source_task_id=task_id,
                created_by=ERROR_LESSON_AUTO_ACTOR,
                tenant=tenant,
            )
            recorded.append(int(row["id"]))
    except Exception as exc:
        _event(
            "error_lesson_error",
            {"error": type(exc).__name__, "detail": str(exc)[:400]},
        )
    return recorded


def _extract_lesson_on_failure(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    verifier: Optional[str],
    reason: Optional[str],
    evidence: Optional[dict],
    run_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Record a failed verdict as a non-binding candidate lesson.

    The failure-side counterpart of :func:`_extract_lesson_on_verify`. That
    path fires only on PASS, so every failed verdict -- 119 of them on the live
    board when this was added -- produced no learning material at all.

    It cannot reuse :func:`extract_lesson`: :func:`lesson_source_state` refuses
    a FAILED head by design, and that gate must not be loosened. So the row is
    written directly, and is ALWAYS ``state='candidate'``, ``active=0`` --
    never run through :func:`lesson_promotion_eligibility`, because a failed
    attempt stays an observation, never canonical learning. It binds nothing
    (:func:`lessons_for_task` reads ``active = 1`` only) until an operator runs
    :func:`approve_lesson` on it. No ``error_signature`` is set: the reasons
    automated verifiers write are near-identical boilerplate, and exact
    signature matching would make every such row "match" every other.

    Same contract as the PASS path: runs after the verdict has committed, in
    its own transaction, and **never raises**.
    """
    payload = evidence if isinstance(evidence, dict) else {}
    reason_text = str(redact_review_value(reason or "")).strip()
    offered = str(
        redact_review_value(payload.get(LESSON_EVIDENCE_KEY) or "")
    ).strip()
    selector = LESSON_SELECTOR_ALL
    raw_selector = str(payload.get(LESSON_APPLICABILITY_EVIDENCE_KEY) or "").strip()
    if raw_selector:
        try:
            selector = normalize_lesson_applicability(raw_selector)
        except LessonPromotionError:
            selector = LESSON_SELECTOR_ALL

    text = (
        f"Verification FAILED for task {task_id}"
        f" (verifier {verifier or 'unknown'}): {reason_text or 'no reason recorded'}"
    )
    if offered:
        text += f"\nVerifier-offered lesson: {offered}"
    if len(text) > LESSON_MAX_CHARS:
        text = text[: LESSON_MAX_CHARS - 3].rstrip() + "..."

    try:
        with write_txn(conn):
            task = conn.execute(
                "SELECT tenant, status, verification_state FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                return None
            verdict = _latest_verdict_row(conn, task_id)
            tenant = task["tenant"] or None
            scope = LESSON_SCOPE_TENANT if tenant else LESSON_SCOPE_GLOBAL
            verification_id = (
                int(verdict["id"])
                if verdict is not None and verdict["state"] == VERIFICATION_FAILED
                else None
            )
            provenance: dict[str, Any] = {
                "origin": "verification_failed",
                "source_task_id": task_id,
                "source_status": task["status"],
                "verification_state": task["verification_state"],
                "verification_id": verification_id,
                "failed_at": verdict["created_at"] if verdict is not None else None,
                "verifier": verifier,
                "reason": reason_text or None,
            }
            if payload:
                provenance["verifier_evidence"] = redact_review_value(payload)
            if verification_id is not None:
                # The same fingerprint objective_failure_ledger derives for the
                # attempt ceiling, snapshotted at the moment of failure.
                fp = failure_fingerprint(_failed_verdict_report(conn, verdict))
                provenance["failure_fingerprint"] = fp["fingerprint"]
                provenance["failing_criteria"] = redact_review_value(
                    [c["label"] for c in fp["criteria"]]
                )
            now = int(time.time())
            cur = conn.execute(
                """
                INSERT INTO task_lessons
                    (source_task_id, tenant, scope, applicability, lesson,
                     evidence, verification_id, created_by, created_at,
                     active, state, review_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    task_id,
                    tenant,
                    scope,
                    selector,
                    text,
                    json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                    verification_id,
                    verifier,
                    now,
                    LESSON_STATE_CANDIDATE,
                    now + LESSON_DEFAULT_REVIEW_DAYS * 86400,
                ),
            )
            lesson_id = int(cur.lastrowid)
            _append_event(
                conn,
                task_id,
                "failure_lesson_candidate_recorded",
                {
                    "lesson_id": lesson_id,
                    "state": LESSON_STATE_CANDIDATE,
                    "scope": scope,
                    "tenant": tenant,
                    "applicability": selector,
                    "verifier": verifier,
                    "verification_id": verification_id,
                    "source": "record_verification",
                },
                run_id=run_id,
            )
            row = conn.execute(
                "SELECT * FROM task_lessons WHERE id = ?", (lesson_id,)
            ).fetchone()
        return _lesson_row_to_dict(row)
    except Exception as exc:
        try:
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    "failure_lesson_extraction_error",
                    {
                        "verifier": verifier,
                        "source": "record_verification",
                        "error": type(exc).__name__,
                        "detail": str(exc)[:400],
                    },
                    run_id=run_id,
                )
        except Exception:
            pass
        return None


def record_verification(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    passed: bool,
    verifier: Optional[str] = None,
    evidence: Optional[dict] = None,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    route_on_failure: bool = True,
    regression_evidence: Optional[dict] = None,
) -> tuple[bool, Optional[str]]:
    """Record the VERIFIED / failed verdict for a task in the review lane.

    This is the only way to reach ``verification_state='verified'``, and it is
    legal only from inside the verification phase — the task is parked in
    ``review``, or a reviewer run claimed from ``review`` is in flight. An
    implementer's own ``running`` task is refused, which is what makes the
    Gauntlet chain non-bypassable: the executor has no route to the state its
    own completion requires.

    ``evidence`` is a free-form dict of falsifiable facts (commands, exit
    codes, test counts, artefact paths) persisted on the ledger row. It is
    passed through :func:`redact_review_value` like every other reviewer-
    supplied payload.

    ``regression_evidence`` is the distinct, structured thing a PASS after a
    material repair must carry: proof that the checks were re-run against the
    repaired work. It is recorded as its own ledger row (``kind='regression'``)
    bound to the current verification phase, in the same transaction as the
    verdict that spends it. Required — and only required — once a failing
    verdict has armed ``tasks.regression_required``; a first-pass task never
    needs it. A PASS that owes one and cannot produce one is refused with the
    verdict unwritten, so the task stays non-complete and the ledger never
    shows a repair blessed on a bare assurance.

    On ``passed=False`` the verdict is durable and the task is routed back for
    repair: ``request_changes`` when a reviewer run is in flight, otherwise
    ``reopen_review_task``. If routing is declined (``route_on_failure=False``)
    or fails, the task stays in ``review`` with a ``failed`` head — either way
    it cannot complete. ``reason`` is required for a failing verdict.

    Returns ``(ok, detail)``: ``detail`` is the resulting status on success and
    a diagnostic string on refusal.
    """
    reason_text = str(redact_review_value(reason or "")).strip() or None
    if not passed and not reason_text:
        return False, "reason is required for a failing verification"
    verifier = _canonical_assignee(verifier) if verifier else None

    review_run_id: Optional[int] = None
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, assignee, current_run_id, verification_state "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return False, "task not found"
        status = row["status"]
        if status == "review":
            # Parked awaiting a verifier. No run in flight to bind to; the
            # verdict attaches to the run that requested the review.
            phase_run_id = None
        elif status == "running":
            review_run_id, run_reviewer = _review_run_verification(conn, task_id)
            if review_run_id is None:
                return False, (
                    "task is running an implementation run; an executor "
                    "cannot verify its own work — hand off with "
                    "request_review first"
                )
            if expected_run_id is not None and int(expected_run_id) != review_run_id:
                return False, "run_id mismatch"
            if verifier is None and run_reviewer:
                verifier = _canonical_assignee(run_reviewer)
            phase_run_id = review_run_id
        else:
            return False, (
                f"task is {status!r}; verification is only valid from the "
                f"review lane (status 'review', or a reviewer run claimed "
                f"from it)"
            )

        # Verifier identity (D4). Three gates, all fail-closed, all committing
        # a durable audit event on refusal — a refusal that leaves no event is
        # a refusal the rest of the board cannot act on, which is how the same
        # identity was re-selected three times on t_00690780. ``return`` (not
        # raise) so the event commits with the rest of the txn.

        # (b) An anonymous verdict. This used to be a documented concession to
        # "untracked/legacy callers": the guard read ``if verifier and ...``,
        # so passing no verifier bypassed the independence comparison outright.
        # No such caller survives — ``kanban.py`` falls back to the acting
        # profile and ``_return_verifier_verdict_to_subjects`` always signs —
        # and an unattributable verdict cannot be shown to be independent of
        # anyone, which is the whole property being asserted. Fail closed.
        if not verifier:
            _append_event(
                conn, task_id, "verification_blocked_unknown_verifier",
                {
                    "verifier": None,
                    "detail": "no verifier identity supplied",
                    "source": "record_verification",
                },
                run_id=review_run_id,
            )
            return False, (
                "verification requires a named verifier; an anonymous verdict "
                "cannot be shown to be independent of the implementer — pass "
                "--verifier <profile> or route through the codex_verify lane"
            )

        # (a) The identity must resolve to something the system knows: an
        # existing Hermes profile, or a recognized verifier lane. Nothing
        # checked this before, so any string that merely DIFFERED from the
        # implementer rendered a binding verdict — how 'chatgpt-systems'
        # closed t_023d2af6 and t_616527d7 (a live production deployment).
        identity_status, identity_detail = _verifier_identity_status(
            verifier, conn=conn, subject_id=task_id,
        )
        if identity_status == VERIFIER_IDENTITY_UNKNOWN:
            _append_event(
                conn, task_id, "verification_blocked_unknown_verifier",
                {
                    "verifier": verifier,
                    "detail": identity_detail,
                    "source": "record_verification",
                },
                run_id=review_run_id,
            )
            return False, (
                f"verifier {verifier!r} is not a registered identity: "
                f"{identity_detail}. A verdict signed by an identity the "
                f"system cannot resolve is not attributable and cannot be "
                f"shown to be independent — register the profile or route "
                f"through the codex_verify lane"
            )
        if identity_status == VERIFIER_IDENTITY_UNCHECKED:
            # The registry itself is unreadable. Do not convert that into a
            # false accusation against a possibly-valid verifier, but do not
            # let it pass silently either: the gap is on the ledger.
            _append_event(
                conn, task_id, "verifier_identity_unchecked",
                {
                    "verifier": verifier,
                    "detail": identity_detail,
                    "source": "record_verification",
                },
                run_id=review_run_id,
            )

        # (c) Verifier independence (doctrine_verification.md: "the verifier
        # should be independent of the executor where practical"). The
        # `status == "running"` branch above already refuses an executor
        # verifying its OWN in-flight run structurally, but that guard is
        # about which RUN is claimed, not WHO is acting — the same profile
        # can author a decision (as the task's assignee/implementer) and
        # then separately claim the review run, or verify a parked
        # 'review' task outright, and nothing compared identities. No
        # doctrine exception for this is currently on file; do not invent
        # one here — a real exception is a doctrine amendment, not a code
        # bypass.
        # The implementer identity lives on the ``review_requested`` event
        # that parked this phase — see ``_review_requested_implementer``.
        # The task's own ``assignee`` is now compared too, except where that
        # assignee was deliberately installed as this phase's reviewer — see
        # ``_phase_reviewer_identities`` for why that carve-out is required
        # rather than optional.
        implementer = _review_requested_implementer(conn, task_id, row["assignee"])
        conflict = _verifier_independence_conflict(
            conn, task_id, verifier,
            implementer=implementer,
            assignee=row["assignee"],
        )
        if conflict is not None:
            conflict_identity, conflict_source = conflict
            # Emit the same audit event ``complete_task``'s implicit path
            # emits. It is not decoration: it is the durable record the
            # selection guard in ``claim_review_task``, the retry carve-out
            # in ``_record_task_failure`` and the governed-handoff release in
            # ``_GOVERNED_IMPLEMENTATION_HANDOFF_SQL`` all read.
            #
            # Bind it to a run that release predicate can actually follow. A
            # refusal raised from a claimed review run keeps that run: the
            # ``claimed``/``source_status='review'`` chain leads back to the
            # handoff. A refusal against a task parked in 'review' has no run
            # in flight, and used to be recorded with no run at all — leaving
            # the phase unanchored, so the parked arm of that predicate could
            # never match. Fall back to the implementation run that opened the
            # current verification phase, which is exactly what the predicate
            # compares against.
            blocked_run_id = review_run_id
            if blocked_run_id is None:
                blocked_phase = _current_verification_phase(conn, task_id)
                if blocked_phase is not None:
                    blocked_run_id = blocked_phase[1]
            _append_event(
                conn, task_id, "verification_blocked_self_review",
                {
                    "verifier": verifier,
                    "implementer": implementer,
                    "conflict_identity": conflict_identity,
                    "conflict_source": conflict_source,
                    "source": "record_verification",
                },
                run_id=blocked_run_id,
            )
            return False, (
                f"verifier {verifier!r} matches the "
                + (
                    f"implementer {conflict_identity!r}"
                    if conflict_source == "implementer"
                    else f"task's own assignee {conflict_identity!r}, which "
                         f"was never installed as this phase's reviewer"
                )
                + "; doctrine_verification.md requires an independent "
                  "verifier and no governance-ruling exception is on file — "
                  "route to a different reviewer or the codex_verify lane"
            )

        proof_id: Optional[int] = None
        if passed:
            # A material repair owes regression evidence before it can be
            # blessed. The gate records the proof (when supplied) inside this
            # transaction; on refusal nothing at all is written — no verdict,
            # no partial proof — so the task simply stays where it was.
            ok_reg, reg_detail, proof_id = _regression_gate(
                conn,
                task_id,
                evidence=regression_evidence,
                verifier=verifier,
                reason=reason_text,
            )
            if not ok_reg:
                _append_event(
                    conn,
                    task_id,
                    "verification_blocked_no_regression",
                    {
                        "verifier": verifier,
                        "detail": reg_detail,
                        "status": status,
                    },
                    run_id=phase_run_id,
                )
                return False, reg_detail
        elif regression_evidence is not None:
            # Nothing to bless, so nothing to prove. Refusing loudly beats
            # silently dropping a payload the caller thought was recorded.
            return False, (
                "regression evidence belongs on a passing verdict; a failing "
                "one takes --reason and ordinary evidence"
            )

        state = VERIFICATION_VERIFIED if passed else VERIFICATION_FAILED
        verdict_evidence = evidence
        if proof_id is not None:
            # Bind proof to verdict from both sides: the proof names the phase
            # it covers, the verdict names the proof it spent.
            verdict_evidence = dict(evidence or {})
            verdict_evidence["regression_proof_id"] = proof_id
        _record_verification_row(
            conn,
            task_id,
            state=state,
            run_id=phase_run_id,
            verifier=verifier,
            evidence=verdict_evidence,
            reason=reason_text,
        )
        if passed:
            if proof_id is not None:
                _spend_regression_requirement(conn, task_id)
        else:
            # Arm the repair gate: whatever happens next is a repair of work
            # that has already been found wanting, so its eventual PASS has to
            # show the checks re-run rather than re-assert that it works.
            conn.execute(
                "UPDATE tasks SET regression_required = 1 WHERE id = ?",
                (task_id,),
            )
        _append_event(
            conn,
            task_id,
            "verification_passed" if passed else "verification_failed",
            {
                "verifier": verifier,
                "reason": reason_text,
                "source": "record_verification",
                **(
                    {"regression_proof_id": proof_id}
                    if passed
                    else {"regression_required": True}
                ),
            },
            run_id=phase_run_id,
        )

    if passed:
        # The verdict is committed above; only now is the source durably
        # VERIFIED, which is the state extract_lesson's gates require.
        _extract_lesson_on_verify(
            conn,
            task_id,
            verifier=verifier,
            evidence=evidence,
            run_id=phase_run_id,
        )
        # A pass that follows a failure is where an error meets its fix.
        _record_error_lessons_on_resolution(
            conn,
            task_id,
            verifier=verifier,
            reason=reason_text,
            evidence=evidence,
            run_id=phase_run_id,
        )
        return True, VERIFICATION_VERIFIED
    # The failing verdict is committed above too. Record it as a non-binding
    # candidate before routing, so both route_on_failure outcomes produce one.
    _extract_lesson_on_failure(
        conn,
        task_id,
        verifier=verifier,
        reason=reason_text,
        evidence=evidence,
        run_id=phase_run_id,
    )
    if not route_on_failure:
        # Verdict is durable; the task stays in review and stays non-complete.
        return True, VERIFICATION_FAILED
    # Repeated FAIL: hold in review for a person instead of reopening another
    # paid rework run against the same finding. Records the hold once.
    if _verifier_respawn_unchanged(conn, task_id) and _repeated_fail_anchor(conn, task_id):
        # Kernel part 3 (t_6450901b): the held subject gets a governed
        # repair card built from the verifier's finding instead of waiting
        # on a person. Never needs_input; the subject stays in review.
        _findings = reason_text or "independent verification failed"
        _vtask = (evidence or {}).get("verifier_task") if isinstance(evidence, dict) else None
        if _vtask:
            # The finding itself is the verifier card's own output.
            _vrow = conn.execute(
                "SELECT summary FROM task_runs WHERE task_id = ? "
                "AND summary IS NOT NULL ORDER BY id DESC LIMIT 1",
                (_vtask,),
            ).fetchone()
            _vres = conn.execute(
                "SELECT result FROM tasks WHERE id = ?", (_vtask,)
            ).fetchone()
            _vtext = ((_vrow["summary"] if _vrow else None)
                      or (_vres["result"] if _vres else None) or "").strip()
            if _vtext:
                _findings += "\n\n" + _vtext[:3000]
        if evidence:
            try:
                _findings += "\n\nVerifier evidence: " + json.dumps(
                    redact_review_value(evidence)
                )[:2000]
            except (TypeError, ValueError):
                pass
        _start_verifier_fail_repair(conn, task_id, _findings)
        return True, VERIFICATION_FAILED

    # Route to repair/rework. Both helpers open their own write transaction,
    # so they run after the verdict is already committed — a failure to route
    # therefore leaves a task that is blocked from completing, never one that
    # slipped through.
    if review_run_id is not None:
        ok, _detail = request_changes(
            conn, task_id, reason=reason_text or "verification failed",
            expected_run_id=review_run_id,
        )
        if not ok:
            return True, VERIFICATION_FAILED
        return True, "rework"
    if reopen_review_task(conn, task_id):
        return True, "rework"
    return True, VERIFICATION_FAILED


def verification_history(
    conn: sqlite3.Connection, task_id: str
) -> list[dict[str, Any]]:
    """Return the task's verification ledger, oldest first.

    The audit trail behind ``tasks.verification_state``: which phase starts,
    verdicts, verifiers and evidence produced the current head. Survives the
    head being cleared by a re-claim, so a completed task's history shows what
    was verified and when.
    """
    rows = conn.execute(
        "SELECT id, run_id, state, verifier, evidence, reason, created_at, "
        "       kind, covers_phase_id "
        "FROM task_verifications WHERE task_id = ? ORDER BY id",
        (task_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            evidence = json.loads(r["evidence"]) if r["evidence"] else None
        except (json.JSONDecodeError, TypeError):
            evidence = None
        out.append(
            {
                "id": int(r["id"]),
                "run_id": r["run_id"],
                "state": r["state"],
                "verifier": r["verifier"],
                "evidence": evidence,
                "reason": r["reason"],
                "created_at": r["created_at"],
                # 'verdict' | 'regression' — what this row is evidence OF.
                "kind": r["kind"] or LEDGER_KIND_VERDICT,
                "covers_phase_id": r["covers_phase_id"],
            }
        )
    return out


def set_gauntlet_enforced(
    conn: sqlite3.Connection, task_id: str, enabled: bool
) -> bool:
    """Turn the per-task Gauntlet chain on or off. Returns False if unknown.

    Turning it *off* does not erase the ledger; it only stops the completion
    guard consulting the head. ``kanban.gauntlet_enforcement`` still applies
    board-wide regardless of this flag.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET gauntlet_enforced = ? WHERE id = ?",
            (1 if enabled else 0, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "gauntlet_enforcement",
            {"enabled": bool(enabled)},
        )
    return True


class LessonPromotionError(ValueError):
    """A lesson promotion was refused. ``code`` classifies why.

    Structured because every surface has to say the same thing about the same
    refusal: the CLI prints it, the worker tool turns it into a retryable
    error naming the legal next step, and the tests assert on the code rather
    than on prose.
    """

    def __init__(self, code: str, message: str, **detail: Any):
        self.code = code
        self.detail = detail
        super().__init__(message)


LESSON_STATE_CANDIDATE = "candidate"
LESSON_STATE_ACTIVE = "active"
LESSON_STATE_RETIRED = "retired"
VALID_LESSON_STATES = {
    LESSON_STATE_CANDIDATE, LESSON_STATE_ACTIVE, LESSON_STATE_RETIRED,
}
LESSON_DEFAULT_REVIEW_DAYS = 90
_LESSON_REVIEW_REQUIRED_RE = re.compile(
    r"\b("
    r"architect(ure|ural)?"
    r"|governance|doctrine|hierarchy|authority|escalat(e|ion)"
    r"|entity[ _-]?(registry|routing)|rout(e|ing)[ _-]?(rule|change|table)"
    r"|cross[ _-]?(company|entity|tenant)|company[ _-]?lane|tenant[ _-]?boundary"
    r"|polic(y|ies)|always|never|every[ _-]?(task|worker|agent|profile)"
    r")\b",
    re.IGNORECASE,
)


def lesson_promotion_eligibility(
    scope: str, applicability: str, lesson: str
) -> tuple[bool, str]:
    """May this candidate promote itself, or must an operator rule on it?

    Deterministic and structural -- no model judgement. Three refusals, each
    keyed on something already in the data model:

    * ``scope='global'`` -- reaches every company. Cross-company reach is
      exactly the class that needs a human.
    * ``applicability='all'`` -- unbounded. A broad behavioural rule binds work
      nobody reviewed it against.
    * governance/architecture/routing language -- an interpretation, not an
      observation.

    Anything narrow, single-tenant and free of those is safe to promote:
    "do not count provider 429 as implementation failure" constrains one
    selector and asserts nothing about how the company is run.
    """
    if scope == LESSON_SCOPE_GLOBAL:
        return False, "global_scope_needs_operator"
    if (applicability or "").strip().lower() == LESSON_SELECTOR_ALL:
        return False, "unbounded_applicability_needs_operator"
    if _LESSON_REVIEW_REQUIRED_RE.search(lesson or ""):
        return False, "governance_impacting_needs_operator"
    return True, "narrow_and_scoped"


def normalize_lesson_applicability(value: Optional[str]) -> str:
    """Normalise and validate an applicability selector. Raises on garbage.

    The selector is the whole matching mechanism, so it is deliberately a
    closed grammar rather than free text: either the literal ``all`` or
    ``<dimension>:<value>`` for a dimension in
    :data:`LESSON_SELECTOR_DIMENSIONS`. Case and surrounding whitespace are
    normalised at write time so that matching downstream is plain string
    equality — no trimming, no case folding, no partial matches, no
    inference.
    """
    text = (value or "").strip().lower()
    if not text:
        raise LessonPromotionError(
            "empty_applicability",
            "applicability is required: name exactly which future tasks this "
            "lesson binds, e.g. 'all', 'assignee:coder', 'project:api', "
            f"'workspace:repo'. Valid dimensions: "
            f"{', '.join(sorted(LESSON_SELECTOR_DIMENSIONS))}.",
        )
    if text == LESSON_SELECTOR_ALL:
        return LESSON_SELECTOR_ALL
    dimension, sep, target = text.partition(":")
    target = target.strip()
    if not sep or not target or dimension not in LESSON_SELECTOR_DIMENSIONS:
        raise LessonPromotionError(
            "invalid_applicability",
            f"applicability {text!r} is not a valid selector. Use 'all' or "
            f"'<dimension>:<value>' with dimension in "
            f"{', '.join(sorted(LESSON_SELECTOR_DIMENSIONS))} — matching is "
            f"exact, so a selector this kernel cannot evaluate is refused "
            f"rather than silently never firing.",
        )
    return f"{dimension}:{target}"


def task_lesson_selectors(task: Task) -> list[str]:
    """Return the exact selector strings ``task`` matches, in stable order.

    Read straight off the task row. This is the receiving half of the exact
    match: a lesson fires for this task if and only if its ``applicability``
    is one of these strings.
    """
    selectors = [LESSON_SELECTOR_ALL]
    for dimension in sorted(LESSON_SELECTOR_DIMENSIONS):
        raw = getattr(task, LESSON_SELECTOR_DIMENSIONS[dimension], None)
        value = str(raw).strip().lower() if raw is not None else ""
        if value:
            selectors.append(f"{dimension}:{value}")
    return selectors


def _latest_verdict_row(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    """Most recent ``kind='verdict'`` ledger row for a task, or None."""
    return conn.execute(
        "SELECT id, state, verifier, evidence, reason, created_at "
        "FROM task_verifications "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, LEDGER_KIND_VERDICT),
    ).fetchone()


def _ever_failed_verification(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when this task's ledger contains any failing verdict, ever."""
    return conn.execute(
        "SELECT 1 FROM task_verifications "
        "WHERE task_id = ? AND kind = ? AND state = ? LIMIT 1",
        (task_id, LEDGER_KIND_VERDICT, VERIFICATION_FAILED),
    ).fetchone() is not None


def lesson_source_state(
    conn: sqlite3.Connection, task_id: str
) -> dict[str, Any]:
    """Decide whether ``task_id`` is a legitimate source of canonical learning.

    Returns a dict with ``ok`` plus, on refusal, ``code``/``detail``; on
    success, the ledger ids that authorised it. The rule, in order:

    * The head must be a durable ``verified`` — not ``pending`` (nobody has
      ruled yet), not ``failed`` (it was ruled against), and not ``None``
      (either it never entered the chain, or a later implementation run
      invalidated the verdict, which is exactly the case where a lesson would
      be drawn from work the verifier never saw).
    * No regression debt may be outstanding: ``regression_required`` must be
      clear. This is the same pair ``complete_task`` tests, so "may promote a
      lesson" and "may complete" cannot drift apart.
    * If the task EVER failed verification, the passing verdict must name the
      regression proof it spent (``evidence.regression_proof_id``) and that
      proof must be a real ledger row for this task recorded after the failure
      that armed it. A repair blessed without re-running the checks is exactly
      the "unresolved attempt" the contract keeps out of canon.
    """
    row = conn.execute(
        "SELECT id, tenant, status, verification_state, regression_required "
        "FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {
            "ok": False,
            "code": "unknown_task",
            "detail": f"task {task_id} not found",
        }
    keys = set(row.keys())
    state = row["verification_state"] if "verification_state" in keys else None
    regression_required = bool(
        "regression_required" in keys and row["regression_required"]
    )
    base = {
        "task_id": task_id,
        "tenant": row["tenant"] if "tenant" in keys else None,
        "status": row["status"],
        "verification_state": state,
    }

    if state != VERIFICATION_VERIFIED:
        if state == VERIFICATION_FAILED:
            detail = (
                f"task {task_id} FAILED verification; a failed attempt stays "
                f"an observation, never canonical learning. Repair it, "
                f"re-verify with regression evidence, then promote."
            )
        elif state == VERIFICATION_PENDING:
            detail = (
                f"task {task_id} is still awaiting a verdict; nothing is "
                f"verified yet, so there is nothing to promote."
            )
        else:
            detail = (
                f"task {task_id} has no live verification (state="
                f"{state!r}). Either it never entered the Gauntlet chain, or "
                f"a later implementation run invalidated the verdict — "
                f"either way the lesson would not be backed by a verifier."
            )
        return {**base, "ok": False, "code": "not_verified", "detail": detail}

    if regression_required:
        return {
            **base,
            "ok": False,
            "code": "regression_outstanding",
            "detail": (
                f"task {task_id} still owes regression evidence for a "
                f"material repair; its verified head cannot be spent on a "
                f"lesson (or a completion) until a passing verdict carries "
                f"the proof."
            ),
        }

    verdict = _latest_verdict_row(conn, task_id)
    if verdict is None or verdict["state"] != VERIFICATION_VERIFIED:
        # The head says verified but the ledger behind it does not agree.
        return {
            **base,
            "ok": False,
            "code": "ledger_mismatch",
            "detail": (
                f"task {task_id} reads verified but its verification ledger "
                f"does not show a passing verdict as the latest entry; "
                f"refusing to promote learning with no evidence behind it."
            ),
        }

    try:
        verdict_evidence = json.loads(verdict["evidence"] or "null")
    except (json.JSONDecodeError, TypeError):
        verdict_evidence = None
    proof_id = None
    if isinstance(verdict_evidence, dict):
        raw_proof = verdict_evidence.get("regression_proof_id")
        if isinstance(raw_proof, int):
            proof_id = raw_proof

    if _ever_failed_verification(conn, task_id):
        armed_after = _arming_failure_id(conn, task_id)
        proof_ok = False
        if proof_id is not None:
            proof_ok = conn.execute(
                "SELECT 1 FROM task_verifications "
                "WHERE id = ? AND task_id = ? AND kind = ? AND id > ? LIMIT 1",
                (proof_id, task_id, LEDGER_KIND_REGRESSION, armed_after),
            ).fetchone() is not None
        if not proof_ok:
            return {
                **base,
                "ok": False,
                "code": "regression_proof_missing",
                "detail": (
                    f"task {task_id} failed verification at least once, so "
                    f"its passing verdict must name the regression proof it "
                    f"spent. The ledger shows no such proof recorded after "
                    f"the failure, so the repair was never demonstrated and "
                    f"cannot become canonical learning."
                ),
            }

    return {
        **base,
        "ok": True,
        "verification_id": int(verdict["id"]),
        "regression_proof_id": proof_id,
        "verifier": verdict["verifier"],
        "verified_at": verdict["created_at"],
        "ever_failed": _ever_failed_verification(conn, task_id),
    }


def _lesson_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    try:
        evidence = json.loads(row["evidence"]) if row["evidence"] else None
    except (json.JSONDecodeError, TypeError):
        evidence = None
    return {
        "id": int(row["id"]),
        "source_task_id": row["source_task_id"],
        "tenant": row["tenant"],
        "scope": row["scope"],
        "applicability": row["applicability"],
        "lesson": row["lesson"],
        "evidence": evidence,
        "verification_id": row["verification_id"],
        "regression_proof_id": row["regression_proof_id"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "active": bool(row["active"]),
        "retired_at": row["retired_at"] if "retired_at" in keys else None,
        "retired_by": row["retired_by"] if "retired_by" in keys else None,
        "state": (
            row["state"] if "state" in keys else LESSON_STATE_ACTIVE
        ),
        "review_by": row["review_by"] if "review_by" in keys else None,
        "retire_condition": (
            row["retire_condition"] if "retire_condition" in keys else None
        ),
        "retired_reason": (
            row["retired_reason"] if "retired_reason" in keys else None
        ),
    }


def promote_lesson(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lesson: str,
    applicability: str,
    actor: Optional[str] = None,
    evidence: Optional[dict] = None,
    allow_global: bool = False,
    gate_promotion: bool = False,
) -> dict[str, Any]:
    """Promote a verified task's finding into a durable, binding lesson.

    ``gate_promotion`` applies :func:`lesson_promotion_eligibility` to the
    DERIVED scope at insert time, so an ineligible lesson is written as a
    non-binding ``candidate`` in the same transaction that creates it. It is a
    parameter here rather than a follow-up UPDATE in the caller because a
    second transaction would leave a window in which an unapproved
    interpretation is live and binding.

    The one way a row enters ``task_lessons``. Refuses — raising
    :class:`LessonPromotionError` and writing nothing — unless all of:

    * ``lesson`` and ``applicability`` are non-empty, and the selector is one
      this kernel can actually evaluate (:func:`normalize_lesson_applicability`).
    * The source task is durably VERIFIED with any material-repair proof
      already spent (:func:`lesson_source_state`).
    * The scope is safe: a tenant-scoped source yields a lesson only that
      tenant's tasks can receive, and never a global one. An untenanted
      source yields nothing unless ``allow_global`` — an operator saying, in
      as many words, "this belongs to every lane".

    Returns the created lesson row as a dict. The refusal is recorded as a
    ``lesson_promotion_blocked`` event on the source task, so an attempt to
    launder an unverified claim into canon is as auditable as a successful
    promotion. That event is written in its own transaction *after* the
    refusal has rolled the promotion back — an audit row inside the aborted
    transaction would roll back with it and the attempt would leave no trace.
    """
    text = (lesson or "").strip()
    selector = normalize_lesson_applicability(applicability)
    actor = _canonical_assignee(actor) if actor else None

    if not text:
        raise LessonPromotionError(
            "empty_lesson",
            "lesson text is required: state the rule future work must "
            "follow, not that one exists.",
        )
    if len(text) > LESSON_MAX_CHARS:
        raise LessonPromotionError(
            "lesson_too_long",
            f"lesson text is {len(text)} chars; the limit is "
            f"{LESSON_MAX_CHARS}. A binding rule that does not fit in a "
            f"paragraph is a document, and belongs on the card instead.",
        )
    if evidence is not None and not isinstance(evidence, dict):
        raise LessonPromotionError(
            "invalid_evidence",
            "evidence must be a JSON object of falsifiable facts",
        )
    # A lesson is injected verbatim into future workers' prompts, so it goes
    # through the same redaction boundary as every other durable handoff.
    text = str(redact_review_value(text)).strip()

    # Set when the promotion is refused inside the transaction below. The
    # refusal has to leave the write txn before it is raised or audited, so
    # the decision is carried out rather than thrown out.
    refusal: Optional[LessonPromotionError] = None
    row = None
    with write_txn(conn):
        state = lesson_source_state(conn, task_id)
        source_tenant = state.get("tenant")
        if not state["ok"]:
            refusal = LessonPromotionError(
                state["code"], state["detail"],
                verification_state=state.get("verification_state"),
            )
        elif source_tenant and allow_global:
            refusal = LessonPromotionError(
                "tenant_cannot_go_global",
                f"task {task_id} belongs to tenant {source_tenant!r}; a "
                f"tenant-scoped source cannot produce a global lesson. Its "
                f"learning stays inside its own lane.",
                tenant=source_tenant,
            )
        elif not source_tenant and not allow_global:
            refusal = LessonPromotionError(
                "global_not_authorised",
                f"task {task_id} has no tenant, so promoting it would create "
                f"a lesson binding on every lane. That is an operator "
                f"decision: re-run with the explicit global flag if it "
                f"genuinely applies board-wide.",
            )

        if refusal is None:
            if source_tenant:
                scope, tenant = LESSON_SCOPE_TENANT, source_tenant
            else:
                scope, tenant = LESSON_SCOPE_GLOBAL, None

            provenance: dict[str, Any] = {
                "source_status": state["status"],
                "verification_state": state["verification_state"],
                "verification_id": state["verification_id"],
                "verified_at": state["verified_at"],
                "verifier": state["verifier"],
                "source_ever_failed_verification": state["ever_failed"],
            }
            if state["regression_proof_id"] is not None:
                provenance["regression_proof_id"] = state["regression_proof_id"]
            if evidence:
                provenance["promoter_evidence"] = redact_review_value(evidence)

            now = int(time.time())
            # The gate runs HERE, against the scope this function derived, so
            # the row is never briefly binding before being downgraded.
            if gate_promotion:
                eligible, gate_reason = lesson_promotion_eligibility(
                    scope, selector, text
                )
            else:
                eligible, gate_reason = True, "ungated"
            lesson_state = (
                LESSON_STATE_ACTIVE if eligible else LESSON_STATE_CANDIDATE
            )
            cur = conn.execute(
                """
                INSERT INTO task_lessons
                    (source_task_id, tenant, scope, applicability, lesson,
                     evidence, verification_id, regression_proof_id,
                     created_by, created_at, active, state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    tenant,
                    scope,
                    selector,
                    text,
                    json.dumps(provenance, ensure_ascii=False, sort_keys=True),
                    state["verification_id"],
                    state["regression_proof_id"],
                    actor,
                    now,
                    1 if eligible else 0,
                    lesson_state,
                ),
            )
            lesson_id = int(cur.lastrowid)
            _append_event(
                conn, task_id,
                "lesson_promoted" if eligible else "lesson_candidate_recorded",
                {
                    "lesson_id": lesson_id,
                    "state": lesson_state,
                    "eligibility_reason": gate_reason,
                    "scope": scope,
                    "tenant": tenant,
                    "applicability": selector,
                    "actor": actor,
                    "verification_id": state["verification_id"],
                    "regression_proof_id": state["regression_proof_id"],
                },
            )
            row = conn.execute(
                "SELECT * FROM task_lessons WHERE id = ?", (lesson_id,)
            ).fetchone()

    if refusal is not None:
        # Nothing was written by the aborted transaction. Record the attempt
        # itself — a refused promotion is exactly the event a later reader
        # wants to see, because it is someone trying to make unverified work
        # canonical. Skipped for an unknown task: there is no card to hang it
        # on, and task_events would carry a dangling id.
        if refusal.code != "unknown_task":
            with write_txn(conn):
                _append_event(
                    conn, task_id, "lesson_promotion_blocked",
                    {
                        "code": refusal.code,
                        "detail": str(refusal),
                        "actor": actor,
                        "applicability": selector,
                        **refusal.detail,
                    },
                )
        raise refusal
    return _lesson_row_to_dict(row)


def retire_lesson(
    conn: sqlite3.Connection,
    lesson_id: int,
    *,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Retire a lesson without erasing that it once existed or governed.

    Sets ``state`` to ``retired`` and ``active`` to 0, and stamps who/when/why.
    The row stays, the ``lesson_promoted`` / ``lesson_candidate_recorded``
    event stays, and a ``lesson_retired`` event is appended to the source
    task — so the history reads "this rule bound work between these two
    dates" (or "this candidate was considered and declined"), which is the
    question a later reader actually has.

    Both undecided states retire to the same final state:

    * ``active`` — the rule stops binding future work.
    * ``candidate`` — the operator declines it; it leaves the decision queue
      (:func:`lesson_candidates`) without ever having bound anything.

    Retirement is decided on ``state``, not on ``active``. A candidate is
    ``active = 0`` from the moment it is recorded, so testing ``active`` alone
    made every candidate look "already retired" and left the operator with no
    supported way to clear the queue (2026-10-12, candidate rows 213-295).
    Idempotent: a row that is already ``state = retired`` — or that already
    carries a retirement stamp — is refused rather than silently re-stamped.
    """
    actor = _canonical_assignee(actor) if actor else None
    reason_text = str(redact_review_value(reason or "")).strip() or None
    with write_txn(conn):
        row = conn.execute(
            "SELECT id, source_task_id, active, state, retired_at "
            "FROM task_lessons WHERE id = ?",
            (int(lesson_id),),
        ).fetchone()
        if row is None:
            return False, f"lesson {lesson_id} not found"
        prior_state = str(row["state"] or LESSON_STATE_ACTIVE)
        if prior_state == LESSON_STATE_RETIRED or row["retired_at"] is not None:
            return False, f"lesson {lesson_id} is already retired"
        now = int(time.time())
        cur = conn.execute(
            "UPDATE task_lessons "
            "SET state = ?, active = 0, retired_at = ?, retired_by = ?, "
            "    retired_reason = ? "
            "WHERE id = ? AND state != ? AND retired_at IS NULL",
            (
                LESSON_STATE_RETIRED, now, actor, reason_text,
                int(lesson_id), LESSON_STATE_RETIRED,
            ),
        )
        if cur.rowcount != 1:
            # Lost the race to a concurrent retirement inside the same
            # instant; the guard above already read a non-retired row, so
            # this is the only way the UPDATE can miss.
            return False, f"lesson {lesson_id} is already retired"
        _append_event(
            conn, row["source_task_id"], "lesson_retired",
            {
                "lesson_id": int(lesson_id),
                "from_state": prior_state,
                "was_binding": bool(row["active"]),
                "actor": actor,
                "reason": reason_text,
            },
        )
    return True, None


def get_lesson(
    conn: sqlite3.Connection, lesson_id: int
) -> Optional[dict[str, Any]]:
    """One lesson row by id in the same shape :func:`list_lessons` returns,
    whatever its state (candidate, active or retired). ``None`` when absent.

    Read-only. The tool surface reads a lesson before and after a retirement
    so the caller gets read-back evidence rather than a bare ``ok``.
    """
    row = conn.execute(
        "SELECT * FROM task_lessons WHERE id = ?", (int(lesson_id),)
    ).fetchone()
    return _lesson_row_to_dict(row) if row is not None else None


def list_lessons(
    conn: sqlite3.Connection,
    *,
    active_only: bool = True,
    tenant: Optional[str] = None,
    source_task_id: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """List promoted lessons, newest first. Read-only audit surface.

    ``tenant`` filters to one lane's lessons plus the global ones — the same
    visibility a task in that tenant has. Pass ``active_only=False`` to see
    retired rows too (the history retirement deliberately preserves).
    """
    query = "SELECT * FROM task_lessons WHERE 1=1"
    params: list[Any] = []
    if active_only:
        query += " AND active = 1"
    if tenant is not None:
        query += " AND (tenant = ? OR scope = ?)"
        params.extend([tenant, LESSON_SCOPE_GLOBAL])
    if source_task_id is not None:
        query += " AND source_task_id = ?"
        params.append(source_task_id)
    query += " ORDER BY id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    return [
        _lesson_row_to_dict(row) for row in conn.execute(query, params)
    ]


def extract_lesson(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    lesson: str,
    applicability: str,
    actor: Optional[str] = None,
    evidence: Optional[dict] = None,
    allow_global: bool = False,
    review_after_days: int = LESSON_DEFAULT_REVIEW_DAYS,
    retire_condition: Optional[str] = None,
) -> dict[str, Any]:
    """The required post-VERIFIED step. Promotes only when narrow and safe.

    Runs every existing :func:`promote_lesson` gate first -- verified head, no
    outstanding regression debt, a named regression proof when the source ever
    failed -- so an unverified task still produces nothing. Then applies
    :func:`lesson_promotion_eligibility`:

    * eligible -> stays ``active`` and binds future matching work.
    * not eligible -> downgraded to ``candidate``: recorded, auditable,
      attributable, and binding nothing until an operator approves it.

    The distinction matters because a verifier proves an IMPLEMENTATION met its
    acceptance criteria. It does not prove the INTERPRETATION drawn from the
    incident should become doctrine.
    """
    # gate_promotion=True makes the eligibility decision part of the INSERT, so
    # an ineligible lesson is NEVER briefly active.
    row = promote_lesson(
        conn,
        task_id,
        lesson=lesson,
        applicability=applicability,
        actor=actor,
        evidence=evidence,
        allow_global=allow_global,
        gate_promotion=True,
    )
    lesson_id = int(row["id"])
    eligible = bool(row.get("active"))
    reason = lesson_promotion_eligibility(
        str(row.get("scope") or LESSON_SCOPE_TENANT),
        str(row.get("applicability") or applicability),
        lesson,
    )[1]
    now = int(time.time())
    review_by = now + int(review_after_days) * 86400 if review_after_days else None
    # Only the review horizon is set here. It gates nothing, so a second
    # statement is harmless -- unlike the binding state, which is not.
    with write_txn(conn):
        conn.execute(
            "UPDATE task_lessons SET review_by = ?, retire_condition = ? "
            "WHERE id = ?",
            (review_by, retire_condition, lesson_id),
        )
    out = dict(row)
    out.update(
        state=LESSON_STATE_ACTIVE if eligible else LESSON_STATE_CANDIDATE,
        auto_promoted=eligible,
        eligibility_reason=reason,
        review_by=review_by,
        retire_condition=retire_condition,
    )
    return out


def approve_lesson(
    conn: sqlite3.Connection,
    lesson_id: int,
    *,
    approver: str,
) -> bool:
    """Operator approval of a candidate. Returns False if it is not one.

    Deliberately requires a named approver: a lesson that binds every future
    matching task should carry who decided it binds.
    """
    if not (approver or "").strip():
        raise LessonPromotionError(
            "unnamed_approver",
            "approving a candidate lesson requires a named approver",
        )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE task_lessons SET state = ?, active = 1 "
            "WHERE id = ? AND state = ?",
            (LESSON_STATE_ACTIVE, int(lesson_id), LESSON_STATE_CANDIDATE),
        )
        if cur.rowcount != 1:
            return False
        row = conn.execute(
            "SELECT source_task_id FROM task_lessons WHERE id = ?", (int(lesson_id),)
        ).fetchone()
        if row is not None:
            _append_event(
                conn, str(row["source_task_id"]), "lesson_candidate_approved",
                {"lesson_id": int(lesson_id), "approver": approver},
            )
    return True


LESSON_MATCH_MIN_SCORE = 0.30
LESSON_MATCH_MIN_TERMS = 2
_ERROR_STOPWORDS = frozenset({
    "error", "errors", "failed", "failure", "exception", "traceback", "call",
    "last", "recent", "line", "file", "self", "none", "true", "false", "with",
    "from", "this", "that", "then", "than", "have", "been", "when", "which",
    "while", "into", "your", "must", "does", "will", "would", "could", "such",
    "return", "returns", "returned", "raise", "raised", "python", "module",
})


def _error_signature_terms(text: str) -> set:
    """Significant terms from an error, with the variable parts stripped.

    Two occurrences of the SAME error differ in their ids, paths, line numbers
    and timestamps. Matching on raw text therefore makes every recurrence look
    novel -- which is exactly how a bounded "only a new error opens a loop"
    rule degrades back into an unbounded one. Strip those first.
    """
    import re as _re

    lowered = (text or "").lower()
    lowered = _re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b", " ", lowered)
    lowered = _re.sub(r"[a-z]?[:/][^\s'\"]+", " ", lowered)
    lowered = _re.sub(r"\b\d[\d.:_-]*\b", " ", lowered)
    terms = {t for t in _re.split(r"[^a-z_]+", lowered) if len(t) > 3}
    return terms - _ERROR_STOPWORDS


def error_signature(text: str) -> str:
    """A stable key for an error, safe to compare across occurrences.

    The sorted, normalised significant terms joined by spaces. Two occurrences
    of the same failure differ in ids, paths, line numbers and timestamps;
    those are stripped by :func:`_error_signature_terms`, so both produce the
    same key. Empty when the text carries no signal.
    """
    return " ".join(sorted(_error_signature_terms(text)))


def record_error_lesson(
    conn: sqlite3.Connection,
    *,
    error_text: str,
    lesson: str,
    source_task_id: str,
    created_by: str = "loop",
    tenant: Optional[str] = None,
) -> dict:
    """Log the fix for an error, keyed by that error's signature.

    This is the write half of retrieval-on-error, and the step that makes the
    loop compound: an error is hit, diagnosed and fixed once, and every later
    occurrence finds the fix by exact signature match rather than by hoping
    two prose descriptions share vocabulary.

    Recorded as a CANDIDATE (``active = 0``). It is retrievable immediately —
    retrieval searches the whole corpus — but binds nothing until somebody
    approves it. Logging a fix must never silently create a rule that
    constrains every future task.
    """
    sig = error_signature(error_text)
    if not sig:
        raise ValueError("error_text carries no significant terms to key on")
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            "INSERT INTO task_lessons (source_task_id, tenant, scope, "
            "applicability, lesson, error_signature, created_by, created_at, "
            "active, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                source_task_id,
                tenant,
                LESSON_SCOPE_TENANT if tenant else LESSON_SCOPE_GLOBAL,
                "all",
                lesson,
                sig,
                created_by,
                now,
                LESSON_STATE_CANDIDATE,
            ),
        )
        lid = int(cur.lastrowid or 0)
        _append_event(
            conn, source_task_id, "error_lesson_recorded",
            {"lesson_id": lid, "signature_terms": len(sig.split())},
        )
    return {"id": lid, "error_signature": sig, "lesson": lesson}


def count_lessons(conn: sqlite3.Connection) -> int:
    """Size of the whole retrievable corpus, bound or not.

    Tolerates the table being absent. ``build_worker_context`` runs on legacy
    boards that predate ``task_lessons`` and on boards mid-migration; a
    context builder must never be the thing that breaks them. Returns 0, which
    correctly renders no retrieval pointer.
    """
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM task_lessons").fetchone()[0]
        )
    except sqlite3.Error:
        return 0


def lessons_for_error(
    conn: sqlite3.Connection,
    error_text: str,
    *,
    limit: int = 5,
    tenant: Optional[str] = None,
) -> list:
    """Lessons whose recorded rule matches this error's signature.

    Searches the WHOLE corpus, not just ``active`` rows. That distinction is
    the point of this design: ``active`` now means "binds unconditionally"
    (and should be near-empty), while every row -- candidate, active or
    retired -- stays retrievable when something actually goes wrong. A lesson
    nobody chose to make binding is still the best available evidence about an
    error somebody already hit.

    Scored by overlap of normalised signature terms, best first. Returns [] on
    empty input rather than the whole table, so a caller that fails to capture
    the error text gets nothing instead of everything.
    """
    terms = _error_signature_terms(error_text)
    if not terms:
        return []
    sql = "SELECT * FROM task_lessons"
    params: list = []
    if tenant:
        sql += " WHERE tenant = ? OR scope = ?"
        params += [tenant, LESSON_SCOPE_GLOBAL]

    # Signature match first: exact, no heuristics, and the reason recording
    # the error at fix time is worth doing. Only rows that HAVE a signature can
    # match this way, so legacy prose falls through to the overlap fallback
    # below.
    query_sig = " ".join(sorted(terms))
    exact = []
    for row in conn.execute(
        sql + (" AND " if tenant else " WHERE ") + "error_signature = ?",
        tuple(params) + (query_sig,),
    ):
        lesson = _lesson_row_to_dict(row)
        lesson["match_score"] = "exact"
        lesson["match_terms"] = sorted(terms)
        exact.append(lesson)
    if exact:
        return exact[:max(1, int(limit))]

    corpus = []
    doc_freq: dict = {}
    for row in conn.execute(sql, tuple(params)):
        lesson = _lesson_row_to_dict(row)
        lesson_terms = _error_signature_terms(lesson["lesson"])
        corpus.append((lesson, lesson_terms))
        for t in lesson_terms:
            doc_freq[t] = doc_freq.get(t, 0) + 1

    # Weight each shared term by how RARE it is in the corpus. Raw overlap
    # counting let common words dominate: a here-document failure matched a
    # lesson about wrapper syntax on the word "syntax" alone, and missed the
    # heredoc lesson entirely. A term in two lessons is evidence; a term in
    # ninety is noise.
    scored = []
    for lesson, lesson_terms in corpus:
        overlap = terms & lesson_terms
        # Corroboration before rarity. A single shared word between an error
        # and a paragraph of prose is a coincidence: pure IDF ranked a
        # here-document failure against a lesson about unlabelled icons on the
        # word "near" alone, because "near" happened to appear in exactly one
        # lesson and therefore scored the maximum.
        if len(overlap) < LESSON_MATCH_MIN_TERMS:
            continue
        score = sum(1.0 / doc_freq.get(t, 1) for t in overlap)
        if score < LESSON_MATCH_MIN_SCORE:
            continue
        scored.append((score, -int(lesson["id"]), lesson, overlap))

    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    out = []
    for score, _, lesson, overlap in scored[:max(1, int(limit))]:
        lesson["match_score"] = round(score, 3)
        lesson["match_terms"] = sorted(
            overlap, key=lambda t: doc_freq.get(t, 1)
        )
        out.append(lesson)
    return out


def lesson_candidates(
    conn: sqlite3.Connection, tenant: Optional[str] = None
) -> list[dict[str, Any]]:
    """Candidates awaiting an operator decision. These bind nothing."""
    sql = "SELECT * FROM task_lessons WHERE state = ?"
    params: list[Any] = [LESSON_STATE_CANDIDATE]
    if tenant:
        sql += " AND tenant = ?"
        params.append(tenant)
    return [
        _lesson_row_to_dict(r)
        for r in conn.execute(sql + " ORDER BY id", tuple(params))
    ]


def lessons_due_for_review(
    conn: sqlite3.Connection, now: Optional[int] = None
) -> list[dict[str, Any]]:
    """Active lessons past their review horizon.

    ``retire_lesson`` has always existed and nothing ever called it, so a
    lesson that stopped being true kept constraining work. This is the query
    that makes expiry actionable rather than theoretical.
    """
    stamp = int(now if now is not None else time.time())
    return [
        _lesson_row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM task_lessons WHERE state = ? AND review_by IS NOT NULL "
            "AND review_by <= ? ORDER BY review_by",
            (LESSON_STATE_ACTIVE, stamp),
        )
    ]


def lessons_for_task(
    conn: sqlite3.Connection, task_id: str
) -> list[dict[str, Any]]:
    """Return the active lessons that bind ``task_id``, oldest first.

    Two independent gates, both exact and both in SQL:

    * SCOPE — a tenant-scoped lesson reaches only tasks in that same tenant.
      A global lesson (which only an operator can create, and only from a
      source that had no tenant of its own) reaches every task; that is what
      global means, and it carries no company-private provenance by
      construction.
    * APPLICABILITY — the lesson's selector must be exactly one of the
      selectors the receiving task itself produces
      (:func:`task_lesson_selectors`). No fuzzy, semantic, or prefix
      matching: a selector that does not match exactly does not fire.

    This is the function :func:`build_worker_context` calls, so what a worker
    is bound by is decided here by the kernel, not by a model recalling it.
    """
    task = get_task(conn, task_id)
    if task is None:
        return []
    selectors = task_lesson_selectors(task)
    placeholders = ",".join("?" for _ in selectors)
    params: list[Any] = [*selectors]
    if task.tenant:
        scope_clause = "(scope = ? OR (scope = ? AND tenant = ?))"
        params.extend([LESSON_SCOPE_GLOBAL, LESSON_SCOPE_TENANT, task.tenant])
    else:
        # An untenanted task is in no lane, so it can only receive lessons
        # that belong to no lane either. Defaulting the other way would make
        # "no tenant" the widest possible read of every company's learning.
        scope_clause = "scope = ?"
        params.append(LESSON_SCOPE_GLOBAL)
    rows = conn.execute(
        f"SELECT * FROM task_lessons "
        f"WHERE active = 1 AND applicability IN ({placeholders}) "
        f"  AND {scope_clause} "
        f"ORDER BY id",
        params,
    ).fetchall()
    return [_lesson_row_to_dict(r) for r in rows]


def promote_task(
    conn: sqlite3.Connection, task_id: str, *, actor: str, reason: Optional[str] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Operator promotion ``todo``/``blocked`` -> ``ready`` with an audit event.
    Refused while a parent is unfinished; ``dry_run`` only validates.
    Returns ``(ok, reason)``."""
    row = conn.execute(
        "SELECT status, terminal_disposition FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"

    # Checked before the status check so the operator gets the real reason
    # ("this card is terminal") rather than the generic status complaint.
    # Only IRREVERSIBLE dispositions bar promotion — a reopened 'completed'
    # card is ordinary work again.
    if row["terminal_disposition"] in IRREVERSIBLE_DISPOSITIONS:
        return False, (
            f"task {task_id} is terminal "
            f"(disposition {row['terminal_disposition']!r}); it cannot be "
            f"promoted back into the queue"
        )

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    loop_ok, loop_reason = validation_loop_entry_valid(conn, task_id)
    if not loop_ok:
        return False, f"validation-loop admission refused: {loop_reason}"

    # No override: claim_task demotes ready -> todo on an undone parent whichever
    # writer set 'ready', so a forced promotion would only report a success the
    # first claim silently reverts (#106195). The dependency itself is the knob.
    parents = conn.execute(
        "SELECT t.id, t.status, t.terminal_disposition FROM tasks t "
        "JOIN task_links l ON l.parent_id = t.id "
        "WHERE l.child_id = ?", (task_id,),
    ).fetchall()
    unsatisfied = [
        p["id"] for p in parents
        # Same terminality rule as _parents_satisfied: an irreversibly
        # disposed parent is done blocking anything, whatever its status
        # column says.
        if p["status"] not in ("done", "archived")
        and p["terminal_disposition"] not in IRREVERSIBLE_DISPOSITIONS
    ]
    if unsatisfied and not _parents_satisfied(conn, task_id):
        # The claim-time invariant is the authority: claim_task re-reads
        # _parents_satisfied and demotes the card straight back. On 2026-09-14
        # t_4d21959c was force-promoted at 12:49:03 and demoted by
        # claim_rejected parents_not_done one second later — a flap that looked
        # like an unblock and ran nothing. Say so instead of reporting success.
        return False, (
            f"unsatisfied parent dependencies: {', '.join(unsatisfied)} "
            f"(the ready -> running claim re-checks parents, so promotion cannot "
            f"bypass them; complete the parents, drop the link with "
            f"`hermes kanban unlink <parent_id> {task_id}`, or, if this card is "
            f"the parent's independent verifier, declare it with a 'verifies' "
            f"relation)"
        )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')", (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(conn, task_id, "promoted_manual", {"actor": actor, "reason": reason})

    return True, None


def _reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses, now: int, note: str,
) -> None:
    """Close a leaked open run before a status flip so the invariant
    ``current_run_id IS NULL <=> run row terminal`` holds; no-op normally."""
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )


def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """Return ``'todo'`` if any parent isn't ``done`` yet, else ``'ready'``.

    The parent-completion re-gate shared by :func:`unblock_task` and
    :func:`reopen_review_task`: flipping straight to ``ready`` would bypass the
    parent-completion invariant the dispatcher trusts (it would spawn a child
    whose upstream work isn't finished). If parents are still in progress the
    task waits in ``todo`` until ``recompute_ready`` picks it up. RCA: Bug 2 at
    kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md. Kept in one place
    so the two transitions can't drift.

    Deliberately stricter than :func:`_parents_satisfied`: an unblock/reopen
    never lands ``ready`` on an evidence-ready (not yet done) parent.
    """
    undone_parents = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? "
        "AND p.status NOT IN ('done', 'archived') "
        # Same terminality rule as _UNSATISFIED_PARENTS_SQL: an irreversibly
        # disposed parent never runs again, so it can never stop being
        # satisfied.
        f"AND {_not_irreversibly_disposed_sql('p')} LIMIT 1",
        (task_id,),
    ).fetchone()
    return "todo" if undone_parents else "ready"


def resolve_task_approval(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    decision: str,
    actor: str = "Christopher",
    source: str = "operator",
) -> bool:
    """Resolve a first-class human approval blocker.

    ``approve`` records an auditable grant and resumes the same task through
    :func:`unblock_task`. ``deny`` records an auditable denial, changes the
    block kind to ``needs_input`` so it is no longer advertised as awaiting
    approval, and deliberately leaves the task blocked. Keeping denial
    non-terminal is fail-closed: dependent work cannot auto-promote merely
    because authorization was refused.

    Returns False for stale/duplicate decisions or tasks that are not
    currently ``blocked`` with ``block_kind='approval_required'``.
    """
    choice = (decision or "").strip().lower()
    if choice not in {"approve", "deny"}:
        raise ValueError("decision must be 'approve' or 'deny'")
    who = (actor or "Christopher").strip() or "Christopher"
    via = (source or "operator").strip() or "operator"

    with write_txn(conn):
        row = conn.execute(
            "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            row is None
            or row["status"] != "blocked"
            or row["block_kind"] != "approval_required"
        ):
            return False
        kind = "approval_granted" if choice == "approve" else "approval_denied"
        _append_event(
            conn, task_id, kind,
            {"actor": who, "source": via, "decision": choice},
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                task_id,
                who,
                ("APPROVED" if choice == "approve" else "DENIED")
                + f" via {via}.",
                now,
            ),
        )
        if choice == "deny":
            conn.execute(
                "UPDATE tasks SET block_kind = 'needs_input', result = ? WHERE id = ?",
                (f"Approval denied by {who} via {via}; proposed action not authorized.", task_id),
            )
            return True

    # Run the normal resume machinery outside the decision transaction so all
    # parent gating / stale-run cleanup stays centralized in unblock_task.
    return unblock_task(conn, task_id)


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``blocked``/``scheduled`` -> its resumable phase (parent re-gated; ``review``
    when that is where it left off), closing any leaked run first."""
    now = int(time.time())
    with write_txn(conn):
        loop_ok, loop_reason = validation_loop_entry_valid(conn, task_id)
        if not loop_ok:
            _append_event(conn, task_id, "execution_refused",
                          {"reason": "validation_loop_missing", "detail": loop_reason,
                           "source": "unblock_task"})
            return False
        resume_status = (
            _resume_status_from_events(conn, task_id)
            if _task_status(conn, task_id) == "blocked"
            else "ready"
        )
        _reclaim_dangling_run(
            conn, task_id, statuses=("blocked", "scheduled"), now=now,
            note="invariant recovery on unblock",
        )
        # Re-gate on parent completion before restoring the source phase.
        landing_status = _landing_status_after_parents(conn, task_id)
        new_status = (
            "review"
            if landing_status == "ready" and resume_status == "review"
            else landing_status
        )
        # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
        # resetting them is the amnesia that let cron-unblock <-> re-block loop
        # unbounded; only complete_task clears them. ``consecutive_failures``
        # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
        # is a fresh start for the retry budget.
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled') "
            # An irreversible disposition survives an unblock attempt: a card
            # whose objective was satisfied elsewhere must not be resumable by
            # the ordinary blocked-card recovery path (or by a cron running
            # it).
            f"AND {_not_irreversibly_disposed_sql()}",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            (
                {"status": new_status, "resume_status": resume_status}
                if new_status != "ready" or resume_status != "ready"
                else None
            ),
        )
    # Production ledger projection (post-commit, best-effort, outside the
    # txn just closed). See _sync_production_ledger docstring — this closes
    # the gap identified in t_da07457d's FAIL verdict on t_faf6e26a.
    _sync_production_ledger(conn, task_id, status=new_status, phase="unblock")
    return True


def reopen_review_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """``review`` -> ``ready``/``todo`` so the implementer re-runs on the new
    comments; restores the implementer from the ``review_requested`` event.
    Preserves ``consecutive_failures`` and the block loop counter (review is
    not a block; only :func:`complete_task` clears them)."""
    now = int(time.time())
    with write_txn(conn):
        _reclaim_dangling_run(
            conn, task_id, statuses=("review",), now=now,
            note="invariant recovery on review reopen",
        )
        new_status = _landing_status_after_parents(conn, task_id)
        review_event = _latest_event(conn, task_id, "review_requested")
        handoff = _json_dict(_row_get(review_event, "payload"))
        implementer = _nonblank_str(handoff.get("implementer"))
        params: tuple[Any, ...] = (new_status, *((implementer,) if implementer else ()), task_id)
        cur = conn.execute(
            # consecutive_failures deliberately PRESERVED: review reopen is not
            # a success signal; only complete_task resets the breaker (#35072).
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            + (", assignee = ?" if implementer else "")
            # An irreversible disposition survives a review reopen too — same
            # reason as unblock_task.
            + " WHERE id = ? AND status = 'review' AND "
            + _not_irreversibly_disposed_sql(),
            params,
        )
        if cur.rowcount != 1:
            return False
        # Leaving the review lane for rework — see request_changes.
        _clear_verification_state(conn, task_id)
        payload: dict[str, Any] = {"status": new_status}
        if implementer:
            payload["implementer"] = implementer
        _append_event(
            conn, task_id, "review_reopened", payload if payload != {"status": "ready"} else None,
        )
    # Production ledger projection (post-commit, best-effort). See
    # _sync_production_ledger docstring — this closes the gap identified in
    # t_da07457d's FAIL verdict on t_faf6e26a.
    _sync_production_ledger(conn, task_id, status=new_status, phase="review_reopened")
    return True


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``/``done``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated.
    Every surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor. Running descendants are closed
    ``reclaimed`` and their workers killed strictly post-commit (audit trail
    before death) — when composed, the CALLER must drain ``terminations``
    after its own commit. ``consecutive_failures`` resets (deliberate operator
    action), the opposite of :func:`reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "terminations": [(worker_pid, claim_lock, worker_started_at)]}``.
    """
    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn, allow_nested=True):
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock, t.worker_started_at
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append((row["worker_pid"], row["claim_lock"], row["worker_started_at"]))
                run_id = _end_run(
                    conn, row["id"], outcome="reclaimed", status="todo",
                    summary=f"ancestor {task_id} reopened",
                )
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            #
            # The disposition is cleared here for exactly the same reason
            # ``completed_at`` is: this row is being un-completed, so the
            # assertion "this executor finished it" stops being true and must
            # not be left sitting on a card that is going back out for rework.
            # (It is not load-bearing for redispatch — 'completed' is a
            # reversible ending, see IRREVERSIBLE_DISPOSITIONS — but a stale
            # completion stamp would still misreport in the disposition
            # counts.) The clear is scoped to 'completed': an
            # 'overtaken_by_events' card is never revived by an ancestor
            # reopen (its status is 'archived', which this loop skips), and
            # scoping it here means even a future caller that widened the
            # status filter could not resurrect one by accident.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "current_run_id = NULL, consecutive_failures = 0, "
                "terminal_disposition = CASE WHEN terminal_disposition = ? "
                "                            THEN NULL ELSE terminal_disposition END, "
                "disposition_at = CASE WHEN terminal_disposition = ? "
                "                      THEN NULL ELSE disposition_at END "
                "WHERE id = ?",
                (DISPOSITION_COMPLETED, DISPOSITION_COMPLETED, row["id"]),
            )
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for pid, claim_lock, started_at in terminations:
            _terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return {"invalidated": invalidated, "terminations": terminations}


class BlockLoopRepairRequired(ValueError):
    """A block-loop card tried to leave triage with no recorded repair.

    ``block_task`` routes a task to triage after ``BLOCK_RECURRENCE_LIMIT``
    same-cause blocks. Christopher's rule (2026-10-14): a twice-blocked card
    is diagnosed, repaired and re-run through the Gauntlet, never bounced
    back to the work pool unchanged. On t_4c41cdde a status-only specify
    sent the card back with the root cause unrepaired and four more runs
    spent the attempt budget. Every triage exit back to work (specify,
    decompose, dashboard status moves) now needs a ``block_loop_repaired``
    event newer than its latest loop detection. Archiving (closing the card)
    and ``verification_lane_restored`` (a real verifier verdict sends the
    card to review, not back to work) are deliberately not gated.
    """


def block_loop_repair_pending(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the task's latest ``block_loop_detected`` has no later
    ``block_loop_repaired`` event."""
    row = conn.execute(
        "SELECT MAX(id) AS loop_id FROM task_events "
        "WHERE task_id = ? AND kind = 'block_loop_detected'",
        (task_id,),
    ).fetchone()
    if row is None or row["loop_id"] is None:
        return False
    repaired = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'block_loop_repaired' AND id > ? LIMIT 1",
        (task_id, row["loop_id"]),
    ).fetchone()
    return repaired is None


def _block_loop_refusal(task_id: str) -> str:
    return (
        f"{task_id} reached triage through the block loop and has no "
        f"recorded repair. Diagnose the repeated blocker, fix it, check the "
        f"fix, then run: hermes kanban loop-repair {task_id} --root-cause ... "
        f"--change ... --check ..."
    )


def record_block_loop_repair(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_cause: str,
    change: str,
    check: str,
    author: str,
) -> None:
    """Record the repair of a block-loop card and arm the Gauntlet on it.

    Requires the task to sit in triage with an unrepaired loop detection and
    all of root cause, change made, and the check that proved it. Does not
    move the task: ``specify_triage_task`` remains the only exit, and it
    now consults this record. Raises ``ValueError`` on any missing piece.
    """
    fields = {
        "root_cause": (root_cause or "").strip(),
        "change": (change or "").strip(),
        "check": (check or "").strip(),
    }
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise ValueError("loop repair needs " + ", ".join(missing))
    author = (author or "").strip()
    if not author:
        raise ValueError("loop repair needs an author")
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown task {task_id}")
        if row["status"] != "triage" or not block_loop_repair_pending(conn, task_id):
            raise ValueError(
                f"{task_id} has no unrepaired block loop in triage "
                f"(status={row['status']!r})"
            )
        conn.execute(
            "UPDATE tasks SET gauntlet_enforced = 1 WHERE id = ?", (task_id,)
        )
        _append_event(conn, task_id, "block_loop_repaired", {**fields, "author": author})
        _append_event(conn, task_id, "gauntlet_enforcement", {"enabled": True})
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                task_id,
                author,
                "Block-loop repair recorded.\n"
                f"Root cause: {fields['root_cause']}\n"
                f"Change: {fields['change']}\n"
                f"Check: {fields['check']}",
                int(time.time()),
            ),
        )


def specify_triage_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, assignee: Optional[str] = None, author: Optional[str] = None,
) -> bool:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one
    txn; False when not in triage. Lands in ``todo`` (not ``ready``) so parent
    gating still applies; the audit comment is written only when a field changed.

    Raises ``BlockLoopRepairRequired`` for a block-loop card with no
    recorded repair (see ``record_block_loop_repair``).
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        if block_loop_repair_pending(conn, task_id):
            raise BlockLoopRepairRequired(_block_loop_refusal(task_id))
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'", tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Not add_comment (own txn + 'commented' event); 'specified' below records it.
            _insert_comment(
                conn, task_id, author.strip(),
                "Specified — updated " + ", ".join(changed_fields) + " and promoted to todo.",
                int(time.time()),
            )
        _append_event(
            conn, task_id, "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Own IMMEDIATE txn (outside the one above): a parent-free specified task
    # flips to 'ready' now instead of idling until the next tick.
    recompute_ready(conn)
    return True


def archive_task(conn: sqlite3.Connection, task_id: str, *, signal_fn=None) -> bool:
    """Archive a task; a *running* task's host-local worker is terminated.

    Clearing ``worker_pid`` in the DB alone left the OS process running past its
    own archive — it kept executing (and pushing work) against a task nothing
    tracked anymore (#76196). Snapshot pid+claim inside the archive txn so the
    kill is contingent on THIS caller winning the archive transition (a losing
    concurrent archiver must never signal the pid); the kill itself runs after
    commit — ``_poll_worker_exit`` can wait ~5 s and must not hold the write
    lock. Post-release kill is safe here because ``archived`` is terminal: no
    dispatcher can spawn a duplicate worker off the released claim. The
    termination outcome lands as its own ``archive_worker_termination`` event so
    the ``archived`` event stays atomic with the status flip.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return False
        was_running = row["status"] == "running"
        prev_pid, prev_lock, prev_started = row["worker_pid"], row["claim_lock"], row["worker_started_at"]
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND status != 'archived'", (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # Archived mid-run (dashboard): close the run so history isn't orphaned.
        run_id = _end_run(
            conn, task_id, outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    if was_running:
        termination = _terminate_reclaimed_worker(prev_pid, prev_lock, signal_fn=signal_fn, started_at=prev_started)
        with write_txn(conn):
            _append_event(conn, task_id, "archive_worker_termination", termination, run_id=run_id)
    # ``archived`` parents no longer block children; promote them now.
    recompute_ready(conn)
    # Production ledger projection (post-commit, best-effort). Without this an
    # archived card stayed Blocked/FAILED on the dashboard forever and kept
    # tripping the hourly email-task Telegram watch (2026-10-12).
    _sync_production_ledger(conn, task_id, status="archived", phase="archive")
    # Reap the workspace on archive too (never-completed tasks kept it forever).
    _cleanup_workspace(conn, task_id)
    return True


def reconcile_overtaken_by_events(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: str,
    evidence: str,
    superseded_by: Optional[Iterable[str]] = None,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Close a card whose objective was satisfied by intervening events.

    The sanctioned path from "this card is safely blocked forever because the
    world moved on" to a real terminal state, WITHOUT claiming an execution
    that never happened. Returns ``(True, None)`` on success and
    ``(False, refusal_reason)`` when refused — the same shape as
    :func:`promote_task`, so CLI and API callers handle both alike.

    What it writes (all of it additive):

    * ``status = 'archived'`` — terminal in every place the board already
      understands terminality: dependency gating, the dispatcher's ready and
      review queries, ``board_stats``, ``list_tasks``' default filter, the
      assignee workload counts and the hygiene sweep's
      ``blocked/ready/todo/triage`` candidate scan.
    * ``terminal_disposition = 'overtaken_by_events'`` — the machine-readable
      fact, and the fail-closed half of the redispatch guard. Status alone
      would let a racing writer flip the card back into a runnable column;
      the disposition is tested in the WHERE clause of ``claim_task``,
      ``claim_review_task``, ``unblock_task``, ``reopen_review_task``,
      ``complete_task``, ``promote_task`` and ``check_respawn_guard``, so it
      survives that.
    * ``disposition_reason`` / ``disposition_at`` — the mandatory rationale
      plus evidence, and when it was recorded.
    * one ``reconciled_overtaken_by_events`` audit event carrying
      ``executed: false``, and one comment saying the same in prose.

    What it deliberately does NOT write, because each would be a fabrication
    or a loss:

    * ``title`` / ``body`` — the original contract, including every step that
      is now historically obsolete, stays visible and unedited. Obsolete steps
      are never marked executed; the disposition explains why they were not
      run, which is a different and honest claim.
    * ``result`` and ``completed_at`` — both mean "this executor produced this
      outcome at this time". Neither is true here, and writing them would put
      a phantom completion into every report and time-to-complete metric.
    * ``consecutive_failures``, ``block_recurrences``, ``block_kind``,
      ``verification_state``, ``regression_required``, ``gauntlet_enforced`` —
      counters and verification state are history, not bookkeeping to tidy.
    * runs, verdict-ledger rows, comments, attachments, dependency links and
      prior events — untouched. An in-flight run (only reachable from a status
      this function accepts if a previous transition leaked the pointer) is
      closed as ``reclaimed``, never ``completed``.

    The workspace is intentionally left on disk: unlike a completion, nobody
    has finished with it, and it may hold the investigation that produced the
    evidence. ``hermes kanban gc`` reaps archived-task workspaces on its own
    schedule.

    ``reason`` and ``evidence`` are both required and must be non-empty. The
    whole point of the disposition is that it is justified; an unexplained one
    would be indistinguishable from quietly deleting inconvenient work.
    """
    who = (actor or "").strip()
    why = (reason or "").strip()
    proof = (evidence or "").strip()
    if not who:
        return False, "actor is required"
    if not why:
        return False, "reason is required (why the objective is already satisfied)"
    if not proof:
        return False, (
            "evidence is required (what verified intervening event satisfied it)"
        )
    refs = [str(r).strip() for r in (superseded_by or []) if str(r).strip()]

    row = conn.execute(
        "SELECT status, terminal_disposition FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"
    if row["terminal_disposition"] in IRREVERSIBLE_DISPOSITIONS:
        return False, (
            f"task {task_id} already has terminal disposition "
            f"{row['terminal_disposition']!r}"
        )
    # A stale 'completed' stamp is NOT a bar. It means the card was completed
    # once and then reopened; if the world moved on while it sat reopened,
    # overtaken-by-events is the honest ending and the stamp is overwritten.
    # The ``done`` status check below is what protects a live completion.
    prior_status = row["status"]
    if prior_status not in RECONCILABLE_STATUSES:
        # 'running' and 'done' are the two exclusions; spell out which one so
        # the operator knows whether to stop a worker or leave the card alone.
        detail = (
            "stop or reclaim the worker first"
            if prior_status == "running"
            else "it already records a real completion by this executor"
        )
        return False, (
            f"task {task_id} is {prior_status!r} and cannot be reconciled as "
            f"overtaken by events: {detail}"
        )
    if dry_run:
        return True, None

    now = int(time.time())
    payload: dict[str, Any] = {
        "actor": who,
        "reason": why,
        "evidence": proof,
        "prior_status": prior_status,
        "disposition": DISPOSITION_OVERTAKEN_BY_EVENTS,
        # Read by anything summarising the card. States the negative claim
        # outright so no consumer has to infer it from the absence of a
        # result: the card's steps were NOT performed.
        "executed": False,
    }
    if refs:
        payload["superseded_by"] = refs

    with write_txn(conn):
        # Never 'completed'. If a run pointer leaked from an earlier
        # transition, close it as reclaimed so the runs invariant holds
        # without inventing an attempt that succeeded.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task reconciled as overtaken by events (not executed)",
        )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status               = 'archived',
                   terminal_disposition = ?,
                   disposition_reason   = ?,
                   disposition_at       = ?,
                   claim_lock           = NULL,
                   claim_expires        = NULL,
                   worker_pid           = NULL
             WHERE id = ?
               AND status = ?
               AND """ + _not_irreversibly_disposed_sql() + """
            """,
            (
                DISPOSITION_OVERTAKEN_BY_EVENTS,
                f"{why}\n\nEvidence: {proof}"
                + (f"\nSuperseded by: {', '.join(refs)}" if refs else ""),
                now,
                task_id,
                prior_status,
            ),
        )
        if cur.rowcount != 1:
            # Status moved under us between the read and the write.
            return False, f"task {task_id} changed state during reconciliation"
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                task_id,
                who,
                "OVERTAKEN_BY_EVENTS — closed without execution. The steps in "
                "this card were NOT performed; its objective was satisfied by "
                f"intervening events.\n\nReason: {why}\n\nEvidence: {proof}"
                + (f"\n\nSuperseded by: {', '.join(refs)}" if refs else ""),
                now,
            ),
        )
        _append_event(
            conn, task_id, "reconciled_overtaken_by_events", payload,
            run_id=run_id,
        )
    # A disposed parent no longer gates its children — promote anything this
    # just freed instead of waiting for the next dispatcher tick, exactly as
    # archive_task does.
    recompute_ready(conn)
    _sync_production_ledger(
        conn, task_id, status="archived", phase="overtaken_by_events",
        run_id=run_id,
    )
    return True, None


def _delete_task_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete every row referencing ``task_id`` (schema has no ON DELETE CASCADE)."""
    conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
    for table in ("task_comments", "task_events", "task_runs", "kanban_notify_subs"):
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an already-archived task and its related rows.

    Safety guard: only archived tasks can be deleted. Active / blocked / done
    tasks must be explicitly archived first so accidental data loss requires a
    second deliberate action.
    """
    with write_txn(conn):
        purged = _purge_archived_task_locked(conn, task_id)
    if purged:
        _purge_task_files(task_id)
    return purged


def _purge_archived_task_locked(conn: sqlite3.Connection, task_id: str) -> bool:
    """Shared cascade for both delete paths. Caller holds ``write_txn``.

    The card row goes FIRST. The append-only history triggers
    (``trg_task_*_no_delete_while_task_lives``) refuse to delete a comment,
    event or run while its card still exists, and
    ``trg_obs_timer_no_delete_while_task_lives`` says the same for timers.
    Deleting the card first — and only when it is ``archived`` — is what
    turns every DELETE below into the one cascade the triggers permit.

    The status check is in the DELETE's WHERE clause, not a prior SELECT,
    so a card un-archived between the two cannot be purged by a stale read.
    """
    # An unresolved execution effect (rollback_pending / conflict) is real
    # work still owed outside the board; purging its record would hide it.
    # Fail closed and say why on the card.
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM execution_effects "
        "WHERE task_id = ? AND state IN ('rollback_pending', 'conflict')",
        (task_id,),
    ).fetchone()[0]
    if unresolved:
        if conn.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND status = 'archived'", (task_id,),
        ).fetchone() is not None:
            _append_event(
                conn, task_id, "history_delete_refused",
                {"reason": "unresolved_execution_effects",
                 "count": int(unresolved), "source": "purge"},
            )
        return False
    cur = conn.execute(
        "DELETE FROM tasks WHERE id = ? AND status = 'archived'",
        (task_id,),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
        (task_id, task_id),
    )
    # Governance relations cascade with the row for the same reason the
    # dependency links do: a relation whose endpoint no longer exists is
    # not evidence, it is a dangling pointer that makes the linkage
    # read-back report a link the board cannot honour.
    conn.execute(
        "DELETE FROM task_relations WHERE from_task_id = ? OR to_task_id = ?",
        (task_id, task_id),
    )
    conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
    # Everything else keyed to the card (2026-10-13, t_6cc051f2): the purge
    # printed "Deleted" and left 175 rows behind in these tables. Execution
    # events go first because they are keyed by execution, not by card.
    conn.execute(
        "DELETE FROM execution_events WHERE execution_id IN "
        "(SELECT id FROM executions WHERE task_id = ?)",
        (task_id,),
    )
    conn.execute("DELETE FROM executions WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM execution_effects WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_attachments WHERE task_id = ?", (task_id,))
    conn.execute("DELETE FROM task_verifications WHERE task_id = ?", (task_id,))
    # No current code creates this table (init_db does not); boards that ran
    # the earlier timeout-checkpoint code still carry it, so clear it when
    # present and never fail the purge on a board without it.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'task_timeout_checkpoints'"
    ).fetchone() is not None:
        conn.execute(
            "DELETE FROM task_timeout_checkpoints WHERE task_id = ?", (task_id,),
        )
    # Only unpromoted candidates go with their card. An active or retired
    # lesson is durable learning (verified lessons outlive their source) and
    # keeps its source_task_id as provenance.
    conn.execute(
        "DELETE FROM task_lessons WHERE source_task_id = ? AND state = ?",
        (task_id, LESSON_STATE_CANDIDATE),
    )
    # Timers cascade AFTER the card row, not before it, because
    # ``trg_obs_timer_no_delete_while_task_lives`` refuses to let a timer be
    # deleted while its subject is still on the board.
    _delete_observation_timers_for_task(conn, task_id)
    return True


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete an ARCHIVED task and cascade to all related rows.

    Same precondition as :func:`delete_archived_task`, and the same cascade.
    Until this gate the two paths differed in exactly one way: this one had
    no status check, so a dashboard ``DELETE /tasks/{id}`` could erase a
    live card's comments, events and runs — including any entry recording
    an agent's own violation — in one call. Task history is append-only
    (operations.md; execution-honesty.md 1.5); a card has to be archived,
    a visible and reversible step, before its history can be purged.

    Returns ``True`` if the task was archived and has been deleted. Returns
    ``False`` if the task was not found OR is not archived; in the latter
    case a ``history_delete_refused`` event is appended to the card so the
    attempt itself is on the record.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    the cascade is explicit and runs in a single ``write_txn``.
    """
    with write_txn(conn):
        if _purge_archived_task_locked(conn, task_id):
            purged = True
        else:
            purged = False
            row = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is not None:
                _append_event(
                    conn, task_id, "history_delete_refused",
                    {
                        "reason": "task_not_archived",
                        "status": row["status"],
                        "source": "delete_task",
                    },
                )
    if purged:
        _purge_task_files(task_id)
        recompute_ready(conn)
    return purged


def _purge_task_files(task_id: str) -> None:
    """Remove a purged card's attachment folder and worker log.

    Runs after the row cascade has committed, for the active board only (the
    same resolution the connection used): ids are unique per board, not
    across boards. A file that cannot be removed is logged, never raised;
    the rows are already gone.
    """
    if not task_id or "/" in task_id or task_id in (".", ".."):
        return
    targets = []
    try:
        targets.append(task_attachments_dir(task_id))
        targets.append(worker_log_path(task_id))
    except Exception as exc:  # pragma: no cover - path resolution failure
        _log.warning("purge %s: cannot resolve file paths: %s", task_id, exc)
        return
    for path in targets:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        except OSError as exc:
            _log.warning("purge %s: could not remove %s: %s", task_id, path, exc)


def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable)
    until ``unblock_task`` re-gates it."""
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason, synthesize=bool(reason),
        )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


_scoped_worker_launchers: dict[int, tuple[int, subprocess.Popen]] = {}
_WORKER_SCOPE_RE = re.compile(r"^hermes-kanban-(?P<task>.+)-(?P<run>\d+)-[0-9a-f]{8}\.scope$")


def stop_finished_worker_scopes(conn: sqlite3.Connection) -> list[str]:
    """Stop transient worker scopes whose run has ended and whose worker is gone.

    A ``systemd-run --scope`` unit lives until every process in it exits, so
    anything a worker started and never closed (headless Chrome, browser
    harness daemons, Xvfb, ad-hoc servers) outlives the card and holds
    memory indefinitely. Once the run row is ended and the worker PID is
    dead, stopping the scope terminates those leftovers. Running runs and
    units that do not match a run on this board are left alone.
    """
    if os.name == "nt":
        return []
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return []
    try:
        listing = subprocess.run(
            [systemctl, "--user", "list-units", "--type=scope", "--all",
             "--no-legend", "--plain", "hermes-kanban-*"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except Exception:
        return []
    stopped: list[str] = []
    for line in listing.splitlines():
        fields = line.split()
        m = _WORKER_SCOPE_RE.match(fields[0]) if fields else None
        if not m:
            continue
        try:
            row = conn.execute(
                "SELECT ended_at, worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
                (int(m.group("run")), m.group("task")),
            ).fetchone()
        except Exception:
            continue
        if row is None or row[0] is None or _pid_alive(row[1]):
            continue
        unit = fields[0]
        for action in ("stop", "reset-failed"):
            try:
                subprocess.run([systemctl, "--user", action, unit],
                               capture_output=True, timeout=30, check=False)
            except Exception:
                pass
        stopped.append(unit)
    return stopped


def _kanban_config_int(key: str, default: int) -> int:
    """Read a non-negative int from ``kanban.<key>``, live.

    Consulted per scan rather than captured at import so an operator can turn
    detection off (0) or retune it without restarting the gateway — the same
    live-read contract as :func:`gauntlet_enforcement_default`. Missing config,
    an unreadable file, or a malformed value all fall back to ``default``: a
    freshness scan must never be the thing that breaks a dispatcher tick.
    """
    try:
        from hermes_cli.config import load_config
        raw = _kanban_cfg(load_config).get(key, default)
    except Exception:
        return default
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _log.warning(
            "kanban: invalid kanban.%s=%r; using default %d", key, raw, default,
        )
        return default
    return max(0, value)


def gauntlet_stale_timeout_default() -> int:
    """Seconds of no lifecycle progress before Gauntlet work is called stale.

    ``kanban.gauntlet_stale_timeout_seconds``; 0 disables the scan entirely.
    """
    return _kanban_config_int(
        "gauntlet_stale_timeout_seconds",
        DEFAULT_GAUNTLET_STALE_TIMEOUT_SECONDS,
    )


def gauntlet_stale_realert_default() -> int:
    """Seconds before the SAME unchanged stale episode may alert again.

    ``kanban.gauntlet_stale_realert_seconds``. 0 means "never re-alert on an
    unchanged episode" — the alert then fires exactly once and only durable
    lifecycle progress can arm a fresh one.
    """
    return _kanban_config_int(
        "gauntlet_stale_realert_seconds",
        DEFAULT_GAUNTLET_STALE_REALERT_SECONDS,
    )


@dataclass
class GauntletStaleTask:
    """One Gauntlet-enforced card with no durable lifecycle progress.

    Carries the same facts as the emitted ``gauntlet_stale`` event so a caller
    (dispatcher telemetry, an observer, a test) can act on the scan result
    without re-reading the ledger.
    """

    task_id: str
    status: str
    verification_state: Optional[str]
    regression_required: bool
    age_seconds: int
    reasons: list[str]
    last_event_id: Optional[int]
    last_event_at: Optional[int]
    last_event_kind: Optional[str]
    # False for an unchanged episode inside its re-alert window: still stale
    # and still evaluated for recovery, but not to be announced again.
    alert_due: bool = True


def _last_meaningful_event(
    conn: sqlite3.Connection, task_id: str
) -> Optional[sqlite3.Row]:
    """Latest ``task_events`` row that represents actual lifecycle progress.

    Filters out the per-tick dispatcher chatter and refusal records listed in
    ``_GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS`` — including this scan's own
    ``gauntlet_stale`` event, which would otherwise reset the very clock it
    reads and make every card permanently fresh after its first alert.
    """
    placeholders = ",".join("?" * len(_GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS))
    return conn.execute(
        f"SELECT id, kind, created_at FROM task_events "
        f"WHERE task_id = ? AND kind NOT IN ({placeholders}) "
        f"ORDER BY id DESC LIMIT 1",
        (task_id, *_GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS),
    ).fetchone()


def _gauntlet_stale_reasons(
    status: str, verification_state: Optional[str], regression_required: bool,
) -> list[str]:
    """Classify WHY this card is parked. Ordered, stable, machine-readable."""
    reasons: list[str] = []
    if status == "review":
        if verification_state == VERIFICATION_PENDING:
            reasons.append("verification_pending")
        elif verification_state == VERIFICATION_FAILED:
            reasons.append("verification_failed")
        elif verification_state == VERIFICATION_VERIFIED:
            # Verified but never completed: the verdict was issued and then
            # nobody spent it. Still unfinished work.
            reasons.append("verified_not_completed")
        else:
            reasons.append("verification_unstarted")
    if regression_required:
        reasons.append("regression_required")
    if status != "review":
        reasons.append(f"parked_{status}")
    return reasons


def detect_stale_gauntlet_work(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: Optional[int] = None,
    realert_seconds: Optional[int] = None,
    now: Optional[int] = None,
    include_suppressed: bool = False,
) -> list[GauntletStaleTask]:
    """Find Gauntlet-enforced work that has been parked mid-chain and forgotten.

    A card is stale when all of the following hold:

    1. Its status is one of :data:`GAUNTLET_STALE_STATUSES` — never ``done``
       or ``archived`` (terminal), never ``running`` (owned by
       :func:`detect_stale_running`), never ``scheduled``/``triage``.
    2. It is Gauntlet-enforced, per-task (``tasks.gauntlet_enforced``) or
       board-wide (``kanban.gauntlet_enforcement``, read live).
    3. Its last DURABLE lifecycle event is older than the stale timeout.
       "Durable" excludes per-tick dispatcher chatter, refusal records, and
       this scan's own alerts — see
       ``_GAUNTLET_STALE_NONPROGRESS_EVENT_KINDS``. With no qualifying event
       at all, the clock runs from ``started_at``/``created_at``, so a card
       created and then never touched is detectable too.

    **This function never mutates workflow state.** It does not complete,
    verify, unblock, requeue, reassign or fail anything: a scanner has no
    evidence, and manufacturing a transition here would defeat the entire
    point of the chain it is watching. Its only write is an append-only
    ``gauntlet_stale`` event on ``task_events`` — the auditable record that
    the board was told.

    Anti-spam: one alert per *episode*. An episode is keyed on the id of the
    last meaningful event, so an alert repeats only when (a) durable progress
    has happened since — a new episode, which alerts immediately once it goes
    stale again — or (b) ``realert_seconds`` has elapsed since the last alert
    for the same unchanged episode. ``realert_seconds=0`` means an unchanged
    episode alerts exactly once, ever. A change in the card's stale reasons
    (for example ``regression_required`` appearing) is a material change and
    alerts immediately even inside the window.

    Returns the cards alerted on THIS scan (suppressed duplicates are not
    returned — the event is the surface, and returning them would re-spam any
    consumer of the dispatch result). With ``include_suppressed=True`` the
    suppressed cards are returned too, marked ``alert_due=False`` and with no
    event written, so recovery keeps evaluating every stale card on the
    detection cadence while notification follows the re-alert cadence
    (operations.md stale-supervision amendment, 2026-09-14). ``stale_timeout_seconds=0`` disables
    detection entirely and returns ``[]``. Both windows default to the live
    config values when not passed explicitly.
    """
    timeout = (
        gauntlet_stale_timeout_default()
        if stale_timeout_seconds is None
        else int(stale_timeout_seconds)
    )
    if timeout <= 0:
        return []
    realert = (
        gauntlet_stale_realert_default()
        if realert_seconds is None
        else int(realert_seconds)
    )
    now_ts = int(time.time()) if now is None else int(now)

    placeholders = ",".join("?" * len(GAUNTLET_STALE_STATUSES))
    try:
        rows = conn.execute(
            f"SELECT id, status, gauntlet_enforced, verification_state, "
            f"regression_required, created_at, started_at FROM tasks "
            f"WHERE status IN ({placeholders}) "
            f"ORDER BY created_at ASC, id ASC",
            GAUNTLET_STALE_STATUSES,
        ).fetchall()
    except sqlite3.OperationalError:
        # Pre-Gauntlet board that has not been migrated yet (no
        # gauntlet_enforced column). Nothing on it can be enforced, so there
        # is nothing to detect.
        return []

    # Board-wide enforcement is a config read; resolve it at most once per
    # scan and only if some card actually needs it.
    board_default: Optional[bool] = None
    stale: list[GauntletStaleTask] = []

    for row in rows:
        tid = row["id"]
        if row["gauntlet_enforced"]:
            enforced = True
        else:
            if board_default is None:
                board_default = gauntlet_enforcement_default()
            enforced = board_default
        if not enforced:
            continue

        event = _last_meaningful_event(conn, tid)
        if event is not None:
            last_event_id: Optional[int] = int(event["id"])
            last_event_kind: Optional[str] = event["kind"]
            last_at = int(event["created_at"])
        else:
            last_event_id = None
            last_event_kind = None
            last_at = int(row["started_at"] or row["created_at"] or now_ts)

        age = now_ts - last_at
        if age < timeout:
            continue

        status = str(row["status"])
        if status == "blocked" and last_event_kind == "blocked":
            try:
                _p = json.loads(conn.execute(
                    "SELECT payload FROM task_events WHERE id = ?",
                    (last_event_id,),
                ).fetchone()["payload"] or "{}")
            except Exception:
                _p = {}
            if str(_p.get("reason") or "").startswith(YT_SOURCE_WAIT_PREFIX):
                continue
        verification_state = row["verification_state"]
        regression_required = bool(row["regression_required"])
        reasons = _gauntlet_stale_reasons(
            status, verification_state, regression_required,
        )

        # Episode de-duplication. The marker is the last meaningful event id
        # plus the stale reasons; equality means nothing durable or material
        # has happened since we last said so.
        prev = conn.execute(
            "SELECT payload, created_at FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (tid, GAUNTLET_STALE_EVENT),
        ).fetchone()
        if prev is not None:
            try:
                prev_payload = json.loads(prev["payload"]) if prev["payload"] else {}
            except Exception:
                prev_payload = {}
            same_episode = (
                prev_payload.get("last_event_id", "__missing__") == last_event_id
                and prev_payload.get("reasons", reasons) == reasons
            )
            if same_episode and (
                realert <= 0 or now_ts - int(prev["created_at"]) < realert
            ):
                if include_suppressed:
                    stale.append(GauntletStaleTask(
                        task_id=tid,
                        status=status,
                        verification_state=verification_state,
                        regression_required=regression_required,
                        age_seconds=int(age),
                        reasons=reasons,
                        last_event_id=last_event_id,
                        last_event_at=last_at,
                        last_event_kind=last_event_kind,
                        alert_due=False,
                    ))
                continue

        payload = {
            "age_seconds": int(age),
            "status": status,
            "verification_state": verification_state,
            "regression_required": regression_required,
            "reasons": reasons,
            "last_event_id": last_event_id,
            "last_event_at": last_at,
            "last_event_kind": last_event_kind,
            "timeout_seconds": int(timeout),
            "realert_seconds": int(realert),
        }
        # The ONLY write. No UPDATE on tasks anywhere in this function.
        with write_txn(conn):
            _append_event(conn, tid, GAUNTLET_STALE_EVENT, payload)
        stale.append(
            GauntletStaleTask(
                task_id=tid,
                status=status,
                verification_state=verification_state,
                regression_required=regression_required,
                age_seconds=int(age),
                reasons=reasons,
                last_event_id=last_event_id,
                last_event_at=last_at,
                last_event_kind=last_event_kind,
            )
        )

    return stale


def _worker_hermes_argv() -> list[str]:
    """Return the argv prefix for a dispatcher-spawned Kanban worker.

    Workers are always launched through the dispatcher's own interpreter,
    never through a ``$HERMES_BIN`` / ``PATH`` lookup. The worker's task
    scope travels only in the environment ``_default_spawn`` builds
    (``HERMES_KANBAN_TASK``, ``HERMES_KANBAN_DB``, run id, claim lock), and a
    resolved ``hermes`` may be a wrapper that resets it: on 2026-09-14 a
    dispatcher run under sudo resolved the root-owned ``sudo -n -u chris``
    wrapper via ``secure_path``, ``env_reset`` stripped the Kanban scope, and
    runs 2814/2816/2817 skipped the executor-lane bypass and ran as unscoped
    ``default`` agents. The running interpreter has already imported
    ``hermes_cli``, so the module form is always a working Hermes.

    The worker runs with ``cwd`` set to the task workspace, and ``-m`` puts
    the cwd first on ``sys.path``. A task checkout that carries its own
    ``hermes_cli`` would therefore shadow the dispatcher's package and the
    worker would run that stale launch/handoff/budget code instead of the
    trusted runtime. The bootstrap drops the implicit cwd entry and pins
    the dispatcher's own package root first; the workspace stays the
    process cwd for tools (TERMINAL_CWD / HERMES_KANBAN_WORKSPACE).
    """
    root = str(Path(__file__).resolve().parent.parent)
    bootstrap = (
        "import sys;"
        "sys.path[:]=[p for p in sys.path if p!=''];"
        f"sys.path.insert(0,{root!r});"
        "sys.argv[0]='hermes_cli.main';"
        "from hermes_cli.main import main;main()"
    )
    return [sys.executable, "-c", bootstrap]


KANBAN_WORKER_QUERY_PREFIX = "work kanban task "
_KANBAN_WORKER_QUERY_RE = re.compile(r"^work kanban task (t_[A-Za-z0-9]+)$")


def dispatcher_worker_query_task_id(query: object) -> Optional[str]:
    """Return the task id when ``query`` is exactly the dispatcher worker prompt.

    ``_default_spawn`` passes ``chat -q "work kanban task <id>"``. That argv
    survives an environment reset even when the Kanban scope variables do
    not, so the CLI uses this to refuse a worker whose scope was stripped
    instead of running it as an unscoped agent.
    """
    if not isinstance(query, str):
        return None
    match = _KANBAN_WORKER_QUERY_RE.match(query.strip())
    return match.group(1) if match else None


def _spawn_gateway_scoped_worker(
    cmd: list[str],
    *,
    cwd: Optional[str],
    env: dict[str, str],
    stdout,
    task_id: str,
    run_id: Optional[int],
) -> Optional[int]:
    """Launch a Kanban worker outside the gateway service cgroup on Linux.

    ``start_new_session=True`` detaches a POSIX process group but does not move
    the child out of its systemd cgroup. A Kanban worker that legitimately
    restarts ``hermes-gateway.service`` would therefore kill itself and every
    sibling worker still living in the gateway unit. When this dispatcher is
    itself running inside the gateway, use ``systemd-run --user --scope`` to
    move the worker into a sibling transient scope before it starts work.

    The transient ``systemd-run`` client remains a gateway child, so its PID is
    not a valid worker-liveness handle. A fixed shell shim writes its own PID
    after systemd has moved it into the scope and then ``exec``s Hermes. That
    stable PID is what the Kanban task records. No argv or environment values
    are serialized into the unit definition, and the child inherits the same
    environment as the historical direct ``Popen`` path.

    Returns ``None`` when the escape path is not applicable. Once a scope
    launch is attempted, inability to obtain the scoped worker PID is a spawn
    failure rather than a fallback to a second direct launch, avoiding
    duplicate execution.
    """
    if (
        not sys.platform.startswith("linux")
        or env.get("_HERMES_GATEWAY") != "1"
        or not os.environ.get("INVOCATION_ID")
    ):
        return None
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        return None

    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-._") or "task"
    suffix = secrets.token_hex(4)
    run_part = str(run_id) if run_id is not None else "run"
    unit = f"hermes-kanban-{safe_task[:64]}-{run_part}-{suffix}"

    fd, pid_path = tempfile.mkstemp(prefix="hermes-kanban-worker-", suffix=".pid")
    os.close(fd)
    try:
        # $1 is the private pidfile. After shift, "$@" is the exact worker
        # argv. The shell becomes the worker via exec, so the PID written here
        # remains valid for the whole Hermes worker lifetime.
        shim = 'printf "%s\n" "$$" > "$1"; shift; exec "$@"'
        launcher = subprocess.Popen(
            [
                systemd_run,
                "--user",
                "--scope",
                "--quiet",
                "--unit",
                unit,
                "/bin/sh",
                "-c",
                shim,
                "sh",
                pid_path,
                *cmd,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                raw = Path(pid_path).read_text(encoding="ascii").strip()
            except OSError:
                raw = ""
            if raw:
                try:
                    pid = int(raw)
                except ValueError as exc:
                    raise RuntimeError(
                        "systemd scope worker wrote an invalid PID"
                    ) from exc
                if pid > 1:
                    _scoped_worker_launchers[int(launcher.pid)] = (pid, launcher)
                    return pid
                raise RuntimeError("systemd scope worker wrote an invalid PID")
            rc = launcher.poll()
            if rc is not None:
                raise RuntimeError(
                    f"systemd-run worker scope exited before publishing PID (rc={rc})"
                )
            time.sleep(0.02)
        raise RuntimeError("systemd-run worker scope did not publish PID within 5s")
    finally:
        try:
            Path(pid_path).unlink(missing_ok=True)
        except OSError:
            pass


def _worker_targets_routed_home(
    profile_arg: str,
    profile_home: Optional[str],
) -> bool:
    """True when this worker acts for a profile other than the dispatcher's own.

    The authority test for scrubbing a worker's environment is "does this
    worker act for a ROUTED home", not the gateway-wide
    ``is_multiplex_active()`` flag. Gating on the flag leaves every
    single-profile host — which is the common Kanban deployment, and this
    one: ``gateway.multiplex_profiles`` is unset while 32 profiles exist —
    building profile B's worker from the dispatcher's own environment.
    Upstream: ``bc0a42fd96``.

    ``profile_home`` is ``None`` when :func:`resolve_profile_env` raised. That
    happens for exactly one reason: a NAMED profile with no directory on
    disk. ``"default"`` always resolves (it *is* the launch root), so a raise
    can never mean "the launch profile's own work" — it means the worker is
    for another lane whose home is missing or was removed. Fail closed and
    scrub, rather than hand that worker the dispatcher's environment because
    its home could not be proven.
    """
    if profile_home is None:
        from hermes_cli.profiles import normalize_profile_name
        return normalize_profile_name(profile_arg) != "default"
    from tools.environments.local import _is_routed_home
    return _is_routed_home(profile_home)


_BWRAP_VERSION: Optional[str] = None


def _bwrap_version() -> str:
    global _BWRAP_VERSION
    if _BWRAP_VERSION is None:
        import subprocess
        try:
            out = subprocess.run(
                ["bwrap", "--version"], capture_output=True, text=True, timeout=5, check=False,
            )
            _BWRAP_VERSION = (out.stdout or out.stderr).strip() or "unknown"
        except Exception:
            _BWRAP_VERSION = "unknown"
    return _BWRAP_VERSION


_OPENROUTER_ENV = "OPENROUTER_API_KEY"


def _shared_openrouter_key() -> Optional[str]:
    """The platform OpenRouter key from the dispatcher env or the root .env."""
    val = os.environ.get(_OPENROUTER_ENV)
    if val:
        return val
    try:
        from dotenv import dotenv_values
        from hermes_constants import get_default_hermes_root
        return dotenv_values(get_default_hermes_root() / ".env").get(_OPENROUTER_ENV) or None
    except Exception:
        return None


def _apply_worker_sandbox(
    task: Task,
    cmd: list[str],
    *,
    profile: str,
    profile_home: Optional[str],
    workspace: str,
    env: dict[str, str],
) -> list[str]:
    """Wrap ``cmd`` in bubblewrap when the task's lane is enforced.

    Not enforced (mode off, executor-lane task, shared profile, lane not
    listed): ``cmd`` is returned unchanged and the manifest is never read, so
    a broken manifest cannot affect those lanes. Enforced: any failure to
    build the sandbox raises ``RuntimeError`` (fail closed), including a
    missing manifest, which ``worker_sandbox.wrap`` alone would treat as
    "run unsandboxed".
    """
    from hermes_cli import worker_sandbox as ws

    try:
        sc = ws.sandbox_config()
    except Exception as exc:
        raise RuntimeError(
            f"worker sandbox: invalid kanban.worker_sandbox config, refusing to spawn: {exc}"
        ) from exc
    if sc["mode"] != ws.MODE_ENFORCE or getattr(task, "executor_lane", None):
        return cmd
    lane = ws.lane_for(profile)
    if lane is None or lane not in sc["lanes"]:
        return cmd
    refuse = f"worker sandbox: refusing to spawn enforced lane {lane!r} unsandboxed"
    if not profile_home:
        raise RuntimeError(f"{refuse}: profile {profile!r} has no home directory")
    try:
        manifest = ws.load_manifest()
        if manifest is None:
            raise ws.SandboxConfigError(
                f"manifest {ws.default_manifest_path()} is missing"
            )
        wrapped = ws.wrap(
            cmd, task=task, profile=profile, profile_home=profile_home,
            workspace=workspace, env=env, cfg=None, manifest=manifest,
        )
    except Exception as exc:
        raise RuntimeError(f"{refuse}: {exc}") from exc
    if not wrapped or wrapped[0] != "bwrap":
        raise RuntimeError(f"{refuse}: sandbox wrapper was not applied")
    env[ws.SANDBOX_NO_REFRESH_ENV] = "1"
    # Hermes agents reach models through OpenRouter (shared platform
    # credential). The sandbox hides the root .env, so hand the key to the
    # child through its environment; never through argv, which is world-
    # readable in /proc/<pid>/cmdline.
    if not env.get(_OPENROUTER_ENV):
        key = _shared_openrouter_key()
        if key:
            env[_OPENROUTER_ENV] = key
        else:
            _log.warning("worker sandbox: %s not available for lane %s", _OPENROUTER_ENV, lane)
    _log.info(
        "kanban worker spawn task=%s lane=%s sandbox=1 bwrap=%s",
        task.id, lane, _bwrap_version(),
    )
    return wrapped


def build_worker_context(
    conn: sqlite3.Connection, task_id: str, *, isolated: bool = False,
) -> str:
    """Everything a worker should read about its task: header, current
    Christopher directives (highest task-context precedence; they explicitly
    supersede conflicting older task history), body, lessons pointer,
    attachments, prior attempts, done-parent handoffs, the assignee's recent
    work, remaining comments. Lists are tail-capped and fields char-capped
    (``_CTX_MAX_*``) so the prompt stays bounded on pathological boards.

    ``isolated=True`` omits the sections that reach beyond this card (the
    assignee's cross-task role history and the lessons pointer) for
    executors that must see only their own card's context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    from hermes_cli.kanban_db_context import (
        _CTX_MAX_COMMENT_BYTES, _ctx_attachments, _ctx_cap, _ctx_comments, _ctx_header,
        _ctx_parent_results, _ctx_prior_attempts, _ctx_role_history, _ctx_stamp,
    )
    # One clock reading so every relative age in this rendering agrees.
    now = int(time.time())
    lines: list[str] = []
    # Header renders title/assignee/status/workspace/runtime/branch AND the
    # body. The body is held back here so current operator directives can be
    # rendered between the header and the body (see below).
    header_lines: list[str] = []
    _ctx_header(header_lines, task)
    body_idx = next((i for i, l in enumerate(header_lines) if l == "## Body"), None)
    if body_idx is None:
        lines.extend(header_lines)
        body_section: list[str] = []
    else:
        lines.extend(header_lines[:body_idx])
        body_section = header_lines[body_idx:]

    # Current operator directives outrank task history. Christopher may correct
    # or narrow a task after a failed/blocked attempt; rendering that correction
    # after stale summaries caused retries to repeat superseded reasoning.
    all_comments = list_comments(conn, task_id)
    operator_directives = [
        c for c in all_comments
        if (c.author or "").strip().casefold() == "christopher"
    ]
    if operator_directives:
        lines.append("## CURRENT CHRISTOPHER DIRECTIVES — HIGHEST TASK-CONTEXT PRECEDENCE")
        lines.append(
            "These are newer operator instructions. If any task body, prior attempt, "
            "parent handoff, role history, or older comment conflicts with them, follow "
            "Christopher's newest directive. Do not revive superseded reasoning."
        )
        for c in reversed(operator_directives[-10:]):
            lines.append(f"### Christopher directive ({_ctx_stamp(c.created_at, now)})")
            lines.append(_ctx_cap(c.body, _CTX_MAX_COMMENT_BYTES))
        lines.append("")

    lines.extend(body_section)

    # Lessons are deliberately NOT loaded here.
    #
    # Christopher's ruling, 2026-09-07: "no agent should load all the lessons
    # at any point. that is part of the identifying process when an error is
    # encountered."
    #
    # Measured before this change: 196 approved lessons put ~22,600 tokens into
    # every dispatched task, ahead of the actual work, with 196 rules competing
    # for attention. It is also the wrong shape. A lesson is diagnostic
    # material for a failure that HAS happened; what binds unconditionally is
    # doctrine, and doctrine loads at boot.
    #
    # This is a removal, not a cap. There is no code path in this function that
    # can inject the corpus, so approving lessons can never reintroduce bulk
    # loading -- which is the property the previous ``_CTX_MAX_LESSONS`` cap
    # did not have.
    corpus_size = 0 if isolated else count_lessons(conn)
    if corpus_size:
        lines.append("## If you hit an error")
        lines.append(
            f"{corpus_size} lesson(s) recorded from earlier failures are "
            "available. **Do not read them now.** When -- and only when -- you "
            "hit an error, look up the ones matching it:"
        )
        lines.append("")
        lines.append('    hermes kanban lessons --error "<the error text>"')
        lines.append("")
        lines.append(
            "If a match resolves it, follow it and name the lesson id in your "
            "handoff. If nothing matches, you have hit a NEW failure: fix it, "
            "then record it so the next agent finds it instead of "
            "rediscovering it."
        )
        lines.append("")

    _ctx_attachments(lines, list_attachments(conn, task_id))

    # Prior attempts: when current directives exist, mark the history as
    # evidence only, right under the section header.
    attempt_lines: list[str] = []
    _ctx_prior_attempts(attempt_lines, conn, task_id, now)
    if attempt_lines and operator_directives:
        attempt_lines.insert(
            1,
            "_HISTORICAL ONLY — these attempts predate or may conflict with current "
            "Christopher directives above. They provide failure evidence, not current authority._",
        )
    lines.extend(attempt_lines)

    _ctx_parent_results(lines, conn, task_id, now)
    # Cross-task role history reaches beyond this card; isolated executors
    # must not see it.
    if not isolated:
        _ctx_role_history(lines, conn, task, now)
    # Remaining comment thread: Christopher's comments were rendered above as
    # directives and are not repeated here.
    _ctx_comments(
        lines,
        [c for c in all_comments if (c.author or "").strip().casefold() != "christopher"],
        now,
    )
    return "\n".join(lines).rstrip() + "\n"


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).

    ``by_terminal_disposition`` is the reporting half of the terminal
    disposition contract: it counts the WHOLE board, archived rows included,
    so "how many cards did we actually complete?" and "how many were closed
    because the world moved on?" are two different, separately readable
    numbers instead of one undifferentiated pile. ``by_status`` and
    ``by_assignee`` still exclude archived rows exactly as before, so an
    overtaken card leaves the active-work view entirely — which is the point.
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee = _counts_by_assignee(conn)

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    by_disposition: dict[str, int] = {}
    for row in conn.execute(
        "SELECT terminal_disposition AS d, COUNT(*) AS n FROM tasks "
        "WHERE terminal_disposition IS NOT NULL "
        "GROUP BY terminal_disposition"
    ):
        by_disposition[row["d"]] = int(row["n"])

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "by_terminal_disposition": by_disposition,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _to_epoch(val) -> Optional[int]:
    """Epoch seconds from int/float/numeric string/ISO-8601; None for empty/invalid."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c, _s, _co = task.created_at, task.started_at, task.completed_at  # from_row coerced
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


def drop_notify_sub_after_delivery_failure(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    delivery_mode: Optional[str] = None,
    attempts: int,
    error: str,
    failure_kind: str,
) -> bool:
    """Remove one exhausted notification route and leave durable evidence.

    Normal unsubscription is intentionally silent. This path is different:
    the notifier has exhausted its retry budget, so the task has lost a return
    route. Delete that exact subscription and append the failure event in the
    same transaction. Other subscriptions can then surface the event, while a
    task with no surviving route still keeps the evidence on its board.
    """
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
        if cur.rowcount:
            _append_event(
                conn,
                task_id,
                "notification_delivery_failed",
                {
                    "platform": platform,
                    "delivery_mode": delivery_mode or "notify",
                    "attempts": int(attempts),
                    "failure_kind": failure_kind,
                    "error": str(error)[:500],
                },
            )
    return cur.rowcount > 0


def _retention_seconds(older_than_seconds: int) -> int:
    """Normalise a gc retention window, rejecting negatives.

    Shared by both gc sweeps: a negative window puts the cutoff in the future,
    so "older than cutoff" would match every row / file instead of none —
    refuse before any sweep runs.
    """
    older_than_seconds = int(older_than_seconds)
    if older_than_seconds < 0:
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds!r}: "
            "a negative retention selects everything."
        )
    return older_than_seconds


def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Delete ORPHANED task_events rows older than ``older_than_seconds`` —
    rows whose task no longer exists on the board. Returns the number of
    rows deleted.

    This used to also delete aged events of ``done`` / ``archived`` tasks.
    It no longer does: task history is append-only for as long as the card
    exists (operations.md "prior runs, evidence, verifier results, comments,
    and failure history are never deleted or rewritten"), and the
    ``trg_task_events_no_delete_while_task_lives`` trigger would refuse the
    old statement row by row. The retention knob now only reaps rows left
    behind by purges that pre-date the cascade (200 such rows were on the
    live board on 2026-10-07).

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.
    """
    cutoff = int(time.time()) - _retention_seconds(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? "
            "AND task_id NOT IN (SELECT id FROM tasks)",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    older_than_seconds = _retention_seconds(older_than_seconds)
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        with contextlib.suppress(OSError):
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
    return removed


def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None, board: Optional[str] = None,
) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip the partial first line unless the window has no newline
                # at all (readline() would eat everything).
                probe = f.tell()
                if not f.readline().endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def list_profiles_on_disk() -> list[str]:
    """Profiles with a ``config.yaml`` plus the implicit ``default``; reads paths
    directly to avoid importing ``hermes_cli.profiles`` at startup."""
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")
    if profiles_dir.is_dir():
        try:
            names.update(e.name for e in profiles_dir.iterdir() if e.is_dir() and (e / "config.yaml").is_file())
        except OSError:
            pass
    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """``{"name", "on_disk", "counts"}`` for every on-disk profile or task
    assignee, so a fresh profile appears in pickers before it has a task."""
    on_disk = set(list_profiles_on_disk())
    counts = _counts_by_assignee(conn)
    return [
        {"name": name, "on_disk": name in on_disk, "counts": counts.get(name, {})}
        for name in sorted(on_disk | set(counts))
    ]


def list_runs(
    conn: sqlite3.Connection, task_id: str, *, include_active: bool = True,
    state_type: Optional[str] = None, state_name: Optional[str] = None,
) -> list[Run]:
    """Runs in start order; ``include_active=False`` = closed only; ``state_type``
    (``status``/``outcome``) + ``state_name`` filter together."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Newest non-empty run summary, or None. Workers hand off via ``summary`` and
    leave ``tasks.result`` NULL, so views need this or a done task looks empty."""
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, str]:
    """``{task_id: newest non-empty run summary}`` in one query (window function,
    SQLite >= 3.25); tasks without a summary are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}


def current_run_started_ats(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """``{task_id: started_at of the run ``tasks.current_run_id`` points at}``
    in one query; tasks with no active run (NULL or dangling pointer) are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS task_id, r.started_at AS started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        f"WHERE t.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["task_id"]: r["started_at"] for r in rows}


_FORK_MIGRATED_PATHS: set[str] = set()


def _migrate_fork_schema(conn: sqlite3.Connection) -> None:
    """Fork-only additive migrations, run once per board after upstream's
    :func:`hermes_cli.kanban_db_connect._migrate_add_optional_columns`.

    Everything here is idempotent and additive: Gauntlet / provenance /
    production-authority ``tasks`` columns, the ``task_verifications`` and
    ``task_lessons`` discriminators, the observation-timer uniqueness repair,
    the ``execution_effects`` spike migration and, LAST, the append-only
    history triggers (upstream's ``_rebuild_drifted_tables`` drops and
    recreates the history tables, which takes their triggers with them).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    if "executor_lane" not in cols:
        # NULL = normal Hermes worker (existing rows keep the behaviour they
        # already had). "claude_recovery" mechanically bypasses the Hermes
        # agent/tool loop — see VALID_EXECUTOR_LANES.
        _add_column_if_missing(conn, "tasks", "executor_lane", "executor_lane TEXT")

    if "recovery_gate_cmd" not in cols:
        _add_column_if_missing(
            conn, "tasks", "recovery_gate_cmd", "recovery_gate_cmd TEXT"
        )

    if "gauntlet_enforced" not in cols:
        # 0 on every legacy row: the Gauntlet chain is opt-in per task, so a
        # migrated board keeps the exact completion behaviour it had before
        # this column existed. Operators raise the floor board-wide with the
        # ``kanban.gauntlet_enforcement`` config key, which is consulted live
        # rather than backfilled.
        _add_column_if_missing(
            conn,
            "tasks",
            "gauntlet_enforced",
            "gauntlet_enforced INTEGER NOT NULL DEFAULT 0",
        )

    if "verification_state" not in cols:
        # NULL on legacy rows = "no verification phase entered". A task
        # already parked in ``review`` when this migration runs is NOT
        # backfilled to 'pending': it never produced a ledger row, so
        # inventing one would fabricate audit history. It re-enters the
        # chain on its next request_review.
        _add_column_if_missing(
            conn, "tasks", "verification_state", "verification_state TEXT"
        )

    if "control_plane" not in cols:
        # 0 on every legacy row. Nothing is retro-classified: the boundary is
        # about what may be CREATED from now on, and back-labelling historical
        # cards would rewrite audit history to look like a rule that did not
        # exist was being enforced.
        _add_column_if_missing(
            conn,
            "tasks",
            "control_plane",
            "control_plane INTEGER NOT NULL DEFAULT 0",
        )

    if "control_plane_authority" not in cols:
        # The named justification admitted this card (operator:/regression:/
        # requirement:/defect:). NULL on legacy rows and on ordinary work.
        _add_column_if_missing(
            conn, "tasks", "control_plane_authority", "control_plane_authority TEXT"
        )

    if "production_actions" not in cols:
        # Rule 6 production authority. '[]' on every legacy row: no existing
        # card is retro-authorized for anything — authority is granted only
        # at creation, through the structured field, by an operator.
        _add_column_if_missing(
            conn,
            "tasks",
            "production_actions",
            "production_actions TEXT NOT NULL DEFAULT '[]'",
        )

    if "regression_required" not in cols:
        # 0 on every legacy row. Nothing is retro-armed: the requirement is
        # evidence-driven, and a board migrated mid-flight has no recorded
        # failing verdict for the work already in progress, so inventing one
        # would block cards on a repair that was never diagnosed. It arms on
        # the first failing verdict after the migration.
        _add_column_if_missing(
            conn,
            "tasks",
            "regression_required",
            "regression_required INTEGER NOT NULL DEFAULT 0",
        )

    if "terminal_disposition" not in cols:
        # NULL on every legacy row, and nothing is backfilled. In particular
        # existing ``done`` cards are NOT retro-stamped 'completed': the
        # column asserts a fact about how a card ended, and inventing that
        # fact for history nobody observed is the same class of fabrication
        # this feature exists to prevent. Cards completed after the migration
        # get the stamp from ``complete_task``; older ones stay NULL and are
        # simply reported as "disposition not recorded".
        _add_column_if_missing(
            conn, "tasks", "terminal_disposition", "terminal_disposition TEXT"
        )

    if "disposition_reason" not in cols:
        _add_column_if_missing(
            conn, "tasks", "disposition_reason", "disposition_reason TEXT"
        )

    if "disposition_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "disposition_at", "disposition_at INTEGER"
        )

    # Structured actor provenance. Every one of these is NULL on every legacy
    # row and NOTHING is backfilled — deliberately. ``created_by`` cannot be
    # translated into an ``actor_kind`` after the fact (that translation is
    # precisely the string inference this repair exists to abolish), so a
    # migrated board answers "provenance not recorded" for its history and
    # records it properly from the next creation onward. The orphan
    # reproductions this repair preserves as evidence (t_aef6bbe1,
    # t_f9b3b48b) depend on that: retro-stamping them would destroy the
    # very evidence they are kept for.
    for _prov_col in (
        "actor_kind",
        "actor_id",
        "actor_lane",
        "actor_run_id",
        "creation_cause",
        "recovery_owner",
    ):
        if _prov_col not in cols:
            _add_column_if_missing(conn, "tasks", _prov_col, f"{_prov_col} TEXT")

    # Indexes over additive ``tasks`` columns must be created after the
    # columns exist, never in SCHEMA_SQL: a legacy board would abort the whole
    # init script on an index over a column the ALTER TABLE above has not
    # added yet. Reporting groups by disposition and the dispatch guards
    # filter on IS NULL, so a plain index serves both.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_terminal_disposition "
        "ON tasks(terminal_disposition)"
    )
    # "How many cards came from governed automation vs a human relay?"
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_actor_kind "
        "ON tasks(actor_kind, creation_cause)"
    )

    # task_verifications gained the regression-proof discriminators. A board
    # written by the first Gauntlet release has verdict rows only, so the
    # 'verdict' default is exactly right for them and covers_phase_id stays
    # NULL (a verdict row does not cover a phase, it *is* one).
    verif_table_exists = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='task_verifications'"
    ).fetchone() is not None
    if verif_table_exists:
        verif_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(task_verifications)")
        }
        if "kind" not in verif_cols:
            _add_column_if_missing(
                conn,
                "task_verifications",
                "kind",
                "kind TEXT NOT NULL DEFAULT 'verdict'",
            )
        if "covers_phase_id" not in verif_cols:
            _add_column_if_missing(
                conn,
                "task_verifications",
                "covers_phase_id",
                "covers_phase_id INTEGER",
            )

    # Promoted lessons. The table itself arrives via SCHEMA_SQL's CREATE TABLE
    # IF NOT EXISTS, which ``connect()`` re-runs for every board — a legacy DB
    # simply gains an empty table and behaves exactly as before, since nothing
    # is injected until something is promoted. This pass covers the narrower
    # case of a board written by an intermediate build that has the table but
    # not every column, and is a no-op on a current one.
    lessons_table_exists = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='task_lessons'"
    ).fetchone() is not None
    if lessons_table_exists:
        lesson_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_lessons)")
        }
        for col, ddl in (
            ("tenant", "tenant TEXT"),
            ("scope", f"scope TEXT NOT NULL DEFAULT '{LESSON_SCOPE_TENANT}'"),
            # Existing rows are 'active': they were promoted under the old
            # unconditional path, and re-labelling them candidates would
            # silently un-bind lessons already in force.
            ("state", f"state TEXT NOT NULL DEFAULT '{LESSON_STATE_ACTIVE}'"),
            ("review_by", "review_by INTEGER"),
            ("retire_condition", "retire_condition TEXT"),
            # The normalised signature of the error this lesson fixes,
            # recorded when the fix is logged. NULL on every legacy row
            # (imported prose has no error attached), which is exactly why
            # retrieval keeps a term-overlap fallback.
            ("error_signature", "error_signature TEXT"),
            ("evidence", "evidence TEXT"),
            ("verification_id", "verification_id INTEGER"),
            ("regression_proof_id", "regression_proof_id INTEGER"),
            ("created_by", "created_by TEXT"),
            ("active", "active INTEGER NOT NULL DEFAULT 1"),
            ("retired_at", "retired_at INTEGER"),
            ("retired_by", "retired_by TEXT"),
            ("retired_reason", "retired_reason TEXT"),
        ):
            if col not in lesson_cols:
                _add_column_if_missing(conn, "task_lessons", col, ddl)
        # Only now that every column is guaranteed present. SCHEMA_SQL cannot
        # carry these: it runs as one script *before* this pass, so an index
        # over ``active`` would abort the whole script — and therefore the
        # connection — on exactly the intermediate board this pass exists to
        # upgrade. The lookup index serves lessons_for_task's
        # ``active = 1 AND applicability IN (...)`` on every context build.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_lessons_lookup "
            "ON task_lessons(active, applicability)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_lessons_source "
            "ON task_lessons(source_task_id, id)"
        )

    # Duplicate observing timers are closed (oldest kept) before the unique
    # index is enforced; see the function for why this cannot live in
    # SCHEMA_SQL.
    _migrate_observation_timer_uniqueness(conn)

    # execution_effects existed briefly as an isolated spike before it joined
    # SCHEMA_SQL. Migrate that early shape only during normal DB initialization,
    # while the connection is open and already inside the schema-init lock.
    from hermes_cli.execution_effects import ensure_effect_schema
    ensure_effect_schema(conn, commit=False)

    # LAST, after every rebuild above: upstream's ``_rebuild_drifted_tables``
    # drops and recreates the history tables, which takes their triggers
    # with them.
    _ensure_task_history_append_only(conn)


def _ensure_fork_schema(conn: sqlite3.Connection, resolved: str) -> None:
    """Run :func:`_migrate_fork_schema` once per process per board path.

    Only after upstream's first-connect init has populated
    ``_INITIALIZED_PATHS`` for the path: a fenced (delegation) or otherwise
    read-only open never initialised the board, so nothing may be written
    on its behalf — the owner migrates it.
    """
    if resolved in _FORK_MIGRATED_PATHS:
        return
    with _INIT_LOCK:
        if resolved in _FORK_MIGRATED_PATHS:
            return
        if resolved not in _INITIALIZED_PATHS:
            return
        try:
            _migrate_fork_schema(conn)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower():
                # Read-only file or mount: upstream init already accepted the
                # board as-is, so the additive pass would have been a no-op
                # or the first write fails with its own clear message.
                return
            raise
        _FORK_MIGRATED_PATHS.add(resolved)


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open the kanban DB; ``query_only`` while it is latched corrupt.

    See :func:`hermes_cli.kanban_db_connect.connect` for the open/initialise
    behaviour. The fork's additive migrations (:func:`_migrate_fork_schema`)
    run once per path on top of upstream's first-connect init.
    """
    conn = _connect_impl(db_path=db_path, board=board)
    try:
        path = db_path if db_path is not None else kanban_db_path(board=board)
        resolved_path = path.resolve()
        _ensure_fork_schema(conn, str(resolved_path))
        if _corrupt_latch_active(resolved_path) or _wal_reset_writer_blocked():
            conn.execute("PRAGMA query_only=ON")
    except Exception:
        conn.close()
        raise
    return conn


def init_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Like :func:`hermes_cli.kanban_db_connect.init_db` this always re-runs the
    migration pass, including the fork's additive pass, so a board whose
    on-disk schema drifted (legacy event kinds, external upgrades, rebuilt
    history tables) is brought current.
    """
    path = _init_db_impl(db_path=db_path, board=board)
    with _INIT_LOCK:
        _FORK_MIGRATED_PATHS.discard(str(path.resolve()))
    with contextlib.closing(connect(path)):
        pass
    return path


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int | None = None,
) -> bool:
    """Legacy alias — fork callers / tests still reference the old name."""
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
    )


# --- Split modules (imported at the tail: they import this module as ``_kb``) ---
from hermes_cli.kanban_db_boards import (
    _default_board_display_name,
    _dir_holds_board,
    board_metadata_path,
    create_board,
    list_boards,
    read_board_metadata,
    remove_board,
    write_board_metadata,
)
from hermes_cli.kanban_db_connect import (
    _INITIALIZED_PATHS,
    _INIT_LOCK,
    _cross_process_init_lock,
    _migrate_add_optional_columns,
    _schema_is_present,
    connect_closing,
    repair_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (
    _cleanup_workspace,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _scratch_workspace,
    resolve_workspace,
    set_workspace_path,
)
from hermes_cli.kanban_db_dispatch import (
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    DispatchResult,
    _PROTOCOL_VIOLATION_FAILURE_LIMIT,
    _clear_failure_counter,
    _default_spawn,
    _defer_reclaim_for_live_worker,
    _dispatch_once_locked,
    _error_fingerprint,
    _pid_alive,
    _record_task_failure,
    _record_worker_exit,
    _resolve_hermes_argv,
    _set_worker_pid,
    _terminate_reclaimed_worker,
    _worker_alive,
    _worker_survived_termination,
    _worker_terminal_timeout_env,
    check_respawn_guard,
    configured_max_in_progress,
    count_running_tasks,
    detect_crashed_workers,
    detect_stale_running,
    dispatch_once,
    enforce_max_runtime,
    has_spawnable_ready,
    has_spawnable_review,
    heartbeat_worker,
    resolve_max_in_progress,
    review_dispatch_enabled,
)
from hermes_cli.kanban_db_graph import (
    decompose_triage_task,
)
from hermes_cli.kanban_db_notify import (
    add_notify_sub,
    claim_unseen_events_for_sub,
    count_notify_subs,
    list_notify_subs,
)
# Fork compatibility bindings (names the fork's own code and tests still reach through this module).
from hermes_cli.kanban_db_connect import connect as _connect_impl
from hermes_cli.kanban_db_connect import init_db as _init_db_impl
from hermes_cli.kanban_db_context import _ctx_cap as _cap

# Legacy alias — fork callers (hermes_cli/kanban.py) still reference the old name.
DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT
