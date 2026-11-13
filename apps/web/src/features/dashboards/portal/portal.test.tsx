import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";
import { WorkspaceGate } from "@/features/auth/gate";
import { MMM_DASHBOARD_ID } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY } from "@/mocks/tenancy";
import { routeTree } from "@/router";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { isClientPath } from "./client-router";

const server = setupServer(...createHandlers());
const seen: Request[] = [];
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  seen.length = 0;
  localStorage.setItem("mapi.mock.readoutMs", "40");
});
afterEach(() => server.resetHandlers());
afterAll(() => {
  server.close();
  window.history.replaceState(null, "", "/");
});

/**
 * The whole app at `url` as `persona` (as `test/render-app.tsx`, which does not list the client
 * persona). The client portal uses the browser history, so the URL is set there too.
 */
function renderApp(url: string, persona: string) {
  localStorage.setItem(MOCK_SESSION_KEY, persona);
  localStorage.setItem("mapi.mock.readoutMs", "40");
  window.history.replaceState(null, "", url);
  useAuth.setState({ mode: "unknown", expired: false, dismissed: false });
  useScope.setState({ active: null, preferred: null, recent: [] });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [url] }), context: { queryClient } });
  render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WorkspaceGate>
          <RouterProvider router={router} />
        </WorkspaceGate>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { router };
}

describe("isClientPath", () => {
  it("accepts /c and /c/<id> only", () => {
    expect(isClientPath("/c")).toBe(true);
    expect(isClientPath(`/c/${MMM_DASHBOARD_ID}`)).toBe(true);
    expect(isClientPath("/")).toBe(false);
    expect(isClientPath("/runs")).toBe(false);
    expect(isClientPath("/c/a/b")).toBe(false);
    expect(isClientPath("/client")).toBe(false);
  });
});

describe("client portal", () => {
  it("lands a client on /c with only their dashboards: no navigation, no command palette", async () => {
    renderApp("/", "cleo");
    expect(await screen.findByRole("heading", { level: 1, name: "Your dashboards" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/c"));
    const list = await screen.findByRole("list", { name: "Dashboards" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Weekly media readout")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Search/ })).toBeNull();
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.queryByRole("dialog")).toBeNull();
    // The full app's routes were never asked for.
    expect(seen.some((r) => new URL(r.url).pathname === "/v1/runs")).toBe(false);
    expect(screen.getByRole("button", { name: /^Project: Acme/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Account: Cleo Client" })).toBeInTheDocument();
  });

  it("redirects any other path to /c", async () => {
    renderApp("/runs", "cleo");
    expect(await screen.findByRole("heading", { level: 1, name: "Your dashboards" })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe("/c"));
    expect(screen.queryByText("No runs yet")).toBeNull();
  });

  it("opens a dashboard and keeps the tab in the URL", async () => {
    renderApp("/c", "cleo");
    const list = await screen.findByRole("list", { name: "Dashboards" });
    await userEvent.click(within(list).getByText("Weekly media readout"));
    expect(await screen.findByRole("heading", { level: 1, name: "Weekly media readout" })).toBeInTheDocument();
    expect(window.location.pathname).toBe(`/c/${MMM_DASHBOARD_ID}`);
    // The client's own private and pending readouts reach it; the header shows the client.
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    await waitFor(() => expect(window.location.search).toBe("?tab=history"));
    const readouts = await screen.findByRole("list", { name: "Readouts" });
    expect(await within(readouts).findByText("Numbers sent on 20/10/2026")).toBeInTheDocument();
    expect(within(readouts).getAllByText("Refit requested").length).toBeGreaterThan(0);
    const requests = seen.filter((r) => new URL(r.url).pathname.startsWith("/v1/dashboards"));
    expect(requests.length).toBeGreaterThan(0);
    expect(requests.every((r) => r.headers.get("X-Mapi-Project") === "acme/growth")).toBe(true);
  });

  it("keeps the full app for a member", async () => {
    renderApp("/runs", "ana");
    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Your dashboards" })).toBeNull();
  });
});
