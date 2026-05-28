#!/usr/bin/env bash
set -euo pipefail

SECRET_FILE="$HOME/.hermes/secrets/op-service-account.env"
VAULT_ID="xx4ijxpia7obhewol3uzv3kfu4"
LOG="$HOME/.hermes/logs/credential-repair.log"
ALIAS="grok_reasoning_primary"
TARGET_MODEL="grok-4.20-0309-reasoning"

mkdir -p "$(dirname "$LOG")"
echo "=== Credential Ladder Check $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"

fail() {
  local reason="$1"
  echo "[FAIL] $reason" | tee -a "$LOG"
  echo "{\"event\":\"credential_check_failed\",\"reason\":\"$reason\",\"status\":\"failed\"}" | tee -a "$LOG"
  exit 1
}

ok() {
  local msg="$1"
  echo "[OK] $msg" | tee -a "$LOG"
}

# 1. OP_SERVICE_ACCOUNT_TOKEN file exists and loads
[ -f "$SECRET_FILE" ] || fail "missing_op_service_account_file"
chmod 600 "$SECRET_FILE"
# shellcheck disable=SC1090
source "$SECRET_FILE"
[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ] || fail "missing_op_service_account_token"
ok "OP_SERVICE_ACCOUNT_TOKEN loaded"

# 2. op vault list works
command -v op >/dev/null 2>&1 || fail "op_cli_missing"
op vault list --no-color >/dev/null 2>&1 || fail "op_vault_list_failed"
ok "op vault list works"

# 3. XAI_KEY with exact required pattern + validation
XAI_KEY="$(op item get XAI_API_KEY \
  --vault $VAULT_ID \
  --fields label=credential \
  --reveal 2>/dev/null | tr -d '\r')"

if [ -z "$XAI_KEY" ] || [[ "$XAI_KEY" == *'use '\''op item get'* ]] || [[ "$XAI_KEY" == *'[use'* ]]; then
  fail "XAI_API_KEY is masked placeholder or empty"
fi
if [ ${#XAI_KEY} -lt 30 ]; then
  fail "XAI_API_KEY too short to be valid"
fi
export XAI_API_KEY="$XAI_KEY"
ok "XAI_API_KEY loaded from 1Password (using op item get --reveal)"

# 4. XAI_BUILD_KEY same pattern
XAI_BUILD_KEY="$(op item get XAI_API_KEY_BUILD \
  --vault $VAULT_ID \
  --fields label=credential \
  --reveal 2>/dev/null | tr -d '\r')"

if [ -z "$XAI_BUILD_KEY" ] || [[ "$XAI_BUILD_KEY" == *'use '\''op item get'* ]] || [[ "$XAI_BUILD_KEY" == *'[use'* ]]; then
  fail "XAI_API_KEY_BUILD is masked placeholder or empty"
fi
if [ ${#XAI_BUILD_KEY} -lt 30 ]; then
  fail "XAI_API_KEY_BUILD too short to be valid"
fi
export XAI_API_KEY_BUILD="$XAI_BUILD_KEY"
ok "XAI_API_KEY_BUILD loaded from 1Password (using op item get --reveal)"

# 5. Provider validation command
if ! curl -fsS https://api.x.ai/v1/models \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -o /tmp/xai_models.json 2>/tmp/curl_err.log; then
  cat /tmp/curl_err.log >&2
  fail "xAI_provider_validation_failed"
fi
ok "xAI /v1/models responded successfully"

# 6. Configured model exists in list
if ! grep -q '"id":\s*["'"'"']'"$TARGET_MODEL"'["'"'"']' /tmp/xai_models.json && ! grep -q "$TARGET_MODEL" /tmp/xai_models.json; then
  echo "Target model '$TARGET_MODEL' not found. First 10 models:" | tee -a "$LOG"
  jq -r '.data[].id' /tmp/xai_models.json 2>/dev/null | head -10 || head -20 /tmp/xai_models.json
  fail "target_model_not_in_catalog"
fi
ok "configured model $TARGET_MODEL exists in xAI catalog"

# 7. Hermes runtime test with alias preference
echo "Running Hermes test with $ALIAS (resolves to $TARGET_MODEL)..." | tee -a "$LOG"
if HERMES_OUT=$(hermes chat -q "reply only with: GROK_OK" --model "$ALIAS" 2>&1); then
  if echo "$HERMES_OUT" | grep -q "GROK_OK"; then
    ok "hermes chat test passed"
    echo "GROK_OK"
    rm -f /tmp/xai_models.json /tmp/curl_err.log 2>/dev/null || true
    echo "[SUCCESS] Full dependency ladder passed" | tee -a "$LOG"
    exit 0
  fi
fi
# Fallback to direct model
if HERMES_OUT=$(hermes chat -q "reply only with: GROK_OK" --model "$TARGET_MODEL" 2>&1); then
  if echo "$HERMES_OUT" | grep -q "GROK_OK"; then
    ok "hermes chat test passed (direct model)"
    echo "GROK_OK"
    rm -f /tmp/xai_models.json /tmp/curl_err.log 2>/dev/null || true
    echo "[SUCCESS] Full dependency ladder passed" | tee -a "$LOG"
    exit 0
  fi
fi
echo "Hermes test output was: $HERMES_OUT" | tee -a "$LOG"
fail "hermes_runtime_test_failed"
