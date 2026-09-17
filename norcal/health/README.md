# NorCal system health controller

Kanban task `t_b8d62378`. One deterministic controller above the existing monitors:
`OBSERVE -> CLASSIFY -> RECOVER -> REVALIDATE -> GREEN/CONTINUE`, else `ESCALATE/DEGRADED`.
It consumes existing surfaces (Kanban board, Hermes cron `jobs.json`, user systemd timers, the
gateway heartbeat, the canonical boot gate, `hermes config get`) and replaces none of them.
No LLM; silent when GREEN.

- `system_health_controller.py` — controller, invariants, CLI (`run --tier light|deep [--dry-run] [--json]`, `status`).
- `health-controller.json` — watched jobs/timers, doctrine runtime ceilings, `alert_target` (the Hermes Telegram "Alerts" group, the `TELEGRAM_ALERTS_CHAT_ID` channel the gateway watchdog and post-update health check already use; if that id changes, update both).
- `systemd/` — user unit templates: light pass every 5 min, deep pass hourly, `OnFailure=` alert. Installed as user units (Phase E Gate 6, 2026-09-14).
- `scripts/` — canonical source for standalone watcher scripts the `repository_drift` and `watcher_integrity`
  invariants depend on (`deploy-drift-check.sh` + `deploy-drift-check-selftest.sh`, fingerprint 7350d7ecd2f30810,
  kanban `t_a9ef4620`/`t_9891fa93`). Deployed by copying to `~/.hermes/scripts/`, pinned there by sha256 in
  `pinned_scripts` in `health-controller.json` — keep both in sync when the script changes: edit here, redeploy the
  copy, then update the pin to the new file's sha256.

## v1 scope (Christopher, 2026-09-14)

Automatic recovery is limited to verifier-routing damage:

| Invariant | Tier | Recovery |
|---|---|---|
| `verifier_route_open` | light | open the codex_verify child (`_ensure_independent_verifier_child`) for a subject in review whose evidence the gate accepts |
| `subject_lane_relabelled` | light | `restore_relabelled_subject_lane` |
| `verifier_child_deadlocked` | light | add the `verifies` relation to a card whose title is unambiguously a verification card and whose assignee is not the implementer |
| `subject_review_regressed`, `gateway_heartbeat_fresh`, `critical_cron_jobs_healthy`, `controller_heartbeat_fresh_deep` | light | escalate only |
| `verifier_of_verifier`, `verified_closure_attributable`, `critical_timers_active`, `canonical_boot_complete`, `runtime_ceilings_match_doctrine`, `controller_heartbeat_fresh_light` | deep | escalate only |

### F1 detect-only invariants (Christopher, 2026-09-14)

No recovery; each reads an existing surface through the code that owns it. Light (5 min):
`ready_backlog_explained`, `run_lease_consistency`, `verdict_returned_to_subject`,
`verifier_child_stalled_in_todo`, `gateway_platforms_connected`, `resource_thresholds`. Deep (hourly):
`ownership_and_linkage`, `task_graph_integrity`, `control_defect_regressions` (unsafe findings for
harvested denied files and automation attempt grants), `life_wiki_daily_note`, `backup_results`,
`escalation_cards_dispositioned`. Every invariant declares `source`, `failure`, `evidence` and `tier`;
print them with `venv/bin/python norcal/health/system_health_controller.py invariants`. Thresholds live in
`health-controller.json`. Full table and calibration evidence:
`Business/Operations/2026-09-14-system-health-controller-t_b8d62378.md` §18.

### F2 invariants and boundaries (Christopher, 2026-09-14)

Light: `company_health_endpoints`, `shared_endpoints_healthy`, `shared_units_active`. Deep:
`watcher_integrity`, `repository_drift`, `company_isolation`. Detection only.

**Company health probes** — "Active company health may be observed through a minimal, non-content
health probe. Dormant companies are excluded. Company health observations never become shared company
data." The probe set in `company_health_probes` (Orion Formation Services ENT-004, Logos Covenant ENT-003,
The Glass Pepper ENT-007) is pinned by `authorization_sha256`; North Caledonia ENT-001 and NCASS/LCASS
ENT-002 are excluded. An unrecorded change, or listing an excluded entity, probes nothing. Probes are an
unauthenticated loopback `GET` that reads only the status line (never body, headers or cookies). The exact
status code and latency go only to `~/.hermes/state/system-health-controller/company-health/<ENT>.json`
(0600); findings carry the entity id and a coarse state (`UNREACHABLE`, `TIMEOUT`, `HTTP_4XX`, `HTTP_5XX`),
route `company`: no shared Kanban card is ever created, and the shared alert is the coarse summary Erika
routes to the owning Team Leader. Re-pin with `company_probe_authorization_digest` only after a recorded
authorization change.

**Company isolation** findings (another company's files on a lead card; company-named or runtime-state
files on a non-company card; the historical inventory) are `unsafe`: never covered by a governed exception,
escalated at once. Filenames are never recorded — card ids and counts only.

### F3 bounded recovery classes (Christopher, 2026-09-14) — built and tested, NOT authorized live

`recovery_classes` in `health-controller.json` lists six classes, **all `enabled: false`**. A class runs only
when `enabled: true`, `authorized_by` is set and `authorization_sha256` equals
`recovery_authorization_digest(name, spec)` (scope, allowlists and bounds pinned); an enabled class that fails
validation runs nothing and reports `recovery_class_authorization_valid` (DEGRADED). Print them with
`venv/bin/python norcal/health/system_health_controller.py recoveries`.

| Class | Detector condition | Mutation | Postcondition |
|---|---|---|---|
| `gateway_restart` | `gateway_heartbeat_fresh` stale/missing, 2 passes | shared watchdog cooldown marker, then `hermes gateway restart` | heartbeat fresh, unit active, required platforms connected by the live pid |
| `shared_service_restart` | `shared_units_active` not_active, allowlisted shared user unit, 2 passes | `systemctl --user reset-failed` + `restart` | unit active + loopback health endpoint HEALTHY |
| `lease_reconciliation` | `run_lease_consistency`, 2 passes | `reclaim_task` / `_reclaim_dangling_run` / `exec_supervisor._reconcile_one` | card/run/claim consistent, execution settled |
| `life_wiki_retry` | missing daily note / failing validation job | schedule the existing cron job (`trigger_job`) | note exists + validator exits 0 / job reran ok |
| `vault_git_sync` | `repository_drift` unpushed/behind on an allowlisted watched repo | fetch, `merge --ff-only`, `push` (never force) | `ls-remote` equals local HEAD, tree clean |
| `evidence_attachment` | `verifier_route_open` evidence_missing | attach existing allowlisted files from the task-owned workspace | `subject_has_evidence` + dispatchable |

Every class: gate re-reads live state (`proceed` / `cleared` / `deferred` / `refused`); refused → ESCALATED
without mutation; at most 2 mutations per episode (first attempt + one retry); an unchanged recurrence inside
the class window → ESCALATED (`recurred_after_recovery`); deferral bounded; a failed postcondition is never
silently resolved when the detector goes quiet. Never for unsafe, company-route or frozen-card findings; v1
verifier-routing recovery keeps its in-code path. Escalate-only detectors are listed in
`ESCALATE_ONLY_INVARIANTS`. Before enabling `gateway_restart` or `shared_service_restart` live, the light unit's
`TimeoutStartSec=240` must be raised to cover a graceful gateway drain plus the postcondition poll.

### Pass duration bounds (F4, 2026-09-15)

Every external call the controller makes is bounded, and a pass performs at most
`max_recovery_mutations_per_pass` (1) recovery mutation. Worst cases, derived from the per-call timeouts and
the configured counts (pinned by `TestF4PassBounds`):

| Tier | Detection worst case | Worst single recovery | Bound | `TimeoutStartSec` |
|---|---|---|---|---|
| light | 340 s — 8 shared units x 30 s, 8 loopback probes x 5 s, one batched alert 60 s | shared-service restart 580 s (gate 60, reset 30, restart 120, poll 60 + final check 35, revalidation of 8 unit checks 240, postcondition 35); gateway restart 450 s (gate 30, governed restart capped 240, poll 120, checks 60) | 920 s | 1200 |
| deep | 1560 s — timers 300, boot gate 240, runtime config 120, watcher integrity 360, repository drift 360, registry check 120, alert 60 | vault sync 1320 s (gate 300, fetch/merge/push 540, remote proof 180, drift revalidation 360); Life Wiki validator 120 s | 2880 s | 3300 (< the hourly interval) |

Measured reality is far below these bounds (idle graceful gateway restarts on 2026-09-14 took 29–31 s from stop
to Telegram connected; a live light pass takes a few seconds). The gateway class only restarts a dead process or a
loop the gateway's own probe reports **wedged**: an alive or unknown loop would take the graceful drain, which can
wait `agent.restart_after_turn_timeout` (1800 s live) for in-flight turns, so it is refused and escalated instead.
When a pass cannot take the controller lock within `pass_lock_wait_seconds` (the other tier is still running,
bounded by its own timeout) it records `pass_skipped` and exits 0 without a heartbeat; a genuinely stuck pass
surfaces through `controller_heartbeat_fresh_<tier>`.

Escalation is exactly once per fingerprint: one `triage` card (unassigned, tenant-less,
`idempotency_key=health:<invariant>:<fingerprint>`, system provenance, `defect:` authority) and one
delivery-checked `hermes send` alert carrying identifiers and signatures only. A card that already
exists (e.g. after lost state) suppresses a repeat alert.

State, ledger and heartbeats: `~/.hermes/state/system-health-controller/`.

## State model — TEMPORARY governed exceptions (Christopher, 2026-09-14)

`state_model` selects how a pass reports. **Absent or `strict`** is the original behaviour:
GREEN or DEGRADED, and any held finding is DEGRADED. **`governed_exceptions`** is a temporary
stabilization measure so the controller can report a healthy runtime while intentionally frozen
or historically preserved conditions remain:

| Status | Meaning |
|---|---|
| `GREEN` | no actionable fault, no recovery in flight, nothing escalated, no exception active |
| `GREEN_WITH_HOLDS` | healthy; only conditions named by a valid governed exception remain (still observed every pass, visible in the heartbeat and state, one deduplicated card per exception) |
| `RECOVERY` | an allowlisted repair ran and has not revalidated yet (budget left) |
| `DEGRADED` | an actionable fault: detect-only escalation, a condition not yet confirmed, a new/changed condition on a frozen card (`frozen_condition_not_covered`), or an invalid/expired exception (`governed_exception_valid`) |
| `ESCALATED` | automatic repair failed its budget, or the finding is unsafe (security/boundary) — never covered by an exception, escalated at once |

Precedence when several apply: ESCALATED > DEGRADED > RECOVERY > GREEN_WITH_HOLDS > GREEN.

Each entry in `governed_exceptions.entries` names **exact conditions** (`invariant|subject|signature`),
`kind` (`recovery_hold` also freezes `task_ids`; `preserved_condition`), `owner`, `reason`,
`authorized_by`, `created`, `review_condition`, optional `expires_at`, and `authorization_sha256`
(`exception_authorization_digest`) pinning scope + authorization. An edited scope, a digest mismatch,
a missing field or a past expiry makes the entry invalid: it covers nothing and reports DEGRADED.
Task ids under **any** recovery hold — valid or not, and `recovery_holds` too — are never
automatically mutated. A condition on a frozen card that the exception does not name is DEGRADED.
Holds never hide an earlier escalation card (`previous_card_id` is kept).

**Revert:** delete the `state_model` key. Strict GREEN/DEGRADED returns immediately; the
`governed_exceptions` block is then ignored and can be removed at leisure; no board or ledger
history is rewritten. Review and revert once the backlog and the historical incidents are resolved
(`Business/Operations/2026-09-14-system-health-controller-t_b8d62378.md` §17).

Current entries: `phase3-hermes-cutover-freeze` (four exact Phase 3 conditions; owner Christopher;
review when the freeze is released) and `preserved-false-verified-records` (`t_e48487e5`,
`t_29c7a57b`; owner Erika; review on her incident ruling). `t_69440ff2` is deliberately **not**
covered — no authorization names it.

## Verify

```
cd ~/.hermes/hermes-agent-next
venv/bin/python -m pytest -q tests/hermes_cli/test_system_health_controller.py
venv/bin/python norcal/health/system_health_controller.py run --tier light --dry-run --json
```

## Deploy (separately gated — Phase E)

Requires the verifier-routing commits on the live checkout (its uncommitted `kanban_db.py` /
`exec_supervisor.py` edits must be committed by their owner first), a gateway restart, an
`alert_target`, then copying `systemd/*` to `~/.config/systemd/user/` and enabling both timers.
Rollback: disable the two timers; `git revert` the branch merge; restart the gateway.
