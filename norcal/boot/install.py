#!/usr/bin/env python3
from pathlib import Path
import datetime as dt, hashlib, os, shutil, sys

HERE=Path(__file__).resolve().parent
HOME=Path.home()
LOCAL=HOME/'.local/bin'
CODEX=HOME/'.codex'
LOCAL.mkdir(parents=True,exist_ok=True); CODEX.mkdir(parents=True,exist_ok=True)
ts=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')

def backup(path: Path):
    if path.exists() or path.is_symlink():
        target=path.with_name(path.name+f'.bak-{ts}')
        if path.is_symlink():
            target.write_text(os.readlink(path)+'\n',encoding='utf-8')
        else:
            shutil.copy2(path,target)

def link(src: Path,dst: Path):
    if dst.is_symlink() and Path(os.readlink(dst)).resolve()==src.resolve(): return
    backup(dst)
    if dst.exists() or dst.is_symlink(): dst.unlink()
    dst.symlink_to(src)

for src,dst in [
    (HERE/'hermes-shared-boot-context', LOCAL/'hermes-shared-boot-context'),
    (HERE/'claude-boot-context', LOCAL/'boot-context'),
    (HERE/'claude-session-start-gate.py', LOCAL/'claude-session-start-gate.py'),
    (HERE/'codex-boot', LOCAL/'codex'),
]: link(src,dst)

# Codex always gets the canonical static constraints from CODEX_HOME, even if someone
# bypasses the wrapper and runs the real binary directly.
constraints=(HERE/'boot-constraints.md').read_text(encoding='utf-8')
ag=CODEX/'AGENTS.md'
managed=constraints.rstrip()+'\n<!-- NORCAL_BOOT_MANAGED_END -->\n'
old=ag.read_text(encoding='utf-8') if ag.exists() else ''
end='<!-- NORCAL_BOOT_MANAGED_END -->'
if end in old:
    after=old.split(end,1)[1]
    new=managed+after.lstrip('\n')
else:
    new=managed+('\n'+old if old.strip() else '')
if new!=old:
    backup(ag); ag.write_text(new,encoding='utf-8')

# Set model_instructions_file to the wrapper-refreshed live boot. Preserve every other key.
cfg=CODEX/'config.toml'; old=cfg.read_text(encoding='utf-8') if cfg.exists() else ''
line='model_instructions_file = "/home/chris/.codex/norcal-boot-current.md"'
lines=old.splitlines(); replaced=False; out=[]
for x in lines:
    if x.strip().startswith('model_instructions_file'):
        out.append(line); replaced=True
    else: out.append(x)
if not replaced: out.insert(0,line)
new='\n'.join(out).rstrip()+'\n'
if new!=old:
    backup(cfg); cfg.write_text(new,encoding='utf-8')

print('installed North Caledonia boot contract')
