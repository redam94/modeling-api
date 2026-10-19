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
import { guidedTemplateForModel, startTarget } from "@/features/about/start";
import { specStarterFor } from "@/features/about/spec-starter";
import { createHandlers } from "@/mocks/handlers";
import { DS_ID, MODEL_ID } from "@/mocks/fixtures";
import { SCI_DEMO_ID, SCI_DEMO_NOT_APPLICABLE, SCI_DEMO_TREE_ID } from "@/mocks/explore-sci-demo";
import { DATA_SECTION_META } from "@/features/explore/sections-meta";
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
    expect(startTarget("workflow_template", "workflow.ts_readiness_arimax")).toMatchObject({ to: "/workflows/new", search: { template: "ts_readiness_arimax" } });
    expect(startTarget("workflow_template", "workflow.ts_readiness_arimax", "guided")).toMatchObject({ to: "/workflows/recipe", search: { template: "ts_readiness_arimax" } });
    expect(startTarget("check", "convergence")).toMatchObject({ to: "/governance", search: { tab: "checks" } });
    expect(startTarget("step", "core.resample")).toMatchObject({ search: { about: "step:core.resample" } });
  });

  it("starts a model in Guided mode through the Guided template that uses it, or explains Expert mode", () => {
    const templates = [{ id: "store_demand", plugin: "tabular.regressor@^1" }, { id: "geo_mmm", plugin: "mmm.bayesian@^1" }];
    expect(guidedTemplateForModel(templates, "tabular.regressor")).toBe("store_demand");
    expect(guidedTemplateForModel(templates, "mmm.bayesian")).toBe("geo_mmm");
    expect(guidedTemplateForModel(templates, "ts.ets")).toBeNull();
    expect(startTarget("model", "tabular.regressor", "guided", { templateForModel: "store_demand" })).toMatchObject({ kind: "guided", to: "/guided/new/$templateId", params: { templateId: "store_demand" } });
    expect(startTarget("model", "ts.ets", "guided", { templateForModel: null })).toMatchObject({ kind: "expert", to: "/build/new", search: { model: "ts.ets" }, locked: false });
    expect(startTarget("model", "ts.ets", "guided", { templateForModel: null, locked: true })).toMatchObject({ kind: "expert", locked: true });
    // Expert mode is unchanged.
    expect(startTarget("model", "tabular.regressor", "expert", { templateForModel: "store_demand" })).toMatchObject({ kind: "spec", to: "/build/new" });
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

  it("never drops a Guided user into the Expert spec editor without saying so", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    const { router } = renderApp("/choose?goal=forecast&data_shape=panel&kind=model");
    const list = await screen.findByRole("list", { name: "Recommendations" }, T);
    // A model with a Guided template starts that template.
    const demand = await waitFor(() => {
      const card = list.querySelector<HTMLElement>('[data-recommendation="tabular.regressor"]');
      expect(card).not.toBeNull();
      return card!;
    }, T);
    expect(within(demand).getByRole("link", { name: "Start with this" })).toHaveAttribute("href", "/guided/new/store_demand");
    // No card links straight to the Expert editor.
    for (const link of within(list).queryAllByRole("link")) expect(link.getAttribute("href") ?? "").not.toMatch(/^\/build\/new/);
    // A model without one says it needs Expert mode, and switching is one deliberate click.
    const expertOnly = within(list).getAllByRole("button", { name: "Set up in Expert mode" });
    expect(expertOnly.length).toBeGreaterThan(0);
    const card = expertOnly[0]!.closest<HTMLElement>("[data-recommendation]")!;
    expect(within(card).getByText(/Expert mode shows every setting/)).toBeInTheDocument();
    await userEvent.click(expertOnly[0]!);
    await waitFor(() => expect(router.state.location.pathname).toBe("/build/new"));
    expect(router.state.location.search).toMatchObject({ model: card.dataset.recommendation });
    expect(useUi.getState().persona).toBe("expert");
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
    expect(await screen.findByText(/^Half of the values of revenue are between/, undefined, T)).toBeInTheDocument();
    const stats = screen.getByText("Show the statistics", { selector: "summary" }).closest("details")!;
    expect(stats.open).toBe(false);
  });
});

describe("the Model Explorer", () => {
  it("lists what applies, with governance evidence and lineage", async () => {
    renderApp(`/models/${MODEL_ID}/versions/1/explore?section=governance`);
    const checks = await screen.findByRole("list", { name: "Check results" }, T);
    // The latest validation of the version (the governance mock's, awaiting a reviewer).
    expect(checks.querySelector('[data-check="null.permutation_target"]')).toHaveAttribute("data-status", "pass");
    expect(screen.getAllByText("Awaiting approval").length).toBeGreaterThan(0);
    const nav = screen.getByRole("navigation", { name: "Model Explorer sections" });
    expect(nav.querySelector('[data-section="irf"]')).toHaveAttribute("data-status", "skipped");
    expect(nav.querySelector('[data-section="forecast"]')).toHaveAttribute("data-status", "missing");
  });

  it("shows the impulse responses of a VECM with pickers", async () => {
    renderApp(`/models/${EXTRA_MODEL_IDS.vecm}/versions/1/explore?section=irf`);
    expect(await screen.findByLabelText("A shock to", undefined, T)).toBeInTheDocument();
    expect(screen.getByText(/^After an unexpected increase in/)).toBeInTheDocument();
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

  it("speaks plainly to a Guided user and keeps the solver's words for experts", async () => {
    useUi.setState({ persona: "guided", personas: {} });
    renderApp(`/models/${MODEL_ID}/versions/1/optimize`);
    await userEvent.click(await screen.findByRole("button", { name: "Optimise the budget" }, T));
    const plan = await screen.findByTestId("plan-result", undefined, { timeout: 8000 });
    expect(plan).toHaveTextContent("Goal: the most likely revenue.");
    const expertNotes = within(plan).getByText("For experts: how the plan was found").closest("details")!;
    expect(expertNotes.open).toBe(false);
    expect(expertNotes).toHaveTextContent("expected KPI (posterior mean)");
    expect(expertNotes).toHaveTextContent("greedy allocator");
    // Outside the expert notes, no solver or statistics words.
    const plainText = [...plan.querySelectorAll<HTMLElement>(":scope > *")].filter((el) => !el.contains(expertNotes)).map((el) => el.textContent ?? "").join(" ");
    expect(plainText).not.toMatch(/posterior|greedy|allocator|Marginal ROI|concave/i);
    // Ranges whose two ends differ.
    for (const m of plan.textContent.matchAll(/90% likely (\S+) – (\S+?)(?=Expected|Chance|$)/g)) expect(m[1]).not.toBe(m[2]);
    expect(plan).toHaveTextContent("90% likely 1.305M – 1.312M");
  });

  it("refuses models that do not optimise budgets", async () => {
    renderApp(`/models/${EXTRA_MODEL_IDS.arimax}/versions/1/optimize`);
    expect(await screen.findByText("This model does not optimise budgets", undefined, T)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------ scientific data

const SCI_ID = SCI_DEMO_ID;
const NOT_APPLICABLE = SCI_DEMO_NOT_APPLICABLE;

describe("the Data Explorer on scientific data", () => {
  // jsdom has no canvas: the field viewer paints nothing (fields.test.ts tests the pixels)
  beforeAll(() => {
    HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
  });

  it("lists the sections by relevance, hides what does not apply and links the shapes", async () => {
    const { router } = renderApp(`/datasets/${SCI_ID}/versions/1/explore`);
    const chips = await screen.findByTestId("shape-chips", undefined, T);
    const nav = screen.getByRole("navigation", { name: "Data Explorer sections" });
    // the picture sections come before the time-series ones, in the index's order
    const listed = [...nav.querySelectorAll("[data-section]:not([data-reason])")].map((e) => e.getAttribute("data-section"));
    expect(listed.slice(0, 2)).toEqual(["overview", "structure"]);
    expect(within(nav).getByText("Pictures of the data")).toBeInTheDocument();
    for (const s of NOT_APPLICABLE) expect(listed).not.toContain(s);
    // "Not for this data" holds them, closed
    const hidden = within(nav).getByTestId("nav-not-applicable");
    expect(hidden).toHaveTextContent(`Not for this data (${NOT_APPLICABLE.length})`);
    expect((hidden as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText(/views are ready|Every view is ready/)).toBeInTheDocument();
    // a shape chip opens its section
    await userEvent.click(within(chips).getByRole("button", { name: /^Image/ }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ section: "images" }));
    expect(await screen.findByTestId("field-viewer", undefined, T)).toBeInTheDocument();
  });

  it("opens a section that does not apply from its link, with the reason", async () => {
    renderApp(`/datasets/${SCI_ID}/versions/1/explore?section=stl`);
    const skipped = await screen.findByTestId("section-skipped", undefined, T);
    expect(skipped).toHaveTextContent("Not available: the data has no time dimension.");
    const hidden = screen.getByTestId("nav-not-applicable");
    expect((hidden as HTMLDetailsElement).open).toBe(true);
    expect(within(hidden).getByRole("button", { name: /Trend and season/ })).toHaveAttribute("aria-current", "page");
  });

  it.each([
    ["structure", "Variables and their dimensions"],
    ["images", null],
    ["profiles", "cells along position"],
    ["map", "Map of cases"],
    ["network", "Ties of cowork"],
    ["tree", "Tree of node"],
    ["circular", "Rose of wind_from"],
    ["points", "chip: Colour of each point"],
  ] as const)("renders the %s section from its view", async (section, plotTitle) => {
    renderApp(`/datasets/${SCI_ID}/versions/1/explore?section=${section}`);
    expect(await screen.findByTestId("plain-summary", undefined, T)).toBeInTheDocument();
    expect(screen.getByText(DATA_SECTION_META[section].question)).toBeInTheDocument();
    if (plotTitle) {
      const frame = screen.getAllByTestId("plot-frame").find((f) => f.getAttribute("title") === plotTitle);
      expect(frame, `plot "${plotTitle}"`).toBeDefined();
    } else {
      expect(screen.getByTestId("field-viewer")).toBeInTheDocument();
    }
  });

  it("gives each node of a tree dataset its own sections", async () => {
    const { router } = renderApp(`/datasets/${SCI_DEMO_TREE_ID}/versions/1/explore?node=%2Flinks&section=network`);
    expect(await screen.findByText(/^The network of cowork has 4 nodes/, undefined, T)).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Data Explorer sections" });
    expect(nav.querySelector('[data-section="network"]:not([data-reason])')).not.toBeNull();
    expect(nav.querySelector('[data-section="images"]')).toHaveAttribute("data-reason", "not_applicable");
    // the other node: the image is there, the network does not apply
    await userEvent.selectOptions(screen.getByLabelText("Node"), "/image");
    await waitFor(() => expect(router.state.location.search).toMatchObject({ node: "/image" }));
    await userEvent.click(await screen.findByRole("button", { name: /Images and fields/ }, T));
    expect(await screen.findByTestId("field-viewer", undefined, T)).toBeInTheDocument();
    const nav2 = screen.getByRole("navigation", { name: "Data Explorer sections" });
    expect(nav2.querySelector('[data-section="network"]')).toHaveAttribute("data-reason", "not_applicable");
  });
});
