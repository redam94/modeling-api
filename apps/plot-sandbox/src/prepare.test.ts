import { describe, expect, it } from "vitest";

import { compile } from "vega-lite";

import { deepMerge, polarSubplots, preparePlotly, prepareVegaLite, resolvePlotlyRefs, SpecError } from "./prepare";
import { findUnsafeConstructs, type JsonObject } from "./protocol";
import { contrast, DARK_VARIANTS, NON_TEXT_FILLS } from "./recipe-palette";

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

  it("fits unit and layered views to the frame width but pads composite views", () => {
    const unit = prepareVegaLite({ mark: "bar", data: { name: "contrib" } }, datasets, "light");
    expect(unit.autosize).toEqual({ type: "fit-x", contains: "padding" });
    const layered = prepareVegaLite({ layer: [{ mark: "line" }], width: 400 }, datasets, "light");
    expect(layered.autosize).toEqual({ type: "fit-x", contains: "padding" });
    const composites: JsonObject[] = [
      { vconcat: [{ mark: "bar", width: 240 }] },
      { hconcat: [{ mark: "bar" }] },
      { facet: { field: "geo", type: "nominal" }, spec: { layer: [{ mark: "line" }], width: 240 } },
    ];
    for (const composite of composites) {
      const out = prepareVegaLite(composite, datasets, "light");
      expect(out.autosize).toEqual({ type: "pad" });
      expect(out.width).toBeUndefined();
    }
    expect(prepareVegaLite({ vconcat: [], autosize: "none" }, datasets, "light").autosize).toBe("none");
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

describe("recipe colours in dark mode", () => {
  const recipe: JsonObject = {
    data: { name: "fan" },
    layer: [
      { mark: { type: "area", color: "#3366cc", opacity: 0.3 } },
      { mark: { type: "circle", color: "#1B2333" } },
      { mark: { type: "rule", color: "#c62828" } },
      { mark: "bar", encoding: { color: { field: "k", scale: { range: ["#6b7685", "#c45a00"] } } } },
    ],
  };
  const sets = { fan: [{ k: "#1b2333", label: "#3366cc" }] };

  it("swaps every palette colour for its dark variant, case-insensitively", () => {
    const out = prepareVegaLite(recipe, sets, "dark");
    const layers = out.layer as JsonObject[];
    expect((layers[0]!.mark as JsonObject).color).toBe("#6fa8f0");
    expect((layers[1]!.mark as JsonObject).color).toBe("#e3e7ee");
    expect((layers[2]!.mark as JsonObject).color).toBe("#ff7b72");
    expect(JSON.stringify(layers[3])).toContain('["#97a6b6","#f0a35e"]');
    expect(out.datasets).toEqual(sets); // data is never rewritten
  });

  it("keeps the light palette in light mode and leaves other colours alone", () => {
    const out = prepareVegaLite(recipe, sets, "light");
    expect(((out.layer as JsonObject[])[1]!.mark as JsonObject).color).toBe("#1B2333");
    const custom = prepareVegaLite({ mark: { type: "bar", color: "#123456" } }, {}, "dark");
    expect((custom.mark as JsonObject).color).toBe("#123456");
  });

  it("draws rule, text and tick marks without a colour of their own in the theme's ink", () => {
    // Vega-Lite's own defaults are black: invisible on the dark card (the Guided beliefs
    // preview's 90% line, the forecast's split line and its "Forecast starts" label).
    const spec: JsonObject = {
      data: { name: "contrib" },
      layer: [
        { mark: "bar", encoding: { x: { field: "value", type: "quantitative" }, y: { field: "channel", type: "nominal" } } },
        { mark: "rule", encoding: { x: { field: "value", type: "quantitative" } } },
        { mark: "tick", encoding: { x: { field: "value", type: "quantitative" }, y: { field: "channel", type: "nominal" } } },
        { mark: "text", encoding: { x: { field: "value", type: "quantitative" }, text: { field: "channel" } } },
      ],
    };
    for (const [theme, card] of [["dark", "#1b212d"], ["light", "#ffffff"]] as const) {
      const vg = compile(prepareVegaLite(spec, datasets, theme) as never).spec as { marks: { name: string; encode: { update: Record<string, { value?: string }> } }[] };
      const colour = (layer: number, channel: "stroke" | "fill") => vg.marks.find((m) => m.name === `layer_${layer}_marks`)?.encode.update[channel]?.value ?? "";
      for (const [layer, channel] of [[1, "stroke"], [2, "fill"], [3, "fill"]] as const) expect(colour(layer, channel)).toMatch(/^#[0-9a-f]{6}$/i);
      expect(contrast(colour(1, "stroke"), card), `${theme} rule`).toBeGreaterThanOrEqual(3);
      expect(contrast(colour(3, "fill"), card), `${theme} text`).toBeGreaterThanOrEqual(4.5);
      // the median tick stands apart from the series colour of the bar it sits on
      expect(contrast(colour(2, "fill"), card), `${theme} tick`).toBeGreaterThanOrEqual(3);
      expect(colour(2, "fill")).not.toBe(colour(0, "fill"));
    }
  });

  it("gives every dark variant at least 3:1 contrast on the dark card", () => {
    for (const [light, dark] of Object.entries(DARK_VARIANTS)) {
      if (NON_TEXT_FILLS.has(light)) continue;
      expect(contrast(dark, "#1b212d"), `${light} -> ${dark}`).toBeGreaterThanOrEqual(3);
    }
  });
});

describe("preparePlotly", () => {
  it("requires a data array and merges the layout under the theme", () => {
    expect(() => preparePlotly({ layout: {} }, datasets, "light")).toThrow(SpecError);
    const out = preparePlotly({ data: [], layout: { title: { text: "T" } } }, datasets, "light");
    expect(out.layout.title).toEqual({ text: "T" });
    expect(out.layout.paper_bgcolor).toBe("rgba(0,0,0,0)");
  });

  it("swaps recipe colours in traces and frames in dark mode, never data values", () => {
    const sets = { cells: [{ x: 1, c: "#3366cc" }] };
    const spec: JsonObject = {
      data: [
        { type: "scatter", x: { $ref: "cells.x" }, text: ["#3366cc"], line: { color: "#3366CC" }, fillcolor: "#1b2333" },
        { type: "heatmap", z: { $ref: "cells.x" }, colorscale: [[0, "#440154"], [1, "#fde725"]] },
      ],
      layout: { annotations: [{ text: "a", font: { color: "#5b6577" } }] },
      frames: [{ name: "1", data: [{ type: "scatter", marker: { color: "#1b2333" } }] }],
    };
    const dark = preparePlotly(spec, sets, "dark");
    const t0 = dark.data[0] as JsonObject;
    expect((t0.line as JsonObject).color).toBe("#6fa8f0");
    expect(t0.fillcolor).toBe("#e3e7ee");
    expect(t0.text).toEqual(["#3366cc"]);
    expect(t0.x).toEqual([1]);
    expect((dark.data[1] as JsonObject).colorscale).toEqual([[0, "#440154"], [1, "#fde725"]]);
    expect(JSON.stringify(dark.layout.annotations)).toContain("#9aa3b2");
    expect(JSON.stringify(dark.frames)).toContain("#e3e7ee");
    const light = preparePlotly(spec, sets, "light");
    expect(((light.data[0] as JsonObject).line as JsonObject).color).toBe("#3366CC");
  });
});

describe("deepMerge", () => {
  it("merges objects recursively and replaces arrays", () => {
    expect(deepMerge({ a: { b: 1, c: [1] } }, { a: { c: [2], d: 3 } })).toEqual({ a: { b: 1, c: [2], d: 3 } });
  });
});

describe("polar plots", () => {
  const rose: JsonObject = {
    data: [
      { type: "barpolar", theta: { $ref: "bars.theta" }, r: { $ref: "bars.r" }, width: 90, marker: { color: "#1b2333" } },
      { type: "scatterpolar", mode: "lines", theta: { $ref: "bars.theta" }, r: { $ref: "bars.r" }, line: { color: "#3366cc" } },
      { type: "scatterpolar", subplot: "polar2", theta: [0, 90], r: [1, 2] },
    ],
    layout: {
      polar: { angularaxis: { direction: "clockwise", rotation: 90, tickvals: [0, 90], ticktext: ["0 h", "6 h"] } },
    },
    frames: [{ name: "DJF", data: [{ type: "barpolar", r: { $ref: "bars.r" } }] }],
  };
  const bars = {
    bars: [
      { theta: 45, r: 3 },
      { theta: 135, r: 5 },
    ],
  };

  it("passes the safety check", () => {
    expect(findUnsafeConstructs("plotly", rose)).toEqual([]);
  });

  it("resolves theta and r and themes every polar subplot", () => {
    const out = preparePlotly(rose, bars, "dark");
    const [bar, line] = out.data as JsonObject[];
    expect(bar?.theta).toEqual([45, 135]);
    expect(bar?.r).toEqual([3, 5]);
    expect((bar?.marker as JsonObject).color).toBe(DARK_VARIANTS["#1b2333"]);
    expect((line?.line as JsonObject).color).toBe(DARK_VARIANTS["#3366cc"]);
    const polar = out.layout.polar as Record<string, Record<string, unknown>>;
    expect(polar.bgcolor).toBe("rgba(0,0,0,0)");
    // the spec's own settings win over the theme
    expect(polar.angularaxis).toMatchObject({ direction: "clockwise", rotation: 90, ticktext: ["0 h", "6 h"] });
    expect(polar.angularaxis?.gridcolor).toBe("#2A3140");
    expect(polar.radialaxis?.color).toBe("#9AA3B2");
    expect((out.layout.polar2 as Record<string, unknown>).bgcolor).toBe("rgba(0,0,0,0)");
    expect((out.frames[0] as JsonObject).data).toEqual([{ type: "barpolar", r: [3, 5] }]);
  });

  it("adds no polar subplot to a cartesian plot", () => {
    const out = preparePlotly({ data: [{ type: "scatter", x: [1], y: [2] }], layout: {} }, {}, "light");
    expect(out.layout.polar).toBeUndefined();
    expect(polarSubplots([{ type: "barpolar" }, { type: "scatterpolar", subplot: "polar3" }], {})).toEqual([
      "polar",
      "polar3",
    ]);
  });
});
