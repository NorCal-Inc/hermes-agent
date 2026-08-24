import { Route, Routes } from "react-router";

import { ChatScreen } from "@/screens/ChatScreen";
import { HermesPicker } from "@/screens/HermesPicker";
import { HomeSelector } from "@/screens/HomeSelector";
import { ChatStoreProvider } from "@/store/chat-store";

/** Top-level route map: Home selector -> Hermes picker -> Chat. You/Claude
 *  skip the picker and go straight to /chat/lane/:lane. */
export function App() {
  return (
    <ChatStoreProvider>
      <div className="flex h-dvh w-full flex-col overflow-hidden bg-neutral-950 text-neutral-100">
        <Routes>
          <Route element={<HomeSelector />} path="/" />
          <Route element={<HermesPicker />} path="/hermes" />
          <Route element={<ChatScreen />} path="/chat/:kind/:key" />
        </Routes>
      </div>
    </ChatStoreProvider>
  );
}
