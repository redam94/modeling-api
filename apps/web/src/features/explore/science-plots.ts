/**
 * First-party plots of the Data Explorer's sections for scientific data (v0.10), built from the
 * views for what the user picked (a layer, a frame, a colour). Pure functions (see
 * `science-plots.test.ts`). Like every plot they render only inside the plot sandbox: data
 * travels as named datasets (Vega-Lite `data.name`, Plotly `{"$ref": "dataset.column"}`), never
 * as URLs or expressions built from data. Every dataset has at most {@link MAX_SANDBOX_ROWS}
 * rows. Strings from data that Plotly draws as text are made inert ({@link plotlyText}), and
 * colours from data are used only as "#rrggbb" ({@link safeHex}).
 *
 * Engines (design §5.2): Vega-Lite for most charts; Plotly for the map (Vega-Lite cannot fill
 * polygons from flat rows) and the rose diagrams (polar bars and the mean-direction line).
 */
import type { JsonObject, PlotDatasets, PlotKind, Row } from "@mapi/plot-sandbox/protocol";

import type { BuiltPlot } from "@/features/plots/builtin-specs";

import { colorAt, type ColormapName } from "./fields";
import { heatmapPlot, histogramPlot, xType } from "./plots";
import {
  safeHex,
  type CircularEntry,
  type CircularPair,
  type FrameStats,
  type Histogram,
  type MapView,
  type NetworkEntry,
  type PointsView,
  type ProfileEntry,
  type StructureView,
  type TreeView,
} from "./views";

/** The most rows of one dataset (the sandbox's row budget per dataset). */
export const MAX_SANDBOX_ROWS = 20_000;

/** A plot of either engine for the sandbox. */
export interface SciPlot {
  kind: PlotKind;
  spec: JsonObject;
  datasets: PlotDatasets;
}

type Scalar = string | number | boolean | null;

const vl = (p: BuiltPlot): SciPlot => ({ kind: "vega-lite", spec: p.spec, datasets: p.datasets });

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** A value from a view as text (objects as JSON). */
export function textOf(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean" || typeof v === "bigint") return String(v);
  return JSON.stringify(v) ?? "";
}

function scalar(v: unknown): Scalar {
  return v === null || typeof v === "string" || typeof v === "boolean" || finite(v) ? v : v === undefined ? null : textOf(v);
}

/** Text from data that Plotly draws: markup characters are replaced, so it is never HTML. */
export function plotlyText(v: unknown): string {
  return textOf(v).replace(/</g, "‹").replace(/>/g, "›").replace(/&/g, "+").replace(/javascript\s*:/gi, "javascript ").slice(0, 200);
}

/** Every `step`-th index of `n` so that at most `max` stay (the first and the last kept). */
export function thinIndex(n: number, max: number): number[] {
  if (n <= max) return Array.from({ length: n }, (_, i) => i);
  if (max <= 1) return [0];
  const out = new Set<number>();
  for (let k = 0; k < max; k++) out.add(Math.round((k * (n - 1)) / (max - 1)));
  return [...out];
}

function hexOf(name: ColormapName, t: number): string {
  const [r, g, b] = colorAt(name, t);
  return `#${[r, g, b].map((c) => c.toString(16).padStart(2, "0")).join("")}`;
}

// ------------------------------------------------------------------------------ structure

const ROLE_ORDER = ["time", "sequence", "frame", "y", "x", "component", "channel", "area", "vertex", "node", "pair", "item", "columns", "tip", "along", "category", "index", "other"];

/** The variables × dimensions matrix: one cell per (var, dim), coloured by the dim's role, the size as text. */
export function structureMatrixPlot(view: StructureView): SciPlot | null {
  const dims = view.dims ?? [];
  const vars = view.vars ?? [];
  const roles = new Map(dims.map((d) => [d.name, d]));
  const rows: Row[] = [];
  for (const v of vars) {
    (v.dims ?? []).forEach((dim, i) => {
      const d = roles.get(dim);
      rows.push({ var: v.name, dim, role: d?.role_in_data ?? "other", size: v.shape?.[i] ?? d?.size ?? null, used_as: v.used_as ?? "data" });
    });
  }
  if (!rows.length) return null;
  const capped = rows.slice(0, MAX_SANDBOX_ROWS);
  const x = { field: "dim", type: "nominal", title: "Dimension", sort: dims.map((d) => d.name), axis: { labelAngle: 0, labelOverlap: true } };
  const y = { field: "var", type: "nominal", title: "Variable", sort: vars.map((v) => v.name) };
  return {
    kind: "vega-lite",
    datasets: { cells: capped },
    spec: {
      data: { name: "cells" },
      height: { step: 22 },
      layer: [
        {
          mark: { type: "rect", stroke: "white", strokeWidth: 1 },
          encoding: {
            x,
            y,
            color: { field: "role", type: "nominal", title: "Role of the dimension", sort: ROLE_ORDER },
            tooltip: [
              { field: "var", title: "Variable" },
              { field: "dim", title: "Dimension" },
              { field: "role", title: "Role" },
              { field: "size", title: "Size" },
              { field: "used_as", title: "Used as" },
            ],
          },
        },
        { mark: { type: "text", fontSize: 10 }, encoding: { x, y, text: { field: "size", type: "quantitative", format: "," } } },
      ],
    },
  };
}

// ------------------------------------------------------------------------------ images

/** The mean of every frame with the band from the minimum to the maximum. */
export function frameStatsPlot(fs: FrameStats, opts: { frameDim: string; title: string }): SciPlot | null {
  const idx = thinIndex(fs.coords.length, MAX_SANDBOX_ROWS);
  const rows: Row[] = idx.map((i) => ({ x: scalar(fs.coords[i]), mean: fs.mean[i] ?? null, min: fs.min?.[i] ?? null, max: fs.max?.[i] ?? null, missing: fs.missing_share?.[i] ?? null }));
  if (rows.length < 2) return null;
  const xt = xType(rows.map((r) => r.x));
  const x = { field: "x", type: xt, title: opts.frameDim };
  const layers: JsonObject[] = [];
  if (fs.min && fs.max) {
    layers.push({ mark: { type: "area", opacity: 0.2 }, encoding: { x, y: { field: "min", type: "quantitative", title: opts.title }, y2: { field: "max" } } });
  }
  layers.push({
    mark: { type: "line", strokeWidth: 1.6 },
    encoding: {
      x,
      y: { field: "mean", type: "quantitative", title: opts.title, scale: { zero: false } },
      tooltip: [
        { field: "x", title: opts.frameDim, ...(xt === "temporal" ? { type: "temporal" } : {}) },
        { field: "mean", title: "Mean", format: ",.4~g" },
        { field: "min", title: "Minimum", format: ",.4~g" },
        { field: "max", title: "Maximum", format: ",.4~g" },
        { field: "missing", title: "Missing", format: ".1%" },
      ],
    },
  });
  return { kind: "vega-lite", datasets: { frames: rows }, spec: { data: { name: "frames" }, height: 200, layer: layers } };
}

/** A histogram of precomputed bins, with a log count axis when the counts are skewed. */
export function countsHistogramPlot(h: Histogram, opts: { title: string; logCount?: boolean }): SciPlot {
  const p = histogramPlot(h, { title: opts.title });
  if (opts.logCount) {
    const enc = (p.spec.encoding ?? {}) as JsonObject;
    enc.y = { field: "count", type: "quantitative", title: "Count (log scale)", scale: { type: "symlog" } };
    p.spec.encoding = enc;
  }
  return vl(p);
}

// ------------------------------------------------------------------------------ profiles

/**
 * The curves of a profile along its axis, one colour per frame (sequential), or only frame
 * `frame`. Log axes leave out the values at or below 0. Thinned along the axis to fit the budget.
 */
export function profilesPlot(entry: ProfileEntry, frame: number | null = null): SciPlot | null {
  const frames = frame === null ? entry.frames : entry.frames.slice(frame, frame + 1);
  if (!frames.length) return null;
  const idx = thinIndex(entry.coords.length, Math.max(2, Math.floor(MAX_SANDBOX_ROWS / frames.length)));
  const rows: Row[] = [];
  frames.forEach((f, k) => {
    for (const i of idx) {
      const x = scalar(entry.coords[i]);
      const y = f.values[i];
      if (!finite(y)) continue;
      if (entry.log_x && !(finite(x) && x > 0)) continue;
      if (entry.log_y && !(y > 0)) continue;
      rows.push({ x, y, frame: String(f.label ?? k), order: k });
    }
  });
  if (!rows.length) return null;
  const xt = xType(rows.map((r) => r.x));
  const title = entry.units ? `${entry.var} (${entry.units})` : entry.var;
  const many = frames.length > 1;
  return {
    kind: "vega-lite",
    datasets: { curves: rows },
    spec: {
      data: { name: "curves" },
      height: 280,
      mark: { type: "line", strokeWidth: 1.5 },
      encoding: {
        x: { field: "x", type: xt, title: entry.along, ...(entry.log_x && xt === "quantitative" ? { scale: { type: "log" } } : {}) },
        y: { field: "y", type: "quantitative", title, scale: entry.log_y ? { type: "log" } : { zero: false } },
        ...(many
          ? {
              color: { field: "frame", type: "ordinal", title: entry.frame_dim ?? "frame", sort: frames.map((f, k) => String(f.label ?? k)), scale: { scheme: "viridis" }, legend: frames.length <= 12 ? { orient: "right" } : null },
              detail: { field: "order", type: "quantitative" },
            }
          : {}),
        tooltip: [
          { field: "frame", title: entry.frame_dim ?? "frame" },
          { field: "x", title: entry.along, ...(xt === "temporal" ? { type: "temporal" } : {}) },
          { field: "y", title, format: ",.4~g" },
        ],
      },
    },
  };
}

// ------------------------------------------------------------------------------ map

/** Class breaks of values by quantiles (at most `k` classes, distinct breaks). */
export function classBreaks(values: readonly (number | null)[], k = 7): number[] {
  const v = values.filter(finite).sort((a, b) => a - b);
  if (!v.length) return [];
  const out: number[] = [v[0]!];
  for (let i = 1; i < k; i++) out.push(v[Math.min(v.length - 1, Math.floor((i * v.length) / k))]!);
  out.push(v[v.length - 1]!);
  return [...new Set(out)];
}

/** The class of a value for `breaks` (`-1`: no value). */
export function classOf(v: number | null | undefined, breaks: readonly number[]): number {
  if (!finite(v) || breaks.length < 2) return breaks.length === 1 && finite(v) ? 0 : -1;
  for (let c = 0; c < breaks.length - 1; c++) if (v <= breaks[c + 1]!) return c;
  return breaks.length - 2;
}

function fmtNum(v: number): string {
  return Math.abs(v) >= 1e4 || (v !== 0 && Math.abs(v) < 1e-2) ? v.toExponential(2) : String(Number(v.toPrecision(3)));
}

/**
 * The choropleth (Plotly): one filled trace per colour class with the rings of its areas
 * (separated by empty rows), the neighbour lines and a hover point at each centroid.
 */
export function mapPlot(view: MapView, layer: number, frame: number | null = null, opts: { edges?: boolean; classes?: number } = {}): SciPlot | null {
  const l = view.layers?.[layer];
  if (!l) return null;
  const values = frame !== null && l.frames?.[frame] ? l.frames[frame].values : l.values;
  const cx = view.centroids?.x ?? [];
  const cy = view.centroids?.y ?? [];
  const breaks = classBreaks(values, opts.classes ?? 7);
  const nClass = Math.max(1, breaks.length - 1);
  const classOfArea = view.areas.map((_, a) => classOf(values[a], breaks));
  const datasets: PlotDatasets = {};
  const traces: JsonObject[] = [];
  const budget = MAX_SANDBOX_ROWS;
  const groups: { c: number; name: string; color: string }[] = [
    ...Array.from({ length: nClass }, (_, c) => ({ c, name: breaks.length > 1 ? `${fmtNum(breaks[c]!)} – ${fmtNum(breaks[c + 1]!)}` : fmtNum(breaks[0] ?? 0), color: hexOf("viridis", nClass > 1 ? c / (nClass - 1) : 0.5) })),
    { c: -1, name: "No value", color: "#bdbdbd" },
  ];
  for (const g of groups) {
    const rows: Row[] = [];
    for (const ring of view.rings ?? []) {
      if (classOfArea[ring.area] !== g.c) continue;
      const n = Math.min(ring.x.length, ring.y.length);
      if (rows.length + n + 1 > budget) break;
      for (let i = 0; i < n; i++) rows.push({ x: finite(ring.x[i]) ? ring.x[i]! : null, y: finite(ring.y[i]) ? ring.y[i]! : null });
      rows.push({ x: null, y: null });
    }
    if (!rows.length) continue;
    const name = `class${g.c < 0 ? "_none" : g.c}`;
    datasets[name] = rows;
    traces.push({
      type: "scatter",
      mode: "lines",
      x: { $ref: `${name}.x` },
      y: { $ref: `${name}.y` },
      fill: "toself",
      fillcolor: g.color,
      line: { color: "rgba(60,60,60,0.55)", width: 0.6 },
      name: g.name,
      hoverinfo: "skip",
      connectgaps: false,
    });
  }
  if (!traces.length) return null;
  if (opts.edges && view.edges?.length) {
    const rows: Row[] = [];
    for (const [i, j] of view.edges) {
      if (rows.length + 3 > budget) break;
      if (i === undefined || j === undefined) continue;
      const xi = cx[i];
      const yi = cy[i];
      const xj = cx[j];
      const yj = cy[j];
      if (![xi, yi, xj, yj].every(finite)) continue;
      rows.push({ x: xi!, y: yi! }, { x: xj!, y: yj! }, { x: null, y: null });
    }
    if (rows.length) {
      datasets.edges = rows;
      traces.push({ type: "scatter", mode: "lines", x: { $ref: "edges.x" }, y: { $ref: "edges.y" }, line: { color: "rgba(220,60,40,0.75)", width: 1 }, name: "Neighbours", hoverinfo: "skip" });
    }
  }
  const units = l.units ? ` ${plotlyText(l.units)}` : "";
  const hover: Row[] = view.areas.slice(0, budget).map((a, i) => ({
    x: finite(cx[i]) ? cx[i] : null,
    y: finite(cy[i]) ? cy[i] : null,
    text: `${plotlyText(a)}: ${finite(values[i]) ? fmtNum(values[i]) : "no value"}${units}`,
  }));
  datasets.centroids = hover;
  traces.push({ type: "scatter", mode: "markers", x: { $ref: "centroids.x" }, y: { $ref: "centroids.y" }, text: { $ref: "centroids.text" }, hoverinfo: "text", marker: { size: 7, opacity: 0 }, showlegend: false, name: "Areas" });
  return {
    kind: "plotly",
    datasets,
    spec: {
      data: traces,
      layout: {
        height: 520,
        margin: { l: 10, r: 10, t: 10, b: 10 },
        xaxis: { visible: false },
        yaxis: { visible: false, scaleanchor: "x", scaleratio: 1 },
        legend: { title: { text: plotlyText(l.var) }, orientation: "v" },
        hovermode: "closest",
      },
    },
  };
}

// ------------------------------------------------------------------------------ network

const label = (v: unknown, i: number) => (v === null || v === undefined ? `#${i}` : textOf(v));

/** The adjacency matrix as a heatmap in seriation order (connected nodes come together). */
export function networkMatrixPlot(entry: NetworkEntry): SciPlot | null {
  const m = entry.matrix;
  if (!m?.length) return null;
  const order = entry.order?.length === m.length ? entry.order : m.map((_, i) => i);
  const names = order.map((o, k) => label(entry.labels[o], o) + (entry.labels.filter((l) => String(l) === String(entry.labels[o])).length > 1 ? ` (${k})` : ""));
  const cells: { x: string; y: string; value: number | null }[] = [];
  m.forEach((row, i) =>
    row.forEach((v, j) => {
      if (cells.length < MAX_SANDBOX_ROWS && finite(v) && v !== 0) cells.push({ x: names[j]!, y: names[i]!, value: v });
    }),
  );
  const max = Math.max(1e-12, ...cells.map((c) => Math.abs(c.value ?? 0)));
  const p = heatmapPlot(cells, {
    legend: entry.binary ? "Tie" : "Weight",
    xOrder: names,
    yOrder: names,
    ...(entry.signed ? { scheme: "redblue", domain: [-max, max] as [number, number] } : { scheme: "blues" }),
    format: ",.3~g",
  });
  // a square matrix with small cells, no labels when there are many nodes
  const many = names.length > 40;
  const layers = (p.spec.layer as JsonObject[]).map((l) => {
    const enc = l.encoding as JsonObject;
    return { ...l, encoding: { ...enc, x: { ...(enc.x as JsonObject), title: entry.node_dim, axis: many ? null : { labelAngle: -90 } }, y: { ...(enc.y as JsonObject), title: entry.node_dim, axis: many ? null : {} } } };
  });
  return { kind: "vega-lite", datasets: p.datasets, spec: { ...p.spec, layer: layers, height: { step: many ? 4 : 12 }, width: { step: many ? 4 : 12 } } };
}

/** Node-link drawing with the runner's layout: edges as rules, nodes sized by degree, coloured by an attribute. */
export function nodeLinkPlot(entry: NetworkEntry, attr: string | null = null): SciPlot | null {
  const xs = entry.positions?.x ?? [];
  const ys = entry.positions?.y ?? [];
  const attrValues = attr ? entry.node_attrs?.[attr] : undefined;
  const nodes: Row[] = [];
  entry.labels.forEach((l, i) => {
    if (!finite(xs[i]) || !finite(ys[i]) || nodes.length >= MAX_SANDBOX_ROWS) return;
    nodes.push({ x: xs[i], y: ys[i], label: label(l, i), degree: entry.degree?.[i] ?? null, attr: attrValues ? scalar(attrValues[i]) : null });
  });
  if (!nodes.length) return null;
  const edges: Row[] = [];
  for (const e of entry.edges ?? []) {
    if (edges.length >= MAX_SANDBOX_ROWS) break;
    if (!finite(xs[e.i]) || !finite(ys[e.i]) || !finite(xs[e.j]) || !finite(ys[e.j])) continue;
    edges.push({ x: xs[e.i]!, y: ys[e.i]!, x2: xs[e.j]!, y2: ys[e.j]!, w: finite(e.w) ? e.w : 1, sign: finite(e.w) && e.w < 0 ? "negative" : "positive" });
  }
  const numericAttr = attrValues ? attrValues.every((v) => v === null || finite(v)) : false;
  const axis = { field: "x", type: "quantitative", axis: null, scale: { zero: false, nice: false } };
  const yaxis = { field: "y", type: "quantitative", axis: null, scale: { zero: false, nice: false } };
  const layers: JsonObject[] = [];
  if (edges.length) {
    layers.push({
      data: { name: "edges" },
      mark: { type: "rule", strokeWidth: 0.8 },
      encoding: {
        x: axis,
        y: yaxis,
        x2: { field: "x2" },
        y2: { field: "y2" },
        opacity: entry.weighted ? { field: "w", type: "quantitative", scale: { type: "symlog", range: [0.08, 0.7] }, legend: null } : { value: 0.35 },
        ...(entry.signed ? { color: { field: "sign", type: "nominal", title: "Sign", scale: { domain: ["positive", "negative"], range: ["#4a6fa5", "#c0392b"] } } } : { color: { value: "#8a8f98" } }),
      },
    });
  }
  layers.push({
    data: { name: "nodes" },
    mark: { type: "circle", stroke: "white", strokeWidth: 0.6, opacity: 0.95 },
    encoding: {
      x: axis,
      y: yaxis,
      size: { field: "degree", type: "quantitative", title: entry.weighted ? "Strength" : "Degree", scale: { range: [20, 240] } },
      ...(attr ? { color: { field: "attr", type: numericAttr ? "quantitative" : "nominal", title: attr } } : {}),
      tooltip: [
        { field: "label", title: entry.node_dim },
        { field: "degree", title: entry.weighted ? "Strength" : "Degree", format: ",.4~g" },
        ...(attr ? [{ field: "attr", title: attr }] : []),
      ],
    },
  });
  return { kind: "vega-lite", datasets: { nodes, ...(edges.length ? { edges } : {}) }, spec: { height: 440, layer: layers } };
}

// ------------------------------------------------------------------------------ tree

/**
 * The tip nodes in the order of the trait values: `tips` of the view, else the nodes marked
 * as tips in layout order (increasing y).
 */
export function tipOrder(view: Pick<TreeView, "layout" | "tips">): number[] {
  if (view.tips?.length) return view.tips;
  const { y, is_tip } = view.layout;
  return is_tip.map((t, i) => (t ? i : -1)).filter((i) => i >= 0).sort((a, b) => (y[a] ?? 0) - (y[b] ?? 0));
}

/**
 * The dendrogram (rectangular: a horizontal branch per node, a vertical connector at its
 * parent) and, sharing the y axis, a strip per trait at the tips.
 */
export function dendrogramPlot(view: TreeView, traits: readonly string[] = []): SciPlot | null {
  const { x, y, parent, label: labels } = view.layout;
  const branches: Row[] = [];
  const connectors: Row[] = [];
  parent.forEach((p, k) => {
    if (p < 0 || p >= parent.length || p === k) return;
    if (!finite(x[k]) || !finite(y[k]) || !finite(x[p]) || !finite(y[p])) return;
    if (branches.length < MAX_SANDBOX_ROWS) branches.push({ x: x[p], x2: x[k], y: y[k], label: label(labels[k], k) });
    if (connectors.length < MAX_SANDBOX_ROWS) connectors.push({ x: x[p], y: y[p], y2: y[k] });
  });
  if (!branches.length) return null;
  const tips = tipOrder(view);
  const tipRows: Row[] = tips.filter((k) => finite(x[k]) && finite(y[k])).slice(0, MAX_SANDBOX_ROWS).map((k) => ({ x: x[k]!, y: y[k]!, label: label(labels[k], k) }));
  const yEnc = { field: "y", type: "quantitative", axis: null, scale: { zero: false, nice: false, reverse: true } };
  const showLabels = tipRows.length <= 80;
  const height = Math.max(240, Math.min(900, tipRows.length * (showLabels ? 12 : 2)));
  const tree: JsonObject = {
    width: 360,
    height,
    layer: [
      { data: { name: "connectors" }, mark: { type: "rule", strokeWidth: 1, color: "#8a8f98" }, encoding: { x: { field: "x", type: "quantitative", title: "Distance from the root" }, y: yEnc, y2: { field: "y2" } } },
      {
        data: { name: "branches" },
        mark: { type: "rule", strokeWidth: 1.2 },
        encoding: { x: { field: "x", type: "quantitative", title: "Distance from the root" }, x2: { field: "x2" }, y: yEnc, tooltip: [{ field: "label", title: view.node_dim }] },
      },
      ...(showLabels
        ? [{ data: { name: "tips" }, mark: { type: "text", align: "left", dx: 4, fontSize: 9 }, encoding: { x: { field: "x", type: "quantitative" }, y: yEnc, text: { field: "label", type: "nominal" } } }]
        : []),
    ],
  };
  const datasets: PlotDatasets = { branches, connectors, tips: tipRows };
  const strips: JsonObject[] = [];
  for (const name of traits.slice(0, 4)) {
    const t = (view.traits ?? []).find((tr) => tr.var === name);
    if (!t) continue;
    const rows: Row[] = [];
    tips.forEach((k, i) => {
      const v = t.values[i];
      if (finite(v) && finite(y[k])) rows.push({ y: y[k], value: v, label: label(labels[k], k) });
    });
    if (!rows.length) continue;
    const ds = `trait_${strips.length}`;
    datasets[ds] = rows.slice(0, MAX_SANDBOX_ROWS);
    const title = t.units ? `${t.var} (${t.units})` : t.var;
    strips.push({
      width: 120,
      height,
      data: { name: ds },
      mark: { type: "tick", thickness: 2 },
      encoding: {
        x: { field: "value", type: "quantitative", title, scale: { zero: false } },
        y: yEnc,
        tooltip: [{ field: "label", title: view.tip_dim ?? "Tip" }, { field: "value", title, format: ",.4~g" }],
      },
    });
  }
  const spec: JsonObject = strips.length ? { hconcat: [tree, ...strips], spacing: 8, resolve: { scale: { y: "shared" } } } : tree;
  return { kind: "vega-lite", datasets, spec };
}

// ------------------------------------------------------------------------------ circular

/** The bin centres in degrees (0..360) from the bin edges (`bins`, one more than the counts). */
export function binCentresDeg(bins: readonly number[], period: number): number[] {
  return bins.slice(0, -1).map((lo, i) => (((lo + (bins[i + 1] ?? period)) / 2 / period) * 360));
}

/** The width of each bin in degrees. */
export function binWidthsDeg(bins: readonly number[], period: number): number[] {
  return bins.slice(0, -1).map((lo, i) => (((bins[i + 1] ?? period) - lo) / period) * 360);
}

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

/**
 * Where 0 is and the direction: compass and clock (north, clockwise; the default) or
 * mathematics (east, anticlockwise).
 */
export function circularFrame(entry: Pick<CircularEntry, "zero" | "clockwise">): { rotation: number; direction: "clockwise" | "counterclockwise" } {
  const zero = entry.zero ?? "north";
  const rotation = zero === "east" ? 0 : 90;
  const clockwise = entry.clockwise ?? zero !== "east";
  return { rotation, direction: clockwise ? "clockwise" : "counterclockwise" };
}

function angularTicks(entry: CircularEntry): { tickvals: number[]; ticktext: string[] } {
  const units = (entry.units ?? "").toLowerCase();
  if (units.startsWith("h")) {
    const vals = [0, 3, 6, 9, 12, 15, 18, 21].filter((h) => h < entry.period);
    return { tickvals: vals.map((h) => (h / entry.period) * 360), ticktext: vals.map((h) => `${h} h`) };
  }
  if (units.startsWith("deg") || units === "°") {
    const { rotation, direction } = circularFrame(entry);
    const tickvals = [0, 45, 90, 135, 180, 225, 270, 315];
    return { tickvals, ticktext: rotation === 90 && direction === "clockwise" ? COMPASS : tickvals.map((d) => `${d}°`) };
  }
  const k = 4;
  const vals = Array.from({ length: k }, (_, i) => (i * entry.period) / k);
  return { tickvals: vals.map((v) => (v / entry.period) * 360), ticktext: vals.map((v) => fmtNum(v)) };
}

/**
 * Rose diagrams (Plotly `barpolar`): one polar subplot per group (at most 12), the counts per
 * bin and the mean direction as a line whose length is the resultant length R.
 */
export function rosePlot(entry: CircularEntry): SciPlot | null {
  const groups = entry.groups.slice(0, 12).filter((g) => g.counts.length);
  if (!groups.length) return null;
  const cols = Math.min(groups.length, 3);
  const rowsN = Math.ceil(groups.length / cols);
  const datasets: PlotDatasets = {};
  const traces: JsonObject[] = [];
  const layout: JsonObject = { height: Math.max(320, rowsN * 300), margin: { l: 30, r: 30, t: 40, b: 30 }, showlegend: false, annotations: [] };
  const { rotation, direction } = circularFrame(entry);
  const ticks = angularTicks(entry);
  groups.forEach((g, k) => {
    const id = k === 0 ? "polar" : `polar${k + 1}`;
    const centres = binCentresDeg(entry.bins, entry.period);
    const widths = binWidthsDeg(entry.bins, entry.period);
    const ds = `g${k}`;
    datasets[ds] = g.counts.map((c, i) => ({ theta: centres[i] ?? 0, width: widths[i] ?? 360 / g.counts.length, count: c }));
    traces.push({ type: "barpolar", subplot: id, r: { $ref: `${ds}.count` }, theta: { $ref: `${ds}.theta` }, width: { $ref: `${ds}.width` }, name: plotlyText(g.label ?? "all"), marker: { line: { width: 0.5 } }, hovertemplate: "%{theta:.0f}°: %{r}<extra></extra>" });
    const max = Math.max(0, ...g.counts);
    if (finite(g.mean_direction) && finite(g.resultant_length) && max > 0) {
      const deg = (g.mean_direction / entry.period) * 360;
      traces.push({ type: "scatterpolar", subplot: id, mode: "lines", r: [0, Math.max(0, Math.min(1, g.resultant_length)) * max], theta: [deg, deg], line: { width: 3, color: "#c0392b" }, hoverinfo: "skip", name: "Mean direction" });
    }
    const col = k % cols;
    const row = Math.floor(k / cols);
    const pad = 0.06;
    const x0 = col / cols + pad / 2;
    const x1 = (col + 1) / cols - pad / 2;
    const y1 = 1 - row / rowsN - pad;
    const y0 = 1 - (row + 1) / rowsN + pad / 2;
    layout[id] = {
      domain: { x: [x0, x1], y: [y0, y1] },
      angularaxis: { rotation, direction, tickmode: "array", tickvals: ticks.tickvals, ticktext: ticks.ticktext },
      radialaxis: { showticklabels: false, ticks: "" },
    };
    if (groups.length > 1 || g.label !== null) {
      (layout.annotations as JsonObject[]).push({ text: `${plotlyText(g.label ?? "all")} (n = ${g.n})`, x: (x0 + x1) / 2, y: y1 + 0.035, xref: "paper", yref: "paper", showarrow: false, xanchor: "center", yanchor: "bottom", font: { size: 12 } });
    }
  });
  return { kind: "plotly", datasets, spec: { data: traces, layout } };
}

/** "0–90": the label of bin `i` from its edges. */
function binLabel(edges: readonly number[], i: number): string {
  const lo = edges[i];
  const hi = edges[i + 1];
  return lo !== undefined && hi !== undefined ? `${fmtNum(lo)}–${fmtNum(hi)}` : String(i);
}

/** Two circular variables on one dimension: counts per pair of bins (e.g. hour × direction). */
export function circularPairPlot(pair: CircularPair, a?: CircularEntry, b?: CircularEntry): SciPlot | null {
  const cells: { x: string; y: string; value: number }[] = [];
  const na = pair.counts.length;
  const nb = pair.counts[0]?.length ?? 0;
  const yOrder = Array.from({ length: na }, (_, i) => binLabel(pair.a_bins, i));
  const xOrder = Array.from({ length: nb }, (_, j) => binLabel(pair.b_bins, j));
  pair.counts.forEach((row, i) => row.forEach((c, j) => cells.length < MAX_SANDBOX_ROWS && cells.push({ x: xOrder[j]!, y: yOrder[i]!, value: c })));
  if (!cells.length) return null;
  return vl(heatmapPlot(cells, { xTitle: b?.units ? `${pair.b} (${b.units})` : pair.b, yTitle: a?.units ? `${pair.a} (${a.units})` : pair.a, legend: "Count", scheme: "blues", xOrder, yOrder, format: "," }));
}

// ------------------------------------------------------------------------------ points

export type PointsColour = { kind: "none" } | { kind: "srgb" } | { kind: "mode" } | { kind: "agreement" } | { kind: "var"; var: string };

/** The colours a points view can show (the swatch colour first when there is one). */
export function pointsColours(view: PointsView): PointsColour[] {
  const out: PointsColour[] = [];
  if (view.srgb?.some((h) => safeHex(h) !== null)) out.push({ kind: "srgb" });
  if (view.answers) out.push({ kind: "mode" }, { kind: "agreement" });
  for (const c of view.colour ?? []) out.push({ kind: "var", var: c.var });
  if (!out.length) out.push({ kind: "none" });
  return out;
}

/** The modal term of point `i`. */
export function modeTerm(view: PointsView, i: number): string | null {
  return view.answers?.mode[i] ?? null;
}

/**
 * The points in their sample: cells of a lattice (rect) or dots, coloured by the swatch
 * colour (validated "#rrggbb"), the modal answer, the agreement or a numeric variable.
 */
export function pointsPlot(view: PointsView, colour: PointsColour): SciPlot | null {
  const s = view.sample;
  const n = Math.min(s.x.length, s.y.length, MAX_SANDBOX_ROWS);
  const values = colour.kind === "var" ? view.colour?.find((c) => c.var === colour.var)?.values : undefined;
  const rows: Row[] = [];
  for (let i = 0; i < n; i++) {
    if (!finite(s.x[i]) || !finite(s.y[i])) continue;
    const hex = colour.kind === "srgb" ? safeHex(view.srgb?.[i]) : null;
    if (colour.kind === "srgb" && !hex) continue;
    rows.push({ x: s.x[i]!, y: s.y[i]!, index: s.index[i] ?? i, hex, mode: modeTerm(view, i), agreement: view.answers?.agreement[i] ?? null, value: values?.[i] ?? null });
  }
  if (!rows.length) return null;
  const lattice = Boolean(view.lattice);
  const color: JsonObject | null =
    colour.kind === "srgb"
      ? { field: "hex", type: "nominal", scale: null, legend: null }
      : colour.kind === "mode"
        ? { field: "mode", type: "nominal", title: view.answers?.var ?? "Answer", scale: { scheme: "tableau20" } }
        : colour.kind === "agreement"
          ? { field: "agreement", type: "quantitative", title: "Agreement", scale: { domain: [0, 1], scheme: "viridis" }, legend: { format: ".0%" } }
          : colour.kind === "var"
            ? { field: "value", type: "quantitative", title: colour.var, scale: { scheme: "viridis" } }
            : null;
  const x: JsonObject = lattice ? { field: "x", type: "ordinal", title: view.x, axis: { labelOverlap: true, labelAngle: 0 } } : { field: "x", type: "quantitative", title: view.x, scale: { zero: false } };
  const y: JsonObject = lattice ? { field: "y", type: "ordinal", title: view.y } : { field: "y", type: "quantitative", title: view.y, scale: { zero: false } };
  const tooltip: JsonObject[] = [
    { field: "index", title: view.point_dim },
    { field: "x", title: view.x },
    { field: "y", title: view.y },
    ...(colour.kind === "srgb" ? [{ field: "hex", title: "Colour" }] : []),
    ...(view.answers ? [{ field: "mode", title: `Most frequent ${view.answers.var}` }, { field: "agreement", title: "Agreement", format: ".0%" }] : []),
    ...(colour.kind === "var" ? [{ field: "value", title: colour.var, format: ",.4~g" }] : []),
  ];
  return {
    kind: "vega-lite",
    datasets: { points: rows },
    spec: {
      data: { name: "points" },
      ...(lattice ? { height: { step: 22 }, width: { step: 14 } } : { height: 380 }),
      mark: lattice ? { type: "rect", stroke: "white", strokeWidth: 0.5 } : { type: "circle", size: 22, opacity: 0.85 },
      encoding: { x, y, ...(color ? { color } : {}), tooltip },
    },
  };
}

/** The density of the points (64 × 64 counts, when the sample is not all of them). */
export function pointsDensityPlot(view: PointsView): SciPlot | null {
  const d = view.density;
  if (!d?.counts.length) return null;
  const rows: Row[] = [];
  d.counts.forEach((row, i) =>
    row.forEach((c, j) => {
      if (c > 0 && rows.length < MAX_SANDBOX_ROWS) rows.push({ x: d.x_edges[j] ?? j, x2: d.x_edges[j + 1] ?? j + 1, y: d.y_edges[i] ?? i, y2: d.y_edges[i + 1] ?? i + 1, count: c });
    }),
  );
  if (!rows.length) return null;
  return {
    kind: "vega-lite",
    datasets: { density: rows },
    spec: {
      data: { name: "density" },
      height: 320,
      mark: { type: "rect" },
      encoding: {
        x: { field: "x", type: "quantitative", title: view.x, scale: { zero: false } },
        x2: { field: "x2" },
        y: { field: "y", type: "quantitative", title: view.y, scale: { zero: false } },
        y2: { field: "y2" },
        color: { field: "count", type: "quantitative", title: "Points", scale: { scheme: "blues", type: "symlog" } },
        tooltip: [{ field: "count", title: "Points" }],
      },
    },
  };
}
