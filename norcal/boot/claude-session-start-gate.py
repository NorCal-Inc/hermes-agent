#!/usr/bin/env python3
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

STATE_DIR = Path('/home/chris/.claude/session-gates')
BOOT = str(Path(__file__).resolve().parent / 'claude-boot-context')
STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(STATE_DIR, 0o700)

try:
    raw = sys.stdin.read()
    hook = json.loads(raw) if raw.strip() else {}
except Exception:
    hook = {}

sid = str(hook.get('session_id') or '').strip()
cwd = str(hook.get('cwd') or '').strip()

try:
    proc = subprocess.run([BOOT], capture_output=True, text=True, timeout=60)
    stdout = (proc.stdout or '').strip()
    payload = json.loads(stdout) if stdout else {}
except Exception as exc:
    proc = None
    payload = {
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': f'<claude-bootstrap>\nCLAUDE BOOT STATUS: DEGRADED — STOP BEFORE TASK EXECUTION\nFAILURE: session-start wrapper exception: {exc}\n</claude-bootstrap>'
        },
        'systemMessage': 'shared boot: DEGRADED — STOP BEFORE TASK EXECUTION'
    }

ctx = str(payload.get('hookSpecificOutput', {}).get('additionalContext') or '')
claude_complete = 'CLAUDE BOOT STATUS: COMPLETE' in ctx
shared_complete = '<shared-boot-state>' in ctx and 'BOOT STATUS: COMPLETE' in ctx
complete = bool(sid and claude_complete and shared_complete and (proc is None or proc.returncode == 0))

state = {
    'session_id': sid,
    'cwd': cwd,
    'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'complete': complete,
    'claude_boot_complete': claude_complete,
    'shared_boot_complete': shared_complete,
    'boot_returncode': None if proc is None else proc.returncode,
}
if sid:
    tmp = STATE_DIR / f'.{sid}.tmp'
    final = STATE_DIR / f'{sid}.json'
    tmp.write_text(json.dumps(state, sort_keys=True) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)

if not complete:
    msg = payload.get('systemMessage') or ''
    payload['systemMessage'] = (msg + ' | HARD GATE ACTIVE: all tools denied until a fresh session reaches COMPLETE').strip(' |')
    hso = payload.setdefault('hookSpecificOutput', {})
    existing = str(hso.get('additionalContext') or '')
    hso['additionalContext'] = existing + '\n\n<HARD-GATE>INCOMPLETE BOOT. DO NOT EXECUTE TOOLS. START A FRESH SESSION AFTER THE NAMED BOOT FAILURE IS FIXED.</HARD-GATE>'

print(json.dumps(payload))
