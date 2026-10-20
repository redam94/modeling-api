/**
 * Pure helpers of the compare page (v0.14): run labels and colours, which metric keys draw as
 * curves and which as bars, the best run of a metric, the params diff and the media item a run
 * shows at a step of the slider.
 */
import type { CompareParam, CompareSummary, Run, RunMedia } from "@/api/types";
import { runName } from "@/api/tracking";
import { labelsKey, LOWER_IS_BETTER_RE, type MetricAgg } from "@/features/runs/run-query";
import { shortId } from "@/lib/format";

/** Colours of the chart palette (`--series-1..8`, the sandbox's category range). */
export const PALETTE_SIZE = 8;

/** The CSS colour of run `index` (runs 9 and 10 take the first colours again). */
export function runColor(index: number): string {
  return `var(--series-${(index % PALETTE_SIZE) + 1})`;
}

/**
 * A label per run, in run order: the run's name or its short id. Two runs with the same name
 * get their short id after the name, so every label is unique (the charts key on it).
 */
export function runLabels(runs: readonly Pick<Run, "id" | "name">[]): string[] {
  const base = runs.map((r) => runName(r as Run) ?? shortId(r.id));
  const count = new Map<string, number>();
  for (const b of base) count.set(b, (count.get(b) ?? 0) + 1);
  const labels = base.map((b, i) => ((count.get(b) ?? 0) > 1 ? `${b} (${shortId(runs[i]!.id)})` : b));
  // Short ids can collide too: fall back to the full id.
  const seen = new Set<string>();
  return labels.map((l, i) => {
    const out = seen.has(l) ? `${l} ${runs[i]!.id}` : l;
    seen.add(out);
    return out;
  });
}

// ------------------------------------------------------------------------- metrics

/** Is the smallest value of `key` the best one (loss, error, rmse, mae, mape, ...)? */
export function lowerIsBetter(key: string): boolean {
  return LOWER_IS_BETTER_RE.test(key);
}

export interface MetricKeyInfo {
  key: string;
  /** Runs that logged the key. */
  runs: number;
  /** Some series of the key has more than one stepped point: draw it as curves. */
  stepped: boolean;
  /** Label keys of its series, sorted. */
  labelKeys: string[];
}

/** The metric keys of the summaries: most runs first, then by key. */
export function metricKeys(summaries: readonly CompareSummary[]): MetricKeyInfo[] {
  const byKey = new Map<string, { runs: Set<string>; stepped: boolean; labels: Set<string> }>();
  for (const s of summaries) {
    let e = byKey.get(s.key);
    if (!e) byKey.set(s.key, (e = { runs: new Set(), stepped: false, labels: new Set() }));
    e.runs.add(s.run_id);
    if (s.last_step !== null && s.last_step !== undefined && s.count > 1) e.stepped = true;
    for (const k of Object.keys(s.labels ?? {})) e.labels.add(k);
  }
  return [...byKey.entries()]
    .map(([key, e]) => ({ key, runs: e.runs.size, stepped: e.stepped, labelKeys: [...e.labels].sort() }))
    .sort((a, b) => b.runs - a.runs || a.key.localeCompare(b.key));
}

/** The label that dashes the curves of a key by default: `split` when present, else none. */
export function defaultSplitLabel(labelKeys: readonly string[]): string | null {
  return labelKeys.includes("split") ? "split" : null;
}

/** A metric series of the bars: a key and one label set. */
export interface SeriesRef {
  key: string;
  labels: Record<string, string>;
  /** `labels_key` form ("" without labels). */
  id: string;
}

/** The (key, labels) series of the summaries, by key then labels. */
export function summarySeries(summaries: readonly CompareSummary[]): SeriesRef[] {
  const out = new Map<string, SeriesRef>();
  for (const s of summaries) {
    const id = labelsKey(s.labels ?? {});
    out.set(`${s.key}|${id}`, { key: s.key, labels: s.labels ?? {}, id });
  }
  return [...out.values()].sort((a, b) => a.key.localeCompare(b.key) || a.id.localeCompare(b.id, "en", { numeric: true }));
}

/** The summary value (`last`, `min`, `max` or `count`) of one series of a run; `null` when not logged. */
export function summaryValue(summaries: readonly CompareSummary[], runId: string, series: Pick<SeriesRef, "key" | "id">, agg: MetricAgg): number | null {
  // the newest attempt wins when a run has several
  let best: CompareSummary | null = null;
  for (const s of summaries) {
    if (s.run_id !== runId || s.key !== series.key || labelsKey(s.labels ?? {}) !== series.id) continue;
    if (!best || s.attempt > best.attempt) best = s;
  }
  if (!best) return null;
  return agg === "count" ? best.count : agg === "min" ? best.min : agg === "max" ? best.max : best.last;
}

export interface BestRun {
  /** Index of the best run in the values. */
  index: number;
  direction: "min" | "max";
}

/**
 * The best run of a metric: the smallest value for keys like loss, error or rmse, else the
 * largest. `null` with fewer than two values, or when every value is the same.
 */
export function bestRun(key: string, values: readonly (number | null)[]): BestRun | null {
  const direction = lowerIsBetter(key) ? "min" : "max";
  let index = -1;
  let present = 0;
  let distinct = false;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v === null || v === undefined || !Number.isFinite(v)) continue;
    present += 1;
    if (index < 0) {
      index = i;
      continue;
    }
    const cur = values[index]!;
    if (v !== cur) distinct = true;
    if (direction === "min" ? v < cur : v > cur) index = i;
  }
  return present >= 2 && distinct ? { index, direction } : null;
}

// ------------------------------------------------------------------------- params

/**
 * The params diff rows: only the keys whose values differ (when `onlyDiffs`), whose key or a
 * value contains `text` (case-insensitive).
 */
export function paramRows(params: readonly CompareParam[], opts: { onlyDiffs: boolean; text?: string }): CompareParam[] {
  const q = (opts.text ?? "").trim().toLowerCase();
  return params.filter(
    (p) =>
      (!opts.onlyDiffs || p.differs) &&
      (!q || p.key.toLowerCase().includes(q) || Object.values(p.values).some((v) => v !== null && v.toLowerCase().includes(q))),
  );
}

/** Do the values of the runs differ (a run without the param counts as a different value)? */
export function paramDiffers(values: readonly (string | null | undefined)[]): boolean {
  return new Set(values.map((v) => v ?? null)).size > 1;
}

// ------------------------------------------------------------------------- media

const stepOf = (m: Pick<RunMedia, "step">) => m.step ?? 0;

/** The steps of every item, sorted, without repeats (an item without a step counts as step 0). */
export function unionSteps(lists: readonly (readonly Pick<RunMedia, "step">[])[]): number[] {
  const steps = new Set<number>();
  for (const list of lists) for (const m of list) steps.add(stepOf(m));
  return [...steps].sort((a, b) => a - b);
}

/** The item at the greatest step at or below `step` (`null`: the run has none yet). */
export function itemAtStep<T extends Pick<RunMedia, "step">>(items: readonly T[], step: number): T | null {
  let best: T | null = null;
  for (const m of items) {
    const s = stepOf(m);
    if (s <= step && (!best || s >= stepOf(best))) best = m;
  }
  return best;
}
