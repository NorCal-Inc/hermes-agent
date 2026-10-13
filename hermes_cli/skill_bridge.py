"""Vault-first learned-skill bridge and need-based profile skill provisioning.

Two halves, one module, built for kanban card ``t_9c745fdc`` (Erika's design
decision of 2026-10-12, Christopher's standing direction):

1. **Vault bridge** — the LifeWiki vault ``Skills/<capability>/`` canonicals
   are the knowledge source of truth. Runtime learning (``/learn`` output,
   ``skill_manage`` writes held in a Hermes home's ``pending/skills`` store)
   is mirrored into the vault as a *pending observation* through the
   supported boundary ``POST /api/skills/<cap>/observe``. An observation is
   non-binding until ``promote`` approves it through ``/verify``, which the
   bridge only does with evidence: a kanban task whose verification state is
   ``verified`` (the Gauntlet's independent verdict), or an explicit,
   attributed operator attestation. Approval appends to the vault
   ``doctrine.md`` first; only then may a runtime projection be *staged* (never
   written) into a Hermes home for operator review. Every step is recorded in
   a ledger with the vault observation id and the content hashes on both
   sides so drift is detectable.

2. **Need-based provisioning** — before a kanban worker is spawned with
   per-task ``skills`` the dispatcher asks :func:`preflight_task_skills`. A
   skill missing from the target profile is copied *only* from an approved
   baseline whose bytes still match their immutable hash: the root skill tree
   as pinned in the root-owned skill-trust registry (shared skills), or a
   company-lane baseline attested in the bridge's own ledger (company
   skills, provisionable only inside that company's lane). Unregistered,
   drifted, cross-lane or already-diverged targets fail closed with a typed
   refusal; the dispatcher turns that into a ``capability`` block that routes
   to Erika, and the card returns to the same assignee once provisioned.

Runtime ``~/.hermes/**/skills`` packages are executable projections. Nothing
in this module writes into a frozen root skill, and nothing here edits the
skill-trust registry or the skill-integrity lock; a replacement for frozen
bytes still requires the governed review/reseal path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

DEFAULT_TRUST_REGISTRY = Path("/home/chris/.hermes/security/skill-trust/registry.json")
PROVENANCE_FILE = ".provenance.json"
LEDGER_FILE = "ledger.jsonl"
OBSERVATIONS_FILE = "observations.jsonl"
COMPANY_BASELINES_FILE = "company-baselines.json"
MATERIALIZE_REL_PATH = "references/vault-lessons.md"
# Summary prefix on pending records the bridge itself stages (runtime
# projections of already-promoted vault lessons). The mirror must skip these,
# or a projection would be re-observed into the vault as new learning (loop).
PROJECTION_SUMMARY_PREFIX = "vault-bridge projection of "

DEFAULT_LIFEWIKI_URL = "http://127.0.0.1:5100"
VERIFIER_TOKEN_ENV = "SKILL_VERIFIER_TOKEN"
VERIFIER_HEADER = "x-verifier-token"

SHARED_LANE = "shared"

# Refusal reason codes (stable strings; the dispatcher and Erika's capability
# path key on them).
REFUSED_UNKNOWN_SKILL = "UNKNOWN_SKILL"
REFUSED_UNREGISTERED = "UNREGISTERED_BASELINE"
REFUSED_SOURCE_DRIFT = "SOURCE_DRIFT"
REFUSED_TARGET_DRIFT = "TARGET_DRIFT"
REFUSED_COMPANY_SCOPE = "COMPANY_SCOPE"
REFUSED_COPY_VERIFY = "COPY_VERIFY_FAILED"
REFUSED_NO_PROFILE = "NO_SUCH_PROFILE"
REFUSED_AMBIGUOUS = "AMBIGUOUS_SKILL_NAME"

STATUS_PRESENT = "present"
STATUS_PROVISIONED = "provisioned"
STATUS_WOULD_PROVISION = "would_provision"
STATUS_REFUSED = "refused"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CAP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OBS_RE = re.compile(r"^obs_[a-z0-9_]+$")

# Runtime skill category -> vault capability. Anything unmapped lands in
# ``knowledge`` (the vault's research/reference capability) unless the caller
# names a capability explicitly.
CATEGORY_CAPABILITY_MAP = {
    "email": "email",
    "governance": "governance",
    "stripe": "stripe",
    "payments": "stripe",
    "website-health": "website-health",
    "web": "website-health",
    "knowledge": "knowledge",
    "research": "knowledge",
    "verification": "verification",
    "communication": "communication",
    "incident-response": "incident-response",
    "incident": "incident-response",
    "deploy": "deploy",
    "devops": "ops",
    "ops": "ops",
    "marketing": "marketing",
    "media": "media",
    "creative": "media",
    "hermes-api": "hermes-api",
}
DEFAULT_CAPABILITY = "knowledge"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hermes_root() -> Path:
    """The root Hermes home (``~/.hermes``), never a profile home."""
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def trust_registry_path() -> Path:
    env = os.environ.get("HERMES_SKILL_TRUST_REGISTRY", "").strip()
    return Path(env) if env else DEFAULT_TRUST_REGISTRY


def bridge_dir() -> Path:
    env = os.environ.get("HERMES_SKILL_BRIDGE_DIR", "").strip()
    return Path(env) if env else hermes_root() / "skill-bridge"


def lifewiki_url() -> str:
    return (os.environ.get("LIFEWIKI_URL") or DEFAULT_LIFEWIKI_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Hashing — byte-identical to security/skill-trust/verify.py::tree_hash so a
# profile copy can be checked against the root-owned registry entry.
# ---------------------------------------------------------------------------

def tree_hash(root: Path | str) -> str:
    root = Path(root)
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if "__pycache__" in p.parts or p.suffix in {".pyc", ".pyo"}:
            continue
        if ".bak-" in p.name or p.name.endswith(".bak-20260821-supply-chain"):
            continue
        if p.is_symlink():
            h.update(b"L\0" + rel.encode() + b"\0" + os.readlink(p).encode() + b"\0")
        elif p.is_file():
            h.update(b"F\0" + rel.encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest() + b"\0")
    return h.hexdigest()


def _copy_ignore(_dir: str, names: List[str]) -> set:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


# ---------------------------------------------------------------------------
# Skill lookup
# ---------------------------------------------------------------------------

def find_skill_dir(skills_dir: Path, name: str) -> Tuple[Optional[Path], Optional[str]]:
    """Locate ``name`` (``skill`` or ``category/skill``) under *skills_dir*.

    Returns ``(path, error_code)``. A skill is a directory holding
    ``SKILL.md``. Dot-directories (``.archive``, ``.curator_backups``) are
    never searched. Two matches for a bare name is ambiguous and refused.
    """
    name = (name or "").strip().strip("/")
    if not name or not skills_dir.is_dir():
        return None, REFUSED_UNKNOWN_SKILL
    if "/" in name:
        cand = skills_dir / name
        if (cand / "SKILL.md").is_file() and not any(part.startswith(".") for part in name.split("/")):
            return cand, None
        return None, REFUSED_UNKNOWN_SKILL
    matches: List[Path] = []
    for p in skills_dir.rglob("SKILL.md"):
        d = p.parent
        rel_parts = d.relative_to(skills_dir).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if d.name == name:
            matches.append(d)
    if not matches:
        return None, REFUSED_UNKNOWN_SKILL
    if len(matches) > 1:
        return None, REFUSED_AMBIGUOUS
    return matches[0], None


def load_trust_registry() -> Dict[str, dict]:
    """Registry items keyed by resolved absolute path. Empty when unreadable."""
    path = trust_registry_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    for item in data.get("items", []) or []:
        p = item.get("path")
        if not p:
            continue
        try:
            key = str(Path(p).resolve())
        except Exception:
            key = str(p)
        out[key] = item
    return out


def profile_lane(profile: str) -> Optional[str]:
    """Company lane of a profile (``shared``, a company slug, or None)."""
    from hermes_cli.profiles import profile_company

    try:
        return profile_company(profile)
    except Exception:
        return None


def profile_skills_dir(profile: str) -> Tuple[Optional[Path], Optional[str]]:
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name

    canon = normalize_profile_name(profile)
    pdir = get_profile_dir(canon)
    if not pdir.is_dir():
        return None, canon
    return pdir / "skills", canon


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------

def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> List[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_provenance(skills_dir: Path) -> Dict[str, dict]:
    p = skills_dir / PROVENANCE_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_provenance(skills_dir: Path, data: Dict[str, dict]) -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    tmp = skills_dir / (PROVENANCE_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, skills_dir / PROVENANCE_FILE)


def _load_company_baselines() -> Dict[str, dict]:
    p = bridge_dir() / COMPANY_BASELINES_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_company_baselines(data: Dict[str, dict]) -> None:
    d = bridge_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (COMPANY_BASELINES_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, d / COMPANY_BASELINES_FILE)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

@dataclass
class ProvisionResult:
    skill: str
    profile: str
    status: str
    reason: Optional[str] = None
    detail: str = ""
    source: Optional[str] = None
    source_path: Optional[str] = None
    target_path: Optional[str] = None
    sha256_tree: Optional[str] = None
    lane: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_PRESENT, STATUS_PROVISIONED, STATUS_WOULD_PROVISION)

    def to_dict(self) -> dict:
        return asdict(self)


class SkillProvisionRefused(Exception):
    """Raised by :func:`preflight_task_skills` when any required skill cannot
    be provisioned from an approved baseline. Carries every result so the
    dispatcher can record a precise, machine-readable capability block."""

    def __init__(self, results: List[ProvisionResult], *, profile: str, task_id: Optional[str] = None):
        self.results = results
        self.profile = profile
        self.task_id = task_id
        refused = [r for r in results if r.status == STATUS_REFUSED]
        parts = [f"{r.skill}: {r.reason} ({r.detail})" for r in refused]
        super().__init__(
            "CAPABILITY_GAP skill provisioning refused for profile "
            f"{profile!r}: " + "; ".join(parts)
            + ". Fail-closed: no bytes were copied for refused skills. "
            "Route to Erika for provisioning (hierarchy.md capability provisioning)."
        )

    def payload(self) -> dict:
        return {
            "profile": self.profile,
            "task_id": self.task_id,
            "results": [r.to_dict() for r in self.results],
        }


def _registry_item_for(path: Path, registry: Dict[str, dict]) -> Optional[dict]:
    try:
        return registry.get(str(path.resolve()))
    except Exception:
        return None


def resolve_source(
    skill: str,
    *,
    target_profile: str,
    target_lane: Optional[str],
    root: Optional[Path] = None,
    source_profile: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[ProvisionResult]]:
    """Find an *approved* source for *skill*.

    Order: the root skill tree pinned in the skill-trust registry (shared
    baseline), then a company-lane baseline attested in the bridge ledger for
    the target's own lane. Returns ``(source, None)`` or ``(None, refusal)``.
    ``source`` is ``{"kind", "path", "sha256_tree", "lane", "label"}``.
    """
    root = root or hermes_root()
    root_skills = root / "skills"
    registry = load_trust_registry()

    root_dir, err = find_skill_dir(root_skills, skill)
    if root_dir is not None:
        item = _registry_item_for(root_dir, registry)
        if item is None:
            return None, ProvisionResult(
                skill, target_profile, STATUS_REFUSED, REFUSED_UNREGISTERED,
                f"root skill {root_dir} is not in the skill-trust registry; "
                "unreviewed bytes are never provisioned",
                source_path=str(root_dir),
            )
        expected = str(item.get("sha256_tree") or "")
        actual = tree_hash(root_dir)
        if not expected or actual != expected:
            return None, ProvisionResult(
                skill, target_profile, STATUS_REFUSED, REFUSED_SOURCE_DRIFT,
                f"root skill bytes differ from registry (registry={expected[:16]} live={actual[:16]}); "
                "a changed frozen skill is a violation to investigate, not bytes to copy",
                source_path=str(root_dir), sha256_tree=actual,
            )
        return {
            "kind": "root-registry",
            "path": str(root_dir),
            "sha256_tree": actual,
            "lane": SHARED_LANE,
            "label": f"skill-trust registry {item.get('skill_key') or root_dir.name} "
                     f"(attested {item.get('attested_at')})",
        }, None
    if err == REFUSED_AMBIGUOUS:
        return None, ProvisionResult(
            skill, target_profile, STATUS_REFUSED, REFUSED_AMBIGUOUS,
            "more than one root skill directory carries this name; use category/name",
        )

    # Company-lane baseline (attested through `skill-bridge attest`).
    baselines = _load_company_baselines()
    candidates = []
    for key, entry in baselines.items():
        if entry.get("skill") == skill or key.split("::", 1)[-1] == skill:
            candidates.append(entry)
    if source_profile:
        candidates = [c for c in candidates if c.get("profile") == source_profile]
    if candidates:
        entry = candidates[0]
        src_lane = entry.get("lane")
        if not target_lane or target_lane == SHARED_LANE or src_lane != target_lane:
            return None, ProvisionResult(
                skill, target_profile, STATUS_REFUSED, REFUSED_COMPANY_SCOPE,
                f"company skill attested for lane {src_lane!r} may not be provisioned "
                f"into profile {target_profile!r} (lane {target_lane!r}); cross-company "
                "provisioning is forbidden and uncertainty defaults to FORBIDDEN",
                lane=target_lane,
            )
        src_path = Path(entry.get("path") or "")
        if not (src_path / "SKILL.md").is_file():
            return None, ProvisionResult(
                skill, target_profile, STATUS_REFUSED, REFUSED_UNREGISTERED,
                f"attested company baseline path missing: {src_path}",
            )
        actual = tree_hash(src_path)
        if actual != entry.get("sha256_tree"):
            return None, ProvisionResult(
                skill, target_profile, STATUS_REFUSED, REFUSED_SOURCE_DRIFT,
                "company baseline bytes differ from their attestation "
                f"(attested={str(entry.get('sha256_tree'))[:16]} live={actual[:16]})",
                source_path=str(src_path), sha256_tree=actual, lane=target_lane,
            )
        return {
            "kind": "company-baseline",
            "path": str(src_path),
            "sha256_tree": actual,
            "lane": src_lane,
            "label": f"company baseline attested by {entry.get('by')} at {entry.get('attested_at')}",
        }, None

    return None, ProvisionResult(
        skill, target_profile, STATUS_REFUSED, REFUSED_UNKNOWN_SKILL,
        "no approved baseline: not in the root skill tree and no attested company baseline",
        lane=target_lane,
    )


def provision_skill(
    skill: str,
    profile: str,
    *,
    task_id: Optional[str] = None,
    task_tenant: Optional[str] = None,
    dry_run: bool = False,
    by: str = "skill-bridge",
    root: Optional[Path] = None,
    source_profile: Optional[str] = None,
) -> ProvisionResult:
    """Ensure *skill* exists in *profile* with approved bytes. Never overwrites."""
    root = root or hermes_root()
    skills_dir, canon = profile_skills_dir(profile)
    if skills_dir is None:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_NO_PROFILE,
                               f"profile directory for {profile!r} does not exist")
    profile = canon or profile
    lane = profile_lane(profile)
    tenant = (task_tenant or "").strip().casefold() or None
    if tenant and lane and lane != SHARED_LANE and tenant != lane:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_COMPANY_SCOPE,
                               f"task tenant {tenant!r} does not match profile lane {lane!r}",
                               lane=lane)

    # Already there? Only "present" when the bytes match a recorded or
    # registry-known approved hash; otherwise the target has diverged and we
    # refuse to touch it either way.
    existing, err = find_skill_dir(skills_dir, skill)
    if existing is not None:
        actual = tree_hash(existing)
        prov = _load_provenance(skills_dir).get(existing.name) or {}
        known = {str(prov.get("sha256_tree") or "")}
        src, _refusal = resolve_source(skill, target_profile=profile, target_lane=lane,
                                       root=root, source_profile=source_profile)
        if src:
            known.add(src["sha256_tree"])
        known.discard("")
        if actual in known:
            return ProvisionResult(skill, profile, STATUS_PRESENT, None,
                                   "already present with approved bytes",
                                   target_path=str(existing), sha256_tree=actual, lane=lane)
        if not known:
            # No baseline anywhere to compare against: the profile's own
            # (pre-bridge) copy. Leave it alone; the worker can load it.
            return ProvisionResult(skill, profile, STATUS_PRESENT, None,
                                   "present (pre-bridge copy, no baseline to compare; left untouched)",
                                   target_path=str(existing), sha256_tree=actual, lane=lane)
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_TARGET_DRIFT,
                               "profile copy differs from every approved baseline "
                               f"(live={actual[:16]}); not overwritten — investigate or unprovision",
                               target_path=str(existing), sha256_tree=actual, lane=lane)
    if err == REFUSED_AMBIGUOUS:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_AMBIGUOUS,
                               "more than one profile skill directory carries this name", lane=lane)

    src, refusal = resolve_source(skill, target_profile=profile, target_lane=lane,
                                  root=root, source_profile=source_profile)
    if refusal is not None:
        refusal.lane = lane
        return refusal

    src_path = Path(src["path"])
    root_skills = root / "skills"
    try:
        rel = src_path.relative_to(root_skills) if src["kind"] == "root-registry" else Path(src_path.name)
    except ValueError:
        rel = Path(src_path.name)
    target = skills_dir / rel
    if dry_run:
        return ProvisionResult(skill, profile, STATUS_WOULD_PROVISION, None,
                               f"would copy approved bytes from {src['label']}",
                               source=src["kind"], source_path=str(src_path),
                               target_path=str(target), sha256_tree=src["sha256_tree"], lane=lane)

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.bridge-", dir=str(target.parent)))
    staged = tmp_dir / target.name
    try:
        shutil.copytree(src_path, staged, symlinks=True, ignore=_copy_ignore)
        copied_hash = tree_hash(staged)
        if copied_hash != src["sha256_tree"]:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_COPY_VERIFY,
                                   f"copied bytes hash {copied_hash[:16]} != baseline "
                                   f"{src['sha256_tree'][:16]}; copy discarded",
                                   source=src["kind"], source_path=str(src_path), lane=lane)
        if target.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_TARGET_DRIFT,
                                   f"target {target} appeared during provisioning; not overwritten",
                                   target_path=str(target), lane=lane)
        os.rename(staged, target)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    final_hash = tree_hash(target)
    record = {
        "skill": skill,
        "profile": profile,
        "lane": lane,
        "sha256_tree": final_hash,
        "source": src["kind"],
        "source_path": str(src_path),
        "source_label": src["label"],
        "target_path": str(target),
        "task_id": task_id,
        "by": by,
        "provisioned_at": _now(),
    }
    prov = _load_provenance(skills_dir)
    prov[target.name] = record
    _save_provenance(skills_dir, prov)
    _append_jsonl(bridge_dir() / LEDGER_FILE, {"event": "provision", **record})
    return ProvisionResult(skill, profile, STATUS_PROVISIONED, None,
                           f"copied approved bytes from {src['label']}; hash verified",
                           source=src["kind"], source_path=str(src_path),
                           target_path=str(target), sha256_tree=final_hash, lane=lane)


def unprovision_skill(skill: str, profile: str, *, by: str = "skill-bridge") -> ProvisionResult:
    """Rollback: remove a bridge-provisioned copy whose bytes still match the
    provenance record. Anything else (hand-edited, not bridge-provisioned) is
    refused so a rollback can never destroy learning that lives only there."""
    skills_dir, canon = profile_skills_dir(profile)
    if skills_dir is None:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_NO_PROFILE, "no such profile")
    profile = canon or profile
    existing, err = find_skill_dir(skills_dir, skill)
    if existing is None:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_UNKNOWN_SKILL,
                               "not present in profile")
    prov = _load_provenance(skills_dir)
    rec = prov.get(existing.name)
    if not rec:
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_UNREGISTERED,
                               "no bridge provenance record; refusing to remove a non-bridge copy",
                               target_path=str(existing))
    actual = tree_hash(existing)
    if actual != rec.get("sha256_tree"):
        return ProvisionResult(skill, profile, STATUS_REFUSED, REFUSED_TARGET_DRIFT,
                               "profile copy has changed since provisioning; refusing to remove",
                               target_path=str(existing), sha256_tree=actual)
    shutil.rmtree(existing)
    prov.pop(existing.name, None)
    _save_provenance(skills_dir, prov)
    _append_jsonl(bridge_dir() / LEDGER_FILE, {
        "event": "unprovision", "skill": skill, "profile": profile,
        "target_path": str(existing), "sha256_tree": actual, "by": by, "at": _now(),
    })
    return ProvisionResult(skill, profile, "unprovisioned", None, "removed bridge-provisioned copy",
                           target_path=str(existing), sha256_tree=actual)


def attest_company_baseline(skill: str, profile: str, *, by: str, note: str = "") -> dict:
    """Record a company-lane skill (one living only in a company profile) as an
    approved baseline for provisioning inside that lane. Explicit, attributed,
    hash-pinned. Shared-lane profiles cannot be attested here: shared skills
    go through the root tree and the skill-trust registry."""
    skills_dir, canon = profile_skills_dir(profile)
    if skills_dir is None:
        raise ValueError(f"no such profile: {profile}")
    profile = canon or profile
    lane = profile_lane(profile)
    if not lane or lane == SHARED_LANE:
        raise ValueError(
            f"profile {profile!r} is in lane {lane!r}; only company-lane profiles may hold "
            "company baselines. Shared skills belong in the root tree + skill-trust registry."
        )
    path, err = find_skill_dir(skills_dir, skill)
    if path is None:
        raise ValueError(f"skill {skill!r} not found in profile {profile!r} ({err})")
    entry = {
        "skill": skill,
        "profile": profile,
        "lane": lane,
        "path": str(path),
        "sha256_tree": tree_hash(path),
        "by": by,
        "note": note,
        "attested_at": _now(),
    }
    data = _load_company_baselines()
    data[f"{lane}::{skill}"] = entry
    _save_company_baselines(data)
    _append_jsonl(bridge_dir() / LEDGER_FILE, {"event": "attest", **entry})
    return entry


def preflight_task_skills(
    skills: Iterable[str],
    profile: str,
    *,
    task_id: Optional[str] = None,
    task_tenant: Optional[str] = None,
    dry_run: bool = False,
    root: Optional[Path] = None,
) -> List[ProvisionResult]:
    """Dispatcher hook: make every required skill available in *profile* from
    an approved baseline, or raise :class:`SkillProvisionRefused`."""
    results: List[ProvisionResult] = []
    seen = set()
    for raw in skills or []:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        results.append(provision_skill(
            name, profile, task_id=task_id, task_tenant=task_tenant, dry_run=dry_run,
            by=f"dispatcher-preflight:{task_id or 'manual'}", root=root,
        ))
    if any(r.status == STATUS_REFUSED for r in results):
        raise SkillProvisionRefused(results, profile=profile, task_id=task_id)
    return results


def drift_report(profile: Optional[str] = None) -> List[dict]:
    """Compare every bridge-provisioned profile copy with its provenance hash."""
    from hermes_cli.profiles import list_profiles

    names: List[str]
    if profile:
        names = [profile]
    else:
        names = []
        try:
            names = [p.name for p in list_profiles()]
        except Exception:
            pass
    out: List[dict] = []
    for name in names:
        skills_dir, canon = profile_skills_dir(name)
        if skills_dir is None:
            continue
        for dname, rec in sorted(_load_provenance(skills_dir).items()):
            tpath = Path(rec.get("target_path") or (skills_dir / dname))
            if not (tpath / "SKILL.md").is_file():
                status = "MISSING"
                live = None
            else:
                live = tree_hash(tpath)
                status = "OK" if live == rec.get("sha256_tree") else "CHANGED"
            out.append({"profile": canon or name, "skill": rec.get("skill", dname), "status": status,
                        "expected": rec.get("sha256_tree"), "live": live, "path": str(tpath)})
    return out


# ---------------------------------------------------------------------------
# Vault bridge — LifeWiki /api/skills
# ---------------------------------------------------------------------------

class VaultError(Exception):
    pass


def _http(method: str, path: str, body: Optional[dict] = None, *, token: Optional[str] = None,
          timeout: float = 20.0) -> Tuple[int, dict]:
    url = lifewiki_url() + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header(VERIFIER_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback vault API
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw[:500]}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VaultError(f"vault API unreachable at {url}: {exc}") from exc


def vault_read(capability: str) -> dict:
    if not _CAP_RE.match(capability):
        raise VaultError(f"invalid capability: {capability!r}")
    status, payload = _http("GET", f"/api/skills/{capability}")
    if status != 200:
        raise VaultError(f"GET /api/skills/{capability} -> {status}: {payload.get('error') or payload}")
    return payload


def doctrine_texts(doc: dict) -> List[str]:
    """Doctrine entries as plain text. The live API returns objects
    (``{timestamp, pattern, confidence, text}``); older shapes were strings."""
    out: List[str] = []
    for d in doc.get("doctrine") or []:
        if isinstance(d, str):
            out.append(d)
        elif isinstance(d, dict) and d.get("text"):
            out.append(str(d["text"]))
    return out


def doctrine_line_for(capability: str, obs_id: str) -> Optional[str]:
    """The approved doctrine text that cites *obs_id*, or None."""
    try:
        doc = vault_read(capability)
    except VaultError:
        return None
    lines = [t for t in doctrine_texts(doc) if obs_id in t]
    return lines[-1] if lines else None


def vault_observe(
    capability: str,
    lesson: str,
    *,
    outcome: str = "neutral",
    task: Optional[str] = None,
    evidence: Optional[str] = None,
    source: str = "hermes-learn",
    confidence: float = 0.5,
    pattern: Optional[str] = None,
    action: Optional[str] = None,
) -> dict:
    """Post a PENDING observation (non-binding) through the supported boundary."""
    if not _CAP_RE.match(capability):
        raise VaultError(f"invalid capability: {capability!r}")
    if not _SLUG_RE.match(source):
        raise VaultError(f"invalid source slug: {source!r}")
    if outcome not in ("success", "failure", "neutral"):
        raise VaultError("outcome must be success|failure|neutral")
    body: Dict[str, Any] = {
        "lesson": lesson.strip()[:2000],
        "outcome": outcome,
        "source": source,
        "confidence": max(0.0, min(1.0, float(confidence))),
    }
    if task:
        body["task"] = task
    if evidence:
        body["evidence"] = evidence
    if pattern:
        body["pattern"] = pattern
    if action:
        body["action"] = action
    status, payload = _http("POST", f"/api/skills/{capability}/observe", body)
    if status not in (200, 201):
        raise VaultError(f"observe {capability} -> {status}: {payload.get('error') or payload}")
    record = {
        "event": "observe",
        "capability": capability,
        "obs_id": payload.get("obs_id"),
        "task": task,
        "source": source,
        "lesson_sha256": hashlib.sha256(body["lesson"].encode("utf-8")).hexdigest(),
        "at": _now(),
    }
    _append_jsonl(bridge_dir() / OBSERVATIONS_FILE, record)
    return payload


def task_is_verified(task_id: str) -> Tuple[bool, str]:
    """Read-only kanban check: the Gauntlet's verdict for *task_id*."""
    try:
        from hermes_cli import kanban_db as kb
    except Exception as exc:  # pragma: no cover - import guard
        return False, f"kanban unavailable: {exc}"
    try:
        with kb.connect_closing() as conn:
            task = kb.get_task(conn, task_id)
            if task is None:
                return False, f"task {task_id} not found"
            state = getattr(task, "verification_state", None)
            if state == kb.VERIFICATION_VERIFIED:
                return True, f"task {task_id} verification_state=verified"
            for row in reversed(kb.verification_history(conn, task_id)):
                if row.get("state") == kb.VERIFICATION_VERIFIED:
                    return True, (f"task {task_id} has a verified ledger row "
                                  f"(verifier={row.get('verifier')} at {row.get('created_at')})")
            return False, f"task {task_id} is not verified (state={state!r}, status={task.status!r})"
    except Exception as exc:
        return False, f"kanban read failed: {exc}"


def vault_promote(
    capability: str,
    obs_id: str,
    *,
    verifier: str,
    note: str = "",
    task: Optional[str] = None,
    attested_by: Optional[str] = None,
    attestation_evidence: Optional[str] = None,
    token: Optional[str] = None,
    confidence: Optional[float] = None,
) -> dict:
    """Approve a pending observation into ``doctrine.md`` (CANONICAL).

    Fails closed unless there is verification evidence: *task* must be a
    kanban task whose verification state is ``verified``, or an explicit
    operator attestation (*attested_by* + *attestation_evidence*) is given.
    """
    if not _CAP_RE.match(capability):
        raise VaultError(f"invalid capability: {capability!r}")
    if not _OBS_RE.match(obs_id):
        raise VaultError(f"invalid obs_id: {obs_id!r}")
    if not _SLUG_RE.match(verifier):
        raise VaultError(f"invalid verifier slug: {verifier!r}")
    token = token or os.environ.get(VERIFIER_TOKEN_ENV, "").strip()
    if not token:
        raise VaultError(f"{VERIFIER_TOKEN_ENV} is not set; the verify gate is closed")

    basis: str
    if task:
        ok, basis = task_is_verified(task)
        if not ok:
            raise VaultError(f"refusing promotion: {basis}. Unverified lessons stay pending.")
    elif attested_by and attestation_evidence:
        basis = f"operator attestation by {attested_by}: {attestation_evidence}"
    else:
        raise VaultError("refusing promotion: give --task <verified kanban task> or "
                         "--attested-by NAME --attestation-evidence TEXT")

    full_note = (note + " | " if note else "") + f"basis: {basis}"
    body: Dict[str, Any] = {"obs_id": obs_id, "decision": "approve", "verifier": verifier,
                            "note": full_note[:2000]}
    if confidence is not None:
        body["confidence"] = max(0.0, min(1.0, float(confidence)))
    status, payload = _http("POST", f"/api/skills/{capability}/verify", body, token=token)
    if status != 200:
        raise VaultError(f"verify {capability}/{obs_id} -> {status}: {payload.get('error') or payload}")
    if payload.get("status") == "not_found":
        raise VaultError(f"observation {obs_id} not pending in {capability}")
    # Bidirectional pointer: vault doctrine line hash <-> ledger.
    line = doctrine_line_for(capability, obs_id)
    doctrine_hash = hashlib.sha256(line.encode("utf-8")).hexdigest() if line else None
    record = {
        "event": "promote",
        "capability": capability,
        "obs_id": obs_id,
        "verifier": verifier,
        "task": task,
        "basis": basis,
        "doctrine_line_sha256": doctrine_hash,
        "vault_git": payload.get("git"),
        "at": _now(),
    }
    _append_jsonl(bridge_dir() / OBSERVATIONS_FILE, record)
    payload["bridge"] = record
    return payload


def vault_reject(capability: str, obs_id: str, *, verifier: str, note: str = "",
                 token: Optional[str] = None) -> dict:
    if not _CAP_RE.match(capability) or not _OBS_RE.match(obs_id) or not _SLUG_RE.match(verifier):
        raise VaultError("invalid capability/obs_id/verifier")
    token = token or os.environ.get(VERIFIER_TOKEN_ENV, "").strip()
    if not token:
        raise VaultError(f"{VERIFIER_TOKEN_ENV} is not set; the verify gate is closed")
    status, payload = _http("POST", f"/api/skills/{capability}/verify",
                            {"obs_id": obs_id, "decision": "reject", "verifier": verifier,
                             "note": note[:2000]}, token=token)
    if status != 200:
        raise VaultError(f"reject -> {status}: {payload.get('error') or payload}")
    _append_jsonl(bridge_dir() / OBSERVATIONS_FILE, {"event": "reject", "capability": capability,
                                                      "obs_id": obs_id, "verifier": verifier, "at": _now()})
    return payload


# ---------------------------------------------------------------------------
# Hermes pending-store mirroring and runtime materialization
# ---------------------------------------------------------------------------

def hermes_homes() -> List[Tuple[str, Path]]:
    """``[("root", ~/.hermes), ("<profile>", ~/.hermes/profiles/<profile>), ...]``
    (symlinked profile aliases are skipped so a home is listed once)."""
    root = hermes_root()
    homes: List[Tuple[str, Path]] = [("root", root)]
    pdir = root / "profiles"
    if pdir.is_dir():
        for p in sorted(pdir.iterdir()):
            if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
                homes.append((p.name, p))
    return homes


def list_pending_skill_writes(home: Path) -> List[dict]:
    pdir = home / "pending" / "skills"
    out: List[dict] = []
    if not pdir.is_dir():
        return out
    for f in sorted(pdir.glob("*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rec, dict):
            rec["_path"] = str(f)
            out.append(rec)
    return out


def capability_for_payload(payload: dict, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    cat = str(payload.get("category") or "").strip().lower()
    if cat in CATEGORY_CAPABILITY_MAP:
        return CATEGORY_CAPABILITY_MAP[cat]
    name = str(payload.get("name") or "").lower()
    for key, cap in CATEGORY_CAPABILITY_MAP.items():
        if name.startswith(key + "-") or name == key:
            return cap
    return DEFAULT_CAPABILITY


def stage_pending_to_vault(*, capability: Optional[str] = None, task: Optional[str] = None,
                           homes: Optional[List[Tuple[str, Path]]] = None,
                           dry_run: bool = False) -> List[dict]:
    """Mirror every not-yet-mirrored staged skill write (any Hermes home) into
    the vault as a pending observation. Idempotent via the observations ledger."""
    ledger = _read_jsonl(bridge_dir() / OBSERVATIONS_FILE)
    already = {(r.get("home"), r.get("pending_id")) for r in ledger if r.get("event") == "mirror"}
    results: List[dict] = []
    for label, home in (homes or hermes_homes()):
        for rec in list_pending_skill_writes(home):
            pid = rec.get("id")
            if not pid or (label, pid) in already:
                continue
            if str(rec.get("summary") or "").startswith(PROJECTION_SUMMARY_PREFIX):
                continue  # bridge-staged projection of vault doctrine: not new learning
            payload = rec.get("payload") or {}
            cap = capability_for_payload(payload, capability)
            payload_sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
            lesson = (f"Hermes home '{label}' staged a {rec.get('action')} of runtime skill "
                      f"'{payload.get('name')}' (pending {pid}): {rec.get('summary') or ''}").strip()
            evidence = (f"home={label} pending_id={pid} origin={rec.get('origin')} "
                        f"payload_sha256={payload_sha[:16]} staged_at={rec.get('created_at')}")
            entry = {"home": label, "pending_id": pid, "capability": cap, "lesson": lesson,
                     "payload_sha256": payload_sha}
            if dry_run:
                entry["event"] = "would_mirror"
                results.append(entry)
                continue
            resp = vault_observe(cap, lesson, outcome="neutral", task=task, evidence=evidence,
                                 source="hermes-learn", confidence=0.3)
            entry.update({"event": "mirror", "obs_id": resp.get("obs_id"), "at": _now()})
            _append_jsonl(bridge_dir() / OBSERVATIONS_FILE, entry)
            results.append(entry)
    return results


def materialize_lesson(
    *,
    home_label: str,
    skill: str,
    capability: str,
    obs_id: str,
    lesson: str,
) -> dict:
    """Stage (never write) a runtime projection of a promoted vault lesson into
    the named home's pending store, for operator review via ``/skills approve``.
    Requires the home's ``skills.write_approval`` to be on; refuses otherwise so
    nothing can land in a skill tree silently."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override  # type: ignore

    homes = dict(hermes_homes())
    home = homes.get(home_label)
    if home is None:
        raise VaultError(f"unknown Hermes home: {home_label!r}")
    skills_dir = home / "skills"
    sdir, err = find_skill_dir(skills_dir, skill)
    if sdir is None:
        raise VaultError(f"skill {skill!r} not found in home {home_label!r} ({err})")
    token = set_hermes_home_override(home)
    try:
        from tools import write_approval as wa

        if not wa.write_approval_enabled(wa.SKILLS):
            raise VaultError(f"home {home_label!r} has skills.write_approval off; refusing to "
                             "materialize because the write could not be held for review")
        target_file = sdir / MATERIALIZE_REL_PATH
        existing = target_file.read_text(encoding="utf-8") if target_file.is_file() else (
            f"# Vault lessons — {sdir.name}\n\nVerified lessons projected from the LifeWiki vault "
            f"(`Skills/<capability>/doctrine.md`). The vault is canonical; this file is a projection.\n"
        )
        line = (f"- [{obs_id}] [{capability}] [{_now()}] {lesson.strip()} "
                f"(vault: Skills/{capability}/doctrine.md)")
        if line.split("] ", 1)[0] in existing:
            return {"staged": False, "reason": "already projected", "home": home_label}
        content = existing.rstrip("\n") + "\n" + line + "\n"
        payload = {"action": "write_file", "name": sdir.name, "file_path": MATERIALIZE_REL_PATH,
                   "file_content": content}
        # A frozen (registry-pinned) skill must not be changed by a plain
        # `/skills approve`: approving the replay would alter attested bytes
        # and trip the skill-trust boot gate. Say so in the review summary so
        # the operator routes it through the governed reseal path first.
        frozen = _registry_item_for(sdir, load_trust_registry()) is not None
        summary = f"{PROJECTION_SUMMARY_PREFIX}{obs_id} ({capability}) into {sdir.name}"
        if frozen:
            summary += (" [FROZEN skill: approve only via the skill-integrity/skill-trust reseal "
                        "path; a plain approve would change attested bytes]")
        rec = wa.stage_write(wa.SKILLS, payload, summary=summary, origin="foreground")
        out = {"staged": True, "home": home_label, "pending_id": rec.get("id"), "skill": sdir.name,
               "file": MATERIALIZE_REL_PATH, "obs_id": obs_id, "capability": capability,
               "frozen_target": frozen,
               "doctrine_line_sha256": hashlib.sha256(lesson.strip().encode("utf-8")).hexdigest(),
               "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "at": _now()}
        _append_jsonl(bridge_dir() / OBSERVATIONS_FILE, {"event": "materialize", **out})
        return out
    finally:
        reset_hermes_home_override(token)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    elif isinstance(obj, list):
        for item in obj:
            _print(item, False)
    elif isinstance(obj, ProvisionResult):
        print(f"{obj.status.upper():16} {obj.profile}:{obj.skill}"
              + (f"  [{obj.reason}]" if obj.reason else "")
              + (f"  sha256_tree={obj.sha256_tree[:16]}" if obj.sha256_tree else "")
              + (f"  — {obj.detail}" if obj.detail else ""))
    elif isinstance(obj, dict):
        print(json.dumps(obj, sort_keys=True, default=str))
    else:
        print(obj)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skill-bridge",
                                description="Vault-first learned-skill bridge and need-based profile provisioning")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("provision", help="copy an approved skill baseline into a profile (never overwrites)")
    s.add_argument("--profile", required=True)
    s.add_argument("--skill", required=True, action="append")
    s.add_argument("--task")
    s.add_argument("--tenant")
    s.add_argument("--from-profile", help="company-lane source profile (attested baseline)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--by", default="skill-bridge-cli")

    s = sub.add_parser("preflight", help="dispatcher preflight: ensure required skills or fail closed")
    s.add_argument("--profile", required=True)
    s.add_argument("--skill", required=True, action="append")
    s.add_argument("--task")
    s.add_argument("--tenant")
    s.add_argument("--dry-run", action="store_true")

    s = sub.add_parser("unprovision", help="rollback: remove a bridge-provisioned copy whose bytes still match")
    s.add_argument("--profile", required=True)
    s.add_argument("--skill", required=True)
    s.add_argument("--by", default="skill-bridge-cli")

    s = sub.add_parser("attest", help="attest a company-lane skill as a provisioning baseline inside its lane")
    s.add_argument("--profile", required=True)
    s.add_argument("--skill", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--note", default="")

    s = sub.add_parser("drift", help="compare bridge-provisioned profile copies with their provenance hashes")
    s.add_argument("--profile")

    s = sub.add_parser("observe", help="post a PENDING (non-binding) vault observation")
    s.add_argument("--capability", required=True)
    s.add_argument("--lesson", required=True)
    s.add_argument("--outcome", default="neutral", choices=["success", "failure", "neutral"])
    s.add_argument("--task")
    s.add_argument("--evidence")
    s.add_argument("--source", default="hermes-learn")
    s.add_argument("--confidence", type=float, default=0.5)
    s.add_argument("--pattern")
    s.add_argument("--action")

    s = sub.add_parser("promote", help="approve a pending observation into vault doctrine (needs verification evidence)")
    s.add_argument("--capability", required=True)
    s.add_argument("--obs-id", required=True)
    s.add_argument("--verifier", required=True)
    s.add_argument("--task", help="kanban task that must be verified")
    s.add_argument("--attested-by")
    s.add_argument("--attestation-evidence")
    s.add_argument("--note", default="")
    s.add_argument("--confidence", type=float)
    s.add_argument("--materialize-skill", help="stage a runtime projection into this skill (pending review)")
    s.add_argument("--materialize-home", default="root")

    s = sub.add_parser("materialize", help="stage (never write) a runtime projection of a PROMOTED vault lesson for review")
    s.add_argument("--capability", required=True)
    s.add_argument("--obs-id", required=True)
    s.add_argument("--skill", required=True)
    s.add_argument("--home", default="root")

    s = sub.add_parser("reject", help="reject a pending observation")
    s.add_argument("--capability", required=True)
    s.add_argument("--obs-id", required=True)
    s.add_argument("--verifier", required=True)
    s.add_argument("--note", default="")

    s = sub.add_parser("read", help="read a vault capability (doctrine + pending count)")
    s.add_argument("--capability", required=True)

    s = sub.add_parser("stage-pending", help="mirror staged Hermes skill writes (all homes) into vault pending observations")
    s.add_argument("--capability")
    s.add_argument("--task")
    s.add_argument("--dry-run", action="store_true")

    sub.add_parser("pending", help="list staged skill writes across every Hermes home")
    sub.add_parser("homes", help="list Hermes homes the bridge covers")
    sub.add_parser("ledger", help="print the bridge ledgers")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "provision":
            res = [provision_skill(s, args.profile, task_id=args.task, task_tenant=args.tenant,
                                   dry_run=args.dry_run, by=args.by, source_profile=args.from_profile)
                   for s in args.skill]
            _print([r.to_dict() for r in res] if args.json else res, args.json)
            return 0 if all(r.ok for r in res) else 2
        if args.cmd == "preflight":
            try:
                res = preflight_task_skills(args.skill, args.profile, task_id=args.task,
                                            task_tenant=args.tenant, dry_run=args.dry_run)
            except SkillProvisionRefused as exc:
                _print(exc.payload() if args.json else exc.results, args.json)
                print(f"REFUSED: {exc}", file=sys.stderr)
                return 2
            _print([r.to_dict() for r in res] if args.json else res, args.json)
            return 0
        if args.cmd == "unprovision":
            r = unprovision_skill(args.skill, args.profile, by=args.by)
            _print(r.to_dict() if args.json else r, args.json)
            return 0 if r.status == "unprovisioned" else 2
        if args.cmd == "attest":
            _print(attest_company_baseline(args.skill, args.profile, by=args.by, note=args.note), args.json)
            return 0
        if args.cmd == "drift":
            rows = drift_report(args.profile)
            _print(rows, True) if args.json else [print(f"{r['status']:8} {r['profile']}:{r['skill']} {r['path']}") for r in rows]
            return 0 if all(r["status"] == "OK" for r in rows) else 2
        if args.cmd == "observe":
            _print(vault_observe(args.capability, args.lesson, outcome=args.outcome, task=args.task,
                                 evidence=args.evidence, source=args.source, confidence=args.confidence,
                                 pattern=args.pattern, action=args.action), True)
            return 0
        if args.cmd == "promote":
            out = vault_promote(args.capability, args.obs_id, verifier=args.verifier, note=args.note,
                                task=args.task, attested_by=args.attested_by,
                                attestation_evidence=args.attestation_evidence, confidence=args.confidence)
            if args.materialize_skill:
                lesson = doctrine_line_for(args.capability, args.obs_id)
                if not lesson:
                    raise VaultError(f"no approved doctrine text cites {args.obs_id}; nothing to project")
                out["materialize"] = materialize_lesson(home_label=args.materialize_home,
                                                        skill=args.materialize_skill,
                                                        capability=args.capability, obs_id=args.obs_id,
                                                        lesson=lesson)
            _print(out, True)
            return 0
        if args.cmd == "materialize":
            lesson = doctrine_line_for(args.capability, args.obs_id)
            if not lesson:
                raise VaultError(f"no approved doctrine text cites {args.obs_id}; only promoted "
                                 "(CANONICAL) lessons may be projected")
            _print(materialize_lesson(home_label=args.home, skill=args.skill, capability=args.capability,
                                      obs_id=args.obs_id, lesson=lesson), True)
            return 0
        if args.cmd == "reject":
            _print(vault_reject(args.capability, args.obs_id, verifier=args.verifier, note=args.note), True)
            return 0
        if args.cmd == "read":
            _print(vault_read(args.capability), True)
            return 0
        if args.cmd == "stage-pending":
            _print(stage_pending_to_vault(capability=args.capability, task=args.task, dry_run=args.dry_run), True)
            return 0
        if args.cmd == "pending":
            rows = []
            for label, home in hermes_homes():
                for rec in list_pending_skill_writes(home):
                    rows.append({"home": label, "id": rec.get("id"), "action": rec.get("action"),
                                 "name": (rec.get("payload") or {}).get("name"), "origin": rec.get("origin"),
                                 "created_at": rec.get("created_at"), "summary": rec.get("summary")})
            _print(rows, True)
            return 0
        if args.cmd == "homes":
            _print([{"home": l, "path": str(p), "skills_dir": str(p / "skills")} for l, p in hermes_homes()], True)
            return 0
        if args.cmd == "ledger":
            _print({"ledger": _read_jsonl(bridge_dir() / LEDGER_FILE),
                    "observations": _read_jsonl(bridge_dir() / OBSERVATIONS_FILE),
                    "company_baselines": _load_company_baselines()}, True)
            return 0
    except (VaultError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
