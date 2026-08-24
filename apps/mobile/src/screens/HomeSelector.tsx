import { useNavigate } from "react-router";

import { MessageSquare, Sparkles, Users } from "@/icons";

interface HomeChoice {
  key: "claude" | "hermes" | "you";
  label: string;
  subtitle: string;
  icon: typeof MessageSquare;
  to: string;
}

const CHOICES: HomeChoice[] = [
  { icon: MessageSquare, key: "you", label: "You", subtitle: "ChatGPT / OpenAI lane", to: "/chat/lane/you" },
  { icon: Sparkles, key: "claude", label: "Claude", subtitle: "Claude-facing lane", to: "/chat/lane/claude" },
  { icon: Users, key: "hermes", label: "Hermes", subtitle: "Companies, teams & agents", to: "/hermes" },
];

/** HOME SELECTOR — mobile-first, dark, exactly three large choices. */
export function HomeSelector() {
  const navigate = useNavigate();

  return (
    <div className="flex h-full flex-col justify-center gap-4 px-6 py-10">
      <header className="mb-6 text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-50">Hermes</h1>
        <p className="mt-1 text-sm text-neutral-500">Choose who to talk to</p>
      </header>

      <div className="flex flex-col gap-4" role="list">
        {CHOICES.map(({ key, label, subtitle, icon: Icon, to }) => (
          <div key={key} role="listitem">
            <button
              aria-label={label}
              className="flex w-full items-center gap-4 rounded-3xl border border-neutral-800 bg-neutral-900/70 px-6 py-6 text-left shadow-lg shadow-black/20 transition active:scale-[0.98] active:bg-neutral-800"
              onClick={() => navigate(to)}
              type="button"
            >
              <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-neutral-800 text-neutral-100">
                <Icon size={24} />
              </span>
              <span className="flex flex-col">
                <span className="text-lg font-medium text-neutral-50">{label}</span>
                <span className="text-sm text-neutral-500">{subtitle}</span>
              </span>
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
