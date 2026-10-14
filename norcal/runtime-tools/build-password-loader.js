// THE ONE safe JS step that turns a one-time secret file (written by
// credential_relay.py) into a Playwright loader snippet for
// browser_run_code_unsafe.
//
// Safe by construction: the password never touches a shell. It is read
// directly from disk with fs.readFileSync and embedded into the generated
// JS via JSON.stringify(), which correctly escapes every character
// (including `<`, `&`, `*`, quotes, backslashes) for use inside a JS string
// literal. No shell command, `echo`, heredoc, or string-formatted command
// line is involved anywhere in this step.
//
// Usage:
//   node build-password-loader.js <secretPath> <loaderOutPath> [fieldSelector]
//
// <secretPath>     one-time file written by credential_relay.py — this
//                   script deletes it immediately after reading, whether
//                   or not the rest of the script succeeds.
// <loaderOutPath>   where to write the generated loader JS. Run that file
//                   with browser_run_code_unsafe(filename=<loaderOutPath>).
// [fieldSelector]   CSS selector for the password input, default
//                   'input[type="password"]'.
//
// This supersedes/replaces three ad-hoc prior variants:
//   - a stdin-only version (fs.readFileSync(0)) with no file-arg support
//   - a near-duplicate "build-voice-password-loader.js" that only differed
//     by output filename and selector
//   - a local http.server variant that served the credential over
//     127.0.0.1 for the browser to fetch — unnecessary extra attack
//     surface and a second process to manage for no benefit over reading
//     a file directly.
// Use THIS script (with the field selector argument) for every login case —
// primary account password, Google Voice recovery password, etc. — instead
// of writing a new per-case variant.

const fs = require('fs');

const [secretPath, loaderOutPath, fieldSelector] = process.argv.slice(2);

if (!secretPath || !loaderOutPath) {
  console.error('usage: node build-password-loader.js <secretPath> <loaderOutPath> [fieldSelector]');
  process.exit(1);
}

let password;
try {
  password = fs.readFileSync(secretPath, 'utf8');
} finally {
  // Always remove the one-time secret file, success or failure.
  fs.rmSync(secretPath, { force: true });
}

if (!password) {
  console.error('empty secret read from ' + secretPath);
  process.exit(2);
}

const selector = fieldSelector || 'input[type="password"]';

// JSON.stringify() is the safe embedding step: it escapes every character
// that would otherwise matter to a JS string literal. This is the ONLY
// place the secret is turned into text that gets written anywhere, and it
// never passes through a shell.
const code = `async (page) => {
  const password = ${JSON.stringify(password)};
  await page.locator(${JSON.stringify(selector)}).fill(password);
  return 'password-filled';
}`;

fs.writeFileSync(loaderOutPath, code, { mode: 0o600 });
console.log(`wrote loader (${password.length} char secret) to ${loaderOutPath}`);
