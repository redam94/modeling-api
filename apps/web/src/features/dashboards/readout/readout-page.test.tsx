import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type * as React from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";
import { FORECAST_DASHBOARD_ID, MMM_DASHBOARD_ID, MOCK_SHARE_PASSCODE, MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";

import { openShareSession, refreshShareSession, shareClient } from "../client";
import type { DashboardClient, ShareSessionHandle } from "../types";
import { ReadoutPage } from "./readout-page";

const server = setupServer(...createHandlers());
const seen: Request[] = [];
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    seen.push(request.clone());
  });
});
beforeEach(() => {
  seen.length = 0;
  localStorage.setItem("mapi.mock.readoutMs", "40");
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

async function clientFor(token: string, passcode: string | null, id: string): Promise<DashboardClient> {
  const first = await openShareSession(token, passcode);
  let session = first.session;
  const h: ShareSessionHandle = {
    current: () => session,
    expired: () => false,
    refresh: async () => {
      session = (await refreshShareSession(session)).session;
    },
  };
  return shareClient(h, id);
}

function view(ui: React.ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("ReadoutPage", () => {
  it("renders the MMM readout: lead, tiles with plain-word changes, chart and scorecard with aria-labels", async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.readOnly, null, MMM_DASHBOARD_ID);
    view(<ReadoutPage client={client} />);
    expect(await screen.findByRole("heading", { level: 1, name: "Weekly media readout" })).toBeInTheDocument();
    expect(screen.getByText(/^Updated \d{2}\/\d{2}\/\d{4} \d{2}\.\d{2} with results to the week ending 18\/10\/2026\.$/)).toBeInTheDocument();
    expect(document.querySelector("[data-readout=lead]")?.textContent).toMatch(/^Media brought .* revenue this week\./);

    const tiles = screen.getByRole("list", { name: "Key numbers" });
    expect(within(tiles).getAllByRole("listitem")).toHaveLength(4);
    expect(within(tiles).getByText("Expected revenue")).toBeInTheDocument();
    expect(within(tiles).getByText(/than the week before$/)).toBeInTheDocument();

    expect(screen.getByRole("img", { name: /^Revenue each week, 26 weeks to 18\/10\/2026\./ })).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Channel scorecard" });
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(within(table).getByRole("img", { name: /^TV: (More room|Within range|Past its best|Not paying back)$/ })).toBeInTheDocument();
    expect(within(table).getByRole("img", { name: /^Paid search return on media: \d\.\d\d, likely range .* the break-even of 1\.00\.$/ })).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Zones" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /^Change in revenue by channel: TV / })).toBeInTheDocument();
    expect(screen.getByText(/^The actual number was in the likely range in \d+ of 26 weeks\.$/)).toBeInTheDocument();

    // A read-only link has no inputs: no Plan scenarios or Update numbers tabs.
    const tabs = screen.getByRole("tablist", { name: "Dashboard sections" });
    expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual(["This week", "Trends", "History"]);

    await userEvent.click(screen.getByRole("switch", { name: "Show technical notes" }));
    expect(screen.getByText("rhat.max")).toBeInTheDocument();
  });

  it("answers the sliders at once and keeps the plan words plain", async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE, MMM_DASHBOARD_ID);
    view(<ReadoutPage client={client} tab="scenarios" />);
    const slider = await screen.findByRole("slider", { name: "Paid search" });
    expect(screen.getByText("This plan brings the same result as the current plan.")).toBeInTheDocument();
    const before = seen.length;
    // Range inputs take a value through a change event.
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(slider, { target: { value: "1.5" } });
    expect(slider).toHaveAttribute("aria-valuetext", expect.stringMatching(/^\+50% spend/));
    expect(screen.getByText(/^This plan brings [\d,]+ more revenue than the current plan, for [\d,]+ more spend\.$/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /^Change in revenue by channel: .*Paid search \+[\d,]+/ })).toBeInTheDocument();
    // Answered in the browser: no request for the slider move.
    expect(seen.slice(before).filter((r) => r.url.includes("whatif"))).toHaveLength(0);

    await userEvent.click(screen.getByRole("button", { name: "Save scenario" }));
    expect(await screen.findByText(/^We saved the plan\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Get exact result" })).toBeEnabled();
  });

  it("offers the refit request only when the dashboard allows it, and shows its status", { timeout: 15_000 }, async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE, MMM_DASHBOARD_ID);
    view(<ReadoutPage client={client} tab="update" />);
    expect(await screen.findByText("We compare your numbers with what the model expected. The model does not change.")).toBeInTheDocument();
    const send = screen.getByRole("button", { name: "Send" });
    // The template leaves the revenue empty: nothing to send yet.
    expect(send).toBeDisabled();
    expect(screen.getByText("Enter at least one number in the column revenue.")).toBeInTheDocument();
    for (let i = 1; i <= 4; i++) await userEvent.type(screen.getByRole("textbox", { name: `revenue, row ${i}` }), String(400000 + i * 1000));
    expect(send).toBeEnabled();
    const box = screen.getByRole("checkbox", { name: "Ask the analyst to refit with these numbers" });
    await userEvent.click(box);
    await userEvent.click(send);
    expect(await screen.findByText(/^We received your numbers\. The new readout appears in History when it is ready\./)).toBeInTheDocument();
    const sent = document.querySelector("[data-readout=sent]") as HTMLElement;
    expect(within(sent).getByText("Refit requested")).toBeInTheDocument();
    expect(within(sent).getByText("Waiting for review")).toBeInTheDocument();
    const body = (await seen.find((r) => r.url.endsWith("/v1/shared/actuals"))!.json()) as { request_refit?: boolean; inputs: { data_vars: Record<string, unknown> } };
    expect(body.request_refit).toBe(true);
    expect(Object.keys(body.inputs.data_vars)).toContain("revenue");
    // Polled until ready: the lead of the comparison, the refit request kept on the readout.
    expect(await within(sent).findByText(/^Your numbers were \d+% (more|less) than the model expected over 4 weeks\./, {}, { timeout: 6000 })).toBeInTheDocument();
    expect(within(sent).getByText(/^You asked the analyst to refit the model with these numbers\./)).toBeInTheDocument();
  });

  it("does not send the same numbers twice, and a retry reuses the Idempotency-Key", { timeout: 15_000 }, async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE, MMM_DASHBOARD_ID);
    view(<ReadoutPage client={client} tab="update" />);
    await screen.findByText("We compare your numbers with what the model expected. The model does not change.");
    for (let i = 1; i <= 4; i++) await userEvent.type(screen.getByRole("textbox", { name: `revenue, row ${i}` }), String(500000 + i));
    const send = screen.getByRole("button", { name: "Send" });
    const posts = () => seen.filter((r) => r.method === "POST" && r.url.endsWith("/v1/shared/actuals"));
    // the first try fails on the way: the retry of the same rows sends the same key
    server.use(
      http.post("*/v1/shared/actuals", () => HttpResponse.json({ type: "about:blank", title: "Unavailable", status: 503, code: "INFRA_ERROR" }, { status: 503 }), { once: true }),
    );
    await userEvent.click(send);
    await waitFor(() => expect(posts()).toHaveLength(1));
    await waitFor(() => expect(send).toBeEnabled());
    await userEvent.click(send);
    await screen.findByText(/^We received your numbers\./);
    expect(send).toBeDisabled(); // a second tap would start a second score run
    expect(posts()).toHaveLength(2);
    const first = posts()[0]!.headers.get("Idempotency-Key");
    expect(posts()[1]!.headers.get("Idempotency-Key")).toBe(first);
    // changing a row allows a new send, with a new key
    await userEvent.type(screen.getByRole("textbox", { name: "revenue, row 1" }), "9");
    expect(send).toBeEnabled();
    await userEvent.click(send);
    await waitFor(() => expect(posts()).toHaveLength(3));
    expect(posts()[2]!.headers.get("Idempotency-Key")).not.toBe(first);
  });

  it("has no refit checkbox when the dashboard does not allow refit requests", async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.forecast, null, FORECAST_DASHBOARD_ID);
    view(<ReadoutPage client={client} tab="update" />);
    expect(await screen.findByText("The model uses your numbers to update its forecast. The model itself does not change.")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /refit/ })).toBeNull();
    expect(screen.getByRole("tab", { name: "This month" })).toBeInTheDocument();
  });

  it("lists the history and opens a readout", async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.readOnly, null, MMM_DASHBOARD_ID);
    view(<ReadoutPage client={client} tab="history" />);
    const list = await screen.findByRole("list", { name: "Readouts" });
    await waitFor(() => expect(within(list).getAllByRole("button").length).toBeGreaterThan(3));
    await userEvent.click(within(list).getAllByRole("button")[0]!);
    const detail = document.querySelector("[data-readout=detail]") as HTMLElement;
    expect(await within(detail).findByRole("list", { name: /^Key numbers of / })).toBeInTheDocument();
  });
});
