/**
 * The catalog as the library builders read it: the latest version of each step, chart type and
 * model (small hooks over the cached `GET /v1/catalog`, shared by the builders and the dialogs).
 */
import { useMemo } from "react";

import { useCatalog } from "@/api/queries";
import type { ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry } from "@/api/types";

function latest<T>(items: readonly T[], id: (t: T) => string, version: (t: T) => string): Map<string, T> {
  const out = new Map<string, T>();
  for (const it of items) {
    const cur = out.get(id(it));
    if (!cur || version(cur) < version(it)) out.set(id(it), it);
  }
  return out;
}

/** The latest version of each catalog step, by id (sorted by kind, then id). */
export function useStepCatalog(): { steps: StepCatalogEntry[]; byId: Map<string, StepCatalogEntry>; isPending: boolean } {
  const catalog = useCatalog();
  return useMemo(() => {
    const byId = latest(catalog.data?.steps ?? [], (s) => s.id, (s) => s.version);
    const steps = [...byId.values()].sort((a, b) => a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id));
    return { steps, byId, isPending: catalog.isPending };
  }, [catalog.data, catalog.isPending]);
}

/** The latest version of each catalog chart type (sorted by title). */
export function useChartTypes(): { plots: PlotCatalogEntry[]; isPending: boolean } {
  const catalog = useCatalog();
  return useMemo(() => {
    const byId = latest(catalog.data?.plots ?? [], (p) => p.id, (p) => p.version ?? "");
    return { plots: [...byId.values()].sort((a, b) => a.title.localeCompare(b.title)), isPending: catalog.isPending };
  }, [catalog.data, catalog.isPending]);
}

/** The latest version of each catalog model that is not yanked (sorted by id). */
export function useModelCatalog(): { models: ModelCatalogEntry[]; isPending: boolean } {
  const catalog = useCatalog();
  return useMemo(() => {
    const byId = latest((catalog.data?.models ?? []).filter((m) => !m.yanked), (m) => m.manifest.id, (m) => m.manifest.version);
    return { models: [...byId.values()].sort((a, b) => a.manifest.id.localeCompare(b.manifest.id)), isPending: catalog.isPending };
  }, [catalog.data, catalog.isPending]);
}
