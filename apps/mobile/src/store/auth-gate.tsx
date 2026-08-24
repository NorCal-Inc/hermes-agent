import { type ReactNode, useEffect, useMemo, useState } from "react";

import { LoginScreen } from "@/screens/LoginScreen";
import { getTransport } from "@/transport";

/**
 * Sits above ChatStoreProvider so nothing under it (roster fetch, WS
 * connect) ever runs while the transport is unauthenticated. Driven purely
 * by `transport.connectionState()` — the SAME signal ChatStoreProvider
 * already surfaces as "Connecting…" / "Connected" in the picker header, so
 * there is exactly one source of truth for connection/auth state, not two.
 *
 * MockTransport's connectionState never becomes "unauthenticated" (see
 * mock-transport.ts), so this is a no-op passthrough in dev/tests — the
 * Home selector and Hermes picker render exactly as before.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const transport = useMemo(() => getTransport(), []);
  const [needsLogin, setNeedsLogin] = useState(() => transport.connectionState() === "unauthenticated");

  useEffect(
    () => transport.onConnectionState((state) => setNeedsLogin(state === "unauthenticated")),
    [transport],
  );

  if (needsLogin) {
    return <LoginScreen onSignedIn={() => setNeedsLogin(false)} transport={transport} />;
  }

  return <>{children}</>;
}
