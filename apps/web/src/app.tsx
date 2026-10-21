import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, Suspense, useState } from "react";

import { sharePathOf } from "./features/share/share-paths";
import { publicPathOf } from "./features/trust/public-paths";

/*
 * The entry decides which app a path gets and loads each one as its own chunk, so the entry
 * itself imports no app code: a share link (`/s#…`) never downloads the signed-in app's router,
 * session middleware or query hooks (the share chunk's budget in `vite.config.ts` checks this).
 */

// A share link's dashboard (plan D13/D15): no session, its own query client and fetch client.
const ShareApp = lazy(() => import("./features/share/share-app").then((m) => ({ default: m.ShareApp })));

// Sign-up and the e-mail link's landing page work without a session (their own chunk).
const PublicApp = lazy(() => import("./features/trust/public-pages").then((m) => ({ default: m.PublicApp })));

// The signed-in app: the workspace gate and the router (the gate loads the client portal itself).
const SignedInApp = lazy(async () => {
  const [{ RouterProvider }, { TooltipProvider }, { WorkspaceGate }, { createAppRouter }] = await Promise.all([
    import("@tanstack/react-router"),
    import("./components/ui/overlays"),
    import("./features/auth/gate"),
    import("./router"),
  ]);
  function SignedIn({ queryClient }: { queryClient: QueryClient }) {
    const [router] = useState(() => createAppRouter(queryClient));
    return (
      <TooltipProvider delayDuration={300}>
        <WorkspaceGate>
          <RouterProvider router={router} />
        </WorkspaceGate>
      </TooltipProvider>
    );
  }
  return { default: SignedIn };
});

/** The HTTP status of an API error (`ApiError` and the share client's errors both carry one). */
function statusOf(err: unknown): number | null {
  if (!(err instanceof Error)) return null;
  const status = (err as Error & { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        refetchOnWindowFocus: true,
        // Never retry client errors (4xx): they will not get better.
        retry: (count, err) => {
          const status = statusOf(err);
          return !(status !== null && status < 500) && count < 2;
        },
      },
      mutations: { retry: false },
    },
  });
}

type Surface = { kind: "share" } | { kind: "public"; path: NonNullable<ReturnType<typeof publicPathOf>> } | { kind: "app" };

function surfaceOf(pathname: string | null): Surface {
  if (pathname === null) return { kind: "app" };
  // The share page comes first: it is the only surface that must never touch the session.
  if (sharePathOf(pathname)) return { kind: "share" };
  const path = publicPathOf(pathname);
  return path ? { kind: "public", path } : { kind: "app" };
}

export function App({ queryClient }: { queryClient: QueryClient }) {
  const [surface] = useState(() => surfaceOf(typeof window === "undefined" ? null : window.location.pathname));
  if (surface.kind === "share") {
    return (
      <Suspense fallback={null}>
        <ShareApp />
      </Suspense>
    );
  }
  if (surface.kind === "public") {
    return (
      <Suspense fallback={null}>
        <PublicApp path={surface.path} />
      </Suspense>
    );
  }
  return (
    <QueryClientProvider client={queryClient}>
      <Suspense fallback={null}>
        <SignedInApp queryClient={queryClient} />
      </Suspense>
    </QueryClientProvider>
  );
}
