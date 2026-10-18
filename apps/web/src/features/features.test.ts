import { describe, expect, it } from "vitest";

import { progressFromEvent, runStreamReducer, type RunStreamState } from "@/hooks/use-run-events";
import { validateResponseFor } from "@/mocks/fixtures";
import { STARTER_SPEC } from "@/features/editor/starter";

import { initialMappings, toIngestMapping, validateMappings } from "./datasets/mapping";
import { buildPipeline, schemaDelta } from "./editor/pipeline";
import { missingnessHeatmap, quickChart, stateArrayPlot } from "./plots/builtin-specs";
import { REPRESENTATIONS, buildScoreRequest, defaultScoreForm, parseSel, sideTableOptions } from "./score/request";

describe("buildPipeline", () => {
  it("joins the document with stages, resolved steps, split and issues", () => {
    const doc = { pipeline: [{ id: "weekly", use: "core.resample@^1" }, { id: "fill" }, { id: "cal" }], model: { use: "m@1" } };
    const res = validateResponseFor(STARTER_SPEC.replace("after_step: weekly", "after_step: fill"));
    const model = buildPipeline(doc, res);
    expect(model.steps.map((s) => s.id)).toEqual(["weekly", "fill", "cal"]);
    expect(model.steps[0]?.splitAfter).toBe(true);
    expect(model.steps[1]?.issues.map((i) => i.code)).toEqual(["LEAKAGE_RISK"]);
    expect(model.steps[2]?.resolved?.context).toBeNull();
    expect(model.steps[2]?.delta?.addedVars).toEqual(["calendar"]);
    expect(model.steps[2]?.delta?.addedDims).toEqual(["calendar_feature"]);
    expect(model.steps[0]?.delta?.changedDims).toEqual(["date"]);
    expect(model.modelUse).toBe("m@1");
  });

  it("tolerates documents without a pipeline and missing validation", () => {
    expect(buildPipeline(null, null).steps).toEqual([]);
    expect(buildPipeline({ pipeline: ["junk"] }, null).steps[0]?.id).toBe("step 1");
    expect(schemaDelta(null, { dims: {}, vars: {} })).toBeNull();
  });
});

describe("score request", () => {
  it("builds a forecast request with interventions and output selection", () => {
    const form = defaultScoreForm();
    form.inputsStage = "scale_spend";
    form.scenario = "dataset://default/plan_2027h1@v1";
    form.interventions = [{ var: "spend", sel: "channel=meta; geo=de,fr", op: "multiply", value: "1.2" }];
    form.output.vars = "y_hat";
    form.seed = "7";
    const out = buildScoreRequest(form, "model://default/eu-geo-mmm@v1");
    expect("request" in out && out.request).toEqual({
      apiVersion: "mapi/v1",
      kind: "ScoreRequest",
      model: "model://default/eu-geo-mmm@v1",
      mode: "forecast",
      inputs_stage: "scale_spend",
      horizon: { dim: "time", periods: 8 },
      future_covariates: "carry_last",
      scenario: "dataset://default/plan_2027h1@v1",
      interventions: [{ var: "spend", sel: { channel: "meta", geo: ["de", "fr"] }, multiply: 1.2 }],
      output: { kind: "expected", summary: ["mean", "hdi_90"], draws: 0, units: "original", names: "dataset", vars: ["y_hat"] },
      seed: 7,
    });
  });

  it("refuses summaries and draws the model's representation cannot give", () => {
    const form = { ...defaultScoreForm(), output: { ...defaultScoreForm().output, draws: "10" } };
    const out = buildScoreRequest(form, "model://default/m@v1", "quantiles");
    expect("problems" in out && out.problems.map((p) => p.field).sort()).toEqual(["draws", "summary"]);
    const ok = buildScoreRequest({ ...form, output: { ...form.output, draws: "0", summary: REPRESENTATIONS.quantiles.defaults } }, "model://default/m@v1", "quantiles");
    expect("request" in ok && ok.request.output?.summary).toEqual(["median", "q5", "q95"]);
    // Unknown representation (an older version): the server decides.
    expect("request" in buildScoreRequest(form, "model://default/m@v1")).toBe(true);
  });

  it("sends the payload dataset (only when it is the source) and the non-empty side tables", () => {
    const typed = buildScoreRequest({ ...defaultScoreForm(), data: "dataset://default/e33_future@v1" }, "model://default/m@v1");
    expect("request" in typed && typed.request.data).toBeUndefined();
    const form = { ...defaultScoreForm(), source: "dataset" as const, data: " dataset://default/e33_future@v1 ", sideTables: { fx: "dataset://default/fx_daily@v1", holidays: " " } };
    const out = buildScoreRequest(form, "model://default/eu-geo-mmm@v1");
    expect("request" in out && out.request.data).toBe("dataset://default/e33_future@v1");
    expect("request" in out && out.request.side_tables).toEqual({ fx: "dataset://default/fx_daily@v1" });
    const bad = buildScoreRequest({ ...form, data: "file:///etc/passwd", sideTables: { fx: "nope" } }, "model://default/m@v1");
    expect("problems" in bad && bad.problems.map((p) => p.field).sort()).toEqual(["data", "sideTables.fx"]);
  });

  it("lists the spec's side tables with their score policy", () => {
    const doc = { data: { side_tables: { holidays: { ref: "dataset://shared/h@v3", score_policy: "pinned" }, fx: { ref: "dataset://default/fx@v1", score_policy: "supplied" }, junk: 3 } } };
    expect(sideTableOptions(doc)).toEqual([
      { name: "fx", policy: "supplied", ref: "dataset://default/fx@v1" },
      { name: "holidays", policy: "pinned", ref: "dataset://shared/h@v3" },
    ]);
    expect(sideTableOptions(null)).toEqual([]);
    expect(sideTableOptions({ data: { side_tables: [] } })).toEqual([]);
  });

  it("reports field problems instead of building a bad request", () => {
    const form = { ...defaultScoreForm(), horizonPeriods: "0", scenario: "s3://bucket/x", seed: "-1" };
    form.interventions = [{ var: "spend", sel: "channel", op: "add", value: "x" }];
    const out = buildScoreRequest(form, "model://default/m@v1");
    expect("problems" in out && out.problems.map((p) => p.field).sort()).toEqual(["horizonPeriods", "interventions.0", "scenario", "seed"]);
  });

  it("parses selections with typed values", () => {
    expect(parseSel("a=1, 2; b=true; c=x")).toEqual({ a: [1, 2], b: [true], c: ["x"] });
    expect(parseSel("bad dim=1")).toMatch(/not a correct dim name/);
  });
});

describe("ingest mapping", () => {
  it("derives mappings from a CSV header and converts them to IngestMapping", () => {
    const m = initialMappings(["date", "market", "revenue", "spend"], [["2026-01-01", "de", "1.5", "2"]]);
    expect(m.map((x) => x.as)).toEqual(["dim", "dim", "var", "var"]);
    expect(m[2]?.role).toBe("target");
    expect(m[3]?.role).toBe("spend");
    expect(validateMappings(m)).toEqual([]);
    const out = toIngestMapping("csv", m);
    expect(out.dims?.date).toEqual({ column: "date", spec: { kind: "time", policy: "extend", freq: "D" } });
    expect(out.vars?.revenue).toEqual({ column: "revenue", role: "target", missing: "forbid" });
  });

  it("flags duplicate and invalid names and a missing dim", () => {
    const m = initialMappings(["a", "b"], [["1", "2"]]).map((x) => ({ ...x, as: "var" as const, name: "dup" }));
    const problems = validateMappings(m).map((p) => p.message);
    expect(problems.some((p) => p.includes("one or more columns as a dim"))).toBe(true);
    expect(problems.some((p) => p.includes("Two columns use the name"))).toBe(true);
  });
});

describe("builtin plot specs", () => {
  it("only reference named datasets", () => {
    const heat = missingnessHeatmap({ along: "w", labels: ["y"], coords: ["a", "b"], fraction: [[0, 0.5]] });
    expect(heat.datasets.missing).toHaveLength(2);
    expect(JSON.stringify(heat.spec)).not.toContain('"url"');
    const arr = stateArrayPlot({ name: "scale", dims: ["g", "c"], coords: { g: ["de"], c: ["tv", "search"] }, values: [[1, 2]] });
    expect(arr.datasets.state).toEqual([
      { g: "de", c: "tv", value: 1 },
      { g: "de", c: "search", value: 2 },
    ]);
    const q = quickChart([{ t: "2026-01-01", y: 1, lo: 0, hi: 2 }], { x: "t", y: "y", lower: "lo", upper: "hi" }, true);
    expect((q.spec.layer as unknown[]).length).toBe(2);
  });
});

describe("run stream reducer", () => {
  const initial: RunStreamState = { status: "connecting", detail: null, events: [], progress: null };
  const ev = (id: number) => ({ id, run_id: "r", attempt: 1, seq: id, ts: "t", type: "state" as const, payload: {} });

  it("dedupes replayed events and keeps them ordered", () => {
    let s = runStreamReducer(initial, { type: "event", event: ev(2) });
    s = runStreamReducer(s, { type: "event", event: ev(1) });
    s = runStreamReducer(s, { type: "event", event: ev(2) });
    expect(s.events.map((e) => e.id)).toEqual([1, 2]);
  });

  it("turns progress events into snapshots and ignores malformed ones", () => {
    const p = progressFromEvent({ ...ev(0), type: "progress", payload: { phase: "sample", fraction: 0.5, chains: [] } });
    expect(p).toMatchObject({ phase: "sample", fraction: 0.5, seq: 0 });
    expect(progressFromEvent({ ...ev(0), type: "progress", payload: { fraction: 1 } })).toBeNull();
    expect(progressFromEvent(ev(1))).toBeNull();
  });
});
