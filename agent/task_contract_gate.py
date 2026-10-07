"""Execution-contract rules 1 and 2: a state change requires a valid,
currently-active task contract (rule 1), re-checked live against the board
before EVERY outside state change (rule 2).

This file is the rule-2 superset of gate-rule1.diff: identical contract
reading and board validation, but the board answer is never cached. Apply
gate-rule1.diff OR gate-rule2.diff, not both.

Scope of this gate (Phase 3, t_92521910 proposal): a *dispatcher-owned Kanban
worker* — a process the dispatcher spawned with ``HERMES_KANBAN_TASK`` /
``HERMES_KANBAN_RUN_ID`` / ``HERMES_KANBAN_CLAIM_LOCK`` in its environment —
may not execute a mutating tool unless the contract those variables describe
is complete and the board agrees with it: the task row exists, is ``running``,
is claimed under *this* worker's ``claim_lock``, and the claim has not expired.

Why here and not in ``claim_task``: ``claim_task`` already refuses an invalid
``ready -> running`` transition (Phase 3 discovery, t_5d7052f9), but nothing
checked that the process doing the work still holds a live claim when it
reaches for ``terminal``, ``write_file`` or ``execute_code``. A worker whose
run was reclaimed, whose task was cancelled, or that was launched with a
half-formed environment could keep mutating the world under a dead contract.

What this gate does NOT do: it does not touch interactive sessions (no
``HERMES_KANBAN_TASK`` → not a governed executor → no change), delegated
children or in-process cron jobs (``is_dispatcher_owned_worker_context`` is
False), or read-only tools. Board-mutating ``kanban_*`` tools are gated the
same way as filesystem/terminal tools (see ``BOARD_MUTATING_KANBAN_TOOLS``);
read-only ``kanban_*`` tools are not. Rule 2: the board is re-read on every mutating call (one primary-key SELECT),
so a cancellation, reclaim, or claim expiry that lands mid-run stops the very
next outside state change instead of being discovered at the next heartbeat.
This is the mechanical form of operations.md "Owner cancellation and
live-authority freshness are execution gates" (the t_b0ba7338 incident: 44
minutes of post-cancellation dispatch).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

ENV_TASK = "HERMES_KANBAN_TASK"
ENV_RUN_ID = "HERMES_KANBAN_RUN_ID"
ENV_CLAIM_LOCK = "HERMES_KANBAN_CLAIM_LOCK"

# Tools whose execution can change state outside the task's own transcript.
# Sourced from the guardrail set so the two lists cannot drift apart.
def _mutating_tool_names() -> frozenset:
    try:
        from agent.tool_guardrails import MUTATING_TOOL_NAMES

        return MUTATING_TOOL_NAMES
    except Exception:  # pragma: no cover - defensive import guard
        return frozenset({"terminal", "execute_code", "write_file", "patch"})


# Kanban tools that write to the board (tasks, runs, comments, links,
# attachments, lessons). The board is outside state exactly like the
# filesystem is, so a worker under a dead contract may not touch it either
# (Phase 4 finding 2, t_e8eb2485). Kept separate from the guardrail
# ``MUTATING_TOOL_NAMES`` on purpose: that set has other consumers (loop
# detection, idempotency) and must not change shape for this gate.
#
# Read-only kanban tools (``kanban_show``, ``kanban_list``,
# ``kanban_attachments``, ``kanban_lessons``) are intentionally absent: a
# worker whose contract has died may still look at the board to find out why.
#
# ``kanban_heartbeat`` IS gated. A live claim passes the gate, so a healthy
# worker's explicit heartbeat is unaffected; the automatic claim keepalive
# (``tools.kanban_tools.heartbeat_current_worker_from_env``) writes through
# ``kanban_db`` directly, never through tool dispatch, so it is unaffected too.
# What the gate stops is a worker whose claim has already expired or been
# reclaimed re-arming ``claim_expires`` on a task the dispatcher now owns.
BOARD_MUTATING_KANBAN_TOOLS = frozenset(
    {
        "kanban_complete",
        "kanban_block",
        "kanban_unblock",
        "kanban_request_review",
        "kanban_request_changes",
        "kanban_heartbeat",
        "kanban_comment",
        "kanban_create",
        "kanban_link",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_promote_lesson",
        "kanban_retire_lesson",
    }
)


def _is_gated_tool(tool_name: str) -> bool:
    return tool_name in _mutating_tool_names() or tool_name in BOARD_MUTATING_KANBAN_TOOLS


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    run_id: Optional[int]
    claim_lock: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.task_id) and self.run_id is not None and bool(self.claim_lock)


def read_task_contract(environ=None) -> Optional[TaskContract]:
    """The contract this process was launched under, or None when the process
    is not a Kanban worker at all."""
    env = os.environ if environ is None else environ
    task_id = (env.get(ENV_TASK) or "").strip()
    if not task_id:
        return None
    run_raw = (env.get(ENV_RUN_ID) or "").strip()
    run_id: Optional[int]
    try:
        run_id = int(run_raw) if run_raw else None
    except ValueError:
        run_id = None
    lock = (env.get(ENV_CLAIM_LOCK) or "").strip() or None
    return TaskContract(task_id=task_id, run_id=run_id, claim_lock=lock)


def _is_dispatcher_owned() -> bool:
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context

        return is_dispatcher_owned_worker_context()
    except Exception:
        return True


def validate_contract_against_board(contract: TaskContract, conn=None) -> Optional[str]:
    """Return None when the board confirms the contract, else a refusal reason.

    Read-only: one SELECT on ``tasks``. Fails closed on any error — a worker
    that cannot read its own board has no provable authority.
    """
    if not contract.complete:
        return (
            f"task contract incomplete: task={contract.task_id!r} "
            f"run_id={contract.run_id!r} claim_lock={'set' if contract.claim_lock else 'missing'}"
        )
    try:
        from hermes_cli import kanban_db as kb

        if conn is None:
            with kb.connect() as c:
                row = c.execute(
                    "SELECT status, claim_lock, claim_expires, current_run_id FROM tasks WHERE id = ?",
                    (contract.task_id,),
                ).fetchone()
        else:
            row = conn.execute(
                "SELECT status, claim_lock, claim_expires, current_run_id FROM tasks WHERE id = ?",
                (contract.task_id,),
            ).fetchone()
    except Exception as exc:
        return f"task contract unverifiable: board read failed ({type(exc).__name__})"
    if row is None:
        return f"task contract invalid: task {contract.task_id} does not exist on the board"
    status, claim_lock, claim_expires, current_run_id = row[0], row[1], row[2], row[3]
    if status != "running":
        return f"task contract not active: task {contract.task_id} is {status!r}, not running"
    if claim_lock != contract.claim_lock:
        return f"task contract not held: task {contract.task_id} is claimed by another worker"
    if current_run_id is not None and int(current_run_id) != contract.run_id:
        return (
            f"task contract superseded: task {contract.task_id} current run is "
            f"{current_run_id}, this worker is run {contract.run_id}"
        )
    if claim_expires is not None and int(claim_expires) < int(time.time()):
        return f"task contract expired: claim on task {contract.task_id} lapsed"
    return None


_LAST_REFUSAL: dict = {}


def task_contract_refusal(tool_name: str, *, environ=None) -> Optional[str]:
    """Rules 1 + 2 entry point for the tool dispatcher.

    Returns None (allow) for: non-mutating tools (including read-only kanban
    tools); processes that are not Kanban workers; delegated/non-dispatcher-
    owned contexts. Gated tools are the guardrail ``MUTATING_TOOL_NAMES`` plus
    ``BOARD_MUTATING_KANBAN_TOOLS``. Otherwise the
    board is read LIVE on this call (no cache) and the refusal text is returned
    when the contract is missing, incomplete, or no longer active. A refusal is
    logged once per distinct reason to keep a tight tool loop from flooding
    the log; the refusal itself is returned every time.
    """
    if not _is_gated_tool(tool_name):
        return None
    contract = read_task_contract(environ)
    if contract is None:
        return None
    if not _is_dispatcher_owned():
        return None
    reason = validate_contract_against_board(contract)
    if reason is None:
        return None
    key = (contract.task_id, reason)
    if key not in _LAST_REFUSAL:
        _LAST_REFUSAL[key] = time.time()
        logger.warning("task contract gate refused %s: %s", tool_name, reason)
    return f"Refused by task contract gate (execution contract rules 1-2, live re-check): {reason}"


def reset_cache() -> None:
    """Kept for API parity with gate-rule1; rule 2 holds no validation cache."""
    _LAST_REFUSAL.clear()
