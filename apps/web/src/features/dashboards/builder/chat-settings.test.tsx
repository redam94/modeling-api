import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import type { ProjectRole } from "@/api/types";
import { TooltipProvider } from "@/components/ui/overlays";
import { env } from "@/env";
import { buildWorkspace, localMe, WorkspaceContext, type Workspace } from "@/features/auth/workspace";
import { V2_DASHBOARD_IDS, V2_PROJECT } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";
import { renderApp } from "@/test/render-app";

import type { ChatSettingsView } from "../chat/types";
import type { Dashboard } from "../types";
import { chatFormIssues, chatLayoutBlock, ChatSettings, costWords, formOf } from "./chat-settings";

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
  server.close();
  server = setupServer(...createHandlers());
  listen();
  seen.length = 0;
  localStorage.setItem("mapi.mock.readoutMs", "40");
});
afterEach(() => useAuth.setState({ mode: "unknown", csrf: null }));
afterAll(() => server.close());

const FORECAST = V2_DASHBOARD_IDS.forecast;
const RETAIL = { tenantId: "acme", projectId: "retail" };
const T = { timeout: 10_000 };

async function openBuilder() {
  renderApp(`/dashboards/${FORECAST}`, "ana", RETAIL);
  const section = await screen.findByTestId("chat-settings", undefined, T);
  await within(section).findByRole("form", { name: "Chat settings" }, T);
  return section;
}

/** Another tab saves the settings (Ana, straight to the API). */
async function saveElsewhere(patch: Partial<{ enabled: boolean; keep_transcripts: boolean; tokens_per_day: number }>) {
  const h = { "X-Mapi-Project": V2_PROJECT, "X-CSRF-Token": mockCsrfToken(PERSONAS.ana!), "Content-Type": "application/json" };
  const url = `${env.apiBase}/v1/dashboards/${FORECAST}/chat/settings`;
  const cur = (await (await fetch(url, { headers: h })).json()) as ChatSettingsView;
  const body = { enabled: false, audiences: { members: true, clients: true, share_links: false }, keep_transcripts: false, tokens_per_day: 200_000, share_tokens_per_day: 50_000, ...patch };
  const res = await fetch(url, { method: "PUT", headers: { ...h, "If-Match": cur.etag }, body: JSON.stringify(body) });
  expect(res.status).toBe(200);
}

describe("builder › chat settings", () => {
  it("turns chat on with its audiences and budgets, and saves with If-Match", async () => {
    const user = userEvent.setup();
    const section = await openBuilder();
    const enabled = within(section).getByTestId("chat-enabled");
    expect(enabled.getAttribute("aria-checked")).toBe("false");
    // off by default: the audiences wait for the switch
    expect(within(section).getByRole("checkbox", { name: "Share links" })).toBeDisabled();
    const save = within(section).getByRole("button", { name: /Save chat settings/ });
    expect(save).toBeDisabled();

    await user.click(enabled);
    await user.click(within(section).getByRole("checkbox", { name: "Share links" }));
    await user.click(within(section).getByRole("checkbox", { name: "Client users" }));
    await user.click(within(section).getByRole("switch", { name: /Keep conversations for 30 days/ }));
    const budget = within(section).getByLabelText("Tokens each day for the dashboard");
    await user.clear(budget);
    await user.type(budget, "150000");
    await user.click(save);

    await waitFor(() => expect(seen.some((r) => r.method === "PUT")).toBe(true), T);
    const put = seen.find((r) => r.method === "PUT")!;
    expect(put.url).toBe(`${env.apiBase}/v1/dashboards/${FORECAST}/chat/settings`);
    expect(put.headers.get("If-Match")).toBe('"c1"');
    expect(await put.json()).toEqual({
      enabled: true,
      audiences: { members: true, clients: false, share_links: true },
      keep_transcripts: true,
      tokens_per_day: 150_000,
      share_tokens_per_day: 50_000,
    });
    await waitFor(() => expect(within(section).getByRole("button", { name: /Save chat settings/ })).toBeDisabled(), T);
    expect(within(section).getByTestId("chat-enabled").getAttribute("aria-checked")).toBe("true");
    expect(within(section).getByTestId("chat-used-today").textContent).toContain("of 150,000 tokens");
    // the transcripts list follows the saved setting
    expect(within(section).getByTestId("chat-transcripts")).toBeTruthy();
  });

  it("shows the other person's settings after a 412 conflict", async () => {
    const user = userEvent.setup();
    const section = await openBuilder();
    await user.click(within(section).getByTestId("chat-enabled"));
    await saveElsewhere({ keep_transcripts: true, tokens_per_day: 9_000 });
    await user.click(within(section).getByRole("button", { name: /Save chat settings/ }));
    expect(await within(section).findByText(/Someone changed the chat settings after you opened them/, undefined, T)).toBeTruthy();
    // the form now shows their settings (the newer etag is used next time)
    await waitFor(() => expect(within(section).getByLabelText<HTMLInputElement>("Tokens each day for the dashboard").value).toBe("9000"), T);
    expect(within(section).getByTestId("chat-enabled").getAttribute("aria-checked")).toBe("false");
    expect(within(section).getByRole("switch", { name: /Keep conversations/ }).getAttribute("aria-checked")).toBe("true");
    await user.click(within(section).getByTestId("chat-enabled"));
    await user.click(within(section).getByRole("button", { name: /Save chat settings/ }));
    await waitFor(() => expect(seen.filter((r) => r.method === "PUT").at(-1)?.headers.get("If-Match")).toBe('"c2"'), T);
    await waitFor(() => expect(within(section).queryByText(/Someone changed the chat settings/)).toBeNull(), T);
  });

  it("refuses a budget that is not a whole number in range, before it sends", async () => {
    const user = userEvent.setup();
    const section = await openBuilder();
    const budget = within(section).getByLabelText("Tokens each day for the dashboard");
    await user.clear(budget);
    await user.type(budget, "12.5");
    expect(within(section).getByText("Type a whole number of tokens.")).toBeTruthy();
    expect(within(section).getByRole("button", { name: /Save chat settings/ })).toBeDisabled();
    await user.clear(budget);
    await user.type(budget, "5000001");
    expect(within(section).getByText("Use at most 5,000,000 tokens.")).toBeTruthy();
    expect(seen.some((r) => r.method === "PUT")).toBe(false);
  });
});

// ------------------------------------------------------------------------------ direct renders

function renderSettings(dashboard: Dashboard, role: ProjectRole) {
  localStorage.setItem(MOCK_SESSION_KEY, "ana");
  useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
  useScope.setState({ active: RETAIL, preferred: null, recent: [] });
  const ws: Workspace = { ...buildWorkspace(localMe(), "session", null), grants: { projectRole: role, tenantRole: null, operator: false, tenantStatus: "active" } };
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <WorkspaceContext.Provider value={ws}>
          <ChatSettings dashboard={dashboard} />
        </WorkspaceContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const v2 = { layout_version: 2 } as unknown as Dashboard["draft_spec"];
const v1 = { title: "Old" } as unknown as Dashboard["draft_spec"];
const dashboardOf = (draft: Dashboard["draft_spec"], published: Dashboard["draft_spec"] | null): Dashboard =>
  ({ id: FORECAST, status: published ? "published" : "draft", draft_spec: draft, published_revision: published ? { spec: published } : null }) as unknown as Dashboard;

describe("chat settings outside the builder page", () => {
  it("asks for the layout upgrade on a layout 1 dashboard and shows no form", async () => {
    renderSettings(dashboardOf(v1, v1), "admin");
    const section = screen.getByTestId("chat-settings");
    expect(within(section).getByText("Upgrade the layout to use chat.")).toBeTruthy();
    expect(within(section).queryByRole("form")).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(seen.some((r) => r.url.includes("/chat/settings"))).toBe(false);
  });

  it("asks to publish when the draft is layout 2 but the published dashboard is not", async () => {
    renderSettings(dashboardOf(v2, v1), "admin");
    const section = screen.getByTestId("chat-settings");
    expect(within(section).getByText("Upgrade the layout to use chat.")).toBeTruthy();
    expect(within(section).getByText(/Publish this draft to use chat/)).toBeTruthy();
    // the settings can be ready before the publish
    expect(await within(section).findByRole("form", { name: "Chat settings" }, T)).toBeTruthy();
  });

  it("tells a viewer that only editors change the settings, and reads nothing", async () => {
    renderSettings(dashboardOf(v2, v2), "viewer");
    expect(screen.getByText("Only editors and admins change the chat settings.")).toBeTruthy();
    expect(screen.queryByTestId("chat-enabled")).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(seen.some((r) => r.url.includes("/chat/"))).toBe(false);
  });
});

describe("chat settings helpers", () => {
  it("finds the layout block", () => {
    expect(chatLayoutBlock(dashboardOf(v1, null))).toBe("draft_v1");
    expect(chatLayoutBlock(dashboardOf(v2, v1))).toBe("published_v1");
    expect(chatLayoutBlock(dashboardOf(v2, v2))).toBeNull();
    expect(chatLayoutBlock(dashboardOf(v2, null))).toBeNull();
  });

  it("fills server defaults and checks budgets", () => {
    const f = formOf({ dashboard_id: FORECAST, etag: '"c1"' });
    expect(f).toEqual({ enabled: false, audiences: { members: true, clients: true, share_links: false }, keep_transcripts: false, tokens_per_day: "200000", share_tokens_per_day: "50000" });
    expect(chatFormIssues(f)).toEqual({});
    expect(chatFormIssues({ ...f, tokens_per_day: "-1", share_tokens_per_day: "1000001" })).toEqual({ tokens_per_day: "Type a whole number of tokens.", share_tokens_per_day: "Use at most 1,000,000 tokens." });
  });

  it("says the cost in words", () => {
    expect(costWords(0)).toBe("US$0.00");
    expect(costWords(4_210)).toBe("less than US$0.01");
    expect(costWords(42_100)).toBe("about US$0.04");
  });
});
