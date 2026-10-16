/**
 * MSW emulation of the v0.3 multi-tenant API (`mapi_spec.api.M2_ENDPOINTS`) for mock mode,
 * the Playwright smoke suite and component tests: the BFF session, `/v1/me`, tenants,
 * projects, members, tokens, usage and the operator endpoints, plus the checks a v0.3 server
 * applies to every project-scoped request (session, project header, role, CSRF header).
 *
 * The signed-in persona survives page loads (localStorage `mapi.mock.session`); everything
 * else lives in memory per page. With no stored value the persona is Ana, an organisation
 * admin and platform operator, so v0.2 smoke tests keep working unchanged.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type {
  AdminUsageReport,
  ApiToken,
  AuditEvent,
  CostTotals,
  ExecutorHealth,
  KillSwitches,
  Me,
  Membership,
  Problem,
  ProjectRole,
  QuotaLimitStatus,
  QuotaPolicy,
  Tenant,
  UsageReport,
} from "@/api/types";
import { env } from "@/env";

import { PRICE_VERSION, T0 } from "./fixtures";

const API = env.apiBase;
const AUTH = env.authBase;
export const MOCK_SESSION_KEY = "mapi.mock.session";
const ROLES: ProjectRole[] = ["viewer", "editor", "runner", "admin"];

// ---------------------------------------------------------------------------- personas

export const ACME: Tenant = { id: "acme", name: "Acme Retail", trust_tier: "verified", status: "active", data_residency: "eu", created_at: "2026-09-01T08:00:00Z" };
export const GLOBEX: Tenant = { id: "globex", name: "Globex Labs", trust_tier: "public", status: "active", data_residency: "us", created_at: "2026-09-20T08:00:00Z" };

interface Persona {
  id: string;
  name: string;
  email: string;
  operator: boolean;
  /** tenant → tenant-wide role (null: project memberships only). */
  tenants: Record<string, ProjectRole | null>;
  /** `tenant/project` → project role. */
  projects: Record<string, ProjectRole>;
}

export const PERSONAS: Record<string, Persona> = {
  ana: {
    id: "usr_01jaf3a9b8c7d6e5f4g3h2j1k0",
    name: "Ana Analyst",
    email: "ana@acme.example",
    operator: true,
    tenants: { acme: "admin", globex: null },
    projects: { "acme/growth": "admin", "acme/retail": "admin", "globex/sandbox": "runner" },
  },
  rui: {
    id: "usr_01jaf3b9c8d7e6f5g4h3j2k1m0",
    name: "Rui Runner",
    email: "rui@acme.example",
    operator: false,
    tenants: { acme: null },
    projects: { "acme/growth": "runner" },
  },
  vic: {
    id: "usr_01jaf3c9d8e7f6g5h4j3k2m1n0",
    name: "Vic Viewer",
    email: "vic@acme.example",
    operator: false,
    tenants: { acme: null },
    projects: { "acme/growth": "viewer" },
  },
};

export const PROJECT_NAMES: Record<string, string> = {
  "acme/growth": "Growth",
  "acme/retail": "Retail demand",
  "globex/sandbox": "Sandbox",
};

/** The active persona id, or `null` when signed out. */
export function mockPersonaId(): string | null {
  let v: string | null = null;
  try {
    v = localStorage.getItem(MOCK_SESSION_KEY);
  } catch {
    v = null;
  }
  if (v === "signed-out") return null;
  return v && PERSONAS[v] ? v : "ana";
}

export function setMockPersona(id: string | null): void {
  try {
    localStorage.setItem(MOCK_SESSION_KEY, id ?? "signed-out");
  } catch {
    // storage unavailable: the persona falls back to the default
  }
}

/** Projects locked to Guided mode (`Project.guided_locked`): localStorage `mapi.mock.guidedLocked`, comma separated. */
export const MOCK_GUIDED_LOCK_KEY = "mapi.mock.guidedLocked";

export function mockGuidedLocked(): Set<string> {
  try {
    return new Set((localStorage.getItem(MOCK_GUIDED_LOCK_KEY) ?? "").split(",").map((s) => s.trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

/** The mock session's CSRF token (what `/auth/session` hands out for this persona). */
export function mockCsrfToken(p: Pick<Persona, "id">): string {
  return `csrf-${p.id}`;
}

function persona(): Persona | null {
  const id = mockPersonaId();
  return id ? (PERSONAS[id] ?? null) : null;
}

function rank(r: ProjectRole | null | undefined): number {
  return r ? ROLES.indexOf(r) : -1;
}

function projectRole(p: Persona, key: string): ProjectRole | null {
  const tenant = key.split("/")[0] ?? "";
  const a = p.projects[key] ?? null;
  const b = p.tenants[tenant] ?? null;
  return rank(a) >= rank(b) ? a : b;
}

// ---------------------------------------------------------------------------- state

interface TenancyState {
  tenants: Tenant[];
  projects: Map<string, { id: string; tenant_id: string; name: string; description: string | null; created_at: string }>;
  members: Membership[];
  tokens: ApiToken[];
  quotas: QuotaPolicy[];
  killSwitches: KillSwitches;
  executors: ExecutorHealth[];
  audit: AuditEvent[];
}

function membership(principal: Persona, tenant: string, project: string | null, role: ProjectRole): Membership {
  return { tenant_id: tenant, project_id: project, principal_id: principal.id, principal_kind: "user", role, capabilities: [], email: principal.email, display_name: principal.name, created_at: T0 };
}

const TOKEN_ID = (n: number) => `tok_01jaf3r1s2t3v4w5x6y7z8a9${String(n).padStart(2, "0").replace(/\d/g, (d) => "abcdefghjk"[Number(d)] ?? "a")}`;

function audit(i: number, patch: Partial<AuditEvent>): AuditEvent {
  const ts = new Date(Date.parse("2026-10-15T09:00:00Z") - i * 37 * 60_000).toISOString().replace(/\.\d+Z$/, "Z");
  return {
    id: `aud_01jaf3${String(i).padStart(20, "0").replace(/\d/g, (d) => "0123456789"[Number(d)] ?? "0")}`,
    ts,
    tenant_id: "acme",
    project_id: "growth",
    actor_kind: "user",
    actor_id: PERSONAS.ana?.id ?? null,
    operator: false,
    action: "run.create",
    resource_kind: "run",
    resource_id: "run_01jaf3q2x8m9v7k5h4g3f2e1d0",
    outcome: "success",
    request_id: `req_${i}`,
    details: {},
    ...patch,
  };
}

export function createTenancyState(): TenancyState {
  const ana = PERSONAS.ana!;
  const rui = PERSONAS.rui!;
  const vic = PERSONAS.vic!;
  const actions: [AuditEvent["action"], Partial<AuditEvent>][] = [
    ["token.create", { resource_kind: "token", resource_id: TOKEN_ID(1) }],
    ["run.create", {}],
    ["quota.denied", { outcome: "denied", code: "QUOTA_EXCEEDED", details: { limit: "max_resource_class" } }],
    ["member.add", { resource_kind: "member", resource_id: rui.id }],
    ["auth.login", { resource_kind: "tenant", resource_id: "acme" }],
    ["kill_switch.update", { operator: true, tenant_id: null, project_id: null, resource_kind: "kill_switch", resource_id: null }],
    ["auth.login_failed", { outcome: "denied", code: "UNAUTHENTICATED", actor_id: null, resource_kind: "tenant" }],
    [
      "endpoint.promote_override",
      {
        resource_kind: "endpoint",
        resource_id: "ep_01jaf3e1n2d3p4o5i6n7t8a9b0",
        details: { alias: "geo-mmm-prod", revision: 3, model_version_id: "mv_01jaf3c1d2e3f4g5h6j7k8m9n0", gate: "needs_override", reason: "Coverage is fine on the last quarter; refit scheduled", failing_checks: ["holdout.coverage"] },
      },
    ],
    [
      "endpoint.promote",
      {
        outcome: "denied",
        code: "PROMOTION_BLOCKED",
        tenant_id: "globex",
        project_id: "sandbox",
        resource_kind: "endpoint",
        resource_id: null,
        details: { alias: "churn-prod", model_version_id: "mv_01jaf3g1h2j3k4m5n6p7q8r9s0", gate: "blocked", failing_checks: ["convergence.rhat", "holdout.mape"] },
      },
    ],
    ["token.auth_failed", { outcome: "denied", code: "UNAUTHENTICATED", actor_kind: "token", resource_kind: "token" }],
  ];
  return {
    tenants: [ACME, GLOBEX, { id: "initech", name: "Initech", trust_tier: "internal", status: "suspended", status_reason: "billing review", data_residency: "us", created_at: "2026-08-02T08:00:00Z" }],
    projects: new Map([
      ["acme/growth", { id: "growth", tenant_id: "acme", name: "Growth", description: "EU media mix models", created_at: "2026-09-01T08:00:00Z" }],
      ["acme/retail", { id: "retail", tenant_id: "acme", name: "Retail demand", description: null, created_at: "2026-09-03T08:00:00Z" }],
      ["globex/sandbox", { id: "sandbox", tenant_id: "globex", name: "Sandbox", description: null, created_at: "2026-09-20T08:00:00Z" }],
    ]),
    members: [
      membership(ana, "acme", null, "admin"),
      membership(rui, "acme", "growth", "runner"),
      membership(vic, "acme", "growth", "viewer"),
      { tenant_id: "acme", project_id: "growth", principal_id: "sa_01jaf3d9e8f7g6h5j4k3m2n1p0", principal_kind: "service_account", role: "runner", email: null, display_name: "nightly-refit", created_at: T0 },
      membership(ana, "globex", "sandbox", "runner"),
    ],
    tokens: [
      {
        id: TOKEN_ID(1),
        tenant_id: "acme",
        project_id: "growth",
        owner_id: ana.id,
        name: "budget-planner",
        prefix: "7k2m9q4x",
        display: "mapi_7k2m9q4x_…",
        scopes: ["score:geo-mmm-prod", "models:read"],
        status: "active",
        rate_limit_per_minute: 120,
        vcpu_seconds_budget: 36_000,
        budget_period: "month",
        created_at: "2026-10-01T09:30:00Z",
        expires_at: "2026-10-19T09:30:00Z",
        last_used_at: "2026-10-15T08:00:00Z",
      },
      {
        id: TOKEN_ID(2),
        tenant_id: "acme",
        project_id: "growth",
        owner_id: rui.id,
        name: "notebook",
        prefix: "p3d8w1zr",
        display: "mapi_p3d8w1zr_…",
        scopes: ["runs:create"],
        status: "revoked",
        budget_period: "month",
        created_at: "2026-09-10T09:30:00Z",
        expires_at: "2026-12-09T09:30:00Z",
        revoked_at: "2026-10-02T12:00:00Z",
      },
    ],
    quotas: [
      { scope: "global", subject_id: "*", limits: { concurrent_runs: 700, requests_per_minute: 6000 }, note: "Cloud Run running-execution budget (70% of 1,000)", updated_at: T0, updated_by: ana.id },
      { scope: "tenant", subject_id: "acme", limits: { vcpu_seconds_per_day: 72_000, concurrent_runs: 20, max_resource_class: "M", storage_bytes: 200 * 1024 ** 3 }, note: "beta customer", updated_at: T0, updated_by: ana.id },
    ],
    killSwitches: { untrusted_execution_enabled: true, global_spend_cap_micros: 500_000_000, spend_cap_currency: "USD", spend_cap_period: "day", reason: "beta launch cap", updated_at: "2026-10-14T10:00:00Z", updated_by: ana.id },
    executors: EXECUTORS.map((e) => ({ ...e })),
    audit: actions.concat(Array.from({ length: 140 }, () => ["run.create", {}] as [AuditEvent["action"], Partial<AuditEvent>])).map(([action, patch], i) => audit(i, { action, ...patch })),
  };
}

// ---------------------------------------------------------------------------- helpers

function problem(status: number, code: Problem["code"], title: string, detail?: string, headers: Record<string, string> = {}) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json", ...headers } },
  );
}

const page = <T,>(items: T[], next: string | null = null) => HttpResponse.json({ items, next_cursor: next });

const UNAUTH = () => problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");

function meFor(p: Persona, state: TenancyState): Me {
  const tenantIds = [...new Set([...Object.keys(p.tenants), ...Object.keys(p.projects).map((k) => k.split("/")[0] ?? "")])];
  return {
    principal: { kind: "user", id: p.id, issuer: "https://accounts.google.com", subject: p.id.slice(4), email: p.email, email_verified: true, name: p.name },
    auth: "session",
    operator: p.operator,
    tenants: tenantIds.flatMap((tid) => {
      const tenant = state.tenants.find((t) => t.id === tid);
      if (!tenant) return [];
      const tenantRole = p.tenants[tid] ?? null;
      const keys = [...state.projects.keys()].filter((k) => k.startsWith(`${tid}/`) && (tenantRole !== null || p.projects[k]));
      return [
        {
          tenant,
          role: tenantRole,
          projects: keys.map((k) => ({ tenant_id: tid, project_id: k.split("/")[1] ?? "", name: state.projects.get(k)?.name ?? k, role: projectRole(p, k) ?? "viewer" })),
        },
      ];
    }),
    token: null,
  };
}

const PROJECT_SCOPED = /^\/v1\/(?!me\b|tenants\b|admin\b|projects\b|catalog\b|schemas\b)/;
/** Projects whose v0.2 resources are empty in the mock (to show the switcher at work). */
const EMPTY_PROJECTS = new Set(["acme/retail", "globex/sandbox"]);
const LIST_RE = /^\/v1\/(datasets|specs|runs|models)$/;

function seeded(seed: string): () => number {
  let h = 2166136261;
  for (const c of seed) h = Math.imul(h ^ c.charCodeAt(0), 16777619);
  return () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return ((h ^= h >>> 16) >>> 0) / 4294967296;
  };
}

function totals(parts: CostTotals[]): CostTotals {
  return parts.reduce<CostTotals>(
    (a, b) => ({
      vcpu_seconds: (a.vcpu_seconds ?? 0) + (b.vcpu_seconds ?? 0),
      gib_seconds: (a.gib_seconds ?? 0) + (b.gib_seconds ?? 0),
      amount_micros: (a.amount_micros ?? 0) + (b.amount_micros ?? 0),
      runs: (a.runs ?? 0) + (b.runs ?? 0),
      attempts: (a.attempts ?? 0) + (b.attempts ?? 0),
      currency: "USD",
    }),
    { vcpu_seconds: 0, gib_seconds: 0, amount_micros: 0, runs: 0, attempts: 0, currency: "USD" },
  );
}

function dayTotals(rand: () => number, scale: number): CostTotals {
  const runs = Math.round(rand() * 6 * scale);
  const vcpu = runs * (600 + Math.round(rand() * 2400));
  return { runs, attempts: runs + (rand() > 0.8 ? 1 : 0), vcpu_seconds: vcpu, gib_seconds: vcpu * 4, amount_micros: Math.round(vcpu * 18 + vcpu * 4 * 2), currency: "USD" };
}

/** A deterministic usage report for one period and grouping. */
export function usageReport(tenantId: string, projectId: string | null, url: URL, quotas: QuotaPolicy[]): UsageReport {
  const period = url.searchParams.get("period") === "day" ? "day" : "month";
  const startParam = url.searchParams.get("start");
  const now = new Date();
  const anchor = startParam ? new Date(startParam) : now;
  const start = period === "month" ? new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), 1)) : new Date(Date.UTC(anchor.getUTCFullYear(), anchor.getUTCMonth(), anchor.getUTCDate()));
  const end = new Date(start);
  if (period === "month") end.setUTCMonth(end.getUTCMonth() + 1);
  else end.setUTCDate(end.getUTCDate() + 1);
  const scale = projectId === null ? 2.2 : EMPTY_PROJECTS.has(`${tenantId}/${projectId}`) ? 0.3 : 1;
  const days: [string, CostTotals][] = [];
  for (let t = start.getTime(); t < end.getTime() && t <= now.getTime(); t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    days.push([day, dayTotals(seeded(`${tenantId}/${projectId ?? "*"}/${day}`), scale)]);
  }
  const total = totals(days.map(([, v]) => v));
  const split = (keys: string[], weights: number[]): Record<string, CostTotals> => {
    const sum = weights.reduce((a, b) => a + b, 0);
    return Object.fromEntries(
      keys.map((k, i) => {
        const w = (weights[i] ?? 0) / sum;
        return [k, { runs: Math.round((total.runs ?? 0) * w), attempts: Math.round((total.attempts ?? 0) * w), vcpu_seconds: (total.vcpu_seconds ?? 0) * w, gib_seconds: (total.gib_seconds ?? 0) * w, amount_micros: Math.round((total.amount_micros ?? 0) * w), currency: "USD" }];
      }),
    );
  };
  const groupBy = url.searchParams.get("group_by") as UsageReport["group_by"];
  let groups: Record<string, CostTotals> = {};
  if (groupBy === "day") groups = Object.fromEntries(days);
  else if (groupBy === "resource_class") groups = split(["S", "M", "L"], [2, 6, 1]);
  else if (groupBy === "token") groups = split([TOKEN_ID(1), "session"], [3, 5]);
  else if (groupBy === "project") groups = split(["growth", "retail"], [7, 2]);
  else if (groupBy === "run") groups = split(["run_01jaf3q2x8m9v7k5h4g3f2e1a1", "run_01jaf3q2x8m9v7k5h4g3f2e1b2"], [2, 1]);
  const policy = quotas.find((q) => q.scope === "tenant" && q.subject_id === tenantId);
  const today = days.at(-1)?.[1].vcpu_seconds ?? 0;
  const tomorrow = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1)).toISOString().replace(/\.\d+Z$/, "Z");
  const quotaRows: QuotaLimitStatus[] = [];
  const add = (limit_name: QuotaLimitStatus["limit_name"], used: number, limit: number | null | undefined, resets_at: string | null = null) => {
    if (limit == null) return;
    quotaRows.push({ scope: "tenant", subject_id: tenantId, limit_name, used, limit, remaining: Math.max(0, limit - used), fraction_used: limit ? used / limit : null, resets_at });
  };
  add("vcpu_seconds_per_day", Math.max(today, (policy?.limits.vcpu_seconds_per_day ?? 0) * 0.93), policy?.limits.vcpu_seconds_per_day, tomorrow);
  add("concurrent_runs", 15, policy?.limits.concurrent_runs);
  add("storage_bytes", 42 * 1024 ** 3, policy?.limits.storage_bytes);
  quotaRows.push({ scope: "global", subject_id: "*", limit_name: "concurrent_runs", used: 212, limit: 700, remaining: 488, fraction_used: 212 / 700, resets_at: null });
  return {
    tenant_id: tenantId,
    project_id: projectId,
    period,
    start: start.toISOString().replace(/\.\d+Z$/, "Z"),
    end: end.toISOString().replace(/\.\d+Z$/, "Z"),
    totals: total,
    group_by: groupBy ?? null,
    groups,
    price_versions: [PRICE_VERSION],
    quotas: quotaRows,
  };
}

/** Each tenant's share of the mock platform's spend (Acme spends the most). */
const TENANT_WEIGHT: Record<string, number> = { acme: 1, globex: 0.35, initech: 0.08 };

/** `GET /v1/admin/usage`: per-tenant totals (biggest first) and spend against the cap. */
export function adminUsageReport(tenants: Tenant[], quotas: QuotaPolicy[], killSwitches: KillSwitches, url: URL): AdminUsageReport {
  const rows = tenants
    .map((t) => {
      const base = usageReport(t.id, null, url, quotas);
      const w = TENANT_WEIGHT[t.id] ?? 0.1;
      const b = base.totals;
      const runs = Math.max(1, Math.round((b.runs ?? 0) * w));
      const totals: CostTotals = {
        runs,
        attempts: runs + Math.max(0, (b.attempts ?? 0) - (b.runs ?? 0)),
        vcpu_seconds: (b.vcpu_seconds ?? 0) * w,
        gib_seconds: (b.gib_seconds ?? 0) * w,
        amount_micros: Math.round((b.amount_micros ?? 0) * w),
        currency: "USD",
      };
      return { tenant_id: t.id, name: t.name, trust_tier: t.trust_tier, totals };
    })
    .sort((a, b) => (b.totals.amount_micros ?? 0) - (a.totals.amount_micros ?? 0));
  const first = usageReport("acme", null, url, quotas);
  const capPeriod = killSwitches.spend_cap_period ?? "day";
  const now = new Date();
  const capStart = capPeriod === "month" ? new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)) : new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const capEnd = new Date(capStart);
  if (capPeriod === "month") capEnd.setUTCMonth(capEnd.getUTCMonth() + 1);
  else capEnd.setUTCDate(capEnd.getUTCDate() + 1);
  const iso = (d: Date) => d.toISOString().replace(/\.\d+Z$/, "Z");
  const cap = killSwitches.global_spend_cap_micros ?? null;
  const spent = 370_000_000; // 74 % of the default $500/day cap: inside the 70 % warning band
  return {
    period: first.period,
    start: first.start,
    end: first.end,
    totals: [totals(rows.map((r) => r.totals))],
    tenants: rows,
    price_versions: [PRICE_VERSION],
    spend_cap: {
      period: capPeriod,
      start: iso(capStart),
      end: iso(capEnd),
      currency: killSwitches.spend_cap_currency ?? "USD",
      spent_micros: spent,
      cap_micros: cap,
      fraction: cap ? spent / cap : null,
    },
  };
}

const EXECUTORS: ExecutorHealth[] = [
  {
    name: "cloudrun-jobs",
    enabled: true,
    status: "ok",
    caps: { isolation: 3, egress_enforced: true, dns_enforced: true, classes: ["S", "M", "L"], max_tasks: 10_000, regions: ["europe-west4", "us-central1"], checkpoint_storage: true },
    regions: ["europe-west4", "us-central1"],
    allows_public_tenants: true,
    conformance_passed: true,
    security_passed: true,
    shared_tiers_enabled: true,
    running_executions: 212,
    queued_runs: 14,
    submits_last_minute: 131,
    submit_rate_limit_per_minute: 170,
    throttled_last_hour: 3,
    failures_last_hour: 1,
    last_success_at: "2026-10-15T09:29:40Z",
    checked_at: "2026-10-15T09:30:00Z",
  },
  {
    name: "docker-gvisor",
    enabled: true,
    status: "degraded",
    caps: { isolation: 2, egress_enforced: true, dns_enforced: true, classes: ["S", "M"], max_tasks: 64, regions: ["local"], checkpoint_storage: true },
    allows_public_tenants: true,
    conformance_passed: true,
    security_passed: true,
    shared_tiers_enabled: true,
    running_executions: 3,
    queued_runs: 0,
    submits_last_minute: 2,
    failures_last_hour: 4,
    message: "2 of 3 hosts reachable",
    last_success_at: "2026-10-15T09:21:00Z",
    checked_at: "2026-10-15T09:30:00Z",
  },
  {
    name: "local-process",
    enabled: false,
    status: "disabled",
    caps: { isolation: 1, classes: ["S", "M", "L"], max_tasks: 8, regions: ["local"] },
    allows_public_tenants: false,
    conformance_passed: false,
    conformance: {
      executor: "local-process",
      started_at: "2026-10-15T02:00:00Z",
      results: [
        { case_id: "submit", outcome: "passed" },
        { case_id: "oom_classification", outcome: "failed", problems: ["exit 137 classified as user_code_error"] },
      ],
    },
    message: "refused in multi-tenant profiles",
    checked_at: "2026-10-15T09:30:00Z",
  },
];

let tokenSeq = 10;

function newToken(): { prefix: string; secret: string } {
  const alphabet = "0123456789abcdefghjkmnpqrstvwxyz";
  const pick = (n: number) => Array.from({ length: n }, () => alphabet[Math.floor(Math.random() * 32)] ?? "0").join("");
  const prefix = pick(8);
  return { prefix, secret: `mapi_${prefix}_${pick(39)}` };
}

// ---------------------------------------------------------------------------- handlers

export function createTenancyHandlers(state: TenancyState = createTenancyState()): HttpHandler[] {
  const tenantRole = (p: Persona, t: string) => p.tenants[t] ?? null;
  const needOperator = () => {
    const p = persona();
    if (!p) return UNAUTH();
    if (!p.operator) return problem(403, "FORBIDDEN", "Not allowed", "platform operators only");
    return null;
  };
  return [
    // ------------------------------------------------------------------ session (BFF)
    http.get(`${AUTH}/auth/session`, () => {
      const p = persona();
      if (!p) return HttpResponse.json({ authenticated: false, operator: false, login_url: "/auth/login" });
      return HttpResponse.json(
        {
        authenticated: true,
        principal: { kind: "user", id: p.id, issuer: "https://accounts.google.com", subject: p.id.slice(4), email: p.email, email_verified: true, name: p.name },
        operator: p.operator,
        expires_at: new Date(Date.now() + 2 * 3600_000).toISOString(),
        login_url: "/auth/login",
        },
        { headers: { "X-CSRF-Token": mockCsrfToken(p) } },
      );
    }),
    http.post(`${AUTH}/auth/logout`, () => {
      setMockPersona(null);
      return new HttpResponse(null, { status: 204 });
    }),
    // The mock identity provider's "sign in as" (see mocks/dev-idp.tsx).
    http.post(`${AUTH}/__mock/auth/login`, async ({ request }) => {
      const body = (await request.json()) as { persona?: string };
      if (!body.persona || !PERSONAS[body.persona]) return problem(422, "SCHEMA_INVALID", "Invalid request", "unknown persona");
      setMockPersona(body.persona);
      return new HttpResponse(null, { status: 204 });
    }),

    // ------------------------------------------------------------------ the v0.3 checks on every /v1 route
    http.all(`${API}/v1/*`, ({ request }) => {
      const p = persona();
      if (!p) return UNAUTH();
      const url = new URL(request.url);
      const header = request.headers.get("X-Mapi-Project");
      if (!header || !PROJECT_SCOPED.test(url.pathname)) return undefined;
      // A v0.3 client sends the CSRF header on every mutation.
      if (request.method !== "GET" && request.headers.get("X-CSRF-Token") !== mockCsrfToken(p)) {
        return problem(403, "FORBIDDEN", "Not allowed", "missing CSRF header");
      }
      const role = projectRole(p, header);
      if (role === null || !state.projects.has(header)) return problem(404, "NOT_FOUND", "Not found", "no such project");
      if (state.tenants.find((t) => t.id === header.split("/")[0])?.status === "suspended" && request.method !== "GET") {
        return problem(403, "FORBIDDEN", "Not allowed", "the tenant is suspended");
      }
      const tokenRoute = url.pathname.startsWith("/v1/tokens");
      if (request.method !== "GET" && !tokenRoute && role === "viewer" && !/:validate$/.test(url.pathname)) {
        return problem(403, "FORBIDDEN", "Not allowed", "the viewer role cannot change or run anything here");
      }
      if (EMPTY_PROJECTS.has(header) && request.method === "GET" && LIST_RE.test(url.pathname)) return page([]);
      return undefined;
    }),

    // ------------------------------------------------------------------ identity
    http.get(`${API}/v1/me`, () => {
      const p = persona();
      return p ? HttpResponse.json(meFor(p, state)) : UNAUTH();
    }),
    http.get(`${API}/v1/tenants/:tenant/projects`, ({ params }) =>
      page(
        [...state.projects.values()]
          .filter((x) => x.tenant_id === params.tenant)
          .map((x) => ({ ...x, guided_locked: mockGuidedLocked().has(`${x.tenant_id}/${x.id}`) })),
      ),
    ),
    http.post(`${API}/v1/tenants/:tenant/projects`, async ({ params, request }) => {
      const p = persona()!;
      const tenant = String(params.tenant);
      if (rank(tenantRole(p, tenant)) < rank("admin")) return problem(403, "FORBIDDEN", "Not allowed", "only tenant admins create projects");
      const body = (await request.json()) as { id: string; name: string; description?: string };
      const key = `${tenant}/${body.id}`;
      if (state.projects.has(key)) return problem(409, "CONFLICT", "Conflict", "a project with this id exists");
      const created = { id: body.id, tenant_id: tenant, name: body.name, description: body.description ?? null, created_at: new Date().toISOString() };
      state.projects.set(key, created);
      PROJECT_NAMES[key] = body.name;
      return HttpResponse.json(created, { status: 201 });
    }),
    http.patch(`${API}/v1/tenants/:tenant/projects/:project`, async ({ params, request }) => {
      const p = persona()!;
      const key = `${String(params.tenant)}/${String(params.project)}`;
      const row = state.projects.get(key);
      if (!row) return problem(404, "NOT_FOUND", "Not found", "no such project");
      if (rank(projectRole(p, key)) < rank("admin")) return problem(403, "FORBIDDEN", "Not allowed", "only project admins change a project");
      const body = (await request.json()) as { name?: string | null; description?: string | null; guided_locked?: boolean | null };
      const next = { ...row, ...(body.name ? { name: body.name } : {}), ...("description" in body ? { description: body.description ?? null } : {}) };
      state.projects.set(key, next);
      if (typeof body.guided_locked === "boolean") {
        const locked = mockGuidedLocked();
        if (body.guided_locked) locked.add(key);
        else locked.delete(key);
        try {
          localStorage.setItem(MOCK_GUIDED_LOCK_KEY, [...locked].join(","));
        } catch {
          // storage unavailable: the lock lasts for this response only
        }
      }
      return HttpResponse.json({ ...next, guided_locked: mockGuidedLocked().has(key) });
    }),

    // ------------------------------------------------------------------ members
    http.get(`${API}/v1/tenants/:tenant/members`, ({ params }) => page(state.members.filter((m) => m.tenant_id === params.tenant && m.project_id == null))),
    http.get(`${API}/v1/tenants/:tenant/projects/:project/members`, ({ params }) =>
      page(state.members.filter((m) => m.tenant_id === params.tenant && (m.project_id === params.project || m.project_id == null))),
    ),
    ...(["", "/projects/:project"] as const).flatMap((suffix) => [
      http.post(`${API}/v1/tenants/:tenant${suffix}/members`, async ({ params, request }) => {
        const p = persona()!;
        const tenant = String(params.tenant);
        const project = params.project ? String(params.project) : null;
        const role = project ? projectRole(p, `${tenant}/${project}`) : tenantRole(p, tenant);
        if (rank(role) < rank("admin")) return problem(403, "FORBIDDEN", "Not allowed", "only admins add members");
        const body = (await request.json()) as { email?: string; principal_id?: string; role: ProjectRole };
        const id = body.principal_id ?? `usr_01jaf3z${String(state.members.length).padStart(19, "0")}`;
        if (state.members.some((m) => m.tenant_id === tenant && m.project_id === project && (m.principal_id === id || (body.email && m.email === body.email)))) {
          return problem(409, "CONFLICT", "Conflict", "already a member");
        }
        const m: Membership = { tenant_id: tenant, project_id: project, principal_id: id, principal_kind: "user", role: body.role, email: body.email ?? null, display_name: null, created_at: new Date().toISOString(), created_by: p.id };
        state.members.push(m);
        return HttpResponse.json(m, { status: 201 });
      }),
      http.patch(`${API}/v1/tenants/:tenant${suffix}/members/:principal`, async ({ params, request }) => {
        const project = params.project ? String(params.project) : null;
        const m = state.members.find((x) => x.tenant_id === params.tenant && x.project_id === project && x.principal_id === params.principal);
        if (!m) return problem(404, "NOT_FOUND", "Not found");
        const body = (await request.json()) as { role?: ProjectRole | null; capabilities?: Membership["capabilities"] | null };
        const admins = state.members.filter((x) => x.tenant_id === params.tenant && x.project_id === project && x.role === "admin");
        if (body.role && m.role === "admin" && body.role !== "admin" && admins.length <= 1) return problem(409, "CONFLICT", "Conflict", "the last admin cannot be demoted");
        if (body.role) m.role = body.role;
        // M3.5: grants on top of the role (`reviewer` approves validations).
        if (body.capabilities) m.capabilities = [...new Set(body.capabilities)];
        return HttpResponse.json(m);
      }),
      http.delete(`${API}/v1/tenants/:tenant${suffix}/members/:principal`, ({ params }) => {
        const project = params.project ? String(params.project) : null;
        const i = state.members.findIndex((x) => x.tenant_id === params.tenant && x.project_id === project && x.principal_id === params.principal);
        if (i < 0) return problem(404, "NOT_FOUND", "Not found");
        const admins = state.members.filter((x) => x.tenant_id === params.tenant && x.project_id === project && x.role === "admin");
        if (state.members[i]?.role === "admin" && admins.length <= 1) return problem(409, "CONFLICT", "Conflict", "the last admin cannot be removed");
        state.members.splice(i, 1);
        return new HttpResponse(null, { status: 204 });
      }),
    ]),

    // ------------------------------------------------------------------ tokens
    http.get(`${API}/v1/tokens`, ({ request }) => {
      const p = persona()!;
      const url = new URL(request.url);
      const [tenant, project] = (request.headers.get("X-Mapi-Project") ?? "acme/growth").split("/");
      const all = url.searchParams.get("all") === "true";
      const status = url.searchParams.get("status");
      if (all && rank(projectRole(p, `${tenant}/${project}`)) < rank("admin")) return problem(403, "FORBIDDEN", "Not allowed", "listing everyone's tokens needs manage on token");
      return page(state.tokens.filter((t) => t.tenant_id === tenant && t.project_id === project && (all || t.owner_id === p.id) && (!status || t.status === status)));
    }),
    http.post(`${API}/v1/tokens`, async ({ request }) => {
      const p = persona()!;
      const [tenant = "acme", project = "growth"] = (request.headers.get("X-Mapi-Project") ?? "acme/growth").split("/");
      const body = (await request.json()) as { name: string; scopes: string[]; expires_in_days?: number; rate_limit_per_minute?: number; vcpu_seconds_budget?: number; budget_period?: "day" | "month" };
      const { prefix, secret } = newToken();
      tokenSeq += 1;
      const now = new Date();
      const token: ApiToken = {
        id: TOKEN_ID(tokenSeq),
        tenant_id: tenant,
        project_id: project,
        owner_id: p.id,
        name: body.name,
        prefix,
        display: `mapi_${prefix}_…`,
        scopes: body.scopes,
        status: "active",
        rate_limit_per_minute: body.rate_limit_per_minute ?? null,
        vcpu_seconds_budget: body.vcpu_seconds_budget ?? null,
        budget_period: body.budget_period ?? "month",
        created_at: now.toISOString(),
        expires_at: new Date(now.getTime() + (body.expires_in_days ?? 90) * 86_400_000).toISOString(),
        last_used_at: null,
      };
      state.tokens.unshift(token);
      return HttpResponse.json({ token, secret }, { status: 201 });
    }),
    http.get(`${API}/v1/tokens/:id`, ({ params }) => {
      const t = state.tokens.find((x) => x.id === params.id);
      return t ? HttpResponse.json(t) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.delete(`${API}/v1/tokens/:id`, ({ params }) => {
      const t = state.tokens.find((x) => x.id === params.id);
      if (!t) return problem(404, "NOT_FOUND", "Not found");
      if (t.status === "active") Object.assign(t, { status: "revoked", revoked_at: new Date().toISOString() });
      return new HttpResponse(null, { status: 204 });
    }),

    // ------------------------------------------------------------------ usage
    http.get(`${API}/v1/usage`, ({ request }) => {
      const [tenant = "acme", project = "growth"] = (request.headers.get("X-Mapi-Project") ?? "acme/growth").split("/");
      return HttpResponse.json(usageReport(tenant, project, new URL(request.url), state.quotas));
    }),
    http.get(`${API}/v1/tenants/:tenant/usage`, ({ params, request }) => {
      const p = persona()!;
      if (rank(tenantRole(p, String(params.tenant))) < rank("admin")) return problem(403, "FORBIDDEN", "Not allowed", "tenant usage needs a tenant admin");
      return HttpResponse.json(usageReport(String(params.tenant), null, new URL(request.url), state.quotas));
    }),

    // ------------------------------------------------------------------ operators
    http.get(`${API}/v1/admin/tenants`, ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const url = new URL(request.url);
      const status = url.searchParams.get("status");
      const tier = url.searchParams.get("trust_tier");
      return page(state.tenants.filter((t) => (!status || t.status === status) && (!tier || t.trust_tier === tier)));
    }),
    http.post(`${API}/v1/admin/tenants`, async ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const body = (await request.json()) as Tenant;
      if (state.tenants.some((t) => t.id === body.id)) return problem(409, "CONFLICT", "Conflict", "a tenant with this id exists");
      const t: Tenant = { id: body.id, name: body.name, trust_tier: body.trust_tier ?? "verified", status: "active", data_residency: body.data_residency ?? "us", created_at: new Date().toISOString() };
      state.tenants.push(t);
      return HttpResponse.json(t, { status: 201 });
    }),
    http.patch(`${API}/v1/admin/tenants/:tenant`, async ({ params, request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const t = state.tenants.find((x) => x.id === params.tenant);
      if (!t) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as Partial<Tenant>;
      if (body.status && body.status !== "active" && !body.status_reason) return problem(422, "SCHEMA_INVALID", "Invalid request", "suspending or deleting a tenant needs a status_reason");
      Object.assign(t, body, { updated_at: new Date().toISOString() });
      return HttpResponse.json(t);
    }),
    http.get(`${API}/v1/admin/quotas`, ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const scope = new URL(request.url).searchParams.get("scope");
      return page(state.quotas.filter((q) => !scope || q.scope === scope));
    }),
    http.put(`${API}/v1/admin/quotas`, async ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const body = (await request.json()) as QuotaPolicy;
      const next: QuotaPolicy = { ...body, updated_at: new Date().toISOString(), updated_by: persona()!.id };
      const i = state.quotas.findIndex((q) => q.scope === body.scope && q.subject_id === body.subject_id);
      if (i >= 0) state.quotas[i] = next;
      else state.quotas.push(next);
      return HttpResponse.json(next);
    }),
    http.delete(`${API}/v1/admin/quotas/:scope/:subject`, ({ params }) => {
      const denied = needOperator();
      if (denied) return denied;
      const i = state.quotas.findIndex((q) => q.scope === params.scope && q.subject_id === params.subject);
      if (i < 0) return problem(404, "NOT_FOUND", "Not found");
      state.quotas.splice(i, 1);
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/v1/admin/kill-switches`, () => needOperator() ?? HttpResponse.json(state.killSwitches)),
    http.put(`${API}/v1/admin/kill-switches`, async ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const body = (await request.json()) as { untrusted_execution_enabled?: boolean; global_spend_cap_micros?: number; clear_spend_cap?: boolean; spend_cap_period?: "day" | "month"; reason: string };
      if (!body.reason || body.reason.length < 3) return problem(422, "SCHEMA_INVALID", "Invalid request", "reason is required");
      const ks = state.killSwitches;
      state.killSwitches = {
        ...ks,
        ...(body.untrusted_execution_enabled !== undefined ? { untrusted_execution_enabled: body.untrusted_execution_enabled } : {}),
        ...(body.global_spend_cap_micros !== undefined ? { global_spend_cap_micros: body.global_spend_cap_micros } : {}),
        ...(body.clear_spend_cap ? { global_spend_cap_micros: null } : {}),
        ...(body.spend_cap_period ? { spend_cap_period: body.spend_cap_period } : {}),
        reason: body.reason,
        updated_at: new Date().toISOString(),
        updated_by: persona()!.id,
      };
      return HttpResponse.json(state.killSwitches);
    }),
    http.get(`${API}/v1/admin/usage`, ({ request }) => needOperator() ?? HttpResponse.json(adminUsageReport(state.tenants, state.quotas, state.killSwitches, new URL(request.url)))),
    http.get(`${API}/v1/admin/executors`, () => needOperator() ?? page(state.executors)),
    http.put(`${API}/v1/admin/executors/:name`, async ({ request, params }) => {
      const denied = needOperator();
      if (denied) return denied;
      const body = (await request.json()) as { enabled: boolean; reason: string };
      if (!body.reason || body.reason.length < 3) return problem(422, "SCHEMA_INVALID", "Invalid request", "reason is required");
      const i = state.executors.findIndex((e) => e.name === params.name);
      if (i < 0) return problem(404, "NOT_FOUND", "Not found");
      const prev = state.executors[i]!;
      const next: ExecutorHealth = body.enabled
        ? { ...prev, enabled: true, status: "ok", message: null }
        : { ...prev, enabled: false, status: "disabled", message: `disabled by an operator: ${body.reason}` };
      state.executors[i] = next;
      return HttpResponse.json(next);
    }),
    http.get(`${API}/v1/admin/audit`, ({ request }) => {
      const denied = needOperator();
      if (denied) return denied;
      const url = new URL(request.url);
      const f = (k: string) => url.searchParams.get(k);
      const rows = state.audit.filter(
        (e) =>
          (!f("tenant_id") || e.tenant_id === f("tenant_id")) &&
          (!f("actor_id") || e.actor_id === f("actor_id")) &&
          (!f("action") || e.action === f("action")) &&
          (!f("outcome") || e.outcome === f("outcome")) &&
          (!f("since") || e.ts >= (f("since") ?? "")) &&
          (!f("until") || e.ts <= (f("until") ?? "")),
      );
      const limit = Number(f("limit") ?? 100);
      const offset = Number(f("cursor") ?? 0);
      const slice = rows.slice(offset, offset + limit);
      return page(slice, offset + limit < rows.length ? String(offset + limit) : null);
    }),
  ];
}
