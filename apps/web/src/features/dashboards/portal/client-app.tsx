/**
 * The client portal (plan D15): what a signed-in `client` sees instead of the app. The workspace
 * gate loads this chunk when the selected project's role is `client`; it brings its own router
 * (`client-router.tsx`) and frame, and no navigation, command palette or keyboard shortcuts.
 */
import { RouterProvider, type RouterHistory } from "@tanstack/react-router";
import { useState } from "react";

import { createClientRouter } from "./client-router";

export function ClientApp({ history }: { history?: RouterHistory | undefined }) {
  const [router] = useState(() => createClientRouter(history));
  return <RouterProvider router={router} />;
}

export default ClientApp;
