#!/usr/bin/env python3
"""
THE ONE safe way to move a 1Password secret (e.g. a Blogger/Google login
password) into a one-time file a Playwright loader can read.

Why this exists (2026-10-05, root-caused after kanban task t_73c841b3 got a
spurious "Wrong password" from Google): earlier ad-hoc relay variants moved
the secret through a SHELL STRING at some point in the chain — e.g.
building a command like `f"echo {password} > path"` or `f"PW={password}"`
and running it via `terminal()`/`subprocess.run(..., shell=True)`. Any
shell-special character in the password silently corrupts it that way:
  - `&`           backgrounds the rest of the line — the shell runs
                  everything after the `&` as a SEPARATE command/job and the
                  effective value is silently truncated at the `&`.
  - `*`           glob-expands in that now-backgrounded fragment.
  - `<`/`>`/`|`/`"`/`'`/backtick etc. — redirection, piping, quoting changes.
None of this raises an error. The password that reaches the browser is
simply wrong, and Google's "Wrong password" response gives no hint that the
credential itself was fine and a shell ate part of it.

This script never builds a shell command string and never passes a
credential through `shell=True`. It only uses:
  (a) `subprocess.run([...])` with a plain **argument list** to call `op`
      (optionally wrapped by `hermes-run --company <x> --`, also as list
      items, not string-interpolated), and
  (b) a direct file write via Python's own `open()`/`os.fdopen()` — never a
      shell redirect, heredoc, or `echo`.

The secret is written byte-for-byte (after stripping 1Password's own
trailing newline, if any) to --out with mode 0600. The caller is expected to
be a one-time JS loader (see build-password-loader.js in this same
directory) that reads the file exactly once and deletes it immediately.

Evidence receipt (added 2026-10-05, kanban t_6e3c94d9): every run that gets
as far as calling `op` also writes a small NON-SECRET JSON receipt to
`<out>.relay-receipt.json` (override with --receipt). It records item id,
vault, company, field, exit code, chars written, timestamp and this script's
own sha256 — never the secret. The `wrote N chars` stdout line used to be the
only trace of a clean relay and it died with the worker's terminal, so the
independent verifier could never confirm the password moved only through
this script. Attach the receipt to the kanban task right after the relay
call (`hermes kanban attach <task_id> <receipt>`). The secret file itself is
deleted by build-password-loader.js; the receipt is what survives.

Usage:
    python3 credential_relay.py <item_id> --vault "<Vault Name>" \\
        [--field password] --out /path/to/one-time-secret-file \\
        [--company <company_slug>] [--receipt /path/to/receipt.json]

Exit codes: 0 ok, 2 field not found, non-zero from `op`/`hermes-run` on
their own failure (propagated via subprocess.run(check=True)).
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys


def write_receipt(args, exit_code: int, chars_written: int) -> None:
    """Write the non-secret relay receipt. Never contains the secret value."""
    path = args.receipt or f"{args.out}.relay-receipt.json"
    with open(__file__, "rb") as fh:
        script_sha256 = hashlib.sha256(fh.read()).hexdigest()
    receipt = {
        "item_id": args.item_id,
        "vault": args.vault,
        "company": args.company,
        "field": args.field,
        "exit_code": exit_code,
        "chars_written": chars_written,
        "secret_file": args.out,
        "secret_file_mode": "0600" if exit_code == 0 else None,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "relay_script": os.path.abspath(__file__),
        "relay_script_sha256": script_sha256,
        "mechanism": "subprocess argv list (no shell=True) + os.open/os.fdopen direct write",
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as fh:
        json.dump(receipt, fh, indent=2)
        fh.write("\n")
    print(f"relay receipt: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id", help="1Password item id or name")
    parser.add_argument("--vault", required=True, help='e.g. "4. Orion Formation Services (Digital Products)"')
    parser.add_argument("--field", default="password", help="field label/id to extract (default: password)")
    parser.add_argument("--out", required=True, help="path to write the raw secret to (one-time file)")
    parser.add_argument("--company", default=None, help="if set, run op through `hermes-run --company <x> --`")
    parser.add_argument("--receipt", default=None, help="non-secret evidence receipt path (default: <out>.relay-receipt.json)")
    args = parser.parse_args()

    # SAFE (a): argument list only. Never build this as a formatted string
    # and never pass shell=True — that is the exact bug this script exists
    # to eliminate.
    op_cmd = [
        "op", "item", "get", args.item_id,
        "--vault", args.vault,
        "--format", "json",
        "--reveal",
    ]
    cmd = ["hermes-run", "--company", args.company, "--", *op_cmd] if args.company else op_cmd

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or f"op exited {result.returncode}\n")
        write_receipt(args, result.returncode or 1, 0)
        return result.returncode or 1

    item = json.loads(result.stdout)
    secret = None
    for f in item.get("fields", []):
        if f.get("label") == args.field or f.get("id") == args.field:
            secret = f.get("value", "")
            break
    if secret is None:
        sys.stderr.write(f"ERROR: field '{args.field}' not found on item {args.item_id}\n")
        write_receipt(args, 2, 0)
        return 2

    secret = secret.rstrip("\n")

    # SAFE (b): direct file write via Python's own open(), never a shell
    # echo/printf/heredoc. Mode 0600 set atomically at creation via os.open.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret)

    print(f"wrote {len(secret)} chars to {args.out}")
    write_receipt(args, 0, len(secret))
    return 0


if __name__ == "__main__":
    sys.exit(main())
