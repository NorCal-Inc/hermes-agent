#!/usr/bin/env python3
"""Feed the vault's mistake log into ``task_lessons`` as non-binding candidates.

The two halves of this system never touched. ``task_lessons`` is *enforced* --
``build_worker_context`` injects every active lesson into every worker as a
binding constraint -- but it was empty. ``Jobs/mistake-logging.md`` is
*populated* -- 191 rows, 25% of which say outright that they repeat an earlier
row -- but nothing reads it: no code anywhere opens that file. Agents were told
to read it and kept re-making the same mistakes; the mechanism that could
actually bind them had nothing in it.

This is the one-way bridge.

WHY CANDIDATES, NEVER ACTIVE
----------------------------
``execution-honesty.md``: "Only verified lessons may become durable canonical
learning. Failed or unresolved attempts remain observations/candidates." A
mistake-log row has no verified source task behind it -- it is an agent's
own account of an error. Importing such a row as *binding* would launder an
unverified claim into canon, which is exactly what ``promote_lesson``'s gates
exist to stop.

So every row lands as ``state='candidate', active=0``: recorded, attributable,
queryable, and constraining nothing until a named operator approves it with
``kanban lesson approve``. Approval is where a human decides a rule should bind
future work. That decision is not this script's to make.

WHY THIS IS NOT A LOOP
----------------------
Deliberately bounded, and worth stating because a self-feeding learning system
is a real failure mode:

* **One direction.** Reads the vault file, writes the board. Never writes back
  to the mistake log, so an imported lesson can never re-enter as a new row.
* **Idempotent.** Provenance id is a hash of the rule text, so re-running
  imports nothing new. Safe to run after every vault edit.
* **Manual.** No scheduler, no hook, no dispatcher tick. It runs when someone
  runs it.
* **Inert on arrival.** ``active=0`` binds no work, so an import cannot change
  agent behaviour, so it cannot generate the next mistake that becomes the next
  import. The cycle only advances when a human approves something.

Usage::

    python3 scripts/import_mistake_log_lessons.py --dry-run
    python3 scripts/import_mistake_log_lessons.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_LOG = Path(
    "/home/chris/services/life-wiki/vault/Jobs/mistake-logging.md"
)
DEFAULT_DB = Path.home() / ".hermes" / "kanban.db"

#: Rule text longer than this is truncated on a sentence boundary. The kernel's
#: own limit is 4000; a binding rule that does not fit in a paragraph is a
#: document and belongs on a card, so we stay well under it and link out.
MAX_RULE_CHARS = 1200

CREATED_BY = "mistake-log-import"
PROVENANCE_PREFIX = "mistakelog:"

ROW_RE = re.compile(r"^\| (\d{4}-\d{2}-\d{2}) \|")

#: A well-formed row splits to exactly this many fragments:
#: '' | date | what | rule | resolution | account | ''
EXPECTED_CELLS = 7


def split_cells(line: str) -> list[str]:
    """Split a markdown row on unescaped pipes.

    ``grep -v '/tests/'``-class bug, avoided deliberately: a naive ``.split('|')``
    breaks every row whose text contains a pipe (``env | grep``), and there are
    four such rows in this file today.
    """
    return re.split(r"(?<!\\)\|", line)


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = text.replace("\\|", "|")
    return " ".join(text.split()).strip()


def truncate(text: str, limit: int = MAX_RULE_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = cut.rfind(". ")
    if dot > limit // 2:
        cut = cut[: dot + 1]
    return cut.rstrip() + " […]"


def parse_rows(path: Path, skipped: list | None = None) -> list[dict]:
    """Extract (date, rule, resolution, account) for every indexed mistake.

    ``skipped`` collects ``(line_number, cell_count)`` for each row refused
    for having an unexpected width, so the caller can report them loudly
    instead of letting a malformed row vanish silently.
    """
    out: list[dict] = []
    if skipped is None:
        skipped = []
    for lineno, line in enumerate(path.read_text().split("\n"), 1):
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = split_cells(line.rstrip())
        # STRICT: exactly ``| date | what | rule | resolution | account |``,
        # which splits to EXPECTED_CELLS fragments (leading/trailing empties
        # included).
        #
        # Indexing from either end is a guess, and both guesses have already
        # been wrong on this file. From the START, an unescaped pipe in the
        # "what" cell shifts the rule. From the END, a row still in the OLD
        # 4-column format (no resolution column) puts the rule index on "what"
        # -- which happened: another session appended one and its narrative
        # imported as a rule. A pipe inside the rule cell breaks the end-index
        # too.
        #
        # No indexing scheme survives an unknown width, so a row of unexpected
        # width is REFUSED and reported rather than guessed at. These rows
        # become candidate rules an operator may approve into binding force;
        # importing the wrong cell is worse in every case than importing
        # nothing and saying so out loud.
        if len(cells) != EXPECTED_CELLS:
            skipped.append((lineno, len(cells)))
            continue
        rule = strip_markdown(cells[-4])
        if not rule:
            continue
        out.append(
            {
                "line": lineno,
                "date": m.group(1),
                "rule": truncate(rule),
                "resolution": strip_markdown(cells[-3]),
                "account": strip_markdown(cells[-2]),
            }
        )
    return out


def provenance_id(rule: str) -> str:
    """Stable id derived from the rule text -- this is what makes it idempotent."""
    return PROVENANCE_PREFIX + hashlib.sha256(rule.encode()).hexdigest()[:16]


def existing_ids(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT source_task_id FROM task_lessons WHERE source_task_id LIKE ?",
            (PROVENANCE_PREFIX + "%",),
        )
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"mistake log not found: {args.log}", file=sys.stderr)
        return 2
    if not args.db.exists():
        print(f"board not found: {args.db}", file=sys.stderr)
        return 2

    skipped: list = []
    rows = parse_rows(args.log, skipped)
    if not rows:
        print("no indexed mistake rows found — refusing to proceed", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(args.db))
    conn.row_factory = sqlite3.Row
    try:
        seen = existing_ids(conn)
        new = [r for r in rows if provenance_id(r["rule"]) not in seen]
        # A row whose rule text is byte-identical to another's collapses onto
        # one candidate. That is correct: the same imperative twice is one rule.
        by_id: dict[str, dict] = {}
        for r in new:
            by_id.setdefault(provenance_id(r["rule"]), r)

        print(f"mistake log : {args.log}")
        print(f"board       : {args.db}")
        print(f"rows parsed : {len(rows)}")
        print(f"already imported: {len(seen)}")
        print(f"to import   : {len(by_id)}")
        if skipped:
            print(
                f"\nSKIPPED {len(skipped)} malformed row(s) — fix the table, "
                f"do not guess at them:"
            )
            for ln, n in skipped:
                print(f"  line {ln}: {n} cells, expected {EXPECTED_CELLS}")

        if not args.apply:
            print("\n--- DRY RUN (pass --apply to write) ---")
            for pid, r in list(by_id.items())[:5]:
                print(f"  {pid}  {r['date']}  {r['rule'][:110]}")
            if len(by_id) > 5:
                print(f"  … and {len(by_id) - 5} more")
            return 0

        now = int(time.time())
        written = 0
        with conn:
            for pid, r in by_id.items():
                evidence = {
                    "origin": "Jobs/mistake-logging.md",
                    "logged_date": r["date"],
                    "source_line": r["line"],
                    "full_account": r["account"],
                    "resolution": r["resolution"] or None,
                    "note": (
                        "Imported from the vault mistake log. No verified source "
                        "task stands behind it, so it is a CANDIDATE and binds "
                        "nothing until a named operator approves it."
                    ),
                }
                conn.execute(
                    "INSERT INTO task_lessons ("
                    " source_task_id, tenant, scope, applicability, lesson,"
                    " evidence, created_by, created_at, active, state,"
                    " review_by, retire_condition"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pid,
                        None,
                        # Agent-behaviour rules genuinely reach every lane, which
                        # is precisely why they need a human: 'global' is a
                        # structural refusal in lesson_promotion_eligibility.
                        "global",
                        "all",
                        r["rule"],
                        json.dumps(evidence, sort_keys=True),
                        CREATED_BY,
                        now,
                        0,  # binds nothing
                        "candidate",
                        None,
                        "Retire when the practice it names is enforced mechanically.",
                    ),
                )
                written += 1
        print(f"\nimported {written} candidate lesson(s), all active=0")
        total = conn.execute("SELECT COUNT(*) FROM task_lessons").fetchone()[0]
        binding = conn.execute(
            "SELECT COUNT(*) FROM task_lessons WHERE active = 1"
        ).fetchone()[0]
        print(f"task_lessons now: {total} rows, {binding} binding")
        print(
            "\nNothing binds until approved. Review with:\n"
            "  hermes kanban lesson list --state candidate\n"
            "and approve individually with:\n"
            "  hermes kanban lesson approve <id> --approver christopher"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
