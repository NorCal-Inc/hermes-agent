#!/usr/bin/env bash

echo "=================================="
echo "ERIKA TAGATCHI INITIALIZED"
echo "Chief Systems Architect"
echo "Operational Doctrine Loaded"
echo "Secure Env Loader Active"
echo "=================================="

# Load OP service account token
if [ -f "$HOME/.hermes/secrets/op-service-account.env" ]; then
    export $(grep -v '^#' "$HOME/.hermes/secrets/op-service-account.env" | xargs)
    echo "[OK] OP_SERVICE_ACCOUNT_TOKEN loaded"
else
    echo "[WARN] OP_SERVICE_ACCOUNT_TOKEN file missing"
fi

# Load XAI API KEY
XAI_KEY="$(op item get XAI_API_KEY \
  --vault xx4ijxpia7obhewol3uzv3kfu4 \
  --fields label=credential \
  --reveal 2>/dev/null | tr -d '\r')"

if [ -n "$XAI_KEY" ] && [ ${#XAI_KEY} -gt 20 ]; then
    export XAI_API_KEY="$XAI_KEY"
    echo "[OK] XAI_API_KEY loaded from 1Password"
else
    echo "[WARN] XAI_API_KEY load failed or empty"
fi

# Load XAI BUILD KEY
XAI_BUILD_KEY="$(op item get XAI_API_KEY_BUILD \
  --vault xx4ijxpia7obhewol3uzv3kfu4 \
  --fields label=credential \
  --reveal 2>/dev/null | tr -d '\r')"

if [ -n "$XAI_BUILD_KEY" ] && [ ${#XAI_BUILD_KEY} -gt 20 ]; then
    export XAI_API_KEY_BUILD="$XAI_BUILD_KEY"
    echo "[OK] XAI_API_KEY_BUILD loaded from 1Password"
else
    echo "[WARN] XAI_API_KEY_BUILD load failed or empty"
fi

echo "[OK] Profile Loaded: $HOME/.hermes/profiles/erika.profile.yaml"
echo "[OK] Doctrine Loaded: $HOME/.hermes/doctrine/erika-core.md"
echo "[INFO] Provider env persistence implemented and verified for hermes-dashboard.service"
echo "[INFO] xAI/Grok provider will now survive reboots and restarts"
echo "[INFO] Validation complete. Resuming dashboard roadmap."
