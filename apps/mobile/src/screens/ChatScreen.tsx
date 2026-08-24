import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router";

import { ArrowLeft, Paperclip, Send } from "@/icons";
import { useChatStore } from "@/store/chat-store";
import type { ChatTarget } from "@/store/chat-store";

const LANE_LABELS: Record<string, string> = { claude: "Claude", you: "You" };

/** CHAT — messaging-app surface: header names the target, history loads the
 *  last session, bubbles + bottom composer + streaming indicator, back
 *  preserves the session (it stays cached in ChatStoreProvider). */
export function ChatScreen() {
  const params = useParams<{ kind: string; key: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const { openChat, getSession, sendMessage, roster } = useChatStore();
  const [draft, setDraft] = useState("");

  const [cacheKey, setCacheKey] = useState<null | string>(
    (location.state as { cacheKey?: string } | null)?.cacheKey ?? null,
  );

  const scrollRef = useRef<HTMLDivElement>(null);

  const kind = params.kind === "lane" ? "lane" : "profile";
  const key = params.key ?? "";

  const label = useMemo(() => {
    if (kind === "lane") {return LANE_LABELS[key] ?? key;}

    return roster.find((r) => r.name === key)?.displayName ?? key;
  }, [kind, key, roster]);

  useEffect(() => {
    if (cacheKey) {return;}
    const target: ChatTarget = { key, kind, label };
    void openChat(target).then(setCacheKey);
  }, [cacheKey, key, kind, label, openChat]);

  const session = cacheKey ? getSession(cacheKey) : undefined;
  const lastMessageContent = session?.messages.at(-1)?.content;

  useEffect(() => {
    scrollRef.current?.scrollTo?.({ top: scrollRef.current.scrollHeight });
  }, [session?.messages.length, lastMessageContent]);

  const handleSend = () => {
    if (!cacheKey || !draft.trim()) {return;}
    const text = draft;
    setDraft("");
    void sendMessage(cacheKey, text);
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
        <button
          aria-label="Back"
          className="flex h-9 w-9 items-center justify-center rounded-full text-neutral-300 active:bg-neutral-800"
          onClick={() => navigate(kind === "lane" ? "/" : "/hermes")}
          type="button"
        >
          <ArrowLeft size={20} />
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-semibold text-neutral-50">{label || "Chat"}</h1>
          <p className="text-xs text-neutral-500">
            {session?.streaming ? "typing…" : session ? "Active" : "Loading…"}
          </p>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4" ref={scrollRef}>
        {!session ? (
          <p className="py-8 text-center text-sm text-neutral-500">Loading conversation…</p>
        ) : session.messages.length === 0 ? (
          <p className="py-8 text-center text-sm text-neutral-500">Say hello to start the conversation.</p>
        ) : (
          <ul aria-label="Messages" className="flex flex-col gap-2">
            {session.messages.map((message) => (
              <li
                className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
                key={message.id}
              >
                <div
                  className={`max-w-[80%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${
                    message.role === "user"
                      ? "rounded-br-sm bg-indigo-600 text-white"
                      : "rounded-bl-sm bg-neutral-800 text-neutral-100"
                  } ${message.pending ? "opacity-80" : ""}`}
                >
                  {message.content || (message.pending ? "…" : "")}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <form
        className="flex items-end gap-2 border-t border-neutral-800 px-3 py-3"
        onSubmit={(e) => {
          e.preventDefault();
          handleSend();
        }}
      >
        <button
          aria-label="Attach a file (coming soon)"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-neutral-600"
          disabled
          title="Attachments are not part of this v1 slice"
          type="button"
        >
          <Paperclip size={18} />
        </button>
        <textarea
          aria-label="Message composer"
          className="max-h-32 flex-1 resize-none rounded-2xl border border-neutral-800 bg-neutral-900 px-3.5 py-2.5 text-sm text-neutral-100 placeholder:text-neutral-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          placeholder="Message"
          rows={1}
          value={draft}
        />
        <button
          aria-label="Send"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-indigo-600 text-white disabled:bg-neutral-800 disabled:text-neutral-600"
          disabled={!draft.trim() || !cacheKey}
          type="submit"
        >
          <Send size={16} />
        </button>
      </form>
    </div>
  );
}
