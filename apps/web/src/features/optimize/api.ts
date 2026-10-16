/**
 * Budget optimisation (M3.5, mmm-framework planning): `POST …/versions/{version}:optimize`
 * (`Prefer: wait` answers 200 with the plan when it finishes in time, else 202) and
 * `GET /v1/optimizations/{run_id}`, polled until done.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, newIdempotencyKey, unwrap, unwrapWithStatus } from "@/api/client";
import type { components } from "@/api/schema";
import { qk, useProjectScope } from "@/api/queries";

type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type S = { [K in keyof components["schemas"]]: Arrayify<components["schemas"][K]> };

export type Optimization = S["Optimization"];
export type OptimizeRequest = S["OptimizeRequest"];
export type OptimizeResult = S["OptimizeResult"];
export type ChannelBounds = S["ChannelBounds"];
export type ChannelAllocation = S["ChannelAllocation"];
export type SpendGroup = S["SpendGroup"];
export type FrontierPoint = S["FrontierPoint"];
export type DistributionSummary = S["DistributionSummary"];

/** Seconds the POST waits for the plan before answering 202 (`Prefer: wait=N`, at most 60). */
export const OPTIMIZE_WAIT_SECONDS = 20;

export const optimizeKeys = {
  optimization: (p: string, runId: string) => ["p", p, "optimizations", runId] as const,
};

export function useStartOptimize(modelId: string, version: number) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: OptimizeRequest): Promise<Optimization> => {
      const res = await unwrapWithStatus(
        api.POST("/v1/models/{model_id}/versions/{version}:optimize", {
          params: { path: { model_id: modelId, version }, header: { Prefer: `wait=${OPTIMIZE_WAIT_SECONDS}`, "Idempotency-Key": newIdempotencyKey() } },
          headers: s.headers,
          body,
        }),
      );
      return res.data;
    },
    onSuccess: (o) => {
      qc.setQueryData(optimizeKeys.optimization(s.key, o.run.id), o);
      void qc.invalidateQueries({ queryKey: qk.runLists(s.key) });
    },
  });
}

/** An optimisation, polled until its run is done. */
export function useOptimization(runId: string | null | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: optimizeKeys.optimization(s.key, runId ?? ""),
    queryFn: ({ signal }): Promise<Optimization> => unwrap(api.GET("/v1/optimizations/{run_id}", { params: { path: { run_id: runId ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(runId),
    refetchInterval: (q) => (q.state.data && !q.state.data.done ? 2000 : false),
  });
}
