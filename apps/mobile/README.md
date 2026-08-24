# Hermes Mobile

A mobile-first PWA front end for Hermes Agent — Home selector (You / Claude
/ Hermes), the Hermes company/team/person picker, and a phone-messaging-style
chat surface. Lives at `apps/mobile` alongside `apps/desktop` and `web`.

This is a v1 slice: navigation, roster, resume-last-session, streaming chat
UI, and the transport seam to a real backend are implemented; a production
login flow for the gateway transport is explicitly out of scope (see
"Known limitations" below).

## Run it locally

```sh
cd apps/mobile
npm install            # or: npm install --workspace=apps/mobile from repo root
npm run dev             # http://127.0.0.1:5175, mock transport by default
```

In dev the app talks to `MockTransport` (`src/transport/mock-transport.ts`)
by default — no running Hermes backend required. This is what makes the
three screens fully explorable (roster, resume, streaming replies) with zero
setup.

To exercise the real backend instead:

```sh
# terminal 1 — a Hermes dashboard process on the default port
hermes dashboard

# terminal 2
cd apps/mobile
VITE_HERMES_TRANSPORT=gateway npm run dev
```

## Commands

| Command | What it does |
| --- | --- |
| `npm run dev` | Vite dev server (mock transport by default) |
| `npm run build` | `tsc -b && vite build` → `dist/` |
| `npm run typecheck` | Project-wide `tsc --noEmit` |
| `npm run test` | `vitest run` (jsdom + Testing Library) |
| `npm run lint` / `lint:fix` | Shared repo ESLint flat config |
| `npm run check` | typecheck + test + lint (CI gate) |

## Architecture

```
apps/mobile/src/
  App.tsx                 route map: / -> /hermes -> /chat/:kind/:key
  screens/
    HomeSelector.tsx       HOME SELECTOR — You / Claude / Hermes
    HermesPicker.tsx       HERMES PICKER — 2-col roster, recency-sorted
    ChatScreen.tsx         CHAT — bubbles, composer, streaming state
  store/
    chat-store.tsx         React context: roster + per-target session cache
  transport/
    types.ts               HermesTransport contract (the ONLY backend seam)
    mock-transport.ts       fully in-memory, deterministic (default in dev)
    gateway-transport.ts    real backend: REST + /api/ws JSON-RPC over
                            @hermes/shared's JsonRpcGatewayClient
    index.ts                picks mock vs gateway (env-controlled)
  lib/
    roster.ts               tile ordering + Erika/Paris-presence contract
    time.ts                 relative-time formatting
```

### Transport abstraction

Every screen goes through `useChatStore()` (`src/store/chat-store.tsx`),
which itself only calls the `HermesTransport` interface
(`src/transport/types.ts`). Nothing else in the app imports
`@hermes/shared`'s WebSocket client or reaches for `fetch` directly — that
keeps the UI code identical whether it's running against the mock or the
real backend, and keeps the backend boundary in exactly one place for
review.

- **MockTransport**: deterministic seed roster (Erika, Paris, Orion,
  TripTracker, Logos, Nexora, The Glass Pepper) spanning minutes/hours/days/
  never-used recency, in-memory sessions, and a simulated streamed echo
  reply. Used by `npm run dev` and every `npm run test` run.
- **GatewayTransport**: talks to the *same* backend Desktop and the web
  dashboard use — `profiles.list`, `session.most_recent`, `session.resume`,
  `session.create`, and `prompt.submit` over the `/api/ws` JSON-RPC
  WebSocket (`@hermes/shared`'s `JsonRpcGatewayClient` +
  `buildHermesWebSocketUrl`), no new backend endpoints. No SSH, no Electron
  import, no `window.hermesDesktop` reference anywhere in this app
  (enforced by `src/electron-free.test.ts`, a source-scan guard).

### Roster / picker contract (`src/lib/roster.ts`)

- Backend roster (`profiles.list`) is used verbatim when present.
- Erika and Paris are *always* rendered as direct tiles — synthesized
  client-side if the backend roster happens to omit them, so the picker
  contract holds even against a partial/mock backend. A backend-provided
  Erika/Paris row is used as-is (no duplication) and still flagged
  `isDirect`.
- Sort: most-recently-used first (`lastSession.lastActive` descending);
  profiles with no session history sort after ones with history, then
  alphabetically for stability.

### Chat / lanes

"You" and "Claude" on the Home selector are the two fixed chat lanes named
in the product spec — there is no dedicated backend concept for a lane
today. This v1 slice maps each lane onto an ordinary Hermes profile of the
same name (`you`, `claude`) and reuses the exact `profiles.list` /
`session.create` / `session.resume` contract every company tile already
uses. See "Known limitations" below for what a first-class multi-provider
lane would need instead.

### Session caching / Back behavior

`ChatStoreProvider` caches an open session's transcript in memory per
`(kind, key)` target. Navigating Chat → Back → Chat again re-displays the
cached transcript instantly (ACCEPTANCE: "Back returns to the selector/
picker without losing the session") instead of re-fetching or losing state.

## Security notes

- **No secrets in the bundle.** The only credential-shaped value referenced
  anywhere in `src/` is a short-lived dashboard session token/ticket
  resolved at connect time (`GatewayTransport`'s `resolveAuthParam`) — never
  a provider API key, `.env` value, or SSH key. `npm run build`'s output is
  inspectable in `dist/assets/*.js`; nothing there embeds a long-lived
  secret because none is ever imported.
- **Security is enforced server-side, not in this UI.** Profile/company
  scoping (which sessions a chat can see) is whatever the backend's
  `profiles.list` / `session.resume` / `session.create` already enforce for
  every other Hermes surface — this app adds no new authorization logic and
  trusts none of its own. A compromised or modified mobile bundle cannot
  see more than the backend already allows the authenticated connection.
- **No SSH client, no Electron, no `window.hermesDesktop`.** Verified by
  `src/electron-free.test.ts`, which source-scans every non-test file under
  `src/` for those patterns as part of `npm run test`.
- **HTTPS/WSS-ready, not yet exposed.** `buildHermesWebSocketUrl` already
  upgrades to `wss:` whenever the page is served over `https:`
  (`apps/shared/src/websocket-url.ts`); this task does not deploy a public
  endpoint or touch Caddy/shared services, per its own constraints.

## Known limitations (by design, for this slice)

1. **Gateway auth is a dev seam, not a login flow.** `GatewayTransport`'s
   default `resolveAuthParam` reads `VITE_HERMES_DEV_TOKEN` — a stand-in for
   the same short-lived dashboard session token/ticket Desktop's dev proxy
   already injects locally. **Exact backend change needed for a real
   HTTPS/WSS-gated remote deployment:** either (a) a mobile-appropriate
   OAuth/session login flow analogous to the dashboard's gated-mode cookie
   auth (`hermes_cli/web_server.py`'s auth gate + `/api/auth/ws-ticket`),
   reachable from a pure-PWA client with no Electron-side OAuth listener, or
   (b) a documented device-pairing flow (QR/code) that mints a scoped,
   revocable token for the mobile client specifically. Until one of those
   ships, this transport must only be pointed at a trusted
   loopback/VPN-internal backend a user has already authenticated to by
   other means — never a public endpoint. This task deliberately stops
   here rather than weakening auth to make it "work."
2. **You/Claude lanes are profile-shaped, not provider-shaped.** They reuse
   the profile contract (see above) rather than a first-class
   "OpenAI-facing vs Claude-facing" backend concept, because no such concept
   exists yet. A real implementation would want a backend-declared lane
   registry instead of a naming convention.
3. **Attachments are a stub.** The composer's paperclip button is disabled
   with a tooltip explaining why (ACCEPTANCE explicitly allows a stub for
   v1).
4. **Service worker / installability.** `public/manifest.webmanifest` and
   the theme-color meta tag are in place (PWA-first per the product spec),
   but no service worker/offline cache is implemented in this slice —
   installability via the manifest works today; offline support does not.

## Tests

`npm run test` (vitest + jsdom + Testing Library) covers:

- Home selector: exactly the three You/Claude/Hermes choices, navigation to
  each destination.
- Hermes picker: two-column grid, direct Erika/Paris tiles present even when
  synthesized, recency ordering.
- Resume-last-session: a profile with history resumes its transcript
  deterministically; a profile with none starts a fresh empty chat instead
  of erroring.
- Chat is a messaging surface (bubbles + composer), not a terminal.
- Back preserves the session cache (no reload flash).
- `MockTransport` unit tests: roster shape, deterministic resume-or-create,
  streamed send/reply.
- `lib/roster.ts` and `lib/time.ts` unit tests.
- `electron-free.test.ts`: source-level guard against Electron /
  `window.hermesDesktop` / SSH creeping into `src/`.
