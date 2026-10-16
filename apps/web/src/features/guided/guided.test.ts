import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import templatesPage from "@/mocks/guided-templates.gen.json";
import { buildDraft, suggestRoles } from "@/mocks/guided-engine";
import { personaOf, useUi } from "@/stores/ui";

import { gateSentence, previewGate } from "./gate";
import { amount, band, etaText, multiplierText, phaseSlot, signedAmount, stateText } from "./plain";
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

  it("without a calibration block every channel starts with a sensible range", () => {
    const form = formFromCalibration(undefined, ["tv"]);
    expect(form.channels[0]).toMatchObject({ enabled: true, low: 0.5, high: 4 });
    expect(withMode(form, "share").channels[0]).toMatchObject({ low: 0.02, high: 0.15 });
    expect(toBusinessPriors({ ...form, channels: [{ ...form.channels[0]!, enabled: false }] })).toBeNull();
  });

  it("explains impossible beliefs in plain words (check_business_priors)", () => {
    const form = withMode(formFromCalibration(undefined, ["tv", "search"]), "share");
    const tooMuch = { ...form, channels: form.channels.map((c) => ({ ...c, low: 0.4, high: 0.6 })), baseline: { enabled: true, low: 0.3, high: 0.5, credibility: 0.8 } };
    expect(beliefProblems(tooMuch)[0]?.message).toContain("already add up to 110%");
    const reversed = { ...form, channels: [{ ...form.channels[0]!, low: 0.3, high: 0.2 }] };
    expect(beliefProblems(reversed)[0]?.message).toContain("low end must be below the high end");
    const zeroRoi = formFromCalibration(undefined, ["tv"]);
    expect(beliefProblems({ ...zeroRoi, channels: [{ ...zeroRoi.channels[0]!, low: 0 }] })[0]?.message).toContain("above zero");
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
