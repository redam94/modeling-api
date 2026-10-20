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

export function specStarterFor(modelId: string, opts: { version?: string | null; about?: About | null } = {}): string {
  if (modelId === "mmm.bayesian") return STARTER_SPEC;
  if (modelId === "mmm.nested") return NESTED_STARTER;
  if (modelId === "mmm.multivariate") return MULTIVARIATE_STARTER;
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
