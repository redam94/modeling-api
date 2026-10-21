/**
 * MSW emulation of the v0.16 client dashboards (`mapi_spec.api.V016_ENDPOINTS`) for mock mode,
 * the Playwright smoke suite and component tests: the analyst routes (drafts, publishing,
 * grants, share links, readout review, refresh, refit requests), the client user's routes
 * and the public share-link routes (sessions, passcodes, lockout, the shared readouts).
 *
 * Two published dashboards live in `acme/growth`: a weekly MMM dashboard on the
 * `geo-mmm-prod` endpoint (what-ifs on a response grid, exact scenarios, actuals compared and
 * refit requests) and a monthly statsmodels SARIMAX forecast on `sales-forecast` (actuals
 * condition the forecast, published at once, no what-ifs). A third one is a draft. Cleo (a
 * client user) holds a grant on both published ones. Readouts that need a run (exact
 * scenarios, actuals, refreshes, the baseline after a publish) are `queued` first and become
 * `ready` a few seconds later (`mapi.mock.readoutMs`, default 2500 ms).
 *
 * Share links to open in mock mode (`/s#<token>`): {@link MOCK_SHARE_TOKENS}. The passcode of
 * the `whatif` link is {@link MOCK_SHARE_PASSCODE}.
 *
 * v0.17 (layout 2, `mapi_spec.api.V017_ENDPOINTS`): `acme/retail` holds a dashboard for each
 * model family, built from the real probe captures ({@link V2_DASHBOARD_IDS}: forecast,
 * regression, classifier, causal impact, field map, image) and the weekly MMM dashboard as
 * the automatic upgrade leaves it (layout 1 history, then layout 2). The routes: suggested
 * presets, the source catalog, `:upgrade`, tried cases (`…/predictions`, kept with
 * `…:keep`), scored files (`…/batch`, the template and the predictions CSV), pictures
 * (`…/assets/{id}`) and their share-link equivalents. New dashboards are layout 2 (a layout 1
 * spec is refused with `DASHBOARD_LAYOUT_RETIRED` unless `mapi.mock.layout1Create` is `1`).
 * The two v0.16 dashboards of `acme/growth` stay layout 1 (the migration fixtures).
 */
import { delay, http, HttpResponse, type HttpHandler } from "msw";

import type {
  ActualsAccuracy,
  ActualsSubmit,
  ActualsTemplate,
  ChannelDef,
  ChannelReadout,
  ClientDashboard,
  Dashboard,
  DashboardCreate,
  DashboardGrant,
  DashboardRefitRequest,
  DashboardRevision,
  DashboardShare,
  DashboardShareCreate,
  DashboardShareCreated,
  DashboardSpec,
  DashboardSummary,
  DashboardValidation,
  DashboardWhatIfResult,
  EffectiveInputs,
  InputTable,
  Issue,
  KpiValue,
  Membership,
  Problem,
  ProjectRole,
  Readout,
  ReadoutHeader,
  ReadoutSnapshot,
  ScenarioSave,
  Series,
  ShareSession,
} from "@/api/types";
import { env } from "@/env";
import { kpiOfSubject } from "@/features/dashboards/client";
import {
  layout1Spec,
  panelMeta,
  UPGRADE_NOTE,
  UPGRADE_NOTE_RANGES,
  type AccuracyDetail,
  type AccuracyPoint,
  type AnyDashboardSpec,
  type BatchForm,
  type BatchStats,
  type BatchSubmit,
  type BatchSummary,
  type BatchTemplate,
  type ClassAccuracy,
  type DashboardSpecV2,
  type DashboardUpgrade,
  type ErrorAccuracy,
  type PanelBlock,
  type PredictionResult,
  type PredictionValue,
  type PresetSuggestion,
  type ScenarioForm,
  type ScenarioInputDef,
  type ScenarioRowsDef,
  type ScenarioRowsRequest,
  type SectionKey,
  type SourceCatalog,
} from "@/features/dashboards/types";
import { evaluateScenario, type ResponseGrid } from "@/features/guided/whatif";

import { EXTRA_MODEL_IDS, EXTRA_MODELS } from "./explore";
import { MODEL_ID as fx_MODEL_ID, MV_ID, modelVersion } from "./fixtures";
import { synthGrid } from "./guided-engine";
import integrationsGen from "./integrations.gen.json";
import { encodeRgbPng } from "./png";
import { mockPersona, mockProjectRole, PERSONAS } from "./tenancy";
import { WORKFLOW_ID } from "./workflows";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const PROJECT = "acme/growth";

type Params = Record<string, string | readonly string[] | undefined>;
type Resolver = (info: { request: Request; params: Params }) => Response | Promise<Response>;

/** A route with `:action` suffixes (`…/{id}:publish`): a RegExp whose named groups are the params. */
function action(method: "get" | "post", path: string, resolver: Resolver): HttpHandler {
  const rx = new RegExp(`^${API_RE}${path}$`);
  return http[method](rx, ({ request }) => {
    const m = rx.exec(new URL(request.url).href.replace(/[?#].*$/, ""));
    return resolver({ request, params: { ...(m?.groups ?? {}) } });
  });
}
const ID = "(?<id>[^/:]+)";

export const MMM_DASHBOARD_ID = "dsh_01jaf3w1mmm2weekly3x4y5z6";
export const FORECAST_DASHBOARD_ID = "dsh_01jaf3w1fcst2monthly3x4y5";
export const DRAFT_DASHBOARD_ID = "dsh_01jaf3w1promo2draft3x4y5z";

/** Share tokens of the mock (`mpd_` + 40 Crockford base32 characters). */
export const MOCK_SHARE_TOKENS = {
  /** The MMM dashboard, read-only, no passcode. */
  readOnly: `mpd_${"k7m2q9x4".repeat(5)}`,
  /** The MMM dashboard, what-ifs and uploads, passcode {@link MOCK_SHARE_PASSCODE}. */
  inputs: `mpd_${"r3t8v1w6".repeat(5)}`,
  /** The MMM dashboard, revoked (410). */
  revoked: `mpd_${"h5j0n2p7".repeat(5)}`,
  /** The forecast dashboard, uploads allowed, no passcode. */
  forecast: `mpd_${"a1b2c3d4".repeat(5)}`,
  /** v0.17: the layout 2 forecast (acme/retail): uploads and tried cases, no passcode. */
  forecastV2: `mpd_${"f2v3w4x5".repeat(5)}`,
  /** v0.17: the regression dashboard: tried cases and scored files. */
  regression: `mpd_${"e2d3g4j5".repeat(5)}`,
  /** v0.17: the classifier dashboard: uploads, tried cases and scored files. */
  classifier: `mpd_${"c4s5b6n7".repeat(5)}`,
  /** v0.17: the image dashboard, read-only (a share link never scores images). */
  image: `mpd_${"m8g9h2k3".repeat(5)}`,
  /** v0.17: the automatically upgraded MMM dashboard, read-only. */
  mmmUpgraded: `mpd_${"p4q5r6t7".repeat(5)}`,
} as const;
export const MOCK_SHARE_PASSCODE = "harbour-lights";
const SHARE_TTL_KEY = "mapi.mock.shareTtlS";
const READOUT_MS_KEY = "mapi.mock.readoutMs";
const MAX_FAILURES = 5;
const LOCKOUT_S = 900;

const ana = PERSONAS.ana!;
const cleo = PERSONAS.cleo!;

// ---------------------------------------------------------------------------- helpers

function problem(status: number, code: Problem["code"], title: string, detail?: string, opts: { errors?: Issue[]; headers?: Record<string, string> } = {}) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: opts.errors ?? [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json", ...(opts.headers ?? {}) } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
const NOT_FOUND = (what = "dashboard") => problem(404, "NOT_FOUND", "Not found", `no such ${what}`);
const iso = (ms: number) => new Date(ms).toISOString().replace(/\.\d+Z$/, "Z");
const day = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const DAY = 86_400_000;

function readoutMs(): number {
  try {
    const v = Number(localStorage.getItem(READOUT_MS_KEY));
    return Number.isFinite(v) && v > 0 ? v : 2500;
  } catch {
    return 2500;
  }
}

function shareTtlS(): number {
  try {
    const v = Number(localStorage.getItem(SHARE_TTL_KEY));
    return Number.isFinite(v) && v > 0 ? v : 1800;
  } catch {
    return 1800;
  }
}

function seeded(seed: number): () => number {
  let h = seed >>> 0 || 1;
  return () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return ((h ^= h >>> 16) >>> 0) / 4294967296;
  };
}

const MINUS = "−";
function fmt(v: number, decimals = 0): string {
  const text = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  return v < 0 && /[1-9]/.test(text) ? MINUS + text : text;
}

function dir(delta: number | null, alert = false): KpiValue["delta_dir"] {
  if (alert) return "alert";
  if (delta === null || Math.abs(delta) < 0.005) return "flat";
  return delta > 0 ? "up" : "down";
}

function breakevenOf(spec: DashboardSpec): number | null {
  if (spec.kpi.unit === "currency") return 1;
  if (spec.kpi.unit === "count" && spec.kpi.value_per_unit) return 1 / spec.kpi.value_per_unit;
  return null;
}

/** `mapi_spec.dashboards.zone_of`. */
export function zoneOf(roi: number | null, mroi: number | null, breakeven: number, band = 0.2): ChannelReadout["zone"] {
  if (roi !== null && Number.isFinite(roi) && roi < breakeven) return "no_payback";
  if (roi === null || mroi === null || !Number.isFinite(mroi)) return null;
  if (mroi < breakeven) return "over";
  if (mroi > breakeven * (1 + band)) return "headroom";
  return "optimal";
}

/** `mapi_spec.dashboards.metrics_of`. */
export function metricsOf(predicted: (number | null)[], actual: (number | null)[], low: (number | null)[], high: (number | null)[]): ActualsAccuracy {
  let n = 0;
  let ape = 0;
  let apeN = 0;
  let bias = 0;
  let hits = 0;
  let bandN = 0;
  predicted.forEach((p, i) => {
    const a = actual[i];
    if (p === null || p === undefined || a === null || a === undefined) return;
    n += 1;
    bias += p - a;
    if (a !== 0) {
      ape += Math.abs((p - a) / a);
      apeN += 1;
    }
    const lo = low[i];
    const hi = high[i];
    if (lo !== null && lo !== undefined && hi !== null && hi !== undefined) {
      bandN += 1;
      if (lo <= a && a <= hi) hits += 1;
    }
  });
  return { n, mape: apeN ? (100 * ape) / apeN : null, bias: n ? bias / n : null, band_hit_rate: bandN ? (100 * hits) / bandN : null };
}

function noun(spec: DashboardSpec): string {
  const l = spec.kpi.label;
  return l.slice(1) === l.slice(1).toLowerCase() ? l.toLowerCase() : l;
}

function hash(text: string): string {
  let h1 = 0x811c9dc5;
  let out = "";
  for (let round = 0; round < 8; round++) {
    for (const c of `${round}:${text}`) h1 = Math.imul(h1 ^ c.charCodeAt(0), 16777619);
    out += (h1 >>> 0).toString(16).padStart(8, "0");
  }
  return out;
}

const B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
function b64url(text: string): string {
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomB64(n: number): string {
  return Array.from({ length: n }, () => B64URL[Math.floor(Math.random() * 64)] ?? "A").join("");
}
const BASE32 = "0123456789abcdefghjkmnpqrstvwxyz";
function randomToken(): string {
  return `mpd_${Array.from({ length: 40 }, () => BASE32[Math.floor(Math.random() * 32)] ?? "0").join("")}`;
}

// ---------------------------------------------------------------------------- state

type ActorKind = "member" | "client" | "share" | "system";

interface Actor {
  kind: ActorKind;
  id: string;
}

interface MockReadout {
  r: Readout;
  actor: Actor;
  /** When a queued readout becomes ready (epoch ms); `null`: already settled. */
  readyAt: number | null;
  /** Builds the snapshot once the "run" finished. */
  build: (() => { snapshot: ReadoutSnapshot } | { problem: Problem }) | null;
  /** v0.17: the readout spent a run (it counts against the dashboard's daily caps). */
  spent?: boolean;
}

interface MockShare {
  share: DashboardShare;
  token: string;
  passcode: string | null;
  failures: number;
  sessionVersion: number;
}

interface MockDashboard {
  project: string;
  d: Dashboard;
  revisions: DashboardRevision[];
  readouts: MockReadout[];
  grants: DashboardGrant[];
  shares: MockShare[];
  refits: DashboardRefitRequest[];
  grid: ResponseGrid | null;
  template: ActualsTemplate | null;
  /** `Idempotency-Key` → readout seq. */
  idem: Map<string, number>;
  /** v0.17: the layout 2 side (catalog, snapshots, the mock's scorer); `null` for a layout 1 binding without one. */
  v2: V2Runtime | null;
  /** v0.17: the scored lines of each batch readout (seq → lines), for the predictions CSV. */
  batches: Map<number, BatchLine[]>;
}

interface ShareSessionRow {
  dashboardId: string;
  shareId: string;
  version: number;
  expiresAt: number;
}

export interface DashboardsState {
  dashboards: Map<string, MockDashboard>;
  sessions: Map<string, ShareSessionRow>;
  /** Project members (grants need one), from the tenancy mock. */
  members: () => Membership[];
}

// ---------------------------------------------------------------------------- fixtures: the MMM dashboard

const MMM_CHANNELS: ChannelDef[] = [
  { name: "tv", label: "TV", min_mult: 0.5, max_mult: 1.5, step: 0.05 },
  { name: "search", label: "Paid search", min_mult: 0.5, max_mult: 2, step: 0.05 },
  { name: "social", label: "Social", min_mult: 0.5, max_mult: 1.5, step: 0.05 },
  { name: "display", label: "Display", min_mult: 0.25, max_mult: 1.5, step: 0.05 },
];

const MMM_SPEC: DashboardSpec = {
  layout_version: 1,
  title: "Weekly media readout",
  client_name: "Northwind Outdoor",
  summary: "How your media spend turns into revenue, week by week. The numbers come from the media mix model that we fitted on your sales.",
  period_label: "week",
  source: { kind: "endpoint", alias: integrationsGen.alias },
  kpi: { variable: "revenue", label: "Revenue", unit: "currency", currency: "EUR", decimals: 0 },
  channels: MMM_CHANNELS,
  tiles: ["expected", "return_on_media", "in_range_spend", "vs_expected"],
  tabs: ["this_period", "trends", "scenarios", "update", "history"],
  whatif: { enabled: true, exact: true },
  actuals: { enabled: true, publish: "review", allow_refit_requests: true, refit_workflow_id: WORKFLOW_ID },
  refresh: { workflow_id: WORKFLOW_ID, follow_endpoint: true },
  limits: { exact_runs_per_day: 20 },
  show_technical: true,
  note: "Spend in this readout is net media spend. Agency fees are not included.",
};

/** Weeks of the fit window: the grid holds totals over them, the readout shows one week. */
const MMM_WEEKS = 52;

const MMM_GRID: ResponseGrid = synthGrid({
  channels: MMM_CHANNELS.map((c) => c.name),
  geos: null,
  spend: [42_000, 18_000, 12_000, 8_000].map((s) => s * MMM_WEEKS),
  kpiTotal: 410_000 * MMM_WEEKS,
  kpiSeries: Array.from({ length: MMM_WEEKS }, (_, k) => ({ t: day(Date.parse("2025-10-26T00:00:00Z") + k * 7 * DAY), y: 410_000 })),
  statedRoi: { tv: 1.9, search: 2.7, social: 1.25, display: 0.85 },
  kpiUnits: "EUR",
});

/** Week `w` (0 = the newest) ends on Sunday 18/10/2026 minus `w` weeks. */
const LAST_SUNDAY = Date.parse("2026-10-18T00:00:00Z");

function gridAt(grid: ResponseGrid, multipliers: Record<string, number>) {
  return evaluateScenario(grid, { multipliers }, [0.05, 0.5, 0.95]);
}

/**
 * Channel rows of the grid at `multipliers`, per week (the grid's window totals / its periods,
 * as the server's `grid_facts`), scaled by `scale` (a week's level).
 */
function channelRows(spec: DashboardSpec, grid: ResponseGrid, multipliers: Record<string, number>, level = 1): ChannelReadout[] {
  const be = breakevenOf(spec);
  const res = gridAt(grid, multipliers);
  const scale = level / grid.periods;
  const total = Object.values(res.channels).reduce((a, c) => a + c.contribution.mean * scale, 0);
  return spec.channels.map((ch) => {
    const out = res.channels[ch.name];
    if (!out) return { name: ch.name, label: ch.label, spend: null, contribution: null, share: null, roi: null, roi_low: null, roi_high: null, mroi: null, zone: null };
    const m = out.multiplier;
    const step = Math.min(m + 0.1, grid.multipliers[grid.multipliers.length - 1] ?? m + 0.1);
    const next = step > m ? gridAt(grid, { ...multipliers, [ch.name]: step }).channels[ch.name] : null;
    const spend = out.spend * scale;
    const contribution = out.contribution.mean * scale;
    const roi = spend > 0 ? contribution / spend : null;
    const mroi = next && next.spend > out.spend ? (next.contribution.mean - out.contribution.mean) / (next.spend - out.spend) : null;
    const lo = out.contribution.quantiles[0] ?? contribution;
    const hi = out.contribution.quantiles[2] ?? contribution;
    return {
      name: ch.name,
      label: ch.label,
      spend,
      contribution,
      share: total ? contribution / total : null,
      roi,
      roi_low: spend > 0 ? (lo * scale) / spend : null,
      roi_high: spend > 0 ? (hi * scale) / spend : null,
      mroi,
      zone: be !== null ? zoneOf(roi, mroi, be) : null,
    };
  });
}

function mediaTiles(spec: DashboardSpec, channels: ChannelReadout[]): Partial<Record<KpiValue["key"], KpiValue>> {
  const tiles: Partial<Record<KpiValue["key"], KpiValue>> = {};
  const spend = channels.reduce((a, c) => a + (c.spend ?? 0), 0);
  const contrib = channels.reduce((a, c) => a + (c.contribution ?? 0), 0);
  if (spend > 0) {
    const roi = contrib / spend;
    tiles.return_on_media = { key: "return_on_media", label: "Return on media", value: roi, unit: "ratio", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: `Each 1 ${spec.kpi.currency ?? "unit"} of media brought ${fmt(roi, 2)} ${noun(spec)}` };
    const zoned = channels.filter((c) => c.zone && c.spend !== null);
    if (zoned.length) {
      const good = zoned.filter((c) => c.zone === "headroom" || c.zone === "optimal").reduce((a, c) => a + (c.spend ?? 0), 0);
      const share = (good / spend) * 100;
      tiles.in_range_spend = { key: "in_range_spend", label: "Spend in a good range", value: share, unit: "percent", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: `${fmt(share)}% of media spend is within range or has more room` };
    }
  }
  return tiles;
}

function pick(spec: DashboardSpec, tiles: Partial<Record<KpiValue["key"], KpiValue>>): KpiValue[] {
  const order = [...(spec.tiles ?? []), ...(Object.keys(tiles) as KpiValue["key"][])];
  return [...new Set(order)].map((k) => tiles[k]).filter((t): t is KpiValue => Boolean(t)).slice(0, 6);
}

/** The weekly MMM readout of week `w` (0 = newest): KPI band over 26 weeks, channels, tiles. */
function mmmSnapshot(spec: DashboardSpec, grid: ResponseGrid, w: number): ReadoutSnapshot {
  const level = (k: number) => 1 + 0.07 * Math.sin((k + 3) / 2.3) + 0.02 * Math.cos(k * 1.7);
  const rand = seeded(9001 + w);
  const base = gridAt(grid, {});
  const expectedAt = (k: number) => (base.kpi ? base.kpi.mean / grid.periods : 400_000) * level(k);
  const points = Array.from({ length: 26 }, (_, i) => {
    const k = w + 25 - i;
    const m = expectedAt(k);
    const noise = seeded(31 + k)();
    return { t: day(LAST_SUNDAY - k * 7 * DAY), mean: m, low: m * 0.93, high: m * 1.07, actual: m * (1 + 0.09 * (noise - 0.45)) };
  });
  const last = points[points.length - 1]!;
  const prev = points[points.length - 2]!;
  const channels = channelRows(spec, grid, {}, level(w) * (0.98 + 0.04 * rand()));
  const vs = (last.actual - last.mean) / last.mean;
  const outside = last.actual < last.low || last.actual > last.high;
  const change = (last.mean - prev.mean) / prev.mean;
  const n = noun(spec);
  const tiles: Partial<Record<KpiValue["key"], KpiValue>> = {
    ...mediaTiles(spec, channels),
    expected: { key: "expected", label: `Expected ${n}`, value: last.mean, unit: spec.kpi.unit, delta: change, delta_basis: "the week before", delta_dir: dir(change), low: last.low, high: last.high, caption: `Likely range ${fmt(last.low)} to ${fmt(last.high)}` },
    vs_expected: { key: "vs_expected", label: "Compared with the model", value: vs * 100, unit: "percent", delta: vs, delta_basis: "the model's expectation", delta_dir: dir(vs, outside), low: null, high: null, caption: `${fmt(last.actual)} ${n} against ${fmt(last.mean)} expected` },
    index: { key: "index", label: "Index", value: (100 * last.actual) / last.mean, unit: "index", delta: null, delta_basis: null, delta_dir: dir(vs, outside), low: null, high: null, caption: "100 is the model's expectation" },
  };
  const media = channels.reduce((a, c) => a + (c.contribution ?? 0), 0);
  const series: Series[] = [{ name: "kpi", label: spec.kpi.label, points }];
  const word = vs > 0 ? "more" : "less";
  return {
    lead: `Media brought ${fmt(media)} ${n} this week. The result was ${fmt(Math.abs(vs) * 100)}% ${word} than the model expects.`,
    tiles: pick(spec, tiles),
    channels,
    series,
    accuracy: null,
    conditioning: null,
    scenario: null,
    source_run_ids: [`run_01jaf3q2x8m9v7k5h4g3f2w${String(w).padStart(3, "0")}`.slice(0, 30)],
    input_fingerprint: null,
    notes_plain: w === 0 ? ["Display spend was lower than planned in the last two weeks."] : [],
    technical: { model_ref: modelVersion.ref, draws: grid.draws, "rhat.max": 1.01, "ess_bulk.min": 812 },
  };
}

/** A what-if on the grid as the server answers it (`inputs.evaluate_whatif`). */
export function whatIfOf(spec: DashboardSpec, grid: ResponseGrid, multipliers: Record<string, number>): DashboardWhatIfResult {
  const res = gridAt(grid, multipliers);
  const cur = gridAt(grid, {});
  const channels = channelRows(spec, grid, multipliers);
  const d = spec.kpi.decimals ?? 0;
  // per week, as the server's `whatif_result`: the grid answers over its whole window
  const per = (v: number | null | undefined) => (v == null ? null : v / grid.periods);
  const week = spec.period_label ?? "week";
  const deltaKpi = res.kpi_delta.mean / grid.periods;
  const baseKpi = per(cur.kpi?.mean);
  const lo = per(res.kpi?.quantiles[0]);
  const hi = per(res.kpi?.quantiles[2]);
  const n = noun(spec);
  const tiles: Partial<Record<KpiValue["key"], KpiValue>> = {
    ...mediaTiles(spec, channels),
    expected: {
      key: "expected",
      label: `Expected ${n}`,
      value: per(res.kpi?.mean),
      unit: spec.kpi.unit,
      delta: baseKpi ? deltaKpi / baseKpi : null,
      delta_basis: "the current plan",
      delta_dir: dir(baseKpi ? deltaKpi / baseKpi : null),
      low: lo,
      high: hi,
      caption: `A ${week} on average over ${grid.periods} ${week}s. Likely range ${fmt(lo ?? 0, d)} to ${fmt(hi ?? 0, d)}`,
    },
  };
  const spendDelta = res.spend_delta / grid.periods;
  const lead =
    Math.abs(deltaKpi) < 0.5
      ? "This plan brings the same result as the current plan."
      : `This plan brings ${fmt(Math.abs(deltaKpi), d)} ${deltaKpi > 0 ? "more" : "less"} ${n} a ${week} than the current plan, for ${fmt(Math.abs(spendDelta), d)} ${spendDelta >= 0 ? "more" : "less"} spend.`;
  return { tiles: pick(spec, tiles), channels, delta_kpi: deltaKpi, lead };
}

// ---------------------------------------------------------------------------- fixtures: the forecast dashboard

const FORECAST_TABLE = integrationsGen.forecast.input_table as unknown as InputTable;
const ARIMAX = EXTRA_MODELS.find((m) => m.model.id === EXTRA_MODEL_IDS.arimax)!;
const ARIMAX_V2 = ARIMAX.versions.find((v) => v.version === 2) ?? ARIMAX.versions[0]!;

const FORECAST_SPEC: DashboardSpec = {
  layout_version: 1,
  title: "Monthly sales forecast",
  client_name: "Northwind Outdoor",
  summary: "The sales that we expect for the next months. Send the actual sales of each month: the forecast then starts from your newest numbers.",
  period_label: "month",
  source: { kind: "endpoint", alias: integrationsGen.forecast.alias },
  kpi: { variable: "sales", label: "Sales", unit: "currency", currency: "EUR", decimals: 0 },
  channels: [{ name: "price", label: "Average price", min_mult: 0.5, max_mult: 1.5, step: 0.05 }],
  tiles: ["expected", "vs_expected", "accuracy", "index"],
  tabs: ["this_period", "trends", "update", "history"],
  whatif: { enabled: false, exact: false },
  actuals: { enabled: true, publish: "auto", allow_refit_requests: false, refit_workflow_id: null },
  refresh: { workflow_id: null, follow_endpoint: true },
  // Uploads spend a run (`_spent` counts actuals): 0 would refuse every upload with 429.
  limits: { exact_runs_per_day: 20 },
  show_technical: false,
  note: null,
};

const MONTH0 = Date.UTC(2026, 9, 1); // October 2026: the first month to forecast
function monthLabel(k: number): string {
  const d = new Date(MONTH0);
  d.setUTCMonth(d.getUTCMonth() + k);
  return d.toISOString().slice(0, 10);
}

/** The forecast readout made `m` months ago: 12 months of history with actuals, 6 ahead. */
function forecastSnapshot(spec: DashboardSpec, m: number, sent: number[] | null = null): ReadoutSnapshot {
  const level = (k: number) => 182_000 * (1 + 0.012 * k) * (1 + 0.11 * Math.sin(((k + 2) * Math.PI) / 6));
  const actualOf = (k: number) => level(k) * (1 + 0.06 * (seeded(77 + k + 100)() - 0.5));
  const cut = -m; // months before this one have actuals
  const points = Array.from({ length: 18 }, (_, i) => {
    const k = cut - 12 + i;
    const mu = level(k);
    const ahead = k >= cut ? k - cut + 1 : 0;
    const band = ahead ? 0.05 + 0.02 * ahead : 0.04;
    const fromUser = sent && k < cut && k >= cut - sent.length ? (sent[sent.length - (cut - k)] ?? null) : null;
    return { t: monthLabel(k), mean: mu, low: mu * (1 - band), high: mu * (1 + band), actual: k < cut ? (fromUser ?? actualOf(k)) : null };
  });
  const history = points.filter((p) => p.actual !== null);
  const acc = metricsOf(history.map((p) => p.mean), history.map((p) => p.actual), history.map((p) => p.low), history.map((p) => p.high));
  const next = points.find((p) => p.actual === null)!;
  const last = history[history.length - 1]!;
  const vs = (last.actual! - last.mean) / last.mean;
  const outside = last.actual! < last.low || last.actual! > last.high;
  const n = noun(spec);
  const tiles: Partial<Record<KpiValue["key"], KpiValue>> = {
    expected: { key: "expected", label: `Expected ${n} next month`, value: next.mean, unit: "currency", delta: (next.mean - last.mean) / last.mean, delta_basis: "the month before", delta_dir: dir((next.mean - last.mean) / last.mean), low: next.low, high: next.high, caption: `Likely range ${fmt(next.low)} to ${fmt(next.high)}` },
    vs_expected: { key: "vs_expected", label: "Compared with the model", value: vs * 100, unit: "percent", delta: vs, delta_basis: "the model's expectation", delta_dir: dir(vs, outside), low: null, high: null, caption: `${fmt(last.actual!)} ${n} against ${fmt(last.mean)} expected` },
    accuracy: { key: "accuracy", label: "Prediction error", value: acc.mape ?? null, unit: "percent", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: `Average error over ${acc.n} months` },
    index: { key: "index", label: "Index", value: (100 * last.actual!) / last.mean, unit: "index", delta: null, delta_basis: null, delta_dir: dir(vs, outside), low: null, high: null, caption: "100 is the model's expectation" },
  };
  return {
    lead: `We expect ${fmt(next.mean)} ${n} in the next month. Last month the result was ${fmt(Math.abs(vs) * 100)}% ${vs > 0 ? "more" : "less"} than the model expected.`,
    tiles: pick(spec, tiles),
    channels: [{ name: "price", label: "Average price", spend: null, contribution: null, share: null, roi: null, roi_low: null, roi_high: null, mroi: null, zone: null }],
    series: [{ name: "kpi", label: spec.kpi.label, points }],
    accuracy: acc,
    conditioning: sent
      ? { dim: "month", fit_end: "2026-09-01", conditioned: true, n_actuals: sent.length, horizon_source: "rows", periods: 6, message: `Forecast updated with ${sent.length} actual${sent.length === 1 ? "" : "s"} after the fitted history; the model's parameters were not re-estimated.`, n_series: 1, parameters: "fitted", revised: 0, series: [], series_conditioned: 1, truncated: false }
      : null,
    scenario: null,
    source_run_ids: [],
    input_fingerprint: null,
    notes_plain: [],
    technical: { model_ref: ARIMAX_V2.ref, order: "(1,1,1)(0,1,1,12)" },
  };
}

// ---------------------------------------------------------------------------- fixtures: assembly

function header(seq: number, patch: Partial<Readout> & Pick<Readout, "kind" | "trigger" | "title">, mv: { id: string; ref: string }, createdMs: number): Readout {
  return {
    seq,
    visibility: "published",
    status: "ready",
    period: null,
    model_version_id: mv.id,
    model_ref: mv.ref,
    endpoint_revision: 1,
    created_at: iso(createdMs),
    ready_at: iso(createdMs + 90_000),
    refit_status: null,
    snapshot: null,
    problem: null,
    ...patch,
  };
}

function revisionOf(id: string, seq: number, spec: AnyDashboardSpec, mv: { id: string; ref: string }, alias: string | null, at: number): DashboardRevision {
  return {
    id: `dsr_${id.slice(4, 18)}r${seq}`,
    seq,
    spec: structuredClone(spec),
    spec_hash: `sha256:${hash(JSON.stringify(spec))}`,
    model_version_id: mv.id,
    model_ref: mv.ref,
    endpoint_alias: alias,
    endpoint_revision: alias ? 1 : null,
    created_by: ana.id,
    created_at: iso(at),
  };
}

function dashboardRow(id: string, name: string, spec: AnyDashboardSpec, rev: DashboardRevision | null, at: number): Dashboard {
  return {
    id,
    name,
    status: rev ? "published" : "draft",
    draft_spec: structuredClone(spec),
    draft_etag: `"${hash(JSON.stringify(spec)).slice(0, 16)}"`,
    published_revision: rev,
    run_as: rev ? ana.id : null,
    live_inputs: "ok",
    problem: null,
    created_by: ana.id,
    created_at: iso(at),
    updated_at: iso(at),
    latest_readout: null,
  };
}

function mmmFixture(): MockDashboard {
  const mv = { id: MV_ID, ref: modelVersion.ref };
  const rev = revisionOf(MMM_DASHBOARD_ID, 1, MMM_SPEC, mv, integrationsGen.alias, Date.parse("2026-07-27T09:00:00Z"));
  const readouts: MockReadout[] = [];
  const weeks = 12;
  for (let i = 0; i < weeks; i++) {
    const w = weeks - 1 - i;
    const end = LAST_SUNDAY - w * 7 * DAY;
    const r = header(
      i + 1,
      { kind: i === 0 ? "baseline" : "refresh", trigger: i === 0 ? "publish" : "workflow", title: `Week ending ${new Date(end).toLocaleDateString("en-GB", { timeZone: "UTC" })}`, period: [day(end - 6 * DAY), day(end)], snapshot: mmmSnapshot(MMM_SPEC, MMM_GRID, w) },
      mv,
      end + DAY + 9.5 * 3_600_000,
    );
    readouts.push({ r, actor: { kind: "system", id: "system" }, readyAt: null, build: null });
  }
  const t = LAST_SUNDAY + 2 * DAY + 11 * 3_600_000;
  // Cleo's upload, waiting for review, with a refit request.
  const sent = mmmSnapshot(MMM_SPEC, MMM_GRID, 0);
  const pts = sent.series?.[0]?.points ?? [];
  readouts.push({
    r: header(
      13,
      {
        kind: "actuals",
        trigger: "actuals",
        visibility: "pending_review",
        title: "Numbers sent on 20/10/2026",
        period: [pts[pts.length - 4]?.t ?? day(LAST_SUNDAY), pts[pts.length - 1]?.t ?? day(LAST_SUNDAY)],
        refit_status: "open",
        snapshot: {
          ...sent,
          lead: "Your numbers were 4% more than the model expected over the last 4 weeks.",
          accuracy: { n: 4, mape: 4.1, bias: -15_800, band_hit_rate: 75 },
          series: [{ name: "kpi", label: "Revenue", points: pts.slice(-4) }],
        },
      },
      mv,
      t,
    ),
    actor: { kind: "client", id: cleo.id },
    readyAt: null,
    build: null,
  });
  // Cleo's saved scenario (private).
  const scen = { tv: 0.9, search: 1.3 };
  readouts.push({
    r: header(14, { kind: "scenario", trigger: "whatif", visibility: "private", title: "More search, less TV", snapshot: scenarioSnapshot(MMM_SPEC, MMM_GRID, scen) }, mv, t + 3_600_000),
    actor: { kind: "client", id: cleo.id },
    readyAt: null,
    build: null,
  });
  // A refresh that failed (analysts only).
  readouts.push({
    r: header(
      15,
      {
        kind: "refresh",
        trigger: "manual",
        status: "failed",
        title: "Refresh of 20/10/2026",
        ready_at: null,
        problem: { type: "urn:mapi:error:QUOTA_EXCEEDED", title: "Quota exceeded", status: 429, code: "QUOTA_EXCEEDED", detail: "the project's vCPU-seconds for today are used up", errors: [] },
      },
      mv,
      t + 2 * 3_600_000,
    ),
    actor: { kind: "member", id: ana.id },
    readyAt: null,
    build: null,
  });
  const d = dashboardRow(MMM_DASHBOARD_ID, "northwind-weekly", MMM_SPEC, rev, Date.parse("2026-07-27T09:00:00Z"));
  const at = (days: number) => iso(LAST_SUNDAY - days * DAY);
  const share = (id: string, token: string, patch: Partial<DashboardShare>, passcode: string | null = null): MockShare => ({
    token,
    passcode,
    failures: 0,
    sessionVersion: 1,
    share: {
      id,
      label: null,
      display: `mpd_…${token.slice(-4)}`,
      status: "active",
      has_passcode: passcode !== null,
      allowed_inputs: [],
      views: 0,
      last_viewed_at: null,
      locked_until: null,
      expires_at: iso(Date.now() + 30 * DAY),
      revoked_at: null,
      created_by: ana.id,
      created_at: at(9),
      ...patch,
    },
  });
  return {
    project: PROJECT,
    d,
    revisions: [rev],
    readouts,
    grants: [{ user_id: cleo.id, email: cleo.email, display_name: cleo.name, granted_by: ana.id, created_at: at(80) }],
    shares: [
      share("dss_01jaf3w1read2only3x4y5z6", MOCK_SHARE_TOKENS.readOnly, { label: "Board pack", views: 14, last_viewed_at: at(1) }),
      share("dss_01jaf3w1inputs2pass3x4y5", MOCK_SHARE_TOKENS.inputs, { label: "Media agency", allowed_inputs: ["whatif", "actuals"], views: 3, last_viewed_at: at(3) }, MOCK_SHARE_PASSCODE),
      share("dss_01jaf3w1revoked2old3x4y5", MOCK_SHARE_TOKENS.revoked, { label: "Old link", status: "revoked", revoked_at: at(20), created_at: at(60), views: 41 }),
    ],
    refits: [
      {
        id: "rfr_01jaf3w1refit2open3x4y5z6",
        dashboard_id: MMM_DASHBOARD_ID,
        readout_seq: 13,
        status: "open",
        requested_by_kind: "client",
        requested_by: cleo.id,
        created_at: iso(t),
        decided_by: null,
        decided_at: null,
        dataset_version_id: null,
        workflow_run_id: null,
        note: null,
        problem: null,
      },
    ],
    grid: MMM_GRID,
    template: mmmTemplate(),
    idem: new Map(),
    v2: null,
    batches: new Map(),
  };
}

function mmmTemplate(): ActualsTemplate {
  const table: InputTable = {
    horizon_dim: "date",
    conditions_on_actuals: false,
    columns: [
      { name: "date", role: "coord", dims: [], freq: "W-SUN", description: "The last day of the week (yyyy-MM-dd)" },
      { name: "revenue", role: "target", dims: ["date"], freq: null, description: "The actual revenue of the week" },
      ...MMM_CHANNELS.map((c) => ({ name: `spend_${c.name}`, role: "input" as const, dims: ["date"], freq: null, description: `The spend on ${c.label} in the week` })),
    ],
    rows: [],
  };
  const lines = [table.columns!.map((c) => c.name).join(",")];
  for (let w = 3; w >= 0; w--) lines.push([day(LAST_SUNDAY - w * 7 * DAY), "", "42000", "18000", "12000", "8000"].join(","));
  return { mode: "compared", input_table: table, kpi_column: "revenue", example_csv: `${lines.join("\n")}\n`, max_rows: 520 };
}

function forecastTemplate(): ActualsTemplate {
  const cols = FORECAST_TABLE.columns ?? [];
  const lines = [cols.map((c) => c.name).join(",")];
  for (const row of FORECAST_TABLE.rows ?? []) lines.push(row.map((v) => (v === null || v === undefined ? "" : String(v))).join(","));
  return { mode: "conditioned", input_table: FORECAST_TABLE, kpi_column: "sales", example_csv: `${lines.join("\n")}\n`, max_rows: 520 };
}

function forecastFixture(): MockDashboard {
  const mv = { id: ARIMAX_V2.id, ref: ARIMAX_V2.ref };
  const rev = revisionOf(FORECAST_DASHBOARD_ID, 1, FORECAST_SPEC, mv, integrationsGen.forecast.alias, Date.parse("2026-04-03T09:00:00Z"));
  const readouts: MockReadout[] = [];
  const months = 6;
  for (let i = 0; i < months; i++) {
    const m = months - 1 - i;
    const created = Date.UTC(2026, 9 - m, 3, 9, 30);
    const start = monthLabel(-m - 1);
    const end = day(Date.UTC(2026, 9 - m, 0));
    readouts.push({
      r: header(
        i + 1,
        { kind: i === 0 ? "baseline" : "actuals", trigger: i === 0 ? "publish" : "actuals", title: `Forecast from ${new Date(created).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })}`, period: [start, end], snapshot: forecastSnapshot(FORECAST_SPEC, m, i === 0 ? null : [180_000 + i * 900]) },
        mv,
        created,
      ),
      actor: i === 0 ? { kind: "system", id: "system" } : { kind: "client", id: cleo.id },
      readyAt: null,
      build: null,
    });
  }
  const d = dashboardRow(FORECAST_DASHBOARD_ID, "northwind-sales-forecast", FORECAST_SPEC, rev, Date.parse("2026-04-03T09:00:00Z"));
  return {
    project: PROJECT,
    d,
    revisions: [rev],
    readouts,
    grants: [{ user_id: cleo.id, email: cleo.email, display_name: cleo.name, granted_by: ana.id, created_at: "2026-04-03T09:05:00Z" }],
    shares: [
      {
        token: MOCK_SHARE_TOKENS.forecast,
        passcode: null,
        failures: 0,
        sessionVersion: 1,
        share: { id: "dss_01jaf3w1fcst2share3x4y5z6", label: "Finance team", display: `mpd_…${MOCK_SHARE_TOKENS.forecast.slice(-4)}`, status: "active", has_passcode: false, allowed_inputs: ["actuals"], views: 6, last_viewed_at: "2026-10-14T08:00:00Z", locked_until: null, expires_at: iso(Date.now() + 60 * DAY), revoked_at: null, created_by: ana.id, created_at: "2026-09-01T08:00:00Z" },
      },
    ],
    refits: [],
    grid: null,
    template: forecastTemplate(),
    idem: new Map(),
    v2: null,
    batches: new Map(),
  };
}

function draftFixture(): MockDashboard {
  const spec: DashboardSpec = {
    ...structuredClone(MMM_SPEC),
    title: "Spring promotion plan",
    summary: null,
    tiles: ["expected", "return_on_media"],
    tabs: ["this_period", "scenarios"],
    actuals: { enabled: false, publish: "review", allow_refit_requests: false, refit_workflow_id: null },
    note: null,
  };
  return { project: PROJECT, d: dashboardRow(DRAFT_DASHBOARD_ID, "spring-promo-plan", spec, null, Date.parse("2026-10-16T10:00:00Z")), revisions: [], readouts: [], grants: [], shares: [], refits: [], grid: MMM_GRID, template: null, idem: new Map(), v2: null, batches: new Map() };
}

function scenarioSnapshot(spec: DashboardSpec, grid: ResponseGrid, multipliers: Record<string, number>): ReadoutSnapshot {
  const w = whatIfOf(spec, grid, multipliers);
  return { lead: w.lead, tiles: w.tiles ?? [], channels: w.channels ?? [], series: [], accuracy: null, conditioning: null, scenario: multipliers, source_run_ids: [], input_fingerprint: null, notes_plain: [], technical: {} };
}

// ---------------------------------------------------------------------------- fixtures: layout 2 (v0.17)

/**
 * The layout 2 dashboards live in `acme/retail` (Cleo is a client there too), so the v0.16
 * dashboards of `acme/growth` stay exactly as they were: they are the migration fixtures.
 */
export const V2_PROJECT = "acme/retail";

type V2Key = "forecast" | "regression" | "classifier" | "causal" | "field" | "image";

interface V2Raw {
  spec: DashboardSpecV2;
  catalog: SourceCatalog;
  issues: Issue[];
  suggestions: PresetSuggestion[];
  baseline: ReadoutSnapshot;
  actuals?: ReadoutSnapshot;
  actuals_template?: ActualsTemplate;
  batch_template?: BatchTemplate;
  batch?: ReadoutSnapshot;
  prediction?: ReadoutSnapshot;
}

interface V2Anchors {
  /** Units sold of sku k0 in week 2025-03-02 per store: centre, low, high (conformal interval). */
  regression: Record<string, [number, number, number]>;
  /** P(high demand) of sku k0 in week 2024-10-06 per store. */
  classifier: Record<string, number>;
  /** Class probabilities per test image. */
  image: Record<string, Record<string, number>>;
  /** The forecast total without the change over the 4 post weeks (`ForecastView.total`, predictive). */
  causal_cf_total: { mean: number; quantiles: Record<string, number> };
}

interface V1Upgrade {
  spec: DashboardSpecV2;
  notes: string[];
  issues: Issue[];
  catalog: SourceCatalog;
  suggestions: PresetSuggestion[];
}

/*
 * Real readouts of the v0.17 probe (packages/server/tests/fixtures/dashboards_v017): for each
 * case the preset was compiled from the captured catalog (`compile_preset`, then the edits an
 * analyst makes: labels, inputs, uploads), checked (`check_spec_v2`) and its snapshots built
 * by the server's own panel code (`panels.build_snapshot_v2`) from the captured score runs:
 * the baseline (the predictive score where the model has one), an actuals readout (the
 * fixtures' derived actuals), a tried case and a scored file. Totals carry the run's own
 * band (never summed bounds); numbers are rounded to 6 significant digits.
 * V1_UPGRADES: `upgrade_v1` of the two layout 1 mocks above (MMM on `mmm.bayesian`, the
 * forecast with its dummy channel on `ts.sarimax`).
 */

const V2_RAW = {
  forecast: {
    spec: {"layout_version":2,"preset":"forecast","title":"Monthly store visits","client_name":"Harbour Retail","summary":"The store visits that we expect for the next months, with a likely range. Send the visits of each month: the accuracy updates with your numbers.","period_label":"month","source":{"kind":"endpoint","alias":"store-visits"},"subject":{"variable":"y_hat","label":"Store visits","unit":"count","decimals":0},"panels":[{"id":"next","title":"Visits next month","kind":"kpi","source":{"kind":"output","variable":"y_hat","select":{}},"stat":"centre","reduce":{"how":"next","over":[]},"band":"outcome","compare":"previous","good":"up"},{"id":"horizon_total","title":"Visits over the next months","kind":"kpi","source":{"kind":"view","view":"forecast_total"},"stat":"centre","reduce":{"how":"none","over":[]},"band":"outcome","compare":"previous","good":"up"},{"id":"typical_error","title":"Typical error","kind":"kpi","source":{"kind":"accuracy","metric":"mae"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"previous","good":"down"},{"id":"inside_range","title":"Results inside the likely range","kind":"kpi","source":{"kind":"accuracy","metric":"band_hit_rate"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"previous","good":"none"},{"id":"fan","title":"Visits and forecast","kind":"chart","intent":"fan","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"accuracy","title":"Accuracy over time","kind":"chart","intent":"accuracy_history","source":{"kind":"accuracy","metric":"mape"},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"next_periods","title":"The next periods","kind":"table","source":{"kind":"output","variable":"y_hat","select":{}},"columns":["date","centre","low","high"],"max_rows":12},{"id":"o_level","title":"Underlying level","kind":"chart","intent":"decomposition","source":{"kind":"output","variable":"level","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20}],"scenario_rows":{"enabled":true,"max_rows":3,"outputs":["y_hat"],"inputs":[{"name":"price","label":"Average price","help":"The average price over the months ahead","bind":{"var":"price"},"type":"number","min":5.0,"max":15.0,"step":0.5,"choices":[],"default":10.0,"applies":"horizon"}],"fixed":{},"horizon":{"dim":"t","min":1,"max":12,"default":6}},"actuals":{"enabled":true,"publish":"auto","allow_refit_requests":false,"match":"time","threshold":0.5},"refresh":{"follow_endpoint":false,"horizon":6},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":true},
    catalog: {"outputs":[{"name":"level","manifest_name":"level","dims":["date"],"time_dim":"date","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter","decomposition"],"predictive_ok":true},{"name":"y_hat","manifest_name":"y_hat","dims":["date"],"time_dim":"date","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true}],"metrics":[{"key":"coverage_90","labels":{},"last":0.833333},{"key":"divergences","labels":{"chain":"0"},"last":0.0},{"key":"divergences","labels":{"chain":"0","fit":"evaluation"},"last":0.0},{"key":"divergences","labels":{"chain":"1"},"last":0.0},{"key":"divergences","labels":{"chain":"1","fit":"evaluation"},"last":0.0},{"key":"mape","labels":{},"last":2.44212},{"key":"num_steps","labels":{"chain":"0"},"last":7.0},{"key":"num_steps","labels":{"chain":"0","fit":"evaluation"},"last":15.0},{"key":"num_steps","labels":{"chain":"1"},"last":7.0},{"key":"num_steps","labels":{"chain":"1","fit":"evaluation"},"last":15.0},{"key":"one_step_coverage_90","labels":{},"last":0.894737},{"key":"one_step_coverage_90","labels":{"fit":"evaluation"},"last":0.955556},{"key":"one_step_rmse","labels":{},"last":24.0425},{"key":"one_step_rmse","labels":{"fit":"evaluation"},"last":23.6163},{"key":"rmse","labels":{},"last":33.5239},{"key":"sigma_level","labels":{},"last":5.54021},{"key":"sigma_level","labels":{"fit":"evaluation"},"last":4.14716},{"key":"sigma_obs","labels":{},"last":16.7871},{"key":"sigma_obs","labels":{"fit":"evaluation"},"last":17.1451},{"key":"step_size","labels":{"chain":"0"},"last":0.445439},{"key":"step_size","labels":{"chain":"0","fit":"evaluation"},"last":0.40832},{"key":"step_size","labels":{"chain":"1"},"last":0.423222},{"key":"step_size","labels":{"chain":"1","fit":"evaluation"},"last":0.478247}],"scorecard":["convergence.rhat","convergence.ess","convergence.divergences","holdout.mape","holdout.coverage_90","data.missing","data.history"],"pictures":[],"inputs":[{"var":"price","kind":"number","dtype":"float64","levels":[]},{"dim":"t","kind":"date","levels":[],"date_min":"2015-01-01","date_max":"2019-12-01","policy":"extend"}],"score_modes":["evaluate","filter","forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","time_dim":"t","has_grid":false,"views":["forecast"],"family":"ts","namespace":"ts"},
    issues: [],
    suggestions: [{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":["batch: the model has no row dim"]}],
    baseline: {"lead":"We expect 1,073 store visits next month, likely between 1,030 and 1,108.","tiles":[{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false},{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false}],"channels":[],"series":[{"name":"y_hat","label":"Store visits","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89}]}],"conditioning":{"dim":"t","fit_end":"2019-12-01","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":1,"horizon_source":"rows","periods":12,"parameters":"fitted","series":[{"series":{},"n_actuals":0,"revised":0,"missing":0,"origin":"2019-12-01","forecast_from":"2020-01-01","forecast_to":"2020-12-01","n_forecast":12}],"truncated":false,"message":"Forecast from the end of the fitted history (2019-12-01); no actuals were supplied. 12 periods forecast from 2020-01-01 to 2020-12-01."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/store-visits@v3"},"schema_version":2,"preset":"forecast","panels":[{"panel_id":"next","title":"Visits next month","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false}},{"panel_id":"horizon_total","title":"Visits over the next months","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false}},{"panel_id":"typical_error","title":"Typical error","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"inside_range","title":"Results inside the likely range","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"fan","title":"Visits and forecast","approximate":false,"kind":"series","intent":"fan","series":[{"name":"y_hat","label":"Visits and forecast","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89}]}],"split_at":"2020-01-01","x_kind":"time"},{"panel_id":"accuracy","title":"Accuracy over time","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"next_periods","title":"The next periods","approximate":false,"kind":"table","columns":["date","centre","low","high"],"rows":[["2020-01-01",1072.88,1029.84,1108.14],["2020-02-01",1132.04,1090.51,1168.26],["2020-03-01",1157.49,1111.79,1194.64],["2020-04-01",1210.96,1167.91,1250.21],["2020-05-01",1192.62,1145.95,1232.45],["2020-06-01",1114.61,1068.46,1154.78],["2020-07-01",1110.58,1063.05,1171.65],["2020-08-01",1029.9,972.553,1078.71],["2020-09-01",1063.1,994.053,1114.88],["2020-10-01",1036.69,984.498,1105.35],["2020-11-01",1077.35,1022.29,1142.1],["2020-12-01",1097.94,1036.78,1159.89]]},{"panel_id":"o_level","title":"Underlying level","approximate":false,"kind":"series","intent":"decomposition","series":[{"name":"level","label":"Underlying level","points":[{"t":"2020-01-01","mean":1112.18,"low":1107.59,"high":1116.88},{"t":"2020-02-01","mean":1113.81,"low":1108.33,"high":1118.59},{"t":"2020-03-01","mean":1115.44,"low":1109.72,"high":1120.7},{"t":"2020-04-01","mean":1117.07,"low":1111.43,"high":1123.73},{"t":"2020-05-01","mean":1118.7,"low":1113.24,"high":1126.74},{"t":"2020-06-01","mean":1120.33,"low":1114.48,"high":1128.77},{"t":"2020-07-01","mean":1121.97,"low":1116.35,"high":1131.59},{"t":"2020-08-01","mean":1123.6,"low":1117.55,"high":1133.72},{"t":"2020-09-01","mean":1125.23,"low":1119.27,"high":1136.27},{"t":"2020-10-01","mean":1126.86,"low":1120.72,"high":1138.51},{"t":"2020-11-01","mean":1128.49,"low":1122.17,"high":1140.81},{"t":"2020-12-01","mean":1130.12,"low":1123.44,"high":1143.12}]}],"split_at":"2020-01-01","x_kind":"time"}],"accuracy_history":[],"inputs_echo":[]},
    actuals: {"lead":"We expect 1,073 store visits next month, likely between 1,030 and 1,108. On the 12 results you sent, the error was 3.6% on average.","tiles":[{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false},{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false},{"key":"panel","label":"Typical error","value":38.8345,"unit":"number","delta_dir":"flat","caption":"Over 12 results that you sent","panel_id":"typical_error","approximate":false},{"key":"panel","label":"Results inside the likely range","value":58.3333,"unit":"percent","delta_dir":"flat","caption":"Over 12 results that you sent","panel_id":"inside_range","approximate":false}],"channels":[],"series":[{"name":"y_hat","label":"Store visits","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14,"actual":1053.31},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26,"actual":1139.81},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64,"actual":1211.35},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21,"actual":1145.12},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45,"actual":1192.62},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78,"actual":1144.82},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65,"actual":1061.71},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71,"actual":1051.13},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88,"actual":1032.89},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35,"actual":1048.77},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1,"actual":1155.23},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89,"actual":999.445}]}],"accuracy":{"mape":3.55584,"bias":4.99583,"band_hit_rate":58.3333,"n":12,"mae":38.8345,"rmse":48.5231,"wape":3.52075,"smape":3.52375,"r2":0.465106},"conditioning":{"dim":"t","fit_end":"2019-12-01","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":1,"horizon_source":"rows","periods":12,"parameters":"fitted","series":[{"series":{},"n_actuals":0,"revised":0,"missing":0,"origin":"2019-12-01","forecast_from":"2020-01-01","forecast_to":"2020-12-01","n_forecast":12}],"truncated":false,"message":"Forecast from the end of the fitted history (2019-12-01); no actuals were supplied. 12 periods forecast from 2020-01-01 to 2020-12-01."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b002"],"input_fingerprint":"sha256:abababababababababababababababababababababababababababababababab","notes_plain":["Your numbers were compared with the model's predictions; the model does not change."],"technical":{},"schema_version":2,"preset":"forecast","panels":[{"panel_id":"next","title":"Visits next month","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false}},{"panel_id":"horizon_total","title":"Visits over the next months","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false}},{"panel_id":"typical_error","title":"Typical error","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Typical error","value":38.8345,"unit":"number","delta_dir":"flat","caption":"Over 12 results that you sent","panel_id":"typical_error","approximate":false}},{"panel_id":"inside_range","title":"Results inside the likely range","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Results inside the likely range","value":58.3333,"unit":"percent","delta_dir":"flat","caption":"Over 12 results that you sent","panel_id":"inside_range","approximate":false}},{"panel_id":"fan","title":"Visits and forecast","approximate":false,"kind":"series","intent":"fan","series":[{"name":"y_hat","label":"Visits and forecast","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14,"actual":1053.31},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26,"actual":1139.81},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64,"actual":1211.35},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21,"actual":1145.12},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45,"actual":1192.62},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78,"actual":1144.82},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65,"actual":1061.71},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71,"actual":1051.13},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88,"actual":1032.89},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35,"actual":1048.77},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1,"actual":1155.23},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89,"actual":999.445}]}],"split_at":"2020-01-01","x_kind":"time"},{"panel_id":"accuracy","title":"Accuracy over time","approximate":false,"kind":"series","intent":"accuracy_history","series":[{"name":"mape","label":"Accuracy over time","points":[{"t":"2026-08-03","mean":4.1959},{"t":"2026-09-02","mean":3.80475},{"t":"2026-10-05","mean":3.55584}]}],"x_kind":"time"},{"panel_id":"next_periods","title":"The next periods","approximate":false,"kind":"table","columns":["date","centre","low","high"],"rows":[["2020-01-01",1072.88,1029.84,1108.14],["2020-02-01",1132.04,1090.51,1168.26],["2020-03-01",1157.49,1111.79,1194.64],["2020-04-01",1210.96,1167.91,1250.21],["2020-05-01",1192.62,1145.95,1232.45],["2020-06-01",1114.61,1068.46,1154.78],["2020-07-01",1110.58,1063.05,1171.65],["2020-08-01",1029.9,972.553,1078.71],["2020-09-01",1063.1,994.053,1114.88],["2020-10-01",1036.69,984.498,1105.35],["2020-11-01",1077.35,1022.29,1142.1],["2020-12-01",1097.94,1036.78,1159.89]]},{"panel_id":"o_level","title":"Underlying level","approximate":false,"kind":"series","intent":"decomposition","series":[{"name":"level","label":"Underlying level","points":[{"t":"2020-01-01","mean":1112.18,"low":1107.59,"high":1116.88,"actual":1053.31},{"t":"2020-02-01","mean":1113.81,"low":1108.33,"high":1118.59,"actual":1139.81},{"t":"2020-03-01","mean":1115.44,"low":1109.72,"high":1120.7,"actual":1211.35},{"t":"2020-04-01","mean":1117.07,"low":1111.43,"high":1123.73,"actual":1145.12},{"t":"2020-05-01","mean":1118.7,"low":1113.24,"high":1126.74,"actual":1192.62},{"t":"2020-06-01","mean":1120.33,"low":1114.48,"high":1128.77,"actual":1144.82},{"t":"2020-07-01","mean":1121.97,"low":1116.35,"high":1131.59,"actual":1061.71},{"t":"2020-08-01","mean":1123.6,"low":1117.55,"high":1133.72,"actual":1051.13},{"t":"2020-09-01","mean":1125.23,"low":1119.27,"high":1136.27,"actual":1032.89},{"t":"2020-10-01","mean":1126.86,"low":1120.72,"high":1138.51,"actual":1048.77},{"t":"2020-11-01","mean":1128.49,"low":1122.17,"high":1140.81,"actual":1155.23},{"t":"2020-12-01","mean":1130.12,"low":1123.44,"high":1143.12,"actual":999.445}]}],"split_at":"2020-01-01","x_kind":"time"}],"accuracy_detail":{"kind":"error","n":12,"mae":38.8345,"rmse":48.5231,"mape":3.55584,"wape":3.52075,"smape":3.52375,"bias":4.99583,"r2":0.465106,"band_hit_rate":58.3333},"accuracy_history":[{"seq":3,"created_at":"2026-08-03T09:30:00Z","n":12,"values":{"mae":45.8247,"rmse":57.2573,"mape":4.1959,"wape":4.15449,"smape":4.15803,"bias":5.89508,"r2":0.548824,"band_hit_rate":68.8333,"n":12.0}},{"seq":5,"created_at":"2026-09-02T09:30:00Z","n":12,"values":{"mae":41.5529,"rmse":51.9198,"mape":3.80475,"wape":3.7672,"smape":3.77041,"bias":5.34554,"r2":0.497663,"band_hit_rate":62.4167,"n":12.0}},{"seq":7,"created_at":"2026-10-05T09:30:00Z","n":12,"values":{"mae":38.8345,"rmse":48.5231,"mape":3.55584,"wape":3.52075,"smape":3.52375,"bias":4.99583,"r2":0.465106,"band_hit_rate":58.3333,"n":12.0}}],"inputs_echo":[]},
    actuals_template: {"mode":"compared","input_table":{"horizon_dim":"t","conditions_on_actuals":false,"columns":[{"name":"t","role":"coord","dims":[],"freq":"MS","description":"The period (yyyy-MM-dd)"},{"name":"y","role":"target","dims":["t"],"description":"Leave empty. This model forecasts from the end of its fitted history"},{"name":"price","role":"input","dims":["t"],"description":"An input"}],"rows":[["2020-01-01",null,null],["2020-02-01",null,null],["2020-03-01",null,null],["2020-04-01",null,null],["2020-05-01",null,null],["2020-06-01",null,null],["2020-07-01",null,null],["2020-08-01",null,null]]},"kpi_column":"y","example_csv":"t,y,price\n2020-01-01,,\n2020-02-01,,\n2020-03-01,,\n2020-04-01,,\n2020-05-01,,\n2020-06-01,,\n2020-07-01,,\n2020-08-01,,\n","max_rows":520,"match":"time"},
    prediction: {"lead":"We expect 1,073 store visits next month, likely between 1,030 and 1,108.","tiles":[{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false},{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false}],"channels":[],"series":[{"name":"y_hat","label":"Store visits","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89}]}],"conditioning":{"dim":"t","fit_end":"2019-12-01","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":1,"horizon_source":"rows","periods":12,"parameters":"fitted","series":[{"series":{},"n_actuals":0,"revised":0,"missing":0,"origin":"2019-12-01","forecast_from":"2020-01-01","forecast_to":"2020-12-01","n_forecast":12}],"truncated":false,"message":"Forecast from the end of the fitted history (2019-12-01); no actuals were supplied. 12 periods forecast from 2020-01-01 to 2020-12-01."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b004"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"forecast","panels":[{"panel_id":"next","title":"Visits next month","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits next month","value":1072.88,"unit":"count","delta_dir":"flat","low":1029.84,"high":1108.14,"caption":"Period 2020-01-01","panel_id":"next","approximate":false}},{"panel_id":"horizon_total","title":"Visits over the next months","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Visits over the next months","value":13296.2,"unit":"count","delta_dir":"flat","low":12813.3,"high":13711.7,"caption":"The total over the next 12 periods","panel_id":"horizon_total","approximate":false}},{"panel_id":"typical_error","title":"Typical error","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"inside_range","title":"Results inside the likely range","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"fan","title":"Visits and forecast","approximate":false,"kind":"series","intent":"fan","series":[{"name":"y_hat","label":"Visits and forecast","points":[{"t":"2015-01-01","actual":996.728},{"t":"2015-02-01","actual":1023.87},{"t":"2015-03-01","actual":1097.29},{"t":"2015-04-01"},{"t":"2015-05-01","actual":1094.1},{"t":"2015-06-01","actual":1002.02},{"t":"2015-07-01","actual":1038.8},{"t":"2015-08-01","actual":1016.76},{"t":"2015-09-01","actual":994.118},{"t":"2015-10-01","actual":993.673},{"t":"2015-11-01","actual":985.838},{"t":"2015-12-01","actual":1049.71},{"t":"2016-01-01","actual":1027.6},{"t":"2016-02-01","actual":1071.01},{"t":"2016-03-01","actual":1126.2},{"t":"2016-04-01","actual":1152.02},{"t":"2016-05-01","actual":1075.85},{"t":"2016-06-01","actual":1095.61},{"t":"2016-07-01","actual":1042.83},{"t":"2016-08-01","actual":1029.62},{"t":"2016-09-01","actual":939.79},{"t":"2016-10-01","actual":995.159},{"t":"2016-11-01","actual":953.298},{"t":"2016-12-01","actual":1046.89},{"t":"2017-01-01","actual":1012.22},{"t":"2017-02-01","actual":1049.81},{"t":"2017-03-01","actual":1096.22},{"t":"2017-04-01","actual":1122.12},{"t":"2017-05-01","actual":1121.59},{"t":"2017-06-01","actual":1085.79},{"t":"2017-07-01"},{"t":"2017-08-01","actual":1049.36},{"t":"2017-09-01","actual":1038.02},{"t":"2017-10-01","actual":946.62},{"t":"2017-11-01","actual":1082.13},{"t":"2017-12-01","actual":1052.66},{"t":"2018-01-01","actual":1076.6},{"t":"2018-02-01","actual":1089.45},{"t":"2018-03-01","actual":1071.37},{"t":"2018-04-01","actual":1110.5},{"t":"2018-05-01","actual":1101.9},{"t":"2018-06-01","actual":1080.1},{"t":"2018-07-01","actual":1078.94},{"t":"2018-08-01","actual":1027.8},{"t":"2018-09-01","actual":986.316},{"t":"2018-10-01"},{"t":"2018-11-01","actual":1051.01},{"t":"2018-12-01","actual":1083.62},{"t":"2019-01-01","actual":1066.24},{"t":"2019-02-01","actual":1162.73},{"t":"2019-03-01","actual":1172.68},{"t":"2019-04-01","actual":1107.5},{"t":"2019-05-01","actual":1161.95},{"t":"2019-06-01","actual":1120.6},{"t":"2019-07-01","actual":1145.03},{"t":"2019-08-01","actual":1076.63},{"t":"2019-09-01","actual":1081.53},{"t":"2019-10-01","actual":1063.39},{"t":"2019-11-01","actual":1095.01},{"t":"2019-12-01","actual":1044.67},{"t":"2020-01-01","mean":1072.88,"low":1029.84,"high":1108.14},{"t":"2020-02-01","mean":1132.04,"low":1090.51,"high":1168.26},{"t":"2020-03-01","mean":1157.49,"low":1111.79,"high":1194.64},{"t":"2020-04-01","mean":1210.96,"low":1167.91,"high":1250.21},{"t":"2020-05-01","mean":1192.62,"low":1145.95,"high":1232.45},{"t":"2020-06-01","mean":1114.61,"low":1068.46,"high":1154.78},{"t":"2020-07-01","mean":1110.58,"low":1063.05,"high":1171.65},{"t":"2020-08-01","mean":1029.9,"low":972.553,"high":1078.71},{"t":"2020-09-01","mean":1063.1,"low":994.053,"high":1114.88},{"t":"2020-10-01","mean":1036.69,"low":984.498,"high":1105.35},{"t":"2020-11-01","mean":1077.35,"low":1022.29,"high":1142.1},{"t":"2020-12-01","mean":1097.94,"low":1036.78,"high":1159.89}]}],"split_at":"2020-01-01","x_kind":"time"},{"panel_id":"accuracy","title":"Accuracy over time","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"next_periods","title":"The next periods","approximate":false,"kind":"table","columns":["date","centre","low","high"],"rows":[["2020-01-01",1072.88,1029.84,1108.14],["2020-02-01",1132.04,1090.51,1168.26],["2020-03-01",1157.49,1111.79,1194.64],["2020-04-01",1210.96,1167.91,1250.21],["2020-05-01",1192.62,1145.95,1232.45],["2020-06-01",1114.61,1068.46,1154.78],["2020-07-01",1110.58,1063.05,1171.65],["2020-08-01",1029.9,972.553,1078.71],["2020-09-01",1063.1,994.053,1114.88],["2020-10-01",1036.69,984.498,1105.35],["2020-11-01",1077.35,1022.29,1142.1],["2020-12-01",1097.94,1036.78,1159.89]]},{"panel_id":"o_level","title":"Underlying level","approximate":false,"kind":"series","intent":"decomposition","series":[{"name":"level","label":"Underlying level","points":[{"t":"2020-01-01","mean":1112.18,"low":1107.59,"high":1116.88},{"t":"2020-02-01","mean":1113.81,"low":1108.33,"high":1118.59},{"t":"2020-03-01","mean":1115.44,"low":1109.72,"high":1120.7},{"t":"2020-04-01","mean":1117.07,"low":1111.43,"high":1123.73},{"t":"2020-05-01","mean":1118.7,"low":1113.24,"high":1126.74},{"t":"2020-06-01","mean":1120.33,"low":1114.48,"high":1128.77},{"t":"2020-07-01","mean":1121.97,"low":1116.35,"high":1131.59},{"t":"2020-08-01","mean":1123.6,"low":1117.55,"high":1133.72},{"t":"2020-09-01","mean":1125.23,"low":1119.27,"high":1136.27},{"t":"2020-10-01","mean":1126.86,"low":1120.72,"high":1138.51},{"t":"2020-11-01","mean":1128.49,"low":1122.17,"high":1140.81},{"t":"2020-12-01","mean":1130.12,"low":1123.44,"high":1143.12}]}],"split_at":"2020-01-01","x_kind":"time"}],"prediction":{"rows":[{"inputs":{"price":10.0},"outputs":{"y_hat":{"centre":1072.88,"low":1029.84,"high":1108.14}}}],"band":"outcome","approximate":false},"accuracy_history":[],"inputs_echo":[{"price":10.0}]},
  },
  regression: {
    spec: {"layout_version":2,"preset":"regression","title":"Weekly demand per store","client_name":"Harbour Retail","summary":"How many units a store sells in a week, for the price and promotion you set. The range shows where one new week will probably be.","note":"The model was fitted on 60 weeks of sales of five stores.","period_label":"case","source":{"kind":"endpoint","alias":"store-demand"},"subject":{"variable":"y_hat","label":"Units sold","unit":"count","decimals":1},"panels":[{"id":"predicted","title":"Predicted units","kind":"kpi","source":{"kind":"output","variable":"y_hat","select":{}},"stat":"centre","reduce":{"how":"none","over":[]},"band":"outcome","compare":"previous","good":"up"},{"id":"m_rmse","title":"Typical error (RMSE)","kind":"kpi","source":{"kind":"metric","key":"rmse","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"down"},{"id":"m_coverage_80","title":"Results inside the 80% range","kind":"kpi","source":{"kind":"metric","key":"coverage_80","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"none"},{"id":"by_case","title":"Units per store this week","kind":"chart","intent":"range","source":{"kind":"output","variable":"y_hat","select":{"sku":"k0","week":"2025-03-02"}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"fit","title":"Predicted and actual","kind":"chart","intent":"scatter","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"residuals","title":"Errors per case","kind":"chart","intent":"residuals","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20}],"scenario_rows":{"enabled":true,"max_rows":5,"outputs":["y_hat"],"inputs":[{"name":"price","label":"Price","help":"The shelf price in EUR","bind":{"var":"price"},"type":"number","min":5.0,"max":40.0,"step":0.5,"choices":[],"default":18.0,"applies":"row"},{"name":"promo","label":"Promotion","help":"1 when the product is on promotion","bind":{"var":"promo"},"type":"integer","min":0.0,"max":1.0,"step":1.0,"choices":[],"default":0.0,"applies":"row"},{"name":"store","label":"Store","bind":{"dim":"store"},"type":"choice","choices":["s0","s1","s2","s3","s4"],"default":"s0","applies":"row"},{"name":"week","label":"Week","bind":{"dim":"time"},"type":"date","choices":[],"date_min":"2025-03-02","date_max":"2025-05-25","default":"2025-03-02","applies":"row"}],"fixed":{"sku":"k0"}},"actuals":{"enabled":true,"publish":"review","allow_refit_requests":false,"match":"row","id_column":"store","threshold":0.5},"batch":{"enabled":true,"outputs":["y_hat"],"max_rows":2000,"id_column":"store","enforce_ranges":true},"refresh":{"follow_endpoint":false},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    catalog: {"outputs":[{"name":"y_hat","manifest_name":"y_hat","dims":["store_id","sku","week"],"time_dim":"week","shape":"time","representation":"interval","classes":[],"centre":"point","stats":["centre","low","high"],"reduces":["none","select","last","next","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true}],"metrics":[{"key":"coverage_80","labels":{},"last":0.891667},{"key":"mape","labels":{},"last":23.0827},{"key":"rmse","labels":{},"last":4.26232}],"scorecard":["holdout.mape","data.missing","data.history"],"pictures":[],"inputs":[{"var":"price","kind":"number","dtype":"float64","levels":[]},{"var":"promo","kind":"number","dtype":"float64","levels":[]},{"dim":"sku","kind":"choice","levels":["k0","k1","k2"],"policy":"fixed"},{"dim":"store","kind":"choice","levels":["s0","s1","s2","s3","s4"],"policy":"new_levels"},{"dim":"time","kind":"date","levels":[],"date_min":"2024-01-07","date_max":"2025-02-23","policy":"extend"}],"score_modes":["counterfactual","evaluate","forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","time_dim":"time","has_grid":false,"views":["forecast","counterfactual"],"family":"sklearn","namespace":"sklearn"},
    issues: [{"code":"BINDING_INVALID","severity":"warning","pointer":"/actuals/match","message":"the model has no row dim to match cases on","context":{}}],
    suggestions: [{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":[]},{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":[]}],
    baseline: {"lead":"For the case you set, the model predicts 12.9 units sold, likely between 8.6 and 19.0.","tiles":[{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false},{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false},{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}],"channels":[],"series":[],"conditioning":{"dim":"time","fit_end":"2025-02-23","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":18,"horizon_source":"rows","periods":4,"parameters":"fitted","series":[{"series":{"sku":"k0","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4}],"truncated":false,"message":"Forecast from the end of the fitted history (2025-02-23); no actuals were supplied. 4 periods forecast from 2025-03-02 to 2025-03-23."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/store-demand@v3"},"schema_version":2,"preset":"regression","panels":[{"panel_id":"predicted","title":"Predicted units","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false}},{"panel_id":"m_rmse","title":"Typical error (RMSE)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false}},{"panel_id":"m_coverage_80","title":"Results inside the 80% range","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}},{"panel_id":"by_case","title":"Units per store this week","approximate":false,"kind":"series","intent":"range","series":[{"name":"y_hat:s0","label":"s0","points":[{"t":"2025-03-02","mean":12.8999,"low":8.64949,"high":19.0225}]},{"name":"y_hat:s1","label":"s1","points":[{"t":"2025-03-02","mean":21.2809,"low":14.4677,"high":31.0952}]},{"name":"y_hat:s2","label":"s2","points":[{"t":"2025-03-02","mean":14.4922,"low":9.75487,"high":21.3162}]},{"name":"y_hat:s3","label":"s3","points":[{"t":"2025-03-02","mean":13.1181,"low":8.80095,"high":19.3368}]},{"name":"y_hat:s4","label":"s4","points":[{"t":"2025-03-02","mean":14.589,"low":9.82211,"high":21.4557}]},{"name":"y_hat:s_new","label":"s_new","points":[{"t":"2025-03-02","mean":9.10224,"low":6.01311,"high":13.5521}]}],"x_kind":"time"},{"panel_id":"fit","title":"Predicted and actual","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"residuals","title":"Errors per case","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"accuracy_history":[],"inputs_echo":[]},
    actuals: {"lead":"For the case you set, the model predicts 12.9 units sold, likely between 8.6 and 19.0.","tiles":[{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false},{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false},{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}],"channels":[],"series":[],"accuracy":{"mape":40.8339,"bias":0.669776,"band_hit_rate":62.5,"n":72,"mae":5.07772,"rmse":6.66437,"wape":28.7908,"smape":30.6269,"r2":0.37972},"conditioning":{"dim":"time","fit_end":"2025-02-23","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":18,"horizon_source":"rows","periods":4,"parameters":"fitted","series":[{"series":{"sku":"k0","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4}],"truncated":false,"message":"Forecast from the end of the fitted history (2025-02-23); no actuals were supplied. 4 periods forecast from 2025-03-02 to 2025-03-23."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b002"],"input_fingerprint":"sha256:abababababababababababababababababababababababababababababababab","notes_plain":["Your numbers were compared with the model's predictions; the model does not change."],"technical":{},"schema_version":2,"preset":"regression","panels":[{"panel_id":"predicted","title":"Predicted units","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false}},{"panel_id":"m_rmse","title":"Typical error (RMSE)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false}},{"panel_id":"m_coverage_80","title":"Results inside the 80% range","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}},{"panel_id":"by_case","title":"Units per store this week","approximate":false,"kind":"series","intent":"range","series":[{"name":"y_hat:s0","label":"s0","points":[{"t":"2025-03-02","mean":12.8999,"low":8.64949,"high":19.0225}]},{"name":"y_hat:s1","label":"s1","points":[{"t":"2025-03-02","mean":21.2809,"low":14.4677,"high":31.0952}]},{"name":"y_hat:s2","label":"s2","points":[{"t":"2025-03-02","mean":14.4922,"low":9.75487,"high":21.3162}]},{"name":"y_hat:s3","label":"s3","points":[{"t":"2025-03-02","mean":13.1181,"low":8.80095,"high":19.3368}]},{"name":"y_hat:s4","label":"s4","points":[{"t":"2025-03-02","mean":14.589,"low":9.82211,"high":21.4557}]},{"name":"y_hat:s_new","label":"s_new","points":[{"t":"2025-03-02","mean":9.10224,"low":6.01311,"high":13.5521}]}],"x_kind":"time"},{"panel_id":"fit","title":"Predicted and actual","approximate":false,"kind":"scatter","intent":"scatter","x_label":"Predicted","y_label":"Actual","points":[{"x":12.8999,"y":10.307},{"x":16.3908,"y":16.391},{"x":24.7958,"y":19.983},{"x":21.2809,"y":21.281},{"x":27.4193,"y":22.117},{"x":18.4264,"y":18.426},{"x":14.4922,"y":11.602},{"x":20.0831,"y":20.083},{"x":28.8057,"y":23.245},{"x":13.1181,"y":13.118},{"x":14.6478,"y":11.728},{"x":20.1653,"y":20.165},{"x":14.589,"y":11.681},{"x":16.9144,"y":16.914},{"x":27.2924,"y":22.014},{"x":9.10224,"y":9.102},{"x":16.7646,"y":13.45},{"x":20.1645,"y":20.164},{"x":15.33,"y":16.549},{"x":30.3306,"y":38.514},{"x":21.2939,"y":22.958},{"x":12.1565,"y":15.593},{"x":27.777,"y":29.925},{"x":19.3486,"y":24.664},{"x":15.6621,"y":16.906},{"x":13.1835,"y":16.888},{"x":30.8927,"y":33.273},{"x":14.5627,"y":18.628},{"x":14.6478,"y":15.816},{"x":36.9517,"y":46.864},{"x":13.3947,"y":14.469},{"x":19.5694,"y":24.942},{"x":17.6286,"y":19.019},{"x":10.9938,"y":14.127},{"x":25.0021,"y":26.943},{"x":18.8543,"y":24.04},{"x":14.0012,"y":21.278},{"x":16.8896,"y":10.882},{"x":22.1147,"y":33.327},{"x":12.0715,"y":7.682},{"x":11.6414,"y":17.773},{"x":20.3641,"y":13.19},{"x":16.7266,"y":25.325},{"x":21.2702,"y":13.791},{"x":28.3533,"y":42.592},{"x":21.4418,"y":13.905},{"x":14.456,"y":21.953},{"x":22.2803,"y":14.462},{"x":6.52489,"y":10.175},{"x":10.6107,"y":6.712},{"x":13.3891,"y":20.369},{"x":10.6525,"y":6.739},{"x":16.9971,"y":25.727},{"x":21.5166,"y":13.955},{"x":15.0419,"y":5.465},{"x":15.3566,"y":17.798},{"x":17.3646,"y":6.401},{"x":8.13528,"y":9.499},{"x":26.5278,"y":10.093},{"x":19.6496,"y":22.732},{"x":14.8416,"y":5.384},{"x":23.8001,"y":27.502},{"x":27.2533,"y":10.386},{"x":11.4073,"y":13.259},{"x":27.5563,"y":10.508},{"x":20.9031,"y":24.172},{"x":12.6822,"y":4.514},{"x":10.1675,"y":11.834},{"x":27.7673,"y":10.593},{"x":11.9186,"y":13.847},{"x":15.5118,"y":5.654},{"x":15.9391,"y":18.467}]},{"panel_id":"residuals","title":"Errors per case","approximate":false,"kind":"scatter","intent":"residuals","x_label":"Predicted","y_label":"Actual minus predicted","points":[{"x":12.8999,"y":-2.5929},{"x":16.3908,"y":0.000180284},{"x":24.7958,"y":-4.81284},{"x":21.2809,"y":9.09438e-05},{"x":27.4193,"y":-5.30229},{"x":18.4264,"y":-0.000426896},{"x":14.4922,"y":-2.89017},{"x":20.0831,"y":-7.98672e-05},{"x":28.8057,"y":-5.56069},{"x":13.1181,"y":-6.76379e-05},{"x":14.6478,"y":-2.91984},{"x":20.1653,"y":-0.00026453},{"x":14.589,"y":-2.90803},{"x":16.9144,"y":-0.000357968},{"x":27.2924,"y":-5.27838},{"x":9.10224,"y":-0.0002417},{"x":16.7646,"y":-3.31461},{"x":20.1645,"y":-0.000461444},{"x":15.33,"y":1.21897},{"x":30.3306,"y":8.18338},{"x":21.2939,"y":1.66407},{"x":12.1565,"y":3.43653},{"x":27.777,"y":2.14801},{"x":19.3486,"y":5.31539},{"x":15.6621,"y":1.24393},{"x":13.1835,"y":3.70453},{"x":30.8927,"y":2.38034},{"x":14.5627,"y":4.06526},{"x":14.6478,"y":1.16816},{"x":36.9517,"y":9.91227},{"x":13.3947,"y":1.07427},{"x":19.5694,"y":5.37259},{"x":17.6286,"y":1.39045},{"x":10.9938,"y":3.13317},{"x":25.0021,"y":1.94088},{"x":18.8543,"y":5.18568},{"x":14.0012,"y":7.27684},{"x":16.8896,"y":-6.00763},{"x":22.1147,"y":11.2123},{"x":12.0715,"y":-4.38952},{"x":11.6414,"y":6.1316},{"x":20.3641,"y":-7.17406},{"x":16.7266,"y":8.59843},{"x":21.2702,"y":-7.47916},{"x":28.3533,"y":14.2387},{"x":21.4418,"y":-7.53684},{"x":14.456,"y":7.49702},{"x":22.2803,"y":-7.81829},{"x":6.52489,"y":3.65011},{"x":10.6107,"y":-3.89869},{"x":13.3891,"y":6.97993},{"x":10.6525,"y":-3.91345},{"x":16.9971,"y":8.72986},{"x":21.5166,"y":-7.5616},{"x":15.0419,"y":-9.57695},{"x":15.3566,"y":2.44144},{"x":17.3646,"y":-10.9636},{"x":8.13528,"y":1.36372},{"x":26.5278,"y":-16.4348},{"x":19.6496,"y":3.08244},{"x":14.8416,"y":-9.45761},{"x":23.8001,"y":3.70189},{"x":27.2533,"y":-16.8673},{"x":11.4073,"y":1.85169},{"x":27.5563,"y":-17.0483},{"x":20.9031,"y":3.26894},{"x":12.6822,"y":-8.16816},{"x":10.1675,"y":1.66654},{"x":27.7673,"y":-17.1743},{"x":11.9186,"y":1.9284},{"x":15.5118,"y":-9.85776},{"x":15.9391,"y":2.52793}]}],"accuracy_detail":{"kind":"error","n":72,"mae":5.07772,"rmse":6.66437,"mape":40.8339,"wape":28.7908,"smape":30.6269,"bias":0.669776,"r2":0.37972,"band_hit_rate":62.5},"accuracy_history":[{"seq":3,"created_at":"2026-08-03T09:30:00Z","n":72,"values":{"mae":5.99171,"rmse":7.86396,"mape":48.184,"wape":33.9732,"smape":36.1398,"bias":0.790336,"r2":0.448069,"band_hit_rate":73.75,"n":72.0}},{"seq":5,"created_at":"2026-09-02T09:30:00Z","n":72,"values":{"mae":5.43316,"rmse":7.13088,"mape":43.6923,"wape":30.8062,"smape":32.7708,"bias":0.71666,"r2":0.4063,"band_hit_rate":66.875,"n":72.0}},{"seq":7,"created_at":"2026-10-05T09:30:00Z","n":72,"values":{"mae":5.07772,"rmse":6.66437,"mape":40.8339,"wape":28.7908,"smape":30.6269,"bias":0.669776,"r2":0.37972,"band_hit_rate":62.5,"n":72.0}}],"inputs_echo":[]},
    actuals_template: {"mode":"compared","input_table":{"horizon_dim":"time","conditions_on_actuals":false,"columns":[{"name":"time","role":"coord","dims":[],"freq":"W-SUN","description":"The period (yyyy-MM-dd)"},{"name":"sku","role":"coord","dims":[],"description":"A level of sku"},{"name":"store","role":"coord","dims":[],"description":"A level of store"},{"name":"y","role":"target","dims":["store","sku","time"],"description":"Leave empty. This model forecasts from the end of its fitted history"},{"name":"price","role":"driver","dims":["store","sku","time"],"description":"Necessary on each period, also on the periods to forecast"},{"name":"promo","role":"driver","dims":["store","sku","time"],"description":"Necessary on each period, also on the periods to forecast"}],"rows":[["2025-03-02","k0","s0",null,null,null],["2025-03-02","k0","s1",null,null,null],["2025-03-02","k1","s0",null,null,null],["2025-03-02","k1","s1",null,null,null],["2025-03-09","k0","s0",null,null,null],["2025-03-09","k0","s1",null,null,null],["2025-03-09","k1","s0",null,null,null],["2025-03-09","k1","s1",null,null,null],["2025-03-16","k0","s0",null,null,null],["2025-03-16","k0","s1",null,null,null],["2025-03-16","k1","s0",null,null,null],["2025-03-16","k1","s1",null,null,null],["2025-03-23","k0","s0",null,null,null],["2025-03-23","k0","s1",null,null,null],["2025-03-23","k1","s0",null,null,null],["2025-03-23","k1","s1",null,null,null],["2025-03-30","k0","s0",null,null,null],["2025-03-30","k0","s1",null,null,null],["2025-03-30","k1","s0",null,null,null],["2025-03-30","k1","s1",null,null,null],["2025-04-06","k0","s0",null,null,null],["2025-04-06","k0","s1",null,null,null],["2025-04-06","k1","s0",null,null,null],["2025-04-06","k1","s1",null,null,null],["2025-04-13","k0","s0",null,null,null],["2025-04-13","k0","s1",null,null,null],["2025-04-13","k1","s0",null,null,null],["2025-04-13","k1","s1",null,null,null],["2025-04-20","k0","s0",null,null,null],["2025-04-20","k0","s1",null,null,null],["2025-04-20","k1","s0",null,null,null],["2025-04-20","k1","s1",null,null,null]]},"kpi_column":"y","example_csv":"store,time,price,promo,y\n","max_rows":5000,"match":"row","id_column":"store"},
    batch_template: {"columns":[{"name":"store","role":"id","type":"text","required":true,"choices":[]},{"name":"time","role":"driver","type":"date","required":true,"choices":[]},{"name":"price","role":"driver","type":"number","required":true,"min":5.0,"max":40.0,"choices":[]},{"name":"promo","role":"driver","type":"integer","required":true,"min":0.0,"max":1.0,"choices":[]}],"id_column":"store","example_csv":"store,time,price,promo\ncase1,2025-03-02,18,0\ncase2,2025-03-02,18,0\n","max_rows":2000},
    batch: {"lead":"For the case you set, the model predicts 12.9 units sold, likely between 8.6 and 19.0.","tiles":[{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false},{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false},{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}],"channels":[],"series":[],"conditioning":{"dim":"time","fit_end":"2025-02-23","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":18,"horizon_source":"rows","periods":4,"parameters":"fitted","series":[{"series":{"sku":"k0","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4}],"truncated":false,"message":"Forecast from the end of the fitted history (2025-02-23); no actuals were supplied. 4 periods forecast from 2025-03-02 to 2025-03-23."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b003"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"regression","panels":[{"panel_id":"predicted","title":"Predicted units","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false}},{"panel_id":"m_rmse","title":"Typical error (RMSE)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false}},{"panel_id":"m_coverage_80","title":"Results inside the 80% range","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}},{"panel_id":"by_case","title":"Units per store this week","approximate":false,"kind":"series","intent":"range","series":[{"name":"y_hat:s0","label":"s0","points":[{"t":"2025-03-02","mean":12.8999,"low":8.64949,"high":19.0225}]},{"name":"y_hat:s1","label":"s1","points":[{"t":"2025-03-02","mean":21.2809,"low":14.4677,"high":31.0952}]},{"name":"y_hat:s2","label":"s2","points":[{"t":"2025-03-02","mean":14.4922,"low":9.75487,"high":21.3162}]},{"name":"y_hat:s3","label":"s3","points":[{"t":"2025-03-02","mean":13.1181,"low":8.80095,"high":19.3368}]},{"name":"y_hat:s4","label":"s4","points":[{"t":"2025-03-02","mean":14.589,"low":9.82211,"high":21.4557}]},{"name":"y_hat:s_new","label":"s_new","points":[{"t":"2025-03-02","mean":9.10224,"low":6.01311,"high":13.5521}]}],"x_kind":"time"},{"panel_id":"fit","title":"Predicted and actual","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"residuals","title":"Errors per case","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"batch":{"n_rows":72,"n_scored":72,"outputs":["y_hat"],"stats":{"y_hat":{"mean":18.3064,"min":6.52489,"p10":11.4307,"p50":16.902,"p90":27.5426,"max":36.9517}},"class_counts":[],"histogram":[{"lo":6.52489,"hi":8.04623,"count":1},{"lo":8.04623,"hi":9.56757,"count":2},{"lo":9.56757,"hi":11.0889,"count":4},{"lo":11.0889,"hi":12.6103,"count":5},{"lo":12.6103,"hi":14.1316,"count":7},{"lo":14.1316,"hi":15.6529,"count":11},{"lo":15.6529,"hi":17.1743,"count":8},{"lo":17.1743,"hi":18.6956,"count":3},{"lo":18.6956,"hi":20.217,"count":7},{"lo":20.217,"hi":21.7383,"count":7},{"lo":21.7383,"hi":23.2596,"count":2},{"lo":23.2596,"hi":24.781,"count":1},{"lo":24.781,"hi":26.3023,"count":2},{"lo":26.3023,"hi":27.8237,"count":7},{"lo":27.8237,"hi":29.345,"count":2},{"lo":29.345,"hi":30.8664,"count":1},{"lo":30.8664,"hi":32.3877,"count":1},{"lo":32.3877,"hi":33.909,"count":0},{"lo":33.909,"hi":35.4304,"count":0},{"lo":35.4304,"hi":36.9517,"count":1}],"preview":[{"id":"s0 \u00b7 k0 \u00b7 2025-03-02","y_hat":12.8999},{"id":"s0 \u00b7 k0 \u00b7 2025-03-09","y_hat":15.33},{"id":"s0 \u00b7 k0 \u00b7 2025-03-16","y_hat":14.0012},{"id":"s0 \u00b7 k0 \u00b7 2025-03-23","y_hat":15.0419},{"id":"s0 \u00b7 k1 \u00b7 2025-03-02","y_hat":16.3908},{"id":"s0 \u00b7 k1 \u00b7 2025-03-09","y_hat":30.3306},{"id":"s0 \u00b7 k1 \u00b7 2025-03-16","y_hat":16.8896},{"id":"s0 \u00b7 k1 \u00b7 2025-03-23","y_hat":15.3566},{"id":"s0 \u00b7 k2 \u00b7 2025-03-02","y_hat":24.7958},{"id":"s0 \u00b7 k2 \u00b7 2025-03-09","y_hat":21.2939},{"id":"s0 \u00b7 k2 \u00b7 2025-03-16","y_hat":22.1147},{"id":"s0 \u00b7 k2 \u00b7 2025-03-23","y_hat":17.3646},{"id":"s1 \u00b7 k0 \u00b7 2025-03-02","y_hat":21.2809},{"id":"s1 \u00b7 k0 \u00b7 2025-03-09","y_hat":12.1565},{"id":"s1 \u00b7 k0 \u00b7 2025-03-16","y_hat":12.0715},{"id":"s1 \u00b7 k0 \u00b7 2025-03-23","y_hat":8.13528},{"id":"s1 \u00b7 k1 \u00b7 2025-03-02","y_hat":27.4193},{"id":"s1 \u00b7 k1 \u00b7 2025-03-09","y_hat":27.777},{"id":"s1 \u00b7 k1 \u00b7 2025-03-16","y_hat":11.6414},{"id":"s1 \u00b7 k1 \u00b7 2025-03-23","y_hat":26.5278}],"download":{"available":true,"rows":72,"bytes_estimate":3024}},"accuracy_history":[],"inputs_echo":[]},
    prediction: {"lead":"For the case you set, the model predicts 12.9 units sold, likely between 8.6 and 19.0.","tiles":[{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false},{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false},{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}],"channels":[],"series":[],"conditioning":{"dim":"time","fit_end":"2025-02-23","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":18,"horizon_source":"rows","periods":4,"parameters":"fitted","series":[{"series":{"sku":"k0","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s0"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s1"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s2"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s3"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s4"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k0","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k1","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4},{"series":{"sku":"k2","store":"s_new"},"n_actuals":0,"revised":0,"missing":0,"origin":"2025-02-23","forecast_from":"2025-03-02","forecast_to":"2025-03-23","n_forecast":4}],"truncated":false,"message":"Forecast from the end of the fitted history (2025-02-23); no actuals were supplied. 4 periods forecast from 2025-03-02 to 2025-03-23."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b004"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"regression","panels":[{"panel_id":"predicted","title":"Predicted units","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Predicted units","value":12.8999,"unit":"count","delta_dir":"flat","low":8.64949,"high":19.0225,"caption":"For store_id s0, sku k0, week 2025-03-02","panel_id":"predicted","approximate":false}},{"panel_id":"m_rmse","title":"Typical error (RMSE)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Typical error (RMSE)","value":4.26232,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_rmse","approximate":false}},{"panel_id":"m_coverage_80","title":"Results inside the 80% range","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Results inside the 80% range","value":0.891667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_coverage_80","approximate":false}},{"panel_id":"by_case","title":"Units per store this week","approximate":false,"kind":"series","intent":"range","series":[{"name":"y_hat:s0","label":"s0","points":[{"t":"2025-03-02","mean":12.8999,"low":8.64949,"high":19.0225}]},{"name":"y_hat:s1","label":"s1","points":[{"t":"2025-03-02","mean":21.2809,"low":14.4677,"high":31.0952}]},{"name":"y_hat:s2","label":"s2","points":[{"t":"2025-03-02","mean":14.4922,"low":9.75487,"high":21.3162}]},{"name":"y_hat:s3","label":"s3","points":[{"t":"2025-03-02","mean":13.1181,"low":8.80095,"high":19.3368}]},{"name":"y_hat:s4","label":"s4","points":[{"t":"2025-03-02","mean":14.589,"low":9.82211,"high":21.4557}]},{"name":"y_hat:s_new","label":"s_new","points":[{"t":"2025-03-02","mean":9.10224,"low":6.01311,"high":13.5521}]}],"x_kind":"time"},{"panel_id":"fit","title":"Predicted and actual","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"residuals","title":"Errors per case","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"prediction":{"rows":[{"inputs":{"price":18.0,"promo":0.0,"store":"s0","week":"2025-03-02"},"outputs":{"y_hat":{"centre":12.8999,"low":8.64949,"high":19.0225}}}],"band":"outcome","approximate":false},"accuracy_history":[],"inputs_echo":[{"price":18.0,"promo":0.0,"store":"s0","week":"2025-03-02"}]},
  },
  classifier: {
    spec: {"layout_version":2,"preset":"classifier","title":"High demand weeks","client_name":"Harbour Retail","summary":"The chance that a store has a high demand week, for the price and promotion you set. Score a file of planned weeks to see them all.","period_label":"case","source":{"kind":"endpoint","alias":"high-demand"},"subject":{"variable":"y_hat","label":"Chance of high demand","unit":"probability","decimals":2},"panels":[{"id":"chance","title":"Chance of high demand","kind":"kpi","source":{"kind":"output","variable":"y_hat","select":{}},"stat":"centre","reduce":{"how":"none","over":[]},"band":"none","compare":"previous","good":"up"},{"id":"m_auc","title":"How well the model ranks cases (AUC)","kind":"kpi","source":{"kind":"metric","key":"auc","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"m_accuracy","title":"How often the model is right","kind":"kpi","source":{"kind":"metric","key":"accuracy","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"chances","title":"Chance of high demand per case","kind":"chart","intent":"probability_bars","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"none","max_items":20},{"id":"calibration","title":"Predicted and observed chances","kind":"chart","intent":"calibration","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"none","max_items":20},{"id":"confusion","title":"Right and wrong results","kind":"chart","intent":"confusion","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"none","max_items":20},{"id":"accuracy","title":"How often the model was right","kind":"chart","intent":"accuracy_history","source":{"kind":"accuracy","metric":"accuracy"},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20}],"scenario_rows":{"enabled":true,"max_rows":5,"outputs":["y_hat"],"inputs":[{"name":"price","label":"Price","help":"The shelf price in EUR","bind":{"var":"price"},"type":"number","min":5.0,"max":40.0,"step":0.5,"choices":[],"default":18.0,"applies":"row"},{"name":"promo","label":"Promotion","help":"1 when the product is on promotion","bind":{"var":"promo"},"type":"integer","min":0.0,"max":1.0,"step":1.0,"choices":[],"default":0.0,"applies":"row"},{"name":"store","label":"Store","bind":{"dim":"store"},"type":"choice","choices":["s0","s1","s2","s3","s4"],"default":"s0","applies":"row"},{"name":"week","label":"Week","bind":{"dim":"time"},"type":"date","choices":[],"date_min":"2024-10-13","date_max":"2024-12-29","default":"2024-10-13","applies":"row"}],"fixed":{"sku":"k0"}},"actuals":{"enabled":true,"publish":"review","allow_refit_requests":false,"match":"row","id_column":"store","threshold":0.5},"batch":{"enabled":true,"outputs":["y_hat"],"max_rows":2000,"id_column":"store","enforce_ranges":true},"refresh":{"follow_endpoint":false},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    catalog: {"outputs":[{"name":"contributions","manifest_name":"contributions","dims":["component","store","sku","week"],"time_dim":"week","shape":"time","representation":"point","classes":[],"centre":"mean","stats":["centre"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter","decomposition"],"predictive_ok":false},{"name":"y_hat","manifest_name":"y_hat","dims":["store","sku","week"],"time_dim":"week","shape":"classification","representation":"point","meaning":"class_probability","classes":[],"centre":"mean","stats":["centre"],"reduces":["none","select","last","next","mean","sum"],"intents":["probability_bars","bars","calibration","confusion","range"],"predictive_ok":false,"units":"1"}],"metrics":[{"key":"accuracy","labels":{},"last":0.566667},{"key":"auc","labels":{},"last":0.867347},{"key":"brier","labels":{},"last":0.25699}],"scorecard":["data.missing","data.history"],"pictures":[],"inputs":[{"var":"price","kind":"number","dtype":"float64","levels":[]},{"var":"promo","kind":"number","dtype":"float64","levels":[]},{"dim":"sku","kind":"choice","levels":["k0","k1","k2"],"policy":"fixed"},{"dim":"store","kind":"choice","levels":["s0","s1","s2","s3","s4"],"policy":"new_levels"},{"dim":"time","kind":"date","levels":[],"date_min":"2024-01-07","date_max":"2024-10-06","policy":"extend"}],"score_modes":["counterfactual","evaluate","forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","time_dim":"time","has_grid":false,"views":["forecast","counterfactual"],"family":"sklearn","namespace":"sklearn"},
    issues: [{"code":"BINDING_INVALID","severity":"warning","pointer":"/actuals/match","message":"the model has no row dim to match cases on","context":{}}],
    suggestions: [{"preset":"classifier","reasons":["The model gives the chance of each class."],"blocked_inputs":[]},{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":[]}],
    baseline: {"lead":"The model gives a 55% chance of a positive result for this case.","tiles":[{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false},{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/high-demand@v3"},"schema_version":2,"preset":"classifier","panels":[{"panel_id":"chance","title":"Chance of high demand","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"chances","title":"Chance of high demand per case","caption":"The first 20 of 45.","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"s0 \u00b7 k0 \u00b7 2024-09-22","value":0.546891},{"label":"s0 \u00b7 k0 \u00b7 2024-09-29","value":0.162309},{"label":"s0 \u00b7 k0 \u00b7 2024-10-06","value":0.153694},{"label":"s0 \u00b7 k1 \u00b7 2024-09-22","value":0.462807},{"label":"s0 \u00b7 k1 \u00b7 2024-09-29","value":0.481023},{"label":"s0 \u00b7 k1 \u00b7 2024-10-06","value":0.48242},{"label":"s0 \u00b7 k2 \u00b7 2024-09-22","value":0.712665},{"label":"s0 \u00b7 k2 \u00b7 2024-09-29","value":0.786136},{"label":"s0 \u00b7 k2 \u00b7 2024-10-06","value":0.710518},{"label":"s1 \u00b7 k0 \u00b7 2024-09-22","value":0.120593},{"label":"s1 \u00b7 k0 \u00b7 2024-09-29","value":0.154293},{"label":"s1 \u00b7 k0 \u00b7 2024-10-06","value":0.135382},{"label":"s1 \u00b7 k1 \u00b7 2024-09-22","value":0.290999},{"label":"s1 \u00b7 k1 \u00b7 2024-09-29","value":0.773457},{"label":"s1 \u00b7 k1 \u00b7 2024-10-06","value":0.188255},{"label":"s1 \u00b7 k2 \u00b7 2024-09-22","value":0.62443},{"label":"s1 \u00b7 k2 \u00b7 2024-09-29","value":0.652529},{"label":"s1 \u00b7 k2 \u00b7 2024-10-06","value":0.548465},{"label":"s2 \u00b7 k0 \u00b7 2024-09-22","value":0.381929},{"label":"s2 \u00b7 k0 \u00b7 2024-09-29","value":0.39909}]},{"panel_id":"calibration","title":"Predicted and observed chances","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"confusion","title":"Right and wrong results","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"accuracy","title":"How often the model was right","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"accuracy_history":[],"inputs_echo":[]},
    actuals: {"lead":"On the 45 cases you sent, the model was right 78% of the time.","tiles":[{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false},{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b002"],"input_fingerprint":"sha256:abababababababababababababababababababababababababababababababab","notes_plain":["Your numbers were compared with the model's predictions; the model does not change."],"technical":{},"schema_version":2,"preset":"classifier","panels":[{"panel_id":"chance","title":"Chance of high demand","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"chances","title":"Chance of high demand per case","caption":"The first 20 of 45.","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"s0 \u00b7 k0 \u00b7 2024-09-22","value":0.546891},{"label":"s0 \u00b7 k0 \u00b7 2024-09-29","value":0.162309},{"label":"s0 \u00b7 k0 \u00b7 2024-10-06","value":0.153694},{"label":"s0 \u00b7 k1 \u00b7 2024-09-22","value":0.462807},{"label":"s0 \u00b7 k1 \u00b7 2024-09-29","value":0.481023},{"label":"s0 \u00b7 k1 \u00b7 2024-10-06","value":0.48242},{"label":"s0 \u00b7 k2 \u00b7 2024-09-22","value":0.712665},{"label":"s0 \u00b7 k2 \u00b7 2024-09-29","value":0.786136},{"label":"s0 \u00b7 k2 \u00b7 2024-10-06","value":0.710518},{"label":"s1 \u00b7 k0 \u00b7 2024-09-22","value":0.120593},{"label":"s1 \u00b7 k0 \u00b7 2024-09-29","value":0.154293},{"label":"s1 \u00b7 k0 \u00b7 2024-10-06","value":0.135382},{"label":"s1 \u00b7 k1 \u00b7 2024-09-22","value":0.290999},{"label":"s1 \u00b7 k1 \u00b7 2024-09-29","value":0.773457},{"label":"s1 \u00b7 k1 \u00b7 2024-10-06","value":0.188255},{"label":"s1 \u00b7 k2 \u00b7 2024-09-22","value":0.62443},{"label":"s1 \u00b7 k2 \u00b7 2024-09-29","value":0.652529},{"label":"s1 \u00b7 k2 \u00b7 2024-10-06","value":0.548465},{"label":"s2 \u00b7 k0 \u00b7 2024-09-22","value":0.381929},{"label":"s2 \u00b7 k0 \u00b7 2024-09-29","value":0.39909}]},{"panel_id":"calibration","title":"Predicted and observed chances","approximate":false,"kind":"calibration","bins":[{"p_mean":0.0431646,"observed":0.0,"n":1},{"p_mean":0.146587,"observed":0.181818,"n":11},{"p_mean":0.258416,"observed":0.333333,"n":3},{"p_mean":0.382559,"observed":0.0,"n":4},{"p_mean":0.46584,"observed":0.714286,"n":7},{"p_mean":0.545626,"observed":0.8,"n":5},{"p_mean":0.64643,"observed":0.666667,"n":3},{"p_mean":0.745694,"observed":1.0,"n":4},{"p_mean":0.865678,"observed":1.0,"n":3},{"p_mean":0.945486,"observed":1.0,"n":4}]},{"labels":["0","1"],"counts":[[18,2],[8,17]],"panel_id":"confusion","title":"Right and wrong results","approximate":false,"kind":"confusion"},{"panel_id":"accuracy","title":"How often the model was right","approximate":false,"kind":"series","intent":"accuracy_history","series":[{"name":"accuracy","label":"How often the model was right","points":[{"t":"2026-08-03","mean":0.917778},{"t":"2026-09-02","mean":0.832222},{"t":"2026-10-05","mean":0.777778}]}],"x_kind":"time"}],"accuracy_detail":{"kind":"classification","n":45,"positive":"1","threshold":0.5,"accuracy":0.777778,"brier":0.157474,"log_score":-0.480882,"auc":0.868,"rate_pred":0.422222,"rate_obs":0.555556,"bins":[{"p_mean":0.0431646,"observed":0.0,"n":1},{"p_mean":0.146587,"observed":0.181818,"n":11},{"p_mean":0.258416,"observed":0.333333,"n":3},{"p_mean":0.382559,"observed":0.0,"n":4},{"p_mean":0.46584,"observed":0.714286,"n":7},{"p_mean":0.545626,"observed":0.8,"n":5},{"p_mean":0.64643,"observed":0.666667,"n":3},{"p_mean":0.745694,"observed":1.0,"n":4},{"p_mean":0.865678,"observed":1.0,"n":3},{"p_mean":0.945486,"observed":1.0,"n":4}],"confusion":{"labels":["0","1"],"counts":[[18,2],[8,17]]}},"accuracy_history":[{"seq":3,"created_at":"2026-08-03T09:30:00Z","n":45,"values":{"accuracy":0.917778,"brier":0.18582,"log_score":-0.567441,"auc":1.02424,"n":45.0}},{"seq":5,"created_at":"2026-09-02T09:30:00Z","n":45,"values":{"accuracy":0.832222,"brier":0.168497,"log_score":-0.514544,"auc":0.92876,"n":45.0}},{"seq":7,"created_at":"2026-10-05T09:30:00Z","n":45,"values":{"accuracy":0.777778,"brier":0.157474,"log_score":-0.480882,"auc":0.868,"n":45.0}}],"inputs_echo":[]},
    actuals_template: {"mode":"compared","input_table":{"horizon_dim":"time","conditions_on_actuals":false,"columns":[{"name":"time","role":"coord","dims":[],"freq":"W-SUN","description":"The period (yyyy-MM-dd)"},{"name":"sku","role":"coord","dims":[],"description":"A level of sku"},{"name":"store","role":"coord","dims":[],"description":"A level of store"},{"name":"y","role":"target","dims":["store","sku","time"],"description":"Leave empty. This model forecasts from the end of its fitted history"},{"name":"price","role":"input","dims":["store","sku","time"],"description":"An input"},{"name":"promo","role":"input","dims":["store","sku","time"],"description":"An input"}],"rows":[["2024-10-13","k0","s0",null,null,null],["2024-10-13","k0","s1",null,null,null],["2024-10-13","k1","s0",null,null,null],["2024-10-13","k1","s1",null,null,null],["2024-10-20","k0","s0",null,null,null],["2024-10-20","k0","s1",null,null,null],["2024-10-20","k1","s0",null,null,null],["2024-10-20","k1","s1",null,null,null],["2024-10-27","k0","s0",null,null,null],["2024-10-27","k0","s1",null,null,null],["2024-10-27","k1","s0",null,null,null],["2024-10-27","k1","s1",null,null,null],["2024-11-03","k0","s0",null,null,null],["2024-11-03","k0","s1",null,null,null],["2024-11-03","k1","s0",null,null,null],["2024-11-03","k1","s1",null,null,null],["2024-11-10","k0","s0",null,null,null],["2024-11-10","k0","s1",null,null,null],["2024-11-10","k1","s0",null,null,null],["2024-11-10","k1","s1",null,null,null],["2024-11-17","k0","s0",null,null,null],["2024-11-17","k0","s1",null,null,null],["2024-11-17","k1","s0",null,null,null],["2024-11-17","k1","s1",null,null,null],["2024-11-24","k0","s0",null,null,null],["2024-11-24","k0","s1",null,null,null],["2024-11-24","k1","s0",null,null,null],["2024-11-24","k1","s1",null,null,null],["2024-12-01","k0","s0",null,null,null],["2024-12-01","k0","s1",null,null,null],["2024-12-01","k1","s0",null,null,null],["2024-12-01","k1","s1",null,null,null]]},"kpi_column":"y","example_csv":"store,time,price,promo,y\n","max_rows":5000,"match":"row","id_column":"store"},
    batch_template: {"columns":[{"name":"store","role":"id","type":"text","required":true,"choices":[]},{"name":"time","role":"driver","type":"date","required":true,"choices":[]},{"name":"price","role":"driver","type":"number","required":true,"min":5.0,"max":40.0,"choices":[]},{"name":"promo","role":"driver","type":"integer","required":true,"min":0.0,"max":1.0,"choices":[]}],"id_column":"store","example_csv":"store,time,price,promo\ncase1,2024-10-13,18,0\ncase2,2024-10-13,18,0\n","max_rows":2000},
    batch: {"lead":"The model gives a 55% chance of a positive result for this case.","tiles":[{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false},{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b003"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"classifier","panels":[{"panel_id":"chance","title":"Chance of high demand","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance of high demand","value":0.546891,"unit":"probability","delta_dir":"flat","caption":"For store s0, sku k0, week 2024-09-22","panel_id":"chance","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"chances","title":"Chance of high demand per case","caption":"The first 20 of 45.","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"s0 \u00b7 k0 \u00b7 2024-09-22","value":0.546891},{"label":"s0 \u00b7 k0 \u00b7 2024-09-29","value":0.162309},{"label":"s0 \u00b7 k0 \u00b7 2024-10-06","value":0.153694},{"label":"s0 \u00b7 k1 \u00b7 2024-09-22","value":0.462807},{"label":"s0 \u00b7 k1 \u00b7 2024-09-29","value":0.481023},{"label":"s0 \u00b7 k1 \u00b7 2024-10-06","value":0.48242},{"label":"s0 \u00b7 k2 \u00b7 2024-09-22","value":0.712665},{"label":"s0 \u00b7 k2 \u00b7 2024-09-29","value":0.786136},{"label":"s0 \u00b7 k2 \u00b7 2024-10-06","value":0.710518},{"label":"s1 \u00b7 k0 \u00b7 2024-09-22","value":0.120593},{"label":"s1 \u00b7 k0 \u00b7 2024-09-29","value":0.154293},{"label":"s1 \u00b7 k0 \u00b7 2024-10-06","value":0.135382},{"label":"s1 \u00b7 k1 \u00b7 2024-09-22","value":0.290999},{"label":"s1 \u00b7 k1 \u00b7 2024-09-29","value":0.773457},{"label":"s1 \u00b7 k1 \u00b7 2024-10-06","value":0.188255},{"label":"s1 \u00b7 k2 \u00b7 2024-09-22","value":0.62443},{"label":"s1 \u00b7 k2 \u00b7 2024-09-29","value":0.652529},{"label":"s1 \u00b7 k2 \u00b7 2024-10-06","value":0.548465},{"label":"s2 \u00b7 k0 \u00b7 2024-09-22","value":0.381929},{"label":"s2 \u00b7 k0 \u00b7 2024-09-29","value":0.39909}]},{"panel_id":"calibration","title":"Predicted and observed chances","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"confusion","title":"Right and wrong results","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"accuracy","title":"How often the model was right","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"batch":{"n_rows":45,"n_scored":45,"outputs":["y_hat"],"stats":{"y_hat":{"mean":0.472248,"min":0.0431646,"p10":0.137902,"p50":0.480575,"p90":0.893271,"max":0.9858}},"share_positive":0.422222,"class_counts":[],"histogram":[{"lo":0.0431646,"hi":0.0902964,"count":1},{"lo":0.0902964,"hi":0.137428,"count":4},{"lo":0.137428,"hi":0.18456,"count":6},{"lo":0.18456,"hi":0.231692,"count":2},{"lo":0.231692,"hi":0.278824,"count":1},{"lo":0.278824,"hi":0.325955,"count":1},{"lo":0.325955,"hi":0.373087,"count":0},{"lo":0.373087,"hi":0.420219,"count":5},{"lo":0.420219,"hi":0.467351,"count":2},{"lo":0.467351,"hi":0.514482,"count":5},{"lo":0.514482,"hi":0.561614,"count":3},{"lo":0.561614,"hi":0.608746,"count":1},{"lo":0.608746,"hi":0.655878,"count":2},{"lo":0.655878,"hi":0.70301,"count":1},{"lo":0.70301,"hi":0.750141,"count":2},{"lo":0.750141,"hi":0.797273,"count":2},{"lo":0.797273,"hi":0.844405,"count":1},{"lo":0.844405,"hi":0.891537,"count":0},{"lo":0.891537,"hi":0.938669,"count":4},{"lo":0.938669,"hi":0.9858,"count":2}],"preview":[{"id":"s0 \u00b7 k0 \u00b7 2024-09-22","y_hat":0.546891},{"id":"s0 \u00b7 k0 \u00b7 2024-09-29","y_hat":0.162309},{"id":"s0 \u00b7 k0 \u00b7 2024-10-06","y_hat":0.153694},{"id":"s0 \u00b7 k1 \u00b7 2024-09-22","y_hat":0.462807},{"id":"s0 \u00b7 k1 \u00b7 2024-09-29","y_hat":0.481023},{"id":"s0 \u00b7 k1 \u00b7 2024-10-06","y_hat":0.48242},{"id":"s0 \u00b7 k2 \u00b7 2024-09-22","y_hat":0.712665},{"id":"s0 \u00b7 k2 \u00b7 2024-09-29","y_hat":0.786136},{"id":"s0 \u00b7 k2 \u00b7 2024-10-06","y_hat":0.710518},{"id":"s1 \u00b7 k0 \u00b7 2024-09-22","y_hat":0.120593},{"id":"s1 \u00b7 k0 \u00b7 2024-09-29","y_hat":0.154293},{"id":"s1 \u00b7 k0 \u00b7 2024-10-06","y_hat":0.135382},{"id":"s1 \u00b7 k1 \u00b7 2024-09-22","y_hat":0.290999},{"id":"s1 \u00b7 k1 \u00b7 2024-09-29","y_hat":0.773457},{"id":"s1 \u00b7 k1 \u00b7 2024-10-06","y_hat":0.188255},{"id":"s1 \u00b7 k2 \u00b7 2024-09-22","y_hat":0.62443},{"id":"s1 \u00b7 k2 \u00b7 2024-09-29","y_hat":0.652529},{"id":"s1 \u00b7 k2 \u00b7 2024-10-06","y_hat":0.548465},{"id":"s2 \u00b7 k0 \u00b7 2024-09-22","y_hat":0.381929},{"id":"s2 \u00b7 k0 \u00b7 2024-09-29","y_hat":0.39909}],"download":{"available":true,"rows":45,"bytes_estimate":1890}},"accuracy_history":[],"inputs_echo":[]},
    prediction: {"lead":"The model gives a 15% chance of a positive result for this case.","tiles":[{"key":"panel","label":"Chance of high demand","value":0.153694,"unit":"probability","delta_dir":"flat","caption":"For sku k0","panel_id":"chance","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false},{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b004"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"classifier","panels":[{"panel_id":"chance","title":"Chance of high demand","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance of high demand","value":0.153694,"unit":"probability","delta_dir":"flat","caption":"For sku k0","panel_id":"chance","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.867347,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.566667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"chances","title":"Chance of high demand per case","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"k0","value":0.153694},{"label":"k1","value":0.48242}]},{"panel_id":"calibration","title":"Predicted and observed chances","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"confusion","title":"Right and wrong results","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."},{"panel_id":"accuracy","title":"How often the model was right","approximate":false,"kind":"unavailable","reason":"This shows after you send actual results."}],"prediction":{"rows":[{"inputs":{"price":18.0,"promo":0.0,"store":"s0","week":"2024-10-13"},"outputs":{"y_hat":{"centre":0.153694}}},{"inputs":{"price":24.0,"promo":0.0,"store":"s0","week":"2024-10-13"},"outputs":{"y_hat":{"centre":0.48242}}}],"band":"none","approximate":false},"accuracy_history":[],"inputs_echo":[{"price":18.0,"promo":0.0,"store":"s0","week":"2024-10-13"},{"price":24.0,"promo":0.0,"store":"s0","week":"2024-10-13"}]},
  },
  causal: {
    spec: {"layout_version":2,"preset":"causal_impact","title":"Effect of the loyalty launch","client_name":"Harbour Retail","summary":"How many more orders the loyalty programme brought since its launch, compared with the orders the model expects without it.","period_label":"week","source":{"kind":"endpoint","alias":"loyalty-launch"},"subject":{"variable":"y_hat","label":"Orders","unit":"count","decimals":0},"panels":[{"id":"m_effect_total","title":"Total effect of the change","kind":"kpi","source":{"kind":"metric","key":"effect_total","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"m_effect_relative","title":"Effect in percent","kind":"kpi","source":{"kind":"metric","key":"effect_relative","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"m_p_effect_positive","title":"Chance that the effect is positive","kind":"kpi","source":{"kind":"metric","key":"p_effect_positive","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"m_pre_mape","title":"Fit before the change (error in percent)","kind":"kpi","source":{"kind":"metric","key":"pre_mape","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"down"},{"id":"effect","title":"Effect of the change over time","kind":"chart","intent":"effect","source":{"kind":"output","variable":"effect","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"outcome","title":"Orders over time","kind":"chart","intent":"fan","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20}],"scenario_rows":{"enabled":false,"max_rows":5,"outputs":[],"inputs":[],"fixed":{}},"actuals":{"enabled":true,"publish":"review","allow_refit_requests":false,"match":"time","threshold":0.5},"refresh":{"follow_endpoint":false},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    catalog: {"outputs":[{"name":"effect","manifest_name":"effect","dims":["week"],"time_dim":"week","shape":"time","representation":"draws","meaning":"effect","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter","effect","diverging"],"predictive_ok":true},{"name":"y_hat","manifest_name":"y_hat","dims":["week"],"time_dim":"week","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true}],"metrics":[{"key":"divergences","labels":{"chain":"0"},"last":0.0},{"key":"divergences","labels":{"chain":"1"},"last":1.0},{"key":"effect_average","labels":{},"last":9.11831},{"key":"effect_relative","labels":{},"last":0.0893982},{"key":"effect_total","labels":{},"last":109.42},{"key":"effect_total_p05","labels":{},"last":92.2026},{"key":"effect_total_p95","labels":{},"last":133.248},{"key":"effect_total_sd","labels":{},"last":12.6896},{"key":"num_steps","labels":{"chain":"0"},"last":7.0},{"key":"num_steps","labels":{"chain":"1"},"last":3.0},{"key":"one_step_coverage_90","labels":{},"last":0.942308},{"key":"one_step_rmse","labels":{},"last":1.28336},{"key":"p_effect_positive","labels":{},"last":1.0},{"key":"pre_mape","labels":{},"last":0.0111603},{"key":"pre_rmse","labels":{},"last":1.30074},{"key":"sigma_level","labels":{},"last":0.246994},{"key":"sigma_obs","labels":{},"last":1.23843},{"key":"step_size","labels":{"chain":"0"},"last":0.527599},{"key":"step_size","labels":{"chain":"1"},"last":0.539478}],"scorecard":["convergence.rhat","convergence.ess","convergence.divergences","data.missing","data.history"],"pictures":[],"inputs":[{"var":"control_a","kind":"number","dtype":"float64","levels":[]},{"var":"control_b","kind":"number","dtype":"float64","levels":[]},{"dim":"time","kind":"date","levels":[],"date_min":"2023-01-02","date_max":"2024-03-18","policy":"extend"}],"score_modes":["forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","time_dim":"time","has_grid":false,"views":["forecast"],"family":"causal","namespace":"causal"},
    issues: [],
    suggestions: [{"preset":"causal_impact","reasons":["The model estimates the effect of a change (a causal model)."],"blocked_inputs":[]},{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":["batch: the model has no row dim"]}],
    baseline: {"lead":"The change added about 109 orders (likely 92 to 133), 8.9% more than without it.","tiles":[{"key":"panel","label":"Total effect of the change","value":109.42,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_total","approximate":false},{"key":"panel","label":"Effect in percent","value":8.93982,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_relative","approximate":false},{"key":"panel","label":"Chance that the effect is positive","value":1.0,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_p_effect_positive","approximate":false},{"key":"panel","label":"Fit before the change (error in percent)","value":1.11603,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_pre_mape","approximate":false}],"channels":[],"series":[{"name":"y_hat","label":"Orders","points":[{"t":"2023-01-02","mean":96.9369,"low":83.1763,"high":108.487},{"t":"2023-01-09","mean":96.8769,"low":94.4209,"high":100.397},{"t":"2023-01-16","mean":97.95,"low":95.2801,"high":101.064},{"t":"2023-01-23","mean":98.3392,"low":95.1502,"high":100.375},{"t":"2023-01-30","mean":98.7147,"low":96.2248,"high":101.282},{"t":"2023-02-06","mean":98.8785,"low":96.8585,"high":101.069},{"t":"2023-02-13","mean":101.106,"low":98.9573,"high":103.134},{"t":"2023-02-20","mean":101.284,"low":98.8421,"high":103.606},{"t":"2023-02-27","mean":101.352,"low":99.5304,"high":103.847},{"t":"2023-03-06","mean":100.473,"low":98.5709,"high":102.853},{"t":"2023-03-13","mean":100.305,"low":98.1548,"high":102.567},{"t":"2023-03-20","mean":100.829,"low":98.7266,"high":103.017},{"t":"2023-03-27","mean":99.3125,"low":96.9498,"high":101.159},{"t":"2023-04-03","mean":98.5223,"low":96.077,"high":100.584},{"t":"2023-04-10","mean":96.8747,"low":94.1541,"high":98.8545},{"t":"2023-04-17","mean":95.3552,"low":92.8343,"high":97.371},{"t":"2023-04-24","mean":94.8939,"low":92.7276,"high":96.9857},{"t":"2023-05-01","mean":94.2354,"low":91.9747,"high":97.0272},{"t":"2023-05-08","mean":93.6597,"low":91.5459,"high":95.9508},{"t":"2023-05-15","mean":94.1021,"low":91.6283,"high":96.3295},{"t":"2023-05-22","mean":93.41,"low":91.3369,"high":95.5815},{"t":"2023-05-29","mean":94.0399,"low":92.0656,"high":96.6508},{"t":"2023-06-05","mean":93.0358,"low":90.4014,"high":94.8366},{"t":"2023-06-12","mean":93.2385,"low":90.9783,"high":95.6175},{"t":"2023-06-19","mean":92.8068,"low":90.0204,"high":94.5714},{"t":"2023-06-26","mean":91.5609,"low":89.25,"high":93.3144},{"t":"2023-07-03","mean":90.3057,"low":88.2859,"high":92.7106},{"t":"2023-07-10","mean":89.6198,"low":87.689,"high":92.383},{"t":"2023-07-17","mean":88.5314,"low":86.2592,"high":90.3481},{"t":"2023-07-24","mean":88.349,"low":86.3489,"high":90.5356},{"t":"2023-07-31","mean":86.899,"low":84.138,"high":89.0233},{"t":"2023-08-07","mean":85.7181,"low":83.4663,"high":88.1325},{"t":"2023-08-14","mean":85.385,"low":83.4805,"high":87.6498},{"t":"2023-08-21","mean":85.7122,"low":83.5093,"high":88.0042},{"t":"2023-08-28","mean":85.6708,"low":83.1922,"high":88.4371},{"t":"2023-09-04","mean":85.2404,"low":83.1462,"high":88.1043},{"t":"2023-09-11","mean":85.0777,"low":83.1057,"high":87.6312},{"t":"2023-09-18","mean":85.1982,"low":82.7131,"high":87.0582},{"t":"2023-09-25","mean":85.7844,"low":83.291,"high":87.9251},{"t":"2023-10-02","mean":87.4115,"low":85.3781,"high":89.4827},{"t":"2023-10-09","mean":86.0613,"low":83.7132,"high":88.5279},{"t":"2023-10-16","mean":87.9904,"low":85.3938,"high":90.2096},{"t":"2023-10-23","mean":88.9779,"low":86.5147,"high":91.1116},{"t":"2023-10-30","mean":89.9611,"low":87.0938,"high":91.7244},{"t":"2023-11-06","mean":91.1453,"low":88.573,"high":93.3966},{"t":"2023-11-13","mean":91.4001,"low":89.1977,"high":93.346},{"t":"2023-11-20","mean":92.9541,"low":90.8274,"high":95.5259},{"t":"2023-11-27","mean":94.6557,"low":92.6631,"high":96.9504},{"t":"2023-12-04","mean":96.7515,"low":94.7195,"high":99.1951},{"t":"2023-12-11","mean":98.9646,"low":96.683,"high":101.143},{"t":"2023-12-18","mean":99.7759,"low":97.3021,"high":102.298},{"t":"2023-12-25","mean":100.042,"low":97.9715,"high":102.609},{"t":"2024-01-01","mean":101.255,"low":99.1808,"high":103.485},{"t":"2024-01-08","mean":102.498,"low":100.04,"high":104.826},{"t":"2024-01-15","mean":102.273,"low":100.073,"high":104.571},{"t":"2024-01-22","mean":102.481,"low":100.289,"high":105.077},{"t":"2024-01-29","mean":103.074,"low":100.183,"high":105.291},{"t":"2024-02-05","mean":103.497,"low":100.929,"high":106.379},{"t":"2024-02-12","mean":102.428,"low":99.786,"high":104.818},{"t":"2024-02-19","mean":102.335,"low":99.4405,"high":105.039},{"t":"2024-02-26","mean":101.933,"low":98.5364,"high":104.87},{"t":"2024-03-04","mean":101.071,"low":98.1812,"high":103.856},{"t":"2024-03-11","mean":101.5,"low":98.6976,"high":105.184},{"t":"2024-03-18","mean":101.209,"low":97.7162,"high":104.55}]}],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/loyalty-launch@v3"},"schema_version":2,"preset":"causal_impact","panels":[{"panel_id":"m_effect_total","title":"Total effect of the change","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Total effect of the change","value":109.42,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_total","approximate":false}},{"panel_id":"m_effect_relative","title":"Effect in percent","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Effect in percent","value":8.93982,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_relative","approximate":false}},{"panel_id":"m_p_effect_positive","title":"Chance that the effect is positive","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance that the effect is positive","value":1.0,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_p_effect_positive","approximate":false}},{"panel_id":"m_pre_mape","title":"Fit before the change (error in percent)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Fit before the change (error in percent)","value":1.11603,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_pre_mape","approximate":false}},{"panel_id":"effect","title":"Effect of the change over time","approximate":false,"kind":"series","intent":"effect","series":[{"name":"effect","label":"Effect of the change over time","points":[{"t":"2023-01-02","mean":0.757688,"low":-10.7924,"high":14.5183},{"t":"2023-01-09","mean":-0.493495,"low":-4.01367,"high":1.96257},{"t":"2023-01-16","mean":-0.34073,"low":-3.45479,"high":2.32921},{"t":"2023-01-23","mean":-1.23087,"low":-3.26643,"high":1.95812},{"t":"2023-01-30","mean":-0.303824,"low":-2.87146,"high":2.18611},{"t":"2023-02-06","mean":1.83151,"low":-0.358592,"high":3.85154},{"t":"2023-02-13","mean":0.0793966,"low":-1.94797,"high":2.22847},{"t":"2023-02-20","mean":2.50293,"low":0.1802,"high":4.94445},{"t":"2023-02-27","mean":-1.10288,"low":-3.59846,"high":0.718321},{"t":"2023-03-06","mean":0.695967,"low":-1.68432,"high":2.5979},{"t":"2023-03-13","mean":-0.569505,"low":-2.83097,"high":1.58094},{"t":"2023-03-20","mean":0.223627,"low":-1.96484,"high":2.32585},{"t":"2023-03-27","mean":-0.460895,"low":-2.30693,"high":1.90184},{"t":"2023-04-03","mean":-0.645297,"low":-2.70658,"high":1.8},{"t":"2023-04-10","mean":-0.537117,"low":-2.51688,"high":2.18346},{"t":"2023-04-17","mean":3.75071,"low":1.73498,"high":6.27167},{"t":"2023-04-24","mean":-0.245738,"low":-2.33762,"high":1.92054},{"t":"2023-05-01","mean":-2.09862,"low":-4.89035,"high":0.162089},{"t":"2023-05-08","mean":-0.279405,"low":-2.5705,"high":1.83446},{"t":"2023-05-15","mean":0.825722,"low":-1.40168,"high":3.29948},{"t":"2023-05-22","mean":-0.561586,"low":-2.73317,"high":1.51145},{"t":"2023-05-29","mean":1.36292,"low":-1.24802,"high":3.33723},{"t":"2023-06-05","mean":0.676212,"low":-1.12459,"high":3.31062},{"t":"2023-06-12","mean":-0.993514,"low":-3.37251,"high":1.26666},{"t":"2023-06-19","mean":-1.06247,"low":-2.8271,"high":1.72389},{"t":"2023-06-26","mean":-1.21839,"low":-2.97194,"high":1.09243},{"t":"2023-07-03","mean":-0.72476,"low":-3.12963,"high":1.29508},{"t":"2023-07-10","mean":-1.6571,"low":-4.42032,"high":0.273702},{"t":"2023-07-17","mean":1.6139,"low":-0.202764,"high":3.88611},{"t":"2023-07-24","mean":1.626,"low":-0.560652,"high":3.62604},{"t":"2023-07-31","mean":-1.2292,"low":-3.35356,"high":1.53181},{"t":"2023-08-07","mean":-1.14354,"low":-3.55794,"high":1.10834},{"t":"2023-08-14","mean":-1.38819,"low":-3.65294,"high":0.516383},{"t":"2023-08-21","mean":-0.361594,"low":-2.65366,"high":1.84123},{"t":"2023-08-28","mean":-2.37926,"low":-5.14556,"high":0.0993658},{"t":"2023-09-04","mean":-0.181888,"low":-3.04582,"high":1.91231},{"t":"2023-09-11","mean":2.4511,"low":-0.102336,"high":4.42317},{"t":"2023-09-18","mean":0.342847,"low":-1.51715,"high":2.82796},{"t":"2023-09-25","mean":1.45663,"low":-0.684046,"high":3.95005},{"t":"2023-10-02","mean":-0.200072,"low":-2.27125,"high":1.8334},{"t":"2023-10-09","mean":2.01816,"low":-0.448423,"high":4.36634},{"t":"2023-10-16","mean":0.0973475,"low":-2.12184,"high":2.69397},{"t":"2023-10-23","mean":-0.693945,"low":-2.82766,"high":1.76925},{"t":"2023-10-30","mean":2.22763,"low":0.464303,"high":5.09493},{"t":"2023-11-06","mean":-1.17792,"low":-3.42929,"high":1.39432},{"t":"2023-11-13","mean":-2.0168,"low":-3.96271,"high":0.185629},{"t":"2023-11-20","mean":-0.333285,"low":-2.90509,"high":1.79332},{"t":"2023-11-27","mean":-0.723347,"low":-3.01807,"high":1.26922},{"t":"2023-12-04","mean":0.419049,"low":-2.02448,"high":2.45113},{"t":"2023-12-11","mean":-2.01516,"low":-4.19336,"high":0.266425},{"t":"2023-12-18","mean":-0.0248974,"low":-2.54708,"high":2.4489},{"t":"2023-12-25","mean":0.260892,"low":-2.30619,"high":2.33112},{"t":"2024-01-01","mean":8.42323,"low":6.1935,"high":10.4975},{"t":"2024-01-08","mean":9.39581,"low":7.06748,"high":11.8539},{"t":"2024-01-15","mean":7.9864,"low":5.68775,"high":10.1854},{"t":"2024-01-22","mean":11.3637,"low":8.768,"high":13.5555},{"t":"2024-01-29","mean":8.31751,"low":6.10015,"high":11.2079},{"t":"2024-02-05","mean":8.76728,"low":5.88458,"high":11.335},{"t":"2024-02-12","mean":8.17309,"low":5.78364,"high":10.8155},{"t":"2024-02-19","mean":9.24407,"low":6.53946,"high":12.1383},{"t":"2024-02-26","mean":9.49644,"low":6.559,"high":12.8927},{"t":"2024-03-04","mean":10.0671,"low":7.28186,"high":12.9566},{"t":"2024-03-11","mean":8.82502,"low":5.14176,"high":11.6278},{"t":"2024-03-18","mean":9.36005,"low":6.01889,"high":12.8528}]}],"x_kind":"time"},{"panel_id":"outcome","title":"Orders over time","approximate":false,"kind":"series","intent":"fan","series":[{"name":"y_hat","label":"Orders over time","points":[{"t":"2023-01-02","mean":96.9369,"low":83.1763,"high":108.487},{"t":"2023-01-09","mean":96.8769,"low":94.4209,"high":100.397},{"t":"2023-01-16","mean":97.95,"low":95.2801,"high":101.064},{"t":"2023-01-23","mean":98.3392,"low":95.1502,"high":100.375},{"t":"2023-01-30","mean":98.7147,"low":96.2248,"high":101.282},{"t":"2023-02-06","mean":98.8785,"low":96.8585,"high":101.069},{"t":"2023-02-13","mean":101.106,"low":98.9573,"high":103.134},{"t":"2023-02-20","mean":101.284,"low":98.8421,"high":103.606},{"t":"2023-02-27","mean":101.352,"low":99.5304,"high":103.847},{"t":"2023-03-06","mean":100.473,"low":98.5709,"high":102.853},{"t":"2023-03-13","mean":100.305,"low":98.1548,"high":102.567},{"t":"2023-03-20","mean":100.829,"low":98.7266,"high":103.017},{"t":"2023-03-27","mean":99.3125,"low":96.9498,"high":101.159},{"t":"2023-04-03","mean":98.5223,"low":96.077,"high":100.584},{"t":"2023-04-10","mean":96.8747,"low":94.1541,"high":98.8545},{"t":"2023-04-17","mean":95.3552,"low":92.8343,"high":97.371},{"t":"2023-04-24","mean":94.8939,"low":92.7276,"high":96.9857},{"t":"2023-05-01","mean":94.2354,"low":91.9747,"high":97.0272},{"t":"2023-05-08","mean":93.6597,"low":91.5459,"high":95.9508},{"t":"2023-05-15","mean":94.1021,"low":91.6283,"high":96.3295},{"t":"2023-05-22","mean":93.41,"low":91.3369,"high":95.5815},{"t":"2023-05-29","mean":94.0399,"low":92.0656,"high":96.6508},{"t":"2023-06-05","mean":93.0358,"low":90.4014,"high":94.8366},{"t":"2023-06-12","mean":93.2385,"low":90.9783,"high":95.6175},{"t":"2023-06-19","mean":92.8068,"low":90.0204,"high":94.5714},{"t":"2023-06-26","mean":91.5609,"low":89.25,"high":93.3144},{"t":"2023-07-03","mean":90.3057,"low":88.2859,"high":92.7106},{"t":"2023-07-10","mean":89.6198,"low":87.689,"high":92.383},{"t":"2023-07-17","mean":88.5314,"low":86.2592,"high":90.3481},{"t":"2023-07-24","mean":88.349,"low":86.3489,"high":90.5356},{"t":"2023-07-31","mean":86.899,"low":84.138,"high":89.0233},{"t":"2023-08-07","mean":85.7181,"low":83.4663,"high":88.1325},{"t":"2023-08-14","mean":85.385,"low":83.4805,"high":87.6498},{"t":"2023-08-21","mean":85.7122,"low":83.5093,"high":88.0042},{"t":"2023-08-28","mean":85.6708,"low":83.1922,"high":88.4371},{"t":"2023-09-04","mean":85.2404,"low":83.1462,"high":88.1043},{"t":"2023-09-11","mean":85.0777,"low":83.1057,"high":87.6312},{"t":"2023-09-18","mean":85.1982,"low":82.7131,"high":87.0582},{"t":"2023-09-25","mean":85.7844,"low":83.291,"high":87.9251},{"t":"2023-10-02","mean":87.4115,"low":85.3781,"high":89.4827},{"t":"2023-10-09","mean":86.0613,"low":83.7132,"high":88.5279},{"t":"2023-10-16","mean":87.9904,"low":85.3938,"high":90.2096},{"t":"2023-10-23","mean":88.9779,"low":86.5147,"high":91.1116},{"t":"2023-10-30","mean":89.9611,"low":87.0938,"high":91.7244},{"t":"2023-11-06","mean":91.1453,"low":88.573,"high":93.3966},{"t":"2023-11-13","mean":91.4001,"low":89.1977,"high":93.346},{"t":"2023-11-20","mean":92.9541,"low":90.8274,"high":95.5259},{"t":"2023-11-27","mean":94.6557,"low":92.6631,"high":96.9504},{"t":"2023-12-04","mean":96.7515,"low":94.7195,"high":99.1951},{"t":"2023-12-11","mean":98.9646,"low":96.683,"high":101.143},{"t":"2023-12-18","mean":99.7759,"low":97.3021,"high":102.298},{"t":"2023-12-25","mean":100.042,"low":97.9715,"high":102.609},{"t":"2024-01-01","mean":101.255,"low":99.1808,"high":103.485},{"t":"2024-01-08","mean":102.498,"low":100.04,"high":104.826},{"t":"2024-01-15","mean":102.273,"low":100.073,"high":104.571},{"t":"2024-01-22","mean":102.481,"low":100.289,"high":105.077},{"t":"2024-01-29","mean":103.074,"low":100.183,"high":105.291},{"t":"2024-02-05","mean":103.497,"low":100.929,"high":106.379},{"t":"2024-02-12","mean":102.428,"low":99.786,"high":104.818},{"t":"2024-02-19","mean":102.335,"low":99.4405,"high":105.039},{"t":"2024-02-26","mean":101.933,"low":98.5364,"high":104.87},{"t":"2024-03-04","mean":101.071,"low":98.1812,"high":103.856},{"t":"2024-03-11","mean":101.5,"low":98.6976,"high":105.184},{"t":"2024-03-18","mean":101.209,"low":97.7162,"high":104.55}]}],"x_kind":"time"}],"accuracy_history":[],"inputs_echo":[]},
    actuals: {"lead":"The change added about 10 orders (likely 0 to 23), 2.4% more than without it.","tiles":[{"key":"panel","label":"Total effect of the change","value":109.42,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_total","approximate":false},{"key":"panel","label":"Effect in percent","value":8.93982,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_relative","approximate":false},{"key":"panel","label":"Chance that the effect is positive","value":1.0,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_p_effect_positive","approximate":false},{"key":"panel","label":"Fit before the change (error in percent)","value":1.11603,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_pre_mape","approximate":false}],"channels":[],"series":[{"name":"y_hat","label":"Orders","points":[{"t":"2023-01-02","actual":97.6946},{"t":"2023-01-09","actual":96.3834},{"t":"2023-01-16","actual":97.6093},{"t":"2023-01-23","actual":97.1084},{"t":"2023-01-30","actual":98.4109},{"t":"2023-02-06","actual":100.71},{"t":"2023-02-13","actual":101.186},{"t":"2023-02-20","actual":103.787},{"t":"2023-02-27","actual":100.249},{"t":"2023-03-06","actual":101.169},{"t":"2023-03-13","actual":99.7357},{"t":"2023-03-20","actual":101.052},{"t":"2023-03-27","actual":98.8516},{"t":"2023-04-03","actual":97.877},{"t":"2023-04-10","actual":96.3376},{"t":"2023-04-17","actual":99.1059},{"t":"2023-04-24","actual":94.6481},{"t":"2023-05-01","actual":92.1368},{"t":"2023-05-08","actual":93.3803},{"t":"2023-05-15","actual":94.9278},{"t":"2023-05-22","actual":92.8484},{"t":"2023-05-29","actual":95.4028},{"t":"2023-06-05","actual":93.7121},{"t":"2023-06-12","actual":92.2449},{"t":"2023-06-19","actual":91.7443},{"t":"2023-06-26","actual":90.3425},{"t":"2023-07-03","actual":89.581},{"t":"2023-07-10","actual":87.9627},{"t":"2023-07-17","actual":90.1453},{"t":"2023-07-24","actual":89.975},{"t":"2023-07-31","actual":85.6698},{"t":"2023-08-07","actual":84.5746},{"t":"2023-08-14","actual":83.9968},{"t":"2023-08-21","actual":85.3506},{"t":"2023-08-28","actual":83.2915},{"t":"2023-09-04","actual":85.0585},{"t":"2023-09-11","actual":87.5288},{"t":"2023-09-18","actual":85.5411},{"t":"2023-09-25","actual":87.241},{"t":"2023-10-02","actual":87.2115},{"t":"2023-10-09","actual":88.0795},{"t":"2023-10-16","actual":88.0878},{"t":"2023-10-23","actual":88.2839},{"t":"2023-10-30","actual":92.1887},{"t":"2023-11-06","actual":89.9673},{"t":"2023-11-13","actual":89.3833},{"t":"2023-11-20","actual":92.6208},{"t":"2023-11-27","actual":93.9323},{"t":"2023-12-04","actual":97.1706},{"t":"2023-12-11","actual":96.9494},{"t":"2023-12-18","actual":99.751},{"t":"2023-12-25","actual":100.303},{"t":"2024-01-01","actual":109.678},{"t":"2024-01-08","actual":111.894},{"t":"2024-01-15","actual":110.259},{"t":"2024-01-22","actual":113.845},{"t":"2024-01-29","actual":111.391},{"t":"2024-02-05","actual":112.264},{"t":"2024-02-12","actual":110.602},{"t":"2024-02-19","actual":111.579},{"t":"2024-02-26","actual":111.429},{"t":"2024-03-04","actual":111.138},{"t":"2024-03-11","actual":110.325},{"t":"2024-03-18","actual":110.569},{"t":"2024-03-25","mean":101.246,"low":98.5059,"high":104.914,"actual":102.644},{"t":"2024-04-01","mean":101.268,"low":98.0939,"high":104.805,"actual":104.939},{"t":"2024-04-08","mean":101.8,"low":97.573,"high":104.343,"actual":109.2},{"t":"2024-04-15","mean":102.576,"low":99.532,"high":106.555,"actual":99.958}]}],"conditioning":{"dim":"time","fit_end":"2024-03-18","conditioned":false,"n_actuals":0,"revised":0,"series_conditioned":0,"n_series":1,"horizon_source":"rows","periods":4,"parameters":"fitted","series":[{"series":{},"n_actuals":0,"revised":0,"missing":0,"origin":"2024-03-18","forecast_from":"2024-03-25","forecast_to":"2024-04-15","n_forecast":4}],"truncated":false,"message":"Forecast from the end of the fitted history (2024-03-18); no actuals were supplied. 4 periods forecast from 2024-03-25 to 2024-04-15."},"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b002"],"input_fingerprint":"sha256:abababababababababababababababababababababababababababababababab","notes_plain":["Your numbers were compared with the model's predictions; the model does not change."],"technical":{},"schema_version":2,"preset":"causal_impact","panels":[{"panel_id":"m_effect_total","title":"Total effect of the change","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Total effect of the change","value":109.42,"unit":"count","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_total","approximate":false}},{"panel_id":"m_effect_relative","title":"Effect in percent","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Effect in percent","value":8.93982,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_effect_relative","approximate":false}},{"panel_id":"m_p_effect_positive","title":"Chance that the effect is positive","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Chance that the effect is positive","value":1.0,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_p_effect_positive","approximate":false}},{"panel_id":"m_pre_mape","title":"Fit before the change (error in percent)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Fit before the change (error in percent)","value":1.11603,"unit":"percent","delta_dir":"flat","caption":"From the model fit","panel_id":"m_pre_mape","approximate":false}},{"panel_id":"effect","title":"Effect of the change over time","approximate":false,"kind":"unavailable","reason":"This readout has no values of effect."},{"panel_id":"outcome","title":"Orders over time","approximate":false,"kind":"series","intent":"fan","series":[{"name":"y_hat","label":"Orders over time","points":[{"t":"2023-01-02","actual":97.6946},{"t":"2023-01-09","actual":96.3834},{"t":"2023-01-16","actual":97.6093},{"t":"2023-01-23","actual":97.1084},{"t":"2023-01-30","actual":98.4109},{"t":"2023-02-06","actual":100.71},{"t":"2023-02-13","actual":101.186},{"t":"2023-02-20","actual":103.787},{"t":"2023-02-27","actual":100.249},{"t":"2023-03-06","actual":101.169},{"t":"2023-03-13","actual":99.7357},{"t":"2023-03-20","actual":101.052},{"t":"2023-03-27","actual":98.8516},{"t":"2023-04-03","actual":97.877},{"t":"2023-04-10","actual":96.3376},{"t":"2023-04-17","actual":99.1059},{"t":"2023-04-24","actual":94.6481},{"t":"2023-05-01","actual":92.1368},{"t":"2023-05-08","actual":93.3803},{"t":"2023-05-15","actual":94.9278},{"t":"2023-05-22","actual":92.8484},{"t":"2023-05-29","actual":95.4028},{"t":"2023-06-05","actual":93.7121},{"t":"2023-06-12","actual":92.2449},{"t":"2023-06-19","actual":91.7443},{"t":"2023-06-26","actual":90.3425},{"t":"2023-07-03","actual":89.581},{"t":"2023-07-10","actual":87.9627},{"t":"2023-07-17","actual":90.1453},{"t":"2023-07-24","actual":89.975},{"t":"2023-07-31","actual":85.6698},{"t":"2023-08-07","actual":84.5746},{"t":"2023-08-14","actual":83.9968},{"t":"2023-08-21","actual":85.3506},{"t":"2023-08-28","actual":83.2915},{"t":"2023-09-04","actual":85.0585},{"t":"2023-09-11","actual":87.5288},{"t":"2023-09-18","actual":85.5411},{"t":"2023-09-25","actual":87.241},{"t":"2023-10-02","actual":87.2115},{"t":"2023-10-09","actual":88.0795},{"t":"2023-10-16","actual":88.0878},{"t":"2023-10-23","actual":88.2839},{"t":"2023-10-30","actual":92.1887},{"t":"2023-11-06","actual":89.9673},{"t":"2023-11-13","actual":89.3833},{"t":"2023-11-20","actual":92.6208},{"t":"2023-11-27","actual":93.9323},{"t":"2023-12-04","actual":97.1706},{"t":"2023-12-11","actual":96.9494},{"t":"2023-12-18","actual":99.751},{"t":"2023-12-25","actual":100.303},{"t":"2024-01-01","actual":109.678},{"t":"2024-01-08","actual":111.894},{"t":"2024-01-15","actual":110.259},{"t":"2024-01-22","actual":113.845},{"t":"2024-01-29","actual":111.391},{"t":"2024-02-05","actual":112.264},{"t":"2024-02-12","actual":110.602},{"t":"2024-02-19","actual":111.579},{"t":"2024-02-26","actual":111.429},{"t":"2024-03-04","actual":111.138},{"t":"2024-03-11","actual":110.325},{"t":"2024-03-18","actual":110.569},{"t":"2024-03-25","mean":101.246,"low":98.5059,"high":104.914,"actual":102.644},{"t":"2024-04-01","mean":101.268,"low":98.0939,"high":104.805,"actual":104.939},{"t":"2024-04-08","mean":101.8,"low":97.573,"high":104.343,"actual":109.2},{"t":"2024-04-15","mean":102.576,"low":99.532,"high":106.555,"actual":99.958}]}],"split_at":"2024-03-25","x_kind":"time"}],"accuracy_detail":{"kind":"effect","n_periods":4,"total":9.85047,"low":0.173153,"high":23.2306,"relative":2.42092,"note":"Your results minus the forecast without the change."},"accuracy_history":[{"seq":3,"created_at":"2026-08-03T09:30:00Z","n":4,"values":{"effect_total":11.6236,"effect_relative":2.85668,"n":4.0}},{"seq":5,"created_at":"2026-09-02T09:30:00Z","n":4,"values":{"effect_total":10.54,"effect_relative":2.59038,"n":4.0}},{"seq":7,"created_at":"2026-10-05T09:30:00Z","n":4,"values":{"effect_total":9.85047,"effect_relative":2.42092,"n":4.0}}],"inputs_echo":[]},
    actuals_template: {"mode":"compared","input_table":{"horizon_dim":"time","conditions_on_actuals":false,"columns":[{"name":"time","role":"coord","dims":[],"freq":"W-MON","description":"The period (yyyy-MM-dd)"},{"name":"y","role":"target","dims":["time"],"description":"Leave empty. This model forecasts from the end of its fitted history"},{"name":"control_a","role":"input","dims":["time"],"description":"An input"},{"name":"control_b","role":"input","dims":["time"],"description":"An input"}],"rows":[["2024-03-25",null,null,null],["2024-04-01",null,null,null],["2024-04-08",null,null,null],["2024-04-15",null,null,null],["2024-04-22",null,null,null],["2024-04-29",null,null,null],["2024-05-06",null,null,null],["2024-05-13",null,null,null]]},"kpi_column":"y","example_csv":"time,y,control_a,control_b\n2024-03-25,,,\n2024-04-01,,,\n2024-04-08,,,\n2024-04-15,,,\n2024-04-22,,,\n2024-04-29,,,\n2024-05-06,,,\n2024-05-13,,,\n","max_rows":520,"match":"time"},
  },
  field: {
    spec: {"layout_version":2,"preset":"field_map","title":"Incident map","client_name":"Harbour Retail","summary":"Where incidents are likely, per cell of the map and quarter. Darker cells expect more incidents.","period_label":"quarter","source":{"kind":"endpoint","alias":"incident-map"},"subject":{"variable":"rate","label":"Incidents","unit":"count","decimals":1},"panels":[{"id":"last_total","title":"Incidents in the last quarter","kind":"kpi","source":{"kind":"output","variable":"total","select":{}},"stat":"centre","reduce":{"how":"last","over":[]},"band":"outcome","compare":"previous","good":"down"},{"id":"map","title":"Incidents per cell","kind":"chart","intent":"field","source":{"kind":"output","variable":"rate","select":{}},"reduce":{"how":"none","over":[]},"band":"none","max_items":20},{"id":"over_time","title":"Incidents over time","kind":"chart","intent":"fan","source":{"kind":"output","variable":"total","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20}],"scenario_rows":{"enabled":false,"max_rows":5,"outputs":[],"inputs":[],"fixed":{}},"actuals":{"enabled":false,"publish":"review","allow_refit_requests":false,"match":"time","threshold":0.5},"refresh":{"follow_endpoint":false},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    catalog: {"outputs":[{"name":"rate","manifest_name":"rate","dims":["period","row","col"],"time_dim":"period","shape":"grid","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["field"],"predictive_ok":true},{"name":"total","manifest_name":"total","dims":["period"],"time_dim":"period","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true}],"metrics":[{"key":"coverage_90","labels":{},"last":0.983333},{"key":"divergences","labels":{"chain":"0"},"last":5.0},{"key":"divergences","labels":{"chain":"0","fit":"evaluation"},"last":2.0},{"key":"divergences","labels":{"chain":"1"},"last":7.0},{"key":"divergences","labels":{"chain":"1","fit":"evaluation"},"last":5.0},{"key":"mae","labels":{},"last":0.915792},{"key":"num_steps","labels":{"chain":"0"},"last":63.0},{"key":"num_steps","labels":{"chain":"0","fit":"evaluation"},"last":31.0},{"key":"num_steps","labels":{"chain":"1"},"last":63.0},{"key":"num_steps","labels":{"chain":"1","fit":"evaluation"},"last":63.0},{"key":"rmse","labels":{},"last":1.34209},{"key":"step_size","labels":{"chain":"0"},"last":0.146997},{"key":"step_size","labels":{"chain":"0","fit":"evaluation"},"last":0.172117},{"key":"step_size","labels":{"chain":"1"},"last":0.059418},{"key":"step_size","labels":{"chain":"1","fit":"evaluation"},"last":0.159873},{"key":"tree_depth","labels":{"chain":"0"},"last":6.0},{"key":"tree_depth","labels":{"chain":"0","fit":"evaluation"},"last":5.0},{"key":"tree_depth","labels":{"chain":"1"},"last":6.0},{"key":"tree_depth","labels":{"chain":"1","fit":"evaluation"},"last":6.0}],"scorecard":["convergence.rhat","convergence.ess","convergence.divergences","holdout.coverage_90","plausibility.prior_overlap","data.missing","data.history"],"pictures":[],"inputs":[{"dim":"col","kind":"choice","levels":["0","1","2","3","4"],"policy":"fixed"},{"dim":"period","kind":"date","levels":[],"date_min":"2009-01-01","date_max":"2012-10-01","policy":"extend"},{"dim":"row","kind":"choice","levels":["0","1","2","3","4","5"],"policy":"fixed"}],"score_modes":["evaluate","forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","row_dim":"row","row_policy":"fixed","time_dim":"period","has_grid":true,"views":["forecast","fields"],"family":"sci","namespace":"sci"},
    issues: [],
    suggestions: [{"preset":"field_map","reasons":["The model writes maps (a grid or areas)."],"blocked_inputs":[]},{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":["scenario_rows: the model's cases are fixed (no new cases)","batch: the model's cases are fixed (no new cases)"]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":["scenario_rows: the model's cases are fixed (no new cases)","batch: the model's cases are fixed (no new cases)"]}],
    baseline: {"lead":"The map shows the incidents that the model expects in each place.","tiles":[{"key":"panel","label":"Incidents in the last quarter","value":31.5032,"unit":"number","delta_dir":"flat","low":22.687,"high":38.9901,"caption":"Period 2012-10-01","panel_id":"last_total","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/incident-map@v3"},"schema_version":2,"preset":"field_map","panels":[{"panel_id":"last_total","title":"Incidents in the last quarter","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"Incidents in the last quarter","value":31.5032,"unit":"number","delta_dir":"flat","low":22.687,"high":38.9901,"caption":"Period 2012-10-01","panel_id":"last_total","approximate":false}},{"panel_id":"map","title":"Incidents per cell","approximate":false,"kind":"field","width":5,"height":6,"frames":[{"label":"2012-01-01","codes_b64":"PzsqYKAIRVuPsih7jXxVZtfYgDB+uu69VjRFkKVo"},{"label":"2012-04-01","codes_b64":"O1MqZ6ocRk+SvRONkYNsadzklh6FvPLJUUhOn7Z3"},{"label":"2012-07-01","codes_b64":"P0UAYaEeO1OGvx59koNtddXekRx9tvXJZUJPlaZ1"},{"label":"2012-10-01","codes_b64":"QTtJcasjSWSTwyiUlYhmbuDgnUSHyf/PZUZWmLyA"}],"vmin":-1.18709,"vmax":0.7352,"log":true,"colormap":"magma","x_label":"col","y_label":"row"},{"panel_id":"over_time","title":"Incidents over time","approximate":false,"kind":"series","intent":"fan","series":[{"name":"total","label":"Incidents over time","points":[{"t":"2012-01-01","mean":24.7348,"low":17.2024,"high":32.2302},{"t":"2012-04-01","mean":28.5384,"low":21.6441,"high":35.0286},{"t":"2012-07-01","mean":26.854,"low":17.6935,"high":33.7523},{"t":"2012-10-01","mean":31.5032,"low":22.687,"high":38.9901}]}],"x_kind":"time"}],"accuracy_history":[],"inputs_echo":[]},
  },
  image: {
    spec: {"layout_version":2,"preset":"image","title":"Shape check","client_name":"Harbour Retail","summary":"How well the model tells the shapes in the product photos apart, on images it did not see in training.","period_label":"case","source":{"kind":"endpoint","alias":"shape-check"},"subject":{"variable":"y_hat","label":"Chance","unit":"probability","decimals":2},"panels":[{"id":"m_accuracy","title":"How often the model is right","kind":"kpi","source":{"kind":"metric","key":"accuracy","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"m_auc","title":"How well the model ranks cases (AUC)","kind":"kpi","source":{"kind":"metric","key":"auc","labels":{},"run":"fit"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"none","good":"up"},{"id":"classes","title":"Chance of each shape","kind":"chart","intent":"probability_bars","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"mean","over":["image"]},"band":"outcome","max_items":20},{"id":"missed","title":"Images the model got wrong","caption":"Test images with the shape the model chose","kind":"recipe","recipe":"ml.misclassified","plot_id":"plt_01jaf3w1img2missed3x4y5z6"},{"id":"about","title":"About these numbers","kind":"note","text":"The model saw 30 product photos in training. The numbers on this page come from 12 photos it did not see."}],"scenario_rows":{"enabled":false,"max_rows":5,"outputs":[],"inputs":[],"fixed":{}},"actuals":{"enabled":false,"publish":"review","allow_refit_requests":false,"match":"time","threshold":0.5},"batch":{"enabled":true,"outputs":["y_hat"],"max_rows":200,"id_column":"image","enforce_ranges":false},"refresh":{"follow_endpoint":false},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    catalog: {"outputs":[{"name":"y_hat","manifest_name":"y_hat","dims":["class","image"],"shape":"classification","representation":"draws","meaning":"class_probability","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","mean","sum","top_class"],"intents":["probability_bars","bars","calibration","confusion","range"],"predictive_ok":false,"units":"1"}],"metrics":[{"key":"accuracy","labels":{},"last":0.666667},{"key":"accuracy_naive","labels":{},"last":0.333333},{"key":"auc","labels":{},"last":0.833333},{"key":"brier","labels":{},"last":0.440787},{"key":"divergences","labels":{"chain":"0"},"last":0.0},{"key":"divergences","labels":{"chain":"0","fit":"evaluation"},"last":0.0},{"key":"divergences","labels":{"chain":"1"},"last":0.0},{"key":"divergences","labels":{"chain":"1","fit":"evaluation"},"last":0.0},{"key":"explained_variance","labels":{},"last":0.626083},{"key":"explained_variance","labels":{"fit":"evaluation"},"last":0.659069},{"key":"num_steps","labels":{"chain":"0"},"last":7.0},{"key":"num_steps","labels":{"chain":"0","fit":"evaluation"},"last":7.0},{"key":"num_steps","labels":{"chain":"1"},"last":7.0},{"key":"num_steps","labels":{"chain":"1","fit":"evaluation"},"last":7.0},{"key":"step_size","labels":{"chain":"0"},"last":0.633681},{"key":"step_size","labels":{"chain":"0","fit":"evaluation"},"last":0.650024},{"key":"step_size","labels":{"chain":"1"},"last":0.582963},{"key":"step_size","labels":{"chain":"1","fit":"evaluation"},"last":0.666196}],"scorecard":["convergence.rhat","convergence.ess","convergence.divergences","data.missing"],"pictures":[{"plot_id":"plt_01jaf3w1img2missed3x4y5z6","recipe":"ml.misclassified","title":"Images the model got wrong"}],"inputs":[{"var":"pixels","kind":"text","dtype":"uint8","levels":[]},{"dim":"channel","kind":"choice","levels":["gray"],"policy":"fixed"},{"dim":"image","kind":"choice","levels":["00","01","02","03","04","05","06","07","08","09","10","11","12","13","14","15","16","17","18","19","20","21","22","23","24","25","26","27","28","29"],"policy":"new_levels"},{"dim":"x","kind":"choice","levels":["0","1","2","3","4","5","6","7","8","9","10","11"],"policy":"fixed"},{"dim":"y","kind":"choice","levels":["0","1","2","3","4","5","6","7","8","9","10","11"],"policy":"fixed"}],"score_modes":["evaluate","predict"],"conditions_on_actuals":false,"input_kind":"dataset","has_grid":false,"views":[],"family":"tabular","namespace":"tabular"},
    issues: [{"code":"BINDING_INVALID","severity":"warning","pointer":"/preset","message":"the image preset is for models of image collections","context":{}},{"code":"BINDING_INVALID","severity":"warning","pointer":"/panels/2/band","message":"'y_hat' has no range of single results: the panel shows the range of the average","suggested_action":"set band to mean","context":{}}],
    suggestions: [{"preset":"classifier","reasons":["The model gives the chance of each class."],"blocked_inputs":[]},{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":[]}],
    baseline: {"lead":"On the test images, the model was right 67% of the time.","tiles":[{"key":"panel","label":"How often the model is right","value":0.666667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.833333,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b001"],"notes_plain":[],"technical":{"model_ref":"model://retail/shape-check@v3"},"schema_version":2,"preset":"image","panels":[{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.666667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.833333,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"classes","title":"Chance of each shape","caption":"For image 00 The range shows the average, not single results.","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"circle","value":0.69197,"low":0.342587,"high":0.99533},{"label":"cross","value":0.0798052,"low":5.52293e-07,"high":0.257338},{"label":"square","value":0.228225,"low":0.0045748,"high":0.574184}]},{"panel_id":"missed","title":"Images the model got wrong","caption":"Test images with the shape the model chose","approximate":false,"kind":"picture","asset_id":"dsa_01jaf3w1img2missed3x4y5z6p","alt":"Test images with the shape the model chose","width":480,"height":320},{"panel_id":"about","title":"About these numbers","approximate":false,"kind":"note","text":"The model saw 30 product photos in training. The numbers on this page come from 12 photos it did not see."}],"accuracy_history":[],"inputs_echo":[]},
    batch_template: {"columns":[{"name":"image","role":"id","type":"text","required":true,"choices":[]},{"name":"x","role":"driver","type":"choice","required":true,"choices":["0","1","2","3","4","5","6","7","8","9","10","11"]},{"name":"y","role":"driver","type":"choice","required":true,"choices":["0","1","2","3","4","5","6","7","8","9","10","11"]},{"name":"pixels","role":"driver","type":"text","required":true,"choices":[]}],"id_column":"image","example_csv":"image,x,y,pixels\ncase1,0,0,\ncase2,1,1,\n","max_rows":200},
    batch: {"lead":"On the test images, the model was right 67% of the time.","tiles":[{"key":"panel","label":"How often the model is right","value":0.666667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false},{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.833333,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}],"channels":[],"series":[],"source_run_ids":["run_01jaf3q2x8m9v7k5h4g3f2b003"],"notes_plain":[],"technical":{},"schema_version":2,"preset":"image","panels":[{"panel_id":"m_accuracy","title":"How often the model is right","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How often the model is right","value":0.666667,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_accuracy","approximate":false}},{"panel_id":"m_auc","title":"How well the model ranks cases (AUC)","approximate":false,"kind":"kpi","tile":{"key":"panel","label":"How well the model ranks cases (AUC)","value":0.833333,"unit":"probability","delta_dir":"flat","caption":"From the model fit","panel_id":"m_auc","approximate":false}},{"panel_id":"classes","title":"Chance of each shape","caption":"For image 00 The range shows the average, not single results.","approximate":false,"kind":"bars","intent":"probability_bars","rows":[{"label":"circle","value":0.69197,"low":0.342587,"high":0.99533},{"label":"cross","value":0.0798052,"low":5.52293e-07,"high":0.257338},{"label":"square","value":0.228225,"low":0.0045748,"high":0.574184}]},{"panel_id":"missed","title":"Images the model got wrong","approximate":false,"kind":"unavailable","reason":"The picture is not available."},{"panel_id":"about","title":"About these numbers","approximate":false,"kind":"note","text":"The model saw 30 product photos in training. The numbers on this page come from 12 photos it did not see."}],"batch":{"n_rows":4,"n_scored":4,"outputs":["y_hat"],"stats":{"y_hat":{"mean":0.80779,"min":0.69197,"p10":0.703277,"p50":0.80869,"p90":0.911583,"max":0.92181}},"class_counts":[{"label":"circle","count":3},{"label":"square","count":1}],"histogram":[{"lo":0.69197,"hi":0.74943,"count":2},{"lo":0.74943,"hi":0.80689,"count":0},{"lo":0.80689,"hi":0.86435,"count":0},{"lo":0.86435,"hi":0.92181,"count":2}],"preview":[{"id":"00","y_hat":"circle","y_hat_probability":0.69197},{"id":"01","y_hat":"circle","y_hat_probability":0.92181},{"id":"02","y_hat":"circle","y_hat_probability":0.887721},{"id":"03","y_hat":"square","y_hat_probability":0.729659}],"download":{"available":true,"rows":4,"bytes_estimate":280}},"accuracy_history":[],"inputs_echo":[]},
  },
} as unknown as Record<V2Key, V2Raw>;

const V2_ANCHORS = {"regression":{"s0":[12.8999,8.64949,19.0225],"s1":[21.2809,14.4677,31.0952],"s2":[14.4922,9.75487,21.3162],"s3":[13.1181,8.80095,19.3368],"s4":[14.589,9.82211,21.4557],"s_new":[9.10224,6.01311,13.5521]},"classifier":{"s0":0.153694,"s1":0.135382,"s2":0.375524,"s3":0.141682,"s4":0.0431646},"image":{"00":{"circle":0.69197,"cross":0.0798052,"square":0.228225},"01":{"circle":0.92181,"cross":0.0339831,"square":0.0442066},"02":{"circle":0.887721,"cross":0.0547203,"square":0.0575591},"03":{"circle":0.246907,"cross":0.0234344,"square":0.729659}},"causal_cf_total":{"mean":406.891,"sd":7.55369,"quantiles":{"0.05":393.51,"0.25":402.751,"0.5":407.827,"0.75":412.059,"0.95":416.568},"units":"units"}} as unknown as V2Anchors;

const V1_UPGRADES = {
  mmm: {
    spec: {"layout_version":2,"preset":"mmm_planner","title":"Weekly media readout","client_name":"Northwind Outdoor","summary":"How your media spend turns into revenue, week by week. The numbers come from the media mix model that we fitted on your sales.","note":"Spend in this readout is net media spend. Agency fees are not included.","period_label":"week","source":{"kind":"endpoint","alias":"geo-mmm-prod"},"subject":{"variable":"revenue","label":"Revenue","unit":"currency","currency":"EUR","decimals":0},"panels":[{"id":"tiles","title":"This period","kind":"mmm","part":"tiles"},{"id":"fan","title":"Revenue over time","kind":"chart","intent":"fan","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"scorecard","title":"Channels","kind":"mmm","part":"scorecard"},{"id":"changes","title":"What changed","kind":"mmm","part":"changes"},{"id":"roi_trend","title":"Return on media over time","kind":"mmm","part":"roi_trend"},{"id":"scenarios","title":"Scenarios","kind":"mmm","part":"scenarios"}],"mmm":{"channels":[{"name":"tv","label":"TV","min_mult":0.5,"max_mult":1.5,"step":0.05},{"name":"search","label":"Paid search","min_mult":0.5,"max_mult":2.0,"step":0.05},{"name":"social","label":"Social","min_mult":0.5,"max_mult":1.5,"step":0.05},{"name":"display","label":"Display","min_mult":0.25,"max_mult":1.5,"step":0.05}],"whatif":{"enabled":true,"exact":true},"tiles":["expected","return_on_media","in_range_spend","vs_expected"]},"scenario_rows":{"enabled":false,"max_rows":5,"outputs":[],"inputs":[],"fixed":{}},"actuals":{"enabled":true,"publish":"review","refit_workflow_id":"wf_01jaf3w1x2y3z4a5b6c7d8e9f0","allow_refit_requests":true,"match":"time","threshold":0.5},"refresh":{"workflow_id":"wf_01jaf3w1x2y3z4a5b6c7d8e9f0","follow_endpoint":true},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":true},
    notes: ["The likely ranges now show where one new result will probably be. They are wider than before."],
    issues: [],
    catalog: {"outputs":[{"name":"y_hat","manifest_name":"y_hat","dims":["time"],"time_dim":"time","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true},{"name":"contributions","manifest_name":"contributions","dims":["component","time"],"time_dim":"time","shape":"time","representation":"draws","classes":[],"centre":"mean","stats":["centre","low","high"],"reduces":["none","select","last","next","mean","sum","share_above"],"intents":["fan","line","range","residuals","scatter","decomposition"],"predictive_ok":true}],"metrics":[],"scorecard":[],"pictures":[],"inputs":[],"score_modes":["counterfactual","evaluate","forecast","predict"],"conditions_on_actuals":false,"input_kind":"dataset","time_dim":"time","has_grid":false,"views":[],"family":"mmm","namespace":"mmm"},
    suggestions: [{"preset":"mmm_planner","reasons":["The model is a media mix model: the planner shows channels and what-ifs."],"blocked_inputs":[]},{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":[]}],
  },
  forecast: {
    spec: {"layout_version":2,"preset":"forecast","title":"Monthly sales forecast","client_name":"Northwind Outdoor","summary":"The sales that we expect for the next months. Send the actual sales of each month: the forecast then starts from your newest numbers.","period_label":"month","source":{"kind":"endpoint","alias":"sales-forecast"},"subject":{"variable":"y_hat","label":"Sales","unit":"currency","currency":"EUR","decimals":0},"panels":[{"id":"next","title":"Sales next month","kind":"kpi","source":{"kind":"output","variable":"y_hat","select":{}},"stat":"centre","reduce":{"how":"next","over":[]},"band":"outcome","compare":"previous","good":"up"},{"id":"typical_error","title":"Typical error","kind":"kpi","source":{"kind":"accuracy","metric":"mae"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"previous","good":"down"},{"id":"inside_range","title":"Results inside the likely range","kind":"kpi","source":{"kind":"accuracy","metric":"band_hit_rate"},"stat":"value","reduce":{"how":"none","over":[]},"band":"none","compare":"previous","good":"none"},{"id":"fan","title":"Sales and forecast","kind":"chart","intent":"fan","source":{"kind":"output","variable":"y_hat","select":{}},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"accuracy","title":"Accuracy over time","kind":"chart","intent":"accuracy_history","source":{"kind":"accuracy","metric":"mape"},"reduce":{"how":"none","over":[]},"band":"outcome","max_items":20},{"id":"next_periods","title":"The next months","kind":"table","source":{"kind":"output","variable":"y_hat","select":{}},"columns":["t","centre","low","high"],"max_rows":12}],"scenario_rows":{"enabled":false,"max_rows":5,"outputs":[],"inputs":[],"fixed":{}},"actuals":{"enabled":true,"publish":"auto","allow_refit_requests":false,"match":"time","threshold":0.5},"refresh":{"follow_endpoint":true,"horizon":6},"limits":{"exact_runs_per_day":20,"batch_runs_per_day":3},"show_technical":false},
    notes: ["The KPI 'sales' is now read from the model output 'y_hat'.","The channels 'price' were placeholders: the forecast preset shows sales without channels.","The likely ranges now show where one new result will probably be. They are wider than before."],
    issues: [],
    catalog: {"outputs":[{"name":"y_hat","manifest_name":"y_hat","dims":["t"],"time_dim":"t","shape":"time","representation":"quantiles","classes":[],"centre":"median","stats":["centre","low","high"],"reduces":["none","select","last","next","share_above"],"intents":["fan","line","range","residuals","scatter"],"predictive_ok":true}],"metrics":[],"scorecard":[],"pictures":[],"inputs":[],"score_modes":["evaluate","forecast","predict"],"conditions_on_actuals":true,"input_kind":"dataset","time_dim":"t","has_grid":false,"views":[],"family":"forecast","namespace":"ts"},
    suggestions: [{"preset":"forecast","reasons":["The model forecasts an output over time."],"blocked_inputs":[]},{"preset":"regression","reasons":["The model predicts one value per case."],"blocked_inputs":[]},{"preset":"generic","reasons":["Any model: one value and one chart of its main output."],"blocked_inputs":[]}],
  },
} as unknown as Record<"mmm" | "forecast", V1Upgrade>;

/** The layout 2 specs of the fixtures (as published). */
export const FORECAST_V2: DashboardSpecV2 = V2_RAW.forecast.spec;
export const REGRESSION_V2: DashboardSpecV2 = V2_RAW.regression.spec;
export const CLASSIFIER_V2: DashboardSpecV2 = V2_RAW.classifier.spec;
export const CAUSAL_V2: DashboardSpecV2 = V2_RAW.causal.spec;
export const FIELD_V2: DashboardSpecV2 = V2_RAW.field.spec;
export const IMAGE_V2: DashboardSpecV2 = V2_RAW.image.spec;
/** The MMM dashboard as the automatic upgrade leaves it (`upgrade_v1` of {@link MMM_SPEC}). */
export const MMM_V2: DashboardSpecV2 = V1_UPGRADES.mmm.spec;

/** The layout 2 dashboards of {@link V2_PROJECT} (`mmm`: upgraded automatically from layout 1). */
export const V2_DASHBOARD_IDS = {
  forecast: "dsh_01jaf3w2fcst2visits3x4y5z6",
  regression: "dsh_01jaf3w2regr2demand3x4y5z6",
  classifier: "dsh_01jaf3w2clsf2highdm3x4y5z6",
  causal: "dsh_01jaf3w2caus2loyalt3x4y5z6",
  field: "dsh_01jaf3w2fild2incmap3x4y5z6",
  image: "dsh_01jaf3w2imag2shapes3x4y5z6",
  mmm: "dsh_01jaf3w2mmm22upgrd3x4y5z6a",
} as const satisfies Record<V2Key | "mmm", string>;

/** The flag a test or a developer sets to create layout 1 dashboards again (the server's `dashboard_layout1_create`). */
const LAYOUT1_CREATE_KEY = "mapi.mock.layout1Create";

function layout1CreateAllowed(): boolean {
  try {
    return localStorage.getItem(LAYOUT1_CREATE_KEY) === "1";
  } catch {
    return false;
  }
}

/** A stored spec of either layout. */
function isV2(spec: AnyDashboardSpec | null | undefined): spec is DashboardSpecV2 {
  return !!spec && spec.layout_version === 2;
}

/**
 * The layout 1 reading of a stored spec: a layout 2 spec as the v0.16 what-if, scenario and
 * actuals code reads it (the planner's channels, tiles and what-ifs; the KPI from the subject).
 */
function v1View(spec: AnyDashboardSpec): DashboardSpec {
  if (!isV2(spec)) return spec;
  const planner = spec.preset === "mmm_planner" ? (spec.mmm ?? null) : null;
  return {
    layout_version: 1,
    title: spec.title,
    client_name: spec.client_name ?? null,
    summary: spec.summary ?? null,
    note: spec.note ?? null,
    period_label: spec.period_label as DashboardSpec["period_label"],
    source: spec.source,
    kpi: kpiOfSubject(spec.subject),
    channels: planner?.channels ?? [],
    tiles: (planner?.tiles ?? []).filter((t) => t !== "panel"),
    tabs: planner ? ["this_period", "trends", "scenarios", "update", "history"] : ["this_period", "history"],
    whatif: planner?.whatif ?? { enabled: false, exact: false },
    actuals: {
      enabled: spec.actuals?.enabled ?? false,
      publish: spec.actuals?.publish ?? "review",
      allow_refit_requests: spec.actuals?.allow_refit_requests ?? false,
      refit_workflow_id: spec.actuals?.refit_workflow_id ?? null,
    },
    refresh: spec.refresh,
    limits: { exact_runs_per_day: spec.limits?.exact_runs_per_day ?? 20 },
    show_technical: spec.show_technical ?? false,
  };
}

function nounOf(label: string): string {
  return label.slice(1) === label.slice(1).toLowerCase() ? label.toLowerCase() : label;
}

const clone = <T,>(v: T): T => structuredClone(v);
type CalibrationBin = NonNullable<ClassAccuracy["bins"]>[number];
const isFiniteNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
/** A scalar cell as text (`""` for anything else). */
const textOf = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" || typeof v === "boolean" ? String(v) : "");

// ---------------------------------------------------------------------------- layout 2: scoring (the mock's own model)

/** One output of one scored case. */
interface Scored {
  centre: number | null;
  low: number | null;
  high: number | null;
  label?: string | null;
  probability?: number | null;
}

/** Scores one case (a resolved scenario row or a file row: values by input name). */
type Scorer = (row: Record<string, unknown>) => Scored;

const sigmoid = (z: number) => 1 / (1 + Math.exp(-z));
const logit = (p: number) => Math.log(p / (1 - p));
const numOr = (v: unknown, d: number) => (isFiniteNum(v) ? v : typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v)) ? Number(v) : d);
const flag = (v: unknown) => (v === true || v === 1 || v === "1" || v === "true" ? 1 : 0);

/** A small weekly wave by date, so two weeks of the same case differ a little. */
function weekWave(v: unknown): number {
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? 1 + 0.05 * Math.sin(t / (7 * DAY) / 1.7) : 1;
}

/** A level the fit never saw: a mix of the known stores, nudged by its name. */
function unseen(label: string): number {
  return 0.85 + 0.3 * seeded(Number.parseInt(hash(label).slice(0, 8), 16))();
}

/**
 * Units sold (conformal interval): the store's captured prediction at price 18, no promotion,
 * scaled by a price elasticity of -0.6 and a promotion lift of 30%.
 */
const scoreRegression: Scorer = (row) => {
  const store = textOf(row.store) || "s0";
  const a = V2_ANCHORS.regression[store] ?? V2_ANCHORS.regression.s_new!.map((v) => v * unseen(store));
  const f = (18 / Math.max(1, numOr(row.price, 18))) ** 0.6 * (1 + 0.3 * flag(row.promo)) * weekWave(row.week ?? row.time);
  return { centre: a[0]! * f, low: a[1]! * f, high: a[2]! * f };
};

/** P(high demand): the store's captured chance at price 18, -0.12 logit per EUR, +1.2 for a promotion. */
const scoreClassifier: Scorer = (row) => {
  const store = textOf(row.store) || "s0";
  const p0 = V2_ANCHORS.classifier[store] ?? 0.2 * unseen(store);
  const z = logit(Math.min(0.99, Math.max(0.01, p0))) - 0.12 * (numOr(row.price, 18) - 18) + 1.2 * flag(row.promo) + Math.log(weekWave(row.week ?? row.time));
  return { centre: sigmoid(z), low: null, high: null };
};

/** The first forecast period of the captured fan, scaled by a price elasticity of -0.35. */
function forecastScorer(base: ReadoutSnapshot): Scorer {
  const next = base.panels?.find((b) => b.panel_id === "next");
  const tile = next && next.kind === "kpi" ? next.tile : null;
  return (row) => {
    const f = (10 / Math.max(1, numOr(row.price, 10))) ** 0.35;
    const c = tile?.value ?? 1000;
    return { centre: c * f, low: (tile?.low ?? c * 0.95) * f, high: (tile?.high ?? c * 1.05) * f };
  };
}

/** The most likely shape of a test image (unknown images: probabilities drawn from the name). */
const scoreImage: Scorer = (row) => {
  const id = textOf(row.image);
  let probs = V2_ANCHORS.image[id];
  if (!probs) {
    const r = seeded(Number.parseInt(hash(id).slice(0, 8), 16));
    const w = [r() + 0.2, r(), r()];
    const s = w.reduce((a, b) => a + b, 0);
    probs = { circle: w[0]! / s, cross: w[1]! / s, square: w[2]! / s };
  }
  const [label, p] = Object.entries(probs).sort((a, b) => b[1] - a[1])[0]!;
  return { centre: p, low: null, high: null, label, probability: p };
};

// ---------------------------------------------------------------------------- layout 2: runtimes

/** What the mock keeps per layout 2 dashboard besides its rows (its catalog, snapshots and scorer). */
interface V2Runtime {
  key: V2Key | "mmm" | "forecast_v1";
  catalog: SourceCatalog;
  suggestions: PresetSuggestion[];
  /** The baseline snapshot (`null`: built from the layout 1 snapshot, see {@link legacyV2}). */
  base: ReadoutSnapshot | null;
  actualsBase: ReadoutSnapshot | null;
  predictionBase: ReadoutSnapshot | null;
  batchBase: ReadoutSnapshot | null;
  batchTemplate: BatchTemplate | null;
  scorer: Scorer | null;
  /** Pictures of the published revision: asset id → size. */
  assets: Map<string, { width: number; height: number }>;
}

function runtimeOf(key: V2Key): V2Runtime {
  const raw = V2_RAW[key];
  const scorer: Record<V2Key, Scorer | null> = {
    forecast: forecastScorer(raw.baseline),
    regression: scoreRegression,
    classifier: scoreClassifier,
    causal: null,
    field: null,
    image: scoreImage,
  };
  const assets = new Map<string, { width: number; height: number }>();
  for (const b of raw.baseline.panels ?? []) if (b.kind === "picture") assets.set(b.asset_id, { width: b.width, height: b.height });
  return {
    key,
    catalog: raw.catalog,
    suggestions: raw.suggestions,
    base: raw.baseline,
    actualsBase: raw.actuals ?? null,
    predictionBase: raw.prediction ?? null,
    batchBase: raw.batch ?? null,
    batchTemplate: raw.batch_template ?? null,
    scorer: scorer[key],
    assets,
  };
}

function upgradedRuntime(key: "mmm" | "forecast"): V2Runtime {
  const u = V1_UPGRADES[key];
  return {
    key: key === "mmm" ? "mmm" : "forecast_v1",
    catalog: u.catalog,
    suggestions: u.suggestions,
    base: null,
    actualsBase: null,
    predictionBase: null,
    batchBase: null,
    batchTemplate: null,
    scorer: null,
    assets: new Map(),
  };
}

/** The runtime of a binding (`alias`): a layout 2 fixture's, else the MMM or the forecast's upgrade. */
function runtimeForAlias(alias: string | null | undefined): V2Runtime | null {
  for (const key of Object.keys(V2_RAW) as V2Key[]) if (V2_RAW[key].spec.source.alias === alias) return runtimeOf(key);
  if (alias === integrationsGen.alias) return upgradedRuntime("mmm");
  if (alias === integrationsGen.forecast.alias) return upgradedRuntime("forecast");
  return null;
}

// ---------------------------------------------------------------------------- layout 2: snapshots

type Block = PanelBlock;

function unavailable(p: { id: string; title: string; caption?: string | null }, reason: string): Block {
  return { kind: "unavailable", panel_id: p.id, title: p.title, caption: p.caption ?? null, approximate: false, reason };
}

function kpiBlocks(blocks: Block[]): KpiValue[] {
  return blocks.flatMap((b) => (b.kind === "kpi" ? [b.tile] : [])).slice(0, 6);
}

/**
 * The blocks of `spec`'s panels (`panels.resolve_panels` as far as the mock goes): the block
 * of the same panel id in `base` (retitled), a note, a fit metric KPI from the catalog, or an
 * `unavailable` block. Planner parts have no block (they read the layout 1 fields).
 */
function resolveV2(spec: DashboardSpecV2, base: ReadoutSnapshot | null, catalog: SourceCatalog | null): Block[] {
  const out: Block[] = [];
  for (const p of spec.panels) {
    if (p.kind === "mmm") continue;
    const had = base?.panels?.find((b) => b.panel_id === p.id);
    if (had) {
      const b = clone(had);
      b.title = p.title;
      if (p.caption) b.caption = p.caption;
      if (b.kind === "kpi") b.tile = { ...b.tile, label: p.title };
      out.push(b);
    } else if (p.kind === "note") {
      out.push({ kind: "note", panel_id: p.id, title: p.title, caption: p.caption ?? null, approximate: false, text: p.text });
    } else if (p.kind === "kpi" && p.source.kind === "metric") {
      const key = p.source.key;
      const m = catalog?.metrics?.find((x) => x.key === key && Object.keys(x.labels ?? {}).length === 0);
      if (m?.last === null || m?.last === undefined) out.push(unavailable(p, `The model fit logged no ${key}.`));
      else
        out.push({
          kind: "kpi",
          panel_id: p.id,
          title: p.title,
          caption: p.caption ?? null,
          approximate: false,
          tile: { key: "panel", panel_id: p.id, label: p.title, value: m.last, unit: "number", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: "From the model fit", approximate: false },
        });
    } else {
      out.push(unavailable(p, "This panel has no numbers yet. It shows after the next readout."));
    }
  }
  return out;
}

/** A layout 2 snapshot of `spec` from a fixture snapshot (its panels re-resolved for the spec's panels). */
function v2From(spec: DashboardSpecV2, base: ReadoutSnapshot, catalog: SourceCatalog | null, patch: Partial<ReadoutSnapshot> = {}): ReadoutSnapshot {
  const panels = resolveV2(spec, base, catalog);
  return { ...clone(base), panels, tiles: kpiBlocks(panels), ...patch };
}

/**
 * The layout 2 snapshot of a dashboard whose numbers come from a layout 1 snapshot (the media
 * planner, the upgraded dummy-channel forecast): the legacy lead, tiles, channels and series
 * (`build_snapshot_v2(legacy=…)`), the outcome bands widened to the range of one new result
 * (§10.2: wider than the range of the average layout 1 showed), and blocks for the panels
 * that read the same numbers.
 */
function legacyV2(spec: DashboardSpecV2, legacy: ReadoutSnapshot): ReadoutSnapshot {
  const widen = (m: number | null | undefined, lo: number | null | undefined, hi: number | null | undefined, k = 1.8) =>
    m == null || lo == null || hi == null ? { low: lo ?? null, high: hi ?? null } : { low: m - (m - lo) * k, high: m + (hi - m) * k };
  const series = (legacy.series ?? []).map((s) => ({ ...s, points: (s.points ?? []).map((p) => ({ ...p, ...widen(p.mean, p.low, p.high) })) }));
  const tiles = (legacy.tiles ?? []).map((t) => (t.low != null && t.high != null ? { ...t, ...widen(t.value, t.low, t.high), caption: t.caption?.startsWith("Likely range") ? `Likely range ${fmt(widen(t.value, t.low, t.high).low ?? 0)} to ${fmt(widen(t.value, t.low, t.high).high ?? 0)}` : t.caption } : t));
  const first = series[0];
  const future = (first?.points ?? []).filter((p) => p.actual == null && p.mean != null);
  const nextTile = tiles.find((t) => t.key === "expected");
  const acc = legacy.accuracy ?? null;
  const panels: Block[] = [];
  for (const p of spec.panels) {
    if (p.kind === "mmm") continue;
    const base = { panel_id: p.id, title: p.title, caption: p.caption ?? null, approximate: false };
    if (p.kind === "chart" && (p.intent === "fan" || p.intent === "line") && first) {
      panels.push({ ...base, kind: "series", intent: p.intent, series: [{ ...first, label: p.title }], split_at: future[0]?.t ?? null, x_kind: "time" });
    } else if (p.kind === "kpi" && p.source.kind === "output" && nextTile) {
      panels.push({ ...base, kind: "kpi", tile: { ...nextTile, key: "panel", panel_id: p.id, label: p.title } });
    } else if (p.kind === "kpi" && p.source.kind === "accuracy" && acc) {
      const value = p.source.metric === "band_hit_rate" ? (acc.band_hit_rate ?? null) : p.source.metric === "mape" ? (acc.mape ?? null) : null;
      panels.push(value === null ? unavailable(p, "Send numbers to see this.") : { ...base, kind: "kpi", tile: { key: "panel", panel_id: p.id, label: p.title, value, unit: "percent", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: `Over ${acc.n} results that you sent`, approximate: false } });
    } else if (p.kind === "table" && future.length) {
      panels.push({ ...base, kind: "table", columns: ["period", "centre", "low", "high"], rows: future.slice(0, p.max_rows ?? 20).map((q) => [q.t, q.mean ?? null, q.low ?? null, q.high ?? null]) });
    } else if (p.kind === "note") {
      panels.push({ ...base, kind: "note", text: p.text });
    } else {
      panels.push(unavailable(p, "This panel has no numbers yet. It shows after the next readout."));
    }
  }
  return {
    ...legacy,
    tiles: spec.preset === "mmm_planner" ? tiles : kpiBlocks(panels),
    series,
    schema_version: 2,
    preset: spec.preset,
    panels,
    accuracy_history: [],
    inputs_echo: [],
  };
}

/** The upgraded planner's readout of week `w` (0 = newest). */
function mmmSnapshotV2(spec: DashboardSpecV2, grid: ResponseGrid, w: number): ReadoutSnapshot {
  return legacyV2(spec, mmmSnapshot(v1View(spec), grid, w));
}

/** A fresh baseline of a layout 2 dashboard (publish, refresh). */
function baselineV2(x: MockDashboard, spec: DashboardSpecV2): ReadoutSnapshot {
  const rt = x.v2;
  if (spec.preset === "mmm_planner") return mmmSnapshotV2(spec, x.grid ?? MMM_GRID, 0);
  if (rt?.base) return v2From(spec, rt.base, rt.catalog, { accuracy_history: lastHistory(x) });
  if (rt?.key === "forecast_v1") return legacyV2(spec, forecastSnapshot(v1View(spec), 0));
  const panels = resolveV2(spec, null, rt?.catalog ?? null);
  return { lead: "The first readout of this dashboard.", tiles: kpiBlocks(panels), channels: [], series: [], schema_version: 2, preset: spec.preset, panels, accuracy_history: [], inputs_echo: [], notes_plain: [], source_run_ids: [], technical: {} };
}

/** The accuracy history of the dashboard so far (the newest published actuals readout's). */
function lastHistory(x: MockDashboard): AccuracyPoint[] {
  const last = [...x.readouts].reverse().find((r) => r.r.kind === "actuals" && r.r.visibility === "published" && r.r.status === "ready" && r.r.snapshot?.accuracy_history?.length);
  return clone(last?.r.snapshot?.accuracy_history ?? []);
}

// ---------------------------------------------------------------------------- layout 2: tried cases

/** `dashboard_inputs.check_value`. */
function checkValue(value: unknown, d: ScenarioInputDef): string | null {
  const label = d.label;
  if (d.type === "number" || d.type === "integer") {
    if (typeof value !== "number") return `${label} must be a number`;
    if (!Number.isFinite(value)) return `${label} must be a finite number`;
    const lo = d.min ?? 0;
    const hi = d.max ?? 0;
    if (value < lo || value > hi) return `${label} must be between ${lo} and ${hi}`;
    if (d.type === "integer" && !Number.isInteger(value)) return `${label} must be a whole number`;
    const step = d.step ?? (d.type === "integer" ? 1 : null);
    if (step !== null) {
      const k = Math.round((value - lo) / step);
      if (Math.abs(value - (lo + k * step)) > 1e-9 * (hi - lo)) return `${label} must be ${lo} plus a multiple of ${step}`;
    }
    return null;
  }
  if (d.type === "toggle") return typeof value === "boolean" ? null : `${label} must be true or false`;
  if (d.type === "date") {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(Date.parse(value))) return `${label} must be a date as YYYY-MM-DD`;
    if ((d.date_min && value < d.date_min) || (d.date_max && value > d.date_max)) return `${label} must be between ${d.date_min} and ${d.date_max}`;
    return null;
  }
  if (typeof value === "boolean" || (typeof value !== "string" && typeof value !== "number")) return `${label} must be one of the choices`;
  if (typeof value === "string" && value.length > 128) return `${label} holds more than 128 characters`;
  return matchChoice(value, d.choices ?? []) < 0 ? `${label} must be one of the choices` : null;
}

/** `normalise_label`: trimmed, case-folded, `1.0` → `1`, runs of other characters one `_`. */
function looseLabel(v: unknown): string {
  let s = String(v).normalize("NFC").trim();
  if (/^-?\d+\.0+$/.test(s)) s = s.replace(/\.0+$/, "");
  return s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "_").replace(/^_+|_+$/g, "");
}

function matchChoice(value: unknown, choices: readonly string[]): number {
  const exact = choices.indexOf(String(value));
  return exact >= 0 ? exact : choices.findIndex((c) => looseLabel(c) === looseLabel(value));
}

function inputIssue(pointer: string, message: string): Issue {
  return { code: "DASHBOARD_INPUT_INVALID", severity: "error", pointer, message };
}

/** `dashboard_inputs.check_rows` (pointers `/rows/<i>/<name>`, at most 50). */
function checkRows(rows: unknown[], defs: ScenarioRowsDef): Issue[] {
  const issues: Issue[] = [];
  if (!rows.length) return [inputIssue("/rows", "send at least one row")];
  const max = defs.max_rows ?? 5;
  if (rows.length > max) issues.push(inputIssue("/rows", `send at most ${max} rows (got ${rows.length})`));
  const hkey = defs.horizon && !(defs.inputs ?? []).some((i) => i.name === "horizon") ? "horizon" : null;
  rows.forEach((row, r) => {
    if (row === null || typeof row !== "object" || Array.isArray(row)) {
      issues.push(inputIssue(`/rows/${r}`, "a row must be an object"));
      return;
    }
    for (const [name, value] of Object.entries(row as Record<string, unknown>)) {
      const pointer = `/rows/${r}/${name}`;
      if (value === null) continue;
      if (hkey && name === hkey) {
        const h = defs.horizon!;
        if (typeof value !== "number" || !Number.isInteger(value)) issues.push(inputIssue(pointer, "the number of periods must be a whole number"));
        else if (value < (h.min ?? 1) || value > h.max) issues.push(inputIssue(pointer, `the number of periods must be ${h.min ?? 1} to ${h.max}`));
        continue;
      }
      const d = (defs.inputs ?? []).find((i) => i.name === name);
      if (!d) {
        issues.push(inputIssue(pointer, `'${name}' is not an input of this dashboard`));
        continue;
      }
      const why = checkValue(value, d);
      if (why) issues.push(inputIssue(pointer, why));
    }
  });
  return issues.slice(0, 50);
}

/** `dashboard_inputs.resolve_row`: the defaults, the row's values (the choice as written), the fixed values. */
function resolveRow(row: Record<string, unknown>, defs: ScenarioRowsDef): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const d of defs.inputs ?? []) {
    const v = row[d.name] ?? d.default;
    out[d.name] = d.type === "choice" ? (d.choices?.[matchChoice(v, d.choices ?? [])] ?? textOf(v)) : (v as string | number | boolean);
  }
  if (defs.horizon && !(defs.inputs ?? []).some((i) => i.name === "horizon")) out.horizon = isFiniteNum(row.horizon) ? row.horizon : defs.horizon.default;
  for (const [k, v] of Object.entries(defs.fixed ?? {})) out[k] = v;
  return out;
}

function predictionValue(s: Scored, band: boolean): PredictionValue {
  return s.label ? { centre: s.centre, low: null, high: null, label: s.label, probability: s.probability ?? null } : { centre: s.centre, low: band ? s.low : null, high: band ? s.high : null };
}

function pct(p: number): string {
  return fmt(p * 100);
}

/** The lead of a tried case (`LEADS`, from numbers only). */
function caseLead(spec: DashboardSpecV2, s: Scored): string {
  const d = spec.subject.decimals ?? 0;
  const n = nounOf(spec.subject.label);
  if (spec.preset === "classifier") return `The model gives a ${pct(s.centre ?? 0)}% chance of ${spec.subject.positive_label ?? "a positive result"} for this case.`;
  const ranged = s.low != null && s.high != null;
  if (spec.preset === "forecast") return ranged ? `We expect ${fmt(s.centre ?? 0, d)} ${n} next ${spec.period_label ?? "period"}, likely between ${fmt(s.low ?? 0, d)} and ${fmt(s.high ?? 0, d)}.` : `We expect ${fmt(s.centre ?? 0, d)} ${n} next ${spec.period_label ?? "period"}.`;
  return ranged ? `For the case you set, the model predicts ${fmt(s.centre ?? 0, d)} ${n}, likely between ${fmt(s.low ?? 0, d)} and ${fmt(s.high ?? 0, d)}.` : `For the case you set, the model predicts ${fmt(s.centre ?? 0, d)} ${n}.`;
}

/** The readout of tried cases: the prediction per row, the subject's KPI set to the first row. */
function predictionSnapshot(x: MockDashboard, spec: DashboardSpecV2, rows: Record<string, string | number | boolean>[], sent: Record<string, unknown>[]): ReadoutSnapshot {
  const rt = x.v2!;
  const scorer = rt.scorer!;
  const base = rt.predictionBase ?? rt.base!;
  const band = base.prediction?.band ?? (scoreBand(spec) ? "outcome" : "none");
  const outputs = spec.scenario_rows?.outputs?.length ? spec.scenario_rows.outputs : [spec.subject.variable];
  const scored = rows.map((r) => scorer(r));
  const echo = sent.map((r) => Object.fromEntries(Object.entries(r).filter(([, v]) => v !== null && v !== undefined)) as Record<string, string | number | boolean>);
  const prediction: PredictionResult = {
    rows: rows.map((_, i) => ({ inputs: echo[i] ?? {}, outputs: Object.fromEntries(outputs.map((o) => [o, predictionValue(scored[i]!, band !== "none")])) })),
    band,
    approximate: false,
  };
  const snap = v2From(spec, base, rt.catalog, { prediction, inputs_echo: echo, lead: caseLead(spec, scored[0]!), source_run_ids: [`run_01jaf3q2x8m9v7k5h4g3f2p${randomB64(3).toLowerCase().replace(/[^a-z0-9]/g, "x")}`], input_fingerprint: `sha256:${hash(JSON.stringify(rows))}`, notes_plain: [] });
  const subjectKpi = spec.panels.find((p) => p.kind === "kpi" && p.source.kind === "output" && p.source.variable === spec.subject.variable);
  const first = scored[0]!;
  snap.panels = (snap.panels ?? []).map((b) =>
    b.kind === "kpi" && subjectKpi && b.panel_id === subjectKpi.id
      ? { ...b, tile: { ...b.tile, value: first.centre, low: band === "none" ? null : first.low, high: band === "none" ? null : first.high, caption: "For the case you set" } }
      : b,
  );
  snap.tiles = kpiBlocks(snap.panels);
  return snap;
}

/** Does the subject output have a range (not a point model)? */
function scoreBand(spec: DashboardSpecV2): boolean {
  return spec.preset === "regression" || spec.preset === "forecast";
}

// ---------------------------------------------------------------------------- layout 2: scored files

interface XrVarLike {
  dims?: string[];
  data?: unknown;
}

/** The rows of an upload along its one row dim: `{column: value}` (the id column included). */
function tableRowsOf(inputs: unknown, idColumn: string | null): { rows: Record<string, unknown>[]; columns: string[] } | Issue {
  const ds = (inputs ?? {}) as { coords?: Record<string, XrVarLike>; data_vars?: Record<string, XrVarLike> };
  const vars: Record<string, XrVarLike> = { ...(ds.coords ?? {}), ...(ds.data_vars ?? {}) };
  const columns = Object.keys(vars);
  if (!columns.length) return inputIssue("/inputs", "the file has no columns");
  const lengths = columns.map((c) => (Array.isArray(vars[c]?.data) ? (vars[c].data as unknown[]).length : -1));
  if (lengths.some((n) => n < 0)) return inputIssue(`/inputs/data_vars/${columns[lengths.findIndex((n) => n < 0)]}`, "each column needs a list of values");
  const n = idColumn && Array.isArray(vars[idColumn]?.data) ? (vars[idColumn].data as unknown[]).length : Math.max(...lengths);
  if (lengths.some((k) => k !== n)) return inputIssue("/inputs", "every column needs one value per row");
  const rows = Array.from({ length: n }, (_, i) => Object.fromEntries(columns.map((c) => [c, (vars[c]!.data as unknown[])[i] ?? null])));
  return { rows, columns };
}

/** The scenario input a file column fills (by its bind), for ranges and the scorer's names. */
function inputForColumn(spec: DashboardSpecV2, column: string): ScenarioInputDef | null {
  return (spec.scenario_rows?.inputs ?? []).find((d) => ("var" in d.bind ? d.bind.var : d.bind.dim) === column || d.name === column) ?? null;
}

/** A file row in the scorer's names (scenario input names where a column binds one). */
function caseOf(spec: DashboardSpecV2, row: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...(spec.scenario_rows?.fixed ?? {}) };
  for (const [col, v] of Object.entries(row)) out[inputForColumn(spec, col)?.name ?? col] = v;
  return out;
}

function quantile(xs: number[], q: number): number {
  const s = [...xs].sort((a, b) => a - b);
  const pos = (s.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return s[lo]! + (s[hi]! - s[lo]!) * (pos - lo);
}

interface BatchLine {
  id: string;
  inputs: Record<string, unknown>;
  out: Scored;
}

/** `panels.batch_summary`: the spread of the row centres (never an interval), shares, a histogram, 20 rows. */
function batchSummaryOf(spec: DashboardSpecV2, lines: BatchLine[], nRows: number): BatchSummary {
  const outputs = spec.batch?.outputs ?? [spec.subject.variable];
  const name = outputs[0]!;
  const centres = lines.map((l) => l.out.centre).filter(isFiniteNum);
  const stats: BatchStats = centres.length
    ? { mean: centres.reduce((a, b) => a + b, 0) / centres.length, min: Math.min(...centres), p10: quantile(centres, 0.1), p50: quantile(centres, 0.5), p90: quantile(centres, 0.9), max: Math.max(...centres) }
    : {};
  const classes = lines.some((l) => l.out.label);
  const counts = new Map<string, number>();
  for (const l of lines) if (l.out.label) counts.set(l.out.label, (counts.get(l.out.label) ?? 0) + 1);
  const histogram: BatchSummary["histogram"] = [];
  if (centres.length) {
    const lo = Math.min(...centres);
    const hi = Math.max(...centres);
    const k = hi > lo ? Math.min(20, Math.max(1, centres.length)) : 1;
    const w = (hi - lo) / k || 1;
    for (let i = 0; i < k; i++) histogram.push({ lo: lo + i * w, hi: i === k - 1 ? hi : lo + (i + 1) * w, count: 0 });
    for (const c of centres) histogram[Math.min(k - 1, Math.floor((c - lo) / w))]!.count += 1;
  }
  const threshold = spec.actuals?.threshold ?? 0.5;
  return {
    n_rows: nRows,
    n_scored: lines.length,
    outputs: [...outputs],
    stats: { [name]: stats },
    share_positive: spec.preset === "classifier" && !classes && centres.length ? centres.filter((c) => c >= threshold).length / centres.length : null,
    class_counts: [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 20).map(([label, count]) => ({ label, count })),
    histogram,
    preview: lines.slice(0, 20).map((l) => (classes ? { id: l.id, [name]: l.out.label ?? null, [`${name}_probability`]: l.out.probability ?? null } : { id: l.id, [name]: l.out.centre })),
    download: { available: true, rows: lines.length, bytes_estimate: 64 * lines.length + 64 },
  };
}

/** `downloads.render_predictions` for the mock's own scores (formula cells prefixed with `'`). */
function predictionsCsv(spec: DashboardSpecV2, idColumn: string, columns: string[], lines: BatchLine[]): string {
  const name = spec.batch?.outputs?.[0] ?? spec.subject.variable;
  const classes = lines.some((l) => l.out.label);
  const cell = (v: unknown): string => {
    if (v === null || v === undefined) return "";
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
    const text = textOf(v);
    const quoted = /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    return /^[=+\-@\t\r]/.test(text) && !Number.isFinite(Number(text)) ? `'${quoted}` : quoted;
  };
  const head = [idColumn, ...columns, ...(classes ? [name, `${name}_probability`] : [name, `${name}_low`, `${name}_high`])];
  const body = lines.map((l) => [l.id, ...columns.map((c) => l.inputs[c]), ...(classes ? [l.out.label, l.out.probability] : [l.out.centre, l.out.low, l.out.high])].map(cell).join(","));
  return `${[head.map(cell).join(","), ...body].join("\n")}\n`;
}

// ---------------------------------------------------------------------------- layout 2: accuracy

function errorAccuracyOf(pairs: { p: number; a: number; lo: number | null; hi: number | null }[]): ErrorAccuracy {
  const n = pairs.length;
  if (!n) return { kind: "error", n: 0 };
  const e = pairs.map((x) => x.p - x.a);
  const absA = pairs.reduce((s, x) => s + Math.abs(x.a), 0);
  const nonzero = pairs.filter((x) => x.a !== 0);
  const meanA = pairs.reduce((s, x) => s + x.a, 0) / n;
  const ssTot = pairs.reduce((s, x) => s + (x.a - meanA) ** 2, 0);
  const banded = pairs.filter((x) => x.lo !== null && x.hi !== null);
  return {
    kind: "error",
    n,
    mae: e.reduce((s, v) => s + Math.abs(v), 0) / n,
    rmse: Math.sqrt(e.reduce((s, v) => s + v * v, 0) / n),
    mape: nonzero.length ? (100 * nonzero.reduce((s, x) => s + Math.abs((x.p - x.a) / x.a), 0)) / nonzero.length : null,
    wape: absA ? (100 * e.reduce((s, v) => s + Math.abs(v), 0)) / absA : null,
    smape: (100 * pairs.reduce((s, x) => s + (Math.abs(x.a) + Math.abs(x.p) ? (2 * Math.abs(x.p - x.a)) / (Math.abs(x.a) + Math.abs(x.p)) : 0), 0)) / n,
    bias: e.reduce((s, v) => s + v, 0) / n,
    r2: ssTot ? 1 - e.reduce((s, v) => s + v * v, 0) / ssTot : null,
    band_hit_rate: banded.length ? (100 * banded.filter((x) => x.lo! <= x.a && x.a <= x.hi!).length) / banded.length : null,
  };
}

function classAccuracyOf(ps: number[], ys: number[], threshold: number, positive: string, negative: string): ClassAccuracy {
  const n = ps.length;
  const pred: number[] = ps.map((p) => (p >= threshold ? 1 : 0));
  const eps = 1e-15;
  const pos = ps.filter((_, i) => ys[i] === 1);
  const neg = ps.filter((_, i) => ys[i] === 0);
  let wins = 0;
  for (const a of pos) for (const b of neg) wins += a > b ? 1 : a === b ? 0.5 : 0;
  const bins: CalibrationBin[] = [];
  for (let k = 0; k < 10; k++) {
    const idx = ps.map((p, i) => ({ p, i })).filter(({ p }) => (k === 9 ? p >= 0.9 : p >= k / 10 && p < (k + 1) / 10));
    if (idx.length) bins.push({ p_mean: idx.reduce((s, v) => s + v.p, 0) / idx.length, observed: idx.reduce((s, v) => s + ys[v.i]!, 0) / idx.length, n: idx.length });
  }
  const counts = [
    [0, 0],
    [0, 0],
  ];
  ys.forEach((y, i) => (counts[y]![pred[i]!]! += 1));
  return {
    kind: "classification",
    n,
    positive,
    threshold,
    accuracy: n ? pred.filter((v, i) => v === ys[i]).length / n : null,
    brier: n ? ps.reduce((s, p, i) => s + (p - ys[i]!) ** 2, 0) / n : null,
    log_score: n ? ps.reduce((s, p, i) => s + (ys[i] ? Math.log(Math.max(eps, p)) : Math.log(Math.max(eps, 1 - p))), 0) / n : null,
    auc: pos.length && neg.length ? wins / (pos.length * neg.length) : null,
    rate_pred: n ? pred.reduce<number>((s, v) => s + v, 0) / n : null,
    rate_obs: n ? ys.reduce((s, v) => s + v, 0) / n : null,
    bins,
    confusion: { labels: [negative, positive], counts },
  };
}

/** `panels.accuracy_point`. */
function accuracyPoint(seq: number, createdAt: string, period: [string, string] | null, d: AccuracyDetail): AccuracyPoint {
  const values: Record<string, number> = {};
  const put = (k: string, v: number | null | undefined) => {
    if (isFiniteNum(v)) values[k] = v;
  };
  let n = 0;
  if (d.kind === "error") {
    n = d.n ?? 0;
    for (const k of ["mae", "rmse", "mape", "wape", "smape", "bias", "r2", "band_hit_rate"] as const) put(k, d[k]);
  } else if (d.kind === "classification") {
    n = d.n ?? 0;
    for (const k of ["accuracy", "brier", "log_score", "auc"] as const) put(k, d[k]);
  } else {
    n = d.n_periods ?? 0;
    put("effect_total", d.total);
    put("effect_relative", d.relative);
  }
  values.n = n;
  return { seq, created_at: createdAt, period, n, values };
}

/** The fixture's actuals readout with this upload's accuracy (KPIs, charts, history and lead follow). */
function actualsSnapshotV2(rt: V2Runtime, spec: DashboardSpecV2, detail: AccuracyDetail, pairs: { p: number; a: number }[], history: AccuracyPoint[], fingerprint: string): ReadoutSnapshot {
  const base = rt.actualsBase ?? rt.base!;
  const snap = v2From(spec, base, rt.catalog, { accuracy_detail: detail, accuracy_history: history, input_fingerprint: fingerprint, source_run_ids: [`run_01jaf3q2x8m9v7k5h4g3f2a${randomB64(3).toLowerCase().replace(/[^a-z0-9]/g, "x")}`] });
  const d = spec.subject.decimals ?? 0;
  const n = nounOf(spec.subject.label);
  let lead = snap.lead;
  if (detail.kind === "error") {
    snap.accuracy = { n: detail.n ?? 0, mape: detail.mape ?? null, bias: detail.bias ?? null, band_hit_rate: detail.band_hit_rate ?? null, mae: detail.mae ?? null, rmse: detail.rmse ?? null, wape: detail.wape ?? null, smape: detail.smape ?? null, r2: detail.r2 ?? null };
    const next = (rt.base?.lead ?? "").split(". On the ")[0]!.replace(/\.$/, "");
    // the forecast adds the error to its lead; a regression keeps the lead of its case
    if (spec.preset === "forecast" && detail.mape != null) lead = `${next}. On the ${detail.n} results you sent, the error was ${fmt(detail.mape, 1)}% on average.`;
  } else if (detail.kind === "classification") {
    lead = `On the ${detail.n} cases you sent, the model was right ${pct(detail.accuracy ?? 0)}% of the time.`;
  } else {
    lead = `The change added about ${fmt(detail.total ?? 0, d)} ${n} (likely ${fmt(detail.low ?? 0, d)} to ${fmt(detail.high ?? 0, d)}), ${fmt(detail.relative ?? 0, 1)}% more than without it.`;
  }
  snap.lead = lead.slice(0, 400);
  const metric = detail.kind === "classification" ? "accuracy" : detail.kind === "effect" ? "effect_total" : "mape";
  snap.panels = (snap.panels ?? []).map((b): Block => {
    const p = spec.panels.find((q) => q.id === b.panel_id);
    const tile = (value: number | null | undefined, unit: KpiValue["unit"], caption: string): Block => ({
      kind: "kpi",
      panel_id: b.panel_id,
      title: b.title,
      caption: b.caption ?? null,
      approximate: false,
      tile: { key: "panel", panel_id: b.panel_id, label: b.title, value: value ?? null, unit, delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption, approximate: false },
    });
    if (p?.kind === "kpi" && p.source.kind === "accuracy" && detail.kind === "error") {
      const m = p.source.metric;
      const v = (detail as Record<string, unknown>)[m];
      return tile(isFiniteNum(v) ? v : null, m === "band_hit_rate" || m === "mape" || m === "wape" || m === "smape" ? "percent" : "number", `Over ${detail.n} results that you sent`);
    }
    if (b.kind === "calibration" && detail.kind === "classification") return { ...b, bins: detail.bins ?? [] };
    if (b.kind === "confusion" && detail.kind === "classification" && detail.confusion) return { ...b, labels: detail.confusion.labels ?? [], counts: detail.confusion.counts ?? [] };
    if (b.kind === "series" && b.intent === "accuracy_history") {
      return { ...b, series: [{ name: metric, label: b.title, points: history.filter((h) => isFiniteNum(h.values?.[metric])).map((h) => ({ t: h.created_at.slice(0, 10), mean: h.values![metric]! })) }] };
    }
    if (b.kind === "scatter" && pairs.length) {
      return { ...b, points: pairs.slice(0, 520).map((q) => ({ x: q.p, y: b.intent === "residuals" ? q.a - q.p : q.a })) };
    }
    return b;
  });
  snap.tiles = kpiBlocks(snap.panels);
  return snap;
}

// ---------------------------------------------------------------------------- layout 2: the fixtures of acme/retail

interface V2Seed {
  seq: number;
  patch: Partial<Readout> & Pick<Readout, "kind" | "trigger" | "title">;
  actor: Actor;
  at: number;
  lines?: BatchLine[];
}

const SYSTEM: Actor = { kind: "system", id: "system" };
const CLEO: Actor = { kind: "client", id: cleo.id };
const ANA: Actor = { kind: "member", id: ana.id };

function mvOf(key: V2Key | "mmm", spec: AnyDashboardSpec): { id: string; ref: string } {
  const tag = { forecast: "fcst", regression: "regr", classifier: "clsf", causal: "caus", field: "fild", image: "imag", mmm: "mmmx" }[key];
  return { id: `mv_01jaf3w2${tag}2version3x4y5z6`.slice(0, 29), ref: `model://retail/${spec.source.alias ?? key}@v3` };
}

/** The accuracy history of the fixture's actuals readout up to its `k`-th point (seq and time as stored). */
function actualsAt(rt: V2Runtime, spec: DashboardSpecV2, k: number, seqs: number[]): ReadoutSnapshot {
  const base = rt.actualsBase!;
  const hist = (base.accuracy_history ?? []).slice(0, k).map((h, i) => ({ ...h, seq: seqs[i] ?? h.seq }));
  const last = hist[hist.length - 1];
  const detail = { ...clone(base.accuracy_detail!), ...(last && base.accuracy_detail?.kind === "error" ? Object.fromEntries(Object.entries(last.values ?? {}).filter(([key]) => key !== "n")) : {}) };
  if (detail.kind === "classification" && last?.values?.accuracy != null) detail.accuracy = last.values.accuracy;
  return actualsSnapshotV2(rt, spec, detail, [], hist, base.input_fingerprint ?? `sha256:${"ab".repeat(32)}`);
}

/** The scored lines behind a fixture batch readout (its preview rows: the download of the mock). */
function linesOf(snap: ReadoutSnapshot | null | undefined, spec: DashboardSpecV2): BatchLine[] {
  const name = spec.batch?.outputs?.[0] ?? spec.subject.variable;
  return (snap?.batch?.preview ?? []).map((p) => {
    const v = p[name];
    const prob = p[`${name}_probability`];
    return typeof v === "string"
      ? { id: String(p.id), inputs: {}, out: { centre: isFiniteNum(prob) ? prob : null, low: null, high: null, label: v, probability: isFiniteNum(prob) ? prob : null } }
      : { id: String(p.id), inputs: {}, out: { centre: isFiniteNum(v) ? v : null, low: null, high: null } };
  });
}

function v2Dashboard(key: V2Key | "mmm", id: string, name: string, spec: DashboardSpecV2, rt: V2Runtime, created: number, seeds: (rev: DashboardRevision) => V2Seed[], shares: MockShare[] = []): MockDashboard {
  const mv = mvOf(key, spec);
  const rev = revisionOf(id, 1, spec, mv, spec.source.alias ?? null, created);
  const readouts: MockReadout[] = [];
  const batches = new Map<number, BatchLine[]>();
  for (const s of seeds(rev)) {
    readouts.push({ r: header(s.seq, s.patch, mv, s.at), actor: s.actor, readyAt: null, build: null });
    if (s.lines) batches.set(s.seq, s.lines);
  }
  return {
    project: V2_PROJECT,
    d: dashboardRow(id, name, spec, rev, created),
    revisions: [rev],
    readouts,
    grants: [{ user_id: cleo.id, email: cleo.email, display_name: cleo.name, granted_by: ana.id, created_at: iso(created + 3_600_000) }],
    shares,
    refits: [],
    grid: null,
    template: V2_RAW[key as V2Key]?.actuals_template ?? null,
    idem: new Map(),
    v2: rt,
    batches,
  };
}

function shareRow(id: string, token: string, label: string, allowed: DashboardShare["allowed_inputs"], createdAt: string, views = 0): MockShare {
  return {
    token,
    passcode: null,
    failures: 0,
    sessionVersion: 1,
    share: { id, label, display: `mpd_…${token.slice(-4)}`, status: "active", has_passcode: false, allowed_inputs: allowed ?? [], views, last_viewed_at: views ? createdAt : null, locked_until: null, expires_at: iso(Date.now() + 60 * DAY), revoked_at: null, created_by: ana.id, created_at: createdAt },
  };
}

const at = (s: string) => Date.parse(s);

function forecastV2Fixture(): MockDashboard {
  const spec = FORECAST_V2;
  const rt = runtimeOf("forecast");
  const base = rt.base!;
  const seqs = [3, 5, 7];
  return v2Dashboard("forecast", V2_DASHBOARD_IDS.forecast, "harbour-store-visits", spec, rt, at("2026-07-01T09:00:00Z"), () => [
    { seq: 1, at: at("2026-07-01T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(base) } },
    { seq: 2, at: at("2026-08-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Month ending 31/07/2026", snapshot: clone(base) } },
    { seq: 3, at: at("2026-08-03T09:30:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", title: "Numbers sent on 03/08/2026", snapshot: actualsAt(rt, spec, 1, seqs) } },
    { seq: 4, at: at("2026-09-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Month ending 31/08/2026", snapshot: v2From(spec, base, rt.catalog, { accuracy_history: actualsAt(rt, spec, 1, seqs).accuracy_history ?? [] }) } },
    { seq: 5, at: at("2026-09-02T09:30:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", title: "Numbers sent on 02/09/2026", snapshot: actualsAt(rt, spec, 2, seqs) } },
    { seq: 6, at: at("2026-10-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Month ending 30/09/2026", snapshot: v2From(spec, base, rt.catalog, { accuracy_history: actualsAt(rt, spec, 2, seqs).accuracy_history ?? [] }) } },
    { seq: 7, at: at("2026-10-05T09:30:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", title: "Numbers sent on 05/10/2026", snapshot: actualsAt(rt, spec, 3, seqs) } },
    { seq: 8, at: at("2026-10-19T14:00:00Z"), actor: CLEO, patch: { kind: "prediction", trigger: "scenario_rows", visibility: "private", title: "Average price 10", snapshot: clone(rt.predictionBase!) } },
  ], [shareRow("dss_01jaf3w2fcst2share3x4y5z6", MOCK_SHARE_TOKENS.forecastV2, "Store managers", ["actuals", "scenario_rows"], "2026-09-01T08:00:00Z", 9)]);
}

function regressionV2Fixture(): MockDashboard {
  const spec = REGRESSION_V2;
  const rt = runtimeOf("regression");
  const base = rt.base!;
  return v2Dashboard("regression", V2_DASHBOARD_IDS.regression, "harbour-store-demand", spec, rt, at("2026-09-01T09:00:00Z"), () => [
    { seq: 1, at: at("2026-09-01T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(base) } },
    { seq: 2, at: at("2026-10-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Refresh of 01/10/2026", snapshot: clone(base) } },
    { seq: 3, at: at("2026-10-12T10:00:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", visibility: "pending_review", title: "Numbers sent on 12/10/2026", snapshot: actualsAt(rt, spec, 1, [3]) } },
    { seq: 4, at: at("2026-10-19T14:00:00Z"), actor: CLEO, patch: { kind: "prediction", trigger: "scenario_rows", visibility: "private", title: "Price 18, store s0", snapshot: clone(rt.predictionBase!) } },
    { seq: 5, at: at("2026-10-20T08:15:00Z"), actor: CLEO, patch: { kind: "batch", trigger: "batch", visibility: "private", title: "November plan", snapshot: clone(rt.batchBase!) }, lines: linesOf(rt.batchBase, spec) },
  ], [shareRow("dss_01jaf3w2regr2share3x4y5z6", MOCK_SHARE_TOKENS.regression, "Buying team", ["scenario_rows", "batch"], "2026-10-02T08:00:00Z", 2)]);
}

function classifierV2Fixture(): MockDashboard {
  const spec = CLASSIFIER_V2;
  const rt = runtimeOf("classifier");
  const base = rt.base!;
  const kept = clone(rt.predictionBase!);
  return v2Dashboard("classifier", V2_DASHBOARD_IDS.classifier, "harbour-high-demand", spec, rt, at("2026-09-15T09:00:00Z"), () => [
    { seq: 1, at: at("2026-09-15T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(base) } },
    { seq: 2, at: at("2026-10-05T09:30:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", title: "Numbers sent on 05/10/2026", snapshot: actualsAt(rt, spec, 1, [2]) } },
    { seq: 3, at: at("2026-10-19T14:00:00Z"), actor: CLEO, patch: { kind: "prediction", trigger: "scenario_rows", visibility: "private", title: "Two prices for store s0", snapshot: clone(rt.predictionBase!) } },
    { seq: 4, at: at("2026-10-20T08:15:00Z"), actor: CLEO, patch: { kind: "batch", trigger: "batch", visibility: "private", title: "Planned weeks", snapshot: clone(rt.batchBase!) }, lines: linesOf(rt.batchBase, spec) },
    { seq: 5, at: at("2026-10-20T11:00:00Z"), actor: CLEO, patch: { kind: "prediction", trigger: "scenario_rows", visibility: "pending_review", title: "Kept: store s0 on promotion", snapshot: kept } },
  ], [shareRow("dss_01jaf3w2clsf2share3x4y5z6", MOCK_SHARE_TOKENS.classifier, "Store planners", ["actuals", "scenario_rows", "batch"], "2026-09-20T08:00:00Z", 5)]);
}

function causalV2Fixture(): MockDashboard {
  const spec = CAUSAL_V2;
  const rt = runtimeOf("causal");
  const base = rt.base!;
  return v2Dashboard("causal", V2_DASHBOARD_IDS.causal, "harbour-loyalty-effect", spec, rt, at("2026-04-01T09:00:00Z"), () => [
    { seq: 1, at: at("2026-04-01T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(base) } },
    { seq: 2, at: at("2026-07-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Refresh of 01/07/2026", snapshot: clone(base) } },
    { seq: 3, at: at("2026-10-14T10:00:00Z"), actor: CLEO, patch: { kind: "actuals", trigger: "actuals", visibility: "pending_review", title: "Numbers sent on 14/10/2026", snapshot: actualsAt(rt, spec, 1, [3]) } },
  ]);
}

function fieldV2Fixture(): MockDashboard {
  const spec = FIELD_V2;
  const rt = runtimeOf("field");
  return v2Dashboard("field", V2_DASHBOARD_IDS.field, "harbour-incident-map", spec, rt, at("2026-06-01T09:00:00Z"), () => [
    { seq: 1, at: at("2026-06-01T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(rt.base!) } },
    { seq: 2, at: at("2026-10-01T06:00:00Z"), actor: SYSTEM, patch: { kind: "refresh", trigger: "workflow", title: "Quarter ending 30/09/2026", snapshot: clone(rt.base!) } },
  ]);
}

function imageV2Fixture(): MockDashboard {
  const spec = IMAGE_V2;
  const rt = runtimeOf("image");
  return v2Dashboard("image", V2_DASHBOARD_IDS.image, "harbour-shape-check", spec, rt, at("2026-10-01T09:00:00Z"), () => [
    { seq: 1, at: at("2026-10-01T09:30:00Z"), actor: SYSTEM, patch: { kind: "baseline", trigger: "publish", title: "First readout", snapshot: clone(rt.base!) } },
    { seq: 2, at: at("2026-10-16T15:00:00Z"), actor: ANA, patch: { kind: "batch", trigger: "batch", visibility: "private", title: "New product photos", snapshot: clone(rt.batchBase!) }, lines: linesOf(rt.batchBase, spec) },
  ], [shareRow("dss_01jaf3w2imag2share3x4y5z6", MOCK_SHARE_TOKENS.image, "Photo studio", [], "2026-10-02T08:00:00Z", 1)]);
}

/**
 * The weekly MMM dashboard after the automatic upgrade (§10.1): revision 1 is the layout 1
 * spec, revision 2 the upgraded layout 2 one (same model version, same publisher); the old
 * readouts stay layout 1 and the baseline the upgrade queued is layout 2 (predictive bands).
 */
function mmmV2Fixture(): MockDashboard {
  const id = V2_DASHBOARD_IDS.mmm;
  const mv = { id: MV_ID, ref: modelVersion.ref };
  const v1 = { ...clone(MMM_SPEC), title: "Weekly media readout (retail)" };
  const spec: DashboardSpecV2 = { ...clone(MMM_V2), title: v1.title };
  const rev1 = revisionOf(id, 1, v1, mv, integrationsGen.alias, Date.parse("2026-07-27T09:00:00Z"));
  const upgradedAt = Date.parse("2026-10-21T04:00:00Z");
  const rev2 = revisionOf(id, 2, spec, mv, integrationsGen.alias, upgradedAt);
  const readouts: MockReadout[] = [];
  const weeks = 8;
  for (let i = 0; i < weeks; i++) {
    const w = weeks - 1 - i;
    const end = LAST_SUNDAY - w * 7 * DAY;
    readouts.push({
      r: header(i + 1, { kind: i === 0 ? "baseline" : "refresh", trigger: i === 0 ? "publish" : "workflow", title: `Week ending ${new Date(end).toLocaleDateString("en-GB", { timeZone: "UTC" })}`, period: [day(end - 6 * DAY), day(end)], snapshot: mmmSnapshot(v1, MMM_GRID, w) }, mv, end + DAY + 9.5 * 3_600_000),
      actor: SYSTEM,
      readyAt: null,
      build: null,
    });
  }
  readouts.push({
    r: header(weeks + 1, { kind: "baseline", trigger: "publish", title: "First readout of the new layout", period: [day(LAST_SUNDAY - 6 * DAY), day(LAST_SUNDAY)], snapshot: mmmSnapshotV2(spec, MMM_GRID, 0) }, mv, upgradedAt + 120_000),
    actor: SYSTEM,
    readyAt: null,
    build: null,
  });
  return {
    project: V2_PROJECT,
    d: { ...dashboardRow(id, "northwind-weekly-retail", spec, rev2, Date.parse("2026-07-27T09:00:00Z")), updated_at: iso(upgradedAt) },
    revisions: [rev1, rev2],
    readouts,
    grants: [{ user_id: cleo.id, email: cleo.email, display_name: cleo.name, granted_by: ana.id, created_at: "2026-07-27T10:00:00Z" }],
    shares: [shareRow("dss_01jaf3w2mmmx2share3x4y5z6", MOCK_SHARE_TOKENS.mmmUpgraded, "Board pack (retail)", [], "2026-08-01T08:00:00Z", 4)],
    refits: [],
    grid: MMM_GRID,
    template: mmmTemplate(),
    idem: new Map(),
    v2: upgradedRuntime("mmm"),
    batches: new Map(),
  };
}

export function createDashboardsState(members: () => Membership[] = () => []): DashboardsState {
  const all = [
    mmmFixture(),
    forecastFixture(),
    draftFixture(),
    forecastV2Fixture(),
    regressionV2Fixture(),
    classifierV2Fixture(),
    causalV2Fixture(),
    fieldV2Fixture(),
    imageV2Fixture(),
    mmmV2Fixture(),
  ];
  return { dashboards: new Map(all.map((x) => [x.d.id, x])), sessions: new Map(), members };
}

// ---------------------------------------------------------------------------- views

interface Viewer {
  kind: "member" | "client" | "share";
  id: string;
  analyst: boolean;
  /** A share link's allowed inputs (`null`: a signed-in caller). */
  allowed: readonly string[] | null;
  share: MockShare | null;
}

function tick(x: MockDashboard, now = Date.now()): void {
  for (const row of x.readouts) {
    if (row.readyAt === null || now < row.readyAt) {
      if (row.readyAt !== null && row.r.status === "queued" && now >= row.readyAt - readoutMs() / 2) row.r.status = "running";
      continue;
    }
    // Settle first: a build may read the dashboard's other readouts (and tick again).
    const build = row.build;
    row.readyAt = null;
    row.build = null;
    const out = build?.();
    if (out && "problem" in out) Object.assign(row.r, { status: "failed", problem: out.problem });
    else Object.assign(row.r, { status: "ready", snapshot: out?.snapshot ?? null, ready_at: iso(now) });
  }
}

function visible(x: MockDashboard, v: Viewer): MockReadout[] {
  tick(x);
  if (v.analyst) return x.readouts;
  return x.readouts.filter(
    (row) =>
      (row.r.visibility === "published" && row.r.status !== "failed") ||
      (row.actor.kind === v.kind && row.actor.id === v.id && row.r.visibility !== "hidden"),
  );
}

function headerOf(r: Readout): ReadoutHeader {
  const { snapshot: _s, problem: _p, ...h } = r;
  return h;
}

/** The published spec as stored (either layout). */
function storedSpec(x: MockDashboard): AnyDashboardSpec | null {
  return x.d.published_revision?.spec ?? null;
}


function latestPublished(x: MockDashboard): Readout | null {
  tick(x);
  return [...x.readouts].reverse().find((row) => row.r.visibility === "published" && row.r.status === "ready")?.r ?? null;
}

/**
 * `access.effective_inputs`: the published spec's inputs, narrowed by a share link's
 * `allowed_inputs` (`null`: signed in). Batch scoring of an image model is for members only.
 */
function inputsOf(x: MockDashboard, any: AnyDashboardSpec, allowed: readonly string[] | null, client = false): EffectiveInputs {
  const narrow = allowed === null ? null : new Set(allowed);
  const on = (key: string, enabled: boolean) => enabled && (narrow === null || narrow.has(key));
  const spec = v1View(any);
  const whatif = on("whatif", Boolean(spec.whatif?.enabled)) && x.grid !== null;
  const actuals = on("actuals", Boolean(spec.actuals?.enabled)) && x.template !== null;
  const out: EffectiveInputs = {
    whatif,
    exact: whatif && Boolean(spec.whatif?.exact),
    actuals,
    actuals_mode: actuals ? (x.template?.mode ?? "compared") : null,
    refit_requests: actuals && Boolean(spec.actuals?.allow_refit_requests),
  };
  if (!isV2(any)) return out;
  const member = narrow === null && !client;
  const batch = on("batch", Boolean(any.batch?.enabled)) && (any.preset !== "image" || member);
  const kind = any.preset === "classifier" ? "classification" : any.preset === "causal_impact" ? "effect" : "error";
  return { ...out, scenario_rows: on("scenario_rows", Boolean(any.scenario_rows?.enabled)), batch, accuracy_kind: actuals ? kind : null };
}

/** `access._sections`: the layout 2 readout sections the viewer can use. */
function sectionsOf(inputs: EffectiveInputs): SectionKey[] {
  const out: SectionKey[] = ["overview"];
  if (inputs.scenario_rows) out.push("try");
  if (inputs.actuals) out.push("accuracy");
  if (inputs.batch) out.push("batch");
  out.push("history");
  return out;
}

/** `access.upgrade_note`: once, on a layout 2 revision that follows a layout 1 one. */
function upgradeNoteOf(x: MockDashboard, spec: DashboardSpecV2): string | null {
  const rev = x.d.published_revision;
  if (!rev || rev.seq <= 1) return null;
  const prev = x.revisions.find((r) => r.seq === rev.seq - 1);
  if (!prev || isV2(prev.spec)) return null;
  return spec.panels.some((p) => "band" in p && p.band === "outcome") ? `${UPGRADE_NOTE} ${UPGRADE_NOTE_RANGES}`.slice(0, 400) : UPGRADE_NOTE;
}

function clientView(x: MockDashboard, allowed: readonly string[] | null, endsAt: string | null, client = false): ClientDashboard {
  const any = storedSpec(x)!;
  const inputs = inputsOf(x, any, allowed, client);
  if (isV2(any)) return clientViewV2(x, any, inputs, endsAt);
  const spec = v1View(any);
  return {
    id: x.d.id,
    title: spec.title,
    client_name: spec.client_name ?? null,
    summary: spec.summary ?? null,
    note: spec.note ?? null,
    period_label: spec.period_label ?? "week",
    kpi: spec.kpi,
    channels: spec.channels,
    tiles: spec.tiles ?? [],
    tabs: (spec.tabs ?? ["this_period"]).filter((t) => !(t === "scenarios" && !inputs.whatif) && !(t === "update" && !inputs.actuals)),
    inputs,
    live_inputs: x.d.live_inputs ?? "ok",
    show_technical: spec.show_technical ?? false,
    latest_readout: latestPublished(x),
    ends_at: endsAt,
  };
}

/** `access.client_dashboard` of a layout 2 revision. */
function clientViewV2(x: MockDashboard, spec: DashboardSpecV2, inputs: EffectiveInputs, endsAt: string | null): ClientDashboard {
  const planner = spec.preset === "mmm_planner" ? (spec.mmm ?? null) : null;
  const rows = spec.scenario_rows;
  const scenarioForm: ScenarioForm | null = inputs.scenario_rows && rows ? { max_rows: rows.max_rows ?? 5, outputs: rows.outputs ?? [], inputs: rows.inputs ?? [], horizon: rows.horizon ?? null } : null;
  let batchForm: BatchForm | null = null;
  if (inputs.batch && spec.batch) {
    const idc = spec.batch.id_column;
    const drivers = (rows?.inputs ?? []).map((i) => i.name).filter((n) => n !== idc);
    batchForm = { max_rows: spec.batch.max_rows ?? 2000, id_column: idc, columns: [idc, ...drivers].slice(0, 64), outputs: spec.batch.outputs };
  }
  const tabs = (["this_period", "trends", "scenarios", "update", "history"] as const).filter((t) => !(t === "scenarios" && !inputs.whatif) && !(t === "update" && !inputs.actuals));
  return {
    id: x.d.id,
    title: spec.title,
    client_name: spec.client_name ?? null,
    summary: spec.summary ?? null,
    note: spec.note ?? null,
    period_label: spec.period_label ?? "period",
    kpi: kpiOfSubject(spec.subject),
    channels: planner?.channels ?? [],
    tiles: (planner?.tiles ?? []).filter((t) => t !== "panel"),
    tabs: planner ? [...tabs] : [],
    inputs,
    live_inputs: x.d.live_inputs ?? "ok",
    show_technical: spec.show_technical ?? false,
    latest_readout: latestPublished(x),
    ends_at: endsAt,
    layout_version: 2,
    preset: spec.preset,
    subject: spec.subject,
    panels_meta: spec.panels.map(panelMeta),
    scenario_form: scenarioForm,
    batch_form: batchForm,
    sections: sectionsOf(inputs),
    upgrade_note: upgradeNoteOf(x, spec),
  };
}

function analystView(x: MockDashboard): Dashboard {
  tick(x);
  const last = x.readouts[x.readouts.length - 1];
  return { ...x.d, latest_readout: last ? headerOf(last.r) : null };
}

function summaryOf(x: MockDashboard, analyst: boolean): DashboardSummary {
  const spec = storedSpec(x) ?? x.d.draft_spec;
  const latest = analyst ? (x.readouts[x.readouts.length - 1]?.r ?? null) : latestPublished(x);
  return {
    id: x.d.id,
    name: x.d.name,
    title: spec.title,
    client_name: spec.client_name ?? null,
    status: x.d.status,
    live_inputs: x.d.live_inputs ?? "ok",
    period_label: spec.period_label ?? "week",
    updated_at: x.d.updated_at,
    latest_readout: latest ? headerOf(latest) : null,
  };
}

function nextSeq(x: MockDashboard): number {
  return (x.readouts[x.readouts.length - 1]?.r.seq ?? 0) + 1;
}

function touch(x: MockDashboard): void {
  x.d.updated_at = iso(Date.now());
}

// ---------------------------------------------------------------------------- inputs (members, clients and share links)

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function checkMultipliers(spec: DashboardSpec, grid: ResponseGrid, multipliers: Record<string, unknown>): Response | null {
  for (const [name, m] of Object.entries(multipliers)) {
    const ch = spec.channels.find((c) => c.name === name);
    if (!ch || !grid.channels.includes(name)) return problem(422, "SCENARIO_OUT_OF_RANGE", "Scenario out of range", `the dashboard has no channel named ${name}`, { errors: [{ code: "SCENARIO_OUT_OF_RANGE", severity: "error", pointer: `/multipliers/${name}`, message: "unknown channel" }] });
    const lo = Math.max(ch.min_mult ?? 0.5, grid.multipliers[0] ?? 0);
    const hi = Math.min(ch.max_mult ?? 1.5, grid.multipliers[grid.multipliers.length - 1] ?? 1);
    if (!isNum(m) || m < lo - 1e-9 || m > hi + 1e-9) {
      return problem(422, "SCENARIO_OUT_OF_RANGE", "Scenario out of range", `the multiplier of ${ch.label} must be from ${lo} to ${hi}`, { errors: [{ code: "SCENARIO_OUT_OF_RANGE", severity: "error", pointer: `/multipliers/${name}`, message: `must be from ${lo} to ${hi}` }] });
    }
  }
  return null;
}

function disabled(what: string): Response {
  return problem(409, "DASHBOARD_INPUT_DISABLED", "This input is not available on the dashboard", `${what} is not available on this dashboard`);
}

function paused(x: MockDashboard): Response | null {
  return x.d.live_inputs === "paused" ? problem(409, "DASHBOARD_LIVE_PAUSED", "New results are paused on this dashboard", x.d.problem ?? "ask the analyst to check the dashboard") : null;
}

interface InputCtx {
  x: MockDashboard;
  v: Viewer;
  /** The published spec as the layout 1 code reads it. */
  spec: DashboardSpec;
  /** The published spec as stored (either layout). */
  any: AnyDashboardSpec;
  inputs: EffectiveInputs;
}

function grid(c: InputCtx): Response {
  if (!c.x.grid || !c.inputs.whatif) return disabled("The what-if");
  return HttpResponse.json(c.x.grid);
}

async function evaluate(c: InputCtx, request: Request): Promise<Response> {
  if (!c.x.grid || !c.inputs.whatif) return disabled("The what-if");
  const body = (await request.json()) as { multipliers?: Record<string, number> };
  const multipliers = body.multipliers ?? {};
  const bad = checkMultipliers(c.spec, c.x.grid, multipliers);
  if (bad) return bad;
  await delay(80);
  return HttpResponse.json(whatIfOf(c.spec, c.x.grid, multipliers));
}

function replay(c: InputCtx, request: Request, path: string): Readout | null {
  const key = request.headers.get("Idempotency-Key");
  if (!key) return null;
  const seq = c.x.idem.get(`${c.v.kind}:${c.v.id}:${path}:${key}`);
  return seq ? (c.x.readouts.find((r) => r.r.seq === seq)?.r ?? null) : null;
}

function remember(c: InputCtx, request: Request, path: string, seq: number): void {
  const key = request.headers.get("Idempotency-Key");
  if (key) c.x.idem.set(`${c.v.kind}:${c.v.id}:${path}:${key}`, seq);
}

function waitSeconds(request: Request): number {
  const m = /wait=(\d+)/.exec(request.headers.get("Prefer") ?? "");
  return m ? Math.min(30, Number(m[1])) : 0;
}

async function answerQueued(c: InputCtx, row: MockReadout, request: Request): Promise<Response> {
  const wait = waitSeconds(request) * 1000;
  if (wait > 0 && row.readyAt !== null && row.readyAt - Date.now() <= wait) {
    await delay(Math.max(0, row.readyAt - Date.now()));
    tick(c.x);
    return HttpResponse.json(row.r);
  }
  return HttpResponse.json(headerOf(row.r), { status: 202, headers: { Location: `/v1/dashboards/${c.x.d.id}/readouts/${row.r.seq}` } });
}

async function saveScenario(c: InputCtx, request: Request, path: string): Promise<Response> {
  if (!c.x.grid || !c.inputs.whatif) return disabled("The what-if");
  const body = (await request.json()) as ScenarioSave;
  const multipliers = body.multipliers ?? {};
  const bad = checkMultipliers(c.spec, c.x.grid, multipliers);
  if (bad) return bad;
  if (body.exact && !c.inputs.exact) return disabled("The exact result");
  const stop = paused(c.x);
  if (body.exact && stop) return stop;
  const earlier = replay(c, request, path);
  if (earlier) return earlier.status === "ready" ? HttpResponse.json(earlier) : HttpResponse.json(headerOf(earlier), { status: 202 });
  const limited = body.exact ? runLimit(c) : null;
  if (limited) return limited;
  const seq = nextSeq(c.x);
  const spec = c.spec;
  const g = c.x.grid;
  const now = Date.now();
  const r: Readout = {
    seq,
    kind: "scenario",
    trigger: "whatif",
    visibility: "private",
    status: body.exact ? "queued" : "ready",
    title: body.label ?? (body.exact ? "Exact result of a plan" : "Saved plan"),
    period: null,
    model_version_id: c.x.d.published_revision?.model_version_id ?? MV_ID,
    model_ref: c.x.d.published_revision?.model_ref ?? modelVersion.ref,
    endpoint_revision: c.x.d.published_revision?.endpoint_revision ?? null,
    created_at: iso(now),
    ready_at: body.exact ? null : iso(now),
    refit_status: null,
    snapshot: body.exact ? null : scenarioSnapshot(spec, g, multipliers),
    problem: null,
  };
  const row: MockReadout = {
    r,
    actor: { kind: c.v.kind, id: c.v.id },
    readyAt: body.exact ? now + readoutMs() : null,
    spent: Boolean(body.exact),
    build: body.exact ? () => ({ snapshot: { ...scenarioSnapshot(spec, g, multipliers), notes_plain: ["This is the exact result: the model calculated the plan with a new run."] } }) : null,
  };
  c.x.readouts.push(row);
  remember(c, request, path, seq);
  return body.exact ? answerQueued(c, row, request) : HttpResponse.json(r);
}

function template(c: InputCtx): Response {
  if (!c.x.template || !c.inputs.actuals) return disabled("Sending numbers");
  return HttpResponse.json(c.x.template);
}

interface XrVar {
  dims?: string[];
  data?: unknown;
}

function actualsOf(body: ActualsSubmit, t: ActualsTemplate): { values: (number | null)[]; periods: string[] } | Response {
  const vars = (body.inputs?.data_vars ?? {}) as Record<string, XrVar>;
  const coords = (body.inputs?.coords ?? {}) as Record<string, XrVar>;
  const kpi = vars[t.kpi_column];
  const pointer = `/inputs/data_vars/${t.kpi_column}`;
  if (!kpi || !Array.isArray(kpi.data)) {
    return problem(422, "ACTUALS_INVALID", "The numbers cannot be used", `the column ${t.kpi_column} is missing`, { errors: [{ code: "ACTUALS_INVALID", severity: "error", pointer, message: `add the column ${t.kpi_column}` }] });
  }
  const values = (kpi.data as unknown[]).map((v) => (isNum(v) ? v : null));
  if (values.length > (t.max_rows ?? 520)) return problem(413, "ACTUALS_INVALID", "The numbers cannot be used", `at most ${t.max_rows ?? 520} rows`);
  if (!values.some((v) => v !== null)) {
    return problem(422, "ACTUALS_INVALID", "The numbers cannot be used", `the column ${t.kpi_column} has no numbers`, { errors: [{ code: "ACTUALS_INVALID", severity: "error", pointer, message: "enter at least one number" }] });
  }
  if (values.some((v) => v !== null && v < 0)) {
    return problem(422, "ACTUALS_INVALID", "The numbers cannot be used", `the column ${t.kpi_column} has a negative number`, { errors: [{ code: "ACTUALS_INVALID", severity: "error", pointer, message: "numbers must be 0 or more" }] });
  }
  const dim = t.input_table.horizon_dim ?? "date";
  const periods = (Array.isArray(coords[dim]?.data) ? (coords[dim].data as unknown[]) : values.map((_, i) => String(i + 1))).map(String);
  return { values, periods };
}

function actualsSnapshot(x: MockDashboard, spec: DashboardSpec, values: (number | null)[], periods: string[]): ReadoutSnapshot {
  if (x.template?.mode === "conditioned") {
    return forecastSnapshot(spec, 0, values.filter((v): v is number => v !== null));
  }
  const base = latestPublished(x)?.snapshot ?? mmmSnapshot(spec, x.grid ?? MMM_GRID, 0);
  const band = base.series?.[0]?.points ?? [];
  const points = values.map((a, i) => {
    const ref = band[band.length - values.length + i] ?? band[band.length - 1];
    const m = ref?.mean ?? 400_000;
    return { t: periods[i] ?? String(i + 1), mean: m, low: ref?.low ?? m * 0.93, high: ref?.high ?? m * 1.07, actual: a };
  });
  const acc = metricsOf(points.map((p) => p.mean), points.map((p) => p.actual), points.map((p) => p.low), points.map((p) => p.high));
  const sent = points.filter((p) => p.actual !== null);
  const totalA = sent.reduce((a, p) => a + (p.actual ?? 0), 0);
  const totalM = sent.reduce((a, p) => a + p.mean, 0);
  const vs = totalM ? (totalA - totalM) / totalM : 0;
  const n = noun(spec);
  const tiles: Partial<Record<KpiValue["key"], KpiValue>> = {
    vs_expected: { key: "vs_expected", label: "Compared with the model", value: vs * 100, unit: "percent", delta: vs, delta_basis: "the model's expectation", delta_dir: dir(vs), low: null, high: null, caption: `${fmt(totalA)} ${n} against ${fmt(totalM)} expected` },
    accuracy: { key: "accuracy", label: "Prediction error", value: acc.mape ?? null, unit: "percent", delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: `Average error over ${acc.n} ${spec.period_label ?? "week"}s` },
  };
  for (const t of base.tiles ?? []) if (!tiles[t.key]) tiles[t.key] = t;
  return {
    ...base,
    lead: `Your numbers were ${fmt(Math.abs(vs) * 100)}% ${vs >= 0 ? "more" : "less"} than the model expected over ${sent.length} ${spec.period_label ?? "week"}${sent.length === 1 ? "" : "s"}. The model does not change.`,
    tiles: pick(spec, tiles),
    series: [{ name: "kpi", label: spec.kpi.label, points }],
    accuracy: acc,
    input_fingerprint: `sha256:${hash(JSON.stringify(values))}`,
    notes_plain: [],
  };
}

/** A readout an input starts (queued: it becomes ready after {@link readoutMs}). */
function newReadout(x: MockDashboard, seq: number, patch: Partial<Readout> & Pick<Readout, "kind" | "trigger" | "title" | "visibility">, now: number): Readout {
  return {
    seq,
    status: "queued",
    period: null,
    model_version_id: x.d.published_revision?.model_version_id ?? MV_ID,
    model_ref: x.d.published_revision?.model_ref ?? modelVersion.ref,
    endpoint_revision: x.d.published_revision?.endpoint_revision ?? null,
    created_at: iso(now),
    ready_at: null,
    refit_status: null,
    snapshot: null,
    problem: null,
    ...patch,
  };
}

/**
 * `readouts._limits_check`: every run an input spends counts against `exact_runs_per_day`
 * (uploads too: probe §6), a scored file also against `batch_runs_per_day`.
 */
function runLimit(c: InputCtx, batch = false): Response | null {
  const now = Date.now();
  const recent = c.x.readouts.filter((r) => r.spent && now - Date.parse(r.r.created_at) < DAY);
  const cap = c.spec.limits?.exact_runs_per_day ?? 20;
  const batchCap = isV2(c.any) ? (c.any.limits?.batch_runs_per_day ?? 3) : 0;
  const over = recent.length >= cap || (batch && recent.filter((r) => r.r.kind === "batch").length >= batchCap);
  return over
    ? problem(429, "DASHBOARD_RUN_LIMIT", "The dashboard used its runs for today", batch && recent.length < cap ? `at most ${batchCap} scored files a day: try again tomorrow` : `at most ${cap} new results a day: try again tomorrow`, { headers: { "Retry-After": "60" } })
    : null;
}

/** An upload asks for a refit: at most one open request per dashboard (a newer one supersedes the older). */
function openRefit(c: InputCtx, r: Readout, now: number): void {
  const x = c.x;
  for (const old of x.refits) {
    if (old.status !== "open") continue;
    old.status = "superseded";
    const linked = x.readouts.find((row) => row.r.seq === old.readout_seq);
    if (linked) linked.r.refit_status = "superseded";
  }
  x.refits.unshift({
    id: `rfr_01jaf3w1${randomB64(16).toLowerCase().replace(/[^a-z0-9]/g, "x")}`,
    dashboard_id: x.d.id,
    readout_seq: r.seq,
    status: "open",
    requested_by_kind: c.v.kind,
    requested_by: c.v.id,
    created_at: iso(now),
    decided_by: null,
    decided_at: null,
    dataset_version_id: null,
    workflow_run_id: null,
    note: null,
    problem: null,
  });
  r.refit_status = "open";
}

async function submitActuals(c: InputCtx, request: Request, path: string): Promise<Response> {
  if (!c.x.template || !c.inputs.actuals) return disabled("Sending numbers");
  if (isV2(c.any) && c.any.preset !== "mmm_planner") return submitActualsV2(c, c.any, request, path);
  const stop = paused(c.x);
  if (stop) return stop;
  const body = (await request.json()) as ActualsSubmit;
  if (body.request_refit && !c.inputs.refit_requests) return disabled("Asking for a refit");
  const earlier = replay(c, request, path);
  if (earlier) return HttpResponse.json(headerOf(earlier), { status: 202 });
  const parsed = actualsOf(body, c.x.template);
  if (parsed instanceof Response) return parsed;
  const limited = runLimit(c);
  if (limited) return limited;
  const seq = nextSeq(c.x);
  const now = Date.now();
  const spec = c.spec;
  const any = c.any;
  const x = c.x;
  const r = newReadout(x, seq, {
    kind: "actuals",
    trigger: "actuals",
    visibility: spec.actuals?.publish === "auto" ? "published" : "pending_review",
    title: body.label ?? `Numbers sent on ${new Date(now).toLocaleDateString("en-GB")}`,
    period: parsed.periods.length ? [parsed.periods[0]!.slice(0, 10), parsed.periods[parsed.periods.length - 1]!.slice(0, 10)] : null,
  }, now);
  // the upgraded planner compares as layout 1 did, in a layout 2 snapshot
  const build = () => {
    const v1 = actualsSnapshot(x, spec, parsed.values, parsed.periods);
    return { snapshot: isV2(any) ? { ...legacyV2(any, v1), accuracy: v1.accuracy ?? null } : v1 };
  };
  x.readouts.push({ r, actor: { kind: c.v.kind, id: c.v.id }, readyAt: now + readoutMs(), build, spent: true });
  if (body.request_refit) openRefit(c, r, now);
  remember(c, request, path, seq);
  return HttpResponse.json(headerOf(r), { status: 202, headers: { Location: `/v1/dashboards/${x.d.id}/readouts/${seq}` } });
}

// ---------------------------------------------------------------------------- inputs of layout 2 (v0.17)

function actualsProblem(message: string, pointer = "/inputs"): Response {
  return problem(422, "ACTUALS_INVALID", "The numbers cannot be used", message, { errors: [{ code: "ACTUALS_INVALID", severity: "error", pointer, message }] });
}

/** The fan of the subject in a snapshot (time actuals are compared with it). */
function fanPoints(snap: ReadoutSnapshot | null | undefined): Map<string, { mean: number; low: number | null; high: number | null }> {
  const out = new Map<string, { mean: number; low: number | null; high: number | null }>();
  const fan = snap?.panels?.find((b) => b.kind === "series" && b.intent === "fan");
  const points = fan?.kind === "series" ? (fan.series?.[0]?.points ?? []) : (snap?.series?.[0]?.points ?? []);
  for (const p of points) if (isFiniteNum(p.mean)) out.set(p.t.slice(0, 10), { mean: p.mean, low: p.low ?? null, high: p.high ?? null });
  return out;
}

/**
 * `actuals_v2.submit_actuals_v2`: per period (`match=time`: compared with the forecast),
 * per case (`match=row`: each row scored as a case, joined on the id column) or the updated
 * effect of a causal model (Σ actual − the forecast without the change, with its range).
 */
async function submitActualsV2(c: InputCtx, spec: DashboardSpecV2, request: Request, path: string): Promise<Response> {
  const x = c.x;
  const t = x.template!;
  const rt = x.v2;
  if (!rt?.base) return disabled("Sending numbers");
  const stop = paused(x);
  if (stop) return stop;
  const body = (await request.json()) as ActualsSubmit;
  if (body.request_refit && !c.inputs.refit_requests) return disabled("Asking for a refit");
  const earlier = replay(c, request, path);
  if (earlier) return HttpResponse.json(headerOf(earlier), { status: 202 });
  const outcome = t.label_column ?? t.kpi_column;
  const idc = t.match === "row" ? (t.id_column ?? null) : null;
  const tab = tableRowsOf(body.inputs, idc);
  if (!("rows" in tab)) return actualsProblem(tab.message ?? "the numbers cannot be used", tab.pointer ?? "/inputs");
  const pointer = `/inputs/data_vars/${outcome}`;
  if (!tab.columns.includes(outcome)) return actualsProblem(`the column ${outcome} is missing`, pointer);
  if (idc && !tab.columns.includes(idc)) return actualsProblem(`the column ${idc} is missing`, `/inputs/data_vars/${idc}`);
  const max = t.max_rows ?? (t.match === "row" ? 5000 : 520);
  if (tab.rows.length > max) return problem(413, "ACTUALS_INVALID", "The numbers cannot be used", `at most ${max} rows`);
  const given = tab.rows.filter((r) => r[outcome] !== null && r[outcome] !== undefined && r[outcome] !== "");
  if (!given.length) return actualsProblem(`the column ${outcome} has no values`, pointer);
  const classifier = spec.preset === "classifier";
  if (!classifier && given.some((r) => !Number.isFinite(numOr(r[outcome], NaN)))) return actualsProblem(`the column ${outcome} must hold numbers`, pointer);
  if (idc && new Set(given.map((r) => String(r[idc]))).size !== given.length) return actualsProblem(`each ${idc} may appear once`, `/inputs/data_vars/${idc}`);
  let detail: AccuracyDetail;
  const pairs: { p: number; a: number; lo: number | null; hi: number | null }[] = [];
  let periods: string[] = [];
  if (spec.preset === "causal_impact") {
    const dim = t.input_table.horizon_dim ?? "time";
    periods = given.map((r) => textOf(r[dim]).slice(0, 10)).filter(Boolean).sort();
    const total = given.reduce((s, r) => s + numOr(r[outcome], 0), 0);
    const cf = V2_ANCHORS.causal_cf_total;
    const effect = total - cf.mean;
    detail = { kind: "effect", n_periods: given.length, total: effect, low: total - (cf.quantiles["0.95"] ?? cf.mean), high: total - (cf.quantiles["0.05"] ?? cf.mean), relative: (100 * effect) / cf.mean, note: "Your results minus the forecast without the change." };
  } else if (t.match === "row") {
    if (!rt.scorer) return disabled("Sending numbers");
    if (classifier) {
      const positive = spec.subject.positive_label ?? "1";
      const labels = new Set(given.map((r) => looseLabel(r[outcome])));
      if (labels.size > 2) return actualsProblem(`the column ${outcome} holds more than two labels`, pointer);
      const ys = given.map((r) => (looseLabel(r[outcome]) === looseLabel(positive) || r[outcome] === true ? 1 : 0));
      const ps = given.map((r) => rt.scorer!(caseOf(spec, r)).centre ?? 0);
      detail = classAccuracyOf(ps, ys, spec.actuals?.threshold ?? 0.5, positive, spec.subject.positive_label ? "other" : "0");
    } else {
      for (const r of given) {
        const s = rt.scorer(caseOf(spec, r));
        if (isFiniteNum(s.centre)) pairs.push({ p: s.centre, a: numOr(r[outcome], 0), lo: s.low, hi: s.high });
      }
      detail = errorAccuracyOf(pairs);
    }
  } else {
    const dim = t.input_table.horizon_dim ?? "t";
    const fan = fanPoints(latestPublished(x)?.snapshot ?? rt.base);
    for (const r of given) {
      const key = textOf(r[dim]).slice(0, 10);
      const f = fan.get(key);
      if (!f) continue;
      periods.push(key);
      pairs.push({ p: f.mean, a: numOr(r[outcome], 0), lo: f.low, hi: f.high });
    }
    if (!pairs.length) return actualsProblem("None of the results you sent match a prediction of the model.", `/inputs/coords/${dim}`);
    detail = errorAccuracyOf(pairs);
  }
  const limited = runLimit(c);
  if (limited) return limited;
  const seq = nextSeq(x);
  const now = Date.now();
  periods.sort();
  const period: [string, string] | null = periods.length ? [periods[0]!, periods[periods.length - 1]!] : null;
  const r = newReadout(x, seq, {
    kind: "actuals",
    trigger: "actuals",
    visibility: spec.actuals?.publish === "auto" ? "published" : "pending_review",
    title: body.label ?? `Numbers sent on ${new Date(now).toLocaleDateString("en-GB")}`,
    period,
  }, now);
  const fingerprint = `sha256:${hash(JSON.stringify(body.inputs ?? null))}`;
  const build = () => {
    const history = [...lastHistory(x).filter((h) => h.seq !== seq), accuracyPoint(seq, iso(now), period, detail)].slice(-104);
    const snap = actualsSnapshotV2(rt, spec, detail, pairs, history, fingerprint);
    snap.notes_plain = [spec.preset === "causal_impact" ? "Your results were compared with the forecast without the change; the model does not change." : "Your numbers were compared with the model's predictions; the model does not change."];
    return { snapshot: snap };
  };
  x.readouts.push({ r, actor: { kind: c.v.kind, id: c.v.id }, readyAt: now + readoutMs(), build, spent: true });
  if (body.request_refit) openRefit(c, r, now);
  remember(c, request, path, seq);
  return HttpResponse.json(headerOf(r), { status: 202, headers: { Location: `/v1/dashboards/${x.d.id}/readouts/${seq}` } });
}

/** 202 with the full readout of a tried case or a scored file (or the ready one within `Prefer: wait=N`). */
async function answerReadout(c: InputCtx, row: MockReadout, request: Request): Promise<Response> {
  const wait = waitSeconds(request) * 1000;
  if (wait > 0 && row.readyAt !== null && row.readyAt - Date.now() <= wait) {
    await delay(Math.max(0, row.readyAt - Date.now()));
    tick(c.x);
    return HttpResponse.json(row.r);
  }
  return HttpResponse.json(row.r, { status: 202, headers: { Location: `/v1/dashboards/${c.x.d.id}/readouts/${row.r.seq}` } });
}

/** `cases.submit_scenario_rows` ("Try a case"): checked against the published defs, never clipped. */
async function predict(c: InputCtx, request: Request, path: string): Promise<Response> {
  const spec = c.any;
  if (!isV2(spec) || !c.inputs.scenario_rows || !c.x.v2?.scorer || !spec.scenario_rows) return disabled("Trying a case");
  const stop = paused(c.x);
  if (stop) return stop;
  const body = (await request.json().catch(() => null)) as ScenarioRowsRequest | null;
  if (!body || !Array.isArray(body.rows) || body.rows.length > 20 || (body.label != null && (typeof body.label !== "string" || body.label.length > 80))) {
    return problem(422, "SCHEMA_INVALID", "Invalid request", "send 1 to 20 rows and a label of at most 80 characters", { errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/rows", message: "1 to 20 rows" }] });
  }
  const issues = checkRows(body.rows, spec.scenario_rows);
  if (issues.length) return problem(422, "DASHBOARD_INPUT_INVALID", "Some values cannot be used", issues[0]!.message ?? "check the values", { errors: issues });
  const earlier = replay(c, request, path);
  if (earlier) return HttpResponse.json(earlier, { status: 202 });
  const limited = runLimit(c);
  if (limited) return limited;
  const rows = body.rows.map((r) => resolveRow(r as Record<string, unknown>, spec.scenario_rows!));
  const now = Date.now();
  const seq = nextSeq(c.x);
  const first = body.rows[0] ?? {};
  const title = body.label ?? (Object.entries(first).filter(([, v]) => v !== null).slice(0, 3).map(([k, v]) => `${(spec.scenario_rows!.inputs ?? []).find((i) => i.name === k)?.label ?? k} ${String(v)}`).join(", ") || "Tried case");
  const r = newReadout(c.x, seq, { kind: "prediction", trigger: "scenario_rows", visibility: "private", title: title.slice(0, 120) }, now);
  const x = c.x;
  const row: MockReadout = { r, actor: { kind: c.v.kind, id: c.v.id }, readyAt: now + readoutMs(), build: () => ({ snapshot: predictionSnapshot(x, spec, rows, body.rows) }), spent: true };
  x.readouts.push(row);
  remember(c, request, path, seq);
  return answerReadout(c, row, request);
}

function batchTemplate(c: InputCtx): Response {
  if (!c.inputs.batch || !c.x.v2?.batchTemplate) return disabled("Scoring a file");
  return HttpResponse.json(c.x.v2.batchTemplate);
}

/** A file cell as its column's type reads it (CSV text parsed in the browser may still be text). */
function cellValue(v: unknown, d: ScenarioInputDef | null): unknown {
  if (d === null || v === null || v === undefined) return v;
  if ((d.type === "number" || d.type === "integer") && typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  if (d.type === "toggle" && typeof v === "string") return v === "true" ? true : v === "false" ? false : v;
  return v;
}

/** `cases.submit_batch` ("Score a file"): the columns of the template, the ranges, the caps. */
async function submitBatch(c: InputCtx, request: Request, path: string): Promise<Response> {
  const spec = c.any;
  const rt = c.x.v2;
  if (!isV2(spec) || !spec.batch || !c.inputs.batch || !rt?.scorer || !rt.batchTemplate) return disabled("Scoring a file");
  const stop = paused(c.x);
  if (stop) return stop;
  const body = (await request.json().catch(() => null)) as BatchSubmit | null;
  const earlier = replay(c, request, path);
  if (earlier) return HttpResponse.json(earlier, { status: 202 });
  const t = rt.batchTemplate;
  const idc = t.id_column;
  const tab = tableRowsOf(body?.inputs, idc);
  if (!("rows" in tab)) return problem(422, "DASHBOARD_INPUT_INVALID", "The file cannot be used", tab.message ?? "check the file", { errors: [tab] });
  const max = spec.batch.max_rows ?? 2000;
  if (tab.rows.length > max) return problem(409, "DASHBOARD_BATCH_TOO_LARGE", "The file has too many rows", `send at most ${max} rows (got ${tab.rows.length})`);
  const known = new Set(t.columns.map((col) => col.name));
  const issues: Issue[] = [];
  for (const col of tab.columns) if (!known.has(col)) issues.push(inputIssue(`/inputs/data_vars/${col}`, `the file has a column ${col} that the model does not use`));
  for (const col of t.columns) if ((col.required ?? true) && !tab.columns.includes(col.name)) issues.push(inputIssue(`/inputs/data_vars/${col.name}`, `the column ${col.name} is missing`));
  const ids = tab.rows.map((r) => textOf(r[idc]));
  if (ids.some((v) => !v)) issues.push(inputIssue(`/inputs/coords/${idc}`, `every row needs a ${idc}`));
  else if (new Set(ids).size !== ids.length) issues.push(inputIssue(`/inputs/coords/${idc}`, `each ${idc} may appear once`));
  const rows = tab.rows.map((r) => Object.fromEntries(Object.entries(r).map(([k, v]) => [k, cellValue(v, inputForColumn(spec, k))])));
  if (spec.batch.enforce_ranges ?? true) {
    rows.forEach((r, i) => {
      for (const [col, v] of Object.entries(r)) {
        const d = col === idc ? null : inputForColumn(spec, col);
        if (!d || v === null || v === undefined || v === "") continue;
        const why = checkValue(v, d);
        if (why && issues.length < 50) issues.push(inputIssue(`/inputs/data_vars/${col}/data/${i}`, `row ${i + 1}: ${why}`));
      }
    });
  }
  if (issues.length) return problem(422, "DASHBOARD_INPUT_INVALID", "The file cannot be used", issues[0]!.message ?? "check the file", { errors: issues.slice(0, 50) });
  const limited = runLimit(c, true);
  if (limited) return limited;
  const lines: BatchLine[] = rows.map((r) => ({ id: String(r[idc]), inputs: Object.fromEntries(Object.entries(r).filter(([k]) => k !== idc)), out: rt.scorer!(caseOf(spec, r)) }));
  const now = Date.now();
  const seq = nextSeq(c.x);
  const r = newReadout(c.x, seq, { kind: "batch", trigger: "batch", visibility: "private", title: (body?.label ?? `File of ${rows.length} cases`).slice(0, 120) }, now);
  const base = rt.batchBase ?? rt.base!;
  const row: MockReadout = {
    r,
    actor: { kind: c.v.kind, id: c.v.id },
    readyAt: now + readoutMs(),
    build: () => ({ snapshot: v2From(spec, base, rt.catalog, { batch: batchSummaryOf(spec, lines, rows.length), lead: rt.base?.lead ?? base.lead, inputs_echo: [], input_fingerprint: `sha256:${hash(JSON.stringify(rows))}`, source_run_ids: [`run_01jaf3q2x8m9v7k5h4g3f2c${String(seq).padStart(3, "0")}`] }) }),
    spent: true,
  };
  c.x.readouts.push(row);
  c.x.batches.set(seq, lines);
  remember(c, request, path, seq);
  return answerReadout(c, row, request);
}

/** `downloads.predictions_csv`: the owner's (or an editor's) ready batch readout; 410 once the file is gone. */
function downloadPredictions(c: InputCtx, seq: number, editor: boolean): Response {
  const spec = c.any;
  const row = visible(c.x, c.v).find((r) => r.r.seq === seq);
  const own = !!row && row.actor.kind === c.v.kind && row.actor.id === c.v.id;
  if (!row || row.r.kind !== "batch" || !(own || editor) || row.r.status !== "ready" || !isV2(spec)) return NOT_FOUND("readout");
  const lines = c.x.batches.get(seq);
  if (!lines || row.r.snapshot?.batch?.download?.available === false) return problem(410, "DASHBOARD_DOWNLOAD_GONE", "The predictions are no longer available", "the predictions of this readout are no longer available: score the file again");
  const idc = c.x.v2?.batchTemplate?.id_column ?? spec.batch?.id_column ?? "id";
  const columns = (c.x.v2?.batchTemplate?.columns ?? []).map((col) => col.name).filter((n) => n !== idc);
  const text = predictionsCsv(spec, idc, columns, lines);
  return new HttpResponse(text, {
    headers: { "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="predictions-${seq}.csv"`, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}

/** A picture (PNG): the shapes of the test images, the misread ones framed in red. */
function picturePng(assetId: string, width: number, height: number): Uint8Array {
  const rgb = new Uint8Array(width * height * 3).fill(255);
  const cols = 6;
  const rows = 4;
  const cw = Math.floor(width / cols);
  const ch = Math.floor(height / rows);
  const rand = seeded(Number.parseInt(hash(assetId).slice(0, 8), 16));
  for (let k = 0; k < cols * rows; k++) {
    const x0 = (k % cols) * cw;
    const y0 = Math.floor(k / cols) * ch;
    const shape = Math.floor(rand() * 3);
    const wrong = rand() < 0.3;
    for (let y = 0; y < ch; y++) {
      for (let x = 0; x < cw; x++) {
        const u = (x - cw / 2) / (cw / 3);
        const v = (y - ch / 2) / (ch / 3);
        const inside = shape === 0 ? u * u + v * v < 1 : shape === 1 ? Math.abs(u) < 0.8 && Math.abs(v) < 0.8 : Math.abs(u) < 0.25 || Math.abs(v) < 0.25;
        const border = x < 3 || y < 3 || x >= cw - 3 || y >= ch - 3;
        const i = ((y0 + y) * width + x0 + x) * 3;
        const [r, g, b] = border ? (wrong ? [200, 40, 40] : [230, 230, 235]) : inside && Math.max(Math.abs(u), Math.abs(v)) < 1.2 ? [40, 60, 90] : [250, 250, 252];
        rgb[i] = r!;
        rgb[i + 1] = g!;
        rgb[i + 2] = b!;
      }
    }
  }
  return encodeRgbPng(width, height, rgb);
}

/** `downloads.asset_bytes`: a picture of the published revision (`404` for any other). */
function asset(c: InputCtx, assetId: string): Response {
  const a = c.x.v2?.assets.get(assetId);
  if (!a) return NOT_FOUND("picture");
  return new HttpResponse(picturePng(assetId, a.width, a.height), { headers: { "Content-Type": "image/png", "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300" } });
}

/** `cases.keep_prediction`: the caller's own tried case (an editor: anybody's) waits for review. */
function keep(x: MockDashboard, v: Viewer, seq: number, editor: boolean): Response {
  const row = visible(x, v).find((r) => r.r.seq === seq);
  const own = !!row && row.actor.kind === v.kind && row.actor.id === v.id;
  if (!row || !(own || editor)) return NOT_FOUND("readout");
  if (row.r.kind !== "prediction") return problem(409, "CONFLICT", "Conflict", "only a tried case (a prediction readout) can be kept");
  if (row.r.visibility === "pending_review" || row.r.visibility === "published") return HttpResponse.json(headerOf(row.r));
  if (row.r.visibility === "hidden") return problem(409, "CONFLICT", "Conflict", "an editor hid this readout: it cannot be kept");
  if (row.r.status === "failed") return problem(409, "CONFLICT", "Conflict", "a failed readout cannot be kept");
  row.r.visibility = "pending_review";
  touch(x);
  return HttpResponse.json(headerOf(row.r));
}

function listReadouts(x: MockDashboard, v: Viewer, url: URL): Response {
  const kind = url.searchParams.get("kind");
  const visibility = v.analyst ? url.searchParams.get("visibility") : null;
  const before = Number(url.searchParams.get("before_seq") ?? "") || null;
  const limit = Math.max(1, Math.min(50, Number(url.searchParams.get("limit") ?? 20) || 20));
  const rows = visible(x, v)
    .filter((row) => (!kind || row.r.kind === kind) && (!visibility || row.r.visibility === visibility) && (before === null || row.r.seq < before))
    .map((row) => row.r)
    .sort((a, b) => b.seq - a.seq);
  const items = rows.slice(0, limit);
  return HttpResponse.json({ items: items.map(headerOf), next_before_seq: rows.length > limit ? (items[items.length - 1]?.seq ?? null) : null });
}

function getReadout(x: MockDashboard, v: Viewer, seq: number): Response {
  const row = visible(x, v).find((r) => r.r.seq === seq);
  return row ? HttpResponse.json(row.r) : NOT_FOUND("readout");
}

// ---------------------------------------------------------------------------- handlers

const RANK: ProjectRole[] = ["client", "viewer", "editor", "runner", "admin"];
const atLeast = (r: ProjectRole | null, min: ProjectRole) => r !== null && RANK.indexOf(r) >= RANK.indexOf(min);

export function createDashboardHandlers(state: DashboardsState): HttpHandler[] {
  const D = `${API}/v1/dashboards`;

  /** The caller's dashboard: 404 for a client without a grant or on an unpublished one. */
  function signedIn(request: Request, id: string | null): { x: MockDashboard | null; v: Viewer; role: ProjectRole } | Response {
    const p = mockPersona();
    if (!p) return problem(401, "UNAUTHENTICATED", "Authentication required", "sign in to continue");
    const key = request.headers.get("X-Mapi-Project") ?? PROJECT;
    const role = mockProjectRole(key);
    if (!role) return NOT_FOUND("project");
    const v: Viewer = { kind: role === "client" ? "client" : "member", id: p.id, analyst: role !== "client", allowed: null, share: null };
    if (id === null) return { x: null, v, role };
    const x = state.dashboards.get(id);
    if (!x || x.project !== key) return NOT_FOUND();
    if (role === "client" && (x.d.status !== "published" || !x.grants.some((g) => g.user_id === p.id))) return NOT_FOUND();
    return { x, v, role };
  }

  function need(role: ProjectRole, min: ProjectRole, what: string): Response | null {
    return atLeast(role, min) ? null : problem(403, "FORBIDDEN", "Not allowed", `${what} needs the ${min} role`);
  }

  /** The published inputs of a signed-in caller (409 before the first publish). */
  function memberInputs(x: MockDashboard, v: Viewer): InputCtx | Response {
    const any = storedSpec(x);
    if (!any || x.d.status !== "published") return problem(409, "DASHBOARD_NOT_PUBLISHED", "The dashboard is not published", "publish it first");
    return { x, v, spec: v1View(any), any, inputs: inputsOf(x, any, null, v.kind === "client") };
  }

  /** A share session from `X-Mapi-Share` (401 missing or ended, 410 for a revoked link). */
  function shared(request: Request): InputCtx | Response {
    const url = new URL(request.url);
    if (request.headers.get("X-Mapi-Project") || request.headers.get("X-CSRF-Token")) {
      return problem(400, "SCHEMA_INVALID", "Invalid request", `share-link routes take no project or CSRF header (${url.pathname})`);
    }
    const header = request.headers.get("X-Mapi-Share");
    const row = header ? state.sessions.get(header) : undefined;
    if (!header || !row) return problem(401, "UNAUTHENTICATED", "Authentication required", "open the link again");
    const x = state.dashboards.get(row.dashboardId);
    const s = x?.shares.find((sh) => sh.share.id === row.shareId);
    if (!x || !s) return problem(410, "LINK_EXPIRED", "The link has expired or was removed", "this link has ended or was removed; ask the sender for a new link");
    if (s.share.status !== "active" || Date.parse(s.share.expires_at) <= Date.now() || x.d.status !== "published" || row.version !== s.sessionVersion) {
      state.sessions.delete(header);
      return problem(410, "LINK_EXPIRED", "The link has expired or was removed", "this link has ended or was removed; ask the sender for a new link");
    }
    if (row.expiresAt <= Date.now()) {
      state.sessions.delete(header);
      return problem(401, "UNAUTHENTICATED", "Authentication required", "the session ended; open the link again");
    }
    const any = storedSpec(x)!;
    const v: Viewer = { kind: "share", id: s.share.id, analyst: false, allowed: s.share.allowed_inputs ?? [], share: s };
    return { x, v, spec: v1View(any), any, inputs: inputsOf(x, any, v.allowed) };
  }

  function mintSession(x: MockDashboard, s: MockShare): ShareSession {
    const ttl = shareTtlS();
    const expiresAt = Math.min(Date.now() + ttl * 1000, Date.parse(s.share.expires_at));
    const session = `mps_${b64url(JSON.stringify({ s: s.share.id, v: s.sessionVersion, n: randomB64(8) }))}.${randomB64(43)}`;
    state.sessions.set(session, { dashboardId: x.d.id, shareId: s.share.id, version: s.sessionVersion, expiresAt });
    return { session, expires_at: iso(expiresAt), dashboard: clientView(x, s.share.allowed_inputs ?? [], s.share.expires_at) };
  }

  const withShared = (fn: (c: InputCtx, request: Request, params: Params) => Response | Promise<Response>) =>
    async ({ request, params }: { request: Request; params: Params }) => {
      const c = shared(request);
      return c instanceof Response ? c : fn(c, request, params);
    };

  const withMember = (fn: (c: InputCtx, request: Request) => Response | Promise<Response>) =>
    async ({ request, params }: { request: Request; params: Params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const c = memberInputs(s.x!, s.v);
      return c instanceof Response ? c : fn(c, request);
    };

  return [
    // ------------------------------------------------------------------ public share links
    http.post(`${API}/v1/share-sessions`, async ({ request }) => {
      await delay(120);
      const body = (await request.json().catch(() => ({}))) as { token?: string; passcode?: string | null };
      if (!body.token || !/^mpd_[0-9a-hjkmnp-tv-z]{40}$/.test(body.token)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "the link is not complete", { errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/token", message: "not a share token" }] });
      }
      for (const x of state.dashboards.values()) {
        const s = x.shares.find((sh) => sh.token === body.token);
        if (!s) continue;
        if (s.share.status !== "active" || Date.parse(s.share.expires_at) <= Date.now() || x.d.status !== "published") {
          return problem(410, "LINK_EXPIRED", "The link has expired or was removed", "this link has ended or was removed; ask the sender for a new link");
        }
        const locked = s.share.locked_until ? Date.parse(s.share.locked_until) - Date.now() : 0;
        if (locked > 0) return problem(429, "SHARE_LOCKED", "The link is locked after too many wrong passcodes", "try again later", { headers: { "Retry-After": String(Math.ceil(locked / 1000)) } });
        if (s.passcode !== null) {
          if (!body.passcode) return problem(401, "SHARE_PASSCODE_REQUIRED", "The link needs a passcode", "enter the passcode that you got with the link");
          if (body.passcode !== s.passcode) {
            s.failures += 1;
            if (s.failures >= MAX_FAILURES) {
              s.failures = 0;
              s.share.locked_until = iso(Date.now() + LOCKOUT_S * 1000);
              return problem(429, "SHARE_LOCKED", "The link is locked after too many wrong passcodes", "try again later", { headers: { "Retry-After": String(LOCKOUT_S) } });
            }
            return problem(401, "SHARE_PASSCODE_INVALID", "The passcode is not correct", `${MAX_FAILURES - s.failures} tries left before the link locks`);
          }
        }
        s.failures = 0;
        s.share.views = (s.share.views ?? 0) + 1;
        s.share.last_viewed_at = iso(Date.now());
        return HttpResponse.json(mintSession(x, s));
      }
      return NOT_FOUND("link");
    }),
    action("post", "/v1/share-sessions:refresh", ({ request }) => {
      const c = shared(request);
      if (c instanceof Response) return c;
      state.sessions.delete(request.headers.get("X-Mapi-Share") ?? "");
      return HttpResponse.json(mintSession(c.x, c.v.share!));
    }),
    http.get(`${API}/v1/shared/dashboard`, withShared((c) => HttpResponse.json(clientView(c.x, c.v.allowed, c.v.share!.share.expires_at)))),
    http.get(`${API}/v1/shared/readouts`, withShared((c, request) => listReadouts(c.x, c.v, new URL(request.url)))),
    http.get(`${API}/v1/shared/readouts/:seq`, withShared((c, _r, params) => getReadout(c.x, c.v, Number(params.seq)))),
    http.get(`${API}/v1/shared/grid`, withShared((c) => grid(c))),
    action("post", "/v1/shared/whatif:evaluate", withShared((c, request) => evaluate(c, request))),
    http.post(`${API}/v1/shared/scenarios`, withShared((c, request) => saveScenario(c, request, "/v1/shared/scenarios"))),
    http.get(`${API}/v1/shared/actuals/template`, withShared((c) => template(c))),
    http.post(`${API}/v1/shared/actuals`, withShared((c, request) => submitActuals(c, request, "/v1/shared/actuals"))),
    // v0.17: tried cases, scored files and pictures through a share link (a link has no keep route)
    http.post(`${API}/v1/shared/predictions`, withShared((c, request) => predict(c, request, "/v1/shared/predictions"))),
    http.get(`${API}/v1/shared/batch/template`, withShared((c) => batchTemplate(c))),
    http.post(`${API}/v1/shared/batch`, withShared((c, request) => submitBatch(c, request, "/v1/shared/batch"))),
    http.get(`${API}/v1/shared/readouts/:seq/predictions.csv`, withShared((c, _r, params) => downloadPredictions(c, Number(params.seq), false))),
    http.get(`${API}/v1/shared/assets/:asset`, withShared((c, _r, params) => asset(c, String(params.asset)))),
    // a share link has no keep route (`cases.keep_prediction` is for signed-in callers)
    action("post", "/v1/shared/readouts/(?<seq>\\d+):keep", () => problem(404, "NOT_FOUND", "Not found", "a share link cannot keep a tried case")),

    // ------------------------------------------------------------------ list and create
    http.get(D, ({ request }) => {
      const s = signedIn(request, null);
      if (s instanceof Response) return s;
      const key = request.headers.get("X-Mapi-Project") ?? PROJECT;
      const status = new URL(request.url).searchParams.get("status");
      const rows = [...state.dashboards.values()]
        .filter((x) => x.project === key)
        .filter((x) => (s.v.analyst ? (status ? x.d.status === status : x.d.status !== "archived") : x.d.status === "published" && x.grants.some((g) => g.user_id === s.v.id)))
        .map((x) => summaryOf(x, s.v.analyst))
        .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
      return page(rows);
    }),
    http.post(D, async ({ request }) => {
      const s = signedIn(request, null);
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Creating a dashboard");
      if (denied) return denied;
      const body = (await request.json()) as DashboardCreate;
      // v0.17: a new dashboard is layout 2 (crud.create_dashboard)
      if (!isV2(body.spec) && !layout1CreateAllowed()) {
        return problem(422, "DASHBOARD_LAYOUT_RETIRED", "New dashboards use the new layout", "new dashboards use layout 2: pick a preset (GET /v1/dashboards:presets) and send its spec", { errors: [{ code: "DASHBOARD_LAYOUT_RETIRED", severity: "error", pointer: "/spec/layout_version", message: "pick a preset" }] });
      }
      const key = request.headers.get("X-Mapi-Project") ?? PROJECT;
      if ([...state.dashboards.values()].some((x) => x.project === key && x.d.name === body.name)) return problem(409, "CONFLICT", "Conflict", "a dashboard with this name exists");
      const id = `dsh_01jaf3w1${Array.from({ length: 18 }, () => BASE32[Math.floor(Math.random() * 32)]).join("")}`;
      const now = Date.now();
      const alias = body.spec.source.alias;
      const isForecast = alias === integrationsGen.forecast.alias;
      const rt = isV2(body.spec) ? runtimeForAlias(alias) : null;
      const fixture = rt && rt.key in V2_RAW ? V2_RAW[rt.key as V2Key] : null;
      const x: MockDashboard = {
        project: key,
        d: { ...dashboardRow(id, body.name, body.spec, null, now), created_by: s.v.id },
        revisions: [],
        readouts: [],
        grants: [],
        shares: [],
        refits: [],
        grid: fixture || isForecast ? null : MMM_GRID,
        template: fixture ? (fixture.actuals_template ?? null) : isForecast ? forecastTemplate() : mmmTemplate(),
        idem: new Map(),
        v2: rt,
        batches: new Map(),
      };
      state.dashboards.set(id, x);
      return HttpResponse.json(analystView(x), { status: 201, headers: { ETag: x.d.draft_etag } });
    }),

    // ------------------------------------------------------------------ v0.17: suggested presets (`GET /v1/dashboards:presets`)
    action("get", "/v1/dashboards:presets", ({ request }) => {
      const s = signedIn(request, null);
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Choosing a preset");
      if (denied) return denied;
      return presets(new URL(request.url));
    }),

    // ------------------------------------------------------------------ one dashboard (actions before `:id` routes)
    action("post", `/v1/dashboards/${ID}:upgrade`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Changing a dashboard");
      if (denied) return denied;
      const x = s.x!;
      const ifMatch = request.headers.get("If-Match");
      if (!ifMatch) return problem(428, "PRECONDITION_FAILED", "Precondition failed", "send If-Match with the draft_etag that you read");
      if (ifMatch !== x.d.draft_etag) return problem(412, "DASHBOARD_SPEC_STALE", "The dashboard draft changed since you read it", "reload the dashboard and make your change again");
      if (isV2(x.d.draft_spec)) return problem(409, "CONFLICT", "Conflict", "the draft uses the new layout already");
      await delay(120);
      return HttpResponse.json(upgrade(x), { headers: { ETag: x.d.draft_etag } });
    }),
    action("post", `/v1/dashboards/${ID}:validate`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Checking a dashboard");
      if (denied) return denied;
      return HttpResponse.json(validate(s.x!));
    }),
    action("post", `/v1/dashboards/${ID}:publish`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "runner", "Publishing a dashboard (live inputs run as you)");
      if (denied) return denied;
      const x = s.x!;
      const checked = request.headers.get("If-Match");
      if (checked && checked !== x.d.draft_etag) return problem(412, "DASHBOARD_SPEC_STALE", "The dashboard draft changed since you read it", "the draft changed since you checked it: look at the new draft, then publish");
      const check = validate(x);
      if (!check.ok) return problem(422, "SCHEMA_INVALID", "The dashboard cannot be published", check.issues?.[0]?.message, { errors: check.issues ?? [] });
      await delay(150);
      const now = Date.now();
      const draft = x.d.draft_spec;
      const isForecast = draft.source.alias === integrationsGen.forecast.alias;
      if (isV2(draft) && !x.v2) x.v2 = runtimeForAlias(draft.source.alias);
      const fixtureKey = x.v2 && x.v2.key in V2_RAW ? (x.v2.key as V2Key) : null;
      const mv = fixtureKey ? mvOf(fixtureKey, draft) : isForecast ? { id: ARIMAX_V2.id, ref: ARIMAX_V2.ref } : { id: MV_ID, ref: modelVersion.ref };
      const rev = { ...revisionOf(x.d.id, x.revisions.length + 1, draft, mv, draft.source.alias ?? null, now), created_by: s.v.id };
      x.revisions.push(rev);
      Object.assign(x.d, { status: "published", published_revision: rev, run_as: s.v.id, live_inputs: "ok", problem: null });
      touch(x);
      const stored = rev.spec;
      const spec = v1View(stored);
      const seq = nextSeq(x);
      const planner = !isV2(stored) || stored.preset === "mmm_planner";
      x.readouts.push({
        r: header(seq, { kind: "baseline", trigger: "publish", status: "queued", ready_at: null, title: "First readout", period: isForecast || !planner ? null : [day(LAST_SUNDAY - 6 * DAY), day(LAST_SUNDAY)] }, mv, now),
        actor: { kind: "member", id: s.v.id },
        readyAt: now + readoutMs(),
        build: () => ({ snapshot: isV2(stored) ? baselineV2(x, stored) : isForecast ? forecastSnapshot(spec, 0) : mmmSnapshot(spec, x.grid ?? MMM_GRID, 0) }),
      });
      return HttpResponse.json(analystView(x));
    }),
    action("post", `/v1/dashboards/${ID}:archive`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Archiving a dashboard");
      if (denied) return denied;
      const x = s.x!;
      x.d.status = "archived";
      for (const sh of x.shares) if (sh.share.status === "active") Object.assign(sh.share, { status: "revoked", revoked_at: iso(Date.now()) });
      touch(x);
      return HttpResponse.json(analystView(x));
    }),
    action("post", `/v1/dashboards/${ID}:refresh`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Refreshing a dashboard");
      if (denied) return denied;
      const x = s.x!;
      const c = memberInputs(x, s.v);
      if (c instanceof Response) return c;
      const stop = paused(x);
      if (stop) return stop;
      const key = request.headers.get("Idempotency-Key");
      const again = key ? x.idem.get(`refresh:${key}`) : undefined;
      const earlier = again ? x.readouts.find((r) => r.r.seq === again) : undefined;
      if (earlier) return HttpResponse.json(headerOf(earlier.r), { status: 202 });
      const now = Date.now();
      const seq = nextSeq(x);
      const any = c.any;
      const isForecast = !isV2(any) && x.template?.mode === "conditioned";
      const planner = !isV2(any) || any.preset === "mmm_planner";
      const rev = x.d.published_revision!;
      x.readouts.push({
        r: header(seq, { kind: "refresh", trigger: "manual", status: "queued", ready_at: null, title: `Refresh of ${new Date(now).toLocaleDateString("en-GB")}`, period: isForecast || !planner ? null : [day(LAST_SUNDAY - 6 * DAY), day(LAST_SUNDAY)] }, { id: rev.model_version_id, ref: rev.model_ref }, now),
        actor: { kind: "member", id: s.v.id },
        readyAt: now + readoutMs(),
        build: () => ({ snapshot: isV2(any) ? baselineV2(x, any) : isForecast ? forecastSnapshot(c.spec, 0) : mmmSnapshot(c.spec, x.grid ?? MMM_GRID, 0) }),
        spent: true,
      });
      if (key) x.idem.set(`refresh:${key}`, seq);
      return HttpResponse.json(headerOf(x.readouts[x.readouts.length - 1]!.r), { status: 202 });
    }),
    http.get(`${D}/:id`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const x = s.x!;
      if (!s.v.analyst) return HttpResponse.json(clientView(x, null, null, s.v.kind === "client"));
      return HttpResponse.json(analystView(x), { headers: { ETag: x.d.draft_etag } });
    }),
    http.put(`${D}/:id/draft`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Changing a dashboard");
      if (denied) return denied;
      const x = s.x!;
      const ifMatch = request.headers.get("If-Match");
      if (!ifMatch) return problem(428, "PRECONDITION_FAILED", "Precondition failed", "send If-Match with the draft_etag that you read");
      if (ifMatch !== x.d.draft_etag) return problem(412, "DASHBOARD_SPEC_STALE", "The dashboard draft changed since you read it", "reload the dashboard and make your change again");
      const body = (await request.json()) as { spec: AnyDashboardSpec };
      if (!isV2(body.spec) && isV2(x.d.draft_spec) && !layout1CreateAllowed()) {
        return problem(422, "DASHBOARD_LAYOUT_RETIRED", "The draft uses the new layout", "the draft uses layout 2: a layout 1 spec cannot replace it", { errors: [{ code: "DASHBOARD_LAYOUT_RETIRED", severity: "error", pointer: "/spec/layout_version", message: "send a layout 2 spec" }] });
      }
      x.d.draft_spec = body.spec;
      if (isV2(body.spec) && (!x.v2 || x.v2.catalog !== runtimeForAlias(body.spec.source.alias)?.catalog)) x.v2 = x.v2 ?? runtimeForAlias(body.spec.source.alias);
      x.d.draft_etag = `"${hash(`${JSON.stringify(body.spec)}${Date.now()}`).slice(0, 16)}"`;
      touch(x);
      return HttpResponse.json(analystView(x), { headers: { ETag: x.d.draft_etag } });
    }),
    http.get(`${D}/:id/sources`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Reading what the model offers");
      if (denied) return denied;
      const x = s.x!;
      return HttpResponse.json(x.v2?.catalog ?? runtimeForAlias(x.d.draft_spec.source.alias)?.catalog ?? {});
    }),
    http.get(`${D}/:id/revisions`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      if (!s.v.analyst) return NOT_FOUND();
      return page([...s.x!.revisions].reverse());
    }),

    // ------------------------------------------------------------------ grants
    http.get(`${D}/:id/grants`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      return need(s.role, "editor", "Seeing who has access") ?? page(s.x!.grants);
    }),
    http.put(`${D}/:id/grants/:user`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Giving access");
      if (denied) return denied;
      const x = s.x!;
      const userId = String(params.user);
      const [tenant, project] = x.project.split("/");
      const member = state.members().find((m) => m.principal_id === userId && m.tenant_id === tenant && (m.project_id === project || m.project_id === null));
      if (!member) return problem(409, "CONFLICT", "Conflict", "the user is not a member of this project: invite them as a client first");
      const existing = x.grants.find((g) => g.user_id === userId);
      if (existing) return HttpResponse.json(existing);
      const g: DashboardGrant = { user_id: userId, email: member.email ?? null, display_name: member.display_name ?? null, granted_by: s.v.id, created_at: iso(Date.now()) };
      x.grants.push(g);
      return HttpResponse.json(g);
    }),
    http.delete(`${D}/:id/grants/:user`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Removing access");
      if (denied) return denied;
      const x = s.x!;
      const i = x.grants.findIndex((g) => g.user_id === params.user);
      if (i < 0) return NOT_FOUND("grant");
      x.grants.splice(i, 1);
      return new HttpResponse(null, { status: 204 });
    }),

    // ------------------------------------------------------------------ share links (management)
    http.get(`${D}/:id/shares`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Seeing the share links");
      if (denied) return denied;
      const now = Date.now();
      return page(s.x!.shares.map((sh) => ({ ...sh.share, status: sh.share.status === "active" && Date.parse(sh.share.expires_at) <= now ? "expired" : sh.share.status })).reverse());
    }),
    http.post(`${D}/:id/shares`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Creating a share link");
      if (denied) return denied;
      const x = s.x!;
      if (x.d.status !== "published") return problem(409, "DASHBOARD_NOT_PUBLISHED", "The dashboard is not published", "publish it before you share it");
      const body = (await request.json()) as DashboardShareCreate;
      const allowed = [...new Set(body.allowed_inputs ?? [])];
      if (allowed.length && !atLeast(s.role, "runner")) return problem(403, "FORBIDDEN", "Not allowed", "a link with inputs spends compute: it needs the runner role");
      const days = body.expires_in_days ?? 30;
      if (!Number.isInteger(days) || days < 1 || days > 365) return problem(422, "SCHEMA_INVALID", "Invalid request", "the link can last 1 to 365 days", { errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/expires_in_days", message: "1 to 365" }] });
      const passcode = body.passcode ?? null;
      if (passcode !== null && (passcode.length < 8 || passcode.length > 128)) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "a passcode has 8 to 128 characters", { errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/passcode", message: "8 to 128 characters" }] });
      }
      const token = randomToken();
      const now = Date.now();
      const share: DashboardShare = {
        id: `dss_01jaf3w1${token.slice(4, 22)}`,
        label: body.label ?? null,
        display: `mpd_…${token.slice(-4)}`,
        status: "active",
        has_passcode: passcode !== null,
        allowed_inputs: allowed,
        views: 0,
        last_viewed_at: null,
        locked_until: null,
        expires_at: iso(now + days * DAY),
        revoked_at: null,
        created_by: s.v.id,
        created_at: iso(now),
      };
      x.shares.push({ share, token, passcode, failures: 0, sessionVersion: 1 });
      const created: DashboardShareCreated = { ...share, token, url_fragment: `/s#${token}` };
      return HttpResponse.json(created, { status: 201 });
    }),
    action("post", `/v1/dashboards/${ID}/shares/(?<share>[^/:]+):revoke`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Revoking a share link");
      if (denied) return denied;
      const sh = s.x!.shares.find((x) => x.share.id === params.share);
      if (!sh) return NOT_FOUND("share link");
      if (sh.share.status !== "revoked") {
        Object.assign(sh.share, { status: "revoked", revoked_at: iso(Date.now()) });
        sh.sessionVersion += 1;
      }
      return HttpResponse.json(sh.share);
    }),

    // ------------------------------------------------------------------ inputs (members and clients)
    http.get(`${D}/:id/grid`, withMember((c) => grid(c))),
    action("post", `/v1/dashboards/${ID}/whatif:evaluate`, withMember((c, request) => evaluate(c, request))),
    http.post(`${D}/:id/scenarios`, withMember((c, request) => saveScenario(c, request, `/v1/dashboards/${c.x.d.id}/scenarios`))),
    http.get(`${D}/:id/actuals/template`, withMember((c) => template(c))),
    http.post(`${D}/:id/actuals`, withMember((c, request) => submitActuals(c, request, `/v1/dashboards/${c.x.d.id}/actuals`))),
    // v0.17: tried cases, scored files, their predictions and the pictures
    http.post(`${D}/:id/predictions`, withMember((c, request) => predict(c, request, `/v1/dashboards/${c.x.d.id}/predictions`))),
    http.get(`${D}/:id/batch/template`, withMember((c) => batchTemplate(c))),
    http.post(`${D}/:id/batch`, withMember((c, request) => submitBatch(c, request, `/v1/dashboards/${c.x.d.id}/batch`))),
    http.get(`${D}/:id/readouts/:seq/predictions.csv`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const c = memberInputs(s.x!, s.v);
      return c instanceof Response ? c : downloadPredictions(c, Number(params.seq), atLeast(s.role, "editor"));
    }),
    http.get(`${D}/:id/assets/:asset`, withMember((c, request) => asset(c, new URL(request.url).pathname.split("/").at(-1) ?? ""))),
    action("post", `/v1/dashboards/${ID}/readouts/(?<seq>\\d+):keep`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      return keep(s.x!, s.v, Number(params.seq), atLeast(s.role, "editor"));
    }),

    // ------------------------------------------------------------------ readouts
    http.get(`${D}/:id/readouts`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      return s instanceof Response ? s : listReadouts(s.x!, s.v, new URL(request.url));
    }),
    action("post", `/v1/dashboards/${ID}/readouts/(?<seq>\\d+):publish`, ({ request, params }) => review(request, String(params.id), Number(params.seq), "published")),
    action("post", `/v1/dashboards/${ID}/readouts/(?<seq>\\d+):hide`, ({ request, params }) => review(request, String(params.id), Number(params.seq), "hidden")),
    http.get(`${D}/:id/readouts/:seq`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      return s instanceof Response ? s : getReadout(s.x!, s.v, Number(params.seq));
    }),

    // ------------------------------------------------------------------ refit requests
    http.get(`${D}/:id/refit-requests`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Seeing refit requests");
      if (denied) return denied;
      const status = new URL(request.url).searchParams.get("status");
      return page(s.x!.refits.filter((r) => !status || r.status === status));
    }),
    action("post", `/v1/dashboards/${ID}/refit-requests/(?<req>[^/:]+):approve`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Deciding a refit request") ?? need(s.role, "runner", "Approving a refit (it starts a workflow run as you)");
      if (denied) return denied;
      const x = s.x!;
      const rq = x.refits.find((r) => r.id === params.req);
      if (!rq) return NOT_FOUND("refit request");
      if (rq.status === "approved") return HttpResponse.json(rq);
      if (rq.status !== "open") return problem(409, "CONFLICT", "Conflict", `the request is ${rq.status}`);
      const body = (await request.json().catch(() => ({}))) as { note?: string | null };
      await delay(150);
      const now = Date.now();
      const tag = randomB64(10).toLowerCase().replace(/[^a-z0-9]/g, "x");
      Object.assign(rq, { status: "approved", decided_by: s.v.id, decided_at: iso(now), note: body.note ?? null, dataset_version_id: `dsv_01jaf3w1${tag}`, workflow_run_id: `wfr_01jaf3w1${tag}` });
      const linked = x.readouts.find((r) => r.r.seq === rq.readout_seq);
      if (linked) Object.assign(linked.r, { visibility: "published", refit_status: "approved" });
      touch(x);
      return HttpResponse.json(rq);
    }),
    action("post", `/v1/dashboards/${ID}/refit-requests/(?<req>[^/:]+):reject`, async ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const denied = need(s.role, "editor", "Deciding a refit request");
      if (denied) return denied;
      const x = s.x!;
      const rq = x.refits.find((r) => r.id === params.req);
      if (!rq) return NOT_FOUND("refit request");
      if (rq.status === "rejected") return HttpResponse.json(rq);
      if (rq.status !== "open") return problem(409, "CONFLICT", "Conflict", `the request is ${rq.status}`);
      const body = (await request.json().catch(() => ({}))) as { note?: string | null };
      Object.assign(rq, { status: "rejected", decided_by: s.v.id, decided_at: iso(Date.now()), note: body.note ?? null });
      const linked = x.readouts.find((r) => r.r.seq === rq.readout_seq);
      if (linked) linked.r.refit_status = "rejected";
      return HttpResponse.json(rq);
    }),
  ];

  function review(request: Request, id: string, seq: number, to: "published" | "hidden"): Response {
    const s = signedIn(request, id);
    if (s instanceof Response) return s;
    const denied = need(s.role, "editor", "Reviewing readouts");
    if (denied) return denied;
    const x = s.x!;
    tick(x);
    const row = x.readouts.find((r) => r.r.seq === seq);
    if (!row) return NOT_FOUND("readout");
    if (row.r.visibility === to) return HttpResponse.json(headerOf(row.r));
    if (row.r.kind === "scenario" || row.r.visibility === "private") return problem(409, "CONFLICT", "Conflict", "a saved scenario is private: it cannot enter the history");
    if (to === "published" && row.r.status === "failed") return problem(409, "CONFLICT", "Conflict", "a failed readout cannot be published");
    row.r.visibility = to;
    touch(x);
    return HttpResponse.json(headerOf(row.r));
  }
}

/** `crud.validate_draft` (the parts the mock can check): JSON pointers into the draft. */
function validate(x: MockDashboard): DashboardValidation {
  if (isV2(x.d.draft_spec)) return validateV2(x, x.d.draft_spec);
  const spec = layout1Spec(x.d.draft_spec);
  const issues: Issue[] = [];
  const add = (pointer: string, message: string) => issues.push({ code: "SCHEMA_INVALID", severity: "error", pointer, message });
  if (!spec.title.trim()) add("/title", "Give the dashboard a title.");
  if ((spec.tiles ?? []).length > 4) add("/tiles", "Choose at most 4 tiles.");
  if (!(spec.tabs ?? []).length || spec.tabs?.[0] !== "this_period") add("/tabs", "The first tab must be This period.");
  if (spec.source.kind === "endpoint" && !spec.source.alias) add("/source/alias", "Choose an endpoint.");
  if (spec.source.kind === "model_version" && (!spec.source.model_id || !spec.source.version)) add("/source", "Choose a model and a version.");
  if (spec.whatif?.enabled) {
    if (!x.grid) add("/whatif/enabled", "The model has no response grid: turn off what-ifs.");
    else
      spec.channels.forEach((c, i) => {
        if (!x.grid!.channels.includes(c.name)) add(`/channels/${i}/name`, `The response grid has no channel named ${c.name}.`);
      });
  }
  if (spec.actuals?.enabled && !x.template) add("/actuals/enabled", "The model cannot take actuals: turn off uploads.");
  if (spec.actuals?.allow_refit_requests && !spec.actuals.refit_workflow_id) add("/actuals/refit_workflow_id", "Choose the workflow that refits the model.");
  return { ok: issues.length === 0, issues };
}

/** `check_spec_v2` and the validators of `DashboardSpecV2` (the parts the mock can check). */
function validateV2(x: MockDashboard, spec: DashboardSpecV2): DashboardValidation {
  const issues: Issue[] = [];
  const add = (pointer: string, message: string, severity: Issue["severity"] = "error") => issues.push({ code: "SCHEMA_INVALID", severity, pointer, message });
  const catalog = x.v2?.catalog ?? runtimeForAlias(spec.source.alias)?.catalog ?? null;
  if (!spec.title.trim()) add("/title", "Give the dashboard a title.");
  if (spec.source.kind === "endpoint" && !spec.source.alias) add("/source/alias", "Choose an endpoint.");
  if (!spec.panels.length) add("/panels", "Add at least one panel.");
  if (spec.panels.length > 24) add("/panels", "Use at most 24 panels.");
  if (spec.panels.filter((p) => p.kind === "kpi").length > 8) add("/panels", "Use at most 8 key numbers.");
  const seen = new Set<string>();
  spec.panels.forEach((p, i) => {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(p.id)) add(`/panels/${i}/id`, "A panel id has lowercase letters, digits and _ (at most 32).");
    if (seen.has(p.id)) add(`/panels/${i}/id`, `Two panels have the id ${p.id}.`);
    seen.add(p.id);
    if (!p.title.trim()) add(`/panels/${i}/title`, "Give the panel a title.");
    if (p.kind === "mmm" && spec.preset !== "mmm_planner") add(`/panels/${i}/kind`, "Planner parts are only for the media planner.");
    const variable = (p.kind === "kpi" || p.kind === "chart" || p.kind === "table") && p.source.kind === "output" ? p.source.variable : null;
    if (variable && catalog?.outputs?.length && !catalog.outputs.some((o) => o.name === variable)) add(`/panels/${i}/source/variable`, `The model has no output named ${variable}.`);
    if (p.kind === "kpi" && p.source.kind === "metric" && catalog?.metrics?.length && !catalog.metrics.some((m) => m.key === (p.source as { key: string }).key)) add(`/panels/${i}/source/key`, "The model fit logged no such metric.", "warning");
  });
  if ((spec.preset === "mmm_planner") !== Boolean(spec.mmm)) add("/mmm", spec.preset === "mmm_planner" ? "The media planner needs its channels." : "Only the media planner has channels.");
  const rows = spec.scenario_rows;
  if (rows?.enabled) {
    if (!rows.outputs?.length) add("/scenario_rows/outputs", "Choose 1 to 4 outputs to predict.");
    if (!rows.inputs?.length && !rows.horizon) add("/scenario_rows/inputs", "Add an input or a horizon.");
    (rows.inputs ?? []).forEach((d, i) => {
      const why = checkValue(d.default, d);
      if (why) add(`/scenario_rows/inputs/${i}/default`, `The default does not fit: ${why}.`);
    });
    if (!x.v2?.scorer && catalog) add("/scenario_rows/enabled", "The model cannot score new cases here: turn off Try a case.");
  }
  if (spec.batch?.enabled && !x.v2?.batchTemplate) add("/batch/enabled", "The model has no row dim to score a file on: turn off Score a file.");
  if (spec.actuals?.enabled && !x.template) add("/actuals/enabled", "The model cannot take actuals: turn off uploads.");
  if (spec.actuals?.enabled && spec.actuals.match === "row" && !spec.actuals.id_column) add("/actuals/id_column", "Choose the column that names each case.");
  if (spec.actuals?.allow_refit_requests && !spec.actuals.refit_workflow_id) add("/actuals/refit_workflow_id", "Choose the workflow that refits the model.");
  if (spec.actuals?.enabled && (spec.limits?.exact_runs_per_day ?? 20) === 0) add("/limits/exact_runs_per_day", "Uploads spend a run: with 0 runs a day every upload is refused.", "warning");
  return { ok: !issues.some((i) => i.severity === "error"), issues };
}

/**
 * `GET /v1/dashboards:presets`: the presets of a binding, best first, and the compiled spec of
 * the first (`?alias=` or `?model_id=&version=`). The mock knows the bindings of its fixtures.
 */
function presets(url: URL): Response {
  const alias = url.searchParams.get("alias");
  const modelId = url.searchParams.get("model_id");
  const version = Number(url.searchParams.get("version") ?? "");
  if (!alias && !(modelId && Number.isInteger(version) && version > 0)) {
    return problem(422, "SCHEMA_INVALID", "Invalid request", "give alias, or model_id and version", { errors: [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/alias", message: "give alias, or model_id and version" }] });
  }
  const source: DashboardSpecV2["source"] = alias ? { kind: "endpoint", alias } : { kind: "model_version", model_id: modelId, version };
  const byModel: Record<string, string> = { [fx_MODEL_ID]: integrationsGen.alias, [ARIMAX.model.id]: integrationsGen.forecast.alias };
  const key = alias ?? byModel[modelId ?? ""] ?? null;
  const fixture = (Object.keys(V2_RAW) as V2Key[]).find((k) => V2_RAW[k].spec.source.alias === key);
  const found = fixture ? { suggestions: V2_RAW[fixture].suggestions, spec: V2_RAW[fixture].spec, issues: V2_RAW[fixture].issues } : key === integrationsGen.alias ? V1_UPGRADES.mmm : key === integrationsGen.forecast.alias ? V1_UPGRADES.forecast : null;
  if (!found) {
    if (alias) return NOT_FOUND("endpoint");
    return HttpResponse.json({ suggestions: [{ preset: "generic", reasons: ["Any model: one value and one chart of its main output."], blocked_inputs: [] }], spec: null, issues: [{ code: "NOT_FOUND", severity: "warning", pointer: "/model_id", message: "The mock has no catalog of this model version." }] });
  }
  const spec = { ...clone(found.spec), title: "New dashboard", source };
  return HttpResponse.json({ suggestions: clone(found.suggestions), spec, issues: clone(found.issues.filter((i) => i.severity !== "error")) });
}

/**
 * `POST …:upgrade` (`upgrade.convert`): the layout 1 draft as a layout 2 draft (`upgrade_v1`):
 * the planner for a media model, the forecast preset for a forecast with a dummy channel. The
 * draft's own words, binding, uploads, refresh and limits are kept.
 */
function upgrade(x: MockDashboard): DashboardUpgrade {
  const v1 = layout1Spec(x.d.draft_spec);
  const which = v1.source.alias === integrationsGen.forecast.alias || (!v1.whatif?.enabled && v1.source.alias !== integrationsGen.alias) ? "forecast" : "mmm";
  const u = V1_UPGRADES[which];
  const spec: DashboardSpecV2 = {
    ...clone(u.spec),
    title: v1.title,
    client_name: v1.client_name ?? null,
    summary: v1.summary ?? null,
    note: v1.note ?? null,
    period_label: v1.period_label ?? u.spec.period_label,
    source: clone(v1.source),
    subject: { ...u.spec.subject, label: v1.kpi.label, unit: v1.kpi.unit, currency: v1.kpi.currency ?? null, decimals: v1.kpi.decimals ?? 0, ...(which === "mmm" ? { variable: v1.kpi.variable, value_per_unit: v1.kpi.value_per_unit ?? null } : {}) },
    actuals: { ...u.spec.actuals, enabled: v1.actuals?.enabled ?? false, publish: v1.actuals?.publish ?? "review", allow_refit_requests: v1.actuals?.allow_refit_requests ?? false, refit_workflow_id: v1.actuals?.refit_workflow_id ?? null },
    refresh: { ...clone(v1.refresh), horizon: u.spec.refresh?.horizon ?? null },
    limits: { ...u.spec.limits, exact_runs_per_day: v1.limits?.exact_runs_per_day ?? 20 },
    show_technical: v1.show_technical ?? false,
  };
  if (which === "mmm") spec.mmm = { channels: clone(v1.channels), tiles: clone(v1.tiles ?? []), whatif: clone(v1.whatif ?? { enabled: true, exact: false }) };
  x.d.draft_spec = spec;
  x.d.draft_etag = `"${hash(`${JSON.stringify(spec)}${Date.now()}`).slice(0, 16)}"`;
  x.v2 = x.v2 ?? upgradedRuntime(which);
  if (which === "forecast" && !x.template) x.template = forecastTemplate();
  touch(x);
  const check = validateV2(x, spec);
  return { dashboard: analystView(x), issues: [...clone(u.issues), ...(check.issues ?? [])], notes: [...u.notes] };
}
