/**
 * The budget form and its request: what a planner fills in, turned into an
 * `OptimizeRequest`, and the static feasibility checks of `mapi_spec.optimize.check_feasible`
 * run in the browser so impossible bounds are explained before any run starts.
 */
import type { ChannelBounds, OptimizeRequest, SpendGroup } from "./api";

export type BudgetMode = "same" | "total" | "change" | "free";

export interface ChannelLimit {
  /** Bounds as multiples of today's spend (×) or in money. */
  unit: "multiplier" | "money";
  min: number | null;
  max: number | null;
  locked: boolean;
}

export interface PlanForm {
  mode: BudgetMode;
  total: number | null;
  changePct: number | null;
  objective: string;
  horizon: number | null;
  byGeo: boolean;
  limits: Record<string, ChannelLimit>;
  defaultMin: number;
  defaultMax: number;
  groups: SpendGroup[];
  frontier: boolean;
  frontierPoints: number;
  valuePerKpi: number | null;
  minChannelSpend: number | null;
  maxDraws: number;
  nSteps: number;
  seed: number | null;
}

export const OBJECTIVES: readonly { value: string; label: string; help: string }[] = [
  { value: "mean", label: "Most likely result", help: "Maximise the expected (average) result." },
  { value: "p25", label: "Cautious", help: "Maximise a result you beat 3 times in 4 (the 25th percentile)." },
  { value: "p10", label: "Very cautious", help: "Maximise a result you beat 9 times in 10 (the 10th percentile)." },
  { value: "cvar10", label: "Protect the downside", help: "Maximise the average of the worst 10% of outcomes." },
];

export function defaultForm(channels: readonly string[]): PlanForm {
  return {
    mode: "same",
    total: null,
    changePct: null,
    objective: "mean",
    horizon: null,
    byGeo: false,
    limits: Object.fromEntries(channels.map((c) => [c, { unit: "multiplier", min: null, max: null, locked: false } satisfies ChannelLimit])),
    defaultMin: 0,
    defaultMax: 2,
    groups: [],
    frontier: true,
    frontierPoints: 12,
    valuePerKpi: null,
    minChannelSpend: null,
    maxDraws: 200,
    nSteps: 400,
    seed: null,
  };
}

const OBJECTIVE_RE = /^(?:mean|p(?:[1-9]|[1-4][0-9])|cvar(?:[1-9]|[1-4][0-9]))$/;

function bounds(l: ChannelLimit): ChannelBounds | null {
  if (l.locked) return { locked: true };
  if (l.min === null && l.max === null) return null;
  return l.unit === "money"
    ? { ...(l.min !== null ? { min_spend: l.min } : {}), ...(l.max !== null ? { max_spend: l.max } : {}) }
    : { ...(l.min !== null ? { min_multiplier: l.min } : {}), ...(l.max !== null ? { max_multiplier: l.max } : {}) };
}

/** The request for a form (fields left at their defaults are not sent). */
export function toRequest(f: PlanForm): OptimizeRequest {
  const channels: Record<string, ChannelBounds> = {};
  for (const [ch, l] of Object.entries(f.limits)) {
    const b = bounds(l);
    if (b) channels[ch] = b;
  }
  const req: OptimizeRequest = { objective: f.objective, mode: f.mode === "free" ? "free" : "fixed" };
  if (f.mode === "total" && f.total !== null) req.total_budget = f.total;
  if (f.mode === "change" && f.changePct !== null) req.budget_change_pct = f.changePct;
  if (Object.keys(channels).length) req.channels = channels;
  if (f.defaultMin !== 0 || f.defaultMax !== 2) req.default_bounds = { min_multiplier: f.defaultMin, max_multiplier: f.defaultMax };
  if (f.groups.length) req.groups = f.groups;
  if (f.horizon !== null) req.horizon = f.horizon;
  if (f.byGeo) req.by_geo = true;
  if (f.frontier) req.frontier = { n_points: f.frontierPoints };
  if (f.valuePerKpi !== null) req.value_per_kpi = f.valuePerKpi;
  if (f.minChannelSpend !== null) req.min_channel_spend = f.minChannelSpend;
  if (f.maxDraws !== 200) req.max_draws = f.maxDraws;
  if (f.nSteps !== 400) req.n_steps = f.nSteps;
  if (f.seed !== null) req.seed = f.seed;
  return req;
}

/** The budget the plan spends (`null` in free mode, where the model chooses). */
export function targetBudget(f: PlanForm, currentTotal: number): number | null {
  if (f.mode === "free") return null;
  if (f.mode === "total") return f.total;
  if (f.mode === "change") return currentTotal * (1 + (f.changePct ?? 0) / 100);
  return currentTotal;
}

/**
 * Plain-language problems with a form, before it is sent: field rules of `OptimizeRequest`
 * and the static feasibility of `check_feasible` (minimums above the budget, maximums below
 * it, group shares over 100%). An empty list means the request can be sent.
 */
export function planProblems(f: PlanForm, current: Record<string, number>): string[] {
  const out: string[] = [];
  if (!OBJECTIVE_RE.test(f.objective)) out.push("The goal must be 'mean', 'p1'–'p49' or 'cvar1'–'cvar49'.");
  if (f.mode === "total" && (f.total === null || !(f.total > 0))) out.push("Enter a total budget above zero.");
  if (f.mode === "change" && (f.changePct === null || f.changePct <= -100 || f.changePct > 1000)) out.push("Enter a change between −99% and +1000%.");
  if (f.horizon !== null && (!Number.isInteger(f.horizon) || f.horizon < 1 || f.horizon > 520)) out.push("The horizon is a whole number of periods between 1 and 520.");
  if (f.defaultMin < 0 || f.defaultMax < f.defaultMin) out.push("The default range must run from a minimum of at least 0 up to a larger maximum.");
  for (const [ch, l] of Object.entries(f.limits)) {
    if (l.locked) continue;
    if ((l.min !== null && l.min < 0) || (l.max !== null && l.max < 0)) out.push(`${ch}: limits cannot be negative.`);
    if (l.min !== null && l.max !== null && l.min > l.max) out.push(`${ch}: the minimum is above the maximum.`);
    if (l.unit === "multiplier" && ((l.min ?? 0) > 100 || (l.max ?? 0) > 100)) out.push(`${ch}: multiples of today's spend go up to 100×.`);
  }
  for (const g of f.groups) {
    if (!g.channels.length) out.push(`Group "${g.name}": pick at least one channel.`);
    if (g.min_share != null && g.max_share != null && g.min_share > g.max_share) out.push(`Group "${g.name}": the minimum share is above the maximum.`);
  }
  if (out.length) return out;
  const budget = targetBudget(f, Object.values(current).reduce((a, b) => a + b, 0));
  if (budget === null) return out;
  let lo = 0;
  let hi = 0;
  for (const [ch, spend] of Object.entries(current)) {
    const l = f.limits[ch];
    if (l?.locked) {
      lo += spend;
      hi += spend;
      continue;
    }
    // As the server: a listed channel's unset side is 0× (minimum) or 2× (maximum); an
    // unlisted channel takes the default range.
    const listed = l !== undefined && (l.min !== null || l.max !== null);
    const money = l?.unit === "money";
    let chLo = !listed ? f.defaultMin * spend : money ? (l.min ?? 0) : (l.min ?? 0) * spend;
    if (f.minChannelSpend !== null) chLo = Math.max(chLo, f.minChannelSpend);
    const chHi = !listed ? f.defaultMax * spend : money ? (l.max ?? 2 * spend) : (l.max ?? 2) * spend;
    lo += chLo;
    hi += Math.max(chHi, chLo);
  }
  const fmt = (v: number) => Math.round(v).toLocaleString("en");
  if (lo > budget * (1 + 1e-9)) out.push(`The channel minimums add up to ${fmt(lo)}, more than the budget of ${fmt(budget)}. Lower some minimums or raise the budget.`);
  if (hi < budget * (1 - 1e-9)) out.push(`The channel maximums add up to ${fmt(hi)}, less than the budget of ${fmt(budget)}. Raise some maximums or lower the budget.`);
  const shares = f.groups.reduce((s, g) => s + (g.min_share ?? 0), 0);
  if (shares > 1) {
    const overlap = f.groups.some((a, i) => f.groups.slice(i + 1).some((b) => a.channels.some((c) => b.channels.includes(c))));
    if (!overlap) out.push("The groups' minimum shares add up to more than 100%.");
  }
  return out;
}
