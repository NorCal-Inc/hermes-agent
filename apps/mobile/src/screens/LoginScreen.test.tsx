import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuthProviderInfo, HermesTransport, TransportConnectionState } from "@/transport";

import { LoginScreen } from "./LoginScreen";

function fakeTransport(overrides: Partial<HermesTransport> = {}): HermesTransport {
  return {
    connect: vi.fn(),
    connectionState: vi.fn((): TransportConnectionState => "unauthenticated"),
    kind: "gateway",
    listAuthProviders: vi.fn(async () => []),
    listRosterProfiles: vi.fn(),
    loginWithPassword: vi.fn(),
    logout: vi.fn(),
    oauthLoginUrl: vi.fn((provider: string) => `/auth/login?provider=${provider}`),
    onConnectionState: vi.fn(() => () => {}),
    onStreamEvent: vi.fn(() => () => {}),
    openSession: vi.fn(),
    sendMessage: vi.fn(),
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("LoginScreen", () => {
  it("renders a username/password form for a password provider and signs in on submit", async () => {
    const providers: AuthProviderInfo[] = [{ displayName: "Username & Password", name: "basic", supportsPassword: true }];
    const loginWithPassword = vi.fn().mockResolvedValue(undefined);
    const onSignedIn = vi.fn();

    render(
      <LoginScreen
        onSignedIn={onSignedIn}
        transport={fakeTransport({ listAuthProviders: vi.fn(async () => providers), loginWithPassword })}
      />,
    );

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "erika" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledTimes(1));
    expect(loginWithPassword).toHaveBeenCalledWith("basic", "erika", "hunter2");
  });

  it("shows the server's error message on invalid credentials without signing in", async () => {
    const providers: AuthProviderInfo[] = [{ displayName: "Username & Password", name: "basic", supportsPassword: true }];
    const loginWithPassword = vi.fn().mockRejectedValue(new Error("Invalid credentials"));
    const onSignedIn = vi.fn();

    render(
      <LoginScreen
        onSignedIn={onSignedIn}
        transport={fakeTransport({ listAuthProviders: vi.fn(async () => providers), loginWithPassword })}
      />,
    );

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "erika" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => screen.getByText("Invalid credentials"));
    expect(onSignedIn).not.toHaveBeenCalled();
  });

  it("renders an OAuth provider as a same-origin sign-in link, not a popup", async () => {
    const providers: AuthProviderInfo[] = [{ displayName: "Nous", name: "nous", supportsPassword: false }];

    render(
      <LoginScreen
        onSignedIn={vi.fn()}
        transport={fakeTransport({ listAuthProviders: vi.fn(async () => providers) })}
      />,
    );

    const link = await waitFor(() => screen.getByRole("link", { name: "Sign in with Nous" }));

    expect(link.getAttribute("href")).toBe("/auth/login?provider=nous");
  });

  it("shows a configuration error when no auth providers are registered", async () => {
    render(<LoginScreen onSignedIn={vi.fn()} transport={fakeTransport({ listAuthProviders: vi.fn(async () => []) })} />);

    await waitFor(() => screen.getByText(/No sign-in method is configured/));
  });
});
