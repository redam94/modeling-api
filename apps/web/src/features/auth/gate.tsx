/**
 * Decides what the app can show before any page renders:
 *
 * 1. `/auth/session` → no `/auth` (v0.2 server): the local single-tenant workspace.
 * 2. Signed out → the sign-in screen (the BFF runs the OIDC flow; we only redirect).
 * 3. Signed in → `/v1/me` → resolve the selected project (persisted preference, else the
 *    first one) → render the app with the workspace in context.
 *
 * Once the app has rendered, an ended session never tears it down: a dialog asks the user to
 * sign in again, so unsaved work on the page survives.
 *
 * A `client` in the selected project gets the client portal (its own lazy chunk and router)
 * instead of the app: the main router is never mounted for them (plan D15).
 */
import { useQueryClient } from "@tanstack/react-query";
import { LogIn, LogOut, RefreshCw } from "lucide-react";
import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { useMe, useSession } from "@/api/queries";
import { currentPath, logout, signIn } from "@/api/session";
import type { SessionInfo } from "@/api/types";
import signinArt from "@/assets/brand/signin.webp";
import { BrandArt } from "@/components/brand/brand-art";
import { Logo, Wordmark } from "@/components/brand/logo";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useApplyTheme } from "@/hooks/use-theme";
import { RECENTS_STORAGE_KEY } from "@/lib/recents-key";
import { useAuth } from "@/stores/auth";
import { scopeKey, useScope } from "@/stores/scope";

import { buildWorkspace, localMe, WorkspaceContext, type Workspace } from "./workspace";

// The client portal: only clients load it (and the full app's routes never load for them).
const ClientApp = lazy(() => import("@/features/dashboards/portal/client-app").then((m) => ({ default: m.ClientApp })));

export function WorkspaceGate({ children }: { children: ReactNode }) {
  useApplyTheme();
  const session = useSession();
  const expired = useAuth((s) => s.expired && !s.dismissed);
  const entered = useRef(false);
  const state = session.data;
  const sessionMode = state?.mode === "session";
  const info: SessionInfo | null = state?.mode === "session" ? state.info : null;
  const me = useMe(sessionMode && Boolean(info?.authenticated));
  const preferred = useScope((s) => s.preferred);
  const active = useScope((s) => s.active);
  const setActive = useScope((s) => s.setActive);

  // Fall back to the local workspace when there is no session API or it is unreachable
  // (the shell then shows the connection problem, as v0.2 did).
  const local = state?.mode === "none" || (session.isError && !sessionMode);
  const workspace = useMemo<Workspace | null>(() => {
    if (local) return buildWorkspace(localMe(), "none", preferred);
    if (me.data) return buildWorkspace(me.data, "session", preferred, info?.expires_at ?? null);
    return null;
  }, [local, me.data, preferred, info?.expires_at]);

  const resolved = workspace?.current ? { tenantId: workspace.current.project.tenant_id, projectId: workspace.current.project.project_id } : null;
  const resolvedKey = resolved ? scopeKey(resolved) : null;
  const activeKey = active ? scopeKey(active) : null;
  useLayoutEffect(() => {
    if (workspace && resolvedKey !== activeKey) setActive(resolved);
  });

  // v0.19 (D11): the catalog routes answer with the selected tenant's view (uploaded entries)
  // and their cache keys are global, so a tenant switch refetches them.
  const queryClient = useQueryClient();
  const activeTenant = active?.tenantId ?? null;
  const lastTenant = useRef(activeTenant);
  useEffect(() => {
    if (lastTenant.current === activeTenant) return;
    const switched = lastTenant.current !== null;
    lastTenant.current = activeTenant;
    if (switched) void queryClient.invalidateQueries({ queryKey: ["catalog"] });
  }, [activeTenant, queryClient]);

  // A session that ends after the app rendered: keep the page, ask to sign in again.
  const signedOutLater = sessionMode && info && !info.authenticated && entered.current;
  useEffect(() => {
    if (signedOutLater) useAuth.getState().markExpired();
  }, [signedOutLater]);

  if (workspace && resolvedKey === activeKey && (!sessionMode || info?.authenticated || entered.current)) {
    entered.current = true;
    return (
      <WorkspaceContext.Provider value={workspace}>
        {workspace.current?.role === "client" ? (
          <Suspense fallback={<Splash />}>
            <ClientApp />
          </Suspense>
        ) : (
          children
        )}
        {expired ? <SessionExpiredDialog info={info} /> : null}
      </WorkspaceContext.Provider>
    );
  }
  if (session.isPending || (sessionMode && info?.authenticated && me.isPending) || workspace) return <Splash />;
  if (sessionMode && info && !info.authenticated) return <SignInScreen info={info} />;
  if (me.isError) {
    if (me.error instanceof ApiError && me.error.status === 401) return <SignInScreen info={info} />;
    return (
      <CenteredPanel>
        <ErrorNotice error={me.error} title="The app cannot load your workspace" />
        <Button variant="outline" onClick={() => void me.refetch()}>
          <RefreshCw /> Try again
        </Button>
      </CenteredPanel>
    );
  }
  return <Splash />;
}

function CenteredPanel({ children }: { children: ReactNode }) {
  return (
    <main id="main" className="grid min-h-svh place-items-center bg-background px-4 py-10">
      <div className="grid w-full max-w-md gap-4">{children}</div>
    </main>
  );
}

function Splash() {
  return (
    <div className="grid min-h-svh place-items-center bg-background" role="status" aria-live="polite">
      <div className="flex items-center gap-3 text-muted-foreground">
        <Spinner />
        <span>The app loads your workspace…</span>
      </div>
    </div>
  );
}

export function SignInScreen({ info }: { info: SessionInfo | null }) {
  const [busy, setBusy] = useState(false);
  return (
    <main id="main" className="grid min-h-svh bg-background lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <section className="flex flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto grid w-full max-w-sm gap-7">
          <Wordmark className="size-7" />
          <div className="grid gap-3">
            <h1 className="display-title text-[clamp(2rem,4vw,2.5rem)] leading-[1.06]">Sign in to your workspace</h1>
            <p className="text-base text-muted-foreground">
              Use your organisation account. The app sends you to your identity provider. Then the identity provider sends you back here.
            </p>
          </div>
          <Button
            size="lg"
            className="w-fit"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              signIn(currentPath(), info);
            }}
          >
            {busy ? <Spinner /> : <LogIn />} Continue to sign in
          </Button>
          <p className="text-sm text-muted-foreground">
            New here?{" "}
            <a href="/signup" className="link font-medium text-foreground">
              Create a workspace
            </a>
          </p>
          <p className="text-xs text-muted-foreground">
            The app never sees your password or an OAuth token. The API keeps the session in a secure,
            HTTP-only cookie.
          </p>
        </div>
      </section>
      {/* The art panel: dark in both themes. The scrim keeps the line of text readable on any art. */}
      <aside className="band-dark relative m-3 hidden overflow-hidden rounded-3xl lg:block">
        <BrandArt src={signinArt} width={1200} height={1500} blend={false} className="absolute inset-0 size-full object-cover" />
        <div aria-hidden className="absolute inset-0 bg-[linear-gradient(to_bottom_right,var(--background)_18%,transparent_58%)]" />
        <p className="display-title relative max-w-[13ch] p-[clamp(2rem,4vw,3.5rem)] text-[clamp(1.75rem,3vw,2.75rem)] leading-[1.08]">
          Fit a model. See how sure it is. Share the result.
        </p>
      </aside>
    </main>
  );
}

function SessionExpiredDialog({ info }: { info: SessionInfo | null }) {
  const dismiss = useAuth((s) => s.dismiss);
  return (
    <Dialog open onOpenChange={(open) => !open && dismiss()}>
      <DialogContent>
        <DialogTitle>Your session has ended</DialogTitle>
        <DialogDescription>
          Sign in again to continue your work. Until you sign in, the page does not change, but the app cannot load or save data.
        </DialogDescription>
        <DialogFooter>
          <Button variant="ghost" onClick={dismiss}>
            Stay on this page
          </Button>
          <Button onClick={() => signIn(currentPath(), info)}>
            <LogIn /> Sign in again
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Sign out: end the server session, then reload at `/` so nothing from the session (cached
 * data, the workspace) stays in memory; the gate then shows the sign-in screen. The recent
 * items of the search (names and descriptions, kept in this browser) go too.
 */
export function useSignOut(): () => Promise<void> {
  return async () => {
    try {
      await logout();
    } catch {
      // The server forgets the session either way; reloading shows what it thinks.
    }
    try {
      localStorage.removeItem(RECENTS_STORAGE_KEY);
    } catch {
      // No storage (a private window): there is nothing to remove.
    }
    window.location.assign("/");
  };
}

export function NoProjectScreen({ email }: { email: string | null }) {
  const signOut = useSignOut();
  return (
    <CenteredPanel>
      <div className="grid gap-3 rounded-3xl bg-card p-8">
        <Logo className="size-7" />
        <h1 className="display-title text-2xl">You are not in a project yet</h1>
        <p className="text-muted-foreground">
          You are signed in{email ? ` as ${email}` : ""}, but no project has added you. Ask a project admin or an
          organisation admin to invite {email ?? "you"}. The app finds the invitation the next time that you sign in.
        </p>
        <div className="flex gap-2 pt-1">
          <Button variant="outline" onClick={() => void signOut()}>
            <LogOut /> Sign out
          </Button>
        </div>
      </div>
    </CenteredPanel>
  );
}
