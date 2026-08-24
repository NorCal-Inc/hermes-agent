import type { RosterProfile } from "@/transport";

/** Roster tile ordering + Erika/Paris-presence contract, isolated from the
 *  transport so it's unit-testable without a mock/gateway round trip. */
export interface RosterTile extends RosterProfile {
  /** True for the two always-present direct tiles (Erika, Paris). */
  isDirect: boolean;
}

const DIRECT_NAMES = ["erika", "paris"] as const;

const DIRECT_LABELS: Record<(typeof DIRECT_NAMES)[number], string> = {
  erika: "Erika",
  paris: "Paris",
};

/**
 * Builds the two-column picker roster: every backend-reported profile,
 * plus synthesized Erika/Paris tiles when the backend roster omits them
 * (e.g. a mock/partial backend, or a registry that doesn't enumerate the
 * orchestrator/coding-agent profiles as ordinary profiles). Sorted by most
 * recently used first; profiles with no session history sort after any
 * with history, alphabetically among themselves so the list is stable
 * instead of reshuffling on every render.
 */
export function buildRosterTiles(profiles: RosterProfile[]): RosterTile[] {
  const byName = new Map(profiles.map((p) => [p.name.toLowerCase(), p]));

  const tiles: RosterTile[] = profiles.map((p) => ({
    ...p,
    isDirect: (DIRECT_NAMES as readonly string[]).includes(p.name.toLowerCase()),
  }));

  for (const name of DIRECT_NAMES) {
    if (!byName.has(name)) {
      tiles.push({
        description: "",
        displayName: DIRECT_LABELS[name],
        isDefault: false,
        isDirect: true,
        lastSession: null,
        name,
      });
    }
  }

  return tiles.sort((a, b) => {
    const aTime = a.lastSession?.lastActive ?? -1;
    const bTime = b.lastSession?.lastActive ?? -1;

    if (aTime !== bTime) {return bTime - aTime;}

    return a.displayName.localeCompare(b.displayName);
  });
}
