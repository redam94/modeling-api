/**
 * Money and compute formatting (`mapi_spec.cost`): amounts are integer **micros** of a
 * currency (1 USD = 1,000,000 micros), compute is vCPU-seconds.
 */
import type { RunCost } from "@/api/types";

export const MICROS_PER_UNIT = 1_000_000;

/**
 * `31_200` USD micros → `$0.0312`. Amounts under ten cents keep up to four decimals, and amounts
 * under a hundredth of a cent two significant digits (`47` → `$0.000047`), so a run that cost
 * something is never shown as `$0.00`; larger amounts use two.
 */
export function formatMicros(micros: number | null | undefined, currency: string | null | undefined = "USD"): string {
  if (micros == null || !Number.isFinite(micros)) return "—";
  const value = micros / MICROS_PER_UNIT;
  const abs = Math.abs(value);
  const digits =
    abs === 0 ? 2 : abs < 0.0001 ? Math.min(8, Math.ceil(-Math.log10(abs)) + 1) : abs < 0.1 ? 4 : abs < 1 ? 3 : 2;
  try {
    return new Intl.NumberFormat("en", {
      style: "currency",
      currency: currency || "USD",
      minimumFractionDigits: abs === 0 ? 2 : Math.min(digits, 2),
      maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return `${value.toFixed(digits)} ${currency ?? ""}`.trim();
  }
}

/** `5400` → `1.5 vCPU·h`; under an hour stays in seconds (`930 vCPU·s`). */
export function formatVcpuSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (Math.abs(seconds) < 3600) return `${trimNumber(seconds, seconds < 10 ? 2 : 0)} vCPU·s`;
  return `${trimNumber(seconds / 3600, 1)} vCPU·h`;
}

/** `90_000` → `25 GiB·h`; under an hour stays in seconds (`52 GiB·s`), like compute. */
export function formatGibSeconds(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (Math.abs(seconds) < 3600) return `${trimNumber(seconds, seconds < 10 ? 2 : 0)} GiB·s`;
  return `${trimNumber(seconds / 3600, 1)} GiB·h`;
}

/**
 * The usage report's ``group_by=token`` key for runs started without an API token (browser
 * and CLI sessions): ``"-"`` (``mapi_spec.cost``); older servers sent an empty key.
 */
export const SESSION_GROUP_KEY = "-";

export function isSessionGroup(key: string): boolean {
  return key === SESSION_GROUP_KEY || key === "" || key === "none" || key === "session";
}

/** Compact counts for axis labels: `12 300` → `12.3k`. */
export function compactNumber(v: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

function trimNumber(v: number, digits: number): string {
  return Number(v.toFixed(digits)).toLocaleString("en");
}

export interface CostView {
  /** The amount to show (actual when metered, else the estimate). */
  text: string;
  /** `true` until the run's first attempt has been metered. */
  estimated: boolean;
  /** Hover text: price table, attempts, compute. */
  detail: string;
}

/**
 * How to show a run's cost. v0.3 servers fill `amount_micros` (plus `price_version`); v0.2
 * servers only had `usd`; a local single-tenant server meters nothing.
 */
export function runCostView(cost: RunCost | null | undefined): CostView | null {
  if (!cost) return null;
  const currency = cost.currency ?? "USD";
  const estimated = cost.estimated ?? true;
  let micros: number | null = null;
  if (cost.amount_micros != null && !(estimated && cost.amount_micros === 0 && cost.estimate_micros != null)) {
    micros = cost.amount_micros;
  } else if (cost.estimate_micros != null) {
    micros = cost.estimate_micros;
  } else if (cost.usd != null) {
    micros = Math.round(cost.usd * MICROS_PER_UNIT);
  }
  if (micros == null) return null;
  const parts = [
    estimated ? "Estimate before metering" : "Metered",
    cost.price_version ? `price table ${cost.price_version}` : null,
    cost.vcpu_seconds != null ? formatVcpuSeconds(cost.vcpu_seconds) : null,
    cost.attempts_billed ? `${cost.attempts_billed} attempt${cost.attempts_billed === 1 ? "" : "s"} billed` : null,
  ].filter(Boolean);
  return { text: `${estimated ? "≈ " : ""}${formatMicros(micros, currency)}`, estimated, detail: parts.join(" · ") };
}
