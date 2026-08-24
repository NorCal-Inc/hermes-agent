/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Force a transport: "mock" | "gateway". Unset = mock in dev, gateway in prod. */
  readonly VITE_HERMES_TRANSPORT?: string
  /** Dev-only: base dashboard URL the mock/gateway transports talk to. */
  readonly VITE_HERMES_DASHBOARD_URL?: string
  /** Dev-only escape hatch: a pre-minted session token/ticket for the gateway
   *  transport when no login flow is wired yet. Never a provider secret. */
  readonly VITE_HERMES_DEV_TOKEN?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
