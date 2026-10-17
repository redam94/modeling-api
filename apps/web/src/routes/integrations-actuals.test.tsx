/**
 * Forecasts with actuals in the Integrations hub: the Sheets card shows the Input sheet layout of
 * a forecasting endpoint, and the endpoint's Try it turns actuals and dates to forecast into the
 * call and shows what the forecast was conditioned on.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { createHandlers } from "@/mocks/handlers";
import { FORECAST_ALIAS } from "@/mocks/integrations";
import { renderApp } from "@/test/render-app";

const server = setupServer(...createHandlers());
const scored: unknown[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  server.events.on("request:start", ({ request }) => {
    if (request.method === "POST" && new URL(request.url).pathname.endsWith("/score")) {
      void request
        .clone()
        .json()
        .then((b) => scored.push(b));
    }
  });
  window.scrollTo = () => undefined;
});
beforeEach(() => {
  scored.length = 0;
  server.resetHandlers(...createHandlers());
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("forecasts with actuals", () => {
  it("shows the Input sheet layout of a forecasting endpoint on the Sheets card", async () => {
    const user = userEvent.setup();
    renderApp("/integrations");
    const select = await screen.findByLabelText("Endpoint");
    await user.selectOptions(select, FORECAST_ALIAS);
    const layout = await screen.findByRole("table", { name: "Input sheet layout" });
    const header = within(layout).getAllByRole("columnheader").map((h) => h.textContent);
    expect(header).toEqual(["month (date / level)", "sales (target)", "price (driver)"]);
    expect(within(layout).getByText("2026-10-01")).toBeInTheDocument();
    const help = screen.getByRole("group", { name: "Input sheet" });
    expect(help).toHaveTextContent(/leave it blank on the dates to forecast/);
    expect(within(help).getByText("scoreSalesForecastInputSheet()")).toBeInTheDocument();
  });

  it("turns actuals and dates to forecast into the call and shows the conditioning", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/endpoints/${FORECAST_ALIAS}?tab=schema`);
    const editor = await screen.findByRole("region", { name: "Actuals and dates to forecast" });
    expect(within(editor).getByText("conditions on actuals")).toBeInTheDocument();
    await user.type(within(editor).getByLabelText("sales, row 1"), "1520.5");
    await user.type(within(editor).getByLabelText("price, row 1"), "9.99");
    await user.type(within(editor).getByLabelText("sales, row 2"), "1610");
    expect(within(editor).getByText(/2 actuals, 6 periods to forecast/)).toBeInTheDocument();
    await user.click(within(editor).getByRole("button", { name: "Remove row 8" }));
    await user.click(within(editor).getByRole("button", { name: "Add a period" }));
    expect(within(editor).getByLabelText("month, row 8")).toHaveValue("2027-05-01");
    await user.click(within(editor).getByRole("button", { name: "Use in the call" }));
    const body = JSON.parse(screen.getByLabelText<HTMLTextAreaElement>("Call body").value) as Record<string, unknown>;
    expect(body).toMatchObject({ mode: "forecast", horizon: { dim: "month", periods: 6 }, future_covariates: "carry_last" });
    expect((body.data as { data_vars: Record<string, { data: unknown[] }> }).data_vars.sales?.data.slice(0, 3)).toEqual([1520.5, 1610, null]);
    await user.click(screen.getByRole("button", { name: "Score" }));
    const note = await screen.findByRole("note", { name: "What the forecast was conditioned on" });
    expect(note).toHaveTextContent("Conditioned on 2 actuals through 2026-11-01");
    expect(note).toHaveTextContent("the model's parameters were not re-estimated");
    expect(screen.getByRole("table", { name: "Summary of sales" })).toHaveTextContent("2026-12-01");
    await waitFor(() => expect(scored).toHaveLength(1));
  });

  it("pastes rows copied from a sheet", async () => {
    const user = userEvent.setup();
    renderApp(`/integrations/endpoints/${FORECAST_ALIAS}?tab=schema`);
    const editor = await screen.findByRole("region", { name: "Actuals and dates to forecast" });
    await user.click(within(editor).getByRole("button", { name: "Paste from a spreadsheet" }));
    const area = within(editor).getByLabelText("Rows copied from a sheet");
    await user.click(area);
    await user.paste("month\tsales\tprice\n2026-10-01\t1520.5\t9.99\n2026-11-01\t\t\n");
    await user.click(within(editor).getByRole("button", { name: "Replace the rows" }));
    expect(within(editor).getByLabelText("sales, row 1")).toHaveValue("1520.5");
    expect(within(editor).queryByLabelText("month, row 3")).not.toBeInTheDocument();
    expect(within(editor).getByText(/1 actual, 1 period to forecast/)).toBeInTheDocument();
  });

  it("explains a model that forecasts from the end of its fit", async () => {
    renderApp(`/integrations/endpoints/${FORECAST_ALIAS}?tab=schema`);
    expect(await screen.findByText(/A forecast can carry your latest actuals/)).toBeInTheDocument();
  });
});
