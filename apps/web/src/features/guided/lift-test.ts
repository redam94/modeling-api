/**
 * A geo-lift result as a lift test for the marketing mix (`calibration.lift_tests` of an
 * `mmm.bayesian` spec): the effect the causal fit logged (`effect_total`, `effect_total_sd`,
 * `causal.intervention_start/end`, v0.13 tracking keys) plus the channel and the spend change
 * that only the user knows. Pure, so the report card and the tests share it.
 */
import { stringify } from "yaml";

import type { MetricSeries, RunParam } from "@/api/tracking";

import { slugify, valueAt } from "./session";

/** The `LiftTest` fields this card writes (`mapi_spec.calibration.LiftTest`). */
export interface LiftTestEntry {
  id: string;
  channel: string;
  geo?: string;
  start: string;
  end: string;
  spend_delta: number;
  kpi_delta: number;
  se: number;
}

export interface LiftTestInput {
  channel: string;
  /** The text of the spend field: extra spend in the test, negative for a holdout. */
  spend: string;
  /** Write the treated market as `geo` (only offered with one treated market). */
  useGeo: boolean;
}

export type LiftTestResult = { ok: true; entry: LiftTestEntry; yaml: string } | { ok: false; problems: string[] };

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

/** The run-level value of a metric (the series without labels; per-unit series have a `unit` label). */
export function overallMetric(series: readonly MetricSeries[], key: string): number | null {
  const s = series.find((m) => m.key === key && Object.keys(m.labels ?? {}).length === 0);
  return s && Number.isFinite(s.last) ? s.last : null;
}

export function paramValue(params: readonly RunParam[], key: string): string | null {
  return params.find((p) => p.key === key)?.value ?? null;
}

/** The treated markets of a synthetic-control spec: a list, or the Guided text answer "a, b". */
export function treatedUnits(document: unknown): string[] {
  const v = valueAt(document, "/model/settings/treated");
  const items = Array.isArray(v) ? v.map(String) : typeof v === "string" ? v.split(",") : [];
  return items.map((s) => s.trim()).filter(Boolean);
}

/** Six significant digits: enough for a prior, short enough to read. */
function round(x: number): number {
  return Number(x.toPrecision(6));
}

export function buildLiftTest(
  data: { metrics: readonly MetricSeries[]; params: readonly RunParam[]; treated: readonly string[] },
  input: LiftTestInput,
): LiftTestResult {
  const problems: string[] = [];
  const total = overallMetric(data.metrics, "effect_total");
  const sd = overallMetric(data.metrics, "effect_total_sd");
  const start = paramValue(data.params, "causal.intervention_start") ?? "";
  const end = paramValue(data.params, "causal.intervention_end") ?? "";
  if (total === null || sd === null) problems.push("The fit has no effect metrics. Fit the model again to record them.");
  else if (!(sd > 0)) problems.push("The range of the effect is zero. A lift test needs a range larger than zero.");
  if (!ISO_DATE.test(start) || !ISO_DATE.test(end)) problems.push("A lift test needs a start date and an end date (YYYY-MM-DD). This fit has no end date or uses period numbers.");
  const channel = input.channel.trim();
  if (!channel) problems.push("Type the channel of the campaign.");
  else if (channel.length > 128) problems.push("The channel name has more than 128 characters.");
  const spend = input.spend.trim() === "" ? NaN : Number(input.spend);
  if (!Number.isFinite(spend) || spend === 0) problems.push("Type the spend change as a number that is not zero. Use a negative number for a holdout.");
  if (problems.length) return { ok: false, problems };
  const geo = input.useGeo && data.treated.length === 1 ? data.treated[0] : undefined;
  const entry: LiftTestEntry = {
    id: slugify(`geo-lift-${channel}-${start}`, "geo-lift").slice(0, 63),
    channel,
    ...(geo ? { geo } : {}),
    start,
    end,
    spend_delta: spend,
    kpi_delta: round(total!),
    se: round(sd!),
  };
  return { ok: true, entry, yaml: stringify({ calibration: { lift_tests: [entry] } }) };
}
