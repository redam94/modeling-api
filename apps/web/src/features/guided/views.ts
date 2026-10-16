/**
 * The Guided views a score run writes (`mapi_spec.views.ForecastView` and
 * `CounterfactualView`): parsed and checked before anything is shown. Unknown formats are
 * refused, extra producer fields ignored.
 */
import type { DistributionSummary, FanChart } from "./types";

export const FORECAST_VIEW = "ui/guided/forecast.json";
export const COUNTERFACTUAL_VIEW = "ui/guided/counterfactual.json";

export interface ForecastView {
  format: "mapi.forecast/v1";
  kpi: string;
  units: string | null;
  kind: "expected" | "predictive";
  dim: string;
  history: number;
  split_at: string | number | boolean | null;
  summed_over: string[];
  fan: FanChart;
  total: DistributionSummary | null;
}

export interface CounterfactualView {
  format: "mapi.counterfactual/v1";
  kpi: string;
  units: string | null;
  kind: "expected" | "predictive";
  dim: string | null;
  summed_over: string[];
  interventions: Record<string, unknown>[];
  scenario_total: DistributionSummary;
  baseline_total: DistributionSummary;
  delta_total: DistributionSummary;
  delta_share: DistributionSummary | null;
  delta_fan: FanChart | null;
}

export class GuidedViewError extends Error {
  override name = "GuidedViewError";
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function bad(what: string): never {
  throw new GuidedViewError(`the result view is not valid: ${what}`);
}

function distribution(v: unknown, what: string): DistributionSummary {
  if (!isRecord(v) || typeof v.mean !== "number" || typeof v.sd !== "number") bad(`${what} needs a mean and sd`);
  const quantiles: Record<string, number> = {};
  if (isRecord(v.quantiles)) {
    for (const [k, x] of Object.entries(v.quantiles)) if (typeof x === "number" && Number.isFinite(Number(k))) quantiles[k] = x;
  }
  return { mean: v.mean, sd: v.sd, quantiles, units: typeof v.units === "string" ? v.units : null };
}

export function parseFan(v: unknown, what = "fan"): FanChart {
  if (!isRecord(v) || !Array.isArray(v.coords) || !Array.isArray(v.quantiles) || !Array.isArray(v.values)) bad(`${what} needs coords, quantiles and values`);
  const coords = v.coords.filter((c): c is string | number => typeof c === "string" || typeof c === "number");
  if (coords.length !== v.coords.length) bad(`${what}.coords must be names or numbers`);
  const quantiles = v.quantiles.filter((q): q is number => typeof q === "number" && q > 0 && q < 1);
  if (!quantiles.length || quantiles.length !== v.quantiles.length) bad(`${what}.quantiles must be in (0, 1)`);
  if (v.values.length !== quantiles.length) bad(`${what}.values needs one row per quantile`);
  const values = v.values.map((row) => {
    if (!Array.isArray(row) || row.length !== coords.length) bad(`${what}.values rows need one value per coord`);
    return row.map((x) => (typeof x === "number" && Number.isFinite(x) ? x : null));
  });
  const observed = Array.isArray(v.observed) ? v.observed.map((x) => (typeof x === "number" && Number.isFinite(x) ? x : null)) : null;
  if (observed && observed.length !== coords.length) bad(`${what}.observed needs one value per coord`);
  return { dim: typeof v.dim === "string" ? v.dim : "time", coords, quantiles, values, observed, units: typeof v.units === "string" ? v.units : null };
}

export function parseForecastView(raw: unknown): ForecastView {
  if (!isRecord(raw) || raw.format !== "mapi.forecast/v1") bad("unknown forecast format");
  const split = raw.split_at;
  return {
    format: "mapi.forecast/v1",
    kpi: typeof raw.kpi === "string" ? raw.kpi : "result",
    units: typeof raw.units === "string" ? raw.units : null,
    kind: raw.kind === "predictive" ? "predictive" : "expected",
    dim: typeof raw.dim === "string" ? raw.dim : "time",
    history: typeof raw.history === "number" ? raw.history : 0,
    split_at: typeof split === "string" || typeof split === "number" || typeof split === "boolean" ? split : null,
    summed_over: Array.isArray(raw.summed_over) ? raw.summed_over.filter((s): s is string => typeof s === "string") : [],
    fan: parseFan(raw.fan),
    total: raw.total == null ? null : distribution(raw.total, "total"),
  };
}

export function parseCounterfactualView(raw: unknown): CounterfactualView {
  if (!isRecord(raw) || raw.format !== "mapi.counterfactual/v1") bad("unknown counterfactual format");
  return {
    format: "mapi.counterfactual/v1",
    kpi: typeof raw.kpi === "string" ? raw.kpi : "result",
    units: typeof raw.units === "string" ? raw.units : null,
    kind: raw.kind === "predictive" ? "predictive" : "expected",
    dim: typeof raw.dim === "string" ? raw.dim : null,
    summed_over: Array.isArray(raw.summed_over) ? raw.summed_over.filter((s): s is string => typeof s === "string") : [],
    interventions: Array.isArray(raw.interventions) ? raw.interventions.filter(isRecord) : [],
    scenario_total: distribution(raw.scenario_total, "scenario_total"),
    baseline_total: distribution(raw.baseline_total, "baseline_total"),
    delta_total: distribution(raw.delta_total, "delta_total"),
    delta_share: raw.delta_share == null ? null : distribution(raw.delta_share, "delta_share"),
    delta_fan: raw.delta_fan == null ? null : parseFan(raw.delta_fan, "delta_fan"),
  };
}
