import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { ProjectRole } from "@/api/types";
import { TooltipProvider } from "@/components/ui/overlays";
import { buildWorkspace, localMe, WorkspaceContext, type Workspace } from "@/features/auth/workspace";
import { V2_DASHBOARD_IDS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { isClientRoute, MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { ConnectChatToolButton } from "./connect-button";
import { TOKEN_PLACEHOLDER } from "./mcp-snippets";

let server = setupServer(...createHandlers());
const seen: Request[] = [];
function listen() {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
}
beforeAll(() => {
  listen();
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  // A fresh mock state for each test (the seeded tokens: Ana's and Cleo's).
  server.close();
  server = setupServer(...createHandlers());
  listen();
  seen.length = 0;
});
afterEach(() => {
  useAuth.setState({ mode: "unknown", csrf: null });
  vi.restoreAllMocks();
});
afterAll(() => server.close());

const FORECAST = V2_DASHBOARD_IDS.forecast;
const T = { timeout: 10_000 };

/** The button as `persona` with `role` in acme/retail (the forecast dashboard's project). */
function renderAs(persona: "ana" | "cleo", role: ProjectRole, ui: ReactNode = <ConnectChatToolButton dashboardId={FORECAST} title="Weekly visits" />) {
  localStorage.setItem(MOCK_SESSION_KEY, persona);
  useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS[persona]!) });
  useScope.setState({ active: { tenantId: "acme", projectId: "retail" }, preferred: null, recent: [] });
  const base = buildWorkspace(localMe(), "session", null);
  const ws: Workspace = { ...base, grants: { projectRole: role, tenantRole: null, operator: false, tenantStatus: "active" } };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <WorkspaceContext.Provider value={ws}>{ui}</WorkspaceContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("connect-chat-tool"));
  const dialog = await screen.findByRole("dialog", { name: "Connect a chat tool" });
  await within(dialog).findAllByTestId("read-token-row", undefined, T);
  return dialog;
}

/**
 * The mock tenancy guard lets a client user reach only the routes it lists; until it lists the
 * read-token routes (`mocks/tenancy.ts`, reported to the integrator), the client cases skip.
 */
const CLIENT_MOCKED = isClientRoute("GET", `/v1/dashboards/${FORECAST}/read-tokens`) && isClientRoute("POST", `/v1/dashboards/${FORECAST}/read-tokens`) && isClientRoute("DELETE", `/v1/dashboards/${FORECAST}/read-tokens/tok_x`);

/** Each viewer kind with its own seeded token (Ana: "Claude Desktop", Cleo: "Cleo's laptop"). */
const OWNERS = [
  { persona: "ana", role: "admin", own: "Claude Desktop", skip: false },
  { persona: "cleo", role: "client", own: "Cleo's laptop", skip: !CLIENT_MOCKED },
] as const;

const storedValues = () => [...Object.keys(localStorage).map((k) => localStorage.getItem(k) ?? ""), ...Object.keys(sessionStorage).map((k) => sessionStorage.getItem(k) ?? "")].join("\n");

describe("connect a chat tool", () => {
  for (const o of OWNERS) {
    it.skipIf(o.skip)(`lets ${o.persona} (${o.role}) make a token: the secret shows once, fills the snippets and is never stored`, async () => {
      const user = userEvent.setup();
      renderAs(o.persona, o.role);
      const dialog = await open(user);
      // the list starts with your own tokens only; a client has no "everyone's tokens" switch
      expect(within(dialog).getAllByTestId("read-token-row").map((r) => r.textContent)).toEqual([expect.stringContaining(o.own)]);
      expect(within(dialog).queryByLabelText("Show everyone's tokens") === null).toBe(o.role === "client");
      // before a token is made, the snippet holds the placeholder
      expect(within(dialog).getByTestId("connect-snippet-stdio").textContent).toContain(TOKEN_PLACEHOLDER);

      const form = within(dialog).getByTestId("read-token-create");
      await user.type(within(form).getByLabelText("Name"), "Laptop assistant");
      await user.selectOptions(within(form).getByLabelText("Ends after"), "90");
      await user.click(within(form).getByRole("button", { name: /Make token/ }));

      const secretBox = await within(dialog).findByTestId("read-token-secret", undefined, T);
      const secret = within(secretBox).getByLabelText<HTMLInputElement>(/Read token/).value;
      expect(secret).toMatch(/^mapi_/);
      const post = seen.find((r) => r.method === "POST" && r.url.endsWith(`/v1/dashboards/${FORECAST}/read-tokens`));
      expect(post).toBeTruthy();
      expect(await post!.json()).toEqual({ name: "Laptop assistant", expires_in_days: 90 });

      // stdio: the token in the environment only; HTTP: in the Authorization header only
      const stdio = JSON.parse(within(dialog).getByTestId("connect-snippet-stdio").textContent ?? "{}") as { mcpServers: Record<string, { command: string; args?: unknown; env: Record<string, string> }> };
      const s = Object.values(stdio.mcpServers)[0]!;
      expect(s.env.MAPI_DASHBOARD_TOKEN).toBe(secret);
      expect(s.args).toBeUndefined();
      await user.click(within(dialog).getByRole("radio", { name: /HTTP/ }));
      const http = within(dialog).getByTestId("connect-snippet-http").textContent ?? "";
      expect(http).toContain(`"Authorization": "Bearer ${secret}"`);
      expect(http).toMatch(/"url": "[^"]*\/v1\/mcp"/);

      // the list is read again, and never carries the secret
      await waitFor(() => expect(within(dialog).getAllByTestId("read-token-row")).toHaveLength(2), T);
      expect(within(dialog).getByRole("table", { name: "Read tokens" }).textContent).not.toContain(secret);
      expect(storedValues()).not.toContain(secret);

      // closing without copying asks first; after closing, the secret is gone
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      await user.click(within(dialog).getAllByRole("button", { name: "Close" }).at(-1)!);
      expect(confirm).toHaveBeenCalledOnce();
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(document.body.textContent).not.toContain(secret);
      const again = await open(user);
      expect(within(again).queryByTestId("read-token-secret")).toBeNull();
      expect(again.textContent).not.toContain(secret);
      expect(storedValues()).not.toContain(secret);
    });

    it.skipIf(o.skip)(`lets ${o.persona} (${o.role}) revoke their token after a confirmation`, async () => {
      const user = userEvent.setup();
      renderAs(o.persona, o.role);
      const dialog = await open(user);
      const row = within(dialog).getByTestId("read-token-row");
      expect(row.getAttribute("data-status")).toBe("active");
      await user.click(within(row).getByRole("button", { name: `Revoke ${o.own}` }));
      await user.click(within(row).getByRole("button", { name: /Revoke now/ }));
      await waitFor(() => expect(within(dialog).getByTestId("read-token-row").getAttribute("data-status")).toBe("revoked"), T);
      expect(seen.some((r) => r.method === "DELETE" && r.url.endsWith(`/read-tokens/${row.getAttribute("data-token")}`))).toBe(true);
    });
  }

  it("lets an editor list everyone's tokens of the dashboard", async () => {
    const user = userEvent.setup();
    renderAs("ana", "admin");
    const dialog = await open(user);
    expect(within(dialog).getAllByTestId("read-token-row")).toHaveLength(1);
    await user.click(within(dialog).getByLabelText("Show everyone's tokens"));
    await waitFor(() => expect(within(dialog).getAllByTestId("read-token-row")).toHaveLength(2), T);
    const table = within(dialog).getByRole("table", { name: "Read tokens" });
    expect(within(table).getByText("Cleo Client")).toBeTruthy();
    expect(seen.some((r) => r.url.includes("/read-tokens?all=true"))).toBe(true);
  });

  it("says what to fix before it sends, and explains the limit of active tokens", async () => {
    const user = userEvent.setup();
    renderAs("ana", "admin");
    const dialog = await open(user);
    const form = within(dialog).getByTestId("read-token-create");
    await user.click(within(form).getByRole("button", { name: /Make token/ }));
    expect(within(form).getByText("Give the token a name.")).toBeTruthy();
    expect(seen.some((r) => r.method === "POST")).toBe(false);

    // Ana has one active token; 4 more reach the limit of 5, the 6th is refused
    for (let i = 0; i < 5; i += 1) {
      const f = within(dialog).queryByTestId("read-token-create");
      if (!f) await user.click(within(dialog).getByRole("button", { name: "Make another token" }));
      const fresh = within(dialog).getByTestId("read-token-create");
      await user.type(within(fresh).getByLabelText("Name"), `Tool ${i}`);
      await user.click(within(fresh).getByRole("button", { name: /Make token/ }));
      if (i < 4) await within(dialog).findByTestId("read-token-secret", undefined, T);
    }
    expect(await within(dialog).findByText(/You have 5 active tokens for this dashboard/, undefined, T)).toBeTruthy();
  });
});
