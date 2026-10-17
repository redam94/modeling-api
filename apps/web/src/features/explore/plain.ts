/**
 * Plain-language readings of explorer views: what a non-statistician should take away from a
 * section, from its numbers alone (pure functions; `plain.test.ts`). The thresholds are the
 * ones the docs explain (seasonal strength above 0.6 is strong, a VIF above 10 is poorly
 * identified, a band of ±1.96/√n for correlations, R-hat at most 1.01).
 */
import type {
  BacktestView,
  ComparisonView,
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
import { num, pct } from "./ui";

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

export function distributionReading(item: VarDistribution): Reading {
  const parts: string[] = [];
  const lo = q(item, 0.25);
  const mid = q(item, 0.5);
  const hi = q(item, 0.75);
  if (lo !== null && hi !== null) parts.push(`Half of the values of ${item.var} are between ${num(lo, 3)} and ${num(hi, 3)}${mid !== null ? ` (the middle value is ${num(mid, 3)})` : ""}.`);
  else if (item.mean != null) parts.push(`The mean of ${item.var} is ${num(item.mean, 3)}.`);
  let tone: Tone = "neutral";
  if (item.zero_share != null && item.zero_share >= 0.2) {
    parts.push(`${pct(item.zero_share)} of the values are zero. This is usual for spend that starts and stops.`);
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
  const strong = correlationPairs(v).filter((p) => Math.abs(p.r) >= 0.7);
  if (!strong.length) return { text: "No two variables change closely together. Thus, it is easy for a model to find the effect of each variable.", tone: "good" };
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
