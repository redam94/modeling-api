/**
 * Render the whole app (workspace gate + router) at a URL against MSW, as the browser would in
 * mock mode. Component tests of pages that need the signed-in workspace use this.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render } from "@testing-library/react";

import { TooltipProvider } from "@/components/ui/overlays";
import { WorkspaceGate } from "@/features/auth/gate";
import { MOCK_SESSION_KEY } from "@/mocks/tenancy";
import { routeTree } from "@/router";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

export type Persona = "ana" | "rui" | "vic" | "signed-out";

export function renderApp(url: string, persona: Persona = "ana") {
  localStorage.setItem(MOCK_SESSION_KEY, persona);
  useAuth.setState({ mode: "unknown", expired: false, dismissed: false });
  useScope.setState({ active: null, preferred: null, recent: [] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [url] }), context: { queryClient } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WorkspaceGate>
          <RouterProvider router={router} />
        </WorkspaceGate>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { router, queryClient, ...view };
}
