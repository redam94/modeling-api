import { parse } from "yaml";
import { describe, expect, it } from "vitest";

import { insertStep, moveStep, removeStep, renameStep, setPath } from "@/features/editor/spec-doc";

import { inlineRefs, omitProps, splitKinds } from "./schema-utils";

const SPEC = `# keep me
data:
  dataset: dataset://default/x@latest
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}
pipeline:
  - {id: weekly, use: core.resample@^1, with: {dim: time, how: sum}}
  - {id: fill, use: core.impute@^1, with: {vars: [spend]}}
model:
  use: mmm.bayesian@^1
  settings:
    adstock: {kind: geometric, l_max: 6} # carry-over
    trend: {kind: linear}
`;

const ids = (text: string | null) => (parse(text!) as { pipeline: { id: string }[] }).pipeline.map((s) => s.id);
const get = (text: string | null, ...path: (string | number)[]): unknown =>
  path.reduce<unknown>((cur, k) => (cur && typeof cur === "object" ? (cur as Record<string | number, unknown>)[k] : undefined), parse(text!));

describe("pipeline edits", () => {
  it("inserts a step at an index with a unique id", () => {
    const next = insertStep(SPEC, 1, "core.resample@^1", {}, "core.resample");
    expect(ids(next)).toEqual(["weekly", "resample", "fill"]);
    expect(ids(insertStep(SPEC, 0, "core.impute@^1", {}, "core.impute"))).toEqual(["impute", "weekly", "fill"]);
    expect(ids(insertStep(SPEC, 99, "core.impute@^1", {}, "fill"))).toEqual(["weekly", "fill", "fill_2"]);
  });

  it("moves, removes and renames steps, keeping the split's after_step in step", () => {
    expect(ids(moveStep(SPEC, 1, 0))).toEqual(["fill", "weekly"]);
    const renamed = renameStep(SPEC, 0, "weeks");
    expect(ids(renamed)).toEqual(["weeks", "fill"]);
    expect(get(renamed, "data", "split", "after_step")).toBe("weeks");
    const removed = removeStep(SPEC, 0);
    expect(ids(removed)).toEqual(["fill"]);
    expect(get(removed, "data", "split")).toEqual({ kind: "time_holdout", dim: "time", last: 8 });
    expect(removed).toContain("# keep me");
  });
});

describe("setPath", () => {
  it("applies an object key by key, keeping comments and flow style", () => {
    const next = setPath(SPEC, ["model", "settings"], { adstock: { kind: "geometric", l_max: 8 }, seasonality: { yearly_order: 3 } })!;
    expect(next).toContain("adstock: {kind: geometric, l_max: 8} # carry-over");
    expect(get(next, "model", "settings")).toEqual({ adstock: { kind: "geometric", l_max: 8 }, seasonality: { yearly_order: 3 } });
    expect(next).toContain("# keep me");
  });

  it("keeps a flow list a flow list and deletes on undefined", () => {
    expect(setPath(SPEC, ["pipeline", 1, "with", "vars"], ["spend", "y"])).toContain("with: {vars: [spend, y]}");
    expect(get(setPath(SPEC, ["model", "settings", "trend"], undefined), "model", "settings")).toEqual({ adstock: { kind: "geometric", l_max: 6 } });
    expect(setPath(SPEC, ["execution"], undefined)).toBe(SPEC);
  });
});

describe("schema utils", () => {
  const root = {
    $defs: {
      Adstock: { title: "AdstockSettings", type: "object", properties: { l_max: { type: "integer" }, inner: { $ref: "#/$defs/Inner" } } },
      Inner: { type: "object", properties: { x: { type: "number" } } },
      Loop: { type: "object", properties: { next: { $ref: "#/$defs/Loop" } } },
      DataBlock: {
        properties: {
          split: { anyOf: [{ discriminator: { mapping: { time_holdout: "#/$defs/TimeHoldoutSplit", as_of: "#/$defs/AsOfSplit" }, propertyName: "kind" } }, { type: "null" }] },
        },
      },
    },
  };

  it("inlines local refs without the definitions' class-name titles, and stops at cycles", () => {
    const out = inlineRefs({ properties: { adstock: { $ref: "#/$defs/Adstock", description: "carry-over" }, loop: { $ref: "#/$defs/Loop" } } }, root);
    expect(get(JSON.stringify(out), "properties", "adstock")).toEqual({
      type: "object",
      description: "carry-over",
      properties: { l_max: { type: "integer" }, inner: { type: "object", properties: { x: { type: "number" } } } },
    });
    expect(JSON.stringify(out)).not.toContain("$ref");
  });

  it("reads the split kinds and drops properties", () => {
    expect(splitKinds(root)).toEqual([
      { kind: "time_holdout", def: "TimeHoldoutSplit" },
      { kind: "as_of", def: "AsOfSplit" },
    ]);
    expect(omitProps({ properties: { kind: {}, dim: {} }, required: ["kind", "dim"] }, ["kind"])).toEqual({ properties: { dim: {} }, required: ["dim"] });
  });
});
