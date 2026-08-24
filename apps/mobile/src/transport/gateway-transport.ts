import { buildHermesWebSocketUrl, JsonRpcGatewayClient } from "@hermes/shared";

import type {
  ChatMessage,
  HermesTransport,
  OpenSessionResult,
  RosterProfile,
  TransportConnectionState,
  TransportStreamEvent,
} from "./types";

export interface GatewayTransportOptions {
  /** Base REST origin, e.g. "http://127.0.0.1:9119". Empty string means
   *  same-origin (production PWA served by the dashboard/reverse proxy). */
  baseUrl?: string;
  /**
   * Resolves the auth query param for the /api/ws upgrade — `["token", v]`
   * for a long-lived loopback/dev token, `["ticket", v]` for a freshly
   * minted single-use gated-mode ticket. See ARCHITECTURE.md: this is the
   * seam that needs a real login flow before this transport can be used
   * against a public HTTPS/WSS endpoint. In dev it reads
   * VITE_HERMES_DEV_TOKEN as a stand-in.
   */
  resolveAuthParam?: () => Promise<readonly [string, string]>;
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

  private readonly client = new JsonRpcGatewayClient({ requestIdPrefix: "m" });
  private readonly baseUrl: string;
  private readonly resolveAuthParam: () => Promise<readonly [string, string]>;
  private state: TransportConnectionState = "idle";
  private readonly stateHandlers = new Set<(state: TransportConnectionState) => void>();
  private readonly streamHandlers = new Set<(event: TransportStreamEvent) => void>();
  private connectPromise: null | Promise<void> = null;

  constructor(options: GatewayTransportOptions = {}) {
    this.baseUrl = options.baseUrl ?? "";
    this.resolveAuthParam = options.resolveAuthParam ?? (async () => ["token", devToken()] as const);

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

  private emit(event: TransportStreamEvent): void {
    for (const handler of this.streamHandlers) {handler(event);}
  }

  private setState(state: TransportConnectionState): void {
    if (this.state === state) {return;}
    this.state = state;

    for (const handler of this.stateHandlers) {handler(state);}
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

/** Dev-only stand-in for a real login flow (see GatewayTransportOptions).
 *  Never a provider API key / SSH key / long-lived backend secret — just
 *  the same short-lived dashboard session token Desktop's dev proxy already
 *  injects into index.html for local development. */
function devToken(): string {
  return import.meta.env.VITE_HERMES_DEV_TOKEN ?? "";
}
