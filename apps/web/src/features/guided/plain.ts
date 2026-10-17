/**
 * Plain-language wording for Guided mode: run phases, numbers with their uncertainty, run
 * states. Analysts read "about 3 minutes left", "between 1.8 and 3.2", not `eta_seconds` or
 * `hdi_90`.
 */
import type { ModelVersion, RunState, RunnerPhase } from "@/api/types";

export interface PhaseText {
  title: string;
  body: string;
}

const PHASES: Record<RunnerPhase, PhaseText> = {
  bootstrap: { title: "Getting a secure workspace ready", body: "Your data is processed in an isolated sandbox that can't reach the internet." },
  load_inputs: { title: "Loading your data", body: "Reading the dataset you uploaded and checking it is intact." },
  validate: { title: "Checking your data", body: "Making sure every column has what the model needs." },
  preprocess: { title: "Preparing the numbers", body: "Putting spend and results on comparable scales and adding seasonality." },
  fit: { title: "Learning from your data", body: "The model looks for the patterns that explain your results." },
  sample: { title: "Learning from your data", body: "The model tries thousands of plausible explanations to measure how sure it can be." },
  score: { title: "Working out the answer", body: "Applying the model to your question." },
  evaluate: { title: "Testing on held-back weeks", body: "Checking how well the model predicts periods it did not see." },
  derive: { title: "Calculating the results", body: "Working out returns, contributions and response curves." },
  plots: { title: "Drawing the charts", body: "Preparing the charts for your report." },
  upload: { title: "Saving the results", body: "Storing the fitted model safely so you can come back to it." },
  done: { title: "Finishing up", body: "Almost there." },
};

export function phaseText(phase: string | null | undefined): PhaseText {
  return (phase && PHASES[phase as RunnerPhase]) || { title: "Waiting for a worker", body: "Your run is in the queue and starts as soon as a worker is free." };
}

/** The order phases normally happen in (for the checklist on the run step). */
export const PHASE_ORDER: readonly RunnerPhase[] = ["bootstrap", "load_inputs", "preprocess", "sample", "evaluate", "plots", "upload"];

/** Map a phase to its slot in {@link PHASE_ORDER} (`fit` and `sample` share one; `score` and `derive` sit with `evaluate`). */
export function phaseSlot(phase: string | null | undefined): number {
  if (!phase) return -1;
  const alias: Record<string, RunnerPhase> = { fit: "sample", validate: "load_inputs", score: "evaluate", derive: "evaluate", done: "upload" };
  return PHASE_ORDER.indexOf(alias[phase] ?? (phase as RunnerPhase));
}

export function etaText(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  if (seconds < 45) return "less than a minute left";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `about ${minutes} minute${minutes === 1 ? "" : "s"} left`;
  const hours = Math.round((seconds / 3600) * 10) / 10;
  return `about ${hours} hours left`;
}

/** A run state in plain words (`reporting`: the worker already sends progress). */
export function stateText(state: RunState, reporting = false): string {
  switch (state) {
    case "created":
    case "queued":
    case "dispatching":
      return reporting ? "Running" : "Waiting to start";
    case "provisioning":
      return "Starting up";
    case "running":
      return "Running";
    case "stalled":
      return "Taking longer than usual";
    case "finalizing":
      return "Saving the results";
    case "succeeded":
      return "Finished";
    case "partial":
      return "Stopped early";
    case "failed":
      return "Did not finish";
    case "rejected":
      return "Could not start";
    case "cancelling":
      return "Stopping";
    case "cancelled":
      return "Cancelled";
  }
}

// ----------------------------------------------------------------------------- numbers

const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const plain2 = new Intl.NumberFormat("en", { maximumFractionDigits: 2 });

/** 1234567 → "1.2M", 0.034 → "0.03", with units ("EUR 1.2M"). */
export function amount(v: number | null | undefined, units?: string | null): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const s = Math.abs(v) >= 10_000 ? compact.format(v) : plain2.format(v);
  return units ? `${units} ${s}` : s;
}

const compactAt = [1, 2, 3].map((d) => new Intl.NumberFormat("en", { notation: "compact", minimumFractionDigits: d === 1 ? 0 : d, maximumFractionDigits: d }));

function amountAt(v: number | null | undefined, units: string | null | undefined, level: number): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const s = Math.abs(v) >= 10_000 ? compactAt[level]!.format(v) : plain2.format(v);
  return units ? `${units} ${s}` : s;
}

/**
 * {@link amount} with the fewest extra digits (up to three in "1.305M") at which each pair of
 * different values reads differently, so a range never shows as "1.3M – 1.3M". Use one
 * formatter for numbers shown side by side.
 */
export function distinctAmounts(pairs: readonly (readonly [number, number])[]): (v: number | null | undefined, units?: string | null) => string {
  const apart = pairs.filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && a !== b);
  const level = [0, 1, 2].find((l) => apart.every(([a, b]) => amountAt(a, null, l) !== amountAt(b, null, l))) ?? 0;
  return (v, units) => amountAt(v, units, level);
}

export function signedAmount(v: number, units?: string | null): string {
  const s = amount(Math.abs(v), units);
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

export function share(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function signedShare(v: number, digits = 1): string {
  const s = share(Math.abs(v), digits);
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

export function ratio(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return plain2.format(v);
}

/** "tv", "tv and search", "tv, search and social". */
export function listWords(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** "between 1.8 and 3.2" from the 5% and 95% quantiles of a summary. */
export function between(lo: number | undefined, hi: number | undefined, fmt: (v: number) => string): string {
  if (lo === undefined || hi === undefined) return "";
  return `between ${fmt(lo)} and ${fmt(hi)}`;
}

/** The 5% and 95% quantiles of a `DistributionSummary` (keys "0.05", "0.95"). */
export function band(quantiles: Record<string, number> | undefined): [number, number] | null {
  if (!quantiles) return null;
  const entries = Object.entries(quantiles)
    .map(([k, v]) => [Number(k), v] as const)
    .filter(([k, v]) => Number.isFinite(k) && Number.isFinite(v))
    .sort((a, b) => a[0] - b[0]);
  const first = entries[0];
  const last = entries[entries.length - 1];
  return first && last && entries.length >= 2 ? [first[1], last[1]] : null;
}

export function multiplierText(m: number): string {
  if (Math.abs(m - 1) < 1e-9) return "as spent";
  if (m === 0) return "switched off";
  const pct = Math.round((m - 1) * 100);
  return pct > 0 ? `+${pct}%` : `${pct}%`;
}

/** Metrics `mapi_sdk` reports in percent (`metrics.json`, `ModelVersion.metrics`). */
const PERCENT_METRICS = new Set(["mape", "smape", "wape"]);

/**
 * A holdout metric as a fraction (0.008 for 0.8%), ready for {@link share}: the scorecard's
 * measured value when it has the check (its values are fractions), else the model version's
 * metric, divided by 100 when `mapi_sdk` reports it in percent (`mape`, `smape`, `wape`).
 */
export function holdoutFraction(mv: Pick<ModelVersion, "metrics" | "scorecard">, metric: string): number | null {
  const item = mv.scorecard?.items?.find((i) => i.metric === metric && i.value != null);
  if (item?.value != null) return item.value;
  const raw = mv.metrics?.[metric];
  if (raw == null || !Number.isFinite(raw)) return null;
  return PERCENT_METRICS.has(metric) ? raw / 100 : raw;
}
