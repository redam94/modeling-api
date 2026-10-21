/**
 * Types and limits of the client dashboards (`mapi_spec.dashboards`, `mapi_spec.dashboard_v2`,
 * `mapi_spec.dashboard_tools`): layout 1 (v0.16, MMM and forecast tiles) and layout 2 (v0.17,
 * panels for any model type).
 *
 * The DTOs are the generated aliases of `api/types.ts` (the v0.17 ones are read straight from
 * the generated schema below); the tool results that no HTTP route returns
 * (`ReadoutComparison`, `ChannelHistory`, `MetricExplanation`, the v0.17 reports) are written
 * out here and checked against `api/dashboard-tools.gen.json` by `tools.test.ts`. This module
 * has no runtime imports, so the share app and the readout page can use it freely.
 */
import type { components } from "@/api/schema";
import type {
  ActualsSubmit,
  ActualsTemplate,
  ChannelReadout,
  ClientDashboard,
  Dashboard,
  DashboardGrid,
  DashboardSpec,
  DashboardSpecV2,
  DashboardWhatIfResult,
  EffectiveInputs,
  KpiDef,
  KpiValue,
  Problem,
  Readout,
  ReadoutHeader,
  ReadoutList,
  ReadoutSnapshot,
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
  DashboardSpecV2,
  AnyDashboardSpec,
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

type DashboardShareAllowed = import("@/api/types").DashboardShare["allowed_inputs"];

/** Is a stored spec a layout 2 one (v0.17)? */
export function isLayout2(spec: DashboardSpec | DashboardSpecV2): spec is DashboardSpecV2 {
  return spec.layout_version === 2;
}

/** {@link isLayout2} under the plan's name (I-1: `layout_version: 2` is required on layout 2). */
export const isV2Spec = isLayout2;

/**
 * The layout 1 view of a stored spec. The v0.16 builder and preview edit layout 1 only; until
 * the v0.17 layout 2 builder lands, a layout 2 spec is read through the fields both layouts
 * share (title, source, actuals, refresh, limits) and the layout 1 fields are simply absent.
 * Prefer {@link asLayout1} where a layout 2 spec (an auto-upgraded dashboard) can arrive.
 */
export function layout1Spec(spec: DashboardSpec | DashboardSpecV2): DashboardSpec {
  return spec as DashboardSpec;
}

/** The spec when it is layout 1, else `null` (an upgraded or new dashboard is layout 2). */
export function asLayout1(spec: DashboardSpec | DashboardSpecV2): DashboardSpec | null {
  return isLayout2(spec) ? null : spec;
}

/** Does the viewer's dashboard use layout 2 (panels)? */
export function isLayout2Dashboard(d: Pick<ClientDashboard, "layout_version">): boolean {
  return d.layout_version === 2;
}

/** Is a readout snapshot a layout 2 one (`schema_version` 2: panels and the v0.17 blocks)? */
export function hasPanels(snapshot: ReadoutSnapshot | null | undefined): boolean {
  return !!snapshot && (snapshot.schema_version === 2 || (snapshot.panels?.length ?? 0) > 0);
}

/**
 * Which renderer a readout takes (plan 3.7): the panels one for a layout 2 snapshot, or for a
 * layout 2 dashboard's readout that has no snapshot yet; the v1 tree otherwise. History mixes
 * both after the upgrade, so each readout is decided by its own snapshot first.
 */
export function readoutLayout(dashboard: Pick<ClientDashboard, "layout_version">, readout: Pick<Readout, "snapshot"> | null): 1 | 2 {
  const snap = readout?.snapshot ?? null;
  if (snap) return hasPanels(snap) ? 2 : 1;
  return isLayout2Dashboard(dashboard) ? 2 : 1;
}

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
export type ShareInput = NonNullable<DashboardShareAllowed>[number];
export type PeriodLabel = NonNullable<ClientDashboard["period_label"]>;
export type KpiUnit = KpiDef["unit"];
export type PrincipalKind = "member" | "client" | "share";

export const TILE_KEYS = ["expected", "vs_expected", "return_on_media", "in_range_spend", "accuracy", "index"] as const satisfies readonly TileKey[];
export const TAB_KEYS = ["this_period", "trends", "scenarios", "update", "history"] as const satisfies readonly TabKey[];
export const ZONES = ["headroom", "optimal", "over", "no_payback"] as const satisfies readonly Zone[];
export const READOUT_KINDS = ["baseline", "refresh", "scenario", "actuals", "prediction", "batch"] as const satisfies readonly ReadoutKind[];
/** What a share link may allow (`ShareInput`), in the order the access editor lists them. */
export const SHARE_INPUTS = ["whatif", "actuals", "scenario_rows", "batch"] as const satisfies readonly ShareInput[];
/** The share inputs a layout 1 dashboard has (it never has scenario rows or batch scoring). */
export const SHARE_INPUTS_V1 = ["whatif", "actuals"] as const satisfies readonly ShareInput[];

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
/** The slowest poll of a pending readout (the backoff stops growing here). */
export const READOUT_POLL_MAX_MS = 15_000;

/**
 * The wait before the next poll of a pending readout: {@link READOUT_POLL_MS}, then 1.5×
 * longer each time up to {@link READOUT_POLL_MAX_MS}. A share link's requests count against
 * a per-address budget that viewers in one office share, so a long run must not poll at a
 * steady 30 requests a minute.
 */
export function readoutPollMs(polls: number): number {
  return Math.min(READOUT_POLL_MAX_MS, Math.round(READOUT_POLL_MS * 1.5 ** Math.max(0, polls)));
}

/**
 * What the analyst reads for `dashboards.problem` (a short machine word the server sets),
 * besides the paused live inputs, which have their own message.
 */
export function dashboardProblemText(problem: string): string {
  switch (problem) {
    case "new_version_available":
      return "The endpoint serves a newer model version. Clients still see the pinned one: publish again to move to it.";
    case "new_version_incompatible":
      return "The endpoint serves a model version this dashboard does not fit (a channel, the KPI or the actuals contract is missing). Clients still see the pinned one: check the draft against the new version.";
    case "workflow_version_refused":
      return "The linked workflow scored a model version this dashboard may not show (it ran uploaded code, or it is another model's version). Nothing was published to clients.";
    case "upgrade_needs_review":
      return "This dashboard could not move to the new layout by itself. Clients still see the old layout: check the issues and use the new layout from the builder.";
    default:
      return `The dashboard needs attention: ${problem}.`;
  }
}

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

/** The change of one KPI panel (layout 2) between two readouts. */
export interface PanelDelta {
  panel_id: string;
  label: string;
  a?: number | null;
  b?: number | null;
  /** b - a */
  delta?: number | null;
}

/** Tile, channel and (layout 2) KPI panel changes between two readouts (`compare_readouts`). */
export interface ReadoutComparison {
  a: ReadoutHeader;
  b: ReadoutHeader;
  same_model: boolean;
  tiles: TileDelta[];
  channels: ChannelDelta[];
  /** Layout 2 only (absent or empty on layout 1). */
  panel_deltas?: PanelDelta[];
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

/**
 * The glossary of v0.17 (`mapi_spec.dashboards.GlossaryKey`, the `explain_metric` enum of
 * `dashboard-tools.gen.json`): the v0.16 keys, then the words of any model type. The tool
 * surface (`tools.ts`) explains every key of this list.
 */
export const GLOSSARY_KEYS_V2 = [
  ...GLOSSARY_KEYS,
  "prediction",
  "likely_range",
  "probability",
  "share_positive",
  "accuracy_rate",
  "calibration",
  "typical_error",
  "effect",
  "cumulative_effect",
  "without_intervention",
  "chance_of_effect",
  "map",
  "batch",
  "approximate",
  "expected_count",
] as const;
export type GlossaryKeyV2 = (typeof GLOSSARY_KEYS_V2)[number];

export interface MetricExplanation {
  metric: GlossaryKeyV2;
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

// ------------------------------------------------------------------------------ layout 2 (v0.17)

/**
 * openapi-fetch hands back `Readable<T>` data (tuples become arrays); the same mapping as
 * `api/types.ts`, so these aliases are assignable to and from what the client returns.
 */
type Arrayify<T> = T extends (infer E)[] ? Arrayify<E>[] : T extends object ? { [K in keyof T]: Arrayify<T[K]> } : T;
type Schema<K extends keyof components["schemas"]> = Arrayify<components["schemas"][K]>;

export type PresetKey = DashboardSpecV2["preset"];
/** The layout 2 readout sections (`TabKeyV2`; the layout 1 `TabKey` is unchanged). */
export type SectionKey = NonNullable<ClientDashboard["sections"]>[number];
export type AccuracyKind = NonNullable<EffectiveInputs["accuracy_kind"]>;
export type SubjectDef = Schema<"SubjectDef">;
export type SubjectUnit = SubjectDef["unit"];
export type PanelMeta = Schema<"PanelMeta">;
export type PanelKind = PanelMeta["kind"];
export type ChartIntent = NonNullable<PanelMeta["intent"]>;
/** One panel of a layout 2 spec (discriminated on `kind`). */
export type Panel = DashboardSpecV2["panels"][number];
export type ScenarioForm = Schema<"ScenarioForm">;
export type ScenarioInputDef = Schema<"ScenarioInputDef">;
export type ScenarioInputType = ScenarioInputDef["type"];
export type HorizonDef = Schema<"HorizonDef">;
export type BatchForm = Schema<"BatchForm">;
export type ScenarioRowsDef = Schema<"ScenarioRowsDef">;
export type BatchDef = Schema<"BatchDef">;

/** One block of a layout 2 snapshot (discriminated on `kind`). */
export type PanelBlock = NonNullable<ReadoutSnapshot["panels"]>[number];
export type PanelBlockKind = PanelBlock["kind"];
export type PanelBlockOf<K extends PanelBlockKind> = Extract<PanelBlock, { kind: K }>;
export type KpiBlock = PanelBlockOf<"kpi">;
export type SeriesBlock = PanelBlockOf<"series">;
export type BarsBlock = PanelBlockOf<"bars">;
export type ScatterBlock = PanelBlockOf<"scatter">;
export type CalibrationBlock = PanelBlockOf<"calibration">;
export type ConfusionBlock = PanelBlockOf<"confusion">;
export type FieldBlock = PanelBlockOf<"field">;
export type FieldFrame = FieldBlock["frames"][number];
export type TableBlock = PanelBlockOf<"table">;
export type NoteBlock = PanelBlockOf<"note">;
export type PictureBlock = PanelBlockOf<"picture">;
export type UnavailableBlock = PanelBlockOf<"unavailable">;

export type PredictionResult = Schema<"PredictionResult">;
export type PredictionRow = Schema<"PredictionRow">;
export type PredictionValue = Schema<"PredictionValue">;
export type BatchSummary = Schema<"BatchSummary">;
export type BatchStats = Schema<"BatchStats">;
export type BatchDownload = Schema<"BatchDownload">;
/** The accuracy of an actuals upload (discriminated on `kind`: error, classification or effect). */
export type AccuracyDetail = NonNullable<ReadoutSnapshot["accuracy_detail"]>;
export type ErrorAccuracy = Schema<"ErrorAccuracy">;
export type ClassAccuracy = Schema<"ClassAccuracy">;
export type EffectAccuracy = Schema<"EffectAccuracy">;
export type AccuracyPoint = Schema<"AccuracyPoint">;

export type ScenarioRowsRequest = Schema<"ScenarioRowsRequest">;
/** One scenario row: input name → value (`null`: the default). */
export type ScenarioRow = ScenarioRowsRequest["rows"][number];
export type BatchSubmit = Schema<"BatchSubmit">;
export type BatchTemplate = Schema<"BatchTemplate">;
export type BatchColumn = Schema<"BatchColumn">;
export type DashboardUpgrade = Schema<"DashboardUpgrade">;
export type DashboardPresets = Schema<"DashboardPresets">;
export type PresetSuggestion = Schema<"PresetSuggestion">;
export type SourceCatalog = Schema<"SourceCatalog">;
export type CatalogOutput = Schema<"CatalogOutput">;
export type CatalogMetric = Schema<"CatalogMetric">;
export type CatalogInput = Schema<"CatalogInput">;
export type CatalogPicture = Schema<"CatalogPicture">;
export type SpecIssue = Schema<"Issue">;

export const PRESET_KEYS = ["mmm_planner", "forecast", "regression", "classifier", "causal_impact", "field_map", "image", "generic"] as const satisfies readonly PresetKey[];
export const SECTION_KEYS = ["overview", "try", "accuracy", "batch", "history"] as const satisfies readonly SectionKey[];
export const PANEL_KINDS = ["kpi", "chart", "table", "note", "recipe", "mmm"] as const satisfies readonly PanelKind[];
export const PANEL_BLOCK_KINDS = ["kpi", "series", "bars", "scatter", "calibration", "confusion", "field", "table", "note", "picture", "unavailable"] as const satisfies readonly PanelBlockKind[];

/** `MAX_PANELS`, `MAX_KPI_PANELS` (`mapi_spec.dashboard_v2`). */
export const MAX_PANELS = 24;
export const MAX_KPI_PANELS = 8;
/** `MAX_SCENARIO_ROWS`: rows of one "Try a case" request. */
export const MAX_SCENARIO_ROWS = 20;
export const MAX_SCENARIO_INPUTS = 16;
export const MAX_SCENARIO_STEPS = 10_000;
/** `MAX_BATCH_ROWS`: rows of one batch file ("Score a file"); 5001 is refused before a run. */
export const MAX_BATCH_ROWS = 5000;
/** `MAX_ROW_ACTUALS`: rows of a row-matched actuals upload. */
export const MAX_ROW_ACTUALS = 5000;
export const MAX_HORIZON = 104;
export const MAX_FIELD_SIDE = 64;
export const MAX_FIELD_FRAMES = 8;
export const MAX_SERIES_POINTS_V2 = 1040;
/** `MAX_SPEC_BYTES`: the serialised spec, either layout. */
export const MAX_SPEC_BYTES = 65_536;
/** `SNAPSHOT_SCHEMA_VERSION`: the `schema_version` of a layout 2 snapshot. */
export const SNAPSHOT_SCHEMA_VERSION = 2;
/** `PANEL_ID_RE`. */
export const PANEL_ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
/** `ASSET_ID_RE`: a picture rendered at publish (`dsa_` + a ULID). */
export const ASSET_ID_RE = /^dsa_[0-9a-z]{26}$/;
/** The one-time plain note of an upgraded dashboard (the server sends it as `upgrade_note`). */
export const UPGRADE_NOTE = "This dashboard has a new layout. The numbers come from the same model.";
export const UPGRADE_NOTE_RANGES = "The likely ranges now show where one new result will probably be. They are wider than before.";

/**
 * `RECIPE_INTENTS`: platform plot recipes drawn natively (as a chart intent). Any other recipe
 * of a recipe panel is a picture (the model version's fit-time plot, rendered at publish).
 */
export const RECIPE_INTENTS: Readonly<Record<string, ChartIntent>> = {
  "core.ppc_fan": "fan",
  "core.fit_vs_actual": "scatter",
  "core.residuals_over_time": "residuals",
  "core.calibration": "calibration",
  "core.confusion": "confusion",
  "core.counterfactual": "diverging",
  "causal.impact": "effect",
  "sci.field_map": "field",
  "sci.area_map": "bars",
  "core.state_sequence": "state_sequence",
  "ml.feature_importance": "bars",
};

/** The client view of one panel (`dashboard_v2.panel_meta`). */
export function panelMeta(p: Panel): PanelMeta {
  const intent = p.kind === "chart" ? p.intent : p.kind === "recipe" ? (RECIPE_INTENTS[p.recipe] ?? null) : null;
  return { id: p.id, kind: p.kind, title: p.title, caption: p.caption ?? null, intent };
}

/** Is this panel block of kind `kind`? */
export function isBlock<K extends PanelBlockKind>(block: PanelBlock, kind: K): block is PanelBlockOf<K> {
  return block.kind === kind;
}

/** The block of panel `panelId` in a snapshot, or `null`. */
export function panelBlock(snapshot: ReadoutSnapshot | null | undefined, panelId: string): PanelBlock | null {
  return snapshot?.panels?.find((b) => b.panel_id === panelId) ?? null;
}

// ------------------------------------------------------------------------------ v0.17 tool results

/** `list_panels`: the panels of the published revision (empty on layout 1). */
export interface PanelList {
  panels: PanelMeta[];
}

/** `get_panel`: one panel block of a readout. */
export interface PanelReport {
  seq: number;
  panel: PanelBlock;
}

/** `get_accuracy`: a readout's accuracy and the history over published actuals readouts. */
export interface AccuracyReport {
  seq?: number | null;
  detail?: AccuracyDetail | null;
  history?: AccuracyPoint[];
}

/** `list_inputs`: what the caller may send, the forms and the daily caps. */
export interface InputsReport {
  inputs: EffectiveInputs;
  scenario_form?: ScenarioForm | null;
  batch_form?: BatchForm | null;
  exact_runs_per_day?: number | null;
  batch_runs_per_day?: number | null;
}

/** `get_prediction`: the caller's own scenario rows readout. */
export interface PredictionReport {
  readout: ReadoutHeader;
  prediction?: PredictionResult | null;
}

/** `get_batch_summary`: the caller's own batch readout (no CSV, no link). */
export interface BatchReport {
  readout: ReadoutHeader;
  summary?: BatchSummary | null;
}

// ------------------------------------------------------------------------------ the layout 2 data client

/** Options of a request that may wait for its readout (`Prefer: wait=N`, N ≤ 30). */
export interface WaitOptions {
  waitSeconds?: number | undefined;
}

/**
 * {@link DashboardClient} plus the v0.17 inputs and downloads (plan I-4). Every client
 * `memberClient` and `shareClient` make is one; the readout page of layout 2 takes this type.
 */
export interface DashboardClientV2 extends DashboardClient {
  /**
   * "Try a case": score scenario rows (≤ {@link MAX_SCENARIO_ROWS}). `202` with the readout
   * (its snapshot `null` until ready: poll it); a value out of range is a 422
   * `DASHBOARD_INPUT_INVALID` with `/rows/<i>/<name>` pointers. Private to the caller.
   */
  predict(body: ScenarioRowsRequest, idemKey: string, opts?: WaitOptions): Promise<Readout>;
  /** The columns, example CSV and row cap of a batch file (409 without batch scoring). */
  batchTemplate(): Promise<BatchTemplate>;
  /** "Score a file": a private batch readout (`202`; 409 `DASHBOARD_BATCH_TOO_LARGE`, 429 over the daily cap). */
  submitBatch(body: BatchSubmit, idemKey: string, opts?: WaitOptions): Promise<Readout>;
  /** The predictions CSV of a batch readout as a Blob (410 `DASHBOARD_DOWNLOAD_GONE` once expired). */
  downloadPredictions(seq: number): Promise<Blob>;
  /** A picture of the dashboard (PNG ≤ 512 KiB) as a Blob, for an object URL. */
  asset(assetId: string): Promise<Blob>;
  /**
   * Keep a tried case (a private prediction readout) for an editor's review: it stops expiring
   * and waits for `:publish`. `null` for a share link (it has no keep route).
   */
  keep: ((seq: number) => Promise<ReadoutHeader>) | null;
}
