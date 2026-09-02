"""Claude-first recovery lane for explicitly marked Kanban recovery tasks.

This module is the mechanical bypass for a task created with
``executor_lane=EXECUTOR_LANE_CLAUDE_RECOVERY`` (see ``hermes_cli.kanban_db``).
It is invoked from ``cli.py`` BEFORE the normal Hermes worker path builds an
agent, a system prompt, or a tool-calling loop — nothing here calls a Hermes
model or a Hermes tool (``kanban_show``, ``read_file``, ``terminal``, ...).

Sequence, each step plain deterministic Python:

1. Invoke Claude Code exactly once, bounded by a timeout ("one bounded
   repair attempt").
2. Mechanically run the task's ``recovery_gate_cmd`` and check its exit
   code — never trust Claude's own report of success.
3. Gate green -> complete the task directly via ``kanban_db.complete_task``
   (which promotes dependents via its own ``recompute_ready`` call). Done.
4. Gate still red -> invoke Codex exactly once with the original failure
   plus Claude's evidence, bounded by the same timeout.
5. Re-run the same gate command mechanically.
6. Gate green -> complete the task, noting both attempts.
7. Gate still red -> block the task once (kind="needs_input") with both
   attempts' evidence recorded as a comment. One escalation, no retry loop.

Steps 3 and 6 assume the card allows an executor to close itself. When the
card is Gauntlet-enforced (``tasks.gauntlet_enforced``, or the board-wide
``kanban.gauntlet_enforcement`` config), it does not: this lane is an
executor, so a green gate hands the card to the review lane as
VERIFICATION_PENDING with the gate evidence attached instead of completing
it. See ``_handoff_for_verification``.

Every external process in that sequence — both agentic attempts and the gate
command — is launched through ``hermes_cli.exec_supervisor`` rather than a
bare ``subprocess.run(timeout=...)``.

This lane is where the split-brain actually happened. ``subprocess.run``'s
timeout kills only the direct child, so a Claude invocation that hit the
bound was reported to the controller as a timeout while its surviving
grandchildren kept working, unsupervised, and later produced commits. The
supervisor removes that outcome structurally: a durable execution record
exists before the process does, the child is started in its own session so
the whole tree is signalable as one group, and every exit path — timeout,
exception, KeyboardInterrupt — either confirms the group dead or transfers
ownership to the supervisor in the same transaction. There is no path on
which this lane returns while an executor it started is still running
unowned.

The lane keeps its own Gauntlet handoff (``route_task=False`` on every
launch) because its evidence is the GATE's exit code, gathered after the
executor exits — a handoff fired by the supervisor at executor-exit time
would move the card out of ``running`` before the gate had even run.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli import exec_supervisor as ex
from hermes_cli import kanban_db as kb

# Bound on each of the Claude and Codex attempts when the task doesn't set
# its own max_runtime_seconds. Deliberately generous — this is one shot, not
# a loop, so there's no compounding cost to a long-ish bound.
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 1800
# Separate (short) bound for the gate command itself — a test suite or CI
# gate should never legitimately run as long as an agentic repair attempt.
DEFAULT_GATE_TIMEOUT_SECONDS = 600
# How much of each CLI's stdout/stderr is kept as evidence in comments/
# metadata. Full transcripts belong in the CLI's own session logs.
EVIDENCE_MAX_CHARS = 4000


@dataclass
class AttemptResult:
    executor: str
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    error: Optional[str] = None
    #: Durable execution record this attempt ran under. Carried into the
    #: evidence so a human reading a blocked card can run
    #: ``hermes kanban exec show <id>`` and see who owned the process and how
    #: it actually ended, rather than inferring it from an exit code.
    execution_id: Optional[str] = None
    #: Supervisor status (completed / failed / timed_out / controller_lost /
    #: terminated / stale). ``returncode == 0`` alone is NOT success here.
    execution_status: Optional[str] = None

    @property
    def ok(self) -> bool:
        """Whether this attempt genuinely ran to a clean finish.

        Deliberately not ``returncode == 0``: a controller-lost or terminated
        execution can leave a stale zero lying around, and the entire point of
        the supervisor is that the status, not the exit code, is the authority
        on whether an executor finished.
        """
        return self.execution_status == ex.STATUS_COMPLETED and self.returncode == 0

    @property
    def evidence(self) -> str:
        suffix = f" [execution {self.execution_id}]" if self.execution_id else ""
        if self.error:
            return f"[{self.executor}] invocation error: {self.error}{suffix}"
        if self.timed_out:
            return (
                f"[{self.executor}] timed out (rc=None); the supervisor "
                f"confirmed the process group ended{suffix}"
            )
        if self.execution_status and self.execution_status != ex.STATUS_COMPLETED:
            return (
                f"[{self.executor}] execution ended "
                f"{self.execution_status} (rc={self.returncode}){suffix}"
            )
        parts = [f"[{self.executor}] rc={self.returncode}{suffix}"]
        if self.stdout.strip():
            parts.append(f"stdout:\n{_truncate(self.stdout)}")
        if self.stderr.strip():
            parts.append(f"stderr:\n{_truncate(self.stderr)}")
        return "\n".join(parts)


@dataclass
class GateResult:
    ok: bool
    returncode: Optional[int]
    output: str
    timed_out: bool = False
    error: Optional[str] = None

    @property
    def evidence(self) -> str:
        if self.error:
            return f"[gate] invocation error: {self.error}"
        if self.timed_out:
            return "[gate] timed out"
        return f"[gate] rc={self.returncode}\n{_truncate(self.output)}"


def _truncate(text: str) -> str:
    text = text or ""
    if len(text) <= EVIDENCE_MAX_CHARS:
        return text
    return text[:EVIDENCE_MAX_CHARS] + f"\n... [truncated, {len(text)} chars total]"


def _build_repair_prompt(task: kb.Task, prior_evidence: Optional[str] = None) -> str:
    lines = [
        "You are performing a bounded automated repair on a Hermes Kanban "
        "recovery task. Fix the underlying issue so the following gate "
        "command passes, then stop.",
        "",
        f"Task title: {task.title}",
        "",
        "Task body / failure description:",
        task.body or "(no body)",
        "",
        f"Gate command (must exit 0 when you are done): {task.recovery_gate_cmd}",
    ]
    if prior_evidence:
        lines += [
            "",
            "A previous automated repair attempt already ran and did NOT "
            "make the gate pass. Its evidence (do not repeat the same "
            "unsuccessful approach):",
            prior_evidence,
        ]
    return "\n".join(lines)


def _build_claude_task_prompt(task: kb.Task) -> str:
    return "\n".join([
        "You are the direct Claude Code executor for one Hermes Kanban task.",
        "Do the task itself; do not create or manage Kanban cards. The wrapper",
        "will record completion or blocking after your process exits.",
        "",
        f"Task ID: {task.id}",
        f"Task title: {task.title}",
        f"Tenant: {task.tenant or 'shared / none'}",
        "",
        "Task body / acceptance criteria:",
        task.body or "(no body)",
        "",
        "Execution rules:",
        "- Work only within the authority/scope stated in the task body.",
        "- Treat read-only/no-mutation wording as binding.",
        "- Never print or expose secrets or credential values.",
        "- If a file/brief/artifact is requested, create it in the current task workspace.",
        "- Run the narrow verification appropriate to the task before stopping.",
        "- End with a concise factual result: what you did, exact evidence/tests, and any blocker.",
    ])


def _build_codex_verifier_prompt(task: kb.Task) -> str:
    return "\n".join([
        "You are Atlas, the independent Codex verification executor for one Hermes Kanban task.",
        "This is a READ-ONLY verification lane. Do not edit files, commit, deploy, or mutate services/databases.",
        "Re-derive the requested checks independently from live files/state and report falsifiable evidence.",
        "Do not trust an implementer's self-report when the task asks you to verify it.",
        "",
        f"Task ID: {task.id}",
        f"Task title: {task.title}",
        f"Tenant: {task.tenant or 'shared / none'}",
        f"Target workspace/path: {task.workspace_path or '(none; use task brief paths)'}",
        "The verifier process itself runs from a governed scratch directory. Read the target path by absolute path when the brief requires it.",
        "",
        "Verification brief / acceptance criteria:",
        task.body or "(no body)",
        "",
        "End with a concise structured verdict: PASS/FAIL per requested item, evidence, and explicit GO/NO-GO when applicable.",
    ])


def _extract_codex_result(stdout: str) -> str:
    text = (stdout or "").strip()
    marker = "--- last message ---"
    if marker in text:
        last = text.rsplit(marker, 1)[1].strip()
        if last:
            return last
    return _truncate(text) if text else "Codex verifier completed with no textual result."


def _extract_claude_result(stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return "Claude executor completed with no textual result."
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            for key in ("result", "message", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        pass
    return _truncate(text)


def _snapshot_workspace(cwd: str) -> dict[str, tuple[int, int]]:
    root = Path(cwd)
    out: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return out
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            st = path.stat()
            out[str(rel)] = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            continue
    return out


def _changed_workspace_files(cwd: str, before: dict[str, tuple[int, int]]) -> list[Path]:
    root = Path(cwd)
    changed: list[Path] = []
    if not root.is_dir():
        return changed
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            st = path.stat()
            sig = (int(st.st_mtime_ns), int(st.st_size))
            if before.get(str(rel)) != sig:
                changed.append(path)
        except OSError:
            continue
    return sorted(changed, key=lambda p: str(p))


def _claim_direct_claude_attempt(conn, task_id: str, run_id: Optional[int]) -> bool:
    if run_id is None:
        raise RuntimeError("Claude executor task has no active run")
    kind = "claude_executor_started"
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["current_run_id"] is None
            or int(current["current_run_id"]) != int(run_id)
        ):
            raise RuntimeError("stale Claude executor run")
        prior = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND run_id = ? LIMIT 1",
            (task_id, kind, run_id),
        ).fetchone()
        if prior is not None:
            return False
        kb._append_event(
            conn, task_id, kind, {"executor": "claude"}, run_id=run_id
        )
    return True


def _claim_codex_verifier_attempt(conn, task_id: str, run_id: Optional[int]) -> bool:
    if run_id is None:
        raise RuntimeError("Codex verifier task has no active run")
    kind = "codex_verifier_started"
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["current_run_id"] is None
            or int(current["current_run_id"]) != int(run_id)
        ):
            raise RuntimeError("stale Codex verifier run")
        prior = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND run_id = ? LIMIT 1",
            (task_id, kind, run_id),
        ).fetchone()
        if prior is not None:
            return False
        kb._append_event(conn, task_id, kind, {"executor": "codex"}, run_id=run_id)
    return True


def _attach_changed_files(conn, task_id: str, files: list[Path]) -> list[str]:
    attached: list[str] = []
    for path in files[:20]:
        try:
            data = path.read_bytes()
            kb.store_attachment_bytes(
                conn,
                task_id,
                path.name,
                data,
                uploaded_by="claude-lane",
            )
            attached.append(str(path))
        except Exception:
            # Attachment evidence is best-effort except when the task body
            # explicitly requires an attachment; the caller checks that case.
            continue
    return attached


def _register_attachment_dir_files(conn, task_id: str) -> list[str]:
    """Register files already written into this task's canonical attachment dir.

    Direct executors sometimes know the canonical attachment path and write the
    requested artifact there themselves. The blob is valid evidence, but the
    notifier/completion gate reads ``task_attachments`` rows, not the filesystem
    alone. Register any untracked regular files in that task-scoped directory
    without copying or renaming them. Idempotent on stored_path.
    """
    root = kb.task_attachments_dir(task_id)
    if not root.is_dir():
        return []
    try:
        known = {
            str(Path(a.stored_path).resolve())
            for a in kb.list_attachments(conn, task_id)
            if getattr(a, "stored_path", None)
        }
    except Exception:
        known = set()
    registered: list[str] = []
    for path in sorted(root.iterdir(), key=lambda x: x.name):
        try:
            if not path.is_file() or path.name.startswith("."):
                continue
            resolved = str(path.resolve())
            if resolved in known:
                continue
            kb.add_attachment(
                conn, task_id, filename=path.name, stored_path=resolved,
                size=path.stat().st_size, uploaded_by="claude-lane",
            )
            known.add(resolved)
            registered.append(resolved)
        except Exception:
            continue
    return registered


def run_codex_verifier(task_id: str) -> int:
    """Run one independent read-only verification task through Codex."""
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        if task is None or task.executor_lane != kb.EXECUTOR_LANE_CODEX_VERIFY:
            return 1
        expected_run_id = task.current_run_id
        if expected_run_id is None:
            return 1
        try:
            # Verifiers execute from the governed Kanban scratch root regardless
            # of the target repository path. This preserves execution.allowed_roots
            # while the read-only Codex sandbox may inspect the target by absolute path.
            verifier_cwd = kb.workspaces_root() / task.id / "atlas-verify"
            verifier_cwd.mkdir(parents=True, exist_ok=True)
            cwd = str(verifier_cwd)
        except Exception as exc:
            return 0 if kb.block_task(
                conn, task_id, reason=f"Codex verifier scratch workspace resolution failed: {exc}",
                kind="needs_input", expected_run_id=expected_run_id,
            ) else 1
        timeout = int(task.max_runtime_seconds or DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
        started_at = time.time()
        try:
            should_run = _claim_codex_verifier_attempt(conn, task_id, expected_run_id)
        except RuntimeError:
            return 1
        if not should_run:
            return 1
        attempt = _invoke_codex_verifier(
            _build_codex_verifier_prompt(task), cwd, timeout, task_id=task_id,
        )
        if not attempt.ok:
            reason = "Codex verifier failed or timed out.\n\n" + attempt.evidence
            ok = kb.block_task(conn, task_id, reason=reason, kind="needs_input", expected_run_id=expected_run_id)
            if ok:
                kb.add_comment(conn, task_id, author="atlas-codex-lane", body=attempt.evidence)
            return 0 if ok else 1
        result = _extract_codex_result(attempt.stdout)
        summary = result[:4000]
        metadata = {
            "executor_lane": "codex_verify",
            "executor": "codex",
            "sandbox": "read-only",
            "duration_seconds": round(time.time() - started_at, 1),
        }
        try:
            ok = kb.complete_task(
                conn, task_id, result=result, summary=summary, metadata=metadata,
                expected_run_id=expected_run_id,
            )
        except kb.VerificationRequiredError:
            handed_off = _handoff_for_verification(
                conn, task_id, summary=summary, evidence=attempt.evidence,
                metadata=metadata, expected_run_id=expected_run_id,
                author="atlas-codex-lane",
            )
            return 0 if handed_off else 1
        if ok:
            kb.add_comment(conn, task_id, author="atlas-codex-lane", body=attempt.evidence)
        return 0 if ok else 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_claude_executor(task_id: str) -> int:
    """Run one ordinary task directly through Claude Code, no Hermes agent loop."""
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        if task is None or task.executor_lane != kb.EXECUTOR_LANE_CLAUDE:
            return 1
        expected_run_id = task.current_run_id
        if expected_run_id is None:
            return 1
        cwd = task.workspace_path or "."
        timeout = int(task.max_runtime_seconds or DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
        started_at = time.time()

        try:
            should_run = _claim_direct_claude_attempt(conn, task_id, expected_run_id)
        except RuntimeError:
            return 1
        if not should_run:
            return 1

        track_files = task.workspace_kind == "scratch" or "attach" in (task.body or "").lower()
        before = _snapshot_workspace(cwd) if track_files else {}
        attempt = _invoke_claude(
            _build_claude_task_prompt(task), cwd, timeout, task_id=task_id,
        )

        # ``attempt.ok`` rather than ``returncode == 0``: the supervisor's
        # status is the authority on whether the executor finished. A
        # controller-lost or terminated execution can leave a zero behind,
        # and treating that as success is exactly the split-brain this lane
        # was rewired to end.
        if not attempt.ok:
            reason = "Direct Claude executor failed or timed out.\n\n" + attempt.evidence
            ok = kb.block_task(
                conn,
                task_id,
                reason=reason,
                kind="needs_input",
                expected_run_id=expected_run_id,
            )
            if ok:
                kb.add_comment(
                    conn, task_id, author="claude-lane", body=attempt.evidence
                )
            return 0 if ok else 1

        changed = _changed_workspace_files(cwd, before) if track_files else []
        attached = _attach_changed_files(conn, task_id, changed) if changed else []
        # A direct executor may write an artifact straight into the canonical
        # per-task attachment directory. Register those files as first-class
        # task_attachments rows instead of treating the filesystem blob as
        # missing evidence.
        registered_in_place = _register_attachment_dir_files(conn, task_id)
        try:
            has_registered_attachment = bool(kb.list_attachments(conn, task_id))
        except Exception:
            has_registered_attachment = bool(attached or registered_in_place)
        requires_attachment = "attach" in (task.body or "").lower()
        if requires_attachment and not has_registered_attachment:
            ok = kb.block_task(
                conn,
                task_id,
                reason=(
                    "Direct Claude executor exited successfully but the task required an "
                    "attached artifact and no changed workspace file was produced."
                ),
                kind="needs_input",
                expected_run_id=expected_run_id,
            )
            if ok:
                kb.add_comment(
                    conn, task_id, author="claude-lane", body=attempt.evidence
                )
            return 0 if ok else 1

        result = _extract_claude_result(attempt.stdout)
        summary = result[:4000]
        metadata = {
            "executor_lane": "claude",
            "executor": "claude",
            "duration_seconds": round(time.time() - started_at, 1),
            "changed_workspace_files": [str(p) for p in changed],
            "attached_files": attached + registered_in_place,
        }
        try:
            ok = kb.complete_task(
                conn,
                task_id,
                result=result,
                summary=summary,
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
        except kb.VerificationRequiredError:
            handed_off = _handoff_for_verification(
                conn, task_id,
                summary=summary,
                evidence=(
                    f"{attempt.evidence}\n\n"
                    f"Attached files: {(attached + registered_in_place) or 'none'}"
                ),
                metadata=metadata,
                expected_run_id=expected_run_id,
                author="claude-lane",
            )
            return 0 if handed_off else 1
        if ok:
            kb.add_comment(
                conn,
                task_id,
                author="claude-lane",
                body=(
                    "Direct Claude executor completed.\n\n"
                    f"{attempt.evidence}\n\n"
                    f"Attached files: {(attached + registered_in_place) or 'none'}"
                ),
            )
        return 0 if ok else 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _invoke_claude(
    prompt: str, cwd: str, timeout: int, *, task_id: Optional[str] = None,
) -> AttemptResult:
    """One bounded Claude attempt, under durable supervisor ownership.

    The prompt is passed to the launcher in memory and is never persisted:
    the execution record stores the command CLASS (``claude.headless``), not
    the argv. See ``exec_supervisor.Launcher``.
    """
    return _supervised_attempt(
        "claude", "claude.headless", {"prompt": prompt}, cwd, timeout,
        task_id=task_id,
    )


def _invoke_codex_verifier(
    prompt: str, cwd: str, timeout: int, *, task_id: Optional[str] = None,
) -> AttemptResult:
    """One bounded independent Codex verification attempt in read-only sandbox."""
    with tempfile.NamedTemporaryFile(prefix="hermes-codex-verify-", suffix=".txt", delete=False) as tmp:
        last_message_path = tmp.name
    try:
        attempt = _supervised_attempt(
            "codex", "codex.verify",
            {"prompt": prompt, "last_message_path": last_message_path},
            cwd, timeout, task_id=task_id,
        )
    finally:
        try:
            last_message = Path(last_message_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            last_message = ""
        try:
            Path(last_message_path).unlink(missing_ok=True)
        except Exception:
            pass
    if last_message.strip():
        attempt.stdout = f"{attempt.stdout}\n--- last message ---\n{last_message}"
    return attempt


def _invoke_recovery_claude(
    prompt: str, cwd: str, timeout: int, *, task_id: Optional[str] = None,
) -> AttemptResult:
    """One bounded Claude recovery attempt using the recovery-only boot path."""
    if not task_id:
        return AttemptResult("claude", None, "", "", error="recovery task id missing")
    return _supervised_attempt(
        "claude", "claude.recovery", {"prompt": prompt, "task_id": task_id},
        cwd, timeout, task_id=task_id,
    )


def _supervised_attempt(
    executor: str,
    command_class: str,
    spec: dict,
    cwd: str,
    timeout: int,
    *,
    task_id: Optional[str] = None,
) -> AttemptResult:
    """Shared body for the agentic attempts.

    A policy refusal is returned as a failed attempt rather than raised: the
    lane's contract is one bounded attempt then a decision, and a refused
    launch is a decidable outcome. It is never silently retried, and the
    refusal text names the policy code so an operator can see which bound
    said no.
    """
    try:
        result = ex.run_supervised(
            command_class=command_class,
            spec=spec,
            cwd=cwd,
            task_id=task_id,
            timeout=timeout,
            # This lane performs its own Gauntlet handoff after the gate runs.
            route_task=False,
        )
    except ex.ExecutionPolicyError as exc:
        return AttemptResult(
            executor, None, "", "",
            error=f"refused by execution policy ({exc.code}): {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return AttemptResult(executor, None, "", "", error=str(exc))
    return AttemptResult(
        executor,
        result.exit_code,
        result.stdout,
        result.stderr,
        timed_out=result.timed_out,
        error=result.error,
        execution_id=result.execution_id,
        execution_status=result.status,
    )


def _invoke_codex(
    prompt: str, cwd: str, timeout: int, *, task_id: Optional[str] = None,
) -> AttemptResult:
    """One bounded Codex attempt, under durable supervisor ownership."""
    with tempfile.NamedTemporaryFile(
        prefix="hermes-recovery-codex-", suffix=".txt", delete=False
    ) as tmp:
        last_message_path = tmp.name
    try:
        if not task_id:
            return AttemptResult("codex", None, "", "", error="recovery task id missing")
        attempt = _supervised_attempt(
            "codex", "codex.recovery",
            {"prompt": prompt, "last_message_path": last_message_path, "task_id": task_id},
            cwd, timeout, task_id=task_id,
        )
    finally:
        try:
            last_message = Path(last_message_path).read_text(
                encoding="utf-8", errors="replace"
            )
        except Exception:
            last_message = ""
        try:
            Path(last_message_path).unlink(missing_ok=True)
        except Exception:
            pass
    if last_message.strip():
        attempt.stdout = f"{attempt.stdout}\n--- last message ---\n{last_message}"
    return attempt


def _gate_argv(gate_cmd: str) -> list[str]:
    """Parse a simple gate command without invoking a shell.

    Delegates to the supervisor so there is exactly ONE definition of what a
    gate command may contain. Two copies of a security-relevant parser drift,
    and the copy that drifts is always the one that stops refusing something.

    Re-raised as ``ValueError`` to preserve this function's historical
    contract for existing callers and tests.
    """
    try:
        return ex.gate_argv(gate_cmd)
    except ex.ExecutionPolicyError as exc:
        raise ValueError(f"recovery_gate_cmd rejected: {exc}") from exc


def _run_gate(
    gate_cmd: str, cwd: str, timeout: int, *, task_id: Optional[str] = None,
) -> GateResult:
    """Run the mechanical gate under supervision.

    The gate decides whether the card may be handed off, so an ambiguous gate
    is worse than a red one: ``ok`` requires the supervisor to have observed a
    genuine ``completed`` execution with exit code 0. A gate that was
    terminated, timed out, or lost its controller is NOT green, no matter what
    exit code happened to be recorded.
    """
    try:
        result = ex.run_supervised(
            command_class="shell.gate",
            spec={"command": gate_cmd},
            cwd=cwd,
            task_id=task_id,
            timeout=timeout,
            route_task=False,
        )
    except ex.ExecutionPolicyError as exc:
        return GateResult(
            False, None, "",
            error=f"refused by execution policy ({exc.code}): {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return GateResult(False, None, "", error=str(exc))
    output = (result.stdout or "") + (result.stderr or "")
    green = result.status == ex.STATUS_COMPLETED and result.exit_code == 0
    return GateResult(
        green, result.exit_code, output, timed_out=result.timed_out,
    )


def _claim_attempt(conn, task_id: str, kind: str, run_id: Optional[int]) -> bool:
    """Durably consume one executor attempt using the existing task event log."""
    if run_id is None:
        raise RuntimeError("recovery task has no active run")
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if (
            current is None
            or current["status"] != "running"
            or current["current_run_id"] is None
            or int(current["current_run_id"]) != int(run_id)
        ):
            raise RuntimeError("stale recovery run")
        row = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
            (task_id, kind),
        ).fetchone()
        if row is not None:
            return False
        kb._append_event(
            conn, task_id, kind,
            {"executor": "claude" if kind == "recovery_claude_started" else "codex"},
            run_id=run_id,
        )
    return True


def run_claude_first_recovery(task_id: str) -> int:
    """Run the explicit Claude-first recovery lane without a Hermes agent loop."""
    conn = kb.connect()
    try:
        task = kb.get_task(conn, task_id)
        if task is None or task.executor_lane != kb.EXECUTOR_LANE_CLAUDE_RECOVERY:
            return 1
        expected_run_id = task.current_run_id
        if expected_run_id is None:
            return 1
        if not task.recovery_gate_cmd:
            return 0 if kb.block_task(
                conn, task_id,
                reason="recovery lane misconfigured: no recovery_gate_cmd set",
                kind="needs_input", expected_run_id=expected_run_id,
            ) else 1
        try:
            _gate_argv(task.recovery_gate_cmd)
        except ValueError as exc:
            return 0 if kb.block_task(
                conn, task_id,
                reason=f"recovery lane misconfigured: {exc}",
                kind="needs_input", expected_run_id=expected_run_id,
            ) else 1

        cwd = task.workspace_path or "."
        attempt_timeout = int(task.max_runtime_seconds or DEFAULT_ATTEMPT_TIMEOUT_SECONDS)
        gate_timeout = min(attempt_timeout, DEFAULT_GATE_TIMEOUT_SECONDS)
        started_at = time.time()

        # Resume/human-intervention short circuit: the prerequisite may have
        # been repaired out-of-band while this recovery task was blocked.
        # Always test the deterministic gate before consuming another bounded
        # executor attempt. If it is already green, close through the recovery
        # lane with gate evidence and let normal dependency promotion proceed.
        pre_gate = _run_gate(
            task.recovery_gate_cmd, cwd, gate_timeout, task_id=task_id,
        )
        if pre_gate.ok:
            return 0 if _complete(
                conn, task_id,
                summary="Recovery lane: gate already green at resume; no executor attempt required.",
                attempts=[], gate=pre_gate,
                started_at=started_at, expected_run_id=expected_run_id,
            ) else 1

        try:
            run_claude = _claim_attempt(
                conn, task_id, "recovery_claude_started", expected_run_id
            )
        except RuntimeError:
            return 1
        claude_attempt = (
            _invoke_recovery_claude(
                _build_repair_prompt(task), cwd, attempt_timeout, task_id=task_id,
            )
            if run_claude
            else AttemptResult(
                "claude", None, "", "",
                error="prior Claude attempt already recorded; not repeated",
            )
        )
        claude_gate = _run_gate(
            task.recovery_gate_cmd, cwd, gate_timeout, task_id=task_id,
        )
        if claude_gate.ok:
            return 0 if _complete(
                conn, task_id,
                summary="Recovery lane: Claude repair verified gate green.",
                attempts=[claude_attempt], gate=claude_gate,
                started_at=started_at, expected_run_id=expected_run_id,
            ) else 1

        try:
            run_codex = _claim_attempt(
                conn, task_id, "recovery_codex_started", expected_run_id
            )
        except RuntimeError:
            return 1
        codex_attempt = (
            _invoke_codex(
                _build_repair_prompt(task, prior_evidence=claude_attempt.evidence),
                cwd, attempt_timeout, task_id=task_id,
            )
            if run_codex
            else AttemptResult(
                "codex", None, "", "",
                error="prior Codex attempt already recorded; not repeated",
            )
        )
        codex_gate = _run_gate(
            task.recovery_gate_cmd, cwd, gate_timeout, task_id=task_id,
        )
        if codex_gate.ok:
            return 0 if _complete(
                conn, task_id,
                summary="Recovery lane: Codex repair verified gate green after Claude.",
                attempts=[claude_attempt, codex_attempt], gate=codex_gate,
                started_at=started_at, expected_run_id=expected_run_id,
            ) else 1

        return 0 if _escalate(
            conn, task_id,
            attempts=[claude_attempt, codex_attempt],
            gates=[claude_gate, codex_gate],
            expected_run_id=expected_run_id,
        ) else 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _handoff_for_verification(
    conn, task_id, *, summary, evidence, metadata, expected_run_id, author
) -> bool:
    """Park a gauntlet-enforced task in the review lane with its gate evidence.

    The recovery lane is an executor, so under Gauntlet enforcement it may not
    close its own card even though its gate is a mechanical (non-LLM) check.
    Rather than fail the run, hand the work off as VERIFICATION_PENDING with
    the gate output attached — that is the one legal next step, and it keeps
    the evidence with the card for whoever records the verdict.
    """
    ok = kb.request_review(
        conn, task_id,
        summary=summary,
        metadata=metadata,
        expected_run_id=expected_run_id,
    )
    if ok:
        kb.add_comment(
            conn, task_id, author=author,
            body=(
                "Gauntlet enforcement is on for this card, so the executor "
                "cannot self-complete. Handed off for verification with the "
                f"gate evidence below.\n\n{summary}\n\n{evidence}"
            ),
        )
    return bool(ok)


def _complete(
    conn, task_id, *, summary, attempts, gate, started_at, expected_run_id
) -> bool:
    evidence = "\n\n".join(a.evidence for a in attempts) + f"\n\n{gate.evidence}"
    metadata = {
        "recovery_lane": "claude_recovery",
        "executors": [a.executor for a in attempts],
        "duration_seconds": round(time.time() - started_at, 1),
    }
    try:
        ok = kb.complete_task(
            conn, task_id,
            result=summary,
            summary=summary,
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
    except kb.VerificationRequiredError:
        return _handoff_for_verification(
            conn, task_id,
            summary=summary, evidence=evidence, metadata=metadata,
            expected_run_id=expected_run_id, author="recovery-lane",
        )
    if ok:
        kb.add_comment(
            conn, task_id, author="recovery-lane", body=f"{summary}\n\n{evidence}"
        )
    return ok


def _escalate(conn, task_id, *, attempts, gates, expected_run_id) -> bool:
    evidence = "\n\n".join(
        f"{a.evidence}\n\n{g.evidence}" for a, g in zip(attempts, gates)
    )
    ok = kb.block_task(
        conn, task_id,
        reason=(
            "Recovery lane exhausted: Claude and Codex each had one attempt; "
            "the gate is still red. Evidence:\n\n"
            f"{evidence}"
        ),
        kind="needs_input",
        expected_run_id=expected_run_id,
    )
    if ok:
        kb.add_comment(
            conn, task_id, author="recovery-lane",
            body="Recovery attempts exhausted; escalated for human review.",
        )
    return ok
