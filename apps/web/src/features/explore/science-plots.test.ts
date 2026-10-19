/**
 * The plot builders of the sections for scientific data, on the example views the spec
 * freezes (`packages/spec/tests/data/explorer_science`) and on oversized inputs: every spec
 * passes the plot sandbox's own safety check, every dataset fits the sandbox's row budget,
 * data reaches the specs only as named datasets, and colours from data are "#rrggbb" only.
 */
import { findUnsafeConstructs, type JsonValue } from "@mapi/plot-sandbox/protocol";
import { describe, expect, it } from "vitest";

import { clone, scienceExample } from "./science-examples.test-helper";
import {
  binCentresDeg,
  circularFrame,
  circularPairPlot,
  classBreaks,
  classOf,
  countsHistogramPlot,
  dendrogramPlot,
  frameStatsPlot,
  mapPlot,
  MAX_SANDBOX_ROWS,
  modeTerm,
  networkMatrixPlot,
  nodeLinkPlot,
  plotlyText,
  pointsColours,
  pointsDensityPlot,
  pointsPlot,
  profilesPlot,
  rosePlot,
  structureMatrixPlot,
  textOf,
  thinIndex,
  tipOrder,
  type SciPlot,
} from "./science-plots";
import { safeHex, type NetworkEntry, type ProfileEntry, type TreeView } from "./views";

/** The plot is safe for the sandbox and within its budget. */
function checked(plot: SciPlot | null): SciPlot {
  expect(plot).not.toBeNull();
  const p = plot!;
  expect(findUnsafeConstructs(p.kind, p.spec)).toEqual([]);
  for (const rows of Object.values(p.datasets)) expect(rows.length).toBeLessThanOrEqual(MAX_SANDBOX_ROWS);
  // no URLs and no expressions built from data
  const text = JSON.stringify(p.spec);
  expect(text).not.toMatch(/"url"|"expr"|"signal"|javascript:/);
  return p;
}

/** Every `$ref` of a Plotly spec names a dataset column that exists. */
function refsResolve(p: SciPlot): void {
  const walk = (node: JsonValue): void => {
    if (Array.isArray(node)) return node.forEach(walk);
    if (node && typeof node === "object") {
      if (typeof node.$ref === "string") {
        const [name, col] = node.$ref.split(".");
        expect(p.datasets[name!], node.$ref).toBeDefined();
        if (p.datasets[name!]!.length) expect(Object.keys(p.datasets[name!]![0]!)).toContain(col);
        return;
      }
      Object.values(node).forEach(walk);
    }
  };
  walk(p.spec);
}

const EVIL = '<img src=x onerror="alert(1)"> & <a href="javascript:alert(2)">x</a>';

describe("helpers", () => {
  it("thins evenly and keeps both ends", () => {
    expect(thinIndex(5, 10)).toEqual([0, 1, 2, 3, 4]);
    const t = thinIndex(10_000, 7);
    expect(t).toHaveLength(7);
    expect(t[0]).toBe(0);
    expect(t.at(-1)).toBe(9999);
  });

  it("makes Plotly text inert and validates colours", () => {
    expect(plotlyText(EVIL)).not.toMatch(/[<>&]|javascript:/i);
    expect(plotlyText(null)).toBe("");
    expect(textOf({ a: 1 })).toBe('{"a":1}');
    expect(safeHex("#A0b1C2")).toBe("#a0b1c2");
    for (const bad of ["#abc", "red", "#a0b1c2;background:url(x)", "url(#x)", 12, null]) expect(safeHex(bad)).toBeNull();
  });

  it("classes values by quantiles", () => {
    const b = classBreaks([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5);
    expect(b[0]).toBe(1);
    expect(b.at(-1)).toBe(10);
    expect(classOf(1, b)).toBe(0);
    expect(classOf(10, b)).toBe(b.length - 2);
    expect(classOf(null, b)).toBe(-1);
    expect(classBreaks([null, NaN])).toEqual([]);
  });

  it("places circular bins from their edges, compass or mathematics", () => {
    expect(binCentresDeg([0, 90, 180, 270, 360], 360)).toEqual([45, 135, 225, 315]);
    expect(binCentresDeg([0, 6, 12, 18, 24], 24)).toEqual([45, 135, 225, 315]);
    expect(circularFrame({ zero: "north", clockwise: true })).toEqual({ rotation: 90, direction: "clockwise" });
    expect(circularFrame({ zero: "east", clockwise: false })).toEqual({ rotation: 0, direction: "counterclockwise" });
    expect(circularFrame({})).toEqual({ rotation: 90, direction: "clockwise" });
  });
});

describe("structure", () => {
  it("draws the variables x dimensions matrix with the role of each dim", () => {
    const view = scienceExample("structure");
    const p = checked(structureMatrixPlot(view));
    expect(p.kind).toBe("vega-lite");
    const rows = p.datasets.cells!;
    expect(rows.length).toBe((view.vars ?? []).reduce((n, v) => n + (v.dims ?? []).length, 0));
    expect(rows.find((r) => r.dim === "row")).toMatchObject({ role: "y", var: "quakes", size: 9 });
    expect(structureMatrixPlot({ ...view, vars: [] })).toBeNull();
  });
});

describe("images", () => {
  it("draws the per-frame statistics and the histogram, with log counts when asked", () => {
    const e = scienceExample("images").fields[0]!;
    const fs = checked(frameStatsPlot(e.frame_stats!, { frameDim: "quarter", title: "quakes" }));
    expect(fs.datasets.frames).toHaveLength(2);
    expect(fs.datasets.frames![1]).toMatchObject({ x: "2009-04-01", mean: 3, min: 1, max: 7 });
    const h = checked(countsHistogramPlot(e.histogram!, { title: "quakes", logCount: true }));
    expect(JSON.stringify(h.spec)).toContain("symlog");
    // 5000 frames (the cap) fit the sandbox
    const n = 5000;
    const big = { coords: Array.from({ length: n }, (_, i) => i), mean: Array.from({ length: n }, () => 1), sd: [], min: [], max: [], missing_share: [] };
    expect(checked(frameStatsPlot(big, { frameDim: "frame", title: "x" })).datasets.frames).toHaveLength(n);
  });
});

describe("profiles", () => {
  it("draws one curve per frame, or one frame, and thins long curves to the budget", () => {
    const v = scienceExample("profiles");
    const all = checked(profilesPlot(v.items[0]!));
    expect(new Set(all.datasets.curves!.map((r) => r.frame))).toEqual(new Set(["0", "12"]));
    const one = checked(profilesPlot(v.items[0]!, 1));
    expect(new Set(one.datasets.curves!.map((r) => r.frame))).toEqual(new Set(["12"]));
    // log x: the axis is a log scale and values <= 0 are left out
    const logx = checked(profilesPlot({ ...v.items[1]!, coords: [0, 3, 4, 5, 6] }));
    expect(JSON.stringify(logx.spec)).toContain('"type":"log"');
    expect(logx.datasets.curves!.every((r) => (r.x as number) > 0)).toBe(true);
    // the caps: 60 frames of 5000 points
    const big: ProfileEntry = {
      var: "x",
      along: "k",
      coords: Array.from({ length: 5000 }, (_, i) => i + 1),
      frames: Array.from({ length: 60 }, (_, f) => ({ label: f, values: Array.from({ length: 5000 }, (_, i) => Math.sin(i / 50) + f) })),
    };
    expect(checked(profilesPlot(big)).datasets.curves!.length).toBeLessThanOrEqual(MAX_SANDBOX_ROWS);
  });
});

describe("map", () => {
  it("fills the rings of each colour class (Plotly), with neighbour lines and safe hover text", () => {
    const v = scienceExample("map");
    const p = checked(mapPlot(v, 0, null, { edges: true }));
    expect(p.kind).toBe("plotly");
    refsResolve(p);
    const traces = p.spec.data as { type: string; fill?: string; name?: string }[];
    expect(traces.filter((t) => t.fill === "toself").length).toBeGreaterThanOrEqual(2);
    expect(traces.some((t) => t.name === "Neighbours")).toBe(true);
    // rings are separated by an empty row
    const rings = Object.entries(p.datasets).filter(([k]) => k.startsWith("class"));
    expect(rings.every(([, rows]) => rows.at(-1)!.x === null)).toBe(true);
    // no Plotly key that loads tiles
    expect(JSON.stringify(p.spec)).not.toMatch(/"map"|mapbox/);
  });

  it("keeps markup from area names and units out of Plotly", () => {
    const v = clone(scienceExample("map"));
    v.areas[0] = EVIL;
    v.layers![0]!.units = EVIL;
    v.layers![0]!.var = EVIL;
    const p = checked(mapPlot(v, 0));
    const hover = p.datasets.centroids!.map((r) => String(r.text));
    expect(hover.join(" ")).not.toMatch(/[<>]/);
  });

  it("draws a layer's frame, and areas without a value in grey", () => {
    const v = clone(scienceExample("map"));
    v.layers![0]!.frames = [{ label: 2001, values: [1, null, 3] }];
    const p = checked(mapPlot(v, 0, 0));
    expect((p.spec.data as { name?: string }[]).some((t) => t.name === "No value")).toBe(true);
    expect(mapPlot(v, 9)).toBeNull();
  });
});

describe("network", () => {
  it("draws the matrix in seriation order and the node-link drawing", () => {
    const e = scienceExample("network").items[0]!;
    const m = checked(networkMatrixPlot(e));
    // the 4 ties of the example, both ways, no zeros
    expect(m.datasets.cells).toHaveLength(8);
    const links = checked(nodeLinkPlot(e, "office"));
    expect(links.datasets.nodes).toHaveLength(4);
    expect(links.datasets.edges).toHaveLength(4);
    expect(links.datasets.nodes![2]).toMatchObject({ label: "3", degree: 3, attr: 2 });
    expect(networkMatrixPlot({ ...e, matrix: null })).toBeNull();
  });

  it("colours signed ties and needs positions", () => {
    const e = clone(scienceExample("network").items[0]!);
    e.signed = true;
    e.edges![0]!.w = -0.5;
    const p = checked(nodeLinkPlot(e));
    expect(p.datasets.edges![0]).toMatchObject({ sign: "negative" });
    expect(nodeLinkPlot({ ...e, positions: null })).toBeNull();
    // 300 nodes and 3000 edges (the caps)
    const n = 300;
    const big: NetworkEntry = {
      var: "w",
      node_dim: "n",
      n_nodes: n,
      labels: Array.from({ length: n }, (_, i) => i),
      positions: { x: Array.from({ length: n }, (_, i) => Math.cos(i)), y: Array.from({ length: n }, (_, i) => Math.sin(i)) },
      edges: Array.from({ length: 3000 }, (_, k) => ({ i: k % n, j: (k * 7) % n, w: 1 })),
      degree: Array.from({ length: n }, () => 10),
    };
    expect(checked(nodeLinkPlot(big)).datasets.edges).toHaveLength(3000);
  });
});

describe("tree", () => {
  it("draws the branches and connectors and the traits of the tips in order", () => {
    const v = scienceExample("tree");
    expect(tipOrder(v)).toEqual([0, 1, 2]);
    const p = checked(dendrogramPlot(v, ["brain", "body"]));
    expect(p.datasets.branches).toHaveLength(4);
    expect(p.datasets.connectors).toHaveLength(4);
    expect(p.datasets.trait_0).toHaveLength(3);
    // the missing body value is left out
    expect(p.datasets.trait_1).toHaveLength(2);
    expect(p.datasets.trait_0![0]).toMatchObject({ label: "Homo_sapiens", value: 1350 });
    expect(p.spec.hconcat).toHaveLength(3);
    // without `tips`: the tips in layout order
    const noTips = { ...v, tips: [] } as TreeView;
    expect(tipOrder(noTips)).toEqual([0, 1, 2]);
  });

  it("fits 5000 nodes", () => {
    const n = 5000;
    const big: TreeView = {
      node_dim: "node",
      n_nodes: n,
      n_tips: (n + 1) / 2,
      layout: {
        x: Array.from({ length: n }, (_, i) => Math.floor(Math.log2(i + 1))),
        y: Array.from({ length: n }, (_, i) => i),
        parent: Array.from({ length: n }, (_, i) => (i === 0 ? -1 : Math.floor((i - 1) / 2))),
        is_tip: Array.from({ length: n }, (_, i) => 2 * i + 1 >= n),
        label: Array.from({ length: n }, (_, i) => `n${i}`),
      },
      traits: [],
    };
    const p = checked(dendrogramPlot(big));
    expect(p.datasets.branches).toHaveLength(n - 1);
  });
});

describe("circular", () => {
  it("draws one polar subplot per group with the mean direction", () => {
    const v = scienceExample("circular");
    const wind = checked(rosePlot(v.items[0]!));
    expect(wind.kind).toBe("plotly");
    refsResolve(wind);
    const layout = wind.spec.layout as Record<string, { angularaxis?: { ticktext?: string[]; rotation?: number; direction?: string } }>;
    expect(layout.polar?.angularaxis).toMatchObject({ rotation: 90, direction: "clockwise" });
    expect(layout.polar?.angularaxis?.ticktext?.[0]).toBe("N");
    const traces = wind.spec.data as { type: string; theta?: unknown }[];
    expect(traces.map((t) => t.type)).toEqual(["barpolar", "scatterpolar"]);
    // mean direction 182° (in degrees)
    expect(traces[1]!.theta).toEqual([182, 182]);
    const clock = checked(rosePlot(v.items[1]!));
    const cl = clock.spec.layout as Record<string, unknown>;
    expect(Object.keys(cl).filter((k) => k.startsWith("polar"))).toEqual(["polar", "polar2"]);
    // 12:00 is half a turn
    expect((clock.spec.data as { theta?: unknown }[]).filter((t) => Array.isArray(t.theta)).map((t) => t.theta)).toContainEqual([180, 180]);
  });

  it("draws a pair of circular variables as counts per pair of bins", () => {
    const v = scienceExample("circular");
    const p = checked(circularPairPlot(v.pairs![0]!));
    expect(p.datasets.cells).toHaveLength(4);
    expect(p.datasets.cells![0]).toMatchObject({ y: "0–12", x: "0–180", value: 30 });
  });

  it("keeps group labels inert", () => {
    const v = clone(scienceExample("circular"));
    v.items[1]!.groups[0]!.label = EVIL;
    checked(rosePlot(v.items[1]!));
  });
});

describe("points", () => {
  it("offers the swatch colour, the answers and the numeric variables, and draws each", () => {
    const v = scienceExample("points");
    const colours = pointsColours(v);
    expect(colours.map((c) => c.kind)).toEqual(["srgb", "mode", "agreement", "var"]);
    const swatch = checked(pointsPlot(v, { kind: "srgb" }));
    expect(swatch.datasets.points!.map((r) => r.hex)).toEqual(["#f5f5f5", "#a83a3a", "#3a7ca8"]);
    expect(JSON.stringify(swatch.spec)).toContain('"scale":null');
    expect(modeTerm(v, 0)).toBe("W");
    expect(checked(pointsPlot(v, { kind: "mode" })).datasets.points![1]).toMatchObject({ mode: "F", agreement: 0.6 });
    expect(checked(pointsPlot(v, { kind: "var", var: "cie_l" })).datasets.points![2]).toMatchObject({ value: 61.7 });
  });

  it("drops colours that are not #rrggbb", () => {
    const v = clone(scienceExample("points"));
    v.srgb = ["#f5f5f5", "red", "#3a7ca8;url(x)"];
    const p = checked(pointsPlot(v, { kind: "srgb" }));
    expect(p.datasets.points!.map((r) => r.hex)).toEqual(["#f5f5f5"]);
  });

  it("draws the density of all the points", () => {
    const v = clone(scienceExample("points"));
    expect(pointsDensityPlot(v)).toBeNull();
    v.density = { x_edges: [0, 1, 2], y_edges: [0, 1], counts: [[3, 0]] };
    expect(checked(pointsDensityPlot(v)).datasets.density).toHaveLength(1);
  });
});
