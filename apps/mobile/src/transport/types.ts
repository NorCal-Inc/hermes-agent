/**
 * Transport abstraction — the ONLY seam Hermes Mobile uses to reach a
 * backend. Two implementations exist:
 *
 *  - MockTransport (mock-transport.ts): fully in-memory, deterministic,
 *    used in dev by default and in tests. No network at all.
 *  - GatewayTransport (gateway-transport.ts): talks to the real Hermes
 *    backend over REST + the /api/ws JSON-RPC WebSocket, reusing
 *    @hermes/shared (the same client Desktop and the web dashboard use).
 *
 * Nothing outside src/transport may import @hermes/shared's WebSocket
 * client directly, and nothing in the app may reach for Electron or the
 * desktop capability bridge — nothing here does either.
 */

export interface RosterProfileSession {
  id: string;
  title: string;
  preview: string;
  /** Unix seconds, matching the backend's session timestamps. */
  lastActive: number;
  messageCount: number;
}

/** One entry from `profiles.list` — a company, team, or direct person
 *  (Erika, Paris) the user can open a chat with. */
export interface RosterProfile {
  name: string;
  displayName: string;
  description: string;
  isDefault: boolean;
  lastSession: RosterProfileSession | null;
}

export type ChatRole = "assistant" | "system" | "tool" | "user";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  /** Unix milliseconds. */
  timestamp: number;
  /** True while an assistant message is still streaming in. */
  pending?: boolean;
}

export interface OpenSessionResult {
  sessionId: string;
  messages: ChatMessage[];
  /** True when this call resumed an existing session rather than creating one. */
  resumed: boolean;
}

export type TransportStreamEvent =
  | { type: "message.delta"; sessionId: string; messageId: string; delta: string }
  | { type: "message.complete"; sessionId: string; message: ChatMessage }
  | { type: "status"; sessionId: string; status: "idle" | "streaming" };

/**
 * "unauthenticated" means the backend is in gated mode and this transport
 * holds no valid session — connect() will not proceed until a caller signs
 * in via `loginWithPassword` / `oauthLoginUrl` and connect() is retried.
 */
export type TransportConnectionState = "connected" | "connecting" | "error" | "idle" | "unauthenticated";

/** One entry from `GET /api/auth/providers` — the login screen's bootstrap. */
export interface AuthProviderInfo {
  name: string;
  displayName: string;
  supportsPassword: boolean;
}

export interface HermesTransport {
  readonly kind: "gateway" | "mock";
  connect(): Promise<void>;
  connectionState(): TransportConnectionState;
  onConnectionState(handler: (state: TransportConnectionState) => void): () => void;
  listRosterProfiles(): Promise<RosterProfile[]>;
  /**
   * Open a chat with `profile` (null = the caller's own/launch profile —
   * used for the You/Claude lanes). When `preferredSessionId` is set this
   * resumes that exact session; otherwise it resumes the profile's most
   * recent session, falling back to creating a fresh one when none exists.
   * Deterministic: same roster state in -> same resume-or-create decision.
   */
  openSession(profile: null | string, preferredSessionId: null | string): Promise<OpenSessionResult>;
  sendMessage(sessionId: string, profile: null | string, text: string): Promise<void>;
  onStreamEvent(handler: (event: TransportStreamEvent) => void): () => void;
  /**
   * Auth surface — a no-op for MockTransport (kind "mock" never requires
   * login). GatewayTransport backs these with the SAME cookie/ws-ticket
   * dashboard-auth routes the web dashboard already uses
   * (hermes_cli/dashboard_auth/routes.py): no new backend auth system.
   */
  listAuthProviders(): Promise<AuthProviderInfo[]>;
  loginWithPassword(provider: string, username: string, password: string): Promise<void>;
  /** URL to full-page-navigate the browser to for an OAuth provider's sign-in. */
  oauthLoginUrl(provider: string): string;
  logout(): Promise<void>;
}
