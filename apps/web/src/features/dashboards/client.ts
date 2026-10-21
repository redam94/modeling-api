/**
 * The data clients of a dashboard page (plan I-6): one interface, {@link DashboardClient}, for
 * a member (the analyst's preview), a client user and a share link, so the readout page never
 * knows who is looking.
 *
 * - {@link memberClient}: the signed-in routes `/v1/dashboards/{id}/…` through the typed
 *   `api` (session cookie, CSRF header, `X-Mapi-Project`). `api/client.ts` is loaded on the
 *   first call (a dynamic import), so the share app's static import closure stays free of the
 *   session middleware.
 * - {@link shareClient}: the public routes `/v1/shared/…` with plain `fetch`: no cookies
 *   (`credentials: "omit"`), no CSRF header, no project header, no referrer. The share session
 *   rides in `X-Mapi-Share` only; an expired session is refreshed once and the request retried.
 *
 * Both are {@link DashboardClientV2}s: layout 2 (v0.17) adds scenario rows ("Try a case"),
 * batch scoring ("Score a file"), the predictions download and the pictures, both read as a
 * Blob (the share client sends the session header, never a cookie), and keeping a tried case
 * (members and client users only: a share link has no keep route).
 *
 * The hooks at the end key their queries by the client's `cacheKey` (see `keys.ts`).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { env } from "@/env";
import type { Problem } from "@/api/types";

import { dk, memberCacheKey, shareCacheKey } from "./keys";
import {
  isAnalystDashboard,
  isFullReadout,
  isLayout2,
  layout1Spec,
  panelMeta,
  readoutPollMs,
  SHARE_SESSION_HEADER,
  TERMINAL_READOUT,
  type ActualsSubmit,
  type ActualsTemplate,
  type AnyDashboardSpec,
  type BatchForm,
  type BatchSubmit,
  type BatchTemplate,
  type ClientDashboard,
  type Dashboard,
  type DashboardClient,
  type DashboardClientV2,
  type DashboardGrid,
  type DashboardProblem,
  type DashboardSpecV2,
  type EffectiveInputs,
  type KpiDef,
  type Readout,
  type ReadoutHeader,
  type ReadoutList,
  type ReadoutQuery,
  type ScenarioForm,
  type ScenarioRowsRequest,
  type ScenarioSave,
  type SectionKey,
  type ShareSession,
  type ShareSessionHandle,
  type SubjectDef,
  type TabKey,
  type TileKey,
  type WaitOptions,
  type WhatIfResult,
} from "./types";

export type { DashboardClient, DashboardClientV2, ShareSessionHandle } from "./types";

// ------------------------------------------------------------------------------ errors

/** An HTTP error of a share-link route (the share app does not load `api/client.ts`'s `ApiError`). */
export class ShareApiError extends Error {
  override name = "ShareApiError";

  constructor(
    readonly status: number,
    readonly problem: Problem | null,
    message?: string,
    readonly retryAfter: number | null = null,
  ) {
    super(message ?? problem?.detail ?? problem?.title ?? `HTTP ${status}`);
  }

  get code(): string | null {
    return this.problem?.code ?? null;
  }
}

function looksLikeProblem(value: unknown): value is Problem {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.code === "string" && typeof v.title === "string" && typeof v.status === "number";
}

/** The status and problem of anything a dashboard client throws (`ApiError` or {@link ShareApiError}). */
export function problemOf(err: unknown): DashboardProblem | null {
  if (!(err instanceof Error)) return null;
  const e = err as Error & { status?: unknown; problem?: unknown; retryAfter?: unknown };
  if (typeof e.status !== "number") return null;
  const problem = looksLikeProblem(e.problem) ? e.problem : null;
  return { status: e.status, code: problem?.code ?? null, problem, retryAfter: typeof e.retryAfter === "number" ? e.retryAfter : null };
}

/** A plain message for a dashboard error (the problem's detail, else its title). */
export function dashboardErrorMessage(err: unknown): string {
  const p = problemOf(err);
  if (p?.problem) return p.problem.detail ? `${p.problem.title}. ${p.problem.detail}` : p.problem.title;
  if (err instanceof TypeError) return "We could not reach the service. Check your connection and try again.";
  if (err instanceof Error) return err.message;
  return String(err);
}

function retryAfterOf(value: string | null): number | null {
  if (!value) return null;
  const v = value.trim();
  if (/^\d{1,9}$/.test(v)) return Number(v);
  const t = Date.parse(v);
  return Number.isNaN(t) ? null : Math.max(0, Math.ceil((t - Date.now()) / 1000));
}

// ------------------------------------------------------------------------------ pure helpers

export interface EffectiveInputsOptions {
  /** A client user (not a member): batch scoring of an image model is for members only. */
  client?: boolean;
}

/** `access._ACCURACY_KIND`: what an actuals upload measures, by preset (default `error`). */
const ACCURACY_KIND: Partial<Record<DashboardSpecV2["preset"], NonNullable<EffectiveInputs["accuracy_kind"]>>> = {
  classifier: "classification",
  causal_impact: "effect",
};

/**
 * `access.effective_inputs`: the spec's inputs, narrowed by a share link's allowed inputs
 * (`allowed`; `null`: a signed-in caller). Layout 2 adds scenario rows, batch scoring (an
 * image model's only for members) and the accuracy kind of an upload.
 */
export function effectiveInputs(
  spec: AnyDashboardSpec,
  mode: EffectiveInputs["actuals_mode"] = null,
  allowed: readonly string[] | null = null,
  opts: EffectiveInputsOptions = {},
): EffectiveInputs {
  const narrow = allowed === null ? null : new Set(allowed);
  const on = (key: string, enabled: boolean) => enabled && (narrow === null || narrow.has(key));
  if (!isLayout2(spec)) {
    const whatif = on("whatif", spec.whatif?.enabled ?? true);
    const actuals = on("actuals", spec.actuals?.enabled ?? false);
    return {
      whatif,
      exact: whatif && (spec.whatif?.exact ?? false),
      actuals,
      actuals_mode: actuals ? (mode ?? "compared") : null,
      refit_requests: actuals && (spec.actuals?.allow_refit_requests ?? false),
    };
  }
  const planner = spec.preset === "mmm_planner" ? (spec.mmm ?? null) : null;
  const whatif = planner !== null && on("whatif", planner.whatif?.enabled ?? true);
  const actuals = on("actuals", spec.actuals?.enabled ?? false);
  const member = narrow === null && !(opts.client ?? false);
  const batch = !!spec.batch && on("batch", spec.batch.enabled ?? false) && (spec.preset !== "image" || member);
  return {
    whatif,
    exact: whatif && (planner?.whatif?.exact ?? false),
    actuals,
    actuals_mode: actuals ? (mode ?? "compared") : null,
    refit_requests: actuals && (spec.actuals?.allow_refit_requests ?? false),
    scenario_rows: on("scenario_rows", spec.scenario_rows?.enabled ?? false),
    batch,
    accuracy_kind: actuals ? (ACCURACY_KIND[spec.preset] ?? "error") : null,
  };
}

/** `access._tabs`: no scenarios without what-ifs, no upload without actuals. */
export function visibleTabs(tabs: readonly TabKey[], inputs: EffectiveInputs): TabKey[] {
  return tabs.filter((t) => !(t === "scenarios" && !inputs.whatif) && !(t === "update" && !inputs.actuals));
}

/** `access._sections`: the layout 2 readout sections the viewer can use (each input only when allowed). */
export function visibleSections(inputs: EffectiveInputs): SectionKey[] {
  const out: SectionKey[] = ["overview"];
  if (inputs.scenario_rows) out.push("try");
  if (inputs.actuals) out.push("accuracy");
  if (inputs.batch) out.push("batch");
  out.push("history");
  return out;
}

const SUBJECT_UNIT: Record<SubjectDef["unit"], KpiDef["unit"]> = {
  count: "count",
  currency: "currency",
  ratio: "ratio",
  percent: "percent",
  probability: "percent",
  rate: "ratio",
  number: "count",
  label: "count",
  index: "count",
};

/** `dashboard_v2.kpi_of_subject`: the layout 1 KPI of a subject (`ClientDashboard.kpi` stays required). */
export function kpiOfSubject(subject: SubjectDef): KpiDef {
  return {
    variable: subject.variable,
    label: subject.label,
    unit: SUBJECT_UNIT[subject.unit],
    currency: subject.currency ?? null,
    decimals: Math.min(subject.decimals ?? 0, 4),
    value_per_unit: subject.value_per_unit ?? null,
  };
}

const PLANNER_TABS: readonly TabKey[] = ["this_period", "trends", "scenarios", "update", "history"];
const DEFAULT_PLANNER_TILES: readonly TileKey[] = ["expected", "vs_expected", "return_on_media", "in_range_spend"];

/**
 * What a client would see of the analyst's `Dashboard` (the preview): the draft spec (or the
 * published one) with `latest` as its latest readout.
 */
export function toClientDashboard(d: Dashboard, latest: Readout | null, use: "draft" | "published" = "draft"): ClientDashboard {
  const stored = use === "published" && d.published_revision ? d.published_revision.spec : d.draft_spec;
  if (isLayout2(stored)) return toClientDashboardV2(d, stored, latest);
  const spec = layout1Spec(stored);
  const inputs = effectiveInputs(spec);
  return {
    id: d.id,
    title: spec.title,
    client_name: spec.client_name ?? null,
    summary: spec.summary ?? null,
    note: spec.note ?? null,
    period_label: spec.period_label ?? "week",
    kpi: spec.kpi,
    channels: spec.channels,
    tiles: spec.tiles ?? [],
    tabs: visibleTabs(spec.tabs ?? ["this_period"], inputs),
    inputs,
    live_inputs: d.live_inputs ?? "ok",
    show_technical: spec.show_technical ?? false,
    latest_readout: latest,
    ends_at: null,
  };
}

/** The layout 2 half of {@link toClientDashboard} (`access.client_dashboard`; no upgrade note in a preview). */
function toClientDashboardV2(d: Dashboard, spec: DashboardSpecV2, latest: Readout | null): ClientDashboard {
  // what a client gets (`access.effective_inputs(client=True)`): never "Score a file" of images
  const inputs = effectiveInputs(spec, null, null, { client: true });
  const planner = spec.preset === "mmm_planner" ? (spec.mmm ?? null) : null;
  const rows = spec.scenario_rows;
  const scenarioForm: ScenarioForm | null =
    inputs.scenario_rows && rows ? { max_rows: rows.max_rows ?? 5, outputs: rows.outputs ?? [], inputs: rows.inputs ?? [], horizon: rows.horizon ?? null } : null;
  let batchForm: BatchForm | null = null;
  if (inputs.batch && spec.batch) {
    const id = spec.batch.id_column;
    const drivers = (rows?.inputs ?? []).map((i) => i.name).filter((n) => n !== id);
    batchForm = { max_rows: spec.batch.max_rows ?? 2000, id_column: id, columns: [id, ...drivers].slice(0, 64), outputs: spec.batch.outputs };
  }
  return {
    id: d.id,
    title: spec.title,
    client_name: spec.client_name ?? null,
    summary: spec.summary ?? null,
    note: spec.note ?? null,
    period_label: spec.period_label ?? "period",
    kpi: kpiOfSubject(spec.subject),
    channels: planner?.channels ?? [],
    tiles: planner ? (planner.tiles ?? DEFAULT_PLANNER_TILES).filter((t) => t !== "panel") : [],
    tabs: planner ? visibleTabs(PLANNER_TABS, inputs) : [],
    inputs,
    live_inputs: d.live_inputs ?? "ok",
    show_technical: spec.show_technical ?? false,
    latest_readout: latest,
    ends_at: null,
    layout_version: 2,
    preset: spec.preset,
    subject: spec.subject,
    panels_meta: spec.panels.map(panelMeta),
    scenario_form: scenarioForm,
    batch_form: batchForm,
    sections: visibleSections(inputs),
    upgrade_note: null,
  };
}

function readoutQuery(q: ReadoutQuery = {}): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  if (q.kind) out.kind = q.kind;
  if (q.beforeSeq !== undefined) out.before_seq = q.beforeSeq;
  if (q.limit !== undefined) out.limit = q.limit;
  return out;
}

function waitHeader(waitSeconds: number | undefined): Record<string, string> {
  return waitSeconds && waitSeconds > 0 ? { Prefer: `wait=${Math.min(30, Math.round(waitSeconds))}` } : {};
}

// ------------------------------------------------------------------------------ member and client users

export interface MemberClientOptions {
  /** `client` for a client user (the server answers `ClientDashboard`); default `member`. */
  kind?: "member" | "client";
  /** Which spec an analyst's preview shows (default: the draft). */
  spec?: "draft" | "published";
}

/** The signed-in client of dashboard `dashboardId` in project `scopeKey` (`tenant/project`). */
export function memberClient(scopeKey: string, dashboardId: string, opts: MemberClientOptions = {}): DashboardClientV2 {
  const kind = opts.kind ?? "member";
  const load = () => import("@/api/client");
  const path = { dashboard_id: dashboardId };

  const readoutPage = async (q?: ReadoutQuery, visibility?: Readout["visibility"]): Promise<ReadoutList> => {
    const { api, projectHeaders, unwrap } = await load();
    return unwrap(
      api.GET("/v1/dashboards/{dashboard_id}/readouts", {
        params: { path, query: { ...readoutQuery(q), ...(visibility ? { visibility } : {}) } },
        headers: projectHeaders(scopeKey),
      }),
    );
  };
  const readout = async (seq: number): Promise<Readout> => {
    const { api, projectHeaders, unwrap } = await load();
    return unwrap(api.GET("/v1/dashboards/{dashboard_id}/readouts/{seq}", { params: { path: { ...path, seq } }, headers: projectHeaders(scopeKey) }));
  };

  return {
    kind,
    cacheKey: memberCacheKey(scopeKey, dashboardId, kind),
    async dashboard() {
      const { api, projectHeaders, unwrap } = await load();
      const d = await unwrap(api.GET("/v1/dashboards/{dashboard_id}", { params: { path }, headers: projectHeaders(scopeKey) }));
      if (!isAnalystDashboard(d)) return d;
      // The analyst sees every readout: the client's "latest" is the newest ready, published one.
      const published = d.published_revision ? await readoutPage({ limit: 20 }, "published") : { items: [] };
      const head = published.items?.find((r) => r.status === "ready") ?? null;
      return toClientDashboard(d, head ? await readout(head.seq) : null, opts.spec ?? "draft");
    },
    async readouts(q) {
      return (await readoutPage(q)).items ?? [];
    },
    readoutPage: (q) => readoutPage(q),
    readout,
    async grid() {
      const { api, projectHeaders, unwrap } = await load();
      try {
        return await unwrap(api.GET("/v1/dashboards/{dashboard_id}/grid", { params: { path }, headers: projectHeaders(scopeKey) }));
      } catch (err) {
        if (problemOf(err)?.status === 409) return null;
        throw err;
      }
    },
    async evaluate(multipliers) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(api.POST("/v1/dashboards/{dashboard_id}/whatif:evaluate", { params: { path }, headers: projectHeaders(scopeKey), body: { multipliers } }));
    },
    async saveScenario(s, idemKey, o = {}) {
      const { api, projectHeaders, unwrap } = await load();
      // 200: the readout (grid result); 202: its header only (the exact result is running).
      // The OpenAPI document declares only the 200 shape; `isFullReadout` tells them apart.
      const data: Readout | ReadoutHeader = await unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/scenarios", {
          params: { path },
          headers: { ...projectHeaders(scopeKey), "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) },
          body: s,
        }),
      );
      return data;
    },
    async actualsTemplate() {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(api.GET("/v1/dashboards/{dashboard_id}/actuals/template", { params: { path }, headers: projectHeaders(scopeKey) }));
    },
    async submitActuals(body, idemKey) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/actuals", { params: { path }, headers: { ...projectHeaders(scopeKey), "Idempotency-Key": idemKey }, body }),
      );
    },
    async predict(body: ScenarioRowsRequest, idemKey: string, o: WaitOptions = {}) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/predictions", {
          params: { path },
          headers: { ...projectHeaders(scopeKey), "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) },
          body,
        }),
      );
    },
    async batchTemplate() {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(api.GET("/v1/dashboards/{dashboard_id}/batch/template", { params: { path }, headers: projectHeaders(scopeKey) }));
    },
    async submitBatch(body: BatchSubmit, idemKey: string, o: WaitOptions = {}) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(
        api.POST("/v1/dashboards/{dashboard_id}/batch", {
          params: { path },
          headers: { ...projectHeaders(scopeKey), "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) },
          body,
        }),
      );
    },
    async downloadPredictions(seq: number) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/readouts/{seq}/predictions.csv", {
          params: { path: { ...path, seq } },
          headers: { ...projectHeaders(scopeKey), Accept: "text/csv, application/problem+json" },
          parseAs: "blob",
        }),
      );
    },
    async asset(assetId: string) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(
        api.GET("/v1/dashboards/{dashboard_id}/assets/{asset_id}", {
          params: { path: { ...path, asset_id: assetId } },
          headers: { ...projectHeaders(scopeKey), Accept: "image/png, application/problem+json" },
          parseAs: "blob",
        }),
      );
    },
    async keep(seq: number) {
      const { api, projectHeaders, unwrap } = await load();
      return unwrap(api.POST("/v1/dashboards/{dashboard_id}/readouts/{seq}:keep", { params: { path: { ...path, seq } }, headers: projectHeaders(scopeKey) }));
    },
  };
}

// ------------------------------------------------------------------------------ share links

export interface ShareFetchOptions {
  /** The API base (default `env.apiBase`). */
  baseUrl?: string;
}

interface ShareRequest {
  method?: "GET" | "POST";
  query?: Record<string, string | number> | undefined;
  body?: unknown;
  headers?: Record<string, string>;
}

async function shareResponse<T>(response: Response): Promise<{ data: T; status: number }> {
  if (response.ok) {
    if (response.status === 204) return { data: undefined as T, status: 204 };
    return { data: (await response.json()) as T, status: response.status };
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  throw new ShareApiError(response.status, looksLikeProblem(body) ? body : null, undefined, retryAfterOf(response.headers.get("Retry-After")));
}

/** One public request: never cookies, never the CSRF or project header, never a referrer. */
function publicFetch(base: string, path: string, req: ShareRequest): Promise<Response> {
  const url = new URL(`${base}${path}`);
  for (const [k, v] of Object.entries(req.query ?? {})) url.searchParams.set(k, String(v));
  const headers: Record<string, string> = { Accept: "application/json", ...(req.headers ?? {}) };
  if (req.body !== undefined) headers["Content-Type"] = "application/json";
  // Resolve fetch at call time so test spies and interceptors installed later apply.
  return globalThis.fetch(url.toString(), {
    method: req.method ?? "GET",
    headers,
    body: req.body === undefined ? null : JSON.stringify(req.body),
    credentials: "omit",
    referrerPolicy: "no-referrer",
    cache: "no-store",
    mode: "cors",
  });
}

/** `POST /v1/share-sessions`: trade the link's token (and passcode) for a session. */
export async function openShareSession(token: string, passcode: string | null = null, opts: ShareFetchOptions = {}): Promise<ShareSession> {
  const base = opts.baseUrl ?? env.apiBase;
  const body = passcode ? { token, passcode } : { token };
  return (await shareResponse<ShareSession>(await publicFetch(base, "/v1/share-sessions", { method: "POST", body }))).data;
}

/** `POST /v1/share-sessions:refresh`: a new session (and the dashboard) before this one ends. */
export async function refreshShareSession(session: string, opts: ShareFetchOptions = {}): Promise<ShareSession> {
  const base = opts.baseUrl ?? env.apiBase;
  const res = await publicFetch(base, "/v1/share-sessions:refresh", { method: "POST", headers: { [SHARE_SESSION_HEADER]: session } });
  return (await shareResponse<ShareSession>(res)).data;
}

/** The client of a share link's dashboard (`dashboardId` only keys the cache). */
export function shareClient(session: ShareSessionHandle, dashboardId = "dashboard", opts: ShareFetchOptions = {}): DashboardClientV2 {
  const base = opts.baseUrl ?? env.apiBase;

  /** The response of one request with the session header (refreshed once on a 401 `UNAUTHENTICATED`). */
  async function send(path: string, req: ShareRequest): Promise<Response> {
    if (session.expired()) await session.refresh();
    const once = () => publicFetch(base, path, { ...req, headers: { ...(req.headers ?? {}), [SHARE_SESSION_HEADER]: session.current() } });
    let res = await once();
    if (res.status === 401) {
      // A session that ended between two requests: refresh once (a revoked link answers 410).
      const first = await shareResponse<unknown>(res).catch((e: unknown) => e);
      if (problemOf(first)?.code !== "UNAUTHENTICATED") throw first;
      await session.refresh();
      res = await once();
    }
    return res;
  }
  async function call<T>(path: string, req: ShareRequest = {}): Promise<{ data: T; status: number }> {
    return shareResponse<T>(await send(path, req));
  }
  /** A file (CSV, PNG) as a Blob: the session header only, never a cookie or a link with the session in it. */
  async function blob(path: string, accept: string): Promise<Blob> {
    const res = await send(path, { headers: { Accept: `${accept}, application/problem+json` } });
    if (!res.ok) await shareResponse<never>(res);
    return res.blob();
  }
  const get = async <T,>(path: string, query?: ShareRequest["query"]) => (await call<T>(path, { query })).data;
  const readoutPage = (q?: ReadoutQuery) => get<ReadoutList>("/v1/shared/readouts", readoutQuery(q));

  return {
    kind: "share",
    cacheKey: shareCacheKey(dashboardId),
    dashboard: () => get<ClientDashboard>("/v1/shared/dashboard"),
    async readouts(q) {
      return (await readoutPage(q)).items ?? [];
    },
    readoutPage,
    readout: (seq) => get<Readout>(`/v1/shared/readouts/${encodeURIComponent(String(seq))}`),
    async grid() {
      try {
        return await get<DashboardGrid>("/v1/shared/grid");
      } catch (err) {
        if (problemOf(err)?.status === 409) return null;
        throw err;
      }
    },
    evaluate: async (multipliers) => (await call<WhatIfResult>("/v1/shared/whatif:evaluate", { method: "POST", body: { multipliers } })).data,
    async saveScenario(s, idemKey, o = {}) {
      const { data } = await call<Readout | ReadoutHeader>("/v1/shared/scenarios", {
        method: "POST",
        body: s,
        headers: { "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) },
      });
      return data;
    },
    actualsTemplate: () => get<ActualsTemplate>("/v1/shared/actuals/template"),
    submitActuals: async (body: ActualsSubmit, idemKey: string) =>
      (await call<ReadoutHeader>("/v1/shared/actuals", { method: "POST", body, headers: { "Idempotency-Key": idemKey } })).data,
    predict: async (body: ScenarioRowsRequest, idemKey: string, o: WaitOptions = {}) =>
      (await call<Readout>("/v1/shared/predictions", { method: "POST", body, headers: { "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) } })).data,
    batchTemplate: () => get<BatchTemplate>("/v1/shared/batch/template"),
    submitBatch: async (body: BatchSubmit, idemKey: string, o: WaitOptions = {}) =>
      (await call<Readout>("/v1/shared/batch", { method: "POST", body, headers: { "Idempotency-Key": idemKey, ...waitHeader(o.waitSeconds) } })).data,
    downloadPredictions: (seq: number) => blob(`/v1/shared/readouts/${encodeURIComponent(String(seq))}/predictions.csv`, "text/csv"),
    asset: (assetId: string) => blob(`/v1/shared/assets/${encodeURIComponent(assetId)}`, "image/png"),
    // A share link has no keep route: a tried case stays private to the link and expires.
    keep: null,
  };
}

// ------------------------------------------------------------------------------ hooks over any client

function newKey(): string {
  return crypto.randomUUID();
}

/** The dashboard as its viewer sees it (`ClientDashboard`). */
export function useDashboardView(client: DashboardClient) {
  return useQuery({ queryKey: dk.viewDashboard(client.cacheKey), queryFn: () => client.dashboard() });
}

export function useReadoutList(client: DashboardClient, q: ReadoutQuery = {}, opts: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: dk.viewReadouts(client.cacheKey, q), queryFn: () => client.readoutPage(q), enabled: opts.enabled ?? true });
}

/** One readout, polled with a backoff ({@link readoutPollMs}) until it is ready or failed. */
export function useReadout(client: DashboardClient, seq: number | null) {
  return useQuery({
    queryKey: dk.viewReadout(client.cacheKey, seq ?? 0),
    queryFn: () => client.readout(seq ?? 0),
    enabled: seq !== null,
    refetchInterval: (q) => (q.state.data && TERMINAL_READOUT.has(q.state.data.status) ? false : readoutPollMs(q.state.dataUpdateCount - 1)),
  });
}

/** The response grid (`null`: the dashboard has none). Large and stable: kept for 30 minutes. */
export function useDashboardGrid(client: DashboardClient, opts: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: dk.viewGrid(client.cacheKey), queryFn: () => client.grid(), enabled: opts.enabled ?? true, staleTime: 30 * 60_000 });
}

export function useActualsTemplate(client: DashboardClient, opts: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: dk.viewTemplate(client.cacheKey), queryFn: () => client.actualsTemplate(), enabled: opts.enabled ?? true, staleTime: 10 * 60_000 });
}

/** The server's what-if on the grid (no run, no write). */
export function useEvaluateWhatIf(client: DashboardClient) {
  return useMutation({ mutationFn: (multipliers: Record<string, number>) => client.evaluate(multipliers) });
}

function useInvalidateReadouts(client: DashboardClient) {
  const qc = useQueryClient();
  return (r: Readout | ReadoutHeader) => {
    if (isFullReadout(r)) qc.setQueryData(dk.viewReadout(client.cacheKey, r.seq), r);
    return qc.invalidateQueries({ queryKey: [...dk.view(client.cacheKey), "readouts"] });
  };
}

/** Save a scenario (private readout). One key per intent: pass `idemKey` again on a retry. */
export function useSaveScenario(client: DashboardClient) {
  const invalidate = useInvalidateReadouts(client);
  return useMutation({
    mutationFn: ({ body, idemKey, waitSeconds }: { body: ScenarioSave; idemKey?: string; waitSeconds?: number }) =>
      client.saveScenario(body, idemKey ?? newKey(), waitSeconds ? { waitSeconds } : {}),
    onSuccess: invalidate,
  });
}

/** Send actuals (`202`: the readout appears when it is ready; poll it with {@link useReadout}). */
export function useSubmitActuals(client: DashboardClient) {
  const invalidate = useInvalidateReadouts(client);
  return useMutation({
    mutationFn: ({ body, idemKey }: { body: ActualsSubmit; idemKey?: string }) => client.submitActuals(body, idemKey ?? newKey()),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------------------------------------ hooks of layout 2 (v0.17)

/** The columns, example CSV and row cap of a batch file (stable: kept for 10 minutes). */
export function useBatchTemplate(client: DashboardClientV2, opts: { enabled?: boolean } = {}) {
  return useQuery({ queryKey: dk.viewBatchTemplate(client.cacheKey), queryFn: () => client.batchTemplate(), enabled: opts.enabled ?? true, staleTime: 10 * 60_000 });
}

/** "Try a case" (a private prediction readout; poll it with {@link useReadout}). One key per intent. */
export function usePredict(client: DashboardClientV2) {
  const invalidate = useInvalidateReadouts(client);
  return useMutation({
    mutationFn: ({ body, idemKey, waitSeconds }: { body: ScenarioRowsRequest; idemKey?: string; waitSeconds?: number }) =>
      client.predict(body, idemKey ?? newKey(), waitSeconds ? { waitSeconds } : {}),
    onSuccess: invalidate,
  });
}

/** "Score a file" (a private batch readout; poll it with {@link useReadout}). One key per intent. */
export function useSubmitBatch(client: DashboardClientV2) {
  const invalidate = useInvalidateReadouts(client);
  return useMutation({
    mutationFn: ({ body, idemKey, waitSeconds }: { body: BatchSubmit; idemKey?: string; waitSeconds?: number }) =>
      client.submitBatch(body, idemKey ?? newKey(), waitSeconds ? { waitSeconds } : {}),
    onSuccess: invalidate,
  });
}

/** The predictions CSV of a batch readout (a Blob for `lib/download`; never cached: each download is audited). */
export function useDownloadPredictions(client: DashboardClientV2) {
  return useMutation({ mutationFn: (seq: number) => client.downloadPredictions(seq) });
}

/**
 * A picture of the dashboard as a Blob (make an object URL of it; revoke it on unmount). Assets
 * never change under one id, so the Blob is kept for as long as the page is open.
 */
export function useDashboardAsset(client: DashboardClientV2, assetId: string | null) {
  return useQuery({
    queryKey: dk.viewAsset(client.cacheKey, assetId ?? ""),
    queryFn: () => client.asset(assetId ?? ""),
    enabled: assetId !== null,
    staleTime: Infinity,
    gcTime: 30 * 60_000,
  });
}

/** Keep a tried case for an editor's review (`null` client method: a share link cannot keep). */
export function useKeepReadout(client: DashboardClientV2) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (seq: number) => {
      if (!client.keep) return Promise.reject(new Error("a share link cannot keep a tried case"));
      return client.keep(seq);
    },
    onSuccess: (h) => {
      qc.setQueryData<Readout>(dk.viewReadout(client.cacheKey, h.seq), (r) => (r ? { ...r, ...h } : r));
      return qc.invalidateQueries({ queryKey: [...dk.view(client.cacheKey), "readouts"] });
    },
  });
}
