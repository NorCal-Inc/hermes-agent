#!/usr/bin/env python3
"""Deterministic lesson-candidate review bridge.

Purpose:
- Never auto-approve global/unbounded candidates.
- Separate legacy unverified mistake-log imports from verified candidates.
- Produce a durable daily report.
- Create at most one bounded Erika review card per UTC day when VERIFIED
  candidates require operator review.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sqlite3, subprocess, sys
from pathlib import Path

DEFAULT_DB = Path.home()/'.hermes/kanban/kanban.db'
DEFAULT_REPORT_DIR = Path.home()/'.hermes/logs/lesson-candidate-review'

def classify(row: sqlite3.Row) -> str:
    source = str(row['source_task_id'] or '')
    created_by = str(row['created_by'] or '')
    if source.startswith('mistakelog:') or created_by == 'mistake-log-import':
        return 'legacy_unverified'
    if row['verification_id'] is not None:
        return 'verified_review_required'
    return 'unverified_nonlegacy'

def load_candidates(db: Path):
    c=sqlite3.connect(db); c.row_factory=sqlite3.Row
    rows=list(c.execute("SELECT * FROM task_lessons WHERE state='candidate' AND active=0 ORDER BY id ASC"))
    c.close(); return rows

def review_body(rows):
    lines=[
      'Verified lesson candidates require operator review. No candidate has been auto-approved.',
      '', 'Review each candidate against its recorded verification provenance. Approve only if its broad/global interpretation is intended to bind future work.', ''
    ]
    for r in rows[:10]:
        text=' '.join(str(r['lesson'] or '').split())
        if len(text)>500: text=text[:497]+'...'
        lines.append(f"- Lesson {r['id']} | source {r['source_task_id']} | verification {r['verification_id']} | applicability {r['applicability']} | {text}")
    if len(rows)>10:
        lines.append(f"- {len(rows)-10} additional verified candidates remain queued for a later bounded review batch.")
    lines += ['', 'Acceptance: review is explicit; no global/all lesson is activated without named operator approval; provenance remains intact.']
    return '\n'.join(lines)

def create_review_task(rows, day):
    if not rows: return None
    cmd=[
      str(Path.home()/'.hermes/hermes-agent-next/venv/bin/python'), '-m','hermes_cli.main','kanban','create',
      'Review verified lesson candidates', '--body', review_body(rows), '--assignee','erika','--triage',
      '--max-runtime','300', '--created-by','lesson-candidate-review',
      '--idempotency-key',f'lesson-candidate-review:{day}', '--json'
    ]
    env=os.environ.copy(); env['HOME']=str(Path.home()); env['HERMES_HOME']=str(Path.home()/'.hermes')
    p=subprocess.run(cmd, env=env, cwd=str(Path.home()/'.hermes/hermes-agent-next'), text=True, capture_output=True, timeout=45)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip())
    try: return json.loads(p.stdout)
    except Exception: return {'raw':p.stdout.strip()}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db', type=Path, default=DEFAULT_DB)
    ap.add_argument('--report-dir', type=Path, default=DEFAULT_REPORT_DIR)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--self-test', action='store_true')
    args=ap.parse_args()
    if args.self_test:
        class R(dict):
            __getattr__=dict.__getitem__
        samples=[
          ({'source_task_id':'mistakelog:x','created_by':'mistake-log-import','verification_id':None},'legacy_unverified'),
          ({'source_task_id':'t_x','created_by':'kernel','verification_id':44},'verified_review_required'),
          ({'source_task_id':'other:x','created_by':'import','verification_id':None},'unverified_nonlegacy'),
        ]
        for d,want in samples:
            got=classify(d)
            assert got==want,(got,want)
        print('SELF_TEST=PASS'); return 0
    rows=load_candidates(args.db)
    buckets={k:[] for k in ('legacy_unverified','verified_review_required','unverified_nonlegacy')}
    for r in rows: buckets[classify(r)].append(r)
    now=dt.datetime.now(dt.timezone.utc); day=now.strftime('%Y-%m-%d')
    report={
      'generated_at':now.isoformat(), 'total_candidates':len(rows),
      'legacy_unverified':len(buckets['legacy_unverified']),
      'verified_review_required':len(buckets['verified_review_required']),
      'unverified_nonlegacy':len(buckets['unverified_nonlegacy']),
      'auto_approved':0, 'review_task':None,
      'verified_candidate_ids':[int(r['id']) for r in buckets['verified_review_required'][:10]],
    }
    if buckets['verified_review_required'] and not args.dry_run:
        report['review_task']=create_review_task(buckets['verified_review_required'], day)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    latest=args.report_dir/'latest.json'; dated=args.report_dir/f'{day}.json'
    payload=json.dumps(report,indent=2,sort_keys=True)+'\n'
    latest.write_text(payload); dated.write_text(payload)
    print(json.dumps(report,sort_keys=True))
    return 0
if __name__=='__main__':
    raise SystemExit(main())
