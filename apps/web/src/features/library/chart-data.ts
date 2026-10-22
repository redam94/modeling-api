/**
 * Chart data for the chart template builder, with the Arrow decoder loaded on first use (the
 * `library-*` chunk must not pull Apache Arrow in statically). The example query shares its
 * key and its result shape with `features/plots/plot-example.tsx`, so a page that already drew
 * the example does not fetch it twice.
 */
import type { PlotDatasets } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";

import { ApiError, api, unwrap } from "@/api/client";
import type { PlotSpec } from "@/api/types";
import type { ResolvedPlot } from "@/features/plots/plot-data";
import type { JsonObject } from "@mapi/plot-sandbox/protocol";

/** The datasets of a plot whose data is inline (a catalog example or a preview). */
export async function decodeInline(plot: Pick<PlotSpec, "datasets">, signal?: AbortSignal): Promise<PlotDatasets> {
  const { resolvePlotDatasets } = await import("@/features/plots/plot-data");
  return resolvePlotDatasets(plot, () => Promise.reject(new Error("an example or a preview embeds its datasets")), signal);
}

/** The catalog example of `id@version`, decoded; `null` when the chart type has none. */
export function useChartExample(id: string, version: string, enabled = true) {
  return useQuery({
    queryKey: ["catalog", "plots", id, version, "example"] as const,
    enabled: enabled && Boolean(id),
    staleTime: Infinity,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
    queryFn: async ({ signal }): Promise<ResolvedPlot | null> => {
      let spec: PlotSpec;
      try {
        spec = await unwrap(api.GET("/v1/catalog/plots/{plot_id}/{version}/example", { params: { path: { plot_id: id, version } }, signal }));
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
      return { spec, body: spec.spec as JsonObject, datasets: await decodeInline(spec, signal) };
    },
  });
}
