#!/usr/bin/env bash
# PreToolUse guard for Claude Code's Bash tool.
#
# Blocks the four highest-confidence recurring command patterns behind real
# incidents logged in the Life Wiki vault's Jobs/mistake-logging.md, per
# Business/Systems/2026-08-18-agent-incident-response-proposal.md and the
# analysis it extends, Business/Systems/2026-08-14-vault-memory-vs-mechanism-gap.md.
#
# Reads PreToolUse hook JSON on stdin, emits PreToolUse hook JSON on stdout.
set -euo pipefail

input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

deny() {
  local reason="$1"
  jq -n --arg reason "$reason" '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: $reason
    },
    systemMessage: $reason
  }'
  exit 0
}

advisories=()
advisory() {
  advisories+=("$1")
  # Emit only once at the end. Security refusals must remain valid single JSON.
}

[ -z "$cmd" ] && exit 0

# 1. Heredoc nested inside an ssh command.
# mistake-logging.md: 2026-08-09 (x2), 2026-08-10, 2026-08-16 — same failure
# four times. Outer quoting breaks the heredoc before it exists; a vault note
# or commit message gets silently mangled while the wrapping script reports
# success.
if printf '%s' "$cmd" | grep -qE '(^|[;&|])[[:space:]]*ssh[[:space:]]' \
   && printf '%s' "$cmd" | grep -qE "<<[[:space:]]*['\"]?[A-Za-z_]+"; then
  advisory "Advisory: heredoc nested inside an ssh command. This exact pattern corrupted a vault note 4 times (mistake-logging.md 2026-08-09 x2, 2026-08-10, 2026-08-16) because the outer quoting breaks the heredoc before it exists, and the wrapping script still reports success. Stage the payload as a local file, push it (hermes-push.ps1 or scp), and run/apply it by path instead."
fi

# 1b. Inline interpreter payload or heredoc inside a remote/nested shell string
# (ssh ... or bash/sh -c ...), added 2026-09-16 (t_710184c8). Same failure
# class as check 1: the payload's own quoting fights the outer string. The
# remote-exec token must PRECEDE the payload, so a local `python3 - <<EOF`
# or `python3 script.py` stays allowed, as does `ssh host python3 - < file.py`.
flat="$(printf '%s' "$cmd" | tr '\n' ' ')"
if printf '%s' "$flat" | grep -qE "(^|[;&|(])[[:space:]]*(sudo[[:space:]]+)?(ssh[[:space:]]|(bash|sh)[[:space:]]+-c[[:space:]]).*(python3?[[:space:]]+-c[[:space:]]|<<[[:space:]]*['\"]?[A-Za-z_]+)"; then
  advisory "Advisory: inline python -c payload or heredoc embedded inside an ssh/bash -c string. Nested quoting mangles the payload while the wrapper still reports success (mistake-logging.md 2026-08-09 x2, 2026-08-10, 2026-08-16). Write the payload to a file (Write tool), then run it by path: ssh host 'python3 /path/script.py', or bash /path/script.sh."
fi

# 2. pkill/pgrep -f.
# mistake-logging.md 2026-08-14 — matched its own invoking ssh command line
# and killed the shell running it. Must be in command position (start, after
# ; & | ( $( or an opening quote) so prose inside a heredoc or commit message
# that merely mentions the pattern is not blocked (false positive observed
# 2026-09-16, t_710184c8).
if printf '%s' "$cmd" | grep -qE "(^|[;&|(\"']|\\\$\()[[:space:]]*(sudo[[:space:]]+)?(pkill)[[:space:]][^|;&]*-[a-zA-Z]*f\b"; then
  deny "Blocked: pkill/pgrep -f can match its own invoking command line and kill the shell running it (mistake-logging.md 2026-08-14). Match on an explicit PID instead (ps/pgrep without -f to find it, then kill <pid>), or use the tool's own lifecycle command if one exists (e.g. claude daemon stop --any)."
fi

# 3. Unredacted `git remote -v`.
# mistake-logging.md 2026-08-07 — printed a live GitHub PAT embedded in the
# remote URL into the transcript.
# `git -C <dir> remote -v` bypassed the original pattern; covered 2026-09-16 (t_710184c8).
if printf '%s' "$cmd" | grep -qE 'git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+remote[[:space:]]+(-v|--verbose)' \
   && ! printf '%s' "$cmd" | grep -qE 'sed|awk'; then
  deny "Blocked: unredacted 'git remote -v' can print a live token embedded in the remote URL (mistake-logging.md 2026-08-07). Redact at the command: git remote -v | sed -E 's#//[^@]*@#//***@#g'"
fi

# 4. Raw reads of credential-shaped files.
# mistake-logging.md 2026-08-06/07/16 — three separate live-credential leaks
# this way (.replit -> Apple Sign-In key + DB passphrase, an uploaded .json
# -> OAuth client_secret, a source-the-.env check -> partial password).
# `cut -d=` is the doctrine-recommended safe idiom (names only) and is
# exempted so it still passes.
cred_pattern='\.env\b|\.replit\b|token|secret|credential|docker-compose\.ya?ml\b|\.toml\b|wrangler\.toml\b|Procfile\b|netlify\.toml\b|app\.ya?ml\b|id_rsa|\.pem\b|\.key\b|/incoming/.*\.(json|env)\b'
if printf '%s' "$cmd" | grep -qE '\b(cat|head|tail|less|more)\b' \
   && printf '%s' "$cmd" | grep -qEi "$cred_pattern" \
   && ! printf '%s' "$cmd" | grep -qE 'cut -d='; then
  deny "Blocked: raw read of a credential-shaped file/path. Three logged incidents printed live credentials this way (.replit -> Apple key + DB passphrase, an uploaded .json -> OAuth client_secret, git remote -v -> GitHub PAT; mistake-logging.md 2026-08-06/07/16). Extract the fact you need instead: grep -n '^[section]' -A6 <file>, or cut -d= -f1 <file> for key names only — never render the whole file."
fi

# 5. Bare `gh pr list`.
# mistake-logging.md 2026-08-06 — fails auth in a non-login shell and reads
# as "no open PRs"; 4 draft PRs sat stranded for a week because of this.
if printf '%s' "$cmd" | grep -qE '(^|[;&|]|&&)[[:space:]]*gh pr list\b'; then
  advisory "Advisory: bare 'gh pr list' fails auth in a non-login shell and silently reads as 'no open PRs' (mistake-logging.md 2026-08-06 — 4 draft PRs sat stranded for a week because of this). Use ~/.local/bin/vault-pr-check instead (full path; ~/.local/bin is not on a non-login shell's PATH)."
fi

# 6. Third-party skill/plugin install and update commands.
# Supply-chain rule 2026-08-21: discovery is allowed, adoption is a separate reviewed intake.
# Cleared copies are frozen locally and upstream updates never apply automatically.
if printf '%s' "$cmd" | grep -qEi '(^|[;&|])[[:space:]]*(npx[[:space:]]+skills[[:space:]]+(add|update)|hermes[[:space:]]+skills[[:space:]]+(install|update|reset|opt-in)|hermes[[:space:]]+plugins[[:space:]]+(install|update))\b'; then
  deny "Blocked by skill supply-chain policy: third-party skill/plugin install or update cannot run directly. Record the source, immutable commit/ref, files, permissions, and hashes; review the code and behavior; then import a cleared local copy through the governed intake. Installed cleared copies never update from upstream."
fi

# 7. Direct git mutation of externally sourced skill/plugin trees.
if printf '%s' "$cmd" | grep -qEi '(git[[:space:]]+(pull|fetch|merge|rebase|reset|checkout)|git[[:space:]]+-C[^;&|]*(pull|fetch|merge|rebase|reset|checkout))' \
   && printf '%s' "$cmd" | grep -qEi '(/\.agents/skills|/\.hermes/plugins|/\.hermes/skills/)'; then
  deny "Blocked by skill supply-chain policy: do not git-update an installed skill or plugin tree. Upstream is reference-only after intake. Build local derivatives from the frozen cleared baseline instead."
fi

# 8. Signalling a supervised process before checking its lifecycle command.
# mistake-logging.md 2026-08-14 — the claude daemon was fought with kill through
# three self-restarts while `claude daemon stop --any` existed all along.
# Added 2026-09-16 (t_710184c8). Mechanical, per session: a pkill/killall of a
# known-supervised name, or kill $(pgrep|pidof <name>), is denied until this
# session has run the owning binary's help (e.g. `claude daemon --help`) or
# `systemctl ... status|help`. Bare `kill <pid>` cannot be attributed and is
# deliberately not blocked.
supervised='claude|bg-pty-host|hermes|hermes-gateway|caddy|dockerd|containerd|postgres|redis-server|tmux'
sid="$(printf '%s' "$input" | jq -r '.session_id // "nosession"' | tr -c 'A-Za-z0-9_.-' '_')"
lc_file="/home/chris/.claude/session-gates/lifecycle/${sid}"
owner_of() {
  case "$1" in
    claude|bg-pty-host) echo claude ;;
    hermes|hermes-gateway) echo hermes ;;
    dockerd|containerd) echo docker ;;
    tmux) echo tmux ;;
    *) echo systemctl ;;
  esac
}
looked_up="$(printf '%s' "$flat" | grep -oE "(^|[;&|(])[[:space:]]*(sudo[[:space:]]+)?(claude|hermes|caddy|docker|tmux|systemctl)\b[^;&|]*([[:space:]]--help\b|[[:space:]]-h\b|[[:space:]]help\b|[[:space:]]status\b)" | grep -oE '(claude|hermes|caddy|docker|tmux|systemctl)' | head -1 || true)"
if [ -n "$looked_up" ]; then
  mkdir -p "$(dirname "$lc_file")" && printf '%s\n' "$looked_up" >> "$lc_file"
  [ "$looked_up" = caddy ] && printf 'systemctl\n' >> "$lc_file"
fi
target="$(printf '%s' "$flat" | grep -oE "(^|[;&|(])[[:space:]]*(sudo[[:space:]]+)?(pkill|killall)[[:space:]][^;&|]*\b($supervised)\b|(^|[;&|(])[[:space:]]*(sudo[[:space:]]+)?kill[[:space:]][^;&|]*\\\$\((pgrep|pidof)[^)]*\b($supervised)\b" | grep -oE "\b($supervised)\b" | tail -1 || true)"
if [ -n "$target" ]; then
  owner="$(owner_of "$target")"
  if ! grep -qx "$owner" "$lc_file" 2>/dev/null; then
    deny "Blocked: '$target' looks supervised (it has its own lifecycle manager), and this session has not checked that manager's stop path yet. Signalling a supervised process makes it respawn and fight back (mistake-logging.md 2026-08-14: claude daemon, three respawns; the fix was 'claude daemon stop --any'). First run '$owner --help' (or '$owner <subcommand> --help'; for services 'systemctl --user status <unit>') and use its stop/shutdown command. If none exists, this kill is allowed after that lookup."
  fi
fi

if [ "${#advisories[@]}" -gt 0 ]; then
  joined="$(printf '%s\n' "${advisories[@]}")"
  jq -n --arg reason "$joined" '{systemMessage: $reason}'
fi

exit 0
