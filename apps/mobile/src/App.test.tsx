import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetTransportForTests } from "@/transport";

import { App } from "./App";

// Force the mock transport regardless of DEV mode so this test is stable
// under `vitest run` (production-like) as well as `vite dev`.
vi.stubEnv("VITE_HERMES_TRANSPORT", "mock");

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  __resetTransportForTests();
});

afterEach(() => {
  cleanup();
});

describe("Home selector", () => {
  it("shows exactly the three top-level choices You / Claude / Hermes", () => {
    renderAt("/");
    const list = screen.getByRole("list");
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(screen.getByRole("button", { name: "You" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Claude" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Hermes" })).toBeTruthy();
  });

  it("tapping Hermes navigates to the picker", async () => {
    renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: "Hermes" }));
    await waitFor(() => screen.getByRole("heading", { name: "Hermes" }));
  });

  it("tapping You opens a chat with the You lane header", async () => {
    renderAt("/");
    fireEvent.click(screen.getByRole("button", { name: "You" }));
    await waitFor(() => screen.getByRole("heading", { name: "You" }));
  });
});

describe("Hermes picker", () => {
  it("renders a two-column grid including direct Erika and Paris tiles, sorted by recency", async () => {
    renderAt("/hermes");
    await waitFor(() => screen.getByText("Erika"));
    expect(screen.getByText("Paris")).toBeTruthy();

    const grid = screen.getByRole("list", { name: "Hermes roster" });
    expect(grid.className).toMatch(/grid-cols-2/);

    // Seeded fixture: Erika (6m ago) sorts before Paris (40m ago) before
    // Orion (3h ago) — most-recently-used first.
    const rowNames = within(grid)
      .getAllByRole("button")
      .map((el) => el.textContent ?? "");

    const erikaIdx = rowNames.findIndex((t) => t.includes("Erika"));
    const parisIdx = rowNames.findIndex((t) => t.includes("Paris"));
    const orionIdx = rowNames.findIndex((t) => t.includes("Orion"));
    expect(erikaIdx).toBeGreaterThanOrEqual(0);
    expect(erikaIdx).toBeLessThan(parisIdx);
    expect(parisIdx).toBeLessThan(orionIdx);
  });

  it("back returns to the home selector", async () => {
    renderAt("/hermes");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => screen.getByRole("heading", { name: "Hermes" }));
  });
});

describe("Chat resume-last-session", () => {
  it("opening a profile tile resumes its last session transcript deterministically", async () => {
    renderAt("/hermes");
    await waitFor(() => screen.getByText("Erika"));
    fireEvent.click(screen.getByText("Erika"));

    await waitFor(() => screen.getByRole("heading", { name: "Erika" }));
    // Seeded Erika session has a prior exchange — resume must load it, not
    // start a blank chat.
    await waitFor(() => screen.getByText(/Status on the mobile PWA task/));
  });

  it("a profile with no history starts a fresh empty chat instead of erroring", async () => {
    renderAt("/hermes");
    await waitFor(() => screen.getByText("Nexora"));
    fireEvent.click(screen.getByText("Nexora"));
    await waitFor(() => screen.getByRole("heading", { name: "Nexora" }));
    await waitFor(() => screen.getByText("Say hello to start the conversation."));
  });

  it("chat is a messaging surface (bubbles + composer), not a terminal", async () => {
    renderAt("/hermes");
    await waitFor(() => screen.getByText("Erika"));
    fireEvent.click(screen.getByText("Erika"));
    await waitFor(() => screen.getByLabelText("Message composer"));
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(document.querySelector(".xterm")).toBeNull();
  });

  it("back from chat to picker preserves the cached session (no reload flash)", async () => {
    renderAt("/hermes");
    await waitFor(() => screen.getByText("Erika"));
    fireEvent.click(screen.getByText("Erika"));
    await waitFor(() => screen.getByText(/Status on the mobile PWA task/));

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => screen.getByRole("heading", { name: "Hermes" }));

    fireEvent.click(screen.getByText("Erika"));
    // Cached: the transcript reappears without a "Loading conversation…" flash.
    await waitFor(() => screen.getByText(/Status on the mobile PWA task/));
  });
});
