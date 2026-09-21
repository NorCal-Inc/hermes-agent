#!/usr/bin/env python3
"""Deterministic lesson-candidate review bridge.

Purpose:
- Never auto-approve global/unbounded candidates.
- Separate legacy unverified mistake-log imports from verified candidates.
- Produce a durable daily report.
- Do not manufacture Kanban work merely because review candidates exist.
"""
from __future__ import annotations
import argparse, datetime as dt, json, sqlite3
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
    args.report_dir.mkdir(parents=True, exist_ok=True)
    latest=args.report_dir/'latest.json'; dated=args.report_dir/f'{day}.json'
    payload=json.dumps(report,indent=2,sort_keys=True)+'\n'
    latest.write_text(payload); dated.write_text(payload)
    print(json.dumps(report,sort_keys=True))
    return 0
if __name__=='__main__':
    raise SystemExit(main())
