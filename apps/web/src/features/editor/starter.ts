/** Starter document for a new spec (an E33-style hierarchical geo MMM, plan §6.1). */
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
  - {id: scale_spend, use: core.mean_scale@^1, with: {vars: [spend], over: [time], unseen_levels: pooled}}
  - {id: scale_y, use: core.mean_scale@^1, with: {vars: [y], over: [time], unseen_levels: pooled}}
  - {id: lags, use: core.lag_tensor@^1, with: {vars: [spend], along: time, within: [geo], l_max: 6, pad: first}}
  - {id: season, use: core.fourier@^1, with: {along: time, period: 52.18, order: 3, out: fourier}}
model:
  use: mmm.hierarchical_geo@^1
  settings:
    pooling: partial
    adstock: {kind: geometric, l_max: 6}
    saturation: {kind: logistic}
  fit:
    mode: evaluate_then_refit
    tune: 400
    draws: 1000
    chains: 4
    seed: 20261014
outputs:
  plots: [core.ppc_fan, core.convergence, mmm.decomposition]
execution: {resource_class: M, timeout: 45m}
`;

/** `metadata.name` of a YAML document, best effort (for display before validation). */
export function specNameOf(yamlText: string): string | null {
  const m = /^metadata:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*([A-Za-z0-9_-]+)/m.exec(yamlText);
  return m?.[1] ?? null;
}
