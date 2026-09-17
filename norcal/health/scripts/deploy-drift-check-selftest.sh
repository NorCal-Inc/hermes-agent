#!/usr/bin/env bash
# deploy-drift-check-selftest.sh
#
# Regression coverage for the verdict fix in deploy-drift-check.sh (health
# finding t_a9ef4620 / fingerprint 7350d7ecd2f30810): a git inspection
# failure (dubious ownership, fetch failure) must produce verdict UNVERIFIED,
# never CLEAN (fail-closed) and never DRIFT (never conflated with confirmed
# divergence).
#
# Uses DRIFT_SERVICES / DRIFT_HEARTBEAT to point the checker at fabricated
# repos instead of the production inventory -- never touches any real
# service checkout. GIT_TEST_ASSUME_DIFFERENT_OWNER=1 makes git itself emit
# the real "dubious ownership" fatal error (git's own test-only hook for
# this, no root/chown required), so case 2 reproduces the actual failure
# mode rather than a stubbed approximation.
#
# Exits 0 and prints PASS for each case, or exits 1 on the first failure.

set -uo pipefail

SCRIPT="$(dirname "$0")/deploy-drift-check.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

_git() { git -c user.email=t@t -c user.name=t -c init.defaultBranch=main -C "$1" "${@:2}"; }

# ---------------------------------------------------------------------------
# Case 1: clean, synchronized repo -> CLEAN, silent stdout, exit 0
# ---------------------------------------------------------------------------
repo1="$WORK/case1-repo"
mkdir -p "$repo1"
_git "$repo1" init -q
echo a > "$repo1/a.txt"
_git "$repo1" add a.txt
_git "$repo1" commit -q -m one
_git "$repo1" update-ref refs/remotes/origin/main HEAD
# origin is the repo itself acting as its own remote: `git fetch origin main`
# resolves via the configured remote URL, so wire one up pointing at a bare
# clone that matches current HEAD exactly.
bare1="$WORK/case1-bare.git"
git clone -q --bare "$repo1" "$bare1"
_git "$repo1" remote add origin "$bare1"

out="$(DRIFT_SERVICES="case1:$repo1:main" \
      DRIFT_HEARTBEAT="$WORK/case1.json" \
      bash "$SCRIPT")"
rc=$?
verdict="$(python3 -c "import json;print(json.load(open('$WORK/case1.json'))['verdict'])")"
[ "$verdict" = "CLEAN" ] || fail "case1: expected verdict CLEAN, got $verdict"
[ -z "$out" ] || fail "case1: expected silent stdout on clean, got: $out"
[ "$rc" -eq 0 ] || fail "case1: expected exit 0, got $rc"
echo "PASS: case1 clean synchronized repo -> CLEAN"

# ---------------------------------------------------------------------------
# Case 2: dubious ownership / inspection failure -> UNVERIFIED, never DRIFT,
# never CLEAN
# ---------------------------------------------------------------------------
repo2="$WORK/case2-repo"
mkdir -p "$repo2"
_git "$repo2" init -q
echo a > "$repo2/a.txt"
_git "$repo2" add a.txt
_git "$repo2" commit -q -m one
_git "$repo2" update-ref refs/remotes/origin/main HEAD
bare2="$WORK/case2-bare.git"
git clone -q --bare "$repo2" "$bare2"
_git "$repo2" remote add origin "$bare2"

out="$(DRIFT_SERVICES="case2:$repo2:main" \
      DRIFT_HEARTBEAT="$WORK/case2.json" \
      GIT_TEST_ASSUME_DIFFERENT_OWNER=1 \
      bash "$SCRIPT")"
rc=$?
verdict="$(python3 -c "import json;print(json.load(open('$WORK/case2.json'))['verdict'])")"
[ "$verdict" = "UNVERIFIED" ] || fail "case2: expected verdict UNVERIFIED, got $verdict"
case "$out" in
  *"[PERMISSION]"*) : ;;
  *) fail "case2: expected a [PERMISSION] line in the report, got: $out" ;;
esac
case "$out" in
  *"[DRIFT]"*|*"[UNPUSHED]"*|*"[DIRTY]"*) fail "case2: inspection failure must never read as confirmed drift, got: $out" ;;
esac
[ "$rc" -eq 0 ] || fail "case2: expected exit 0 (default, non-strict), got $rc"
# DRIFT_STRICT_EXIT must still separate "did it run" from "what did it find":
# an inspection failure is a finding, so strict mode exits 1 -- but the
# heartbeat verdict must remain UNVERIFIED, not DRIFT.
DRIFT_SERVICES="case2:$repo2:main" \
  DRIFT_HEARTBEAT="$WORK/case2-strict.json" \
  GIT_TEST_ASSUME_DIFFERENT_OWNER=1 \
  DRIFT_STRICT_EXIT=1 \
  bash "$SCRIPT" >/dev/null
rc=$?
[ "$rc" -eq 1 ] || fail "case2: expected DRIFT_STRICT_EXIT to exit 1 on inspection failure, got $rc"
verdict="$(python3 -c "import json;print(json.load(open('$WORK/case2-strict.json'))['verdict'])")"
[ "$verdict" = "UNVERIFIED" ] || fail "case2 strict: expected verdict UNVERIFIED, got $verdict"
echo "PASS: dubious-ownership inspection failure -> UNVERIFIED, distinct from DRIFT"

# ---------------------------------------------------------------------------
# Case 3: real ahead/behind divergence -> DRIFT
# ---------------------------------------------------------------------------
repo3="$WORK/case3-repo"
mkdir -p "$repo3"
_git "$repo3" init -q
echo a > "$repo3/a.txt"
_git "$repo3" add a.txt
_git "$repo3" commit -q -m one
bare3="$WORK/case3-bare.git"
git clone -q --bare "$repo3" "$bare3"
_git "$repo3" remote add origin "$bare3"
# Advance the bare "origin" past local HEAD so the checkout is genuinely
# behind -- a real divergence, not an inspection failure.
clone3="$WORK/case3-clone"
git clone -q "$bare3" "$clone3"
echo b > "$clone3/b.txt"
_git "$clone3" add b.txt
_git "$clone3" commit -q -m two
_git "$clone3" push -q origin HEAD:main

out="$(DRIFT_SERVICES="case3:$repo3:main" \
      DRIFT_HEARTBEAT="$WORK/case3.json" \
      bash "$SCRIPT")"
rc=$?
verdict="$(python3 -c "import json;print(json.load(open('$WORK/case3.json'))['verdict'])")"
[ "$verdict" = "DRIFT" ] || fail "case3: expected verdict DRIFT, got $verdict"
case "$out" in
  *"[DRIFT]"*) : ;;
  *) fail "case3: expected a [DRIFT] line in the report, got: $out" ;;
esac
[ "$rc" -eq 0 ] || fail "case3: expected exit 0 (default, non-strict), got $rc"
echo "PASS: real ahead/behind divergence -> DRIFT"

echo "ALL PASS"
