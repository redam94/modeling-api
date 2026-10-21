import { describe, expect, it } from "vitest";

import { CAUSAL_V2, CLASSIFIER_V2, FIELD_V2, FORECAST_V2, IMAGE_V2, MMM_V2, REGRESSION_V2 } from "@/mocks/dashboards";

import type { CatalogInput, DashboardSpecV2, SourceCatalog } from "../types";
import { cleanSpec, sameSpec, shareInputsOf, specIssues, tabOfIssue } from "./spec-form";
import {
  addPanelReason,
  catalogIssuesV2,
  defaultPanel,
  limitWarnings,
  movePanel,
  newPanelId,
  placeOfIssue,
  presetSwitchReason,
  PRESET_RULES,
  seedInput,
  shareInputsOfV2,
  specIssuesV2,
  switchPreset,
} from "./spec-form-v2";

const FIXTURES = { FORECAST_V2, REGRESSION_V2, CLASSIFIER_V2, CAUSAL_V2, FIELD_V2, IMAGE_V2, MMM_V2 } as const;
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
const pointers = (s: DashboardSpecV2) => specIssuesV2(s).map((i) => i.pointer);

describe("specIssuesV2", () => {
  it.each(Object.entries(FIXTURES))("accepts the server-made %s spec as it is", (_, spec) => {
    expect(specIssuesV2(spec)).toEqual([]);
    expect(specIssues(spec)).toEqual([]);
    expect(sameSpec(spec, clone(spec))).toBe(true);
  });

  it("refuses what DashboardSpecV2 refuses, with pointers", () => {
    const s = clone(CLASSIFIER_V2);
    s.title = " ";
    s.subject = { ...s.subject, unit: "currency", currency: null, decimals: 7 };
    s.panels = [...s.panels, { ...s.panels[0]!, title: "Copy" }];
    expect(pointers(s)).toEqual(expect.arrayContaining(["/title", "/subject/currency", "/subject/decimals", `/panels/${s.panels.length - 1}/id`]));
  });

  it("checks kinds and chart types against the preset, and the planner part", () => {
    const s = clone(FORECAST_V2);
    s.panels = [...s.panels, { kind: "chart", id: "conf", title: "Confusion", intent: "confusion", source: { kind: "output", variable: "y_hat" } }, { kind: "mmm", id: "sc", title: "Scorecard", part: "scorecard" }];
    const n = s.panels.length;
    expect(pointers(s)).toEqual(expect.arrayContaining([`/panels/${n - 2}/intent`, `/panels/${n - 1}/kind`]));
    const m = clone(MMM_V2);
    m.mmm = null;
    expect(pointers(m)).toContain("/mmm");
  });

  it("checks reductions, accuracy charts and notes", () => {
    const s = clone(REGRESSION_V2);
    s.panels = [
      { kind: "kpi", id: "a", title: "Total", source: { kind: "output", variable: "y_hat" }, reduce: { how: "sum", over: [] } },
      { kind: "kpi", id: "b", title: "Share", source: { kind: "output", variable: "y_hat" }, reduce: { how: "share_above", over: [] } },
      { kind: "kpi", id: "c", title: "Metric", source: { kind: "metric", key: "rmse" }, reduce: { how: "last", over: [] } },
      { kind: "chart", id: "d", title: "History", intent: "accuracy_history", source: { kind: "output", variable: "y_hat" } },
      { kind: "note", id: "e", title: "Note", text: "  " },
    ];
    expect(pointers(s)).toEqual(["/panels/0/reduce/over", "/panels/1/reduce/threshold", "/panels/2/reduce/how", "/panels/3/intent", "/panels/4/text"]);
  });

  it("refuses inputs that the preset has not, and bad scenario inputs", () => {
    const s = clone(CAUSAL_V2);
    s.scenario_rows = {
      enabled: true,
      max_rows: 5,
      outputs: [],
      inputs: [
        { name: "price", label: "Price", bind: { var: "price" }, type: "number", min: 10, max: 5, default: 7 },
        { name: "plan", label: "Plan", bind: { var: "plan" }, type: "choice", choices: ["a", "b"], default: "c" },
      ],
    };
    expect(pointers(s)).toEqual(expect.arrayContaining(["/scenario_rows/enabled", "/scenario_rows/outputs", "/scenario_rows/inputs/0/min", "/scenario_rows/inputs/1/default"]));
  });

  it("needs an id column for uploads matched by case, and a threshold in range", () => {
    const s = clone(CLASSIFIER_V2);
    s.actuals = { ...s.actuals, enabled: true, match: "row", id_column: null, threshold: 0.99 };
    expect(pointers(s)).toEqual(expect.arrayContaining(["/actuals/id_column", "/actuals/threshold"]));
  });

  it("checks the planner's channels against the response grid only when it is known", () => {
    const names = MMM_V2.mmm!.channels.map((c) => c.name);
    expect(specIssuesV2(MMM_V2, { gridChannels: names })).toEqual([]);
    expect(specIssuesV2(MMM_V2, { gridChannels: names.slice(1) }).map((i) => i.pointer)).toEqual(["/mmm/channels/0/name"]);
    expect(specIssuesV2(MMM_V2, { gridChannels: null }).map((i) => i.pointer)).toEqual(["/mmm/whatif/enabled"]);
  });
});

describe("catalog checks", () => {
  const catalog: SourceCatalog = {
    outputs: [{ name: "y_hat", manifest_name: "y_hat", dims: ["date"], time_dim: "date", shape: "time", representation: "point", centre: "mean", stats: ["centre"], reduces: ["none", "last"], intents: ["line"], predictive_ok: false }],
    metrics: [{ key: "rmse" }],
    pictures: [],
  };
  it("says when an output, a summary, a band, a value or a chart does not fit", () => {
    const s = clone(FORECAST_V2);
    s.panels = [
      { kind: "kpi", id: "a", title: "A", source: { kind: "output", variable: "nope" } },
      { kind: "kpi", id: "b", title: "B", source: { kind: "output", variable: "y_hat" }, stat: "high", reduce: { how: "next", over: [] }, band: "outcome" },
      { kind: "chart", id: "c", title: "C", intent: "fan", source: { kind: "output", variable: "y_hat" }, band: "none" },
      { kind: "kpi", id: "d", title: "D", source: { kind: "metric", key: "mape" }, stat: "value" },
    ];
    expect(catalogIssuesV2(s, catalog).map((i) => i.pointer)).toEqual(["/panels/0/source/variable", "/panels/1/reduce/how", "/panels/1/band", "/panels/1/stat", "/panels/2/intent", "/panels/3/source/key"]);
    expect(catalogIssuesV2(s, null)).toEqual([]);
  });
});

describe("panel edits", () => {
  it("moves a panel up and down, clamped at the ends", () => {
    const ids = (s: DashboardSpecV2) => s.panels.map((p) => p.id);
    const s = clone(CLASSIFIER_V2);
    const before = ids(s);
    expect(ids(movePanel(s, 0, 1))).toEqual([before[1], before[0], ...before.slice(2)]);
    expect(ids(movePanel(s, 0, -1))).toEqual(before);
    expect(ids(movePanel(s, before.length - 1, 1))).toEqual(before);
  });

  it("makes ids no other panel has", () => {
    expect(newPanelId({ panels: [{ kind: "note", id: "note", title: "N", text: "t" }] }, "note")).toBe("note_2");
    expect(newPanelId({ panels: [] }, "9 Bad id!")).toBe("bad_id_");
  });

  it("says why a panel cannot be added", () => {
    const s = clone(FORECAST_V2);
    expect(addPanelReason(s, "mmm", {})).toMatch(/only for the media planner/);
    expect(addPanelReason(s, "recipe", { pictures: [] })).toMatch(/no plots/);
    expect(addPanelReason(s, "kpi", undefined)).toMatch(/Wait/);
    expect(addPanelReason(s, "note", undefined)).toBeNull();
    s.panels = Array.from({ length: 24 }, (_, i) => ({ kind: "note" as const, id: `n${i}`, title: "N", text: "t" }));
    expect(addPanelReason(s, "note", {})).toMatch(/at most 24/);
  });

  it("binds a new KPI to the main output with a band that fits it", () => {
    const s = clone(FORECAST_V2);
    const catalog: SourceCatalog = { outputs: [{ name: s.subject.variable, manifest_name: s.subject.variable, dims: ["t"], time_dim: "t", shape: "time", representation: "quantiles", centre: "median", stats: ["centre", "low", "high"], reduces: ["none", "last", "next"], intents: ["fan"], predictive_ok: false }] };
    const p = defaultPanel(s, "kpi", catalog);
    expect(p).toMatchObject({ kind: "kpi", source: { kind: "output", variable: s.subject.variable }, reduce: { how: "last" }, band: "mean" });
    expect(specIssuesV2({ ...s, panels: [...s.panels, p!] })).toEqual([]);
  });
});

describe("preset switch", () => {
  it("drops the panels and inputs the new preset has not, and says so", () => {
    const s = clone(CLASSIFIER_V2);
    s.batch = { enabled: true, outputs: [s.subject.variable], id_column: "id" };
    const out = switchPreset(s, "causal_impact");
    expect(out.spec.preset).toBe("causal_impact");
    expect(out.spec.batch?.enabled).toBe(false);
    expect(out.removed).toContain("Score a file is turned off.");
    for (const p of out.spec.panels) if (p.kind === "chart") expect(PRESET_RULES.causal_impact.intents).toContain(p.intent);
    expect(specIssuesV2(out.spec)).toEqual([]);
  });

  it("leaves the planner with its channels gone, and joins it only with a response grid", () => {
    const out = switchPreset(clone(MMM_V2), "forecast");
    expect(out.spec.mmm).toBeNull();
    expect(out.spec.panels.some((p) => p.kind === "mmm")).toBe(false);
    expect(out.removed.join(" ")).toMatch(/channels/);
    expect(specIssuesV2(out.spec)).toEqual([]);
    expect(presetSwitchReason(FORECAST_V2, "mmm_planner", { gridChannels: null })).toMatch(/response grid/);
    const planner = switchPreset(clone(FORECAST_V2), "mmm_planner", { gridChannels: ["tv", "search"] }).spec;
    expect(planner.mmm?.channels.map((c) => c.name)).toEqual(["tv", "search"]);
    expect(specIssuesV2(planner)).toEqual([]);
  });

  it("keeps a note when nothing else fits", () => {
    const s = clone(FIELD_V2);
    s.panels = [{ kind: "chart", id: "f", title: "Map", intent: "field", source: { kind: "output", variable: "x" } }];
    expect(switchPreset(s, "image").spec.panels.map((p) => p.kind)).toEqual(["note"]);
  });
});

describe("inputs", () => {
  it("seeds a number input from declared bounds, else from the fit data (to check)", () => {
    const declared: CatalogInput = { var: "price", kind: "number", bound_min: 0, bound_max: 50, profile_min: 3, profile_max: 40 };
    expect(seedInput(declared)).toMatchObject({ fromProfile: false, def: { name: "price", type: "number", min: 0, max: 50, default: 25 } });
    const profiled: CatalogInput = { var: "tenure", kind: "integer", profile_min: 1.2, profile_max: 71.6 };
    const s = seedInput(profiled)!;
    expect(s.fromProfile).toBe(true);
    expect(s.def).toMatchObject({ type: "integer", min: 1, max: 72, step: 1, default: 37 });
    expect(seedInput({ dim: "store", kind: "choice", levels: ["s1", "s2"] })?.def).toMatchObject({ bind: { dim: "store" }, choices: ["s1", "s2"], default: "s1" });
    expect(seedInput(declared, ["price"])).toBeNull();
  });

  it("never offers batch on an image dashboard's links", () => {
    const s = clone(IMAGE_V2);
    s.batch = { enabled: true, outputs: ["y"], id_column: "id" };
    expect(shareInputsOfV2(s)).not.toContain("batch");
    const r = clone(REGRESSION_V2);
    r.batch = { enabled: true, outputs: ["y"], id_column: "id" };
    r.scenario_rows = { ...r.scenario_rows, enabled: true };
    expect(shareInputsOf(r)).toEqual(expect.arrayContaining(["scenario_rows", "batch"]));
  });

  it("warns when a limit refuses every run", () => {
    const s = clone(FORECAST_V2);
    s.actuals = { ...s.actuals, enabled: true };
    s.limits = { exact_runs_per_day: 0 };
    expect(limitWarnings(s).map((w) => w.pointer)).toEqual(["/limits/exact_runs_per_day"]);
    s.limits = { exact_runs_per_day: 20 };
    expect(limitWarnings(s)).toEqual([]);
  });
});

describe("either layout", () => {
  it("puts each issue on the tab where it is fixed", () => {
    expect(tabOfIssue("/scenario_rows/inputs/0/min")).toBe("inputs");
    expect(tabOfIssue("/batch/outputs")).toBe("inputs");
    expect(tabOfIssue("/mmm/whatif/enabled")).toBe("inputs");
    expect(tabOfIssue("/actuals/threshold")).toBe("setup");
    expect(tabOfIssue("/actuals/id_column")).toBe("inputs");
    expect(tabOfIssue("/whatif/enabled")).toBe("inputs");
    expect(tabOfIssue("/panels/2/source/variable")).toBe("setup");
    expect(tabOfIssue("/mmm/channels/1/name")).toBe("setup");
  });

  it("names the place of an issue in plain words", () => {
    expect(placeOfIssue("/panels/2/title")).toBe("Panel 3");
    expect(placeOfIssue("/channels/0/name")).toBe("Channels");
    expect(placeOfIssue("/kpi/variable")).toBe("KPI");
    expect(placeOfIssue(null)).toBeNull();
  });

  it("cleans a layout 2 spec as layout 2", () => {
    const s = clone(FORECAST_V2);
    s.title = "  Sales  ";
    s.client_name = " ";
    const c = cleanSpec(s);
    expect(c.layout_version).toBe(2);
    expect(c.title).toBe("Sales");
    expect(c.client_name).toBeNull();
  });
});
