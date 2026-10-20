import { parse } from "yaml";
import { describe, expect, it } from "vitest";

import type { CatalogResponse, Dataset } from "@/api/types";

import { addStep, addToList, newStepId, setDataset, setModel, starterWith } from "./spec-doc";
import { enrichSpecSchema, hintKey, namesBeforeStep, parseDatasetRef, specShape, type HintSources } from "./spec-hints";

const USE = { type: "string", examples: ["core.resample@^1"], pattern: "^.+$" };

/** The shape of the server's `modelspec.json`, cut down to what the hints touch. */
const BASE = {
  type: "object",
  properties: {
    data: { $ref: "#/$defs/DataBlock" },
    pipeline: { type: "array", items: { $ref: "#/$defs/StepCall" } },
    model: { $ref: "#/$defs/ModelBlock" },
    outputs: { $ref: "#/$defs/OutputsBlock" },
  },
  $defs: {
    DataBlock: { type: "object", properties: { dataset: { type: "string", examples: ["dataset://growth/x@latest"] }, bind: { $ref: "#/$defs/Binding" } } },
    Binding: {
      type: "object",
      properties: {
        dims: { type: "object", patternProperties: { "^.+$": { type: "string" } } },
        vars: { type: "object", patternProperties: { "^.+$": { type: "string" } } },
      },
    },
    TimeHoldoutSplit: { type: "object", properties: { dim: { type: "string" }, after_step: { anyOf: [{ type: "string" }, { type: "null" }] } } },
    ColumnSplit: { type: "object", properties: { dim: { type: "string" }, column: { type: "string" } } },
    StratifiedSplit: { type: "object", properties: { dim: { type: "string" }, by: { type: "string" } } },
    StepCall: { type: "object", properties: { id: { type: "string" }, use: USE, with: { type: "object" } } },
    ModelBlock: { type: "object", properties: { use: USE, settings: { type: "object" } } },
    OutputsBlock: { type: "object", properties: { plots: { type: "array", items: USE }, derived: { type: "array", items: { type: "string" } } } },
  },
};

const catalog = {
  digest: "sha256:x",
  steps: [
    {
      id: "core.impute",
      version: "1.0.0",
      kind: "value",
      stateful: true,
      summary: "Fill missing values",
      var_params: ["vars"],
      dim_params: ["over"],
      settings_schema: {
        type: "object",
        required: ["vars"],
        properties: {
          vars: { type: "array", items: { type: "string" }, minItems: 1 },
          over: { type: "array", items: { type: "string" }, default: [] },
          method: { $ref: "#/$defs/Method" },
        },
        $defs: { Method: { enum: ["mean", "median"] } },
      },
    },
    { id: "core.impute", version: "0.9.0", kind: "value", stateful: true },
    {
      id: "core.resample",
      version: "1.2.0",
      kind: "reshape",
      stateful: false,
      dim_params: ["dim"],
      settings_schema: { type: "object", required: ["dim", "how"], properties: { dim: { type: "string" }, how: { enum: ["sum", "mean"] } } },
    },
  ],
  plots: [{ id: "core.ppc_fan", title: "PPC fan" }],
  models: [
    {
      manifest: {
        id: "mmm.bayesian",
        version: "1.4.0",
        capabilities: {},
        plots: ["mmm.decomposition"],
        derived: [{ name: "contributions", dims: ["time", "channel"] }],
        schema_template: { template: { dims: { geo: {}, time: {}, "{{extra}}": {} }, vars: { y: {}, spend: {} } } },
        settings_schema: { type: "object", properties: { adstock: { $ref: "#/$defs/Adstock" } }, $defs: { Adstock: { type: "object" } } },
      },
    },
  ],
} as unknown as CatalogResponse;

const datasets = [{ id: "ds_1", name: "eu_media_daily", ref: "dataset://default/eu_media_daily", latest_version: 3 }] as unknown as Dataset[];

const columns = {
  dims: { market: { kind: "categorical" }, date: { kind: "time", freq: "D" } },
  vars: { revenue_local: { dtype: "float64", dims: ["market", "date"] }, spend_local: { dtype: "float64", dims: ["market", "date"] } },
} as HintSources["columns"];

const SPEC = `data:
  dataset: dataset://default/eu_media_daily@latest
  bind:
    dims: {geo: market, time: date}
    vars: {y: revenue_local}
pipeline:
  - {id: weekly, use: core.resample@^1, with: {dim: time, how: sum}}
  - {id: fill, use: core.impute@^1}
model:
  use: mmm.bayesian@^1
`;

const sources = (over: Partial<HintSources> = {}): HintSources => ({ catalog, datasets, columns, stages: [], ...over });

/** Walk a JSON value by keys; `$ref` strings are followed into `$defs` of `root`. */
function at(root: unknown, ...path: (string | number)[]): unknown {
  let cur: unknown = root;
  for (const key of path) {
    if (cur === null || typeof cur !== "object") return undefined;
    cur = (cur as Record<string | number, unknown>)[key];
  }
  return cur;
}

function deref(root: unknown, holder: unknown): unknown {
  const ref = at(holder, "$ref");
  return typeof ref === "string" ? at(root, "$defs", ref.slice("#/$defs/".length)) : undefined;
}

const bodies = (schema: unknown) => ((at(schema, "defaultSnippets") ?? []) as { body: string }[]).map((s) => s.body);
const keys = (v: unknown) => Object.keys(v as object);

describe("enrichSpecSchema", () => {
  const shape = specShape(parse(SPEC));
  const out = enrichSpecSchema(BASE, shape, sources());
  const def = (...path: string[]) => at(out, "$defs", ...path);

  it("does not mutate the server schema", () => {
    expect(at(BASE, "$defs", "StepCall", "properties", "use", "defaultSnippets")).toBeUndefined();
    expect(BASE.properties.pipeline).not.toHaveProperty("prefixItems");
  });

  it("offers catalog steps, models and datasets in place of the generic examples", () => {
    expect(def("StepCall", "properties", "use", "examples")).toBeUndefined();
    expect(bodies(def("StepCall", "properties", "use"))).toEqual(["core.impute@^1", "core.resample@^1"]);
    expect(bodies(def("ModelBlock", "properties", "use"))).toEqual(["mmm.bayesian@^1"]);
    expect(bodies(def("DataBlock", "properties", "dataset"))).toEqual(["dataset://default/eu_media_daily@latest"]);
  });

  it("suggests the model's canonical names as bind keys and dataset columns as values", () => {
    const dims = def("Binding", "properties", "dims");
    expect(keys(at(dims, "properties"))).toEqual(["geo", "time"]);
    expect(bodies(at(dims, "properties", "geo"))).toEqual(["market", "date"]);
    expect(bodies(at(dims, "patternProperties", "^.+$"))).toEqual(["market", "date"]);
    expect(keys(def("Binding", "properties", "vars", "properties"))).toEqual(["y", "spend"]);
  });

  it("gives each known step its settings schema, with names the step can see", () => {
    const resampleWith = deref(out, at(out, "properties", "pipeline", "prefixItems", 0, "properties", "with"));
    // Before any validation, the names are the dataset columns under the binding.
    expect(bodies(at(resampleWith, "properties", "dim"))).toEqual(["geo", "time"]);
    const imputeWith = deref(out, at(out, "properties", "pipeline", "prefixItems", 1, "properties", "with"));
    expect(bodies(at(imputeWith, "properties", "vars", "items"))).toEqual(["y", "spend_local"]);
    // The plugin's own $defs are hoisted and its $refs rewritten.
    expect(at(imputeWith, "properties", "method", "$ref")).toMatch(/^#\/\$defs\/x-.*\.Method$/);
    expect(deref(out, at(imputeWith, "properties", "method"))).toEqual({ enum: ["mean", "median"] });
  });

  it("binds the model settings schema and lists its plots and derived outputs", () => {
    const settings = deref(out, def("ModelBlock", "properties", "settings"));
    expect(deref(out, at(settings, "properties", "adstock"))).toEqual({ type: "object" });
    expect(bodies(def("OutputsBlock", "properties", "plots", "items"))).toEqual(["mmm.decomposition", "core.ppc_fan"]);
    expect(bodies(def("OutputsBlock", "properties", "derived", "items"))).toEqual(["contributions"]);
  });

  it("offers the raw names to the column and stratified splits (v0.14)", () => {
    expect(bodies(def("ColumnSplit", "properties", "dim"))).toEqual(["geo", "time"]);
    expect(bodies(def("ColumnSplit", "properties", "column"))).toEqual(expect.arrayContaining(["y", "geo"]));
    expect(bodies(def("StratifiedSplit", "properties", "by"))).toEqual(bodies(def("ColumnSplit", "properties", "column")));
  });

  it("uses stage schemas from validation once there are any", () => {
    const stages = [{ name: "weekly", data_schema: { dims: { geo: { kind: "categorical" }, week: { kind: "time" } }, vars: { y: { dtype: "float64", dims: [] } } } }] as HintSources["stages"];
    expect(namesBeforeStep(1, shape, sources({ stages })).dims.map((d) => d.name)).toEqual(["geo", "week"]);
    expect(hintKey(shape, sources({ stages }))).not.toBe(hintKey(shape, sources()));
  });
});

describe("parseDatasetRef", () => {
  it("splits the version off", () => {
    expect(parseDatasetRef("dataset://p/x@v3")).toEqual({ ref: "dataset://p/x", version: 3 });
    expect(parseDatasetRef("dataset://p/x@7")).toEqual({ ref: "dataset://p/x", version: 7 });
    expect(parseDatasetRef("dataset://p/x@latest")).toEqual({ ref: "dataset://p/x", version: null });
    expect(parseDatasetRef("dataset://p/x")).toEqual({ ref: "dataset://p/x", version: null });
  });
});

describe("spec-doc edits", () => {
  const commented = `# my spec\n${SPEC}outputs:\n  plots: [core.ppc_fan] # keep\n`;

  it("appends a step with a unique id and keeps comments", () => {
    const next = addStep(commented, "core.resample@^1", { dim: "time", how: "sum" }, "core.resample")!;
    expect(next).toContain("# my spec");
    expect(next).toContain("# keep");
    expect(next).toContain("- {id: resample, use: core.resample@^1, with: {dim: time, how: sum}}");
    // Untouched flow collections keep their formatting.
    expect(next).toContain("dims: {geo: market, time: date}");
    expect(next).toContain("plots: [core.ppc_fan] # keep");
    expect(newStepId("core.resample", ["resample", "resample_2"])).toBe("resample_3");
  });

  it("fills required settings without defaults", () => {
    const step = catalog.steps!.find((s) => s.id === "core.resample")!;
    const names = { dims: [{ name: "geo", detail: "categorical" }, { name: "date", detail: "time · D" }], vars: [] };
    expect(starterWith(step, names)).toEqual({ dim: "date", how: "sum" });
  });

  it("sets the model, dropping settings only when the model changes", () => {
    const withSettings = SPEC.replace("model:\n  use: mmm.bayesian@^1\n", "model:\n  use: mmm.bayesian@^1\n  settings: {a: 1}\n");
    expect(at(parse(setModel(withSettings, "mmm.bayesian@^2")!), "model", "settings")).toEqual({ a: 1 });
    expect(at(parse(setModel(withSettings, "glm.hierarchical@^1")!), "model")).toEqual({ use: "glm.hierarchical@^1" });
  });

  it("sets the dataset and adds to lists once", () => {
    expect(at(parse(setDataset(SPEC, "dataset://p/y@latest")!), "data", "dataset")).toBe("dataset://p/y@latest");
    const once = addToList(commented, ["outputs", "plots"], "mmm.decomposition")!;
    expect(at(parse(addToList(once, ["outputs", "plots"], "mmm.decomposition")!), "outputs", "plots")).toEqual(["core.ppc_fan", "mmm.decomposition"]);
    expect(at(parse(addToList(SPEC, ["outputs", "derived"], "contributions")!), "outputs", "derived")).toEqual(["contributions"]);
  });

  it("refuses to edit YAML that does not parse", () => {
    expect(setDataset("data: [unclosed", "dataset://p/y")).toBeNull();
  });
});
