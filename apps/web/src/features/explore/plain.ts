/**
 * Plain-language readings of explorer views: what a non-statistician should take away from a
 * section, from its numbers alone (pure functions; `plain.test.ts`). The thresholds are the
 * ones the docs explain (seasonal strength above 0.6 is strong, a VIF above 10 is poorly
 * identified, a band of ±1.96/√n for correlations, R-hat at most 1.01).
 */
import type {
  BacktestView,
  CircularEntry,
  CircularGroup,
  ComparisonView,
  ImageEntry,
  MapView,
  NetworkEntry,
  PointsView,
  ProfileEntry,
  StructureView,
  TreeView,
  CorrelationView,
  CrossCorrelationView,
  DecompositionView,
  FitVsActualView,
  GroupComparisonView,
  LagProfile,
  MissingnessView,
  ModelDiagnosticsView,
  OutliersView,
  ParamTableView,
  QualityView,
  StationarityView,
  StlComponents,
  VarDistribution,
  VifView,
} from "./views";
import { count, num, pct } from "./ui";

export type Tone = "neutral" | "good" | "warn" | "bad";

export interface Reading {
  text: string;
  tone: Tone;
}

/** "1 problem", "3 problems". */
export function plural(n: number, word: string, many?: string): string {
  return `${n} ${n === 1 ? word : (many ?? `${word}s`)}`;
}

function listWords(items: readonly string[], max = 4): string {
  const shown = items.slice(0, max);
  const rest = items.length - shown.length;
  const words = rest > 0 ? [...shown, `${rest} more`] : shown;
  if (words.length <= 1) return words[0] ?? "";
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

// ------------------------------------------------------------------------------ data

export function qualityReading(v: QualityView): Reading {
  const items = v.items ?? [];
  const errors = items.filter((i) => i.severity === "error").length;
  const warnings = items.filter((i) => (i.severity ?? "warning") === "warning").length;
  if (errors) return { text: `The data has ${plural(errors, "serious problem")}. Correct ${errors === 1 ? "it" : "them"} before you fit a model.${warnings ? ` Also look at ${plural(warnings, "other item")}.` : ""}`, tone: "bad" };
  if (warnings) return { text: `The data has no serious problems. Look at ${plural(warnings, "item")} before you fit a model.`, tone: "warn" };
  // "good to know" items are not problems, but the page must not say there is nothing above them
  const infos = items.filter((i) => i.severity === "info").length;
  if (infos) return { text: `The data has no problems to correct. ${plural(infos, "item")} ${infos === 1 ? "gives" : "give"} information.`, tone: "good" };
  return { text: "The checks found no problems in the data.", tone: "good" };
}

export function qualityScoreReading(score: number | null | undefined): Reading | null {
  if (score === null || score === undefined) return null;
  if (score >= 0.9) return { text: `The data quality score is ${pct(score)}. The data is ready for a model.`, tone: "good" };
  if (score >= 0.7) return { text: `The data quality score is ${pct(score)}. You can use the data, but look at some items first.`, tone: "warn" };
  return { text: `The data quality score is ${pct(score)}. Correct the problems below before you fit a model.`, tone: "bad" };
}

/** The 25% / 50% / 75% quantiles of a distribution item ("0.25" keys). */
function q(item: VarDistribution, key: number): number | null {
  for (const [k, v] of Object.entries(item.quantiles ?? {})) if (Math.abs(Number(k) - key) < 1e-9) return v;
  return null;
}

/**
 * A variable with only the values 0 and 1 (an indicator, the ties of a network): the bins go
 * from exactly 0 to exactly 1 and no value is between. A two-valued variable such as {0.2, 0.7}
 * or a dose {0, 0.5} also has its values in the first and last bins, but its edges are not 0 and 1.
 */
function isBinary(item: VarDistribution): boolean {
  const h = item.histogram;
  if (!h || item.mean == null || item.mean < 0 || item.mean > 1) return false;
  const lo = h.edges[0]!;
  const hi = h.edges[h.edges.length - 1]!;
  if (lo !== 0 || hi !== 1) return false;
  // every count is in the first or the last bin
  return h.counts.every((n, i) => n === 0 || i === 0 || i === h.counts.length - 1);
}

export function distributionReading(item: VarDistribution): Reading {
  if (isBinary(item)) return { text: `${item.var} has only the values 0 and 1. ${pct(item.mean)} of the values are 1.`, tone: "neutral" };
  const parts: string[] = [];
  const lo = q(item, 0.25);
  const mid = q(item, 0.5);
  const hi = q(item, 0.75);
  if (lo !== null && hi !== null) parts.push(`Half of the values of ${item.var} are between ${num(lo, 3)} and ${num(hi, 3)}${mid !== null ? ` (the middle value is ${num(mid, 3)})` : ""}.`);
  else if (item.mean != null) parts.push(`The mean of ${item.var} is ${num(item.mean, 3)}.`);
  let tone: Tone = "neutral";
  if (item.zero_share != null && item.zero_share >= 0.2) {
    parts.push(`${pct(item.zero_share)} of the values are zero. This is usual for spend that starts and stops, and for counts of rare events.`);
    tone = "warn";
  }
  if (item.skew != null && Math.abs(item.skew) >= 1) {
    parts.push(item.skew > 0 ? "A few values are much larger than the rest (a long right tail)." : "A few values are much smaller than the rest (a long left tail).");
    tone = "warn";
  }
  return { text: parts.join(" "), tone };
}

export function missingnessReading(v: MissingnessView): Reading {
  const missing = Object.entries(v.share_by_var ?? {}).filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]);
  if (!missing.length) return { text: "No values are missing.", tone: "good" };
  const worst = missing[0]!;
  const gaps = Object.entries(v.longest_gap ?? {}).filter(([, g]) => g > 0).sort((a, b) => b[1] - a[1]);
  const gap = gaps[0];
  return {
    text: `${listWords(missing.map(([k, s]) => `${k} (${pct(s, s < 0.01 ? 1 : 0)})`))} ${missing.length === 1 ? "has" : "have"} missing values.${missing.length > 1 ? ` ${worst[0]} has the most.` : ""}${gap ? ` The longest gap is ${plural(gap[1], "period")} in ${gap[0]}.` : ""}`,
    tone: worst[1] > 0.05 ? "warn" : "neutral",
  };
}

export function outliersReading(v: OutliersView): Reading {
  const items = (v.items ?? []).filter((i) => i.count > 0).sort((a, b) => b.count - a.count);
  if (!items.length) return { text: "The data has no unusual values.", tone: "good" };
  const total = items.reduce((s, i) => s + i.count, 0);
  return {
    text: `${plural(total, "value")} ${total === 1 ? "is" : "are"} unusual. ${total === 1 ? "It is" : "Most of them are"} in ${items[0]!.var}. Find out if each value is an error or a real event, for example a launch or a holiday. For a real event, add a control variable.`,
    tone: "warn",
  };
}

export interface CorrelationPair {
  a: string;
  b: string;
  r: number;
  p: number | null;
}

/** Distinct pairs, strongest first. */
export function correlationPairs(v: CorrelationView): CorrelationPair[] {
  const out: CorrelationPair[] = [];
  v.vars.forEach((a, i) => {
    v.vars.forEach((b, j) => {
      if (j <= i) return;
      const r = v.matrix[i]?.[j];
      if (r === null || r === undefined) return;
      out.push({ a, b, r, p: v.p_values?.[i]?.[j] ?? null });
    });
  });
  return out.sort((x, y) => Math.abs(y.r) - Math.abs(x.r));
}

export function strengthWord(r: number): string {
  const a = Math.abs(r);
  return a >= 0.8 ? "very strongly" : a >= 0.6 ? "strongly" : a >= 0.4 ? "moderately" : a >= 0.2 ? "weakly" : "hardly";
}

export function correlationReading(v: CorrelationView): Reading {
  const pairs = correlationPairs(v);
  if (!pairs.length) return { text: "The data has no pair of numeric variables on the same dimensions to compare.", tone: "neutral" };
  const strong = pairs.filter((p) => Math.abs(p.r) >= 0.7);
  if (!strong.length) {
    // the table calls 0.6 "strongly": name the largest pair so the summary agrees with it
    const first = pairs[0];
    const largest = first && Math.abs(first.r) >= 0.4 ? ` The largest correlation is ${first.a} and ${first.b} (${num(first.r, 2)}).` : "";
    return { text: `No pair of variables has a correlation of 0.7 or more.${largest} Thus, it is easy for a model to find the effect of each variable.`, tone: "good" };
  }
  const top = strong[0]!;
  return {
    text: `${top.a} and ${top.b} change ${strengthWord(top.r)} ${top.r > 0 ? "together" : "in opposite directions"} (${num(top.r, 2)}).${strong.length > 1 ? ` ${plural(strong.length - 1, "other pair")} also ${strong.length === 2 ? "has" : "have"} a strong correlation.` : ""} It is difficult for a model to separate variables that change together.`,
    tone: "warn",
  };
}

/** Lags whose correlation is beyond the band, the peak first. */
export function leadReading(item: LagProfile, kpi: string, unit = "period"): string {
  const bound = item.bound ?? null;
  const peak = item.peak_lag;
  const at = peak !== null && peak !== undefined ? item.values[item.lags.indexOf(peak)] : null;
  if (peak === null || peak === undefined || at === null || at === undefined || (bound !== null && Math.abs(at) <= bound)) {
    return `${item.var}: no clear link with ${kpi} at any lag.`;
  }
  const dir = at > 0 ? "increases" : "decreases";
  if (peak > 0) return `${item.var} leads: ${kpi} ${dir} ${plural(peak, unit)} after ${item.var} changes (correlation ${num(at, 2)}).`;
  if (peak < 0) return `${item.var} follows ${kpi} by ${plural(-peak, unit)} (correlation ${num(at, 2)}). Possibly, ${item.var} changes because of the results and does not cause them.`;
  return `${item.var} and ${kpi} change in the same ${unit} (correlation ${num(at, 2)}).`;
}

export function ccfReading(v: CrossCorrelationView): Reading {
  const leads = (v.items ?? []).filter((i) => i.peak_lag != null && i.peak_lag > 0 && (i.bound == null || Math.abs(i.values[i.lags.indexOf(i.peak_lag)] ?? 0) > i.bound));
  if (!leads.length) return { text: `No driver clearly leads ${v.kpi}.`, tone: "neutral" };
  const names = [...new Set(leads.map((l) => l.var))];
  return { text: `${listWords(names)} lead${names.length === 1 ? "s" : ""} ${v.kpi}. ${names.length === 1 ? "Its" : "Their"} effect comes after a delay. Thus, the model must include a carry-over effect.`, tone: "neutral" };
}

export function acfReading(acf: LagProfile | undefined, suggested: Record<string, number> | undefined): Reading {
  if (!acf) return { text: "No series is selected. Select a series.", tone: "neutral" };
  const sig = (acf.significant_lags ?? []).filter((l) => l > 0);
  const parts = [sig.length ? `The values are related to their past values at ${plural(sig.length, "lag")} (${listWords(sig.map(String), 6)}).` : "The values are not related to their past values. Each period is almost independent."];
  if (suggested && (suggested.p !== undefined || suggested.q !== undefined)) parts.push(`Start with these orders: AR ${suggested.p ?? 0}, MA ${suggested.q ?? 0}.`);
  return { text: parts.join(" "), tone: "neutral" };
}

const VERDICT_TEXT = {
  stable: ["returns to a stable level", "return to a stable level"],
  difference: ["wanders (model its changes)", "wander (model their changes)"],
  trend_stable: ["is stable around a trend", "are stable around a trend"],
  unclear: ["is not clear (the tests do not agree)", "are not clear (the tests do not agree)"],
} as const;

export function stationarityReading(v: StationarityView): Reading {
  const rows = v.rows ?? [];
  if (!rows.length) return { text: "The view has no test results for a series.", tone: "neutral" };
  const counts = new Map<string, number>();
  for (const r of rows) counts.set(r.verdict, (counts.get(r.verdict) ?? 0) + 1);
  const parts = (["stable", "trend_stable", "difference", "unclear"] as const).filter((k) => counts.get(k)).map((k) => `${plural(counts.get(k)!, "series", "series")} ${VERDICT_TEXT[k][counts.get(k) === 1 ? 0 : 1]}`);
  const wander = (counts.get("difference") ?? 0) + (counts.get("unclear") ?? 0);
  return { text: `${listWords(parts)}.`, tone: wander ? "warn" : "good" };
}

export function strengthLabel(s: number | null | undefined): string {
  if (s === null || s === undefined) return "unknown";
  return s >= 0.6 ? "strong" : s >= 0.3 ? "moderate" : "weak";
}

export function stlReading(item: StlComponents): Reading {
  const season = item.seasonal_strength;
  const trend = item.trend_strength;
  const parts: string[] = [];
  if (season != null) parts.push(`The repeating ${item.period}-period pattern is ${strengthLabel(season)} (${num(season, 2)})${season >= 0.6 ? ". Include the season in the model" : ""}.`);
  if (trend != null) parts.push(`The longer-term trend is ${strengthLabel(trend)} (${num(trend, 2)}).`);
  return { text: parts.join(" ") || "The view has no strength values.", tone: season != null && season >= 0.6 ? "warn" : "neutral" };
}

export function vifReading(v: VifView): Reading {
  const flagged = v.flagged ?? [];
  if (!flagged.length) return { text: `Each driver gives its own information (all values are less than ${num(v.limit ?? 10, 3)}).`, tone: "good" };
  return { text: `${listWords(flagged)} overlap${flagged.length === 1 ? "s" : ""} too much with the other drivers. The model cannot measure ${flagged.length === 1 ? "its" : "their"} separate effect accurately. Combine the drivers, remove one, or add prior information.`, tone: "warn" };
}

export function groupsReading(v: GroupComparisonView): Reading {
  const differ = (v.items ?? []).filter((i) => i.differs);
  if (!differ.length) return { text: `The ${v.by} groups are similar.`, tone: "neutral" };
  return { text: `${listWords(differ.map((d) => d.var))} ${differ.length === 1 ? "is" : "are"} clearly different between the ${v.by} groups. A hierarchical model gives each ${v.by} its own level. Possibly, this model fits better.`, tone: "neutral" };
}

// ------------------------------------------------------------------------------ models

export function paramsReading(v: ParamTableView): Reading {
  const rows = v.rows ?? [];
  const withCi = rows.filter((r) => r.lower != null && r.upper != null);
  const clear = withCi.filter((r) => (r.lower ?? 0) > 0 || (r.upper ?? 0) < 0);
  const text = withCi.length
    ? `${plural(rows.length, "estimate")}. Of the ${withCi.length} estimates with an interval, ${clear.length} ${clear.length === 1 ? "is" : "are"} clearly different from zero.`
    : `${plural(rows.length, "estimate")}.`;
  return { text: v.omitted ? `${text} The page does not show ${v.omitted} more.` : text, tone: "neutral" };
}

export function diagnosticsReading(v: ModelDiagnosticsView): Reading {
  const s = v.stats ?? {};
  const rhat = s.r_hat_max ?? null;
  const div = s.divergences ?? null;
  const parts: string[] = [];
  let tone: Tone = "good";
  if (rhat !== null) {
    if (rhat <= 1.01) parts.push(`The sampling chains agree (largest R-hat ${num(rhat, 4)}).`);
    else {
      parts.push(`The sampling chains do not agree (largest R-hat ${num(rhat, 4)}, more than 1.01). Run the fit for a longer time before you use the numbers.`);
      tone = rhat > 1.05 ? "bad" : "warn";
    }
  }
  if (div !== null && div > 0) {
    parts.push(`${plural(div, "divergent transition")}. Some estimates can possibly be wrong.`);
    if (tone === "good") tone = "warn";
  }
  const conv = s.converged_share ?? s.converged ?? null;
  if (conv !== null && conv < 1) {
    parts.push(conv === 0 ? "The optimiser did not converge." : `The optimiser did not converge for ${pct(1 - conv)} of the series.`);
    tone = "bad";
  } else if (conv === 1) parts.push("The fit converged.");
  if (!parts.length) parts.push(`The inference method of the fit is ${v.inference}.`);
  return { text: parts.join(" "), tone };
}

/** Share of actual values inside the fitted band, overall (all series). */
export function bandCoverage(v: FitVsActualView): number | null {
  let inside = 0;
  let total = 0;
  (v.series ?? []).forEach((s, k) => {
    const fan = v.fitted?.[k];
    if (!fan || fan.quantiles.length < 2) return;
    const lo = fan.values[0] ?? [];
    const hi = fan.values[fan.values.length - 1] ?? [];
    const at = new Map(fan.coords.map((c, j) => [String(c), j]));
    s.values.forEach((y, i) => {
      const j = at.get(String(s.coords[i]));
      if (y === null || j === undefined) return;
      const a = lo[j];
      const b = hi[j];
      if (a == null || b == null) return;
      total += 1;
      if (y >= a && y <= b) inside += 1;
    });
  });
  return total ? inside / total : null;
}

export function fitReading(v: FitVsActualView): Reading {
  const cov = bandCoverage(v);
  if (cov === null) return { text: "The actual values compared with the fitted values of the model.", tone: "neutral" };
  const level = Math.round(((v.fitted?.[0]?.quantiles.at(-1) ?? 0.95) - (v.fitted?.[0]?.quantiles[0] ?? 0.05)) * 100);
  const tone: Tone = Math.abs(cov * 100 - level) <= 10 ? "good" : "warn";
  return { text: `The ${level}% range of the model contains ${pct(cov)} of the actual values.${tone === "good" ? " This is approximately correct." : cov * 100 < level ? " The range is too narrow: the model is too confident." : " The range is wider than necessary."}`, tone };
}

export function decompositionReading(v: DecompositionView): Reading {
  const shares = Object.entries(v.shares ?? {}).filter((e): e is [string, number] => e[1] !== null && e[1] !== undefined).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  if (!shares.length) return { text: "The parts of the model over time.", tone: "neutral" };
  return { text: `The largest parts of the result are ${listWords(shares.slice(0, 3).map(([k, s]) => `${k} ${pct(s)}`))}.`, tone: "neutral" };
}

export function backtestReading(v: BacktestView): Reading {
  const metric = ["mape", "wape", "mae", "rmse"].find((m) => v.summary?.[m] != null);
  if (!metric) return { text: `The backtest fitted the model again on ${plural(v.folds?.length ?? 0, "fold")}.`, tone: "neutral" };
  const model = v.summary![metric]!;
  const bench = Object.entries(v.benchmarks ?? {})
    .map(([name, m]) => [name, m[metric]] as const)
    .filter((e): e is readonly [string, number] => e[1] != null)
    .sort((a, b) => a[1] - b[1])[0];
  if (!bench) return { text: `The average ${metric.toUpperCase()} on ${plural(v.folds?.length ?? 0, "fold")} is ${num(model, 3)}.`, tone: "neutral" };
  const better = bench[1] > 0 ? 1 - model / bench[1] : 0;
  return better > 0
    ? { text: `On ${plural(v.folds?.length ?? 0, "fold")}, the ${metric.toUpperCase()} of the model is ${pct(better)} lower than the ${metric.toUpperCase()} of the best simple rule (${bench[0].replaceAll("_", " ")}).`, tone: "good" }
    : { text: `The best simple rule (${bench[0].replaceAll("_", " ")}) forecasts equally well or better than the model (${metric.toUpperCase()} ${num(bench[1], 3)} for the rule, ${num(model, 3)} for the model).`, tone: "bad" };
}

export function comparisonReading(v: ComparisonView): Reading {
  const metric = ["mape", "wape", "mae", "rmse", "crps"].find((m) => v.ranking?.[m]?.length);
  if (!metric) return { text: `${plural(v.models.length, "model version")} side by side.`, tone: "neutral" };
  const best = v.ranking![metric]![0]!;
  const tests = (v.tests ?? []).filter((t) => (dmPair(t) as string[]).includes(best));
  const sig = tests.filter((t) => t.reject);
  const tail = tests.length
    ? sig.length
      ? ` Its advantage over ${plural(sig.length, "other version")} is more than chance (Diebold-Mariano).`
      : " But the differences in accuracy can possibly be chance (Diebold-Mariano)."
    : "";
  return { text: `${best} has the lowest ${metric.toUpperCase()}.${tail}`, tone: sig.length ? "good" : "neutral" };
}

/** The two model versions a Diebold-Mariano result compares (`details.model_a`/`model_b`). */
export function dmPair(t: { details?: Record<string, unknown> | undefined; var?: string | null | undefined }): [string, string] | [] {
  const a = t.details?.model_a;
  const b = t.details?.model_b;
  if (typeof a === "string" && typeof b === "string") return [a, b];
  const m = /^(.+) vs (.+)$/.exec(t.var ?? "");
  return m ? [m[1]!, m[2]!] : [];
}

/** The key of a series' suggested ARMA orders in an ACF/PACF view (`var[north]`). */
export function suggestionKey(variable: string, facet: Record<string, unknown> | undefined): string {
  const values = Object.values(facet ?? {});
  return values.length ? `${variable}[${values.map(String).join(",")}]` : variable;
}

export function verdictText(verdict: keyof typeof VERDICT_TEXT): string {
  return VERDICT_TEXT[verdict][0];
}

// ------------------------------------------------------------------------------ scientific data

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function structureReading(v: StructureView): Reading {
  const shapes = v.shapes ?? [];
  if (!shapes.length) return { text: "The explorer found no special shape in the data. The views for tables apply.", tone: "neutral" };
  const geometry = (v.vars ?? []).filter((x) => x.used_as === "geometry").map((x) => x.name);
  const main = shapes[0]!;
  return {
    text: `The data has ${plural(shapes.length, "shape")}. The main shape is: ${main.label_plain}.${shapes.length > 1 ? ` The others: ${listWords(shapes.slice(1).map((s) => s.label_plain), 3)}.` : ""}${geometry.length ? ` ${listWords(geometry)} ${geometry.length === 1 ? "gives" : "give"} the geometry. The statistics do not use ${geometry.length === 1 ? "it" : "them"} as data.` : ""}`,
    tone: "neutral",
  };
}

export function imageReading(e: ImageEntry): Reading {
  const parts: string[] = [];
  const size = `${e.source_height} x ${e.source_width}`;
  const frames = e.frames.length + (e.omitted_frames ?? 0);
  const fs = e.frame_stats?.coords.length ?? frames;
  parts.push(`${e.var} is ${e.kind === "vector" ? "a vector field" : "an image"} of ${size} cells${fs > 1 ? `, with ${count(fs)} frames${e.frame_dim ? ` along ${e.frame_dim}` : ""}` : ""}.`);
  if (e.downsampled) parts.push(`The picture shows block means of ${e.height} x ${e.width} cells.`);
  if ((e.omitted_frames ?? 0) > 0) parts.push(`The slider shows ${e.frames.length} of the ${count(frames)} frames, evenly spaced.`);
  const st = e.stats;
  if (st && isNum(st.min) && isNum(st.max)) parts.push(`The values go from ${num(st.min, 3)} to ${num(st.max, 3)}${e.units ? ` ${e.units}` : ""}.`);
  let tone: Tone = "neutral";
  if (st && isNum(st.missing_share) && st.missing_share > 0) {
    parts.push(`${pct(st.missing_share, st.missing_share < 0.01 ? 1 : 0)} of the cells have no value.`);
    if (st.missing_share > 0.05) tone = "warn";
  }
  if (st && isNum(st.saturated_share) && st.saturated_share >= 0.01) {
    parts.push(`${pct(st.saturated_share, 1)} of the cells are at the maximum value. The detector possibly saturated.`);
    tone = "warn";
  }
  return { text: parts.join(" "), tone };
}

export function profileReading(e: ProfileEntry): Reading {
  const n = e.frames.length + (e.omitted_frames ?? 0);
  const peak = (e.peak ?? []).filter((p) => isNum(p.value));
  const parts = [`${e.var} changes along ${e.along} (${plural(e.coords.length, "point")}${n > 1 ? `, ${plural(n, "curve")}, one for each ${e.frame_dim ?? "frame"}` : ""}).`];
  if (peak.length === 1) parts.push(`The highest value is ${num(peak[0]!.value, 3)} at ${e.along} = ${String(peak[0]!.at)}.`);
  else if (peak.length > 1) {
    const ats = new Set(peak.map((p) => String(p.at)));
    parts.push(ats.size === 1 ? `Each curve has its highest value at ${e.along} = ${String(peak[0]!.at)}.` : `The position of the highest value changes between the curves (from ${e.along} = ${String(peak[0]!.at)} to ${String(peak[peak.length - 1]!.at)}).`);
  }
  if (e.log_x || e.log_y) parts.push(`The plot uses a log scale on the ${e.log_x && e.log_y ? "two axes" : e.log_x ? "horizontal axis" : "vertical axis"}.`);
  return { text: parts.join(" "), tone: "neutral" };
}

export function mapReading(v: MapView, layerVar: string): Reading {
  const parts = [`The map has ${plural(v.areas.length, "area")}.`];
  const isolates = v.isolates ?? [];
  if (isolates.length) parts.push(`${plural(isolates.length, "area")} ${isolates.length === 1 ? "has" : "have"} no neighbour (for example, an island).`);
  const m = (v.moran ?? []).find((r) => r.var === layerVar);
  let tone: Tone = "neutral";
  if (m && isNum(m.morans_i)) {
    const sig = isNum(m.p_value) && m.p_value < 0.05;
    if (sig && m.morans_i > 0) {
      parts.push(`Neighbouring areas have similar values of ${layerVar} (Moran's I = ${num(m.morans_i, 2)}). A model with a spatial effect is possibly necessary.`);
      tone = "warn";
    } else if (sig && m.morans_i < 0) parts.push(`Neighbouring areas have different values of ${layerVar} (Moran's I = ${num(m.morans_i, 2)}).`);
    else parts.push(`The values of ${layerVar} do not cluster in space more than by chance (Moran's I = ${num(m.morans_i, 2)}).`);
    if (m.weights === "knn4") parts.push("The data has no neighbour list. The explorer used the 4 nearest areas as neighbours.");
  }
  return { text: parts.join(" "), tone };
}

export function networkReading(e: NetworkEntry): Reading {
  const parts = [`The network of ${e.var} has ${plural(e.n_nodes, "node")} and ${plural((e.edges ?? []).length + (e.omitted_edges ?? 0), e.directed ? "link" : "tie")}${(e.omitted_nodes ?? 0) > 0 ? ` (the drawing shows the ${e.labels.length} nodes with the most ties)` : ""}.`];
  if (isNum(e.density)) parts.push(`${pct(e.density, e.density < 0.01 ? 1 : 0)} of all possible ties are present.`);
  if (e.components && e.components.count > 1) parts.push(`It has ${plural(e.components.count, "separate part")}; the largest part has ${pct(e.components.largest_share ?? 0)} of the nodes.`);
  if (isNum(e.transitivity) && e.transitivity > 0) parts.push(`When two nodes have a tie with the same node, they also have a tie with each other in ${pct(e.transitivity)} of the cases.`);
  const h = (e.homophily ?? []).filter((x) => isNum(x.within_share) && isNum(x.expected_share) && x.within_share > x.expected_share * 1.2);
  if (h.length) parts.push(`Ties occur more often in the same ${listWords(h.map((x) => x.attr))} than by chance.`);
  return { text: parts.join(" "), tone: "neutral" };
}

export function treeReading(v: TreeView): Reading {
  const parts = [`The tree has ${plural(v.n_tips, "tip")} and ${plural(v.n_nodes, "node")} in all${v.ultrametric ? ". All tips are at the same distance from the root" : ""}.`];
  let tone: Tone = "neutral";
  const roots = v.roots?.length ?? 1;
  if (roots !== 1) {
    parts.push(`It has ${plural(roots, "root")}, not one.`);
    tone = "warn";
  }
  if ((v.unmatched ?? 0) > 0) {
    parts.push(`${plural(v.unmatched ?? 0, "tip")} ${(v.unmatched ?? 0) === 1 ? "has" : "have"} no trait values.`);
    tone = "warn";
  }
  const strong = (v.signal ?? []).filter((s) => isNum(s.p_value) && s.p_value < 0.05 && isNum(s.morans_i) && s.morans_i > 0).map((s) => s.var);
  if (strong.length) parts.push(`Near relatives have similar values of ${listWords(strong)} (a phylogenetic signal). A model must include the tree.`);
  else if ((v.signal ?? []).length) parts.push("The traits do not follow the tree more than by chance.");
  return { text: parts.join(" "), tone };
}

/** The mean direction of a group in the entry's units ("about 270°", "about 14 h"). */
export function circularMean(e: Pick<CircularEntry, "units">, g: CircularGroup): string | null {
  if (!isNum(g.mean_direction)) return null;
  const u = (e.units ?? "").toLowerCase();
  if (u.startsWith("deg") || u === "°") return `${Math.round(g.mean_direction)}°`;
  if (u.startsWith("h")) {
    const h = Math.floor(g.mean_direction);
    const m = Math.round((g.mean_direction - h) * 60);
    return `${String(h).padStart(2, "0")}:${String(m === 60 ? 59 : m).padStart(2, "0")}`;
  }
  return `${num(g.mean_direction, 3)}${e.units ? ` ${e.units}` : ""}`;
}

export function circularReading(e: CircularEntry): Reading {
  const all = e.groups ?? [];
  // with groups, the runner puts the pooled rose "all" first: it is not a group
  const pooled = all.length > 1 ? all.find((g) => String(g.label) === "all") : undefined;
  const groups = pooled ? all.filter((g) => g !== pooled) : all;
  const peaked = groups.filter((g) => isNum(g.rayleigh_p) && g.rayleigh_p < 0.05);
  const word = (e.units ?? "").toLowerCase().startsWith("h") ? "time of day" : "direction";
  if (!groups.length) return { text: `${e.var} has no values.`, tone: "neutral" };
  if (groups.length === 1) {
    const g = groups[0]!;
    const mean = circularMean(e, g);
    return peaked.length && mean
      ? { text: `${e.var} has a preferred ${word}: about ${mean} (the concentration R is ${num(g.resultant_length, 2)}; 0 is no preference, 1 is all values the same).`, tone: "neutral" }
      : { text: `${e.var} has no clear preferred ${word}. The values spread around the circle.`, tone: "neutral" };
  }
  const by = e.group_by ?? "group";
  const parts = [`One rose for each ${by} (${groups.length})${pooled ? ", and one for all the records together" : ""}.`];
  if (peaked.length) parts.push(`${listWords(peaked.map((g) => `${String(g.label)} (about ${circularMean(e, g) ?? "—"})`))} ${peaked.length === 1 ? "has" : "have"} a preferred ${word}.`);
  else parts.push(`No ${by} has a clear preferred ${word}.`);
  return { text: parts.join(" "), tone: "neutral" };
}

export function pointsReading(v: PointsView): Reading {
  const parts = [`${count(v.n)} ${v.n === 1 ? "point" : "points"} along ${v.point_dim}, at ${v.x} and ${v.y}${v.lattice ? " (a grid of cells)" : ""}.`];
  if (v.sample.x.length < v.n) parts.push(`The plot shows an even sample of ${count(v.sample.x.length)} points.`);
  if (v.answers) {
    const agree = v.answers.agreement.filter(isNum);
    const mean = agree.length ? agree.reduce((a, b) => a + b, 0) / agree.length : null;
    parts.push(`For each point, the plot can show the most frequent ${v.answers.var} over ${v.answers.by_dim} (${plural(v.answers.terms.length, "term")})${mean !== null ? `; on average, ${pct(mean)} of the answers agree with it` : ""}.`);
  }
  const ce = v.clark_evans;
  if (ce && isNum(ce.r) && !v.lattice) parts.push(ce.r < 0.9 ? `The points cluster (Clark-Evans R = ${num(ce.r, 2)}).` : ce.r > 1.1 ? `The points are more regular than random (Clark-Evans R = ${num(ce.r, 2)}).` : `The points are spread approximately at random (Clark-Evans R = ${num(ce.r, 2)}).`);
  return { text: parts.join(" "), tone: "neutral" };
}
