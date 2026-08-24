import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { __resetTransportForTests } from "@/transport";

import { AuthGate } from "./auth-gate";

// AuthGate calls getTransport() (the module-level cache in src/transport),
// which selects MockTransport unless VITE_HERMES_TRANSPORT="gateway" — force
// gateway here so we control connectionState() deterministically instead of
// exercising a real WebSocket.
vi.stubEnv("VITE_HERMES_TRANSPORT", "gateway");

const handlers: Array<(state: string) => void> = [];
let currentState = "idle";

vi.mock("@/transport/gateway-transport", () => ({
  GatewayTransport: class {
    kind = "gateway";
    connectionState() {
      return currentState;
    }
    onConnectionState(handler: (state: string) => void) {
      handlers.push(handler);
      handler(currentState);

      return () => {};
    }
    listAuthProviders() {
      return Promise.resolve([]);
    }
  },
}));

beforeEach(() => {
  __resetTransportForTests();
  handlers.length = 0;
  currentState = "idle";
});

afterEach(() => cleanup());

describe("AuthGate", () => {
  it("renders children when the transport is not unauthenticated", () => {
    currentState = "connected";
    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    );
    expect(screen.getByText("protected content")).toBeTruthy();
  });

  it("swaps to the login screen once the transport reports 'unauthenticated'", async () => {
    currentState = "idle";
    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    );
    expect(screen.getByText("protected content")).toBeTruthy();

    currentState = "unauthenticated";
    act(() => {
      handlers.forEach((h) => h("unauthenticated"));
    });

    await waitFor(() => screen.getByRole("heading", { name: "Sign in to Hermes" }));
    expect(screen.queryByText("protected content")).toBeNull();
  });
});
