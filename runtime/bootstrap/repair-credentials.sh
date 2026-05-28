#!/usr/bin/env bash
set -euo pipefail

LOG="$HOME/.hermes/logs/credential-repair.log"
mkdir -p "$(dirname "$LOG")"

echo "=== Credential Repair Started $(date '+%Y-%m-%d %H:%M:%S') ===" | tee -a "$LOG"
echo "{\"event\":\"credential_repair_started\",\"status\":\"running\"}" | tee -a "$LOG"

# Run full check first - will abort on any layer failure
echo "Running full credential ladder check..."
if ! "$HOME/.hermes/bootstrap/check-credentials.sh"; then
  echo "[CRITICAL] Credential or model layer failed. Refusing to proceed with repair." | tee -a "$LOG"
  echo "{\"event\":\"credential_repair_aborted\",\"reason\":\"check_failed\",\"status\":\"failed\"}" | tee -a "$LOG"
  exit 1
fi

echo "All layers passed. Proceeding with systemd repair..."

# Reload and restart (as required)
systemctl --user daemon-reload
systemctl --user restart hermes-dashboard.service

# Wait a bit for startup
sleep 3

# Read *newest logs only* and analyze for exact failed dependency (if any)
echo "=== Newest Logs Analysis (last 60 lines) ===" | tee -a "$LOG"
journalctl --user -u hermes-dashboard.service -n 60 --no-pager | tee -a "$LOG" | grep -E "OP_SERVICE|XAI|WARN|ERROR|FAIL|OK|loaded|GROK_OK|credential|provider|model" || true

if ! systemctl --user is-active --quiet hermes-dashboard.service; then
  echo "[FAIL] Dashboard service not active after restart. Check logs above for exact failed dependency." | tee -a "$LOG"
  echo "{\"event\":\"credential_repair_failed\",\"reason\":\"service_not_active_after_restart\",\"status\":\"failed\"}" | tee -a "$LOG"
  exit 1
fi

# Final verification
if "$HOME/.hermes/bootstrap/check-credentials.sh" > /dev/null 2>&1; then
  echo "[SUCCESS] Repair completed. All layers verified. No secrets printed." | tee -a "$LOG"
  echo "{\"event\":\"credential_repair_completed\",\"status\":\"ok\"}" | tee -a "$LOG"
else
  echo "[FAIL] Post-repair check failed. Refusing to mark as successful." | tee -a "$LOG"
  exit 1
fi

echo "Recovery complete. Use 'journalctl --user -u hermes-dashboard.service -n 30 --no-pager' for details."
