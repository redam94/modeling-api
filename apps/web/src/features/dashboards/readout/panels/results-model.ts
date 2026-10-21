/**
 * The pure model of what the layout 2 inputs send and give back (v0.17, plan §3.2-§3.4), loaded
 * only with the lazy parts of the readout: the predictions of tried cases and the summary of a
 * scored file in words, and the accuracy of uploaded actuals in words (the server's numbers;
 * the web recomputes no layout 2 metric). The browser's checks of what the inputs send are in
 * `inputs-model.ts`.
 */
import { fixed, grouped, MISSING } from "@/components/ods/format";

import type {
  AccuracyDetail,
  AccuracyPoint,
  BatchSummary,
  ClientDashboard,
  PredictionResult,
} from "../../types";
import { rangeWords, unitText } from "../panel-model";
import { periodOf, plural } from "../readout-model";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);

// ------------------------------------------------------------------------------ predictions and batches

export interface PredictionLine {
  output: string;
  centre: string;
  range: string | null;
  label: string | null;
  probability: string | null;
}

/** The outputs of one tried case in words. */
export function predictionLines(result: PredictionResult, row: number, d: Pick<ClientDashboard, "kpi" | "subject">): PredictionLine[] {
  const outs = result.rows?.[row]?.outputs ?? {};
  const unit = d.subject?.unit ?? d.kpi.unit;
  return Object.entries(outs).map(([name, v]) => {
    const fmt = (x: number) => (unit === "probability" ? `${fixed(x * 100, 1)}%` : unitText(x, unit, d));
    return {
      output: name,
      centre: isNum(v.centre) ? fmt(v.centre) : MISSING,
      range: result.band === "none" ? null : rangeWords(v.low, v.high, fmt),
      label: v.label ?? null,
      probability: isNum(v.probability) ? `${fixed(v.probability * 100, 1)}%` : null,
    };
  });
}

/** The note under a prediction: what the range means (predictive, average or none). */
export function bandNote(band: PredictionResult["band"]): string | null {
  if (band === "outcome") return "The likely range shows where one new result will probably be. The model is 90% sure that the result is in this range.";
  if (band === "mean") return "The range shows the average, not single results.";
  return null;
}

/**
 * The words for a model output in a client's text: the subject's label for the subject (or for the
 * only output), else the name in plain words ("y_hat" never shows).
 */
export function outputWords(name: string, d: Pick<ClientDashboard, "kpi" | "subject">, outputs = 1): string {
  const subject = d.subject?.variable ?? d.kpi.variable;
  if (name === subject || outputs === 1) return d.subject?.label ?? d.kpi.label ?? "The prediction";
  const words = name.replace(/_hat$/, "").replace(/[_\s]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "The prediction";
}

/** The summary lines of a scored file ("spread of predictions", never an interval). */
export function batchLines(b: BatchSummary, d: Pick<ClientDashboard, "kpi" | "subject">): string[] {
  const lines = [`The model scored ${grouped(b.n_scored)} of ${grouped(b.n_rows)} ${plural("case", b.n_rows)}.`];
  if (isNum(b.share_positive)) {
    const pos = d.subject?.positive_label;
    lines.push(`${fixed(b.share_positive * 100, 1)}% of the cases are ${pos ? `likely "${pos}"` : "above the threshold"}.`);
  }
  const unit = d.subject?.unit ?? d.kpi.unit;
  const stats = Object.entries(b.stats ?? {});
  for (const [name, s] of stats) {
    const fmt = (x: number) => unitText(x, unit, d);
    if (!isNum(s.p50)) continue;
    const spread = isNum(s.p10) && isNum(s.p90) ? `; most are between ${fmt(s.p10)} and ${fmt(s.p90)}` : "";
    lines.push(`${outputWords(name, d, stats.length)}: the middle prediction is ${fmt(s.p50)}${spread}. This is the spread of the predictions, not a likely range.`);
  }
  return lines;
}

/** "predictions-weekly-churn-12.csv". */
export function predictionsFileName(title: string, seq: number): string {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60) || "dashboard";
  return `predictions-${slug}-${seq}.csv`;
}

// ------------------------------------------------------------------------------ accuracy

/** A share (0..1) in percent. */
const pct = (v: Num) => (isNum(v) ? `${fixed(v * 100, 1)}%` : MISSING);
/** A measure the server already gives in percent (mape, wape, smape, band_hit_rate, an effect's relative). */
const inPct = (v: Num) => (isNum(v) ? `${fixed(v, 1)}%` : MISSING);

/** The accuracy of an upload in plain words, by kind (the server's numbers). */
export function accuracyDetailLines(a: AccuracyDetail, d: Pick<ClientDashboard, "kpi" | "subject" | "period_label">): string[] {
  const fmt = (v: Num) => (isNum(v) ? unitText(v, d.subject?.unit ?? d.kpi.unit, d) : MISSING);
  const period = periodOf(d);
  if (a.kind === "error") {
    const n = a.n ?? 0;
    const lines = [`We compared ${n} ${plural(period === "case" ? "case" : period, n)} with the predictions.`];
    if (isNum(a.mae)) lines.push(`The typical error is ${fmt(a.mae)}.`);
    if (isNum(a.mape)) lines.push(`On average the prediction was ${fixed(a.mape, 1)}% from the actual number.`);
    if (isNum(a.bias)) lines.push(a.bias > 0 ? `The predictions were ${fmt(a.bias)} too high on average.` : a.bias < 0 ? `The predictions were ${fmt(-a.bias)} too low on average.` : "The predictions were not too high or too low on average.");
    if (isNum(a.band_hit_rate)) lines.push(`The actual number was in the likely range ${inPct(a.band_hit_rate)} of the time.`);
    return lines;
  }
  if (a.kind === "classification") {
    const n = a.n ?? 0;
    const lines = [`We compared ${n} ${plural("case", n)} with the predictions.`];
    if (isNum(a.accuracy)) lines.push(`The model was right ${pct(a.accuracy)} of the time.`);
    if (isNum(a.rate_pred) && isNum(a.rate_obs)) lines.push(`It predicted ${pct(a.rate_pred)}${a.positive ? ` "${a.positive}"` : ""}; the actual share was ${pct(a.rate_obs)}.`);
    if (isNum(a.threshold)) lines.push(`A case counts as${a.positive ? ` "${a.positive}"` : " positive"} when its probability is ${pct(a.threshold)} or more.`);
    return lines;
  }
  const lines: string[] = [];
  if (isNum(a.total)) lines.push(`The change added about ${fmt(a.total)}${isNum(a.low) && isNum(a.high) ? ` (likely ${fmt(a.low)} to ${fmt(a.high)})` : ""} over ${a.n_periods ?? 0} ${plural(period, a.n_periods ?? 0)}.`);
  if (isNum(a.relative)) lines.push(`That is ${fixed(Math.abs(a.relative), 1)}% ${a.relative >= 0 ? "more" : "less"} than without it.`);
  if (a.note) lines.push(a.note);
  return lines;
}

/** The technical measures of an upload (name, value) for the visually quiet table under the words. */
export function accuracyMeasures(a: AccuracyDetail, d: Pick<ClientDashboard, "kpi" | "subject">): { label: string; value: string }[] {
  const num = (v: Num) => (isNum(v) ? unitText(v, d.subject?.unit ?? d.kpi.unit, d) : MISSING);
  const dec = (v: Num) => fixed(v, 3);
  if (a.kind === "error")
    return [
      { label: "Typical error (MAE)", value: num(a.mae) },
      { label: "RMSE", value: num(a.rmse) },
      { label: "MAPE", value: inPct(a.mape) },
      { label: "WAPE", value: inPct(a.wape) },
      { label: "R²", value: dec(a.r2) },
      { label: "Inside the likely range", value: inPct(a.band_hit_rate) },
    ].filter((m) => m.value !== MISSING);
  if (a.kind === "classification")
    return [
      { label: "Accuracy", value: pct(a.accuracy) },
      { label: "Brier score", value: dec(a.brier) },
      { label: "Log score", value: dec(a.log_score) },
      { label: "AUC", value: dec(a.auc) },
    ].filter((m) => m.value !== MISSING);
  return [
    { label: "Effect", value: num(a.total) },
    { label: "Relative effect", value: inPct(a.relative) },
  ].filter((m) => m.value !== MISSING);
}

/** The metric of the accuracy history to draw, by kind (the first that has values). */
export function historyMetric(points: readonly AccuracyPoint[]): string | null {
  const order = ["mae", "mape", "accuracy", "brier", "effect_total", "rmse", "band_hit_rate", "auc"];
  for (const k of order) if (points.some((p) => isNum(p.values?.[k]))) return k;
  for (const p of points) for (const [k, v] of Object.entries(p.values ?? {})) if (isNum(v)) return k;
  return null;
}

export const HISTORY_METRIC_LABEL: Record<string, string> = {
  mae: "Typical error",
  rmse: "RMSE",
  mape: "Average error (%)",
  wape: "WAPE",
  smape: "SMAPE",
  bias: "Bias",
  r2: "R²",
  band_hit_rate: "Inside the likely range",
  accuracy: "Right predictions",
  brier: "Brier score",
  log_score: "Log score",
  auc: "AUC",
  effect_total: "Effect",
  effect_relative: "Relative effect",
};
