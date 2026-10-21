/**
 * What the builder knows about a dashboard's source: the model version it resolves to (an
 * endpoint's current version, or the pinned one) and that version's response grid, which
 * gives the channels and the KPI the what-ifs can use.
 */
import { useEndpoints } from "@/api/integrations";
import { useModelVersions } from "@/api/queries";
import type { ModelVersion } from "@/api/types";
import { useResponseGrid } from "@/features/guided/api";

import type { DashboardSource } from "../types";

export interface SourceGrid {
  /** The resolved model version (`null` while unknown). */
  version: ModelVersion | null;
  /** The grid's channels; `null`: the model has no grid; `undefined`: not known (yet). */
  channels: readonly string[] | null | undefined;
  /** The grid's KPI variable, when there is a grid. */
  kpi: string | null;
  loading: boolean;
}

/** The source's model version and response grid (no request until a source is chosen). */
export function useSourceGrid(source: DashboardSource): SourceGrid {
  const endpoints = useEndpoints();
  const endpoint = source.kind === "endpoint" && source.alias ? (endpoints.data?.items.find((e) => e.alias === source.alias) ?? null) : null;
  const modelId = source.kind === "endpoint" ? (endpoint?.model_id ?? undefined) : (source.model_id ?? undefined);
  const versions = useModelVersions(modelId);
  const version =
    (source.kind === "endpoint"
      ? versions.data?.items.find((v) => v.id === endpoint?.model_version_id)
      : versions.data?.items.find((v) => v.version === source.version)) ?? null;
  const grid = useResponseGrid(version);
  const loading = endpoints.isPending || (Boolean(modelId) && versions.isPending) || (Boolean(version) && grid.isPending);
  const channels = grid.isSuccess ? (grid.data ? grid.data.channels : null) : undefined;
  return { version, channels, kpi: grid.data?.kpi ?? null, loading };
}
