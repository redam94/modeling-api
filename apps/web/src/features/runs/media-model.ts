/**
 * Pure helpers of the run page's Media tab (v0.14): group the media series of a run by key, and
 * pick the items of one label set in step order.
 */
import type { MediaKeySummary, MediaKind, RunMedia } from "@/api/types";

import { labelsKey } from "./run-query";

export interface MediaGroup {
  key: string;
  kind: MediaKind;
  /** One entry per label set, by labels. */
  series: MediaKeySummary[];
  count: number;
}

/** The media series of a run grouped by key (sorted by key). */
export function groupMediaKeys(keys: readonly MediaKeySummary[]): MediaGroup[] {
  const byKey = new Map<string, MediaKeySummary[]>();
  for (const k of keys) {
    const list = byKey.get(k.key);
    if (list) list.push(k);
    else byKey.set(k.key, [k]);
  }
  return [...byKey.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, list]) => {
      const series = [...list].sort((a, b) => labelsKey(a.labels ?? {}).localeCompare(labelsKey(b.labels ?? {}), "en", { numeric: true }));
      return { key, kind: series[0]!.kind, series, count: series.reduce((n, s) => n + s.count, 0) };
    });
}

/** "split=val, fold=2" for a label set ("" without labels). */
export function labelSetText(labels: Record<string, string> | undefined): string {
  return labelsKey(labels ?? {}).replace(/,/g, ", ");
}

/** The items of one label set (`labelsKey` form), in step order (items without a step first). */
export function seriesItems(items: readonly RunMedia[], labelsId: string): RunMedia[] {
  return items.filter((m) => labelsKey(m.labels ?? {}) === labelsId).sort((a, b) => (a.step ?? -1) - (b.step ?? -1) || a.created_at.localeCompare(b.created_at));
}

/** The slider position: `null` follows the newest item (a live run adds items at the end). */
export function sliderIndex(pos: number | null, n: number): number {
  if (n <= 0) return 0;
  return pos === null ? n - 1 : Math.min(Math.max(pos, 0), n - 1);
}
