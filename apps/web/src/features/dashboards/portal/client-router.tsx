/**
 * The client portal's own small router (plan D15): `/c` (the dashboards shared with the user)
 * and `/c/$dashboardId?tab=`. Every other path redirects to `/c`, so no page of the full app is
 * reachable from a client session (the server refuses those routes anyway). The app's main
 * router is never mounted for a client.
 *
 * Links and redirects use `href` strings: the app's typed route register is the main router's.
 */
import { createBrowserHistory, createRootRoute, createRoute, createRouter, Outlet, useRouter, type RouterHistory } from "@tanstack/react-router";
import { useEffect } from "react";

import { useDashboards, useMemberClient } from "../api";
import { ReadoutPage } from "../readout/readout-page";
import { TAB_KEYS, type TabKey } from "../types";
import { ClientShell } from "./client-shell";
import { DashboardList } from "./dashboard-list";

/** `/c` and `/c/<id>` (one segment), nothing else. */
export const CLIENT_PATH_RE = /^\/c(?:\/[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?\/?$/;

export function isClientPath(pathname: string): boolean {
  return CLIENT_PATH_RE.test(pathname);
}

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): T | undefined {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : undefined;
}

/** The client name for the header: the one every shared dashboard names, if they agree. */
function useClientName(): string | null {
  // The portal's own list query (shared cache): an observer of that key without a queryFn
  // made TanStack Query log "No queryFn was passed" whenever the list refetched.
  const q = useDashboards({ status: "published" });
  const names = new Set((q.data?.items ?? []).map((d) => d.client_name).filter((n): n is string => Boolean(n)));
  return names.size === 1 ? ([...names][0] ?? null) : null;
}

function Root() {
  const router = useRouter();
  const name = useClientName();
  return (
    <ClientShell clientName={name} onHome={() => void router.navigate({ href: "/c" })}>
      <Outlet />
    </ClientShell>
  );
}

function ToHome() {
  const router = useRouter();
  useEffect(() => {
    void router.navigate({ href: "/c", replace: true });
  }, [router]);
  return null;
}

const rootRoute = createRootRoute({
  component: Root,
  // Any other path (`/`, `/runs`, `/c/a/b`) matches no route: back to `/c`.
  notFoundComponent: ToHome,
});

function ListPage() {
  const router = useRouter();
  return <DashboardList onOpen={(id) => void router.navigate({ href: `/c/${encodeURIComponent(id)}` })} />;
}

const listRoute = createRoute({ getParentRoute: () => rootRoute, path: "/c", component: ListPage });

function DashboardPage() {
  const { dashboardId } = dashboardRoute.useParams();
  const { tab } = dashboardRoute.useSearch();
  const router = useRouter();
  const client = useMemberClient(dashboardId, { kind: "client" });
  const onTab = (t: TabKey) => {
    const q = t === "this_period" ? "" : `?tab=${t}`;
    void router.navigate({ href: `/c/${encodeURIComponent(dashboardId)}${q}`, replace: true });
  };
  return <ReadoutPage key={client.cacheKey.join("/")} client={client} tab={tab ?? "this_period"} onTabChange={onTab} />;
}

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/c/$dashboardId",
  validateSearch: (s: Record<string, unknown>): { tab?: TabKey | undefined } => ({ tab: oneOf(s.tab, TAB_KEYS) }),
  component: DashboardPage,
});

export const clientRouteTree = rootRoute.addChildren([listRoute, dashboardRoute]);

export function createClientRouter(history: RouterHistory = createBrowserHistory()) {
  return createRouter({ routeTree: clientRouteTree, history, scrollRestoration: true });
}
