/**
 * The v0.13 causal templates in Guided mode: the template cards and their order, the upload
 * hint, the chart order on the results and share pages, and the geo-lift "lift test for your
 * marketing mix" card (pure builder and the rendered card).
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { parse } from "yaml";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { MetricSeries, RunParam } from "@/api/tracking";

import { buildLiftTest, overallMetric, treatedUnits } from "./lift-test";
import { dataShapePlain } from "./session";
import type { GuidedTemplate } from "./types";
import type { GuidedSession } from "./use-session";

interface QueryState {
  data: unknown;
  isPending: boolean;
  isError: boolean;
  error: unknown;
}
const queries = vi.hoisted((): { metrics: QueryState; params: QueryState; revision: QueryState; revisionAsked: (number | undefined)[] } => {
  const pending: QueryState = { data: undefined, isPending: true, isError: false, error: null };
  return { metrics: pending, params: pending, revision: pending, revisionAsked: [] };
});
vi.mock("@/api/queries", async (orig) => ({
  ...(await orig<typeof import("@/api/queries")>()),
  useRunMetrics: () => queries.metrics,
  useRunParams: () => queries.params,
  useSpecRevision: (_id: string | undefined, revision: number | undefined) => {
    queries.revisionAsked.push(revision);
    return revision === undefined ? { data: undefined, isPending: true, isError: false, error: null } : queries.revision;
  },
}));

function series(key: string, last: number, labels: Record<string, string> = {}): MetricSeries {
  return { key, last, labels, attempt: 1, count: 1, first_ts: "2026-10-20T00:00:00Z" } as unknown as MetricSeries;
}
function param(key: string, value: string): RunParam {
  return { key, value, logged_at: "2026-10-20T00:00:00Z", source: "runner" };
}

const METRICS = [series("effect_total", 1234.56789), series("effect_total_sd", 210.4), series("effect_total", 99, { unit: "geo_03" })];
const PARAMS = [param("causal.intervention_start", "2025-06-02"), param("causal.intervention_end", "2025-08-25"), param("causal.n_treated", "1")];
const tpl = (id: string, family: GuidedTemplate["family"]) => ({ id, family, title: id, description_plain: "Something." }) as unknown as GuidedTemplate;

describe("the causal template cards", () => {
  it("come after the marketing mix and before the forecasts", async () => {
    const { sortTemplates } = await import("./steps/template-step");
    const ids = ["customer_choice", "geo_lift", "weekly_forecast", "campaign_impact", "store_demand", "geo_mmm", "multi_kpi_mmm", "mediated_mmm"];
    const sorted = sortTemplates(ids.map((id) => ({ id, title: id }) as unknown as GuidedTemplate)).map((t) => t.id);
    expect(sorted).toEqual(["geo_mmm", "mediated_mmm", "multi_kpi_mmm", "campaign_impact", "geo_lift", "weekly_forecast", "store_demand", "customer_choice"]);
  });

  it("show the causal icon and what each template needs and gives", async () => {
    const { TemplateCard } = await import("./steps/template-step");
    const t = { ...tpl("geo_lift", "causal"), plugin_available: false, tasks: [] } as unknown as GuidedTemplate;
    const { container } = render(<TemplateCard template={t} />);
    expect(container.querySelector("svg.lucide-flask-conical")).not.toBeNull();
    expect(screen.getByText(/some markets with the campaign/)).toBeTruthy();
    expect(screen.getByText(/ready for your marketing mix/)).toBeTruthy();
  });

  it("describe the upload: one series for a campaign, markets for a geo test", () => {
    const campaign = dataShapePlain(tpl("campaign_impact", "causal"));
    expect(campaign).toMatch(/one row for each week \(or day\), before and after the start/);
    expect(campaign).toMatch(/control columns/);
    expect(campaign).not.toMatch(/MFF|Meridian/);
    const geo = dataShapePlain(tpl("geo_lift", "causal"));
    expect(geo).toMatch(/one row for each market and week/);
    expect(geo).toMatch(/Some markets got the campaign/);
  });
});

describe("the causal charts", () => {
  it("come first on the results page: the effect, then the donors", async () => {
    const { reportPlots } = await import("./steps/report-step");
    const ids = (xs: string[]) => reportPlots(xs.map((id) => ({ id }))).map((p) => p.id);
    expect(ids(["core.convergence", "causal.pre_fit", "causal.donor_weights", "causal.impact"])).toEqual(["causal.impact", "causal.donor_weights"]);
    expect(ids(["causal.pre_fit", "causal.event_study", "causal.impact"])).toEqual(["causal.impact", "causal.event_study"]);
    expect(ids(["core.convergence", "causal.pre_fit"])).toEqual(["causal.pre_fit"]);
  });

  it("can be embedded: causal recipes are platform recipes", async () => {
    // regression: the Share card filters with isPlatformRecipe first, and "causal" was not in the list
    const { isPlatformRecipe } = await import("./steps/share-step");
    for (const id of ["causal.impact@1.0.0", "causal.donor_weights", "causal.pre_fit", "causal.event_study@1"]) expect(isPlatformRecipe(id)).toBe(true);
    expect(isPlatformRecipe("sci.heatmap@1")).toBe(false);
    expect(isPlatformRecipe("causal")).toBe(false);
  });

  it("are offered first for sharing, the diagnostics last", async () => {
    const { shareOrder } = await import("./steps/share-step");
    const input = ["core.convergence", "causal.pre_fit", "causal.donor_weights", "causal.event_study", "causal.impact"];
    expect(shareOrder(input.map((id) => ({ id }))).map((p) => p.id)).toEqual([
      "causal.impact",
      "causal.event_study",
      "causal.donor_weights",
      "causal.pre_fit",
      "core.convergence",
    ]);
  });
});

describe("the lift test from a geo test", () => {
  const data = { metrics: METRICS, params: PARAMS, treated: ["geo_03"] };

  it("reads the run-level effect, not the per-market series", () => {
    expect(overallMetric(METRICS, "effect_total")).toBeCloseTo(1234.56789);
    expect(overallMetric(METRICS, "missing")).toBeNull();
  });

  it("reads the treated markets from a list or from the Guided text answer", () => {
    expect(treatedUnits({ model: { settings: { treated: "geo_03, geo_07 " } } })).toEqual(["geo_03", "geo_07"]);
    expect(treatedUnits({ model: { settings: { treated: ["geo_01"] } } })).toEqual(["geo_01"]);
    expect(treatedUnits({})).toEqual([]);
  });

  it("builds a calibration.lift_tests entry that mmm.bayesian accepts", () => {
    const r = buildLiftTest(data, { channel: " TV ", spend: "50000", useGeo: true });
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.entry).toEqual({
      id: "geo-lift-tv-2025-06-02",
      channel: "TV",
      geo: "geo_03",
      start: "2025-06-02",
      end: "2025-08-25",
      spend_delta: 50000,
      kpi_delta: 1234.57,
      se: 210.4,
    });
    expect(r.entry.id).toMatch(/^[a-z0-9][a-z0-9_-]{0,62}$/);
    const doc = parse(r.yaml) as { calibration: { lift_tests: Record<string, unknown>[] } };
    expect(doc.calibration.lift_tests).toEqual([r.entry]);
  });

  it("is national without the market, or with several treated markets", () => {
    const off = buildLiftTest(data, { channel: "tv", spend: "-20000", useGeo: false });
    expect(off.ok && off.entry.geo).toBe(undefined);
    expect(off.ok && off.entry.spend_delta).toBe(-20000);
    const two = buildLiftTest({ ...data, treated: ["a", "b"] }, { channel: "tv", spend: "1", useGeo: true });
    expect(two.ok && "geo" in two.entry).toBe(false);
  });

  it("says what is missing", () => {
    const empty = buildLiftTest(data, { channel: "", spend: "0", useGeo: true });
    expect(empty.ok).toBe(false);
    if (!empty.ok) expect(empty.problems).toEqual(["Type the channel of the campaign.", "Type the spend change as a number that is not zero. Use a negative number for a holdout."]);
    const noMetrics = buildLiftTest({ ...data, metrics: [] }, { channel: "tv", spend: "1", useGeo: true });
    expect(!noMetrics.ok && noMetrics.problems[0]).toMatch(/no effect metrics/);
    const open = buildLiftTest({ ...data, params: [param("causal.intervention_start", "2025-06-02"), param("causal.intervention_end", "")] }, { channel: "tv", spend: "1", useGeo: true });
    expect(!open.ok && open.problems[0]).toMatch(/start date and an end date/);
    const periods = buildLiftTest({ ...data, params: [param("causal.intervention_start", "60"), param("causal.intervention_end", "71")] }, { channel: "tv", spend: "1", useGeo: true });
    expect(periods.ok).toBe(false);
    const flat = buildLiftTest({ ...data, metrics: [series("effect_total", 5), series("effect_total_sd", 0)] }, { channel: "tv", spend: "1", useGeo: true });
    expect(!flat.ok && flat.problems[0]).toMatch(/range of the effect is zero/);
  });
});

describe("the lift test card", () => {
  const session = {
    spec: { id: "spec_1" },
    latest: { revision: 2 },
    lastGoodFit: { id: "run_1", spec_revision: 2 },
    document: { model: { settings: { treated: "geo_03" } } },
    template: { id: "geo_lift" },
  } as unknown as GuidedSession;

  beforeEach(() => {
    queries.metrics = { data: { items: METRICS, more: false }, isPending: false, isError: false, error: null };
    queries.params = { data: { items: PARAMS, more: false }, isPending: false, isError: false, error: null };
    queries.revision = { data: undefined, isPending: true, isError: false, error: null };
    queries.revisionAsked = [];
  });

  it("takes the market from the revision that the fit used, not from later changes", async () => {
    // regression: the latest revision (geo_05) was paired with the effect measured for geo_03
    const { LiftTestCard } = await import("./steps/report-step");
    const user = userEvent.setup();
    const changed = { ...session, latest: { revision: 3 }, document: { model: { settings: { treated: "geo_05" } } } } as unknown as GuidedSession;
    const view = render(<LiftTestCard session={changed} />);
    expect(queries.revisionAsked).toContain(2);
    expect(screen.queryByText(/geo_05/)).toBeNull();
    queries.revision = { data: { revision: 2, document: { model: { settings: { treated: "geo_03" } } } }, isPending: false, isError: false, error: null };
    view.rerender(<LiftTestCard session={changed} />);
    expect(screen.getByText("The lift test uses the markets of the last fit, not your latest changes.")).toBeTruthy();
    await user.type(screen.getByLabelText("Channel"), "tv");
    await user.type(screen.getByLabelText("Spend change in the test"), "50000");
    const yaml = screen.getByTestId("lift-test-yaml").textContent ?? "";
    expect(yaml).toMatch(/geo: geo_03/);
    expect(yaml).not.toMatch(/geo_05/);
  });

  it("does not make a national lift test while the fitted revision loads", async () => {
    const { LiftTestCard } = await import("./steps/report-step");
    const changed = { ...session, latest: { revision: 3 }, document: { model: { settings: { treated: "geo_05, geo_06" } } } } as unknown as GuidedSession;
    render(<LiftTestCard session={changed} />);
    expect(screen.queryByTestId("lift-test-yaml")).toBeNull();
    expect(screen.queryByText(/the lift test is for the whole country/)).toBeNull();
  });

  it("asks for the channel and the spend change, then shows the YAML to copy", async () => {
    const { LiftTestCard } = await import("./steps/report-step");
    const user = userEvent.setup();
    render(<LiftTestCard session={session} />);
    expect(screen.getByText("Lift test for your marketing mix")).toBeTruthy();
    expect(screen.getByText("Type the channel of the campaign.")).toBeTruthy();
    expect(screen.queryByTestId("lift-test-yaml")).toBeNull();
    await user.type(screen.getByLabelText("Channel"), "tv");
    await user.type(screen.getByLabelText("Spend change in the test"), "50000");
    const yaml = screen.getByTestId("lift-test-yaml").textContent ?? "";
    expect(yaml).toMatch(/lift_tests:/);
    expect(yaml).toMatch(/geo: geo_03/);
    expect(screen.getByRole("button", { name: "Copy the lift test" })).toBeTruthy();
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByTestId("lift-test-yaml").textContent).not.toMatch(/geo:/);
  });

  it("shows an error when the run results do not load", async () => {
    queries.metrics = { data: undefined, isPending: false, isError: true, error: new Error("boom") };
    const { LiftTestCard } = await import("./steps/report-step");
    render(<LiftTestCard session={session} />);
    expect(screen.getByText("The app could not load the results of the fit")).toBeTruthy();
  });
});
