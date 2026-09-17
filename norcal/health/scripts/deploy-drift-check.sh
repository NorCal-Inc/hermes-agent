#!/usr/bin/env bash
# deploy-drift-check.sh
#
# Catches the exact failure class found 2026-08-20: a live systemd service
# running from a git checkout that has silently fallen behind its own
# origin/<branch> -- sometimes by months -- with no symptom until someone
# happens to exercise the specific code path that's missing.
#
# This script is the fix: it checks EVERY known service-backing git
# checkout against its remote, every run, and reports drift plainly.
# No LLM, no judgment call -- pure git commands, deterministic output.
# Intended to run under cron via the `cronjob` tool with no_agent=True
# (see the paired cron job), so it costs no tokens and can't be skipped
# by an agent forgetting to check.
#
# EXIT CONTRACT (2026-08-20): the exit code answers "did this check RUN",
# not "what did it find". Exiting 1 on findings made the scheduler record a
# correctly-working watchdog as 'Execution: failed / 2 failures in a row' --
# and a watchdog that reads as broken gets ignored, which is the cry-wolf
# failure this job exists to prevent. Findings travel in stdout, which is what
# a no_agent cron job delivers. DRIFT_STRICT_EXIT=1 restores exit-1-on-drift.
# stdout is designed to be silent (empty) when everything is clean, per
# the project's monitor convention: silence = nothing to report, output
# = something needs a look.
#
# VERDICT CONTRACT (2026-09-17, health finding t_a9ef4620 / fingerprint
# 7350d7ecd2f30810): a git command that fails to RUN (fetch/rev-list/status
# erroring out -- e.g. "dubious ownership" when the checkout is owned by a
# different account than the one running this check) is NOT evidence of
# divergence. It means the state is unknown. Before this fix, any such
# failure fell into the same DRIFT_FOUND flag as real ahead/behind/dirty
# findings, so a permission error and a genuinely out-of-sync checkout wrote
# the identical "DRIFT" verdict to the heartbeat -- indistinguishable to any
# consumer, including the system health controller, which escalated a
# permission hiccup as if it were confirmed repository divergence. The
# heartbeat verdict is now three-valued: CLEAN (verified, nothing to report),
# DRIFT (a git command ran and found real ahead/behind/dirty/missing/
# mismatch), or UNVERIFIED (a git command failed to answer the question at
# all -- fail closed, but distinctly, never silently reported as CLEAN and
# never conflated with confirmed DRIFT).

set -uo pipefail

# Per ~/.hermes/doctrine/operations.md v2.14 "Non-login shell execution": cron
# and scheduler invocations get no PATH additions and no ~/.hermes/.env
# credentials by default. This script fetches from private/token-authed
# remotes, so it must source the env explicitly rather than assume the
# caller already did. Safe to source even if already loaded.
if [ -f "$HOME/.hermes/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$HOME/.hermes/.env"
  set +a
fi

# service_name:working_directory:branch
# Add a new line here whenever a new git-backed systemd service is created.
# This file IS the inventory -- keep it current, it's the single source of
# truth for "which live services run from which checkout."
#
# DRIFT_SERVICES lets a regression test inject a fabricated inventory instead
# of this production list -- never set in production. Newline-separated
# entries in the same "name:dir:branch" format.
if [ -n "${DRIFT_SERVICES:-}" ]; then
  mapfile -t SERVICES <<< "$DRIFT_SERVICES"
else
  SERVICES=(
    "life-wiki-api:/home/chris/services/life-wiki/vault:main"
    "life-wiki-web:/home/chris/services/life-wiki/vault:main"
    "glass-pepper-api:/home/chris/hermes/repos/The-Glass-Pepper:main"
    "logos-covenant-api:/home/chris/hermes/repos/Logos-Covenant:main"
    "northcaledonia-api:/home/chris/hermes/repos/North-Caledonia:master"
    "orion-api:/home/chris/hermes/repos/OrionFormationServicesInc:main"
    "NorCal_Hermes:/home/chris/hermes/repos/NorCal_Hermes:main"
  )
fi

# DRIFT_FOUND: a git command ran successfully and observed real divergence
# (behind/ahead/dirty) or a real inventory problem (missing checkout, unit
# WorkingDirectory mismatch). INSPECTION_FAILED: a git command could not
# answer the question at all (fetch/rev-list/status errored -- permission,
# dubious ownership, network). Never conflate the two -- see VERDICT CONTRACT
# above.
DRIFT_FOUND=0
INSPECTION_FAILED=0
REPORT=""

# True when a git failure message is a permission/ownership refusal rather
# than a network or auth problem -- classified separately in the report text,
# but both are INSPECTION_FAILED, never DRIFT_FOUND.
_is_permission_error() {
  case "$1" in
    *"dubious ownership"*|*"Permission denied"*|*"permission denied"*) return 0 ;;
    *) return 1 ;;
  esac
}

for entry in "${SERVICES[@]}"; do
  svc="${entry%%:*}"
  rest="${entry#*:}"
  repo_dir="${rest%%:*}"
  branch="${rest#*:}"

  if [ ! -d "$repo_dir/.git" ]; then
    REPORT+="[MISSING] $svc: expected git repo at $repo_dir not found (checkout deleted or moved?)\n"
    DRIFT_FOUND=1
    continue
  fi

  # Confirm the systemd unit's WorkingDirectory still resolves inside this repo.
  # Catches the OTHER half of the bug class: unit re-pointed but this inventory not updated.
  unit_wd="$(systemctl --user show "${svc}.service" --property=WorkingDirectory 2>/dev/null | cut -d= -f2-)"
  if [ -n "$unit_wd" ] && [[ "$unit_wd" != "$repo_dir"* ]]; then
    REPORT+="[MISMATCH] $svc: systemd WorkingDirectory ($unit_wd) is not inside inventoried repo ($repo_dir) -- this inventory is stale, update deploy-drift-check.sh\n"
    DRIFT_FOUND=1
  fi

  # Fetch quietly, bounded, never blocks the whole run on one dead remote.
  if ! git -C "$repo_dir" fetch origin "$branch" --quiet 2>/tmp/drift-fetch-err-$$; then
    err="$(cat /tmp/drift-fetch-err-$$ 2>/dev/null | tr '\n' ' ')"
    if _is_permission_error "$err"; then
      REPORT+="[PERMISSION] $svc ($repo_dir): git inspection blocked by an ownership/permission error, state UNVERIFIED (not drift) -- $err\n"
    else
      REPORT+="[FETCH FAILED] $svc ($repo_dir): could not fetch origin/$branch, state UNVERIFIED (not drift) -- $err\n"
    fi
    INSPECTION_FAILED=1
    rm -f /tmp/drift-fetch-err-$$
    continue
  fi
  rm -f /tmp/drift-fetch-err-$$

  behind="$(git -C "$repo_dir" rev-list --count HEAD.."origin/$branch" 2>/tmp/drift-rl-err-$$)"
  rc_behind=$?
  ahead="$(git -C "$repo_dir" rev-list --count "origin/$branch"..HEAD 2>>/tmp/drift-rl-err-$$)"
  rc_ahead=$?
  local_head="$(git -C "$repo_dir" log -1 --format='%h %ci' 2>/dev/null || echo "?")"

  if [ "$rc_behind" -ne 0 ] || [ "$rc_ahead" -ne 0 ]; then
    err="$(cat /tmp/drift-rl-err-$$ 2>/dev/null | tr '\n' ' ')"
    if _is_permission_error "$err"; then
      REPORT+="[PERMISSION] $svc ($repo_dir): git inspection blocked by an ownership/permission error, state UNVERIFIED (not drift) -- $err\n"
    else
      REPORT+="[INSPECT FAILED] $svc ($repo_dir): could not compare against origin/$branch, state UNVERIFIED (not drift) -- $err\n"
    fi
    INSPECTION_FAILED=1
    rm -f /tmp/drift-rl-err-$$
    continue
  fi
  rm -f /tmp/drift-rl-err-$$

  if [ "$behind" != "0" ]; then
    REPORT+="[DRIFT] $svc ($repo_dir): $behind commit(s) behind origin/$branch. Deployed HEAD: $local_head\n"
    DRIFT_FOUND=1
  fi

  if [ "$ahead" != "0" ]; then
    REPORT+="[UNPUSHED] $svc ($repo_dir): $ahead commit(s) exist locally but never reached origin/$branch -- possible data at risk if this checkout is ever discarded.\n"
    DRIFT_FOUND=1
  fi

  # Uncommitted/untracked changes sitting in a deploy checkout are themselves
  # a risk (exactly what blocked the life-wiki-vault pull today) -- surface them.
  dirty="$(git -C "$repo_dir" status --porcelain 2>/tmp/drift-st-err-$$ | wc -l)"
  rc_status=${PIPESTATUS[0]}
  if [ "$rc_status" -ne 0 ]; then
    err="$(cat /tmp/drift-st-err-$$ 2>/dev/null | tr '\n' ' ')"
    if _is_permission_error "$err"; then
      REPORT+="[PERMISSION] $svc ($repo_dir): git inspection blocked by an ownership/permission error, state UNVERIFIED (not drift) -- $err\n"
    else
      REPORT+="[INSPECT FAILED] $svc ($repo_dir): could not read working-tree status, state UNVERIFIED (not drift) -- $err\n"
    fi
    INSPECTION_FAILED=1
  elif [ "$dirty" != "0" ]; then
    REPORT+="[DIRTY] $svc ($repo_dir): $dirty uncommitted/untracked path(s) in the deploy checkout.\n"
    DRIFT_FOUND=1
  fi
  rm -f /tmp/drift-st-err-$$
done

# Heartbeat: this job is silent when clean, and per execution-honesty.md a job that
# reports only on failure is indistinguishable from one that never ran. Written every
# run, clean or not, so "no alert last night" becomes a checkable claim.
HEARTBEAT="${DRIFT_HEARTBEAT:-$HOME/.hermes/state/deploy-drift-check.json}"
mkdir -p "$(dirname "$HEARTBEAT")"
# Priority: a confirmed real divergence always outranks an unrelated
# inspection failure elsewhere in the run. Only when nothing could be
# confirmed AND at least one repo could not be inspected do we report
# UNVERIFIED -- never CLEAN when something was skipped, per fail-closed.
if [ "$DRIFT_FOUND" -eq 1 ]; then
  DRIFT_VERDICT=DRIFT
elif [ "$INSPECTION_FAILED" -eq 1 ]; then
  DRIFT_VERDICT=UNVERIFIED
else
  DRIFT_VERDICT=CLEAN
fi
{
  echo "{"
  echo "  \"checker\": \"deploy-drift-check.sh\","
  echo "  \"ran_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"verdict\": \"$DRIFT_VERDICT\""
  echo "}"
} > "$HEARTBEAT"

if [ "$DRIFT_FOUND" -eq 1 ] || [ "$INSPECTION_FAILED" -eq 1 ]; then
  printf "DEPLOY DRIFT CHECK -- issues found ($(date -Iseconds))\n\n"
  printf "%b" "$REPORT"
  [ -n "${DRIFT_STRICT_EXIT:-}" ] && exit 1
  exit 0
else
  # Silent on success by design -- no_agent cron jobs stay quiet when clean.
  exit 0
fi
