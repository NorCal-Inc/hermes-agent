import path from 'path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const BACKEND = process.env.HERMES_DASHBOARD_URL ?? 'http://127.0.0.1:9119'

// Hermes Mobile is a standalone PWA workspace (apps/mobile). It talks to the
// SAME Hermes backend as Desktop/Dashboard/TUI over REST + the /api/ws
// JSON-RPC WebSocket (see apps/shared/src/json-rpc-gateway.ts) — it does not
// run its own server. In dev, /api and the ws upgrade are proxied to the
// local dashboard process so the mock/live transport split (src/transport)
// can talk to a real backend without CORS friction.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
      '@hermes/shared': path.resolve(import.meta.dirname, '../shared/src')
    },
    dedupe: ['react', 'react-dom']
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true
  },
  server: {
    port: 5175,
    proxy: {
      '/api': {
        target: BACKEND,
        ws: true
      }
    }
  }
})
