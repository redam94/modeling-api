/**
 * Client-side mirror of `mapi_spec.tenancy.AuthContext.allows` (without token scopes, which
 * never apply to a browser session). The matrix is generated from `PERMISSIONS`
 * (`access.gen.json`, written by `scripts/gen_openapi.py`), so the UI hides or disables what
 * the caller's role cannot do. The server still decides every request.
 */
import generated from "./access.gen.json";
import type { ProjectRole, ResourceKind, TenantStatus } from "./types";

export type Action = "read" | "create" | "update" | "delete" | "run" | "cancel" | "promote" | "manage" | "validate" | "approve";
type Level = "project" | "tenant" | "platform" | "internal";

interface Rule {
  min_role: ProjectRole | null;
  level: Level;
}

const PERMISSIONS = generated.permissions as Record<string, Rule>;

export const ROLES: readonly ProjectRole[] = generated.roles as ProjectRole[];
export const TOKEN_SCOPES: readonly string[] = generated.token_scopes;
export const DEFAULT_TOKEN_TTL_DAYS: number = generated.default_token_ttl_days;
export const MAX_TOKEN_TTL_DAYS: number = generated.max_token_ttl_days;

export const ROLE_META: Record<ProjectRole, { label: string; description: string }> = {
  viewer: { label: "Viewer", description: "Reads datasets, specs, runs, models and plots." },
  editor: { label: "Editor", description: "Viewer, plus uploads data and edits specs and plots." },
  runner: { label: "Runner", description: "Editor, plus starts runs and scores models (spends compute)." },
  admin: { label: "Admin", description: "Runner, plus manages members, endpoints and the project." },
};

export function roleRank(role: ProjectRole | null | undefined): number {
  return role ? ROLES.indexOf(role) : -1;
}

export function atLeast(role: ProjectRole | null | undefined, min: ProjectRole): boolean {
  return roleRank(role) >= roleRank(min);
}

/** The higher of two roles (a tenant-wide grant applies to every project). */
export function maxRole(a: ProjectRole | null | undefined, b: ProjectRole | null | undefined): ProjectRole | null {
  return roleRank(a) >= roleRank(b) ? (a ?? null) : (b ?? null);
}

export interface Grants {
  /** The effective role in the selected project (max of tenant-wide and project grants). */
  projectRole: ProjectRole | null;
  /** The tenant-wide role only. */
  tenantRole: ProjectRole | null;
  operator: boolean;
  tenantStatus?: TenantStatus | undefined;
}

/** The rule for `action` on `resource`; `undefined` means nobody may (deny by default). */
export function requiredRule(action: Action, resource: ResourceKind): Rule | undefined {
  return PERMISSIONS[`${resource}:${action}`];
}

/** May the caller do `action` on `resource`? */
export function can(grants: Grants, action: Action, resource: ResourceKind): boolean {
  const rule = requiredRule(action, resource);
  if (!rule) return false;
  if (rule.level === "internal") return false;
  if (rule.level === "platform") return grants.operator;
  if (grants.tenantStatus === "deleting") return false;
  if (grants.tenantStatus === "suspended" && action !== "read") return false;
  const role = rule.level === "tenant" ? grants.tenantRole : grants.projectRole;
  return rule.min_role !== null && atLeast(role, rule.min_role);
}

/** "runner" for `run` on `model`, `null` for operator-only or unknown permissions. */
export function minimumRole(action: Action, resource: ResourceKind): ProjectRole | null {
  return requiredRule(action, resource)?.min_role ?? null;
}
