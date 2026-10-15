/**
 * Deterministic fixtures matching the M1 DTOs (mapi_spec.api). Used by MSW in `--mode mock`,
 * by the Playwright smoke test and by component tests.
 */
import { tableToIPC, tableFromArrays } from "apache-arrow";

import type {
  CatalogResponse,
  Dataset,
  DatasetVersion,
  LogLine,
  Model,
  ModelVersion,
  PlotListItem,
  PlotSpec,
  Run,
  RunEventView,
  SpecDoc,
  SpecRevision,
  ValidateResponse,
} from "@/api/types";
import { crc32cBase64 } from "@/lib/crc32c";
import { STARTER_SPEC } from "@/features/editor/starter";

export const T0 = "2026-10-15T09:30:00Z";
export const SHA = `sha256:${"3f".repeat(32)}`;
export const DS_ID = "ds_01jaf3a1b2c3d4e5f6g7h8j9k0";
export const SPEC_ID = "spc_01jaf3b1c2d3e4f5g6h7j8k9m0";
export const MODEL_ID = "mdl_01jaf3c1d2e3f4g5h6j7k8m9n0";
export const MV_ID = "mv_01jaf3e1f2g3h4j5k6m7n8p9q0";
export const RUN_RUNNING = "run_01jaf3q2x8m9v7k5h4g3f2e1d0";
export const RUN_DONE = "run_01jaf3q2x8m9v7k5h4g3f2e1a1";
export const RUN_FAILED = "run_01jaf3q2x8m9v7k5h4g3f2e1b2";

const base = { tenant_id: "default", project_id: "default" } as const;

export const dataset: Dataset = {
  ...base,
  id: DS_ID,
  name: "eu_media_daily",
  ref: "dataset://default/eu_media_daily",
  description: "Daily spend and revenue per market",
  latest_version: 1,
  created_at: T0,
  updated_at: T0,
};

export const datasetVersion: DatasetVersion = {
  ...base,
  dataset_id: DS_ID,
  version: 1,
  ref: "dataset://default/eu_media_daily@v1",
  status: "ready",
  sha256: SHA,
  size_bytes: 1_048_576,
  data_schema: {
    layout: "dense",
    dims: {
      market: { kind: "categorical", policy: "new_levels", new_level_strategy: "hyperprior" },
      channel: { kind: "categorical", policy: "fixed" },
      date: { kind: "time", policy: "extend", freq: "D" },
    },
    vars: {
      revenue_local: { dims: ["market", "date"], dtype: "float64", role: "target", units: "local" },
      spend_local: { dims: ["market", "channel", "date"], dtype: "float64", role: "spend", units: "local" },
    },
  },
  summary: {
    vars: ["revenue_local", "spend_local"],
    coords: {
      market: { kind: "categorical", size: 3, values: ["de", "fr", "it"] },
      channel: { kind: "categorical", size: 2, values: ["search", "tv"] },
      date: { kind: "time", size: 730, start: "2024-10-01", end: "2026-09-30", freq: "D", contiguous: true },
    },
  },
  profile: { name: "profile.json", size: 2048, content_type: "application/json" },
  preview: { name: "preview.json", size: 2048, content_type: "application/json" },
  ingest_run_id: RUN_DONE,
  issues: [],
  created_at: T0,
  ready_at: T0,
};

export const profileView = {
  format: "mapi.profile/v1",
  vars: {
    revenue_local: { dtype: "float64", count: 2184, missing: 6, min: 120.5, max: 9821.2, mean: 2310.4, std: 811.9 },
    spend_local: { dtype: "float64", count: 4380, missing: 0, min: 0, max: 1201.7, mean: 233.1, std: 98.4 },
  },
  missingness: {
    along: "month",
    labels: ["revenue_local · de", "revenue_local · fr", "spend_local · de"],
    coords: ["2026-07", "2026-08", "2026-09"],
    fraction: [
      [0, 0.1, 0],
      [0, 0, 0.03],
      [0, 0, 0],
    ],
  },
};

export const previewView = {
  format: "mapi.preview/v1",
  columns: [
    { name: "market", dtype: "str", role: "dim" },
    { name: "date", dtype: "datetime64[ns]", role: "dim" },
    { name: "revenue_local", dtype: "float64", role: "var" },
  ],
  rows: [
    ["de", "2026-09-28", 2410.2],
    ["de", "2026-09-29", null],
    ["fr", "2026-09-28", 1830.0],
  ],
  total_rows: 2190,
};

const VALID_YAML = STARTER_SPEC;

export const specRevision: SpecRevision = {
  ...base,
  spec_id: SPEC_ID,
  revision: 2,
  ref: "spec://default/eu-geo-mmm@r2",
  kind: "ModelSpec",
  hash: SHA,
  yaml: VALID_YAML,
  document: { apiVersion: "mapi/v1", kind: "ModelSpec" },
  valid: true,
  issues: [],
  parent_revision: 1,
  message: "tune priors",
  created_at: T0,
};

export const specRevision1: SpecRevision = {
  ...specRevision,
  revision: 1,
  ref: "spec://default/eu-geo-mmm@r1",
  yaml: VALID_YAML.replace("draws: 1000", "draws: 500"),
  parent_revision: null,
  message: "first draft",
};

export const specDoc: SpecDoc = {
  ...base,
  id: SPEC_ID,
  name: "eu-geo-mmm",
  kind: "ModelSpec",
  ref: "spec://default/eu-geo-mmm",
  latest_revision: 2,
  latest: specRevision,
  created_at: T0,
  updated_at: T0,
};

const schemaRaw = datasetVersion.data_schema!;

export function validateResponseFor(yaml: string): ValidateResponse {
  // A split placed after a stateful step is the mock's example of a static error.
  const leak = /after_step:\s*scale_y/.test(yaml);
  const issues: ValidateResponse["issues"] = leak
    ? [
        {
          code: "LEAKAGE_RISK",
          severity: "error",
          pointer: "/pipeline/2",
          message: "stateful step 'scale_y' runs before the split",
          suggested_action: "move the split to after 'weekly'",
        },
      ]
    : /use: mmm\.hierarchical_geo/.test(yaml)
      ? []
      : [{ code: "REF_UNRESOLVED", severity: "error", pointer: "/model/use", message: "model plugin not found in the catalog" }];
  const weekly = { ...schemaRaw, dims: { ...schemaRaw.dims, date: { kind: "time" as const, policy: "extend" as const, freq: "W-SUN" } } };
  return {
    valid: issues.length === 0,
    kind: "ModelSpec",
    hash: SHA,
    issues,
    stages: [
      { name: "raw", data_schema: schemaRaw, split: false },
      { name: "weekly", data_schema: weekly, split: true },
      { name: "scale_spend", data_schema: weekly, split: false },
      { name: "scale_y", data_schema: weekly, split: false },
      {
        name: "lags",
        data_schema: {
          ...weekly,
          dims: { ...weekly.dims, lag: { kind: "integer", policy: "fixed" } },
          vars: { ...weekly.vars, spend_lags: { dims: ["market", "channel", "date", "lag"], dtype: "float64", role: "media" } },
        },
        split: false,
      },
      { name: "season", data_schema: null, split: false },
    ],
    resolved: {
      spec_hash: SHA,
      binding: { dims: { geo: "market", channel: "channel", time: "date" }, vars: { y: "revenue_local", spend: "spend_local" } },
      split_stage: "weekly",
      stages: {},
      steps: [
        { id: "weekly", use: "core.resample@^1", resolved: "core.resample@1.0.0", kind: "reshape", stateful: false, invertible: false, depends_on_target: false, context: null, input_stage: "raw" },
        { id: "scale_spend", use: "core.mean_scale@^1", resolved: "core.mean_scale@1.0.0", kind: "value", stateful: true, invertible: true, depends_on_target: false, context: null, input_stage: "weekly" },
        { id: "scale_y", use: "core.mean_scale@^1", resolved: "core.mean_scale@1.0.0", kind: "value", stateful: true, invertible: true, depends_on_target: false, context: null, input_stage: "scale_spend" },
        { id: "lags", use: "core.lag_tensor@^1", resolved: "core.lag_tensor@1.0.0", kind: "feature", stateful: false, invertible: false, depends_on_target: false, context: { along: "time", n: 6, within: ["geo"] }, input_stage: "scale_y" },
        { id: "season", use: "core.fourier@^1", resolved: "core.fourier@1.0.0", kind: "feature", stateful: true, invertible: false, depends_on_target: false, context: null, input_stage: "lags" },
      ],
      model: {
        use: "mmm.hierarchical_geo@^1",
        resolved: "mmm.hierarchical_geo@1.3.2",
        capabilities: {
          inference: "mcmc",
          representation: "draws",
          score_modes: ["predict", "forecast", "counterfactual", "evaluate"],
        },
        effective_policies: {},
      },
    },
    catalog_digest: `sha256:${"cd".repeat(32)}`,
  };
}

function run(id: string, patch: Partial<Run>): Run {
  return {
    ...base,
    id,
    kind: "fit",
    state: "queued",
    attempt: 1,
    spec_id: SPEC_ID,
    spec_revision: 2,
    spec_hash: SHA,
    resource_class: "M",
    timeout: "45m",
    executor: "local-process",
    cost: { estimated: true },
    created_at: T0,
    updated_at: T0,
    ...patch,
  };
}

export const PRICE_VERSION = "cloudrun-list-2026-10";

export const runRunning = run(RUN_RUNNING, {
  state: "running",
  cost: { estimated: true, estimate_micros: 31_200, currency: "USD", price_version: PRICE_VERSION },
  queued_at: T0,
  started_at: "2026-10-15T09:30:05Z",
  progress: {
    seq: 40,
    phase: "sample",
    fraction: 0.4,
    message: "drawing samples",
    eta_seconds: 95,
    chains: [
      { chain: 0, draws_done: 400, draws_total: 1000, tuning: false, divergences: 0 },
      { chain: 1, draws_done: 380, draws_total: 1000, tuning: false, divergences: 2 },
    ],
    updated_at: T0,
  },
});

export const runDone = run(RUN_DONE, {
  state: "succeeded",
  started_at: "2026-10-15T09:00:05Z",
  finished_at: "2026-10-15T09:12:40Z",
  output_model_version_id: MV_ID,
  cost: { estimated: false, amount_micros: 78_624, estimate_micros: 31_200, currency: "USD", price_version: PRICE_VERSION, vcpu_seconds: 3020, gib_seconds: 12_080, usd: 0.078624, attempts_billed: 1 },
});

export const runFailed = run(RUN_FAILED, {
  state: "failed",
  failure_class: "resource_exhausted",
  cost: { estimated: false, amount_micros: 124_800, currency: "USD", price_version: PRICE_VERSION, vcpu_seconds: 4780, gib_seconds: 19_120, usd: 0.1248, attempts_billed: 1 },
  started_at: "2026-10-15T08:00:05Z",
  finished_at: "2026-10-15T08:20:00Z",
  problem: {
    type: "urn:mapi:error:RESOURCE_EXHAUSTED",
    title: "Run exhausted its resources",
    status: 507,
    code: "RESOURCE_EXHAUSTED",
    detail: "the worker was killed (exit 137); retry at class L",
    untrusted_text: "\u001b[31mMemoryError\u001b[0m: Unable to allocate 3.2 GiB <script>alert(1)</script>",
  },
});

export const runs: Run[] = [runRunning, runDone, runFailed];

export function events(runId: string): RunEventView[] {
  return [
    { id: 1, run_id: runId, attempt: 1, seq: 0, ts: "2026-10-15T09:30:06Z", type: "state", payload: { phase: "load_inputs" } },
    { id: 2, run_id: runId, attempt: 1, seq: 1, ts: "2026-10-15T09:30:20Z", type: "state", payload: { phase: "sample", message: "4 chains" } },
    {
      id: 3,
      run_id: runId,
      attempt: 1,
      seq: 2,
      ts: "2026-10-15T09:31:00Z",
      type: "warning",
      payload: { code: "DIVERGENCES", message: "2 divergent transitions in chain 1", untrusted_text: "\u001b[33mwarn\u001b[0m <b>not bold</b>" },
    },
  ];
}

export function logLines(runId: string): LogLine[] {
  return [
    { run_id: runId, attempt: 1, seq: 1, ts: "2026-10-15T09:30:06Z", stream: "system", line: "bootstrap ok", truncated: false },
    { run_id: runId, attempt: 1, seq: 2, ts: "2026-10-15T09:30:20Z", stream: "stderr", line: "\u001b[32mSampling\u001b[0m 4 chains for 400 tune and 1000 draw iterations", truncated: false },
    { run_id: runId, attempt: 1, seq: 3, ts: "2026-10-15T09:30:21Z", stream: "stdout", line: "<img src=x onerror=alert(1)> plain text only", truncated: false },
  ];
}

export const model: Model = {
  ...base,
  id: MODEL_ID,
  name: "eu-geo-mmm",
  ref: "model://default/eu-geo-mmm",
  latest_version: 1,
  created_at: T0,
  updated_at: T0,
};

export const modelVersion: ModelVersion = {
  ...base,
  id: MV_ID,
  model_id: MODEL_ID,
  version: 1,
  ref: "model://default/eu-geo-mmm@v1",
  status: "ready",
  run_id: RUN_DONE,
  spec_hash: SHA,
  manifest: {
    schema_version: "mapi.bundle-manifest/v1",
    ...base,
    model_id: MODEL_ID,
    model_version: 1,
    run_id: RUN_DONE,
    attempt: 1,
    kind: "fit",
    status: "complete",
    spec_hash: SHA,
    data_hash: SHA,
    catalog_digest: `sha256:${"cd".repeat(32)}`,
    base_image: "base-bayes@2026.10",
    objects: {
      "bundle.zarr.zip": {
        bucket: "mapi-local-bundles",
        key: `t/default/models/${MODEL_ID}/1/bundle.zarr.zip`,
        generation: 7,
        size: 2_097_152,
        crc32c: "4waSgw==",
        md5: "1B2M2Y8AsgTpgAmY7PhCfg==",
        content_type: "application/zip",
      },
    },
    source_manifest_crc32c: "AAAAAA==",
    signed_at: T0,
    signature: { alg: "HS256", key_id: "local-1", value: "c2lnbmF0dXJl" },
  },
  capabilities: {
    inference: "mcmc",
    representation: "draws",
    score_modes: ["predict", "forecast", "counterfactual", "evaluate"],
    loo_valid: true,
  },
  diagnostics: [
    { code: "RHAT_OK", severity: "info", metric: "r_hat_max", value: 1.004, threshold: 1.01, message_expert: "max r-hat 1.004", message_plain: "All chains agree.", var: "beta_channel" },
    { code: "ESS_LOW", severity: "warning", metric: "ess_bulk_min", value: 310, threshold: 400, message_expert: "bulk ESS 310 for lam[tv]", message_plain: "Some estimates are noisy.", var: "lam", coords: { channel: "tv" } },
  ],
  scorecard: {
    overall: "warn",
    items: [
      { code: "RHAT_OK", status: "pass", title: "Chains agree", message_plain: "All chains converged to the same answer.", metric: "r_hat_max", value: 1.004, threshold: 1.01 },
      { code: "HOLDOUT_MAPE", status: "warn", title: "Holdout error", message_plain: "Forecasts were off by 14% on the last 8 weeks.", metric: "mape", value: 0.14, threshold: 0.1, suggested_action: "Add holidays or check the last weeks of data." },
    ],
  },
  lineage: {
    run_id: RUN_DONE,
    attempt: 1,
    spec_hash: SHA,
    dataset_sha: SHA,
    plugin: "mmm.hierarchical_geo@1.3.2",
    base_image: "base-bayes@2026.10",
    seed: 20261014,
    engine: "nutpie",
    executor: "local-process",
    resource_class: "M",
    lib_versions: { pymc: "5.25.1" },
  },
  metrics: { mape: 0.14, coverage_90: 0.88 },
  artifacts: [
    { name: "bundle.zarr.zip", size: 2_097_152, content_type: "application/zip" },
    { name: "ui/tree_index.json", size: 1024, content_type: "application/json" },
    { name: "ui/preprocess.json", size: 1024, content_type: "application/json" },
  ],
  created_at: T0,
};

export const treeIndexView = {
  format: "mapi.tree-index/v1",
  groups: {
    "/posterior": {
      dims: { chain: 4, draw: 1000, channel: 2, market: 3 },
      vars: { beta_channel: { dims: ["chain", "draw", "channel"], shape: [4, 1000, 2], dtype: "float64" } },
      coords: { channel: { dims: ["channel"], dtype: "str", size: 2, values: ["search", "tv"] } },
    },
    "/observed_data": { dims: { market: 3, date: 104 }, vars: { y: { dims: ["market", "date"], shape: [3, 104], dtype: "float64", units: "EUR" } } },
  },
};

export const preprocessView = {
  format: "mapi.preprocess/v1",
  steps: [
    {
      id: "scale_spend",
      use: "core.mean_scale@1.0.0",
      kind: "value",
      stateful: true,
      learned: { over: ["time"] },
      learned_truncated: false,
      omitted: [],
      arrays: [
        {
          name: "scale",
          dims: ["market", "channel"],
          coords: { market: ["de", "fr", "it"], channel: ["search", "tv"] },
          values: [
            [120.1, 340.2],
            [98.4, 210.7],
            [77.0, 150.3],
          ],
        },
      ],
    },
  ],
};

export const plotItems: PlotListItem[] = [
  {
    id: "core.ppc_fan",
    title: "Posterior predictive fan",
    kind: "vega-lite",
    persona: "both",
    recipe: "core.ppc_fan@1.0.0",
    caption_plain: "The band is where the model expects 90% of weeks.",
    href: `/v1/models/${MODEL_ID}/versions/1/plots/core.ppc_fan`,
  },
];

export const plotSpec: PlotSpec = {
  id: "core.ppc_fan",
  title: "Posterior predictive fan",
  kind: "vega-lite",
  spec: {
    layer: [
      { mark: { type: "area", opacity: 0.25 }, encoding: { x: { field: "week", type: "quantitative" }, y: { field: "lo", type: "quantitative" }, y2: { field: "hi" } } },
      { mark: "line", encoding: { x: { field: "week", type: "quantitative" }, y: { field: "mean", type: "quantitative" } } },
    ],
    data: { name: "fan" },
  },
  datasets: { fan: { kind: "data_ref", path: "ui/plotdata/ppc_fan.arrow", format: "arrow" } },
  schema_version: "v5",
  persona: "both",
  caption_plain: "The band is where the model expects 90% of weeks.",
  provenance: { recipe: "core.ppc_fan@1.0.0", generated_at: T0 },
};

export function fanArrow(): Uint8Array {
  const n = 26;
  const week = Float64Array.from({ length: n }, (_, i) => i + 1);
  const mean = Float64Array.from(week, (w) => 100 + 10 * Math.sin(w / 4));
  return tableToIPC(
    tableFromArrays({ week, mean, lo: Float64Array.from(mean, (m) => m - 8), hi: Float64Array.from(mean, (m) => m + 8) }),
    "stream",
  );
}

export function artifactLink(name: string, bytes: Uint8Array, contentType: string) {
  return {
    name,
    url: `http://127.0.0.1:4443/mock-artifacts/${encodeURIComponent(name)}?X-Goog-Signature=mock`,
    method: "GET" as const,
    expires_at: "2099-01-01T00:00:00Z",
    size: bytes.length,
    content_type: contentType,
    crc32c: crc32cBase64(bytes),
    generation: 7,
  };
}

export const catalog: CatalogResponse = {
  digest: `sha256:${"cd".repeat(32)}`,
  generated_at: T0,
  steps: [
    { id: "core.resample", version: "1.0.0", kind: "reshape", stateful: false, invertible: false, depends_on_target: false, summary: "Resample a time dim to a coarser frequency" },
    { id: "core.mean_scale", version: "1.0.0", kind: "value", stateful: true, invertible: true, depends_on_target: false, summary: "Divide by the mean over the given dims" },
  ] as CatalogResponse["steps"],
  plots: [{ id: "core.ppc_fan", version: "1.0.0", title: "PPC fan", persona: "both" }] as CatalogResponse["plots"],
  models: [],
};
