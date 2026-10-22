import { parse } from "yaml";
import { describe, expect, it } from "vitest";

import { FIXTURE_DOCS, SAMPLE_SEED_DOCS } from "@/mocks/library";

import { chartStarters, columnsOf, simpleSpec } from "./chart-starters";
import {
  fillPrepSlots,
  identifier,
  moveItem,
  newModelRecipe,
  newTerm,
  slotOfValue,
  slotsUsed,
  slotValue,
  slugify,
  specFromFragment,
  specNameFor,
  summarizeModelRecipe,
  tidyDoc,
} from "./doc";
import { docToYaml, yamlToDoc } from "./doc-yaml";
import { LIBRARY_TABS, libraryHref, validateLibrarySearch } from "./search";
import { datasetNamesOf, docOfKind, isModelRecipeDoc } from "./types";

describe("builder ↔ YAML round trip", () => {
  it.each([
    ["declarative model", FIXTURE_DOCS.model],
    ["preset", FIXTURE_DOCS.preset],
    ["prep preset", FIXTURE_DOCS.prep],
    ["chart template", FIXTURE_DOCS.chart],
    ["seed model", SAMPLE_SEED_DOCS[0]!],
  ])("%s survives doc → YAML → doc", (_name, doc) => {
    const kind = doc.kind === "ModelRecipe" ? "model_recipe" : doc.kind === "PrepPreset" ? "prep_preset" : "chart_template";
    const back = yamlToDoc(docToYaml(doc), kind, (v) => docOfKind(kind, v) !== null);
    expect(back.ok).toBe(true);
    if (back.ok) expect(back.doc).toEqual(tidyDoc(doc));
  });

  it("a builder document round-trips with its empty parts left out", () => {
    const doc = newModelRecipe("Store sales, with promo lift!");
    doc.declarative!.outcome.var = "sales";
    doc.declarative!.terms = [{ ...newTerm("price"), label: "", sign: "negative", varies_by: null }];
    const yaml = docToYaml(doc);
    expect(yaml).not.toContain("varies_by");
    expect(yaml).not.toContain("label:");
    expect(yaml).not.toContain("interactions");
    const back = yamlToDoc(yaml, "model_recipe", isModelRecipeDoc);
    expect(back.ok && back.doc).toEqual(tidyDoc(doc));
    expect(parse(yaml)).toMatchObject({ kind: "ModelRecipe", metadata: { name: "store-sales-with-promo-lift" }, declarative: { terms: [{ var: "price", sign: "negative" }] } });
  });

  it("refuses YAML of another kind, broken YAML and aliases", () => {
    expect(yamlToDoc(docToYaml(FIXTURE_DOCS.prep), "model_recipe", isModelRecipeDoc)).toEqual({ ok: false, error: 'The document must be "kind: ModelRecipe".' });
    expect(yamlToDoc("kind: [", "model_recipe", isModelRecipeDoc).ok).toBe(false);
    const bomb = "a: &a [1, 2]\nb: *a\nkind: ModelRecipe\n";
    expect(yamlToDoc(bomb, "model_recipe", isModelRecipeDoc).ok).toBe(false);
  });

  it("keeps a chart's Vega-Lite spec exactly, empty parts included", () => {
    const doc = { ...FIXTURE_DOCS.chart, plot: { data: { name: "fan" }, mark: "line", transform: [], encoding: {} } };
    expect(tidyDoc(doc).plot).toEqual(doc.plot);
  });
});

describe("plain words", () => {
  it("summarises a declarative model", () => {
    const lines = summarizeModelRecipe(FIXTURE_DOCS.model);
    expect(lines[0]).toBe("Explains revenue_local, which is a number.");
    expect(lines[1]).toBe("One driver: Media spend (spend_local), logged and standardised, can only lift it, differently in each market.");
    expect(lines[2]).toBe("Each market gets its own starting level.");
  });

  it("summarises a preset", () => {
    expect(summarizeModelRecipe(FIXTURE_DOCS.preset)).toEqual([
      "Uses the catalog model ts.sarimax@^1 with your settings for order, seasonal_order and trend.",
      "People who use it can change order and trend.",
    ]);
  });

  it("says what is missing on a new model", () => {
    expect(summarizeModelRecipe(newModelRecipe())).toEqual(["Explains an outcome you have not picked, which is a number.", "It has no drivers yet. Add at least one."]);
  });

  it("makes names", () => {
    expect(slugify("  Revenue — by Market (EU)  ")).toBe("revenue-by-market-eu");
    expect(slugify("!!!")).toBe("my-item");
    expect(slugify("x".repeat(80))).toHaveLength(64);
    expect(identifier("var list")).toBe("var_list");
    expect(identifier("2nd")).toBe("x_2nd");
    expect(specNameFor("Store sales", new Date(2026, 9, 22, 9, 5))).toBe("store-sales-261022-0905");
  });
});

describe("prep presets", () => {
  it("fills slots like the server: whole values, and lists spliced in", () => {
    const { steps, missing } = fillPrepSlots(FIXTURE_DOCS.prep, { time: "date", skewed: ["revenue_local", "spend_local"] });
    expect(missing).toEqual([]);
    expect(steps.map((s) => s.id)).toEqual(["p_weekly", "p_log"]);
    expect(steps[0]!.with).toMatchObject({ dim: "date", freq: "W-SUN" });
    expect(steps[1]!.with).toEqual({ vars: ["revenue_local", "spend_local"] });
  });

  it("reports unfilled required slots", () => {
    expect(fillPrepSlots(FIXTURE_DOCS.prep, { time: "date" }).missing).toEqual(["skewed"]);
  });

  it("finds and writes slot references", () => {
    expect([...slotsUsed(FIXTURE_DOCS.prep.steps)].sort()).toEqual(["skewed", "time"]);
    expect(slotValue("cols", true)).toEqual(["$slot:cols"]);
    expect(slotOfValue(["$slot:cols"])).toBe("cols");
    expect(slotOfValue("$slot:time")).toBe("time");
    expect(slotOfValue(["a", "b"])).toBeNull();
  });

  it("moves items for keyboard reordering", () => {
    expect(moveItem(["a", "b", "c"], 0, 1)).toEqual(["b", "a", "c"]);
    expect(moveItem(["a", "b", "c"], 2, 0)).toEqual(["c", "a", "b"]);
    expect(moveItem(["a", "b"], 1, 9)).toEqual(["a", "b"]);
  });
});

describe("use in Build", () => {
  it("puts a compiled fragment on the dataset, labels kept", () => {
    const fragment = { data: { bind: { vars: { y: "sales" } } }, pipeline: [{ id: "t0_log1p", use: "core.log1p@^1", with: { vars: ["price"] } }], model: { use: "tabular.declarative_glm@^1", settings: { family: "normal" } }, metadata: { labels: { "mapi.library/model": "lib_x-r2" } } };
    const spec = specFromFragment(fragment, { name: "store-sales-1", project: "growth", dataset: "dataset://growth/sales@latest" });
    expect(spec).toEqual({
      apiVersion: "mapi/v1",
      kind: "ModelSpec",
      metadata: { name: "store-sales-1", project: "growth", labels: { "mapi.library/model": "lib_x-r2" } },
      data: { dataset: "dataset://growth/sales@latest", bind: { vars: { y: "sales" } } },
      pipeline: fragment.pipeline,
      model: fragment.model,
    });
  });
});

describe("chart starters", () => {
  const rows = [
    { x: "2025-01-05T00:00:00.000Z", group: "DE", point: 1, lo_90: 0.5, hi_90: 1.5, observed: 1.1 },
    { x: "2025-01-12T00:00:00.000Z", group: "FR", point: 2, lo_90: 1.5, hi_90: 2.5, observed: null },
  ];

  it("reads column kinds from the example rows", () => {
    expect(columnsOf({ fan: rows })[0]!.columns).toEqual([
      { name: "x", type: "temporal" },
      { name: "group", type: "nominal" },
      { name: "point", type: "quantitative" },
      { name: "lo_90", type: "quantitative" },
      { name: "hi_90", type: "quantitative" },
      { name: "observed", type: "quantitative" },
    ]);
  });

  it("offers a band chart first and reads only the named dataset", () => {
    const cols = columnsOf({ fan: rows });
    const starters = chartStarters(cols);
    expect(starters[0]!.label).toBe("Line with a band");
    const spec = simpleSpec(starters[0]!.chart, cols);
    expect(datasetNamesOf(spec)).toEqual(["fan"]);
    expect(JSON.stringify(spec)).not.toContain("url");
    expect(spec).toMatchObject({ data: { name: "fan" }, facet: { field: "group" }, spec: { layer: [{ mark: { type: "area" } }, { mark: { type: "line" } }, { mark: { type: "point" } }] } });
  });

  it("offers a curve for spend-shaped data", () => {
    const curves = [{ channel: "tv", spend: 1, point: 2, lo: 1, hi: 3 }];
    const s = chartStarters(columnsOf({ curves }));
    expect(s[0]).toMatchObject({ label: "Curve", chart: { x: "spend", y: "point", color: "channel", bandLo: "lo", bandHi: "hi" } });
  });
});

describe("search params", () => {
  it("keeps only valid values", () => {
    expect(validateLibrarySearch({ tab: "charts", new: "chart", source: "core.ppc_fan", item: "lib_bad", base: "Bad Id" })).toEqual({ tab: "charts", new: "chart", source: "core.ppc_fan", item: undefined, base: undefined });
    expect(validateLibrarySearch({ tab: "nope", item: "lib_01jaf3l1b2r3y4m5n6000000001" }).item).toBeUndefined();
    expect(validateLibrarySearch({ item: "lib_01jaf3l1b2r3y4m5n60000000a" }).item).toBe("lib_01jaf3l1b2r3y4m5n60000000a");
    expect(LIBRARY_TABS).toEqual(["models", "prep", "charts", "workflows"]);
    expect(libraryHref({ tab: "charts", new: "chart" })).toBe("/library?tab=charts&new=chart");
  });

});
