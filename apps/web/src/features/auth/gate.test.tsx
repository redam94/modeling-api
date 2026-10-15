import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import type { Me, Problem } from "@/api/types";
import { ErrorNotice } from "@/components/common";
import { TooltipProvider } from "@/components/ui/overlays";
import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { routeTree } from "@/router";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { WorkspaceGate } from "./gate";

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
  useAuth.setState({ mode: "unknown", expired: false, dismissed: false });
  useScope.setState({ active: null, preferred: null, recent: [] });
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderApp(url: string) {
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
  return { router, queryClient };
}

describe("WorkspaceGate", () => {
  it("shows the sign-in screen when the browser has no session", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "signed-out");
    renderApp("/runs");
    expect(await screen.findByRole("heading", { name: "Sign in to your workspace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue to sign in" })).toBeEnabled();
    expect(screen.queryByRole("navigation", { name: "Primary" })).toBeNull();
  });

  it("opens the persisted project and names it on every project request", async () => {
    useScope.setState({ preferred: { tenantId: "acme", projectId: "retail" } });
    renderApp("/runs");
    expect(await screen.findByRole("button", { name: /^Project: Acme Retail \/ Retail demand/ })).toBeInTheDocument();
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
    const runs = seen.filter((r) => new URL(r.url).pathname === "/v1/runs");
    expect(runs.length).toBeGreaterThan(0);
    expect(runs.every((r) => r.headers.get("X-Mapi-Project") === "acme/retail")).toBe(true);
    const me = seen.find((r) => new URL(r.url).pathname === "/v1/me");
    expect(me?.headers.get("X-Mapi-Project")).toBeNull();
    expect(me?.credentials).toBe("include");
  });

  it("sends the CSRF header on mutations of a signed-in session", async () => {
    renderApp("/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "New token" }));
    const dialog = await screen.findByRole("dialog", { name: "New API token" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "ci");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create token" }));
    await screen.findByRole("dialog", { name: "Copy your new token now" });
    const post = seen.find((r) => r.method === "POST" && new URL(r.url).pathname === "/v1/tokens");
    expect(post?.headers.get("X-CSRF-Token")).toBe(mockCsrfToken(PERSONAS.ana!));
    expect(post?.headers.get("X-Mapi-Project")).toBe("acme/growth");
  });

  it("keeps the page and asks to sign in again when the session ends", async () => {
    renderApp("/runs");
    expect(await screen.findByRole("table", { name: "Runs" })).toBeInTheDocument();
    localStorage.setItem(MOCK_SESSION_KEY, "signed-out");
    act(() => useAuth.getState().markExpired());
    const dialog = await screen.findByRole("dialog", { name: "Your session has ended" });
    expect(within(dialog).getByRole("button", { name: "Sign in again" })).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Stay on this page" }));
    expect(await screen.findByText(/You are signed out/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Runs" })).toBeInTheDocument();
  });

  it("explains that a signed-in user without a project must be invited", async () => {
    localStorage.setItem(MOCK_SESSION_KEY, "rui");
    server.use(
      http.get(`${env.apiBase}/v1/me`, () =>
        HttpResponse.json({
          principal: { kind: "user", id: "usr_01jaf3b9c8d7e6f5g4h3j2k1m0", issuer: "https://accounts.google.com", subject: "x", email: "new@acme.example", name: "New Person" },
          auth: "session",
          operator: false,
          tenants: [],
        } satisfies Me),
      ),
    );
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "You are not in a project yet" })).toBeInTheDocument();
    expect(screen.getByText(/invite new@acme.example/)).toBeInTheDocument();
  });

  it("falls back to the single-tenant workspace on a server without /auth", async () => {
    server.use(http.get(`${env.authBase}/auth/session`, () => new HttpResponse(null, { status: 404 })));
    renderApp("/runs");
    expect(await screen.findByRole("table", { name: "Runs" })).toBeInTheDocument();
    expect(useAuth.getState().mode).toBe("none");
    expect(screen.queryByRole("button", { name: /^Project:/ })).toBeNull();
    const runs = seen.filter((r) => new URL(r.url).pathname === "/v1/runs");
    expect(runs.every((r) => !r.headers.has("X-Mapi-Project"))).toBe(true);
  });
});

describe("Tokens page", () => {
  it("shows the secret once and needs it stored before closing", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderApp("/tokens");
    await userEvent.click(await screen.findByRole("button", { name: "New token" }));
    const dialog = await screen.findByRole("dialog", { name: "New API token" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "nightly");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: /Full access/ }));
    expect(within(dialog).getByText(/Full access acts with your whole role/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Create token" }));

    const secret = await screen.findByRole("dialog", { name: "Copy your new token now" });
    const input = within(secret).getByLabelText("Token", { selector: "input" });
    const value = (input as HTMLInputElement).value;
    expect(value).toMatch(/^mapi_[0-9a-z]{8}_[0-9a-z]{39}$/);
    const done = within(secret).getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await userEvent.click(within(secret).getByRole("button", { name: "Copy token" }));
    expect(writeText).toHaveBeenCalledWith(value);
    await waitFor(() => expect(done).toBeEnabled());
    await userEvent.click(done);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.queryByDisplayValue(value)).toBeNull();
    expect(await screen.findByText("nightly")).toBeInTheDocument();
  });
});

describe("Members page", () => {
  it("invites by e-mail, shows organisation grants, and refuses to drop the last admin", async () => {
    renderApp("/members");
    const table = await screen.findByRole("table", { name: "Members of Growth" });
    // Ana's admin role comes from the organisation; it cannot be edited per project.
    const ana = within(table).getByText("Ana Analyst").closest("tr")!;
    expect(within(ana).getByText("organisation")).toBeInTheDocument();
    expect(within(ana).queryByRole("combobox")).toBeNull();
    expect(within(table).getByRole("combobox", { name: "Role of Rui Runner" })).toHaveValue("runner");

    await userEvent.click(screen.getByRole("button", { name: "Add member" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a member to Growth" });
    await userEvent.type(within(dialog).getByLabelText("E-mail or user id"), "not-an-address");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add member" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Use an e-mail address");
    await userEvent.clear(within(dialog).getByLabelText("E-mail or user id"));
    await userEvent.type(within(dialog).getByLabelText("E-mail or user id"), "Lee@Acme.example");
    await userEvent.click(within(dialog).getByRole("radio", { name: /^Editor/ }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Add member" }));
    expect(await within(table).findByText("lee@acme.example")).toBeInTheDocument();

    // Organisation tab: Ana is the only tenant admin.
    await userEvent.click(screen.getByRole("radio", { name: "Organisation" }));
    const org = await screen.findByRole("table", { name: "Members of Acme Retail" });
    await userEvent.selectOptions(within(org).getByRole("combobox", { name: "Role of Ana Analyst" }), "runner");
    expect(await screen.findByText("The role could not be changed")).toBeInTheDocument();
  });
});

describe("problem notices", () => {
  const rate: Problem = { type: "urn:mapi:error:RATE_LIMITED", title: "Too many requests", status: 429, code: "RATE_LIMITED", detail: "tenant acme would exceed requests_per_minute (600 + 1 > 600)" };

  it("counts down Retry-After before offering a retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const onRetry = vi.fn();
      render(<ErrorNotice error={new ApiError(429, rate, undefined, 3)} onRetry={onRetry} />);
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent("Too many requests");
      expect(alert).toHaveTextContent("600 per minute");
      expect(screen.getByRole("button", { name: "Try again in 3 s" })).toBeDisabled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3200);
      });
      const retry = screen.getByRole("button", { name: "Try again" });
      expect(retry).toBeEnabled();
      retry.click();
      expect(onRetry).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps ordinary errors technical", () => {
    const p: Problem = { type: "urn:mapi:error:NOT_FOUND", title: "Not found", status: 404, code: "NOT_FOUND", detail: "no such run" };
    render(<ErrorNotice error={new ApiError(404, p)} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Not found");
    expect(screen.getByRole("alert")).toHaveTextContent("NOT_FOUND");
    expect(screen.getByRole("alert").getAttribute("data-advice")).toBeNull();
  });
});
