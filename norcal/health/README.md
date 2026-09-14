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

Escalation is exactly once per fingerprint: one `triage` card (unassigned, tenant-less,
`idempotency_key=health:<invariant>:<fingerprint>`, system provenance, `defect:` authority) and one
delivery-checked `hermes send` alert carrying identifiers and signatures only. A card that already
exists (e.g. after lost state) suppresses a repeat alert.

State, ledger and heartbeats: `~/.hermes/state/system-health-controller/`.

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
