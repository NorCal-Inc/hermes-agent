import { GatewayTransport } from "./gateway-transport";
import { MockTransport } from "./mock-transport";
import type { HermesTransport } from "./types";

export { GatewayTransport } from "./gateway-transport";
export { MockTransport } from "./mock-transport";
export type {
  AuthProviderInfo,
  ChatMessage,
  ChatRole,
  HermesTransport,
  OpenSessionResult,
  RosterProfile,
  RosterProfileSession,
  TransportConnectionState,
  TransportStreamEvent,
} from "./types";

let cached: HermesTransport | null = null;

/**
 * Selects the transport for this build/runtime.
 *
 * - VITE_HERMES_TRANSPORT=mock|gateway forces a choice (used by tests).
 * - Otherwise: `import.meta.env.DEV` (vite dev server) -> mock, so the app
 *   is fully explorable with zero backend setup; a production build ->
 *   gateway, so `npm run build && vite preview` (or a real deployment)
 *   talks to the live Hermes backend.
 *
 * Memoized per module instance — callers share one connection.
 */
export function getTransport(): HermesTransport {
  if (cached) {return cached;}
  const forced = import.meta.env.VITE_HERMES_TRANSPORT;
  const useMock = forced ? forced === "mock" : import.meta.env.DEV;
  cached = useMock ? new MockTransport() : new GatewayTransport({ baseUrl: import.meta.env.VITE_HERMES_DASHBOARD_URL });

  return cached;
}

/** Test-only: reset the memoized transport between test files/cases. */
export function __resetTransportForTests(): void {
  cached = null;
}
