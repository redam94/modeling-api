import type { JsonObject, PlotDatasets } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";

import { modelArtifactLink } from "@/api/queries";
import type { ArtifactLink, PlotSpec } from "@/api/types";
import { base64ToBytes, decodeArrow, type Row } from "@/lib/arrow";
import { fetchArtifactBytes, fetchArtifactJson } from "@/lib/artifacts";

export const MAX_PLOT_ROWS = 100_000;

export type LinkResolver = (path: string) => Promise<ArtifactLink>;

function jsonRows(value: unknown, name: string): Row[] {
  if (!Array.isArray(value)) throw new Error(`dataset ${name} (JSON) must be an array of records`);
  return value.slice(0, MAX_PLOT_ROWS).map((r) => {
    if (typeof r !== "object" || r === null || Array.isArray(r)) throw new Error(`dataset ${name} has a non-record row`);
    const row: Row = {};
    for (const [k, v] of Object.entries(r as Record<string, unknown>)) {
      row[k] = v === null || typeof v === "string" || typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v)) ? v : null;
    }
    return row;
  });
}

/** Resolve every named dataset of a PlotSpec into rows (Arrow decoded in the app). */
export async function resolvePlotDatasets(
  spec: Pick<PlotSpec, "datasets">,
  resolveLink: LinkResolver,
  signal?: AbortSignal,
): Promise<PlotDatasets> {
  const out: PlotDatasets = {};
  const entries = Object.entries(spec.datasets ?? {});
  await Promise.all(
    entries.map(async ([name, ds]) => {
      if (ds.kind === "inline_arrow") {
        out[name] = decodeArrow(base64ToBytes(ds.data), MAX_PLOT_ROWS).rows;
        return;
      }
      const link = await resolveLink(ds.path);
      if (ds.format === "json") {
        out[name] = jsonRows(await fetchArtifactJson(link, signal ? { signal } : {}), name);
      } else {
        const bytes = await fetchArtifactBytes(link, signal ? { signal } : {});
        out[name] = decodeArrow(bytes, MAX_PLOT_ROWS).rows;
      }
    }),
  );
  return out;
}

export interface ResolvedPlot {
  spec: PlotSpec;
  body: JsonObject;
  datasets: PlotDatasets;
}

export function useModelPlotData(modelId: string, version: number, plot: PlotSpec | undefined) {
  return useQuery({
    queryKey: ["models", modelId, "versions", version, "plots", plot?.id ?? "", "data"],
    enabled: Boolean(plot),
    staleTime: 10 * 60_000,
    queryFn: async ({ signal }): Promise<ResolvedPlot> => {
      const spec = plot as PlotSpec;
      const datasets = await resolvePlotDatasets(spec, (path) => modelArtifactLink(modelId, version, path), signal);
      return { spec, body: spec.spec as JsonObject, datasets };
    },
  });
}
