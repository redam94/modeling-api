/**
 * The Data Explorer and Model Explorer views (`mapi_spec.views`, M3.5): one JSON document per
 * section under `ui/explore/`, written by ingests, fits and explore runs. The types are
 * generated from the Pydantic schemas (`views.gen.ts`); {@link parseView} checks the `format`
 * tag so an unknown or newer document degrades to "not available" instead of breaking the
 * page. Every string in a view comes from the user's data: render it as text only.
 */
import type { components as ApiComponents } from "@/api/schema";

import type { components } from "./views.gen";

type V = components["schemas"];
type ApiSchemas = ApiComponents["schemas"];

export type DataOverviewView = V["DataOverviewView"];
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
// The views of the eight sections for scientific data (v0.10, format tags
// `mapi.data-explorer.<section>/v1`), generated from `mapi_spec.views`. Every list is capped
// by the runner. Every string comes from the user's data: text only.

/** The data shapes the runner detects (`mapi_spec.views.ShapeKind`). */
export type ShapeKind = V["ShapeView"]["kind"];
/** Why a section is skipped (`ExplorerSectionRef.reason`). */
export type SkipReason = NonNullable<V["ExplorerSectionRef"]["reason"]>;
export type UsedAs = NonNullable<V["VarOverview"]["used_as"]>;

export type ShapeView = V["ShapeView"];
export type StructureDim = V["StructureDim"];
export type StructureVar = V["StructureVar"];
export type StructureView = V["StructureView"];
export type FrameStats = V["FrameStats"];
export type ImageStats = V["ImageStats"];
export type ImageGroupMean = V["ImageGroupMean"];
export type ImageComponent = V["ImageComponent"];
export type ImageEntry = V["ImageEntry"];
export type ImagesView = V["ImagesView"];
export type ProfileFrame = V["ProfileFrame"];
export type ProfilePeak = V["ProfilePeak"];
export type ProfileEntry = V["ProfileEntry"];
export type ProfilesView = V["ProfilesView"];
export type MapRing = V["MapRing"];
export type MapFrame = V["MapFrame"];
export type MapLayer = V["MapLayer"];
export type MoranRow = V["MoranRow"];
export type MapView = V["MapView"];
export type NetworkEdge = V["NetworkEdge"];
export type NetworkEntry = V["NetworkEntry"];
export type NetworkView = V["NetworkView"];
export type TreeNodeLayout = V["TreeNodeLayout"];
export type TipTrait = V["TipTrait"];
export type TreeView = V["TreeView"];
export type CircularGroup = V["CircularGroup"];
export type CircularEntry = V["CircularEntry"];
export type CircularPair = V["CircularPair"];
export type CircularView = V["CircularView"];
export type PointAnswers = V["PointAnswers"];
export type PointsView = V["PointsView"];

/** The hints of an explore run (`DataExploreRequest.hints`): they override the detection. */
export type StructureHints = ApiSchemas["StructureHints"];

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
