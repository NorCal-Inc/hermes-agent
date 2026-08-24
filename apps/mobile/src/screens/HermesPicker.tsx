import { useNavigate } from "react-router";

import { AlertTriangle, ArrowLeft, LogOut } from "@/icons";
import type { RosterTile } from "@/lib/roster";
import { timeAgo } from "@/lib/time";
import { useChatStore } from "@/store/chat-store";

/** HERMES PICKER — two-column grid, most-recently-used first, Erika/Paris
 *  always present as direct tiles. */
export function HermesPicker() {
  const navigate = useNavigate();
  const { roster, rosterLoading, rosterError, refreshRoster, openChat, connectionState, logout } = useChatStore();

  const openProfile = (tile: RosterTile) => {
    void openChat({ key: tile.name, kind: "profile", label: tile.displayName }).then((cacheKey) => {
      navigate(`/chat/profile/${encodeURIComponent(tile.name)}`, { state: { cacheKey } });
    });
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-neutral-800 px-4 py-3">
        <button
          aria-label="Back"
          className="flex h-9 w-9 items-center justify-center rounded-full text-neutral-300 active:bg-neutral-800"
          onClick={() => navigate("/")}
          type="button"
        >
          <ArrowLeft size={20} />
        </button>
        <div className="flex-1">
          <h1 className="text-base font-semibold text-neutral-50">Hermes</h1>
          <p className="text-xs text-neutral-500">
            {connectionState === "connected" ? "Connected" : connectionState === "connecting" ? "Connecting…" : connectionState}
          </p>
        </div>
        <button
          aria-label="Sign out"
          className="flex h-9 w-9 items-center justify-center rounded-full text-neutral-300 active:bg-neutral-800"
          onClick={() => void logout()}
          type="button"
        >
          <LogOut size={18} />
        </button>
      </header>

      {rosterError && (
        <div className="mx-4 mt-3 flex items-center gap-2 rounded-xl border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
          <AlertTriangle size={16} />
          <span className="flex-1">{rosterError}</span>
          <button className="underline" onClick={refreshRoster} type="button">
            Retry
          </button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {rosterLoading && roster.length === 0 ? (
          <p className="px-1 py-6 text-center text-sm text-neutral-500">Loading roster…</p>
        ) : (
          <div aria-label="Hermes roster" className="grid grid-cols-2 gap-3" role="list">
            {roster.map((tile) => (
              <button
                className="flex min-h-[104px] flex-col justify-between rounded-2xl border border-neutral-800 bg-neutral-900/70 p-3 text-left transition active:scale-[0.97] active:bg-neutral-800"
                key={tile.name}
                onClick={() => openProfile(tile)}
                type="button"
              >
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="truncate text-sm font-medium text-neutral-50">{tile.displayName}</span>
                    {tile.isDirect && (
                      <span className="rounded-full bg-indigo-500/20 px-1.5 py-0.5 text-[0.6rem] font-medium uppercase tracking-wide text-indigo-300">
                        direct
                      </span>
                    )}
                  </div>
                  {tile.description && <p className="mt-0.5 truncate text-xs text-neutral-500">{tile.description}</p>}
                </div>
                <p className="truncate text-[0.7rem] text-neutral-500">
                  {tile.lastSession ? timeAgo(tile.lastSession.lastActive) : "No conversations yet"}
                </p>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
