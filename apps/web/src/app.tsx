import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { lazy, Suspense, useState } from "react";

import { ApiError } from "./api/client";
import { TooltipProvider } from "./components/ui/overlays";
import { WorkspaceGate } from "./features/auth/gate";
import { publicPathOf } from "./features/trust/public-paths";
import { createAppRouter } from "./router";

// Sign-up and the e-mail link's landing page work without a session (their own chunk).
const PublicApp = lazy(() => import("./features/trust/public-pages").then((m) => ({ default: m.PublicApp })));

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        // Never retry client errors (4xx): they will not get better.
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      },
      mutations: { retry: false },
    },
  });
}

export function App({ queryClient }: { queryClient: QueryClient }) {
  const [router] = useState(() => createAppRouter(queryClient));
  const [publicPath] = useState(() => (typeof window === "undefined" ? null : publicPathOf(window.location.pathname)));
  if (publicPath) {
    return (
      <Suspense fallback={null}>
        <PublicApp path={publicPath} />
      </Suspense>
    );
  }
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={300}>
        <WorkspaceGate>
          <RouterProvider router={router} />
        </WorkspaceGate>
      </TooltipProvider>
    </QueryClientProvider>
  );
}
