import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SRC_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");

function walk(dir: string): string[] {
  const out: string[] = [];

  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);

    if (stat.isDirectory()) {out.push(...walk(full));}
    else if (/\.(ts|tsx)$/.test(entry) && !entry.endsWith(".test.ts") && !entry.endsWith(".test.tsx")) {
      out.push(full);
    }
  }

  return out;
}

/**
 * ARCHITECTURE CONSTRAINT: "Do not port Electron itself and do not depend
 * on window.hermesDesktop for the mobile app." / "No SSH in the mobile
 * client." Enforced here as a source-level guard so a future contribution
 * can't quietly reintroduce either dependency into apps/mobile/src.
 */
describe("apps/mobile has no Electron / hermesDesktop / SSH dependency", () => {
  const files = walk(SRC_DIR);

  it("scanned at least the expected number of source files", () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it.each(files)("%s does not import electron or reference hermesDesktop/ssh", (file) => {
    const content = readFileSync(file, "utf-8");
    expect(content).not.toMatch(/from\s+["']electron["']/);
    expect(content).not.toMatch(/require\(["']electron["']\)/);
    expect(content).not.toMatch(/window\.hermesDesktop/);
    // Guards actual SSH usage (module imports, protocol URLs, child_process
    // spawns) — not prose that legitimately explains "no SSH in this file"
    // (see gateway-transport.ts's doc comment).
    expect(content).not.toMatch(/from\s+["']ssh2["']|require\(["']ssh2["']\)|ssh:\/\/|child_process/);
  });
});
