# 2026-05-28 xAI Credential & Model Routing Failure

## Incident Date
2026-05-28

## Symptom
xAI/Grok provider routing failed in Hermes dashboard and agent sessions. Models resolved to invalid `xai/grok-4.20-reasoning`. 1Password credentials appeared as masked placeholders `[use 'op item get ... --reveal' to reveal]`. OP_SERVICE_ACCOUNT_TOKEN not reliably available to hermes-dashboard.service. Provider validation and model catalog checks were bypassed, allowing degraded routing.

## Root Cause Chain
1. `erika-init.sh` was executed as a child process (`bash -c 'script.sh && ...'`) instead of sourced in the same shell (`bash -lc 'source script.sh && exec ...'`), so exported `XAI_API_KEY*` variables did not persist to the Python daemon.
2. `hermes-dashboard.service` lacked `EnvironmentFile=%h/.hermes/secrets/op-service-account.env`.
3. 1Password retrieval used `op read` or omitted `--reveal`, returning placeholder text instead of real credential.
4. Hermes provider initialization accepted placeholder/bad keys instead of failing fast.
5. Runtime config and code scattered hardcoded deprecated model name `xai/grok-4.20-reasoning` (correct: `grok-4.20-0309-reasoning` as of incident).
6. No central alias file; no enforcement that all routing uses aliases.
7. Bootstrap/auth logic lived outside git governance at `~/.hermes/bootstrap/erika-init.sh`.
8. No layered healthcheck enforcing systemd → OP token → 1PW → provider validation → model validation → Hermes runtime before allowing dashboard/UI.

No single point of failure; it was a systemic gap in credential hydration, service lifecycle, validation gating, and governance.

## Files Changed
- `~/.hermes/bootstrap/erika-init.sh` (sourcing + exact op item get --reveal loaders + fail-fasts)
- `~/.hermes/bootstrap/check-credentials.sh` (full ladder checks + GROK_OK test)
- `~/.hermes/bootstrap/repair-credentials.sh` (newest logs, exact failure reporting, layered refusal)
- `~/.config/systemd/user/hermes-dashboard.service.d/override.conf` (EnvironmentFile + proper sourced ExecStart)
- `~/.hermes/docs/credential_architecture.md` (expanded)
- `~/.hermes/docs/incidents/2026-05-28-xai-credential-routing-failure.md` (this document)
- Central alias file created: `~/.hermes/provider-aliases.yaml`
- Bootstrap files mirrored into governed repo (see below)
- Updates to routing code to use aliases only (no scattered model IDs)

## Working Validation Commands
```bash
# Full ladder
~/.hermes/bootstrap/check-credentials.sh
systemctl --user status hermes-dashboard.service --no-pager
journalctl --user -u hermes-dashboard.service -n 30 --no-pager | grep -E "OP_SERVICE|XAI|OK|WARN|ERROR"
curl -fsS https://api.x.ai/v1/models -H "Authorization: Bearer $XAI_API_KEY" | head -10
hermes chat -q "reply only with: GROK_OK" --model grok_reasoning_primary
```

Expected:
```
[OK] OP_SERVICE_ACCOUNT_TOKEN loaded
[OK] XAI_API_KEY loaded from 1Password
[OK] XAI_API_KEY_BUILD loaded from 1Password
...
GROK_OK
```

## Recovery Command Block
```bash
~/.hermes/bootstrap/repair-credentials.sh
# or manually:
systemctl --user daemon-reload
systemctl --user restart hermes-dashboard.service
journalctl --user -u hermes-dashboard.service -n 60 --no-pager | grep -E "OP_SERVICE|XAI|WARN|ERROR|loaded"
curl -fsS https://api.x.ai/v1/models -H "Authorization: Bearer $XAI_API_KEY"
hermes chat -q "reply only with: GROK_OK" --model grok-4.20-0309-reasoning
```

## Prevention Requirements
- **Sourcing rule**: Always `source` bootstrap inside the *same* systemd shell. Never child exec.
- **1PW pattern**: Always `op item get ... --vault <ID> --fields label=credential --reveal`. Never omit --reveal. Never use `op read` for runtime keys unless separately validated. Reject any output containing "[use 'op item get".
- **Fail-fast**: If OP_SERVICE_ACCOUNT_TOKEN missing, op vault list fails, key empty/masked, curl /v1/models fails, model not in catalog, or hermes test fails → ABORT startup. No degraded mode.
- **Central aliases only**: All code/config/tests/docs MUST reference aliases from `provider-aliases.yaml`. No hardcoded `grok-4.20-reasoning`, `xai/grok-4.20-reasoning`, or dated IDs.
- **Layered debugging**: Never debug dashboard/routing/autonomy/Erika until *all* prior layers pass: systemd > OP token > 1PW auth > secret hydration > provider validation > model validation > routing init > Hermes runtime > dashboard/UI. Stop at first failure.
- **Governance**: Bootstrap, check/repair scripts, override.conf, credential_architecture.md, and incident docs MUST live under git control (NorCal-Inc/hermes-agent or hermes-runtime repo). No critical recovery logic left only in `~/.hermes/`.

## Governance Note
Bootstrap, check/repair, systemd override, credential architecture, and this incident document currently live outside the hermes-agent git repo. They have been mirrored/copied into the governed source at `runtime/bootstrap/` (or equivalent) in NorCal-Inc/hermes-agent. Future changes must be made in the repo and synced. This prevents drift and ensures auditability.

## Related
- `~/.hermes/docs/credential_architecture.md`
- Provider alias enforcement in model routing (hermes-agent/agent/provider_router.py etc.)
- 1Password service account setup in NorCalOps vaults.
