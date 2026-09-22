# Upstream Nous fix triage — 2026-09-21

Card `t_82336f3d` (child of `t_73c7c51a`). Branch
`upstream-intel/2026-09-21-relevant-fixes`, based on `main @ c4d6605e29`.

Seven upstream `NousResearch/hermes-agent` commits were reviewed against this
fork. Each was read in full (`git show <sha>`), the corresponding NorCal code
was read, and the verdict below is per-commit. **No blind merge; no full-history
cherry-pick.**

## Divergence context — why most of these cannot be cherry-picked

| | |
|---|---|
| merge-base with upstream | `f82f2dbabd` — 2026-08-19 |
| upstream commits since | 15,896 |
| NorCal commits since | 165 |

Six of the seven commits touch files that **do not exist in this fork**, because
upstream split its god-files after our merge-base:

| upstream path | here |
|---|---|
| `tools/mcp_tool_lifecycle.py` | absent (`tools/mcp_tool.py`) |
| `hermes_cli/kanban_db_dispatch.py` | absent (`hermes_cli/kanban_db.py`) |
| `gateway/run_adapters.py`, `run_goals.py`, `run_notifications.py`, `run_turn.py`, `run_profile_reconcile.py` | absent (all still in `gateway/run.py`) |
| `tui_gateway/launch_profile_policy.py`, `tui_gateway/session_reaper.py` | absent |
| `cron/scheduler_ownership.py` | absent |
| `hermes_cli/update_cmd_fleet.py`, `update_cmd_stale_survivors.py` | absent |

So every verdict below is about whether the **defect** exists here, not whether
the patch applies textually.

## Live-configuration fact that bears on several verdicts

`gateway.multiplex_profiles` is **unset** in `~/.hermes/config.yaml`, and
`set_multiplex_active()` has exactly one production call site
(`gateway/run.py:6731`), driven by that key. 32 profiles exist under
`~/.hermes/profiles/`, but the gateway does not multiplex them today.

Consequence: **all three adopted changes are behaviour-neutral on the current
configuration.** They close holes that open the moment multiplexing is turned
on — which 32 company/role lanes make a plausible future — and add one log line
on a path that today only says `pass`.

---

## Verdicts

### 1. `af380f66ae` — mcp: divide the shutdown budget across profile passes — **PARTIALLY ADOPTED**

Commit `f9dde66e81`.

Three separable changes upstream:

* **Budget division across per-profile passes — N/A.** Our
  `tools/mcp_tool.py::shutdown_mcp_servers()` takes no arguments at all: no
  `scope`, no `names`, no `timeout`. There are no per-profile passes to starve.
* **`copy_context()` → `Context()` teardown poisoning — N/A.** That bug needs
  the off-loop `mcp-shutdown` worker thread upstream introduced in
  `c718267ef3`. Our fork calls `shutdown_mcp_servers()` inline.
* **`except Exception: pass` → logged WARNING — APPLIES, adopted.** Both gateway
  exit paths (`gateway/run.py`, the aborted-startup return and the shutdown
  tail) silently swallowed teardown failures, so a raise left every MCP
  connection and the shared background loop up while the gateway reported a
  clean exit, with nothing in the log. Upstream lost a real `TypeError` to this
  exact `pass`.

Both sites now go through one seam,
`_shutdown_mcp_servers_reporting_failures()`, which logs a WARNING with the
traceback and returns whether teardown completed. Failures stay non-fatal.

### 2. `bc0a42fd96` — kanban: scrub the dispatcher's credentials from another profile's worker — **REJECTED (architecture absent)**

The commit narrows a gate — `scrub_secrets=is_multiplex_active()` becomes
`is_multiplex_active() or _is_routed_home(profile_home)` — inside upstream's
`hermes_cli/kanban_db_dispatch.py`.

None of that machinery exists here. This fork's `_default_spawn`
(`hermes_cli/kanban_db.py:23396`) predates the whole mechanism: at the
merge-base upstream's `_default_spawn` was also a bare `env = dict(os.environ)`,
and `build_subprocess_env` / `strip_launch_profile_env` / `_is_routed_home` /
`_worker_profile_scope` / `served_profile_child_env` all arrived upstream
afterwards. There is no gate here to narrow, because there is no scrub.

Implementing the scrub chain would be a feature port of a month of upstream
evolution into the live NorCal kanban dispatcher, not a fix cherry-pick — see
**Finding A** below, raised for a governed decision rather than executed here.

### 3. `8863b36fd6` — cron: stale-code yield reads as an outage in `cron status` — **REJECTED (precondition absent)**

The defect requires a cron ticker that yields on code skew and persists a
`CronTickYielded` marker. This fork has **no `CronTickYielded`** and no
stale-code yield in cron at all: `gateway/code_skew.py` exists but its only
consumer is the `/model` switch guard in `gateway/slash_commands.py:73`. A
`hermes update` under a running gateway does not silently stop cron dispatch
here, so there is no outage for `cron status` to misreport.

The second half needs `hermes_cli/update_cmd_fleet.py` and
`update_cmd_stale_survivors.py` — neither exists (`hermes_cli/update_cmd.py`,
`update_lock.py` only).

### 4. `cb647a018f` — cron: one host ticker owns every profile's cron, per profile — **REJECTED (feature port; one bullet deferred, see Finding B)**

Four bullets, assessed separately against this fork:

* **`scheduler_ownership` ownership predicates — N/A.** The module does not
  exist here; `_should_yield_tick_to_fresh_gateway` in its upstream form is not
  present either.
* **`(home key, job id)` in-flight keying — DOES NOT APPLY.** Upstream's stated
  motivation is "two profiles carrying a `daily-brief` no longer read as one
  job". Verified here: cron job ids are always `uuid.uuid4().hex[:12]`
  (`cron/jobs.py:1875`) and `add_job()` exposes **no caller-supplied-id
  parameter**, so two profiles cannot share an id through any API and the
  bare-id set in `cron/scheduler.py` cannot collide.

  *Precision note (added on re-verification):* there is exactly one path by
  which an id can enter from outside `add_job()` —
  `_get_due_jobs_locked()` (`cron/jobs.py:3056`) repairs an id-less record by
  recovering a drifted legacy `"job_id"` key, else synthesizing a uuid. That is
  a corruption-repair path for a hand-edited `jobs.json`, not an API, and a
  collision would need a human to write the *same* literal id into two
  different profiles' job stores **and** multiplexing to be enabled. The
  verdict is unchanged; the original wording ("no caller-supplied-id path") was
  a shade absolute and is corrected here rather than left to be discovered by
  the reviewer.

  Re-keying 45 call sites across `cron/scheduler.py`,
  `cron/jobs.py`, `gateway/run.py`, `agent/monitoring/cron_health.py`,
  `tools/cronjob_tools.py` and ~15 test files would buy nothing here.
* **Per-home parallel pool — real but currently inert.** See **Finding B**.
* **Ungating the cron tick set from `gateway.multiplex_profiles` — REJECTED ON
  ISOLATION GROUNDS.** Upstream's premise is "ONE gateway process per host
  multiplexes every profile". Making the launch gateway tick every profile's
  cron store means a company lane's scheduled job fires in the launch process
  and, absent per-profile adapters, delivers through the primary's routed
  adapters or fails closed. Upstream itself had to add a stand-down gate
  (`_cron_profile_gate`, in `9ecd22e5da`) the same day for exactly this. Taking
  the ungating without that gate would weaken lane separation; taking both is
  the full feature port. Neither is a defect fix against this tree.

### 5. `9ecd22e5da` — cron: release a profile's in-flight claim under the key it registered with — **REJECTED (fixes a bug introduced by #4)**

The leak is: `_submit_with_guard` registers the claim under the ticker thread's
per-profile cron scope, while the pool worker's `finally` runs outside
`ctx.run` and so resolves the LAUNCH home — the discard misses the real key.

That failure mode requires the home-keyed in-flight state `cb647a018f`
introduced. Our `release_running_job(job_id)` (`cron/scheduler.py:679`) takes a
bare id and discards from a bare-id set; there is one key space, so a release
cannot miss. The companion changes (`is_job_running(home=…)`,
`register_ticked_homes` pool reaping, `_inflight_home_path`) are all accessors
on machinery that does not exist here.

### 6. `c718267ef3` — multiplex: close the profile-scope holes the scope machinery misses — **REJECTED (architecture absent + relaxes a control we currently hold)**

Six sub-fixes across `run_adapters.py`, `run_goals.py`, `run_notifications.py`,
`run_turn.py`, `run_profile_reconcile.py`, `session_reaper.py`,
`kanban_db_dispatch.py`, `web_server.py` — of which only `web_server.py` exists
here, and its hunk depends on `tui_gateway/launch_profile_policy.py`, which
does not.

Independent of the missing files, the commit's central new primitive —
`launch_profile_scope_if_multiplexed()` replacing a bare `nullcontext()` for
"no routed profile" — is a **relaxation**: it makes an unbound body run on the
launch profile's credentials instead of failing closed. Upstream's own
`4aa9baf139` (below) opens with the consequence: *"a silent cross-tenant
credential fallback **where origin/main raised**."*

Our `agent/secret_scope.py::get_secret` currently raises `UnscopedSecretError`
for an unscoped read under multiplexing — the origin/main behaviour. Adopting
this commit would trade that for a fallback. Rejected: the card forbids letting
an upstream cherry-pick weaken a NorCal isolation control, and
`data-isolation.md` defaults cross-lane uncertainty to FORBIDDEN.

### 7. `4aa9baf139` — multiplex: a profile whose home does not resolve must not borrow the launch profile's secrets — **REJECTED as a cherry-pick; PRINCIPLE ADOPTED in two places**

As written, this commit repairs the hole `c718267ef3` opened in
`gateway/run_adapters.py`. We did not take `c718267ef3`, and
`run_adapters.py` / `_profile_home_or_none` / `_scope_or_null` do not exist
here, so the patch itself is inapplicable.

Its principle — *never conflate "this named profile's home does not resolve"
with "this is launch-owned", and never let the first borrow the launch
profile's values* — has two live instances in this fork, both adopted:

**7a. `gateway/authz_mixin.py::_auth_env` — commit `ea10ac9e3d`.**
`_auth_env` preferred `get_secret(name)` and then fell through to
`os.getenv(name)` on **both** a scoped miss and an `UnscopedSecretError`
(swallowed by a bare `except Exception`) — precisely the
`except UnscopedSecretError: val = os.getenv(...)` shape `AGENTS.md` bans. Under
multiplexing `os.environ` holds the LAUNCH profile's values, so a secondary
profile inherited:

* fail-CLOSED: another lane's `TELEGRAM_ALLOWED_USERS` / `QQ_ALLOWED_USERS`
  silently replacing profile B's allowlist;
* fail-OPEN: `GATEWAY_ALLOW_ALL_USERS=true` or `<PLATFORM>_ALLOW_ALL_USERS=true`
  set for the launch profile **admitting every sender on profile B's own bot**.

`_platform_gate_env`, defined immediately below it, is the already-correct twin
(#72348); `_auth_env` is the un-hardened one. It now delegates to it.

This fork's own `tests/gateway/test_qqbot_scope_paths.py` carried a
`strict=True` xfail naming this exact gap and predicting the fix — it is now a
passing assertion.

Upstream fixed this same defect in **`2912c36aa4`** (2026-08-23, *"fix(gateway):
stop multiplex allowlist leak…"*), which is **not one of the seven commits on
the card**. It was adopted as the fork-applicable form of `4aa9baf139`'s stated
principle, and is called out here so the reviewer can accept or revert that one
commit specifically. Only its `gateway/authz_mixin.py` hunk was taken; its
`tools/bot_relay.py` half is an unrelated shell-injection fix and was
deliberately left alone.

**7b. `gateway/run.py::_make_profile_platform_event_handler` — commit `bc12fc7048`.**
`get_profile_dir()` raising collapsed into the same `None` the launch-owned path
uses, silently, and the handler then ran with no profile scope — so
`_is_user_authorized` decided profile B's traffic using the LAUNCH profile's
allowlist and allow-all flag, then dispatched the result to plugin hooks. The
factory is only ever called for a named secondary profile, so the failure is
unambiguous. It now logs a WARNING (it was completely silent) and drops the
event.

**Deliberate deviation from upstream, flagged for review:** upstream binds
nothing but still dispatches, relying on a fail-closed credential read. Our
authorization gate falls back to `os.getenv` when no scope is installed, so
"dispatch unscoped" would still consult the launch profile's allowlist.
Dropping is the fail-closed reading; the only thing lost is an observability
hook for a profile whose home no longer exists.

**Repair after failed verification — commit `b09a62bb80`.** Two independent
verifications (`t_49b0a2c6`, `t_51624b71`) returned FAIL on the same defect in
`bc12fc7048`, and both were right: the check rejected only `get_profile_dir()`
*raising*, and `get_profile_dir()` does not raise for the case the docstring
names. It is pure path arithmetic — `hermes_cli/profiles.py:374` normalizes the
name and joins it onto the profiles root — so a profile deleted or renamed
after startup resolved cleanly to a **nonexistent** `Path`, which the handler
then treated as a good home: `_profile_runtime_scope()` pointed `HERMES_HOME`
at nothing, installed an empty secret scope, and the event was dispatched
rather than dropped.

Fixed in two places, because the factory runs **once** per adapter install and
`profiles_to_serve()` only yields directories that exist at startup — so the
realistic failure is a home vanishing while the adapter is up, not one missing
at install:

* factory time — a resolved home that is not a directory takes the same
  warned, drop-everything path as a raise;
* per event — the cached home is re-checked before entering the scope; warns
  once, then drops quietly.

The verifiers' second finding — that the regression was synthetic (it
manufactured a `FileNotFoundError` that real code never raises) — is also
fixed. Two new tests use the **real** `get_profile_dir()` against a temp
`HERMES_HOME`: one for a name with no directory behind it, one for a home
removed between two events. Both assert `_profile_runtime_scope` is never
entered, not merely that dispatch was skipped. The synthetic-raise case is
kept as well, since an invalid name genuinely does raise.

`test_secondary_handler_stamps_profile_before_dispatch` stubbed a
`/profiles/work` path that does not exist and would now take the drop path; it
creates the directory instead, so it still tests stamping.

---

## Findings raised, NOT fixed (outside this card's mutation scope)

Per the Task-Scope Gate, an observed defect is not self-authorization to fix it.
All three are recorded for a governed decision by Erika / Christopher.
(Finding C was added during the post-verification repair.)

### Finding A — a kanban worker is built from the dispatcher's entire environment

`hermes_cli/kanban_db.py::_default_spawn` builds the worker env as
`env = dict(os.environ)` (line 23423) and hands it straight to
`subprocess.Popen(env=env)`. The only removals are `gateway.session_context`
routing keys and `HERMES_TUI`; `HERMES_HOME` is then repointed at the assignee's
profile. **There is no credential scrub on this path at all.**

Consequence: a worker spawned for any of the 32 profiles receives whatever the
dispatcher process carries — and the dispatcher runs inside the gateway
(`kanban.dispatch_in_gateway`), so that is the launch gateway's environment,
including anything systemd injects. Workers for different company lanes are
built from one identical environment, differing only in `HERMES_HOME`.

This is the exposure `bc0a42fd96` and its predecessors close upstream. It is a
`data-isolation.md` / `sovereignty.md` question (do company lanes share the
dispatcher's process credentials?) before it is a code question, and the fix —
adopting `build_subprocess_env(scrub_secrets=…)` on the spawn path — would
change what every kanban worker on this host can see. Recommend a governed
decision; do not let an executor make it silently.

*No claim is made here about which specific credentials are in that environment
— the mechanism was read, the values were not.*

### Finding B — the cron parallel pool is process-global, sized by whichever profile ticks first

`cron/scheduler.py::_get_parallel_pool` keys nothing by home: when the requested
`max_workers` differs from the cached one it shuts the existing pool down
(`wait=False`) and builds a new one. Under `cron/scheduler_provider.py::
_start_multiplex`, which ticks each profile home in turn inside one process,
two served profiles with different `cron.max_parallel_jobs` would tear each
other's pool down every cycle. This is `cb647a018f`'s third bullet.

**Currently inert**: `gateway.multiplex_profiles` is unset, so only one home is
ever ticked, and `cron.max_parallel_jobs` is not set per-profile. Deferred
rather than ported because the change rewrites live cron globals and ~12 test
setups to fix something that cannot fire on this configuration — and would be
speculative infrastructure until multiplexing is enabled. If multiplexing is
ever turned on, this should be ported in the same change.

### Finding C — the *message* handler has the same unresolvable-home defect, and fails OPEN

Found while repairing `_make_profile_platform_event_handler`; raised, not
fixed, because the safe behaviour is a governance call rather than a
mechanical one.

`gateway/run.py::_make_profile_message_handler` (line 15480) resolves a named
secondary profile's home with the identical un-checked call —
`get_profile_dir(profile_name)` inside a bare `try`, `None` on raise, no
existence check — and then, when it has no home:

```python
            if profile_home is not None:
                with _profile_runtime_scope(profile_home):
                    return await self._handle_message(event)
            return await self._handle_message(event)   # ← unscoped
```

It runs the message path **unscoped**, which is the behaviour
`bc12fc7048` removed from the event path: `_handle_message` authorizes before
the agent-turn scope is installed, so profile B's inbound traffic is admitted
or denied by the LAUNCH profile's allowlist and allow-all flag. This is the
same defect on the more consequential path — real inbound messages, not
observer events — and it is silent (no warning at all).

Not fixed here for two reasons. (1) Scope: the card authorizes adapting the
seven upstream commits, and the event-path instance is the one under
verification; a defect observed is not self-authorization. (2) The remedy is a
real product decision — dropping a lane's *messages* when its home vanishes is
visible to users in a way dropping an observer event is not, and the third
option the tree already uses elsewhere is a documented fallback:
`_resolve_profile_home_for_source` (line 27956) checks `profile_exists(name)`
and falls back to `get_hermes_home()` **with a warning**. Three defensible
behaviours, one choice, and it belongs to Erika / Christopher.

Recommendation: at minimum make it loud (it is currently silent), and align it
with whichever of drop / documented-fallback is chosen for the event path.

---

## Tests run

**Runner: full CI parity.** This worktree has no `.venv`/`venv` of its own, so
`scripts/run_tests.sh` falls through its probe list. The sanctioned override
supplies the interpreter instead — the fork checkout's own venv (pytest 9.1.1,
all runtime deps present):

```
HERMES_PYTHON=/home/chris/.hermes/hermes-agent-next/venv/bin/python \
  scripts/run_tests.sh <paths>
```

That is the real runner: per-file subprocess isolation via
`run_tests_parallel.py`, credential-var unset sweep, `TZ=UTC`, `LANG=C.UTF-8`,
`PYTHONHASHSEED=0`. An earlier revision of this file recorded a scratch-venv run
and flagged the parity gap as outstanding — **that gap is now closed**; the
results below are from the CI-parity runner.

### Targeted subset — 15 files, 145 passed, 0 failed, 2 skipped (7.4s)

```
tests/agent/test_secret_scope_tier1_migration.py
tests/gateway/test_qqbot_scope_paths.py
tests/gateway/test_multiplex_profile_authz.py
tests/gateway/test_multiplex_credential_isolation.py
tests/gateway/test_unauthorized_dm_behavior.py
tests/gateway/test_pairing_allowlist_bypass.py
tests/gateway/test_allowlist_startup_check.py
tests/gateway/test_relay_upstream_authz.py
tests/gateway/test_gateway_platform_event_hook.py
tests/gateway/test_gateway_shutdown.py
tests/gateway/test_clean_shutdown_marker.py
tests/gateway/test_shutdown_flush.py
tests/gateway/test_shutdown_forensics.py
tests/gateway/test_shutdown_cache_cleanup.py
tests/gateway/test_shutdown_watchdog.py
```

### Regression sweep — `tests/gateway/` + `tests/cron/`

Run in full because `gateway/run.py` is one of the two files this branch
touches: **716 files, 6675 passed, 1 failed, 36 skipped (235.6s)**.

The single failure is
`tests/gateway/test_reasoning_command.py::TestReasoningCommand::test_run_agent_includes_enabled_mcp_servers_in_gateway_toolsets`
(`assert 'web' in {'executive'}`). **Pre-existing, not caused by this branch** —
confirmed by running that file in a throwaway worktree detached at the base
commit `c4d6605e29`, where it fails identically (1 failed, 7 passed). It is a
NorCal toolset-customization drift unrelated to this card's scope, and is left
untouched and reported rather than fixed here.

### Re-run after the verification repair (`b09a62bb80`)

The repair touches `gateway/run.py` and one gateway test file, so the whole
`tests/gateway/` tree was re-run rather than a subset:

* `tests/gateway/` — **651 files, 5872 passed, 1 failed, 35 skipped (244.9s)**.
  The one failure is the same pre-existing
  `test_reasoning_command.py::…::test_run_agent_includes_enabled_mcp_servers_in_gateway_toolsets`
  (`assert 'web' in {'executive'}`), re-confirmed on this run at the base
  commit `c4d6605e29` in a throwaway worktree: **7 passed, 1 failed**, the same
  test. Not caused by this branch; NorCal toolset drift, left untouched.
* `tests/gateway/test_gateway_platform_event_hook.py` alone — 37 passed.
* `tests/agent/` — **403 files, 4818 passed, 0 failed, 27 skipped (161.2s)**.
  Run as a directory (it contains the tier-1 scope-migration file the earlier
  subset named individually); green.

Both new tests were run against the pre-repair commit `fa20d3b1c5` (that exact
tree, with only the new test file copied in): **2 failed, 35 passed**.
`test_missing_profile_home_drops_the_event_under_real_resolution` and
`test_profile_home_deleted_after_install_drops_the_event` both fail with the
handler having dispatched (`assert <AsyncMock …> is None`) — i.e. the missing
home was scoped and the event delivered.

### Each new test was run against the pre-fix code to confirm it fails there

* `TestAuthzAuthEnv::test_scoped_miss_returns_default_not_env` — fails with
  `assert 'profile-A' == ''`, i.e. the launch profile's allowlist reaching a
  scoped read.
* `test_unresolvable_profile_home_drops_the_event` — fails with *"an
  unresolvable profile home must be reported, not silently ignored"*.
* `TestMcpShutdownFailureIsReported` — fails on base with `AttributeError`
  (the reporting seam does not exist); on base both call sites are literally
  `except Exception: pass`, visible in the commit diff.
