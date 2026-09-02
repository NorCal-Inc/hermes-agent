#!/usr/bin/env python3
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

BOOT_DIR = Path('/home/chris/.hermes/hermes-agent-next/norcal/boot')
if str(BOOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOOT_DIR))
from recovery_boot import validate_recovery_task

STATE_DIR = Path('/home/chris/.claude/session-gates')

try:
    hook = json.loads(sys.stdin.read() or '{}')
except Exception:
    hook = {}

sid = str(hook.get('session_id') or '').strip()
tool = str(hook.get('tool_name') or '').strip()
inputs = hook.get('tool_input') or {}


def deny(reason: str):
    print(json.dumps({
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        },
        'systemMessage': reason,
    }))
    raise SystemExit(0)

# Gate 1. A model session cannot use tools until SessionStart proved both Claude and
# shared Hermes boot COMPLETE. This turns "STOP BEFORE TASK EXECUTION" into enforcement.
if not sid:
    deny('Blocked: Claude tool call has no session_id, so startup completion cannot be proven. Start a fresh Claude session.')
state_path = STATE_DIR / f'{sid}.json'
try:
    state = json.loads(state_path.read_text())
except Exception:
    deny('Blocked: no COMPLETE startup-gate record exists for this Claude session. Start a fresh session so CLAUDE.md, shared doctrine, VAULT-INDEX, mistake log, and current state are loaded first.')
if not state.get('complete'):
    recovery_id = str(state.get('recovery_task_id') or '').strip()
    recovery_only = bool(state.get('recovery_only') and recovery_id)
    if not recovery_only:
        deny('Blocked: this Claude session booted DEGRADED. The mandated startup chain did not complete. Only the mechanically authorized recovery lane may execute until the gate is green.')
    still_authorized, reason = validate_recovery_task(recovery_id)
    if not still_authorized:
        deny(f'Blocked: recovery-only authority expired or is invalid for {recovery_id}: {reason}.')

# Gate 2. Retired/backup material is recovery-only, not normal evidence. Direct file tools
# and Bash are blocked from reaching these paths. Recovery is performed outside Claude or
# only after an explicit temporary guard change authorized by Christopher.
FORBIDDEN_PREFIXES = (
    '/home/chris/.hermes/hermes-agent/',
    '/home/chris/.hermes/backups/',
    '/home/chris/.hermes/secrets/',
    '/home/chris/.hermes/recovered/',
    '/home/chris/.hermes/secure/',
    '/home/chris/.ssh/',
    '/home/chris/.gnupg/',
    '/home/chris/archive/',
    '/home/chris/hermes/repos/NorCal_Hermes/hermes_profiles_backup/',
)
FORBIDDEN_EXACT = {
    '/home/chris/.hermes/.env',
    '/home/chris/.hermes/auth.json',
    '/home/chris/.hermes/google_client_secret.json',
    '/home/chris/.hermes/google_token.json',
    '/home/chris/.git-credentials',
    '/home/chris/.claude/.credentials.json',
}
RETIRED_NAMES = re.compile(r'(^|[/_.-])norcal[-_ ]?ops([/_.-]|$)', re.I)
BAK_COMPONENT = re.compile(r'(^|/)[^/]*\.bak(?:[-._][^/]*)?($|/)', re.I)
ARCHIVE_COMPONENT = re.compile(
    r'(^|/)[^/]*\.(?:zip|7z|tgz|tar|tar\.gz|tar\.bz2|tar\.xz)(?:$|/)',
    re.I,
)


def path_forbidden(value: str):
    v = value.strip()
    if not v:
        return None
    # Normalize only obvious absolute filesystem strings. Keep original for pattern checks.
    if v in FORBIDDEN_EXACT:
        return f'credential-bearing path {v}'
    for prefix in FORBIDDEN_PREFIXES:
        root = prefix.rstrip('/')
        if v == root or v.startswith(prefix):
            return f'protected path {v}'
    if RETIRED_NAMES.search(v):
        return f'retired predecessor-system reference {v}'
    if BAK_COMPONENT.search(v):
        return f'backup-suffixed path {v}'
    if ARCHIVE_COMPONENT.search(v):
        return f'archive-container path {v}'
    return None

# File-oriented tool paths.
for key in ('file_path', 'path', 'notebook_path'):
    val = inputs.get(key)
    if isinstance(val, str):
        why = path_forbidden(val)
        if why:
            deny(f'Blocked: {why}. Backups, archives, and retired predecessor artifacts are excluded from normal Claude discovery and evidence. Use the canonical live path instead.')

# Glob/Grep patterns can explicitly traverse retired material even when their base path is
# broad. Block only when the pattern itself names a recovery/backup target.
for key in ('pattern', 'glob'):
    val = inputs.get(key)
    if isinstance(val, str):
        low = val.lower()
        archive_tokens = ('.zip', '.7z', '.tgz', '.tar', '.tar.gz', '.tar.bz2', '.tar.xz')
        if (
            'norcal-ops' in low
            or 'norcal_ops' in low
            or '.bak' in low
            or 'hermes_profiles_backup' in low
            or any(token in low for token in archive_tokens)
        ):
            deny('Blocked: search pattern explicitly targets retired/backup/archive material. Normal Claude discovery must use canonical live files only.')

# Bash needs string-level path screening because it can bypass Read/Grep/Glob.
if tool == 'Bash':
    cmd = str(inputs.get('command') or '')
    low = cmd.lower()
    hard_tokens = (
        '/home/chris/.hermes/hermes-agent/',
        '/home/chris/.hermes/backups/',
        '/home/chris/.hermes/secrets/',
        '/home/chris/.hermes/recovered/',
        '/home/chris/.hermes/secure/',
        '/home/chris/.ssh/',
        '/home/chris/.gnupg/',
        '/home/chris/.hermes/.env',
        '/home/chris/.hermes/auth.json',
        '/home/chris/.hermes/google_client_secret.json',
        '/home/chris/.hermes/google_token.json',
        '/home/chris/.claude/.credentials.json',
        '/home/chris/archive/',
        '/home/chris/hermes/repos/norcal_hermes/hermes_profiles_backup/',
        'norcal-ops',
        'norcal_ops',
    )
    if any(t in low for t in hard_tokens):
        deny('Blocked: Bash command references a retired or recovery-only path. Retired runtime, archive, and backup trees cannot be used as normal Claude evidence.')
    # Match path-like .bak references but do not block prose such as a commit message that
    # merely contains the word "backup".
    if re.search(r'(?:^|[\s"\'=])(?:/[^\s"\']*|\.\.?/[^\s"\']*)\.bak(?:[-._][^\s"\']*)?', cmd, re.I):
        deny('Blocked: Bash command references a .bak artifact. Sibling backups are recovery-only and excluded from normal Claude evidence.')
    if re.search(
        r'(?:^|[\s"\'=])(?:/[^\s"\']*|\.\.?/[^\s"\']*)\.(?:zip|7z|tgz|tar|tar\.gz|tar\.bz2|tar\.xz)(?=$|[\s"\'])',
        cmd,
        re.I,
    ):
        deny('Blocked: Bash command references an archive container. Archives are recovery/source artifacts and excluded from normal Claude evidence unless Christopher explicitly authorizes that artifact.')

# No stdout means no objection. Existing permission policy continues normally.
