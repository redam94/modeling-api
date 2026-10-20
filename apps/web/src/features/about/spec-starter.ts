/**
 * A starter spec for a model picked from "Help me choose" or the catalog: the model with its
 * major version, placeholders for the data and its About (what it needs) as comments, so live
 * validation can guide the rest.
 */
import { STARTER_SPEC } from "@/features/editor/starter";

import type { About } from "./types";

/** One line of catalog text, safe inside a YAML comment. */
function commentLine(text: string): string {
  return text.replace(/[\r\n\u2028\u2029]+/g, " ").replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 200);
}

/** `1.4.2` → `^1` (any 1.x). */
export function majorRange(version: string | null | undefined): string {
  const major = /^(\d+)\./.exec(version ?? "")?.[1];
  return `^${major ?? "1"}`;
}

/** Shared tail of the extension MMM starters: the fit and the outputs (plan I-8 / I-9). */
function extensionTail(derived: string[], plots: string[]): string[] {
  return [
    "  fit:",
    "    mode: evaluate_then_refit",
    "    tune: 1000",
    "    draws: 1000",
    "    chains: 4",
    "    seed: 20261014",
    "outputs:",
    `  derived: [${derived.join(", ")}]`,
    `  plots: [${plots.join(", ")}]`,
    "execution: {resource_class: M, timeout: 45m}",
    "",
  ];
}

/** A national weekly MMM with a measure between spend and results (visits, search, awareness). */
const NESTED_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-nested",
  "  project: default",
  "data:",
  "  # One national series. Each row is one week and one channel or one mediator label.",
  "  # The mediator column can have empty cells (a survey that you do not do each week).",
  "  # The variable is `mediators`: a variable cannot have the same name as its dim.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {time: date, channel: channel, mediator: mediator}",
  "    vars: {y: revenue, spend: spend, mediators: mediator_value}",
  "  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}",
  "pipeline:",
  "  # Spend and results add up in a week; the mediators are averaged, and a week with no value stays empty.",
  "  - id: weekly",
  "    use: core.resample@^1",
  "    with: {dim: time, freq: W-SUN, how: sum, how_vars: {mediators: mean}, skipna: true, full_periods_only: true}",
  "model:",
  "  use: mmm.nested@^1",
  "  settings:",
  "    trend: {kind: linear}",
  "    seasonality: {yearly_order: 2}",
  "    # Empty: every mediator label with the default settings.",
  "    mediators: []",
  ...extensionTail(
    ["contributions", "response_curves", "marginal_cost", "adstock", "mediation"],
    ["core.ppc_fan", "core.convergence", "mmm.decomposition", "mmm.response_curves", "mmm.mediation", "mmm.roi"],
  ),
].join("\n");

/** A national weekly MMM with two or more results (products or KPIs) in one model. */
const MULTIVARIATE_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-multivariate",
  "  project: default",
  "data:",
  "  # One national series. Each row is one week, one channel and one outcome label.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {time: date, channel: channel, outcome: outcome}",
  "    vars: {y: sales, spend: spend}",
  "  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}",
  "pipeline:",
  "  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}",
  "model:",
  "  use: mmm.multivariate@^1",
  "  settings:",
  "    trend: {kind: linear}",
  "    seasonality: {yearly_order: 2}",
  "    # Empty: every outcome label, each with weight 1 in a budget plan.",
  "    outcomes: []",
  "    # For example: [{source: product_a, target: product_b, kind: cannibalization}].",
  "    # When you add one, also add cross_effects to outputs.derived and mmm.cross_effects to",
  "    # outputs.plots. Without cross-effects, that output is empty and the plot is skipped.",
  "    cross_effects: []",
  ...extensionTail(
    ["contributions", "response_curves", "marginal_cost", "adstock"],
    ["core.ppc_fan", "core.convergence", "mmm.decomposition", "mmm.response_curves", "mmm.roi"],
  ),
].join("\n");

/** The fit of the causal starters: no holdout (the effect window is the test), four chains. */
const CAUSAL_FIT = "  fit: {mode: train_only, tune: 500, draws: 500, chains: 4, seed: 20261020}";

/** A Bayesian structural time series: trend, a yearly pattern and optional regressors. */
const BSTS_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-bsts",
  "  project: default",
  "data:",
  "  # One series. Each row is one period. The target can have empty cells.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {t: date}",
  "    vars: {y: sales}",
  "  split: {kind: time_holdout, dim: t, last: 8}",
  "model:",
  "  use: ts.bsts@^1",
  "  settings:",
  "    trend: local_linear",
  "    # 52.18 for weekly data, 7 for daily data, 12 for monthly data.",
  "    seasonality: [{period: 52.18, harmonics: 3}]",
  "    # Columns that you also know for the future, for example [price].",
  "    regressors: []",
  "  fit: {mode: evaluate_then_refit, tune: 500, draws: 500, chains: 4, seed: 20261020}",
  "outputs:",
  "  plots: [core.ppc_fan, core.decomposition, core.convergence]",
  "execution: {resource_class: S, timeout: 30m}",
  "",
].join("\n");

/** The effect of a campaign on one series (CausalImpact): the model learns the series before the start. */
const IMPACT_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-impact",
  "  project: default",
  "data:",
  "  # One series. Each row is one week (or day), before and after the start of the campaign.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {time: week}",
  "    vars: {y: sales}",
  "model:",
  "  use: causal.impact@^1",
  "  settings:",
  "    # The first date of the campaign (YYYY-MM-DD). Empty end: until the end of the data.",
  '    intervention_start: "2025-06-02"',
  '    intervention_end: ""',
  "    # Columns that the campaign did not change, for example [sales_other_regions].",
  "    controls: []",
  "    seasonality: [{period: 52.18, harmonics: 3}]",
  CAUSAL_FIT,
  "outputs:",
  "  plots: [causal.impact, causal.pre_fit, core.convergence]",
  "execution: {resource_class: S, timeout: 30m}",
  "",
].join("\n");

/** A geo test (synthetic control): the treated markets against a weighted copy made from the other markets. */
const SYNTHETIC_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-geo-lift",
  "  project: default",
  "data:",
  "  # Each row is one market and one week, before and after the start of the test.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {unit: geo, time: week}",
  "    vars: {y: sales}",
  "model:",
  "  use: causal.synthetic_control@^1",
  "  settings:",
  "    # The markets that got the campaign. The other markets are the donors.",
  "    treated: [geo_03]",
  '    intervention_start: "2025-06-02"',
  '    intervention_end: "2025-08-25"',
  CAUSAL_FIT,
  "outputs:",
  "  plots: [causal.impact, causal.donor_weights, causal.pre_fit, core.convergence]",
  "execution: {resource_class: S, timeout: 30m}",
  "",
].join("\n");

/** Difference-in-differences with an event study: units that start the treatment at the same or different times. */
const DID_STARTER = [
  "apiVersion: mapi/v1",
  "kind: ModelSpec",
  "metadata:",
  "  name: new-did",
  "  project: default",
  "data:",
  "  # Each row is one unit (store or market) and one period. The treatment column is 0 before",
  "  # the unit starts the treatment and 1 from then on. Some units must never start it.",
  "  dataset: dataset://default/my-dataset@latest",
  "  bind:",
  "    dims: {unit: store, time: month}",
  "    vars: {y: sales, treatment: treatment}",
  "model:",
  "  use: causal.did@^1",
  "  settings:",
  "    design: event_study",
  "    leads: 4",
  "    lags: 8",
  CAUSAL_FIT,
  "outputs:",
  "  plots: [causal.event_study, causal.impact, causal.pre_fit, core.convergence]",
  "execution: {resource_class: S, timeout: 30m}",
  "",
].join("\n");

const STARTERS: Record<string, string> = {
  "ts.bsts": BSTS_STARTER,
  "causal.impact": IMPACT_STARTER,
  "causal.synthetic_control": SYNTHETIC_STARTER,
  "causal.did": DID_STARTER,
};

export function specStarterFor(modelId: string, opts: { version?: string | null; about?: About | null } = {}): string {
  if (modelId === "mmm.bayesian") return STARTER_SPEC;
  if (modelId === "mmm.nested") return NESTED_STARTER;
  if (modelId === "mmm.multivariate") return MULTIVARIATE_STARTER;
  const fixed = STARTERS[modelId];
  if (fixed !== undefined) return fixed;
  const short = modelId.replace(/^[a-z]+\./, "").replace(/_/g, "-").slice(0, 40);
  const about = opts.about;
  const notes = about
    ? [
        `# ${commentLine(about.title)}: ${commentLine(about.summary_plain)}`,
        ...about.data_needs.slice(0, 4).map((n) => `#   needs: ${commentLine(n)}`),
      ]
    : [];
  return [
    "apiVersion: mapi/v1",
    "kind: ModelSpec",
    "metadata:",
    `  name: new-${short}`,
    "  project: default",
    "data:",
    "  # Set your dataset here. Map its columns to the roles of the model.",
    "  dataset: dataset://default/my-dataset@latest",
    "  bind:",
    "    dims: {time: date}",
    "    vars: {y: my_target_column}",
    "  split: {kind: time_holdout, dim: time, last: 8}",
    "model:",
    ...notes.map((n) => `  ${n}`),
    `  use: ${modelId}@${majorRange(opts.version)}`,
    "  settings: {}",
    "",
  ].join("\n");
}
