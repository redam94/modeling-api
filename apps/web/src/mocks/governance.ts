/**
 * MSW emulation of the M3.5 governance routes for mock mode, the component tests and the smoke
 * suite: the check catalog, project and tenant policies with versions and retirement, model
 * validations with the status state machine (`mapi_spec.governance.TRANSITIONS`), the
 * reviewers' queue, second-person approval (reviewer capability; never the submitter), cancel,
 * resubmit, deprecate, the evidence pack, and the promotion gate's `NOT_VALIDATED` refusal.
 *
 * Check results are real: `workflows.gen.json` holds the evidence of two governed ARIMAX fits
 * (one passes, one fails) computed by the installed check runners. It is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { Membership, Problem, ProjectRole, Run } from "@/api/types";
import type {
  ApprovalCreate,
  CheckResult,
  GovernanceCheckDef,
  GovernancePolicy,
  GovernancePolicyCreate,
  GovernancePolicyUpdate,
  ModelValidation,
  PolicyCheck,
  ValidationEvent,
  ValidationRequest,
  ValidationStatus,
} from "@/features/governance/types";
import { env } from "@/env";

import { artifactLink, MODEL_ID, MV_ID, runDone } from "./fixtures";
import { mockPersonaId, PERSONAS } from "./tenancy";
import generated from "./workflows.gen.json";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

export const CHECKS = generated.checks as unknown as GovernanceCheckDef[];
const PASSED = generated.validation_passed as unknown as CheckResult[];
const FAILED = generated.validation_failed as unknown as CheckResult[];
export const PROJECT_POLICY_ID = (generated.policy as unknown as GovernancePolicy).id;
export const VALIDATION_AWAITING = "val_01jaf3m1n2p3q4r5s6t7v8w9x0";
export const VALIDATION_FAILED = "val_01jaf3m1n2p3q4r5s6t7v8w9y1";
const T0 = "2026-10-14T10:00:00Z";
const ROLES: ProjectRole[] = ["viewer", "editor", "runner", "admin"];

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Problem["errors"] = []) {
  return HttpResponse.json({ type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const clone = <T,>(v: T): T => structuredClone(v);

let seq = 0;
function newId(prefix: string): string {
  seq += 1;
  return `${prefix}_01jaf3z${`${Date.now().toString(36)}${seq.toString(36)}`.padStart(19, "0").slice(-19)}`;
}

// ---------------------------------------------------------------------------- state machine

const TRANSITIONS: Record<string, ValidationStatus> = {
  "draft:submit": "checks_running",
  "checks_running:checks_passed": "checks_passed",
  "checks_running:checks_failed": "checks_failed",
  "checks_running:cancel": "draft",
  "checks_passed:request_approval": "awaiting_approval",
  "checks_passed:validate": "validated",
  "checks_failed:resubmit": "checks_running",
  "checks_failed:reject": "rejected",
  "awaiting_approval:approve": "validated",
  "awaiting_approval:reject": "rejected",
  "rejected:resubmit": "checks_running",
  "validated:deprecate": "deprecated",
};

function move(v: ModelValidation, event: ValidationEvent, actor: string | null, note: string | null = null, at = new Date().toISOString()): ModelValidation | null {
  const to = TRANSITIONS[`${v.status}:${event}`];
  if (!to) return null;
  v.history = [...(v.history ?? []), { from_status: v.status, to_status: to, event, actor_id: actor, note, at }];
  v.status = to;
  v.updated_at = at;
  return v;
}

// ---------------------------------------------------------------------------- state

interface MockValidation {
  v: ModelValidation;
  /** When the running checks finish (ms epoch); `null` once they did. */
  finishesAt: number | null;
  /** The results the running checks will produce. */
  pending: CheckResult[] | null;
}

export interface GovernanceState {
  policies: Map<string, GovernancePolicy[]>;
  validations: Map<string, MockValidation>;
}

function rollup(results: CheckResult[], required: PolicyCheck[]): { passed: boolean; failing: string[] } {
  const failing: string[] = [];
  for (const c of required) {
    const r = results.find((x) => x.check === c.check);
    if ((c.severity ?? "blocking") === "blocking" && (!r || r.status === "fail" || r.status === "error")) failing.push(c.check);
  }
  return { passed: failing.length === 0, failing };
}

export function createGovernanceState(): GovernanceState {
  const ana = PERSONAS.ana!.id;
  const rui = PERSONAS.rui!.id;
  const tenantDefaults: GovernancePolicy[] = (generated.default_policies as unknown as GovernancePolicyCreate[]).map((p, i) => ({
    ...p,
    id: `pol_01jaf3h1j2k3m4n5p6q7r8s9d${i}`,
    tenant_id: "acme",
    project_id: null,
    scope: "tenant",
    version: 1,
    created_at: "2026-10-01T08:00:00Z",
    created_by: "system",
  }));
  const project = clone(generated.policy as unknown as GovernancePolicy);
  // Gating is switched on from the policy editor (the smoke suite does); other flows promote freely.
  const current: GovernancePolicy = { ...project, require_validated_for_promotion: false, created_by: ana };
  const v1: GovernancePolicy = { ...current, version: 1, require_approval: false, checks: current.checks.slice(0, 4), created_at: "2026-10-04T08:00:00Z" };
  const policies = new Map<string, GovernancePolicy[]>(tenantDefaults.map((p) => [p.id, [p]]));
  policies.set(current.id, [v1, current]);

  const required = current.checks;
  const base = (id: string, submittedBy: string, at: string): ModelValidation => ({
    id,
    tenant_id: "acme",
    project_id: "growth",
    model_id: MODEL_ID,
    version: 1,
    status: "draft",
    policies: [`${current.id}@${current.version}`],
    required_checks: clone(required),
    require_approval: true,
    approvals_required: 1,
    results: [],
    failing_checks: [],
    approvals: [],
    history: [],
    submitted_by: submittedBy,
    submitted_at: at,
    workflow_run_id: "run_01jaf3g1h2j3k4m5n6p7q8r9s0",
    updated_at: at,
  });
  const failed = base(VALIDATION_FAILED, ana, "2026-10-13T09:00:00Z");
  move(failed, "submit", ana, "First validation", "2026-10-13T09:00:00Z");
  failed.results = clone(FAILED);
  failed.failing_checks = rollup(FAILED, required).failing;
  move(failed, "checks_failed", "system", null, "2026-10-13T09:06:00Z");

  const awaiting = base(VALIDATION_AWAITING, rui, T0);
  move(awaiting, "submit", rui, "Q4 refresh with the price driver", T0);
  awaiting.results = clone(PASSED);
  move(awaiting, "checks_passed", "system", null, "2026-10-14T10:07:00Z");
  move(awaiting, "request_approval", "system", null, "2026-10-14T10:07:00Z");

  return {
    policies,
    validations: new Map([
      [failed.id, { v: failed, finishesAt: null, pending: null }],
      [awaiting.id, { v: awaiting, finishesAt: null, pending: null }],
    ]),
  };
}

/** The policies in force for the project (the latest active version of each). */
function inForce(state: GovernanceState): GovernancePolicy[] {
  return [...state.policies.values()].map((vs) => vs.at(-1)!).filter((p) => p.active !== false);
}

/** Advance validations whose checks have finished (the mock's governance run). */
function settle(state: GovernanceState, now = Date.now()): void {
  for (const m of state.validations.values()) {
    if (m.finishesAt === null || now < m.finishesAt || m.v.status !== "checks_running") continue;
    const results = m.pending ?? clone(PASSED);
    m.v.results = results.map((r) => ({ ...r, computed_at: new Date(m.finishesAt!).toISOString() }));
    const { passed, failing } = rollup(results, m.v.required_checks ?? []);
    m.v.failing_checks = failing;
    move(m.v, passed ? "checks_passed" : "checks_failed", "system");
    if (passed) move(m.v, m.v.require_approval ? "request_approval" : "validate", "system");
    m.finishesAt = null;
    m.pending = null;
  }
}

/** `409 NOT_VALIDATED` for a model version whose latest validation is not `validated`, when a policy requires it. */
export function notValidatedProblem(state: GovernanceState, modelVersionId: string): Response | null {
  settle(state);
  if (!inForce(state).some((p) => p.require_validated_for_promotion)) return null;
  if (modelVersionId !== MV_ID) return null;
  const latest = [...state.validations.values()].map((m) => m.v).filter((v) => v.model_id === MODEL_ID && v.version === 1).sort((a, b) => (b.submitted_at ?? "").localeCompare(a.submitted_at ?? ""))[0];
  if (latest?.status === "validated") return null;
  const status = (latest?.status ?? "not submitted").replace(/_/g, " ");
  const message = `The governance policy requires a validated model; this version is ${status}. Submit it for validation first.`;
  return problem(409, "NOT_VALIDATED", "The model version is not validated", message, [
    { code: "NOT_VALIDATED", pointer: "", message, severity: "error", context: { gate_code: "NOT_VALIDATED", check: null } },
  ]);
}

// ---------------------------------------------------------------------------- handlers

export function createGovernanceHandlers(
  state: GovernanceState,
  ctx: { members: () => Membership[]; artifacts: Map<string, { bytes: Uint8Array; type: string }> },
): HttpHandler[] {
  const me = () => {
    const id = mockPersonaId();
    return id ? (PERSONAS[id] ?? null) : null;
  };
  const role = (projectKey: string): ProjectRole | null => {
    const p = me();
    if (!p) return null;
    const a = p.projects[projectKey] ?? null;
    const b = p.tenants[projectKey.split("/")[0] ?? ""] ?? null;
    return ROLES.indexOf(a ?? "viewer") >= ROLES.indexOf(b ?? "viewer") ? (a ?? b) : b;
  };
  const isReviewer = (projectKey: string) => {
    const p = me();
    const r = role(projectKey);
    if (!p || !r) return false;
    if (r === "admin") return true;
    if (ROLES.indexOf(r) < ROLES.indexOf("editor")) return false;
    const [tenant, project] = projectKey.split("/");
    return ctx.members().some((m) => m.principal_id === p.id && m.tenant_id === tenant && (m.project_id === project || m.project_id == null) && (m.capabilities ?? []).includes("reviewer"));
  };
  const projectKey = (request: Request) => request.headers.get("X-Mapi-Project") ?? "acme/growth";
  const get = (id: string) => {
    settle(state);
    return state.validations.get(id) ?? null;
  };
  const policyCreate = (body: GovernancePolicyCreate, scope: "tenant" | "project", id: string, version: number): GovernancePolicy | Response => {
    if (!body.name?.trim()) return problem(422, "POLICY_INVALID", "Invalid policy", "a policy needs a name");
    if (!body.checks?.length) return problem(422, "POLICY_INVALID", "Invalid policy", "a policy needs at least one check");
    const seen = new Set<string>();
    for (const [i, c] of body.checks.entries()) {
      const def = CHECKS.find((x) => x.id === c.check);
      if (!def) return problem(422, "POLICY_INVALID", "Invalid policy", `unknown governance check '${c.check}'`, [{ code: "POLICY_INVALID", pointer: `/checks/${i}/check`, message: `unknown governance check '${c.check}'`, severity: "error", context: {} }]);
      if (seen.has(c.check)) return problem(422, "POLICY_INVALID", "Invalid policy", "a policy lists each check once");
      seen.add(c.check);
      const t = c.thresholds;
      if (t && t.fail !== t.warn && (def.direction === "min") === t.fail >= t.warn) {
        return problem(422, "POLICY_INVALID", "Invalid policy", `${c.check}: fail must be at least as strict as warn`, [
          { code: "POLICY_INVALID", pointer: `/checks/${i}/thresholds`, message: "fail must be at least as strict as warn", severity: "error", context: {} },
        ]);
      }
    }
    return {
      ...clone(body),
      id,
      version,
      scope,
      tenant_id: "acme",
      project_id: scope === "tenant" ? null : "growth",
      applies_to: body.applies_to ?? { families: [], models: [], templates: [] },
      created_at: new Date().toISOString(),
      created_by: me()?.id ?? null,
    };
  };
  const needAdmin = (scope: "tenant" | "project", key: string) => {
    const p = me();
    if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required");
    const r = scope === "tenant" ? (p.tenants[key.split("/")[0] ?? ""] ?? null) : role(key);
    return r === "admin" ? null : problem(403, "FORBIDDEN", "Not allowed", scope === "tenant" ? "only tenant admins manage tenant-wide policies" : "only project admins manage policies");
  };

  return [
    http.get(`${API}/v1/catalog/governance-checks`, () => page(CHECKS)),

    // ------------------------------------------------------------------ policies
    http.get(`${API}/v1/governance/policies`, ({ request }) => {
      const scope = new URL(request.url).searchParams.get("scope");
      const all = inForce(state).sort((a, b) => (a.scope === b.scope ? 0 : a.scope === "tenant" ? -1 : 1));
      return page(scope ? all.filter((p) => p.scope === scope) : all);
    }),
    http.get(`${API}/v1/tenants/:tenant/governance/policies`, () => page(inForce(state).filter((p) => p.scope === "tenant"))),
    ...(["/v1/governance/policies", "/v1/tenants/:tenant/governance/policies"] as const).flatMap((prefix) => {
      const scope = prefix.startsWith("/v1/tenants") ? ("tenant" as const) : ("project" as const);
      return [
        http.post(`${API}${prefix}`, async ({ request }) => {
          const denied = needAdmin(scope, projectKey(request));
          if (denied) return denied;
          const made = policyCreate((await request.json()) as GovernancePolicyCreate, scope, newId("pol"), 1);
          if (made instanceof Response) return made;
          state.policies.set(made.id, [made]);
          return HttpResponse.json(made, { status: 201 });
        }),
        http.get(`${API}${prefix}/:id`, ({ params }) => {
          const vs = state.policies.get(String(params.id));
          return vs ? HttpResponse.json(vs.at(-1)) : problem(404, "NOT_FOUND", "Not found", "no such policy");
        }),
        http.put(`${API}${prefix}/:id`, async ({ params, request }) => {
          const denied = needAdmin(scope, projectKey(request));
          if (denied) return denied;
          const vs = state.policies.get(String(params.id));
          if (!vs) return problem(404, "NOT_FOUND", "Not found");
          const body = (await request.json()) as GovernancePolicyUpdate;
          const last = vs.at(-1)!;
          if (body.expected_version != null && body.expected_version !== last.version) {
            return problem(409, "CONFLICT", "Conflict", `the policy is at version ${last.version}, not ${body.expected_version}; reload it`);
          }
          const { expected_version: _ignored, ...rest } = body;
          const made = policyCreate(rest, last.scope, last.id, last.version + 1);
          if (made instanceof Response) return made;
          vs.push(made);
          return HttpResponse.json(made);
        }),
        http.delete(`${API}${prefix}/:id`, ({ params, request }) => {
          const denied = needAdmin(scope, projectKey(request));
          if (denied) return denied;
          const vs = state.policies.get(String(params.id));
          if (!vs) return problem(404, "NOT_FOUND", "Not found");
          vs.push({ ...vs.at(-1)!, active: false, version: vs.at(-1)!.version + 1, created_at: new Date().toISOString() });
          return new HttpResponse(null, { status: 204 });
        }),
      ];
    }),
    http.get(`${API}/v1/governance/policies/:id/versions`, ({ params }) => {
      const vs = state.policies.get(String(params.id));
      return vs ? page([...vs].reverse()) : problem(404, "NOT_FOUND", "Not found");
    }),

    // ------------------------------------------------------------------ validations
    // The governance run that computed a validation's checks (`workflow_run_id`).
    http.get(`${API}/v1/runs/:id`, ({ params }) => {
      settle(state);
      const m = [...state.validations.values()].find((x) => x.v.workflow_run_id === params.id);
      if (!m) return undefined;
      const running = m.v.status === "checks_running";
      const run: Run = { ...structuredClone(runDone), id: String(params.id), kind: "governance", state: running ? "running" : "succeeded", model_version_id: MV_ID, output_model_version_id: null, labels: {} };
      return HttpResponse.json(run);
    }),
    http.get(`${API}/v1/validations`, ({ request }) => {
      settle(state);
      const status = new URL(request.url).searchParams.get("status");
      const all = [...state.validations.values()].map((m) => m.v).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
      return page(status ? all.filter((v) => v.status === status) : all);
    }),
    http.get(`${API}/v1/models/:model/versions/:v/validations`, ({ params }) => {
      settle(state);
      return page(
        [...state.validations.values()]
          .map((m) => m.v)
          .filter((v) => v.model_id === params.model && v.version === Number(params.v))
          .sort((a, b) => (b.submitted_at ?? "").localeCompare(a.submitted_at ?? "")),
      );
    }),
    http.post(`${API}/v1/models/:model/versions/:v/validations`, async ({ params, request }) => {
      const body = (await request.json()) as ValidationRequest;
      const policies = inForce(state).filter((p) => !body.policy_ids?.length || body.policy_ids.includes(p.id));
      const required = policies.flatMap((p) => p.checks).filter((c, i, all) => all.findIndex((x) => x.check === c.check) === i);
      const now = new Date().toISOString();
      const actor = me()?.id ?? null;
      const v: ModelValidation = {
        id: newId("val"),
        tenant_id: "acme",
        project_id: "growth",
        model_id: String(params.model),
        version: Number(params.v),
        status: "draft",
        policies: policies.map((p) => `${p.id}@${p.version}`),
        required_checks: required,
        require_approval: policies.some((p) => p.require_approval),
        approvals_required: Math.max(1, ...policies.map((p) => p.approvals_required ?? 1)),
        results: [],
        failing_checks: [],
        approvals: [],
        history: [],
        submitted_by: actor,
        submitted_at: now,
        workflow_run_id: newId("run"),
        updated_at: now,
      };
      move(v, "submit", actor, body.note ?? null, now);
      const available = new Map(PASSED.map((r) => [r.check, r]));
      const results = required.map(
        (c) =>
          available.get(c.check) ?? {
            check: c.check,
            status: "skipped" as const,
            severity: c.severity ?? "blocking",
            message_plain: "Not applicable: the model has no test period (add a data split and refit).",
            evidence: [],
            params: {},
          },
      );
      state.validations.set(v.id, { v, finishesAt: Date.now() + 2500, pending: results });
      return HttpResponse.json(v, { status: 202 });
    }),
    http.get(`${API}/v1/validations/:id`, ({ params }) => {
      const m = get(String(params.id));
      return m ? HttpResponse.json(m.v) : problem(404, "NOT_FOUND", "Not found", "no such validation");
    }),
    http.get(`${API}/v1/validations/:id/evidence`, ({ params }) => {
      const m = get(String(params.id));
      return m ? page(m.v.results ?? []) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(`${API}/v1/validations/:id/approvals`, async ({ params, request }) => {
      const m = get(String(params.id));
      if (!m) return problem(404, "NOT_FOUND", "Not found");
      const p = me();
      if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required");
      if (!isReviewer(projectKey(request))) return problem(403, "FORBIDDEN", "Not allowed", "approvals need the reviewer capability (or project admin)");
      if (m.v.submitted_by === p.id) return problem(403, "SELF_APPROVAL_FORBIDDEN", "You cannot approve your own submission", "a second person must review it");
      const body = (await request.json()) as ApprovalCreate;
      if (body.decision === "reject" && !body.comment?.trim()) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "a rejection needs a comment", [{ code: "SCHEMA_INVALID", pointer: "/comment", message: "a rejection needs a comment", severity: "error", context: {} }]);
      }
      if (m.v.status !== "awaiting_approval" && !(body.decision === "reject" && m.v.status === "checks_failed")) {
        return problem(409, "VALIDATION_TRANSITION_INVALID", "Not possible now", `the validation is ${m.v.status.replace(/_/g, " ")}`);
      }
      if ((m.v.approvals ?? []).some((a) => a.approver_id === p.id && a.decided_at >= (m.v.history?.at(-1)?.at ?? ""))) {
        return problem(409, "CONFLICT", "Conflict", "you already decided this round");
      }
      const at = new Date().toISOString();
      m.v.approvals = [...(m.v.approvals ?? []), { id: newId("apr"), validation_id: m.v.id, approver_id: p.id, decision: body.decision, comment: body.comment ?? null, decided_at: at }];
      if (body.decision === "reject") move(m.v, "reject", p.id, body.comment ?? null, at);
      else if (m.v.approvals.filter((a) => a.decision === "approve").length >= (m.v.approvals_required ?? 1)) move(m.v, "approve", p.id, body.comment ?? null, at);
      return HttpResponse.json(m.v);
    }),
    http.post(new RegExp(`^${API_RE}/v1/validations/[^/]+:(cancel|resubmit|deprecate)$`), async ({ request }) => {
      const [id = "", action = ""] = decodeURIComponent(new URL(request.url).pathname.split("/").at(-1)!).split(":");
      const m = get(id);
      if (!m) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json().catch(() => ({}))) as { note?: string | null };
      const actor = me()?.id ?? null;
      if (action === "deprecate" && !body.note?.trim()) return problem(422, "SCHEMA_INVALID", "Invalid request", "say why the version should no longer be used");
      const before = m.v.status;
      if (!move(m.v, action as ValidationEvent, actor, body.note ?? null)) {
        return problem(409, "VALIDATION_TRANSITION_INVALID", "Not possible now", `a validation that is ${before.replace(/_/g, " ")} cannot be ${action === "cancel" ? "cancelled" : `${action}d`}`);
      }
      if (action === "cancel") m.finishesAt = null;
      if (action === "resubmit") {
        m.v.results = [];
        m.v.failing_checks = [];
        m.v.approvals = [];
        m.finishesAt = Date.now() + 2500;
        m.pending = clone(PASSED);
        return HttpResponse.json(m.v, { status: 202 });
      }
      return HttpResponse.json(m.v);
    }),
    http.post(`${API}/v1/validations/:id/evidence-pack`, async ({ params, request }) => {
      const m = get(String(params.id));
      if (!m) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as { format?: "html" | "json" };
      const policies = (m.v.policies ?? []).map((ref) => {
        const [id = "", version = "0"] = ref.split("@");
        return state.policies.get(id)?.find((p) => p.version === Number(version)) ?? state.policies.get(id)?.at(-1);
      }).filter((p): p is GovernancePolicy => Boolean(p));
      const pack = {
        format: "mapi.evidence-pack/v1" as const,
        validation: m.v,
        model_ref: `model://growth/eu-geo-mmm@v${m.v.version}`,
        policies,
        lineage: { dataset: "dataset://growth/weekly_sales@v3" },
        scorecard: null,
        generated_at: new Date().toISOString(),
        generated_by: me()?.id ?? null,
        digest: `sha256:${"ab".repeat(32)}`,
        download: null as Record<string, unknown> | null,
      };
      if ((body.format ?? "html") === "html") {
        const name = `evidence/${m.v.id}.html`;
        const html = enc.encode(`<!doctype html><title>Evidence pack ${m.v.id}</title><h1>Evidence pack</h1><p>${m.v.status}</p>`);
        ctx.artifacts.set(name, { bytes: html, type: "text/html" });
        pack.download = artifactLink(name, html, "text/html");
      }
      return HttpResponse.json(pack);
    }),
  ];
}
