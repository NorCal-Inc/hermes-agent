import { type FormEvent, useEffect, useState } from "react";

import type { AuthProviderInfo, HermesTransport } from "@/transport";

interface LoginScreenProps {
  transport: HermesTransport;
  onSignedIn: () => void;
}

/**
 * Full-page sign-in surface shown by AuthGate whenever the transport
 * reports "unauthenticated" — cold load against a gated backend with no
 * session, or a session that expired mid-use. Wired to the SAME
 * cookie/ws-ticket dashboard auth the web dashboard uses
 * (hermes_cli/dashboard_auth): a password provider POSTs credentials via
 * transport.loginWithPassword; an OAuth provider is a plain top-level
 * browser navigation (transport.oauthLoginUrl) — no popup, no embedded
 * webview, no Electron loopback listener.
 */
export function LoginScreen({ transport, onSignedIn }: LoginScreenProps) {
  const [providers, setProviders] = useState<AuthProviderInfo[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    transport
      .listAuthProviders()
      .then((list) => {
        if (!cancelled) {setProviders(list);}
      })
      .catch((err: unknown) => {
        if (!cancelled) {setLoadError(err instanceof Error ? err.message : String(err));}
      });

    return () => {
      cancelled = true;
    };
  }, [transport]);

  const passwordProvider = providers?.find((p) => p.supportsPassword) ?? null;
  const oauthProviders = providers?.filter((p) => !p.supportsPassword) ?? [];

  const handlePasswordSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();

    if (!passwordProvider || submitting) {return;}
    setSubmitError(null);
    setSubmitting(true);
    transport
      .loginWithPassword(passwordProvider.name, username, password)
      .then(() => {
        setPassword("");
        onSignedIn();
      })
      .catch((err: unknown) => {
        setSubmitError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setSubmitting(false));
  };

  return (
    <div className="flex h-full flex-col items-center justify-center gap-6 px-6 py-10">
      <header className="text-center">
        <h1 className="text-2xl font-semibold tracking-tight text-neutral-50">Sign in to Hermes</h1>
        <p className="mt-1 text-sm text-neutral-500">Your session expired or this device hasn't signed in yet.</p>
      </header>

      {loadError && (
        <p className="max-w-sm text-center text-sm text-amber-300" role="alert">
          {loadError}
        </p>
      )}

      {providers && providers.length === 0 && !loadError && (
        <p className="max-w-sm text-center text-sm text-amber-300" role="alert">
          No sign-in method is configured on this Hermes backend.
        </p>
      )}

      {passwordProvider && (
        <form className="flex w-full max-w-sm flex-col gap-3" onSubmit={handlePasswordSubmit}>
          <label className="flex flex-col gap-1 text-sm text-neutral-400">
            Username
            <input
              autoCapitalize="none"
              autoComplete="username"
              className="rounded-xl border border-neutral-800 bg-neutral-900 px-3.5 py-2.5 text-sm text-neutral-100 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              onChange={(e) => setUsername(e.target.value)}
              value={username}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-neutral-400">
            Password
            <input
              autoComplete="current-password"
              className="rounded-xl border border-neutral-800 bg-neutral-900 px-3.5 py-2.5 text-sm text-neutral-100 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              onChange={(e) => setPassword(e.target.value)}
              type="password"
              value={password}
            />
          </label>
          {submitError && (
            <p className="text-sm text-red-400" role="alert">
              {submitError}
            </p>
          )}
          <button
            className="mt-1 rounded-xl bg-indigo-600 px-4 py-2.5 text-sm font-medium text-white disabled:bg-neutral-800 disabled:text-neutral-600"
            disabled={submitting || !username || !password}
            type="submit"
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>
      )}

      {oauthProviders.length > 0 && (
        <div className="flex w-full max-w-sm flex-col gap-2">
          {oauthProviders.map((provider) => (
            <a
              className="rounded-xl border border-neutral-800 bg-neutral-900 px-4 py-2.5 text-center text-sm font-medium text-neutral-100 active:bg-neutral-800"
              href={transport.oauthLoginUrl(provider.name)}
              key={provider.name}
            >
              Sign in with {provider.displayName}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
