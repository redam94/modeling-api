/**
 * The M3.5 pages in the whole app against the MSW mock API (views computed by the real
 * runner): About cards and Help me choose, dataset About, the Data and Model Explorers, the
 * comparison and budget optimisation, in Expert and Guided mode.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { AboutCard } from "@/features/about/about-card";
import { startTarget } from "@/features/about/start";
import { specStarterFor } from "@/features/about/spec-starter";
import { createHandlers } from "@/mocks/handlers";
import { DS_ID, MODEL_ID } from "@/mocks/fixtures";
import { EXTRA_MODEL_IDS, FIXTURE } from "@/mocks/explore";
import { renderApp } from "@/test/render-app";
import { useUi } from "@/stores/ui";

const server = setupServer(...createHandlers());
beforeAll(() => {
  server.listen({ onUnhandledRequest: "bypass" });
  window.scrollTo = () => undefined;
});
afterEach(() => {
  server.resetHandlers();
  useUi.setState({ persona: null, personas: {} });
});
afterAll(() => server.close());

const T = { timeout: 5000 };

describe("About cards", () => {
  it("shows the plain card first and keeps the expert notes collapsed", () => {
    const sarimax = FIXTURE.abouts.find((a) => a.id === "ts.sarimax")!;
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AboutCard about={sarimax.about} kind="model" id="ts.sarimax" onRelated={() => undefined} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("article", { name: sarimax.about.title })).toBeInTheDocument();
    expect(screen.getByText("Use it when")).toBeInTheDocument();
    expect(screen.getByText(sarimax.about.example)).toBeInTheDocument();
    const details = screen.getByText("For experts: how it works").closest("details")!;
    expect(details.open).toBe(false);
  });

  it("starts each kind of item in the right place", () => {
    expect(startTarget("template", "geo_mmm")).toMatchObject({ to: "/guided/new/$templateId", params: { templateId: "geo_mmm" } });
    expect(startTarget("model", "ts.ets")).toMatchObject({ to: "/build/new", search: { model: "ts.ets" } });
    expect(startTarget("workflow_template", "workflow.ts_readiness_arimax", () => true)).toMatchObject({ href: "/workflows/new?template=ts_readiness_arimax" });
    expect(startTarget("workflow_template", "workflow.ts_readiness_arimax")).toMatchObject({ to: "/catalog", search: { about: "workflow_template:workflow.ts_readiness_arimax" } });
    expect(startTarget("check", "convergence")).toMatchObject({ search: { about: "check:convergence" } });
  });

  it("starts a spec for a model with its About as comments, safely", () => {
    const yaml = specStarterFor("ts.sarimax", { version: "1.2.0", about: { ...FIXTURE.abouts.find((a) => a.id === "ts.sarimax")!.about, title: "Evil\nkind: Workflow" } });
    expect(yaml).toContain("  use: ts.sarimax@^1");
    expect(yaml).toContain("  # Evil kind: Workflow:");
    expect(yaml.split("\n").filter((l) => l.startsWith("kind:"))).toEqual(["kind: ModelSpec"]);
  });
});

describe("Help me choose", () => {
  it("ranks starting points as the answers come in, with reasons", async () => {
    renderApp("/choose?goal=forecast");
    const list = await screen.findByRole("list", { name: "Recommendations" }, T);
    expect(within(list).getAllByText(/^Suits: forecast the future/).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "One number over time" }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Recommendations" })).getAllByText("Suits: one number over time").length).toBeGreaterThan(0));
    expect(within(list).getAllByRole("link", { name: "Start with this" }).length).toBeGreaterThan(0);
  });
});

describe("dataset About and the Data Explorer", () => {
  it("edits the owner's description next to the generated summary", async () => {
    renderApp(`/datasets/${DS_ID}`);
    expect(await screen.findByTestId("dataset-summary", undefined, T)).toHaveTextContent("Weekly data from 2023-01-01 to 2025-12-21 (156 weeks)");
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    const box = screen.getByLabelText("Describe the data for your colleagues");
    await userEvent.clear(box);
    await userEvent.type(box, "From finance.");
    await userEvent.click(screen.getByRole("button", { name: "Save description" }));
    expect(await screen.findByTestId("dataset-description", undefined, T)).toHaveTextContent("From finance.");
  });

  it("shows the ingest views and computes a missing section in an explore run", async () => {
    const { router } = renderApp(`/datasets/${DS_ID}/versions/1/explore?section=stationarity`);
    const missing = await screen.findByTestId("section-missing", undefined, T);
    await userEvent.click(within(missing).getByRole("button", { name: "Compute it now" }));
    expect(await screen.findByRole("table", { name: "Stationarity by series" }, { timeout: 8000 })).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ run: expect.stringMatching(/^run_/) as unknown });
    const nav = screen.getByRole("navigation", { name: "Data Explorer sections" });
    expect(nav.querySelector('[data-section="stl"]')).toHaveAttribute("data-status", "ok");
  });

  it("puts the plain reading first in Guided mode", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    renderApp(`/datasets/${DS_ID}/versions/1/explore?section=distributions`);
    expect(await screen.findByText(/^Half of the values of revenue lie between/, undefined, T)).toBeInTheDocument();
    const stats = screen.getByText("Show the statistics", { selector: "summary" }).closest("details")!;
    expect(stats.open).toBe(false);
  });
});

describe("the Model Explorer", () => {
  it("lists what applies, with governance evidence and lineage", async () => {
    renderApp(`/models/${MODEL_ID}/versions/1/explore?section=governance`);
    const checks = await screen.findByRole("list", { name: "Check results" }, T);
    expect(checks.querySelector('[data-check="convergence"]')).toHaveAttribute("data-status", "fail");
    expect(screen.getAllByText("Checks failed").length).toBeGreaterThan(0);
    const nav = screen.getByRole("navigation", { name: "Model Explorer sections" });
    expect(nav.querySelector('[data-section="irf"]')).toHaveAttribute("data-status", "skipped");
    expect(nav.querySelector('[data-section="forecast"]')).toHaveAttribute("data-status", "missing");
  });

  it("shows the impulse responses of a VECM with pickers", async () => {
    renderApp(`/models/${EXTRA_MODEL_IDS.vecm}/versions/1/explore?section=irf`);
    expect(await screen.findByLabelText("A shock to", undefined, T)).toBeInTheDocument();
    expect(screen.getByText(/^After an unexpected rise in/)).toBeInTheDocument();
  });
});

describe("budget optimisation", () => {
  it("explains impossible bounds, then shows the plan", async () => {
    renderApp(`/models/${MODEL_ID}/versions/1/optimize`);
    const limits = await screen.findByRole("table", { name: "Limits per channel" }, T);
    await userEvent.selectOptions(within(limits).getByLabelText("Limits for tv in"), "money");
    await userEvent.type(within(limits).getByLabelText("Minimum for tv"), "500000");
    expect(await screen.findByRole("list", { name: "Problems with the plan" })).toHaveTextContent("more than the budget");
    expect(screen.getByRole("button", { name: "Optimise the budget" })).toBeDisabled();
    await userEvent.clear(within(limits).getByLabelText("Minimum for tv"));
    await userEvent.click(screen.getByRole("button", { name: "Optimise the budget" }));
    const plan = await screen.findByTestId("plan-result", undefined, { timeout: 8000 });
    expect(within(plan).getByRole("table", { name: "Allocation" })).toHaveTextContent("search");
    expect(plan).toHaveTextContent("Move money from tv");
  });

  it("refuses models that do not optimise budgets", async () => {
    renderApp(`/models/${EXTRA_MODEL_IDS.arimax}/versions/1/optimize`);
    expect(await screen.findByText("This model does not optimise budgets", undefined, T)).toBeInTheDocument();
  });
});
