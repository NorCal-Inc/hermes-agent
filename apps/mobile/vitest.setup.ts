import { configure } from "@testing-library/react";

// Node 22+ defines its own `localStorage` accessor on the global object,
// which shadows jsdom's Storage in the jsdom test environment (see
// apps/desktop/vitest.setup.ts for the identical issue). Not currently used
// by apps/mobile, but guarded the same way so any future persisted-state
// code doesn't hit "Cannot read properties of undefined" surprises.
if (typeof (globalThis as { localStorage?: unknown }).localStorage === "undefined") {
  const store = new Map<string, string>();

  const storage: Storage = {
    clear: () => store.clear(),
    getItem: (k: string) => store.get(String(k)) ?? null,
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
    removeItem: (k: string) => void store.delete(String(k)),
    setItem: (k: string, v: string) => void store.set(String(k), String(v)),
  };

  for (const target of [globalThis, (globalThis as { window?: unknown }).window].filter(Boolean)) {
    Object.defineProperty(target, "localStorage", { configurable: true, value: storage, writable: true });
  }
}

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

configure({ asyncUtilTimeout: 5000 });
