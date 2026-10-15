#!/home/chris/.hermes/hermes-agent-next/venv/bin/python
"""Discovery-artifact reuse for Kanban-executed Claude Code sessions.

Mechanism, not memory (Business/Systems/2026-08-14-vault-memory-vs-mechanism-gap.md,
authorized 2026-09-16 in Kanban task t_710184c8). Incident it closes: t_b096d8f2 ran the
same Explore inventory in runs 2899 and 2900; run 2900's 28 KB result returned and then
died with the transcript when the executor was killed, so the next run would start over.

One script, three hook events:

* SubagentStop  -- a discovery-shaped subagent finished inside a Kanban task: persist its
  final report to  attachments/<task>/discovery/<agent_id>.md  the moment it exists.
* PreToolUse Agent|Task -- before a discovery-shaped subagent is spawned inside a Kanban
  task: (a) harvest any completed or interrupted subagent reports left in earlier session
  transcripts of this task that were never persisted, then (b) deny the spawn while any
  discovery artifact in the task's objective lineage has not been Read in this session.
  Once every artifact has been read, the spawn is allowed (the artifact may be stale or
  too narrow) -- the gate forces reuse-first, it does not forbid new discovery.
* PreToolUse Read|Bash -- record that this session read an artifact.

Outside a Kanban task (no HERMES_KANBAN_TASK) every path is a no-op. Any internal error
fails open for the spawn (exit 0, no output): this is a cost/reuse control, not a safety
boundary, and it must never wedge an executor.
"""
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

ATTACH_ROOT = Path(os.environ.get('HERMES_KANBAN_ATTACHMENTS_ROOT', '/home/chris/.hermes/kanban/attachments'))
KANBAN_DB = os.environ.get('HERMES_KANBAN_DB', '/home/chris/.hermes/kanban.db')
PROJECTS_ROOT = Path('/home/chris/.claude/projects')
STATE_DIR = Path('/home/chris/.claude/session-gates/discovery-reuse')
REPO = '/home/chris/.hermes/hermes-agent-next'

DISCOVERY_AGENT_TYPES = {'explore'}
DISCOVERY_WORDS = re.compile(
    r'\b(inventor(y|ies|y of)|dependency map|map (every|all)|every (place|call ?site|caller|usage)'
    r'|find (every|all)|all (call ?sites|callers|usages|places)|survey|sweep|audit|explore'
    r'|very thorough|codebase|across the (repo|codebase))\b',
    re.I,
)
MIN_REPORT_CHARS = 400


def is_discovery(agent_type: str, description: str, prompt: str) -> bool:
    if (agent_type or '').strip().lower() in DISCOVERY_AGENT_TYPES:
        return True
    return bool(DISCOVERY_WORDS.search(f'{description}\n{prompt}'))


def lineage(task_id: str) -> list:
    try:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from hermes_cli import kanban_db as k
        conn = sqlite3.connect(f'file:{KANBAN_DB}?mode=ro', uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            root = k._objective_lineage_root(conn, task_id)
            members = k._objective_lineage_members(conn, root)
        finally:
            conn.close()
        return sorted(set(members) | {task_id})
    except Exception:
        return [task_id]


def artifact_dir(task_id: str) -> Path:
    return ATTACH_ROOT / task_id / 'discovery'


def lineage_artifacts(task_id: str) -> list:
    out = []
    for member in lineage(task_id):
        d = artifact_dir(member)
        if d.is_dir():
            out.extend(sorted(p for p in d.glob('*.md') if p.is_file()))
    return out


def state_path(session_id: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', session_id or 'nosession')
    return STATE_DIR / f'{safe}.json'


def load_state(session_id: str) -> dict:
    try:
        return json.loads(state_path(session_id).read_text())
    except Exception:
        return {'read': []}


def mark_read(session_id: str, paths: list) -> None:
    st = load_state(session_id)
    seen = set(st.get('read') or [])
    new = [str(p) for p in paths if str(p) not in seen]
    if not new:
        return
    st['read'] = sorted(seen | set(new))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(session_id).write_text(json.dumps(st))


def write_artifact(task_id: str, agent_id: str, *, status: str, agent_type: str,
                   description: str, prompt: str, report: str, source: str) -> Path:
    d = artifact_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r'[^A-Za-z0-9_-]', '_', agent_id or f'agent-{int(time.time())}')
    path = d / f'{safe_id}.md'
    if path.exists():
        return path
    header = [
        f'# Discovery artifact — {description or "(no description)"}',
        '',
        f'- status: {status}',
        f'- task: {task_id}   run: {os.environ.get("HERMES_KANBAN_RUN_ID", "?")}',
        f'- agent: {agent_id} ({agent_type or "?"})',
        f'- captured: {time.strftime("%Y-%m-%dT%H:%M:%S%z")} via {source}',
        '- reuse rule: this is a point-in-time finding. Re-verify any specific fact against live '
        'state before acting on it; do not re-run the whole sweep to do so.',
        '',
        '## Prompt given to the discovery agent',
        '',
        prompt.strip() or '(unavailable)',
        '',
        '## Report',
        '',
        report.strip(),
        '',
    ]
    tmp = path.with_suffix('.md.tmp')
    tmp.write_text('\n'.join(header))
    tmp.replace(path)
    return path


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def read_subagent(jsonl: Path):
    """Return (prompt, final_text, completed) from a subagent transcript."""
    prompt, last_text, completed = '', '', False
    try:
        lines = jsonl.read_text(errors='replace').splitlines()
    except Exception:
        return prompt, last_text, completed
    for line in lines:
        try:
            d = json.loads(line)
        except Exception:
            continue
        m = d.get('message') or {}
        role = m.get('role')
        if role == 'user' and not prompt:
            prompt = _text_of(m.get('content'))
        elif role == 'assistant':
            content = m.get('content')
            txt = _text_of(content)
            has_tool = isinstance(content, list) and any(
                isinstance(b, dict) and b.get('type') == 'tool_use' for b in content)
            if txt.strip():
                last_text = txt
            completed = bool(txt.strip()) and not has_tool and m.get('stop_reason') in (None, 'end_turn')
    return prompt, last_text, completed


def harvest(task_id: str) -> list:
    """Persist reports from earlier sessions of this task that never reached an artifact."""
    workspace = os.environ.get('HERMES_KANBAN_WORKSPACE') or ''
    if not workspace:
        return []
    project = PROJECTS_ROOT / re.sub(r'[^A-Za-z0-9-]', '-', workspace)
    written = []
    for meta in sorted(project.glob('*/subagents/agent-*.meta.json')):
        agent_id = meta.name[len('agent-'):-len('.meta.json')]
        if (artifact_dir(task_id) / f'{agent_id}.md').exists():
            continue
        try:
            info = json.loads(meta.read_text())
        except Exception:
            continue
        jsonl = meta.with_name(f'agent-{agent_id}.jsonl')
        prompt, text, completed = read_subagent(jsonl)
        if not is_discovery(info.get('agentType', ''), info.get('description', ''), prompt):
            continue
        if len(text) < MIN_REPORT_CHARS:
            continue
        written.append(write_artifact(
            task_id, agent_id,
            status='COMPLETE' if completed else 'PARTIAL — agent was interrupted; last text only',
            agent_type=info.get('agentType', ''), description=info.get('description', ''),
            prompt=prompt, report=text, source=f'harvest of {jsonl}'))
    return written


def emit_deny(reason: str) -> None:
    reason = reason.replace('Blocked:', 'Finding:').replace('This gate clears automatically once every listed artifact is read.', 'Reuse is recommended and does not block execution.')
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'additionalContext': 'ADVISORY ONLY. ' + reason,
        },
        'systemMessage': 'ADVISORY ONLY. ' + reason,
    }))


def on_subagent_stop(hook: dict, task_id: str) -> None:
    agent_id = str(hook.get('agent_id') or '')
    agent_type = str(hook.get('agent_type') or '')
    report = str(hook.get('last_assistant_message') or '')
    description, prompt = '', ''
    transcript = hook.get('transcript_path')
    if transcript and agent_id:
        sub = Path(transcript).with_suffix('') / 'subagents'
        try:
            description = json.loads((sub / f'agent-{agent_id}.meta.json').read_text()).get('description', '')
        except Exception:
            pass
        prompt, text, _ = read_subagent(sub / f'agent-{agent_id}.jsonl')
        report = report or text
    if len(report) < MIN_REPORT_CHARS or not is_discovery(agent_type, description, prompt):
        return
    write_artifact(task_id, agent_id, status='COMPLETE', agent_type=agent_type,
                   description=description, prompt=prompt, report=report, source='SubagentStop')


def on_pre_agent(hook: dict, task_id: str) -> None:
    ti = hook.get('tool_input') or {}
    if not is_discovery(str(ti.get('subagent_type') or ''), str(ti.get('description') or ''),
                        str(ti.get('prompt') or '')):
        return
    harvest(task_id)
    arts = lineage_artifacts(task_id)
    if not arts:
        return
    read = set(load_state(str(hook.get('session_id') or '')).get('read') or [])
    unread = [p for p in arts if str(p) not in read]
    if not unread:
        return
    listing = '\n'.join(
        f'  - {p}  ({p.stat().st_size} bytes, {time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})'
        for p in unread)
    emit_deny(
        'Blocked: a discovery/inventory artifact already exists for this task lineage and has not been '
        f'read in this session (task {task_id}). Read each one with the Read tool first:\n{listing}\n'
        'Then either act on it, or spawn a NARROWER agent for only the facts it lacks or that have '
        'changed. Re-running a full sweep that a prior run already paid for is what exhausted '
        't_b096d8f2 (runs 2899/2900). This gate clears automatically once every listed artifact is read.')


def on_pre_read(hook: dict, task_id: str) -> None:
    ti = hook.get('tool_input') or {}
    text = str(ti.get('file_path') or '') if hook.get('tool_name') == 'Read' else str(ti.get('command') or '')
    if '/discovery/' not in text:
        return
    hits = [p for p in lineage_artifacts(task_id) if str(p) in text]
    if hits:
        mark_read(str(hook.get('session_id') or ''), hits)


def main() -> int:
    try:
        hook = json.loads(sys.stdin.read() or '{}')
    except Exception:
        return 0
    task_id = os.environ.get('HERMES_KANBAN_TASK', '').strip()
    if not task_id:
        return 0
    event = hook.get('hook_event_name')
    tool = hook.get('tool_name')
    try:
        if event == 'SubagentStop':
            on_subagent_stop(hook, task_id)
        elif event == 'PreToolUse' and tool in ('Agent', 'Task'):
            on_pre_agent(hook, task_id)
        elif event == 'PreToolUse' and tool in ('Read', 'Bash'):
            on_pre_read(hook, task_id)
    except Exception as exc:  # fail open; never wedge an executor
        print(f'claude-discovery-reuse: internal error ignored: {exc!r}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
