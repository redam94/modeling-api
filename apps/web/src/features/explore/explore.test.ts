/**
 * Explorer logic on the views the runner really computes (`explorer.gen.json`): format
 * checks, merging the ingest/fit index with an explore run, plain readings and the plot
 * builders (JSON specs whose data travels as named datasets).
 */
import { tableFromArrays, tableToIPC } from "apache-arrow";
import { describe, expect, it } from "vitest";

import { FIXTURE } from "@/mocks/explore";

import { mergeSections, recipePlots, type Exploration } from "./api";
import { cleanHints, hintProblem } from "./data-explorer";
import { maskWord, paintMask } from "./fields";
import { clone, scienceExample } from "./science-examples.test-helper";
import { groupField, modeCounts } from "./science-sections";
import { circularReading, imageReading, mapReading, networkReading, pointsReading, profileReading, structureReading, treeReading } from "./plain";
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
import { count, num } from "./ui";
import { DATA_SECTIONS, facetLabel, indexOrder, parseIndex, parseView, SCIENCE_SECTIONS, seriesLabel, skipReason, ViewFormatError, type SectionViews } from "./views";

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

describe("number formats", () => {
  it("shows every digit of a count (E81b: 10,292 photographs of the agouti, not 10,290)", () => {
    expect(num(10_292)).toBe("10,290");
    expect(count(10_292)).toBe("10,292");
    expect(count(null)).toBe("—");
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
    expect(infoOnly.text).toBe("The data has no problems to correct. 2 items give information.");
    expect(qualityReading({ items: [] }).text).toBe("The checks found no problems in the data.");
    expect(missingnessReading(view("missingness", ds.ingest.views.missingness)).text).toMatch(/^spend \(0\.\d%\) has missing values\. The longest gap is 4 periods in spend\./);
  });

  it("reads distributions, correlations, leads, stationarity, seasons and overlap", () => {
    const dist = view("distributions", ds.ingest.views.distributions);
    expect(distributionReading(dist.items![0]!).text).toMatch(/^Half of the values of revenue are between/);
    // the ties of a network (E21 cowork) are 0 or 1: no quartiles, no tail
    const ties = { var: "cowork", facet: {}, n: 5041, mean: 0.15, skew: 1.96, zero_share: 0.85, quantiles: { "0.25": 0, "0.5": 0, "0.75": 0 }, histogram: { edges: [0, 0.25, 0.5, 0.75, 1], counts: [4285, 0, 0, 756] } };
    expect(distributionReading(ties as never)).toEqual({ text: "cowork has only the values 0 and 1. 15% of the values are 1.", tone: "neutral" });
    expect(distributionReading({ ...ties, histogram: { edges: [0, 1, 2], counts: [10, 5] } }).text).toMatch(/^Half of the values/);
    // two values that are not 0 and 1 ({0.2, 0.7}, a dose {0, 0.5}) are not an indicator
    const two = { ...ties, var: "x", mean: 0.45, histogram: { edges: [0.2, 0.45, 0.7], counts: [55, 45] } };
    expect(distributionReading(two as never).text).not.toMatch(/only the values 0 and 1/);
    const dose = { ...ties, var: "dose", mean: 0.25, histogram: { edges: [0, 0.25, 0.5], counts: [50, 50] } };
    expect(distributionReading(dose as never).text).not.toMatch(/only the values 0 and 1/);
    const corr = view("correlation", ds.ingest.views.correlation);
    expect(correlationPairs(corr)[0]).toMatchObject({ a: "spend[social]", b: "price" });
    expect(correlationReading(corr)).toMatchObject({ tone: "warn" });
    // below 0.7: no warning, but the summary names the largest pair that the table calls "strongly" (E79)
    const weak = { ...corr, vars: ["reference_dl", "pixel_window"], matrix: [[1, 0.658], [0.658, 1]], p_values: null } as never;
    expect(correlationReading({ ...corr, vars: [], matrix: [] }).text).toBe("The data has no pair of numeric variables on the same dimensions to compare.");
    expect(correlationReading(weak)).toEqual({ tone: "good", text: "No pair of variables has a correlation of 0.7 or more. The largest correlation is reference_dl and pixel_window (0.66). Thus, it is easy for a model to find the effect of each variable." });
    const ccf = view("ccf", ds.explore.views.ccf);
    expect(ccfReading(ccf).text).toContain("lead");
    // prewhitened by default (M3.5 review): the raw view's spurious "falls 7 weeks after" peak is gone
    expect(ccf.prewhitened).toBe(true);
    expect(leadReading(ccf.items![0]!, "revenue", "week")).toMatch(/^spend\[tv\]: no clear link with revenue/);
    expect(stationarityReading(view("stationarity", ds.explore.views.stationarity)).text).toMatch(/series return to a stable level/);
    expect(stlReading(view("stl", ds.explore.views.stl).items![0]!).text).toContain("is strong (0.82). Include the season in the model.");
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
    // forecasters scored from one origin get no Diebold-Mariano verdict (M3.5 review)
    expect(comparisonReading(comparison).text).toBe("model://default/sales-arimax@v2 has the lowest MAPE.");
    expect(comparison.tests ?? []).toEqual([]);
    expect((comparison.warnings_plain ?? []).join(" ")).toContain("one forecast origin");
    expect(dmPair({ test: "test.diebold_mariano", statistic: -2, p_value: 0.01, alpha: 0.05, reject: true, decision: "different", verdict_plain: "x", details: { model_a: "a", model_b: "b" } } as never)).toEqual(["a", "b"]);
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

// ------------------------------------------------------------------------------ scientific data

function b64(bytes: Uint8Array): string {
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return btoa(bin);
}

describe("scientific data: views and index", () => {
  it("accepts the spec's example view of every new section and refuses it under another", () => {
    expect(DATA_SECTIONS).toHaveLength(21);
    for (const s of SCIENCE_SECTIONS) expect(() => scienceExample(s)).not.toThrow();
    expect(() => parseView("map", scienceExample("network"))).toThrow(ViewFormatError);
  });

  it("reads the reason, the order and the shapes of an index of v0.10, and nothing unknown", () => {
    expect(skipReason({ reason: "not_applicable" })).toBe("not_applicable");
    expect(skipReason({ reason: "because" })).toBeNull();
    expect(skipReason(null)).toBeNull();
    expect(indexOrder({ order: ["overview", "images", "params", 3, "images"] })).toEqual(["overview", "images"]);
    expect(indexOrder({})).toBeNull();
    const states = mergeSections(DATA_SECTIONS, { format: "mapi.explorer-index/v1", target: "dataset", ref: "r", sections: [{ section: "map", view: "ui/explore/map.json", status: "skipped", reason: "not_applicable", message_plain: "no polygons" }] }, null);
    expect(states.find((s) => s.section === "map")).toMatchObject({ status: "skipped", reason: "not_applicable", message: "no polygons" });
  });
});

describe("scientific data: recipe plots", () => {
  it("keeps Vega-Lite and Plotly plots, decodes inline Arrow and refuses data at a path", async () => {
    const data = b64(tableToIPC(tableFromArrays({ theta: Float64Array.from([45, 135]), r: Float64Array.from([3, 4]) }), "stream"));
    const vega = { id: "explore.images.histogram", kind: "vega-lite", title: "h", spec: { mark: "bar" }, datasets: {} };
    const plotly = { id: "sci.rose", kind: "plotly", title: "rose", spec: { data: [{ type: "barpolar", r: { $ref: "rose.r" }, theta: { $ref: "rose.theta" } }] }, datasets: { rose: { kind: "inline_arrow", data } } };
    const linked = { id: "net.graph", kind: "plotly", title: "g", spec: { data: [] }, datasets: { g: { kind: "path", path: "plots/net.arrow", format: "arrow" } } };
    const other = { id: "x", kind: "matplotlib", title: "x", spec: {} };
    const plots = await recipePlots([vega, plotly, linked, other, null, "x"]);
    expect(plots.map((p) => [p.plot.id, p.kind])).toEqual([
      ["explore.images.histogram", "vega-lite"],
      ["sci.rose", "plotly"],
    ]);
    expect(plots[1]!.datasets.rose).toEqual([
      { theta: 45, r: 3 },
      { theta: 135, r: 4 },
    ]);
    expect(await recipePlots({ not: "a list" })).toEqual([]);
  });
});

describe("scientific data: plain readings", () => {
  it("reads the structure and the images", () => {
    expect(structureReading(scienceExample("structure")).text).toMatch(/^The data has 2 shapes\. The main shape is: image: 9 x 12 cells, 44 quarter\./);
    expect(structureReading({ shapes: [], dims: [], vars: [] }).text).toMatch(/no special shape/);
    const img = scienceExample("images").fields[0]!;
    const r = imageReading(img);
    expect(r.text).toMatch(/^quakes is an image of 2 x 3 cells, with 2 frames along quarter\./);
    expect(r.text).toMatch(/at the maximum value/);
    expect(r.tone).toBe("warn");
    expect(imageReading(scienceExample("images").fields[1]!).text).toMatch(/^displacement is a vector field of 4 x 4 cells\. The picture shows block means of 2 x 2 cells\./);
  });

  it("reads profiles, maps, networks and trees", () => {
    const pr = scienceExample("profiles");
    expect(profileReading(pr.items[0]!).text).toMatch(/Each curve has its highest value at position = 25\./);
    expect(profileReading(pr.items[1]!).text).toMatch(/log scale on the horizontal axis/);
    const m = scienceExample("map");
    expect(mapReading(m, "cases").text).toMatch(/^The map has 3 areas\. The values of cases do not cluster/);
    const clustered = clone(m);
    clustered.moran = [{ var: "cases", morans_i: 0.4, p_value: 0.001, weights: "knn4" }];
    expect(mapReading(clustered, "cases")).toMatchObject({ tone: "warn" });
    expect(mapReading(clustered, "cases").text).toMatch(/4 nearest areas/);
    expect(networkReading(scienceExample("network").items[0]!).text).toMatch(/^The network of cowork has 4 nodes and 4 ties\. 67% of all possible ties are present\./);
    const t = scienceExample("tree");
    expect(treeReading(t).text).toMatch(/^The tree has 3 tips and 5 nodes in all\. All tips are at the same distance from the root\./);
    expect(treeReading({ ...t, roots: [3, 4] }).tone).toBe("warn");
    expect(treeReading({ ...t, signal: [{ var: "brain", morans_i: 0.5, p_value: 0.01 }] }).text).toMatch(/phylogenetic signal/);
  });

  it("reads directions, times of day and points", () => {
    const c = scienceExample("circular");
    expect(circularReading(c.items[0]!).text).toMatch(/^wind_from has a preferred direction: about 182°/);
    expect(circularReading(c.items[1]!).text).toMatch(/ocelot \(about 00:30\) and agouti \(about 12:00\) have a preferred time of day/);
    // the pooled rose "all" that the runner puts first is not a group
    const clock = c.items[1]!;
    const pooled = { ...clock, groups: [{ ...clock.groups[0]!, label: "all" }, ...clock.groups] };
    const read = circularReading(pooled).text;
    expect(read).toMatch(new RegExp(`^One rose for each ${clock.group_by ?? "group"} \\(${clock.groups.length}\\), and one for all the records together\\.`));
    expect(read).not.toMatch(/all \(about/);
    const pts = scienceExample("points");
    expect(pointsReading(pts).text).toMatch(/^3 points along chip, at wcs_col and wcs_row \(a grid of cells\)\./);
    expect(modeCounts(pts)).toEqual([
      { t: "B", n: 1 },
      { t: "F", n: 1 },
      { t: "W", n: 1 },
    ]);
  });
});

describe("scientific data: helpers", () => {
  it("shows the class means of an image as frames", () => {
    const img = scienceExample("images").fields[0]!;
    const g = groupField(img)!;
    expect(g.frames).toHaveLength(1);
    expect(g.frame_dim).toBe("label");
    expect(g.frames[0]!.label).toBe(0);
    expect(groupField(scienceExample("images").fields[1]!)).toBeNull();
  });

  it("shades the cells outside a mask, more for cells less often inside", () => {
    const px = paintMask([1, 0, 0.5, NaN], 2, 2);
    expect(px[3]).toBe(0);
    expect(px[7]).toBe(150);
    expect(px[11]).toBe(75);
    expect(px[15]).toBe(0);
    expect([maskWord(1), maskWord(0), maskWord(0.25), maskWord(NaN)]).toEqual(["inside", "outside", "inside in 25% of the frames", "—"]);
  });

  it("cleans the structure hints and checks the y/x pair", () => {
    expect(cleanHints({ y_dim: "", x_dim: null, angle_vars: { "": "deg" } })).toBeNull();
    expect(cleanHints({ y_dim: "row", x_dim: "col", angle_vars: { wind_from: "deg" } })).toEqual({ y_dim: "row", x_dim: "col", angle_vars: { wind_from: "deg" } });
    expect(hintProblem({ y_dim: "row" })).toMatch(/or neither/);
    expect(hintProblem({ y_dim: "row", x_dim: "row" })).toMatch(/different/);
    expect(hintProblem({ y_dim: "row", x_dim: "col" })).toBeNull();
    expect(hintProblem(null)).toBeNull();
  });
});
