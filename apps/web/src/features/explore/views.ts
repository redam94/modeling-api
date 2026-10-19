/**
 * The Data Explorer and Model Explorer views (`mapi_spec.views`, M3.5): one JSON document per
 * section under `ui/explore/`, written by ingests, fits and explore runs. The types are
 * generated from the Pydantic schemas (`views.gen.ts`); {@link parseView} checks the `format`
 * tag so an unknown or newer document degrades to "not available" instead of breaking the
 * page. Every string in a view comes from the user's data: render it as text only.
 */
import type { components } from "./views.gen";

type V = components["schemas"];

/** Since v0.10 with `shapes` (the detected shapes) and `VarOverview.used_as`. */
export type DataOverviewView = V["DataOverviewView"] & { shapes?: ShapeView[] | null };
export type DistributionsView = V["DistributionsView"];
export type TimeSeriesView = V["TimeSeriesView"];
export type MissingnessView = V["MissingnessView"];
export type OutliersView = V["OutliersView"];
export type CorrelationView = V["CorrelationView"];
export type CrossCorrelationView = V["CrossCorrelationView"];
export type AcfPacfView = V["AcfPacfView"];
export type StationarityView = V["StationarityView"];
export type StlView = V["StlView"];
export type VifView = V["VifView"];
export type GroupComparisonView = V["GroupComparisonView"];
export type QualityView = V["QualityView"];
export type ParamTableView = V["ParamTableView"];
export type ModelDiagnosticsView = V["ModelDiagnosticsView"];
export type FitVsActualView = V["FitVsActualView"];
export type ResidualDiagnosticsView = V["ResidualDiagnosticsView"];
export type DecompositionView = V["DecompositionView"];
export type ResponseCurvesView = V["ResponseCurvesView"];
export type IrfView = V["IrfView"];
export type FevdView = V["FevdView"];
export type FieldsView = V["FieldsView"];
export type FieldEntry = V["FieldEntry"];
export type FieldFrame = V["FieldFrame"];
export type FieldRaster = V["FieldRaster"];
export type FieldArrows = V["FieldArrows"];
export type ModelForecastView = V["ModelForecastView"];
export type BacktestView = V["BacktestView"];
export type ComparisonView = V["ComparisonView"];
export type ExplorerIndexView = V["ExplorerIndex"];
export type ExplorerSectionRefView = V["ExplorerSectionRef"];

export type LagProfile = V["LagProfile"];
export type SeriesView = V["SeriesView"];
export type StatTestResult = V["StatTestResult"];
export type StationarityRow = V["StationarityRow"];
export type StlComponents = V["StlComponents"];
export type ParamRow = V["ParamRow"];
export type QualityWarning = V["QualityWarning"];
export type VarDistribution = V["VarDistribution"];
export type VarOutliers = V["VarOutliers"];
export type GroupComparison = V["GroupComparison"];
export type Histogram = V["Histogram"];
export type ViewFanChart = V["FanChart"];
export type ForecastFan = V["ForecastView"];
export type BacktestFoldResult = V["BacktestFoldResult"];
export type Facet = Record<string, string | number | boolean>;

/** The thirteen sections of v0.5 (time-series data); an index from before v0.10 has only these. */
export const CLASSIC_DATA_SECTIONS = [
  "overview",
  "distributions",
  "timeseries",
  "missingness",
  "outliers",
  "correlation",
  "ccf",
  "acf_pacf",
  "stationarity",
  "stl",
  "vif",
  "groups",
  "quality",
] as const;

/** The sections for scientific data (pictures of N-D data: images, maps, networks, ...). */
export const SCIENCE_SECTIONS = ["structure", "images", "profiles", "map", "network", "tree", "circular", "points"] as const;
export type ScienceSection = (typeof SCIENCE_SECTIONS)[number];

/** Every Data Explorer section, in the order of `mapi_spec.views.DATA_SECTIONS` (21). */
export const DATA_SECTIONS = [...CLASSIC_DATA_SECTIONS, ...SCIENCE_SECTIONS] as const;
export type DataSection = (typeof DATA_SECTIONS)[number];

export function isScienceSection(v: unknown): v is ScienceSection {
  return typeof v === "string" && (SCIENCE_SECTIONS as readonly string[]).includes(v);
}

export const MODEL_SECTIONS = [
  "params",
  "diagnostics",
  "fit",
  "residuals",
  "decomposition",
  "response_curves",
  "irf",
  "fevd",
  "fields",
  "forecast",
  "backtest",
  "comparison",
] as const;
export type ModelSection = (typeof MODEL_SECTIONS)[number];
export type ExplorerSection = DataSection | ModelSection;

/**
 * The sections an ingest of time-series data writes (`CHEAP_SECTIONS`); the rest need an
 * explore run. Since v0.10 an ingest also writes `structure` and the picture sections that
 * apply, and lists the others as skipped (`reason: "not_applicable"`).
 */
export const INGEST_SECTIONS: readonly DataSection[] = ["overview", "distributions", "timeseries", "missingness", "outliers", "correlation", "quality"];
/** The sections a fit writes (`FIT_SECTIONS`); the rest need an explore run. */
export const FIT_SECTIONS: readonly ModelSection[] = ["params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf", "fevd", "fields"];

export interface SectionViews {
  overview: DataOverviewView;
  distributions: DistributionsView;
  timeseries: TimeSeriesView;
  missingness: MissingnessView;
  outliers: OutliersView;
  correlation: CorrelationView;
  ccf: CrossCorrelationView;
  acf_pacf: AcfPacfView;
  stationarity: StationarityView;
  stl: StlView;
  vif: VifView;
  groups: GroupComparisonView;
  quality: QualityView;
  structure: StructureView;
  images: ImagesView;
  profiles: ProfilesView;
  map: MapView;
  network: NetworkView;
  tree: TreeView;
  circular: CircularView;
  points: PointsView;
  params: ParamTableView;
  diagnostics: ModelDiagnosticsView;
  fit: FitVsActualView;
  residuals: ResidualDiagnosticsView;
  decomposition: DecompositionView;
  response_curves: ResponseCurvesView;
  irf: IrfView;
  fevd: FevdView;
  fields: FieldsView;
  forecast: ModelForecastView;
  backtest: BacktestView;
  comparison: ComparisonView;
}

/** The `format` tag of every section's view. */
export const VIEW_FORMAT: { [S in ExplorerSection]: string } = {
  overview: "mapi.data-explorer.overview/v1",
  distributions: "mapi.data-explorer.distributions/v1",
  timeseries: "mapi.data-explorer.timeseries/v1",
  missingness: "mapi.data-explorer.missingness/v1",
  outliers: "mapi.data-explorer.outliers/v1",
  correlation: "mapi.data-explorer.correlation/v1",
  ccf: "mapi.data-explorer.ccf/v1",
  acf_pacf: "mapi.data-explorer.acf-pacf/v1",
  stationarity: "mapi.data-explorer.stationarity/v1",
  stl: "mapi.data-explorer.stl/v1",
  vif: "mapi.data-explorer.vif/v1",
  groups: "mapi.data-explorer.groups/v1",
  quality: "mapi.data-explorer.quality/v1",
  structure: "mapi.data-explorer.structure/v1",
  images: "mapi.data-explorer.images/v1",
  profiles: "mapi.data-explorer.profiles/v1",
  map: "mapi.data-explorer.map/v1",
  network: "mapi.data-explorer.network/v1",
  tree: "mapi.data-explorer.tree/v1",
  circular: "mapi.data-explorer.circular/v1",
  points: "mapi.data-explorer.points/v1",
  params: "mapi.model-explorer.params/v1",
  diagnostics: "mapi.model-explorer.diagnostics/v1",
  fit: "mapi.model-explorer.fit/v1",
  residuals: "mapi.model-explorer.residuals/v1",
  decomposition: "mapi.model-explorer.decomposition/v1",
  response_curves: "mapi.model-explorer.response-curves/v1",
  irf: "mapi.model-explorer.irf/v1",
  fevd: "mapi.model-explorer.fevd/v1",
  fields: "mapi.model-explorer.fields/v1",
  forecast: "mapi.model-explorer.forecast/v1",
  backtest: "mapi.model-explorer.backtest/v1",
  comparison: "mapi.model-explorer.comparison/v1",
};

export const EXPLORE_INDEX = "ui/explore/index.json";
export const INDEX_FORMAT = "mapi.explorer-index/v1";

export function viewName(section: ExplorerSection): string {
  return `ui/explore/${section}.json`;
}

export function plotsName(section: ExplorerSection): string {
  return `ui/explore/plots/${section}.json`;
}

export class ViewFormatError extends Error {
  override name = "ViewFormatError";
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export function isExplorerSection(v: unknown): v is ExplorerSection {
  return typeof v === "string" && ((DATA_SECTIONS as readonly string[]).includes(v) || (MODEL_SECTIONS as readonly string[]).includes(v));
}

/** A section's view, after checking it is a JSON object with the section's `format` tag. */
export function parseView<S extends ExplorerSection>(section: S, raw: unknown): SectionViews[S] {
  if (!isObj(raw)) throw new ViewFormatError(`the ${section} view is not a JSON object`);
  const expected = VIEW_FORMAT[section];
  if (raw.format !== expected) {
    throw new ViewFormatError(`the ${section} view has format ${JSON.stringify(raw.format)}, not ${expected}`);
  }
  return raw as unknown as SectionViews[S];
}

/** `ui/explore/index.json`. */
export function parseIndex(raw: unknown): ExplorerIndexView {
  if (!isObj(raw) || raw.format !== INDEX_FORMAT || !Array.isArray(raw.sections)) {
    throw new ViewFormatError("the explorer index has an unknown format");
  }
  const sections = raw.sections.filter((s): s is ExplorerSectionRefView => isObj(s) && typeof s.section === "string" && typeof s.view === "string");
  return { ...(raw as ExplorerIndexView), sections };
}

/** "geo=north, product=a" (or "all" for an unfaceted series). */
export function facetLabel(facet: Facet | null | undefined): string {
  const entries = Object.entries(facet ?? {});
  return entries.length ? entries.map(([k, v]) => `${k}=${String(v)}`).join(", ") : "all";
}

/** "revenue (geo=north)" - a series' label, as the plots-core recipes write it. */
export function seriesLabel(variable: string, facet: Facet | null | undefined): string {
  return Object.keys(facet ?? {}).length ? `${variable} (${facetLabel(facet)})` : variable;
}

// ------------------------------------------------------------------------------ scientific data
//
// The views of the eight sections for scientific data (design §4.3/§5.1, format tags
// `mapi.data-explorer.<section>/v1`). Written by hand from the design contract until
// `views.gen.ts` is regenerated from `mapi_spec.views` (`gen_explorer_fixtures.py --types`);
// the field names are the design's. Every list is capped by the runner. Every string comes
// from the user's data: text only.

type Coord = string | number | boolean | null;
type Num = number | null;

/** The data shapes the runner detects (`mapi_runner.worker.structure.ShapeKind`). */
export type ShapeKind = "timeseries" | "sequence" | "image" | "vector_field" | "areal_map" | "points" | "network" | "tree" | "circular" | "profile" | "table";

/** Why a section is skipped (`ExplorerSectionRef.reason`). */
export type SkipReason = "not_applicable" | "insufficient_data" | "needs_option";

interface ExploreBase {
  caption_plain?: string | null;
  warnings_plain?: string[];
  node?: string | null;
  [key: string]: unknown;
}

/** One detected shape: what it shows and where to see it. */
export interface ShapeView {
  kind: ShapeKind;
  vars: string[];
  /** role → dim (y, x, frame, component, channel, area, vertex, node, along, item, columns, time, point, tip). */
  dims: Record<string, string>;
  score: number;
  label_plain: string;
  /** The sections that show this shape, best first. */
  sections?: string[];
}

export interface StructureDim {
  name: string;
  size: number;
  kind?: string | null;
  role_in_data: string;
  start?: Coord;
  end?: Coord;
  evenly_spaced?: boolean | null;
}

export type UsedAs = "data" | "geometry" | "mask" | "weight" | "index" | "label";

export interface StructureVar {
  name: string;
  dims: string[];
  shape: number[];
  dtype: string;
  role?: string | null;
  units?: string | null;
  used_as: UsedAs;
}

export interface StructureView extends ExploreBase {
  format?: "mapi.data-explorer.structure/v1";
  shapes: ShapeView[];
  dims: StructureDim[];
  vars: StructureVar[];
  recommended?: string[];
  not_applicable?: Record<string, string>;
}

/** Statistics of every frame of an image (not only of the frames shown). */
export interface FrameStats {
  coords: Coord[];
  mean: Num[];
  sd?: Num[];
  min?: Num[];
  max?: Num[];
  missing_share?: Num[];
  downsampled?: boolean;
}

export interface ImageStats {
  min?: Num;
  max?: Num;
  mean?: Num;
  sd?: Num;
  missing_share?: Num;
  saturated_share?: Num;
}

/** The mean raster of one level of a label of the frame dim (e.g. the digit classes). */
export interface ImageGroupMean {
  label: Coord;
  n?: number;
  mean: FieldRaster;
}

export interface ImageComponent {
  name: string;
  histogram: Histogram;
}

/** One image: a `FieldEntry` (drawn by the field viewer) whose frames hold only the data value. */
export type ImageEntry = FieldEntry & {
  var: string;
  role?: string | null;
  mask_var?: string | null;
  /** The mask raster (0/1, the same size as the image). */
  mask?: FieldRaster | null;
  frame_stats?: FrameStats | null;
  histogram?: Histogram | null;
  /** The histogram counts are skewed: draw them on a log scale. */
  log_count?: boolean;
  stats?: ImageStats | null;
  /** The label of the frame dim that the group means are of. */
  group_by?: string | null;
  groups?: ImageGroupMean[];
  components?: ImageComponent[];
};

export interface ImagesView extends ExploreBase {
  format?: "mapi.data-explorer.images/v1";
  fields: ImageEntry[];
  omitted?: string[];
}

export interface ProfileFrame {
  label: Coord;
  values: Num[];
}

export interface ProfilePeak {
  frame: Coord;
  at: Coord;
  value: Num;
}

export interface ProfileEntry {
  var: string;
  units?: string | null;
  along: string;
  coords: Coord[];
  frame_dim?: string | null;
  frames: ProfileFrame[];
  facet?: Facet;
  log_x?: boolean;
  log_y?: boolean;
  peak?: ProfilePeak[];
  downsampled?: boolean;
  omitted_frames?: number;
}

export interface ProfilesView extends ExploreBase {
  format?: "mapi.data-explorer.profiles/v1";
  items: ProfileEntry[];
}

export interface MapRing {
  /** The index of the area in `areas`. */
  area: number;
  x: Num[];
  y: Num[];
}

export interface MapLayerFrame {
  label: Coord;
  values: Num[];
}

export interface MapLayer {
  var: string;
  units?: string | null;
  role?: string | null;
  /** One value per area. */
  values: Num[];
  frames?: MapLayerFrame[] | null;
  /** A layer that the runner derived (the ratio target ÷ exposure). */
  derived?: boolean;
}

export interface MoranRow {
  var: string;
  morans_i: Num;
  expected?: Num;
  z?: Num;
  p_value?: Num;
  weights?: "adjacency" | "knn4";
}

export interface MapView extends ExploreBase {
  format?: "mapi.data-explorer.map/v1";
  area_dim: string;
  areas: Coord[];
  rings: MapRing[];
  /** [x min, y min, x max, y max]. */
  bbox?: Num[];
  centroids: { x: Num[]; y: Num[] };
  layers: MapLayer[];
  edges?: [number, number][];
  isolates?: number[];
  moran?: MoranRow[];
}

export interface NetworkEdge {
  i: number;
  j: number;
  w?: Num;
}

export interface NetworkEntry {
  var: string;
  node_dim: string;
  facet?: Facet;
  binary?: boolean;
  signed?: boolean;
  directed?: boolean;
  weighted?: boolean;
  n_nodes: number;
  /** One label per node shown (the nodes of highest degree when there are too many). */
  labels: Coord[];
  /** The adjacency matrix in seriation order (`order`); absent when there are too many nodes. */
  matrix?: Num[][] | null;
  order?: number[] | null;
  positions: { x: Num[]; y: Num[] };
  edges: NetworkEdge[];
  degree: Num[];
  degree_hist?: Histogram | null;
  density?: Num;
  transitivity?: Num;
  reciprocity?: Num;
  components?: { count: number; largest_share: Num } | null;
  node_attrs?: Record<string, Coord[]>;
  homophily?: { attr: string; within_share: Num; expected_share: Num }[];
  omitted_nodes?: number;
}

export interface NetworkView extends ExploreBase {
  format?: "mapi.data-explorer.network/v1";
  items: NetworkEntry[];
}

/** The layout of a tree: one entry per node in every list (columnar). */
export interface TreeNodeLayout {
  x: Num[];
  y: Num[];
  /** The parent's index (-1 for a root). */
  parent: number[];
  is_tip: boolean[];
  label: Coord[];
}

export interface TipTrait {
  var: string;
  units?: string | null;
  /** One value per tip, the tips in layout order (increasing y). */
  values: Num[];
}

export interface TreeView extends ExploreBase {
  format?: "mapi.data-explorer.tree/v1";
  node_dim: string;
  n_nodes: number;
  n_tips: number;
  roots?: number;
  depth?: Num;
  ultrametric?: boolean;
  layout: TreeNodeLayout;
  tip_dim?: string | null;
  matched_by?: "label" | "count" | "order" | null;
  unmatched?: number;
  traits: TipTrait[];
  signal?: { var: string; morans_i: Num; p_value: Num }[];
}

export interface CircularGroup {
  label: Coord;
  n: number;
  counts: number[];
  mean_direction?: Num;
  resultant_length?: Num;
  circular_sd?: Num;
  rayleigh_z?: Num;
  rayleigh_p?: Num;
}

export interface CircularEntry {
  var: string;
  units?: string | null;
  /** The length of one turn in `units` (360 for degrees, 24 for hours, ...). */
  period: number;
  /** Where 0 is: "north" (compass, clock) or "east" (mathematics). */
  zero?: string | null;
  clockwise?: boolean;
  group_by?: string | null;
  groups: CircularGroup[];
  /** The lower edge of each bin, in `units` (as many as `counts`). */
  bins: number[];
}

export interface CircularPair {
  a: string;
  b: string;
  /** counts[a_bin][b_bin]. */
  counts: number[][];
  rho?: Num;
}

export interface CircularView extends ExploreBase {
  format?: "mapi.data-explorer.circular/v1";
  items: CircularEntry[];
  pairs?: CircularPair[];
}

export interface PointsAnswers {
  var: string;
  by_dim: string;
  terms: string[];
  /** The modal term of each sampled point (a term, or its index in `terms`). */
  mode: (string | number | null)[];
  /** The share of answers that agree with the modal term, per sampled point. */
  agreement: Num[];
  counts?: number[];
}

export interface PointsView extends ExploreBase {
  format?: "mapi.data-explorer.points/v1";
  point_dim: string;
  x: string;
  y: string;
  lattice?: boolean;
  n: number;
  sample: { x: Num[]; y: Num[]; index: number[] };
  density?: { x_edges: number[]; y_edges: number[]; counts: number[][] } | null;
  colour?: { var: string; values: Num[] }[];
  /** "#rrggbb" per sampled point (Lab → sRGB in the runner). */
  srgb?: (string | null)[] | null;
  answers?: PointsAnswers | null;
  clark_evans?: { r: Num; z?: Num; p_value?: Num } | null;
}

/** The hints of an explore run (`DataExploreRequest.hints`): they override the detection. */
export interface StructureHints {
  y_dim?: string | null;
  x_dim?: string | null;
  frame_dim?: string | null;
  node_dim?: string | null;
  time_dim?: string | null;
  angle_vars?: Record<string, string>;
}

export const SKIP_REASONS: readonly SkipReason[] = ["not_applicable", "insufficient_data", "needs_option"];

/** `ExplorerSectionRef.reason`, when it is a known one. */
export function skipReason(ref: unknown): SkipReason | null {
  const r = isObj(ref) ? ref.reason : null;
  return typeof r === "string" && (SKIP_REASONS as readonly string[]).includes(r) ? (r as SkipReason) : null;
}

/** The relevance order of an index (`ExplorerIndex.order`): known data sections only. */
export function indexOrder(index: unknown): DataSection[] | null {
  const o = isObj(index) ? index.order : null;
  if (!Array.isArray(o)) return null;
  const known = new Set<string>(DATA_SECTIONS);
  return [...new Set(o.filter((s): s is DataSection => typeof s === "string" && known.has(s)))];
}

/** The shapes of an index (`ExplorerIndex.shapes`). */
export function indexShapes(index: unknown): ShapeKind[] {
  const o = isObj(index) ? index.shapes : null;
  return Array.isArray(o) ? o.filter((s): s is ShapeKind => typeof s === "string") : [];
}

const HEX_RE = /^#[0-9a-f]{6}$/i;

/** A colour from the data, only as "#rrggbb" (anything else is refused). */
export function safeHex(v: unknown): string | null {
  return typeof v === "string" && HEX_RE.test(v) ? v.toLowerCase() : null;
}
