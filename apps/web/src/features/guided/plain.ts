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
  bootstrap: { title: "The app prepares a secure workspace", body: "The app processes your data in an isolated sandbox. The sandbox cannot connect to the internet." },
  load_inputs: { title: "The app loads your data", body: "The app reads the dataset that you uploaded. It makes sure that the dataset is complete." },
  validate: { title: "The app does a check of your data", body: "The app makes sure that each column has the data that the model needs." },
  preprocess: { title: "The app prepares the numbers", body: "The app puts the spend and the results on scales that you can compare. It also adds seasonality." },
  fit: { title: "The model learns from your data", body: "The model finds the patterns that explain your results." },
  sample: { title: "The model learns from your data", body: "The model tries thousands of possible explanations. This shows how sure the model can be." },
  score: { title: "The model calculates the answer", body: "The app applies the model to your question." },
  evaluate: { title: "The app tests the model on held-back weeks", body: "The app measures how well the model predicts the periods that it did not see." },
  derive: { title: "The app calculates the results", body: "The app calculates the returns, the contributions and the response curves." },
  plots: { title: "The app makes the charts", body: "The app prepares the charts for your report." },
  upload: { title: "The app saves the results", body: "The app keeps the fitted model in a safe location. You can come back to it later." },
  done: { title: "The app completes the run", body: "The run is almost complete." },
};

export function phaseText(phase: string | null | undefined): PhaseText {
  return (phase && PHASES[phase as RunnerPhase]) || { title: "The run waits for a worker", body: "Your run is in the queue. It starts when a worker is available." };
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
      return reporting ? "Running" : "In the queue";
    case "provisioning":
      return "Start in progress";
    case "running":
      return "Running";
    case "stalled":
      return "Slower than usual";
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
