**Protected File Change Request: ~/.hermes/config.yaml**

**Status:** Requires manual approval (file is protected to prevent accidental credential/config drift). Do not apply via automated tools. Christopher or authorized operator must review and apply via editor or hermes config commands.

**Exact current lines with scattered dated model IDs (violates central alias policy):**
1. Line 2:
   default: grok-4.20-reasoning
2. Line 297 (delegation):
   model: grok-4.20-reasoning
3. Line 443 (x_search):
   model: grok-4.20-reasoning
4. Line 484 (routing.engineering):
   model: grok-build-0.1
5. Lines 533, 535 (fallback):
   grok-build-0.1: deepseek then gpt-5.4-full
   deepseek: grok-build-0.1 retry

(Note: grok-build-0.1 matches alias target but key should use alias name per "aliases only" rule.)

**Exact replacement lines (use aliases from ~/.hermes/provider-aliases.yaml):**
1. Line 2:
   default: grok_reasoning_primary
2. Line 297:
   model: grok_reasoning_primary
3. Line 443:
   model: grok_reasoning_primary
4. Line 484:
   model: grok_builder
5. Lines 533, 535:
   grok_builder: deepseek then gpt-5.4-full
   deepseek: grok_builder retry

**Reason for change:**
- Prevents recurrence of model-routing failure by making ~/.hermes/provider-aliases.yaml the single source of truth.
- Eliminates hardcoded deprecated/invalid IDs (xai/grok-4.20-reasoning and dated variants).
- Aligns with HERMES DIRECTIVE, credential_architecture.md, and incident doc.
- Ensures routing code, delegation, x_search, engineering fallback, and defaults all resolve via central aliases (grok_reasoning_primary → grok-4.20-0309-reasoning, grok_builder → grok-build-0.1).
- Protected file guard triggered; manual patch required to maintain governance.

**Validation command (run after patch):**
~/.hermes/bootstrap/check-credentials.sh
# Expected: all [OK], model references resolved via alias, final GROK_OK

**Post-patch steps:**
- Update incident doc to note manual approval.
- Rerun full ladder check.
- git commit any related runtime/ updates if aliases.yaml changed.
- Push to NorCal-Inc/hermes-agent.

Approve and apply only if aliases.yaml is synced and no credential exposure risk.
