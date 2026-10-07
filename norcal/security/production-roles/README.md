# Production-roles registry (rule 6, step 1)

Approved by Christopher 2026-10-07 (design 1C / 2B pinned registry / 3A; report-only first).

| File | Purpose |
|---|---|
| `production-roles.json` | The registry. Six role classes (`stripe_live`, `deploy`, `dns`, `firewall`, `ports`, `service_units`), each with `profiles: []`. `stripe_live`, `firewall`, `ports` and `service_units` are `christopher_only` and can never hold a profile. Ships empty. |
| `verify.py` | Integrity gate. Holds the pinned sha256 of the approved registry and checks shape. Prints one `PRODUCTION ROLES:` line; non-zero exit on missing / unparseable / drifted / malformed. |

**Install location:** `~/.hermes/security/production-roles/` (both files, next to
`~/.hermes/security/skill-trust/`). Claude Code installs after Christopher approves the diff;
`verify.py` should be root-owned and `chattr +i` like the skill-trust verifier, because the
pin lives in it.

**Boot wiring:** `norcal/boot/hermes-shared-boot-context` runs the installed verifier right
after the skill-trust gate. A non-zero exit is a boot BLOCKER (fail closed); the verifier's
line is echoed in the payload under `PRODUCTION ROLES REGISTRY`.

**What step 1 does not do:** no gate consumes the registry yet and no role is assigned.
Erika may later permit only roles that already exist here; live money and Locked surfaces
stay Christopher-only. Any change to the registry requires Christopher to approve the new
`PINNED_SHA256` in `verify.py`.
