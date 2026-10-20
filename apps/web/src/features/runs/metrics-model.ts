/**
 * Pure helpers of the run page's Metrics tab: group the run's metric series by key, pick the
 * label that colours a chart, and filter series by label values.
 */
import type { MetricSeries } from "@/api/tracking";

export interface MetricGroup {
  key: string;
  series: MetricSeries[];
  /** Label keys present on any series of the key, sorted. */
  labelKeys: string[];
  /** Every series is a scalar (logged without a step): show it in the "Values" table. */
  scalar: boolean;
}

/** Group series by key (sorted by key, then by their labels). */
export function groupSeries(series: readonly MetricSeries[]): MetricGroup[] {
  const byKey = new Map<string, MetricSeries[]>();
  for (const s of series) {
    const list = byKey.get(s.key);
    if (list) list.push(s);
    else byKey.set(s.key, [s]);
  }
  return [...byKey.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, list]) => {
      const sorted = [...list].sort((a, b) => compareLabels(a.labels, b.labels));
      const labelKeys = [...new Set(sorted.flatMap((s) => Object.keys(s.labels ?? {})))].sort();
      return { key, series: sorted, labelKeys, scalar: sorted.every((s) => s.last_step == null) };
    });
}

/** `chain` when present, else the first label key, else none. */
export function defaultColorLabel(labelKeys: readonly string[]): string | null {
  if (labelKeys.includes("chain")) return "chain";
  return labelKeys[0] ?? null;
}

/** "chain=0, fit=evaluation" (sorted by key); "" without labels. */
export function labelText(labels: Record<string, string> | undefined): string {
  return Object.entries(labels ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`)
    .join(", ");
}

function compareLabels(a: Record<string, string> | undefined, b: Record<string, string> | undefined): number {
  return labelText(a).localeCompare(labelText(b), "en", { numeric: true });
}

/** The values each label key takes over `series`, sorted (numbers in number order). */
export function labelValues(series: readonly { labels?: Record<string, string> | undefined }[]): Record<string, string[]> {
  const out: Record<string, Set<string>> = {};
  for (const s of series) {
    for (const [k, v] of Object.entries(s.labels ?? {})) (out[k] ??= new Set()).add(v);
  }
  return Object.fromEntries(
    Object.entries(out).map(([k, set]) => [k, [...set].sort((a, b) => a.localeCompare(b, "en", { numeric: true }))]),
  );
}

/**
 * Series whose label values are not hidden. `hidden` maps a label key to the values to leave
 * out; a series without that label always stays.
 */
export function filterSeries<T extends { labels?: Record<string, string> | undefined }>(series: readonly T[], hidden: Record<string, readonly string[]>): T[] {
  return series.filter((s) =>
    Object.entries(hidden).every(([k, values]) => {
      const v = s.labels?.[k];
      return v === undefined || !values.includes(v);
    }),
  );
}

export interface GroupStats {
  latest: number | null;
  min: number;
  max: number;
  series: number;
}

/** Latest value (of the series that logged last), min and max over every series. */
export function groupStats(series: readonly MetricSeries[]): GroupStats | null {
  if (!series.length) return null;
  let newest = series[0]!;
  let min = Infinity;
  let max = -Infinity;
  for (const s of series) {
    if (s.last_ts > newest.last_ts) newest = s;
    min = Math.min(min, s.min);
    max = Math.max(max, s.max);
  }
  return { latest: newest.last, min, max, series: series.length };
}
