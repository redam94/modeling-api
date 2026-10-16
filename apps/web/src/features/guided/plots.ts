/**
 * Vega-Lite specs for the Guided screens, built from small JSON views (prior preview, forecast
 * and counterfactual views). They render only inside the plot sandbox, like every plot; data
 * travels as named datasets.
 */
import type { JsonObject, Row } from "@mapi/plot-sandbox/protocol";

import type { BuiltPlot } from "@/features/plots/builtin-specs";

import type { DistributionSummary, FanChart } from "./types";

function quantileRow(q: Record<string, number> | undefined): { lo: number | null; q25: number | null; mid: number | null; q75: number | null; hi: number | null } {
  const entries = Object.entries(q ?? {})
    .map(([k, v]) => [Number(k), v] as const)
    .filter(([k, v]) => Number.isFinite(k) && Number.isFinite(v))
    .sort((a, b) => a[0] - b[0]);
  const at = (target: number) => entries.find(([k]) => Math.abs(k - target) < 1e-6)?.[1] ?? null;
  return {
    lo: entries[0]?.[1] ?? null,
    q25: at(0.25),
    mid: at(0.5),
    q75: at(0.75),
    hi: entries[entries.length - 1]?.[1] ?? null,
  };
}

export interface DistributionItem {
  label: string;
  summary: DistributionSummary;
  /** The range the user stated, drawn behind the distribution. */
  stated?: [number, number] | null;
}

/**
 * One row per channel: the stated range as a soft band, the 90% interval as a line, the middle
 * half as a thick bar and the median as a tick - "what your beliefs mean" at a glance.
 */
export function distributionPlot(items: DistributionItem[], opts: { axisTitle: string; format: string }): BuiltPlot {
  const rows: Row[] = items.map((it) => {
    const q = quantileRow(it.summary.quantiles);
    return {
      label: it.label,
      lo: q.lo,
      q25: q.q25 ?? q.lo,
      mid: q.mid ?? it.summary.mean,
      q75: q.q75 ?? q.hi,
      hi: q.hi,
      mean: it.summary.mean,
      stated_lo: it.stated?.[0] ?? null,
      stated_hi: it.stated?.[1] ?? null,
    };
  });
  const y = { field: "label", type: "nominal", title: null, sort: items.map((i) => i.label) };
  const x = (field: string): JsonObject => ({ field, type: "quantitative", title: opts.axisTitle, axis: { format: opts.format } });
  const tooltip: JsonObject[] = [
    { field: "label", title: "Channel" },
    { field: "lo", title: "5%", format: opts.format },
    { field: "mid", title: "Median", format: opts.format },
    { field: "hi", title: "95%", format: opts.format },
    { field: "stated_lo", title: "You said from", format: opts.format },
    { field: "stated_hi", title: "You said to", format: opts.format },
  ];
  return {
    datasets: { beliefs: rows },
    spec: {
      data: { name: "beliefs" },
      height: { step: 34 },
      layer: [
        {
          transform: [{ filter: "datum.stated_lo != null" }],
          mark: { type: "bar", height: 22, opacity: 0.18, cornerRadius: 3 },
          encoding: { y, x: x("stated_lo"), x2: { field: "stated_hi" } },
        },
        { mark: { type: "rule", strokeWidth: 1.5 }, encoding: { y, x: x("lo"), x2: { field: "hi" }, tooltip } },
        { mark: { type: "bar", height: 10, cornerRadius: 2 }, encoding: { y, x: x("q25"), x2: { field: "q75" }, tooltip } },
        { mark: { type: "tick", thickness: 2, size: 18 }, encoding: { y, x: x("mid"), tooltip } },
      ],
    },
  };
}

function isDateLike(v: unknown): boolean {
  return typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v);
}

/**
 * A fan chart: the outer band (first and last quantile), the inner band (25-75% when given),
 * the median line, the observed values as dots and a dashed split line where the forecast
 * starts.
 */
export function fanPlot(fan: FanChart, opts: { title: string; splitAt?: string | number | boolean | null; format?: string }): BuiltPlot {
  const qs = fan.quantiles;
  const idx = (target: number) => qs.findIndex((q) => Math.abs(q - target) < 1e-6);
  const lo = 0;
  const hi = qs.length - 1;
  const mid = idx(0.5) >= 0 ? idx(0.5) : Math.floor(qs.length / 2);
  const in25 = idx(0.25);
  const in75 = idx(0.75);
  const rows: Row[] = fan.coords.map((coord, j) => ({
    x: coord,
    lo: fan.values[lo]?.[j] ?? null,
    hi: fan.values[hi]?.[j] ?? null,
    mid: fan.values[mid]?.[j] ?? null,
    q25: in25 >= 0 ? (fan.values[in25]?.[j] ?? null) : null,
    q75: in75 >= 0 ? (fan.values[in75]?.[j] ?? null) : null,
    observed: fan.observed?.[j] ?? null,
  }));
  const temporal = fan.coords.length > 0 && fan.coords.every(isDateLike);
  const xEnc: JsonObject = { field: "x", type: temporal ? "temporal" : typeof fan.coords[0] === "number" ? "quantitative" : "ordinal", title: null };
  const yTitle = fan.units ? `${opts.title} (${fan.units})` : opts.title;
  const fmt = opts.format ?? "~s";
  const layers: JsonObject[] = [
    {
      transform: [{ filter: "datum.lo != null && datum.hi != null" }],
      mark: { type: "area", opacity: 0.16 },
      encoding: { x: xEnc, y: { field: "lo", type: "quantitative", title: yTitle, axis: { format: fmt } }, y2: { field: "hi" } },
    },
  ];
  if (in25 >= 0 && in75 >= 0) {
    layers.push({
      transform: [{ filter: "datum.q25 != null && datum.q75 != null" }],
      mark: { type: "area", opacity: 0.22 },
      encoding: { x: xEnc, y: { field: "q25", type: "quantitative" }, y2: { field: "q75" } },
    });
  }
  layers.push({
    transform: [{ filter: "datum.mid != null" }],
    mark: { type: "line", strokeWidth: 2 },
    encoding: {
      x: xEnc,
      y: { field: "mid", type: "quantitative" },
      tooltip: [
        { field: "x", title: "Period", ...(temporal ? { type: "temporal" } : {}) },
        { field: "mid", title: "Most likely", format: ",.0f" },
        { field: "lo", title: `${Math.round((qs[lo] ?? 0) * 100)}%`, format: ",.0f" },
        { field: "hi", title: `${Math.round((qs[hi] ?? 0) * 100)}%`, format: ",.0f" },
      ],
    },
  });
  if (fan.observed?.some((v) => v != null)) {
    layers.push({
      transform: [{ filter: "datum.observed != null" }],
      mark: { type: "point", filled: true, size: 18, opacity: 0.85, color: "#737373" },
      encoding: { x: xEnc, y: { field: "observed", type: "quantitative" }, tooltip: [{ field: "x", title: "Period", ...(temporal ? { type: "temporal" } : {}) }, { field: "observed", title: "Actual", format: ",.0f" }] },
    });
  }
  const split = opts.splitAt;
  const datasets: Record<string, Row[]> = { fan: rows };
  if (split !== undefined && split !== null && typeof split !== "boolean") {
    datasets.split = [{ x: split, label: "Forecast starts" }];
    layers.push({
      data: { name: "split" },
      mark: { type: "rule", strokeDash: [4, 4], strokeWidth: 1.5 },
      encoding: { x: xEnc },
    });
    layers.push({
      data: { name: "split" },
      mark: { type: "text", align: "left", dx: 4, dy: -6, fontSize: 11, baseline: "bottom" },
      encoding: { x: xEnc, y: { value: 0 }, text: { field: "label" } },
    });
  }
  return { datasets, spec: { data: { name: "fan" }, height: 260, layer: layers } };
}
