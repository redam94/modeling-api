/**
 * The workspace: who is signed in, which tenants and projects they can act in, and the
 * selected one. Resolved once by {@link WorkspaceGate} and provided through context.
 */
import { createContext, useContext } from "react";

import { maxRole, type Grants } from "@/api/access";
import type { Me, MeProject, ProjectRole, Tenant } from "@/api/types";
import { scopeKey, type ProjectRef } from "@/stores/scope";

export interface ProjectChoice {
  tenant: Tenant;
  /** The tenant-wide role (`null` when the user only has project memberships). */
  tenantRole: ProjectRole | null;
  project: MeProject;
  /** Effective role in the project: the higher of the tenant-wide and project grants. */
  role: ProjectRole;
  key: string;
}

export interface Workspace {
  /** `session`: a signed-in v0.3 browser session; `none`: a server without auth (v0.2). */
  mode: "session" | "none";
  me: Me;
  operator: boolean;
  user: { id: string; name: string; email: string | null };
  choices: ProjectChoice[];
  current: ProjectChoice | null;
  grants: Grants;
  /** Session expiry (absolute), when the server reports it. */
  expiresAt: string | null;
}

const EPOCH = "1970-01-01T00:00:00Z";

/** The single tenant and project of a server without authentication (v0.2 profile). */
export function localMe(): Me {
  const tenant: Tenant = { id: "default", name: "Default", trust_tier: "self-hosted-trusted", status: "active", created_at: EPOCH };
  return {
    principal: { kind: "user", id: "usr_local", issuer: "https://localhost", subject: "local", name: "Local user", email: null },
    auth: "session",
    operator: false,
    tenants: [{ tenant, role: "admin", projects: [{ tenant_id: "default", project_id: "default", name: "Default", role: "admin" }] }],
    token: null,
  };
}

export function projectChoices(me: Me): ProjectChoice[] {
  const out: ProjectChoice[] = [];
  for (const t of me.tenants ?? []) {
    for (const p of t.projects ?? []) {
      out.push({
        tenant: t.tenant,
        tenantRole: t.role ?? null,
        project: p,
        role: maxRole(p.role, t.role) ?? p.role,
        key: scopeKey({ tenantId: p.tenant_id, projectId: p.project_id }),
      });
    }
  }
  return out;
}

/** The preferred project if the user can still act in it, else the first one. */
export function resolveActive(choices: ProjectChoice[], preferred: ProjectRef | null): ProjectChoice | null {
  if (preferred) {
    const key = scopeKey(preferred);
    const hit = choices.find((c) => c.key === key);
    if (hit) return hit;
  }
  return choices.find((c) => c.tenant.status !== "deleting") ?? choices[0] ?? null;
}

export function grantsFor(current: ProjectChoice | null, operator: boolean): Grants {
  return {
    projectRole: current?.role ?? null,
    tenantRole: current?.tenantRole ?? null,
    operator,
    tenantStatus: current?.tenant.status,
  };
}

export function userOf(me: Me): Workspace["user"] {
  const p = me.principal;
  if (p.kind === "user") return { id: p.id, name: p.name || p.email || p.subject, email: p.email ?? null };
  if (p.kind === "token") return { id: p.id, name: `Token ${p.prefix}`, email: null };
  return { id: p.id, name: p.service, email: null };
}

export function buildWorkspace(me: Me, mode: Workspace["mode"], preferred: ProjectRef | null, expiresAt: string | null = null): Workspace {
  const choices = projectChoices(me);
  const current = resolveActive(choices, preferred);
  const operator = Boolean(me.operator);
  return { mode, me, operator, user: userOf(me), choices, current, grants: grantsFor(current, operator), expiresAt };
}

/** Used when a page renders outside the gate (component tests): the local workspace. */
const FALLBACK: Workspace = buildWorkspace(localMe(), "none", null);

export const WorkspaceContext = createContext<Workspace | null>(null);

export function useWorkspace(): Workspace {
  return useContext(WorkspaceContext) ?? FALLBACK;
}

export function useGrants(): Grants {
  return useWorkspace().grants;
}
