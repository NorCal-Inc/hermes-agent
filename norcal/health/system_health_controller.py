#!/usr/bin/env python3
"""Deterministic recursive system health controller (Kanban task t_b8d62378).

One controller above the fragmented monitors. It does not replace them: it
consumes existing health surfaces (the Kanban board, Hermes cron state, user
systemd timers, the gateway heartbeat, the canonical boot gate) and drives each
detected condition to an actionable disposition::

    OBSERVE -> CLASSIFY -> RECOVER -> REVALIDATE -> GREEN / CONTINUE
                               \\-> ESCALATE / DEGRADED

* OBSERVE runs every invariant of the requested tier (``light`` every five
  minutes, ``deep`` hourly). A check that raises is itself a finding.
* CLASSIFY decides per finding: recoverable by a pre-authorized mechanism with
  retry budget left, or not.
* RECOVER runs only the invariant's own allowlisted recovery. v1 recovers
  verifier-routing damage only (Christopher, 2026-09-14): open the independent
  verifier route, restore a relabelled subject lane, declare a deadlocked
  verifier child with the ``verifies`` relation.
* REVALIDATE reruns exactly that invariant's check. The fingerprint must be gone.
* GREEN is silent. A finding still present with budget left CONTINUES next cycle.
* ESCALATE happens exactly once per fingerprint: one governed triage card
  (stable idempotency key, system provenance, ``defect:`` authority, so nothing
  dispatches automatically) and one delivery-checked alert. Automatic mutation
  stops for that fingerprint until the condition clears.

No LLM is involved. Alerts and cards carry identifiers, statuses and failure
signatures only — never task titles, bodies, or company data.

State model. ``strict`` (the default, and what an absent ``state_model`` means)
reports GREEN or DEGRADED, and any held finding is DEGRADED. The TEMPORARY
``governed_exceptions`` model (Christopher, 2026-09-14, stabilization only; see
the README) reports GREEN, GREEN_WITH_HOLDS, RECOVERY, DEGRADED or ESCALATED:
conditions covered by a valid, owner-approved governed exception are still
observed on every pass and stay visible, but no longer force DEGRADED. Deleting
the ``state_model`` key restores strict semantics without rewriting history.

Run: ``venv/bin/python norcal/health/system_health_controller.py run --tier light``
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONTROLLER_ID = "system-health-controller"
TIER_LIGHT = "light"
TIER_DEEP = "deep"
TIERS = (TIER_LIGHT, TIER_DEEP)
TIER_INTERVAL_SECONDS = {TIER_LIGHT: 300, TIER_DEEP: 3600}
DEFAULT_MAX_ATTEMPTS = 2
MAX_ALERT_ATTEMPTS = 3
STATE_VERSION = 1

STATUS_OPEN = "open"
STATUS_ESCALATED = "escalated"
STATUS_RESOLVED = "resolved"
#: Detected on a card under a configured recovery hold: never repaired, never
#: individually escalated; folded into one escalation per hold.
STATUS_HELD = "held"
HOLD_INVARIANT = "recovery_hold"

#: ``state_model`` values. Strict is the original GREEN/DEGRADED semantics.
STATE_MODEL_STRICT = "strict"
#: TEMPORARY stabilization model (Christopher, 2026-09-14). Revert by deleting
#: ``state_model`` from the config; nothing else has to change.
STATE_MODEL_GOVERNED = "governed_exceptions"
STATE_MODELS = (STATE_MODEL_STRICT, STATE_MODEL_GOVERNED)

AGGREGATE_GREEN = "GREEN"
AGGREGATE_GREEN_WITH_HOLDS = "GREEN_WITH_HOLDS"
AGGREGATE_RECOVERY = "RECOVERY"
AGGREGATE_DEGRADED = "DEGRADED"
AGGREGATE_ESCALATED = "ESCALATED"
#: Most severe first; the pass reports the first class that has a member.
_AGGREGATE_PRECEDENCE = (
    ("escalated", AGGREGATE_ESCALATED),
    ("degraded", AGGREGATE_DEGRADED),
    ("recovery", AGGREGATE_RECOVERY),
    ("exception", AGGREGATE_GREEN_WITH_HOLDS),
)

EXCEPTION_KIND_RECOVERY_HOLD = "recovery_hold"
EXCEPTION_KIND_PRESERVED = "preserved_condition"
EXCEPTION_KINDS = (EXCEPTION_KIND_RECOVERY_HOLD, EXCEPTION_KIND_PRESERVED)
EXCEPTION_VALIDITY_INVARIANT = "governed_exception_valid"

#: Escalation reasons. The governed model classifies an escalated fingerprint
#: as ESCALATED only when automatic repair was tried and failed, or the finding
#: is unsafe; every other escalation is an actionable DEGRADED fault.
ESCALATION_NOT_RECOVERABLE = "not_recoverable"
ESCALATION_BUDGET_EXHAUSTED = "recovery_budget_exhausted"
ESCALATION_UNSAFE = "unsafe"
ESCALATION_FROZEN_UNCOVERED = "frozen_condition_not_covered"
ESCALATION_EXCEPTION_INVALID = "governed_exception_invalid"
_ESCALATED_CLASS_REASONS = (ESCALATION_BUDGET_EXHAUSTED, ESCALATION_UNSAFE)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "health-controller.json"


# ---------------------------------------------------------------------------
# Core data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Finding:
    """One detected unhealthy condition.

    ``signature`` is the normalized failure fingerprint input: it must not
    contain timestamps or counters, so the same condition keeps the same
    fingerprint across cycles and a changed condition gets a new one.
    ``detail`` carries identifiers and statuses only.
    """

    invariant: str
    subject: str
    signature: str
    detail: dict = dataclasses.field(default_factory=dict)
    recoverable: bool = False
    #: Security-sensitive or boundary-violating. Under the governed model an
    #: unsafe finding is never covered by an exception, never recovered, and
    #: escalates immediately. Not part of the fingerprint.
    unsafe: bool = False

    @property
    def condition(self) -> str:
        """``invariant|subject|signature`` — what a governed exception names."""
        return f"{self.invariant}|{self.subject}|{self.signature}"

    @property
    def fingerprint(self) -> str:
        raw = self.condition.encode()
        return hashlib.sha256(raw).hexdigest()[:16]


@dataclasses.dataclass
class RecoveryOutcome:
    applied: bool
    action: str
    detail: dict = dataclasses.field(default_factory=dict)


class Invariant:
    """A read-only check, optionally paired with an allowlisted recovery."""

    name: str = "invariant"
    tier: str = TIER_LIGHT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    #: Consecutive cycles a non-recoverable finding must persist before it
    #: escalates. >1 only for signals that are legitimately momentary.
    confirm_cycles: int = 1

    def check(self, ctx: "Context") -> list[Finding]:  # pragma: no cover - abstract
        raise NotImplementedError

    def recover(self, ctx: "Context", finding: Finding) -> Optional[RecoveryOutcome]:
        return None


@dataclasses.dataclass
class Context:
    """Everything the controller touches, injected so it can be tested."""

    state_dir: Path
    hermes_home: Path
    config: dict
    kanban: Callable[[], contextlib.AbstractContextManager]
    send_alert: Callable[[str, str], tuple[bool, str]]
    run_command: Callable[[list[str], int], tuple[int, str]]
    #: Returns ``(card_id, created_new)``. ``created_new`` is False when the
    #: idempotency key already names a live card (e.g. after lost state).
    create_card: Callable[["Context", Finding, dict, str], tuple[Optional[str], bool]]
    now: Callable[[], float] = time.time
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class StateStore:
    """Fingerprint ledger, append-only event log and per-tier heartbeats."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.path = self.dir / "state.json"
        self.ledger = self.dir / "ledger.jsonl"

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": STATE_VERSION, "fingerprints": {}, "installed_at": None}
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise RuntimeError(f"unrecognized controller state at {self.path}")
        data.setdefault("fingerprints", {})
        data.setdefault("installed_at", None)
        return data

    def save(self, state: dict) -> None:
        _atomic_write_json(self.path, state)

    def record(self, now: float, event: str, **fields: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": _iso(now), "event": event, **fields}, sort_keys=True)
        with self.ledger.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def heartbeat_path(self, tier: str) -> Path:
        return self.dir / f"heartbeat-{tier}.json"

    def write_heartbeat(self, tier: str, payload: dict) -> None:
        _atomic_write_json(self.heartbeat_path(tier), payload)

    def read_heartbeat(self, tier: str) -> Optional[dict]:
        try:
            data = json.loads(self.heartbeat_path(tier).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @contextlib.contextmanager
    def lock(self, timeout_seconds: float = 50.0) -> Iterator[None]:
        """Serialize passes: a light and a deep pass must not interleave."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "controller.lock").open("a") as fh:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("another controller pass holds the lock")
                    time.sleep(0.5)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# The controller
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PassResult:
    tier: str
    status: str  # strict: GREEN | DEGRADED; governed: see AGGREGATE_*
    findings: int
    recovered: list[str]
    escalated: list[str]
    open: list[str]
    resolved: list[str]
    held: list[str] = dataclasses.field(default_factory=list)
    state_model: str = STATE_MODEL_STRICT
    #: Governed model only: fingerprint counts per class for this tier.
    classes: dict = dataclasses.field(default_factory=dict)


def recovery_holds(config: dict) -> list[dict]:
    """Validated ``recovery_holds`` from config. Malformed holds fail closed.

    Each hold is explicit and removable: ``name``, ``task_ids``, ``reason``,
    ``authorized_by`` and ``release`` (the condition for deleting the entry).
    A finding whose subject or any identifier in its detail is a held task is
    detected and reported, but never recovered and never escalated on its own.
    """
    holds = config.get("recovery_holds") or []
    if not isinstance(holds, list):
        raise ValueError("recovery_holds must be a list")
    out = []
    for hold in holds:
        if not isinstance(hold, dict):
            raise ValueError("each recovery hold must be an object")
        missing = [k for k in ("name", "task_ids", "reason", "authorized_by", "release") if not hold.get(k)]
        ids = hold.get("task_ids")
        if missing or not isinstance(ids, list) or not all(isinstance(i, str) and i.strip() for i in ids):
            raise ValueError(f"malformed recovery hold {hold.get('name')!r}: missing/invalid {missing or ['task_ids']}")
        out.append({**hold, "task_ids": sorted({i.strip() for i in ids})})
    return out


def _finding_ids(finding: Finding) -> set:
    ids = {finding.subject}
    ids.update(v for v in finding.detail.values() if isinstance(v, str))
    return ids


def hold_for(finding: Finding, holds: list[dict]) -> Optional[dict]:
    ids = _finding_ids(finding)
    for hold in holds:
        if ids & set(hold["task_ids"]):
            return hold
    return None


# ---------------------------------------------------------------------------
# Governed-exception state model (TEMPORARY — Christopher, 2026-09-14)
# ---------------------------------------------------------------------------


def state_model(config: dict) -> str:
    """The configured state model. Unknown values fail closed (the pass raises)."""
    model = config.get("state_model", STATE_MODEL_STRICT)
    if model not in STATE_MODELS:
        raise ValueError(f"state_model must be one of {STATE_MODELS}, got {model!r}")
    return model


def exception_authorization_digest(entry: dict) -> str:
    """sha256 over an exception's scope and authorization record.

    The config pins this value, so widening the covered conditions, adding task
    ids, changing owner/authorizer/dates or the expiry without re-recording the
    digest invalidates the exception (DEGRADED). It detects unrecorded edits;
    it is not a signature and does not authenticate the author.
    """
    scope = {
        "kind": entry.get("kind"),
        "task_ids": sorted(entry.get("task_ids") or []),
        "conditions": sorted(entry.get("conditions") or []),
        "owner": entry.get("owner"),
        "authorized_by": entry.get("authorized_by"),
        "created": entry.get("created"),
        "expires_at": entry.get("expires_at"),
    }
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _exception_problem(entry: dict, seen: set, now: float) -> Optional[str]:
    if entry.get("kind") not in EXCEPTION_KINDS:
        return "unknown_kind"
    if entry["name"] in seen:
        return "duplicate_name"
    for key in ("owner", "reason", "authorized_by", "review_condition"):
        if not str(entry.get(key) or "").strip():
            return f"missing_{key}"
    try:
        _dt.date.fromisoformat(str(entry.get("created") or ""))
    except ValueError:
        return "invalid_created"
    conditions = entry.get("conditions")
    if (not isinstance(conditions, list) or not conditions
            or not all(isinstance(c, str) and len(c.split("|")) == 3 and all(c.split("|"))
                       for c in conditions)):
        return "invalid_conditions"
    if entry["kind"] == EXCEPTION_KIND_RECOVERY_HOLD:
        ids = entry.get("task_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
            return "invalid_task_ids"
    if entry.get("authorization_sha256") != exception_authorization_digest(entry):
        return "authorization_digest_mismatch"
    expires = entry.get("expires_at")
    if expires not in (None, ""):
        ts = _parse_ts(expires)
        if ts is None:
            return "invalid_expires_at"
        if now >= ts:
            return "expired"
    return None


def governed_exceptions(config: dict, now: float) -> tuple[list[dict], list[tuple[str, str]], set]:
    """Validate the governed-exception block.

    Returns ``(valid, invalid, frozen_task_ids)``. A malformed block raises (the
    pass fails and ``OnFailure=`` alerts). A malformed or expired *entry* is
    reported as an actionable ``governed_exception_valid`` finding instead, and
    covers nothing. Task ids named by any recovery-hold entry — valid or not —
    stay frozen against automatic mutation: invalidity never releases frozen work.
    """
    block = config.get("governed_exceptions")
    if not isinstance(block, dict):
        raise ValueError("state_model 'governed_exceptions' requires a governed_exceptions object")
    if block.get("temporary") is not True:
        raise ValueError("governed_exceptions must declare temporary: true")
    for key in ("authorized_by", "purpose", "revert"):
        if not str(block.get(key) or "").strip():
            raise ValueError(f"governed_exceptions is missing {key!r}")
    entries = block.get("entries")
    if not isinstance(entries, list):
        raise ValueError("governed_exceptions.entries must be a list")
    valid: list[dict] = []
    invalid: list[tuple[str, str]] = []
    frozen: set = set()
    seen: set = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            invalid.append((f"entry-{index}", "not_an_object"))
            continue
        name = str(raw.get("name") or "").strip() or f"entry-{index}"
        entry = {**raw, "name": name}
        if entry.get("kind") == EXCEPTION_KIND_RECOVERY_HOLD and isinstance(entry.get("task_ids"), list):
            frozen.update(i.strip() for i in entry["task_ids"] if isinstance(i, str) and i.strip())
        problem = "missing_name" if not str(raw.get("name") or "").strip() else _exception_problem(entry, seen, now)
        seen.add(name)
        if problem:
            invalid.append((name, problem))
        else:
            valid.append(entry)
    return valid, invalid, frozen


class _GovernanceCarrier(Invariant):
    """Carrier for controller-generated findings about the exceptions themselves."""

    max_attempts = 0

    def __init__(self, name: str, tier: str) -> None:
        self.name = name
        self.tier = tier

    def check(self, ctx: "Context") -> list[Finding]:
        return []


class _HoldEscalation(Invariant):
    """Carrier for the single per-hold escalation. It has no check or recovery."""

    name = HOLD_INVARIANT
    max_attempts = 0

    def __init__(self, tier: str) -> None:
        self.tier = tier

    def check(self, ctx: "Context") -> list[Finding]:
        return []


class Controller:
    def __init__(self, ctx: Context, invariants: Iterable[Invariant]) -> None:
        self.ctx = ctx
        self.invariants = list(invariants)
        self.store = StateStore(ctx.state_dir)

    def run(self, tier: str) -> PassResult:
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        with self.store.lock():
            return self._run_locked(tier)

    def _run_locked(self, tier: str) -> PassResult:
        now = self.ctx.now()
        state = self.store.load()
        if not state.get("installed_at"):
            state["installed_at"] = now
        model = state_model(self.ctx.config)
        result = PassResult(tier, "GREEN", 0, [], [], [], [], state_model=model)
        self._alert_queue: list[tuple[Finding, dict]] = []
        holds = recovery_holds(self.ctx.config)
        governed: dict = {}
        if model == STATE_MODEL_GOVERNED:
            governed = self._observe_governed(tier, holds, state, result, now)
        else:
            held: dict[str, list[Finding]] = {}
            for inv in [i for i in self.invariants if i.tier == tier]:
                findings = self._observe(inv)
                present = {f.fingerprint for f in findings}
                result.findings += len(findings)
                for finding in findings:
                    hold = hold_for(finding, holds)
                    if hold is not None:
                        self._hold(finding, hold, inv.tier, state, result)
                        held.setdefault(hold["name"], []).append(finding)
                        continue
                    self._process(inv, finding, state, result)
                self._resolve_absent(inv, present, state, result)
            self._escalate_holds(tier, holds, held, state, result)
        self._flush_alerts()
        if model == STATE_MODEL_GOVERNED:
            result.status, result.classes = self._classify(tier, state, governed["valid_names"])
        elif result.escalated or result.open or result.held or any(
            rec.get("status") == STATUS_ESCALATED and rec.get("tier") == tier
            for rec in state["fingerprints"].values()
        ):
            result.status = "DEGRADED"
        state.setdefault("last_pass", {})[tier] = _iso(now)
        self.store.save(state)
        beat = {
            "tier": tier,
            "finished_at": _iso(self.ctx.now()),
            "finished_ts": self.ctx.now(),
            "status": result.status,
            "findings": result.findings,
            "recovered": len(result.recovered),
            "escalated": len(result.escalated),
            "open": len(result.open),
            "held": len(result.held),
        }
        if model == STATE_MODEL_GOVERNED:
            beat.update({
                "state_model": model,
                "state_model_temporary": True,
                "classes": result.classes,
                "exceptions": governed["summary"],
                "invalid_exceptions": governed["invalid"],
            })
        self.store.write_heartbeat(tier, beat)
        return result

    # -- GOVERNED EXCEPTIONS (temporary model) --------------------------------

    def _observe_governed(self, tier: str, holds: list[dict], state: dict,
                          result: PassResult, now: float) -> dict:
        """OBSERVE/CLASSIFY under the governed-exception model.

        Every invariant still runs. A finding whose exact condition is named by
        a valid exception is recorded ``held`` (visible, never recovered). A
        finding on a frozen task that no valid exception names — a new or
        changed condition — is processed without recovery and is actionable.
        An unsafe finding is never covered and escalates at once.
        """
        valid, invalid, frozen = governed_exceptions(self.ctx.config, now)
        for hold in holds:
            frozen.update(hold["task_ids"])
        by_condition = {c: entry for entry in valid for c in entry["conditions"]}
        matched: dict[str, list[Finding]] = {}
        for inv in [i for i in self.invariants if i.tier == tier]:
            findings = self._observe(inv)
            present = {f.fingerprint for f in findings}
            result.findings += len(findings)
            for finding in findings:
                entry = None if finding.unsafe else by_condition.get(finding.condition)
                if entry is not None:
                    self._hold(finding, entry, inv.tier, state, result)
                    matched.setdefault(entry["name"], []).append(finding)
                elif finding.unsafe:
                    self._process(inv, dataclasses.replace(finding, recoverable=False),
                                  state, result, reason_override=ESCALATION_UNSAFE)
                elif _finding_ids(finding) & frozen:
                    self._process(inv, dataclasses.replace(finding, recoverable=False),
                                  state, result, reason_override=ESCALATION_FROZEN_UNCOVERED)
                else:
                    self._process(inv, finding, state, result)
            self._resolve_absent(inv, present, state, result)

        validity = _GovernanceCarrier(EXCEPTION_VALIDITY_INVARIANT, tier)
        invalid_present: set[str] = set()
        for name, problem in invalid:
            finding = Finding(EXCEPTION_VALIDITY_INVARIANT, f"{name}@{tier}",
                              f"invalid:{problem}", {"exception": name})
            invalid_present.add(finding.fingerprint)
            result.findings += 1
            self._process(validity, finding, state, result,
                          reason_override=ESCALATION_EXCEPTION_INVALID)
        self._resolve_absent(validity, invalid_present, state, result)

        self._escalate_exceptions(tier, valid, matched, state, result)
        summary = [{
            "name": e["name"], "kind": e["kind"], "owner": e["owner"],
            "authorized_by": e["authorized_by"], "created": e["created"],
            "review_condition": e["review_condition"], "expires_at": e.get("expires_at"),
            "conditions_present_this_pass": len(matched.get(e["name"], [])),
        } for e in valid]
        return {
            "valid_names": {e["name"] for e in valid},
            "summary": summary,
            "invalid": [f"{name}:{problem}" for name, problem in invalid],
        }

    def _escalate_exceptions(self, tier: str, valid: list[dict], matched: dict[str, list[Finding]],
                             state: dict, result: PassResult) -> None:
        """One deduplicated, visible record per active exception (per tier)."""
        carrier = _HoldEscalation(tier)
        present: set[str] = set()
        by_name = {e["name"]: e for e in valid}
        for name, findings in sorted(matched.items()):
            entry = by_name[name]
            aggregate = Finding(
                HOLD_INVARIANT, f"{name}@{tier}", "hold_active",
                {
                    "kind": entry["kind"],
                    "owner": entry["owner"],
                    "authorized_by": entry["authorized_by"],
                    "created": entry["created"],
                    "review_condition": entry["review_condition"],
                    "expires_at": entry.get("expires_at"),
                    "suppressed": sorted(f.condition for f in findings),
                },
            )
            present.add(aggregate.fingerprint)
            self._process(carrier, aggregate, state, result)
        self._resolve_absent(carrier, present, state, result)

    def _classify(self, tier: str, state: dict, valid_names: set) -> tuple[str, dict]:
        """Aggregate this tier's live fingerprints into one governed status."""
        classes = {"escalated": 0, "degraded": 0, "recovery": 0, "exception": 0}
        for rec in state["fingerprints"].values():
            if rec.get("tier") != tier or rec.get("status") == STATUS_RESOLVED:
                continue
            status = rec.get("status")
            if status == STATUS_HELD:
                cls = "exception"
            elif rec.get("invariant") == HOLD_INVARIANT:
                name = str(rec.get("subject") or "").rsplit("@", 1)[0]
                cls = "exception" if name in valid_names else "degraded"
            elif status == STATUS_ESCALATED:
                cls = "escalated" if rec.get("escalation_reason") in _ESCALATED_CLASS_REASONS else "degraded"
            elif status == STATUS_OPEN and int(rec.get("attempts") or 0) > 0:
                cls = "recovery"
            else:
                cls = "degraded"
            classes[cls] += 1
        for cls, aggregate in _AGGREGATE_PRECEDENCE:
            if classes[cls]:
                return aggregate, classes
        return AGGREGATE_GREEN, classes

    # -- OBSERVE -----------------------------------------------------------

    def _observe(self, inv: Invariant) -> list[Finding]:
        try:
            findings = list(inv.check(self.ctx))
        except Exception as exc:  # a monitor that cannot look is a finding
            return [Finding(
                inv.name, "__check__", f"check_error:{type(exc).__name__}",
                {"error": str(exc)[:300]},
            )]
        return [f for f in findings if f.invariant == inv.name]

    # -- CLASSIFY / RECOVER / REVALIDATE / ESCALATE --------------------------

    def _process(self, inv: Invariant, finding: Finding, state: dict, result: PassResult,
                 reason_override: Optional[str] = None) -> None:
        now = self.ctx.now()
        fp = finding.fingerprint
        rec = state["fingerprints"].get(fp)
        if rec is None or rec.get("status") in (STATUS_RESOLVED, STATUS_HELD):
            # A released hold starts a fresh episode: full recovery budget,
            # normal escalation and alerting.
            rec = {
                "invariant": finding.invariant, "subject": finding.subject,
                "signature": finding.signature, "tier": inv.tier,
                "first_seen": _iso(now), "attempts": 0, "observations": 0,
                "status": STATUS_OPEN, "card_id": None,
                "alert_delivered": False, "alert_attempts": 0,
            }
            state["fingerprints"][fp] = rec
            self.store.record(now, "observed", fingerprint=fp, invariant=finding.invariant,
                              subject=finding.subject, signature=finding.signature)
        rec["last_seen"] = _iso(now)
        rec["observations"] = int(rec.get("observations", 0)) + 1

        if rec["status"] == STATUS_ESCALATED:
            # Escalated exactly once. Only an undelivered alert is retried.
            self._deliver_alert(finding, rec)
            return

        if self.ctx.dry_run:
            result.open.append(fp)
            return

        if finding.recoverable and rec["attempts"] < inv.max_attempts:
            rec["attempts"] += 1
            try:
                outcome = inv.recover(self.ctx, finding)
            except Exception as exc:
                outcome = RecoveryOutcome(False, "recover_raised", {"error": str(exc)[:300]})
            if outcome is None:
                outcome = RecoveryOutcome(False, "no_recovery_defined")
            rec["last_recovery"] = {"at": _iso(now), "action": outcome.action,
                                    "applied": outcome.applied, "detail": outcome.detail}
            self.store.record(now, "recovery", fingerprint=fp, attempt=rec["attempts"],
                              action=outcome.action, applied=outcome.applied)
            # REVALIDATE: the exact invariant, rerun now.
            still = {f.fingerprint for f in self._observe(inv)}
            if fp not in still:
                rec["status"] = STATUS_RESOLVED
                rec["resolved_at"] = _iso(self.ctx.now())
                result.recovered.append(fp)
                self.store.record(self.ctx.now(), "green", fingerprint=fp, via="recovery")
                return
            if rec["attempts"] >= inv.max_attempts:
                self._escalate(finding, rec, ESCALATION_BUDGET_EXHAUSTED, result)
            else:
                result.open.append(fp)
            return

        reason = reason_override or (
            ESCALATION_NOT_RECOVERABLE if not finding.recoverable else ESCALATION_BUDGET_EXHAUSTED)
        if reason != ESCALATION_UNSAFE and rec["observations"] < max(1, inv.confirm_cycles):
            result.open.append(fp)
            return
        self._escalate(finding, rec, reason, result)

    def _escalate(self, finding: Finding, rec: dict, reason: str, result: PassResult) -> None:
        now = self.ctx.now()
        card_new = True
        try:
            card_id, card_new = self.ctx.create_card(self.ctx, finding, rec, reason)
            card_error = None
        except Exception as exc:
            card_id, card_error = None, f"{type(exc).__name__}: {str(exc)[:200]}"
        rec["status"] = STATUS_ESCALATED
        rec["escalated_at"] = _iso(now)
        rec["escalation_reason"] = reason
        rec["card_id"] = card_id
        rec["card_error"] = card_error
        self.store.record(now, "escalated", fingerprint=finding.fingerprint, reason=reason,
                          card_id=card_id, card_new=card_new, card_error=card_error)
        if card_id and not card_new:
            # The escalation already happened (controller state was lost or
            # reset): the governed card exists, so do not alert a second time.
            rec["alert_delivered"] = True
            rec["alert_detail"] = "escalation card already existed; alert not repeated"
        else:
            self._deliver_alert(finding, rec)
        result.escalated.append(finding.fingerprint)

    def _deliver_alert(self, finding: Finding, rec: dict) -> None:
        """Queue the fingerprint's alert; the pass sends one batched message."""
        if rec.get("alert_delivered") or int(rec.get("alert_attempts", 0)) >= MAX_ALERT_ATTEMPTS:
            return
        self._alert_queue.append((finding, rec))

    def _flush_alerts(self) -> None:
        """One delivery-checked message per pass, covering every queued fingerprint.

        Each fingerprint is still alerted exactly once (retried only while
        undelivered); batching keeps a backlog discovered in one pass from
        becoming a burst of separate messages.
        """
        queue, self._alert_queue = self._alert_queue, []
        if not queue:
            return
        blocks = [alert_text(finding, rec) for finding, rec in queue]
        if len(blocks) == 1:
            subject, text = blocks[0]
        else:
            subject = f"[health] {len(blocks)} conditions DEGRADED"
            text = "\n\n".join(body for _, body in blocks)
        try:
            delivered, detail = self.ctx.send_alert(subject, text)
        except Exception as exc:
            delivered, detail = False, f"{type(exc).__name__}: {str(exc)[:200]}"
        for finding, rec in queue:
            rec["alert_attempts"] = int(rec.get("alert_attempts", 0)) + 1
            rec["alert_delivered"] = bool(delivered)
            rec["alert_detail"] = detail
            self.store.record(self.ctx.now(), "alert", fingerprint=finding.fingerprint,
                              delivered=bool(delivered), attempt=rec["alert_attempts"],
                              batch_size=len(queue))

    # -- HOLD ----------------------------------------------------------------

    def _hold(self, finding: Finding, hold: dict, tier: str, state: dict, result: PassResult) -> None:
        """Record a held finding. No recovery, no individual card or alert."""
        now = self.ctx.now()
        fp = finding.fingerprint
        rec = state["fingerprints"].get(fp)
        if rec is None or rec.get("status") not in (STATUS_HELD,):
            previous = rec.get("status") if rec else None
            previous_card = rec.get("card_id") or rec.get("previous_card_id") if rec else None
            rec = {
                "invariant": finding.invariant, "subject": finding.subject,
                "signature": finding.signature, "tier": tier,
                "first_seen": rec.get("first_seen", _iso(now)) if rec else _iso(now),
                "attempts": rec.get("attempts", 0) if rec else 0, "observations": 0,
                "status": STATUS_HELD, "held_by": hold["name"], "card_id": None,
                "alert_delivered": True, "alert_attempts": 0,
            }
            if previous_card:
                # A hold never hides an earlier escalation: keep its card pointer.
                rec["previous_card_id"] = previous_card
            state["fingerprints"][fp] = rec
            self.store.record(now, "held", fingerprint=fp, invariant=finding.invariant,
                              subject=finding.subject, hold=hold["name"], previous_status=previous)
        rec["last_seen"] = _iso(now)
        rec["observations"] = int(rec.get("observations", 0)) + 1
        result.held.append(fp)

    def _escalate_holds(self, tier: str, holds: list[dict], held: dict[str, list[Finding]],
                        state: dict, result: PassResult) -> None:
        """One deduplicated escalation per active hold (per tier), ids only."""
        carrier = _HoldEscalation(tier)
        present: set[str] = set()
        by_name = {h["name"]: h for h in holds}
        for name, findings in sorted(held.items()):
            hold = by_name[name]
            aggregate = Finding(
                HOLD_INVARIANT, f"{name}@{tier}", "hold_active",
                {
                    "held_task_ids": ",".join(hold["task_ids"]),
                    "suppressed": sorted(f"{f.invariant}:{f.subject}:{f.signature}" for f in findings),
                    "release": hold["release"],
                },
            )
            present.add(aggregate.fingerprint)
            self._process(carrier, aggregate, state, result)
        self._resolve_absent(carrier, present, state, result)

    def _resolve_absent(self, inv: Invariant, present: set[str], state: dict, result: PassResult) -> None:
        now = self.ctx.now()
        for fp, rec in state["fingerprints"].items():
            if rec.get("invariant") != inv.name or fp in present:
                continue
            if rec.get("tier") not in (None, inv.tier):
                continue  # the other pass owns it
            if rec.get("status") in (STATUS_OPEN, STATUS_ESCALATED, STATUS_HELD):
                rec["status"] = STATUS_RESOLVED
                rec["resolved_at"] = _iso(now)
                result.resolved.append(fp)
                self.store.record(now, "green", fingerprint=fp, via="condition_cleared")


def alert_text(finding: Finding, rec: dict) -> tuple[str, str]:
    """Identifiers and signatures only — never titles, bodies or company data."""
    subject = f"[health] {finding.invariant} DEGRADED"
    lines = [
        f"invariant: {finding.invariant}",
        f"subject: {finding.subject}",
        f"signature: {finding.signature}",
        f"fingerprint: {finding.fingerprint}",
        f"reason: {rec.get('escalation_reason')}",
        f"attempts: {rec.get('attempts', 0)}",
        f"card: {rec.get('card_id') or 'NOT CREATED: ' + str(rec.get('card_error'))}",
    ]
    return subject, "\n".join(lines)


def card_body(finding: Finding, rec: dict, reason: str) -> str:
    safe_detail = json.dumps(finding.detail, sort_keys=True, default=str)[:2000]
    return "\n".join([
        f"Opened by {CONTROLLER_ID} (deterministic health controller, t_b8d62378).",
        "",
        f"- invariant: `{finding.invariant}`",
        f"- subject: `{finding.subject}`",
        f"- signature: `{finding.signature}`",
        f"- fingerprint: `{finding.fingerprint}`",
        f"- escalation reason: `{reason}`",
        f"- automatic recovery attempts: {rec.get('attempts', 0)}",
        f"- first seen: {rec.get('first_seen')}",
        f"- detail: `{safe_detail}`",
        "",
        "Automatic mutation for this fingerprint has stopped. The controller "
        "records the condition clearing on its own; this card needs a "
        "governed decision.",
    ])


# ---------------------------------------------------------------------------
# Kanban helpers shared by the verifier-routing invariants
# ---------------------------------------------------------------------------


def _kb():
    from hermes_cli import kanban_db as kb
    return kb


_VERIFICATION_TITLE_RE = re.compile(
    r"^\s*(independent\s+)?(compliance\s+)?(verif(y|ication)|audit)\b", re.I,
)


def _review_requested_at(conn, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? "
        "AND kind = 'review_requested' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return int(row["created_at"]) if row is not None else None


def _pending_gauntlet_subjects(conn) -> list:
    kb = _kb()
    rows = conn.execute(
        "SELECT id, status, assignee, executor_lane FROM tasks "
        "WHERE verification_state = ? AND terminal_disposition IS NULL "
        "AND status IN ('review', 'triage', 'blocked')",
        (kb.VERIFICATION_PENDING,),
    ).fetchall()
    return [r for r in rows if kb.gauntlet_required(conn, r["id"])]


# ---------------------------------------------------------------------------
# Verifier-routing invariants (v1 automatic recovery scope)
# ---------------------------------------------------------------------------


class VerifierRouteOpen(Invariant):
    """A Gauntlet subject awaiting verification has a route to a verdict.

    Covers the 2026-09-14 chain on t_3883034a / t_15d87799: evidence that
    arrived after the handoff (route never opened), a same-identity review with
    no child, an unroutable subject. Recovery opens the codex_verify child
    through the sanctioned ``_ensure_independent_verifier_child`` — only for a
    subject in ``review`` whose evidence the dispatch gate accepts.
    """

    name = "verifier_route_open"
    tier = TIER_LIGHT
    settle_seconds = 60

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        now = int(ctx.now())
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for row in _pending_gauntlet_subjects(conn):
                tid = row["id"]
                if row["status"] != "review":
                    # A blocked subject already carries its blocker record, and a
                    # triaged one is subject_review_regressed's: routing is only
                    # judged for subjects parked in the review lane.
                    continue
                if row["executor_lane"] == kb.EXECUTOR_LANE_CODEX_VERIFY:
                    continue  # a verifier card is not a subject; see relabel invariant
                requested = _review_requested_at(conn, tid)
                if requested is None or now - requested < self.settle_seconds:
                    continue
                if kb._open_verifier_child(conn, tid) is not None:
                    continue
                implementer = kb._review_requested_implementer(conn, tid, row["assignee"])
                reviewers = kb._phase_reviewer_identities(conn, tid) - {implementer}
                if row["status"] == "review" and reviewers:
                    continue  # an installed independent reviewer is a route
                evidence = kb.subject_has_evidence(conn, tid)
                out.append(Finding(
                    self.name, tid,
                    "no_route:" + ("evidence_ready" if evidence else "evidence_missing"),
                    {"status": row["status"], "evidence": evidence},
                    recoverable=bool(evidence),
                ))
        return out

    def recover(self, ctx: Context, finding: Finding) -> Optional[RecoveryOutcome]:
        kb = _kb()
        with ctx.kanban() as conn:
            row = conn.execute(
                "SELECT assignee FROM tasks WHERE id = ?", (finding.subject,),
            ).fetchone()
            implementer = kb._review_requested_implementer(
                conn, finding.subject, row["assignee"] if row else None,
            )
            child = kb._ensure_independent_verifier_child(
                conn, finding.subject, implementer=implementer,
            )
            kb.recompute_ready(conn)
        return RecoveryOutcome(child is not None, "open_independent_verifier_child",
                               {"verifier_task": child})


class SubjectLaneRelabelled(Invariant):
    """A Gauntlet subject must never carry the codex_verify lane itself.

    The ``reassign atlas`` damage on t_3883034a (event 151185). Recovery
    restores the lane recorded at creation through the audited
    ``restore_relabelled_subject_lane`` helper.
    """

    name = "subject_lane_relabelled"
    tier = TIER_LIGHT

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        out: list[Finding] = []
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT t.id, t.status, t.claim_lock FROM tasks t "
                "WHERE t.executor_lane = ? AND t.terminal_disposition IS NULL "
                "AND t.status NOT IN ('done', 'archived') "
                "AND NOT EXISTS (SELECT 1 FROM task_links l WHERE l.child_id = t.id) "
                "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id "
                "            AND e.kind = 'review_requested') "
                "AND EXISTS (SELECT 1 FROM task_events n WHERE n.task_id = t.id "
                "            AND n.kind = 'executor_lane_normalized')",
                (kb.EXECUTOR_LANE_CODEX_VERIFY,),
            ).fetchall()
            for row in rows:
                out.append(Finding(
                    self.name, row["id"], f"subject_on_codex_verify_lane:{row['status']}",
                    {"status": row["status"]},
                    recoverable=row["claim_lock"] is None,
                ))
        return out

    def recover(self, ctx: Context, finding: Finding) -> Optional[RecoveryOutcome]:
        kb = _kb()
        with ctx.kanban() as conn:
            restored, lane = kb.restore_relabelled_subject_lane(
                conn, finding.subject, actor=CONTROLLER_ID,
            )
        return RecoveryOutcome(restored, "restore_subject_executor_lane", {"lane": lane})


class VerifierChildDeadlocked(Invariant):
    """A verifier child of an evidence-ready subject must be releasable.

    The t_4d21959c shape: a registered-profile verification card parked behind
    its own subject, rejected at claim. Recovery declares it with the typed
    ``verifies`` relation — only when its title is unambiguously a
    verification card and it is not the implementer. Anything else escalates.
    """

    name = "verifier_child_deadlocked"
    tier = TIER_LIGHT

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for subject in _pending_gauntlet_subjects(conn):
                sid = subject["id"]
                if not kb.subject_has_evidence(conn, sid):
                    continue
                implementer = kb._review_requested_implementer(conn, sid, subject["assignee"])
                children = conn.execute(
                    "SELECT c.id, c.title, c.assignee, c.executor_lane FROM task_links l "
                    "JOIN tasks c ON c.id = l.child_id "
                    "WHERE l.parent_id = ? AND c.status = 'todo' "
                    "AND COALESCE(c.executor_lane, '') <> ?",
                    (sid, kb.EXECUTOR_LANE_CODEX_VERIFY),
                ).fetchall()
                for child in children:
                    cid = child["id"]
                    rejected = conn.execute(
                        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claim_rejected' "
                        "AND payload LIKE '%parents_not_done%' LIMIT 1",
                        (cid,),
                    ).fetchone()
                    if rejected is None:
                        continue  # nothing has tried to run it: an ordinary dependent
                    if kb._parents_satisfied(conn, cid):
                        continue
                    declared = conn.execute(
                        "SELECT 1 FROM task_relations WHERE from_task_id = ? AND to_task_id = ? "
                        "AND relation = ?",
                        (cid, sid, kb.RELATION_VERIFIES),
                    ).fetchone()
                    is_implementer = bool(implementer) and child["assignee"] == implementer
                    classified = bool(_VERIFICATION_TITLE_RE.match(child["title"] or ""))
                    out.append(Finding(
                        self.name, cid,
                        "verifier_child_gated:" + ("declared" if declared else "undeclared"),
                        {"subject": sid, "classified_as_verifier": classified,
                         "child_is_implementer": is_implementer},
                        recoverable=bool(classified and not declared and not is_implementer),
                    ))
        return out

    def recover(self, ctx: Context, finding: Finding) -> Optional[RecoveryOutcome]:
        kb = _kb()
        subject = finding.detail["subject"]
        with ctx.kanban() as conn:
            added = kb.add_task_relation(
                conn, finding.subject, subject, kb.RELATION_VERIFIES, created_by=CONTROLLER_ID,
            )
            kb.recompute_ready(conn)
        return RecoveryOutcome(bool(added), "declare_verifies_relation", {"subject": subject})


# ---------------------------------------------------------------------------
# Verifier-graph invariants (escalate only in v1)
# ---------------------------------------------------------------------------


class SubjectReviewRegressed(Invariant):
    """A subject awaiting verification must not sit in triage (review->triage)."""

    name = "subject_review_regressed"
    tier = TIER_LIGHT

    def check(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for row in _pending_gauntlet_subjects(conn):
                if row["status"] == "triage":
                    out.append(Finding(self.name, row["id"], "pending_subject_in_triage",
                                       {"status": "triage"}))
        return out


class VerifierOfVerifier(Invariant):
    """No live codex_verify card may verify another codex_verify card.

    A parent that is itself a relabelled SUBJECT (no parent of its own, handed
    off for review, carrying a lane-normalization event) is excluded: that is
    ``subject_lane_relabelled`` damage, and on the 2026-09-14 board all six
    codex_verify -> codex_verify edges were exactly that shape.
    """

    name = "verifier_of_verifier"
    tier = TIER_DEEP

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        out: list[Finding] = []
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT c.id AS child, p.id AS parent FROM task_links l "
                "JOIN tasks c ON c.id = l.child_id JOIN tasks p ON p.id = l.parent_id "
                "WHERE c.executor_lane = ? AND p.executor_lane = ? "
                "AND c.status NOT IN ('done', 'archived') "
                "AND NOT ("
                "  NOT EXISTS (SELECT 1 FROM task_links pl WHERE pl.child_id = p.id) "
                "  AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = p.id "
                "              AND e.kind = 'review_requested') "
                "  AND EXISTS (SELECT 1 FROM task_events n WHERE n.task_id = p.id "
                "              AND n.kind = 'executor_lane_normalized')"
                ")",
                (kb.EXECUTOR_LANE_CODEX_VERIFY, kb.EXECUTOR_LANE_CODEX_VERIFY),
            ).fetchall()
            for row in rows:
                out.append(Finding(self.name, row["child"], "verifies_a_verifier",
                                   {"parent": row["parent"]}))
        return out


class VerifiedClosureAttributable(Invariant):
    """Every verified Gauntlet closure names an attributable independent verifier.

    Detects the t_e48487e5 shape (verified by a bare lane name) and the
    t_69440ff2 shape (a returned verdict from an unattested verifier run).
    Never rewrites history: escalation only.
    """

    name = "verified_closure_attributable"
    tier = TIER_DEEP

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        lane_names = set(kb.VALID_EXECUTOR_LANES) | {"atlas"}
        out: list[Finding] = []
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT e.task_id, e.payload FROM task_events e "
                "JOIN tasks t ON t.id = e.task_id "
                "WHERE e.kind = 'verification_passed' AND t.gauntlet_enforced = 1",
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                verifier = str(payload.get("verifier") or "").strip().lower()
                if not verifier or verifier in lane_names:
                    out.append(Finding(self.name, row["task_id"],
                                       "verified_by_bare_or_missing_identity",
                                       {"verifier": verifier or None}))
            returned = conn.execute(
                "SELECT task_id, payload FROM task_events WHERE kind = 'verifier_verdict_returned'",
            ).fetchall()
            for row in returned:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    continue
                vt = payload.get("verifier_task")
                if payload.get("recorded") and vt and kb._attested_codex_verifier_run(conn, vt) is None:
                    out.append(Finding(self.name, row["task_id"],
                                       "verdict_returned_from_unattested_verifier",
                                       {"verifier_task": vt}))
        return out


# ---------------------------------------------------------------------------
# Platform and watcher-of-watchers invariants (escalate only in v1)
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _dt.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


class GatewayHeartbeatFresh(Invariant):
    name = "gateway_heartbeat_fresh"
    tier = TIER_LIGHT
    confirm_cycles = 2

    def check(self, ctx: Context) -> list[Finding]:
        max_age = int(ctx.config.get("gateway_heartbeat_max_age_seconds", 180))
        path = ctx.hermes_home / "state" / "gateway.heartbeat"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return [Finding(self.name, "gateway", "heartbeat_missing", {})]
        except (OSError, json.JSONDecodeError) as exc:
            return [Finding(self.name, "gateway", f"heartbeat_unreadable:{type(exc).__name__}", {})]
        updated = _parse_ts(data.get("updated_at"))
        if updated is None:
            return [Finding(self.name, "gateway", "heartbeat_without_timestamp", {})]
        if ctx.now() - updated > max_age:
            return [Finding(self.name, "gateway", "heartbeat_stale",
                            {"max_age_seconds": max_age})]
        return []


class CriticalCronJobsHealthy(Invariant):
    """Hermes cron jobs expected active are enabled, fresh and succeeding.

    A paused critical job must carry a recorded reason; the jobs schema has no
    expiry field, so ``pause_max_age_seconds`` in config bounds how long a
    reasoned pause may last.
    """

    name = "critical_cron_jobs_healthy"
    tier = TIER_LIGHT

    def check(self, ctx: Context) -> list[Finding]:
        expected = ctx.config.get("critical_cron_jobs") or {}
        path = ctx.hermes_home / "cron" / "jobs.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [Finding(self.name, "cron", f"jobs_file_unreadable:{type(exc).__name__}", {})]
        jobs = doc.get("jobs", []) if isinstance(doc, dict) else doc
        by_name = {j.get("name"): j for j in jobs if isinstance(j, dict)}
        now = ctx.now()
        out: list[Finding] = []
        for name, spec in expected.items():
            job = by_name.get(name)
            if job is None:
                out.append(Finding(self.name, name, "job_missing", {}))
                continue
            paused = (not job.get("enabled", True)) or job.get("state") == "paused"
            if paused:
                reason = str(job.get("paused_reason") or "").strip()
                if not reason:
                    out.append(Finding(self.name, name, "paused_without_reason", {}))
                    continue
                pause_limit = spec.get("pause_max_age_seconds")
                paused_at = _parse_ts(job.get("paused_at"))
                if pause_limit and paused_at and now - paused_at > int(pause_limit):
                    out.append(Finding(self.name, name, "pause_expired", {}))
                continue
            if job.get("last_status") not in (None, "ok"):
                out.append(Finding(self.name, name, f"last_status:{job.get('last_status')}", {}))
            if int(job.get("failure_streak") or 0) > 0:
                out.append(Finding(self.name, name, "failure_streak", {}))
            if job.get("last_delivery_error"):
                out.append(Finding(self.name, name, "delivery_failed", {}))
            max_age = spec.get("max_age_seconds")
            last = _parse_ts(job.get("last_run_at"))
            created = _parse_ts(job.get("created_at"))
            reference = last if last is not None else created
            if max_age and reference is not None and now - reference > int(max_age):
                out.append(Finding(self.name, name, "stale" if last else "never_ran", {}))
            script = job.get("script")
            if script:
                script_path = Path(os.path.expanduser(str(script)))
                if not script_path.is_absolute():
                    script_path = ctx.hermes_home / "scripts" / script_path
                if not script_path.exists():
                    out.append(Finding(self.name, name, "script_missing", {}))
        return out


class CriticalTimersActive(Invariant):
    name = "critical_timers_active"
    tier = TIER_DEEP

    def check(self, ctx: Context) -> list[Finding]:
        expected = ctx.config.get("critical_user_timers") or {}
        if not expected:
            return []
        now = ctx.now()
        # ``list-timers --output=json`` reports ``last`` as epoch microseconds;
        # ``systemctl show`` renders LastTriggerUSec as a localized date.
        rc, text = ctx.run_command(
            ["systemctl", "--user", "list-timers", "--all", "--output=json"], 30,
        )
        try:
            listed = {t.get("unit"): t for t in json.loads(text)} if rc == 0 else None
        except (json.JSONDecodeError, TypeError, AttributeError):
            listed = None
        if listed is None:
            return [Finding(self.name, "systemd-user", f"timer_listing_failed:rc={rc}", {})]
        out: list[Finding] = []
        for unit, spec in expected.items():
            entry = listed.get(unit)
            if entry is None:
                out.append(Finding(self.name, unit, "timer_not_loaded", {}))
                continue
            rc_active, active = ctx.run_command(["systemctl", "--user", "is-active", unit], 30)
            if active.strip() != "active":
                out.append(Finding(self.name, unit, f"not_active:{active.strip() or rc_active}", {}))
                continue
            raw_last = entry.get("last")
            last = float(raw_last) / 1_000_000 if isinstance(raw_last, (int, float)) and raw_last else None
            max_age = spec.get("max_age_seconds")
            if max_age and (last is None or now - last > int(max_age)):
                out.append(Finding(self.name, unit, "not_triggered_recently", {}))
        return out


class CanonicalBootComplete(Invariant):
    name = "canonical_boot_complete"
    tier = TIER_DEEP

    def check(self, ctx: Context) -> list[Finding]:
        command = ctx.config.get("boot_gate_command") or [
            str(Path.home() / ".local/bin/hermes-shared-boot-context"), "--gate-exit-code",
        ]
        rc, text = ctx.run_command(list(command), 240)
        lines = [ln.strip() for ln in text.splitlines() if ln.startswith("BOOT STATUS:")]
        # The authoritative state line, parsed exactly; prose mentions do not count.
        if rc == 0 and lines and lines[0] == "BOOT STATUS: COMPLETE":
            return []
        status = lines[0] if lines else "no_status_line"
        return [Finding(self.name, "shared_boot", f"{status}:rc={rc}", {})]


class RuntimeCeilingsMatchDoctrine(Invariant):
    name = "runtime_ceilings_match_doctrine"
    tier = TIER_DEEP

    def check(self, ctx: Context) -> list[Finding]:
        expected = ctx.config.get("expected_runtime_config") or {}
        out: list[Finding] = []
        for key, want in expected.items():
            rc, text = ctx.run_command(
                [sys.executable, "-m", "hermes_cli.main", "config", "get", key], 30,
            )
            value = next((ln.strip() for ln in reversed(text.splitlines())
                          if ln.strip() and "1Password" not in ln), "")
            if rc != 0 or value != str(want):
                out.append(Finding(self.name, key, f"value_mismatch:{value or 'unreadable'}",
                                   {"expected": want}))
        return out


class CounterpartHeartbeatFresh(Invariant):
    """The light and deep passes watch each other."""

    name = "controller_heartbeat_fresh"
    confirm_cycles = 1

    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.other = TIER_DEEP if tier == TIER_LIGHT else TIER_LIGHT
        self.name = f"controller_heartbeat_fresh_{self.other}"

    def check(self, ctx: Context) -> list[Finding]:
        store = StateStore(ctx.state_dir)
        limit = 2 * TIER_INTERVAL_SECONDS[self.other] + 120
        beat = store.read_heartbeat(self.other)
        now = ctx.now()
        if beat is None:
            state = store.load()
            installed = state.get("installed_at") or now
            if now - float(installed) > limit:
                return [Finding(self.name, self.other, "counterpart_never_ran", {})]
            return []
        finished = beat.get("finished_ts")
        if not isinstance(finished, (int, float)) or now - float(finished) > limit:
            return [Finding(self.name, self.other, "counterpart_heartbeat_stale",
                            {"limit_seconds": limit})]
        return []


def default_invariants() -> list[Invariant]:
    return [
        VerifierRouteOpen(),
        SubjectLaneRelabelled(),
        VerifierChildDeadlocked(),
        SubjectReviewRegressed(),
        GatewayHeartbeatFresh(),
        CriticalCronJobsHealthy(),
        CounterpartHeartbeatFresh(TIER_LIGHT),
        VerifierOfVerifier(),
        VerifiedClosureAttributable(),
        CriticalTimersActive(),
        CanonicalBootComplete(),
        RuntimeCeilingsMatchDoctrine(),
        CounterpartHeartbeatFresh(TIER_DEEP),
    ]


# ---------------------------------------------------------------------------
# Production adapters
# ---------------------------------------------------------------------------


def run_command(argv: list[str], timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=str(REPO_ROOT))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout


def make_alert_sender(target: Optional[str]) -> Callable[[str, str], tuple[bool, str]]:
    def send(subject: str, text: str) -> tuple[bool, str]:
        if not target:
            return False, "no alert_target configured"
        rc, out = run_command(
            [sys.executable, "-m", "hermes_cli.main", "send", "--to", target,
             "--subject", subject, "--quiet", text], 60,
        )
        return rc == 0, f"hermes send exit {rc}"
    return send


def kanban_repair_card(
    ctx: Context, finding: Finding, rec: dict, reason: str,
) -> tuple[Optional[str], bool]:
    """One governed triage card per fingerprint; never dispatched automatically."""
    kb = _kb()
    key = f"health:{finding.invariant}:{finding.fingerprint}"
    with ctx.kanban() as conn:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at LIMIT 1",
            (key,),
        ).fetchone()
        if existing is not None:
            return existing["id"], False
        card_id = kb.create_task(
            conn,
            title=f"Health controller: {finding.invariant} on {finding.subject}",
            body=card_body(finding, rec, reason),
            assignee=None,
            triage=True,
            created_by=CONTROLLER_ID,
            idempotency_key=key,
            provenance=kb.ActorProvenance(
                kind=kb.ACTOR_KIND_SYSTEM, actor_id=CONTROLLER_ID,
                cause=kb.CREATION_CAUSE_AUTOMATED,
            ),
            control_plane_authority=f"defect:{finding.invariant}:{finding.fingerprint}",
        )
        return card_id, True


def build_context(args: argparse.Namespace) -> Context:
    kb = _kb()
    config_path = Path(args.config) if args.config else DEFAULT_CONFIG_PATH
    config = json.loads(config_path.read_text(encoding="utf-8"))
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    state_dir = Path(args.state_dir) if args.state_dir else hermes_home / "state" / CONTROLLER_ID
    return Context(
        state_dir=state_dir,
        hermes_home=hermes_home,
        config=config,
        kanban=kb.connect_closing,
        send_alert=make_alert_sender(config.get("alert_target")),
        run_command=run_command,
        create_card=kanban_repair_card,
        dry_run=bool(args.dry_run),
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="system_health_controller")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run one controller pass")
    run_p.add_argument("--tier", choices=TIERS, required=True)
    run_p.add_argument("--dry-run", action="store_true",
                       help="observe and classify only; no recovery, card or alert")
    run_p.add_argument("--config")
    run_p.add_argument("--state-dir")
    run_p.add_argument("--json", action="store_true")
    status_p = sub.add_parser("status", help="print the fingerprint ledger summary")
    status_p.add_argument("--config")
    status_p.add_argument("--state-dir")
    status_p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    ctx = build_context(args)
    if args.command == "status":
        state = StateStore(ctx.state_dir).load()
        print(json.dumps({fp: {k: rec.get(k) for k in (
            "invariant", "subject", "signature", "status", "attempts", "card_id",
            "alert_delivered", "held_by", "escalation_reason", "previous_card_id")}
            for fp, rec in state["fingerprints"].items()}, indent=2))
        return 0
    result = Controller(ctx, default_invariants()).run(args.tier)
    # Silent when GREEN unless asked. Handled findings are not a crash: exit 0
    # so OnFailure= fires only when the controller itself cannot run.
    if args.json or args.dry_run:
        print(json.dumps(dataclasses.asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
