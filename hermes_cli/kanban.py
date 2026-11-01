"""``hermes kanban …`` — dispatch (``kanban_command``), task-verb handlers, ``run_slash`` for ``/kanban``.
DB work lives in ``kanban_db``; siblings: ``kanban_parser`` (argparse, re-exported ``build_parser``),
``kanban_output`` (text/--json), ``kanban_boards`` (``boards …``), ``kanban_ops`` (dispatch/daemon/
tail/watch/gc/repair).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_swarm as ks
from hermes_cli import kanban_workflow
from hermes_cli.kanban_output import (
    _ATTACHMENT_FIELDS, _RUNS_RUN_FIELDS, _SHOW_RUN_FIELDS, _bulk_apply, _err,
    _fmt_counts, _fmt_task_line, _fmt_ts, _json_out, _obj_dict, _print_json,
)
from hermes_cli.kanban_output import _task_to_dict as _upstream_task_to_dict
from hermes_cli.kanban_boards import _dispatch_boards
from hermes_cli.kanban_ops import (
    _cmd_daemon, _kanban_config, _cmd_dispatch, _cmd_gc, _cmd_repair, _cmd_tail, _cmd_watch,
)
from hermes_cli.kanban_parser import build_parser as _upstream_build_parser


# --- NorCal additions on top of the upstream output/parser modules ---

def _task_to_dict(t: kb.Task) -> dict[str, Any]:
    """Upstream ``_task_to_dict`` plus the Gauntlet / disposition / production-authority fields."""
    d = _upstream_task_to_dict(t)
    d.update({
        "gauntlet_enforced": t.gauntlet_enforced,
        "verification_state": t.verification_state,
        "regression_required": t.regression_required,
        # How the card ended, when it ended for a reason worth naming.
        # 'completed' = this executor did the work; 'overtaken_by_events' =
        # the objective was satisfied elsewhere and the work was never run.
        "terminal_disposition": t.terminal_disposition,
        "disposition_reason": t.disposition_reason,
        "disposition_at": t.disposition_at,
        # Rule 6: structured production authorization (role names from the
        # installed registry). Empty on ordinary cards.
        "production_actions": list(t.production_actions or []),
    })
    return d


def _kanban_subparsers(kanban_parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in kanban_parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("kanban parser has no subcommand group")


def _add_norcal_commands(kanban_parser: argparse.ArgumentParser) -> None:
    """Attach the Gauntlet / recovery / lessons / execution-supervisor verbs and the
    NorCal-only ``create`` flags to the upstream ``hermes kanban`` tree."""
    sub = _kanban_subparsers(kanban_parser)

    # --- create: executor lanes, governed repairs, production authority ---
    p_create = sub.choices["create"]
    p_create.add_argument("--executor-lane",
                          choices=sorted(kb.VALID_EXECUTOR_LANES),
                          default=None, dest="executor_lane",
                          help="Explicit executor lane. 'claude' routes one "
                               "ordinary task directly to Claude Code before "
                               "any Hermes agent/tool loop. 'codex_verify' runs "
                               "an independent Codex verifier with writable scratch and read-only target access (assignee=atlas "
                               "is accepted as shorthand). 'claude_recovery' "
                               "is the bounded Claude-then-Codex gate-repair "
                               "lane and requires --recovery-gate-cmd. Omit "
                               "for the normal Hermes worker. assignee=claude "
                               "is accepted as shorthand for the direct lane.")
    p_create.add_argument("--recovery-gate-cmd", default=None,
                          dest="recovery_gate_cmd", metavar="CMD",
                          help="Shell command the recovery lane runs "
                               "(mechanically — never trusting Claude's or "
                               "Codex's own report) to decide whether the "
                               "gate is green. Required with --executor-lane "
                               "claude_recovery.")
    p_create.add_argument("--gauntlet", action="store_true", default=None,
                          help="Require the full Gauntlet chain to complete "
                               "this task: running -> review -> verified -> "
                               "done. The executor cannot close its own card; "
                               "a verifier must record a passing verdict via "
                               "`hermes kanban verify`. Defaults to the "
                               "kanban.gauntlet_enforcement config value.")
    p_create.add_argument("--recovery-owner", default=None,
                          dest="recovery_owner", metavar="IDENTITY",
                          help="Identity accountable for this card's "
                               "recovery decision. Set together with "
                               "--repairs-task-id and --umbrella-task-id "
                               "to declare a governed repair task — never "
                               "describe a repair only in the body. All "
                               "three are required together; creation "
                               "fails closed and no card is created if "
                               "only some are given.")
    p_create.add_argument("--repairs-task-id", default=None,
                          dest="repairs_task_id", metavar="TASK_ID",
                          help="Task id this card repairs (the subject). "
                               "Required together with --recovery-owner "
                               "and --umbrella-task-id.")
    p_create.add_argument("--umbrella-task-id", default=None,
                          dest="umbrella_task_id", metavar="TASK_ID",
                          help="Task id of the umbrella/tracking card this "
                               "repair falls under. Required together "
                               "with --recovery-owner and "
                               "--repairs-task-id.")
    p_create.add_argument("--production-action", action="append", default=[],
                          dest="production_actions", metavar="ROLE",
                          help="Production role this card explicitly "
                               "authorizes (repeatable). Rule 6: the ONLY "
                               "way a card grants production authority — "
                               "body text never does. ROLE must exist in "
                               "the installed production-roles registry "
                               "(~/.hermes/security/production-roles/). "
                               "Set at creation only, immutable afterwards "
                               "(a changed authorization is a new card), "
                               "and refused from a dispatcher-owned worker.")

    # --- block: approval_required is a first-class kind ---
    for _action in sub.choices["block"]._actions:
        if _action.dest == "kind":
            _action.help = (
            "Typed block reason. 'dependency' waits in todo (auto-promoted "
            "when linked parents finish, no human) only while a linked parent "
            "is still open — otherwise it is a real blocker and counts toward "
            "the loop breaker; 'needs_input'/'capability' go to "
            "blocked for a human; 'approval_required' means Christopher must approve/deny; 'transient' marks a maybe-flaky failure. "
            "Repeated same-kind re-blocks after unblock route the task to "
            "triage to break unblock loops. Omit for a generic block."
    )

    # --- dispatch: --dry-run does not take the board lock ---
    for _action in sub.choices["dispatch"]._actions:
        if _action.dest == "dry_run":
            _action.help = ("Don't actually spawn processes; just print what would "
                            "happen. NOTE: a dry run does not take the board lock, so "
                            "it can predict spawns a real tick will not perform when "
                            "another dispatcher holds the lock.")

    p_budget = sub.add_parser(
        "attempt-budget",
        help=(
            "Show an objective's attempt budget, or record one finite operator "
            "grant of extra attempts (never unblocks, claims or dispatches)"
        ),
    )
    p_budget.add_argument("task_id")
    p_budget.add_argument(
        "--grant", type=int, default=None,
        help=f"Attempts to add for this objective lineage (1-{kb.OBJECTIVE_ATTEMPT_GRANT_MAX})",
    )
    p_budget.add_argument("--authorized-by", default=None,
                          help="The human who authorized the grant (required with --grant)")
    p_budget.add_argument("--reason", default=None,
                          help="Why the objective needs more attempts (required with --grant)")
    p_budget.add_argument("--json", action="store_true")

    p_approve = sub.add_parser(
        "approve", help="Approve a task blocked with approval_required and resume it"
    )
    p_approve.add_argument("task_id")
    p_approve.add_argument("--actor", default="Christopher")
    p_approve.add_argument("--source", default="kanban-cli")

    p_deny = sub.add_parser(
        "deny", help="Deny a task blocked with approval_required; leave it safely blocked"
    )
    p_deny.add_argument("task_id")
    p_deny.add_argument("--actor", default="Christopher")
    p_deny.add_argument("--source", default="kanban-cli")

    p_verify = sub.add_parser(
        "verify",
        help=(
            "Record the verification verdict for a task in the review lane "
            "(the VERIFIED step of the Gauntlet chain)"
        ),
    )
    p_verify.add_argument("task_id")
    v_group = p_verify.add_mutually_exclusive_group(required=True)
    v_group.add_argument(
        "--pass", dest="passed", action="store_true",
        help="Verification passed — the task becomes completable.",
    )
    v_group.add_argument(
        "--fail", dest="passed", action="store_false",
        help=(
            "Verification failed — routes the task back for repair. "
            "Requires --reason."
        ),
    )
    p_verify.add_argument(
        "--reason", default=None,
        help="Why it passed or failed. Required for --fail.",
    )
    p_verify.add_argument(
        "--verifier", default=None,
        help="Profile/person recording the verdict. Defaults to the reviewer run's assignee.",
    )
    p_verify.add_argument(
        "--evidence", default=None,
        help=(
            "JSON object of falsifiable facts (commands run, exit codes, test "
            "counts, artefact paths). Stored on the verification ledger."
        ),
    )
    p_verify.add_argument(
        "--regression-evidence", dest="regression_evidence", default=None,
        help=(
            "JSON object proving the checks were re-run against a repaired "
            "task (must name at least one of: command, commands, test, "
            "tests, suite, suites, check, checks). REQUIRED with --pass once "
            "a failing verdict has made the work a material repair; recorded "
            "as its own regression row on the verification ledger."
        ),
    )
    p_verify.add_argument(
        "--no-route", action="store_true",
        help=(
            "On --fail, leave the task in review instead of routing it back "
            "for rework. It stays non-completable either way."
        ),
    )

    p_gauntlet = sub.add_parser(
        "gauntlet",
        help="Turn the Gauntlet completion chain on or off for a task",
    )
    p_gauntlet.add_argument("task_ids", nargs="+")
    g_group = p_gauntlet.add_mutually_exclusive_group(required=True)
    g_group.add_argument(
        "--on", dest="enabled", action="store_true",
        help="Require running -> review -> verified -> done for these tasks.",
    )
    g_group.add_argument(
        "--off", dest="enabled", action="store_false",
        help="Restore the classic lifecycle (the verification ledger is kept).",
    )

    p_loop = sub.add_parser(
        "loop-repair",
        help="Record the repair of a block-loop triage card (required "
             "before it can leave triage); also turns the Gauntlet on",
    )
    p_loop.add_argument("task_id")
    p_loop.add_argument("--root-cause", required=True,
                        help="Why the card kept blocking")
    p_loop.add_argument("--change", required=True,
                        help="What was changed to fix it")
    p_loop.add_argument("--check", required=True,
                        help="The check that proved the fix works")
    p_loop.add_argument("--author", default=None)

    p_rrec = sub.add_parser(
        "repair-record",
        help="Record what an automatic repair card changed (its gate reads this)",
    )
    p_rrec.add_argument("task_id", help="The repair card, or the task it repairs")
    p_rrec.add_argument("--root-cause", required=True, help="Why it failed")
    p_rrec.add_argument("--change", required=True, help="What was changed")
    p_rrec.add_argument("--check", required=True, help="How the fix was checked")
    p_rrec.add_argument("--author", default=None)

    p_rgate = sub.add_parser(
        "repair-gate",
        help="Exit 0 only when the open repair of a task recorded a real, new change",
    )
    p_rgate.add_argument("subject_id")

    p_exec = sub.add_parser(
        "exec",
        help="Inspect and control governed executor processes",
        description=(
            "The execution supervisor's operator surface. Every governed "
            "external executor (claude, codex, a gate command) gets a durable "
            "execution record BEFORE its process starts, and that record "
            "always names an owner: a synchronous controller, or the "
            "supervisor itself. These commands answer what is running, who "
            "owns it, what task it belongs to, how long it has run, when it "
            "last showed progress, whether its controller is still alive, "
            "whether it is inside policy, and how it ended. Raw environment "
            "and argv are deliberately never stored or printed."
        ),
    )
    exec_sub = p_exec.add_subparsers(dest="exec_action")

    e_list = exec_sub.add_parser(
        "list", aliases=["ls"], help="List execution records, newest first",
    )
    e_list.add_argument(
        "--active", action="store_true",
        help="Only executions that have not reached a terminal status.",
    )
    e_list.add_argument(
        "--status", default=None,
        help="Filter to one status (completed, failed, timed_out, "
             "controller_lost, stale, terminated, recovered, running, launching).",
    )
    e_list.add_argument("--task", dest="task_id", default=None,
                        help="Only executions belonging to this task.")
    e_list.add_argument("--limit", type=int, default=25)
    e_list.add_argument("--json", action="store_true")

    e_show = exec_sub.add_parser(
        "show", help="Show one execution with its full audit trail",
    )
    e_show.add_argument("execution_id")
    e_show.add_argument("--json", action="store_true")

    e_reconcile = exec_sub.add_parser(
        "reconcile",
        help=(
            "Resolve every non-terminal execution deterministically "
            "(this MUTATES: live orphans are terminated or adopted)"
        ),
    )
    e_reconcile.add_argument("--json", action="store_true")

    e_terminate = exec_sub.add_parser(
        "terminate", help="End a running execution's process group",
    )
    e_terminate.add_argument("execution_id")
    e_terminate.add_argument(
        "--reason", default="operator_request",
        help="Recorded on the execution's termination_reason.",
    )
    e_terminate.add_argument("--json", action="store_true")

    p_lesson_promote = sub.add_parser(
        "lesson-promote",
        help=(
            "Promote a VERIFIED task's finding into a binding lesson that "
            "future applicable tasks are given automatically"
        ),
    )
    p_lesson_promote.add_argument("task_id")
    p_lesson_promote.add_argument(
        "--lesson", required=True,
        help="The rule future work must follow. Quote it.",
    )
    p_lesson_promote.add_argument(
        "--applicability", "--applies-to", required=True,
        help=(
            "Which future tasks this binds, matched EXACTLY: 'all' or "
            "'<dimension>:<value>' where dimension is one of assignee, "
            "project, workspace, lane, template, tenant "
            "(e.g. 'assignee:coder', 'workspace:repo')."
        ),
    )
    p_lesson_promote.add_argument(
        "--evidence", default=None,
        help=(
            "JSON object of extra provenance to store alongside the "
            "kernel-recorded verdict id and verifier."
        ),
    )
    p_lesson_promote.add_argument(
        "--actor", default=None,
        help="Who is promoting it. Defaults to the acting profile.",
    )
    p_lesson_promote.add_argument(
        "--global", dest="allow_global", action="store_true",
        help=(
            "Operator flag: make this lesson binding on EVERY tenant. Only "
            "legal when the source task has no tenant of its own — a "
            "tenant-scoped task's learning never leaves its lane."
        ),
    )

    p_lesson_record_error = sub.add_parser(
        "lesson-record-error",
        help=(
            "Record the fix for a novel error as a non-binding candidate lesson "
            "keyed to that error's stable signature"
        ),
    )
    p_lesson_record_error.add_argument("task_id")
    p_lesson_record_error.add_argument(
        "--error", required=True,
        help="The error text or failure message that was diagnosed.",
    )
    p_lesson_record_error.add_argument(
        "--lesson", required=True,
        help="The verified fix or rule that should be retrieved if this error recurs.",
    )
    p_lesson_record_error.add_argument(
        "--actor", default=None,
        help="Who recorded the candidate. Defaults to the acting profile.",
    )

    p_lesson_retire = sub.add_parser(
        "lesson-retire",
        help="Stop injecting a lesson (the row and its history are kept)",
    )
    p_lesson_retire.add_argument("lesson_id", type=int)
    p_lesson_retire.add_argument(
        "--reason", default=None,
        help="Why it no longer applies. Recorded on the lesson row.",
    )
    p_lesson_retire.add_argument(
        "--actor", default=None,
        help="Who retired it. Defaults to the acting profile.",
    )

    p_lesson_approve = sub.add_parser(
        "lesson-approve",
        help=(
            "Approve a candidate lesson so it starts binding future "
            "applicable tasks"
        ),
    )
    p_lesson_approve.add_argument("lesson_id", type=int)
    p_lesson_approve.add_argument(
        "--approver", required=True,
        help=(
            "Who decided it binds. Required and deliberately never defaulted "
            "to the acting profile: a rule that constrains every future "
            "matching task carries the name of the person who approved it."
        ),
    )

    p_lesson_candidates = sub.add_parser(
        "lesson-candidates",
        help=(
            "List candidate lessons awaiting an operator decision "
            "(a candidate binds nothing)"
        ),
    )
    p_lesson_candidates.add_argument(
        "--tenant", default=None,
        help="Only candidates in this tenant's lane.",
    )
    p_lesson_candidates.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit JSON instead of the text table.",
    )

    p_lessons = sub.add_parser(
        "lessons",
        help="List promoted lessons, or show the ones binding a given task",
    )
    p_lessons.add_argument(
        "--task", dest="task_id", default=None,
        help=(
            "Show exactly the lessons this task receives at dispatch "
            "(scope + exact selector match), instead of listing the board's."
        ),
    )
    p_lessons.add_argument(
        "--tenant", default=None,
        help="Only lessons visible to this tenant (its own plus global ones).",
    )
    p_lessons.add_argument(
        "--source", default=None,
        help="Only lessons promoted from this source task.",
    )
    p_lessons.add_argument(
        "--all", dest="include_retired", action="store_true",
        help="Include retired lessons (default shows active ones only).",
    )
    p_lessons.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit JSON instead of the text table.",
    )
    p_lessons.add_argument(
        "--error", default=None,
        help=(
            "Look up lessons matching an ERROR you just hit, scored by "
            "signature overlap. This is the intended way to reach the corpus: "
            "lessons are not loaded into a task up front, they are retrieved "
            "when something fails. Searches every lesson, bound or not."
        ),
    )

    p_overtaken = sub.add_parser(
        "reconcile-overtaken",
        help=(
            "Close a card whose objective was satisfied by intervening "
            "events, without claiming it was executed"
        ),
        description=(
            "Records the terminal disposition OVERTAKEN_BY_EVENTS. The card "
            "becomes terminal (no redispatch, no longer gates dependents, out "
            "of active-work views) while its title, body, steps, runs, "
            "verdicts, comments, attachments, counters and event history are "
            "left exactly as they are. No result and no completion timestamp "
            "are written: the work was not done, and reporting must be able "
            "to tell that apart from work that was."
        ),
    )
    p_overtaken.add_argument("task_id")
    p_overtaken.add_argument(
        "--reason",
        required=True,
        help="Why the objective is already satisfied (recorded, required)",
    )
    p_overtaken.add_argument(
        "--evidence",
        required=True,
        help=(
            "The verified intervening event that satisfied it — commit, run, "
            "command output, task id (recorded, required)"
        ),
    )
    p_overtaken.add_argument(
        "--superseded-by",
        dest="superseded_by",
        nargs="+",
        default=None,
        help="Task ids / commits / refs that superseded this card",
    )
    p_overtaken.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the reconciliation without mutating state",
    )
    p_overtaken.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="Emit machine-readable JSON result",
    )


def build_parser(parent_subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Upstream ``kanban_parser.build_parser`` plus the NorCal subcommands/flags."""
    kanban_parser = _upstream_build_parser(parent_subparsers)
    _add_norcal_commands(kanban_parser)
    return kanban_parser


# --- Flag parsing helpers ---

def _none_profile(value: str) -> Optional[str]:
    """``none`` / ``-`` / ``null`` mean "unassign"."""
    return None if value.lower() in {"none", "-", "null"} else value


def _parse_metadata_flag(raw: Optional[str]) -> tuple[Optional[dict], int]:
    """Parse ``--metadata`` JSON; returns ``(dict|None, rc)`` with rc=2 on error."""
    if not raw:
        return None, 0
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        return None, _err(f"kanban: --metadata: {exc}", 2)
    return metadata, 0


def _run_state_kwargs(args: argparse.Namespace, cmd: str) -> tuple[Optional[dict[str, str]], int]:
    """``--state-type``/``--state-name`` must be given together: ``(kwargs, 0)`` or ``(None, 2)``."""
    st = getattr(args, "state_type", None)
    sn = getattr(args, "state_name", None)
    if (st is None) != (sn is None):
        return None, _err(f"kanban {cmd}: pass both --state-type and --state-name, or omit both", 2)
    return ({} if st is None else {"state_type": st, "state_name": sn}), 0


def _parse_workspace_flag(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``--workspace`` -> ``(kind, path|None)``: ``scratch``, ``worktree``, ``worktree:<p>``, ``dir:<p>``.
    Omitted -> ``(None, None)`` so ``create_task`` can tell "default" from an explicit scratch."""
    if not value:
        return (None, None)
    v = value.strip()
    if v in {"scratch", "worktree"}:
        return (v, None)
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if not v.startswith(prefix):
            continue
        path = v[len(prefix):].strip()
        if not path:
            raise argparse.ArgumentTypeError(f"--workspace {prefix} requires a path after the colon")
        return (kind, os.path.expanduser(path))
    raise argparse.ArgumentTypeError(f"unknown --workspace value {value!r}: use scratch, worktree, "
                                     "worktree:<path>, or dir:<path>")


def _parse_branch_flag(value: Optional[str]) -> Optional[str]:
    """Normalize an optional branch name from ``kanban create --branch``."""
    if value is None:
        return None
    branch = value.strip()
    if not branch:
        raise argparse.ArgumentTypeError("--branch requires a non-empty name")
    if branch.startswith("-"):
        raise argparse.ArgumentTypeError("--branch must not start with '-'")
    if any(ch.isspace() for ch in branch):
        raise argparse.ArgumentTypeError("--branch must not contain whitespace")
    return branch


def _check_dispatcher_presence(hermes_home: Optional[Path] = None) -> tuple[bool, str]:
    """``(running, message)`` for the "will anything dispatch this?" warning: True when a gateway is
    alive for this HERMES_HOME with ``kanban.dispatch_in_gateway`` on, else False + human guidance.
    Fails OPEN (probe/config errors -> ``(True, "")``) — a missed warning beats crying wolf.
    ``hermes_home`` scopes the probe to a profile dir (dashboard backend); CLI callers pass None.

    The dashboard plugin API passes it because the dashboard backend process can be running under a
    different HERMES_HOME than the profile the request targets, which otherwise produced a "no gateway is
    running" warning against a perfectly healthy profile gateway (#71211). CLI callers leave it ``None`` and
    keep the existing process-level behavior.
    """
    try:
        from gateway.status import resolve_gateway_liveness  # type: ignore

        # Same ladder as the dashboard status endpoints so PID-file-less / cross-container gateways
        # aren't misreported; use_cache=False because this one-shot probe must see the state now.
        liveness = resolve_gateway_liveness(profile_dir=hermes_home, use_cache=False)
    except Exception:
        return (True, "")  # can't probe — silent
    if liveness.probe_error:  # resolver swallows per-rung failures; "can't tell" != "no gateway"
        return (True, "")
    pid = liveness.pid
    # Even if the gateway is up, dispatch_in_gateway may be off (can't tell -> assume default).
    if pid and bool(_kanban_config().get("dispatch_in_gateway", True)):
        return (True, f"gateway pid={pid}, dispatch enabled")
    if pid:
        return (False, "Gateway is running but kanban.dispatch_in_gateway=false in "
                "config.yaml — the task will sit in 'ready' until you flip it "
                "back on and restart the gateway, OR run the legacy "
                "standalone daemon (`hermes kanban daemon --force`).")
    return (False, "No gateway is running — the task will sit in 'ready' until you "
            "start it. Run:\n    hermes gateway start\n"
            "The gateway hosts an embedded dispatcher (tick interval 60s by "
            "default); your task will be picked up on the next tick after "
            "the gateway comes up.")


# --- Command dispatch ---

def kanban_command(args: argparse.Namespace) -> int:
    """Entry point from ``hermes kanban …``; returns a shell-style exit code."""
    action = getattr(args, "kanban_action", None)
    if not action:
        parser = getattr(args, "_kanban_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print("usage: hermes kanban <action> [options]\n"
                  "Run 'hermes kanban --help' for the full list of actions.", file=sys.stderr)
        return 0

    # Fast-fail for UX only; the durable trust boundary is in kanban_db, since children can
    # import DB mutators directly.
    if _is_delegated_child_cli_mutation(args):
        return _err("kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI")

    # `boards …` manages board metadata and the current-board pointer itself, so it must ignore
    # the `--board` routing override (else `--board beta boards show` reports beta).
    if action == "boards":
        return _dispatch_boards(args)

    # `--board <slug>` pins HERMES_KANBAN_BOARD for the duration of this call so it inherits the
    # exact resolution the dispatcher uses for workers.
    board_override = getattr(args, "board", None)
    board_scope = contextlib.nullcontext()
    if board_override:
        try:
            normed = kb._normalize_board_slug(board_override)
        except ValueError as exc:
            return _err(f"kanban: {exc}", 2)
        if not normed:
            return _err("kanban: --board requires a slug", 2)
        # Boards other than 'default' must already exist — typoed slugs would otherwise silently
        # create an empty board.
        if normed != kb.DEFAULT_BOARD and not kb.board_exists(normed):
            return _err(f"kanban: board {normed!r} does not exist. "
                        f"Create it with `hermes kanban boards create {normed}`.")
        board_scope = kb.scoped_current_board(normed)

    with board_scope:
        # `repair` dispatches BEFORE auto-init: on a corrupt DB init_db() itself raises
        # KanbanDbCorruptError, which would turn every repair into "could not initialize database".
        if action == "repair":
            return _cmd_repair(args)
        # init_db is idempotent (one sqlite_master SELECT when tables exist) and prevents
        # "no such table: tasks" on first use from a fresh HERMES_HOME.
        try:
            kb.init_db()
        except Exception as exc:
            return _err(f"kanban: could not initialize database: {exc}")

        handler = _HANDLERS.get(action)
        if not handler:
            return _err(f"kanban: unknown action {action!r}", 2)
        try:
            return int(handler(args) or 0)
        except (ValueError, RuntimeError, PermissionError) as exc:
            return _err(f"kanban: {exc}")


# --- Handlers ---

def _profile_author() -> str:
    """Best-effort author name for an interactive CLI call."""
    from hermes_cli.profiles import current_profile_name
    return current_profile_name("user") or "user"


_DELEGATED_CHILD_DENIED_ACTIONS: frozenset[str] = frozenset({
    "init", "create", "swarm", "assign", "reclaim", "reassign", "link", "unlink",
    "claim", "comment", "attach", "attach-rm", "complete", "edit", "block",
    "schedule", "unblock", "promote", "archive", "dispatch", "daemon", "repair",
    "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen-review",
    "reconcile-overtaken", "loop-repair",
    "gc",
})

_DELEGATED_CHILD_DENIED_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "import",
})


def _is_delegated_child_cli_mutation(args: argparse.Namespace) -> bool:
    action = getattr(args, "kanban_action", None)
    if action == "boards":
        if (getattr(args, "boards_action", None) or "list") not in _DELEGATED_CHILD_DENIED_BOARD_ACTIONS:
            return False
    elif action not in _DELEGATED_CHILD_DENIED_ACTIONS:
        return False
    from agent.delegation_context import kanban_path_is_fenced

    return kanban_path_is_fenced(kb.kanban_home()) or kanban_path_is_fenced(kb.kanban_db_path())


def _joined_words(words) -> Optional[str]:
    """Free-text positional ``nargs="*"`` words -> stripped string, or None when absent."""
    return " ".join(words).strip() if words else None


def _stripped_or_none(value: Optional[str]) -> Optional[str]:
    """``None`` stays ``None``; otherwise strip, and treat the empty string as ``None``."""
    return None if value is None else (value.strip() or None)


def _ok_or_err(ok, fail: str, done: str) -> int:
    """Single-mutation handlers: print ``done`` (rc 0) or ``fail`` to stderr (rc 1)."""
    if not ok:
        return _err(fail)
    print(done)
    return 0


def _bulk_ids(args: argparse.Namespace) -> list[str]:
    """Positional ``task_id`` plus ``--ids`` extras (bulk verbs)."""
    return [args.task_id] + list(getattr(args, "ids", None) or [])


def _require_ids(args: argparse.Namespace) -> tuple[list[str], int]:
    """``args.task_ids`` -> ``(ids, 0)`` or ``([], 1)`` after printing the standard error."""
    ids = list(args.task_ids or [])
    if not ids:
        return ids, _err("at least one task_id is required")
    return ids, 0


def _parse_duration(val) -> Optional[int]:
    """``30s`` / ``5m`` / ``2h`` / ``1d`` or a raw integer → seconds; None for empty input;
    ValueError on malformed input."""
    if val is None or val == "":
        return None
    s = str(val).strip().lower()
    try:
        return int(s)  # bare integer → seconds
    except ValueError:
        pass
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not (s and s[-1] in units):
        raise ValueError(f"malformed duration {val!r} (expected 30s, 5m, 2h, 1d, or a number)")
    try:
        n = float(s[:-1])
    except ValueError as exc:
        raise ValueError(f"malformed duration {val!r}") from exc
    return int(n * units[s[-1]])


def _cmd_init(args: argparse.Namespace) -> int:
    path = kb.init_db()
    print(f"Kanban DB initialized at {path}")
    print()
    # Profiles on disk == assignees already addressable.
    try:
        profiles = kb.list_profiles_on_disk()
    except Exception:
        profiles = []
    if profiles:
        print(f"Discovered {len(profiles)} profile(s) on disk; any of these can be an --assignee:")
        for name in profiles:
            print(f"  {name}")
    else:
        print("No profiles found under ~/.hermes/profiles/.\n"
              "Create one with `hermes -p <name> setup` before assigning tasks.")
    print(
        "\nNext step: start the gateway so ready tasks actually get picked up.\n"
        "  hermes gateway start\n\n"
        "The gateway hosts an embedded dispatcher that ticks every 60 seconds\n"
        "by default (config: kanban.dispatch_interval_seconds). Without a\n"
        "running gateway, tasks stay in 'ready' forever."
    )
    return 0


def _cmd_heartbeat(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbd.heartbeat_worker(conn, args.task_id, note=getattr(args, "note", None),
                                 expected_run_id=_worker_run_id_for(args.task_id))
    return _ok_or_err(ok, f"cannot heartbeat {args.task_id} (not running?)",
                      f"Heartbeat recorded for {args.task_id}")


def _cmd_assignees(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        data = kb.known_assignees(conn)
    if _json_out(args, data):
        return 0
    if not data:
        print("(no assignees — create a profile with `hermes -p <name> setup`)")
        return 0
    print(f"{'NAME':20s}  {'ON DISK':8s}  COUNTS")
    for entry in data:
        on_disk = "yes" if entry["on_disk"] else "no"
        print(f"{entry['name']:20s}  {on_disk:8s}  {_fmt_counts(entry['counts'] or {}, '(idle)')}")
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from agent.delegation_context import is_dispatcher_owned_worker_context

    body = args.body
    body_file = getattr(args, "body_file", None)
    if body is not None and body_file is not None:
        return _err("kanban: --body and --body-file are mutually exclusive", 2)
    if body_file is not None:
        try:
            body = sys.stdin.read() if body_file == "-" else Path(body_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            return _err(f"kanban: --body-file: {exc}", 2)

    try:
        ws_kind, ws_path = _parse_workspace_flag(args.workspace)
        branch_name = _parse_branch_flag(getattr(args, "branch", None))
    except argparse.ArgumentTypeError as exc:
        return _err(f"kanban: {exc}", 2)
    if branch_name and ws_kind != "worktree":
        return _err("kanban: --branch is only valid with --workspace worktree", 2)
    try:
        max_runtime = _parse_duration(getattr(args, "max_runtime", None))
    except ValueError as exc:
        return _err(f"kanban: --max-runtime: {exc}", 2)
    max_retries = getattr(args, "max_retries", None)
    if max_retries is not None and max_retries < 1:
        return _err(f"kanban: --max-retries must be >= 1 (got {max_retries}); "
                    "use 1 to trip on the first failure.", 2)
    production_actions = list(getattr(args, "production_actions", None) or [])
    if production_actions:
        # Rule 6: validate against the installed registry (and refuse a
        # worker) before opening the board, so a refused grant is a clean
        # exit with no connection touched. create_task re-checks both.
        try:
            production_actions = kb.validate_production_actions(production_actions)
        except kb.ProductionActionError as exc:
            return _err(f"kanban: --production-action: {exc}", 2)
        if "HERMES_KANBAN_TASK" in os.environ:  # presence, not value
            return _err("kanban: --production-action is refused from a dispatcher-owned "
                        "worker (HERMES_KANBAN_TASK is set); a worker can never grant "
                        "production authority to another card", 2)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title=args.title, body=body, assignee=args.assignee,
            created_by=args.created_by or _profile_author(),
            workspace_kind=ws_kind, workspace_path=ws_path, branch_name=branch_name,
            project_id=getattr(args, "project", None), tenant=args.tenant, priority=args.priority,
            parents=tuple(args.parent or ()), triage=bool(getattr(args, "triage", False)),
            idempotency_key=getattr(args, "idempotency_key", None),
            max_runtime_seconds=max_runtime, skills=getattr(args, "skills", None) or None,
            max_retries=max_retries, model_override=getattr(args, "model_override", None),
            provider_override=getattr(args, "provider_override", None),
            goal_mode=bool(getattr(args, "goal_mode", False)),
            goal_max_turns=getattr(args, "goal_max_turns", None),
            completion_contract=getattr(args, "completion_contract", None),
            initial_status=getattr(args, "initial_status", "running"),
            creator_task_id=(os.environ.get("HERMES_KANBAN_TASK")
                             if is_dispatcher_owned_worker_context() else None),
            executor_lane=getattr(args, "executor_lane", None),
            recovery_gate_cmd=getattr(args, "recovery_gate_cmd", None),
            gauntlet=getattr(args, "gauntlet", None),
            recovery_owner=getattr(args, "recovery_owner", None),
            repairs_task_id=getattr(args, "repairs_task_id", None),
            umbrella_task_id=getattr(args, "umbrella_task_id", None),
            production_actions=production_actions or None,
        )
        task = kb.get_task(conn, task_id)
    if getattr(args, "json", False):
        _print_json(_task_to_dict(task))
    else:
        print(f"Created {task_id}  ({task.status}, assignee={task.assignee or '-'})")
        # Warn only for ready+assigned tasks that would sit without a dispatcher (triage/todo idle
        # by design, unassigned can't dispatch); skipped under --json so stdout stays parseable.
        if task.status == "ready" and task.assignee:
            running, message = _check_dispatcher_presence()
            if not running and message:
                print(f"\n⚠  {message}", file=sys.stderr)
    return 0


def _cmd_swarm(args: argparse.Namespace) -> int:
    try:
        workers = [ks.parse_worker_arg(raw) for raw in (args.worker or [])]
    except ValueError as exc:
        return _err(f"kanban swarm: {exc}", 2)
    if not workers:
        return _err("kanban swarm: at least one --worker is required", 2)
    with kbc.connect_closing() as conn:
        created = ks.create_swarm(
            conn, goal=args.goal, workers=workers, verifier_assignee=args.verifier,
            synthesizer_assignee=args.synthesizer, tenant=args.tenant,
            created_by=args.created_by or _profile_author(), priority=args.priority,
            idempotency_key=getattr(args, "idempotency_key", None),
        )
    if getattr(args, "json", False):
        _print_json(created.as_dict())
    else:
        print(f"Swarm root: {created.root_id}\n"
              "Workers: " + ", ".join(created.worker_ids) + "\n"
              f"Verifier: {created.verifier_id}\n"
              f"Synthesizer: {created.synthesizer_id}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    assignee = args.assignee
    if args.mine and not assignee:
        assignee = _profile_author()
    with kbc.connect_closing() as conn:
        # Cheap mini-dispatch so list reflects dependencies cleared since the last tick.
        kb.recompute_ready(conn)
        tasks = kb.list_tasks(
            conn, assignee=assignee, status=args.status, tenant=args.tenant, session_id=args.session,
            include_archived=args.archived, order_by=getattr(args, "sort", None),
            workflow_template_id=args.workflow_template_id, current_step_key=args.current_step_key,
        )
    if _json_out(args, [_task_to_dict(t) for t in tasks]):
        return 0
    # Passive discoverability: only multi-board users see which board this is.
    try:
        all_boards = kb.list_boards(include_archived=False)
    except Exception:
        all_boards = []
    if len(all_boards) > 1:
        other_count = len(all_boards) - 1
        print(f"Board: {kb.get_current_board()} ({other_count} other board{'s' if other_count != 1 else ''} — "
              f"`hermes kanban boards list`)\n")
    if not tasks:
        print("(no matching tasks)")
        return 0
    for t in tasks:
        print(_fmt_task_line(t))
    return 0


def _print_diagnostics(diags, indent: str, *, with_kind: bool) -> None:
    """Shared human rendering for ``show`` and ``diagnostics`` (suggested actions only)."""
    sev_marker = {"warning": "⚠", "error": "!!", "critical": "!!!"}
    for d in diags:
        head = f"{d.kind}: {d.title}" if with_kind else d.title
        print(f"{indent}{sev_marker.get(d.severity, '?')} [{d.severity}] {head}")
        if d.data:
            bits = [f"{k}={','.join(str(x) for x in v)}" if isinstance(v, list) else f"{k}={v}"
                    for k, v in d.data.items()]
            if bits:
                print(f"{indent}   data: {' | '.join(bits)}")
        for a in d.actions:
            if a.suggested:
                print(f"{indent}   → {a.label}")


def _print_section(title: str, lines) -> None:
    """Blank line, ``title``, then each line (``show`` body sections)."""
    print()
    print(title)
    for line in lines:
        print(line)


def _cmd_show(args: argparse.Namespace) -> int:
    rsk, rc = _run_state_kwargs(args, "show")
    if rc:
        return rc
    graph = None
    want_json = getattr(args, "json", False)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, args.task_id)
        if not task:
            return _err(f"no such task: {args.task_id}")
        comments = kb.list_comments(conn, args.task_id)
        events = kb.list_events(conn, args.task_id)
        parents = kb.parent_ids(conn, args.task_id)
        children = kb.child_ids(conn, args.task_id)
        runs = kb.list_runs(conn, args.task_id, **rsk)
        # Workers hand off via task_runs.summary; tasks.result stays NULL unless set.
        latest_summary = kb.latest_summary(conn, args.task_id)
        if not want_json:
            graph = kb.task_graph_context(conn, task.id)

    if want_json:
        _print_json({
            "task": _task_to_dict(task), "latest_summary": latest_summary, "parents": parents, "children": children,
            "comments": [_obj_dict(c, ("author", "body", "created_at")) for c in comments],
            "events": [_obj_dict(e, ("kind", "payload", "created_at", "run_id")) for e in events],
            "runs": [_obj_dict(r, _SHOW_RUN_FIELDS) for r in runs],
        })
        return 0

    def field(label: str, value) -> None:
        print(f"  {label + ':':<11}{value}")

    print(f"Task {task.id}: {task.title}")
    field("status", task.status)
    if task.terminal_disposition:
        # Printed immediately under status because it qualifies it: an
        # archived card that was overtaken reads very differently from one an
        # operator archived as noise, and the difference must not require
        # opening the event log.
        print(f"  disposition: {task.terminal_disposition.upper()}")
        if task.terminal_disposition == kb.DISPOSITION_OVERTAKEN_BY_EVENTS:
            print("               (objective satisfied elsewhere; NOT executed)")
        if task.disposition_reason:
            for line in task.disposition_reason.splitlines():
                print(f"               {line}" if line else "")
    field("assignee", task.assignee or "-")
    if task.tenant:
        field("tenant", task.tenant)
    field("workspace", f"{task.workspace_kind}" + (f" @ {task.workspace_path}" if task.workspace_path else ""))
    if task.branch_name:
        field("branch", task.branch_name)
    if task.skills:
        field("skills", ", ".join(task.skills))
    if task.production_actions:
        # Rule 6: the structured authorization is the only one that counts.
        print(f"  production actions: {', '.join(task.production_actions)}")
    if task.model_override:
        _prov = f" (provider: {task.provider_override})" if task.provider_override else ""
        field("model", f"{task.model_override}{_prov}")
    # Effective retry threshold (task > config > default) explains auto-blocks.
    if task.max_retries is not None:
        print(f"  max-retries: {task.max_retries} (task)")
    else:
        cfg_val = _kanban_config().get("failure_limit")
        if cfg_val is not None and int(cfg_val) != kb.DEFAULT_FAILURE_LIMIT:
            print(f"  max-retries: {int(cfg_val)} (config kanban.failure_limit)")
        else:
            print(f"  max-retries: {kb.DEFAULT_FAILURE_LIMIT} (default)")
    field("created", f"{_fmt_ts(task.created_at)} by {task.created_by or '-'}")

    # Diagnostics up top so CLI users see distress signals before scrolling.
    from hermes_cli import kanban_diagnostics as kd
    diags = kd.compute_task_diagnostics(task, events, runs, graph=graph)
    if diags:
        print(f"\n  Diagnostics ({len(diags)}):")
        _print_diagnostics(diags, "    ", with_kind=False)
    if task.started_at:
        field("started", _fmt_ts(task.started_at))
    if task.completed_at:
        field("completed", _fmt_ts(task.completed_at))
    if parents:
        field("parents", ", ".join(parents))
    if children:
        field("children", ", ".join(children))
    if task.body:
        _print_section("Body:", [task.body])
    if task.result:
        _print_section("Result:", [task.result])
    elif latest_summary:
        _print_section("Latest summary:", [latest_summary])
    if comments:
        _print_section(f"Comments ({len(comments)}):",
                       (f"  [{_fmt_ts(c.created_at)}] {c.author}: {c.body}" for c in comments))
    if events:
        _print_section(f"Events ({len(events)}):", (
            f"  [{_fmt_ts(e.created_at)}]{f' [run {e.run_id}]' if e.run_id else ''} {e.kind}"
            f"{f' {e.payload}' if e.payload else ''}" for e in events[-20:]))
    if runs:
        print()
        print(f"Runs ({len(runs)}):")
        for r in runs:
            # Clamp to 0 so NTP backward-jumps don't print negative seconds.
            elapsed = max(0, r.ended_at - r.started_at) if r.ended_at else None
            el = f"{elapsed}s" if elapsed is not None else "active"
            outcome = r.outcome or r.status or "active"
            print(f"  #{r.id:<3} {outcome:<12} @{r.profile or '-'}  {el}  {_fmt_ts(r.started_at)}")
            if r.summary:
                print(f"        → {r.summary.splitlines()[0][:160]}")
            if r.error:
                print(f"        ! {r.error.splitlines()[0][:160]}")
    return 0


def _cmd_assign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    with kbc.connect_closing() as conn:
        ok = kb.assign_task(conn, args.task_id, profile)
    return _ok_or_err(ok, f"no such task: {args.task_id}",
                      f"Assigned {args.task_id} to {profile or '(unassigned)'}")


def _cmd_set_model(args: argparse.Namespace) -> int:
    model = args.model
    if model is not None and model.lower() in {"none", "-", "null", ""}:
        model = None
    provider = getattr(args, "provider", None)
    try:
        with kbc.connect_closing() as conn:
            ok = kb.set_model_override(conn, args.task_id, model, provider=provider)
    except (ValueError, RuntimeError) as exc:
        return _err(f"kanban: {exc}", 2)
    if not ok:
        return _err(f"no such task: {args.task_id}")
    if model:
        label = f"{provider}:{model}" if provider else model
        print(f"Set model override on {args.task_id}: {label} (applies on next dispatch)")
    else:
        print(f"Cleared model override on {args.task_id} (worker uses its profile default)")
    return 0


def _cmd_reclaim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.reclaim_task(conn, args.task_id, reason=getattr(args, "reason", None))
    return _ok_or_err(ok, f"cannot reclaim {args.task_id} (not running or unknown id)",
                      f"Reclaimed {args.task_id}")


def _cmd_reassign(args: argparse.Namespace) -> int:
    profile = _none_profile(args.profile)
    reclaim = bool(getattr(args, "reclaim", False))
    with kbc.connect_closing() as conn:
        ok = kb.reassign_task(conn, args.task_id, profile, reclaim_first=reclaim, reason=getattr(args, "reason", None))
    return _ok_or_err(
        ok,
        f"cannot reassign {args.task_id} (unknown id, or still running — pass --reclaim to release first)",
        f"Reassigned {args.task_id} to {profile or '(unassigned)'}" + (" (claim reclaimed)" if reclaim else ""),
    )


def _rows_by_task(conn, table: str, ids: list[str]) -> dict[str, list]:
    """``{task_id: [rows ordered by id]}`` for every id (empty list when none)."""
    by = {i: [] for i in ids}
    placeholders = ",".join(["?"] * len(ids))
    for row in conn.execute(f"SELECT * FROM {table} WHERE task_id IN ({placeholders}) ORDER BY id", tuple(ids)):
        by.setdefault(row["task_id"], []).append(row)
    return by


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    """List active diagnostics on the board via the same rule engine the dashboard uses."""
    from hermes_cli import kanban_diagnostics as kd
    # Honour kanban.default_assignee as the fallback for unassigned ready tasks (#27145),
    # kanban.max_in_progress as the global concurrency cap (#33488), kanban.max_in_progress_per_profile as
    # the per-profile cap (#21582), and kanban.max_spawn as the per-tick spawn limit (#28805). Same
    # semantics as the gateway dispatch path so behavior matches whether the user runs the CLI directly or
    # relies on the gateway-embedded dispatcher.
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    with kbc.connect_closing() as conn:
        # Either one-task mode or fleet mode.
        if getattr(args, "task", None):
            task = kb.get_task(conn, args.task)
            if task is None:
                return _err(f"no such task: {args.task}")
            diags_by_task = {args.task: kd.compute_task_diagnostics(
                task, kb.list_events(conn, args.task), kb.list_runs(conn, args.task),
                graph=kb.task_graph_context(conn, args.task), config=diag_config)}
        else:
            # Fleet mode: pull all non-archived tasks + their events/runs.
            rows = list(conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall())
            ids = [r["id"] for r in rows]
            diags_by_task = {}
            if ids:
                ev_by = _rows_by_task(conn, "task_events", ids)
                run_by = _rows_by_task(conn, "task_runs", ids)
                graph_by = kb.task_graph_contexts(conn, ids)
                for r in rows:
                    tid = r["id"]
                    dl = kd.compute_task_diagnostics(r, ev_by.get(tid, []), run_by.get(tid, []),
                                                     graph=graph_by.get(tid), config=diag_config)
                    if dl:
                        diags_by_task[tid] = dl

        sev = getattr(args, "severity", None)
        if sev:
            floor = kd.SEVERITY_ORDER.index(sev)
            diags_by_task = {tid: kept for tid, dl in diags_by_task.items()
                             if (kept := [d for d in dl if kd.SEVERITY_ORDER.index(d.severity) >= floor])}

        # Map task_id → title/status/assignee for the table output.
        meta: dict[str, dict] = {}
        if diags_by_task:
            placeholders = ",".join(["?"] * len(diags_by_task))
            for r in conn.execute(f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                                  tuple(diags_by_task.keys())):
                meta[r["id"]] = {k: r[k] for k in ("title", "status", "assignee")}

    # What this home believes it may claim on a shared board (#113620).
    allowlist = kbd.dispatch_profile_allowlist_summary()

    if getattr(args, "json", False):
        # Per-task rows unchanged; the home-scope allowlist rides as a trailing row
        # (task_id null) so existing `payload[0]["diagnostics"]` consumers keep working.
        _print_json([{"task_id": tid, **meta.get(tid, {}), "diagnostics": [d.to_dict() for d in dl]}
                     for tid, dl in diags_by_task.items()]
                    + [{"task_id": None, "dispatch_profiles": allowlist, "diagnostics": []}])
        return 0

    print(f"kanban.dispatch_profiles: {allowlist}")
    if not diags_by_task:
        print("No active diagnostics on this board.")
        return 0

    total = sum(len(dl) for dl in diags_by_task.values())
    print(f"{total} active diagnostic(s) across {len(diags_by_task)} task(s):\n")
    for tid, dl in diags_by_task.items():
        m = meta.get(tid, {})
        print(f"  {tid}  {m.get('status') or '?':8s}  @{m.get('assignee') or '(unassigned)':18s}  "
              f"{m.get('title') or '(untitled)'}")
        _print_diagnostics(dl, "    ", with_kind=True)
        print()
    return 0


def _cmd_link(args: argparse.Namespace) -> int:
    # A worker linking its own running card (dependency-block handoff) proves
    # ownership with its run id; linking a foreign task never needs one.
    expected_child_run_id = (
        _worker_run_id_for(args.child_id)
        if args.child_id == os.environ.get("HERMES_KANBAN_TASK") else None)
    with kbc.connect_closing() as conn:
        gated = kb.link_tasks(conn, args.parent_id, args.child_id,
                              expected_child_run_id=expected_child_run_id)
    print(f"Linked {args.parent_id} -> {args.child_id}")
    if gated:
        print(
            f"Note: {args.child_id} was ready and is now todo — parent "
            f"{args.parent_id} is not done yet. The ready -> running claim "
            f"re-checks parents, so the child only runs after the parent "
            f"completes; use `hermes kanban unlink {args.parent_id} {args.child_id}` "
            f"to run it now."
        )
    return 0


def _cmd_unlink(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        try:
            ok = kb.unlink_tasks(conn, args.parent_id, args.child_id)
        except kb.VerifierLinkProtected as exc:
            # Refuse to strand a live verifier: the Gauntlet link is its only route back.
            return _err(f"kanban: {exc}", 2)
    return _ok_or_err(ok, f"No such link: {args.parent_id} -> {args.child_id}",
                      f"Unlinked {args.parent_id} -> {args.child_id}")


def _cmd_claim(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        task = kb.claim_task(conn, args.task_id, ttl_seconds=args.ttl)
        if task is None:
            existing = kb.get_task(conn, args.task_id)
            if existing is None:
                return _err(f"no such task: {args.task_id}")
            return _err(f"cannot claim {args.task_id}: status={existing.status} "
                        f"lock={existing.claim_lock or '(none)'}")
        workspace = kbw.resolve_workspace(task)
        kbw.set_workspace_path(conn, task.id, str(workspace))
    print(f"Claimed {task.id}\nWorkspace: {workspace}")
    return 0


def _cmd_comment(args: argparse.Namespace) -> int:
    body = " ".join(args.text).strip()
    if args.max_len is not None:
        if args.max_len < 1:
            return _err("kanban: --max-len must be positive", 2)
        if len(body) > args.max_len:
            suffix = f"\n\n[trimmed to {args.max_len} chars by --max-len]"
            body = body[: max(0, args.max_len - len(suffix))].rstrip() + suffix
    author = args.author or _profile_author()
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, args.task_id, author, body)
    print(f"Comment added to {args.task_id}")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    """Attach a local file via the shared ``store_attachment_bytes`` path (same 25 MB cap and name
    sanitisation as the dashboard upload and agent tool)."""
    import mimetypes
    _worker_run_id_for(args.task_id)

    src = Path(args.path).expanduser()
    if not src.is_file():
        return _err(f"kanban: no such file: {src}")
    data = src.read_bytes()
    name = args.name or src.name
    content_type = args.content_type or mimetypes.guess_type(name)[0]
    uploaded_by = args.author or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            att_id = kb.store_attachment_bytes(conn, args.task_id, name, data, content_type=content_type,
                                               uploaded_by=uploaded_by)
    except kb.AttachmentTooLarge as exc:
        return _err(f"kanban: {exc}")
    print(f"Attached {name} to {args.task_id} (attachment {att_id}, {len(data)} bytes)")
    return 0


def _cmd_attachments(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        atts = kb.list_attachments(conn, args.task_id)
    if _json_out(args, [_obj_dict(a, _ATTACHMENT_FIELDS) for a in atts], ascii=True):
        return 0
    if not atts:
        print(f"No attachments on {args.task_id}")
        return 0
    print(f"Attachments on {args.task_id}:")
    for a in atts:
        ct = a.content_type or "-"
        print(f"  [{a.id}] {a.filename}  ({a.size} bytes, {ct}, by {a.uploaded_by or '-'})")
        print(f"        {a.stored_path}")
    return 0


def _cmd_attach_rm(args: argparse.Namespace) -> int:
    """Delete an attachment by id (removes the row and the on-disk blob).

    Refused unless the owning card is archived: attachments are evidence
    and task history is append-only until the card is archived (rule 8).
    """
    with kbc.connect_closing() as conn:
        try:
            removed = kb.delete_attachment(conn, args.attachment_id)
        except kb.AttachmentDeleteRefused as exc:
            return _err(
                f"kanban: refused to delete attachment {args.attachment_id} "
                f"({exc.attachment.filename}): task {exc.attachment.task_id} is "
                f"{exc.status!r}; archive the card first "
                f"(hermes kanban archive {exc.attachment.task_id}). Task history, "
                "including attachments, is append-only until the card is archived.")
    if removed is None:
        return _err(f"no such attachment: {args.attachment_id}")
    print(f"Deleted attachment {args.attachment_id} ({removed.filename}) from {removed.task_id}")
    return 0


def _worker_run_id_for(task_id: str) -> Optional[int]:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        raise ValueError(f"worker is scoped to task {env_tid}; refusing to mutate {task_id}")
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if os.environ.get("HERMES_KANBAN_TASK") != task_id or not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _goal_mode_handoff_rejection(task: Optional[kb.Task], evidence: str):
    """Goal judge for every terminal worker handoff (including review).

    Returns ``(verdict, reason_or_None)``: ``"done"`` allows; ``"blocked"`` = judge ruled the goal
    unachievable; ``"continue"``/``"wait"`` reject with the judge's reason. Judge failures allow
    the handoff (logged).

    See #100954.
    ``{"done", None}`` means the judge allows the handoff; anything else is a rejection whose verdict
    disambiguates the guidance the caller gives the worker (``continue`` = not done yet, ``blocked`` =
    judged unachievable — see #100954).
    """
    if task is None or not task.goal_mode:
        return ("done", None)
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        client, model = None, None
    if client is None or not model:
        return ("done", None)

    from hermes_cli.goals import judge_goal

    verdict, reason, transport_failed = "done", "", False
    try:
        # Headless handoff checks run outside any agent turn: bind the per-task relay-affinity
        # scope (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{task.id}")
        try:
            verdict, reason, _, _, transport_failed = judge_goal(
                goal=f"{task.title}\n\n{task.body or ''}".strip(),
                last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge check failed, allowing lifecycle handoff: %s",
                                             judge_exc, exc_info=True)
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        import logging as _logging

        _logging.getLogger(__name__).warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return ("done", None)
    return (verdict, None if verdict == "done" else reason)


def _goal_gate_error(conn, tid: str, evidence: str, handoff: str, blocked_hint: str,
                     continue_hint: str) -> Optional[str]:
    """Goal-mode judge gate shared by ``complete`` / ``request-review`` (mirrors tools/kanban_tools.py);
    applied to every terminal handoff so request-review can't bypass it. Returns the error line, or
    None to allow."""
    verdict, rejection = _goal_mode_handoff_rejection(kb.get_task(conn, tid), evidence)
    if verdict == "blocked":
        return (f"kanban: goal {handoff} of {tid} rejected: judge ruled "
                f"the goal unachievable — {rejection}. {blocked_hint}")
    if rejection is not None:
        return f"kanban: goal {handoff} of {tid} rejected by judge: {rejection}. {continue_hint}"
    return None


def _cmd_complete(args: argparse.Namespace) -> int:
    """Mark one or more tasks done. Supports a single id or a list."""
    ids, rc = _require_ids(args)
    if rc:
        return rc
    summary = getattr(args, "summary", None)
    raw_meta = getattr(args, "metadata", None)
    # Handoff fields are per-run; refuse to copy them across N runs.
    if len(ids) > 1 and (summary or raw_meta):
        return _err("kanban: --summary / --metadata are per-task and can't be used "
                    "with multiple ids (would apply the same handoff to every task). "
                    "Complete tasks one at a time, or drop the flags for the bulk close.", 2)
    metadata, rc = _parse_metadata_flag(raw_meta)
    if rc:
        return rc
    fail_msg: dict[str, str] = {}
    with kbc.connect_closing() as conn:
        def op(tid):
            gate_err = _goal_gate_error(
                conn, tid, (summary or args.result or "").strip(), "completion",
                "Re-scope with kanban edit, or record the block with kanban block instead of completing.",
                "Provide evidence matching the task's acceptance criteria.")
            if gate_err:
                fail_msg[tid] = gate_err
                return False
            fail_msg[tid] = f"cannot complete {tid} (unknown id or terminal state)"
            try:
                done = kb.complete_task(conn, tid, result=args.result, summary=summary, metadata=metadata,
                                        expected_run_id=_worker_run_id_for(tid),
                                        force=bool(getattr(args, "force", False)))
            except kb.LiveClaimError:
                fail_msg[tid] = (f"cannot complete {tid}: a live worker is running it. Wait for the "
                                 f"worker, `hermes kanban reclaim {tid}` to release it, or re-run with "
                                 f"--force to close its run and complete anyway.")
                return False
            except kb.EmptyCompletionError as empty_err:
                fail_msg[tid] = (f"cannot complete {tid}: {empty_err}. Pass --result/--summary "
                                 f"describing what was done (an empty completion is not evidence).")
                return False
            except kb.VerificationRequiredError as e:
                # Gauntlet: the executor cannot close its own card; a verifier must.
                verify_hint = (
                    f"'hermes kanban verify {tid} --pass "
                    f"--regression-evidence '{{\"command\": \"...\", "
                    f"\"exit_code\": 0}}''"
                    if e.regression_required
                    else f"'hermes kanban verify {tid} --pass'"
                )
                fail_msg[tid] = (f"kanban: {e} Use 'hermes kanban request-review {tid}' to hand it "
                                 f"off, then {verify_hint} once it checks out.")
                return False
            if not done:
                # complete_task returns bare False for a dependency refusal too;
                # name the open parents instead of claiming the id is unknown.
                blockers = kb.unsatisfied_parents(conn, tid)
                if blockers:
                    detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                    fail_msg[tid] = (f"cannot complete {tid}: unsatisfied parent dependencies: {detail}; "
                                     f"complete the parents first, or `hermes kanban unlink <parent> {tid}`.")
            return done

        return _bulk_apply(ids, op, lambda tid: f"Completed {tid}", fail_msg.__getitem__)


def _cmd_edit(args: argparse.Namespace) -> int:
    result = getattr(args, "result", None)
    raw_metadata = getattr(args, "metadata", None)
    summary = getattr(args, "summary", None)
    title = getattr(args, "title", None)
    body = getattr(args, "body", None)
    priority = getattr(args, "priority", None)
    if result is None and (summary is not None or raw_metadata is not None):
        return _err("kanban edit: --summary and --metadata require --result", 2)
    if all(value is None for value in (title, body, priority, result)):
        return _err("kanban edit: provide --title, --body, --priority, or --result", 2)
    metadata, rc = _parse_metadata_flag(raw_metadata)
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        ok = kb.edit_task(
            conn, args.task_id, title=title, body=body, priority=priority,
            result=result, summary=summary, metadata=metadata,
        )
    return _ok_or_err(
        ok,
        f"cannot edit {args.task_id} (unknown id, or --result used on a task that is not done)",
        f"Edited {args.task_id}",
    )


def _commented(conn, reason: Optional[str], author, prefix: str, op):
    """Wrap a per-task ``op`` so a ``reason`` is first recorded as a ``PREFIX: reason`` comment."""
    def run(tid):
        if reason:
            kb.add_comment(conn, tid, author, f"{prefix}: {reason}")
        return op(tid)
    return run


def _cmd_block(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    kind = getattr(args, "kind", None)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def ok_msg(tid):
            # Report where it landed: dependency blocks -> todo, tripped unblock-loop breaker -> triage.
            landed = kb.get_task(conn, tid)
            where = landed.status if landed else "blocked"
            if where == "todo":
                return f"{tid} → todo (dependency wait){suffix}"
            if kind == "dependency" and where == "blocked":
                return f"Blocked {tid} as needs_input (no open parent to wait on){suffix}"
            if where == "triage":
                # Only a typed owner-input block carries a question for a human.
                verdict = ("needs a human decision" if (landed.block_kind if landed else kind) == "needs_input"
                           else "orchestration attention needed")
                return f"{tid} → triage (unblock loop detected — {verdict}){suffix}"
            return f"Blocked {tid}{suffix}"

        op = _commented(conn, reason, author, "BLOCKED", lambda tid: kb.block_task(
            conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id_for(tid)))
        return _bulk_apply(ids, op, ok_msg, lambda tid: f"cannot block {tid}")


def _cmd_schedule(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    ids = _bulk_ids(args)
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "SCHEDULED", lambda tid: kb.schedule_task(
            conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid)))
        return _bulk_apply(ids, op, lambda tid: f"Scheduled {tid}{suffix}", lambda tid: f"cannot schedule {tid}")


def _cmd_unblock(args: argparse.Namespace) -> int:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return _err("kanban unblock is orchestrator-only; workers must hand off their assigned task")
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = _stripped_or_none(getattr(args, "reason", None))
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        op = _commented(conn, reason, author, "UNBLOCK", lambda tid: kb.unblock_task(conn, tid))
        return _bulk_apply(ids, op, lambda tid: f"Unblocked {tid}{suffix}",
                           lambda tid: f"cannot unblock {tid} (not blocked/scheduled?)")


def _cmd_request_review(args: argparse.Namespace) -> int:
    tid = args.task_id
    summary = _stripped_or_none(getattr(args, "summary", None))
    metadata, rc = _parse_metadata_flag(getattr(args, "metadata", None))
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        gate_err = _goal_gate_error(
            conn, tid, summary or "", "review handoff",
            "Record the block with kanban block instead of requesting review.",
            "Provide acceptance evidence matching the task.")
        if gate_err:
            return _err(gate_err)
        evidence_gap = kb.review_handoff_evidence_gap(conn, tid, metadata)
        if evidence_gap is not None:
            return _err(f"cannot request review: {evidence_gap}")
        ok, reason = kb.request_review(
            conn, tid, summary=summary, metadata=metadata, reviewer=getattr(args, "reviewer", None),
            expected_run_id=_worker_run_id_for(tid), force=bool(getattr(args, "force", False)), with_reason=True)
        if not ok:
            return _err(f"cannot request review for {tid}: {reason or 'not running/ready?'}")
        persisted_run = kb.latest_run(conn, tid)
        display_summary = persisted_run.summary if persisted_run else None
        print(f"Requested review for {tid}" + (f": {display_summary}" if display_summary else ""))
    return 0


def _cmd_request_changes(args: argparse.Namespace) -> int:
    tid = args.task_id
    reason = " ".join(args.reason).strip()
    with kbc.connect_closing() as conn:
        ok, detail = kb.request_changes(conn, tid, reason=reason, expected_run_id=_worker_run_id_for(tid))
        if not ok:
            return _err(f"cannot request changes for {tid}: {detail or 'invalid review state'}")
        print(f"Requested changes for {tid}" + (f"; routed to {detail}" if detail else ""))
    return 0


def _cmd_reopen_review(args: argparse.Namespace) -> int:
    ids, rc = _require_ids(args)
    if rc:
        return rc
    reason = getattr(args, "reason", None)
    if reason is not None:
        reason = str(kb.redact_review_value(reason.strip())).strip() or None
    author = _profile_author() if reason else None
    suffix = f": {reason}" if reason else ""
    with kbc.connect_closing() as conn:
        def op(tid):
            if not kb.reopen_review_task(conn, tid):
                return False
            if reason:
                kb.add_comment(conn, tid, author or "operator", f"CHANGES REQUESTED: {reason}")
            return True

        return _bulk_apply(ids, op, lambda tid: f"Reopened {tid}{suffix}",
                           lambda tid: f"cannot reopen {tid} (not in review?)")


def _cmd_promote(args: argparse.Namespace) -> int:
    reason = _joined_words(args.reason)
    author = _profile_author()
    # Dedupe while preserving order; positional task_id always first.
    ids = list(dict.fromkeys(_bulk_ids(args)))
    dry_run = bool(args.dry_run)

    results: list[dict[str, object]] = []
    with kbc.connect_closing() as conn:
        for tid in ids:
            ok, err = kb.promote_task(conn, tid, actor=author, reason=reason, dry_run=dry_run)
            results.append({"task_id": tid, "promoted": ok, "dry_run": dry_run,
                            "reason": reason, "error": err})

    failed = [r for r in results if not r["promoted"]]
    if getattr(args, "json", False):
        # Single-id stays a flat object for back-compat; bulk emits a list.
        _print_json(results[0] if len(results) == 1 else results)
        return 0 if not failed else 1

    tag = " (dry)" if dry_run else ""
    label = "Would promote" if dry_run else "Promoted"
    suffix = f": {reason}" if reason else ""
    for r in results:
        if r["promoted"]:
            print(f"{label} {r['task_id']} -> ready{tag}{suffix}")
        else:
            print(f"cannot promote {r['task_id']}: {r['error']}", file=sys.stderr)
    return 0 if not failed else 1


def _cmd_archive(args: argparse.Namespace) -> int:
    ids = list(args.task_ids or [])
    purge_ids = list(getattr(args, "purge_ids", None) or [])
    if ids and purge_ids:
        return _err("choose either task_ids to archive or --rm archived task_ids")
    if not ids and not purge_ids:
        return _err("at least one task_id is required")
    with kbc.connect_closing() as conn:
        if purge_ids:
            return _bulk_apply(purge_ids, lambda tid: kb.delete_archived_task(conn, tid), lambda tid: f"Deleted {tid}",
                               lambda tid: f"cannot delete {tid} (must already be archived, with no "
                                           f"unresolved execution effects; see its events)")
        return _bulk_apply(ids, lambda tid: kb.archive_task(conn, tid),
                           lambda tid: f"Archived {tid}", lambda tid: f"cannot archive {tid}")


def _cmd_stats(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        stats = kb.board_stats(conn)
    if _json_out(args, stats):
        return 0
    print("By status:")
    for k in kanban_workflow.DEFAULT_WORKFLOW.keys():
        print(f"  {k:8s}  {stats['by_status'].get(k, 0)}")
    if stats["by_assignee"]:
        print("\nBy assignee:")
        for who, counts in sorted(stats["by_assignee"].items()):
            print(f"  {who:20s}  {_fmt_counts(counts)}")
    age = stats["oldest_ready_age_seconds"]
    if age is not None:
        print(f"\nOldest ready task age: {int(age)}s")
    return 0


def _cmd_notify_subscribe(args: argparse.Namespace) -> int:
    delivery_metadata = {
        key: value
        for key, value in (
            ("parent_chat_id", getattr(args, "parent_chat_id", None)),
            ("guild_id", getattr(args, "guild_id", None)),
        )
        if value
    }
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            return _err(f"no such task: {args.task_id}")
        kbn.add_notify_sub(
            conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
            chat_type=args.chat_type, thread_id=args.thread_id, user_id=args.user_id,
            user_id_alt=getattr(args, "user_id_alt", None),
            notifier_profile=args.notifier_profile or _profile_author(),
            delivery_mode=getattr(args, "delivery_mode", None),
            delivery_metadata=delivery_metadata or None,
        )
    print(f"Subscribed {args.platform}:{args.chat_id}" + (f":{args.thread_id}" if args.thread_id else "")
          + f" to {args.task_id}")
    return 0


def _cmd_notify_list(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        subs = kbn.list_notify_subs(conn, args.task_id)
    if _json_out(args, subs):
        return 0
    if not subs:
        print("(no subscriptions)")
        return 0
    for s in subs:
        thr = f":{s['thread_id']}" if s.get("thread_id") else ""
        dmode, ctype = s.get("delivery_mode") or "notify", s.get("chat_type") or "dm"
        extras = "".join((
            f"  owner={s['notifier_profile']}" if s.get("notifier_profile") else "",
            "" if ctype == "dm" else f"  chat_type={ctype}",
            f"  user_id_alt={s['user_id_alt']}" if s.get("user_id_alt") else "",
            "" if dmode == "notify" else f"  mode={dmode}",
        ))
        print(f"  {s['task_id']:10s}  {s['platform']}:{s['chat_id']}{thr}  (since event {s['last_event_id']}){extras}")
    return 0


def _cmd_notify_unsubscribe(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kbn.remove_notify_sub(conn, task_id=args.task_id, platform=args.platform, chat_id=args.chat_id,
                                  thread_id=args.thread_id)
    return _ok_or_err(ok, "(no such subscription)", f"Unsubscribed from {args.task_id}")


def _cmd_log(args: argparse.Namespace) -> int:
    content = kb.read_worker_log(args.task_id, tail_bytes=args.tail)
    if content is None:
        return _err(f"(no log for {args.task_id} — task may not have spawned yet)")
    sys.stdout.write(content)
    if not content.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show attempt history for a task."""
    rsk, rc = _run_state_kwargs(args, "runs")
    if rc:
        return rc
    with kbc.connect_closing() as conn:
        runs = kb.list_runs(conn, args.task_id, **rsk)
    if _json_out(args, [_obj_dict(r, _RUNS_RUN_FIELDS) for r in runs]):
        return 0
    if not runs:
        print(f"(no runs yet for {args.task_id})")
        return 0
    print(f"{'#':3s}  {'OUTCOME':12s}  {'PROFILE':16s}  {'ELAPSED':>8s}  STARTED")
    for i, r in enumerate(runs, 1):
        end = r.ended_at or int(time.time())
        # Clamp to 0 so NTP backward-jumps don't print negative durations.
        elapsed = max(0, end - r.started_at)
        el = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m" if elapsed < 3600 else f"{elapsed / 3600:.1f}h"
        outcome = r.outcome or ("(running)" if not r.ended_at else r.status)
        print(f"{i:3d}  {outcome:12s}  {(r.profile or '-'):16s}  {el:>8s}  {_fmt_ts(r.started_at)}")
        if r.summary:
            print(f"     → {r.summary.splitlines()[0][:100]}")
        if r.error:
            print(f"     ✖ {r.error[:100]}")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        text = kb.build_worker_context(conn, args.task_id)
    print(text)
    return 0


def _run_triage_sweep(args: argparse.Namespace, verb: str, mod, run_one, json_key: str,
                      json_fields: tuple[str, ...], human_ok) -> int:
    """Shared driver for ``specify`` / ``decompose``: validate ids (one task id XOR ``--all``), run
    ``run_one(tid, author=...)`` per id, print JSON or human lines, exit code."""
    all_flag = bool(getattr(args, "all_triage", False))
    author = getattr(args, "author", None) or _profile_author()
    want_json = bool(getattr(args, "json", False))
    tenant = getattr(args, "tenant", None)
    if args.task_id and all_flag:
        return _err("kanban: pass either a task id OR --all, not both", 2)
    if all_flag:
        ids = mod.list_triage_ids(tenant=tenant)
        if not ids:
            if want_json:
                print(json.dumps({json_key: 0, "total": 0}))
            else:
                print("No triage tasks" + (f" for tenant {tenant!r}" if tenant else "") + ".")
            return 0
    elif args.task_id:
        ids = [args.task_id]
    else:
        return _err(f"kanban: {verb} requires a task id or --all", 2)

    ok_count = 0
    for tid in ids:
        outcome = run_one(tid, author=author)
        if outcome.ok:
            ok_count += 1
        if want_json:
            print(json.dumps(_obj_dict(outcome, json_fields)))
        elif outcome.ok:
            print(human_ok(outcome))
        else:
            print(f"kanban: {verb} {outcome.task_id}: {outcome.reason}", file=sys.stderr)
    if not all_flag:
        return 0 if ok_count == 1 else 1
    # --all: exit 1 only when every candidate failed (honest signal for scripts).
    return 0 if (ok_count > 0 or not ids) else 1


def _retitled_suffix(outcome) -> str:
    return f" — retitled: {outcome.new_title!r}" if outcome.new_title else ""


def _cmd_specify(args: argparse.Namespace) -> int:
    """Spec a triage task (or all) via the auxiliary LLM, promote to todo."""
    from hermes_cli import kanban_specify as spec

    return _run_triage_sweep(args, "specify", spec, spec.specify_task, "specified",
                             ("task_id", "ok", "reason", "new_title"),
                             lambda o: f"Specified {o.task_id} → todo{_retitled_suffix(o)}")


def _decompose_ok_line(o) -> str:
    if o.fanout and o.child_ids:
        return (f"Decomposed {o.task_id} → {len(o.child_ids)} "
                f"children ({', '.join(o.child_ids)}); root promoted to todo")
    return f"Specified {o.task_id} → todo (no fanout){_retitled_suffix(o)}"


def _cmd_decompose(args: argparse.Namespace) -> int:
    """Fan a triage task (or all) out into child tasks via the auxiliary LLM."""
    from hermes_cli import kanban_decompose as decomp

    return _run_triage_sweep(args, "decompose", decomp, decomp.decompose_task, "decomposed",
                             ("task_id", "ok", "reason", "fanout", "child_ids", "new_title"), _decompose_ok_line)


# --- NorCal Gauntlet / recovery / execution-supervisor / lessons verbs ---
# (fork handlers; kanban_db symbols they use are fork-side: see G2 report)

def _cmd_attempt_budget(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, args.task_id) is None:
            print(f"no such task: {args.task_id}", file=sys.stderr)
            return 1
        granted = None
        if args.grant is not None:
            try:
                granted = kb.grant_objective_attempts(
                    conn, args.task_id, added_attempts=args.grant,
                    authorized_by=args.authorized_by or "", reason=args.reason or "",
                )
            except kb.ObjectiveAttemptGrantRefused as exc:
                print(f"attempt-budget grant refused: {exc}", file=sys.stderr)
                return 2
        root = kb._objective_lineage_root(conn, args.task_id)
        report = {
            "objective": root,
            "attempts": kb.gauntlet_objective_attempts(conn, root),
            "base_limit": kb.gauntlet_objective_attempt_limit(),
            "effective_limit": kb.effective_objective_attempt_limit(conn, root),
            "mode": kb.objective_attempt_budget_mode(),
            "enforced": kb.objective_attempt_budget_enforced(),
            "grants": kb.objective_attempt_grants(conn, root),
            "granted_now": granted,
            "failure_basis": kb._safe_objective_failure_basis(conn, root),
        }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"objective {root}: {report['attempts']} attempts, effective limit "
              f"{report['effective_limit']} (base {report['base_limit']} + "
              f"{len(report['grants'])} grant(s)); mode {report['mode']}")
        print(kb._describe_failure_basis(report["failure_basis"]))
        if granted:
            print(f"granted +{granted['added_attempts']} by {granted['authorized_by']}: "
                  f"{granted['prior_effective_limit']} -> {granted['new_effective_limit']}")
    return 0




def _cmd_approve(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.resolve_task_approval(
            conn, args.task_id, decision="approve",
            actor=args.actor, source=args.source,
        )
    if not ok:
        print(
            f"cannot approve {args.task_id} (not awaiting approval or already resolved)",
            file=sys.stderr,
        )
        return 1
    print(f"Approved {args.task_id}; task resumed")
    return 0


def _cmd_deny(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok = kb.resolve_task_approval(
            conn, args.task_id, decision="deny",
            actor=args.actor, source=args.source,
        )
    if not ok:
        print(
            f"cannot deny {args.task_id} (not awaiting approval or already resolved)",
            file=sys.stderr,
        )
        return 1
    print(f"Denied {args.task_id}; proposed action remains blocked")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    tid = args.task_id
    passed = bool(args.passed)
    reason = (getattr(args, "reason", None) or "").strip() or None
    raw_evidence = getattr(args, "evidence", None)
    evidence = None
    if raw_evidence:
        try:
            evidence = json.loads(raw_evidence)
            if not isinstance(evidence, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --evidence: {exc}", file=sys.stderr)
            return 2
    raw_regression = getattr(args, "regression_evidence", None)
    regression_evidence = None
    if raw_regression:
        try:
            regression_evidence = json.loads(raw_regression)
            if not isinstance(regression_evidence, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --regression-evidence: {exc}", file=sys.stderr)
            return 2
    if not passed and not reason:
        print("kanban: --fail requires --reason", file=sys.stderr)
        return 2
    with kbc.connect_closing() as conn:
        ok, detail = kb.record_verification(
            conn,
            tid,
            passed=passed,
            # Fall back to the acting profile: a verdict with no attributable
            # verifier is a weaker audit record than the ledger can carry.
            verifier=getattr(args, "verifier", None) or _profile_author(),
            evidence=evidence,
            reason=reason,
            expected_run_id=_worker_run_id_for(tid),
            route_on_failure=not bool(getattr(args, "no_route", False)),
            regression_evidence=regression_evidence,
        )
        if not ok:
            print(f"cannot verify {tid}: {detail}", file=sys.stderr)
            return 1
        if passed:
            print(f"Verified {tid} — it can now be completed")
        elif detail == "rework":
            task = kb.get_task(conn, tid)
            print(
                f"Verification of {tid} FAILED; routed back for repair"
                + (f" (now {task.status})" if task else "")
            )
        else:
            print(
                f"Verification of {tid} FAILED; it stays in review and "
                f"cannot be completed"
            )
    return 0


def _cmd_gauntlet(args: argparse.Namespace) -> int:
    enabled = bool(args.enabled)
    failed: list[str] = []
    with kbc.connect_closing() as conn:
        for tid in args.task_ids:
            if kb.set_gauntlet_enforced(conn, tid, enabled):
                print(
                    f"Gauntlet enforcement {'ON' if enabled else 'OFF'} for {tid}"
                )
            else:
                failed.append(tid)
                print(f"cannot set gauntlet enforcement for {tid} (unknown id)",
                      file=sys.stderr)
    return 0 if not failed else 1


def _cmd_loop_repair(args: argparse.Namespace) -> int:
    author = getattr(args, "author", None) or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            kb.record_block_loop_repair(
                conn, args.task_id,
                root_cause=args.root_cause, change=args.change,
                check=args.check, author=author,
            )
    except ValueError as exc:
        print(f"kanban: loop-repair refused: {exc}", file=sys.stderr)
        return 1
    print(
        f"Loop repair recorded for {args.task_id}; Gauntlet ON. "
        f"It can now leave triage (hermes kanban specify {args.task_id})."
    )
    return 0


def _cmd_repair_record(args: argparse.Namespace) -> int:
    author = getattr(args, "author", None) or _profile_author()
    try:
        with kbc.connect_closing() as conn:
            repair_id = kb.record_auto_repair(
                conn, args.task_id,
                root_cause=args.root_cause, change=args.change,
                check=args.check, author=author,
            )
    except ValueError as exc:
        print(f"kanban: repair-record refused: {exc}", file=sys.stderr)
        return 1
    print(f"Repair recorded on {repair_id}.")
    return 0


def _cmd_repair_gate(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        ok, reason = kb.auto_repair_gate(conn, args.subject_id)
    print(("PASS: " if ok else "FAIL: ") + reason)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Execution supervisor
# ---------------------------------------------------------------------------


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _dispatch_exec(args: argparse.Namespace) -> int:
    sub = getattr(args, "exec_action", None)
    if not sub:
        print(
            "usage: hermes kanban exec <list|show|reconcile|terminate>",
            file=sys.stderr,
        )
        return 2
    if sub in ("list", "ls"):
        return _cmd_exec_list(args)
    if sub == "show":
        return _cmd_exec_show(args)
    if sub == "reconcile":
        return _cmd_exec_reconcile(args)
    if sub == "terminate":
        return _cmd_exec_terminate(args)
    print(f"kanban exec: unknown action {sub!r}", file=sys.stderr)
    return 2


def _cmd_exec_list(args: argparse.Namespace) -> int:
    from hermes_cli import exec_supervisor as ex

    with kbc.connect_closing() as conn:
        records = ex.list_executions(
            conn,
            status=getattr(args, "status", None),
            active_only=bool(getattr(args, "active", False)),
            task_id=getattr(args, "task_id", None),
            limit=getattr(args, "limit", 25) or 25,
        )
        rows = [ex.describe(conn, record) for record in records]

    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No executions match.")
        return 0

    # The header is the operator's actual question list, in order: what is it,
    # who owns it, whose card, how long, when did it last show progress, is the
    # controller alive, is it inside policy, how did it end.
    print(
        f"{'ID':<20} {'EXECUTOR':<10} {'OWNER':<11} {'TASK':<14} "
        f"{'RUNTIME':>8} {'PROGRESS':>9} {'CTRL':<6} {'POL':<4} STATUS"
    )
    for row in rows:
        since = row.get("seconds_since_heartbeat")
        ctrl = "-" if row["terminal"] else ("alive" if row["controller_alive"] else "DEAD")
        pol = "ok" if row["within_policy"] else "OUT"
        status = row["status"]
        if row.get("exit_code") is not None:
            status += f" (rc={row['exit_code']})"
        print(
            f"{row['id']:<20} {row['executor_type']:<10} {row['ownership']:<11} "
            f"{(row['task_id'] or '-'):<14} "
            f"{_fmt_duration(row['runtime_seconds']):>8} "
            f"{_fmt_duration(since):>9} {ctrl:<6} {pol:<4} {status}"
        )
    return 0


def _cmd_exec_show(args: argparse.Namespace) -> int:
    from hermes_cli import exec_supervisor as ex

    with kbc.connect_closing() as conn:
        record = ex.get_execution(conn, args.execution_id)
        if record is None:
            print(
                f"kanban exec: no execution {args.execution_id!r} on this board",
                file=sys.stderr,
            )
            return 1
        data = ex.describe(conn, record)
        data["events"] = ex.execution_events(conn, record.id)

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0

    print(f"Execution {data['id']}")
    print(f"  status            {data['status']}"
          + (f" (rc={data['exit_code']})" if data.get("exit_code") is not None else ""))
    if data.get("termination_reason"):
        print(f"  termination       {data['termination_reason']}")
    print(f"  executor          {data['executor_type']} / {data['command_class']}")
    print(f"  task              {data['task_id'] or '-'}")
    print(f"  owner             {data['ownership']}")
    print(f"  cwd               {data['cwd']}")
    print(f"  pid / pgid        {data['pid'] or '-'} / {data['pgid'] or '-'}")
    print(f"  controller pid    {data['controller_pid'] or '-'} "
          f"({'alive' if data['controller_alive'] else 'not alive'})")
    print(f"  executor alive    {'yes' if data['executor_alive'] else 'no'}")
    print(f"  runtime           {_fmt_duration(data['runtime_seconds'])}"
          f" (cap {_fmt_duration(data['max_runtime_s'])})")
    print(f"  last progress     {_fmt_duration(data['seconds_since_heartbeat'])} ago")
    print(f"  within policy     {'yes' if data['within_policy'] else 'NO: ' + ', '.join(data['policy_notes'])}")
    print(f"  rollback ref      {data['rollback_ref'] or '-'}")
    print(f"  started           {_fmt_ts(data['started_at'])}")
    print(f"  ended             {_fmt_ts(data['ended_at']) if data['ended_at'] else '-'}")
    if data["events"]:
        print("  audit trail:")
        for event in data["events"]:
            payload = json.dumps(event["payload"]) if event["payload"] else ""
            print(f"    {_fmt_ts(event['created_at'])}  {event['kind']:<10} {payload}")
    return 0


def _cmd_exec_reconcile(args: argparse.Namespace) -> int:
    from hermes_cli import exec_supervisor as ex

    with kbc.connect_closing() as conn:
        result = ex.reconcile(conn)
    data = result.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    print(f"Checked {data['checked']} non-terminal execution(s).")
    for key in (
        "completed", "failed", "timed_out", "controller_lost",
        "stale", "terminated", "recovered", "adopted", "untouched",
    ):
        ids = data.get(key) or []
        if ids:
            print(f"  {key:<16} {len(ids):>3}  {', '.join(ids[:10])}"
                  + (" ..." if len(ids) > 10 else ""))
    return 0


def _cmd_exec_terminate(args: argparse.Namespace) -> int:
    from hermes_cli import exec_supervisor as ex

    with kbc.connect_closing() as conn:
        try:
            record = ex.terminate_execution(
                conn, args.execution_id, reason=args.reason,
            )
        except ex.ExecutionNotFound as exc:
            print(f"kanban exec: {exc}", file=sys.stderr)
            return 1
        data = ex.describe(conn, record)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0
    print(f"{record.id}: {record.status}"
          + (f" ({record.termination_reason})" if record.termination_reason else ""))
    # A record still owned by the supervisor means the process would not die.
    # Say so rather than reporting a clean kill: this module exists precisely
    # because "we sent a signal" was once mistaken for "the process ended".
    if not record.is_terminal:
        print(
            "  NOTE: the process could not be confirmed dead; ownership was "
            "transferred to the supervisor and it will be reconciled again.",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_lesson_promote(args: argparse.Namespace) -> int:
    raw_evidence = getattr(args, "evidence", None)
    evidence = None
    if raw_evidence:
        try:
            evidence = json.loads(raw_evidence)
            if not isinstance(evidence, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"kanban: --evidence: {exc}", file=sys.stderr)
            return 2
    with kbc.connect_closing() as conn:
        try:
            lesson = kb.promote_lesson(
                conn,
                args.task_id,
                lesson=args.lesson,
                applicability=args.applicability,
                actor=getattr(args, "actor", None) or _profile_author(),
                evidence=evidence,
                allow_global=bool(getattr(args, "allow_global", False)),
            )
        except kb.LessonPromotionError as exc:
            print(f"cannot promote a lesson from {args.task_id}: {exc}",
                  file=sys.stderr)
            return 1
    scope = (
        "global (every tenant)"
        if lesson["scope"] == kb.LESSON_SCOPE_GLOBAL
        else f"tenant {lesson['tenant']}"
    )
    print(
        f"Promoted lesson {lesson['id']} from verified task "
        f"{lesson['source_task_id']} — {scope}, binding on tasks matching "
        f"'{lesson['applicability']}'"
    )
    return 0


def _cmd_lesson_record_error(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        row = conn.execute(
            "SELECT tenant FROM tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if row is None:
            print(f"kanban: task {args.task_id} not found", file=sys.stderr)
            return 1
        try:
            lesson = kb.record_error_lesson(
                conn,
                error_text=args.error,
                lesson=args.lesson,
                source_task_id=args.task_id,
                created_by=getattr(args, "actor", None) or _profile_author(),
                tenant=row["tenant"],
            )
        except ValueError as exc:
            print(f"kanban: cannot record error lesson: {exc}", file=sys.stderr)
            return 2
    print(
        f"Recorded candidate lesson {lesson['id']} from {args.task_id}. "
        "It is retrievable by error signature but binds nothing until approved."
    )
    return 0


def _cmd_lesson_retire(args: argparse.Namespace) -> int:
    lesson_id = int(args.lesson_id)
    with kbc.connect_closing() as conn:
        # Read the state first so the confirmation says what was actually
        # decided: declining a candidate that never bound anything is not the
        # same report as un-binding an active rule.
        was_candidate = lesson_id in {
            c["id"] for c in kb.lesson_candidates(conn)
        }
        ok, detail = kb.retire_lesson(
            conn,
            lesson_id,
            actor=getattr(args, "actor", None) or _profile_author(),
            reason=getattr(args, "reason", None),
        )
    if not ok:
        print(f"cannot retire lesson {lesson_id}: {detail}", file=sys.stderr)
        return 1
    if was_candidate:
        print(
            f"Retired candidate lesson {lesson_id} — it leaves the decision "
            f"queue and never bound anything; the row and its history are kept"
        )
        return 0
    print(
        f"Retired lesson {lesson_id} — it stops being injected; the row "
        f"and its history are kept"
    )
    return 0


def _cmd_lesson_approve(args: argparse.Namespace) -> int:
    lesson_id = int(args.lesson_id)
    with kbc.connect_closing() as conn:
        # Read the candidate first, so the confirmation can state exactly what
        # was made binding and so an unknown id is distinguishable from one
        # that was already decided.
        pending = {c["id"]: c for c in kb.lesson_candidates(conn)}
        lesson = pending.get(lesson_id)
        try:
            ok = kb.approve_lesson(conn, lesson_id, approver=args.approver)
        except kb.LessonPromotionError as exc:
            print(f"cannot approve lesson {lesson_id}: {exc}", file=sys.stderr)
            return 2
    if not ok:
        print(
            f"cannot approve lesson {lesson_id}: it is not a candidate "
            f"awaiting a decision (already active, retired, or no such lesson)",
            file=sys.stderr,
        )
        return 1
    if lesson is None:
        print(f"Approved lesson {lesson_id} — approver {args.approver}")
        return 0
    scope = (
        "global (every tenant)"
        if lesson["scope"] == kb.LESSON_SCOPE_GLOBAL
        else f"tenant {lesson['tenant']}"
    )
    print(
        f"Approved lesson {lesson_id} — approver {args.approver}. Now "
        f"binding on tasks matching '{lesson['applicability']}' — {scope}"
    )
    return 0


def _cmd_lesson_candidates(args: argparse.Namespace) -> int:
    with kbc.connect_closing() as conn:
        pending = kb.lesson_candidates(
            conn, tenant=getattr(args, "tenant", None)
        )
    if getattr(args, "as_json", False):
        print(json.dumps(pending, indent=2, ensure_ascii=False))
        return 0
    if not pending:
        print("No candidate lessons awaiting a decision")
        return 0
    print(
        f"{len(pending)} candidate lesson(s) awaiting a decision — a "
        f"candidate binds nothing until it is approved:"
    )
    for lesson in pending:
        scope = (
            "global" if lesson["scope"] == kb.LESSON_SCOPE_GLOBAL
            else f"tenant:{lesson['tenant']}"
        )
        print(
            f"#{lesson['id']:<4} [candidate] {scope}  "
            f"applies-to={lesson['applicability']}  "
            f"from={lesson['source_task_id']}"
        )
        for line in lesson["lesson"].splitlines():
            print(f"      {line}")
        if lesson.get("retire_condition"):
            print(f"      (retire when: {lesson['retire_condition']})")
    print(
        "\nApprove one with:  hermes kanban lesson-approve <id> "
        "--approver <name>"
    )
    return 0


def _cmd_lessons(args: argparse.Namespace) -> int:
    error_text = getattr(args, "error", None)
    if error_text:
        with kbc.connect_closing() as conn:
            matches = kb.lessons_for_error(
                conn, error_text, tenant=getattr(args, "tenant", None),
            )
        if getattr(args, "as_json", False):
            print(json.dumps(matches, indent=2, ensure_ascii=False))
            return 0
        if not matches:
            print(
                "No recorded lesson matches that error.\n"
                "That makes it a NEW failure: fix it, then record it so the "
                "next agent finds it instead of rediscovering it."
            )
            return 0
        print(f"{len(matches)} lesson(s) match that error, best first:")
        for lesson in matches:
            print(
                f"#{lesson['id']:<4} score={lesson['match_score']} "
                f"matched on: {', '.join(lesson['match_terms'][:6])}"
            )
            for line in lesson["lesson"].splitlines():
                print(f"      {line}")
            print(f"      _source_: `{lesson['source_task_id']}`")
        return 0

    task_id = getattr(args, "task_id", None)
    with kbc.connect_closing() as conn:
        if task_id:
            if kb.get_task(conn, task_id) is None:
                print(f"kanban: task {task_id} not found", file=sys.stderr)
                return 1
            lessons = kb.lessons_for_task(conn, task_id)
        else:
            lessons = kb.list_lessons(
                conn,
                active_only=not bool(getattr(args, "include_retired", False)),
                tenant=getattr(args, "tenant", None),
                source_task_id=getattr(args, "source", None),
            )
    if getattr(args, "as_json", False):
        print(json.dumps(lessons, indent=2, ensure_ascii=False))
        return 0
    if not lessons:
        print(
            f"No lessons binding {task_id}" if task_id
            else "No promoted lessons"
        )
        return 0
    if task_id:
        print(f"Lessons injected into {task_id} at dispatch:")
    for lesson in lessons:
        scope = (
            "global" if lesson["scope"] == kb.LESSON_SCOPE_GLOBAL
            else f"tenant:{lesson['tenant']}"
        )
        state = "active" if lesson["active"] else "RETIRED"
        print(
            f"#{lesson['id']:<4} [{state}] {scope}  "
            f"applies-to={lesson['applicability']}  "
            f"from={lesson['source_task_id']}"
        )
        for line in lesson["lesson"].splitlines():
            print(f"      {line}")
        if not lesson["active"] and lesson["retired_reason"]:
            print(f"      (retired: {lesson['retired_reason']})")
    return 0


def _cmd_reconcile_overtaken(args: argparse.Namespace) -> int:
    """Record the OVERTAKEN_BY_EVENTS terminal disposition on one card."""
    actor = _profile_author()
    dry_run = bool(getattr(args, "dry_run", False))
    refs = list(getattr(args, "superseded_by", None) or [])
    with kbc.connect_closing() as conn:
        ok, err = kb.reconcile_overtaken_by_events(
            conn,
            args.task_id,
            actor=actor,
            reason=args.reason,
            evidence=args.evidence,
            superseded_by=refs,
            dry_run=dry_run,
        )
        task = kb.get_task(conn, args.task_id) if ok and not dry_run else None

    if getattr(args, "json", False):
        payload: dict[str, Any] = {
            "task_id": args.task_id,
            "reconciled": ok,
            "dry_run": dry_run,
            "actor": actor,
            "reason": args.reason,
            "evidence": args.evidence,
            "superseded_by": refs,
            # Stated outright rather than left to be inferred from the absence
            # of a result: this card's steps were not performed.
            "executed": False,
            "error": err,
        }
        if task is not None:
            payload["status"] = task.status
            payload["terminal_disposition"] = task.terminal_disposition
            payload["disposition_at"] = task.disposition_at
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if ok else 1

    if not ok:
        print(
            f"cannot reconcile {args.task_id}: {err}", file=sys.stderr,
        )
        return 1
    if dry_run:
        print(
            f"Would reconcile {args.task_id} -> archived "
            f"[{kb.DISPOSITION_OVERTAKEN_BY_EVENTS}] (dry)"
        )
        return 0
    print(
        f"Reconciled {args.task_id} -> archived "
        f"[{kb.DISPOSITION_OVERTAKEN_BY_EVENTS}]; not executed, history preserved"
    )
    return 0


_HANDLERS = {
    "init": _cmd_init, "create": _cmd_create, "swarm": _cmd_swarm,
    "list": _cmd_list, "ls": _cmd_list, "show": _cmd_show,
    "assign": _cmd_assign, "set-model": _cmd_set_model,
    "reclaim": _cmd_reclaim, "reassign": _cmd_reassign,
    "diagnostics": _cmd_diagnostics, "diag": _cmd_diagnostics,
    "link": _cmd_link, "unlink": _cmd_unlink, "claim": _cmd_claim,
    "comment": _cmd_comment, "attach": _cmd_attach,
    "attachments": _cmd_attachments, "attach-rm": _cmd_attach_rm,
    "complete": _cmd_complete, "edit": _cmd_edit, "block": _cmd_block,
    "schedule": _cmd_schedule, "unblock": _cmd_unblock,
    "attempt-budget": _cmd_attempt_budget, "approve": _cmd_approve, "deny": _cmd_deny,
    "request-review": _cmd_request_review, "request-changes": _cmd_request_changes,
    "reopen-review": _cmd_reopen_review,
    "verify": _cmd_verify, "gauntlet": _cmd_gauntlet, "loop-repair": _cmd_loop_repair,
    "repair-record": _cmd_repair_record, "repair-gate": _cmd_repair_gate, "exec": _dispatch_exec,
    "lesson-promote": _cmd_lesson_promote, "lesson-record-error": _cmd_lesson_record_error,
    "lesson-retire": _cmd_lesson_retire, "lesson-approve": _cmd_lesson_approve,
    "lesson-candidates": _cmd_lesson_candidates, "lessons": _cmd_lessons,
    "promote": _cmd_promote,
    "archive": _cmd_archive, "reconcile-overtaken": _cmd_reconcile_overtaken,
    "tail": _cmd_tail, "dispatch": _cmd_dispatch,
    "daemon": _cmd_daemon, "watch": _cmd_watch, "stats": _cmd_stats,
    "log": _cmd_log, "runs": _cmd_runs, "heartbeat": _cmd_heartbeat,
    "assignees": _cmd_assignees, "notify-subscribe": _cmd_notify_subscribe,
    "notify-list": _cmd_notify_list, "notify-unsubscribe": _cmd_notify_unsubscribe,
    "context": _cmd_context, "specify": _cmd_specify, "decompose": _cmd_decompose,
    "gc": _cmd_gc,
}


# --- Slash-command entry point (used by /kanban from CLI and gateway) ---

_SLASH_KANBAN_HELP = """\
**/kanban** — manage the shared task board.

Common subcommands:
  `list` (alias `ls`)   List tasks on the current board
  `show <id>`           Task details + comments + events
  `stats`               Per-status / per-assignee counts
  `create <title>…`     Create a task (auto-subscribes you to events)
  `comment <id> <msg>`  Append a comment
  `attach <id> <path>`  Attach a local file; `attachments <id>` to list
  `complete <id>…`      Mark task(s) done
  `request-review <id>` Enter first-class review; `request-changes <id> <reason>` returns an active review to its implementer
  `block <id> [reason]` Mark blocked; `schedule <id> [reason]` parks time-delay work; `unblock <id>` to revive
  `assign <id> <profile>`  Reassign
  `boards list`         Show all boards
  `assignees`           Known profiles + counts
  `context <id>`        Full worker-context dump
  `runs <id>`           Attempt history
  `log <id>`            Worker log

Run `/kanban <subcommand> -h` for arguments. \
Read-only commands are safe while an agent is running.\
"""


def run_slash(rest: str) -> str:
    """Execute a ``/kanban …`` string (``rest`` = everything after ``/kanban``) and return captured
    stdout/stderr. Shared by the interactive CLI and the gateway so formatting is identical."""
    import io

    # Non-posix split (Windows) keeps backslashes as path separators but
    # leaves quote characters in the tokens — strip a fully wrapping pair
    # so `"my task"` reaches argparse as `my task`, not `"my task"`.
    tokens = []
    if rest and rest.strip():
        for tok in shlex.split(rest, posix=os.name == "posix"):
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            tokens.append(tok)

    # Bare ``/kanban`` or ``/kanban help`` / ``--help`` / ``-h`` / ``?``:
    # show the curated short-help block instead of dumping argparse's full
    # usage tree (which is enormous and reads as garbage in a chat
    # bubble).  Per-subcommand help still works via ``/kanban foo -h``.
    if not tokens or tokens[0] in {"help", "--help", "-h", "?"}:
        return _SLASH_KANBAN_HELP
    # build_parser() needs a subparsers action to attach to: build a throwaway one and drive
    # kanban_parser directly so usage/error text reads ``/kanban``.
    _wrap = argparse.ArgumentParser(prog="/kanban-wrap", add_help=False)
    _wrap.exit_on_error = False  # type: ignore[attr-defined]
    kanban_parser = build_parser(_wrap.add_subparsers(dest="_top"))
    kanban_parser.prog = "/kanban"
    kanban_parser.exit_on_error = False  # type: ignore[attr-defined]
    subparsers = [a for a in kanban_parser._actions if isinstance(a, argparse._SubParsersAction)]
    for _action in subparsers:
        for _name, _choice in _action.choices.items():
            _choice.prog = f"/kanban {_name}"
            _choice.exit_on_error = False  # type: ignore[attr-defined]

    def _usage_for_error() -> str:
        if tokens:
            for _action in subparsers:
                subparser = _action.choices.get(tokens[0])
                if subparser is not None:
                    return subparser.format_usage().rstrip()
        return kanban_parser.format_usage().rstrip()

    buf_out, buf_err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            args = kanban_parser.parse_args(tokens)
    except SystemExit as exc:
        out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
        if exc.code in {0, None} and out:  # ``-h`` help dump
            return out
        body = err or out
        return f"⚠ /kanban usage error\n{body}" if body else "⚠ /kanban usage error"
    except argparse.ArgumentError as exc:
        return f"⚠ /kanban usage error\n{_usage_for_error()}\n{exc}"

    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            kanban_command(args)
        except SystemExit:
            pass
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)

    out, err = buf_out.getvalue().rstrip(), buf_err.getvalue().rstrip()
    if err and out:
        return f"{out}\n{err}"
    return err if err else (out or "(no output)")
