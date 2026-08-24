import { Route, Routes } from "react-router";

import { ChatScreen } from "@/screens/ChatScreen";
import { HermesPicker } from "@/screens/HermesPicker";
import { HomeSelector } from "@/screens/HomeSelector";
import { AuthGate } from "@/store/auth-gate";
import { ChatStoreProvider } from "@/store/chat-store";

/** Top-level route map: Home selector -> Hermes picker -> Chat. You/Claude
 *  skip the picker and go straight to /chat/lane/:lane. AuthGate sits above
 *  ChatStoreProvider so roster/session/WS calls never fire while the
 *  transport is unauthenticated — see store/auth-gate.tsx. */
export function App() {
  return (
    <div className="flex h-dvh w-full flex-col overflow-hidden bg-neutral-950 text-neutral-100">
      <AuthGate>
        <ChatStoreProvider>
          <Routes>
            <Route element={<HomeSelector />} path="/" />
            <Route element={<HermesPicker />} path="/hermes" />
            <Route element={<ChatScreen />} path="/chat/:kind/:key" />
          </Routes>
        </ChatStoreProvider>
      </AuthGate>
    </div>
  );
}
