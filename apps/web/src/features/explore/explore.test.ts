/**
 * Explorer logic on the views the runner really computes (`explorer.gen.json`): format
 * checks, merging the ingest/fit index with an explore run, plain readings and the plot
 * builders (JSON specs whose data travels as named datasets).
 */
import { describe, expect, it } from "vitest";

import { FIXTURE } from "@/mocks/explore";

import { mergeSections, type Exploration } from "./api";
import { bestByMetric, parseModelRef } from "./compare";
import { endOfHorizon, groupTests, paramLabel } from "./model-sections";
import {
  backtestReading,
  bandCoverage,
  ccfReading,
  comparisonReading,
  correlationPairs,
  correlationReading,
  diagnosticsReading,
  distributionReading,
  dmPair,
  leadReading,
  missingnessReading,
  qualityReading,
  stationarityReading,
  stlReading,
  suggestionKey,
  vifReading,
} from "./plain";
import { barsPlot, fevdPlot, fitPlot, histogramPlot, irfPlot, lagPlot, linesPlot, missingSharePlot, stlPlot, xType } from "./plots";
import { periodUnit } from "./sections-meta";
import { DATA_SECTIONS, facetLabel, parseIndex, parseView, seriesLabel, ViewFormatError, type SectionViews } from "./views";

const ds = FIXTURE.dataset;
const mmm = FIXTURE.models["model://default/eu-geo-mmm@v1"]!;
const vecm = FIXTURE.models["model://default/macro-vecm@v1"]!;
const arimax2 = FIXTURE.models["model://default/sales-arimax@v2"]!;

function view<K extends keyof SectionViews>(section: K, raw: unknown): SectionViews[K] {
  return parseView(section, raw);
}

describe("views", () => {
  it("accepts every fixture view under its section and refuses other formats", () => {
    for (const part of [ds.ingest, ds.explore, mmm.fit, vecm.fit, arimax2.explore]) {
      for (const [section, body] of Object.entries(part.views)) {
        expect(() => parseView(section as keyof SectionViews, body)).not.toThrow();
      }
    }
    expect(() => parseView("stl", ds.ingest.views.overview)).toThrow(ViewFormatError);
    expect(() => parseView("overview", [1, 2])).toThrow(ViewFormatError);
    expect(parseIndex(ds.ingest.index).sections?.length).toBe(7);
    expect(() => parseIndex({ format: "x", sections: [] })).toThrow(ViewFormatError);
  });

  it("labels series and facets like the platform recipes", () => {
    expect(facetLabel({})).toBe("all");
    expect(seriesLabel("revenue", { geo: "north" })).toBe("revenue (geo=north)");
    expect(suggestionKey("revenue", { geo: "north" })).toBe("revenue[north]");
    expect(periodUnit("W-SUN")).toBe("week");
    expect(periodUnit("MS")).toBe("month");
    expect(periodUnit(null)).toBe("period");
  });
});

describe("mergeSections", () => {
  const base = parseIndex(ds.ingest.index);
  it("marks what the ingest did not compute as missing", () => {
    const states = mergeSections(DATA_SECTIONS, base, null);
    expect(states.find((s) => s.section === "overview")).toMatchObject({ status: "ok", origin: "base" });
    expect(states.find((s) => s.section === "stl")).toMatchObject({ status: "missing" });
  });

  it("prefers a finished run's result, but never a skipped one over a good base view", () => {
    const run = {
      done: true,
      index: {
        target: "dataset",
        ref: ds.ref,
        sections: [
          { section: "stl", view: "ui/explore/stl.json", status: "ok", plots: "ui/explore/plots/stl.json" },
          { section: "overview", view: "ui/explore/overview.json", status: "skipped", message_plain: "no" },
        ],
      },
    } as unknown as Exploration;
    const states = mergeSections(DATA_SECTIONS, base, run);
    expect(states.find((s) => s.section === "stl")).toMatchObject({ status: "ok", origin: "run", hasPlots: true });
    expect(states.find((s) => s.section === "overview")).toMatchObject({ status: "ok", origin: "base" });
    const pending = mergeSections(DATA_SECTIONS, base, { ...run, done: false });
    expect(pending.find((s) => s.section === "stl")?.status).toBe("missing");
  });
});

describe("plain readings (data)", () => {
  it("reads quality, missingness and outliers", () => {
    expect(qualityReading(view("quality", ds.ingest.views.quality)).tone).toBe("good");
    expect(qualityReading({ items: [{ code: "NEGATIVE_SPEND", severity: "error", message_plain: "x" }] } as never)).toMatchObject({ tone: "bad" });
    // things to know are not "no problems found" (regression: the page said so above a list of them)
    const infoOnly = qualityReading({ items: [{ code: "MISSING", severity: "info", message_plain: "0.3% missing" }, { code: "OUTLIERS", severity: "info", message_plain: "16.5% extreme" }] } as never);
    expect(infoOnly).toMatchObject({ tone: "good" });
    expect(infoOnly.text).toBe("No problems to fix; 2 things to know.");
    expect(qualityReading({ items: [] } as never).text).toBe("No data problems found.");
    expect(missingnessReading(view("missingness", ds.ingest.views.missingness)).text).toMatch(/^spend \(0\.\d%\) has missing values; spend the most\. The longest gap is 4 periods in spend\./);
  });

  it("reads distributions, correlations, leads, stationarity, seasons and overlap", () => {
    const dist = view("distributions", ds.ingest.views.distributions);
    expect(distributionReading(dist.items![0]!).text).toMatch(/^Half of the values of revenue lie between/);
    const corr = view("correlation", ds.ingest.views.correlation);
    expect(correlationPairs(corr)[0]).toMatchObject({ a: "spend[social]", b: "price" });
    expect(correlationReading(corr)).toMatchObject({ tone: "warn" });
    const ccf = view("ccf", ds.explore.views.ccf);
    expect(ccfReading(ccf).text).toContain("lead");
    expect(leadReading(ccf.items![0]!, "revenue", "week")).toMatch(/^spend\[tv\] leads: revenue falls 7 weeks after it moves/);
    expect(stationarityReading(view("stationarity", ds.explore.views.stationarity)).text).toMatch(/series return to a stable level/);
    expect(stlReading(view("stl", ds.explore.views.stl).items![0]!).text).toContain("is strong (0.82): model the season explicitly");
    expect(vifReading(view("vif", ds.explore.views.vif)).tone).toMatch(/good|warn/);
  });
});

describe("plain readings (models)", () => {
  it("reads diagnostics, the fit band and residual tests", () => {
    expect(diagnosticsReading(view("diagnostics", mmm.fit.views.diagnostics))).toMatchObject({ tone: "bad", text: expect.stringContaining("R-hat 1.099") as unknown });
    expect(diagnosticsReading(view("diagnostics", arimax2.fit.views.diagnostics)).text).toBe("The fit converged.");
    const fit = view("fit", mmm.fit.views.fit);
    expect(bandCoverage(fit)).toBeGreaterThan(0.3);
    const grouped = groupTests(view("residuals", mmm.fit.views.residuals).tests ?? []);
    expect(grouped.map((g) => g.text)[0]).toBe("No pattern over time is left (every series).");
  });

  it("reads backtests, forecasts and comparisons", () => {
    expect(backtestReading(view("backtest", arimax2.explore.views.backtest)).text).toMatch(/fold/);
    const forecast = view("forecast", mmm.explore.views.forecast);
    expect(endOfHorizon(forecast.forecasts![0]!)).toMatch(/^By .+, revenue is most likely about .+ \(90% likely between/);
    const comparison = view("comparison", arimax2.explore.views.comparison);
    expect(comparisonReading(comparison).text).toBe("model://default/sales-arimax@v2 has the lowest MAPE. But the differences in accuracy could be chance (Diebold-Mariano).");
    expect(dmPair(comparison.tests![0]!)).toEqual(["model://default/sales-arimax@v2", "model://default/sales-arimax@v1"]);
    expect(bestByMetric(comparison)).toMatchObject({ mape: "model://default/sales-arimax@v2", n_params: null });
    expect(parseModelRef("model://default/sales-arimax@v2")).toEqual({ project: "default", name: "sales-arimax", version: 2 });
    expect(paramLabel({ name: "beta", coords: { channel: "tv" } })).toBe("beta[tv]");
  });
});

describe("plot builders", () => {
  const datasetsOnly = (spec: unknown) => JSON.stringify(spec).includes('"url"');
  it("build Vega-Lite specs with named datasets only", () => {
    const series = view("timeseries", ds.ingest.views.timeseries).series!;
    const plots = [
      linesPlot([{ label: "a", coords: series[0]!.coords, values: series[0]!.values }]),
      histogramPlot(view("distributions", ds.ingest.views.distributions).items![0]!.histogram!, { title: "revenue" }),
      lagPlot(view("ccf", ds.explore.views.ccf).items![0]!, { yTitle: "Correlation" }),
      stlPlot(view("stl", ds.explore.views.stl).items![0]!),
      barsPlot([{ label: "tv", value: 3 }], { title: "VIF", ruleAt: 10 }),
      fitPlot(view("fit", mmm.fit.views.fit).series![0]!, view("fit", mmm.fit.views.fit).fitted![0]!, "2024-09-01", { yTitle: "revenue" }),
      irfPlot(view("irf", vecm.fit.views.irf), 0, 1),
      fevdPlot(view("fevd", vecm.fit.views.fevd).variables, view("fevd", vecm.fit.views.fevd).shares[0]!),
    ];
    for (const p of plots) {
      expect(datasetsOnly(p.spec)).toBe(false);
      expect(Object.keys(p.datasets).length).toBeGreaterThan(0);
    }
    expect(xType(["2024-01-01"])).toBe("temporal");
    expect(xType([1, 2])).toBe("quantitative");
    expect(xType(["a"])).toBe("ordinal");
    expect(plots[1]!.datasets.bins).toHaveLength(30);
  });

  it("let Vega choose the precision of percent axes (no duplicate rounded ticks)", () => {
    // Regression: a fixed ".1~%" on a 0-0.3% range rendered "0% 0% 0% 0.1% 0.1% ..."
    const spec = JSON.stringify(missingSharePlot([{ name: "spend", missing_share: 0.003 }, { name: "price", missing_share: 0.0004 }])!.spec);
    expect(spec).toContain('"axis":{"format":"~%"}');
    expect(spec).toContain('"format":".2~%"'); // the tooltip keeps a readable precision
    expect(missingSharePlot([{ name: "spend", missing_share: 0 }])).toBeNull();
  });
});
