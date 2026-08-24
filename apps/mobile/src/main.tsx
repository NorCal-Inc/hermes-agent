import "./index.css";

import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";

import { App } from "./App";

// Hermes Mobile is a standalone PWA app (apps/mobile) — it never imports
// Electron or reaches for the desktop capability bridge global. All backend
// access goes
// through the transport abstraction in src/transport (REST + /api/ws JSON
// RPC via @hermes/shared), same boundary Desktop and the web dashboard use.
createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);
