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
  if (errors) return { text: `${plural(errors, "serious problem")} to fix before modelling${warnings ? `, and ${plural(warnings, "thing")} to look at` : ""}.`, tone: "bad" };
  if (warnings) return { text: `No serious problems; ${plural(warnings, "thing")} worth a look before modelling.`, tone: "warn" };
  // "good to know" items are not problems, but the page must not say there is nothing above them
  const infos = items.filter((i) => i.severity === "info").length;
  if (infos) return { text: `No problems to fix; ${plural(infos, "thing")} to know.`, tone: "good" };
  return { text: "No data problems found.", tone: "good" };
}

export function qualityScoreReading(score: number | null | undefined): Reading | null {
  if (score === null || score === undefined) return null;
  if (score >= 0.9) return { text: `Data quality score ${pct(score)}: ready to model.`, tone: "good" };
  if (score >= 0.7) return { text: `Data quality score ${pct(score)}: usable, with a few things to check.`, tone: "warn" };
  return { text: `Data quality score ${pct(score)}: fix the problems below first.`, tone: "bad" };
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
  if (lo !== null && hi !== null) parts.push(`Half of the values of ${item.var} lie between ${num(lo, 3)} and ${num(hi, 3)}${mid !== null ? ` (middle value ${num(mid, 3)})` : ""}.`);
  else if (item.mean != null) parts.push(`${item.var} averages ${num(item.mean, 3)}.`);
  let tone: Tone = "neutral";
  if (item.zero_share != null && item.zero_share >= 0.2) {
    parts.push(`${pct(item.zero_share)} of them are zero - common for spend that switches on and off.`);
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
  if (!missing.length) return { text: "Nothing is missing.", tone: "good" };
  const worst = missing[0]!;
  const gaps = Object.entries(v.longest_gap ?? {}).filter(([, g]) => g > 0).sort((a, b) => b[1] - a[1]);
  const gap = gaps[0];
  return {
    text: `${listWords(missing.map(([k, s]) => `${k} (${pct(s, s < 0.01 ? 1 : 0)})`))} ${missing.length === 1 ? "has" : "have"} missing values; ${worst[0]} the most.${gap ? ` The longest gap is ${plural(gap[1], "period")} in ${gap[0]}.` : ""}`,
    tone: worst[1] > 0.05 ? "warn" : "neutral",
  };
}

export function outliersReading(v: OutliersView): Reading {
  const items = (v.items ?? []).filter((i) => i.count > 0).sort((a, b) => b.count - a.count);
  if (!items.length) return { text: "No unusual values stand out.", tone: "good" };
  const total = items.reduce((s, i) => s + i.count, 0);
  return {
    text: `${plural(total, "value")} stand${total === 1 ? "s" : ""} out, most in ${items[0]!.var}. Check whether they are errors or real events (a launch, a holiday); real events deserve a control variable.`,
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
  if (!strong.length) return { text: "No two variables move closely together, so their effects should be easy to tell apart.", tone: "good" };
  const top = strong[0]!;
  return {
    text: `${top.a} and ${top.b} move ${strengthWord(top.r)} ${top.r > 0 ? "together" : "in opposite directions"} (${num(top.r, 2)})${strong.length > 1 ? `, and ${plural(strong.length - 1, "other pair")} also do` : ""}. Variables that move together are hard for a model to separate.`,
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
  const dir = at > 0 ? "rises" : "falls";
  if (peak > 0) return `${item.var} leads: ${kpi} ${dir} ${plural(peak, unit)} after it moves (correlation ${num(at, 2)}).`;
  if (peak < 0) return `${item.var} follows ${kpi} by ${plural(-peak, unit)} (correlation ${num(at, 2)}): it may react to results rather than drive them.`;
  return `${item.var} and ${kpi} move in the same ${unit} (correlation ${num(at, 2)}).`;
}

export function ccfReading(v: CrossCorrelationView): Reading {
  const leads = (v.items ?? []).filter((i) => i.peak_lag != null && i.peak_lag > 0 && (i.bound == null || Math.abs(i.values[i.lags.indexOf(i.peak_lag)] ?? 0) > i.bound));
  if (!leads.length) return { text: `No driver clearly leads ${v.kpi}.`, tone: "neutral" };
  return { text: `${listWords([...new Set(leads.map((l) => l.var))])} lead${leads.length === 1 ? "s" : ""} ${v.kpi}: their effect shows up after a delay, so the model should allow for carry-over.`, tone: "neutral" };
}

export function acfReading(acf: LagProfile | undefined, suggested: Record<string, number> | undefined): Reading {
  if (!acf) return { text: "No series selected.", tone: "neutral" };
  const sig = (acf.significant_lags ?? []).filter((l) => l > 0);
  const parts = [sig.length ? `Values are linked to their own past at ${plural(sig.length, "lag")} (${listWords(sig.map(String), 6)}).` : "Values are not linked to their own past: each period is close to independent."];
  if (suggested && (suggested.p !== undefined || suggested.q !== undefined)) parts.push(`Suggested starting orders: AR ${suggested.p ?? 0}, MA ${suggested.q ?? 0}.`);
  return { text: parts.join(" "), tone: "neutral" };
}

const VERDICT_TEXT = {
  stable: ["returns to a stable level", "return to a stable level"],
  difference: ["wanders (model its changes)", "wander (model their changes)"],
  trend_stable: ["is stable around a trend", "are stable around a trend"],
  unclear: ["is unclear (the tests disagree)", "are unclear (the tests disagree)"],
} as const;

export function stationarityReading(v: StationarityView): Reading {
  const rows = v.rows ?? [];
  if (!rows.length) return { text: "No series were tested.", tone: "neutral" };
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
  if (season != null) parts.push(`The repeating ${item.period}-period pattern is ${strengthLabel(season)} (${num(season, 2)})${season >= 0.6 ? ": model the season explicitly" : ""}.`);
  if (trend != null) parts.push(`The longer-term trend is ${strengthLabel(trend)} (${num(trend, 2)}).`);
  return { text: parts.join(" ") || "No strengths were measured.", tone: season != null && season >= 0.6 ? "warn" : "neutral" };
}

export function vifReading(v: VifView): Reading {
  const flagged = v.flagged ?? [];
  if (!flagged.length) return { text: `Every driver has its own information (all below ${num(v.limit ?? 10, 3)}).`, tone: "good" };
  return { text: `${listWords(flagged)} overlap${flagged.length === 1 ? "s" : ""} so much with the others that ${flagged.length === 1 ? "its" : "their"} separate effect will be poorly measured. Combine them, drop one, or add prior knowledge.`, tone: "warn" };
}

export function groupsReading(v: GroupComparisonView): Reading {
  const differ = (v.items ?? []).filter((i) => i.differs);
  if (!differ.length) return { text: `The ${v.by} groups look alike.`, tone: "neutral" };
  return { text: `${listWords(differ.map((d) => d.var))} differ${differ.length === 1 ? "s" : ""} clearly between ${v.by} groups: a model that lets each ${v.by} have its own level (a hierarchical model) may fit better.`, tone: "neutral" };
}

// ------------------------------------------------------------------------------ models

export function paramsReading(v: ParamTableView): Reading {
  const rows = v.rows ?? [];
  const withCi = rows.filter((r) => r.lower != null && r.upper != null);
  const clear = withCi.filter((r) => (r.lower ?? 0) > 0 || (r.upper ?? 0) < 0);
  const text = withCi.length
    ? `${plural(rows.length, "estimate")}; ${clear.length} of the ${withCi.length} with an interval ${clear.length === 1 ? "is" : "are"} clearly different from zero.`
    : `${plural(rows.length, "estimate")}.`;
  return { text: v.omitted ? `${text} ${v.omitted} more are not shown.` : text, tone: "neutral" };
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
      parts.push(`The sampling chains disagree (largest R-hat ${num(rhat, 4)}, above 1.01): run the fit longer before trusting the numbers.`);
      tone = rhat > 1.05 ? "bad" : "warn";
    }
  }
  if (div !== null && div > 0) {
    parts.push(`${plural(div, "divergent transition")}: some estimates may be off.`);
    if (tone === "good") tone = "warn";
  }
  const conv = s.converged_share ?? s.converged ?? null;
  if (conv !== null && conv < 1) {
    parts.push(conv === 0 ? "The optimiser did not converge." : `The optimiser did not converge for ${pct(1 - conv)} of the series.`);
    tone = "bad";
  } else if (conv === 1) parts.push("The fit converged.");
  if (!parts.length) parts.push(`Fitted by ${v.inference}.`);
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
  if (cov === null) return { text: "Actual values against what the model fitted.", tone: "neutral" };
  const level = Math.round(((v.fitted?.[0]?.quantiles.at(-1) ?? 0.95) - (v.fitted?.[0]?.quantiles[0] ?? 0.05)) * 100);
  const tone: Tone = Math.abs(cov * 100 - level) <= 10 ? "good" : "warn";
  return { text: `The model's ${level}% range contains ${pct(cov)} of the actual values${tone === "good" ? ": about what it should." : cov * 100 < level ? ": too narrow - the model is overconfident." : ": wider than needed."}`, tone };
}

export function decompositionReading(v: DecompositionView): Reading {
  const shares = Object.entries(v.shares ?? {}).filter((e): e is [string, number] => e[1] !== null && e[1] !== undefined).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  if (!shares.length) return { text: "The model's parts over time.", tone: "neutral" };
  return { text: `The biggest parts of the result: ${listWords(shares.slice(0, 3).map(([k, s]) => `${k} ${pct(s)}`))}.`, tone: "neutral" };
}

export function backtestReading(v: BacktestView): Reading {
  const metric = ["mape", "wape", "mae", "rmse"].find((m) => v.summary?.[m] != null);
  if (!metric) return { text: `${plural(v.folds?.length ?? 0, "fold")} refitted.`, tone: "neutral" };
  const model = v.summary![metric]!;
  const bench = Object.entries(v.benchmarks ?? {})
    .map(([name, m]) => [name, m[metric]] as const)
    .filter((e): e is readonly [string, number] => e[1] != null)
    .sort((a, b) => a[1] - b[1])[0];
  if (!bench) return { text: `Average ${metric.toUpperCase()} over ${plural(v.folds?.length ?? 0, "fold")}: ${num(model, 3)}.`, tone: "neutral" };
  const better = bench[1] > 0 ? 1 - model / bench[1] : 0;
  return better > 0
    ? { text: `Across ${plural(v.folds?.length ?? 0, "fold")} the model's ${metric.toUpperCase()} is ${pct(better)} lower than the best simple rule (${bench[0].replaceAll("_", " ")}).`, tone: "good" }
    : { text: `The best simple rule (${bench[0].replaceAll("_", " ")}) forecasts as well as or better than the model (${metric.toUpperCase()} ${num(bench[1], 3)} against ${num(model, 3)}).`, tone: "bad" };
}

export function comparisonReading(v: ComparisonView): Reading {
  const metric = ["mape", "wape", "mae", "rmse", "crps"].find((m) => v.ranking?.[m]?.length);
  if (!metric) return { text: `${plural(v.models.length, "model version")} side by side.`, tone: "neutral" };
  const best = v.ranking![metric]![0]!;
  const tests = (v.tests ?? []).filter((t) => (dmPair(t) as string[]).includes(best));
  const sig = tests.filter((t) => t.reject);
  const tail = tests.length
    ? sig.length
      ? ` Its edge over ${plural(sig.length, "other version")} is more than chance (Diebold-Mariano).`
      : " But the differences in accuracy could be chance (Diebold-Mariano)."
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
