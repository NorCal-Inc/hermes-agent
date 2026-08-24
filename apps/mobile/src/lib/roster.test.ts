import { describe, expect, it } from "vitest";

import type { RosterProfile } from "@/transport";

import { buildRosterTiles } from "./roster";

function profile(overrides: Partial<RosterProfile> & { name: string }): RosterProfile {
  return {
    description: "",
    displayName: overrides.name,
    isDefault: false,
    lastSession: null,
    ...overrides,
  };
}

describe("buildRosterTiles", () => {
  it("sorts by most-recently-used first", () => {
    const tiles = buildRosterTiles([
      profile({ lastSession: { id: "a", lastActive: 100, messageCount: 1, preview: "", title: "" }, name: "orion" }),
      profile({
        lastSession: { id: "b", lastActive: 300, messageCount: 1, preview: "", title: "" },
        name: "triptracker",
      }),
      profile({ lastSession: { id: "c", lastActive: 200, messageCount: 1, preview: "", title: "" }, name: "logos" }),
    ]);

    // Erika/Paris synthesize with no session (sort last), so the top three
    // are the real profiles ordered by lastActive descending.
    expect(tiles.slice(0, 3).map((t) => t.name)).toEqual(["triptracker", "logos", "orion"]);
  });

  it("puts profiles with no session history after ones with history", () => {
    const tiles = buildRosterTiles([
      profile({ name: "nexora" }),
      profile({ lastSession: { id: "a", lastActive: 50, messageCount: 1, preview: "", title: "" }, name: "orion" }),
    ]);

    expect(tiles[0].name).toBe("orion");
    expect(tiles.at(-1)?.lastSession).toBeNull();
  });

  it("always includes direct Erika and Paris tiles even when the backend omits them", () => {
    const tiles = buildRosterTiles([profile({ name: "orion" })]);
    const names = tiles.map((t) => t.name);
    expect(names).toContain("erika");
    expect(names).toContain("paris");
    expect(tiles.find((t) => t.name === "erika")?.isDirect).toBe(true);
    expect(tiles.find((t) => t.name === "paris")?.isDirect).toBe(true);
  });

  it("marks a backend-provided erika/paris row as direct too, without duplicating it", () => {
    const tiles = buildRosterTiles([
      profile({ lastSession: { id: "e", lastActive: 10, messageCount: 2, preview: "", title: "" }, name: "erika" }),
    ]);

    const erikaTiles = tiles.filter((t) => t.name === "erika");
    expect(erikaTiles).toHaveLength(1);
    expect(erikaTiles[0].isDirect).toBe(true);
    expect(erikaTiles[0].lastSession?.id).toBe("e");
  });

  it("company tiles (non Erika/Paris) are never flagged direct", () => {
    const tiles = buildRosterTiles([profile({ name: "orion" })]);
    expect(tiles.find((t) => t.name === "orion")?.isDirect).toBe(false);
  });
});
