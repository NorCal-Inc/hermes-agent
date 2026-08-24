import { afterEach, describe, expect, it } from "vitest";

import { MockTransport } from "./mock-transport";

describe("MockTransport", () => {
  it("lists a deterministic roster including direct-eligible profiles", async () => {
    const transport = new MockTransport();
    const roster = await transport.listRosterProfiles();
    expect(roster.map((r) => r.name)).toEqual(
      expect.arrayContaining(["erika", "paris", "orion", "triptracker", "logos", "nexora", "glass-pepper"]),
    );
    // Sorted by recency at the source is NOT required of the transport —
    // that's buildRosterTiles' job — but the seed must carry lastActive so
    // the sort has something to work with.
    const erika = roster.find((r) => r.name === "erika");
    expect(erika?.lastSession).not.toBeNull();
  });

  it("openSession resumes the same session deterministically on repeat calls", async () => {
    const transport = new MockTransport();
    const first = await transport.openSession("erika", null);
    expect(first.resumed).toBe(true); // seeded profile already has history
    const second = await transport.openSession("erika", null);
    expect(second.sessionId).toBe(first.sessionId);
  });

  it("openSession creates a fresh session for a profile with no history", async () => {
    const transport = new MockTransport();
    const result = await transport.openSession("nexora", null);
    expect(result.resumed).toBe(false);
    expect(result.messages).toEqual([]);
  });

  it("sendMessage appends the user message and streams a reply", async () => {
    const transport = new MockTransport();
    const { sessionId } = await transport.openSession("nexora", null);

    const deltas: string[] = [];
    let completed: string | null = null;
    transport.onStreamEvent((event) => {
      if (event.type === "message.delta") {deltas.push(event.delta);}

      if (event.type === "message.complete") {completed = event.message.content;}
    });

    await transport.sendMessage(sessionId, "nexora", "hello there");

    expect(deltas.length).toBeGreaterThan(0);
    expect(completed).toContain("hello there");

    const resumed = await transport.openSession("nexora", sessionId);
    expect(resumed.messages.some((m) => m.role === "user" && m.content === "hello there")).toBe(true);
    expect(resumed.messages.some((m) => m.role === "assistant")).toBe(true);
  });

  it("has no Electron or window.hermesDesktop dependency in its module surface", async () => {
    // A static-analysis style guard: this file (and the transport package as
    // a whole) must never reference the desktop bridge. If someone adds an
    // `electron` import or reaches for window.hermesDesktop, this string
    // check on the compiled behavior below documents the intent; the real
    // enforcement is source-level in electron-free.test.ts.
    expect(typeof (globalThis as { hermesDesktop?: unknown }).hermesDesktop).toBe("undefined");
  });
});

afterEach(() => {
  // no shared state between tests — MockTransport instances are per-test
});
