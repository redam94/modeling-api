/**
 * The mock API's stand-ins for the Guided logic that runs server-side (mock mode only): role
 * detection (`mapi_spec.guided.suggest_roles`), drafts (`build_draft`), what a sandboxed ingest
 * would make of an upload, and the numbers a fitted geo MMM would report. Close enough to the
 * Python to exercise every screen; it is not the implementation.
 */
import { stringify } from "yaml";

import type { DataSchema, DataSummary, IngestMapping, Issue, Scorecard } from "@/api/types";
import { parseCsv } from "@/lib/csv";
import { pivotMff } from "@/features/guided/profile";
import type { ColumnProfile, GuidedAnswers, GuidedDraft, GuidedQuestion, GuidedTemplate, QuestionSuggestion, RoleSuggestions } from "@/features/guided/types";
import { baselineShare, channelHeadlines, type ResponseGrid } from "@/features/guided/whatif";

// ----------------------------------------------------------------------------- role detection

function tokens(name: string): string[] {
  const spaced = name.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
  return spaced.split(/[^a-z0-9]+|(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])/).filter(Boolean);
}

function score(q: GuidedQuestion, col: ColumnProfile): [number, string[]] {
  if (q.accepts?.length && !q.accepts.includes(col.dtype)) return [0, []];
  let s = 0.1;
  const why: string[] = [];
  const toks = tokens(col.name);
  const flat = toks.join("");
  const keywords = q.keywords ?? [];
  const exact = keywords.find((k) => toks.includes(k));
  if (exact) {
    s += 0.6;
    why.push(`named like a ${exact} column`);
  } else {
    const part = keywords.find((k) => k.length >= 4 && flat.includes(k));
    if (part) {
      s += 0.4;
      why.push(`name mentions ${part}`);
    }
  }
  if (q.dim_spec?.kind === "time" && col.dtype === "date") {
    s += 0.3;
    why.push("contains dates");
    if (col.detected_freq) {
      s += 0.05;
      why.push(`evenly spaced: ${freqPlain(col.detected_freq)}`);
    }
  } else if (q.dim && (col.dtype === "string" || col.dtype === "integer") && col.n_distinct) {
    if (col.n_distinct >= 2 && col.n_distinct <= 1000 && col.n_distinct * 2 <= Math.max(col.n_rows, 1)) {
      s += 0.2;
      why.push(`${col.n_distinct} repeated labels`);
    }
  }
  if (q.role && ["spend", "target", "media"].includes(q.role) && (col.dtype === "number" || col.dtype === "integer")) {
    if (col.min != null && col.min >= 0) {
      s += 0.1;
      why.push("never negative");
    } else if (col.min != null && col.min < 0 && q.role !== "target") {
      s -= 0.2;
      why.push("has negative values");
    }
  }
  if (q.role && col.n_distinct === 1 && col.n_rows > 1) {
    s -= 0.3;
    why.push("always the same value");
  }
  if (col.n_rows && (col.n_missing ?? 0) / col.n_rows > 0.5) {
    s -= 0.3;
    why.push("mostly empty");
  }
  return [Math.max(0, Math.min(1, s)), why];
}

const WEEKDAYS: Record<string, string> = { MON: "Monday", TUE: "Tuesday", WED: "Wednesday", THU: "Thursday", FRI: "Friday", SAT: "Saturday", SUN: "Sunday" };
const FREQ_WORDS: Record<string, string> = { D: "daily", B: "every working day", h: "hourly", H: "hourly", MS: "monthly (on the 1st)", M: "monthly (month ends)", ME: "monthly (month ends)", QS: "quarterly", Q: "quarterly", QE: "quarterly", YS: "yearly", Y: "yearly", YE: "yearly", A: "yearly", AS: "yearly" };

/** Port of `mapi_spec.guided.freq_plain`: a date frequency in words. */
export function freqPlain(freq: string): string {
  const day = freq.startsWith("W-") ? WEEKDAYS[freq.slice(2)] : undefined;
  if (day) return `weekly, on ${day}s`;
  if (freq === "W") return "weekly";
  return FREQ_WORDS[freq.split("-")[0] ?? ""] ?? freq;
}

export function suggestRoles(template: GuidedTemplate, columns: ColumnProfile[]): RoleSuggestions {
  const used = new Set<string>();
  const out: QuestionSuggestion[] = [];
  for (const q of template.questions) {
    if (q.kind !== "column" && q.kind !== "columns") {
      out.push({ question_id: q.id, candidates: [], answer: q.default ?? null });
      continue;
    }
    const ranked = columns
      .map((c) => {
        const [conf, why] = score(q, c);
        return { column: c.name, confidence: Math.round(conf * 1000) / 1000, reason_plain: why.join("; ") || "a possible match" };
      })
      .filter((c) => c.confidence > 0.1)
      .sort((a, b) => b.confidence - a.confidence || (a.column < b.column ? -1 : 1));
    const free = ranked.filter((c) => !used.has(c.column) && c.confidence >= 0.5);
    let answer: string | string[] | null;
    if (q.kind === "column") {
      answer = free[0]?.column ?? null;
      if (answer) used.add(answer);
    } else {
      answer = free.map((c) => c.column);
      answer.forEach((c) => used.add(c));
    }
    out.push({ question_id: q.id, candidates: ranked.slice(0, 20), answer });
  }
  const date = out.find((s) => s.question_id === "date" && s.answer);
  const freq = columns.find((c) => c.name === date?.answer)?.detected_freq ?? "";
  const freqAnswer = freq.startsWith("W") ? "weekly" : freq.startsWith("D") ? "daily" : null;
  for (const s of out) if (s.question_id === "frequency" && freqAnswer) s.answer = freqAnswer;
  const issues: Issue[] = template.questions
    .map((q, i) => [q, out[i]] as const)
    .filter(([q, s]) => q.required !== false && (q.kind === "column" || q.kind === "columns") && (!s?.answer || (Array.isArray(s.answer) && !s.answer.length)))
    .map(([q]) => ({ code: "GUIDED_ANSWERS_INVALID", severity: "warning", pointer: `/answers/${q.id}`, message: "no column looks like an answer to this question; please pick one" }));
  return { template_id: template.id, questions: out, unassigned: columns.map((c) => c.name).filter((c) => !used.has(c)), issues };
}

// ----------------------------------------------------------------------------- drafts

export class DraftError extends Error {
  constructor(
    readonly pointer: string,
    message: string,
  ) {
    super(message);
  }
}

export function safeName(column: string): string {
  let name = column.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "column";
  if (!/^[a-z]/.test(name)) name = `c_${name}`;
  return name.slice(0, 128);
}

const NOISE = new Set(["spend", "cost", "costs", "budget", "investment", "media", "eur", "usd", "gbp", "amount", "total"]);

export function channelLabel(column: string, noise: readonly string[] = []): string {
  const drop = new Set([...NOISE, ...noise.flatMap(tokens)]);
  const kept = tokens(column).filter((t) => !drop.has(t));
  return kept.join("_") || safeName(column);
}

const PLACEHOLDER = /\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}/g;

export function renderTemplate(obj: unknown, values: Record<string, unknown>): unknown {
  if (typeof obj === "string") {
    const whole = /^\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}$/.exec(obj.trim());
    if (whole) {
      const key = whole[1] as string;
      if (!(key in values)) throw new DraftError(`/answers/${key}`, `answer ${key}`);
      return structuredClone(values[key]);
    }
    return obj.replace(PLACEHOLDER, (_, k: string) => {
      if (!(k in values)) throw new DraftError(`/answers/${k}`, `answer ${k}`);
      return String(values[k]);
    });
  }
  if (Array.isArray(obj)) return obj.map((v) => renderTemplate(v, values));
  if (obj && typeof obj === "object") {
    return Object.fromEntries(Object.entries(obj).map(([k, v]) => [renderTemplate(k, values) as string, renderTemplate(v, values)]));
  }
  return obj;
}

export function buildDraft(template: GuidedTemplate, body: GuidedAnswers, project: string): GuidedDraft {
  const profiles = new Map((body.columns ?? []).map((c) => [c.name, c]));
  const answers = body.answers ?? {};
  for (const k of Object.keys(answers)) if (!template.questions.some((q) => q.id === k)) throw new DraftError(`/answers/${k}`, `the template has no question ${k}`);
  const values: Record<string, unknown> = { ...(template.defaults ?? {}) };
  const dims: NonNullable<IngestMapping["dims"]> = {};
  const vars: NonNullable<IngestMapping["vars"]> = {};
  const wide: NonNullable<IngestMapping["wide"]> = {};
  const used = new Map<string, string>();
  const issues: Issue[] = [];
  for (const q of template.questions) {
    const value = answers[q.id] ?? q.default ?? null;
    if (value === null || (Array.isArray(value) && !value.length)) {
      if (q.required !== false && !(q.id in values)) throw new DraftError(`/answers/${q.id}`, `answer '${q.prompt_plain}'`);
      if (q.kind === "columns") values[q.id] = [];
      continue;
    }
    if (q.kind === "column" || q.kind === "columns") {
      const cols = q.kind === "column" ? (typeof value === "string" ? [value] : null) : Array.isArray(value) && value.every((v) => typeof v === "string") ? (value) : null;
      if (!cols) throw new DraftError(`/answers/${q.id}`, q.kind === "column" ? "answer with one column name" : "answer with a list of column names");
      for (const c of cols) {
        if (used.has(c)) throw new DraftError(`/answers/${q.id}`, `column '${c}' already answers '${used.get(c)}'`);
        used.set(c, q.id);
        const p = profiles.get(c);
        if (p && q.accepts?.length && !q.accepts.includes(p.dtype)) {
          issues.push({ code: "GUIDED_ANSWERS_INVALID", severity: "warning", pointer: `/answers/${q.id}`, message: `column '${c}' looks like ${p.dtype}, expected ${q.accepts.join(" or ")}` });
        }
      }
      if (q.dim) {
        const name = safeName(cols[0] as string);
        const p = profiles.get(cols[0] as string);
        const spec = { ...(q.dim_spec ?? { kind: "categorical" }) } as Record<string, unknown>;
        if (spec.kind === "time" && p?.detected_freq) spec.freq = p.detected_freq;
        dims[name] = { column: cols[0] as string, spec: spec as never };
        values[q.id] = name;
      } else if (q.wide_dim && q.as_var) {
        let labels = cols.map((c) => channelLabel(c, q.keywords ?? []));
        if (new Set(labels).size !== labels.length) labels = cols.map(safeName);
        wide[q.as_var] = { dim: q.wide_dim, columns: Object.fromEntries(labels.map((l, i) => [l, cols[i] as string])), role: q.role ?? "covariate", missing: q.missing ?? "forbid" };
        values[q.id] = q.as_var;
      } else {
        const names = cols.map(safeName);
        cols.forEach((c, i) => {
          vars[names[i] as string] = { column: c, role: q.role ?? "covariate", missing: q.missing ?? "forbid" };
        });
        values[q.id] = q.kind === "column" ? names[0] : names;
      }
    } else if (q.kind === "choice") {
      if (!(q.choices ?? []).some((c) => c.value === value)) throw new DraftError(`/answers/${q.id}`, `choose one of ${(q.choices ?? []).map((c) => c.value).join(", ")}`);
      values[q.id] = value;
    } else if (q.kind === "number") {
      if (typeof value !== "number") throw new DraftError(`/answers/${q.id}`, "answer with a number");
      if ((q.minimum != null && value < q.minimum) || (q.maximum != null && value > q.maximum)) throw new DraftError(`/answers/${q.id}`, `answer between ${q.minimum} and ${q.maximum}`);
      values[q.id] = value;
    } else values[q.id] = typeof value === "string" ? value : JSON.stringify(value);
  }
  for (const [name, lk] of Object.entries(template.lookups ?? {})) {
    const src = String(values[lk.source]);
    if (!(src in lk.map)) throw new DraftError(`/answers/${lk.source}`, `no setting for ${src}`);
    values[name] = lk.map[src];
  }
  const datasetRef = `dataset://${project}/${body.dataset}@latest`;
  Object.assign(values, { name: body.name, project, dataset: datasetRef });
  const doc = renderTemplate(template.spec_template, values) as Record<string, unknown>;
  if (body.business_priors || body.lift_tests?.length) {
    doc.calibration = { ...(body.business_priors ? { business_priors: body.business_priors } : {}), ...(body.lift_tests?.length ? { lift_tests: body.lift_tests } : {}) };
  }
  const mapping = { format: body.format ?? "csv", ...(body.format === "mff" ? { preset: "mff" } : {}), dims, vars, ...(Object.keys(wide).length ? { wide } : {}) } as IngestMapping;
  return { template_id: template.id, template_version: template.version ?? "1.0.0", dataset_ref: datasetRef, mapping, spec: doc, yaml: stringify(doc), issues };
}

// ----------------------------------------------------------------------------- ingest

export interface IngestOutcome {
  schema: DataSchema;
  summary: DataSummary;
  /** Totals per time label, for the fit numbers. */
  table: { header: string[]; rows: string[][] };
}

/** What a sandboxed ingest would make of a CSV (or MFF) upload with `mapping`. */
export function ingest(bytes: Uint8Array, mapping: IngestMapping): IngestOutcome {
  const text = new TextDecoder().decode(bytes);
  const csv = parseCsv(text, 100_000);
  let table = { header: csv.header, rows: csv.rows };
  if (mapping.format === "mff" || mapping.preset === "mff") {
    const p = pivotMff({ header: csv.header, rows: csv.rows, truncated: false, mff: null });
    table = { header: p.header, rows: p.rows };
  }
  const col = (name: string) => table.header.indexOf(name);
  const dims: DataSchema["dims"] = {};
  const coords: NonNullable<DataSummary["coords"]> = {};
  const dimNames: string[] = [];
  for (const [name, dm] of Object.entries(mapping.dims ?? {})) {
    dims[name] = dm.spec;
    dimNames.push(name);
    const j = col(dm.column);
    const vals = [...new Set(table.rows.map((r) => r[j] ?? ""))].filter(Boolean).sort();
    coords[name] =
      dm.spec.kind === "time"
        ? { kind: "time", size: vals.length, start: vals[0] ?? null, end: vals[vals.length - 1] ?? null, freq: dm.spec.freq ?? null, contiguous: true }
        : { kind: dm.spec.kind ?? "categorical", size: vals.length, values: vals.slice(0, 1000) };
  }
  const vars: DataSchema["vars"] = {};
  for (const [name, vm] of Object.entries(mapping.vars ?? {})) vars[name] = { dims: dimNames, dtype: "float64", role: vm.role, ...(vm.units ? { units: vm.units } : {}) };
  for (const [name, w] of Object.entries(mapping.wide ?? {})) {
    dims[w.dim] ??= { kind: "categorical", policy: "fixed" };
    const labels = Object.keys(w.columns);
    coords[w.dim] = { kind: "categorical", size: labels.length, values: labels };
    vars[name] = { dims: [...dimNames, w.dim], dtype: "float64", role: w.role };
  }
  return { schema: { layout: "dense", dims, vars }, summary: { vars: Object.keys(vars), coords }, table };
}

// ----------------------------------------------------------------------------- fitted numbers

const DEFAULT_ROI = [2.1, 2.9, 1.3, 1.8, 2.4, 1.1];

function seeded(seed: number): () => number {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return ((s >>> 0) % 1_000_000) / 1_000_000;
  };
}

export interface FitInputs {
  channels: string[];
  geos: string[] | null;
  /** Actual spend per channel over the fit window. */
  spend: number[];
  /** Total KPI over the window. */
  kpiTotal: number;
  /** KPI per period (sorted by time), for the fit plot. */
  kpiSeries: { t: string; y: number }[];
  /** The ROI range midpoints the user stated, per channel. */
  statedRoi: Record<string, number>;
  kpiUnits: string | null;
}

/** A per-draw response grid shaped like a real fit (diminishing returns, joint uncertainty). */
export function synthGrid(inp: FitInputs, draws = 120): ResponseGrid {
  const mults = [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  const rand = seeded(20261015);
  const half = inp.channels.map((_, c) => 0.8 + 0.25 * (c % 3));
  const roi = inp.channels.map((ch, c) => inp.statedRoi[ch] ?? DEFAULT_ROI[c % DEFAULT_ROI.length] ?? 1.5);
  const contribution: number[][][] = [];
  const baseline: number[] = [];
  for (let d = 0; d < draws; d++) {
    const common = 1 + 0.12 * (rand() - 0.5) * 2;
    const perC = inp.channels.map((_, c) => {
      const own = 1 + 0.18 * (rand() - 0.5) * 2;
      const at1 = (roi[c] as number) * (inp.spend[c] as number) * common * own;
      const h = half[c] as number;
      return mults.map((m) => (at1 * (m * (1 + h))) / (m + h));
    });
    contribution.push(perC);
    const media = perC.reduce((a, row) => a + (row[4] as number), 0);
    baseline.push(Math.max(inp.kpiTotal * 0.15, inp.kpiTotal * (1 + 0.02 * (rand() - 0.5)) - media));
  }
  return {
    format: "mapi.response-grid/v1",
    kpi: "y",
    kpi_units: inp.kpiUnits,
    spend_units: inp.kpiUnits,
    periods: inp.kpiSeries.length || 1,
    channels: inp.channels,
    geos: null,
    multipliers: mults,
    draws,
    contribution,
    spend: inp.spend.map((s) => mults.map((m) => s * m)),
    current_spend: inp.spend,
    baseline,
    contribution_by_geo: null,
    spend_by_geo: null,
  };
}

type ScorecardItem = NonNullable<Scorecard["items"]>[number];

/** Pass/warn/fail as `mapi_spec.scorecard` grades a check (`max`, or a band around `target`). */
function grade(value: number, pass: number, warn: number, target?: number): ScorecardItem["status"] {
  const off = target == null ? value : Math.abs(value - target);
  return off <= pass ? "pass" : off <= warn ? "warn" : "fail";
}

/**
 * The media plausibility checks, read from the response grid that the report's headline and
 * channel table summarise, so the screens (and the tutorial screenshots) agree. None without
 * media channels.
 */
function mediaChecks(grid: ResponseGrid | null): Record<string, ScorecardItem> {
  const base = grid?.channels.length ? baselineShare(grid) : null;
  if (!grid || !base) return {};
  const heads = channelHeadlines(grid);
  const pct = (v: number) => `${Math.round(v * 100)}%`;
  const b = base.mean;
  const top = Math.max(...heads.map((h) => h.share?.mean ?? 0));
  const roiMax = Math.max(...heads.map((h) => h.roi?.mean ?? 0));
  const bStatus = grade(b, 0.4, 0.48, 0.5);
  const topStatus = grade(top, 0.6, 0.9);
  const roiStatus = grade(roiMax, 20, 100);
  return {
    "plausibility.roi_absurd": { code: "ROI_ABSURD", check: "plausibility.roi_absurd", category: "plausibility", status: roiStatus, title: "Returns are believable", message_plain: roiStatus === "pass" ? `The best channel returns ${roiMax.toFixed(1)} per unit spent, which is believable.` : `A channel returns ${roiMax.toFixed(1)} per unit spent, which is unusually high.`, metric: "roi_max", value: roiMax, threshold: 20, docs_anchor: "check-plausibility-roi-absurd" },
    "plausibility.share_outlier": { code: "SHARE_OUTLIER", check: "plausibility.share_outlier", category: "plausibility", status: topStatus, title: "No channel dominates", message_plain: topStatus === "pass" ? `No single channel explains most of your results (the largest explains ${pct(top)}).` : `One channel explains ${pct(top)} of your results, more than usual.`, metric: "max_channel_share", value: top, threshold: 0.6, docs_anchor: "check-plausibility-share-outlier" },
    "plausibility.baseline_share": { code: "BASELINE_SHARE", check: "plausibility.baseline_share", category: "plausibility", status: bStatus, title: "Baseline is plausible", message_plain: `About ${pct(b)} of results would happen without any media, which is ${bStatus === "pass" ? "plausible" : "unusual"}.`, suggested_action: bStatus === "pass" ? null : "State a baseline share in business priors, or review the controls.", metric: "baseline_share", value: b, threshold: 0.4, docs_anchor: "check-plausibility-baseline-share" },
  };
}

/**
 * The scorecard of the mock fit: everything passes except a borderline holdout error. The media
 * checks come from `grid`, the fit's response grid; without media channels they are skipped,
 * as the runner skips them.
 */
export function synthScorecard(template: GuidedTemplate, history: number, grid: ResponseGrid | null = null): Scorecard {
  const all: Record<string, NonNullable<Scorecard["items"]>[number]> = {
    "convergence.rhat": { code: "RHAT_OK", check: "convergence.rhat", category: "convergence", status: "pass", title: "Chains agree", message_plain: "All four sampling chains arrived at the same answer.", metric: "r_hat_max", value: 1.004, threshold: 1.01, docs_anchor: "check-convergence-rhat" },
    "convergence.ess": { code: "ESS_OK", check: "convergence.ess", category: "convergence", status: "pass", title: "Enough independent draws", message_plain: "There are enough independent draws for stable numbers.", metric: "ess_bulk_min", value: 812, threshold: 400, docs_anchor: "check-convergence-ess" },
    "convergence.divergences": { code: "DIVERGENCES_OK", check: "convergence.divergences", category: "convergence", status: "pass", title: "Sampler stayed on track", message_plain: "The sampler never lost its way.", metric: "divergence_rate", value: 0, threshold: 0, docs_anchor: "check-convergence-divergences" },
    "holdout.mape": { code: "HOLDOUT_MAPE", check: "holdout.mape", category: "holdout", status: "warn", title: "Holdout error", message_plain: "On the 8 weeks held back, forecasts were off by 12% on average.", suggested_action: "Check the last periods of data for gaps or one-off events, and add holidays or controls that explain them.", metric: "mape", value: 0.12, threshold: 0.1, docs_anchor: "check-holdout-mape" },
    "holdout.coverage_90": { code: "HOLDOUT_COVERAGE", check: "holdout.coverage_90", category: "holdout", status: "pass", title: "Uncertainty is honest", message_plain: "86% of held-back weeks fell inside the 90% range - close to what it promises.", metric: "coverage_90", value: 0.86, threshold: 0.1, docs_anchor: "check-holdout-coverage-90" },
    "plausibility.negative_roi": { code: "NEGATIVE_ROI_OK", check: "plausibility.negative_roi", category: "plausibility", status: "pass", title: "No channel loses money", message_plain: "No channel is likely to return less than it costs.", metric: "negative_roi_share", value: 0.01, threshold: 0.1, docs_anchor: "check-plausibility-negative-roi" },
    ...mediaChecks(grid),
    "data.missing": { code: "MISSING_OK", check: "data.missing", category: "data", status: "pass", title: "Few missing values", message_plain: "No values are missing.", metric: "missing_fraction", value: 0, threshold: 0.05, docs_anchor: "check-data-missing" },
    "data.history": { code: "DATA_HISTORY", check: "data.history", category: "data", status: history >= 104 ? "pass" : "warn", title: "Enough history", message_plain: history >= 104 ? `There are ${(history / 52).toFixed(1)} years of history, enough to learn seasonality.` : `Only ${(history / 52).toFixed(1)} years of history: seasonal effects are uncertain.`, suggested_action: history >= 104 ? null : "Add more history if you have it.", metric: "history_years", value: history / 52, threshold: 2, docs_anchor: "check-data-history" },
    "data.spend_variation": { code: "SPEND_VARIATION_OK", check: "data.spend_variation", category: "data", status: "pass", title: "Spend varies enough", message_plain: "Every channel's spend moves enough to learn from.", metric: "spend_cv_min", value: 0.24, threshold: 0.1, docs_anchor: "check-data-spend-variation" },
  };
  const checks = template.scorecard_checks ?? [];
  const items = checks.map((c) => all[c]).filter((x): x is NonNullable<typeof x> => Boolean(x));
  const skipped: Record<string, string> = {};
  for (const c of checks) if (!all[c] && c.startsWith("plausibility.")) skipped[c] = "the model has no media channels";
  const overall = items.some((i) => i.status === "fail") ? "fail" : items.some((i) => i.status === "warn") ? "warn" : "pass";
  return { overall, items, skipped };
}

/** The totals of the uploaded table that a fit needs. */
export function fitInputs(table: { header: string[]; rows: string[][] }, mapping: IngestMapping, kpiVar: string | null, statedRoi: Record<string, number>, kpiUnits: string | null): FitInputs {
  const col = (name: string) => table.header.indexOf(name);
  const time = Object.values(mapping.dims ?? {}).find((d) => d.spec.kind === "time");
  const geo = Object.values(mapping.dims ?? {}).find((d) => d.spec.kind !== "time");
  const wide = Object.values(mapping.wide ?? {})[0];
  const channels = wide ? Object.keys(wide.columns) : [];
  const kpiCol = kpiVar ? (mapping.vars?.[kpiVar]?.column ?? null) : null;
  const tj = time ? col(time.column) : -1;
  const kj = kpiCol ? col(kpiCol) : -1;
  const per = new Map<string, number>();
  for (const r of table.rows) {
    const y = Number(r[kj]);
    if (Number.isFinite(y)) per.set(r[tj] ?? "", (per.get(r[tj] ?? "") ?? 0) + y);
  }
  const kpiSeries = [...per.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([t, y]) => ({ t, y }));
  const spend = channels.map((ch) => {
    const j = col(wide?.columns[ch] ?? "");
    return table.rows.reduce((a, r) => a + (Number(r[j]) || 0), 0);
  });
  const geos = geo ? [...new Set(table.rows.map((r) => r[col(geo.column)] ?? ""))].sort() : null;
  return { channels, geos, spend, kpiTotal: kpiSeries.reduce((a, p) => a + p.y, 0), kpiSeries, statedRoi, kpiUnits };
}
