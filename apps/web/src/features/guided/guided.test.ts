import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import templatesPage from "@/mocks/guided-templates.gen.json";
import { buildDraft, suggestRoles } from "@/mocks/guided-engine";
import { personaOf, useUi } from "@/stores/ui";

import { gateSentence, previewGate } from "./gate";
import { amount, band, etaText, holdoutFraction, multiplierText, phaseSlot, signedAmount, stateText } from "./plain";
import { fanPlot, distributionPlot } from "./plots";
import { beliefProblems, beliefsKey, coverageSentence, formFromCalibration, newLiftRow, toBusinessPriors, toLiftTests, withCalibration, withMode } from "./priors";
import { columnKind, detectFrequency, detectMeridian, detectMff, profileTable, profilesFromNames, seriesFor, tableFromText, tableProblems } from "./profile";
import { advancedChanges, datasetNameOf, defaultSessionName, guidedTemplateOf, slugify, stepsFor, timeDimOf, valueAt, wideVarOf } from "./session";
import type { GuidedTemplate } from "./types";
import { parseCounterfactualView, parseForecastView } from "./views";

const TEMPLATES = (templatesPage as unknown as { items: GuidedTemplate[] }).items;
const tpl = (id: string) => TEMPLATES.find((t) => t.id === id)!;
const SAMPLE = readFileSync(resolve(__dirname, "../../../e2e/fixtures/guided-geo-mmm.csv"), "utf-8");

describe("local profiling", () => {
  it("types columns and finds the date frequency", () => {
    expect(columnKind(["2025-01-05", "2025-01-12"])).toBe("date");
    expect(columnKind(["1", "-2", ""])).toBe("integer");
    expect(columnKind(["1.5", "2e3"])).toBe("number");
    expect(columnKind(["yes", "No"])).toBe("boolean");
    expect(columnKind(["de", "fr"])).toBe("string");
    expect(detectFrequency(["2025-01-05", "2025-01-12", "2025-01-19"])).toBe("W-SUN");
    expect(detectFrequency(["2025-01-01", "2025-01-02", "2025-01-03"])).toBe("D");
    expect(detectFrequency(["2025-01-01", "2025-02-01", "2025-03-01"])).toBe("MS");
    expect(detectFrequency(["2025-01-01", "2025-01-05", "2025-02-01"])).toBeNull();
  });

  it("profiles the sample file like the server expects", () => {
    const table = tableFromText(SAMPLE);
    expect(table.rows).toHaveLength(80);
    const profiles = profileTable(table);
    const by = Object.fromEntries(profiles.map((p) => [p.name, p]));
    expect(by.week).toMatchObject({ dtype: "date", detected_freq: "W-SUN", n_distinct: 40, monotonic: false, origin: "column" });
    expect(by.market).toMatchObject({ dtype: "string", n_distinct: 2 });
    expect(by["Revenue (EUR)"]?.dtype).toBe("number");
    expect(by.tv_spend?.min).toBeGreaterThan(0);
    expect(tableProblems(table)).toEqual([]);
  });

  it("sums a series per date for the sparklines", () => {
    const table = tableFromText("week,market,y\n2025-01-12,de,1\n2025-01-05,de,2\n2025-01-05,fr,3\n");
    expect(seriesFor(table, "y", "week")).toEqual([5, 1]);
    expect(seriesFor(table, "y", null)).toEqual([1, 2, 3]);
    expect(seriesFor(table, "nope", null)).toEqual([]);
  });

  it("pivots MFF long tables and recognises the preset", () => {
    const mff = "Period,Geography,Product,VariableName,VariableValue\n2025-01-05,de,all,Sales,10\n2025-01-05,de,all,TV_Spend,3\n2025-01-12,de,all,Sales,11\n2025-01-12,de,all,TV_Spend,4\n";
    const table = tableFromText(mff);
    expect(table.mff?.variables).toEqual(["Sales", "TV_Spend"]);
    expect(table.header).toEqual(["Period", "Geography", "Product", "Sales", "TV_Spend"]);
    expect(table.rows).toEqual([
      ["2025-01-05", "de", "all", "10", "3"],
      ["2025-01-12", "de", "all", "11", "4"],
    ]);
    expect(profileTable(table).find((p) => p.name === "Sales")?.origin).toBe("mff_variable");
    expect(detectMff(table)?.answers).toEqual({ date: "Period", geo: "Geography" });
  });

  it("recognises a Google Meridian table by its column names", () => {
    const table = tableFromText("time,geo,conversions,tv_impressions,tv_spend,search_spend,competitor_sales,population\n2025-01-05,a,1,2,3,4,5,6\n2025-01-12,a,1,2,3,4,5,6\n2025-01-19,a,1,2,3,4,5,6\n");
    const m = detectMeridian(profileTable(table));
    expect(m?.answers).toEqual({ date: "time", geo: "geo", kpi: "conversions", spend: ["tv_spend", "search_spend"], controls: ["competitor_sales"] });
    expect(detectMeridian(profileTable(tableFromText(SAMPLE)))).toBeNull();
  });

  it("explains broken headers in plain words", () => {
    expect(tableProblems(tableFromText("a,a\n1,2\n"))[0]).toContain('Two columns are both called "a"');
    expect(tableProblems(tableFromText("a,b\n"))).toContain("The file has a header but no rows of data.");
    expect(profilesFromNames(["week", "revenue", "market"]).map((p) => p.dtype)).toEqual(["date", "number", "string"]);
  });
});

describe("role detection and drafts (mock stand-ins of mapi_spec.guided)", () => {
  const profiles = profileTable(tableFromText(SAMPLE));

  it("pre-fills every question of the geo MMM from the sample", () => {
    const s = suggestRoles(tpl("geo_mmm"), profiles);
    const answer = Object.fromEntries(s.questions.map((q) => [q.question_id, q.answer]));
    expect(answer).toMatchObject({ kpi: "Revenue (EUR)", date: "week", geo: "market", spend: ["search_spend", "social_spend", "tv_spend"], controls: ["avg_price"], frequency: "weekly" });
    expect(s.issues).toEqual([]);
  });

  it("drafts a spec whose labels name the template", () => {
    const d = buildDraft(tpl("geo_mmm"), { template_id: "geo_mmm", name: "eu", dataset: "eu-data", format: "csv", answers: { kpi: "Revenue (EUR)", date: "week", geo: "market", spend: ["tv_spend", "search_spend"] }, columns: profiles }, "growth");
    expect(guidedTemplateOf(d.spec as Record<string, unknown>)).toBe("geo_mmm");
    expect(datasetNameOf(d.spec as Record<string, unknown>)).toBe("eu-data");
    expect(d.mapping.wide?.spend?.columns).toEqual({ tv: "tv_spend", search: "search_spend" });
    expect(d.mapping.vars?.revenue_eur?.column).toBe("Revenue (EUR)");
  });
});

describe("business priors", () => {
  it("round-trips a calibration block through the form", () => {
    const cal = {
      business_priors: { channels: { tv: { roi: [1.5, 4], credibility: 0.9 } }, baseline_share: { share: [0.5, 0.8], credibility: 0.8 } },
      lift_tests: [{ channel: "tv", geo: "de", start: "2025-06-01", end: "2025-06-28", spend_delta: -40000, kpi_delta: -90000, se: 25000 }],
    };
    const form = formFromCalibration(cal, ["tv", "search"]);
    expect(form.mode).toBe("roi");
    expect(form.channels).toEqual([
      { channel: "tv", enabled: true, low: 1.5, high: 4, credibility: 0.9 },
      expect.objectContaining({ channel: "search", enabled: false }),
    ]);
    const bp = toBusinessPriors(form);
    expect(bp).toEqual({ channels: { tv: { roi: [1.5, 4], credibility: 0.9 } }, baseline_share: { share: [0.5, 0.8], credibility: 0.8 } });
    const lt = toLiftTests(form.liftTests, ["tv", "search"]);
    expect(lt.problems).toEqual([]);
    expect(lt.tests).toEqual(cal.lift_tests);
  });

  it("keeps a baseline belief stated without any channel belief (M3 review)", () => {
    const form = formFromCalibration(undefined, ["tv", "search"]);
    const baselineOnly = { ...form, baseline: { enabled: true, low: 0.4, high: 0.8, credibility: 0.8 } };
    const bp = toBusinessPriors(baselineOnly);
    expect(bp).toEqual({ channels: {}, baseline_share: { share: [0.4, 0.8], credibility: 0.8 } });
    // and it survives a save and reload
    expect(formFromCalibration({ business_priors: bp }, ["tv", "search"]).baseline).toEqual(baselineOnly.baseline);
  });

  it("without a calibration block no channel carries a belief until it is ticked", () => {
    // a channel the analyst never touched must not become an ROI belief (M3 usability run)
    const form = formFromCalibration(undefined, ["tv"]);
    expect(form.channels[0]).toMatchObject({ enabled: false, low: 0.5, high: 4 });
    expect(toBusinessPriors(form)).toBeNull();
    expect(toBusinessPriors({ ...form, channels: [{ ...form.channels[0]!, enabled: true }] })).toEqual({ channels: { tv: { roi: [0.5, 4], credibility: 0.8 } } });
    expect(withMode(form, "share").channels[0]).toMatchObject({ low: 0.02, high: 0.15 });
    expect(toBusinessPriors({ ...form, channels: [{ ...form.channels[0]!, enabled: false }] })).toBeNull();
  });

  it("explains impossible beliefs in plain words (check_business_priors)", () => {
    const form = withMode(formFromCalibration(undefined, ["tv", "search"]), "share");
    const tooMuch = { ...form, channels: form.channels.map((c) => ({ ...c, enabled: true, low: 0.4, high: 0.6 })), baseline: { enabled: true, low: 0.3, high: 0.5, credibility: 0.8 } };
    expect(beliefProblems(tooMuch)[0]?.message).toContain("already add up to 110%");
    const reversed = { ...form, channels: [{ ...form.channels[0]!, enabled: true, low: 0.3, high: 0.2 }] };
    expect(beliefProblems(reversed)[0]?.message).toContain("low end must be below the high end");
    const zeroRoi = formFromCalibration(undefined, ["tv"]);
    expect(beliefProblems({ ...zeroRoi, channels: [{ ...zeroRoi.channels[0]!, enabled: true, low: 0 }] })[0]?.message).toContain("above zero");
  });

  it("checks experiment rows and ignores empty ones", () => {
    const blank = newLiftRow();
    const bad = { ...newLiftRow("tv"), start: "2025-06-10", end: "2025-06-01", spendDelta: "0", kpiDelta: "x", se: "-1" };
    const { tests, problems } = toLiftTests([blank, bad], ["tv"]);
    expect(tests).toEqual([]);
    expect(problems.map((p) => p.where.split("/")[2])).toEqual(["end", "spendDelta", "kpiDelta", "se"]);
    expect(problems[0]?.message).toBe("Experiment 2: the test cannot end before it starts.");
  });

  it("writes beliefs and a matching translation into the spec (apply_prior_translation)", () => {
    const doc = { model: { settings: { priors: { intercept: { distribution: "Normal" } }, pooling: "partial" } }, calibration: { prior_translation_run: "run_old" } };
    const bp = { channels: { tv: { roi: [1, 3] as [number, number], credibility: 0.8 } } };
    const result = { format: "mapi.prior-translation/v1" as const, channels: ["tv"], priors: { beta_channel: { distribution: "LogNormal" } }, draws: 500 };
    const applied = withCalibration(doc, bp, [], { result, runId: "run_01" });
    expect(valueAt(applied, "/model/settings/priors")).toEqual({ intercept: { distribution: "Normal" }, beta_channel: { distribution: "LogNormal" } });
    expect(valueAt(applied, "/calibration")).toEqual({ business_priors: bp, prior_translation_run: "run_01" });
    // beliefs changed since the preview: the fit translates them (no run recorded)
    expect(valueAt(withCalibration(doc, bp, []), "/calibration")).toEqual({ business_priors: bp });
    expect(valueAt(withCalibration(doc, null, []), "/calibration")).toBeUndefined();
    expect(doc.calibration.prior_translation_run).toBe("run_old"); // the input is not mutated
    expect(beliefsKey(bp, [])).not.toBe(beliefsKey(null, []));
    expect(coverageSentence(0.79, 0.8)).toBe("79% of what the model believes falls inside your range, close to the 80% you stated.");
    expect(coverageSentence(0.6, 0.8)).toContain("below");
  });
});

describe("promotion gate preview (mapi_spec.scorecard.promotion_gate)", () => {
  const item = (status: "pass" | "warn" | "fail", check: string) => ({ code: "X", status, title: check, message_plain: "m", check });
  it("passes, needs an admin or blocks", () => {
    expect(previewGate({ overall: "pass", items: [item("pass", "a.b")] }).outcome).toBe("allowed");
    const warn = previewGate({ overall: "warn", items: [item("pass", "a.b"), item("warn", "holdout.mape")] });
    expect(warn).toMatchObject({ outcome: "needs_override", failing_checks: ["holdout.mape"] });
    expect(previewGate({ overall: "fail", items: [item("warn", "a.b"), item("fail", "c.d")] }).outcome).toBe("blocked");
    expect(previewGate(null).reasons?.[0]?.code).toBe("NO_SCORECARD");
    expect(previewGate({ overall: "pass", items: [] }, { partial: true }).outcome).toBe("blocked");
    expect(previewGate({ overall: "pass", items: [] }, { fitMode: "train_only" }).reasons?.[0]?.code).toBe("TRAIN_ONLY");
    expect(gateSentence("needs_override", false)).toContain("a project admin has to approve");
    expect(gateSentence("needs_override", true)).toContain("As a project admin you can approve it anyway");
  });
});

describe("sessions", () => {
  it("lays out each template's steps", () => {
    expect(stepsFor(tpl("geo_mmm")).map((s) => s.id)).toEqual(["data", "priors", "run", "report", "explore", "share"]);
    expect(stepsFor(tpl("weekly_forecast")).map((s) => s.title)).toEqual(["Your data", "Fit the model", "Results", "Forecast", "Share"]);
    expect(stepsFor(tpl("customer_choice")).find((s) => s.id === "explore")?.title).toBe("Scenarios");
  });

  it("finds the bound time dim and the wide variable", () => {
    expect(timeDimOf(tpl("geo_mmm"))).toBe("time");
    expect(timeDimOf(tpl("weekly_forecast"))).toBe("t");
    expect(wideVarOf(tpl("geo_mmm"))).toEqual({ variable: "spend", dim: "channel", question: "spend" });
    expect(wideVarOf(tpl("customer_choice"))).toEqual({ variable: "price", dim: "product", question: "prices" });
  });

  it("marks advanced settings changed in Expert mode", () => {
    const base = { model: { fit: { draws: 500 }, settings: { pooling: "partial", adstock: { kind: "geometric" } } }, pipeline: [{ id: "a" }] };
    expect(advancedChanges(tpl("geo_mmm"), base, base)).toEqual([]);
    const edited = { ...base, model: { ...base.model, fit: { draws: 2000 }, settings: { ...base.model.settings, pooling: "none" } } };
    // pooling is a basic setting: only the fit counts
    expect(advancedChanges(tpl("geo_mmm"), edited, base)).toEqual(["/model/fit"]);
    expect(advancedChanges(tpl("geo_mmm"), edited, null)).toEqual([]);
  });

  it("makes names the API accepts", () => {
    expect(slugify("EU media mix (Q3)")).toBe("eu-media-mix-q3");
    expect(slugify("Ünïcode café")).toBe("unicode-cafe");
    expect(slugify("!!!")).toBe("analysis");
    expect(defaultSessionName("geo_mmm", new Date("2026-10-15T09:00:00Z"))).toBe("geo-mmm-20261015");
  });
});

describe("plain words", () => {
  it("formats amounts, ranges, phases and times", () => {
    expect(amount(1_234_567, "EUR")).toBe("EUR 1.2M");
    expect(amount(12.345)).toBe("12.35");
    expect(signedAmount(-2500, "EUR")).toBe("−EUR 2,500");
    expect(signedAmount(0)).toBe("0");
    expect(multiplierText(1)).toBe("as spent");
    expect(multiplierText(1.2)).toBe("+20%");
    expect(multiplierText(0)).toBe("switched off");
    expect(band({ "0.95": 3, "0.05": 1, "0.5": 2 })).toEqual([1, 3]);
    expect(etaText(30)).toBe("less than a minute left");
    expect(etaText(600)).toBe("about 10 minutes left");
    expect(phaseSlot("fit")).toBe(phaseSlot("sample"));
    expect(stateText("queued")).toBe("Waiting to start");
    expect(stateText("queued", true)).toBe("Running");
  });
});

describe("views and plots", () => {
  const fan = { dim: "time", coords: ["2025-01-05", "2025-01-12", "2025-01-19"], quantiles: [0.05, 0.5, 0.95], values: [[null, 1, 2], [null, 2, 3], [null, 3, 4]], observed: [5, null, null] };

  it("parses the forecast and counterfactual views", () => {
    const f = parseForecastView({ format: "mapi.forecast/v1", kpi: "y", dim: "time", history: 1, split_at: "2025-01-12", fan, total: { mean: 5, sd: 1, quantiles: { "0.05": 3, "0.95": 7 } }, extra: true });
    expect(f.fan.values[0]).toEqual([null, 1, 2]);
    expect(f.total?.quantiles).toEqual({ "0.05": 3, "0.95": 7 });
    expect(() => parseForecastView({ format: "mapi.forecast/v2" })).toThrow("unknown forecast format");
    expect(() => parseForecastView({ format: "mapi.forecast/v1", fan: { ...fan, values: [[1]] } })).toThrow();
    const d = { mean: 1, sd: 0.5, quantiles: { "0.05": 0.2 } };
    expect(parseCounterfactualView({ format: "mapi.counterfactual/v1", kpi: "y", scenario_total: d, baseline_total: d, delta_total: d }).delta_share).toBeNull();
  });

  it("builds a fan chart with the split line and a beliefs chart", () => {
    const plot = fanPlot(fan, { title: "Results", splitAt: "2025-01-12" });
    expect(plot.datasets.fan?.[0]).toMatchObject({ x: "2025-01-05", lo: null, observed: 5 });
    expect(plot.datasets.split).toEqual([{ x: "2025-01-12", label: "Forecast starts" }]);
    const beliefs = distributionPlot([{ label: "tv", summary: { mean: 2, sd: 1, quantiles: { "0.05": 1, "0.25": 1.5, "0.5": 2, "0.75": 2.5, "0.95": 3 } }, stated: [1.5, 4] }], { axisTitle: "ROI", format: ".2f" });
    expect(beliefs.datasets.beliefs?.[0]).toMatchObject({ lo: 1, q25: 1.5, mid: 2, q75: 2.5, hi: 3, stated_lo: 1.5, stated_hi: 4 });
  });
});

describe("persona", () => {
  it("is remembered per user, with the browser's last choice as the default", () => {
    useUi.getState().setPersona("guided", "usr_a");
    useUi.getState().setPersona("expert", "usr_b");
    const s = useUi.getState();
    expect(personaOf(s, "usr_a")).toBe("guided");
    expect(personaOf(s, "usr_b")).toBe("expert");
    expect(personaOf(s, "usr_new")).toBe("expert");
  });
});

describe("holdoutFraction", () => {
  const item = { code: "HOLDOUT_MAPE", status: "pass", title: "Holdout error", message_plain: "", metric: "mape", value: 0.0082, threshold: 0.1, check: "holdout.mape" } as const;
  it("reads MAPE as the scorecard measured it (a fraction), not the SDK's percent", () => {
    // a real fit: metrics.mape 0.82 is 0.82%, which the report once showed as "82%"
    expect(holdoutFraction({ metrics: { mape: 0.82, coverage_90: 0.85 }, scorecard: { overall: "pass", items: [item] } }, "mape")).toBe(0.0082);
  });
  it("converts percent metrics without a scorecard and keeps fractions as they are", () => {
    expect(holdoutFraction({ metrics: { mape: 12, coverage_90: 0.86 }, scorecard: null }, "mape")).toBeCloseTo(0.12);
    expect(holdoutFraction({ metrics: { mape: 12, coverage_90: 0.86 }, scorecard: null }, "coverage_90")).toBe(0.86);
    expect(holdoutFraction({ metrics: {}, scorecard: null }, "mape")).toBeNull();
  });
});

describe("baselineSentence", () => {
  it("does not print impossible shares when the prior has no view on the baseline", async () => {
    const { baselineSentence } = await import("./steps/priors-step");
    expect(baselineSentence([-1.64, 2.71], 0.5)).toContain("the model has no view yet");
    expect(baselineSentence([-1.64, 2.71], 0.5)).not.toContain("-164%");
    expect(baselineSentence([0.55, 0.8], 0.7)).toBe("between 55% and 80%.");
    expect(baselineSentence(null, 0.7)).toBe("about 70%.");
  });
});

describe("Guided usability fixes (M3 tester run)", () => {
  it("fits again more carefully, up to a ceiling", async () => {
    const { canFitMoreCarefully, carefulFit, withCarefulFit } = await import("./refit");
    const fit = { mode: "evaluate_then_refit", tune: 500, draws: 500, chains: 4, seed: 7 };
    expect(carefulFit(fit)).toEqual({ ...fit, tune: 1000, draws: 1000, target_accept: 0.95 });
    expect(carefulFit({ tune: 1000, draws: 1000, target_accept: 0.95 })).toMatchObject({ tune: 2000, draws: 2000, target_accept: 0.98 });
    const max = { tune: 4000, draws: 4000, target_accept: 0.99 };
    expect(canFitMoreCarefully(max)).toBe(false);
    expect(canFitMoreCarefully(fit)).toBe(true);
    const doc = { model: { use: "mmm.hierarchical_geo@^1", fit }, data: { dataset: "x" } };
    expect(withCarefulFit(doc).model.fit).toMatchObject({ draws: 1000 });
    expect(doc.model.fit.draws).toBe(500); // the original is untouched
  });

  it("offers result charts for sharing before the sampler's diagnostics", async () => {
    const { shareOrder } = await import("./steps/share-step");
    const ids = shareOrder([{ id: "core.convergence" }, { id: "core.ppc_fan" }, { id: "mmm.decomposition" }, { id: "mmm.response_curves" }]).map((p) => p.id);
    expect(ids).toEqual(["mmm.response_curves", "mmm.decomposition", "core.ppc_fan", "core.convergence"]);
  });
});

describe("the upload hint (M3 review)", () => {
  it("describes the data each template needs, and the MMM presets only for the marketing mix", async () => {
    const { dataShapePlain } = await import("./session");
    const mmm = dataShapePlain(tpl("geo_mmm"));
    expect(mmm).toMatch(/one row per week \(or day\) and market/);
    expect(mmm).toMatch(/MFF/);
    expect(mmm).toMatch(/Meridian/);
    const choice = dataShapePlain(tpl("customer_choice"));
    expect(choice).toMatch(/one row per shopping trip/);
    expect(choice).not.toMatch(/MFF|Meridian|market/);
    expect(dataShapePlain(tpl("store_demand"))).toMatch(/one row per store and week/);
    expect(dataShapePlain(tpl("weekly_forecast"))).not.toMatch(/MFF|market/);
    expect(dataShapePlain({ family: "other" as GuidedTemplate["family"], description_plain: "Anything." })).toMatch(/^A CSV or Parquet file/);
  });
});

describe("the fan chart's y axis (M3 review)", () => {
  it("fits the band instead of starting at zero", () => {
    const fan = { dim: "time", coords: ["2025-01-05", "2025-01-12"], quantiles: [0.05, 0.25, 0.5, 0.75, 0.95], values: [[350, 360], [370, 375], [380, 385], [390, 395], [400, 410]], observed: [382, null] };
    const layers = (fanPlot(fan, { title: "Results", splitAt: "2025-01-12" }).spec.layer ?? []) as { encoding?: { y?: { field?: string; scale?: unknown } } }[];
    const ys = layers.map((l) => l.encoding?.y).filter((y) => y?.field);
    expect(ys.length).toBeGreaterThanOrEqual(4);
    for (const y of ys) expect(y?.scale).toEqual({ zero: false, nice: true });
  });
});

describe("the forecast panel's horizon (M3 review)", () => {
  it("names the data's period", async () => {
    const { periodUnit, periodWords } = await import("./session");
    const dims = (freq: string | null) => [{ kind: "categorical" }, { kind: "time", freq }];
    expect(periodUnit({}, dims("W-SUN"))).toBe("week");
    expect(periodUnit({}, dims("D"))).toBe("day");
    expect(periodUnit({}, dims("MS"))).toBe("month");
    expect(periodUnit({}, dims(null))).toBe("period");
    expect(periodUnit({}, [])).toBe("period");
    // a schema without the frequency, then the summary that measured it
    expect(periodUnit({}, [...dims(null), ...dims("W-SUN")])).toBe("week");
    // weekly_forecast records the answered frequency as the season length
    expect(periodUnit({ model: { settings: { season_period: 365.25 } } }, [])).toBe("day");
    expect(periodUnit({ model: { settings: { season_period: 52.18 } } }, dims("D"))).toBe("week");
    expect(periodWords("day", 1)).toBe("1 day");
    expect(periodWords("week", 13)).toBe("13 weeks");
  });

  it("stops at the horizon the model was fitted for", async () => {
    const { forecastLimit } = await import("./session");
    const forecastTpl = tpl("weekly_forecast");
    expect(forecastLimit({ model: { settings: { max_extension: 26 } } }, forecastTpl)).toEqual({ max: 26, fitted: true });
    expect(forecastLimit({ model: { settings: {} } }, forecastTpl)).toEqual({ max: 520, fitted: false });
    expect(forecastLimit({}, tpl("geo_mmm"))).toEqual({ max: 104, fitted: false });
  });
});

describe("the beliefs preview (M3 review)", () => {
  const d = (mid: number) => ({ mean: mid, sd: 1, quantiles: { "0.05": mid / 2, "0.25": mid * 0.8, "0.5": mid, "0.75": mid * 1.2, "0.95": mid * 2 } });

  it("charts the channels with a stated belief and names the others apart", async () => {
    const { previewGroups } = await import("./priors");
    const form = formFromCalibration(undefined, ["tv", "search", "social"]);
    form.channels[0] = { ...form.channels[0]!, enabled: true, low: 1.5, high: 4 };
    // the server previews every channel: the unticked ones at the model's default prior
    const preview = { roi: { tv: d(2.4), search: d(6.5), social: d(17.2) }, contribution_share: { tv: d(0.15), search: d(0.49), social: d(0.49) } };
    const g = previewGroups(preview, form);
    expect(g.roi.map((i) => i.label)).toEqual(["tv"]);
    expect(g.roi[0]?.stated).toEqual([1.5, 4]);
    expect(g.share.map((i) => i.label)).toEqual(["tv"]);
    expect(g.share[0]?.stated).toBeNull();
    expect(g.unstated).toEqual(["search", "social"]);
  });

  it("shows the stated range on the share chart in share mode", async () => {
    const { previewGroups } = await import("./priors");
    const form = withMode(formFromCalibration(undefined, ["tv", "search"]), "share");
    form.channels[1] = { ...form.channels[1]!, enabled: true, low: 0.05, high: 0.2 };
    const g = previewGroups({ roi: { tv: d(3), search: d(2) }, contribution_share: { tv: d(0.3), search: d(0.1) } }, form);
    expect(g.share).toEqual([{ label: "search", summary: d(0.1), stated: [0.05, 0.2] }]);
    expect(g.roi[0]?.stated).toBeNull();
    expect(g.unstated).toEqual(["tv"]);
  });
});
