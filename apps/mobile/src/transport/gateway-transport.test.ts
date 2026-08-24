import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GatewayTransport } from "./gateway-transport";

class FakeSocket {
  static OPEN = 1;
  static CLOSED = 3;
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number; wasClean: boolean }) => void) | null = null;

  private listeners = new Map<string, Set<(event?: unknown) => void>>();

  addEventListener = vi.fn((type: string, handler: (event?: unknown) => void) => {
    let set = this.listeners.get(type);

    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }

    set.add(handler);

    if (type === "open") {
      queueMicrotask(() => {
        this.readyState = FakeSocket.OPEN;
        handler();
      });
    }
  });

  removeEventListener = vi.fn((type: string, handler: (event?: unknown) => void) => {
    this.listeners.get(type)?.delete(handler);
  });

  close = vi.fn();

  send = vi.fn();

  emitClose(code: number): void {
    this.readyState = FakeSocket.CLOSED;

    for (const handler of this.listeners.get("close") ?? []) {handler({ code, wasClean: code === 1000 });}
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    json: async () => body,
    ok: status >= 200 && status < 300,
    status,
  } as Response;
}

describe("GatewayTransport auth", () => {
  beforeEach(() => {
    vi.stubGlobal("WebSocket", FakeSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("gated backend: mints a fresh single-use ticket and opens the WS with it", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/health")) {return Promise.resolve(jsonResponse({ auth_required: true }));}

      if (url.endsWith("/api/auth/ws-ticket")) {
        return Promise.resolve(jsonResponse({ ticket: "tk-123", ttl_seconds: 30 }));
      }

      throw new Error(`unexpected fetch: ${url} ${init?.method ?? ""}`);
    });

    vi.stubGlobal("fetch", fetchMock);

    const transport = new GatewayTransport({ baseUrl: "" });

    await transport.connect();

    expect(transport.connectionState()).toBe("connected");
    const ticketCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/api/auth/ws-ticket"));

    expect(ticketCall?.[1]).toMatchObject({ method: "POST" });
  });

  it("gated backend: a 401 ws-ticket mint surfaces as 'unauthenticated', not a generic error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/health")) {return Promise.resolve(jsonResponse({ auth_required: true }));}

        if (url.endsWith("/api/auth/ws-ticket")) {return Promise.resolve(jsonResponse({}, 401));}
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    const transport = new GatewayTransport({ baseUrl: "" });

    await expect(transport.connect()).rejects.toThrow(/session has expired/i);
    expect(transport.connectionState()).toBe("unauthenticated");
  });

  it("a WS close with code 4401 (ticket rejected mid-handshake) surfaces as 'unauthenticated'", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/health")) {return Promise.resolve(jsonResponse({ auth_required: true }));}

        if (url.endsWith("/api/auth/ws-ticket")) {return Promise.resolve(jsonResponse({ ticket: "tk-1", ttl_seconds: 30 }));}
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    const transport = new GatewayTransport({ baseUrl: "" });
    const states: string[] = [];

    transport.onConnectionState((s) => states.push(s));
    await transport.connect();
    expect(transport.connectionState()).toBe("connected");

    const socket = (transport as unknown as { client: { socket: FakeSocket } }).client.socket;

    socket.emitClose(4401);

    expect(transport.connectionState()).toBe("unauthenticated");
    expect(states).toContain("unauthenticated");
  });

  it("ungated loopback backend falls back to the dev token in a DEV build", async () => {
    vi.stubEnv("VITE_HERMES_DEV_TOKEN", "dev-secret-token");
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.endsWith("/api/health")) {return Promise.resolve(jsonResponse({ auth_required: false }));}
        throw new Error(`unexpected fetch: ${url}`);
      }),
    );

    const transport = new GatewayTransport({ baseUrl: "" });

    await transport.connect();

    const socket = (transport as unknown as { client: { socket: { url?: string } } }).client;

    expect(socket).toBeTruthy();
    expect(transport.connectionState()).toBe("connected");
  });

  it("listAuthProviders maps the backend's snake_case provider shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({
            providers: [
              { display_name: "Username & Password", name: "basic", supports_password: true },
              { display_name: "Nous", name: "nous", supports_password: false },
            ],
          }),
        ),
      ),
    );

    const transport = new GatewayTransport({ baseUrl: "" });
    const providers = await transport.listAuthProviders();

    expect(providers).toEqual([
      { displayName: "Username & Password", name: "basic", supportsPassword: true },
      { displayName: "Nous", name: "nous", supportsPassword: false },
    ]);
  });

  it("loginWithPassword POSTs credentials as JSON and throws the server's detail on failure", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve(jsonResponse({ detail: "Invalid credentials" }, 401)),
    );

    vi.stubGlobal("fetch", fetchMock);

    const transport = new GatewayTransport({ baseUrl: "" });

    await expect(transport.loginWithPassword("basic", "erika", "wrong")).rejects.toThrow("Invalid credentials");

    const [url, init] = fetchMock.mock.calls[0];

    expect(url).toBe("/auth/password-login");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      next: "/",
      password: "wrong",
      provider: "basic",
      username: "erika",
    });
  });

  it("oauthLoginUrl builds a same-origin provider-scoped login URL", () => {
    const transport = new GatewayTransport({ baseUrl: "" });

    expect(transport.oauthLoginUrl("nous")).toBe("/auth/login?provider=nous");
  });

  it("logout POSTs /auth/logout and transitions to 'unauthenticated'", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(jsonResponse({}))));

    const transport = new GatewayTransport({ baseUrl: "" });

    await transport.logout();

    expect(transport.connectionState()).toBe("unauthenticated");
  });
});
