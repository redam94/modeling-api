import { describe, expect, it } from "vitest";

import { deepMerge, preparePlotly, prepareVegaLite, resolvePlotlyRefs, SpecError } from "./prepare";

const datasets = {
  contrib: [
    { channel: "tv", value: 1.5 },
    { channel: "search", value: 2 },
  ],
};

describe("resolvePlotlyRefs", () => {
  it("replaces {$ref: name.column} with the column", () => {
    const spec = { data: [{ type: "bar", x: { $ref: "contrib.channel" }, y: { $ref: "contrib.value" } }] };
    expect(resolvePlotlyRefs(spec, datasets)).toEqual({
      data: [{ type: "bar", x: ["tv", "search"], y: [1.5, 2] }],
    });
  });

  it("leaves objects with $ref plus other keys alone", () => {
    const node = { $ref: "contrib.value", other: 1 };
    expect(resolvePlotlyRefs(node, datasets)).toEqual(node);
  });

  it.each(["contrib", "missing.value", "contrib.nope", ".x"])("rejects %s", (ref) => {
    expect(() => resolvePlotlyRefs({ $ref: ref }, datasets)).toThrow(SpecError);
  });
});

describe("prepareVegaLite", () => {
  it("injects datasets, theme config and container width", () => {
    const out = prepareVegaLite(
      { $schema: "https://vega.github.io/schema/vega-lite/v6.json", mark: "bar", data: { name: "contrib" } },
      datasets,
      "dark",
    );
    expect(out.datasets).toEqual(datasets);
    expect(out.width).toBe("container");
    expect(out.$schema).toBeUndefined();
    expect((out.config as Record<string, unknown>).background).toBeNull();
  });

  it("keeps the spec's own width, inline datasets and config overrides", () => {
    const out = prepareVegaLite(
      { mark: "bar", width: 300, datasets: { inline: [{ a: 1 }] }, config: { axis: { labelFontSize: 20 } } },
      datasets,
      "light",
    );
    expect(out.width).toBe(300);
    expect(Object.keys(out.datasets as object).sort()).toEqual(["contrib", "inline"]);
    expect((out.config as Record<string, Record<string, unknown>>).axis?.labelFontSize).toBe(20);
  });
});

describe("preparePlotly", () => {
  it("requires a data array and merges the layout under the theme", () => {
    expect(() => preparePlotly({ layout: {} }, datasets, "light")).toThrow(SpecError);
    const out = preparePlotly({ data: [], layout: { title: { text: "T" } } }, datasets, "light");
    expect(out.layout.title).toEqual({ text: "T" });
    expect(out.layout.paper_bgcolor).toBe("rgba(0,0,0,0)");
  });
});

describe("deepMerge", () => {
  it("merges objects recursively and replaces arrays", () => {
    expect(deepMerge({ a: { b: 1, c: [1] } }, { a: { c: [2], d: 3 } })).toEqual({ a: { b: 1, c: [2], d: 3 } });
  });
});
