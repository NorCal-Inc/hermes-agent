#!/usr/bin/env bash
set -euo pipefail

VAULT_ROOT="${VAULT_ROOT:-/home/chris/services/life-wiki/vault}"
APP_ENV="${HERMES_APP_ENV:-/home/chris/.hermes/secrets/NorCal_Hermes.env}"
PYTHON_BIN="${HERMES_PYTHON_BIN:-/home/chris/.hermes/venvs/NorCal_Hermes-prod/bin/python}"
export VAULT_ROOT APP_ENV

"$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
from urllib.parse import urlparse, unquote

vault = Path(os.environ["VAULT_ROOT"])
app_env = Path(os.environ["APP_ENV"])
if not app_env.is_file():
    raise SystemExit(f"Hermes app env not found: {app_env}")

database_url = os.environ.get("DATABASE_URL", "")
if not database_url:
    for raw in app_env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("DATABASE_URL="):
            database_url = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
if not database_url:
    raise SystemExit("DATABASE_URL is not configured for Hermes PostgreSQL")

import pg8000
u = urlparse(database_url)
conn = pg8000.connect(
    user=unquote(u.username or ""),
    password=unquote(u.password or ""),
    host=u.hostname or "localhost",
    port=u.port or 5432,
    database=(u.path or "/").lstrip("/") or "postgres",
)

def query(sql):
    cur = conn.cursor()
    cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

for required in ("incidents", "tasks"):
    rows = query(
        "SELECT 1 FROM information_schema.tables "
        f"WHERE table_schema='public' AND table_name='{required}'"
    )
    if not rows:
        raise SystemExit(f"Required Hermes PostgreSQL table missing: {required}")

paths = {
    "ENT-001": "North-Caledonia-Inc",
    "ENT-002": "Orion-Formation-Services",
    "ENT-003": "Logos-Covenant-Inc",
    "ENT-004": "TripTracker",
    "ENT-005": "NCASS",
    "ENT-006": "CLHubbard-Transportation",
}
aliases = {
    "ent-001": "ENT-001", "nor cal": "ENT-001", "norcal": "ENT-001", "north caledonia": "ENT-001", "infrastructure": "ENT-001", "operations": "ENT-001",
    "ent-002": "ENT-002", "orion": "ENT-002", "orion formation services": "ENT-002",
    "ent-003": "ENT-003", "logos": "ENT-003", "logos covenant": "ENT-003",
    "ent-004": "ENT-004", "triptracker": "ENT-004", "triptracker technologies": "ENT-004",
    "ent-005": "ENT-005", "ncass": "ENT-005",
    "ent-006": "ENT-006", "clhubbard": "ENT-006", "clhubbard transportation": "ENT-006",
}

def company_id(value):
    return aliases.get((value or "").strip().lower())

def clean(value):
    if value is None or value == "":
        return "—"
    return str(value).replace("\r", " ").replace("\n", " ").strip()

# Canonical company lanes. The legacy companies table mislabels ENT-006 as
# "Life Wiki" while the entities table and vault identify ENT-006 as
# CLHubbard Transportation, so do not propagate that stale name.
active = {
    "ENT-001": "North Caledonia",
    "ENT-002": "Orion",
    "ENT-003": "Logos Covenant",
    "ENT-004": "TripTracker",
    "ENT-005": "NCASS",
    "ENT-006": "CLHubbard Transportation",
}
items = {cid: [] for cid in active}

for row in query("SELECT * FROM incidents ORDER BY created_at,id"):
    cid = company_id(row["entity"])
    if cid not in items:
        continue
    marker = f"hermes-incident:{row['id']}"
    body = (
        f"\n<!-- {marker} -->\n"
        f"### {clean(row['created_at'])} — Incident {clean(row['id'])}\n"
        f"- Title: {clean(row['title'])}\n"
        f"- Severity / status: {clean(row['severity'])} / {clean(row['status'])}\n"
        f"- Owner: {clean(row['owner'])}\n"
        f"- Impact: {clean(row['impact'])}\n"
        f"- Evidence: {clean(row['evidence'])}\n"
        f"- Recommended action: {clean(row['recommended_action'])}\n"
        f"- Resolved at: {clean(row['resolved_at'])}\n"
    )
    items[cid].append((marker, body))

for row in query("""
    SELECT task_id,title,entity,owner,status,task_type,verification_status,
           verification_ref,verification_evidence,result_summary,
           COALESCE(verified_at,claimed_at,updated_at,created_at) AS occurred_at
      FROM tasks
     WHERE verification_source='worker_result' OR result_summary IS NOT NULL
     ORDER BY occurred_at,task_id
"""):
    cid = company_id(row["entity"])
    if cid not in items:
        continue
    marker = f"hermes-worker-result:{row['task_id']}"
    evidence = clean(row["result_summary"] or row["verification_ref"] or row["verification_evidence"])
    body = (
        f"\n<!-- {marker} -->\n"
        f"### {clean(row['occurred_at'])} — Worker result {clean(row['task_id'])}\n"
        f"- Task: {clean(row['title'])}\n"
        f"- Worker / owner: {clean(row['owner'])}\n"
        f"- Task type: {clean(row['task_type'])}\n"
        f"- Status / verification: {clean(row['status'])} / {clean(row['verification_status'])}\n"
        f"- Result: {evidence}\n"
    )
    items[cid].append((marker, body))

added = 0
summary = []
for cid, name in active.items():
    note = vault / "Business" / "Companies" / paths[cid] / "incidents.md"
    if not note.is_file():
        raise SystemExit(f"Company incident note missing: {note}")
    existing = note.read_text(encoding="utf-8")
    additions = [body for marker, body in items[cid] if marker not in existing]
    if additions:
        heading = "\n## Hermes incident and worker-result feed\n" if "## Hermes incident and worker-result feed" not in existing else ""
        with note.open("a", encoding="utf-8") as fh:
            fh.write(heading + "".join(additions))
        added += len(additions)
    summary.append(f"{name}: {len(additions)} new")

print(f"incidents-to-vault: appended {added} new entries")
for line in summary:
    print(f"- {line}")
PY
