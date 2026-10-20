/**
 * Deterministic fixtures matching the M1 DTOs (mapi_spec.api). Used by MSW in `--mode mock`,
 * by the Playwright smoke test and by component tests.
 */
import { tableToIPC, tableFromArrays } from "apache-arrow";

import type { RunParam } from "@/api/tracking";

import type {
  ChainProgress,
  Dataset,
  DatasetVersion,
  Experiment,
  LogLine,
  Model,
  ModelVersion,
  PlotListItem,
  PlotSpec,
  Run,
  RunEventView,
  RunView,
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
/** v0.11: a finished `search.grid` node run with trial params and rmse per grid point. */
export const RUN_GRID = "run_01jaf3q2x8m9v7k5h4g3f2e1g5";
// v0.14: two fits of one experiment, to compare (metrics with a split label, params, media).
export const RUN_FIT_A = "run_01jaf3q2x8m9v7k5h4g3f2e1h6";
export const RUN_FIT_B = "run_01jaf3q2x8m9v7k5h4g3f2e1j7";
export const EXP_GEO = "exp_01jaf3x1geo2adstock3m4n5p6";
export const EXP_PRIORS = "exp_01jaf3x1priors2q3r4s5t6v7w";
export const EXP_OLD = "exp_01jaf3x1old2baseline3x4y5z";

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
  const leak = /after_step:\s*fill/.test(yaml);
  const issues: ValidateResponse["issues"] = leak
    ? [
        {
          code: "LEAKAGE_RISK",
          severity: "error",
          pointer: "/pipeline/1",
          message: "stateful step 'fill' runs before the split",
          suggested_action: "move the split to after 'weekly'",
        },
      ]
    : /use: mmm\.bayesian/.test(yaml)
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
      { name: "fill", data_schema: weekly, split: false },
      {
        name: "cal",
        data_schema: {
          ...weekly,
          dims: { ...weekly.dims, calendar_feature: { kind: "categorical", policy: "fixed" } },
          vars: { ...weekly.vars, calendar: { dims: ["date", "calendar_feature"], dtype: "float64", role: "control" } },
        },
        split: false,
      },
    ],
    resolved: {
      spec_hash: SHA,
      binding: { dims: { geo: "market", channel: "channel", time: "date" }, vars: { y: "revenue_local", spend: "spend_local" } },
      split_stage: "weekly",
      stages: {},
      steps: [
        { id: "weekly", use: "core.resample@^1", resolved: "core.resample@1.0.0", kind: "reshape", stateful: false, invertible: false, depends_on_target: false, context: null, input_stage: "raw" },
        { id: "fill", use: "core.impute@^1", resolved: "core.impute@1.0.0", kind: "value", stateful: true, invertible: false, depends_on_target: false, context: null, input_stage: "weekly" },
        { id: "cal", use: "core.calendar@^1", resolved: "core.calendar@1.0.0", kind: "feature", stateful: false, invertible: false, depends_on_target: false, context: null, input_stage: "fill" },
      ],
      model: {
        use: "mmm.bayesian@^1",
        resolved: "mmm.bayesian@1.0.0",
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

/** The four chains of the running PyMC (nutpie) fit, with the sampler's step size (v0.11). */
export const RUNNING_CHAINS: ChainProgress[] = [
  { chain: 0, draws_done: 400, draws_total: 1000, tuning: false, divergences: 0, step_size: 0.12 },
  { chain: 1, draws_done: 380, draws_total: 1000, tuning: false, divergences: 2, step_size: 0.135 },
  { chain: 2, draws_done: 395, draws_total: 1000, tuning: false, divergences: 0, step_size: 0.15 },
  { chain: 3, draws_done: 390, draws_total: 1000, tuning: false, divergences: 1, step_size: 0.165 },
];

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
    chains: RUNNING_CHAINS,
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

export const runGrid: Run = {
  ...run(RUN_GRID, {
    kind: "workflow_node",
    state: "succeeded",
    spec_id: null,
    spec_revision: null,
    spec_hash: null,
    resource_class: "S",
    started_at: "2026-10-15T10:00:05Z",
    finished_at: "2026-10-15T10:06:30Z",
    cost: { estimated: false, amount_micros: 12_480, currency: "USD", price_version: PRICE_VERSION, vcpu_seconds: 385, gib_seconds: 770, usd: 0.01248, attempts_billed: 1 },
  }),
  name: "Grid search: adstock and saturation",
  note: "12 grid points. Point 4 has the smallest RMSE.",
  tags: { "grid.best_point": "4", team: "growth", "mapi.workflow": "wf_mock" },
  experiment_id: EXP_GEO,
};

function fitRun(id: string, name: string, started: string, finished: string, tags: Record<string, string>): Run {
  return {
    ...run(id, {
      state: "succeeded",
      created_at: started,
      updated_at: finished,
      started_at: started,
      finished_at: finished,
      cost: { estimated: false, amount_micros: 54_600, currency: "USD", price_version: PRICE_VERSION, vcpu_seconds: 2100, gib_seconds: 8400, usd: 0.0546, attempts_billed: 1 },
    }),
    name,
    tags,
    experiment_id: EXP_GEO,
  };
}

export const runFitA = fitRun(RUN_FIT_A, "Adstock 8 weeks", "2026-10-14T14:00:00Z", "2026-10-14T14:09:10Z", { team: "growth", adstock: "8w" });
export const runFitB = fitRun(RUN_FIT_B, "Adstock 4 weeks", "2026-10-14T15:00:00Z", "2026-10-14T15:08:20Z", { team: "growth", adstock: "4w" });

export const runs: Run[] = [runRunning, { ...runDone, experiment_id: EXP_GEO }, runFailed, runGrid, runFitA, runFitB];

// ------------------------------------------------------------------------- v0.14 experiments and views

export const experiments: Experiment[] = [
  {
    id: EXP_GEO,
    name: "Geo adstock study",
    description: "Which adstock length fits the EU geo data best? Compare the holdout loss and the RMSE.",
    archived: false,
    created_by: "usr_ana",
    created_at: "2026-10-14T13:00:00Z",
    updated_at: "2026-10-14T13:00:00Z",
  },
  {
    id: EXP_PRIORS,
    name: "Prior sensitivity",
    description: null,
    archived: false,
    created_by: "usr_ana",
    created_at: "2026-10-13T10:00:00Z",
    updated_at: "2026-10-13T10:00:00Z",
  },
  {
    id: EXP_OLD,
    name: "Baseline 2025 <b>old</b>",
    description: "Archived: the 2025 baseline models.",
    archived: true,
    created_by: "usr_ana",
    created_at: "2026-09-01T10:00:00Z",
    updated_at: "2026-09-20T10:00:00Z",
  },
];

export const runViews: RunView[] = [
  {
    id: "rv_01jaf3y1best2rmse3a4b5c6d7e",
    name: "Best RMSE first",
    experiment_id: null,
    query: { filters: ["metric.rmse < 1000"], order_by: "metric.rmse", columns: ["metric.rmse", "param.settings.adstock.l_max"], tags: [] },
    created_by: "usr_ana",
    created_at: "2026-10-14T16:00:00Z",
    updated_at: "2026-10-14T16:00:00Z",
  },
  {
    id: "rv_01jaf3y1geo2loss3f4g5h6j7k",
    name: "Holdout loss",
    experiment_id: EXP_GEO,
    query: { experiment_id: EXP_GEO, columns: ["metric.loss{split=val}.min", "metric.mape"], order_by: "metric.loss{split=val}.min", filters: [], tags: [] },
    created_by: "usr_ana",
    created_at: "2026-10-14T16:10:00Z",
    updated_at: "2026-10-14T16:10:00Z",
  },
];

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
    // v0.11 structured lines (`log_line`): a level, and fields shown as chips (untrusted text).
    { run_id: runId, attempt: 1, seq: 4, ts: "2026-10-15T09:30:22Z", stream: "user", line: "compiled the model", level: "debug", fields: { seconds: 3.2 } },
    { run_id: runId, attempt: 1, seq: 5, ts: "2026-10-15T09:30:23Z", stream: "user", line: "sampler started", level: "info", fields: { chains: 4, draws: 1000 } },
    { run_id: runId, attempt: 1, seq: 6, ts: "2026-10-15T09:31:00Z", stream: "user", line: "step size of chain 1 is small", level: "warning", fields: { chain: 1, step_size: 0.012, note: "<b>not bold</b>" } },
    { run_id: runId, attempt: 1, seq: 7, ts: "2026-10-15T09:31:30Z", stream: "user", line: "grid point 7 failed: singular matrix", level: "error", fields: { point: 7 } },
  ];
}

// ------------------------------------------------------------------------- v0.11 run tracking

/** A stored metric series of the mock: points in step order (step `null` for a scalar). */
export interface MockSeries {
  key: string;
  labels: Record<string, string>;
  attempt: number;
  points: { step: number | null; value: number; ts: string }[];
}

export interface MockTracking {
  series: MockSeries[];
  params: RunParam[];
}

const at = (base: string, seconds: number) => new Date(Date.parse(base) + seconds * 1000).toISOString();

/** Step size of a nutpie chain at draw `step` (1-based): adapts during 300 tuning draws, then stays. */
export function mockStepSize(chain: number, step: number): number {
  const final = 0.12 + 0.015 * chain;
  if (step > 300) return final;
  return Number((final + 0.6 * Math.exp(-step / 45) + 0.02 * Math.sin(step / 7 + chain)).toFixed(4));
}

/** Cumulative divergences of a chain at draw `step` (chain 1: two, chain 3: one, after tuning). */
export function mockDivergences(chain: number, step: number): number {
  const hits = chain === 1 ? [330, 365] : chain === 3 ? [350] : [];
  return hits.filter((s) => s <= step).length;
}

function fitTracking(): MockTracking {
  const series: MockSeries[] = [];
  for (let c = 0; c < 4; c++) {
    const draws = RUNNING_CHAINS[c]?.draws_done ?? 400;
    const steps = Array.from({ length: Math.floor(draws / 5) }, (_, i) => (i + 1) * 5);
    const labels = { chain: String(c) };
    series.push({ key: "step_size", labels, attempt: 1, points: steps.map((st) => ({ step: st, value: mockStepSize(c, st), ts: at("2026-10-15T09:30:20Z", st * 0.2) })) });
    series.push({ key: "divergences", labels, attempt: 1, points: steps.map((st) => ({ step: st, value: mockDivergences(c, st), ts: at("2026-10-15T09:30:20Z", st * 0.2) })) });
  }
  const param = (key: string, value: string, value_num: number | null = null): RunParam => ({ key, value, value_num, source: "runner", logged_at: "2026-10-15T09:30:08Z" });
  return {
    series,
    params: [
      param("model", "mmm.bayesian@1.0.0"),
      param("fit.engine", "nutpie"),
      param("fit.draws", "1000", 1000),
      param("fit.tune", "300", 300),
      param("fit.chains", "4", 4),
      param("fit.target_accept", "0.9", 0.9),
      param("fit.seed", "20261014", 20261014),
      param("data.split", "holdout"),
      param("settings.adstock.l_max", "8", 8),
      param("settings.saturation.kind", "hill"),
      param("settings.note", "<b>not bold</b> <img src=x onerror=alert(1)>"),
    ],
  };
}

const GRID_POINTS = Array.from({ length: 12 }, (_, i) => ({ l_max: 2 + (i % 6) * 2, kind: i < 6 ? "hill" : "logistic" }));

function gridTracking(): MockTracking {
  const t0 = "2026-10-15T10:00:10Z";
  const rmse = GRID_POINTS.map((_, i) => Number((380 + 45 * Math.abs(i - 4) + (i >= 6 ? 30 : 0)).toFixed(1)));
  const mae = rmse.map((v) => Number((v * 0.78).toFixed(1)));
  const stepped = (key: string, values: number[]): MockSeries => ({
    key,
    labels: {},
    attempt: 1,
    points: values.map((value, i) => ({ step: i, value, ts: at(t0, 30 * i) })),
  });
  const param = (key: string, value: string, value_num: number | null = null): RunParam => ({ key, value, value_num, source: "runner", logged_at: t0 });
  return {
    series: [
      stepped("rmse", rmse),
      stepped("mae", mae),
      { key: "best_rmse", labels: {}, attempt: 1, points: [{ step: null, value: Math.min(...rmse), ts: at(t0, 370) }] },
    ],
    params: [
      param("grid.metric", "rmse"),
      param("grid.direction", "minimize"),
      param("grid.points", "12", 12),
      param("grid.nested", "false"),
      ...GRID_POINTS.flatMap((p, i) => [param(`trial.${i}.adstock/l_max`, String(p.l_max), p.l_max), param(`trial.${i}.saturation/kind`, p.kind)]),
    ],
  };
}

/** A fit of the geo study: train and val loss over 50 steps, scalars, params. */
function studyTracking(lMax: number, seed: number, t0: string): MockTracking {
  const series: MockSeries[] = [];
  const steps = Array.from({ length: 50 }, (_, i) => i);
  const floor = lMax === 8 ? 0.21 : 0.26;
  for (const split of ["train", "val"]) {
    const gap = split === "val" ? 0.05 + (lMax === 8 ? 0 : 0.02) : 0;
    series.push({
      key: "loss",
      labels: { split },
      attempt: 1,
      points: steps.map((st) => ({ step: st, value: Number((floor + gap + 1.2 * Math.exp(-st / 9) + 0.01 * Math.sin(st + seed)).toFixed(4)), ts: at(t0, 10 * st) })),
    });
  }
  const scalar = (key: string, value: number): MockSeries => ({ key, labels: {}, attempt: 1, points: [{ step: null, value, ts: at(t0, 540) }] });
  series.push(scalar("rmse", lMax === 8 ? 398.2 : 431.7), scalar("mape", lMax === 8 ? 12.4 : 13.9), scalar("elpd_loo", lMax === 8 ? -1228.1 : -1241.6));
  const param = (key: string, value: string, value_num: number | null = null): RunParam => ({ key, value, value_num, source: "runner", logged_at: t0 });
  return {
    series,
    params: [
      param("model", "mmm.bayesian@1.0.0"),
      param("fit.engine", "nutpie"),
      param("fit.draws", "1000", 1000),
      param("fit.seed", String(seed), seed),
      param("settings.adstock.l_max", String(lMax), lMax),
      param("settings.saturation.kind", "hill"),
      ...(lMax === 4 ? [param("settings.note", "<b>short</b> adstock <img src=x onerror=alert(1)>")] : []),
    ],
  };
}

function doneTracking(): MockTracking {
  const scalar = (key: string, value: number): MockSeries => ({ key, labels: {}, attempt: 1, points: [{ step: null, value, ts: "2026-10-15T09:12:30Z" }] });
  return { series: [scalar("elpd_loo", -1234.5), scalar("rmse", 412.5), scalar("mape", 14)], params: [] };
}

/** Tracking data of the mock runs (the others have none). */
export function trackingFixtures(): Map<string, MockTracking> {
  return new Map([
    [RUN_RUNNING, fitTracking()],
    [RUN_GRID, gridTracking()],
    [RUN_DONE, doneTracking()],
    [RUN_FIT_A, studyTracking(8, 11, "2026-10-14T14:00:10Z")],
    [RUN_FIT_B, studyTracking(4, 12, "2026-10-14T15:00:10Z")],
  ]);
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
    // M3.5: an MMM optimises budgets (mmm-framework planning).
    decision: true,
  },
  diagnostics: [
    { code: "RHAT_OK", severity: "info", metric: "r_hat_max", value: 1.004, threshold: 1.01, message_expert: "max r-hat 1.004", message_plain: "All chains agree.", var: "beta_channel" },
    { code: "ESS_LOW", severity: "warning", metric: "ess_bulk_min", value: 310, threshold: 400, message_expert: "bulk ESS 310 for lam[tv]", message_plain: "Some estimates have noise.", var: "lam", coords: { channel: "tv" } },
  ],
  scorecard: {
    overall: "warn",
    items: [
      { code: "RHAT_OK", status: "pass", title: "Chains agree", message_plain: "All chains got the same answer.", metric: "r_hat_max", value: 1.004, threshold: 1.01 },
      { code: "HOLDOUT_MAPE", status: "warn", title: "Holdout error", message_plain: "The forecast error on the last 8 weeks was 14%.", metric: "mape", value: 0.14, threshold: 0.1, suggested_action: "Add holidays, or look at the data for the last weeks." },
    ],
  },
  lineage: {
    run_id: RUN_DONE,
    attempt: 1,
    spec_hash: SHA,
    dataset_sha: SHA,
    plugin: "mmm.bayesian@1.0.0",
    base_image: "base-bayes@2026.10",
    seed: 20261014,
    engine: "nutpie",
    executor: "local-process",
    resource_class: "M",
    lib_versions: { pymc: "5.25.1" },
  },
  metrics: { mape: 14, coverage_90: 0.88 }, // mapi_sdk reports MAPE in percent
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
