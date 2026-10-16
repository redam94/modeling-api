/** Starter document for a new spec: a geo marketing-mix model on mmm-framework (plan §6.1).
 * The model owns scaling and carry-over, so the pipeline only aggregates to weeks, fills
 * missing spend and adds calendar controls. */
export const STARTER_SPEC = `apiVersion: mapi/v1
kind: ModelSpec
metadata:
  name: eu-geo-mmm
  project: default
data:
  dataset: dataset://default/eu_media_daily@latest
  bind:
    dims: {geo: market, channel: channel, time: date}
    vars: {y: revenue_local, spend: spend_local}
  split: {kind: time_holdout, dim: time, last: 8, after_step: weekly}
pipeline:
  - {id: weekly, use: core.resample@^1, with: {dim: time, freq: W-SUN, how: sum, full_periods_only: true}}
  - {id: fill, use: core.impute@^1, with: {vars: [spend], method: median, over: [time]}}
  - {id: cal, use: core.calendar@^1, with: {along: time, features: [month], out: calendar}}
model:
  use: mmm.bayesian@^1
  settings:
    hierarchy: {pool_over: [geo]}
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
    trend: {kind: linear}
    seasonality: {yearly_order: 3}
  fit:
    mode: evaluate_then_refit
    tune: 400
    draws: 1000
    chains: 4
    seed: 20261014
outputs:
  derived: [contributions, response_curves, marginal_cost]
  plots: [core.ppc_fan, core.convergence, mmm.decomposition, mmm.response_curves]
execution: {resource_class: M, timeout: 45m}
`;

/** `metadata.name` of a YAML document, best effort (for display before validation). */
export function specNameOf(yamlText: string): string | null {
  const m = /^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*([A-Za-z0-9_-]+)/m.exec(yamlText);
  return m?.[1] ?? null;
}
