/**
 * TanStack Query hooks for governance (M3.5): the check catalog, project and tenant policies
 * with their versions, validations of model versions, the reviewers' queue, decisions,
 * resubmission, cancel, deprecation and the evidence pack. Also who may review here.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { atLeast } from "@/api/access";
import { api, newIdempotencyKey, unwrap, unwrapVoid } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import { useWorkspace } from "@/features/auth/workspace";

import type {
  ApprovalCreate,
  EvidencePackRequest,
  GovernancePolicyCreate,
  GovernancePolicyUpdate,
  ModelValidation,
  PolicyScope,
  ValidationRequest,
  ValidationStatus,
} from "./types";

const LIMIT = 200;

export const gk = {
  checks: ["catalog", "governance-checks"] as const,
  policies: (p: string) => [...qk.project(p), "governance", "policies"] as const,
  tenantPolicies: (t: string) => ["tenants", t, "governance", "policies"] as const,
  policy: (p: string, scope: PolicyScope, id: string) => [...qk.project(p), "governance", "policy", scope, id] as const,
  versions: (p: string, scope: PolicyScope, id: string) => [...qk.project(p), "governance", "policy", scope, id, "versions"] as const,
  validations: (p: string) => [...qk.project(p), "validations"] as const,
  validation: (p: string, id: string) => [...qk.project(p), "validations", id] as const,
  modelValidations: (p: string, model: string, v: number) => [...qk.project(p), "validations", "model", model, v] as const,
};

export const ACTIVE_VALIDATION: ReadonlySet<ValidationStatus> = new Set(["checks_running"]);

// ------------------------------------------------------------------------- catalog

export function useGovernanceChecks() {
  const q = useQuery({
    queryKey: gk.checks,
    queryFn: ({ signal }) => unwrap(api.GET("/v1/catalog/governance-checks", { params: { query: { limit: LIMIT } }, signal })),
    staleTime: 10 * 60_000,
  });
  const map = useMemo(() => new Map((q.data?.items ?? []).map((c) => [c.id, c])), [q.data]);
  return { ...q, map };
}

// ------------------------------------------------------------------------- policies

/** The policies in force in the project (tenant-wide first) or only the project's own. */
export function usePolicies(scope: "all" | "project" = "all") {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...gk.policies(s.key), scope],
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/governance/policies", {
          params: { query: { limit: LIMIT, ...(scope === "project" ? { scope: "project" } : {}) } },
          headers: s.headers,
          signal,
        }),
      ),
  });
}

export function useTenantPolicies(enabled = true) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.tenantPolicies(s.tenantId),
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/tenants/{tenant_id}/governance/policies", { params: { path: { tenant_id: s.tenantId }, query: { limit: LIMIT } }, signal })),
    enabled: enabled && Boolean(s.tenantId),
  });
}

export function usePolicy(scope: PolicyScope, id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.policy(s.key, scope, id ?? ""),
    queryFn: ({ signal }) =>
      scope === "tenant"
        ? unwrap(api.GET("/v1/tenants/{tenant_id}/governance/policies/{policy_id}", { params: { path: { tenant_id: s.tenantId, policy_id: id ?? "" } }, signal }))
        : unwrap(api.GET("/v1/governance/policies/{policy_id}", { params: { path: { policy_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function usePolicyVersions(scope: PolicyScope, id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.versions(s.key, scope, id ?? ""),
    // Tenant policies are versioned the same way; the project route lists both kinds' versions.
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/governance/policies/{policy_id}/versions", { params: { path: { policy_id: id ?? "" }, query: { limit: LIMIT } }, headers: s.headers, signal })),
    enabled: Boolean(id),
  });
}

export function usePolicyMutations(scope: PolicyScope) {
  const qc = useQueryClient();
  const s = useProjectScope();
  const invalidate = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: gk.policies(s.key) }),
      qc.invalidateQueries({ queryKey: gk.tenantPolicies(s.tenantId) }),
      qc.invalidateQueries({ queryKey: [...qk.project(s.key), "governance", "policy"] }),
    ]);
  };
  const tenant = { tenant_id: s.tenantId };
  return {
    create: useMutation({
      mutationFn: (body: GovernancePolicyCreate) =>
        scope === "tenant"
          ? unwrap(api.POST("/v1/tenants/{tenant_id}/governance/policies", { params: { path: tenant }, body }))
          : unwrap(api.POST("/v1/governance/policies", { headers: s.headers, body })),
      onSuccess: invalidate,
    }),
    update: useMutation({
      mutationFn: ({ id, body }: { id: string; body: GovernancePolicyUpdate }) =>
        scope === "tenant"
          ? unwrap(api.PUT("/v1/tenants/{tenant_id}/governance/policies/{policy_id}", { params: { path: { ...tenant, policy_id: id } }, body }))
          : unwrap(api.PUT("/v1/governance/policies/{policy_id}", { params: { path: { policy_id: id } }, headers: s.headers, body })),
      onSuccess: invalidate,
    }),
    retire: useMutation({
      mutationFn: (id: string) =>
        scope === "tenant"
          ? unwrapVoid(api.DELETE("/v1/tenants/{tenant_id}/governance/policies/{policy_id}", { params: { path: { ...tenant, policy_id: id } } }))
          : unwrapVoid(api.DELETE("/v1/governance/policies/{policy_id}", { params: { path: { policy_id: id } }, headers: s.headers })),
      onSuccess: invalidate,
    }),
  };
}

// ------------------------------------------------------------------------- validations

export function useValidations(status?: ValidationStatus) {
  const s = useProjectScope();
  return useQuery({
    queryKey: [...gk.validations(s.key), "list", status ?? null],
    queryFn: ({ signal }) =>
      unwrap(api.GET("/v1/validations", { params: { query: { limit: LIMIT, ...(status ? { status } : {}) } }, headers: s.headers, signal })),
    refetchInterval: (q) => (q.state.data?.items.some((v) => ACTIVE_VALIDATION.has(v.status)) ? 5000 : false),
  });
}

export function useValidation(id: string | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.validation(s.key, id ?? ""),
    queryFn: ({ signal }) => unwrap(api.GET("/v1/validations/{validation_id}", { params: { path: { validation_id: id ?? "" } }, headers: s.headers, signal })),
    enabled: Boolean(id),
    refetchInterval: (q) => (q.state.data && ACTIVE_VALIDATION.has(q.state.data.status) ? 3000 : false),
  });
}

export function useModelValidations(modelId: string | undefined, version: number | undefined) {
  const s = useProjectScope();
  return useQuery({
    queryKey: gk.modelValidations(s.key, modelId ?? "", version ?? 0),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/models/{model_id}/versions/{version}/validations", {
          params: { path: { model_id: modelId ?? "", version: version ?? 0 }, query: { limit: 50 } },
          headers: s.headers,
          signal,
        }),
      ),
    enabled: Boolean(modelId) && Boolean(version),
    refetchInterval: (q) => (q.state.data?.items.some((v) => ACTIVE_VALIDATION.has(v.status)) ? 3000 : false),
  });
}

function useSetValidation() {
  const qc = useQueryClient();
  const s = useProjectScope();
  return (v: ModelValidation) => {
    qc.setQueryData(gk.validation(s.key, v.id), v);
    void qc.invalidateQueries({ queryKey: gk.validations(s.key) });
  };
}

export function useRequestValidation(modelId: string, version: number) {
  const s = useProjectScope();
  const set = useSetValidation();
  return useMutation({
    mutationFn: (body: ValidationRequest) =>
      unwrap(
        api.POST("/v1/models/{model_id}/versions/{version}/validations", {
          params: { path: { model_id: modelId, version }, header: { "Idempotency-Key": newIdempotencyKey() } },
          headers: s.headers,
          body,
        }),
      ),
    onSuccess: set,
  });
}

export function useValidationActions(id: string) {
  const s = useProjectScope();
  const set = useSetValidation();
  const path = { validation_id: id };
  return {
    decide: useMutation({
      mutationFn: (body: ApprovalCreate) => unwrap(api.POST("/v1/validations/{validation_id}/approvals", { params: { path }, headers: s.headers, body })),
      onSuccess: set,
    }),
    cancel: useMutation({
      mutationFn: (note: string | null) => unwrap(api.POST("/v1/validations/{validation_id}:cancel", { params: { path }, headers: s.headers, body: { note } })),
      onSuccess: set,
    }),
    resubmit: useMutation({
      mutationFn: (note: string | null) => unwrap(api.POST("/v1/validations/{validation_id}:resubmit", { params: { path }, headers: s.headers, body: { note } })),
      onSuccess: set,
    }),
    deprecate: useMutation({
      mutationFn: (note: string) => unwrap(api.POST("/v1/validations/{validation_id}:deprecate", { params: { path }, headers: s.headers, body: { note } })),
      onSuccess: set,
    }),
    exportPack: useMutation({
      mutationFn: (body: EvidencePackRequest) => unwrap(api.POST("/v1/validations/{validation_id}/evidence-pack", { params: { path }, headers: s.headers, body })),
    }),
  };
}

// ------------------------------------------------------------------------- reviewers

/**
 * May the signed-in user approve here? Project admins hold the reviewer capability implicitly;
 * editors and runners need it granted on a membership (project or tenant-wide). The server
 * decides every approval; this only hides and disables what would be refused.
 */
export function useReviewer(): { isReviewer: boolean; pending: boolean; userId: string } {
  const ws = useWorkspace();
  const role = ws.current?.role ?? null;
  const tenantId = ws.current?.tenant.id ?? "";
  const projectId = ws.current?.project.project_id ?? "";
  const admin = atLeast(role, "admin");
  const members = useQuery({
    queryKey: qk.members(tenantId, projectId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/v1/tenants/{tenant_id}/projects/{project_id}/members", {
          params: { path: { tenant_id: tenantId, project_id: projectId }, query: { limit: LIMIT } },
          signal,
        }),
      ),
    enabled: ws.mode === "session" && !admin && atLeast(role, "editor") && Boolean(tenantId && projectId),
    staleTime: 60_000,
  });
  const granted = (members.data?.items ?? []).some((m) => m.principal_id === ws.user.id && (m.capabilities ?? []).includes("reviewer"));
  return {
    isReviewer: ws.mode === "none" || admin || (atLeast(role, "editor") && granted),
    pending: members.isPending && members.fetchStatus !== "idle",
    userId: ws.user.id,
  };
}
