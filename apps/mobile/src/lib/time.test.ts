import { describe, expect, it } from "vitest";

import { timeAgo } from "./time";

describe("timeAgo", () => {
  const now = new Date("2026-01-15T12:00:00Z").getTime();

  it("returns 'just now' for the current moment and sub-minute gaps", () => {
    expect(timeAgo(now / 1000, now)).toBe("just now");
    expect(timeAgo(now / 1000 - 30, now)).toBe("just now");
  });

  it("formats minutes, hours, days, weeks, months, years", () => {
    expect(timeAgo(now / 1000 - 5 * 60, now)).toBe("5m ago");
    expect(timeAgo(now / 1000 - 3 * 3600, now)).toBe("3h ago");
    expect(timeAgo(now / 1000 - 2 * 86400, now)).toBe("2d ago");
    expect(timeAgo(now / 1000 - 14 * 86400, now)).toBe("2w ago");
    expect(timeAgo(now / 1000 - 90 * 86400, now)).toBe("3mo ago");
    expect(timeAgo(now / 1000 - 400 * 86400, now)).toBe("1y ago");
  });

  it("returns empty string for a falsy timestamp", () => {
    expect(timeAgo(0, now)).toBe("");
  });
});
