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

ROUTE_SHARED = "shared"
ROUTE_COMPANY = "company"

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
    #: ``shared`` escalates with a governed card on the shared board plus the
    #: shared alert. ``company`` (company health probes, Christopher F2
    #: 2026-09-14) never creates a shared card: only the coarse shared summary
    #: Erika needs is sent, and she routes it to the owning company lane.
    route: str = "shared"

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
    #: Every invariant documents itself (Christopher, F1, 2026-09-14): the
    #: authoritative source it reads, the exact failure condition, and the
    #: evidence a finding carries. Enforced by the test suite.
    source: str = ""
    failure: str = ""
    evidence: str = ""
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

    source = "controller config governed_exceptions.entries"
    failure = "an exception entry is malformed, expired, duplicated or its authorization digest no longer matches"
    evidence = "invalid:<problem>; detail exception name"
    max_attempts = 0

    def __init__(self, name: str, tier: str) -> None:
        self.name = name
        self.tier = tier

    def check(self, ctx: "Context") -> list[Finding]:
        return []


class _HoldEscalation(Invariant):
    """Carrier for the single per-hold escalation. It has no check or recovery."""

    name = HOLD_INVARIANT
    source = "controller config recovery_holds / governed_exceptions"
    failure = "an active hold or governed exception covers findings this pass"
    evidence = "hold_active; detail held ids or exception record and suppressed conditions"
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
        card_error = None
        if finding.route == ROUTE_COMPANY:
            # Company health never becomes a shared card (F2 boundary).
            card_id = None
            rec["route"] = ROUTE_COMPANY
        else:
            try:
                card_id, card_new = self.ctx.create_card(self.ctx, finding, rec, reason)
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
        (f"card: none (company lane; Erika routes to the owning Team Leader)"
         if rec.get("route") == ROUTE_COMPANY else
         f"card: {rec.get('card_id') or 'NOT CREATED: ' + str(rec.get('card_error'))}"),
    ]
    return subject, "\n".join(lines)


def _last_recovery_text(rec: dict) -> str:
    last = rec.get("last_recovery")
    if not last:
        return "none"
    return f"`{last.get('action')}` applied={last.get('applied')} at {last.get('at')}"


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
        f"- last recovery: {_last_recovery_text(rec)}",
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
    source = "kanban.db tasks/task_events/task_attachments via kanban_db._open_verifier_child, _phase_reviewer_identities, subject_has_evidence"
    failure = "a Gauntlet subject pending verification in review for >60 s has no open verifier child and no installed independent reviewer"
    evidence = "no_route:evidence_ready (recoverable) or no_route:evidence_missing; detail status, evidence"
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
    source = "kanban.db tasks.executor_lane + task_links + review_requested/executor_lane_normalized events"
    failure = "a parentless card that was handed off for review carries the codex_verify lane after a lane normalization"
    evidence = "subject_on_codex_verify_lane:<status>; recoverable only when unclaimed"

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
    source = "kanban.db task_links/tasks/task_relations + claim_rejected events via kanban_db._parents_satisfied"
    failure = "a profile verification child of an evidence-ready pending subject was rejected at claim (parents_not_done) and is still gated"
    evidence = "verifier_child_gated:declared|undeclared; detail subject, classified_as_verifier, child_is_implementer"

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
    source = "kanban.db tasks (verification_state pending, Gauntlet-required)"
    failure = "a Gauntlet subject awaiting verification sits in triage"
    evidence = "pending_subject_in_triage"

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
    source = "kanban.db task_links joined to tasks.executor_lane"
    failure = "a live codex_verify card is the child of another codex_verify card (relabelled subjects excluded)"
    evidence = "verifies_a_verifier; detail parent"

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
    source = "kanban.db verification_passed and verifier_verdict_returned events via kanban_db._attested_codex_verifier_run"
    failure = "a Gauntlet closure verified by a bare/missing lane identity, or a recorded verdict returned from an unattested verifier"
    evidence = "verified_by_bare_or_missing_identity; verdict_returned_from_unattested_verifier (history never rewritten)"

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
    source = "~/.hermes/state/gateway.heartbeat (gateway/shutdown_watchdog.py) updated_at"
    failure = "heartbeat missing, unreadable, without timestamp, or older than gateway_heartbeat_max_age_seconds for 2 cycles"
    evidence = "heartbeat_missing|heartbeat_unreadable:<exc>|heartbeat_without_timestamp|heartbeat_stale"
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
    source = "~/.hermes/cron/jobs.json (Hermes cron) for jobs named in critical_cron_jobs"
    failure = "job missing, paused without reason or past pause_max_age, non-ok last_status, failure streak, delivery error, stale/never ran, or script missing"
    evidence = "job_missing|paused_without_reason|pause_expired|last_status:<s>|failure_streak|delivery_failed|stale|never_ran|script_missing"

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
    source = "systemctl --user list-timers --all --output=json and is-active for critical_user_timers"
    failure = "timer not loaded, not active, or not triggered within max_age_seconds (systemd has no pause-reason field, so an inactive timer is never a reasoned pause)"
    evidence = "timer_listing_failed:rc=<n>|timer_not_loaded|not_active:<state>|not_triggered_recently"

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
    source = "~/.local/bin/hermes-shared-boot-context --gate-exit-code (authoritative BOOT STATUS line)"
    failure = "exit code non-zero or the first BOOT STATUS line is not exactly 'BOOT STATUS: COMPLETE'"
    evidence = "<status line or no_status_line>:rc=<n>"

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
    source = "hermes config get <key> for expected_runtime_config (doctrine operations.md ceilings)"
    failure = "a live runtime value differs from the doctrine value or cannot be read"
    evidence = "value_mismatch:<live value>; detail expected"

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
    source = "~/.hermes/state/system-health-controller/heartbeat-<other tier>.json finished_ts"
    failure = "the other pass has not finished within 2 intervals + 120 s (or never ran after install)"
    evidence = "counterpart_heartbeat_stale|counterpart_never_ran; detail limit_seconds"
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


# ---------------------------------------------------------------------------
# F1 detect-only invariants (Christopher, 2026-09-14): no recovery, no new
# mutation authority. Each reads an existing authoritative surface through the
# code that already owns it.
# ---------------------------------------------------------------------------


def _is_relabelled_subject(conn, task_id: str) -> bool:
    """The ``subject_lane_relabelled`` shape: a subject, not a verifier."""
    row = conn.execute(
        "SELECT 1 FROM tasks t WHERE t.id = ? "
        "AND NOT EXISTS (SELECT 1 FROM task_links l WHERE l.child_id = t.id) "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id AND e.kind = 'review_requested') "
        "AND EXISTS (SELECT 1 FROM task_events n WHERE n.task_id = t.id AND n.kind = 'executor_lane_normalized')",
        (task_id,),
    ).fetchone()
    return row is not None


class ReadyBacklogExplained(Invariant):
    """Dispatcher consistency: spawnable ready work does not wait unexplained.

    Unlike the gateway's "dispatcher stuck" telemetry, a card is only a fault
    when nothing explains the wait: its respawn guard is clear and the global
    concurrency cap has room.
    """

    name = "ready_backlog_explained"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = ("kanban.db tasks/task_events via kanban_diagnostics._rule_stranded_in_ready, "
              "kanban_db.check_respawn_guard, count_running_tasks, resolve_max_in_progress, "
              "profiles.profile_exists")
    failure = ("a ready, unclaimed, assigned, not-disposed card has waited past "
               "ready_stranded_threshold_seconds while its respawn guard is clear and capacity is "
               "free (spawnable), or its assignee is not a real profile (unspawnable); 2 cycles")
    evidence = "spawnable_ready_unclaimed | ready_assignee_not_spawnable; detail status"

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        from hermes_cli import kanban_diagnostics as kd
        from hermes_cli import profiles
        threshold = int(ctx.config.get("ready_stranded_threshold_seconds", 1800))
        now = int(ctx.now())
        out: list[Finding] = []
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = 'ready' AND claim_lock IS NULL "
                "AND assignee IS NOT NULL AND TRIM(assignee) <> '' "
                f"AND {kb._not_irreversibly_disposed_sql()}"
            ).fetchall()
            if not rows:
                return []
            cap = kb.resolve_max_in_progress(kb.configured_max_in_progress())
            capacity_free = cap is None or kb.count_running_tasks(conn) < cap
            for row in rows:
                events = conn.execute(
                    "SELECT kind, created_at FROM task_events WHERE task_id = ? ORDER BY id",
                    (row["id"],),
                ).fetchall()
                if not kd._rule_stranded_in_ready(
                    row, events, [], now, {"stranded_threshold_seconds": threshold},
                ):
                    continue
                if not profiles.profile_exists(row["assignee"]):
                    out.append(Finding(self.name, row["id"], "ready_assignee_not_spawnable",
                                       {"status": "ready"}))
                    continue
                if not capacity_free or kb.check_respawn_guard(conn, row["id"]) is not None:
                    continue  # the wait is explained
                out.append(Finding(self.name, row["id"], "spawnable_ready_unclaimed",
                                   {"status": "ready"}))
        return out


class RunLeaseConsistency(Invariant):
    """The reclaim passes are actually keeping claims, runs and executions consistent."""

    name = "run_lease_consistency"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = ("kanban.db tasks (claim_expires, current_run_id), task_runs (ended_at), executions "
              "(status, heartbeat_at, ended_at) — the state release_stale_claims, "
              "reconcile_orphaned_running, detect_crashed_workers and exec_supervisor.reconcile maintain")
    failure = ("a running card whose claim expired more than lease_grace_seconds ago, a running card "
               "without an open current run, an open run whose card is not running on it, or a live "
               "execution whose heartbeat is older than execution_heartbeat_stale_seconds; 2 cycles")
    evidence = ("running_claim_expired_unreclaimed | running_without_open_run | open_run_detached:<run> | "
                "execution_heartbeat_stale_unreconciled:<execution>")

    def check(self, ctx: Context) -> list[Finding]:
        now = int(ctx.now())
        grace = int(ctx.config.get("lease_grace_seconds", 300))
        stale = int(ctx.config.get("execution_heartbeat_stale_seconds", 900))
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for task in conn.execute(
                "SELECT id, claim_expires, current_run_id FROM tasks WHERE status = 'running'"
            ).fetchall():
                if task["claim_expires"] is not None and now - int(task["claim_expires"]) > grace:
                    out.append(Finding(self.name, task["id"], "running_claim_expired_unreclaimed", {}))
                run = None
                if task["current_run_id"] is not None:
                    run = conn.execute("SELECT ended_at FROM task_runs WHERE id = ?",
                                       (task["current_run_id"],)).fetchone()
                if run is None or run["ended_at"] is not None:
                    out.append(Finding(self.name, task["id"], "running_without_open_run", {}))
            for run in conn.execute(
                "SELECT r.id, r.task_id, t.status, t.current_run_id FROM task_runs r "
                "JOIN tasks t ON t.id = r.task_id WHERE r.ended_at IS NULL"
            ).fetchall():
                if run["status"] != "running" or run["current_run_id"] != run["id"]:
                    out.append(Finding(self.name, run["task_id"], f"open_run_detached:{run['id']}", {}))
            for ex in conn.execute(
                "SELECT id, task_id, heartbeat_at, started_at FROM executions "
                "WHERE ended_at IS NULL AND status IN ('launching', 'running')"
            ).fetchall():
                beat = ex["heartbeat_at"] if ex["heartbeat_at"] is not None else ex["started_at"]
                if beat is not None and now - int(beat) > stale:
                    out.append(Finding(self.name, ex["task_id"] or ex["id"],
                                       f"execution_heartbeat_stale_unreconciled:{ex['id']}",
                                       {"execution_id": ex["id"]}))
        return out


#: Events the verdict return path writes on a subject for a finished verifier.
_VERDICT_DELIVERY_KINDS = (
    "verifier_verdict_returned", "verifier_verdict_skipped", "verifier_verdict_unattested",
    "verifier_verdict_unreadable", "verification_blocker_returned", "verified_completion_deferred",
)


class VerdictReturnedToSubject(Invariant):
    name = "verdict_returned_to_subject"
    tier = TIER_LIGHT
    settle_seconds = 180
    source = ("kanban.db task_links/tasks + subject task_events of the return path "
              "(_return_verifier_verdict_to_subjects kinds) and kanban_db._verifier_reported_verdict")
    failure = ("a codex_verify child finished more than 180 s ago while its subject is still pending "
               "verification and no return-path event on the subject names that verifier")
    evidence = "verifier_done_verdict_undelivered:<verifier>; detail verifier_task, verdict"

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        now = int(ctx.now())
        marks = ",".join("?" for _ in _VERDICT_DELIVERY_KINDS)
        out: list[Finding] = []
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT c.id AS child, c.completed_at, l.parent_id AS subject FROM task_links l "
                "JOIN tasks c ON c.id = l.child_id JOIN tasks s ON s.id = l.parent_id "
                "WHERE c.executor_lane = ? AND c.status = 'done' "
                "AND s.verification_state = ? AND s.terminal_disposition IS NULL",
                (kb.EXECUTOR_LANE_CODEX_VERIFY, kb.VERIFICATION_PENDING),
            ).fetchall()
            for row in rows:
                if row["completed_at"] is not None and now - int(row["completed_at"]) < self.settle_seconds:
                    continue
                delivered = False
                for ev in conn.execute(
                    f"SELECT payload FROM task_events WHERE task_id = ? AND kind IN ({marks})",
                    (row["subject"], *_VERDICT_DELIVERY_KINDS),
                ):
                    try:
                        if json.loads(ev["payload"] or "{}").get("verifier_task") == row["child"]:
                            delivered = True
                            break
                    except json.JSONDecodeError:
                        continue
                if delivered:
                    continue
                verdict, _ = kb._verifier_reported_verdict(conn, row["child"])
                out.append(Finding(self.name, row["subject"],
                                   f"verifier_done_verdict_undelivered:{row['child']}",
                                   {"verifier_task": row["child"], "verdict": verdict or "none"}))
        return out


class VerifierChildStalledInTodo(Invariant):
    name = "verifier_child_stalled_in_todo"
    tier = TIER_LIGHT
    source = "kanban.db tasks/task_links (codex_verify children of pending subjects)"
    failure = ("a codex_verify child of a subject pending verification has sat in todo longer than "
               "verifier_todo_stall_seconds (the refused repair-leg PASS retry shape)")
    evidence = "codex_verifier_child_stalled_in_todo; detail subject"

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        now = int(ctx.now())
        stall = int(ctx.config.get("verifier_todo_stall_seconds", 900))
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for row in conn.execute(
                "SELECT c.id, c.created_at, l.parent_id FROM tasks c "
                "JOIN task_links l ON l.child_id = c.id JOIN tasks s ON s.id = l.parent_id "
                "WHERE c.executor_lane = ? AND c.status = 'todo' AND c.terminal_disposition IS NULL "
                "AND s.verification_state = ?",
                (kb.EXECUTOR_LANE_CODEX_VERIFY, kb.VERIFICATION_PENDING),
            ).fetchall():
                if now - int(row["created_at"] or now) > stall:
                    out.append(Finding(self.name, row["id"], "codex_verifier_child_stalled_in_todo",
                                       {"subject": row["parent_id"]}))
        return out


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


class GatewayPlatformsConnected(Invariant):
    """Telegram/report delivery path: the platforms alerts depend on are connected."""

    name = "gateway_platforms_connected"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = "~/.hermes/gateway_state.json (gateway/status.py): pid, gateway_state, platforms.<name>.state/writer_pid"
    failure = ("for each required_gateway_platforms entry: gateway not running or its pid dead, platform "
               "missing, state not connected, or its writer is not the live gateway process; 2 cycles")
    evidence = ("gateway_state_unreadable | gateway_not_running:<state> | gateway_pid_dead | platform_missing | "
                "platform_state:<state> | platform_writer_not_gateway; subject = platform")

    def check(self, ctx: Context) -> list[Finding]:
        required = ctx.config.get("required_gateway_platforms") or []
        if not required:
            return []
        path = ctx.hermes_home / "gateway_state.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return [Finding(self.name, "gateway", f"gateway_state_unreadable:{type(exc).__name__}", {})]
        if doc.get("gateway_state") != "running":
            return [Finding(self.name, "gateway", f"gateway_not_running:{doc.get('gateway_state')}", {})]
        pid = doc.get("pid")
        if not _pid_alive(pid):
            return [Finding(self.name, "gateway", "gateway_pid_dead", {})]
        platforms = doc.get("platforms") or {}
        out: list[Finding] = []
        for name in required:
            entry = platforms.get(name)
            if not isinstance(entry, dict):
                out.append(Finding(self.name, name, "platform_missing", {}))
            elif entry.get("state") != "connected":
                out.append(Finding(self.name, name, f"platform_state:{entry.get('state')}", {}))
            elif entry.get("writer_pid") != pid:
                out.append(Finding(self.name, name, "platform_writer_not_gateway", {}))
        return out


def _statvfs(path: str):
    return os.statvfs(path)


def _meminfo() -> dict:
    out = {}
    with open("/proc/meminfo", encoding="utf-8") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts and parts[0].isdigit():
                out[key.strip()] = int(parts[0])  # kB
    return out


def _loadavg5() -> float:
    with open("/proc/loadavg", encoding="utf-8") as fh:
        return float(fh.read().split()[1])


class ResourceThresholds(Invariant):
    """One place for the thresholds scattered across existing watchers.

    Values mirror the existing ones (``shared/health-check.sh`` disk/memory 90 %
    and load 2 x CPUs; the isolated-backend reaper's 1.5 GB MemAvailable and
    90 % swap); inode usage is new — no watcher checked it.
    """

    name = "resource_thresholds"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = "os.statvfs(paths), /proc/meminfo, /proc/loadavg, os.cpu_count(), <HERMES_HOME>/kanban.db-wal size"
    failure = ("disk or inode use at/over its percentage, MemAvailable under its percentage or MiB floor, "
               "swap use at/over its percentage, 5-minute load per CPU over its ratio, or the Kanban WAL "
               "over its size; 2 cycles")
    evidence = ("disk_used_over:<path> | inodes_used_over:<path> | memory_available_low | swap_used_over | "
                "load_per_cpu_over | kanban_wal_oversized; detail measured value and threshold")

    def check(self, ctx: Context) -> list[Finding]:
        cfg = ctx.config.get("resource_thresholds") or {}
        if not cfg:
            return []
        out: list[Finding] = []
        seen_devices: set = set()
        for path in cfg.get("paths", ["/"]):
            try:
                device = os.stat(path).st_dev
            except OSError:
                out.append(Finding(self.name, path, "path_unreadable", {}))
                continue
            if device in seen_devices:
                continue
            seen_devices.add(device)
            st = _statvfs(path)
            used = st.f_blocks - st.f_bfree
            if used + st.f_bavail:
                pct = used * 100.0 / (used + st.f_bavail)
                if pct >= float(cfg.get("disk_used_pct", 90)):
                    out.append(Finding(self.name, path, f"disk_used_over:{path}",
                                       {"used_pct": f"{pct:.1f}", "threshold": str(cfg.get("disk_used_pct", 90))}))
            iused = st.f_files - st.f_ffree
            if st.f_files and iused + st.f_favail:
                ipct = iused * 100.0 / (iused + st.f_favail)
                if ipct >= float(cfg.get("inode_used_pct", 90)):
                    out.append(Finding(self.name, path, f"inodes_used_over:{path}",
                                       {"used_pct": f"{ipct:.1f}", "threshold": str(cfg.get("inode_used_pct", 90))}))
        mem = _meminfo()
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        if total and available is not None:
            avail_pct = available * 100.0 / total
            if (avail_pct < float(cfg.get("mem_available_pct_min", 10))
                    or available / 1024 < float(cfg.get("mem_available_mib_min", 1536))):
                out.append(Finding(self.name, "memory", "memory_available_low",
                                   {"available_mib": str(available // 1024), "available_pct": f"{avail_pct:.1f}"}))
        swap_total, swap_free = mem.get("SwapTotal"), mem.get("SwapFree")
        if swap_total:
            swap_pct = (swap_total - (swap_free or 0)) * 100.0 / swap_total
            if swap_pct >= float(cfg.get("swap_used_pct", 90)):
                out.append(Finding(self.name, "swap", "swap_used_over", {"used_pct": f"{swap_pct:.1f}"}))
        cpus = os.cpu_count() or 1
        per_cpu = _loadavg5() / cpus
        if per_cpu > float(cfg.get("load_per_cpu", 2.0)):
            out.append(Finding(self.name, "load", "load_per_cpu_over", {"load_per_cpu": f"{per_cpu:.2f}"}))
        wal = ctx.hermes_home / "kanban.db-wal"
        limit = float(cfg.get("kanban_wal_mib", 512)) * 1024 * 1024
        if wal.exists() and wal.stat().st_size > limit:
            out.append(Finding(self.name, "kanban.db-wal", "kanban_wal_oversized",
                               {"size_mib": str(wal.stat().st_size // (1024 * 1024))}))
        return out


class OwnershipAndLinkage(Invariant):
    name = "ownership_and_linkage"
    tier = TIER_DEEP
    source = ("kanban_db.unowned_tasks, orphaned_repair_tasks + missing_repair_relations, "
              "orphaned_verifier_tasks (existing read-only census functions)")
    failure = ("a live (not done/archived) card without a resolvable owner, a live recovery card missing "
               "its required repairs/umbrella relations, or a live verifier card without a subject "
               "(relabelled subjects excluded; historical closed cards are history, not health)")
    evidence = "unowned_live_card | repair_card_missing_relations:<relations> | live_verifier_without_subject"

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        out: list[Finding] = []
        with ctx.kanban() as conn:
            live = {r["id"] for r in conn.execute(
                "SELECT id FROM tasks WHERE status NOT IN ('done', 'archived')")}
            for tid in kb.unowned_tasks(conn):
                out.append(Finding(self.name, tid, "unowned_live_card", {}))
            for tid in kb.orphaned_repair_tasks(conn):
                if tid in live:
                    missing = ",".join(kb.missing_repair_relations(conn, tid))
                    out.append(Finding(self.name, tid, f"repair_card_missing_relations:{missing}", {}))
            for tid in kb.orphaned_verifier_tasks(conn):
                if tid in live and not _is_relabelled_subject(conn, tid):
                    out.append(Finding(self.name, tid, "live_verifier_without_subject", {}))
        return out


class TaskGraphIntegrity(Invariant):
    name = "task_graph_integrity"
    tier = TIER_DEEP
    source = "kanban.db task_links, tasks (status, block_kind, terminal_disposition, executor_lane), task_relations"
    failure = ("a dependency cycle; a link to a card that does not exist; a todo child waiting on a parent "
               "automation has given up on (attempt budget exhausted, or abandoned but not closed); or a "
               "live claude_recovery card linked to nothing it recovers")
    evidence = ("link_cycle (subject = smallest id, detail members) | link_endpoint_missing | "
                "waiting_on_given_up_parent:<parent> | recovery_card_unlinked")

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        out: list[Finding] = []
        with ctx.kanban() as conn:
            ids = {r["id"] for r in conn.execute("SELECT id FROM tasks")}
            edges: dict[str, list[str]] = {}
            for link in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
                parent, child = link["parent_id"], link["child_id"]
                if parent not in ids or child not in ids:
                    present = child if child in ids else parent
                    out.append(Finding(self.name, present, "link_endpoint_missing", {}))
                    continue
                edges.setdefault(parent, []).append(child)
            for members in _cycles(edges):
                out.append(Finding(self.name, min(members), "link_cycle",
                                   {"members": ",".join(sorted(members))}))
            for row in conn.execute(
                "SELECT l.child_id, p.id AS parent FROM task_links l "
                "JOIN tasks c ON c.id = l.child_id JOIN tasks p ON p.id = l.parent_id "
                "WHERE c.status = 'todo' AND ("
                "  (p.status = 'blocked' AND p.block_kind = 'attempt_budget_exhausted') "
                "  OR (p.terminal_disposition = ? AND p.status NOT IN ('done', 'archived')))",
                (kb.DISPOSITION_ABANDONED,),
            ).fetchall():
                out.append(Finding(self.name, row["child_id"],
                                   f"waiting_on_given_up_parent:{row['parent']}", {"parent": row["parent"]}))
            for row in conn.execute(
                "SELECT t.id FROM tasks t WHERE t.executor_lane = ? "
                "AND t.status NOT IN ('done', 'archived') "
                "AND NOT EXISTS (SELECT 1 FROM task_links l WHERE l.parent_id = t.id OR l.child_id = t.id) "
                "AND NOT EXISTS (SELECT 1 FROM task_relations r WHERE r.from_task_id = t.id OR r.to_task_id = t.id)",
                (kb.EXECUTOR_LANE_CLAUDE_RECOVERY,),
            ).fetchall():
                out.append(Finding(self.name, row["id"], "recovery_card_unlinked", {}))
        return out


def _cycles(edges: dict[str, list[str]]) -> list[frozenset]:
    """Strongly connected components of size > 1 (or a self-loop), iteratively."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set = set()
    stack: list[str] = []
    found: list[frozenset] = []
    counter = 0
    nodes = set(edges) | {c for children in edges.values() for c in children}
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(edges.get(child, ()))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1 or node in edges.get(node, ()):
                    found.append(frozenset(component))
    return found


class ControlDefectRegressions(Invariant):
    """Supervisory invariants for the control defects repaired on 2026-09-14.

    Watches for recurrence after ``control_defect_watch_since`` (the deploy of
    the repairs). The historical INC-2026-09-14-01 attachments predate it and
    belong to the F2 company-isolation decision; they are not hidden, they are
    simply not a regression of the repaired harvester.
    """

    name = "control_defect_regressions"
    tier = TIER_DEEP
    source = ("kanban.db gauntlet_stale_disposition/blocked/commented events + executions.ended_at "
              "(rule: kanban_db INFRASTRUCTURE_AUTO_RELEASE_WINDOW_SECONDS, AUTOMATION_COMMENT_AUTHORS); "
              "task_attachments by claude-lane (rule: recovery_lane._HARVEST_DENIED_SUFFIXES); "
              "objective_attempt_budget_granted events (rule: kanban_db._automation_identity)")
    failure = ("since control_defect_watch_since: stale supervision released an infrastructure park past "
               "the retry window or after a non-automation comment; the claude lane attached a denied "
               "log/database/bytecode/key file (unsafe); or, at any time, an attempt grant not made by a "
               "named human_interactive operator (unsafe)")
    evidence = ("stale_release_violated:<reason>:<execution> | harvested_denied_file:<attachment id> (unsafe) | "
                "attempt_grant_by_automation:<event id> (unsafe)")

    def check(self, ctx: Context) -> list[Finding]:
        kb = _kb()
        from hermes_cli import recovery_lane
        since = int(ctx.config.get("control_defect_watch_since") or 0)
        out: list[Finding] = []
        with ctx.kanban() as conn:
            for ev in conn.execute(
                "SELECT id, task_id, payload, created_at FROM task_events "
                "WHERE kind = 'gauntlet_stale_disposition' AND created_at >= ?", (since,),
            ).fetchall():
                try:
                    payload = json.loads(ev["payload"] or "{}")
                except json.JSONDecodeError:
                    continue
                if payload.get("action") != "infrastructure_recovery_released":
                    continue
                execution = conn.execute("SELECT ended_at FROM executions WHERE id = ?",
                                         (payload.get("execution_id"),)).fetchone()
                if execution is None or execution["ended_at"] is None:
                    continue
                released_at = int(payload.get("detected_at") or ev["created_at"])
                reason = None
                if released_at - int(execution["ended_at"]) > kb.INFRASTRUCTURE_AUTO_RELEASE_WINDOW_SECONDS:
                    reason = "retry_window_expired"
                else:
                    block = conn.execute(
                        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'blocked' AND id < ? "
                        "ORDER BY id DESC LIMIT 1", (ev["task_id"], ev["id"]),
                    ).fetchone()
                    for comment in conn.execute(
                        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented' "
                        "AND id > ? AND id < ?", (ev["task_id"], block["id"] if block else 0, ev["id"]),
                    ):
                        try:
                            author = str(json.loads(comment["payload"] or "{}").get("author") or "")
                        except json.JSONDecodeError:
                            author = ""
                        if not (author.endswith("-lane") or author in kb.AUTOMATION_COMMENT_AUTHORS):
                            reason = "owner_engaged"
                            break
                if reason:
                    out.append(Finding(self.name, ev["task_id"],
                                       f"stale_release_violated:{reason}:{payload.get('execution_id')}", {}))
            for att in conn.execute(
                "SELECT id, task_id, filename FROM task_attachments "
                "WHERE uploaded_by = 'claude-lane' AND created_at >= ?", (since,),
            ).fetchall():
                name = str(att["filename"] or "")
                if (name.endswith(recovery_lane._HARVEST_DENIED_SUFFIXES)
                        or name.startswith(".env") or ".log." in name):
                    out.append(Finding(self.name, att["task_id"], f"harvested_denied_file:{att['id']}",
                                       {"attachment_id": str(att["id"])}, unsafe=True))
            for ev in conn.execute(
                "SELECT id, task_id, payload FROM task_events WHERE kind = ?",
                (kb.OBJECTIVE_ATTEMPT_GRANT_EVENT,),
            ).fetchall():
                try:
                    payload = json.loads(ev["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                authorizer = str(payload.get("authorized_by") or "")
                actor = str(payload.get("actor_id") or "")
                if (payload.get("actor_kind") != "human_interactive" or not authorizer.strip()
                        or not actor.strip() or kb._automation_identity(authorizer)
                        or kb._automation_identity(actor)):
                    out.append(Finding(self.name, ev["task_id"], f"attempt_grant_by_automation:{ev['id']}",
                                       {}, unsafe=True))
        return out


class LifeWikiDailyNote(Invariant):
    name = "life_wiki_daily_note"
    tier = TIER_DEEP
    source = ("the Life Wiki vault Logs/daily/<local date>.md (written by the 04:00 nightly reconciliation); "
              "the life-wiki-daily-validation result is watched by critical_cron_jobs_healthy")
    failure = "after cutoff_hour local time, today's daily note does not exist (or the vault is missing)"
    evidence = "daily_note_missing_after_cutoff:<date> | vault_missing"

    def check(self, ctx: Context) -> list[Finding]:
        cfg = ctx.config.get("life_wiki_daily_note") or {}
        if not cfg:
            return []
        from zoneinfo import ZoneInfo
        vault = Path(os.path.expanduser(cfg["vault"]))
        if not vault.is_dir():
            return [Finding(self.name, "life-wiki-daily-note", "vault_missing", {})]
        local = _dt.datetime.fromtimestamp(ctx.now(), ZoneInfo(cfg.get("timezone", "America/Chicago")))
        if local.hour < int(cfg.get("cutoff_hour", 6)):
            return []
        day = local.date().isoformat()
        if not (vault / "Logs" / "daily" / f"{day}.md").is_file():
            return [Finding(self.name, "life-wiki-daily-note", f"daily_note_missing_after_cutoff:{day}", {})]
        return []


class BackupResults(Invariant):
    name = "backup_results"
    tier = TIER_DEEP
    source = ("~/.hermes/state/dr-backup-verify.json (dr-backup-verify.sh: verdict, ran_at — itself checks "
              "the nuclear backup audit log) and the newest hermes-*.dump in the Postgres daily backup dir")
    failure = ("DR verify verdict not OK, its result older than dr_max_age_seconds or unreadable; no Postgres "
               "dump, the newest older than postgres_max_age_seconds, or smaller than postgres_min_bytes")
    evidence = ("dr_state_unreadable | dr_verdict:<verdict> | dr_verify_stale | postgres_dump_missing | "
                "postgres_dump_stale | postgres_dump_too_small")

    def check(self, ctx: Context) -> list[Finding]:
        cfg = ctx.config.get("backup_results") or {}
        if not cfg:
            return []
        now = ctx.now()
        out: list[Finding] = []
        state_path = Path(os.path.expanduser(cfg["dr_verify_state"]))
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append(Finding(self.name, "dr-backup-verify", f"dr_state_unreadable:{type(exc).__name__}", {}))
        else:
            if state.get("verdict") != "OK":
                out.append(Finding(self.name, "dr-backup-verify", f"dr_verdict:{state.get('verdict')}", {}))
            ran = _parse_ts(str(state.get("ran_at") or "").replace("Z", "+00:00"))
            if ran is None or now - ran > int(cfg.get("dr_max_age_seconds", 93600)):
                out.append(Finding(self.name, "dr-backup-verify", "dr_verify_stale", {}))
        dump_dir = Path(os.path.expanduser(cfg["postgres_dump_dir"]))
        dumps = sorted(dump_dir.glob(cfg.get("postgres_dump_glob", "hermes-*.dump")),
                       key=lambda p: p.stat().st_mtime) if dump_dir.is_dir() else []
        if not dumps:
            out.append(Finding(self.name, "postgres-daily", "postgres_dump_missing", {}))
        else:
            newest = dumps[-1].stat()
            if now - newest.st_mtime > int(cfg.get("postgres_max_age_seconds", 93600)):
                out.append(Finding(self.name, "postgres-daily", "postgres_dump_stale", {}))
            if newest.st_size < int(cfg.get("postgres_min_bytes", 1048576)):
                out.append(Finding(self.name, "postgres-daily", "postgres_dump_too_small", {}))
        return out


class EscalationCardsDispositioned(Invariant):
    """Detection without an actionable disposition is itself a health failure."""

    name = "escalation_cards_dispositioned"
    tier = TIER_DEEP
    source = "kanban.db tasks created by system-health-controller (idempotency_key health:*)"
    failure = ("a controller escalation card is still unassigned in triage past "
               "escalation_card_max_undispositioned_seconds (default 7 days, the doctrine triage hygiene threshold)")
    evidence = "undispositioned_past_threshold; detail count, oldest card id"

    def check(self, ctx: Context) -> list[Finding]:
        limit = int(ctx.config.get("escalation_card_max_undispositioned_seconds", 604800))
        cutoff = int(ctx.now()) - limit
        with ctx.kanban() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key LIKE 'health:%' AND created_by = ? "
                "AND status = 'triage' AND assignee IS NULL AND created_at < ? ORDER BY created_at",
                (CONTROLLER_ID, cutoff),
            ).fetchall()
        if not rows:
            return []
        return [Finding(self.name, "health-escalation-cards", "undispositioned_past_threshold",
                        {"count": str(len(rows)), "oldest": rows[0]["id"]})]


# ---------------------------------------------------------------------------
# F2 detect-only invariants (Christopher, 2026-09-14): endpoint health (shared
# and authorized active companies), watcher integrity, repository drift,
# company isolation. No recovery.
# ---------------------------------------------------------------------------

HEALTH_HEALTHY = "HEALTHY"
HEALTH_UNREACHABLE = "UNREACHABLE"
HEALTH_TIMEOUT = "TIMEOUT"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _http_probe(url: str, timeout: float) -> tuple[str, Optional[int], int]:
    """Minimal, non-content health probe: ``(coarse, http_status, latency_ms)``.

    Unauthenticated GET to a loopback ``http`` URL only. No credentials, no
    cookies, no custom auth headers; redirects are not followed; the response
    body and headers are never read or returned — only the status line.
    """
    import http.client
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS or parts.username or parts.password:
        raise ValueError("health probes are limited to unauthenticated loopback http URLs")
    path = parts.path or "/"
    started = time.monotonic()
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close", "User-Agent": "norcal-health-probe"})
        response = conn.getresponse()
        status = int(response.status)
        response.close()  # the body is never read
    except (socket.timeout, TimeoutError):
        return HEALTH_TIMEOUT, None, int((time.monotonic() - started) * 1000)
    except (OSError, http.client.HTTPException):
        return HEALTH_UNREACHABLE, None, int((time.monotonic() - started) * 1000)
    finally:
        conn.close()
    latency = int((time.monotonic() - started) * 1000)
    coarse = HEALTH_HEALTHY if 200 <= status < 300 else f"HTTP_{status // 100}XX"
    return coarse, status, latency


def company_probe_authorization_digest(block: dict) -> str:
    """sha256 over the authorized probe set, exclusions and authorizer."""
    scope = {
        "authorized_by": block.get("authorized_by"),
        "entities": sorted(
            (str(e.get("entity_id")), str(e.get("service")), str(e.get("url")))
            for e in (block.get("entities") or []) if isinstance(e, dict)
        ),
        "excluded": sorted(str(e.get("entity_id")) for e in (block.get("excluded_entities") or [])
                           if isinstance(e, dict)),
    }
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _company_probe_problem(block: dict) -> Optional[str]:
    if not str(block.get("authorized_by") or "").strip():
        return "missing_authorized_by"
    entities = block.get("entities")
    excluded = block.get("excluded_entities")
    if not isinstance(entities, list) or not isinstance(excluded, list):
        return "malformed"
    excluded_ids = {str(e.get("entity_id")) for e in excluded if isinstance(e, dict)}
    for entry in entities:
        if not isinstance(entry, dict) or not all(str(entry.get(k) or "").strip()
                                                  for k in ("entity_id", "service", "url")):
            return "malformed_entity"
        if str(entry["entity_id"]) in excluded_ids:
            return "excluded_entity_listed"   # excluded always wins: probe nothing
    if block.get("authorization_sha256") != company_probe_authorization_digest(block):
        return "authorization_digest_mismatch"
    return None


class CompanyHealthEndpoints(Invariant):
    """Active company health through a minimal, non-content probe.

    "Active company health may be observed through a minimal, non-content health
    probe. Dormant companies are excluded. Company health observations never
    become shared company data." (Christopher, 2026-09-14.) Only the authorized
    entities are probed; excluded (dormant) entities are never contacted. The
    exact status code and latency go only to a per-entity 0600 record; shared
    findings carry the entity id and coarse state, and route to the company lane.
    """

    name = "company_health_endpoints"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = ("company_health_probes (Christopher-authorized set, pinned by authorization_sha256): "
              "unauthenticated loopback GET of each active entity's designated health endpoint, status line only")
    failure = ("an authorized entity's health endpoint is UNREACHABLE, TIMEOUT or not 2xx for 2 cycles; or the "
               "probe authorization is invalid (then nothing is probed)")
    evidence = ("<coarse state> on subject = entity id (route company: no shared card); per-entity record "
                "<record_dir>/<entity_id>.json with entity_id, service, timestamp, http_status, latency_ms, result; "
                "probe_authorization_invalid:<problem> on subject company_health_probes")

    def check(self, ctx: Context) -> list[Finding]:
        block = ctx.config.get("company_health_probes")
        if not block:
            return []
        problem = _company_probe_problem(block)
        if problem:
            return [Finding(self.name, "company_health_probes", f"probe_authorization_invalid:{problem}", {})]
        timeout = float(block.get("timeout_seconds", 5))
        record_dir = Path(os.path.expanduser(block.get("record_dir") or
                                             str(ctx.state_dir / "company-health")))
        out: list[Finding] = []
        for entry in block["entities"]:
            coarse, status, latency = _http_probe(entry["url"], timeout)
            if not ctx.dry_run:
                record_dir.mkdir(parents=True, exist_ok=True)
                os.chmod(record_dir, 0o700)
                path = record_dir / f"{entry['entity_id']}.json"
                _atomic_write_json(path, {
                    "entity_id": entry["entity_id"], "service": entry["service"],
                    "timestamp": _iso(ctx.now()), "http_status": status,
                    "latency_ms": latency, "result": coarse,
                })
                os.chmod(path, 0o600)
            if coarse != HEALTH_HEALTHY:
                out.append(Finding(self.name, entry["entity_id"], coarse,
                                   {"entity_id": entry["entity_id"], "service": entry["service"]},
                                   route=ROUTE_COMPANY))
        return out


class SharedEndpointsHealthy(Invariant):
    name = "shared_endpoints_healthy"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = "shared_health_endpoints: loopback GET of shared-infrastructure health routes (same status-line-only probe)"
    failure = "a shared endpoint is UNREACHABLE, TIMEOUT or not 2xx for 2 cycles"
    evidence = "<coarse state> on subject = endpoint name; detail http_status"

    def check(self, ctx: Context) -> list[Finding]:
        endpoints = ctx.config.get("shared_health_endpoints") or {}
        timeout = float(ctx.config.get("shared_probe_timeout_seconds", 5))
        out: list[Finding] = []
        for name, url in endpoints.items():
            coarse, status, _ = _http_probe(url, timeout)
            if coarse != HEALTH_HEALTHY:
                out.append(Finding(self.name, name, coarse, {"http_status": str(status)}))
        return out


class SharedUnitsActive(Invariant):
    name = "shared_units_active"
    tier = TIER_LIGHT
    confirm_cycles = 2
    source = "systemctl [--user] is-active for shared_user_units and shared_system_units (shared infrastructure only)"
    failure = "a shared service unit is not active for 2 cycles"
    evidence = "not_active:<state> on subject = unit"

    def check(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        for scope, units in (("--user", ctx.config.get("shared_user_units") or []),
                             (None, ctx.config.get("shared_system_units") or [])):
            for unit in units:
                argv = ["systemctl"] + ([scope] if scope else []) + ["is-active", unit]
                _, text = ctx.run_command(argv, 30)
                state = text.strip() or "unknown"
                if state != "active":
                    out.append(Finding(self.name, unit, f"not_active:{state}", {}))
        return out


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WatcherIntegrity(Invariant):
    """Watcher-of-watchers completeness beyond job freshness."""

    name = "watcher_integrity"
    tier = TIER_DEEP
    source = ("systemctl --user list-timers/is-failed (a critical timer's service result), "
              "systemctl --user list-unit-files --output=json (expected_user_unit_files), crontab -l "
              "(presence of crontab_watchers only; lines never stored), sha256 of pinned_scripts")
    failure = ("a critical timer's service is failed; an expected unit file is not installed; a crontab watcher "
               "entry is missing; a pinned watcher script is missing or its sha256 changed (freshness of "
               "crontab watchers is not observable: they log only when they act)")
    evidence = ("timer_service_failed on subject = service | unit_file_missing | crontab_entry_missing | "
                "script_missing | script_checksum_drift on subject = script path")

    def check(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        timers = ctx.config.get("critical_user_timers") or {}
        if timers:
            rc, text = ctx.run_command(["systemctl", "--user", "list-timers", "--all", "--output=json"], 30)
            try:
                listed = {t.get("unit"): t for t in json.loads(text)} if rc == 0 else {}
            except (json.JSONDecodeError, TypeError, AttributeError):
                listed = {}
            for unit in timers:
                service = (listed.get(unit) or {}).get("activates")
                if not service:
                    continue  # timer listing gaps are critical_timers_active's findings
                _, state = ctx.run_command(["systemctl", "--user", "is-failed", service], 30)
                if state.strip() == "failed":
                    out.append(Finding(self.name, service, "timer_service_failed", {}))
        expected = ctx.config.get("expected_user_unit_files") or []
        if expected:
            rc, text = ctx.run_command(["systemctl", "--user", "list-unit-files", "--output=json"], 30)
            try:
                present = {u.get("unit_file") for u in json.loads(text)} if rc == 0 else None
            except (json.JSONDecodeError, TypeError, AttributeError):
                present = None
            if present is None:
                out.append(Finding(self.name, "systemd-user", f"unit_file_listing_failed:rc={rc}", {}))
            else:
                out.extend(Finding(self.name, unit, "unit_file_missing", {})
                           for unit in expected if unit not in present)
        watchers = ctx.config.get("crontab_watchers") or []
        if watchers:
            rc, crontab = ctx.run_command(["crontab", "-l"], 30)
            for script in watchers:
                if rc != 0 or script not in crontab:
                    out.append(Finding(self.name, script, "crontab_entry_missing", {}))
            del crontab
        for raw_path, pinned in (ctx.config.get("pinned_scripts") or {}).items():
            path = Path(os.path.expanduser(raw_path))
            if not path.is_file():
                out.append(Finding(self.name, raw_path, "script_missing", {}))
            elif _sha256_file(path) != pinned:
                out.append(Finding(self.name, raw_path, "script_checksum_drift", {}))
        return out


class RepositoryDrift(Invariant):
    name = "repository_drift"
    tier = TIER_DEEP
    source = ("~/.hermes/state/deploy-drift-check.json (deploy-drift-check.sh verdict over the company/service "
              "repos, used coarse) and git --no-optional-locks status / rev-list against the local upstream ref "
              "for watched_repositories (no fetch)")
    failure = ("deploy-drift verdict not CLEAN or older than deploy_drift_max_age_seconds; a watched shared "
               "repository has uncommitted changes, commits not on its upstream, or is behind it")
    evidence = ("deploy_drift_verdict:<v> | deploy_drift_stale | deploy_drift_unreadable | worktree_dirty | "
                "unpushed_commits | behind_upstream | repo_unreadable (subject = repository name; counts in detail)")

    def check(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        cfg = ctx.config.get("repository_drift") or {}
        if not cfg:
            return []
        state_path = Path(os.path.expanduser(cfg["deploy_drift_state"]))
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            out.append(Finding(self.name, "deploy-drift-check", f"deploy_drift_unreadable:{type(exc).__name__}", {}))
        else:
            if state.get("verdict") != "CLEAN":
                out.append(Finding(self.name, "deploy-drift-check", f"deploy_drift_verdict:{state.get('verdict')}", {}))
            ran = _parse_ts(str(state.get("ran_at") or "").replace("Z", "+00:00"))
            if ran is None or ctx.now() - ran > int(cfg.get("deploy_drift_max_age_seconds", 25200)):
                out.append(Finding(self.name, "deploy-drift-check", "deploy_drift_stale", {}))
        for repo in cfg.get("watched_repositories") or []:
            path = os.path.expanduser(repo["path"])
            rc, text = ctx.run_command(["git", "--no-optional-locks", "-C", path, "status", "--porcelain"], 30)
            if rc != 0:
                out.append(Finding(self.name, repo["name"], "repo_unreadable", {}))
                continue
            dirty = len([ln for ln in text.splitlines() if ln.strip()])
            if dirty:
                out.append(Finding(self.name, repo["name"], "worktree_dirty", {"entries": str(dirty)}))
            upstream = repo.get("upstream")
            if upstream:
                for signature, spec in (("unpushed_commits", f"{upstream}..HEAD"),
                                        ("behind_upstream", f"HEAD..{upstream}")):
                    rc, count = ctx.run_command(["git", "-C", path, "rev-list", "--count", spec], 30)
                    if rc == 0 and count.strip().isdigit() and int(count.strip()) > 0:
                        out.append(Finding(self.name, repo["name"], signature, {"commits": count.strip()}))
        return out


class CompanyIsolation(Invariant):
    """Active-company isolation and routing integrity. Boundary findings are unsafe."""

    name = "company_isolation"
    tier = TIER_DEEP
    source = ("~/.local/bin/entity-registry-check exit code (output discarded); kanban.db task_attachments "
              "filenames on cards, matched against company_isolation.company_tokens / lead_profiles and "
              "recovery_lane._HARVEST_DENIED_SUFFIXES (filenames never recorded)")
    failure = ("registry/runtime routing drift (exit 1) or an incomplete check (exit 2); a card assigned to one "
               "company's lead carrying another company's files; a non-company card carrying company-named or "
               "runtime-state files. New occurrences (since isolation_watch_since) are per card; the historical "
               "set is one inventory finding. All attachment findings are unsafe: never excepted, escalated at once")
    evidence = ("registry_runtime_drift | registry_check_incomplete | registry_check_failed:rc=<n> | "
                "boundary_attachments_on_card (subject card, detail count) | "
                "historical_boundary_attachment_inventory (detail card count and ids)")

    def check(self, ctx: Context) -> list[Finding]:
        cfg = ctx.config.get("company_isolation") or {}
        if not cfg:
            return []
        from hermes_cli import recovery_lane
        out: list[Finding] = []
        command = cfg.get("registry_check_command")
        if command:
            rc, _ = ctx.run_command([os.path.expanduser(part) for part in command], 120)
            if rc == 1:
                out.append(Finding(self.name, "entity-registry", "registry_runtime_drift", {}))
            elif rc == 2:
                out.append(Finding(self.name, "entity-registry", "registry_check_incomplete", {}))
            elif rc != 0:
                out.append(Finding(self.name, "entity-registry", f"registry_check_failed:rc={rc}", {}))
        companies = cfg.get("companies") or {}
        lead_to_company = {lead: cid for cid, spec in companies.items() for lead in spec.get("lead_profiles", [])}
        watch_since = int(cfg.get("isolation_watch_since") or 0)
        counts: dict[str, int] = {}
        historical: dict[str, int] = {}
        with ctx.kanban() as conn:
            for row in conn.execute(
                "SELECT a.task_id, a.filename, a.created_at, t.assignee FROM task_attachments a "
                "JOIN tasks t ON t.id = a.task_id"
            ).fetchall():
                name = str(row["filename"] or "").lower()
                owner = lead_to_company.get(row["assignee"] or "")
                foreign = [cid for cid, spec in companies.items() if cid != owner
                           and any(tok in name for tok in spec.get("tokens", []))]
                runtime = (owner is None and (name.endswith(recovery_lane._HARVEST_DENIED_SUFFIXES)
                                              or name.startswith(".env") or ".log." in name))
                if not foreign and not runtime:
                    continue
                bucket = counts if int(row["created_at"] or 0) >= watch_since else historical
                bucket[row["task_id"]] = bucket.get(row["task_id"], 0) + 1
        for task_id, n in sorted(counts.items()):
            out.append(Finding(self.name, task_id, "boundary_attachments_on_card", {"count": str(n)}, unsafe=True))
        if historical:
            ids = sorted(historical)
            out.append(Finding(self.name, "attachment-boundary-inventory", "historical_boundary_attachment_inventory",
                               {"cards": str(len(ids)), "card_ids": ",".join(ids)[:1500]}, unsafe=True))
        return out


F2_DETECT_ONLY = (CompanyHealthEndpoints, SharedEndpointsHealthy, SharedUnitsActive, WatcherIntegrity,
                  RepositoryDrift, CompanyIsolation)

#: F1 invariants are detection only: none defines a recovery.
F1_DETECT_ONLY = (
    ReadyBacklogExplained, RunLeaseConsistency, VerdictReturnedToSubject, VerifierChildStalledInTodo,
    GatewayPlatformsConnected, ResourceThresholds, OwnershipAndLinkage, TaskGraphIntegrity,
    ControlDefectRegressions, LifeWikiDailyNote, BackupResults, EscalationCardsDispositioned,
)


def default_invariants() -> list[Invariant]:
    return [
        VerifierRouteOpen(),
        SubjectLaneRelabelled(),
        VerifierChildDeadlocked(),
        SubjectReviewRegressed(),
        GatewayHeartbeatFresh(),
        CriticalCronJobsHealthy(),
        CounterpartHeartbeatFresh(TIER_LIGHT),
        ReadyBacklogExplained(),
        RunLeaseConsistency(),
        VerdictReturnedToSubject(),
        VerifierChildStalledInTodo(),
        GatewayPlatformsConnected(),
        ResourceThresholds(),
        CompanyHealthEndpoints(),
        SharedEndpointsHealthy(),
        SharedUnitsActive(),
        VerifierOfVerifier(),
        VerifiedClosureAttributable(),
        CriticalTimersActive(),
        CanonicalBootComplete(),
        RuntimeCeilingsMatchDoctrine(),
        CounterpartHeartbeatFresh(TIER_DEEP),
        OwnershipAndLinkage(),
        TaskGraphIntegrity(),
        ControlDefectRegressions(),
        LifeWikiDailyNote(),
        BackupResults(),
        EscalationCardsDispositioned(),
        WatcherIntegrity(),
        RepositoryDrift(),
        CompanyIsolation(),
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
    sub.add_parser("invariants", help="print every invariant's tier, source, failure condition and evidence")
    args = parser.parse_args(argv)
    if args.command == "invariants":
        print(json.dumps([
            {"name": inv.name, "tier": inv.tier, "detect_only": type(inv).recover is Invariant.recover,
             "source": inv.source, "failure": inv.failure, "evidence": inv.evidence}
            for inv in default_invariants()
        ], indent=2))
        return 0
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
