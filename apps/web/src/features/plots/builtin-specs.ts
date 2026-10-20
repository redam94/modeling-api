/**
 * First-party Vega-Lite specs the app builds from view data (missingness heatmap,
 * preprocessing-state heatmap, quick result charts). They still render only inside the plot
 * sandbox; data travels as named datasets, never inline URLs.
 */
import type { JsonObject, PlotDatasets, Row } from "@mapi/plot-sandbox/protocol";

import type { MetricHistory } from "@/api/tracking";
import type { CompareSeries } from "@/api/types";
import type { MissingnessGrid, StateArray } from "@/lib/views";

export interface BuiltPlot {
  spec: JsonObject;
  datasets: PlotDatasets;
}

export function missingnessHeatmap(grid: MissingnessGrid): BuiltPlot {
  const rows: Row[] = [];
  grid.labels.forEach((label, i) => {
    grid.coords.forEach((coord, j) => {
      rows.push({ label, coord, missing: grid.fraction[i]?.[j] ?? 0 });
    });
  });
  return {
    datasets: { missing: rows },
    spec: {
      data: { name: "missing" },
      mark: { type: "rect" },
      height: { step: 18 },
      encoding: {
        x: { field: "coord", type: "ordinal", title: grid.along, axis: { labelOverlap: true, labelAngle: 0 } },
        y: { field: "label", type: "nominal", title: null, sort: grid.labels },
        color: {
          field: "missing",
          type: "quantitative",
          title: "Missing",
          scale: { domain: [0, 1] },
          legend: { format: ".0%" },
        },
        tooltip: [
          { field: "label", title: "Series" },
          { field: "coord", title: grid.along },
          { field: "missing", title: "Missing", format: ".0%" },
        ],
      },
    },
  };
}

export function stateArrayPlot(array: StateArray): BuiltPlot {
  const [rowDim, colDim] = array.dims;
  const rows: Row[] = [];
  if (array.dims.length === 2 && rowDim && colDim) {
    const rc = array.coords[rowDim] ?? [];
    const cc = array.coords[colDim] ?? [];
    array.values.forEach((vals, i) => {
      vals.forEach((v, j) => {
        rows.push({ [rowDim]: String(rc[i] ?? i), [colDim]: String(cc[j] ?? j), value: v });
      });
    });
    return {
      datasets: { state: rows },
      spec: {
        data: { name: "state" },
        mark: { type: "rect" },
        height: { step: 28 },
        encoding: {
          x: { field: colDim, type: "nominal", axis: { labelAngle: 0 } },
          y: { field: rowDim, type: "nominal" },
          color: { field: "value", type: "quantitative", title: array.name },
          tooltip: [{ field: rowDim }, { field: colDim }, { field: "value", format: ".4~g" }],
        },
      },
    };
  }
  const dim = rowDim ?? "index";
  const coords = rowDim ? (array.coords[rowDim] ?? []) : [];
  (array.values[0] ?? []).forEach((v, i) => rows.push({ [dim]: String(coords[i] ?? i), value: v }));
  return {
    datasets: { state: rows },
    spec: {
      data: { name: "state" },
      mark: { type: "bar" },
      encoding: {
        x: { field: dim, type: "nominal", axis: { labelAngle: 0 } },
        y: { field: "value", type: "quantitative", title: array.name },
        tooltip: [{ field: dim }, { field: "value", format: ".4~g" }],
      },
    },
  };
}

export interface QuickChart {
  x: string;
  y: string;
  lower?: string | undefined;
  upper?: string | undefined;
  color?: string | undefined;
}

/** A line (plus optional band) over decoded result rows. */
export function quickChart(rows: Row[], c: QuickChart, xIsTime: boolean): BuiltPlot {
  const xType = xIsTime ? "temporal" : "quantitative";
  const base = {
    x: { field: c.x, type: rows.length && typeof rows[0]?.[c.x] === "string" && !xIsTime ? "ordinal" : xType },
    ...(c.color ? { color: { field: c.color, type: "nominal" } } : {}),
  };
  const layers: JsonObject[] = [];
  if (c.lower && c.upper) {
    layers.push({
      mark: { type: "area", opacity: 0.22 },
      encoding: { ...base, y: { field: c.lower, type: "quantitative", title: c.y }, y2: { field: c.upper } },
    });
  }
  layers.push({
    mark: { type: "line", strokeWidth: 1.6 },
    encoding: {
      ...base,
      y: { field: c.y, type: "quantitative" },
      tooltip: [{ field: c.x }, { field: c.y, format: ".4~g" }],
    },
  });
  return { datasets: { result: rows }, spec: { data: { name: "result" }, layer: layers } };
}

export interface MetricCurvesOptions {
  /** Label key that colours the lines (`null`: one colour). */
  colorBy?: string | null | undefined;
  logY?: boolean | undefined;
  title?: string | undefined;
}

/**
 * The history of one metric key (v0.11 run tracking): a line per series (key + labels) over
 * `step`, or over time when no point has a step. A series with one point draws a point.
 * Label values are untrusted text: they travel as data, never as spec fields.
 */
export function metricCurves(history: MetricHistory, opts: MetricCurvesOptions = {}): BuiltPlot {
  const all = history.series ?? [];
  const byTime = all.every((s) => s.steps.every((st) => st === null));
  const colorBy = opts.colorBy ?? null;
  const lines: Row[] = [];
  const points: Row[] = [];
  let positive = true;
  for (const s of all) {
    const labels = Object.entries(s.labels ?? {})
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${k}=${v}`)
      .join(", ");
    const target = s.values.length === 1 ? points : lines;
    s.values.forEach((value, i) => {
      if (value <= 0) positive = false;
      target.push({
        step: s.steps[i] ?? null,
        ts: s.ts[i] ?? null,
        value,
        series: labels || history.key,
        labels,
        group: colorBy ? (s.labels?.[colorBy] ?? "(none)") : history.key,
      });
    });
  }
  const x: JsonObject = byTime
    ? { field: "ts", type: "temporal", title: "Time" }
    : { field: "step", type: "quantitative", title: "Step", axis: { format: "~s" } };
  const y: JsonObject = {
    field: "value",
    type: "quantitative",
    title: opts.title ?? history.key,
    scale: opts.logY ? { type: positive ? "log" : "symlog" } : { zero: false },
  };
  const color: JsonObject = colorBy
    ? { color: { field: "group", type: "nominal", title: colorBy } }
    : {};
  const tooltip: JsonObject[] = [
    byTime ? { field: "ts", type: "temporal", title: "Time", format: "%Y-%m-%d %H:%M:%S" } : { field: "step", title: "Step" },
    { field: "value", title: history.key, format: ".4~g" },
    { field: "labels", title: "Labels" },
  ];
  const layers: JsonObject[] = [];
  if (lines.length) {
    layers.push({
      data: { name: "lines" },
      mark: { type: "line", strokeWidth: 1.4, interpolate: "linear" },
      encoding: { x, y, ...color, detail: { field: "series", type: "nominal" }, tooltip },
    });
  }
  if (points.length) {
    layers.push({
      data: { name: "points" },
      mark: { type: "point", filled: true, size: 50 },
      encoding: { x, y, ...color, tooltip },
    });
  }
  return { datasets: { lines, points }, spec: { layer: layers } };
}

// ------------------------------------------------------------------------- v0.14 run comparison

export interface CompareCurvesRun {
  /** The run's label on the page (its name or short id, unique among the compared runs). */
  runLabel: string;
  history: CompareSeries[];
}

export interface CompareCurvesOptions {
  /** Label key whose values set the line dash (`null`: every series of a run is solid). */
  splitBy?: string | null | undefined;
  logY?: boolean | undefined;
}

/**
 * One metric key of 2..10 runs (the compare page): a line per run and series over `step`,
 * coloured by run in run order (the palette order of the run chips), dashed by the value of the
 * `splitBy` label. Points without a step are left out; a series with one point draws a point.
 * Run names and label values are untrusted text: they travel as data, never as spec fields.
 */
export function compareCurves(series: readonly CompareCurvesRun[], key: string, opts: CompareCurvesOptions = {}): BuiltPlot {
  const splitBy = opts.splitBy ?? null;
  const lines: Row[] = [];
  const points: Row[] = [];
  let positive = true;
  series.forEach(({ runLabel, history }, order) => {
    for (const s of history) {
      const labels = Object.entries(s.labels ?? {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => `${k}=${v}`)
        .join(", ");
      const stepped = s.steps.flatMap((step, i) => (step === null || step === undefined || s.values[i] === undefined ? [] : [{ step, value: s.values[i] }]));
      const target = stepped.length === 1 ? points : lines;
      for (const { step, value } of stepped) {
        if (value <= 0) positive = false;
        target.push({
          step,
          value,
          run: runLabel,
          order,
          series: `${order}|${labels}`,
          labels,
          split: splitBy ? (s.labels?.[splitBy] ?? "(none)") : "",
        });
      }
    }
  });
  const x: JsonObject = { field: "step", type: "quantitative", title: "Step", axis: { format: "~s" } };
  const y: JsonObject = {
    field: "value",
    type: "quantitative",
    title: key,
    scale: opts.logY ? { type: positive ? "log" : "symlog" } : { zero: false },
  };
  const color: JsonObject = { field: "run", type: "nominal", title: "Run", sort: { field: "order", op: "min" } };
  const dash: JsonObject = splitBy ? { strokeDash: { field: "split", type: "nominal", title: splitBy } } : {};
  const tooltip: JsonObject[] = [
    { field: "run", title: "Run" },
    { field: "step", title: "Step" },
    { field: "value", title: key, format: ".4~g" },
    { field: "labels", title: "Labels" },
  ];
  const layers: JsonObject[] = [];
  if (lines.length) {
    layers.push({
      data: { name: "lines" },
      mark: { type: "line", strokeWidth: 1.6, interpolate: "linear" },
      encoding: { x, y, color, ...dash, detail: { field: "series", type: "nominal" }, tooltip },
    });
  }
  if (points.length) {
    layers.push({
      data: { name: "points" },
      mark: { type: "point", filled: true, size: 60 },
      encoding: { x, y, color, ...(splitBy ? { shape: { field: "split", type: "nominal", title: splitBy } } : {}), tooltip },
    });
  }
  return { datasets: { lines, points }, spec: { layer: layers } };
}

/**
 * One value per run as horizontal bars (the compare page's metric bars), in run order and in
 * the run colours. Runs without a value have no bar. Run names travel as data.
 */
export function compareBars(rows: readonly { run: string; value: number | null }[], title: string): BuiltPlot {
  const bars: Row[] = rows.flatMap((r, order) => (r.value === null || !Number.isFinite(r.value) ? [] : [{ run: r.run, value: r.value, order }]));
  return {
    datasets: { bars },
    spec: {
      data: { name: "bars" },
      mark: { type: "bar", cornerRadiusEnd: 2 },
      height: { step: 22 },
      encoding: {
        y: { field: "run", type: "nominal", title: null, sort: { field: "order", op: "min" } },
        x: { field: "value", type: "quantitative", title },
        color: { field: "run", type: "nominal", sort: { field: "order", op: "min" }, legend: null },
        tooltip: [
          { field: "run", title: "Run" },
          { field: "value", title, format: ".4~g" },
        ],
      },
    },
  };
}
