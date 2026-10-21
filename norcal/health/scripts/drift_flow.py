#!/usr/bin/env python3
"""drift_flow.py -- shared alert / repair / close engine for drift monitors.

Implements ~/.hermes/doctrine/synchronization.md "Drift repair -- alert, repair,
close" (doctrine 2.37) with Christopher's 2026-10-16 #alerts rules (t_38093000):
one "drift found, repair started" message per drift, then one "Drift repaired:
<what> now matches." message when the monitor's own check comes back clean.
No reminders and no reposts while the same drift stays open. There is no
attempt cap: if a repair card ends and the drift is still there, a new repair
card is opened silently, carrying the previous attempt so it must change
approach. "Needs you" is posted only when no repair card could be opened, or
when the repair worker records a decision only Christopher can make.

A monitor pipes its findings into `process` as tab-separated lines:
    F<TAB>kind<TAB>subject<TAB>detail     one line per finding
    C<TAB>subject                         one line per subject it checked
    H<TAB>kind<TAB>subject<TAB>detail     the monitor fixed this itself (ledger only, no message)
Kinds: ahead, behind, diverged, dirty, check_failed, missing, mismatch.
`process` prints the Telegram text to stdout (empty when there is nothing new),
so a no_agent cron job delivers it through its normal channel.

The Claude Code repair task uses:
    drift_flow.py note --fp FP --text "what was done"
    drift_flow.py human-needed --fp FP --text "what is wrong / decision / recommendation"
and then reruns the monitor so its next check confirms or delivers the message.
"""
import argparse, datetime, json, os, subprocess, sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("DRIFT_FLOW_STATE_DIR", Path.home() / ".hermes/state/drift-flow"))
STATE = STATE_DIR / "state.json"
LEDGER = STATE_DIR / "ledger.jsonl"
HERMES = os.environ.get("DRIFT_FLOW_HERMES", "/home/chris/.local/bin/hermes")
KANBAN_DB = os.environ.get("HERMES_KANBAN_DB", str(Path.home() / ".hermes/kanban.db"))
# Repair card states that mean the repair finished without the drift clearing.
REPAIR_ENDED = {"done", "archived"}
VAULT_DIRTY_SUBJECT = "services/life-wiki/vault"
VAULT_DIRTY_GRACE_SECONDS = 20 * 60

# Kinds that describe the same sync relationship share one fingerprint, so a
# change of direction (ahead -> diverged) reads as "changed", never "resolved".
GROUP = {"ahead": "sync", "behind": "sync", "diverged": "sync", "wrong_branch": "sync",
         "dirty": "dirty", "check_failed": "check", "missing": "missing", "mismatch": "mismatch",
         "ledger_mismatch": "ledger_mismatch", "trust_low": "trust_low",
         "failed_verification": "failed_verification", "verified_unchecked": "failed_verification"}
LABEL = {
    "ahead": "VPS ahead of GitHub",
    "behind": "VPS behind GitHub",
    "diverged": "VPS and GitHub have diverged",
    "wrong_branch": "Live checkout is on a side branch",
    "dirty": "Uncommitted changes on the VPS",
    "check_failed": "Check failed, state unknown",
    "missing": "Checkout missing",
    "mismatch": "Service runs from a different folder",
    "ledger_mismatch": "Dashboard task list does not match the Hermes board",
    "trust_low": "Erika trust below 80% for over 24 hours",
    "failed_verification": "Failed verification with no repair owner",
    "verified_unchecked": "Verified without an independent check",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def load():
    try:
        return json.loads(STATE.read_text())
    except FileNotFoundError:
        return {}


def save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE)


def ledger(event, fp, rec, **extra):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    row = {"at": iso(now()), "event": event, "fp": fp, "monitor": rec.get("monitor"),
           "subject": rec.get("subject"), "kind": rec.get("kind"), "detail": rec.get("detail")}
    row.update(extra)
    with LEDGER.open("a") as f:
        f.write(json.dumps(row) + "\n")


def name(subject):
    return subject.rstrip("/").rsplit("/", 1)[-1]


def duration(start):
    secs = int((now() - parse(start)).total_seconds())
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def task_status(task_id):
    """Status of a repair card, read-only; None when unknown."""
    if not task_id:
        return None
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5)
        try:
            row = c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            c.close()
    except Exception:
        return None
    return row[0] if row else None


def handoff(fp, rec, rerun):
    """Route vault maintenance to the existing OpenAI Hermes worker; retain other lanes."""
    if os.environ.get("DRIFT_FLOW_NO_HANDOFF"):
        return None, "hand-off disabled (DRIFT_FLOW_NO_HANDOFF)"
    me = os.path.abspath(__file__)
    attempt = rec.get("attempt", 1)
    retry = ""
    if attempt > 1:
        retry = (
            f"REPAIR ATTEMPT {attempt}: the previous repair card {rec.get('prev_task') or 'unknown'} ended "
            "but the monitor still sees this drift. Previous note: "
            f"{rec.get('note') or 'none recorded'}. Diagnose why that did not work and use a different "
            "approach; do not repeat the same change.\n\n"
        )
    body = retry + (
        f"Drift repair under ~/.hermes/doctrine/synchronization.md, section \"Drift repair -- alert, repair, close\" "
        f"(standing authorization from Christopher, doctrine 2.37). Read that section first, including the step 3 limits.\n\n"
        f"Monitor: {rec['monitor']}\nSubject: {rec['subject']}\nKind: {rec['kind']} ({LABEL.get(rec['kind'], rec['kind'])})\n"
        f"Detail: {rec['detail']}\nFingerprint: {fp}\n\n"
        "Do this:\n"
        "1. Confirm the drift against live state (fresh fetch) and find the cause.\n"
        "2. Repair it if it is within the standing authorization. Company repos: move existing commits and repair access "
        "with that company's own credential only; do not edit or read company product code or private content.\n"
        f"3. Record what you did: python3 {me} note --fp '{fp}' --text '<what was done>'\n"
        f"4. Only if a step 3 limit applies (a decision only Christopher can make): python3 {me} human-needed --fp '{fp}' "
        "--text '<what is wrong; what you checked; decision needed; your recommendation>'. "
        "Otherwise keep diagnosing and repairing; there is no attempt cap.\n"
        f"5. Rerun the monitor so it confirms or delivers the message: {rerun}\n"
        "Do not send Telegram messages yourself; the monitor sends the resolved or human-needed message. "
        "Never print credentials."
    )
    key = f"drift:{fp}:{rec['alerted_at']}" + (f":{attempt}" if attempt > 1 else "")
    title = f"Repair drift: {name(rec['subject'])} ({LABEL.get(rec['kind'], rec['kind'])})"
    env = dict(os.environ, HOME=str(Path.home()), PATH=f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin",
               # A timer, not Christopher: record the card as system-created.
               HERMES_ACTOR_KIND="system", HERMES_ACTOR_ID="drift-flow")
    try:
        lane_args = [] if rec["subject"] == VAULT_DIRTY_SUBJECT else ["--executor-lane", "claude"]
        p = subprocess.run([HERMES, "kanban", "create", title, "--body", body, "--assignee", "default",
                            *lane_args, "--idempotency-key", key, "--max-runtime", "20m",
                            "--max-retries", "1", "--created-by", "drift-flow", "--json"],
                           env=env, text=True, capture_output=True, timeout=90)
    except Exception as e:
        return None, f"hand-off failed: {type(e).__name__}"
    if p.returncode != 0:
        return None, f"hand-off failed: {(p.stderr or p.stdout).strip().splitlines()[-1:] or ['exit ' + str(p.returncode)]}"
    # `kanban create --json` pretty-prints one object over many lines; parse the
    # whole reply first, then fall back to a trailing single-line JSON object.
    try:
        task = json.loads(p.stdout).get("id")
        if task:
            return task, None
    except (ValueError, AttributeError):
        pass
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            task = json.loads(line).get("id")
        except (ValueError, AttributeError):
            continue
        if task:
            return task, None
    return None, "hand-off created but returned no task id"


def process(monitor, rerun, lines):
    state = load()
    t = now()
    findings, checked = {}, set()
    for raw in lines:
        parts = raw.rstrip("\n").split("\t")
        if parts[0] == "F" and len(parts) >= 4:
            kind, subject, detail = parts[1], parts[2], "\t".join(parts[3:])
            fp = f"{GROUP.get(kind, kind)}:{subject}"
            findings.setdefault(fp, {"kind": kind, "subject": subject, "detail": detail})
        elif parts[0] == "C" and len(parts) >= 2:
            checked.add(parts[1])
        elif parts[0] == "H" and len(parts) >= 4:
            # Self-healed by the monitor's own deterministic repair: audit row only.
            # No alert and no repair card; the monitor emits C for the subject too.
            kind, subject = parts[1], parts[2]
            ledger("self_healed", f"{GROUP.get(kind, kind)}:{subject}",
                   {"monitor": monitor, "subject": subject, "kind": kind, "detail": "\t".join(parts[3:])})
    failed = {f["subject"] for f in findings.values() if f["kind"] == "check_failed"}
    out = []

    for fp, f in findings.items():
        rec = state.get(fp)
        if f["kind"] == "dirty" and f["subject"] == VAULT_DIRTY_SUBJECT and (
                rec is None or rec.get("pending")):
            if rec is None:
                rec = {**f, "monitor": monitor, "first_seen": iso(t),
                       "pending": True, "observations": 0}
                state[fp] = rec
            rec["detail"] = f["detail"]
            rec["observations"] += 1
            age = (t - parse(rec["first_seen"])).total_seconds()
            if age < VAULT_DIRTY_GRACE_SECONDS or rec["observations"] < 2:
                ledger("settling", fp, rec, age_seconds=age)
                continue
        if rec and not rec.get("pending"):
            # Same drift still open: never repost (t_38093000).
            if rec["kind"] != f["kind"]:
                ledger("changed", fp, rec, new_kind=f["kind"], new_detail=f["detail"])
                rec["kind"] = f["kind"]
            rec["detail"] = f["detail"]
            if rec.get("human_needed"):
                continue
            if not rec.get("task"):
                # Earlier hand-off failed (Needs-you already posted): retry.
                task, err = handoff(fp, rec, rerun)
                if task:
                    rec["task"] = task
                    ledger("repair_started", fp, rec, task=task)
                    out.append(f"🔧 Drift repair started: {name(f['subject'])}\n"
                               f"{LABEL.get(rec['kind'], rec['kind'])}. {f['detail']}\n"
                               f"Repair task {task}.")
                continue
            if task_status(rec["task"]) in REPAIR_ENDED:
                # Repair finished but the drift is still here: next repair,
                # silently, carrying the previous attempt (no cap).
                rec["prev_task"] = rec["task"]
                rec["attempt"] = rec.get("attempt", 1) + 1
                task, err = handoff(fp, rec, rerun)
                rec["task"] = task
                ledger("repair_restarted", fp, rec, task=task, prev_task=rec["prev_task"],
                       attempt=rec["attempt"], handoff_error=err)
                if not task:
                    out.append(f"⚠️ Drift repair could NOT restart: {name(f['subject'])}\n"
                               f"{LABEL.get(rec['kind'], rec['kind'])}. {err}. Needs you.")
            continue
        first_seen = rec["first_seen"] if rec else iso(t)
        rec = {"monitor": monitor, "subject": f["subject"], "kind": f["kind"], "detail": f["detail"],
               "first_seen": first_seen, "alerted_at": iso(t), "attempt": 1}
        task, err = handoff(fp, rec, rerun)
        rec["task"] = task
        state[fp] = rec
        ledger("detected", fp, rec, task=task, handoff_error=err)
        tail = f"Repair started (task {task})." if task else \
            f"Repair could NOT start: {err}. Needs you."
        out.append(f"⚠️ Drift found: {name(f['subject'])}\n{LABEL.get(f['kind'], f['kind'])}. {f['detail']}\n{tail}")

    for fp in list(state):
        rec = state[fp]
        if fp in findings or rec["subject"] not in checked or rec["subject"] in failed:
            continue
        if rec.get("pending"):
            ledger("settled", fp, rec, duration=duration(rec["first_seen"]))
            del state[fp]
            continue
        ledger("resolved", fp, rec, duration=duration(rec["first_seen"]))
        msg = (f"✅ Drift repaired: {name(rec['subject'])} now matches.\n"
               f"Was: {LABEL.get(rec['kind'], rec['kind'])}. Open {duration(rec['first_seen'])}.")
        if rec.get("note"):
            msg += f"\nWhat was done: {rec['note']}"
        out.append(msg)
        del state[fp]

    for fp, rec in state.items():
        hn = rec.get("human_needed")
        if hn and not hn.get("delivered"):
            hn["delivered"] = iso(t)
            ledger("human_needed_delivered", fp, rec)
            out.append(f"🙋 Needs you: {name(rec['subject'])}\n{LABEL.get(rec['kind'], rec['kind'])}. {hn['text']}")

    save(state)
    if out:
        print("\n\n".join(out))


def last_closed_episode(fp):
    """The ledger's last closure (resolved/settled) for fp, if it hasn't been reopened since.

    Scans the immutable ledger backwards (never touches `state`, so it can't
    reopen or fabricate a drift). A worker's `note` call can legitimately land
    after the monitor's own check already closed the episode -- the repair and
    the next clean observation race independently, and the delay between them
    is not bounded. Returns None once a newer non-closure event for the same
    fp (a fresh detection/change) shows it was reopened; a run of trailing
    post-resolution notes on top of the closure is skipped over so repeated
    `note` calls keep finding the same closure.
    """
    try:
        lines = LEDGER.read_text().splitlines()
    except FileNotFoundError:
        return None
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if row.get("fp") != fp:
            continue
        event = row.get("event")
        if event in ("resolved", "settled"):
            return {"monitor": row.get("monitor"), "subject": row.get("subject"),
                    "kind": row.get("kind"), "detail": row.get("detail"), "at": row.get("at")}
        if event == "note" and row.get("post_resolution"):
            continue  # an earlier post-resolution note; keep looking for the closure itself
        return None  # most recent event for this fp is something else (e.g. still open/reopened)
    return None


def mark(fp, field, text):
    state = load()
    rec = state.get(fp)
    if rec:
        if field == "human_needed":
            rec["human_needed"] = {"text": text, "at": iso(now())}
            ledger("human_needed", fp, rec, text=text)
        else:
            rec["note"] = text
            ledger("note", fp, rec, text=text)
        save(state)
        print(f"recorded {field} for {fp}")
        return
    if field == "note":
        closed = last_closed_episode(fp)
        if closed:
            ledger("note", fp, closed, text=text, post_resolution=True)
            print(f"recorded note for {fp} (resolved {duration(closed['at'])} ago)")
            return
    sys.exit(f"no open drift with fingerprint {fp!r}; open: {', '.join(state) or 'none'}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("process"); p.add_argument("--monitor", required=True); p.add_argument("--rerun", required=True)
    for c in ("note", "human-needed"):
        q = sub.add_parser(c); q.add_argument("--fp", required=True); q.add_argument("--text", required=True)
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "process":
        process(a.monitor, a.rerun, sys.stdin.readlines())
    elif a.cmd == "note":
        mark(a.fp, "note", a.text)
    elif a.cmd == "human-needed":
        mark(a.fp, "human_needed", a.text)
    else:
        print(json.dumps(load(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
