import type {
  AuthProviderInfo,
  ChatMessage,
  HermesTransport,
  OpenSessionResult,
  RosterProfile,
  TransportConnectionState,
  TransportStreamEvent,
} from "./types";

interface SeedProfile {
  name: string;
  displayName: string;
  description: string;
  isDefault?: boolean;
  minutesAgo: null | number;
  preview: string;
  messages: ChatMessage[];
}

const HOUR = 60;
const DAY = 24 * HOUR;

function seedMessages(lines: [ChatRoleSeed, string][], baseAgoMinutes: number): ChatMessage[] {
  const now = Date.now();

  return lines.map(([role, content], i) => ({
    id: `seed-${i}`,
    role,
    content,
    timestamp: now - (baseAgoMinutes - i) * 60_000,
  }));
}

type ChatRoleSeed = ChatMessage["role"];

// Deterministic seed roster: recency spans minutes -> weeks -> "never used"
// so recent-sort and empty-state behaviour are both exercised without a
// live backend. Erika and Paris are seeded like any other profile — the
// roster-completeness fallback in lib/roster.ts only synthesizes them when
// a real backend roster omits them, which this mock deliberately does not.
const SEED_PROFILES: SeedProfile[] = [
  {
    name: "erika",
    displayName: "Erika",
    description: "Executive routing & governance",
    minutesAgo: 6,
    preview: "Kanban task t_31465b8c picked up — building the mobile slice now.",
    messages: seedMessages(
      [
        ["user", "Status on the mobile PWA task?"],
        ["assistant", "In progress — scaffolding the transport layer now."],
      ],
      6,
    ),
  },
  {
    name: "paris",
    displayName: "Paris",
    description: "Deep technical execution",
    minutesAgo: 40,
    preview: "Pushed the worktree branch, tests are green.",
    messages: seedMessages(
      [
        ["user", "Any blockers on the build?"],
        ["assistant", "None — typecheck and vitest both pass locally."],
      ],
      40,
    ),
  },
  {
    name: "orion",
    displayName: "Orion Formation Services",
    description: "Entity formation & filings",
    minutesAgo: 3 * HOUR,
    preview: "Filed the annual report, confirmation attached.",
    messages: seedMessages([["assistant", "Filed the annual report, confirmation attached."]], 3 * HOUR),
  },
  {
    name: "triptracker",
    displayName: "TripTracker",
    description: "Travel ops",
    minutesAgo: DAY,
    preview: "Stripe webhook verified end to end.",
    messages: seedMessages([["assistant", "Stripe webhook verified end to end."]], DAY),
  },
  {
    name: "logos",
    displayName: "Logos Covenant",
    description: "Ministry operations",
    minutesAgo: 5 * DAY,
    preview: "Weekly giving report is ready.",
    messages: seedMessages([["assistant", "Weekly giving report is ready."]], 5 * DAY),
  },
  {
    name: "nexora",
    displayName: "Nexora",
    description: "Product & biotech",
    minutesAgo: null,
    preview: "",
    messages: [],
  },
  {
    name: "glass-pepper",
    displayName: "The Glass Pepper",
    description: "Restaurant group",
    minutesAgo: null,
    preview: "",
    messages: [],
  },
];

/**
 * Fully in-memory transport: no network, deterministic clock math, a fake
 * streamed echo reply after every sent message. Default transport in dev
 * (see transport/index.ts) and the only transport unit tests exercise,
 * since it never depends on a running backend or minted credentials.
 */
export class MockTransport implements HermesTransport {
  readonly kind = "mock" as const;

  private state: TransportConnectionState = "idle";
  private readonly stateHandlers = new Set<(state: TransportConnectionState) => void>();
  private readonly streamHandlers = new Set<(event: TransportStreamEvent) => void>();
  private readonly sessions = new Map<string, ChatMessage[]>();
  private readonly lastSessionByProfile = new Map<string, string>();
  private sessionCounter = 0;

  constructor() {
    for (const seed of SEED_PROFILES) {
      if (seed.messages.length > 0) {
        const sessionId = `mock-session-${seed.name}`;
        this.sessions.set(sessionId, seed.messages);
        this.lastSessionByProfile.set(seed.name, sessionId);
      }
    }
  }

  async connect(): Promise<void> {
    this.setState("connecting");
    await new Promise((resolve) => setTimeout(resolve, 0));
    this.setState("connected");
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
    const now = Date.now();

    return SEED_PROFILES.map((seed) => ({
      name: seed.name,
      displayName: seed.displayName,
      description: seed.description,
      isDefault: seed.isDefault ?? false,
      lastSession:
        seed.minutesAgo === null
          ? null
          : {
              id: this.lastSessionByProfile.get(seed.name) ?? `mock-session-${seed.name}`,
              title: seed.displayName,
              preview: seed.preview,
              lastActive: Math.floor((now - seed.minutesAgo * 60_000) / 1000),
              messageCount: seed.messages.length,
            },
    }));
  }

  async openSession(profile: null | string, preferredSessionId: null | string): Promise<OpenSessionResult> {
    const key = profile ?? "__lane__";
    const existing = preferredSessionId ?? this.lastSessionByProfile.get(key) ?? null;

    if (existing && this.sessions.has(existing)) {
      return { messages: [...this.sessions.get(existing)!], resumed: true, sessionId: existing };
    }

    const sessionId = `mock-session-${key}-${++this.sessionCounter}`;
    this.sessions.set(sessionId, []);
    this.lastSessionByProfile.set(key, sessionId);

    return { messages: [], resumed: false, sessionId };
  }

  async sendMessage(sessionId: string, _profile: null | string, text: string): Promise<void> {
    const history = this.sessions.get(sessionId) ?? [];

    const userMessage: ChatMessage = {
      content: text,
      id: `msg-${history.length}-${Date.now()}`,
      role: "user",
      timestamp: Date.now(),
    };

    history.push(userMessage);
    this.sessions.set(sessionId, history);

    // Simulate a streamed assistant reply so the composer/typing-indicator
    // path is exercised without a live model.
    const replyId = `msg-${history.length}-${Date.now()}`;
    const replyText = `Got it — you said: "${text}"`;
    this.emit({ sessionId, status: "streaming", type: "status" });
    let sent = "";

    for (const chunk of replyText.split(" ")) {
      sent = sent ? `${sent} ${chunk}` : chunk;
      this.emit({ delta: sent, messageId: replyId, sessionId, type: "message.delta" });
       
      await new Promise((resolve) => setTimeout(resolve, 0));
    }

    const finalMessage: ChatMessage = {
      content: replyText,
      id: replyId,
      role: "assistant",
      timestamp: Date.now(),
    };

    history.push(finalMessage);
    this.sessions.set(sessionId, history);
    this.emit({ message: finalMessage, sessionId, type: "message.complete" });
    this.emit({ sessionId, status: "idle", type: "status" });
  }

  onStreamEvent(handler: (event: TransportStreamEvent) => void): () => void {
    this.streamHandlers.add(handler);

    return () => this.streamHandlers.delete(handler);
  }

  // Auth surface: the mock backend never gates — it's never used to test the
  // login screen, only to keep the app fully explorable with zero backend.
  async listAuthProviders(): Promise<AuthProviderInfo[]> {
    return [];
  }

  async loginWithPassword(): Promise<void> {
    // no-op — MockTransport's connectionState never becomes "unauthenticated".
  }

  oauthLoginUrl(): string {
    return "";
  }

  async logout(): Promise<void> {
    this.setState("idle");
  }

  private emit(event: TransportStreamEvent): void {
    for (const handler of this.streamHandlers) {handler(event);}
  }

  private setState(state: TransportConnectionState): void {
    this.state = state;

    for (const handler of this.stateHandlers) {handler(state);}
  }
}
