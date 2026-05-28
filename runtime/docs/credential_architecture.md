# Hermes Credential Architecture

**Authoritative Source**: 1Password (North Caledonia vault ID `xx4ijxpia7obhewol3uzv3kfu4`)

## Components

- **Persistent Token**: `~/.hermes/secrets/op-service-account.env` (chmod 600, loaded via EnvironmentFile in systemd)
- **Runtime Bootstrap**: `~/.hermes/bootstrap/erika-init.sh` — **MUST** be *sourced* in same shell
- **Healthcheck**: `~/.hermes/bootstrap/check-credentials.sh` — enforces full ladder
- **Repair**: `~/.hermes/bootstrap/repair-credentials.sh` — layered restart + log analysis
- **Central Aliases**: `~/.hermes/provider-aliases.yaml` — single source of truth for all model names
- **Incident Log**: `~/.hermes/docs/incidents/2026-05-28-xai-credential-routing-failure.md`
- **Systemd Override**: `~/.config/systemd/user/hermes-dashboard.service.d/override.conf`

## Required Retrieval Pattern (1Password)
```bash
XAI_KEY="$(op item get XAI_API_KEY \
  --vault xx4ijxpia7obhewol3uzv3kfu4 \
  --fields label=credential \
  --reveal 2>/dev/null | tr -d '\r')"
```
Same for `XAI_API_KEY_BUILD`. **Never omit `--reveal`**. Never accept placeholder text containing "use 'op item get". Do not use `op read` for these runtime keys (use only after separate validation).

## Fail-Fast Rules
- Missing `OP_SERVICE_ACCOUNT_TOKEN` → abort
- `op vault list` fails → abort
- Empty or masked XAI key → abort
- `curl https://api.x.ai/v1/models` fails → abort
- Configured model not in `/v1/models` list → abort
- `hermes chat --model grok_reasoning_primary` does not return `GROK_OK` → abort
- No degraded routing allowed.

## Model Alias Policy
**All** routing, config, tests, docs, doctrine, scripts **must** reference aliases from central file. No scattered dated IDs like `grok-4.20-reasoning` or `xai/grok-4.20-reasoning`.

Current known-good (as of 2026-05-28):
```yaml
grok_reasoning_primary: grok-4.20-0309-reasoning
grok_builder: grok-build-0.1
grok_fast: grok-4.3
```

## Operational Rule
Debug **only** in strict order. Stop at first failure:
1. systemd
2. OP_SERVICE_ACCOUNT_TOKEN
3. 1Password auth (`op whoami`, `op vault list`)
4. Secret hydration (keys loaded, not placeholders)
5. Provider validation (`curl /v1/models`)
6. Model validation (target model in catalog)
7. Routing initialization
8. Hermes runtime
9. dashboard/UI

Bootstrap files live outside the hermes-agent git repo by design for secret isolation but **must be mirrored** under governance (see incident doc). Changes to recovery logic go through PR in NorCal-Inc/hermes-agent (runtime/ dir).

See incident document for full root cause, recovery, and prevention.
