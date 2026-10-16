/**
 * Business priors and lift tests as a form (plan §11 Guided task 3).
 *
 * Users state beliefs in business units - an ROI range or a share of the KPI per channel, held
 * with a confidence - plus experiment results. This module turns the form into
 * `mapi_spec.calibration.BusinessPriors` / `LiftTest`, explains problems in plain words (the
 * checks of `check_business_priors` and the `LiftTest` validators), and writes the result into
 * the spec document (`apply_prior_translation`).
 */
import type { BusinessPriors, LiftTest, PriorTranslationResult, SpecDocument } from "./types";

export type BeliefMode = "roi" | "share";

export interface ChannelBeliefForm {
  channel: string;
  /** Unchecked: "I have no opinion about this channel". */
  enabled: boolean;
  low: number;
  high: number;
  credibility: number;
}

export interface LiftTestRow {
  key: string;
  channel: string;
  /** "" = every market. */
  geo: string;
  start: string;
  end: string;
  spendDelta: string;
  kpiDelta: string;
  se: string;
}

export interface PriorsForm {
  mode: BeliefMode;
  channels: ChannelBeliefForm[];
  baseline: { enabled: boolean; low: number; high: number; credibility: number };
  liftTests: LiftTestRow[];
}

export interface FormProblem {
  /** `channels/<label>`, `baseline`, `lift/<key>/<field>` or `` for the whole form. */
  where: string;
  message: string;
}

export const ROI_LIMITS = { min: 0, max: 10, step: 0.1 } as const;
export const SHARE_LIMITS = { min: 0, max: 1, step: 0.01 } as const;
export const CREDIBILITY_CHOICES = [
  { value: 0.5, label: "A rough guess (50% sure)" },
  { value: 0.8, label: "Fairly sure (80%)" },
  { value: 0.9, label: "Very sure (90%)" },
  { value: 0.95, label: "Almost certain (95%)" },
] as const;

const DEFAULT_ROI: [number, number] = [0.5, 4];
const DEFAULT_SHARE: [number, number] = [0.02, 0.15];

let rowCounter = 0;
export function newLiftRow(channel = ""): LiftTestRow {
  rowCounter += 1;
  return { key: `lt-${Date.now().toString(36)}-${rowCounter}`, channel, geo: "", start: "", end: "", spendDelta: "", kpiDelta: "", se: "" };
}

function defaults(mode: BeliefMode): [number, number] {
  return mode === "roi" ? DEFAULT_ROI : DEFAULT_SHARE;
}

/** Switch every channel between ROI and share ranges (ranges reset to sensible defaults). */
export function withMode(form: PriorsForm, mode: BeliefMode): PriorsForm {
  if (mode === form.mode) return form;
  const [low, high] = defaults(mode);
  return { ...form, mode, channels: form.channels.map((c) => ({ ...c, low, high })) };
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** A string or number as form text ("" for anything else). */
function text(v: unknown): string {
  return typeof v === "string" ? v : typeof v === "number" && Number.isFinite(v) ? String(v) : "";
}

function pair(v: unknown): [number, number] | null {
  return Array.isArray(v) && v.length === 2 && typeof v[0] === "number" && typeof v[1] === "number" ? [v[0], v[1]] : null;
}

/** The form for `channels`, filled from a spec's `calibration` block when it has one (resume). */
export function formFromCalibration(calibration: unknown, channels: readonly string[]): PriorsForm {
  const cal = isRecord(calibration) ? calibration : {};
  const bp = isRecord(cal.business_priors) ? cal.business_priors : null;
  const stated = bp && isRecord(bp.channels) ? bp.channels : {};
  const anyRoi = Object.values(stated).some((b) => isRecord(b) && pair(b.roi));
  const anyShare = Object.values(stated).some((b) => isRecord(b) && pair(b.contribution_share));
  const mode: BeliefMode = anyShare && !anyRoi ? "share" : "roi";
  const [dl, dh] = defaults(mode);
  const rows = channels.map((channel): ChannelBeliefForm => {
    const b = stated[channel];
    const range = isRecord(b) ? pair(mode === "roi" ? b.roi : b.contribution_share) : null;
    const cred = isRecord(b) && typeof b.credibility === "number" ? b.credibility : 0.8;
    return range ? { channel, enabled: true, low: range[0], high: range[1], credibility: cred } : { channel, enabled: !bp, low: dl, high: dh, credibility: 0.8 };
  });
  const base = bp && isRecord(bp.baseline_share) ? bp.baseline_share : null;
  const baseRange = base ? pair(base.share) : null;
  const liftTests = Array.isArray(cal.lift_tests)
    ? cal.lift_tests.filter(isRecord).map((t): LiftTestRow => {
        const ci = pair(t.ci);
        const se = typeof t.se === "number" ? t.se : ci ? (ci[1] - ci[0]) / (2 * 1.6448536269514722) : null;
        return {
          ...newLiftRow(text(t.channel)),
          geo: text(t.geo),
          start: text(t.start),
          end: text(t.end),
          spendDelta: text(t.spend_delta),
          kpiDelta: text(t.kpi_delta),
          se: se == null ? "" : String(Math.round(se * 1000) / 1000),
        };
      })
    : [];
  return {
    mode,
    channels: rows,
    baseline: baseRange
      ? { enabled: true, low: baseRange[0], high: baseRange[1], credibility: typeof base?.credibility === "number" ? base.credibility : 0.8 }
      : { enabled: false, low: 0.4, high: 0.8, credibility: 0.8 },
    liftTests,
  };
}

const pct = (x: number) => `${Math.round(x * 100)}%`;

/** Problems with the stated beliefs, in plain words (empty: fine to preview and save). */
export function beliefProblems(form: PriorsForm): FormProblem[] {
  const out: FormProblem[] = [];
  for (const c of form.channels) {
    if (!c.enabled) continue;
    const where = `channels/${c.channel}`;
    if (!(Number.isFinite(c.low) && Number.isFinite(c.high))) out.push({ where, message: `Enter both ends of the range for ${c.channel}.` });
    else if (c.low >= c.high) out.push({ where, message: `For ${c.channel}, the low end must be below the high end.` });
    else if (form.mode === "roi" && c.low <= 0) out.push({ where, message: `For ${c.channel}, start the ROI range above zero (a channel that returns nothing at all cannot be described as a range of returns).` });
    else if (form.mode === "share" && (c.low <= 0 || c.high >= 1)) out.push({ where, message: `For ${c.channel}, a share must lie between 0% and 100% (not at either end).` });
  }
  if (form.baseline.enabled) {
    const b = form.baseline;
    if (b.low >= b.high) out.push({ where: "baseline", message: "For the baseline, the low end must be below the high end." });
    else if (b.low <= 0 || b.high >= 1) out.push({ where: "baseline", message: "The baseline share must lie between 0% and 100% (not at either end)." });
  }
  // check_business_priors: the smallest shares cannot add up to more than the whole KPI.
  if (!out.length) {
    const shares = form.mode === "share" ? form.channels.filter((c) => c.enabled) : [];
    let low = shares.reduce((a, c) => a + c.low, 0);
    let high = shares.reduce((a, c) => a + c.high, 0);
    if (form.baseline.enabled) {
      low += form.baseline.low;
      high += form.baseline.high;
    }
    if (low > 1) {
      out.push({ where: "", message: `The smallest shares you gave already add up to ${pct(low)} of the result. Lower some share ranges or the baseline share.` });
    } else if (form.baseline.enabled && shares.length > 0 && shares.length === form.channels.length && high < 1) {
      out.push({ where: "", message: `Even the largest shares you gave explain only ${pct(high)} of the result. Widen the share ranges or the baseline share.` });
    }
  }
  return out;
}

/** `BusinessPriors` for the form (`null` when no belief is stated). */
export function toBusinessPriors(form: PriorsForm, units: { kpi?: string | null; spend?: string | null } = {}): BusinessPriors | null {
  const channels: BusinessPriors["channels"] = {};
  for (const c of form.channels) {
    if (!c.enabled) continue;
    channels[c.channel] = form.mode === "roi" ? { roi: [c.low, c.high], credibility: c.credibility } : { contribution_share: [c.low, c.high], credibility: c.credibility };
  }
  if (!Object.keys(channels).length) return null;
  const out: BusinessPriors = { channels };
  if (form.baseline.enabled) out.baseline_share = { share: [form.baseline.low, form.baseline.high], credibility: form.baseline.credibility };
  if (units.kpi) out.kpi_units = units.kpi;
  if (units.spend) out.spend_units = units.spend;
  return out;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function num(v: string): number | null {
  if (!v.trim()) return null;
  const x = Number(v.replace(/[\s,_]/g, ""));
  return Number.isFinite(x) ? x : null;
}

/** Lift tests from the table rows; rows left completely empty are ignored. */
export function toLiftTests(rows: readonly LiftTestRow[], channels: readonly string[]): { tests: LiftTest[]; problems: FormProblem[] } {
  const tests: LiftTest[] = [];
  const problems: FormProblem[] = [];
  rows.forEach((r, i) => {
    const n = i + 1;
    const blank = !r.channel && !r.start && !r.end && !r.spendDelta && !r.kpiDelta && !r.se;
    if (blank) return;
    const at = (field: string, message: string) => problems.push({ where: `lift/${r.key}/${field}`, message: `Experiment ${n}: ${message}` });
    const before = problems.length;
    if (!channels.includes(r.channel)) at("channel", "choose the channel that was tested.");
    if (!DATE_RE.test(r.start)) at("start", "enter the first day of the test.");
    if (!DATE_RE.test(r.end)) at("end", "enter the last day of the test.");
    else if (DATE_RE.test(r.start) && r.end < r.start) at("end", "the test cannot end before it starts.");
    const spend = num(r.spendDelta);
    const kpi = num(r.kpiDelta);
    const se = num(r.se);
    if (spend === null || spend === 0) at("spendDelta", "enter how much spend changed (negative if the channel was paused).");
    if (kpi === null) at("kpiDelta", "enter the measured change in the result.");
    if (se === null || se <= 0) at("se", "enter the measurement's uncertainty (the ± value, above zero).");
    if (problems.length > before) return;
    tests.push({
      channel: r.channel,
      ...(r.geo ? { geo: r.geo } : {}),
      start: r.start,
      end: r.end,
      spend_delta: spend as number,
      kpi_delta: kpi as number,
      se: se,
    });
  });
  return { tests, problems };
}

/** A stable key for "the same beliefs" (a preview can be reused when nothing changed). */
export function beliefsKey(bp: BusinessPriors | null, tests: readonly LiftTest[]): string {
  return JSON.stringify([bp, tests]);
}

function clone<T>(v: T): T {
  return structuredClone(v);
}

/**
 * The spec document with the calibration block set to the stated beliefs. With a finished
 * translation of exactly these beliefs, its priors are merged into `model.settings.priors`
 * (translated keys win) and the run is recorded (`calibration.prior_translation_run`), as
 * `mapi_spec.calibration.apply_prior_translation` does; otherwise the fit translates them.
 */
export function withCalibration(
  document: SpecDocument,
  bp: BusinessPriors | null,
  tests: readonly LiftTest[],
  translation: { result: PriorTranslationResult; runId: string } | null = null,
): SpecDocument {
  const doc = clone(document);
  const cal: Record<string, unknown> = {};
  if (bp) cal.business_priors = bp;
  if (tests.length) cal.lift_tests = [...tests];
  if (translation && bp) {
    const model = isRecord(doc.model) ? doc.model : {};
    const settings = isRecord(model.settings) ? model.settings : {};
    const priors = isRecord(settings.priors) ? settings.priors : {};
    settings.priors = { ...priors, ...clone(translation.result.priors) };
    model.settings = settings;
    doc.model = model;
    cal.prior_translation_run = translation.runId;
  }
  if (Object.keys(cal).length) doc.calibration = cal;
  else delete doc.calibration;
  return doc;
}

/** "82% of the model's prior lies in your range, close to the 80% you stated." */
export function coverageSentence(coverage: number | undefined, credibility: number): string | null {
  if (coverage === undefined) return null;
  const gap = Math.abs(coverage - credibility);
  const verdict = gap <= 0.05 ? "close to" : coverage < credibility ? "below" : "above";
  return `${pct(coverage)} of what the model believes falls inside your range, ${verdict} the ${pct(credibility)} you stated.`;
}
