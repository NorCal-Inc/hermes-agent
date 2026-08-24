# Hermes Mobile

A mobile-first PWA front end for Hermes Agent — Home selector (You / Claude
/ Hermes), the Hermes company/team/person picker, and a phone-messaging-style
chat surface. Lives at `apps/mobile` alongside `apps/desktop` and `web`.

This is a v1 slice: navigation, roster, resume-last-session, streaming chat
UI, and the transport seam to a real backend are implemented, including a
production login flow for the gateway transport (see "Auth" below) that
reuses the existing Hermes dashboard's cookie/ws-ticket auth as-is — no new
backend auth system.

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

Against a **gated** dashboard (`auth_required=true`) this hits the real
login screen — sign in with whatever provider the dashboard has configured
(username/password or OAuth), same as `web/`. Against an **ungated**
loopback dashboard (the common local-dev case, no auth provider
configured) there's no session to cookie-auth with, so the dev proxy needs
a stand-in credential: set `VITE_HERMES_DEV_TOKEN` to that dashboard
process's legacy `_SESSION_TOKEN` (see `hermes_cli/web_server.py` for how
that loopback token is issued). This escape hatch only exists in
`vite dev` (`import.meta.env.DEV`) — a production build (`npm run build`)
throws instead of silently connecting to an ungated backend, and the
token/env var never reaches `dist/` (verified by inspecting the built
bundle).

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
    LoginScreen.tsx        SIGN IN — shown by AuthGate when unauthenticated
    HomeSelector.tsx       HOME SELECTOR — You / Claude / Hermes
    HermesPicker.tsx       HERMES PICKER — 2-col roster, recency-sorted
    ChatScreen.tsx         CHAT — bubbles, composer, streaming state
  store/
    auth-gate.tsx           gates ChatStoreProvider on transport auth state
    chat-store.tsx         React context: roster + per-target session cache
  transport/
    types.ts               HermesTransport contract (the ONLY backend seam)
    mock-transport.ts       fully in-memory, deterministic (default in dev)
    gateway-transport.ts    real backend: REST + /api/ws JSON-RPC over
                            @hermes/shared's JsonRpcGatewayClient, plus the
                            cookie/ws-ticket dashboard-auth flow
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

### Auth

`GatewayTransport` reuses the **exact** same-origin cookie/ws-ticket auth
the web dashboard (`web/`) already uses — `hermes_cli/dashboard_auth`'s
`/login`, `/auth/login`, `/auth/callback`, `/auth/password-login`,
`/auth/logout`, `/api/auth/me`, `/api/auth/ws-ticket` routes. No new
backend auth route was added for this task.

- **Cookie auth for REST, single-use ticket for the WS upgrade.** A
  successful login sets a `Secure`/`HttpOnly`/`SameSite=Lax` session cookie
  (browser-managed, never read by this app's JS). Browsers can't set
  `Authorization` on a WebSocket upgrade, so before every `connect()` (and
  every reconnect) `GatewayTransport` mints a fresh single-use, 30s-TTL
  `?ticket=` via `POST /api/auth/ws-ticket` — same pattern as
  `web/src/lib/api.ts`'s `getWsTicket`/`buildWsAuthParam`, never cached or
  reused across connects.
- **`AuthGate` (`src/store/auth-gate.tsx`)** sits above `ChatStoreProvider`
  and swaps in `LoginScreen` whenever `transport.connectionState()` reports
  `"unauthenticated"` — set on a `401` from the ws-ticket mint, or a WS
  close with code `4401` (the gateway's `_ws_auth_ok` rejection code) mid
  session. Nothing under `AuthGate` (roster fetch, WS connect) runs while
  unauthenticated, and remounts fresh the moment a login succeeds.
- **`LoginScreen`** renders whatever `GET /api/auth/providers` advertises —
  a username/password form for a `supports_password` provider (POSTs to
  `/auth/password-login`), or a plain same-origin link to
  `/auth/login?provider=…` for an OAuth provider. The OAuth case is an
  ordinary top-level browser navigation — no popup, no embedded webview, no
  Electron loopback listener (that RFC 8252 native-app flow is Desktop-only;
  see `hermes_cli/dashboard_auth/native_flow.py` and
  `apps/desktop/electron/main.ts`'s OS-keychain token storage — neither is
  safe or appropriate for a browser, which has no keychain and would have
  to fall back to XSS-readable storage).
- **Logout** is a small icon button in the Hermes picker header
  (`HermesPicker.tsx`) — `POST /auth/logout` (clears cookies server-side)
  then transitions the transport back to `"unauthenticated"`, which
  `AuthGate` picks up the same way as any other session expiry.
- **Server-side authorization only.** Identity gating (is this a real
  logged-in session) is all the dashboard-auth layer does; which
  profiles/companies a session may open is whatever `profiles.list` /
  `session.resume` / `session.create` already enforce for every other
  Hermes surface (web dashboard included) — this app adds no authorization
  logic of its own and trusts none.

## Security notes

- **No secrets in the bundle.** The only credential-shaped values in
  `src/` are (a) a short-lived, single-use WS ticket minted at connect
  time via `POST /api/auth/ws-ticket`, and (b) in a `vite dev` build only,
  `VITE_HERMES_DEV_TOKEN` — never a provider API key, `.env` value, or SSH
  key. `import.meta.env.DEV` is a compile-time constant Vite inlines to
  `false` in `vite build`, so the dev-token branch (and the literal string
  `VITE_HERMES_DEV_TOKEN`) is dead-code-eliminated from `dist/assets/*.js`
  — verified by grepping the built bundle, not just "unused at runtime".
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
  endpoint or touch Caddy/shared services, per its own constraints. A real
  deployment must serve this app **same-origin** with the gated dashboard
  so the session cookie applies — see "Known limitations" below.

## Known limitations (by design, for this slice)

1. **Not yet deployed.** Auth is wired to the real gated dashboard flow
   (see "Auth" above) and verified against the backend's own test suite,
   but this task deliberately does not deploy anything or touch Caddy,
   systemd, DNS, or the firewall. A real deployment needs this app served
   **same-origin** with a gated Hermes backend (so the session cookie
   applies) — e.g. built into the dashboard's static asset mount, or
   reverse-proxied under the same host — and that wiring is out of scope
   here.
2. **No deep-link-back-to-location after OAuth login.** A successful
   `/auth/callback` always lands on `/` (the Home selector) rather than
   restoring whatever screen the user was on before their session expired
   (`web/`'s `sessionStorage["hermes.lastLocation"]` pattern). Acceptable
   for v1 since Home selector is the app's own landing screen anyway.
3. **You/Claude lanes are profile-shaped, not provider-shaped.** They reuse
   the profile contract (see above) rather than a first-class
   "OpenAI-facing vs Claude-facing" backend concept, because no such concept
   exists yet. A real implementation would want a backend-declared lane
   registry instead of a naming convention.
4. **Attachments are a stub.** The composer's paperclip button is disabled
   with a tooltip explaining why (ACCEPTANCE explicitly allows a stub for
   v1).
5. **Service worker / installability.** `public/manifest.webmanifest` and
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
- `GatewayTransport` auth unit tests (`gateway-transport.test.ts`): fresh
  ticket minted per connect, a 401 ws-ticket mint and a WS close code 4401
  both surface as `"unauthenticated"` (not a generic error), the DEV-only
  token fallback, `listAuthProviders`/`loginWithPassword`/`oauthLoginUrl`/
  `logout` request shapes.
- `LoginScreen` unit tests: password-provider submit/error, OAuth provider
  rendered as a same-origin link (not a popup), the zero-providers
  configuration-error state.
- `AuthGate` unit tests: children render while authenticated, swap to
  `LoginScreen` the moment the transport reports `"unauthenticated"`.
- `lib/roster.ts` and `lib/time.ts` unit tests.
- `electron-free.test.ts`: source-level guard against Electron /
  `window.hermesDesktop` / SSH creeping into `src/`.
