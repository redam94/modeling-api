/**
 * What-if sliders answered in the browser (plan §11 Guided task 6).
 *
 * A port of `mapi_spec.whatif.evaluate_scenario`, the reference implementation, over the fit's
 * `ui/grids/response_curves.json` (`mapi.response-grid/v1`, `mapi_spec.views.ResponseGridView`).
 * The contract:
 *
 * 1. a channel without a multiplier keeps 1; multipliers outside the grid are refused
 *    (`SCENARIO_OUT_OF_RANGE`) - the grid is never extrapolated;
 * 2. per draw and channel, the contribution is interpolated linearly between the two grid
 *    points around the multiplier; so is spend;
 * 3. per draw, KPI = baseline + the channels' contributions and the change is taken against all
 *    multipliers at 1; means and quantiles are taken over draws *after* summing, so the band
 *    stays joint across channels.
 *
 * `whatif.test.ts` replays the golden cases the Python spec tests export
 * (`packages/spec/tests/snapshots/whatif_golden.json`).
 */

export const RESPONSE_GRID_FORMAT = "mapi.response-grid/v1";
export const RESPONSE_GRID_VIEW = "ui/grids/response_curves.json";
export const MAX_GRID_DRAWS = 500;
export const MAX_GRID_CELLS = 2_000_000;
export const DEFAULT_LEVELS = [0.05, 0.5, 0.95] as const;

/** `mapi_spec.views.ResponseGridView`. */
export interface ResponseGrid {
  format: typeof RESPONSE_GRID_FORMAT;
  kpi: string;
  kpi_units: string | null;
  spend_units: string | null;
  periods: number;
  channels: string[];
  geos: string[] | null;
  multipliers: number[];
  draws: number;
  /** `[draw][channel][multiplier]` */
  contribution: number[][][];
  /** `[channel][multiplier]` */
  spend: number[][];
  current_spend: number[];
  baseline: number[] | null;
  /** `[draw][channel][multiplier][geo]` */
  contribution_by_geo: number[][][][] | null;
  /** `[geo][channel][multiplier]` */
  spend_by_geo: number[][][] | null;
}

export interface WhatIfScenario {
  multipliers?: Record<string, number>;
  geo?: string | null;
  label?: string | null;
}

/** `mapi_spec.whatif.SummaryStats`: mean and quantiles over draws. */
export interface SummaryStats {
  mean: number;
  levels: number[];
  quantiles: number[];
}

export interface ChannelOutcome {
  multiplier: number;
  spend: number;
  contribution: SummaryStats;
}

/** `mapi_spec.whatif.WhatIfResult`. */
export interface WhatIfResult {
  channels: Record<string, ChannelOutcome>;
  total_spend: number;
  spend_delta: number;
  kpi: SummaryStats | null;
  kpi_delta: SummaryStats;
  roi_delta: SummaryStats | null;
  draws: number;
}

/** A refused scenario or an invalid grid (`code` as the server would report it). */
export class WhatIfError extends Error {
  override name = "WhatIfError";
  constructor(
    message: string,
    readonly code: "SCENARIO_OUT_OF_RANGE" | "VIEW_INVALID",
    readonly pointer: string | null = null,
  ) {
    super(message);
  }
}

// ----------------------------------------------------------------------------- parsing

function invalid(message: string): never {
  throw new WhatIfError(`response grid: ${message}`, "VIEW_INVALID");
}

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function nums(v: unknown, n: number, what: string): number[] {
  if (!Array.isArray(v) || v.length !== n || !v.every(isNum)) invalid(`${what} must be ${n} numbers`);
  return v;
}

function strings(v: unknown, what: string, max: number): string[] {
  if (!Array.isArray(v) || !v.every((s) => typeof s === "string") || v.length > max) invalid(`${what} must be a list of names`);
  if (new Set(v).size !== v.length) invalid(`${what} must be unique`);
  return v;
}

/** Validate a `ui/grids/response_curves.json` document (the shape checks of the Python view). */
export function parseResponseGrid(raw: unknown): ResponseGrid {
  if (typeof raw !== "object" || raw === null) invalid("not an object");
  const g = raw as Record<string, unknown>;
  if (g.format !== RESPONSE_GRID_FORMAT) invalid(`unknown format ${JSON.stringify(g.format)}`);
  if (typeof g.kpi !== "string") invalid("kpi must be a name");
  const channels = strings(g.channels, "channels", 64);
  if (!channels.length) invalid("channels must not be empty");
  const geos = g.geos == null ? null : strings(g.geos, "geos", 256);
  const draws = g.draws;
  if (!Number.isInteger(draws) || (draws as number) < 1 || (draws as number) > MAX_GRID_DRAWS) invalid(`draws must be 1-${MAX_GRID_DRAWS}`);
  const nd = draws as number;
  if (!Array.isArray(g.multipliers) || g.multipliers.length < 2 || !g.multipliers.every(isNum)) invalid("multipliers must be numbers");
  const mult = g.multipliers;
  if (mult.some((m, i) => m < 0 || (i > 0 && m <= (mult[i - 1] as number)))) invalid("multipliers must be unique, ascending and non-negative");
  if (!mult.some((m) => Math.abs(m - 1) < 1e-9)) invalid("multipliers must include 1 (the actual spend)");
  const nc = channels.length;
  const nm = mult.length;
  if (nd * nc * nm * (geos?.length || 1) > MAX_GRID_CELLS) invalid("the grid is too large");
  if (!Array.isArray(g.contribution) || g.contribution.length !== nd) invalid("contribution must be [draw][channel][multiplier]");
  const contribution = (g.contribution as unknown[]).map((perC) => {
    if (!Array.isArray(perC) || perC.length !== nc) invalid("contribution must be [draw][channel][multiplier]");
    return perC.map((row) => nums(row, nm, "contribution rows"));
  });
  if (!Array.isArray(g.spend) || g.spend.length !== nc) invalid("spend must be [channel][multiplier]");
  const spend = (g.spend as unknown[]).map((row) => nums(row, nm, "spend rows"));
  const current = nums(g.current_spend, nc, "current_spend");
  const baseline = g.baseline == null ? null : nums(g.baseline, nd, "baseline");
  if ((g.contribution_by_geo != null || g.spend_by_geo != null) && !geos?.length) invalid("per-geo grids need geos");
  let byGeo: number[][][][] | null = null;
  let spendByGeo: number[][][] | null = null;
  if (geos?.length) {
    const ng = geos.length;
    if (g.contribution_by_geo != null) {
      const by = g.contribution_by_geo;
      if (!Array.isArray(by) || by.length !== nd) invalid("contribution_by_geo must be [draw][channel][multiplier][geo]");
      byGeo = (by as unknown[]).map((perC) => {
        if (!Array.isArray(perC) || perC.length !== nc) invalid("contribution_by_geo must be [draw][channel][multiplier][geo]");
        return perC.map((row) => {
          if (!Array.isArray(row) || row.length !== nm) invalid("contribution_by_geo must be [draw][channel][multiplier][geo]");
          return row.map((cell) => nums(cell, ng, "contribution_by_geo cells"));
        });
      });
    }
    if (g.spend_by_geo != null) {
      const sg = g.spend_by_geo;
      if (!Array.isArray(sg) || sg.length !== ng) invalid("spend_by_geo must be [geo][channel][multiplier]");
      spendByGeo = (sg as unknown[]).map((perC) => {
        if (!Array.isArray(perC) || perC.length !== nc) invalid("spend_by_geo must be [geo][channel][multiplier]");
        return perC.map((row) => nums(row, nm, "spend_by_geo rows"));
      });
    }
  }
  const periods = g.periods;
  if (!Number.isInteger(periods) || (periods as number) < 1) invalid("periods must be a positive integer");
  return {
    format: RESPONSE_GRID_FORMAT,
    kpi: g.kpi,
    kpi_units: typeof g.kpi_units === "string" ? g.kpi_units : null,
    spend_units: typeof g.spend_units === "string" ? g.spend_units : null,
    periods: periods as number,
    channels,
    geos,
    multipliers: mult,
    draws: nd,
    contribution,
    spend,
    current_spend: current,
    baseline,
    contribution_by_geo: byGeo,
    spend_by_geo: spendByGeo,
  };
}

// ----------------------------------------------------------------------------- the interpolation

/** Python's `bisect.bisect_left`. */
function bisectLeft(xs: readonly number[], x: number): number {
  let lo = 0;
  let hi = xs.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if ((xs[mid] as number) < x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Linear interpolation of `ys` over `xs` at `x` (inside the grid; exact at grid points). */
export function interp(xs: readonly number[], ys: readonly number[], x: number): number {
  const i = bisectLeft(xs, x);
  if (i < xs.length && xs[i] === x) return ys[i] as number;
  const lo = i - 1;
  const x0 = xs[lo] as number;
  const x1 = xs[i] as number;
  const y0 = ys[lo] as number;
  const y1 = ys[i] as number;
  const t = (x - x0) / (x1 - x0);
  return y0 + t * (y1 - y0);
}

/** Linear-interpolation quantile of sorted values (numpy's default). */
export function quantile(sorted: readonly number[], q: number): number {
  const n = sorted.length;
  const pos = q * (n - 1);
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, n - 1);
  return (sorted[lo] as number) + (pos - lo) * ((sorted[hi] as number) - (sorted[lo] as number));
}

/** Neumaier-compensated sum (close to Python's `math.fsum`). */
export function fsum(values: readonly number[]): number {
  let sum = 0;
  let c = 0;
  for (const v of values) {
    const t = sum + v;
    c += Math.abs(sum) >= Math.abs(v) ? sum - t + v : v - t + sum;
    sum = t;
  }
  return sum + c;
}

export function summarize(values: readonly number[], levels: readonly number[] = DEFAULT_LEVELS): SummaryStats {
  const sorted = [...values].sort((a, b) => a - b);
  return { mean: fsum(values) / values.length, levels: [...levels], quantiles: levels.map((q) => quantile(sorted, q)) };
}

/** The index of multiplier 1 in the grid. */
export function oneIndex(grid: Pick<ResponseGrid, "multipliers">): number {
  const i = grid.multipliers.findIndex((x) => Math.abs(x - 1) < 1e-9);
  if (i < 0) invalid("multipliers must include 1");
  return i;
}

/** Answer `scenario` from `grid` (see the module doc); throws {@link WhatIfError}. */
export function evaluateScenario(grid: ResponseGrid, scenario: WhatIfScenario, levels: readonly number[] = DEFAULT_LEVELS): WhatIfResult {
  const multipliers = scenario.multipliers ?? {};
  const unknown = Object.keys(multipliers).filter((ch) => !grid.channels.includes(ch)).sort();
  if (unknown.length) {
    throw new WhatIfError(`unknown channels ${unknown.join(", ")}; the grid has ${grid.channels.join(", ")}`, "SCENARIO_OUT_OF_RANGE", "/multipliers");
  }
  const xs = grid.multipliers;
  const lo = xs[0] as number;
  const hi = xs[xs.length - 1] as number;
  for (const [ch, m] of Object.entries(multipliers)) {
    if (!Number.isFinite(m) || m < 0) throw new WhatIfError(`the multiplier of ${ch} must be a finite number >= 0`, "SCENARIO_OUT_OF_RANGE", `/multipliers/${ch}`);
    if (!(lo <= m && m <= hi)) {
      throw new WhatIfError(
        `the multiplier of ${ch} (${m}) is outside the precomputed range [${lo}, ${hi}]; use 'Run exact' for it`,
        "SCENARIO_OUT_OF_RANGE",
        `/multipliers/${ch}`,
      );
    }
  }
  let contrib: number[][][];
  let spend: number[][];
  let baseline: number[] | null;
  if (scenario.geo != null) {
    const g = grid.geos?.indexOf(scenario.geo) ?? -1;
    if (g < 0 || !grid.contribution_by_geo) {
      throw new WhatIfError(`the grid has no per-geo curves for ${scenario.geo}`, "SCENARIO_OUT_OF_RANGE", "/geo");
    }
    contrib = grid.contribution_by_geo.map((perC) => perC.map((row) => row.map((cell) => cell[g] as number)));
    spend = grid.spend_by_geo ? (grid.spend_by_geo[g] as number[][]) : grid.spend;
    baseline = null; // the baseline is not split by geo
  } else {
    contrib = grid.contribution;
    spend = grid.spend;
    baseline = grid.baseline;
  }
  const one = oneIndex(grid);
  const mult = grid.channels.map((ch) => multipliers[ch] ?? 1);
  const newSpend = mult.map((m, c) => interp(xs, spend[c] as number[], m));
  const curSpend = grid.channels.map((_, c) => (spend[c] as number[])[one] as number);
  const perChannel: number[][] = grid.channels.map(() => []);
  const kpiDelta: number[] = [];
  const kpi: number[] = [];
  for (let d = 0; d < grid.draws; d++) {
    let totalNew = 0;
    let totalCur = 0;
    const draw = contrib[d] as number[][];
    mult.forEach((m, c) => {
      const row = draw[c] as number[];
      const v = interp(xs, row, m);
      (perChannel[c] as number[]).push(v);
      totalNew += v;
      totalCur += row[one] as number;
    });
    kpiDelta.push(totalNew - totalCur);
    if (baseline) kpi.push((baseline[d] as number) + totalNew);
  }
  const spendDelta = fsum(newSpend) - fsum(curSpend);
  const roi = Math.abs(spendDelta) > 1e-12 ? summarize(kpiDelta.map((k) => k / spendDelta), levels) : null;
  return {
    channels: Object.fromEntries(
      grid.channels.map((ch, c) => [
        ch,
        { multiplier: mult[c] as number, spend: newSpend[c] as number, contribution: summarize(perChannel[c] as number[], levels) },
      ]),
    ),
    total_spend: fsum(newSpend),
    spend_delta: spendDelta,
    kpi: kpi.length ? summarize(kpi, levels) : null,
    kpi_delta: summarize(kpiDelta, levels),
    roi_delta: roi,
    draws: grid.draws,
  };
}

// ----------------------------------------------------------------------------- headline numbers

export interface ChannelHeadline {
  channel: string;
  spend: number;
  /** KPI gained per spend unit over the fit window (per draw summarised). */
  roi: SummaryStats | null;
  /** The channel's share of the expected KPI (needs the grid's baseline). */
  share: SummaryStats | null;
  contribution: SummaryStats;
}

/**
 * ROI and contribution share per channel at the actual spend (multiplier 1), summarised per
 * draw so each interval is a real posterior interval. Shares need the grid's baseline.
 */
export function channelHeadlines(grid: ResponseGrid, levels: readonly number[] = DEFAULT_LEVELS): ChannelHeadline[] {
  const one = oneIndex(grid);
  const totals = grid.contribution.map((perC, d) => {
    const media = fsum(perC.map((row) => row[one] as number));
    return grid.baseline ? (grid.baseline[d] as number) + media : null;
  });
  return grid.channels.map((channel, c) => {
    const spend = grid.current_spend[c] as number;
    const contrib = grid.contribution.map((perC) => (perC[c] as number[])[one] as number);
    const shares = totals.every((t): t is number => t !== null && t !== 0) ? contrib.map((v, d) => v / (totals[d] as number)) : null;
    return {
      channel,
      spend,
      roi: spend > 0 ? summarize(contrib.map((v) => v / spend), levels) : null,
      share: shares ? summarize(shares, levels) : null,
      contribution: summarize(contrib, levels),
    };
  });
}

/** The baseline's share of the expected KPI (per draw summarised), when the grid has one. */
export function baselineShare(grid: ResponseGrid, levels: readonly number[] = DEFAULT_LEVELS): SummaryStats | null {
  if (!grid.baseline) return null;
  const one = oneIndex(grid);
  const shares = grid.contribution.map((perC, d) => {
    const base = grid.baseline?.[d] as number;
    const total = base + fsum(perC.map((row) => row[one] as number));
    return total === 0 ? 0 : base / total;
  });
  return summarize(shares, levels);
}

/** Slider bounds for the grid (multipliers never leave the precomputed range). */
export function sliderRange(grid: Pick<ResponseGrid, "multipliers">): { min: number; max: number; step: number } {
  const min = grid.multipliers[0] as number;
  const max = grid.multipliers[grid.multipliers.length - 1] as number;
  return { min, max, step: 0.05 };
}
