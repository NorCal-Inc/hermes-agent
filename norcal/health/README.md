# NorCal system health controller

Kanban task `t_b8d62378`. One deterministic controller above the existing monitors:
`OBSERVE -> CLASSIFY -> RECOVER -> REVALIDATE -> GREEN/CONTINUE`, else `ESCALATE/DEGRADED`.
It consumes existing surfaces (Kanban board, Hermes cron `jobs.json`, user systemd timers, the
gateway heartbeat, the canonical boot gate, `hermes config get`) and replaces none of them.
No LLM; silent when GREEN.

- `system_health_controller.py` — controller, invariants, CLI (`run --tier light|deep [--dry-run] [--json]`, `status`).
- `health-controller.json` — watched jobs/timers, doctrine runtime ceilings, `alert_target` (the Hermes Telegram "Alerts" group, the `TELEGRAM_ALERTS_CHAT_ID` channel the gateway watchdog and post-update health check already use; if that id changes, update both).
- `systemd/` — user unit templates: light pass every 5 min, deep pass hourly, `OnFailure=` alert. Installed as user units (Phase E Gate 6, 2026-09-14).

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
