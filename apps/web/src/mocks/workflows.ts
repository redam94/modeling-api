/**
 * MSW emulation of the M3.5 workflow routes for mock mode, the component tests and the smoke
 * suite: the node catalog, the gallery, About cards, workflows and revisions (validated at every
 * save by `workflow-validate.ts`), runs with live per-node progress over SSE (`Last-Event-ID`
 * resumes), node runs and output links, cancel, schedules and triggers.
 *
 * Static data comes from `workflows.gen.json` (generated from `mapi_spec` and a real local run
 * of the canonical template). A started run replays that run's event log - or, for another
 * document, a synthesised one - one event every `mapi.mock.wfStepMs` milliseconds (localStorage,
 * default 150). It is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { ArtifactLink, Problem, Run } from "@/api/types";
import { implId, parseWhen, topologicalOrder } from "@/features/workflows/doc";
import type { Catalog } from "@/features/workflows/ports";
import type {
  CatalogAbout,
  NodeImplementation,
  NodeRun,
  ScheduleCreate,
  ScheduleUpdate,
  TriggerCreate,
  Workflow,
  WorkflowCreate,
  WorkflowDoc,
  WorkflowEvent,
  WorkflowRevision,
  WorkflowRevisionCreate,
  WorkflowRun,
  WorkflowRunCreate,
  WorkflowSchedule,
  WorkflowTemplate,
  WorkflowTrigger,
} from "@/features/workflows/types";
import { cronProblem, isTimeZone, nextFire } from "@/features/workflows/cron";
import { env } from "@/env";

import { artifactLink, runDone } from "./fixtures";
import { mockPersonaId, PERSONAS } from "./tenancy";
import { mockValidateWorkflow } from "./workflow-validate";
import generated from "./workflows.gen.json";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

export const NODES = generated.nodes as unknown as NodeImplementation[];
export const TEMPLATES = generated.templates as unknown as WorkflowTemplate[];
export const CATALOG: Catalog = new Map(NODES.map((n) => [n.id, n]));
const CANONICAL = generated.canonical as unknown as { run: WorkflowRun; events: WorkflowEvent[] };

export const WORKFLOW_ID = CANONICAL.run.workflow_id;
export const CANONICAL_RUN_ID = CANONICAL.run.run_id;
export const SUB_WORKFLOW_ID = "wf_01jaf3e1f2g3h4j5k6m7n8p9r1";
export const SCHEDULE_ID = "sch_01jaf3e1f2g3h4j5k6m7n8p9s1";
const T0 = "2026-10-14T08:00:00Z";
export const STEP_KEY = "mapi.mock.wfStepMs";

function stepMs(): number {
  try {
    const v = Number(localStorage.getItem(STEP_KEY));
    return Number.isFinite(v) && v > 0 ? v : 150;
  } catch {
    return 150;
  }
}

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Problem["errors"] = []) {
  return HttpResponse.json({ type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem, {
    status,
    headers: { "content-type": "application/problem+json" },
  });
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

function personaId(): string {
  const id = mockPersonaId();
  return (id && PERSONAS[id]?.id) || "usr_01jaf3a9b8c7d6e5f4g3h2j1k0";
}

let seq = 0;
function newId(prefix: string): string {
  seq += 1;
  const tail = `${Date.now().toString(36)}${seq.toString(36)}`.padStart(16, "0").slice(-16);
  return `${prefix}_01jaf3${"x".repeat(4)}${tail}`.slice(0, prefix.length + 27);
}

const clone = <T,>(v: T): T => structuredClone(v);

// ---------------------------------------------------------------------------- state

interface MockRun {
  /** The finished run this one becomes (results, decisions, outputs, cost). */
  final: WorkflowRun;
  /** The events, in order, each `stepMs` after the previous one. */
  script: WorkflowEvent[];
  startedAt: number;
  step: number;
  cancelledAt: number | null;
}

export interface WorkflowsState {
  workflows: Map<string, Workflow>;
  revisions: Map<string, WorkflowRevision[]>;
  runs: Map<string, MockRun>;
  schedules: Map<string, WorkflowSchedule[]>;
  triggers: Map<string, WorkflowTrigger[]>;
  idempotency: Map<string, string>;
}

function docWithName(doc: WorkflowDoc, name: string, template: string | null): WorkflowDoc {
  const d = clone(doc);
  d.metadata = { ...d.metadata, name, project: "growth", ...(template ? { labels: { ...d.metadata.labels, "workflow-template": template } } : {}) };
  return d;
}

function revision(wf: Workflow, n: number, document: WorkflowDoc, message: string | null, createdBy: string | null, at: string): WorkflowRevision {
  return {
    workflow_id: wf.id,
    revision: n,
    ref: `workflow://growth/${wf.name}@r${n}`,
    document: document as unknown as WorkflowRevision["document"],
    spec_hash: `sha256:${(n * 7919).toString(16).padStart(64, "a")}`,
    validation: mockValidateWorkflow(document, CATALOG),
    message,
    created_at: at,
    created_by: createdBy,
  };
}

/** The canonical template with smaller order searches: revision 2 of the seeded workflow. */
function seededDocs(): { r1: WorkflowDoc; r2: WorkflowDoc; sub: WorkflowDoc } {
  const tpl = TEMPLATES.find((t) => t.id === "ts_readiness_arimax")!;
  const r1 = docWithName(tpl.document as unknown as WorkflowDoc, "weekly-readiness", tpl.id);
  const r2 = clone(r1);
  r2.params = { ...r2.params, max_p: { ...r2.params!.max_p!, default: 2 }, max_q: { ...r2.params!.max_q!, default: 2 } };
  const sub: WorkflowDoc = {
    apiVersion: "mapi/v1",
    kind: "Workflow",
    metadata: { name: "unit-root-tests", project: "growth", description: "ADF and KPSS on one series, reused by other workflows" },
    params: { alpha: { type: "number", default: 0.05, minimum: 0.001, maximum: 0.2 } },
    inputs: { data: { type: "dataset", description: "The series to test" } },
    nodes: [
      { id: "adf", use: "test.adf", inputs: { data: "inputs.data" }, with: { alpha: { $param: "alpha" } } },
      { id: "kpss", use: "test.kpss", inputs: { data: "inputs.data" }, with: { alpha: { $param: "alpha" } } },
    ],
    outputs: { adf: "adf.result", kpss: "kpss.result" },
  };
  return { r1, r2, sub };
}

export function createWorkflowsState(): WorkflowsState {
  const by = PERSONAS.ana!.id;
  const wf: Workflow = {
    id: WORKFLOW_ID,
    tenant_id: "acme",
    project_id: "growth",
    name: "weekly-readiness",
    ref: "workflow://growth/weekly-readiness",
    description: "Weekly sales: tests, differencing, order search and an ARIMAX backtest",
    latest_revision: 2,
    archived: false,
    template: "ts_readiness_arimax",
    created_at: T0,
    updated_at: "2026-10-14T08:40:00Z",
    created_by: by,
  };
  const sub: Workflow = {
    ...wf,
    id: SUB_WORKFLOW_ID,
    name: "unit-root-tests",
    ref: "workflow://growth/unit-root-tests",
    description: "ADF and KPSS on one series, reused by other workflows",
    latest_revision: 1,
    template: null,
  };
  const docs = seededDocs();
  const finished: MockRun = { final: clone(CANONICAL.run), script: clone(CANONICAL.events), startedAt: 0, step: 0, cancelledAt: null };
  const schedule: WorkflowSchedule = {
    id: SCHEDULE_ID,
    workflow_id: WORKFLOW_ID,
    cron: "0 6 * * 1",
    timezone: "Europe/Berlin",
    revision: null,
    params: {},
    inputs: { data: "dataset://growth/weekly_sales@latest" },
    active: true,
    description: "Weekly refresh",
    next_run_at: new Date(nextFire("0 6 * * 1", Date.now(), "Europe/Berlin")).toISOString(),
    last_run_at: "2026-10-12T04:00:00Z",
    last_run_id: CANONICAL_RUN_ID,
    created_at: T0,
    created_by: by,
  };
  return {
    workflows: new Map([
      [wf.id, wf],
      [sub.id, sub],
    ]),
    revisions: new Map([
      [wf.id, [revision(wf, 1, docs.r1, "From the gallery template ts_readiness_arimax", by, T0), revision(wf, 2, docs.r2, "Smaller AR and MA search", by, "2026-10-14T08:40:00Z")]],
      [sub.id, [revision(sub, 1, docs.sub, null, by, T0)]],
    ]),
    runs: new Map([[CANONICAL_RUN_ID, finished]]),
    schedules: new Map([[WORKFLOW_ID, [schedule]]]),
    triggers: new Map([[WORKFLOW_ID, []]]),
    idempotency: new Map(),
  };
}

// ---------------------------------------------------------------------------- runs

/** A plausible run of any document: every node succeeds, branches take `then`. */
function synthesise(runId: string, doc: WorkflowDoc, base: WorkflowRun): { final: WorkflowRun; script: WorkflowEvent[] } {
  const order = topologicalOrder(doc) ?? doc.nodes.map((n) => n.id);
  const script: WorkflowEvent[] = [];
  const ts = new Date().toISOString();
  const ev = (type: WorkflowEvent["type"], node_id: string | null, data: WorkflowEvent["data"] = {}) =>
    script.push({ seq: script.length + 1, type, run_id: runId, node_id, ts, data });
  ev("workflow.run.started", null, { nodes: order.length });
  const skipped = new Set<string>();
  const nodes: NodeRun[] = [];
  const decisions: WorkflowRun["decisions"] = [];
  for (const id of order) {
    const n = doc.nodes.find((x) => x.id === id)!;
    const impl = CATALOG.get(implId(n.use));
    const w = parseWhen(n.when);
    const kind = impl?.kind ?? "test";
    if ((w && w.arm === "else") || (w && skipped.has(w.branch))) {
      skipped.add(id);
      ev("workflow.node.skipped", id, { reason: `the ${w.arm} arm of ${w.branch} was not taken` });
      nodes.push({ node_id: id, use: n.use, kind, status: "skipped", attempt: 0, skipped_reason: `the ${w.arm} arm of ${w.branch} was not taken`, child_runs: [], params: {}, outputs: {} });
      continue;
    }
    if (impl?.id === "branch.if") {
      const condition = typeof n.with?.condition === "string" ? n.with.condition : "";
      ev("workflow.branch.decided", id, { arm: "then", value: true, inputs: {} });
      ev("workflow.node.succeeded", id, { arm: "then" });
      decisions.push({ node_id: id, condition, value: true, arm: "then", inputs: {}, decided_at: ts });
      nodes.push({ node_id: id, use: `${impl.id}@${impl.version ?? "1.0.0"}`, kind, status: "succeeded", attempt: 1, result: { value: true, arm: "then" }, child_runs: [], params: {}, outputs: {} });
      continue;
    }
    ev("workflow.node.queued", id, { attempt: 1, use: n.use });
    ev("workflow.node.started", id, { attempt: 1 });
    ev("workflow.node.succeeded", id, { attempt: 1 });
    const outputs = Object.fromEntries(Object.entries(impl?.outputs ?? {}).map(([port, type]) => [port, { type, inline: null }]));
    nodes.push({ node_id: id, use: n.use, kind, status: "succeeded", attempt: 1, result: { status: "ok" }, child_runs: [], params: (n.with ?? {}), outputs, started_at: ts, finished_at: ts });
  }
  ev("workflow.run.succeeded", null, {});
  return { final: { ...base, status: "succeeded", nodes, decisions, finished_at: ts }, script };
}

function startRun(state: WorkflowsState, wf: Workflow, rev: WorkflowRevision, body: WorkflowRunCreate): MockRun {
  const runId = newId("run");
  const doc = rev.document as unknown as WorkflowDoc;
  const now = new Date().toISOString();
  const base: WorkflowRun = {
    run_id: runId,
    workflow_id: wf.id,
    revision: rev.revision,
    tenant_id: "acme",
    project_id: "growth",
    status: "queued",
    trigger: "manual",
    params: { ...Object.fromEntries(Object.entries(doc.params ?? {}).map(([k, p]) => [k, p.default ?? null])), ...(body.params ?? {}) },
    inputs: Object.fromEntries(Object.entries(body.inputs ?? {}).map(([k, v]) => [k, v.replace(/@latest$/, "@v3")])),
    nodes: [],
    decisions: [],
    outputs: {},
    created_at: now,
    started_at: now,
    created_by: personaId(),
  };
  const canonicalIds = new Set(CANONICAL.run.nodes!.map((n) => n.node_id));
  const sameShape = doc.nodes.length === canonicalIds.size && doc.nodes.every((n) => canonicalIds.has(n.id));
  const made = sameShape
    ? {
        final: {
          ...base,
          status: "succeeded" as const,
          nodes: clone(CANONICAL.run.nodes ?? []),
          decisions: clone(CANONICAL.run.decisions ?? []),
          outputs: clone(CANONICAL.run.outputs ?? {}),
          cost: clone(CANONICAL.run.cost ?? null),
          finished_at: null,
        },
        script: CANONICAL.events.map((e) => ({ ...e, run_id: runId })),
      }
    : synthesise(runId, doc, base);
  const run: MockRun = { ...made, startedAt: Date.now(), step: stepMs(), cancelledAt: null };
  state.runs.set(runId, run);
  return run;
}

/** How many script events have happened by `now`. */
function due(run: MockRun, now = Date.now()): number {
  if (run.startedAt === 0) return run.script.length;
  const until = run.cancelledAt ?? now;
  return Math.min(run.script.length, Math.floor((until - run.startedAt) / run.step) + 1);
}

const CANCELLED_EVENT = "workflow.run.cancelled";

/** The run as the server would show it now. */
export function runView(run: MockRun, now = Date.now()): WorkflowRun {
  const n = due(run, now);
  if (run.startedAt === 0) return clone(run.final);
  const done = run.script.slice(0, n);
  const finalNodes = new Map((run.final.nodes ?? []).map((x) => [x.node_id, x]));
  const status = new Map<string, NodeRun["status"]>();
  const doneNodes = new Set<string>();
  for (const e of done) {
    if (!e.node_id) continue;
    if (e.type === "workflow.node.queued") status.set(e.node_id, "queued");
    else if (e.type === "workflow.node.started") status.set(e.node_id, "running");
    else if (e.type === "workflow.node.succeeded" || e.type === "workflow.node.cached" || e.type === "workflow.node.skipped" || e.type === "workflow.node.failed") {
      status.set(e.node_id, finalNodes.get(e.node_id)?.status ?? "succeeded");
      doneNodes.add(e.node_id);
    }
  }
  const cancelled = run.cancelledAt !== null;
  const finished = n >= run.script.length && !cancelled;
  const nodes: NodeRun[] = (run.final.nodes ?? []).map((f) => {
    if (doneNodes.has(f.node_id)) return clone(f);
    const st = status.get(f.node_id);
    const live = st ?? "pending";
    return {
      node_id: f.node_id,
      use: f.use,
      kind: f.kind,
      status: cancelled && (live === "pending" || live === "queued" || live === "running") ? "cancelled" : live,
      attempt: live === "pending" ? 0 : 1,
      child_runs: [],
      params: f.params ?? {},
      outputs: {},
      ...(live === "running" ? { started_at: new Date(run.startedAt).toISOString() } : {}),
    };
  });
  const decided = new Set(done.filter((e) => e.type === "workflow.branch.decided").map((e) => e.node_id));
  return {
    ...run.final,
    status: cancelled ? (Date.now() - (run.cancelledAt ?? 0) > 600 ? "cancelled" : "cancelling") : finished ? run.final.status : n > 0 ? "running" : "queued",
    nodes,
    decisions: (run.final.decisions ?? []).filter((d) => decided.has(d.node_id)),
    outputs: finished ? run.final.outputs : {},
    cost: finished ? run.final.cost : null,
    finished_at: finished ? new Date(run.startedAt + run.script.length * run.step).toISOString() : null,
  };
}

function sseFrame(event: string, data: unknown, id?: number, retry?: number): string {
  let s = "";
  if (id !== undefined) s += `id: ${id}\n`;
  if (retry !== undefined) s += `retry: ${retry}\n`;
  return `${s}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function runStream(run: MockRun, request: Request): Response {
  const lastId = Number(request.headers.get("Last-Event-ID") ?? "0") || 0;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let sent = lastId;
      let closed = false;
      const close = () => {
        if (closed) return;
        closed = true;
        clearInterval(timer);
        try {
          controller.close();
        } catch {
          // already closed
        }
      };
      const flush = () => {
        if (closed) return;
        const n = due(run);
        for (const e of run.script.slice(sent, n)) {
          sent = e.seq;
          controller.enqueue(enc.encode(sseFrame(e.type, e, e.seq)));
        }
        const view = runView(run);
        if (view.status === "succeeded" || view.status === "failed" || view.status === "cancelled") {
          if (run.cancelledAt !== null && sent < run.script.length + 1) {
            controller.enqueue(
              enc.encode(sseFrame(CANCELLED_EVENT, { seq: run.script.length + 1, type: CANCELLED_EVENT, run_id: view.run_id, node_id: null, ts: new Date().toISOString(), data: {} }, run.script.length + 1)),
            );
          }
          controller.enqueue(enc.encode(sseFrame("end", { run_id: view.run_id, status: view.status })));
          close();
        }
      };
      controller.enqueue(enc.encode(sseFrame("snapshot", runView(run), undefined, 2000)));
      const timer = setInterval(flush, Math.max(40, Math.min(run.step, 250)));
      flush();
      request.signal.addEventListener("abort", close);
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream", "cache-control": "no-store" } });
}

/** Ordinary runs for the children of mock workflow runs (`GET /v1/runs/{id}` falls through otherwise). */
function childRun(state: WorkflowsState, id: string): Run | null {
  for (const r of state.runs.values()) {
    const node = (r.final.nodes ?? []).find((n) => n.run_id === id || (n.child_runs ?? []).includes(id));
    if (!node) continue;
    const kind = node.kind === "model" ? "fit" : node.kind === "governance" ? "governance" : "workflow_node";
    return { ...clone(runDone), id, kind: kind, parent_run_id: r.final.run_id, labels: { workflow: r.final.workflow_id, "workflow-node": node.node_id } };
  }
  return null;
}

// ---------------------------------------------------------------------------- handlers

export function createWorkflowHandlers(state: WorkflowsState, ctx: { artifacts: Map<string, { bytes: Uint8Array; type: string }> }): HttpHandler[] {
  const wfOr404 = (id: string) => state.workflows.get(id) ?? null;
  const latest = (id: string) => state.revisions.get(id)?.at(-1) ?? null;
  const parseDoc = (body: { document?: unknown; text?: string | null }): WorkflowDoc | null => {
    if (body.document && typeof body.document === "object") return body.document as WorkflowDoc;
    if (typeof body.text === "string") {
      try {
        return JSON.parse(body.text) as WorkflowDoc;
      } catch {
        return null;
      }
    }
    return null;
  };
  const abouts = [...(generated.abouts as unknown as CatalogAbout[]), ...NODES.map((n) => ({ kind: "node" as const, id: n.id, about: n.about }))];
  return [
    // ------------------------------------------------------------------ catalog
    http.get(`${API}/v1/catalog/workflow-nodes`, ({ request }) => {
      const kind = new URL(request.url).searchParams.get("kind");
      return page(NODES.filter((n) => !kind || n.kind === kind));
    }),
    http.get(`${API}/v1/catalog/workflow-nodes/:id`, ({ params }) => {
      const n = CATALOG.get(String(params.id));
      return n ? HttpResponse.json(n) : problem(404, "NODE_UNKNOWN", "Unknown node", `no node ${String(params.id)}`);
    }),
    http.get(`${API}/v1/catalog/abouts`, ({ request }) => {
      const kind = new URL(request.url).searchParams.get("kind");
      return page(abouts.filter((a) => !kind || a.kind === kind));
    }),
    http.get(`${API}/v1/workflow-templates`, () => page(TEMPLATES)),
    http.get(`${API}/v1/workflow-templates/:id`, ({ params }) => {
      const t = TEMPLATES.find((x) => x.id === params.id);
      return t ? HttpResponse.json(t) : problem(404, "NOT_FOUND", "Not found", "no such workflow template");
    }),

    // ------------------------------------------------------------------ workflows
    http.post(new RegExp(`^${API_RE}/v1/workflows:validate$`), async ({ request }) => {
      const doc = parseDoc((await request.json()) as { document?: unknown; text?: string });
      if (!doc) return problem(400, "SPEC_PARSE_ERROR", "The document could not be parsed");
      return HttpResponse.json(mockValidateWorkflow(doc, CATALOG));
    }),
    http.get(`${API}/v1/workflows`, ({ request }) => {
      const archived = new URL(request.url).searchParams.get("archived") === "true";
      return page([...state.workflows.values()].filter((w) => Boolean(w.archived) === archived));
    }),
    http.post(`${API}/v1/workflows`, async ({ request }) => {
      const body = (await request.json()) as WorkflowCreate;
      let doc: WorkflowDoc | null;
      if (body.template) {
        const t = TEMPLATES.find((x) => x.id === body.template);
        if (!t) return problem(404, "NOT_FOUND", "Not found", "no such workflow template");
        doc = docWithName(t.document as unknown as WorkflowDoc, (t.document as unknown as WorkflowDoc).metadata.name, t.id);
      } else doc = parseDoc(body);
      if (!doc?.metadata?.name) return problem(422, "WORKFLOW_INVALID", "Invalid workflow", "metadata.name is required");
      const name = doc.metadata.name;
      if ([...state.workflows.values()].some((w) => w.name === name)) return problem(409, "CONFLICT", "Conflict", `a workflow named '${name}' already exists; add a revision instead`);
      const now = new Date().toISOString();
      const wf: Workflow = {
        id: newId("wf"),
        tenant_id: "acme",
        project_id: "growth",
        name,
        ref: `workflow://growth/${name}`,
        description: doc.metadata.description ?? null,
        latest_revision: 1,
        archived: false,
        template: body.template ?? null,
        created_at: now,
        updated_at: now,
        created_by: personaId(),
      };
      state.workflows.set(wf.id, wf);
      state.revisions.set(wf.id, [revision(wf, 1, doc, body.message ?? null, personaId(), now)]);
      state.schedules.set(wf.id, []);
      state.triggers.set(wf.id, []);
      return HttpResponse.json(wf, { status: 201 });
    }),
    http.get(`${API}/v1/workflows/:id`, ({ params }) => {
      const wf = wfOr404(String(params.id));
      return wf ? HttpResponse.json(wf) : problem(404, "NOT_FOUND", "Not found", "no such workflow");
    }),
    http.patch(`${API}/v1/workflows/:id`, async ({ params, request }) => {
      const wf = wfOr404(String(params.id));
      if (!wf) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as { description?: string | null; archived?: boolean | null };
      if (body.description !== undefined) wf.description = body.description;
      if (typeof body.archived === "boolean") wf.archived = body.archived;
      wf.updated_at = new Date().toISOString();
      return HttpResponse.json(wf);
    }),
    http.delete(`${API}/v1/workflows/:id`, ({ params }) => {
      if (!state.workflows.delete(String(params.id))) return problem(404, "NOT_FOUND", "Not found");
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/v1/workflows/:id/revisions`, ({ params }) => page([...(state.revisions.get(String(params.id)) ?? [])].reverse())),
    http.get(`${API}/v1/workflows/:id/revisions/:rev`, ({ params }) => {
      const r = state.revisions.get(String(params.id))?.find((x) => x.revision === Number(params.rev));
      return r ? HttpResponse.json(r) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(`${API}/v1/workflows/:id/revisions`, async ({ params, request }) => {
      const wf = wfOr404(String(params.id));
      if (!wf) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as WorkflowRevisionCreate;
      if (body.expected_revision != null && body.expected_revision !== wf.latest_revision) {
        return problem(409, "CONFLICT", "Conflict", `the latest revision is ${wf.latest_revision}, not ${body.expected_revision}; reload and save again`);
      }
      const doc = parseDoc(body);
      if (!doc) return problem(400, "SPEC_PARSE_ERROR", "The document could not be parsed");
      if (doc.metadata?.name && doc.metadata.name !== wf.name) {
        return problem(422, "WORKFLOW_INVALID", "Invalid workflow", `metadata.name must stay '${wf.name}' (create a new workflow to rename)`, [
          { code: "WORKFLOW_INVALID", pointer: "/metadata/name", message: `metadata.name must stay '${wf.name}'`, severity: "error", context: {} },
        ]);
      }
      wf.latest_revision += 1;
      wf.updated_at = new Date().toISOString();
      const rev = revision(wf, wf.latest_revision, doc, body.message ?? null, personaId(), wf.updated_at);
      state.revisions.get(wf.id)!.push(rev);
      return HttpResponse.json(rev, { status: 201 });
    }),

    // ------------------------------------------------------------------ runs
    http.get(`${API}/v1/workflows/:id/runs`, ({ params }) =>
      page([...state.runs.values()].filter((r) => r.final.workflow_id === params.id).map((r) => runView(r)).reverse()),
    ),
    http.post(`${API}/v1/workflows/:id/runs`, async ({ params, request }) => {
      const wf = wfOr404(String(params.id));
      if (!wf) return problem(404, "NOT_FOUND", "Not found");
      const key = request.headers.get("Idempotency-Key");
      const seen = key ? state.idempotency.get(key) : undefined;
      if (seen && state.runs.has(seen)) return HttpResponse.json(runView(state.runs.get(seen)!), { status: 200 });
      const body = (await request.json()) as WorkflowRunCreate;
      const rev = body.revision ? state.revisions.get(wf.id)?.find((r) => r.revision === body.revision) : latest(wf.id);
      if (!rev) return problem(404, "NOT_FOUND", "Not found", "no such revision");
      if (!rev.validation.valid) {
        return problem(409, "WORKFLOW_NOT_RUNNABLE", "The workflow cannot run", `revision ${rev.revision} has validation errors; save a valid revision first`, rev.validation.issues ?? []);
      }
      const doc = rev.document as unknown as WorkflowDoc;
      for (const [name, decl] of Object.entries(doc.inputs ?? {})) {
        if ((decl.required ?? true) && !decl.default && !body.inputs?.[name]) {
          return problem(422, "WORKFLOW_INVALID", "Invalid run", `input '${name}' is required`, [
            { code: "WORKFLOW_INVALID", pointer: `/inputs/${name}`, message: `input '${name}' is required`, severity: "error", context: {} },
          ]);
        }
      }
      for (const [name, p] of Object.entries(doc.params ?? {})) {
        if (p.required && body.params?.[name] === undefined) {
          return problem(422, "WORKFLOW_INVALID", "Invalid run", `parameter '${name}' is required`, [
            { code: "WORKFLOW_INVALID", pointer: `/params/${name}`, message: `parameter '${name}' is required`, severity: "error", context: {} },
          ]);
        }
      }
      const run = startRun(state, wf, rev, body);
      if (key) state.idempotency.set(key, run.final.run_id);
      return HttpResponse.json(runView(run), { status: 202 });
    }),
    http.get(`${API}/v1/workflow-runs/:id`, ({ params }) => {
      const r = state.runs.get(String(params.id));
      return r ? HttpResponse.json(runView(r)) : problem(404, "NOT_FOUND", "Not found", "no such workflow run");
    }),
    http.get(`${API}/v1/workflow-runs/:id/events`, ({ params, request }) => {
      const r = state.runs.get(String(params.id));
      if (!r) return problem(404, "NOT_FOUND", "Not found");
      if ((request.headers.get("accept") ?? "").includes("text/event-stream")) return runStream(r, request);
      return page(r.script.slice(0, due(r)));
    }),
    http.get(`${API}/v1/workflow-runs/:id/nodes`, ({ params }) => {
      const r = state.runs.get(String(params.id));
      return r ? page(runView(r).nodes ?? []) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${API}/v1/workflow-runs/:id/nodes/:node`, ({ params }) => {
      const r = state.runs.get(String(params.id));
      const n = r ? runView(r).nodes?.find((x) => x.node_id === params.node) : undefined;
      return n ? HttpResponse.json(n) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${API}/v1/workflow-runs/:id/nodes/:node/outputs/:port`, ({ params }) => {
      const r = state.runs.get(String(params.id));
      const n = r ? runView(r).nodes?.find((x) => x.node_id === params.node) : undefined;
      const out = n?.outputs?.[String(params.port)];
      if (!out) return problem(404, "NOT_FOUND", "Not found", "no such output");
      const name = `wf/${String(params.id)}/${String(params.node)}/${String(params.port)}.json`;
      const bytes = enc.encode(JSON.stringify(out.inline ?? { ref: out.ref ?? null }, null, 2));
      ctx.artifacts.set(name, { bytes, type: "application/json" });
      return HttpResponse.json(artifactLink(name, bytes, "application/json") satisfies ArtifactLink);
    }),
    http.post(new RegExp(`^${API_RE}/v1/workflow-runs/[^/]+:cancel$`), ({ request }) => {
      const id = decodeURIComponent(new URL(request.url).pathname.split("/").at(-1)!.split(":")[0]!);
      const r = state.runs.get(id);
      if (!r) return problem(404, "NOT_FOUND", "Not found");
      const view = runView(r);
      if (view.status === "succeeded" || view.status === "failed" || view.status === "cancelled") {
        return problem(409, "CONFLICT", "Conflict", `the run is already ${view.status}`);
      }
      r.cancelledAt = Date.now();
      return HttpResponse.json(runView(r), { status: 202 });
    }),
    // Children of workflow runs (workflow_node, fit and governance runs).
    http.get(`${API}/v1/runs/:id`, ({ params }) => {
      const r = childRun(state, String(params.id));
      return r ? HttpResponse.json(r) : undefined;
    }),

    // ------------------------------------------------------------------ schedules
    http.get(`${API}/v1/workflows/:id/schedules`, ({ params }) => page(state.schedules.get(String(params.id)) ?? [])),
    http.post(`${API}/v1/workflows/:id/schedules`, async ({ params, request }) => {
      const id = String(params.id);
      if (!wfOr404(id)) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as ScheduleCreate;
      const bad = cronProblem(body.cron);
      if (bad) return problem(422, "SCHEDULE_INVALID", "Invalid schedule", bad, [{ code: "SCHEDULE_INVALID", pointer: "/cron", message: bad, severity: "error", context: {} }]);
      const tz = body.timezone ?? "UTC";
      if (!isTimeZone(tz)) return problem(422, "SCHEDULE_INVALID", "Invalid schedule", "unknown time zone", [{ code: "SCHEDULE_INVALID", pointer: "/timezone", message: "unknown time zone", severity: "error", context: {} }]);
      const s: WorkflowSchedule = {
        id: newId("sch"),
        workflow_id: id,
        cron: body.cron,
        timezone: tz,
        revision: body.revision ?? null,
        params: body.params ?? {},
        inputs: body.inputs ?? {},
        active: body.active ?? true,
        description: body.description ?? null,
        next_run_at: new Date(nextFire(body.cron, Date.now(), tz)).toISOString(),
        last_run_at: null,
        last_run_id: null,
        created_at: new Date().toISOString(),
        created_by: personaId(),
      };
      state.schedules.set(id, [...(state.schedules.get(id) ?? []), s]);
      return HttpResponse.json(s, { status: 201 });
    }),
    http.patch(`${API}/v1/workflows/:id/schedules/:sid`, async ({ params, request }) => {
      const s = state.schedules.get(String(params.id))?.find((x) => x.id === params.sid);
      if (!s) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as ScheduleUpdate;
      if (body.cron) {
        const bad = cronProblem(body.cron);
        if (bad) return problem(422, "SCHEDULE_INVALID", "Invalid schedule", bad, [{ code: "SCHEDULE_INVALID", pointer: "/cron", message: bad, severity: "error", context: {} }]);
        s.cron = body.cron;
      }
      if (body.timezone) s.timezone = body.timezone;
      if (typeof body.active === "boolean") s.active = body.active;
      if (body.inputs) s.inputs = body.inputs;
      if (body.params) s.params = body.params;
      s.next_run_at = s.active ? new Date(nextFire(s.cron, Date.now(), s.timezone)).toISOString() : null;
      return HttpResponse.json(s);
    }),
    http.delete(`${API}/v1/workflows/:id/schedules/:sid`, ({ params }) => {
      const list = state.schedules.get(String(params.id)) ?? [];
      state.schedules.set(String(params.id), list.filter((x) => x.id !== params.sid));
      return new HttpResponse(null, { status: 204 });
    }),

    // ------------------------------------------------------------------ triggers
    http.get(`${API}/v1/workflows/:id/triggers`, ({ params }) => page(state.triggers.get(String(params.id)) ?? [])),
    http.post(`${API}/v1/workflows/:id/triggers`, async ({ params, request }) => {
      const id = String(params.id);
      const doc = latest(id)?.document as unknown as WorkflowDoc | undefined;
      if (!doc) return problem(404, "NOT_FOUND", "Not found");
      const body = (await request.json()) as TriggerCreate;
      if (doc.inputs?.[body.input]?.type !== "dataset") {
        return problem(422, "WORKFLOW_INVALID", "Invalid trigger", `the workflow has no dataset input '${body.input}'`, [
          { code: "WORKFLOW_INVALID", pointer: "/input", message: `the workflow has no dataset input '${body.input}'`, severity: "error", context: {} },
        ]);
      }
      const t: WorkflowTrigger = {
        id: newId("trg"),
        workflow_id: id,
        event: "dataset.version.created",
        dataset: body.dataset,
        input: body.input,
        debounce_seconds: body.debounce_seconds ?? 300,
        params: body.params ?? {},
        revision: body.revision ?? null,
        active: body.active ?? true,
        last_fired_at: null,
        last_run_id: null,
        created_at: new Date().toISOString(),
        created_by: personaId(),
      };
      state.triggers.set(id, [...(state.triggers.get(id) ?? []), t]);
      return HttpResponse.json(t, { status: 201 });
    }),
    http.delete(`${API}/v1/workflows/:id/triggers/:tid`, ({ params }) => {
      const list = state.triggers.get(String(params.id)) ?? [];
      state.triggers.set(String(params.id), list.filter((x) => x.id !== params.tid));
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}
