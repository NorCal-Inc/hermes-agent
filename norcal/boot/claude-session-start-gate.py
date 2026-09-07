#!/usr/bin/env python3
import datetime as dt
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from recovery_boot import recovery_task_id, shared_boot_complete, validate_recovery_task

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
claude_complete = bool(re.search(r'(?m)^CLAUDE BOOT STATUS: COMPLETE\s*$', ctx))
shared_loaded = '<shared-boot-state>' in ctx
shared_complete = bool(shared_loaded and shared_boot_complete(ctx))

# Boot parity. ``verify.py`` asserts the boot plumbing itself: that the four
# entry points in ~/.local/bin still resolve into the canonical boot dir, that
# every boot-state parser is exact-line anchored, that Erika's two session
# paths judge boot through the shared chokepoint rather than an exit code, and
# that the live attempt ceiling / admission control / selective-enforcement
# classifier are present and decoupled.
#
# It had ZERO callers, so none of that was checked at session start while
# PR #4 kept adding assertions to it. Run it here, as a gate term.
#
# Fails CLOSED for ordinary work (``complete``) but is deliberately NOT a term
# in ``recovery_only`` below: a parity failure is exactly what a recovery
# session is authorized to repair, and gating recovery on it would reproduce
# the circular deadlock doctrine 2.27 was written to remove.
parity_ok = False
parity_detail = 'not run'
try:
    _verify = Path(__file__).resolve().parent / 'verify.py'
    _vp = subprocess.run(
        [sys.executable, str(_verify)], capture_output=True, text=True, timeout=120
    )
    _vout = ((_vp.stdout or '') + '\n' + (_vp.stderr or '')).strip()
    # Exact-line anchored, per doctrine 2.27: a prose mention of the pass
    # string anywhere in the output is not evidence that parity passed.
    parity_ok = _vp.returncode == 0 and bool(
        re.search(r'(?m)^BOOT PARITY: PASS\s*$', _vout)
    )
    if parity_ok:
        # Record the pass explicitly. Leaving the default here would write
        # "not run" into the state file for a gate that DID run and passed —
        # a state file that misreports its own gate is the failure this whole
        # change exists to remove.
        parity_detail = 'PASS'
    else:
        _errs = [ln for ln in _vout.splitlines() if ln.startswith('ERROR:')]
        parity_detail = '; '.join(_errs) if _errs else f'rc={_vp.returncode}'
except Exception as exc:
    parity_detail = f'boot parity check did not run: {exc}'

complete = bool(sid and claude_complete and shared_complete and parity_ok and (proc is None or proc.returncode == 0))
recovery_id = recovery_task_id()
recovery_authorized, recovery_reason = validate_recovery_task(recovery_id) if recovery_id else (False, 'not requested')
# Claude role rules must be synchronized even for recovery. The shared gate itself may
# be red because repairing that named gate is the sole purpose of this session.
claude_rules_ready = 'LOCAL/CANONICAL CLAUDE.md: OK' in ctx
recovery_only = bool(
    sid and recovery_authorized and shared_loaded and claude_rules_ready
    and (proc is None or proc.returncode == 0)
)

state = {
    'session_id': sid,
    'cwd': cwd,
    'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'complete': complete,
    'recovery_only': recovery_only,
    'recovery_task_id': recovery_id if recovery_only else None,
    'recovery_authority_reason': recovery_reason,
    'claude_boot_complete': claude_complete,
    'shared_boot_complete': shared_complete,
    'boot_parity_ok': parity_ok,
    'boot_parity_detail': parity_detail,
    'boot_returncode': None if proc is None else proc.returncode,
}
if sid:
    tmp = STATE_DIR / f'.{sid}.tmp'
    final = STATE_DIR / f'{sid}.json'
    tmp.write_text(json.dumps(state, sort_keys=True) + '\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, final)

if recovery_only and not complete:
    msg = payload.get('systemMessage') or ''
    payload['systemMessage'] = (msg + f' | RECOVERY-ONLY ACTIVE: {recovery_id}').strip(' |')
    hso = payload.setdefault('hookSpecificOutput', {})
    existing = str(hso.get('additionalContext') or '')
    hso['additionalContext'] = existing + (
        '\n\n<RECOVERY-ONLY>Normal execution remains blocked. This session may only diagnose '
        'and repair the failed boot gate named by its authorized recovery card, rerun '
        'the deterministic gate, and report evidence.</RECOVERY-ONLY>'
    )
elif not complete:
    msg = payload.get('systemMessage') or ''
    payload['systemMessage'] = (msg + ' | HARD GATE ACTIVE: all tools denied until a fresh session reaches COMPLETE').strip(' |')
    hso = payload.setdefault('hookSpecificOutput', {})
    existing = str(hso.get('additionalContext') or '')
    named = '' if parity_ok else f'\nFAILED GATE: boot parity — {parity_detail}'
    hso['additionalContext'] = existing + '\n\n<HARD-GATE>INCOMPLETE BOOT. DO NOT EXECUTE TOOLS. START A FRESH SESSION AFTER THE NAMED BOOT FAILURE IS FIXED.' + named + '</HARD-GATE>'

print(json.dumps(payload))
