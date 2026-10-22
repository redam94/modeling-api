import { describe, expect, it } from "vitest";

import { fillPrepSlots, slotValue } from "./doc";
import { slotKeys } from "./prep-builder";
import type { PrepPresetDoc } from "./types";

// core.formula's shape: `dims` is an array of dim names (a dim parameter), `out` a variable name.
const formula = {
  settings_schema: {
    type: "object",
    properties: { out: { type: "string" }, expr: { type: "string" }, dims: { type: "array", items: { type: "string" } } },
  },
  var_params: ["out"],
  dim_params: ["dims"],
};

describe("slotKeys (v0.18 live: a dim slot on an array setting)", () => {
  it("marks array settings as list slots, whatever the slot kind", () => {
    expect(slotKeys(formula)).toEqual([
      { key: "out", kind: "var", list: false },
      { key: "dims", kind: "dim", list: true },
    ]);
  });

  it("a dim slot on an array setting fills in as a one-item list", () => {
    const [dims] = slotKeys(formula).filter((k) => k.key === "dims");
    const doc: PrepPresetDoc = {
      apiVersion: "mapi/v1",
      kind: "PrepPreset",
      metadata: { name: "p", title: "P" },
      slots: { dims: { kind: "dim", label: "Which dimension" } },
      steps: [{ id: "f", use: "core.formula@^1", with: { out: "x", expr: "a * b", dims: slotValue("dims", dims!.list) } }],
    };
    expect(doc.steps[0]!.with!.dims).toEqual(["$slot:dims"]);
    expect(fillPrepSlots(doc, { dims: "obs" }).steps[0]!.with!.dims).toEqual(["obs"]);
  });
});
