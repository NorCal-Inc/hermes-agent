import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { buildRosterTiles, type RosterTile } from "@/lib/roster";
import { getTransport } from "@/transport";
import type { ChatMessage, RosterProfile, TransportConnectionState } from "@/transport";

/**
 * The two fixed Home lanes ("You" / "Claude") are NOT company/team profiles
 * — they are the ChatGPT/OpenAI-facing and Claude-facing chat lanes named
 * directly in the product spec. There is no dedicated backend concept for
 * "a lane" today, so this maps each lane onto an ordinary Hermes profile of
 * the same name and reuses the exact profiles.list / session.create /
 * session.resume contracts every company tile already uses — no new
 * backend surface. See apps/mobile/README.md "Known limitation: lanes"
 * for what a real multi-provider lane would need instead.
 */
const LANE_PROFILES: Record<"claude" | "you", string> = {
  claude: "claude",
  you: "you",
};

export type ChatTargetKind = "lane" | "profile";

export interface ChatTarget {
  kind: ChatTargetKind;
  /** Lane id ("you" | "claude") or profile name. */
  key: string;
  label: string;
}

interface CachedSession {
  sessionId: string;
  messages: ChatMessage[];
  streaming: boolean;
}

interface ChatStoreValue {
  connectionState: TransportConnectionState;
  roster: RosterTile[];
  rosterLoading: boolean;
  rosterError: string | null;
  refreshRoster: () => void;
  /** Resolves (resume-or-create, deterministic) and caches the session for
   *  `target`, then returns its cache key so the caller can select it. */
  openChat: (target: ChatTarget) => Promise<string>;
  getSession: (cacheKey: string) => CachedSession | undefined;
  sendMessage: (cacheKey: string, text: string) => Promise<void>;
  logout: () => Promise<void>;
}

const ChatStoreContext = createContext<ChatStoreValue | null>(null);

function cacheKeyFor(target: ChatTarget): string {
  return `${target.kind}:${target.key}`;
}

export function ChatStoreProvider({ children }: { children: ReactNode }) {
  const transport = useMemo(() => getTransport(), []);
  const [connectionState, setConnectionState] = useState<TransportConnectionState>(transport.connectionState());
  const [roster, setRoster] = useState<RosterTile[]>([]);
  const [rosterLoading, setRosterLoading] = useState(true);
  const [rosterError, setRosterError] = useState<string | null>(null);
  // Sessions are cached by cacheKey so navigating Chat -> Back -> Chat again
  // shows the same in-memory transcript instantly instead of re-fetching —
  // "Back returns to the selector/picker without losing the session".
  const sessionsRef = useRef(new Map<string, CachedSession>());
  const [, forceRender] = useState(0);
  const bump = useCallback(() => forceRender((n) => n + 1), []);
  const sessionIdToCacheKey = useRef(new Map<string, string>());

  useEffect(() => transport.onConnectionState(setConnectionState), [transport]);

  const loadRoster = useCallback(() => {
    setRosterLoading(true);
    setRosterError(null);
    transport
      .listRosterProfiles()
      .then((profiles: RosterProfile[]) => {
        setRoster(buildRosterTiles(profiles));
      })
      .catch((err: unknown) => {
        setRosterError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setRosterLoading(false));
  }, [transport]);

  useEffect(() => {
    transport.connect().catch(() => {
      // Surfaced via connectionState -> "error"; the UI shows a banner
      // rather than throwing during render.
    });
    loadRoster();
  }, [transport, loadRoster]);

  useEffect(
    () =>
      transport.onStreamEvent((event) => {
        const cacheKey = sessionIdToCacheKey.current.get(event.sessionId);

        if (!cacheKey) {return;}
        const session = sessionsRef.current.get(cacheKey);

        if (!session) {return;}

        if (event.type === "message.delta") {
          const idx = session.messages.findIndex((m) => m.id === event.messageId);

          const draft: ChatMessage = {
            content: event.delta,
            id: event.messageId,
            pending: true,
            role: "assistant",
            timestamp: Date.now(),
          };

          session.messages = idx === -1 ? [...session.messages, draft] : replaceAt(session.messages, idx, draft);
        } else if (event.type === "message.complete") {
          const idx = session.messages.findIndex((m) => m.id === event.message.id);
          session.messages =
            idx === -1 ? [...session.messages, event.message] : replaceAt(session.messages, idx, event.message);
        } else if (event.type === "status") {
          session.streaming = event.status === "streaming";
        }

        sessionsRef.current.set(cacheKey, { ...session });
        bump();
      }),
    [transport, bump],
  );

  const openChat = useCallback(
    async (target: ChatTarget): Promise<string> => {
      const cacheKey = cacheKeyFor(target);

      if (sessionsRef.current.has(cacheKey)) {return cacheKey;}

      const profile = target.kind === "lane" ? LANE_PROFILES[target.key as "claude" | "you"] : target.key;

      const preferredSessionId =
        target.kind === "profile" ? roster.find((r) => r.name === target.key)?.lastSession?.id ?? null : null;

      const result = await transport.openSession(profile, preferredSessionId);
      sessionsRef.current.set(cacheKey, {
        messages: result.messages,
        sessionId: result.sessionId,
        streaming: false,
      });
      sessionIdToCacheKey.current.set(result.sessionId, cacheKey);
      bump();

      return cacheKey;
    },
    [transport, roster, bump],
  );

  const getSession = useCallback((cacheKey: string) => sessionsRef.current.get(cacheKey), []);

  const sendMessage = useCallback(
    async (cacheKey: string, text: string): Promise<void> => {
      const session = sessionsRef.current.get(cacheKey);

      if (!session) {throw new Error(`sendMessage: no open session for ${cacheKey}`);}
      const trimmed = text.trim();

      if (!trimmed) {return;}

      const optimistic: ChatMessage = {
        content: trimmed,
        id: `local-${Date.now()}`,
        role: "user",
        timestamp: Date.now(),
      };

      session.messages = [...session.messages, optimistic];
      session.streaming = true;
      sessionsRef.current.set(cacheKey, { ...session });
      bump();

      const [, key] = cacheKey.split(":", 2);
      const profile = cacheKey.startsWith("lane:") ? LANE_PROFILES[key as "claude" | "you"] : key;
      await transport.sendMessage(session.sessionId, profile, trimmed);
    },
    [transport, bump],
  );

  const logout = useCallback(() => transport.logout(), [transport]);

  const value = useMemo<ChatStoreValue>(
    () => ({
      connectionState,
      getSession,
      logout,
      openChat,
      refreshRoster: loadRoster,
      roster,
      rosterError,
      rosterLoading,
      sendMessage,
    }),
    [connectionState, getSession, logout, openChat, loadRoster, roster, rosterError, rosterLoading, sendMessage],
  );

  return <ChatStoreContext.Provider value={value}>{children}</ChatStoreContext.Provider>;
}

function replaceAt<T>(arr: T[], index: number, value: T): T[] {
  const next = arr.slice();
  next[index] = value;

  return next;
}

export function useChatStore(): ChatStoreValue {
  const ctx = useContext(ChatStoreContext);

  if (!ctx) {throw new Error("useChatStore must be used within ChatStoreProvider");}

  return ctx;
}
