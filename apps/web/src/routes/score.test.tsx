import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { env } from "@/env";
import { createHandlers } from "@/mocks/handlers";
import editorGen from "@/mocks/editor.gen.json";
import { MODEL_ID, modelVersion } from "@/mocks/fixtures";
import { routeTree } from "@/router";

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined; // jsdom: the router restores scroll on navigation
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
});
