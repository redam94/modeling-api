/**
 * TanStack Query hooks for the v0.4 integrations (`mapi_spec.api.M3_ENDPOINTS`): endpoints and
 * aliases, webhooks and their deliveries, saved plots, static renders and embed links.
 *
 * Every route here is project-scoped, so every key starts with `["p", "<tenant>/<project>"]`
 * (as in `queries.ts`) and every request names the same project in `X-Mapi-Project`.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, newIdempotencyKey, unwrap, unwrapVoid, unwrapWithStatus } from "./client";
import { useProjectScope, type ProjectScope } from "./queries";
import type {
  DeliveryStatus,
  Endpoint,
  EndpointPromote,
  EndpointRollback,
  EndpointScoreCall,
  PlotEmbedCreate,
  PlotRender,
  PlotRenderRequest,
  SavedPlotCreate,
  ScoreCallResult,
  WebhookCreate,
  WebhookUpdate,
} from "./types";

const LIMIT = 200;

/** Query keys of the integration resources (project-scoped). */
export const ik = {
  endpoints: (p: string) => ["p", p, "endpoints"] as const,
  endpoint: (p: string, alias: string) => ["p", p, "endpoints", alias] as const,
  endpointHistory: (p: string, alias: string) => ["p", p, "endpoints", alias, "history"] as const,
  endpointSchema: (p: string, alias: string, revision: number) => ["p", p, "endpoints", alias, "schema", revision] as const,
  endpointSnippets: (p: string, alias: string, revision: number) => ["p", p, "endpoints", alias, "snippets", revision] as const,
  webhooks: (p: string) => ["p", p, "webhooks"] as const,
  webhook: (p: string, id: string) => ["p", p, "webhooks", id] as const,
  deliveries: (p: string, id: string, status: DeliveryStatus | null) => ["p", p, "webhooks", id, "deliveries", status] as const,
  savedPlots: (p: string) => ["p", p, "saved-plots"] as const,
  embeds: (p: string, plotId: string) => ["p", p, "saved-plots", plotId, "embeds"] as const,
  render: (p: string, runId: string) => ["p", p, "plot-renders", runId] as const,
};

// ------------------------------------------------------------------------- endpoints

export function useEndpoints() {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.endpoints(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/endpoints", { params: { query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

export function useEndpoint(alias: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.endpoint(s.key, alias),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/endpoints/{alias}", { params: { path: { alias } }, headers: s.headers, signal })),
  });
}

export function useEndpointHistory(alias: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.endpointHistory(s.key, alias),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/endpoints/{alias}/history", { params: { path: { alias }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

/** The frozen input contract and example payload of the endpoint's current revision. */
export function useEndpointSchema(alias: string, revision: number) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.endpointSchema(s.key, alias, revision),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/endpoints/{alias}/schema", { params: { path: { alias } }, headers: s.headers, signal })),
    staleTime: 5 * 60_000,
  });
}

/** Generated snippets (every language), rendered with the server's own base URL. */
export function useEndpointSnippets(alias: string, revision: number) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.endpointSnippets(s.key, alias, revision),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/endpoints/{alias}/snippets", { params: { path: { alias } }, headers: s.headers, signal })),
    staleTime: 5 * 60_000,
  });
}

function useInvalidateEndpoint(s: ProjectScope) {
  const qc = useQueryClient();
  return async (alias: string, next?: Endpoint) => {
    if (next) qc.setQueryData(ik.endpoint(s.key, alias), next);
    await Promise.all([
      qc.invalidateQueries({ queryKey: ik.endpoints(s.key), exact: true }),
      qc.invalidateQueries({ queryKey: ik.endpointHistory(s.key, alias) }),
    ]);
  };
}

/** `PUT /v1/endpoints/{alias}`: create the alias or promote another version through the gate. */
export function usePromoteEndpoint() {
  const s = useProjectScope();
  const invalidate = useInvalidateEndpoint(s);
  return useMutation({
    mutationFn: ({ alias, body }: { alias: string; body: EndpointPromote }) =>
      unwrap(api.PUT("/v1/endpoints/{alias}", { params: { path: { alias } }, headers: s.headers, body })),
    onSuccess: (ep) => invalidate(ep.alias, ep),
  });
}

export function useRollbackEndpoint(alias: string) {
  const s = useProjectScope();
  const invalidate = useInvalidateEndpoint(s);
  return useMutation({
    mutationFn: (body: EndpointRollback) =>
      unwrap(api.POST("/v1/endpoints/{alias}/rollback", { params: { path: { alias } }, headers: s.headers, body })),
    onSuccess: (ep) => invalidate(alias, ep),
  });
}

export function useDeleteEndpoint() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (alias: string) => unwrapVoid(api.DELETE("/v1/endpoints/{alias}", { params: { path: { alias } }, headers: s.headers })),
    onSuccess: (_v, alias) => {
      qc.removeQueries({ queryKey: ik.endpoint(s.key, alias) });
      return qc.invalidateQueries({ queryKey: ik.endpoints(s.key), exact: true });
    },
  });
}

/** `POST …/validate`: the static checks of a score call, without running it. */
export function useValidateEndpointCall(alias: string) {
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: EndpointScoreCall) =>
      unwrap(api.POST("/v1/endpoints/{alias}/validate", { params: { path: { alias } }, headers: s.headers, body })),
  });
}

/** `POST …/score` with `Prefer: wait` (200 with outputs when it finishes in time, else 202). */
export function useScoreEndpoint(alias: string) {
  const s = useProjectScope();
  return useMutation({
    mutationFn: async ({ body, waitSeconds = 30, key = newIdempotencyKey() }: { body: EndpointScoreCall; waitSeconds?: number; key?: string }) => {
      const { data, status } = await unwrapWithStatus<ScoreCallResult>(
        api.POST("/v1/endpoints/{alias}/score", {
          params: { path: { alias }, header: { "Idempotency-Key": key, Prefer: `wait=${Math.max(0, Math.min(60, waitSeconds))}` } },
          headers: s.headers,
          body,
        }),
      );
      return { result: data, status };
    },
  });
}

// ------------------------------------------------------------------------- webhooks

export function useWebhooks() {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.webhooks(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/webhooks", { params: { query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

export function useWebhook(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.webhook(s.key, id),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/webhooks/{webhook_id}", { params: { path: { webhook_id: id } }, headers: s.headers, signal })),
  });
}

export function useCreateWebhook() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: WebhookCreate) => unwrap(api.POST("/v1/webhooks", { headers: s.headers, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.webhooks(s.key), exact: true }),
  });
}

export function useUpdateWebhook(id: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: WebhookUpdate) =>
      unwrap(api.PATCH("/v1/webhooks/{webhook_id}", { params: { path: { webhook_id: id } }, headers: s.headers, body })),
    onSuccess: (wh) => {
      qc.setQueryData(ik.webhook(s.key, id), wh);
      return qc.invalidateQueries({ queryKey: ik.webhooks(s.key), exact: true });
    },
  });
}

export function useDeleteWebhook() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (id: string) => unwrapVoid(api.DELETE("/v1/webhooks/{webhook_id}", { params: { path: { webhook_id: id } }, headers: s.headers })),
    onSuccess: (_v, id) => {
      qc.removeQueries({ queryKey: ik.webhook(s.key, id) });
      return qc.invalidateQueries({ queryKey: ik.webhooks(s.key), exact: true });
    },
  });
}

/** A new signing secret (shown once); the old one keeps signing for 24 hours. */
export function useRotateWebhookSecret(id: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/webhooks/{webhook_id}:rotate-secret", { params: { path: { webhook_id: id } }, headers: s.headers })),
    onSuccess: (created) => qc.setQueryData(ik.webhook(s.key, id), created.webhook),
  });
}

export function useDeliveries(id: string, status: DeliveryStatus | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.deliveries(s.key, id, status),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/webhooks/{webhook_id}/deliveries", {
          params: { path: { webhook_id: id }, query: { limit: LIMIT, ...(status ? { status } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
    placeholderData: keepPreviousData,
    // Retries happen on the server's schedule; refresh while any is still in flight.
    refetchInterval: (q) => (q.state.data?.items.some((d) => d.status === "pending" || d.status === "retrying") ? 5000 : 30_000),
  });
}

export function useReplayDelivery(webhookId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (deliveryId: string) =>
      unwrap(
        api.POST("/v1/webhooks/{webhook_id}/deliveries/{delivery_id}:replay", {
          params: { path: { webhook_id: webhookId, delivery_id: deliveryId } },
          headers: s.headers,
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["p", s.key, "webhooks", webhookId, "deliveries"] }),
  });
}

// ------------------------------------------------------------------------- saved plots, renders, embeds

export function useSavedPlots() {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.savedPlots(s.key),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/plots", { params: { query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

export function useCreateSavedPlot() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: SavedPlotCreate) => unwrap(api.POST("/v1/plots", { headers: s.headers, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.savedPlots(s.key), exact: true }),
  });
}

export function useDeleteSavedPlot() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (plotId: string) => unwrapVoid(api.DELETE("/v1/plots/{plot_id}", { params: { path: { plot_id: plotId } }, headers: s.headers })),
    onSuccess: (_v, plotId) => {
      qc.removeQueries({ queryKey: ik.embeds(s.key, plotId) });
      return qc.invalidateQueries({ queryKey: ik.savedPlots(s.key), exact: true });
    },
  });
}

export function usePlotEmbeds(plotId: string, enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ik.embeds(s.key, plotId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/plots/{plot_id}/embeds", { params: { path: { plot_id: plotId }, query: { limit: LIMIT } }, headers: s.headers, signal })),
    enabled,
  });
}

export function useCreateEmbed(plotId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (body: PlotEmbedCreate) =>
      unwrap(api.POST("/v1/plots/{plot_id}/embeds", { params: { path: { plot_id: plotId } }, headers: s.headers, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.embeds(s.key, plotId) }),
  });
}

export function useRevokeEmbed(plotId: string) {
  const qc = useQueryClient();
  const s = useProjectScope();
  return useMutation({
    mutationFn: (embedId: string) =>
      unwrapVoid(api.DELETE("/v1/plots/{plot_id}/embeds/{embed_id}", { params: { path: { plot_id: plotId, embed_id: embedId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ik.embeds(s.key, plotId) }),
  });
}

/** `POST /v1/plots:render` (waits up to `waitSeconds`; `done: false` means poll the run). */
export async function startPlotRender(
  scope: ProjectScope,
  body: PlotRenderRequest,
  opts: { key: string; waitSeconds: number; signal?: AbortSignal },
): Promise<PlotRender> {
  const { data } = await unwrapWithStatus(
    api.POST("/v1/plots:render", {
      params: { header: { "Idempotency-Key": opts.key, Prefer: `wait=${Math.max(0, Math.min(60, opts.waitSeconds))}` } },
      headers: scope.headers,
      body,
      ...(opts.signal ? { signal: opts.signal } : {}),
    }),
  );
  return data;
}

export function getPlotRender(scope: ProjectScope, runId: string, signal?: AbortSignal): Promise<PlotRender> {
  return unwrap(api.GET("/v1/plots/renders/{run_id}", { params: { path: { run_id: runId } }, headers: scope.headers, ...(signal ? { signal } : {}) }));
}
