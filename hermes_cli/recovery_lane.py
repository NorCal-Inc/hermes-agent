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
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

    @property
    def evidence(self) -> str:
        if self.error:
            return f"[{self.executor}] invocation error: {self.error}"
        if self.timed_out:
            return f"[{self.executor}] timed out (rc=None)"
        parts = [f"[{self.executor}] rc={self.returncode}"]
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
        attempt = _invoke_claude(_build_claude_task_prompt(task), cwd, timeout)

        if attempt.error or attempt.timed_out or attempt.returncode != 0:
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
        ok = kb.complete_task(
            conn,
            task_id,
            result=result,
            summary=summary,
            metadata={
                "executor_lane": "claude",
                "executor": "claude",
                "duration_seconds": round(time.time() - started_at, 1),
                "changed_workspace_files": [str(p) for p in changed],
                "attached_files": attached + registered_in_place,
            },
            expected_run_id=expected_run_id,
        )
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


def _invoke_claude(prompt: str, cwd: str, timeout: int) -> AttemptResult:
    claude_bin = shutil.which("claude") or "claude"
    try:
        proc = subprocess.run(
            [
                claude_bin, "-p", prompt,
                "--output-format", "json",
                "--permission-mode", "acceptEdits",
            ],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return AttemptResult("claude", None, "", "", timed_out=True)
    except Exception as exc:
        return AttemptResult("claude", None, "", "", error=str(exc))
    return AttemptResult("claude", proc.returncode, proc.stdout, proc.stderr)


def _invoke_codex(prompt: str, cwd: str, timeout: int) -> AttemptResult:
    codex_bin = shutil.which("codex") or "codex"
    with tempfile.NamedTemporaryFile(
        prefix="hermes-recovery-codex-", suffix=".txt", delete=False
    ) as tmp:
        last_message_path = tmp.name
    try:
        proc = subprocess.run(
            [
                codex_bin, "exec", prompt,
                "--json",
                "--sandbox", "workspace-write",
                "--skip-git-repo-check",
                "-o", last_message_path,
            ],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return AttemptResult("codex", None, "", "", timed_out=True)
    except Exception as exc:
        return AttemptResult("codex", None, "", "", error=str(exc))
    finally:
        try:
            last_message = Path(last_message_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            last_message = ""
        try:
            Path(last_message_path).unlink(missing_ok=True)
        except Exception:
            pass
    stdout = proc.stdout
    if last_message.strip():
        stdout = f"{stdout}\n--- last message ---\n{last_message}"
    return AttemptResult("codex", proc.returncode, stdout, proc.stderr)


def _gate_argv(gate_cmd: str) -> list[str]:
    """Parse a simple gate command without invoking a shell."""
    if any(token in gate_cmd for token in ("|", ";", "&&", "||", ">", "<", "`", "$(", "\n", "\r")):
        raise ValueError("recovery_gate_cmd may not contain shell operators or redirection")
    argv = shlex.split(gate_cmd)
    if not argv:
        raise ValueError("recovery_gate_cmd is empty")
    return argv


def _run_gate(gate_cmd: str, cwd: str, timeout: int) -> GateResult:
    try:
        proc = subprocess.run(
            _gate_argv(gate_cmd), shell=False, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GateResult(False, None, "", timed_out=True)
    except Exception as exc:
        return GateResult(False, None, "", error=str(exc))
    output = (proc.stdout or "") + (proc.stderr or "")
    return GateResult(proc.returncode == 0, proc.returncode, output)


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

        try:
            run_claude = _claim_attempt(
                conn, task_id, "recovery_claude_started", expected_run_id
            )
        except RuntimeError:
            return 1
        claude_attempt = (
            _invoke_claude(_build_repair_prompt(task), cwd, attempt_timeout)
            if run_claude
            else AttemptResult(
                "claude", None, "", "",
                error="prior Claude attempt already recorded; not repeated",
            )
        )
        claude_gate = _run_gate(task.recovery_gate_cmd, cwd, gate_timeout)
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
                cwd, attempt_timeout,
            )
            if run_codex
            else AttemptResult(
                "codex", None, "", "",
                error="prior Codex attempt already recorded; not repeated",
            )
        )
        codex_gate = _run_gate(task.recovery_gate_cmd, cwd, gate_timeout)
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


def _complete(
    conn, task_id, *, summary, attempts, gate, started_at, expected_run_id
) -> bool:
    evidence = "\n\n".join(a.evidence for a in attempts) + f"\n\n{gate.evidence}"
    ok = kb.complete_task(
        conn, task_id,
        result=summary,
        summary=summary,
        metadata={
            "recovery_lane": "claude_recovery",
            "executors": [a.executor for a in attempts],
            "duration_seconds": round(time.time() - started_at, 1),
        },
        expected_run_id=expected_run_id,
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
