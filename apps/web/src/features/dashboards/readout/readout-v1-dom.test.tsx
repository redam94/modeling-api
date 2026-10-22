/**
 * The layout 1 readout keeps its v0.16 DOM (plan §5 WP11): the MMM and forecast mocks render
 * every tab exactly as before the layout 2 readout landed. The snapshot was taken on the v0.16
 * tree; a change here is a change clients of a layout 1 dashboard (and old History readouts) see.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { setupServer } from "msw/node";
import type * as React from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/overlays";
import { FORECAST_DASHBOARD_ID, MMM_DASHBOARD_ID, MOCK_SHARE_PASSCODE, MOCK_SHARE_TOKENS } from "@/mocks/dashboards";
import { createHandlers } from "@/mocks/handlers";

import { openShareSession, refreshShareSession, shareClient } from "../client";
import type { DashboardClient, ShareSessionHandle, TabKey } from "../types";
import { ReadoutPage } from "./readout-page";

const server = setupServer(...createHandlers());
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
beforeEach(() => localStorage.setItem("mapi.mock.readoutMs", "40"));
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

/** Wait until the tab has its content (no loading line left). */
async function settled(tab: TabKey) {
  await screen.findByRole("heading", { level: 1 });
  await waitFor(() => expect(document.querySelector(`[data-tab=${tab}], [data-readout=lead]`)).not.toBeNull());
}

describe("layout 1 readout DOM", () => {
  it("MMM: This week", async () => {
    const client = await clientFor(MOCK_SHARE_TOKENS.readOnly, null, MMM_DASHBOARD_ID);
    const { container } = view(<ReadoutPage client={client} />);
    await settled("this_period");
    await screen.findByRole("img", { name: /^Change in revenue by channel: TV / });
    expect(container).toMatchSnapshot();
  });

  for (const tab of ["trends", "scenarios", "update", "history"] as const) {
    it(`MMM: ${tab}`, async () => {
      const client = await clientFor(MOCK_SHARE_TOKENS.inputs, MOCK_SHARE_PASSCODE, MMM_DASHBOARD_ID);
      const { container } = view(<ReadoutPage client={client} tab={tab} />);
      await settled(tab);
      if (tab === "history") {
        const list = await screen.findByRole("list", { name: "Readouts" });
        await waitFor(() => expect(within(list).getAllByRole("button").length).toBeGreaterThan(3));
      }
      if (tab === "scenarios") await screen.findByRole("slider", { name: "Paid search" });
      if (tab === "update") await screen.findByRole("button", { name: "Send" });
      await waitFor(() => expect(screen.queryAllByRole("status")).toHaveLength(0));
      expect(container).toMatchSnapshot();
    });
  }

  for (const tab of ["this_period", "update", "history"] as const) {
    it(`forecast: ${tab}`, async () => {
      const client = await clientFor(MOCK_SHARE_TOKENS.forecast, null, FORECAST_DASHBOARD_ID);
      const { container } = view(<ReadoutPage client={client} tab={tab} />);
      await settled(tab);
      if (tab === "update") await screen.findByRole("button", { name: "Send" });
      if (tab === "history") {
        const list = await screen.findByRole("list", { name: "Readouts" });
        await waitFor(() => expect(within(list).getAllByRole("button").length).toBeGreaterThan(0));
      }
      await waitFor(() => expect(screen.queryAllByRole("status")).toHaveLength(0));
      expect(container).toMatchSnapshot();
    });
  }
});
