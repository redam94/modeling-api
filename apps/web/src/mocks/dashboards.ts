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
import { layout1Spec } from "@/features/dashboards/types";
import { evaluateScenario, type ResponseGrid } from "@/features/guided/whatif";

import { EXTRA_MODEL_IDS, EXTRA_MODELS } from "./explore";
import { MV_ID, modelVersion } from "./fixtures";
import { synthGrid } from "./guided-engine";
import integrationsGen from "./integrations.gen.json";
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
  limits: { exact_runs_per_day: 0 },
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

function revisionOf(id: string, seq: number, spec: DashboardSpec, mv: { id: string; ref: string }, alias: string | null, at: number): DashboardRevision {
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

function dashboardRow(id: string, name: string, spec: DashboardSpec, rev: DashboardRevision | null, at: number): Dashboard {
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
  return { project: PROJECT, d: dashboardRow(DRAFT_DASHBOARD_ID, "spring-promo-plan", spec, null, Date.parse("2026-10-16T10:00:00Z")), revisions: [], readouts: [], grants: [], shares: [], refits: [], grid: MMM_GRID, template: null, idem: new Map() };
}

function scenarioSnapshot(spec: DashboardSpec, grid: ResponseGrid, multipliers: Record<string, number>): ReadoutSnapshot {
  const w = whatIfOf(spec, grid, multipliers);
  return { lead: w.lead, tiles: w.tiles ?? [], channels: w.channels ?? [], series: [], accuracy: null, conditioning: null, scenario: multipliers, source_run_ids: [], input_fingerprint: null, notes_plain: [], technical: {} };
}

export function createDashboardsState(members: () => Membership[] = () => []): DashboardsState {
  const all = [mmmFixture(), forecastFixture(), draftFixture()];
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

function publishedSpec(x: MockDashboard): DashboardSpec | null {
  const spec = x.d.published_revision?.spec;
  return spec ? layout1Spec(spec) : null;
}

function latestPublished(x: MockDashboard): Readout | null {
  tick(x);
  return [...x.readouts].reverse().find((row) => row.r.visibility === "published" && row.r.status === "ready")?.r ?? null;
}

function inputsOf(x: MockDashboard, spec: DashboardSpec, allowed: readonly string[] | null): EffectiveInputs {
  const narrow = allowed === null ? null : new Set(allowed);
  const whatif = Boolean(spec.whatif?.enabled) && x.grid !== null && (narrow === null || narrow.has("whatif"));
  const actuals = Boolean(spec.actuals?.enabled) && x.template !== null && (narrow === null || narrow.has("actuals"));
  return {
    whatif,
    exact: whatif && Boolean(spec.whatif?.exact),
    actuals,
    actuals_mode: actuals ? (x.template?.mode ?? "compared") : null,
    refit_requests: actuals && Boolean(spec.actuals?.allow_refit_requests),
  };
}

function clientView(x: MockDashboard, allowed: readonly string[] | null, endsAt: string | null): ClientDashboard {
  const spec = publishedSpec(x)!;
  const inputs = inputsOf(x, spec, allowed);
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

function analystView(x: MockDashboard): Dashboard {
  tick(x);
  const last = x.readouts[x.readouts.length - 1];
  return { ...x.d, latest_readout: last ? headerOf(last.r) : null };
}

function summaryOf(x: MockDashboard, analyst: boolean): DashboardSummary {
  const spec = publishedSpec(x) ?? x.d.draft_spec;
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
  spec: DashboardSpec;
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

async function submitActuals(c: InputCtx, request: Request, path: string): Promise<Response> {
  if (!c.x.template || !c.inputs.actuals) return disabled("Sending numbers");
  const stop = paused(c.x);
  if (stop) return stop;
  const body = (await request.json()) as ActualsSubmit;
  if (body.request_refit && !c.inputs.refit_requests) return disabled("Asking for a refit");
  const earlier = replay(c, request, path);
  if (earlier) return HttpResponse.json(headerOf(earlier), { status: 202 });
  const parsed = actualsOf(body, c.x.template);
  if (parsed instanceof Response) return parsed;
  const seq = nextSeq(c.x);
  const now = Date.now();
  const spec = c.spec;
  const x = c.x;
  const r: Readout = {
    seq,
    kind: "actuals",
    trigger: "actuals",
    visibility: spec.actuals?.publish === "auto" ? "published" : "pending_review",
    status: "queued",
    title: body.label ?? `Numbers sent on ${new Date(now).toLocaleDateString("en-GB")}`,
    period: parsed.periods.length ? [parsed.periods[0]!.slice(0, 10), parsed.periods[parsed.periods.length - 1]!.slice(0, 10)] : null,
    model_version_id: x.d.published_revision?.model_version_id ?? MV_ID,
    model_ref: x.d.published_revision?.model_ref ?? modelVersion.ref,
    endpoint_revision: x.d.published_revision?.endpoint_revision ?? null,
    created_at: iso(now),
    ready_at: null,
    refit_status: null,
    snapshot: null,
    problem: null,
  };
  x.readouts.push({ r, actor: { kind: c.v.kind, id: c.v.id }, readyAt: now + readoutMs(), build: () => ({ snapshot: actualsSnapshot(x, spec, parsed.values, parsed.periods) }) });
  if (body.request_refit) {
    // At most one open request per dashboard: a newer one supersedes the older.
    for (const old of x.refits) {
      if (old.status !== "open") continue;
      old.status = "superseded";
      const linked = x.readouts.find((row) => row.r.seq === old.readout_seq);
      if (linked) linked.r.refit_status = "superseded";
    }
    x.refits.unshift({
      id: `rfr_01jaf3w1${randomB64(16).toLowerCase().replace(/[^a-z0-9]/g, "x")}`,
      dashboard_id: x.d.id,
      readout_seq: seq,
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
  remember(c, request, path, seq);
  return HttpResponse.json(headerOf(r), { status: 202, headers: { Location: `/v1/dashboards/${x.d.id}/readouts/${seq}` } });
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
    const spec = publishedSpec(x);
    if (!spec || x.d.status !== "published") return problem(409, "DASHBOARD_NOT_PUBLISHED", "The dashboard is not published", "publish it first");
    return { x, v, spec, inputs: inputsOf(x, spec, null) };
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
    const spec = publishedSpec(x)!;
    const v: Viewer = { kind: "share", id: s.share.id, analyst: false, allowed: s.share.allowed_inputs ?? [], share: s };
    return { x, v, spec, inputs: inputsOf(x, spec, v.allowed) };
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
      const key = request.headers.get("X-Mapi-Project") ?? PROJECT;
      if ([...state.dashboards.values()].some((x) => x.project === key && x.d.name === body.name)) return problem(409, "CONFLICT", "Conflict", "a dashboard with this name exists");
      const id = `dsh_01jaf3w1${Array.from({ length: 18 }, () => BASE32[Math.floor(Math.random() * 32)]).join("")}`;
      const now = Date.now();
      const x: MockDashboard = { project: key, d: { ...dashboardRow(id, body.name, layout1Spec(body.spec), null, now), created_by: s.v.id }, revisions: [], readouts: [], grants: [], shares: [], refits: [], grid: body.spec.source.alias === integrationsGen.forecast.alias ? null : MMM_GRID, template: body.spec.source.alias === integrationsGen.forecast.alias ? forecastTemplate() : mmmTemplate(), idem: new Map() };
      state.dashboards.set(id, x);
      return HttpResponse.json(analystView(x), { status: 201, headers: { ETag: x.d.draft_etag } });
    }),

    // ------------------------------------------------------------------ one dashboard (actions before `:id` routes)
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
      const isForecast = x.d.draft_spec.source.alias === integrationsGen.forecast.alias;
      const mv = isForecast ? { id: ARIMAX_V2.id, ref: ARIMAX_V2.ref } : { id: MV_ID, ref: modelVersion.ref };
      const rev = { ...revisionOf(x.d.id, x.revisions.length + 1, layout1Spec(x.d.draft_spec), mv, x.d.draft_spec.source.alias ?? null, now), created_by: s.v.id };
      x.revisions.push(rev);
      Object.assign(x.d, { status: "published", published_revision: rev, run_as: s.v.id, live_inputs: "ok", problem: null });
      touch(x);
      const spec = layout1Spec(rev.spec);
      const seq = nextSeq(x);
      x.readouts.push({
        r: header(seq, { kind: "baseline", trigger: "publish", status: "queued", ready_at: null, title: "First readout", period: isForecast ? null : [day(LAST_SUNDAY - 6 * DAY), day(LAST_SUNDAY)] }, mv, now),
        actor: { kind: "member", id: s.v.id },
        readyAt: now + readoutMs(),
        build: () => ({ snapshot: isForecast ? forecastSnapshot(spec, 0) : mmmSnapshot(spec, x.grid ?? MMM_GRID, 0) }),
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
      const isForecast = x.template?.mode === "conditioned";
      const rev = x.d.published_revision!;
      x.readouts.push({
        r: header(seq, { kind: "refresh", trigger: "manual", status: "queued", ready_at: null, title: `Refresh of ${new Date(now).toLocaleDateString("en-GB")}`, period: isForecast ? null : [day(LAST_SUNDAY - 6 * DAY), day(LAST_SUNDAY)] }, { id: rev.model_version_id, ref: rev.model_ref }, now),
        actor: { kind: "member", id: s.v.id },
        readyAt: now + readoutMs(),
        build: () => ({ snapshot: isForecast ? forecastSnapshot(c.spec, 0) : mmmSnapshot(c.spec, x.grid ?? MMM_GRID, 0) }),
      });
      if (key) x.idem.set(`refresh:${key}`, seq);
      return HttpResponse.json(headerOf(x.readouts[x.readouts.length - 1]!.r), { status: 202 });
    }),
    http.get(`${D}/:id`, ({ request, params }) => {
      const s = signedIn(request, String(params.id));
      if (s instanceof Response) return s;
      const x = s.x!;
      if (!s.v.analyst) return HttpResponse.json(clientView(x, null, null));
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
      const body = (await request.json()) as { spec: DashboardSpec };
      x.d.draft_spec = body.spec;
      x.d.draft_etag = `"${hash(`${JSON.stringify(body.spec)}${Date.now()}`).slice(0, 16)}"`;
      touch(x);
      return HttpResponse.json(analystView(x), { headers: { ETag: x.d.draft_etag } });
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
