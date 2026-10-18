/**
 * M4 dataset calls: inspect an upload (`POST …/uploads/{id}/inspect`, then poll
 * `GET …/inspection` until the run is done) and the per-node files of a tree version
 * (`summary.json` with one summary per node).
 */
import { useMutation, useQuery } from "@tanstack/react-query";

import { ApiError, api, newIdempotencyKey, unwrap } from "@/api/client";
import { datasetArtifactLink, qk, useProjectScope } from "@/api/queries";
import type { DataSummary, DatasetVersion, Run, UploadInspection } from "@/api/types";
import { fetchArtifactJson } from "@/lib/artifacts";

/** Start (or join) the inspect run of an upload. */
export function useStartInspect(datasetId: string) {
  const s = useProjectScope();
  return useMutation({
    mutationFn: (uploadId: string): Promise<Run> =>
      unwrap(
        api.POST("/v1/datasets/{dataset_id}/uploads/{upload_id}/inspect", {
          params: { path: { dataset_id: datasetId, upload_id: uploadId }, header: { "Idempotency-Key": newIdempotencyKey() } },
          headers: s.headers,
        }),
      ),
  });
}

/** The inspection, or `pending` while its run works (`409 NOT_READY`). */
export type InspectionState = { status: "pending" } | { status: "ready"; inspection: UploadInspection };

/** Poll delay after `n` pending answers: 0.5 s, growing by half each time, at most 5 s. */
export function inspectBackoff(n: number): number {
  return Math.min(5000, Math.round(500 * 1.5 ** Math.max(0, n - 1)));
}

/** Why an inspect run ended without an inspection: the run's own problem when it has one. */
export class InspectFailed extends Error {
  override name = "InspectFailed";
  constructor(
    message: string,
    readonly run: Run | null,
  ) {
    super(message);
  }
}

async function runProblem(runId: string, headers: Record<string, string>): Promise<InspectFailed> {
  try {
    const run = (await unwrap(api.GET("/v1/runs/{run_id}", { params: { path: { run_id: runId } }, headers })));
    const p = run.problem;
    const message = p ? (p.detail ? `${p.title}: ${p.detail}` : p.title) : `The inspect run ended (${run.state}) without a list of arrays.`;
    return new InspectFailed(message, run);
  } catch {
    return new InspectFailed("The inspect run ended without a list of arrays.", null);
  }
}

/**
 * `GET …/inspection`, polled with backoff while the run works. A run that ends without an
 * inspection (an unsafe or unreadable file) fails the query with the run's problem.
 */
export function useUploadInspection(datasetId: string, uploadId: string | null, runId: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ["p", s.key, "datasets", datasetId, "uploads", uploadId ?? "", "inspection"],
    enabled: Boolean(uploadId && runId),
    staleTime: Infinity,
    retry: false,
    queryFn: async ({ signal }): Promise<InspectionState> => {
      try {
        const inspection = await unwrap(
          api.GET("/v1/datasets/{dataset_id}/uploads/{upload_id}/inspection", {
            params: { path: { dataset_id: datasetId, upload_id: uploadId ?? "" } },
            headers: s.headers,
            signal,
          }),
        );
        return { status: "ready", inspection: inspection };
      } catch (err) {
        if (err instanceof ApiError && err.status === 409 && err.code === "NOT_READY") return { status: "pending" };
        if (err instanceof ApiError && err.status === 404 && runId) throw await runProblem(runId, s.headers);
        throw err;
      }
    },
    refetchInterval: (q) => (q.state.data?.status === "pending" ? inspectBackoff(q.state.dataUpdateCount) : false),
  });
}

/** `summary.json` of a tree version: the root summary plus one per node (`nodes`). */
export function useNodeSummaries(version: DatasetVersion, enabled: boolean) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...qk.datasetVersion(s.key, version.dataset_id, version.version), "view", "summary.json"],
    enabled: enabled && version.status === "ready",
    staleTime: Infinity,
    queryFn: async ({ signal }): Promise<Record<string, DataSummary>> => {
      const link = await datasetArtifactLink(version.dataset_id, version.version, "summary.json");
      const raw = await fetchArtifactJson(link, { signal });
      const nodes = typeof raw === "object" && raw !== null ? (raw as { nodes?: unknown }).nodes : null;
      if (typeof nodes !== "object" || nodes === null || Array.isArray(nodes)) return {};
      return nodes as Record<string, DataSummary>;
    },
  });
}
