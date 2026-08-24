import { buildHermesWebSocketUrl, GatewayReauthRequiredError, isGatewayReauthRequired, JsonRpcGatewayClient } from "@hermes/shared";

import type {
  AuthProviderInfo,
  ChatMessage,
  HermesTransport,
  OpenSessionResult,
  RosterProfile,
  TransportConnectionState,
  TransportStreamEvent,
} from "./types";

/** WS close code the gateway sends when `_ws_auth_ok` rejects the upgrade
 *  credential (missing/expired/already-consumed ticket, or a dead session) —
 *  see hermes_cli/web_server.py `gateway_ws` / `_ws_auth_reason`. */
const WS_AUTH_REJECTED_CLOSE_CODE = 4401;

export interface GatewayTransportOptions {
  /** Base REST origin, e.g. "http://127.0.0.1:9119". Empty string means
   *  same-origin (production PWA served by the dashboard/reverse proxy). */
  baseUrl?: string;
  /**
   * Resolves the auth query param for the /api/ws upgrade — `["ticket", v]`
   * for a freshly minted single-use gated-mode ticket (the production path,
   * backed by the same hermes_cli/dashboard_auth routes the web dashboard
   * uses), or `["token", v]` for the loopback dev token (DEV builds only —
   * see `devModeAuthParam`). Overridable for tests.
   */
  resolveAuthParam?: () => Promise<readonly [string, string]>;
}

interface HealthResponse {
  auth_required?: boolean;
}

interface WsTicketResponse {
  ticket: string;
  ttl_seconds: number;
}

interface AuthProvidersResponse {
  providers: Array<{ name: string; display_name?: string; supports_password?: boolean }>;
}

interface ProfilesListRpcRow {
  name: string;
  display_name?: string;
  description?: string;
  is_default?: boolean;
  last_session?: {
    id: string;
    title?: string;
    preview?: string;
    last_active?: number;
    message_count?: number;
  } | null;
}

/**
 * Real backend transport. REST is used only where the gateway doesn't
 * already expose an equivalent RPC (there is none needed here); everything
 * else — roster, session resume/create, prompt submission, streaming — goes
 * over the single /api/ws JSON-RPC WebSocket via @hermes/shared's
 * JsonRpcGatewayClient, the exact client Desktop uses. No SSH, no Electron
 * desktop-bridge global, no Electron import anywhere in this file.
 */
export class GatewayTransport implements HermesTransport {
  readonly kind = "gateway" as const;

  private readonly client: JsonRpcGatewayClient;
  private readonly baseUrl: string;
  private readonly resolveAuthParam: () => Promise<readonly [string, string]>;
  private state: TransportConnectionState = "idle";
  private readonly stateHandlers = new Set<(state: TransportConnectionState) => void>();
  private readonly streamHandlers = new Set<(event: TransportStreamEvent) => void>();
  private connectPromise: null | Promise<void> = null;

  constructor(options: GatewayTransportOptions = {}) {
    this.baseUrl = options.baseUrl ?? "";
    this.resolveAuthParam = options.resolveAuthParam ?? (() => this.defaultResolveAuthParam());

    this.client = new JsonRpcGatewayClient({
      onSocketClose: (event) => {
        // The gateway closes the WS with 4401 when `_ws_auth_ok` rejects the
        // upgrade credential (expired/consumed ticket, dead session) — see
        // hermes_cli/web_server.py `gateway_ws`. Surface that distinctly as
        // "unauthenticated" (not a generic transport error) so the app can
        // route back to the login screen instead of just retrying, and
        // intercept the client's default 'closed' transition so onState
        // below doesn't immediately overwrite it with "error".
        if (event.code === WS_AUTH_REJECTED_CLOSE_CODE) {
          this.setState("unauthenticated");

          return true;
        }

        return false;
      },
      requestIdPrefix: "m",
    });

    this.client.onState((state) => {
      this.setState(
        state === "open" ? "connected" : state === "connecting" ? "connecting" : state === "idle" ? "idle" : "error",
      );
    });

    this.client.on<{ session_id?: string; delta?: string; id?: string }>("message.delta", (event) => {
      const sessionId = event.session_id ?? event.payload?.session_id;
      const delta = event.payload?.delta;
      const messageId = event.payload?.id;

      if (!sessionId || delta === undefined || !messageId) {return;}
      this.emit({ delta, messageId, sessionId, type: "message.delta" });
    });

    this.client.on<{ session_id?: string; content?: string; id?: string; role?: string }>(
      "message.complete",
      (event) => {
        const sessionId = event.session_id ?? event.payload?.session_id;
        const payload = event.payload;

        if (!sessionId || !payload?.id) {return;}
        this.emit({
          message: {
            content: payload.content ?? "",
            id: payload.id,
            role: (payload.role as ChatMessage["role"]) ?? "assistant",
            timestamp: Date.now(),
          },
          sessionId,
          type: "message.complete",
        });
      },
    );

    this.client.on<{ session_id?: string; status?: string }>("status.update", (event) => {
      const sessionId = event.session_id ?? event.payload?.session_id;
      const status = event.payload?.status;

      if (!sessionId || (status !== "idle" && status !== "streaming")) {return;}
      this.emit({ sessionId, status, type: "status" });
    });
  }

  async connect(): Promise<void> {
    if (this.connectPromise) {return this.connectPromise;}
    this.connectPromise = (async () => {
      const authParam = await this.resolveAuthParam();

      const wsUrl = buildHermesWebSocketUrl({
        authParam: [authParam[0], authParam[1]],
        basePath: this.baseUrl,
        path: "/api/ws",
      });

      await this.client.connect(wsUrl);
    })();

    try {
      await this.connectPromise;
    } catch (error) {
      // Mint failed (e.g. /api/auth/ws-ticket 401'd because the session
      // cookie is gone/expired) — surface as "unauthenticated" so the app
      // routes back to the login screen instead of a generic error banner.
      if (isGatewayReauthRequired(error)) {
        this.setState("unauthenticated");
      }

      throw error;
    } finally {
      this.connectPromise = null;
    }
  }

  connectionState(): TransportConnectionState {
    return this.state;
  }

  onConnectionState(handler: (state: TransportConnectionState) => void): () => void {
    this.stateHandlers.add(handler);
    handler(this.state);

    return () => this.stateHandlers.delete(handler);
  }

  async listRosterProfiles(): Promise<RosterProfile[]> {
    const result = await this.client.request<{ profiles: ProfilesListRpcRow[] }>("profiles.list", {
      include_sessions: true,
    });

    return result.profiles.map((row) => ({
      description: row.description ?? "",
      displayName: row.display_name?.trim() || row.name,
      isDefault: row.is_default ?? false,
      lastSession: row.last_session
        ? {
            id: row.last_session.id,
            lastActive: row.last_session.last_active ?? 0,
            messageCount: row.last_session.message_count ?? 0,
            preview: row.last_session.preview ?? "",
            title: row.last_session.title ?? row.display_name ?? row.name,
          }
        : null,
      name: row.name,
    }));
  }

  async openSession(profile: null | string, preferredSessionId: null | string): Promise<OpenSessionResult> {
    let targetSessionId = preferredSessionId;

    if (!targetSessionId) {
      // profiles.list already carries last_session as part of the roster
      // fetch; callers that already have it should pass it as
      // preferredSessionId instead of paying a second round trip. This
      // fallback covers direct deep-links (e.g. a push notification) that
      // only know the profile name.
      const mostRecent = await this.client.request<{ session_id: string | null }>("session.most_recent", {
        profile: profile ?? undefined,
      });

      targetSessionId = mostRecent.session_id;
    }

    if (targetSessionId) {
      try {
        const resumed = await this.client.request<{
          session_id: string;
          messages?: Array<{ role: string; content: string | null; timestamp?: number }>;
        }>("session.resume", { profile: profile ?? undefined, session_id: targetSessionId });

        return {
          messages: toChatMessages(resumed.messages ?? []),
          resumed: true,
          sessionId: resumed.session_id,
        };
      } catch {
        // Fall through to create — the pinned/most-recent session may have
        // been deleted or is otherwise unresumable; a fresh chat is the
        // correct deterministic fallback described in ACCEPTANCE.
      }
    }

    const created = await this.client.request<{ session_id: string }>("session.create", {
      profile: profile ?? undefined,
    });

    return { messages: [], resumed: false, sessionId: created.session_id };
  }

  async sendMessage(sessionId: string, profile: null | string, text: string): Promise<void> {
    await this.client.request("prompt.submit", { profile: profile ?? undefined, session_id: sessionId, text });
  }

  onStreamEvent(handler: (event: TransportStreamEvent) => void): () => void {
    this.streamHandlers.add(handler);

    return () => this.streamHandlers.delete(handler);
  }

  async listAuthProviders(): Promise<AuthProviderInfo[]> {
    const res = await fetch(`${this.baseUrl}/api/auth/providers`, { credentials: "same-origin" });

    if (!res.ok) {throw new Error(`/api/auth/providers: HTTP ${res.status}`);}
    const body = (await res.json()) as AuthProvidersResponse;

    return (body.providers ?? []).map((p) => ({
      displayName: p.display_name?.trim() || p.name,
      name: p.name,
      supportsPassword: Boolean(p.supports_password),
    }));
  }

  async loginWithPassword(provider: string, username: string, password: string): Promise<void> {
    const res = await fetch(`${this.baseUrl}/auth/password-login`, {
      body: JSON.stringify({ next: "/", password, provider, username }),
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      method: "POST",
    });

    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))) as { detail?: string };

      throw new Error(detail.detail || `login failed: HTTP ${res.status}`);
    }
  }

  oauthLoginUrl(provider: string): string {
    return `${this.baseUrl}/auth/login?provider=${encodeURIComponent(provider)}`;
  }

  async logout(): Promise<void> {
    this.client.close();
    await fetch(`${this.baseUrl}/auth/logout`, { credentials: "same-origin", method: "POST" }).catch(() => {
      // Best-effort — even if the network call fails, drop the client-side
      // state so the app returns to the login screen rather than a stuck UI.
    });
    this.setState("unauthenticated");
  }

  private emit(event: TransportStreamEvent): void {
    for (const handler of this.streamHandlers) {handler(event);}
  }

  private setState(state: TransportConnectionState): void {
    if (this.state === state) {return;}
    this.state = state;

    for (const handler of this.stateHandlers) {handler(state);}
  }

  /**
   * Production auth path: probe `/api/health` (public) to learn whether the
   * backend is gated, then either mint a fresh single-use `?ticket=` via the
   * SAME `POST /api/auth/ws-ticket` route the web dashboard uses (session
   * cookie rides along automatically — same-origin fetch), or, for an
   * ungated loopback backend, fall back to the legacy `?token=` — but ONLY
   * in a Vite dev build. `import.meta.env.DEV` is a compile-time constant
   * Vite inlines to `false` in `vite build`, so this whole branch (and
   * VITE_HERMES_DEV_TOKEN) is dead-code-eliminated from the production
   * bundle rather than merely unused at runtime.
   */
  private async defaultResolveAuthParam(): Promise<readonly [string, string]> {
    const healthRes = await fetch(`${this.baseUrl}/api/health`, { credentials: "same-origin" }).catch(
      () => null,
    );

    if (!healthRes) {
      throw new Error(`/api/health: network error (could not reach ${this.baseUrl || "the backend"})`);
    }

    if (!healthRes.ok) {
      throw new Error(`/api/health: HTTP ${healthRes.status}`);
    }

    const health = (await healthRes.json()) as HealthResponse;

    if (!health.auth_required) {
      if (import.meta.env.DEV) {
        return ["token", import.meta.env.VITE_HERMES_DEV_TOKEN ?? ""] as const;
      }

      throw new Error(
        "Gateway backend is not running in gated mode (auth_required=false). " +
          "Hermes Mobile only connects to a gated backend outside of local dev.",
      );
    }

    const res = await fetch(`${this.baseUrl}/api/auth/ws-ticket`, { credentials: "same-origin", method: "POST" });

    if (res.status === 401) {
      throw new GatewayReauthRequiredError("Your Hermes session has expired. Sign in again.");
    }

    if (!res.ok) {
      throw new Error(`/api/auth/ws-ticket: HTTP ${res.status}`);
    }

    const body = (await res.json()) as WsTicketResponse;

    return ["ticket", body.ticket] as const;
  }
}

function toChatMessages(raw: Array<{ role: string; content: string | null; timestamp?: number }>): ChatMessage[] {
  return raw
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m, i) => ({
      content: m.content ?? "",
      id: `${m.timestamp ?? i}-${i}`,
      role: m.role as ChatMessage["role"],
      timestamp: (m.timestamp ?? Date.now() / 1000) * 1000,
    }));
}
