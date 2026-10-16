/**
 * MSW emulation of the v0.4 integration routes (`mapi_spec.api.M3_ENDPOINTS`) for mock mode,
 * the Playwright smoke suite and component tests: endpoints and aliases with the promotion
 * gate, schema / validate / score / snippets, webhooks with secrets shown once, deliveries
 * and replays, saved plots, static renders (Plotly refused with `MODE_NOT_SUPPORTED`) and
 * embed links.
 *
 * The behaviour follows the server closely enough to exercise the UI; it is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type {
  Endpoint,
  EndpointPromote,
  EndpointRevision,
  EndpointSchema,
  EndpointScoreCall,
  Issue,
  PlotEmbed,
  PlotEmbedCreate,
  PlotRender,
  PlotRenderRequest,
  Problem,
  PromotionGateDecision,
  SavedPlot,
  SavedPlotCreate,
  Snippet,
  Webhook,
  WebhookCreate,
  WebhookDelivery,
  WebhookUpdate,
} from "@/api/types";
import { env } from "@/env";

import { artifactLink, fanArrow, MODEL_ID, modelVersion, MV_ID, plotSpec, runDone } from "./fixtures";
import generated from "./integrations.gen.json";
import { mockPersonaId, PERSONAS } from "./tenancy";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

export const ENDPOINT_ALIAS = generated.alias;
export const WEBHOOK_ID = "whk_01jaf3w1x2y3z4a5b6c7d8e9f0";
export const WEBHOOK_DISABLED_ID = "whk_01jaf3w1x2y3z4a5b6c7d8e9g1";
export const SAVED_PLOT_ID = "plt_01jaf3p1q2r3s4t5v6w7x8y9z0";
export const SAVED_PLOTLY_ID = "plt_01jaf3p1q2r3s4t5v6w7x8y9z1";
export const EMBED_ID = "emb_01jaf3e1m2b3d4e5f6g7h8j9k0";

const ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz";
let seq = 0;

/** A fresh mock id: `<prefix>_` and 26 lowercase base32 characters. */
function mockId(prefix: string): string {
  seq += 1;
  let n = Date.now() * 1000 + seq;
  let tail = "";
  for (let i = 0; i < 26; i++) {
    tail = ALPHABET[n % 32] + tail;
    n = Math.floor(n / 32);
  }
  return `${prefix}_${tail}`;
}

function randomToken(alphabet: string, n: number): string {
  const bytes = crypto.getRandomValues(new Uint8Array(n));
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function problem(status: number, code: Problem["code"], title: string, detail?: string, errors: Issue[] = []) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const now = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

/** The active persona's role in a `tenant/project` (the tenancy mock's rules). */
function roleIn(scope: string | null): string | null {
  const id = mockPersonaId();
  const p = id ? PERSONAS[id] : undefined;
  if (!p || !scope) return null;
  const order = ["viewer", "editor", "runner", "admin"];
  const a = p.projects[scope] ?? null;
  const b = p.tenants[scope.split("/")[0] ?? ""] ?? null;
  return order.indexOf(a ?? "") >= order.indexOf(b ?? "") ? a : b;
}

function atLeast(request: Request, min: "viewer" | "editor" | "runner" | "admin"): boolean {
  const header = request.headers.get("X-Mapi-Project");
  if (!header) return true; // single-tenant (v0.2) profile: the local administrator
  const order = ["viewer", "editor", "runner", "admin"];
  return order.indexOf(roleIn(header) ?? "") >= order.indexOf(min);
}

const forbidden = (what: string) => problem(403, "FORBIDDEN", "Not allowed", `your role cannot ${what}`);

// ---------------------------------------------------------------------------- state

const WARN_GATE: PromotionGateDecision = {
  outcome: "needs_override",
  scorecard_overall: "warn",
  failing_checks: ["holdout.mape"],
  reasons: [{ code: "SCORECARD_WARN", outcome: "needs_override", check: "holdout.mape", message_plain: "Forecasts were off by 14% on the last 8 weeks." }],
};

/**
 * The endpoint's input contract, validated as `EndpointSchemaSnapshot`, with the example call and
 * the snippets the server's own functions make for it (`scripts/gen_integration_fixtures.py`).
 */
const SNAPSHOT = generated.snapshot as unknown as Endpoint["schema_snapshot"];

export const EXAMPLE_CALL = generated.example as unknown as EndpointScoreCall;

function endpointOf(alias: string, revision: number, patch: Partial<Endpoint> = {}): Endpoint {
  return {
    id: `ep_01jaf3n1${alias.replace(/[^a-z0-9]/g, "").padEnd(18, "0").slice(0, 18)}`,
    alias,
    tenant_id: "acme",
    project_id: "growth",
    ref: `endpoint://growth/${alias}`,
    model_id: MODEL_ID,
    model_version_id: MV_ID,
    model_ref: modelVersion.ref,
    revision,
    schema_snapshot: SNAPSHOT,
    gate: WARN_GATE,
    overridden: true,
    evaluation_policy: { fit_mode: "evaluate_then_refit", schema_change: "block" },
    description: "Weekly budget planning for the EU markets",
    promoted_at: "2026-10-14T16:05:00Z",
    promoted_by: PERSONAS.ana?.id ?? null,
    created_at: "2026-10-10T09:00:00Z",
    updated_at: "2026-10-14T16:05:00Z",
    ...patch,
  };
}

function delivery(i: number, patch: Partial<WebhookDelivery>): WebhookDelivery {
  const ts = new Date(Date.parse("2026-10-15T09:00:00Z") - i * 41 * 60_000).toISOString().replace(/\.\d+Z$/, "Z");
  return {
    id: `dlv_01jaf3d${String(i).padStart(19, "0")}`,
    webhook_id: WEBHOOK_ID,
    event_id: `evt_01jaf3v${String(i).padStart(19, "0")}`,
    event_type: "run.succeeded",
    status: "succeeded",
    attempt: 1,
    response_code: 200,
    duration_ms: 182,
    created_at: ts,
    updated_at: ts,
    ...patch,
  };
}

export interface IntegrationsState {
  endpoints: Map<string, Endpoint>;
  history: Map<string, EndpointRevision[]>;
  webhooks: Map<string, Webhook>;
  deliveries: Map<string, WebhookDelivery[]>;
  plots: SavedPlot[];
  embeds: Map<string, PlotEmbed[]>;
  renders: Map<string, { render: PlotRender; ready: number }>;
}

export function createIntegrationsState(): IntegrationsState {
  const ana = PERSONAS.ana?.id ?? null;
  const ep = endpointOf(ENDPOINT_ALIAS, 2);
  const history: EndpointRevision[] = [
    {
      alias: ENDPOINT_ALIAS,
      revision: 2,
      action: "promote",
      model_version_id: MV_ID,
      model_ref: modelVersion.ref,
      previous_model_version_id: MV_ID,
      gate: WARN_GATE,
      overridden: true,
      reason: "Holdout error accepted for the Q4 plan; refit after the holidays.",
      actor_id: ana,
      created_at: "2026-10-14T16:05:00Z",
    },
    {
      alias: ENDPOINT_ALIAS,
      revision: 1,
      action: "promote",
      model_version_id: MV_ID,
      model_ref: modelVersion.ref,
      previous_model_version_id: null,
      gate: WARN_GATE,
      overridden: true,
      reason: "First production version.",
      actor_id: ana,
      created_at: "2026-10-10T09:00:00Z",
    },
  ];
  const hook: Webhook = {
    id: WEBHOOK_ID,
    tenant_id: "acme",
    project_id: "growth",
    url: "https://hooks.acme.example/mapi",
    description: "Planning pipeline",
    events: ["run.succeeded", "model.version.ready", "endpoint.promoted"],
    active: true,
    secret_hint: "whsec_…Q3fA",
    failure_count: 1,
    last_delivery_at: "2026-10-15T09:00:00Z",
    last_success_at: "2026-10-15T09:00:00Z",
    created_at: "2026-10-01T09:00:00Z",
    updated_at: "2026-10-15T09:00:00Z",
  };
  const disabled: Webhook = {
    ...hook,
    id: WEBHOOK_DISABLED_ID,
    url: "https://old-receiver.acme.example/hook",
    description: "Legacy BI refresh",
    events: ["score.completed"],
    active: false,
    secret_hint: "whsec_…x9Lm",
    failure_count: 20,
    disabled_reason: "Disabled after 20 consecutive failed events (the last answer was HTTP 502).",
    last_success_at: "2026-10-02T09:00:00Z",
  };
  return {
    endpoints: new Map([[ENDPOINT_ALIAS, ep]]),
    history: new Map([[ENDPOINT_ALIAS, history]]),
    webhooks: new Map([
      [hook.id, hook],
      [disabled.id, disabled],
    ]),
    deliveries: new Map([
      [
        WEBHOOK_ID,
        [
          delivery(0, { event_type: "model.version.ready" }),
          delivery(1, { event_type: "endpoint.promoted", status: "retrying", attempt: 2, response_code: 503, duration_ms: 5000, error: "HTTP 503 from the receiver", response_excerpt: "upstream unavailable", next_retry_at: "2026-10-15T09:35:00Z" }),
          delivery(2, { status: "failed", attempt: 8, response_code: 500, duration_ms: 311, error: "HTTP 500 from the receiver", response_excerpt: "Traceback (most recent call last): …" }),
          delivery(3, {}),
        ],
      ],
      [WEBHOOK_DISABLED_ID, [delivery(4, { webhook_id: WEBHOOK_DISABLED_ID, event_type: "score.completed", status: "failed", attempt: 8, response_code: 502, duration_ms: 88, error: "HTTP 502 from the receiver" })]],
    ]),
    plots: [
      {
        id: SAVED_PLOT_ID,
        tenant_id: "acme",
        project_id: "growth",
        title: "EU posterior predictive fan (v1)",
        description: "For the weekly planning deck",
        kind: "vega-lite",
        source_kind: "model_plot",
        source: { model_id: MODEL_ID, version: 1, plot_id: "core.ppc_fan" },
        provenance: "recipe",
        recipe: "core.ppc_fan@1.0.0",
        embeddable: true,
        spec: plotSpec,
        created_by: ana,
        created_at: "2026-10-12T10:00:00Z",
        updated_at: "2026-10-12T10:00:00Z",
      },
      {
        id: SAVED_PLOTLY_ID,
        tenant_id: "acme",
        project_id: "growth",
        title: "Spend by market (3D)",
        description: null,
        kind: "plotly",
        source_kind: "spec",
        source: null,
        provenance: "author",
        recipe: null,
        embeddable: false,
        created_by: ana,
        created_at: "2026-10-13T10:00:00Z",
        updated_at: "2026-10-13T10:00:00Z",
      },
    ],
    embeds: new Map([
      [
        SAVED_PLOT_ID,
        [
          { id: EMBED_ID, plot_id: SAVED_PLOT_ID, tenant_id: "acme", project_id: "growth", display: "mpe_…7xk2", label: "Q4 board deck", status: "active", views: 12, last_viewed_at: "2026-10-15T08:10:00Z", allowed_origins: ["https://acme.notion.site"], created_by: ana, created_at: "2026-10-12T10:05:00Z", expires_at: "2027-01-10T10:05:00Z" },
          { id: "emb_01jaf3e1m2b3d4e5f6g7h8j9k1", plot_id: SAVED_PLOT_ID, tenant_id: "acme", project_id: "growth", display: "mpe_…a01c", label: "Slack preview", status: "revoked", views: 3, revoked_at: "2026-10-13T12:00:00Z", allowed_origins: [], created_by: ana, created_at: "2026-10-12T11:00:00Z", expires_at: "2026-11-11T11:00:00Z" },
        ],
      ],
    ]),
    renders: new Map(),
  };
}

// ---------------------------------------------------------------------------- snippets

const camel = (alias: string) =>
  alias
    .split(/[-_]/)
    .filter(Boolean)
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join("");

/**
 * The server's snippets (`mapi_spec.snippets.render_snippets`), verbatim for the fixture endpoint.
 * Another alias (one a Guided session promoted) or another API base is substituted in: aliases are
 * slugs and the base a plain URL, so neither needs quoting in any language.
 */
export function snippetsFor(alias: string): Snippet[] {
  const swap = (code: string) =>
    code
      .replaceAll(generated.base_url, API)
      .replaceAll(generated.alias, alias)
      .replaceAll(camel(generated.alias), camel(alias));
  return (generated.snippets.items as Snippet[]).map((s) => ({ ...s, code: swap(s.code) }));
}

// ---------------------------------------------------------------------------- render bytes

const PNG_1PX = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="), (c) => c.charCodeAt(0));

function renderBytes(fmt: "png" | "svg" | "pdf"): { bytes: Uint8Array; type: string } {
  if (fmt === "png") return { bytes: PNG_1PX, type: "image/png" };
  if (fmt === "svg") return { bytes: enc.encode('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'), type: "image/svg+xml" };
  return { bytes: enc.encode("%PDF-1.4\n% mock render\n%%EOF\n"), type: "application/pdf" };
}

// ---------------------------------------------------------------------------- handlers

export interface IntegrationDeps {
  /** The core mock's signed-URL object store (`http://127.0.0.1:4443/mock-artifacts/<name>`). */
  artifacts: Map<string, { bytes: Uint8Array; type: string }>;
}

export function createIntegrationHandlers(state: IntegrationsState, deps: IntegrationDeps): HttpHandler[] {
  const scoreLink = () => {
    const bytes = fanArrow();
    deps.artifacts.set("ui/plotdata/ppc_fan.arrow", { bytes, type: "application/vnd.apache.arrow.stream" });
    return artifactLink("ui/plotdata/ppc_fan.arrow", bytes, "application/vnd.apache.arrow.stream");
  };
  const endpointOr404 = (alias: string) => state.endpoints.get(alias) ?? null;
  const notFound = (what: string) => problem(404, "NOT_FOUND", "Not found", `no such ${what}`);

  return [
    // ------------------------------------------------------------------ endpoints
    http.get(`${API}/v1/endpoints`, () => page([...state.endpoints.values()].sort((a, b) => a.alias.localeCompare(b.alias)))),
    http.get(`${API}/v1/endpoints/:alias`, ({ params }) => {
      const ep = endpointOr404(String(params.alias));
      return ep ? HttpResponse.json(ep) : notFound("endpoint");
    }),
    http.put(`${API}/v1/endpoints/:alias`, async ({ params, request }) => {
      const alias = String(params.alias);
      if (!atLeast(request, "admin")) return forbidden("promote endpoints (admin)");
      const body = (await request.json()) as EndpointPromote;
      const row = state.endpoints.get(alias) ?? null;
      const current = row?.revision ?? 0;
      if (body.expected_revision != null && body.expected_revision !== current) {
        return problem(409, "CONFLICT", "Conflict", `endpoint '${alias}' is at revision ${current}, not ${body.expected_revision}`);
      }
      const known = body.model_version_id === MV_ID || (body.model ?? "").endsWith("/eu-geo-mmm@v1");
      if (!known) return notFound("model version");
      if (row && row.model_version_id === MV_ID) return HttpResponse.json(row);
      if (!body.override) {
        return problem(409, "OVERRIDE_REQUIRED", "The promotion needs an admin override", "the scorecard warns; a project admin may override with a reason", [
          { code: "OVERRIDE_REQUIRED", pointer: "", severity: "warning", message: "Forecasts were off by 14% on the last 8 weeks.", context: { gate_code: "SCORECARD_WARN", check: "holdout.mape" } },
        ]);
      }
      if (!body.reason?.trim()) return problem(422, "SCHEMA_INVALID", "Invalid request", "an override needs a reason", [{ code: "SCHEMA_INVALID", pointer: "/reason", message: "an override needs a reason" }]);
      const ts = now();
      const next = endpointOf(alias, current + 1, { promoted_at: ts, updated_at: ts, created_at: row?.created_at ?? ts, description: body.description ?? row?.description ?? null, ...(body.evaluation_policy ? { evaluation_policy: body.evaluation_policy } : {}) });
      state.endpoints.set(alias, next);
      const hist = state.history.get(alias) ?? [];
      hist.unshift({ alias, revision: next.revision, action: "promote", model_version_id: MV_ID, model_ref: modelVersion.ref, previous_model_version_id: row?.model_version_id ?? null, gate: WARN_GATE, overridden: true, reason: body.reason, actor_id: PERSONAS[mockPersonaId() ?? ""]?.id ?? null, created_at: ts });
      state.history.set(alias, hist);
      return HttpResponse.json(next);
    }),
    http.delete(`${API}/v1/endpoints/:alias`, ({ params, request }) => {
      if (!atLeast(request, "admin")) return forbidden("delete endpoints (admin)");
      if (!state.endpoints.delete(String(params.alias))) return notFound("endpoint");
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${API}/v1/endpoints/:alias/history`, ({ params }) => {
      const hist = state.history.get(String(params.alias));
      return hist ? page(hist) : notFound("endpoint");
    }),
    http.post(`${API}/v1/endpoints/:alias/rollback`, async ({ params, request }) => {
      const alias = String(params.alias);
      if (!atLeast(request, "admin")) return forbidden("roll endpoints back (admin)");
      const row = state.endpoints.get(alias);
      if (!row) return notFound("endpoint");
      const body = (await request.json()) as { reason: string; to_revision?: number | null };
      const hist = state.history.get(alias) ?? [];
      const target = body.to_revision ? hist.find((h) => h.revision === body.to_revision) : hist.find((h) => h.revision < row.revision);
      if (!target || target.revision >= row.revision) return problem(409, "CONFLICT", "Conflict", "there is no earlier revision to roll back to");
      const ts = now();
      const next = { ...row, revision: row.revision + 1, model_version_id: target.model_version_id, model_ref: target.model_ref, promoted_at: ts, updated_at: ts };
      state.endpoints.set(alias, next);
      hist.unshift({ alias, revision: next.revision, action: "rollback", model_version_id: target.model_version_id, model_ref: target.model_ref, previous_model_version_id: row.model_version_id, gate: target.gate, overridden: false, reason: body.reason, actor_id: PERSONAS[mockPersonaId() ?? ""]?.id ?? null, created_at: ts });
      return HttpResponse.json(next);
    }),
    http.get(`${API}/v1/endpoints/:alias/schema`, ({ params }) => {
      const ep = endpointOr404(String(params.alias));
      if (!ep) return notFound("endpoint");
      const out: EndpointSchema = { alias: ep.alias, ref: ep.ref, model_ref: ep.model_ref, revision: ep.revision, snapshot: ep.schema_snapshot, example: EXAMPLE_CALL, max_inline_bytes: 5 * 1024 * 1024 };
      return HttpResponse.json(out);
    }),
    http.post(`${API}/v1/endpoints/:alias/validate`, async ({ params, request }) => {
      const ep = endpointOr404(String(params.alias));
      if (!ep) return notFound("endpoint");
      const body = (await request.json()) as EndpointScoreCall;
      const mode = body.mode ?? "predict";
      if (!(ep.schema_snapshot.score_modes ?? []).includes(mode)) {
        return HttpResponse.json({ valid: false, issues: [{ code: "MODE_NOT_SUPPORTED", pointer: "/mode", severity: "error", message: `the model does not support mode '${mode}'`, suggested_action: "Use one of predict, forecast, counterfactual or evaluate." }] });
      }
      if (mode === "forecast" && !body.horizon) {
        return HttpResponse.json({ valid: false, issues: [{ code: "SCHEMA_INVALID", pointer: "/horizon", severity: "error", message: "a forecast needs a horizon" }] });
      }
      return HttpResponse.json({ valid: true, issues: [], request: { ...body, model: ep.model_ref } });
    }),
    http.post(`${API}/v1/endpoints/:alias/score`, async ({ params, request }) => {
      const ep = endpointOr404(String(params.alias));
      if (!ep) return notFound("endpoint");
      if (!atLeast(request, "runner")) return forbidden("score endpoints (runner)");
      const body = (await request.json()) as EndpointScoreCall;
      if (!(ep.schema_snapshot.score_modes ?? []).includes(body.mode ?? "predict")) {
        return problem(422, "MODE_NOT_SUPPORTED", "Score mode not supported", `the model does not support mode '${body.mode}'`);
      }
      return HttpResponse.json(
        { done: true, run: { ...runDone, id: "run_01jaf3q2x8m9v7k5h4g3f2e1s5", kind: "score", model_version_id: ep.model_version_id, spec_id: null, spec_revision: null, labels: { endpoint: ep.alias } }, outputs: [scoreLink()] },
        { headers: { "Preference-Applied": "wait=30", Location: "/v1/runs/run_01jaf3q2x8m9v7k5h4g3f2e1s5" } },
      );
    }),
    http.get(`${API}/v1/endpoints/:alias/snippets`, ({ params, request }) => {
      const alias = String(params.alias);
      if (!state.endpoints.has(alias)) return notFound("endpoint");
      const want = new URL(request.url).searchParams.getAll("language");
      return HttpResponse.json({ alias, items: snippetsFor(alias).filter((s) => !want.length || want.includes(s.language)) });
    }),

    // ------------------------------------------------------------------ webhooks
    http.get(`${API}/v1/webhooks`, ({ request }) => (atLeast(request, "admin") ? page([...state.webhooks.values()]) : forbidden("see webhooks (admin)"))),
    http.post(`${API}/v1/webhooks`, async ({ request }) => {
      if (!atLeast(request, "admin")) return forbidden("create webhooks (admin)");
      const body = (await request.json()) as WebhookCreate;
      let host = "";
      let protocol = "";
      try {
        const u = new URL(body.url);
        host = u.hostname;
        protocol = u.protocol;
      } catch {
        host = "";
      }
      if (protocol !== "https:" || !host.includes(".") || /^[\d.]+$|^\[/.test(host) || /(^|\.)(localhost|local|internal)$/.test(host)) {
        return problem(422, "WEBHOOK_URL_BLOCKED", "The webhook URL is not allowed", "webhooks are delivered to public https hosts only", [
          { code: "WEBHOOK_URL_BLOCKED", pointer: "/url", message: "webhooks are delivered to public https hosts on port 443 or 8443, never to internal or private addresses" },
        ]);
      }
      if (state.webhooks.size >= 10) return problem(409, "CONFLICT", "Conflict", "a project has at most 10 webhooks");
      const secret = `whsec_${randomToken("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", 43)}`;
      const ts = now();
      const wh: Webhook = { id: mockId("whk"), tenant_id: "acme", project_id: "growth", url: body.url, description: body.description ?? null, events: body.events, active: body.active ?? true, secret_hint: `whsec_…${secret.slice(-4)}`, failure_count: 0, created_at: ts, updated_at: ts };
      state.webhooks.set(wh.id, wh);
      state.deliveries.set(wh.id, []);
      return HttpResponse.json({ webhook: wh, secret }, { status: 201 });
    }),
    http.get(`${API}/v1/webhooks/:id`, ({ params }) => {
      const wh = state.webhooks.get(String(params.id));
      return wh ? HttpResponse.json(wh) : notFound("webhook");
    }),
    http.patch(`${API}/v1/webhooks/:id`, async ({ params, request }) => {
      if (!atLeast(request, "admin")) return forbidden("change webhooks (admin)");
      const wh = state.webhooks.get(String(params.id));
      if (!wh) return notFound("webhook");
      const body = (await request.json()) as WebhookUpdate;
      const next: Webhook = {
        ...wh,
        ...(body.url != null ? { url: body.url } : {}),
        ...(body.description !== undefined ? { description: body.description } : {}),
        ...(body.events != null ? { events: body.events } : {}),
        ...(body.active != null ? { active: body.active, ...(body.active ? { failure_count: 0, disabled_reason: null } : {}) } : {}),
        updated_at: now(),
      };
      state.webhooks.set(wh.id, next);
      return HttpResponse.json(next);
    }),
    http.delete(`${API}/v1/webhooks/:id`, ({ params, request }) => {
      if (!atLeast(request, "admin")) return forbidden("delete webhooks (admin)");
      if (!state.webhooks.delete(String(params.id))) return notFound("webhook");
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/webhooks/[^/:]+:rotate-secret$`), ({ request }) => {
      if (!atLeast(request, "admin")) return forbidden("rotate webhook secrets (admin)");
      const id = decodeURIComponent(new URL(request.url).pathname.split("/").at(-1)!.split(":")[0]!);
      const wh = state.webhooks.get(id);
      if (!wh) return notFound("webhook");
      const secret = `whsec_${randomToken("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", 43)}`;
      const next = { ...wh, secret_hint: `whsec_…${secret.slice(-4)}`, updated_at: now() };
      state.webhooks.set(id, next);
      return HttpResponse.json({ webhook: next, secret });
    }),
    http.get(`${API}/v1/webhooks/:id/deliveries`, ({ params, request }) => {
      const id = String(params.id);
      const list = state.deliveries.get(id);
      if (!list) return notFound("webhook");
      // A replay is "delivered" a moment after it was queued.
      for (const d of list) {
        if (d.status === "pending" && Date.now() - Date.parse(d.created_at) > 800) Object.assign(d, { status: "succeeded", response_code: 200, duration_ms: 140, updated_at: now() });
      }
      const status = new URL(request.url).searchParams.get("status");
      return page(list.filter((d) => !status || d.status === status));
    }),
    http.post(new RegExp(`^${API_RE}/v1/webhooks/[^/]+/deliveries/[^/:]+:replay$`), ({ request }) => {
      if (!atLeast(request, "admin")) return forbidden("replay deliveries (admin)");
      const parts = new URL(request.url).pathname.split("/");
      const webhookId = decodeURIComponent(parts[3] ?? "");
      const deliveryId = decodeURIComponent((parts.at(-1) ?? "").split(":")[0] ?? "");
      const list = state.deliveries.get(webhookId);
      const src = list?.find((d) => d.id === deliveryId);
      if (!list || !src) return notFound("delivery");
      const wh = state.webhooks.get(webhookId);
      if (wh && !wh.active) return problem(409, "CONFLICT", "Conflict", "the webhook is disabled; enable it before replaying");
      const ts = new Date().toISOString();
      const d: WebhookDelivery = { ...src, id: mockId("dlv"), status: "pending", attempt: 1, response_code: null, duration_ms: null, error: null, response_excerpt: null, next_retry_at: null, replay_of: src.id, created_at: ts, updated_at: ts };
      list.unshift(d);
      return HttpResponse.json(d, { status: 202 });
    }),

    // ------------------------------------------------------------------ saved plots, renders, embeds
    http.get(`${API}/v1/plots`, () => page(state.plots)),
    http.post(`${API}/v1/plots`, async ({ request }) => {
      if (!atLeast(request, "editor")) return forbidden("save plots (editor)");
      const body = (await request.json()) as SavedPlotCreate;
      if (!body.source && !body.spec) return problem(422, "SCHEMA_INVALID", "Invalid request", "name a model plot (source) or send a spec");
      if (body.source && (body.source.model_id !== MODEL_ID || body.source.version !== 1)) return notFound("model version");
      const ts = now();
      const recipe = body.source ? plotSpec.provenance.recipe ?? null : null;
      const saved: SavedPlot = {
        id: mockId("plt"),
        tenant_id: "acme",
        project_id: "growth",
        title: body.title,
        description: body.description ?? null,
        kind: body.spec?.kind ?? "vega-lite",
        source_kind: body.source ? "model_plot" : "spec",
        source: body.source ?? null,
        provenance: body.source ? "recipe" : "author",
        recipe,
        embeddable: Boolean(body.source),
        spec: body.spec ?? plotSpec,
        created_by: PERSONAS[mockPersonaId() ?? ""]?.id ?? null,
        created_at: ts,
        updated_at: ts,
      };
      state.plots.unshift(saved);
      return HttpResponse.json(saved, { status: 201 });
    }),
    http.get(`${API}/v1/plots/renders/:runId`, ({ params }) => {
      const r = state.renders.get(String(params.runId));
      if (!r) return notFound("render");
      if (!r.render.done && Date.now() >= r.ready) {
        const name = `renders/${r.render.run.labels?.plot ?? "plot"}.${r.render.format}`;
        const { bytes, type } = renderBytes(r.render.format);
        deps.artifacts.set(name, { bytes, type });
        r.render = { ...r.render, done: true, run: { ...r.render.run, state: "succeeded", finished_at: now() }, outputs: [artifactLink(name, bytes, type)] };
      }
      return HttpResponse.json(r.render);
    }),
    http.get(`${API}/v1/plots/:plotId`, ({ params }) => {
      const p = state.plots.find((x) => x.id === params.plotId);
      return p ? HttpResponse.json(p) : notFound("saved plot");
    }),
    http.delete(`${API}/v1/plots/:plotId`, ({ params, request }) => {
      if (!atLeast(request, "editor")) return forbidden("delete plots (editor)");
      const i = state.plots.findIndex((x) => x.id === params.plotId);
      if (i < 0) return notFound("saved plot");
      state.plots.splice(i, 1);
      state.embeds.delete(String(params.plotId));
      return new HttpResponse(null, { status: 204 });
    }),
    http.post(new RegExp(`^${API_RE}/v1/plots:render$`), async ({ request }) => {
      if (!atLeast(request, "runner")) return forbidden("start render runs (runner)");
      const body = (await request.json()) as PlotRenderRequest;
      const plot = body.saved_plot_id ? state.plots.find((p) => p.id === body.saved_plot_id) : null;
      if (body.saved_plot_id && !plot) return notFound("saved plot");
      if (plot?.kind === "plotly") {
        return problem(422, "MODE_NOT_SUPPORTED", "Score mode not supported", "rendering Plotly figures needs kaleido and a Chrome in the runner image; an operator enables it with MAPI_RENDER_PLOTLY_ENABLED");
      }
      const fmt = body.format ?? "png";
      const runId = mockId("run");
      const run = { ...runDone, id: runId, kind: "render" as const, state: fmt === "pdf" ? ("running" as const) : ("succeeded" as const), labels: { plot: plot?.id ?? "plot" }, spec_id: null, spec_revision: null, model_version_id: null };
      if (fmt === "pdf") {
        // Slow formats come back 202 and finish on a later poll.
        const render: PlotRender = { done: false, format: fmt, run, outputs: [] };
        state.renders.set(runId, { render, ready: Date.now() + 600 });
        return HttpResponse.json(render, { status: 202, headers: { Location: `/v1/plots/renders/${runId}` } });
      }
      const name = `renders/${plot?.id ?? "plot"}.${fmt}`;
      const { bytes, type } = renderBytes(fmt);
      deps.artifacts.set(name, { bytes, type });
      const render: PlotRender = { done: true, format: fmt, run, outputs: [artifactLink(name, bytes, type)] };
      state.renders.set(runId, { render, ready: 0 });
      return HttpResponse.json(render);
    }),
    http.get(`${API}/v1/plots/:plotId/embeds`, ({ params }) => {
      if (!state.plots.some((p) => p.id === params.plotId)) return notFound("saved plot");
      return page(state.embeds.get(String(params.plotId)) ?? []);
    }),
    http.post(`${API}/v1/plots/:plotId/embeds`, async ({ params, request }) => {
      if (!atLeast(request, "editor")) return forbidden("create embed links (editor)");
      const plot = state.plots.find((p) => p.id === params.plotId);
      if (!plot) return notFound("saved plot");
      if (!plot.embeddable) return problem(422, "EMBED_NOT_ALLOWED", "This plot cannot be embedded", "only plots of the platform's recipes can be shared by link");
      const body = (await request.json()) as PlotEmbedCreate;
      const days = body.expires_in_days ?? 30;
      const token = `mpe_${randomToken(ALPHABET, 40)}`;
      const ts = now();
      const embed: PlotEmbed = {
        id: mockId("emb"),
        plot_id: plot.id,
        tenant_id: "acme",
        project_id: "growth",
        display: `mpe_…${token.slice(-4)}`,
        label: body.label ?? null,
        status: "active",
        views: 0,
        allowed_origins: body.allowed_origins ?? [],
        created_by: PERSONAS[mockPersonaId() ?? ""]?.id ?? null,
        created_at: ts,
        expires_at: new Date(Date.now() + days * 86_400_000).toISOString().replace(/\.\d+Z$/, "Z"),
      };
      const list = state.embeds.get(plot.id) ?? [];
      list.unshift(embed);
      state.embeds.set(plot.id, list);
      return HttpResponse.json({ embed, token, url: `${env.plotsOrigin}/embed/${token}` }, { status: 201 });
    }),
    http.delete(`${API}/v1/plots/:plotId/embeds/:embedId`, ({ params, request }) => {
      if (!atLeast(request, "editor")) return forbidden("revoke embed links (editor)");
      const list = state.embeds.get(String(params.plotId));
      const e = list?.find((x) => x.id === params.embedId);
      if (!e) return notFound("embed");
      if (e.status === "active") Object.assign(e, { status: "revoked", revoked_at: now() });
      return new HttpResponse(null, { status: 204 });
    }),
  ];
}

