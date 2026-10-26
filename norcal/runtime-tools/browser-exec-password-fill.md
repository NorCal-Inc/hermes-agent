# browser_exec password fill — no-echo pattern (dummy-secret proof)

Replaces the `browser_run_code_unsafe` + `build-password-loader.js` route for
agents that drive the browser via **`browser_exec`** (Browser Use CLI,
Python on the host — this is how Paris logs into Blogger, not
`browser_run_code_unsafe`'s Playwright-MCP sandbox). `browser_run_code_unsafe`
was ruled out on this card (see kanban `t_f7d75bb7`, replacing `t_7c16c046`):
its VM sandbox has no filesystem, so it cannot read the one-time secret file
at all.

## Why this works

`browser_exec` returns only whatever the submitted Python code prints to
stdout. If the code reads the one-time secret file, hands the value straight
to `fill_input(...)`, and never prints the value itself, the secret never
appears in:

- the tool call arguments (the code string contains only a file *path*, not
  the secret)
- the tool result (only non-secret values — e.g. a field length — are
  printed)
- this session's `state.db` `messages` table (verified empirically below)

## The snippet

```python
# Fill password from one-time file, no echo
import os
p = '<secret file path from credential_relay.py --out>'
try:
    secret = open(p, encoding='utf-8').read()
    fill_input('input[type="password"]', secret)
finally:
    if os.path.exists(p):
        os.remove(p)
length = js("document.querySelector('input[type=\"password\"]').value.length")
exists = os.path.exists(p)
print({'length': length, 'file_exists_after': exists})
```

Run via the `browser_exec` tool, e.g.:

```
browser_exec(code=<snippet above, with the real selector and secret path>)
```

### Usage with `credential_relay.py`

1. Run the existing relay (unchanged — see `credential_relay.py` in this same
   directory, or the shared copy at
   `/home/chris/.hermes/skills/social-content/blogger-text-only/scripts/credential_relay.py`):

   ```bash
   python3 credential_relay.py <item_id> --vault "<Vault Name>" --field password \
     --out /path/to/one-time-secret-file \
     [--company <company_slug>]
   # prints "wrote N chars to ..." and "relay receipt: <out>.relay-receipt.json"
   ```

2. Attach the relay receipt to the kanban task immediately
   (`hermes kanban attach <task_id> <out>.relay-receipt.json`) — it never
   contains the secret.

3. Call `browser_exec` with the snippet above, substituting:
   - `p` = the `--out` path from step 1 (the one-time secret file)
   - the `fill_input` selector = the real password field's selector on the
     login page (e.g. Blogger/Google's sign-in form)

4. `browser_exec` prints only `{'length': N, 'file_exists_after': False}`.
   Confirm `length` matches the expected credential length and the file is
   gone. Never print or log the secret itself.

5. Continue the sign-in flow (click Next/Submit, handle 2FA, etc.) in a
   **separate** `browser_exec` call that does not reference the secret at
   all — keep the secret-handling call minimal and single-purpose.

## Test result (dummy secrets only — never real credentials)

Procedure, run twice on kanban task `t_f7d75bb7` (current session):

1. Generated a fresh, random dummy value inside a Python process and wrote it
   directly to a one-time file plus a temporary witness file. The value was
   never included in a command string, tool argument, or printed output.
2. `browser_exec` opened a `data:` URL with a single
   `<input type="password">`, ran the snippet above, and printed the field's
   value length plus whether the one-time file still existed.
3. Queried this session's `state.db` (`messages` table, both `content` and
   `tool_calls` columns, full session scope) for the unprinted random witness,
   then deleted the witness and temporary directory.

| Run | Result | File deleted after |
|---|---|---|---|
| 1 | `{'length': 27, 'file_exists_after': False}` | yes |
| 2 | `{'length': 27, 'file_exists_after': False}` | yes |

Both runs: length matched the generated dummy's length (27), the one-time file
was gone immediately after, and the final full-session query returned
`{'rows_matching': 0}`. Because the random value was never printed or placed
in a tool argument, the whole-session zero-occurrence criterion was met.

## If `browser_exec` itself ever echoes `fill_input` values

Per the card's stop condition: if a future run finds `fill_input`'s own
return value, a Browser Use CLI log line, or any other `browser_exec`
channel surfacing the filled value verbatim, stop immediately and report the
exact output — do not continue past it or assume current approvals still
hold. No such echo was observed in either test run above (only the
caller-specified `print({'length': ..., 'file_exists_after': ...})` output
came back).

## Scope note

This file documents and proves the mechanism only. No Blogger posting, no
real credential use, and no `config.yaml` change were performed to produce
it. The pinned `blogger-text-only` / `blogger-text-video` skill trees were
not edited — see the proposed Step 1 diff attached to kanban task
`t_f7d75bb7` for the documentation change a human/Christopher can apply when
re-registering those skills.
