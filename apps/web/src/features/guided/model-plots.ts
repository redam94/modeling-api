/** The plots a fitted model version rendered (`GET …/plots`), for an optional version. */
import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import type { ModelVersion, PlotListItem } from "@/api/types";

export type ModelPlot = PlotListItem;

export function useModelPlots(mv: Pick<ModelVersion, "model_id" | "version"> | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: qk.plots(s.key, mv?.model_id ?? "", mv?.version ?? 0),
    queryFn: async ({ signal }): Promise<ModelPlot[]> => {
      const page = await unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/plots", {
          params: { path: { model_id: mv?.model_id ?? "", version: mv?.version ?? 0 }, query: { limit: 200 } },
          headers: s.headers,
          signal,
        }),
      );
      return page.items;
    },
    enabled: Boolean(mv),
    staleTime: Infinity,
  });
}
