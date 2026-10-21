/**
 * Types and limits of the v0.16 client dashboards (`mapi_spec.dashboards`,
 * `mapi_spec.dashboard_tools`).
 *
 * The DTOs are the generated aliases of `api/types.ts`; the tool results that no HTTP route
 * returns (`ReadoutComparison`, `ChannelHistory`, `MetricExplanation`) are written out here
 * and checked against `api/dashboard-tools.gen.json` by `tools.test.ts`. This module has no
 * runtime imports, so the share app and the readout page can use it freely.
 */
import type {
  ActualsSubmit,
  ActualsTemplate,
  ChannelReadout,
  ClientDashboard,
  Dashboard,
  DashboardGrid,
  DashboardSpec,
  DashboardWhatIfResult,
  KpiDef,
  KpiValue,
  Problem,
  Readout,
  ReadoutHeader,
  ReadoutList,
  ScenarioSave,
} from "@/api/types";

export type {
  ActualsAccuracy,
  ActualsInputs,
  ActualsSubmit,
  ActualsTemplate,
  ChannelDef,
  ChannelReadout,
  ClientDashboard,
  Dashboard,
  DashboardCreate,
  DashboardDraftUpdate,
  DashboardGrant,
  DashboardGrid,
  DashboardLimits,
  DashboardRefitRequest,
  DashboardRevision,
  DashboardShare,
  DashboardShareCreate,
  DashboardShareCreated,
  DashboardSource,
  DashboardSpec,
  DashboardSummary,
  DashboardValidation,
  EffectiveInputs,
  KpiDef,
  KpiValue,
  Readout,
  ReadoutHeader,
  ReadoutList,
  ReadoutSnapshot,
  RefitRequestDecision,
  RefreshPolicy,
  ScenarioSave,
  Series,
  SeriesPoint,
  ShareSession,
  ShareSessionCreate,
  WhatIfInputs,
  WhatIfRequest,
} from "@/api/types";

/** `mapi_spec.dashboards.WhatIfResult` (the guided module has its own, per-draw one). */
export type WhatIfResult = DashboardWhatIfResult;

// ------------------------------------------------------------------------------ enums

export type DashboardStatus = Dashboard["status"];
export type TileKey = KpiValue["key"];
export type TileUnit = KpiValue["unit"];
export type TabKey = NonNullable<DashboardSpec["tabs"]>[number];
export type Zone = NonNullable<ChannelReadout["zone"]>;
export type DeltaDir = NonNullable<KpiValue["delta_dir"]>;
export type ReadoutKind = ReadoutHeader["kind"];
export type ReadoutTrigger = ReadoutHeader["trigger"];
export type ReadoutVisibility = ReadoutHeader["visibility"];
export type ReadoutStatus = ReadoutHeader["status"];
export type RefitStatus = NonNullable<ReadoutHeader["refit_status"]>;
export type ActualsMode = ActualsTemplate["mode"];
export type ShareInput = "whatif" | "actuals";
export type PeriodLabel = NonNullable<DashboardSpec["period_label"]>;
export type KpiUnit = KpiDef["unit"];
export type PrincipalKind = "member" | "client" | "share";

export const TILE_KEYS = ["expected", "vs_expected", "return_on_media", "in_range_spend", "accuracy", "index"] as const satisfies readonly TileKey[];
export const TAB_KEYS = ["this_period", "trends", "scenarios", "update", "history"] as const satisfies readonly TabKey[];
export const ZONES = ["headroom", "optimal", "over", "no_payback"] as const satisfies readonly Zone[];
export const READOUT_KINDS = ["baseline", "refresh", "scenario", "actuals"] as const satisfies readonly ReadoutKind[];
export const SHARE_INPUTS = ["whatif", "actuals"] as const satisfies readonly ShareInput[];

/** Readouts that will not change any more (stop polling). */
export const TERMINAL_READOUT: ReadonlySet<ReadoutStatus> = new Set(["ready", "failed"]);

// ------------------------------------------------------------------------------ limits

/** `DASHBOARD_SHARE_TOKEN_RE`: `mpd_` + 40 Crockford base32 characters. */
export const DASHBOARD_SHARE_TOKEN_RE = /^mpd_[0-9a-hjkmnp-tv-z]{40}$/;
/** `SHARE_SESSION_RE`: `mps_<b64url body>.<b64url HMAC>` (minted by the server). */
export const SHARE_SESSION_RE = /^mps_[A-Za-z0-9_-]{16,400}\.[A-Za-z0-9_-]{43}$/;
/** `SHARE_SESSION_HEADER`: the share session on every `/v1/shared/…` request. */
export const SHARE_SESSION_HEADER = "X-Mapi-Share";
export const MAX_SHARE_DAYS = 365;
export const DEFAULT_SHARE_DAYS = 30;
export const MIN_PASSCODE_LEN = 8;
export const MAX_PASSCODE_LEN = 128;
export const MAX_CHANNELS = 40;
export const MAX_TILES = 4;
export const MAX_SERIES_POINTS = 520;
export const MAX_ACTUALS_ROWS = 520;
export const MAX_ACTUALS_COLS = 64;
export const MAX_READOUT_PAGE = 50;
/** `MAX_CHANNEL_HISTORY` of the `get_channel` tool. */
export const MAX_CHANNEL_HISTORY = 26;
/** How long an exact scenario may take ("This can take up to two minutes."). */
export const READOUT_POLL_MS = 2_000;

/** The redacted form of a share token shown in lists (`share_display`). */
export function shareDisplay(token: string): string {
  return `mpd_…${token.slice(-4)}`;
}

// ------------------------------------------------------------------------------ tool results (no route returns these)

export interface TileDelta {
  key: TileKey;
  label: string;
  a?: number | null;
  b?: number | null;
  /** b - a */
  delta?: number | null;
}

export interface ChannelDelta {
  name: string;
  label: string;
  spend_a?: number | null;
  spend_b?: number | null;
  contribution_a?: number | null;
  contribution_b?: number | null;
  roi_a?: number | null;
  roi_b?: number | null;
  zone_a?: Zone | null;
  zone_b?: Zone | null;
}

/** Tile and channel changes between two readouts (`compare_readouts`). */
export interface ReadoutComparison {
  a: ReadoutHeader;
  b: ReadoutHeader;
  same_model: boolean;
  tiles: TileDelta[];
  channels: ChannelDelta[];
  lead: string;
}

export interface ChannelPoint {
  seq: number;
  created_at: string;
  model_version_id: string;
  spend?: number | null;
  contribution?: number | null;
  roi?: number | null;
  mroi?: number | null;
  zone?: Zone | null;
}

/** One channel over published readouts, oldest first (`get_channel`). */
export interface ChannelHistory {
  name: string;
  label: string;
  points: ChannelPoint[];
}

export const GLOSSARY_KEYS = [
  "expected",
  "vs_expected",
  "return_on_media",
  "marginal_return",
  "contribution",
  "share",
  "range",
  "zones",
  "headroom",
  "optimal",
  "over",
  "no_payback",
  "in_range_spend",
  "accuracy",
  "bias",
  "band_hit_rate",
  "index",
  "scenario",
  "compared",
  "conditioned",
  "refit",
] as const;
export type GlossaryKey = (typeof GLOSSARY_KEYS)[number];

export interface MetricExplanation {
  metric: GlossaryKey;
  text: string;
}

// ------------------------------------------------------------------------------ the data client contract (I-6)

/** What a dashboard page reads and sends, for a member, a client user or a share link. */
export interface DashboardClient {
  kind: PrincipalKind;
  /** The query-key prefix of everything this client reads (no two clients share one). */
  cacheKey: readonly string[];
  dashboard(): Promise<ClientDashboard>;
  readouts(q?: ReadoutQuery): Promise<ReadoutHeader[]>;
  /** {@link readouts} with the cursor (`next_before_seq`) for "Show older". */
  readoutPage(q?: ReadoutQuery): Promise<ReadoutList>;
  readout(seq: number): Promise<Readout>;
  /** The response grid; `null` when the dashboard has none (409). */
  grid(): Promise<DashboardGrid | null>;
  evaluate(multipliers: Record<string, number>): Promise<WhatIfResult>;
  /** 200 with the readout (grid result), or 202 with its header (exact: poll {@link readout}). */
  saveScenario(s: ScenarioSave, idemKey: string, opts?: { waitSeconds?: number }): Promise<Readout | ReadoutHeader>;
  actualsTemplate(): Promise<ActualsTemplate>;
  submitActuals(body: ActualsSubmit, idemKey: string): Promise<ReadoutHeader>;
}

export interface ReadoutQuery {
  kind?: ReadoutKind | undefined;
  beforeSeq?: number | undefined;
  limit?: number | undefined;
}

/** A share session the share app keeps (in memory only) and refreshes before it ends. */
export interface ShareSessionHandle {
  current(): string;
  refresh(): Promise<void>;
  expired(): boolean;
}

/** `true` for the full readout (a 200 answer), `false` for a header (202: still running). */
export function isFullReadout(r: Readout | ReadoutHeader): r is Readout {
  return "snapshot" in r;
}

/** `GET /v1/dashboards/{id}` answers the analyst's `Dashboard` or a client's `ClientDashboard`. */
export function isAnalystDashboard(d: Dashboard | ClientDashboard): d is Dashboard {
  return "draft_spec" in d;
}

/** A problem document as any dashboard client throws it (`ApiError` or `ShareApiError`). */
export interface DashboardProblem {
  status: number;
  code: string | null;
  problem: Problem | null;
  retryAfter: number | null;
}
