/** Titles, plain questions and groups of the explorer sections. */
import type { DataSection, ModelSection } from "./views";

export interface SectionMeta {
  title: string;
  /** The question the section answers, in plain words. */
  question: string;
  group: string;
}

export const DATA_SECTION_META: Record<DataSection, SectionMeta> = {
  overview: { title: "Overview", question: "What is in the data?", group: "The basics" },
  quality: { title: "Data quality", question: "Which problems must you correct before you fit a model?", group: "The basics" },
  distributions: { title: "Distributions", question: "Which values does each variable have?", group: "The basics" },
  timeseries: { title: "Over time", question: "How does each variable change over time?", group: "The basics" },
  missingness: { title: "Missing values", question: "Where are values missing?", group: "The basics" },
  outliers: { title: "Unusual values", question: "Which values are unusual?", group: "The basics" },
  correlation: { title: "Correlations", question: "Which variables change together?", group: "Relationships" },
  ccf: { title: "Leads and lags", question: "Which drivers change before the result, and how many periods before?", group: "Relationships" },
  vif: { title: "Overlapping drivers", question: "Can the model find the separate effect of each driver?", group: "Relationships" },
  groups: { title: "Group comparison", question: "Are markets or stores different?", group: "Relationships" },
  acf_pacf: { title: "Memory (ACF/PACF)", question: "How much does each value depend on its past values?", group: "Time patterns" },
  stationarity: { title: "Stable or wandering", question: "Does each series go back to a stable level, or does it wander?", group: "Time patterns" },
  stl: { title: "Trend and season", question: "How strong are the trend and the season?", group: "Time patterns" },
  structure: { title: "Structure", question: "Which shapes does the data have (images, maps, networks, series)?", group: "The basics" },
  images: { title: "Images and fields", question: "What do the images and grids of the data look like?", group: "Pictures of the data" },
  profiles: { title: "Profiles and spectra", question: "How do the values change along an axis that is not time?", group: "Pictures of the data" },
  map: { title: "Map", question: "Where are the values high or low, and are neighbours similar?", group: "Pictures of the data" },
  network: { title: "Network", question: "Which nodes are connected, and how strongly?", group: "Pictures of the data" },
  // Not a tree dataset (HDF5 groups, the node picker): a variable that holds a tree (parent, branch length).
  tree: { title: "Tree", question: "How are the items related in the tree that the data holds (parents and branch lengths), and do near relatives have similar traits?", group: "Pictures of the data" },
  circular: { title: "Directions and times of day", question: "Which directions or times of day occur most often?", group: "Pictures of the data" },
  points: { title: "Points", question: "Where are the points, and what is the value at each point?", group: "Pictures of the data" },
};

/** The order of the groups in the section list. */
export const DATA_GROUPS: readonly string[] = ["The basics", "Pictures of the data", "Relationships", "Time patterns"];

/** The order of the Data Explorer's list for time-series data (the basics first, quality right after the overview). */
export const CLASSIC_ORDER: readonly DataSection[] = ["overview", "quality", "distributions", "timeseries", "missingness", "outliers", "correlation", "ccf", "vif", "groups", "acf_pacf", "stationarity", "stl"];

/**
 * The default order of every section: the time-series order, then the sections for scientific
 * data. An index from v0.10 brings its own relevance order (`ExplorerIndex.order`).
 */
export const DATA_ORDER: readonly DataSection[] = [...CLASSIC_ORDER, "structure", "images", "profiles", "map", "network", "tree", "circular", "points"];

export const MODEL_SECTION_META: Record<ModelSection, SectionMeta> = {
  params: { title: "Parameters", question: "What did the model learn, and how sure is it?", group: "What it learned" },
  diagnostics: { title: "Diagnostics", question: "Did the fit finish correctly?", group: "Can you trust it?" },
  fit: { title: "Fit vs actual", question: "How close is the model to what happened?", group: "Can you trust it?" },
  residuals: { title: "Residuals", question: "Is there a pattern left in the errors?", group: "Can you trust it?" },
  decomposition: { title: "Contributions", question: "Which parts make up the result?", group: "What it learned" },
  response_curves: { title: "Response curves", question: "What does more spend give?", group: "What it learned" },
  irf: { title: "Impulse responses", question: "How does a shock to one series change the other series?", group: "What it learned" },
  fevd: { title: "Variance decomposition", question: "Which shocks cause the uncertainty of each series?", group: "What it learned" },
  fields: { title: "Fields", question: "What does the model show on its grid, and where is it not sure?", group: "What it learned" },
  forecast: { title: "Forecast", question: "What comes next?", group: "Looking ahead" },
  backtest: { title: "Backtest", question: "How well does it forecast the past?", group: "Can you trust it?" },
  comparison: { title: "Comparison", question: "Is it better than other versions?", group: "Looking ahead" },
};

/** "W-SUN" → "week" (the unit leads and lags are counted in). */
export function periodUnit(freq: string | null | undefined): string {
  const base = (freq ?? "").replace(/^\d+/, "").split("-")[0] ?? "";
  const map: Record<string, string> = { D: "day", B: "day", W: "week", MS: "month", ME: "month", M: "month", QS: "quarter", QE: "quarter", Q: "quarter", YS: "year", YE: "year", Y: "year", A: "year", h: "hour", H: "hour" };
  return map[base] ?? "period";
}
