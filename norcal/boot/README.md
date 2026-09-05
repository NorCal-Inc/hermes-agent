# North Caledonia shared boot contract

Canonical startup source for the three governed operating entry paths:

- GPT/Codex: `~/.local/bin/codex` refreshes `~/.codex/norcal-boot-current.md`; `$CODEX_HOME/AGENTS.md` also carries the static constraints as a fail-safe prefix.
- Claude Code: the SessionStart hook resolves to the tracked `claude-session-start-gate.py`, which calls the tracked `claude-boot-context` and shared generator.
- Hermes/Erika: CLI and gateway sessions build their fresh-session prompt through the shared chokepoint `hermes_cli/norcal_boot.py`, which calls `~/.local/bin/hermes-shared-boot-context` (a symlink to the tracked generator here) and classifies the result with the same `recovery_boot.shared_boot_complete` exact-line parser the Claude and Codex wrappers use. GPT models selected inside Hermes therefore receive the same shared startup context.

A zero exit code from the generator is never sufficient evidence of a passed gate: it exits 0 on a degraded boot unless invoked with `--gate-exit-code`. Every consumer must parse the canonical `BOOT STATUS: COMPLETE` state line. Erika's two session-creation paths did not, from the introduction of the injection block until 2026-09-03: they injected the generator's payload verbatim (so the degraded state was visible *to the model*) while the runtime itself judged the gate by exit code alone, and so held no machine-readable boot status at all. Claude and Codex failed closed mechanically on the identical payload.

`boot-constraints.md` is the single canonical constraint block. Every generated boot record must start with its exact contents. `verify.py` checks deployed symlinks, Codex configuration, shared-generator prefix parity, and that all three entry paths — including both Hermes/Erika paths — evaluate the gate through the canonical parser.

The native ChatGPT consumer application is not controlled by this VPS and cannot be given a VPS SessionStart hook. The mechanically governed GPT path is GPT/Codex on this host or GPT models routed through Hermes. Any future native-GPT integration must consume this same canonical file and pass the same prefix/hash check before it is considered a governed North Caledonia entry point.
