/**
 * First-party Vega-Lite specs of the explorers, built from view data for what the user picked
 * (a variable, a series, a lag range). Like every plot they render only inside the plot
 * sandbox; data travels as named datasets, never as URLs or inline expressions.
 */
import type { JsonObject, Row } from "@mapi/plot-sandbox/protocol";

import type { BuiltPlot } from "@/features/plots/builtin-specs";

import type { Histogram, IrfView, LagProfile, ResponseCurvesView, StlComponents, ViewFanChart } from "./views";

type Scalar = string | number | boolean | null;

const DATE = /^\d{4}-\d{2}-\d{2}/;

/** The Vega-Lite type of an x axis from its values (dates, numbers or labels). */
export function xType(values: readonly unknown[]): "temporal" | "quantitative" | "ordinal" {
  const v = values.find((x) => x !== null && x !== undefined);
  if (typeof v === "number") return "quantitative";
  if (typeof v === "string" && DATE.test(v)) return "temporal";
  return "ordinal";
}

function scalar(v: unknown): Scalar {
  return v === null || typeof v === "string" || typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v)) ? v : v === undefined ? null : JSON.stringify(v);
}

export interface LineSeries {
  label: string;
  coords: readonly unknown[];
  values: readonly (number | null)[];
}

/** Several series over a shared x axis, one colour each. */
export function linesPlot(series: readonly LineSeries[], opts: { xTitle?: string | null; yTitle?: string | null; height?: number } = {}): BuiltPlot {
  const rows: Row[] = [];
  for (const s of series) {
    s.values.forEach((y, i) => {
      if (y !== null && Number.isFinite(y)) rows.push({ series: s.label, x: scalar(s.coords[i]), y });
    });
  }
  const xt = xType(rows.map((r) => r.x));
  return {
    datasets: { lines: rows },
    spec: {
      data: { name: "lines" },
      height: opts.height ?? 260,
      mark: { type: "line", strokeWidth: 1.6, interpolate: "linear" },
      encoding: {
        x: { field: "x", type: xt, title: opts.xTitle ?? null },
        y: { field: "y", type: "quantitative", title: opts.yTitle ?? null, scale: { zero: false } },
        color: { field: "series", type: "nominal", title: null, legend: series.length > 1 ? { orient: "bottom" } : null },
        tooltip: [
          { field: "series", title: "Series" },
          { field: "x", title: opts.xTitle ?? "x", ...(xt === "temporal" ? { type: "temporal" } : {}) },
          { field: "y", title: opts.yTitle ?? "Value", format: ",.4~g" },
        ],
      },
    },
  };
}

/** A histogram from precomputed bins. */
export function histogramPlot(h: Histogram, opts: { title: string }): BuiltPlot {
  const rows: Row[] = (h.counts ?? []).map((c, i) => ({ lo: h.edges[i] ?? null, hi: h.edges[i + 1] ?? null, count: c }));
  return {
    datasets: { bins: rows },
    spec: {
      data: { name: "bins" },
      height: 220,
      // Explicit rectangles (x..x2 by 0..count) from the precomputed bins.
      mark: { type: "rect", opacity: 0.9 },
      encoding: {
        x: { field: "lo", type: "quantitative", title: opts.title, scale: { zero: false } },
        x2: { field: "hi" },
        y: { field: "count", type: "quantitative", title: "Count" },
        y2: { datum: 0 },
        tooltip: [
          { field: "lo", title: "From", format: ",.4~g" },
          { field: "hi", title: "To", format: ",.4~g" },
          { field: "count", title: "Count" },
        ],
      },
    },
  };
}

/** A heatmap of `value` over `x` × `y` (missingness, correlations), optionally with numbers. */
export function heatmapPlot(
  cells: readonly { x: Scalar; y: Scalar; value: number | null }[],
  opts: { xTitle?: string | null; yTitle?: string | null; legend: string; domain?: [number, number]; scheme?: string; text?: boolean; format?: string; xOrder?: readonly Scalar[]; yOrder?: readonly Scalar[]; temporal?: boolean },
): BuiltPlot {
  const rows: Row[] = cells.map((c) => ({ x: c.x, y: c.y, value: c.value }));
  const fmt = opts.format ?? ".2f";
  const x: JsonObject = { field: "x", type: opts.temporal ? "temporal" : "ordinal", title: opts.xTitle ?? null, ...(opts.xOrder ? { sort: [...opts.xOrder] } : {}), axis: { labelOverlap: true } };
  const y: JsonObject = { field: "y", type: "ordinal", title: opts.yTitle ?? null, ...(opts.yOrder ? { sort: [...opts.yOrder] } : {}) };
  const color: JsonObject = {
    field: "value",
    type: "quantitative",
    title: opts.legend,
    scale: { ...(opts.domain ? { domain: opts.domain } : {}), ...(opts.scheme ? { scheme: opts.scheme } : {}) },
  };
  const layers: JsonObject[] = [
    {
      mark: { type: "rect" },
      encoding: { x, y, color, tooltip: [{ field: "y", title: opts.yTitle ?? "Row" }, { field: "x", title: opts.xTitle ?? "Column" }, { field: "value", title: opts.legend, format: fmt }] },
    },
  ];
  if (opts.text) {
    layers.push({
      transform: [{ filter: "datum.value != null" }],
      mark: { type: "text", fontSize: 10 },
      encoding: { x, y, text: { field: "value", type: "quantitative", format: fmt } },
    });
  }
  return { datasets: { cells: rows }, spec: { data: { name: "cells" }, height: { step: 22 }, layer: layers } };
}

/** Bars by lag with the dashed ±bound significance band (ACF, PACF, cross-correlation). */
export function lagPlot(profile: Pick<LagProfile, "lags" | "values" | "bound" | "peak_lag">, opts: { yTitle: string; xTitle?: string; height?: number }): BuiltPlot {
  const rows: Row[] = profile.lags.map((lag, i) => {
    const value = profile.values[i] ?? null;
    const bound = profile.bound ?? null;
    return { lag, value, significant: bound !== null && value !== null ? Math.abs(value) > bound : false, peak: lag === profile.peak_lag };
  });
  const bound = profile.bound ?? null;
  const layers: JsonObject[] = [
    {
      mark: { type: "bar", width: { band: 0.7 } },
      encoding: {
        x: { field: "lag", type: "ordinal", title: opts.xTitle ?? "Lag", axis: { labelOverlap: true, labelAngle: 0 } },
        y: { field: "value", type: "quantitative", title: opts.yTitle, scale: { domain: [-1, 1] } },
        opacity: { condition: { test: "datum.significant", value: 1 }, value: 0.45 },
        tooltip: [
          { field: "lag", title: "Lag" },
          { field: "value", title: opts.yTitle, format: ".3f" },
          { field: "significant", title: "Beyond the band" },
        ],
      },
    },
    { data: { values: [{ zero: 0 }] }, mark: { type: "rule", strokeWidth: 1 }, encoding: { y: { field: "zero", type: "quantitative" } } },
  ];
  if (bound !== null) {
    layers.push({
      data: { values: [{ b: bound }, { b: -bound }] },
      mark: { type: "rule", strokeDash: [4, 4], opacity: 0.8 },
      encoding: { y: { field: "b", type: "quantitative" } },
    });
  }
  return { datasets: { lags: rows }, spec: { data: { name: "lags" }, height: opts.height ?? 220, layer: layers } };
}

/** Observed, trend, season and remainder of one series, stacked as four panels. */
export function stlPlot(item: StlComponents): BuiltPlot {
  const order = ["observed", "trend", "seasonal", "resid"] as const;
  const names: Record<(typeof order)[number], string> = { observed: "Observed", trend: "Trend", seasonal: "Season", resid: "Remainder" };
  const rows: Row[] = [];
  for (const comp of order) {
    item[comp].forEach((y, i) => {
      if (y !== null && Number.isFinite(y)) rows.push({ component: names[comp], x: scalar(item.coords[i]), y });
    });
  }
  const xt = xType(item.coords);
  return {
    datasets: { stl: rows },
    spec: {
      data: { name: "stl" },
      facet: { row: { field: "component", type: "nominal", title: null, sort: order.map((o) => names[o]) } },
      resolve: { scale: { y: "independent" } },
      spec: {
        height: 90,
        mark: { type: "line", strokeWidth: 1.4 },
        encoding: {
          x: { field: "x", type: xt, title: null },
          y: { field: "y", type: "quantitative", title: null, scale: { zero: false } },
          tooltip: [{ field: "component", title: "Part" }, { field: "x", title: "Period", ...(xt === "temporal" ? { type: "temporal" } : {}) }, { field: "y", title: "Value", format: ",.4~g" }],
        },
      },
    },
  };
}

export interface BarDatum {
  label: string;
  value: number | null;
  /** A category for colour (e.g. severity, flagged). */
  group?: string | undefined;
  lo?: number | null | undefined;
  hi?: number | null | undefined;
}

/** Horizontal bars (VIF, shares, outlier counts) with an optional reference line and error bars. */
export function barsPlot(data: readonly BarDatum[], opts: { title: string; format?: string; ruleAt?: number | null; ruleLabel?: string; sort?: "value" | "none"; groups?: { domain: string[]; range?: string[] } }): BuiltPlot {
  const rows: Row[] = data.map((d) => ({ label: d.label, value: d.value, group: d.group ?? null, lo: d.lo ?? null, hi: d.hi ?? null }));
  const y: JsonObject = { field: "label", type: "nominal", title: null, sort: opts.sort === "none" ? null : "-x" };
  const fmt = opts.format ?? ",.4~g";
  const layers: JsonObject[] = [
    {
      mark: { type: "bar", cornerRadiusEnd: 2 },
      encoding: {
        y,
        x: { field: "value", type: "quantitative", title: opts.title, axis: { format: fmt } },
        ...(opts.groups ? { color: { field: "group", type: "nominal", title: null, scale: { domain: opts.groups.domain, ...(opts.groups.range ? { range: opts.groups.range } : {}) } } } : {}),
        tooltip: [{ field: "label", title: "Item" }, { field: "value", title: opts.title, format: fmt }],
      },
    },
  ];
  if (data.some((d) => d.lo != null && d.hi != null)) {
    layers.push({
      transform: [{ filter: "datum.lo != null && datum.hi != null" }],
      mark: { type: "rule", strokeWidth: 1.5 },
      encoding: { y, x: { field: "lo", type: "quantitative" }, x2: { field: "hi" } },
    });
  }
  if (opts.ruleAt !== undefined && opts.ruleAt !== null) {
    layers.push({
      data: { values: [{ at: opts.ruleAt, label: opts.ruleLabel ?? "" }] },
      mark: { type: "rule", strokeDash: [5, 4], strokeWidth: 1.5, color: "#b42f28" },
      encoding: { x: { field: "at", type: "quantitative" } },
    });
  }
  return { datasets: { bars: rows }, spec: { data: { name: "bars" }, height: { step: 24 }, layer: layers } };
}

/** Estimates with their intervals as a forest plot (one row per parameter cell). */
export function forestPlot(rows: readonly { label: string; mean: number | null; lower: number | null; upper: number | null }[], opts: { title: string }): BuiltPlot {
  const data: Row[] = rows.map((r) => ({ label: r.label, mean: r.mean, lower: r.lower, upper: r.upper }));
  const y: JsonObject = { field: "label", type: "nominal", title: null, sort: null };
  return {
    datasets: { est: data },
    spec: {
      data: { name: "est" },
      height: { step: 20 },
      layer: [
        { data: { values: [{ zero: 0 }] }, mark: { type: "rule", strokeDash: [3, 3], opacity: 0.6 }, encoding: { x: { field: "zero", type: "quantitative" } } },
        {
          transform: [{ filter: "datum.lower != null && datum.upper != null" }],
          mark: { type: "rule", strokeWidth: 2 },
          encoding: { y, x: { field: "lower", type: "quantitative", title: opts.title }, x2: { field: "upper" } },
        },
        {
          mark: { type: "point", filled: true, size: 45 },
          encoding: {
            y,
            x: { field: "mean", type: "quantitative" },
            tooltip: [
              { field: "label", title: "Parameter" },
              { field: "mean", title: "Estimate", format: ",.4~g" },
              { field: "lower", title: "Lower", format: ",.4~g" },
              { field: "upper", title: "Upper", format: ",.4~g" },
            ],
          },
        },
      ],
    },
  };
}

/** Actual (dots) against fitted (line, band) with the split line at the first held-out period. */
export function fitPlot(actual: { coords: readonly unknown[]; values: readonly (number | null)[] }, fitted: ViewFanChart | null, splitAt: unknown, opts: { yTitle: string }): BuiltPlot {
  const rows: Row[] = [];
  const quantiles = fitted?.quantiles ?? [];
  const lo = 0;
  const hi = quantiles.length - 1;
  const midIdx = quantiles.findIndex((q) => Math.abs(q - 0.5) < 1e-6);
  const mid = midIdx >= 0 ? midIdx : Math.floor(quantiles.length / 2);
  const coordIndex = new Map<string, number>();
  (fitted?.coords ?? []).forEach((c, j) => coordIndex.set(String(c), j));
  actual.coords.forEach((c, i) => {
    const j = coordIndex.get(String(c));
    rows.push({
      x: scalar(c),
      actual: actual.values[i] ?? null,
      fitted: j !== undefined ? (fitted?.values[mid]?.[j] ?? null) : null,
      lo: j !== undefined && hi > 0 ? (fitted?.values[lo]?.[j] ?? null) : null,
      hi: j !== undefined && hi > 0 ? (fitted?.values[hi]?.[j] ?? null) : null,
    });
  });
  const xt = xType(actual.coords);
  const x: JsonObject = { field: "x", type: xt, title: null };
  const scale: JsonObject = { zero: false };
  const layers: JsonObject[] = [
    {
      transform: [{ filter: "datum.lo != null && datum.hi != null" }],
      mark: { type: "area", opacity: 0.18 },
      encoding: { x, y: { field: "lo", type: "quantitative", title: opts.yTitle, scale }, y2: { field: "hi" } },
    },
    {
      transform: [{ filter: "datum.fitted != null" }],
      mark: { type: "line", strokeWidth: 1.8 },
      encoding: { x, y: { field: "fitted", type: "quantitative", scale } },
    },
    {
      transform: [{ filter: "datum.actual != null" }],
      mark: { type: "point", filled: true, size: 16, color: "#737373" },
      encoding: {
        x,
        y: { field: "actual", type: "quantitative", scale },
        tooltip: [{ field: "x", title: "Period", ...(xt === "temporal" ? { type: "temporal" } : {}) }, { field: "actual", title: "Actual", format: ",.4~g" }, { field: "fitted", title: "Fitted", format: ",.4~g" }],
      },
    },
  ];
  const datasets: Record<string, Row[]> = { fit: rows };
  if (splitAt !== null && splitAt !== undefined && typeof splitAt !== "boolean") {
    datasets.split = [{ x: scalar(splitAt), label: "Held out from here" }];
    layers.push({ data: { name: "split" }, mark: { type: "rule", strokeDash: [4, 4] }, encoding: { x } });
    layers.push({ data: { name: "split" }, mark: { type: "text", align: "left", dx: 4, dy: -4, fontSize: 11, baseline: "bottom" }, encoding: { x, y: { value: 0 }, text: { field: "label" } } });
  }
  return { datasets, spec: { data: { name: "fit" }, height: 260, layer: layers } };
}

/** Normal QQ points against the 45° line. */
export function qqPlot(theoretical: readonly number[], sample: readonly number[]): BuiltPlot {
  const rows: Row[] = theoretical.map((t, i) => ({ t, s: sample[i] ?? null }));
  const finite = rows.flatMap((r) => [r.t, r.s]).filter((v): v is number => typeof v === "number");
  const lo = Math.min(...finite);
  const hi = Math.max(...finite);
  return {
    datasets: { qq: rows, line: [{ t: lo, s: lo }, { t: hi, s: hi }] },
    spec: {
      height: 240,
      layer: [
        { data: { name: "line" }, mark: { type: "line", strokeDash: [4, 4], opacity: 0.7 }, encoding: { x: { field: "t", type: "quantitative" }, y: { field: "s", type: "quantitative" } } },
        {
          data: { name: "qq" },
          mark: { type: "point", filled: true, size: 18 },
          encoding: {
            x: { field: "t", type: "quantitative", title: "Expected if normal (standardised)" },
            y: { field: "s", type: "quantitative", title: "Residual quantile" },
            tooltip: [{ field: "t", title: "Expected", format: ".3f" }, { field: "s", title: "Observed", format: ".3f" }],
          },
        },
      ],
    },
  };
}

/** Stacked contributions over time (decomposition). */
export function stackedAreaPlot(coords: readonly unknown[], components: Record<string, readonly (number | null)[]>, opts: { yTitle: string }): BuiltPlot {
  const rows: Row[] = [];
  for (const [name, values] of Object.entries(components)) {
    values.forEach((v, i) => {
      if (v !== null && Number.isFinite(v)) rows.push({ component: name, x: scalar(coords[i]), value: v });
    });
  }
  const xt = xType(coords);
  return {
    datasets: { parts: rows },
    spec: {
      data: { name: "parts" },
      height: 280,
      mark: { type: "area", opacity: 0.85, interpolate: "monotone" },
      encoding: {
        x: { field: "x", type: xt, title: null },
        y: { field: "value", type: "quantitative", title: opts.yTitle, stack: "zero" },
        color: { field: "component", type: "nominal", title: null, legend: { orient: "bottom" } },
        tooltip: [{ field: "component", title: "Part" }, { field: "x", title: "Period", ...(xt === "temporal" ? { type: "temporal" } : {}) }, { field: "value", title: "Contribution", format: ",.4~g" }],
      },
    },
  };
}

/** Mean response per channel with its band and a dot at today's spend. */
export function responseCurvesPlot(v: ResponseCurvesView, channels: readonly string[]): BuiltPlot {
  const rows: Row[] = [];
  const now: Row[] = [];
  v.channels.forEach((ch, c) => {
    if (!channels.includes(ch)) return;
    (v.spend[c] ?? []).forEach((s, i) => rows.push({ channel: ch, spend: s, mean: v.mean[c]?.[i] ?? null, lo: v.lower?.[c]?.[i] ?? null, hi: v.upper?.[c]?.[i] ?? null }));
    const cur = v.current_spend?.[c];
    if (cur != null) {
      const spends = v.spend[c] ?? [];
      // the mean at today's spend, read off the curve (linear interpolation)
      let at: number | null = null;
      for (let i = 1; i < spends.length; i++) {
        const a = spends[i - 1]!;
        const b = spends[i]!;
        if (cur >= a && cur <= b) {
          const ya = v.mean[c]?.[i - 1] ?? 0;
          const yb = v.mean[c]?.[i] ?? 0;
          at = b === a ? ya : ya + ((cur - a) / (b - a)) * (yb - ya);
          break;
        }
      }
      now.push({ channel: ch, spend: cur, mean: at });
    }
  });
  const color: JsonObject = { field: "channel", type: "nominal", title: null, legend: { orient: "bottom" } };
  return {
    datasets: { curves: rows, now },
    spec: {
      height: 280,
      layer: [
        { data: { name: "curves" }, transform: [{ filter: "datum.lo != null && datum.hi != null" }], mark: { type: "area", opacity: 0.15 }, encoding: { x: { field: "spend", type: "quantitative" }, y: { field: "lo", type: "quantitative" }, y2: { field: "hi" }, color } },
        {
          data: { name: "curves" },
          mark: { type: "line", strokeWidth: 2 },
          encoding: {
            x: { field: "spend", type: "quantitative", title: "Spend", axis: { format: "~s" } },
            y: { field: "mean", type: "quantitative", title: "Contribution", axis: { format: "~s" } },
            color,
            tooltip: [{ field: "channel", title: "Channel" }, { field: "spend", title: "Spend", format: ",.0f" }, { field: "mean", title: "Contribution", format: ",.0f" }],
          },
        },
        {
          data: { name: "now" },
          transform: [{ filter: "datum.mean != null" }],
          mark: { type: "point", filled: true, size: 80, stroke: "white", strokeWidth: 1 },
          encoding: { x: { field: "spend", type: "quantitative" }, y: { field: "mean", type: "quantitative" }, color, tooltip: [{ field: "channel", title: "Channel" }, { field: "spend", title: "Spent today", format: ",.0f" }] },
        },
      ],
    },
  };
}

/** Impulse responses of one response to one shock with the band and the zero line. */
export function irfPlot(v: IrfView, response: number, shock: number): BuiltPlot {
  const values = v.values[response]?.[shock] ?? [];
  const rows: Row[] = values.map((value, h) => ({ h, value, lo: v.lower?.[response]?.[shock]?.[h] ?? null, hi: v.upper?.[response]?.[shock]?.[h] ?? null }));
  return {
    datasets: { irf: rows },
    spec: {
      data: { name: "irf" },
      height: 220,
      layer: [
        { transform: [{ filter: "datum.lo != null && datum.hi != null" }], mark: { type: "area", opacity: 0.18 }, encoding: { x: { field: "h", type: "quantitative" }, y: { field: "lo", type: "quantitative" }, y2: { field: "hi" } } },
        { data: { values: [{ zero: 0 }] }, mark: { type: "rule", strokeDash: [3, 3] }, encoding: { y: { field: "zero", type: "quantitative" } } },
        {
          mark: { type: "line", strokeWidth: 2, point: true },
          encoding: {
            x: { field: "h", type: "quantitative", title: "Periods after the shock" },
            y: { field: "value", type: "quantitative", title: `Response of ${v.variables[response] ?? ""}` },
            tooltip: [{ field: "h", title: "Periods after" }, { field: "value", title: "Response", format: ".4~g" }, { field: "lo", title: "Lower", format: ".4~g" }, { field: "hi", title: "Upper", format: ".4~g" }],
          },
        },
      ],
    },
  };
}

/** Shares of a variable's forecast-error variance by shock and horizon (stacked bars). */
export function fevdPlot(variables: readonly string[], shares: readonly (readonly number[])[]): BuiltPlot {
  const rows: Row[] = [];
  shares.forEach((row, h) => row.forEach((share, j) => rows.push({ horizon: h + 1, shock: variables[j] ?? String(j), share })));
  return {
    datasets: { fevd: rows },
    spec: {
      data: { name: "fevd" },
      height: 240,
      mark: { type: "bar" },
      encoding: {
        x: { field: "horizon", type: "ordinal", title: "Periods ahead", axis: { labelAngle: 0, labelOverlap: true } },
        y: { field: "share", type: "quantitative", title: "Share of forecast uncertainty", stack: "normalize", axis: { format: "%" } },
        color: { field: "shock", type: "nominal", title: "Due to shocks in", legend: { orient: "bottom" } },
        tooltip: [{ field: "horizon", title: "Periods ahead" }, { field: "shock", title: "Shock" }, { field: "share", title: "Share", format: ".1%" }],
      },
    },
  };
}

/** Grouped bars: current against recommended spend per channel (and geo). */
export function allocationPlot(rows: readonly { label: string; current: number; optimal: number }[], opts: { units: string | null }): BuiltPlot {
  const data: Row[] = rows.flatMap((r) => [
    { label: r.label, plan: "Today", spend: r.current },
    { label: r.label, plan: "Recommended", spend: r.optimal },
  ]);
  return {
    datasets: { alloc: data },
    spec: {
      data: { name: "alloc" },
      height: { step: 16 },
      mark: { type: "bar", cornerRadiusEnd: 2 },
      encoding: {
        y: { field: "label", type: "nominal", title: null, sort: null },
        yOffset: { field: "plan", type: "nominal", sort: ["Today", "Recommended"] },
        x: { field: "spend", type: "quantitative", title: opts.units ? `Spend (${opts.units})` : "Spend", axis: { format: "~s" } },
        color: { field: "plan", type: "nominal", title: null, scale: { domain: ["Today", "Recommended"] }, legend: { orient: "bottom" } },
        tooltip: [{ field: "label", title: "Channel" }, { field: "plan", title: "Plan" }, { field: "spend", title: "Spend", format: ",.0f" }],
      },
    },
  };
}

/** The efficient frontier: the best expected KPI at each total budget, with its band. */
export function frontierPlot(points: readonly { budget: number; kpi: number; lo: number | null; hi: number | null }[], marks: { current: number | null; chosen: number | null }, opts: { kpi: string; units: string | null }): BuiltPlot {
  const rows: Row[] = points.map((p) => ({ budget: p.budget, kpi: p.kpi, lo: p.lo, hi: p.hi }));
  const ref: Row[] = [];
  if (marks.current !== null) ref.push({ budget: marks.current, label: "Today's budget" });
  if (marks.chosen !== null && marks.chosen !== marks.current) ref.push({ budget: marks.chosen, label: "This plan" });
  return {
    datasets: { frontier: rows, marks: ref },
    spec: {
      height: 260,
      layer: [
        { data: { name: "frontier" }, transform: [{ filter: "datum.lo != null && datum.hi != null" }], mark: { type: "area", opacity: 0.16 }, encoding: { x: { field: "budget", type: "quantitative" }, y: { field: "lo", type: "quantitative" }, y2: { field: "hi" } } },
        {
          data: { name: "frontier" },
          mark: { type: "line", strokeWidth: 2, point: true },
          encoding: {
            x: { field: "budget", type: "quantitative", title: opts.units ? `Total budget (${opts.units})` : "Total budget", axis: { format: "~s" } },
            y: { field: "kpi", type: "quantitative", title: `Expected ${opts.kpi}`, scale: { zero: false }, axis: { format: "~s" } },
            tooltip: [{ field: "budget", title: "Budget", format: ",.0f" }, { field: "kpi", title: `Expected ${opts.kpi}`, format: ",.0f" }, { field: "lo", title: "Low (5%)", format: ",.0f" }, { field: "hi", title: "High (95%)", format: ",.0f" }],
          },
        },
        { data: { name: "marks" }, mark: { type: "rule", strokeDash: [4, 4] }, encoding: { x: { field: "budget", type: "quantitative" } } },
        { data: { name: "marks" }, mark: { type: "text", align: "left", dx: 4, dy: -4, fontSize: 11, baseline: "bottom" }, encoding: { x: { field: "budget", type: "quantitative" }, y: { value: 0 }, text: { field: "label" } } },
      ],
    },
  };
}
