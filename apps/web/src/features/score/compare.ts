/**
 * Predictions against actuals for a forecast that was sent actuals (`ScoreSummary.actuals`):
 * on the periods with an actual the model's predictions are one-step-ahead (what it expected
 * before seeing each actual), after the last actual they are the forecast. This joins the
 * actuals to the summary's prediction table on their coordinates, measures how far off the
 * one-step-ahead predictions were, and builds the chart (a Vega-Lite spec for the plot
 * sandbox: model data is never drawn in the app's own origin).
 */
import type { ScoreSummary } from "@/api/types";
import type { JsonObject, Row } from "@mapi/plot-sandbox/protocol";

type Table = NonNullable<ScoreSummary["tables"]>[number];
type Cell = Table["rows"][number][number];

export interface ComparePoint {
  series: string;
  period: string;
  actual: number | null;
  predicted: number | null;
  lower: number | null;
  upper: number | null;
  /** After the series' last actual: forecast, not a one-step-ahead prediction. */
  forecast: boolean;
}

export interface Comparison {
  variable: string;
  /** The coordinate column along which the forecast runs. */
  periodColumn: string;
  /** The other coordinate columns, joined into the series label. */
  seriesColumns: string[];
  center: string;
  band: { lower: string; upper: string; label: string } | null;
  points: ComparePoint[];
  series: string[];
  metrics: CompareMetrics;
}

export interface CompareMetrics {
  /** Actuals with a prediction to compare with. */
  n: number;
  mae: number | null;
  /** Mean absolute percentage error (actuals of 0 left out), in percent. */
  mape: number | null;
  /** Mean of predicted − actual: above 0 the model over-predicted. */
  bias: number | null;
  /** Share of actuals inside the band, in percent. */
  coverage: number | null;
}

export const CENTERS = ["mean", "median", "point", "q50"];

function num(v: Cell | undefined): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

export function quantile(name: string): number | null {
  const m = /^q(\d+)(?:_(\d+))?$/.exec(name);
  return m ? Number(`${m[1]}.${m[2] ?? "0"}`) : null;
}

/** The widest interval in the stats: `<x>_lower`/`<x>_upper` columns, else the outer quantiles. */
export function bandOf(stats: string[]): Comparison["band"] {
  const pairs = stats.filter((s) => s.endsWith("_lower")).map((s) => s.slice(0, -6)).filter((b) => stats.includes(`${b}_upper`));
  const widest = pairs.sort((a, b) => Number(/\d+/.exec(b)?.[0] ?? 0) - Number(/\d+/.exec(a)?.[0] ?? 0))[0];
  if (widest) return { lower: `${widest}_lower`, upper: `${widest}_upper`, label: widest.replace("_", " ") };
  const qs = stats.map((s) => [s, quantile(s)] as const).filter((x): x is [string, number] => x[1] !== null && x[1] !== 50);
  const lo = qs.filter(([, q]) => q < 50).sort((a, b) => a[1] - b[1])[0];
  const hi = qs.filter(([, q]) => q > 50).sort((a, b) => b[1] - a[1])[0];
  return lo && hi ? { lower: lo[0], upper: hi[0], label: `${lo[0]}–${hi[0]}` } : null;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;

/** The coordinate column the forecast runs along: the conditioning's dim, else the date column, else the last. */
function periodColumnOf(columns: string[], rows: Cell[][], dim: string | null): string {
  if (dim && columns.includes(dim)) return dim;
  const dated = columns.find((_, i) => rows.some((r) => typeof r[i] === "string" && ISO_DATE.test(r[i])));
  return dated ?? columns[columns.length - 1]!;
}

/**
 * The comparison of one actuals table with its prediction table, or null when they cannot be
 * joined (different coordinates, no central statistic).
 */
export function compareActuals(summary: ScoreSummary, actuals: Table, dim: string | null = null): Comparison | null {
  const pred = (summary.tables ?? []).find((t) => t.variable === actuals.variable);
  if (!pred) return null;
  const coords = actuals.columns.filter((c) => c !== "actual");
  if (!coords.every((c) => pred.columns.includes(c))) return null;
  const stats = pred.columns.filter((c) => !coords.includes(c));
  // Every other column must be a statistic: a prediction table that also varies along a dim
  // the actuals lack would need a sum, which a summary cannot give.
  if (stats.some((s) => !CENTERS.includes(s) && !s.endsWith("_lower") && !s.endsWith("_upper") && quantile(s) === null && s !== "sd")) return null;
  const center = CENTERS.find((c) => stats.includes(c));
  if (!center) return null;
  const band = bandOf(stats);
  const periodColumn = periodColumnOf(coords, actuals.rows, dim);
  const seriesColumns = coords.filter((c) => c !== periodColumn);

  const at = (t: Table, name: string) => t.columns.indexOf(name);
  const key = (t: Table, r: Cell[]) => JSON.stringify(coords.map((c) => r[at(t, c)] ?? null));
  const label = (t: Table, r: Cell[]) => seriesColumns.map((c) => String(r[at(t, c)] ?? "")).join(" · ") || "all";
  const actualAt = new Map(actuals.rows.map((r) => [key(actuals, r), num(r[at(actuals, "actual")])]));
  const lastActual = new Map<string, string>();
  for (const r of actuals.rows) {
    const s = label(actuals, r);
    const p = String(r[at(actuals, periodColumn)] ?? "");
    if (!lastActual.has(s) || p > lastActual.get(s)!) lastActual.set(s, p);
  }

  const points: ComparePoint[] = pred.rows.map((r) => {
    const series = label(pred, r);
    const period = String(r[at(pred, periodColumn)] ?? "");
    const actual = actualAt.get(key(pred, r)) ?? null;
    const last = lastActual.get(series);
    return {
      series,
      period,
      actual,
      predicted: num(r[at(pred, center)]),
      lower: band ? num(r[at(pred, band.lower)]) : null,
      upper: band ? num(r[at(pred, band.upper)]) : null,
      forecast: actual === null && (last === undefined || period > last),
    };
  });
  points.sort((a, b) => (a.series === b.series ? (a.period < b.period ? -1 : a.period > b.period ? 1 : 0) : a.series < b.series ? -1 : 1));
  const series = [...new Set(points.map((p) => p.series))];
  return { variable: pred.variable, periodColumn, seriesColumns, center, band, points, series, metrics: metricsOf(points) };
}

export function metricsOf(points: ComparePoint[]): CompareMetrics {
  const pairs = points.filter((p): p is ComparePoint & { actual: number; predicted: number } => p.actual !== null && p.predicted !== null);
  const n = pairs.length;
  if (!n) return { n: 0, mae: null, mape: null, bias: null, coverage: null };
  const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const pct = pairs.filter((p) => p.actual !== 0).map((p) => Math.abs((p.predicted - p.actual) / p.actual) * 100);
  const banded = pairs.filter((p) => p.lower !== null && p.upper !== null);
  return {
    n,
    mae: mean(pairs.map((p) => Math.abs(p.predicted - p.actual))),
    mape: mean(pct),
    bias: mean(pairs.map((p) => p.predicted - p.actual)),
    coverage: banded.length ? (banded.filter((p) => p.actual >= p.lower! && p.actual <= p.upper!).length / banded.length) * 100 : null,
  };
}

/** Recipe-palette colours (`mapi_plots.palette`): the plot sandbox swaps them for dark mode. */
const BAND = "#3366cc";
const ESTIMATE = "#1f4e99";
const OBSERVED = "#1b2333";

/** The chart of one series (or all, coloured): the band, the predictions and the actuals as points. */
export function comparisonChart(c: Comparison, series: string | null): { spec: JsonObject; datasets: Record<string, Row[]> } {
  const shown = c.points.filter((p) => series === null || p.series === series);
  const rows: Row[] = shown.map((p) => ({
      period: p.period,
      series: p.series,
      predicted: p.predicted,
      lower: p.lower,
      upper: p.upper,
      actual: p.actual,
      kind: p.forecast ? "forecast" : "one-step-ahead",
    }));
  // The dashed forecast starts at each series' last one-step-ahead point, so the line is unbroken.
  for (const name of new Set(shown.map((p) => p.series))) {
    const own = shown.filter((p) => p.series === name);
    const first = own.findIndex((p) => p.forecast);
    const before = first > 0 ? own[first - 1] : undefined;
    if (before && !before.forecast) rows.push({ period: before.period, series: name, predicted: before.predicted, lower: before.lower, upper: before.upper, actual: null, kind: "forecast" });
  }
  // Periods are labels in order (ISO dates sort as text): an ordinal axis shows them as sent,
  // with no time zone shifting a date into the day before.
  const x = { field: "period", type: "ordinal", sort: "ascending", title: c.periodColumn, axis: { labelAngle: -40, labelOverlap: true } };
  const color: { color?: JsonObject } = series === null && c.series.length > 1 ? { color: { field: "series", type: "nominal", title: c.seriesColumns.join(" · ") } } : {};
  const layers: JsonObject[] = [];
  if (c.band) {
    layers.push({
      mark: { type: "area", opacity: 0.18, ...(color.color ? {} : { color: BAND }) },
      encoding: { x, y: { field: "lower", type: "quantitative", title: c.variable, scale: { zero: false } }, y2: { field: "upper" }, ...color },
    });
  }
  layers.push({
    mark: { type: "line", strokeWidth: 1.6, point: { size: 18 }, ...(color.color ? {} : { color: ESTIMATE }) },
    encoding: {
      x,
      y: { field: "predicted", type: "quantitative", title: c.variable, scale: { zero: false } },
      strokeDash: { field: "kind", type: "nominal", title: "prediction", scale: { domain: ["one-step-ahead", "forecast"], range: [[1, 0], [5, 3]] } },
      ...color,
      tooltip: [{ field: "period", type: x.type }, { field: "series" }, { field: "predicted", format: ".4~g" }, { field: "actual", format: ".4~g" }, { field: "kind" }],
    },
  });
  layers.push({
    transform: [{ filter: "datum.actual != null" }],
    mark: { type: "point", filled: true, size: 70, shape: "diamond", color: OBSERVED },
    encoding: { x, y: { field: "actual", type: "quantitative" }, tooltip: [{ field: "period", type: x.type }, { field: "series" }, { field: "actual", format: ".4~g" }, { field: "predicted", format: ".4~g" }] },
  });
  return { datasets: { comparison: rows }, spec: { data: { name: "comparison" }, layer: layers } };
}
