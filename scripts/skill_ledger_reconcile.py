#!/usr/bin/env python3
"""Detect skill file mutations that bypass skill_manage and append audit entries.

This is a reconciliation safety net, not the primary mutation path. The normal
skill_manage path remains responsible for before/after content blobs. This job
records out-of-band changes with actor=external and file-level hashes so no
silent skill drift survives beyond the reconciliation interval.
"""
from __future__ import annotations
import hashlib, json, os, sys, time
from pathlib import Path

HERMES_HOME=Path(os.environ.get("HERMES_HOME", str(Path.home()/".hermes"))).resolve()
SKILLS=HERMES_HOME/"skills"
STATE=SKILLS/".ledger_snapshot.json"
RUNTIME=HERMES_HOME/"hermes-agent-next"
sys.path.insert(0,str(RUNTIME))
from tools import skill_ledger

EXCLUDED_TOP={".archive",".curator_backups"}
EXCLUDED_FILES={".usage.json",".curator_state",".curator_ledger.jsonl",".ledger_snapshot.json",".bundled_manifest"}

def sha(p:Path)->str:
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def skill_roots():
    out=[]
    for p in SKILLS.rglob('SKILL.md'):
        try: rel=p.relative_to(SKILLS)
        except ValueError: continue
        if rel.parts and rel.parts[0] in EXCLUDED_TOP: continue
        out.append(p.parent)
    return sorted(set(out))

def snapshot():
    snap={}
    for d in skill_roots():
        relroot=str(d.relative_to(SKILLS))
        files={}
        for p in sorted(d.rglob('*')):
            if not p.is_file(): continue
            rel=str(p.relative_to(d))
            if p.name in EXCLUDED_FILES or '/.git/' in f'/{rel}/': continue
            files[rel]=sha(p)
        snap[relroot]=files
    return snap

def load_state():
    if not STATE.exists(): return None
    try:return json.loads(STATE.read_text())
    except Exception:return None

def save_state(s):
    tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s,sort_keys=True,indent=2)+'\n')
    os.replace(tmp,STATE)

def main():
    cur=snapshot(); prev=load_state()
    if prev is None:
        save_state(cur); print(f'BASELINE skills={len(cur)} changes=0'); return 0
    changes=0
    names=sorted(set(prev)|set(cur))
    for relroot in names:
        before=prev.get(relroot,{}) or {}; after=cur.get(relroot,{}) or {}
        if before==after: continue
        changed=sorted(k for k in set(before)|set(after) if before.get(k)!=after.get(k))
        evidence={"reconciler":"skill_ledger_reconcile.py","skill_path":relroot,"changed_files":changed,"detected_at":int(time.time())}
        before_list=[{"path":str(SKILLS/relroot/k),"sha256":v} for k,v in sorted(before.items())]
        after_list=[{"path":str(SKILLS/relroot/k),"sha256":v} for k,v in sorted(after.items())]
        action='external_delete' if relroot not in cur else ('external_create' if relroot not in prev else 'external_change')
        eid=skill_ledger.append_entry(action, Path(relroot).name, before=before_list, after=after_list, actor='external', evidence=evidence)
        if eid: changes+=1
    save_state(cur)
    print(f'RECONCILED skills={len(cur)} changes={changes}')
    return 0
if __name__=='__main__': raise SystemExit(main())
