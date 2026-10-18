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
};

/** The order of the Data Explorer's list (the basics first, quality right after the overview). */
export const DATA_ORDER: readonly DataSection[] = ["overview", "quality", "distributions", "timeseries", "missingness", "outliers", "correlation", "ccf", "vif", "groups", "acf_pacf", "stationarity", "stl"];

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
