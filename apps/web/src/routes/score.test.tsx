import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { env } from "@/env";
import { createHandlers, createState } from "@/mocks/handlers";
import editorGen from "@/mocks/editor.gen.json";
import { EXTRA_MODEL_IDS } from "@/mocks/explore";
import { MODEL_ID, modelVersion } from "@/mocks/fixtures";
import { gridScoreHandlers } from "@/mocks/score-fields";
import { routeTree } from "@/router";

const state = createState();
const server = setupServer(...createHandlers(state));
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined; // jsdom: the router restores scroll on navigation
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext; // jsdom has no canvas
});
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function renderAt(url: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [url] }), context: { queryClient } });
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

describe("ScorePage", () => {
  it("shows a loading state while the model version loads", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v`, async () => {
        await delay(300);
        return HttpResponse.json(modelVersion);
      }),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    expect(await screen.findByLabelText("Loading")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /^Forecast$/ }, { timeout: 3000 })).toBeInTheDocument();
  });

  it("drops the previous version's results when the version changes", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v`, ({ params }) =>
        HttpResponse.json({ ...modelVersion, version: Number(params.v), ref: `${modelVersion.ref.replace(/@v\d+$/, "")}@v${String(params.v)}` }),
      ),
    );
    const router = renderAt(`/score?model=${MODEL_ID}&version=1`);
    await userEvent.click(await screen.findByRole("button", { name: "Add a change" }, { timeout: 3000 }));
    await userEvent.type(screen.getByRole("textbox", { name: "Selection" }), "channel=tv");
    await userEvent.click(screen.getByRole("button", { name: /^Forecast$/ }));
    const results = await screen.findByRole("region", { name: "Results" });
    await within(results).findByText("ui/plotdata/ppc_fan.arrow", undefined, { timeout: 3000 });

    await router.navigate({ to: "/score", search: { model: MODEL_ID, version: 2 } });
    await waitFor(() => expect(screen.getByText(/@v2$/)).toBeInTheDocument());
    const fresh = screen.getByRole("region", { name: "Results" });
    expect(within(fresh).queryByText("ui/plotdata/ppc_fan.arrow")).toBeNull();
    expect(within(fresh).getByText(/The results show here/)).toBeInTheDocument();
  });

  it("explains what the model needs and scores typed-in values without a dataset", async () => {
    let sent: { request?: { data?: unknown; future_covariates?: string; horizon?: unknown }; inline?: { data?: { coords: Record<string, { data: unknown[] }>; data_vars: Record<string, { dims: string[]; data: unknown }> } } } = {};
    server.use(
      http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json({ done: true, run: { id: "run_x", kind: "score", state: "succeeded" }, outputs: [] });
      }),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    const needs = await screen.findByRole("region", { name: "What this model needs" }, { timeout: 3000 });
    expect(within(needs).getByText("needed on future periods")).toBeInTheDocument();
    expect(within(needs).getByText(/3 levels: de, fr, it/)).toBeInTheDocument();
    // The grid starts with the 8 days after the fit for every geo and channel: 48 rows.
    const grid = await screen.findByRole("table", { name: "Values to score" });
    expect(within(grid).getAllByRole("row")).toHaveLength(1 + 48);
    expect(within(grid).getByLabelText("time, row 1")).toHaveValue("2026-10-01");
    // Fill spend everywhere, then forecast.
    await userEvent.type(screen.getByRole("textbox", { name: "Value to fill with" }), "1000");
    await userEvent.click(screen.getByRole("button", { name: /Fill blanks/ }));
    expect(within(grid).getByLabelText("spend, row 48")).toHaveValue("1000");
    await userEvent.click(screen.getByRole("button", { name: /^Forecast$/ }));
    await waitFor(() => expect(sent.inline).toBeDefined());
    expect(sent.request?.data).toBeUndefined();
    expect(sent.request?.future_covariates).toBe("carry_last");
    expect(sent.request?.horizon).toEqual({ dim: "time", periods: 8 });
    const data = sent.inline!.data!;
    expect(Object.keys(data.coords).sort()).toEqual(["channel", "geo", "time"]);
    expect(data.data_vars.spend!.dims).toEqual(["geo", "channel", "time"]);
    expect((data.data_vars.spend!.data as number[][][])[0]![0]![0]).toBe(1000);
  });

  it("offers No new data only for modes that the server accepts without data, and says why", async () => {
    let sent: { request?: { mode?: string; data?: unknown; scenario?: string } } = {};
    server.use(
      http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json({ done: true, run: { id: "run_x", kind: "score", state: "succeeded" }, outputs: [] });
      }),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    await screen.findByRole("table", { name: "Values to score" }, { timeout: 3000 });
    const source = () => screen.getByRole("radiogroup", { name: "Where the values come from" });
    // Forecast: the model continues from the end of the fit.
    await userEvent.click(within(source()).getByRole("radio", { name: "No new data" }));
    expect(screen.queryByTestId("no-data-reason")).toBeNull();
    // Predict: the server refuses a request without data, so the option goes and the page says why.
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Score mode" })).getByRole("radio", { name: /^Predict/ }));
    await waitFor(() => expect(within(source()).queryByRole("radio", { name: "No new data" })).toBeNull());
    expect(screen.getByTestId("no-data-reason")).toHaveTextContent(/The server needs values to score, a scenario dataset or a forecast horizon\..*select Forecast\./);
    // The form left "No new data" for typed values.
    expect(within(source()).getByRole("radio", { name: "Type values" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("No values are typed. Type one or more values, or use a dataset.")).toBeInTheDocument();
    // A scenario dataset gives the values, so No new data is possible again.
    await userEvent.click(screen.getByText("Advanced", { exact: true }));
    await userEvent.type(screen.getByLabelText("Scenario dataset (optional)"), "dataset://default/plan_2027h1@v1");
    await userEvent.click(within(source()).getByRole("radio", { name: "No new data" }));
    expect(screen.queryByTestId("no-data-reason")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /^Predict$/ }));
    await waitFor(() => expect(sent.request).toBeDefined());
    expect(sent.request).toMatchObject({ mode: "predict", scenario: "dataset://default/plan_2027h1@v1" });
    expect(sent.request?.data).toBeUndefined();
  });

  it("explains that a model without forecast mode always needs values", async () => {
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v/inputs`, () =>
        HttpResponse.json({ ...editorGen.model_inputs, snapshot: { ...editorGen.model_inputs.snapshot, score_modes: ["predict", "simulate"] } }),
      ),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    const reason = await screen.findByTestId("no-data-reason", undefined, { timeout: 3000 });
    expect(reason).toHaveTextContent("This model cannot forecast, so it always needs values to score.");
    expect(within(screen.getByRole("radiogroup", { name: "Where the values come from" })).queryByRole("radio", { name: "No new data" })).toBeNull();
  });

  it("asks a quantile model for the summaries it can give, and no draws", async () => {
    let sent: { request?: { output?: { summary?: string[]; draws?: number } } } = {};
    server.use(
      http.get(`${env.apiBase}/v1/models/:id/versions/:v/inputs`, () =>
        HttpResponse.json({ ...editorGen.model_inputs, snapshot: { ...editorGen.model_inputs.snapshot, representation: "quantiles" } }),
      ),
      http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), async ({ request }) => {
        sent = (await request.json()) as typeof sent;
        return HttpResponse.json({ done: true, run: { id: "run_x", kind: "score", state: "succeeded" }, outputs: [] });
      }),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    await screen.findByRole("table", { name: "Values to score" }, { timeout: 3000 });
    const summaries = screen.getByRole("group", { name: "Summaries" });
    expect(within(summaries).queryByRole("button", { name: "mean" })).toBeNull();
    expect(screen.getByLabelText("Draws")).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /^Forecast$/ }));
    await waitFor(() => expect(sent.request).toBeDefined());
    expect(sent.request?.output).toMatchObject({ summary: ["median", "q5", "q95"], draws: 0 });
  });

  it("shows the one-step-ahead predictions against the actuals that were sent", async () => {
    server.use(
      http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), () =>
        HttpResponse.json({
          done: true,
          run: { id: "run_x", kind: "score", state: "succeeded" },
          outputs: [],
          summary: {
            tables: [{ variable: "y_hat", columns: ["time", "mean", "hdi_90_lower", "hdi_90_upper"], rows: [["2026-10-01", 100, 90, 110], ["2026-10-02", 104, 94, 114], ["2026-10-03", 103, 92, 114]] }],
            actuals: [{ variable: "y_hat", columns: ["time", "actual"], rows: [["2026-10-01", 98], ["2026-10-02", 120]] }],
            conditioning: { dim: "time", fit_end: "2026-09-30", conditioned: true, n_actuals: 2, actuals_through: "2026-10-02", horizon_source: "payload", periods: 1, message: "Forecast updated with 2 actuals." },
          },
        }),
      ),
    );
    renderAt(`/score?model=${MODEL_ID}&version=1`);
    await userEvent.click(await screen.findByRole("button", { name: /^Forecast$/ }, { timeout: 3000 }));
    const section = await screen.findByRole("region", { name: "Predictions against your actuals: y_hat" });
    const stats = within(section).getByLabelText("How close the one-step-ahead predictions were");
    expect(stats).toHaveTextContent("Actuals compared2");
    expect(stats).toHaveTextContent("Inside the hdi 90 band50%");
    const table = within(section).getByRole("table", { name: "Actuals and predictions" });
    expect(within(table).getAllByRole("row")).toHaveLength(1 + 3);
    expect(within(table).getByText("+2")).toBeInTheDocument(); // predicted − actual
    expect(within(table).getByText(/^[-−]16$/)).toBeInTheDocument();
    expect(within(table).getByText("forecast")).toBeInTheDocument();
  });

  it("shows a gridded forecast as a map with a slider over its periods, and the other outputs by shape", async () => {
    server.use(...gridScoreHandlers(state));
    renderAt(`/score?model=${EXTRA_MODEL_IDS.fields}&version=1`);
    await userEvent.click(await screen.findByRole("button", { name: /^Forecast$/ }, { timeout: 3000 }));
    const section = await screen.findByRole("region", { name: "Results by output" }, { timeout: 3000 });
    // The grid output is the default: the field viewer with its frame slider.
    const viewer = await within(section).findByTestId("field-viewer", undefined, { timeout: 3000 });
    expect(within(viewer).getByRole("slider", { name: /period/ })).toHaveAttribute("aria-valuetext", "period 2019");
    const output = within(section).getByLabelText("Output");
    expect(within(output).getByRole("option", { name: "total (over time)" })).toBeInTheDocument();
    // The field viewer replaces the run's static field map; the fan stays.
    const results = screen.getByRole("region", { name: "Results" });
    expect(await within(results).findByText("Posterior predictive fan")).toBeInTheDocument();
    expect(within(results).queryByText("Field map")).toBeNull();

    await userEvent.selectOptions(output, "total");
    expect(within(section).queryByTestId("field-viewer")).toBeNull();
    expect(within(section).getByTitle("total over period")).toBeInTheDocument();
    expect(within(section).getByText("The band is the hdi 90 interval.")).toBeInTheDocument();

    await userEvent.selectOptions(output, "peak");
    const tiles = within(section).getByLabelText("peak: summary");
    expect(tiles).toHaveTextContent("mean9.5");
    expect(tiles).toHaveTextContent("hdi 906.25 – 13");
  });
});
