import { describe, expect, it } from "vitest";

import {
  exportMessage,
  findUnsafeConstructs,
  isPlainJson,
  isPlotDatasets,
  LIMITS,
  parseParentMessage,
  parseSandboxMessage,
  PROTOCOL,
  renderMessage,
  themeMessage,
  toSelectRow,
} from "./protocol";

const vl = { mark: "line", data: { name: "fan" }, encoding: { x: { field: "t" } } };

describe("parseParentMessage", () => {
  it("accepts a well-formed render message", () => {
    const msg = renderMessage("p1", "vega-lite", vl, { fan: [{ t: 1, y: 2.5 }] }, "dark");
    expect(parseParentMessage(msg)).toEqual(msg);
  });

  it("accepts export and theme messages", () => {
    expect(parseParentMessage(exportMessage("p1", "svg"))).not.toBeNull();
    expect(parseParentMessage(themeMessage("light"))).not.toBeNull();
  });

  it.each([
    ["not an object", "render"],
    ["wrong protocol", { ...renderMessage("p", "vega-lite", vl, {}, "dark"), protocol: "x/v1" }],
    ["unknown type", { protocol: PROTOCOL, type: "navigate", url: "https://evil.test" }],
    ["unknown key", { ...renderMessage("p", "vega-lite", vl, {}, "dark"), extra: 1 }],
    ["bad id", renderMessage("id with spaces", "vega-lite", vl, {}, "dark")],
    ["bad kind", { ...renderMessage("p", "vega-lite", vl, {}, "dark"), kind: "vega" }],
    ["bad theme", { ...renderMessage("p", "vega-lite", vl, {}, "dark"), theme: "sepia" }],
    ["spec is an array", { ...renderMessage("p", "vega-lite", vl, {}, "dark"), spec: [] }],
    ["export format", { protocol: PROTOCOL, type: "export", id: "p", format: "pdf" }],
  ])("rejects %s", (_label, data) => {
    expect(parseParentMessage(data)).toBeNull();
  });

  it("rejects non-JSON values inside the spec", () => {
    const spec = { mark: "line", data: { values: [new Date()] } } as unknown as Record<string, never>;
    expect(parseParentMessage(renderMessage("p", "vega-lite", spec, {}, "dark"))).toBeNull();
    const nan = { mark: "line", width: Number.NaN };
    expect(parseParentMessage(renderMessage("p", "vega-lite", nan, {}, "dark"))).toBeNull();
  });

  it("rejects prototype-polluting keys", () => {
    const spec = JSON.parse('{"mark":"bar","__proto__":{"polluted":true}}') as Record<string, never>;
    expect(parseParentMessage(renderMessage("p", "vega-lite", spec, {}, "dark"))).toBeNull();
  });

  it("rejects datasets with bad names, nested cells or too many rows", () => {
    expect(isPlotDatasets({ "bad name": [] })).toBe(false);
    expect(isPlotDatasets({ ok: [{ a: { nested: 1 } }] })).toBe(false);
    expect(isPlotDatasets({ ok: [{ a: Infinity }] })).toBe(false);
    const big = Array.from({ length: LIMITS.maxRowsTotal + 1 }, () => ({ a: 1 }));
    expect(isPlotDatasets({ ok: big })).toBe(false);
    expect(isPlotDatasets({ ok: [{ a: 1, b: "x", c: null, d: true }] })).toBe(true);
  });

  it("bounds spec depth", () => {
    let deep: unknown = 1;
    for (let i = 0; i < LIMITS.maxSpecDepth + 2; i++) deep = { a: deep };
    expect(isPlainJson(deep)).toBe(false);
  });
});

describe("parseSandboxMessage", () => {
  it("accepts every valid sandbox message", () => {
    const valid = [
      { protocol: PROTOCOL, type: "ready" },
      { protocol: PROTOCOL, type: "rendered", id: "p1" },
      { protocol: PROTOCOL, type: "resize", height: 320 },
      { protocol: PROTOCOL, type: "select", id: "p1", items: [{ x: 1, y: "a" }] },
      {
        protocol: PROTOCOL,
        type: "exported",
        id: "p1",
        format: "png",
        dataUrl: "data:image/png;base64,iVBORw0KGgo=",
      },
      { protocol: PROTOCOL, type: "error", id: null, code: "RENDER_FAILED", message: "boom" },
    ];
    for (const m of valid) expect(parseSandboxMessage(m)).toEqual(m);
  });

  it.each([
    ["negative height", { protocol: PROTOCOL, type: "resize", height: -1 }],
    ["huge height", { protocol: PROTOCOL, type: "resize", height: 1e9 }],
    ["string height", { protocol: PROTOCOL, type: "resize", height: "300" }],
    [
      "javascript export url",
      { protocol: PROTOCOL, type: "exported", id: "p", format: "png", dataUrl: "javascript:alert(1)" },
    ],
    [
      "html export url",
      { protocol: PROTOCOL, type: "exported", id: "p", format: "svg", dataUrl: "data:text/html;base64,PGI+" },
    ],
    [
      "format mismatch",
      { protocol: PROTOCOL, type: "exported", id: "p", format: "svg", dataUrl: "data:image/png;base64,AA==" },
    ],
    ["unknown error code", { protocol: PROTOCOL, type: "error", id: null, code: "PWNED", message: "" }],
    [
      "nested selection",
      { protocol: PROTOCOL, type: "select", id: "p", items: [{ x: { y: 1 } }] },
    ],
    ["navigation request", { protocol: PROTOCOL, type: "navigate", url: "https://evil.test" }],
    ["extra key", { protocol: PROTOCOL, type: "ready", url: "x" }],
  ])("rejects %s", (_label, data) => {
    expect(parseSandboxMessage(data)).toBeNull();
  });

  it("caps selection size", () => {
    const items = Array.from({ length: LIMITS.maxSelectItems + 1 }, (_, i) => ({ i }));
    expect(parseSandboxMessage({ protocol: PROTOCOL, type: "select", id: "p", items })).toBeNull();
  });
});

describe("toSelectRow", () => {
  it("flattens a datum to scalars and drops nested, function and symbol values", () => {
    const row = toSelectRow({
      a: 1,
      b: "text",
      c: { nested: true },
      d: () => 1,
      e: new Date("2026-01-01T00:00:00Z"),
      f: Number.POSITIVE_INFINITY,
    });
    expect(row).toEqual({ a: 1, b: "text", e: "2026-01-01T00:00:00.000Z" });
  });
});

describe("findUnsafeConstructs", () => {
  it("passes a named-data Vega-Lite spec", () => {
    expect(findUnsafeConstructs("vega-lite", vl)).toEqual([]);
  });

  it("flags URL data, href encodings and image marks in Vega-Lite", () => {
    const spec = {
      layer: [
        { data: { url: "https://evil.test/x.csv" }, mark: "point" },
        { mark: { type: "image" }, encoding: { url: { field: "u" } } },
        { mark: "point", encoding: { href: { field: "link" } } },
      ],
    };
    const issues = findUnsafeConstructs("vega-lite", spec);
    expect(issues).toContain("/spec/layer/0/data/url");
    expect(issues).toContain("/spec/layer/1/mark");
    expect(issues).toContain("/spec/layer/2/encoding/href");
  });

  it("flags tile maps, layout images and markup in Plotly", () => {
    const spec = {
      data: [
        { type: "scattermapbox", lat: [1], lon: [2] },
        { type: "scatter", x: [1], y: [2], text: ['<a href="https://evil.test">x</a>'] },
        { type: "bar", x: [1], y: [2], hovertemplate: "<b>%{y}</b><br>ok" },
        // <extra> hides the secondary hover box (the server's sanitizer keeps it too)
        { type: "heatmap", z: [[1]], hovertemplate: "z %{z}<extra></extra>", text: ["<extra>no</extra>"] },
        { type: "scatter", x: [1], y: [2], hovertemplate: ["<img src=x>", "%{y}<extra></extra>"] },
      ],
      layout: { images: [{ source: "https://evil.test/logo.png" }] },
    };
    const issues = findUnsafeConstructs("plotly", spec);
    expect(issues).toContain("/spec/data/0/type");
    expect(issues).toContain("/spec/data/1/text/0");
    expect(issues).toContain("/spec/layout/images");
    expect(issues.some((p) => p.startsWith("/spec/data/2"))).toBe(false);
    expect(issues).toEqual(expect.not.arrayContaining(["/spec/data/3/hovertemplate"]));
    expect(issues).toContain("/spec/data/3/text/0"); // only hover templates may use <extra>
    expect(issues).toContain("/spec/data/4/hovertemplate/0");
    expect(issues).not.toContain("/spec/data/4/hovertemplate/1");
  });
});
