# North Caledonia shared boot contract

Canonical startup source for the three governed operating entry paths:

- GPT/Codex: `~/.local/bin/codex` refreshes `~/.codex/norcal-boot-current.md`; `$CODEX_HOME/AGENTS.md` also carries the static constraints as a fail-safe prefix.
- Claude Code: the SessionStart hook resolves to the tracked `claude-session-start-gate.py`, which calls the tracked `claude-boot-context` and shared generator.
- Hermes/Erika: CLI and gateway sessions call `~/.local/bin/hermes-shared-boot-context`, which is a symlink to the tracked generator here. GPT models selected inside Hermes therefore receive the same shared startup context.

`boot-constraints.md` is the single canonical constraint block. Every generated boot record must start with its exact contents. `verify.py` checks deployed symlinks, Codex configuration, and shared-generator prefix parity.

The native ChatGPT consumer application is not controlled by this VPS and cannot be given a VPS SessionStart hook. The mechanically governed GPT path is GPT/Codex on this host or GPT models routed through Hermes. Any future native-GPT integration must consume this same canonical file and pass the same prefix/hash check before it is considered a governed North Caledonia entry point.
