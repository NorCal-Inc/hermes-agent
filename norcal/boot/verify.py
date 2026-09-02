#!/usr/bin/env python3
from pathlib import Path
import hashlib, os, subprocess, sys
HERE=Path(__file__).resolve().parent; HOME=Path.home()
C=(HERE/'boot-constraints.md').read_text(encoding='utf-8').strip()
errs=[]
for p in [HOME/'.local/bin/hermes-shared-boot-context', HOME/'.local/bin/boot-context', HOME/'.local/bin/claude-session-start-gate.py', HOME/'.local/bin/codex']:
    if not p.is_symlink() or p.resolve().parent!=HERE: errs.append(f'{p}: not linked to canonical boot dir')
ag=HOME/'.codex/AGENTS.md'
if not ag.exists() or not ag.read_text(encoding='utf-8').startswith(C+'\n'):
    errs.append('Codex AGENTS.md does not start with canonical constraints')
cfg=(HOME/'.codex/config.toml').read_text(encoding='utf-8')
if 'model_instructions_file = "/home/chris/.codex/norcal-boot-current.md"' not in cfg: errs.append('Codex model_instructions_file not configured')
runtime=(HOME/'.hermes/config.yaml').read_text(encoding='utf-8')
if 'gauntlet_enforcement: true' not in runtime: errs.append('North Caledonia live config does not enable kanban.gauntlet_enforcement')
session_src=(HERE/'claude-session-start-gate.py').read_text(encoding='utf-8')
claude_src=(HERE/'claude-boot-context').read_text(encoding='utf-8')
codex_src=(HERE/'codex-boot').read_text(encoding='utf-8')
helper_src=(HERE/'recovery_boot.py').read_text(encoding='utf-8')
for name, src in [('Claude SessionStart', session_src), ('Claude boot wrapper', claude_src), ('Codex boot wrapper', codex_src)]:
    if 'shared_boot_complete' not in src:
        errs.append(f'{name} does not use the canonical exact shared boot-state parser')
if '^BOOT STATUS: COMPLETE\\s*$' not in helper_src:
    errs.append('canonical shared boot-state parser is not exact-line anchored')
p=subprocess.run([str(HERE/'hermes-shared-boot-context'),'--gate-exit-code'],capture_output=True,text=True,timeout=60)
body=(p.stdout or p.stderr or '').strip()
if p.returncode!=0: errs.append(f'shared generator gate rc={p.returncode}')
if not body.startswith(C): errs.append('shared generator does not start with canonical constraints')
print('BOOT PARITY: PASS' if not errs else 'BOOT PARITY: FAIL')
print('constraints_sha256='+hashlib.sha256((C+'\n').encode()).hexdigest())
for e in errs: print('ERROR:',e)
sys.exit(1 if errs else 0)
