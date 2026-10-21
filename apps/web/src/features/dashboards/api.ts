/**
 * TanStack Query hooks of the v0.16 dashboard routes for signed-in callers (`/v1/dashboards`):
 * the client portal's list, and the analyst's drafts, publishing, grants, share links,
 * readout review and refit requests.
 *
 * Every route is project-scoped: keys start with `["p", "<tenant>/<project>", "dashboards"]`
 * (`keys.ts`) and every request names the same project in `X-Mapi-Project`. The readout page
 * does not use this module (it reads through a `DashboardClient`, see `client.ts`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, ApiError, newIdempotencyKey, unwrap, unwrapVoid } from "@/api/client";
import { useProjectScope, type ProjectScope } from "@/api/queries";

import { memberClient, type MemberClientOptions } from "./client";
import { dk, type DashboardListFilters, type ReadoutFilters } from "./keys";
import {
  isAnalystDashboard,
  type Dashboard,
  type DashboardClient,
  type DashboardCreate,
  type DashboardRefitRequest,
  type DashboardShareCreate,
  type DashboardSpec,
  type ReadoutHeader,
} from "./types";

const LIMIT = 200;

/** The `DashboardClient` of a signed-in caller in the selected project (stable per project and dashboard). */
export function useMemberClient(dashboardId: string, opts: MemberClientOptions = {}): DashboardClient {
  const s = useProjectScope();
  const kind = opts.kind ?? "member";
  const spec = opts.spec ?? "draft";
  return useMemo(() => memberClient(s.key, dashboardId, { kind, spec }), [s.key, dashboardId, kind, spec]);
}

// ------------------------------------------------------------------------------ dashboards

/**
 * The previous page of the same project while a filter changes; never another project's
 * (the client portal stays mounted across a project switch, so its cards would show under
 * the new project's header).
 */
export function sameProject(prevKey: readonly unknown[] | undefined, projectKey: string): boolean {
  return prevKey?.[1] === projectKey;
}

/** The dashboards of the project (a client user: the published ones granted to them). */
export function useDashboards(f: DashboardListFilters = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.list(s.key, f),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/dashboards", { params: { query: { limit: LIMIT, ...(f.status ? { status: f.status } : {}) } }, headers: s.headers, signal })),
    placeholderData: (prev, prevQuery) => (sameProject(prevQuery?.queryKey, s.key) ? prev : undefined),
  });
}

/** Re-read one dashboard now (after a 412: the draft moved) and answer the fresh copy. */
export function useRefetchDashboard(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return async (): Promise<Dashboard | undefined> => {
    await qc.refetchQueries({ queryKey: dk.one(s.key, id), exact: true });
    return qc.getQueryData<Dashboard>(dk.one(s.key, id));
  };
}

function isStale(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 412 || e.code === "DASHBOARD_SPEC_STALE");
}

/** The analyst's view of one dashboard (with the draft and its `draft_etag`). */
export function useDashboard(id: string, opts: { enabled?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.one(s.key, id),
    queryFn: async ({ signal }): Promise<Dashboard> => {
      const d = await unwrap(api.GET("/v1/dashboards/{dashboard_id}", { params: { path: { dashboard_id: id } }, headers: s.headers, signal }));
      if (!isAnalystDashboard(d)) throw new ApiError(403, null, "only project members see the dashboard's draft");
      return d;
    },
    enabled: opts.enabled ?? true,
  });
}

function useInvalidateDashboard(s: ProjectScope) {
  const qc = useQueryClient();
  return async (id: string, next?: Dashboard) => {
    if (next) qc.setQueryData(dk.one(s.key, id), next);
    await Promise.all([
      qc.invalidateQueries({ queryKey: [...dk.all(s.key), "list"] }),
      qc.invalidateQueries({ queryKey: dk.one(s.key, id), predicate: (q) => q.queryKey.length > 4 }),
    ]);
  };
}

export function useCreateDashboard() {
  const s = useProjectScope();
  const invalidate = useInvalidateDashboard(s);
  return useMutation({
    mutationFn: (body: DashboardCreate) => unwrap(api.POST("/v1/dashboards", { headers: s.headers, body })),
    onSuccess: (d) => invalidate(d.id, d),
  });
}

/** Replace the draft. `etag` is the `draft_etag` you read (412 `DASHBOARD_SPEC_STALE` if it moved). */
export function useUpdateDraft(id: string) {
  const s = useProjectScope();
  const invalidate = useInvalidateDashboard(s);
  return useMutation({
    mutationFn: ({ spec, etag }: { spec: DashboardSpec; etag: string }) =>
      unwrap(api.PUT("/v1/dashboards/{dashboard_id}/draft", { params: { path: { dashboard_id: id } }, headers: { ...s.headers, "If-Match": etag }, body: { spec } })),
    onSuccess: (d) => invalidate(id, d),
    // the draft moved: read the new one, so "Load the new draft" loads it
    onError: (e) => (isStale(e) ? invalidate(id) : undefined),
  });
}

/** Whether the draft can be published now (`etag` keys the answer to the draft it checked). */
export function useValidateDashboard(id: string, etag: string | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.validation(s.key, id, etag ?? ""),
    queryFn: ({ signal }) => unwrap(api.POST("/v1/dashboards/{dashboard_id}:validate", { params: { path: { dashboard_id: id } }, headers: s.headers, signal })),
    enabled: etag !== null,
  });
}

/** Publish the draft of `etag` (the one the dialog checked; 412 when another editor saved since). */
export function usePublishDashboard(id: string) {
  const s = useProjectScope();
  const invalidate = useInvalidateDashboard(s);
  return useMutation({
    mutationFn: (etag: string) =>
      unwrap(api.POST("/v1/dashboards/{dashboard_id}:publish", { params: { path: { dashboard_id: id } }, headers: { ...s.headers, "If-Match": etag } })),
    onSuccess: (d) => invalidate(id, d),
    onError: (e) => (isStale(e) ? invalidate(id) : undefined),
  });
}

export function useArchiveDashboard(id: string) {
  const s = useProjectScope();
  const invalidate = useInvalidateDashboard(s);
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/dashboards/{dashboard_id}:archive", { params: { path: { dashboard_id: id } }, headers: s.headers })),
    onSuccess: (d) => invalidate(id, d),
  });
}

/** "Refresh now" (editor): a new readout from the latest data (`202` with its header). */
export function useRefreshDashboard(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (idemKey?: string) =>
      unwrap(api.POST("/v1/dashboards/{dashboard_id}:refresh", { params: { path: { dashboard_id: id } }, headers: { ...s.headers, "Idempotency-Key": idemKey ?? newIdempotencyKey() } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.one(s.key, id) }),
  });
}

export function useDashboardRevisions(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.revisions(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/dashboards/{dashboard_id}/revisions", { params: { path: { dashboard_id: id }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

// ------------------------------------------------------------------------------ grants

export function useDashboardGrants(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.grants(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/dashboards/{dashboard_id}/grants", { params: { path: { dashboard_id: id }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

/** Grant the dashboard to a client user of the project (editors and admins; idempotent). */
export function usePutGrant(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      unwrap(api.PUT("/v1/dashboards/{dashboard_id}/grants/{user_id}", { params: { path: { dashboard_id: id, user_id: userId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.grants(s.key, id) }),
  });
}

export function useDeleteGrant(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) =>
      unwrapVoid(api.DELETE("/v1/dashboards/{dashboard_id}/grants/{user_id}", { params: { path: { dashboard_id: id, user_id: userId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.grants(s.key, id) }),
  });
}

// ------------------------------------------------------------------------------ share links

export function useDashboardShares(id: string) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.shares(s.key, id),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/dashboards/{dashboard_id}/shares", { params: { path: { dashboard_id: id }, query: { limit: LIMIT } }, headers: s.headers, signal })),
  });
}

/**
 * Create a share link. The answer carries the token once (`token`, `url_fragment`): show it,
 * let the user copy it, and never put it in the query cache.
 */
export function useCreateShare(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: DashboardShareCreate) =>
      unwrap(api.POST("/v1/dashboards/{dashboard_id}/shares", { params: { path: { dashboard_id: id } }, headers: s.headers, body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.shares(s.key, id) }),
  });
}

export function useRevokeShare(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (shareId: string) =>
      unwrap(api.POST("/v1/dashboards/{dashboard_id}/shares/{share_id}:revoke", { params: { path: { dashboard_id: id, share_id: shareId } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.shares(s.key, id) }),
  });
}

/** The share URL for the app origin: `<origin>/s#<token>` (the token stays in the fragment). */
export function shareUrl(urlFragment: string, origin: string = window.location.origin): string {
  return `${origin.replace(/\/+$/, "")}${urlFragment.startsWith("/") ? "" : "/"}${urlFragment}`;
}

// ------------------------------------------------------------------------------ readouts (review)

/** Every readout the analyst sees (pending review, hidden, private and failed included). */
export function useAnalystReadouts(id: string, f: ReadoutFilters = {}, opts: { enabled?: boolean; poll?: boolean } = {}) {
  const s = useProjectScope();
  return useQuery({
    queryKey: dk.readoutList(s.key, id, f),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/readouts", {
          params: { path: { dashboard_id: id }, query: { limit: 50, ...(f.kind ? { kind: f.kind } : {}), ...(f.visibility ? { visibility: f.visibility } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: opts.enabled ?? true,
    refetchInterval: (q) => (opts.poll && q.state.data?.items?.some((r) => r.status !== "ready" && r.status !== "failed") ? 2_000 : false),
  });
}

function useReviewReadout(id: string, action: "publish" | "hide") {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (seq: number): Promise<ReadoutHeader> =>
      action === "publish"
        ? unwrap(api.POST("/v1/dashboards/{dashboard_id}/readouts/{seq}:publish", { params: { path: { dashboard_id: id, seq } }, headers: s.headers }))
        : unwrap(api.POST("/v1/dashboards/{dashboard_id}/readouts/{seq}:hide", { params: { path: { dashboard_id: id, seq } }, headers: s.headers })),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.one(s.key, id) }),
  });
}

/** Take a readout that waits for review (or was hidden) into the clients' history. */
export function usePublishReadout(id: string) {
  return useReviewReadout(id, "publish");
}

/** Remove a readout from the clients' view (the row stays for audit). */
export function useHideReadout(id: string) {
  return useReviewReadout(id, "hide");
}

// ------------------------------------------------------------------------------ refit requests

export function useRefitRequests(id: string, status: DashboardRefitRequest["status"] | null = null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...dk.refitRequests(s.key, id), status] as const,
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/refit-requests", {
          params: { path: { dashboard_id: id }, query: { limit: LIMIT, ...(status ? { status } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
  });
}

/** Approve (editor with the runner role): publishes the readout and starts the refit workflow as you. */
export function useApproveRefit(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ requestId, note, idemKey }: { requestId: string; note?: string | null; idemKey?: string }) =>
      unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/refit-requests/{request_id}:approve", {
          params: { path: { dashboard_id: id, request_id: requestId } },
          headers: { ...s.headers, "Idempotency-Key": idemKey ?? newIdempotencyKey() },
          body: { note: note ?? null },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.one(s.key, id) }),
  });
}

/** Reject: nothing runs. */
export function useRejectRefit(id: string) {
  const s = useProjectScope();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ requestId, note }: { requestId: string; note?: string | null }) =>
      unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/refit-requests/{request_id}:reject", {
          params: { path: { dashboard_id: id, request_id: requestId } },
          headers: s.headers,
          body: { note: note ?? null },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: dk.one(s.key, id) }),
  });
}
