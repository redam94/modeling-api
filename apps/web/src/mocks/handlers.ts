/**
 * MSW handlers emulating the M1 API for `--mode mock`, the Playwright smoke test and
 * component tests. Behaviour follows docs/reference/http-api.md closely enough to exercise
 * the UI (pages, SSE, artifact links, problem documents); it is not a server.
 */
import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type { DatasetVersion, Problem, Run, RunEventView, SpecRevision, UploadSession } from "@/api/types";
import { env } from "@/env";

import * as fx from "./fixtures";
import { createExploreHandlers, createExploreState, explorerValidations } from "./explore";
import { createGovernanceHandlers, createGovernanceState, notValidatedProblem } from "./governance";
import { createGuidedHandlers } from "./guided";
import { createIntegrationHandlers, createIntegrationsState } from "./integrations";
import { createTenancyHandlers, createTenancyState } from "./tenancy";
import { createTrustHandlers, createTrustState } from "./trust";
import { createWorkflowHandlers, createWorkflowsState } from "./workflows";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

interface MockState {
  tenancy: ReturnType<typeof createTenancyState>;
  integrations: ReturnType<typeof createIntegrationsState>;
  trust: ReturnType<typeof createTrustState>;
  explore: ReturnType<typeof createExploreState>;
  workflows: ReturnType<typeof createWorkflowsState>;
  governance: ReturnType<typeof createGovernanceState>;
  runs: Map<string, Run>;
  revisions: SpecRevision[];
  artifacts: Map<string, { bytes: Uint8Array; type: string }>;
  versions: DatasetVersion[];
  uploads: Map<string, number>;
}

/** The validations the explorer fixtures computed join the governance mock's (one store). */
function seedGovernance(governance: ReturnType<typeof createGovernanceState>): ReturnType<typeof createGovernanceState> {
  for (const v of explorerValidations()) governance.validations.set(v.id, { v, finishesAt: null, pending: null });
  return governance;
}

export function createState(): MockState {
  const json = (v: unknown) => enc.encode(JSON.stringify(v));
  return {
    tenancy: createTenancyState(),
    integrations: createIntegrationsState(),
    trust: createTrustState(),
    explore: createExploreState(),
    workflows: createWorkflowsState(),
    governance: seedGovernance(createGovernanceState()),
    runs: new Map(fx.runs.map((r) => [r.id, structuredClone(r)])),
    revisions: [fx.specRevision1, fx.specRevision].map((r) => structuredClone(r)),
    versions: [structuredClone(fx.datasetVersion)],
    uploads: new Map(),
    artifacts: new Map([
      ["ui/plotdata/ppc_fan.arrow", { bytes: fx.fanArrow(), type: "application/vnd.apache.arrow.stream" }],
      ["ui/tree_index.json", { bytes: json(fx.treeIndexView), type: "application/json" }],
      ["ui/preprocess.json", { bytes: json(fx.preprocessView), type: "application/json" }],
      ["profile.json", { bytes: json(fx.profileView), type: "application/json" }],
      ["preview.json", { bytes: json(fx.previewView), type: "application/json" }],
    ]),
  };
}

function sseFrame(event: string, data: unknown, id?: number | string, retry?: number): string {
  let s = "";
  if (id !== undefined) s += `id: ${id}\n`;
  if (retry !== undefined) s += `retry: ${retry}\n`;
  return `${s}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

const TERMINAL = new Set(["rejected", "succeeded", "partial", "failed", "cancelled"]);

function sseResponse(state: MockState, run: Run, request: Request): Response {
  const lastId = Number(request.headers.get("Last-Event-ID") ?? "0") || 0;
  const persisted: RunEventView[] = fx.events(run.id).filter((e) => e.id > lastId);
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(enc.encode(sseFrame("snapshot", run, undefined, 3000)));
      for (const e of persisted) controller.enqueue(enc.encode(sseFrame(e.type, e, e.id)));
      if (TERMINAL.has(run.state)) {
        controller.enqueue(enc.encode(sseFrame("end", run)));
        controller.close();
        return;
      }
      let seq = 100;
      let done = run.progress?.chains?.[0]?.draws_done ?? 0;
      const timer = setInterval(() => {
        const current = state.runs.get(run.id);
        if (!current || TERMINAL.has(current.state)) {
          if (current) controller.enqueue(enc.encode(sseFrame("end", current)));
          clearInterval(timer);
          controller.close();
          return;
        }
        done = Math.min(1000, done + 25);
        seq += 1;
        const payload = {
          phase: "sample",
          fraction: done / 1000,
          message: "drawing samples",
          eta_seconds: (1000 - done) / 10,
          chains: [
            { chain: 0, draws_done: done, draws_total: 1000, tuning: false, divergences: 0 },
            { chain: 1, draws_done: Math.max(0, done - 20), draws_total: 1000, tuning: false, divergences: 2 },
          ],
        };
        // Same shape as the server's live frames (`_live_json`): not persisted, so no `id`.
        const ev: Omit<RunEventView, "id"> = { run_id: run.id, attempt: 1, seq, ts: new Date().toISOString(), type: "progress", payload };
        controller.enqueue(enc.encode(sseFrame("progress", ev)));
      }, 800);
      request.signal.addEventListener("abort", () => {
        clearInterval(timer);
        try {
          controller.close();
        } catch {
          // already closed
        }
      });
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream", "cache-control": "no-store" } });
}

export function createHandlers(state: MockState = createState()): HttpHandler[] {
  const link = (name: string) => {
    const a = state.artifacts.get(name);
    return a ? fx.artifactLink(name, a.bytes, a.type) : null;
  };
  // M3.5: explorers, About cards, Help me choose, governance evidence, budget optimisation.
  const explore = createExploreHandlers(state, state.explore);
  return [
    // v0.3: session, tenancy and the per-request checks (first, so they guard every /v1 route).
    ...createTenancyHandlers(state.tenancy),
    ...explore.early,
    // M3 Guided mode: answers for the resources a Guided session creates, falls through otherwise.
    ...createGuidedHandlers(state),
    ...explore.late,
    // M3.5: workflows (mocks/workflows.ts) and governance (mocks/governance.ts); the promotion
    // gate refuses unvalidated versions when a policy says so, before the endpoint routes run.
    ...createWorkflowHandlers(state.workflows, { artifacts: state.artifacts }),
    ...createGovernanceHandlers(state.governance, { members: () => state.tenancy.members, artifacts: state.artifacts }),
    http.put(`${API}/v1/endpoints/:alias`, async ({ request }) => {
      const body = (await request.clone().json()) as { model_version_id?: string | null };
      return (body.model_version_id && notValidatedProblem(state.governance, body.model_version_id)) || undefined;
    }),
    // v0.4: endpoints, webhooks, sharing (mocks/integrations.ts) and the trust lifecycle (mocks/trust.ts).
    ...createIntegrationHandlers(state.integrations, { artifacts: state.artifacts }),
    ...createTrustHandlers(state.trust, { tenancy: state.tenancy, artifacts: state.artifacts }),
    http.get(`${API}/readyz`, () =>
      HttpResponse.json({ status: "ok", version: "0.5.1-mock", contract_version: "0.2", api_version: "v1", checks: { postgres: { status: "ok", latency_ms: 1.2 }, object_store: { status: "ok", latency_ms: 2.1 } } }),
    ),
    http.get(`${API}/healthz`, () => HttpResponse.json({ status: "ok", version: "0.5.1-mock", contract_version: "0.2" })),
    http.get(`${API}/v1/catalog`, () => HttpResponse.json(fx.catalog)),
    http.get(`${API}/v1/schemas/:name`, ({ params }) =>
      params.name === "modelspec.json"
        ? HttpResponse.json({ $schema: "https://json-schema.org/draft/2020-12/schema", type: "object", properties: { apiVersion: { const: "mapi/v1" }, kind: { const: "ModelSpec" } }, required: ["apiVersion", "kind"] })
        : problem(404, "NOT_FOUND", "Not found"),
    ),

    // datasets
    http.get(`${API}/v1/datasets`, () => page([fx.dataset])),
    http.get(`${API}/v1/datasets/:id`, ({ params }) => (params.id === fx.DS_ID ? HttpResponse.json(fx.dataset) : problem(404, "NOT_FOUND", "Not found", "no such dataset"))),
    http.post(`${API}/v1/datasets`, async ({ request }) => {
      const body = (await request.json()) as { name: string; description?: string };
      if (body.name === fx.dataset.name) return problem(409, "CONFLICT", "Conflict", "the name is taken");
      return HttpResponse.json({ ...fx.dataset, id: "ds_01jaf3a1b2c3d4e5f6g7h8j9z9", name: body.name, ref: `dataset://default/${body.name}`, description: body.description ?? null, latest_version: null }, { status: 201 });
    }),
    http.get(`${API}/v1/datasets/:id/versions`, () => page([...state.versions].reverse())),
    http.get(`${API}/v1/datasets/:id/versions/:v`, ({ params }) => {
      const v = state.versions.find((x) => x.version === Number(params.v));
      return v ? HttpResponse.json(v) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(`${API}/v1/datasets/:id/uploads`, async ({ request, params }) => {
      const body = (await request.json()) as { filename: string; content_type: string; size_bytes: number };
      const id = "upl_01jaf3d1e2f3g4h5j6k7m8n9p0";
      const session: UploadSession = {
        ...fx.dataset,
        id,
        dataset_id: String(params.id),
        filename: body.filename,
        content_type: body.content_type,
        size_bytes: body.size_bytes,
        status: "pending",
        upload: {
          method: "PUT",
          url: `http://127.0.0.1:4443/mock-upload/${id}?X-Goog-Signature=mock`,
          name: `t/default/uploads/${id}`,
          expires_at: "2099-01-01T00:00:00Z",
          headers: { "content-type": body.content_type, "x-goog-if-generation-match": "0" },
          max_bytes: body.size_bytes,
        },
        expires_at: "2099-01-01T00:00:00Z",
        created_at: fx.T0,
      };
      return HttpResponse.json(session, { status: 201 });
    }),
    http.put("http://127.0.0.1:4443/mock-upload/:id", async ({ request, params }) => {
      if (request.headers.get("x-goog-if-generation-match") !== "0") return new HttpResponse(null, { status: 412 });
      state.uploads.set(String(params.id), (await request.arrayBuffer()).byteLength);
      return HttpResponse.json({ generation: "1" });
    }),
    http.post(`${API}/v1/datasets/:id/versions`, async ({ request }) => {
      const body = (await request.json()) as { upload_id: string; message?: string };
      if (!state.uploads.has(body.upload_id)) return problem(409, "CONFLICT", "Conflict", "the upload is not finished");
      const version = state.versions.length + 1;
      const v: DatasetVersion = {
        ...fx.datasetVersion,
        version,
        ref: `dataset://default/eu_media_daily@v${version}`,
        status: "ingesting",
        ingest_run_id: fx.RUN_RUNNING,
        message: body.message ?? null,
        ready_at: null,
      };
      state.versions.push(v);
      return HttpResponse.json(v, { status: 202 });
    }),
    http.get(`${API}/v1/datasets/:id/versions/:v/artifacts/:name`, ({ params }) => {
      const l = link(String(params.name));
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found");
    }),

    // specs
    http.post(new RegExp(`^${API_RE}/v1/specs:validate$`), async ({ request }) => {
      const body = (await request.json()) as { yaml?: string };
      if (!body.yaml || body.yaml.includes("\t")) {
        return HttpResponse.json(
          { type: "urn:mapi:error:SPEC_PARSE_ERROR", title: "The document could not be parsed", status: 400, code: "SPEC_PARSE_ERROR", detail: "tabs are not allowed in YAML indentation", errors: [] },
          { status: 400 },
        );
      }
      await delay(60);
      return HttpResponse.json(fx.validateResponseFor(body.yaml));
    }),
    http.get(`${API}/v1/specs`, () => page([{ ...fx.specDoc, latest_revision: state.revisions.length, latest: state.revisions.at(-1) ?? null }])),
    http.get(`${API}/v1/specs/:id`, ({ params }) =>
      params.id === fx.SPEC_ID ? HttpResponse.json({ ...fx.specDoc, latest_revision: state.revisions.length, latest: state.revisions.at(-1) ?? null }) : problem(404, "NOT_FOUND", "Not found"),
    ),
    http.get(`${API}/v1/specs/:id/revisions`, () => page([...state.revisions].reverse())),
    http.get(`${API}/v1/specs/:id/revisions/:rev`, ({ params }) => {
      const r = state.revisions.find((x) => x.revision === Number(params.rev));
      return r ? HttpResponse.json(r) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(`${API}/v1/specs`, async ({ request }) => {
      const body = (await request.json()) as { yaml: string };
      return HttpResponse.json({ ...fx.specDoc, latest_revision: 1, latest: { ...fx.specRevision1, yaml: body.yaml } }, { status: 201 });
    }),
    http.post(`${API}/v1/specs/:id/revisions`, async ({ request }) => {
      const body = (await request.json()) as { yaml: string; parent_revision: number; message?: string };
      if (body.parent_revision !== state.revisions.length) return problem(409, "CONFLICT", "Conflict", "parent_revision is stale");
      const v = fx.validateResponseFor(body.yaml);
      const rev: SpecRevision = { ...fx.specRevision, revision: state.revisions.length + 1, ref: `spec://default/eu-geo-mmm@r${state.revisions.length + 1}`, yaml: body.yaml, valid: v.valid, issues: v.issues ?? [], parent_revision: body.parent_revision, message: body.message ?? null };
      state.revisions.push(rev);
      return HttpResponse.json(rev, { status: 201 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/specs/[^/]+/revisions/\\d+:dry-run$`), () => HttpResponse.json({ ...fx.runRunning, kind: "validate" }, { status: 202 })),

    // runs
    http.get(`${API}/v1/runs`, ({ request }) => {
      const url = new URL(request.url);
      const s = url.searchParams.get("state");
      const k = url.searchParams.get("kind");
      return page([...state.runs.values()].filter((r) => (!s || r.state === s) && (!k || r.kind === k)));
    }),
    http.post(`${API}/v1/runs`, async ({ request }) => {
      const body = (await request.json()) as { kind: Run["kind"] };
      const r: Run = { ...fx.runRunning, id: "run_01jaf3q2x8m9v7k5h4g3f2e1c3", kind: body.kind, state: "queued", progress: null };
      state.runs.set(r.id, r);
      return HttpResponse.json(r, { status: 201 });
    }),
    http.get(`${API}/v1/runs/:id`, ({ params }) => {
      const r = state.runs.get(String(params.id));
      return r ? HttpResponse.json(r) : problem(404, "NOT_FOUND", "Not found", "no such run");
    }),
    http.get(`${API}/v1/runs/:id/events`, ({ params, request }) => {
      const r = state.runs.get(String(params.id));
      if (!r) return problem(404, "NOT_FOUND", "Not found");
      if ((request.headers.get("accept") ?? "").includes("text/event-stream")) return sseResponse(state, r, request);
      return page(fx.events(r.id));
    }),
    http.get(`${API}/v1/runs/:id/logs`, ({ params }) => page(fx.logLines(String(params.id)))),
    http.post(new RegExp(`^${API_RE}/v1/runs/[^/]+:cancel$`), ({ request }) => {
      const id = decodeURIComponent(new URL(request.url).pathname.split("/").at(-1)!.split(":")[0]!);
      const r = state.runs.get(id);
      if (!r) return problem(404, "NOT_FOUND", "Not found");
      const next: Run = TERMINAL.has(r.state) ? r : { ...r, state: "cancelling" };
      state.runs.set(id, next);
      return HttpResponse.json(next, { status: 202 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/runs/[^/]+:retry$`), async ({ request }) => {
      const body = (await request.json()) as { resource_class?: string };
      if (body.resource_class === "L" || body.resource_class === "XL") {
        // The mock tenant's quota policy caps the resource class at M.
        return problem(429, "QUOTA_EXCEEDED", "Quota exceeded", `tenant acme does not allow max_resource_class ${body.resource_class} (maximum M)`);
      }
      const r: Run = { ...fx.runRunning, id: "run_01jaf3q2x8m9v7k5h4g3f2e1d4", state: "queued", resource_class: (body.resource_class ?? "M") as Run["resource_class"], retry_of: fx.RUN_FAILED, progress: null };
      state.runs.set(r.id, r);
      return HttpResponse.json(r, { status: 201 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/runs/[^/]+:resume$`), () => problem(409, "CONFLICT", "Conflict", "the run is not partial")),

    // models
    http.get(`${API}/v1/models`, () => page([fx.model])),
    http.get(`${API}/v1/models/:id`, ({ params }) => (params.id === fx.MODEL_ID ? HttpResponse.json(fx.model) : problem(404, "NOT_FOUND", "Not found"))),
    http.get(`${API}/v1/models/:id/versions`, () => page([fx.modelVersion])),
    http.get(`${API}/v1/models/:id/versions/:v`, ({ params }) => (params.v === "1" ? HttpResponse.json(fx.modelVersion) : problem(404, "NOT_FOUND", "Not found"))),
    http.get(`${API}/v1/models/:id/versions/:v/plots`, () => page(fx.plotItems)),
    http.get(`${API}/v1/models/:id/versions/:v/plots/:plotId`, () => HttpResponse.json(fx.plotSpec)),
    http.get(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+/artifacts/(.+)$`), ({ request }) => {
      const raw = new URL(request.url).pathname.split("/artifacts/")[1] ?? "";
      const l = link(decodeURIComponent(raw));
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.post(new RegExp(`^${API_RE}/v1/models/[^/]+/versions/\\d+:score$`), () => {
      const out = enc.encode(JSON.stringify({ format: "mapi.score-summary/v1", note: "mock" }));
      state.artifacts.set("outputs/summary.json", { bytes: out, type: "application/json" });
      return HttpResponse.json(
        {
          done: true,
          run: { ...fx.runDone, kind: "score", model_version_id: fx.MV_ID, spec_id: null, spec_revision: null },
          outputs: [link("ui/plotdata/ppc_fan.arrow"), link("outputs/summary.json")],
        },
        { headers: { "Preference-Applied": "wait=30" } },
      );
    }),

    // signed URLs (GCS shim)
    http.get("http://127.0.0.1:4443/mock-artifacts/:name", ({ params }) => {
      const a = state.artifacts.get(decodeURIComponent(String(params.name)));
      return a ? new HttpResponse(a.bytes, { headers: { "content-type": a.type } }) : new HttpResponse(null, { status: 404 });
    }),
  ];
}
