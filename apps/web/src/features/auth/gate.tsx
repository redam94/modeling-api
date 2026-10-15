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
 */
import { useQueryClient } from "@tanstack/react-query";
import { LogIn, LogOut, RefreshCw } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError } from "@/api/client";
import { qk, useMe, useSession } from "@/api/queries";
import { currentPath, logout, signIn } from "@/api/session";
import type { SessionInfo } from "@/api/types";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { useApplyTheme } from "@/hooks/use-theme";
import { useAuth } from "@/stores/auth";
import { scopeKey, useScope } from "@/stores/scope";

import { buildWorkspace, localMe, WorkspaceContext, type Workspace } from "./workspace";

export function WorkspaceGate({ children }: { children: ReactNode }) {
  useApplyTheme();
  const session = useSession();
  const expired = useAuth((s) => s.expired);
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

  // A session that ends after the app rendered: keep the page, ask to sign in again.
  const signedOutLater = sessionMode && info && !info.authenticated && entered.current;
  useEffect(() => {
    if (signedOutLater) useAuth.getState().markExpired();
  }, [signedOutLater]);

  if (workspace && resolvedKey === activeKey && (!sessionMode || info?.authenticated || entered.current)) {
    entered.current = true;
    return (
      <WorkspaceContext.Provider value={workspace}>
        {children}
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
        <ErrorNotice error={me.error} title="Your workspace could not be loaded" />
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
        <span>Loading your workspace…</span>
      </div>
    </div>
  );
}

/** Mark: three stacked bands narrowing to a point estimate (a posterior fan, abstracted). */
export function Logo({ className = "size-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={`${className} shrink-0`} aria-hidden>
      <rect x="2" y="4" width="20" height="4" rx="2" className="fill-primary/25" />
      <rect x="5" y="10" width="14" height="4" rx="2" className="fill-primary/55" />
      <rect x="9" y="16" width="6" height="4" rx="2" className="fill-primary" />
    </svg>
  );
}

export function SignInScreen({ info }: { info: SessionInfo | null }) {
  const [busy, setBusy] = useState(false);
  return (
    <main id="main" className="grid min-h-svh bg-background lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <section className="flex flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto grid w-full max-w-sm gap-8">
          <div className="flex items-center gap-2.5">
            <Logo className="size-8" />
            <span className="text-lg font-semibold tracking-tight">
              modeling<span className="text-muted-foreground">-api</span>
            </span>
          </div>
          <div className="grid gap-2">
            <h1 className="text-[26px] leading-tight font-semibold tracking-[-0.015em]">Sign in to your workspace</h1>
            <p className="text-muted-foreground">
              Use your organisation account. You will be sent to your identity provider and brought back here.
            </p>
          </div>
          <Button
            size="lg"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              signIn(currentPath(), info);
            }}
          >
            {busy ? <Spinner /> : <LogIn />} Continue to sign in
          </Button>
          <p className="text-xs text-muted-foreground">
            The app never sees your password or an OAuth token: the API keeps the session in a secure,
            HTTP-only cookie.
          </p>
        </div>
      </section>
      <aside aria-hidden className="relative hidden overflow-hidden border-l bg-sidebar lg:block">
        <FanArt />
      </aside>
    </main>
  );
}

/** Decorative posterior fan for the sign-in screen (pure SVG, no data). */
function FanArt() {
  const bands = [
    { spread: 1, opacity: 0.12 },
    { spread: 0.66, opacity: 0.2 },
    { spread: 0.33, opacity: 0.32 },
  ];
  const mid = (x: number) => 300 - 120 * Math.sin(x / 120) - x * 0.25;
  const path = (spread: number) => {
    const xs = Array.from({ length: 41 }, (_, i) => i * 20);
    const width = (x: number) => 10 + spread * (x / 800) * 220;
    const top = xs.map((x) => `${x},${(mid(x) - width(x)).toFixed(1)}`);
    const bottom = xs.reverse().map((x) => `${x},${(mid(x) + width(x)).toFixed(1)}`);
    return `M${top.join(" L")} L${bottom.join(" L")} Z`;
  };
  return (
    <svg viewBox="0 0 800 600" preserveAspectRatio="xMidYMid slice" className="absolute inset-0 size-full">
      {bands.map((b) => (
        <path key={b.spread} d={path(b.spread)} className="fill-primary" style={{ opacity: b.opacity }} />
      ))}
      <path
        d={`M${Array.from({ length: 41 }, (_, i) => `${i * 20},${mid(i * 20).toFixed(1)}`).join(" L")}`}
        className="fill-none stroke-primary"
        strokeWidth={2.5}
      />
      <line x1="440" x2="440" y1="40" y2="560" className="stroke-foreground/25" strokeDasharray="4 6" />
    </svg>
  );
}

function SessionExpiredDialog({ info }: { info: SessionInfo | null }) {
  const clear = useAuth((s) => s.clearExpired);
  const qc = useQueryClient();
  return (
    <Dialog open onOpenChange={(open) => !open && clear()}>
      <DialogContent>
        <DialogTitle>Your session has ended</DialogTitle>
        <DialogDescription>
          Sign in again to keep working. Until then the page stays as it is, but nothing can be loaded or saved.
        </DialogDescription>
        <DialogFooter>
          <Button
            variant="ghost"
            onClick={() => {
              clear();
              void qc.invalidateQueries({ queryKey: qk.session });
            }}
          >
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
 * data, the workspace) stays in memory; the gate then shows the sign-in screen.
 */
export function useSignOut(): () => Promise<void> {
  return async () => {
    try {
      await logout();
    } catch {
      // The server forgets the session either way; reloading shows what it thinks.
    }
    window.location.assign("/");
  };
}

export function NoProjectScreen({ email }: { email: string | null }) {
  const signOut = useSignOut();
  return (
    <CenteredPanel>
      <div className="grid gap-3 rounded-xl border bg-card p-6">
        <Logo className="size-7" />
        <h1 className="text-lg font-semibold">You are not in a project yet</h1>
        <p className="text-muted-foreground">
          You are signed in{email ? ` as ${email}` : ""}, but no project has added you. Ask a project or organisation
          admin to invite {email ?? "you"}; the invitation is picked up the next time you sign in.
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
