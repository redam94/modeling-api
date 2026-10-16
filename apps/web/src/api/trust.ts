/**
 * The trust lifecycle (`mapi_spec.trust`): public sign-up and e-mail verification (on the
 * `/auth` backend-for-frontend base), identity verification, tenant deletion with its grace
 * period, GDPR exports, and the operators' sign-up review queue.
 *
 * Tenant routes name the tenant in the path, so their keys start with `["tenants", <id>]`
 * (cleared with the tenant's other caches) and they never carry `X-Mapi-Project`.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, unwrap } from "./client";
import { qk } from "./queries";
import { authApi } from "./session";
import type {
  GdprExport,
  GdprExportRequest,
  SignupRequest,
  SignupReviewDecision,
  SignupReviewState,
  TenantDeletion,
  VerificationUpdate,
} from "./types";

/** `mapi_spec.trust.TERMS_VERSION`: the terms the sign-up page shows and the server expects. */
export const TERMS_VERSION = "2026-10";

/** `mapi_spec.trust` grace period before a scheduled deletion purges the tenant. */
export const DELETION_GRACE_DAYS = 7;

export const tk = {
  verification: (t: string) => ["tenants", t, "verification"] as const,
  deletion: (t: string) => ["tenants", t, "deletion"] as const,
  exports: (t: string) => ["tenants", t, "exports"] as const,
  export: (t: string, id: string) => ["tenants", t, "exports", id] as const,
  adminSignups: (review: SignupReviewState | null) => ["admin", "signups", review] as const,
};

// ------------------------------------------------------------------------- public sign-up

/** `POST /auth/signup`: the same `check_email` answer for new and known addresses. */
export function signup(body: SignupRequest) {
  return unwrap(authApi.POST("/auth/signup", { body }));
}

/** `POST /auth/verify-email`: consumes the single-use token and creates the workspace. */
export function verifyEmail(token: string) {
  return unwrap(authApi.POST("/auth/verify-email", { body: { token } }));
}

// ------------------------------------------------------------------------- tenant: verification, deletion, export

export function useTenantVerification(tenantId: string, enabled = true) {
  return useQuery({
    queryKey: tk.verification(tenantId),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/tenants/{tenant_id}/verification", { params: { path: { tenant_id: tenantId } }, signal })),
    enabled: enabled && Boolean(tenantId),
  });
}

/**
 * The tenant's latest deletion, or `null` when it never asked for one (the API answers 404).
 * Polled while the purge runs, so the progress per store moves on its own.
 */
export function useTenantDeletion(tenantId: string, enabled = true) {
  return useQuery({
    queryKey: tk.deletion(tenantId),
    queryFn: async ({ signal }): Promise<TenantDeletion | null> => {
      try {
        return await unwrap(api.GET("/v1/tenants/{tenant_id}/deletion", { params: { path: { tenant_id: tenantId } }, signal }));
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
    },
    enabled: enabled && Boolean(tenantId),
    refetchInterval: (q) => (q.state.data?.state === "deleting" ? 5000 : false),
  });
}

function useAfterDeletionChange(tenantId: string) {
  const qc = useQueryClient();
  return async (next: TenantDeletion) => {
    qc.setQueryData(tk.deletion(tenantId), next);
    // The tenant's status (suspended / active) comes with /v1/me.
    await qc.invalidateQueries({ queryKey: qk.me });
  };
}

export function useRequestTenantDeletion(tenantId: string) {
  const after = useAfterDeletionChange(tenantId);
  return useMutation({
    mutationFn: (body: { confirm: string; reason?: string | null }) =>
      unwrap(api.POST("/v1/tenants/{tenant_id}/deletion", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: after,
  });
}

export function useCancelTenantDeletion(tenantId: string) {
  const after = useAfterDeletionChange(tenantId);
  return useMutation({
    mutationFn: () => unwrap(api.DELETE("/v1/tenants/{tenant_id}/deletion", { params: { path: { tenant_id: tenantId } } })),
    onSuccess: after,
  });
}

export function useCreateTenantExport(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: GdprExportRequest) =>
      unwrap(api.POST("/v1/tenants/{tenant_id}/exports", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: (exp) => {
      qc.setQueryData(tk.export(tenantId, exp.id), exp);
      void qc.invalidateQueries({ queryKey: tk.exports(tenantId), exact: true });
    },
  });
}

/**
 * The tenant's newest export (`GET /v1/tenants/{id}/exports`, newest first), so every admin
 * in every browser finds the export that is being built or can be downloaded.
 */
export function useLatestTenantExport(tenantId: string) {
  return useQuery({
    queryKey: tk.exports(tenantId),
    queryFn: async ({ signal }) => {
      const page = await unwrap(api.GET("/v1/tenants/{tenant_id}/exports", { params: { path: { tenant_id: tenantId }, query: { limit: 1 } }, signal }));
      return page.items[0] ?? null;
    },
    enabled: Boolean(tenantId),
  });
}

/** An export, polled until its archive is ready (or failed); `download_url` lasts 15 minutes. */
export function useTenantExport(tenantId: string, exportId: string | null) {
  return useQuery({
    queryKey: tk.export(tenantId, exportId ?? ""),
    queryFn: ({ signal }): Promise<GdprExport> =>
      unwrap(api.GET("/v1/tenants/{tenant_id}/exports/{export_id}", { params: { path: { tenant_id: tenantId, export_id: exportId ?? "" } }, signal })),
    enabled: Boolean(tenantId && exportId),
    refetchInterval: (q) => (q.state.data && (q.state.data.state === "queued" || q.state.data.state === "running") ? 3000 : false),
    // The signed link is short-lived: never show a cached one for long.
    staleTime: 60_000,
  });
}

// ------------------------------------------------------------------------- operators

export function useAdminSignups(review: SignupReviewState | null) {
  return useQuery({
    queryKey: tk.adminSignups(review),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/admin/signups", { params: { query: { limit: 200, ...(review ? { review } : {}) } }, signal })),
    placeholderData: keepPreviousData,
  });
}

export function useReviewSignup() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ signupId, body }: { signupId: string; body: SignupReviewDecision }) =>
      unwrap(api.POST("/v1/admin/signups/{signup_id}/review", { params: { path: { signup_id: signupId } }, body })),
    onSuccess: () =>
      Promise.all([qc.invalidateQueries({ queryKey: ["admin", "signups"] }), qc.invalidateQueries({ queryKey: ["admin", "tenants"] })]),
  });
}

export function usePutTenantVerification(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: VerificationUpdate) =>
      unwrap(api.PUT("/v1/admin/tenants/{tenant_id}/verification", { params: { path: { tenant_id: tenantId } }, body })),
    onSuccess: (status) => qc.setQueryData(tk.verification(tenantId), status),
  });
}

export function useRetryTenantDeletion(tenantId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/v1/admin/tenants/{tenant_id}/deletion/retry", { params: { path: { tenant_id: tenantId } } })),
    onSuccess: (next) => qc.setQueryData(tk.deletion(tenantId), next),
  });
}
