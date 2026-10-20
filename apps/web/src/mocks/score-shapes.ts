/**
 * Score results by output shape (v0.10), for `--mode mock`, the component tests and the
 * Playwright smoke suite. Three extra models, each scored with a fixed result:
 *
 * - `lgcp-grid`: a summary Arrow output shaped like the LGCP `ui/summary.arrow` (dims `stat`,
 *   `quarter` as `timestamp[ns]`, `row`, `col`; `rate` with NaN cells on 8 quarters × 9 × 12
 *   cells and `total` per quarter: 864 rows after the stat pivot), for the raw-table quick chart.
 * - `churn-classifier`: an evaluate of a binary classifier; the `y_hat` table names the run's
 *   calibration, ROC/PR and confusion plots (`ScoreSummaryTable.plots`).
 * - `disease-areal`: an areal output whose table names the run's `sci.area_map`.
 *
 * The run plots come from `plot-examples.gen.json` when it has the recipe (loaded on first use)
 * and are hand-written Vega-Lite specs with inline Arrow rows otherwise. Each score run is also
 * a finished run in the mock store, so the run detail page shows its plots.
 */
import {
  Field,
  Float64,
  Int64,
  LargeUtf8,
  makeData,
  RecordBatch,
  Schema,
  Struct,
  Table,
  tableToIPC,
  TimestampNanosecond,
  Utf8,
  type Data,
} from "apache-arrow";
import { http, HttpResponse, type HttpHandler } from "msw";

import type { Model, ModelVersion, PlotSpec, Run, ScoreSummary } from "@/api/types";
import { env } from "@/env";

import * as fx from "./fixtures";

const API = env.apiBase;
const enc = new TextEncoder();

export const SHAPE_MODEL_IDS = {
  grid: "mdl_01jaf3v10grid000000001",
  classifier: "mdl_01jaf3v10clf0000000001",
  areal: "mdl_01jaf3v10area000000001",
} as const;

export const SHAPE_RUN_IDS = {
  grid: "run_01jaf3v10grid000000001",
  classifier: "run_01jaf3v10clf0000000001",
  areal: "run_01jaf3v10area000000001",
} as const;

type ShapeKey = keyof typeof SHAPE_MODEL_IDS;

function shapeModel(key: ShapeKey, name: string, plugin: string, inference: NonNullable<ModelVersion["capabilities"]>["inference"], representation: NonNullable<ModelVersion["capabilities"]>["representation"]): { model: Model; version: ModelVersion } {
  const id = SHAPE_MODEL_IDS[key];
  const model: Model = { ...fx.model, id, name, ref: `model://default/${name}`, latest_version: 1, created_at: fx.T0, updated_at: fx.T0 };
  const version: ModelVersion = {
    ...fx.modelVersion,
    id: `mv_${id.slice(4)}`,
    model_id: id,
    version: 1,
    ref: `model://default/${name}@v1`,
    metrics: {},
    diagnostics: [],
    scorecard: null,
    capabilities: { inference, representation, score_modes: ["predict", "forecast", "evaluate"], decision: false },
    lineage: { ...fx.modelVersion.lineage, plugin, engine: null },
  };
  return { model, version };
}

export const SHAPE_MODELS: Record<ShapeKey, { model: Model; version: ModelVersion }> = {
  grid: shapeModel("grid", "lgcp-grid", "sci.lgcp_grid@1.0.0", "mcmc", "draws"),
  classifier: shapeModel("classifier", "churn-classifier", "sklearn.tabular_classifier@1.0.0", "point", "point"),
  areal: shapeModel("areal", "disease-areal", "sci.areal_bym2@1.0.0", "mcmc", "draws"),
};

// ------------------------------------------------------------------------------ Arrow

function base64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// Arrow builders (`vectorFromArray`, `tableFromArrays` of labels) compile a function at run
// time, which the app's CSP refuses (no unsafe-eval): columns are made from typed arrays.

function labels(values: readonly string[], large: boolean): Data {
  const bytes = values.map((v) => enc.encode(v));
  const offsets = large ? new BigInt64Array(values.length + 1) : new Int32Array(values.length + 1);
  let at = 0;
  bytes.forEach((b, i) => {
    at += b.length;
    if (offsets instanceof BigInt64Array) offsets[i + 1] = BigInt(at);
    else offsets[i + 1] = at;
  });
  const data = new Uint8Array(at);
  let pos = 0;
  for (const b of bytes) {
    data.set(b, pos);
    pos += b.length;
  }
  return large
    ? makeData({ type: new LargeUtf8(), length: values.length, nullCount: 0, valueOffsets: offsets as BigInt64Array, data })
    : makeData({ type: new Utf8(), length: values.length, nullCount: 0, valueOffsets: offsets as Int32Array, data });
}

/** An Arrow IPC stream of the columns (one record batch), with optional schema metadata. */
function arrowStream(columns: Record<string, Data>, metadata?: Map<string, string>): Uint8Array {
  const fields = Object.entries(columns).map(([name, d]) => new Field(name, d.type, true));
  const schema = new Schema(fields, metadata);
  const length = Object.values(columns)[0]?.length ?? 0;
  const batch = new RecordBatch(schema, makeData({ type: new Struct(fields), length, nullCount: 0, children: Object.values(columns) }));
  return tableToIPC(new Table(schema, [batch]), "stream");
}

/** An `inline_arrow` dataset of a PlotSpec from columns of numbers or labels. */
export function inlineArrow(columns: Record<string, number[] | string[]>): { kind: "inline_arrow"; data: string } {
  const data = Object.fromEntries(
    Object.entries(columns).map(([k, v]) => [k, typeof v[0] === "string" ? labels(v as string[], false) : makeData({ type: new Float64(), length: v.length, nullCount: 0, data: Float64Array.from(v as number[]) })]),
  );
  return { kind: "inline_arrow", data: base64(arrowStream(data)) };
}

export const GRID_QUARTERS = 8;
export const GRID_ROWS = 9;
export const GRID_COLS = 12;
const GRID_STATS = ["mean", "sd", "hdi_90_lower", "hdi_90_upper"] as const;

/** Cells outside the study area (NaN in every quarter), shaped like a state with a panhandle. */
function outside(r: number, c: number): boolean {
  return (r >= 2 && c < 3) || (r === GRID_ROWS - 1 && c < 6) || (r >= 6 && c === GRID_COLS - 1);
}

/**
 * The LGCP summary as the runner writes it (`to_arrow_table` + `write_arrow_ipc`): long format
 * with a `stat` column (large_string), the quarter as `timestamp[ns]`, int64 cell indices, the
 * `mapi.dims` schema metadata, NaN for the cells outside the study area and `total` repeated
 * over the cells of its quarter.
 */
export function gridSummaryArrow(): Uint8Array {
  const stat: string[] = [];
  const quarter: number[] = [];
  const row: bigint[] = [];
  const col: bigint[] = [];
  const rate: number[] = [];
  const total: number[] = [];
  const sdOf = (m: number) => 0.2 * m;
  const valueOf = (s: (typeof GRID_STATS)[number], m: number) => (s === "mean" ? m : s === "sd" ? sdOf(m) : s === "hdi_90_lower" ? m - 1.645 * sdOf(m) : m + 1.645 * sdOf(m));
  for (const s of GRID_STATS) {
    for (let q = 0; q < GRID_QUARTERS; q++) {
      const at = Date.UTC(2022 + Math.floor(q / 4), (q % 4) * 3, 1);
      let sum = 0;
      for (let r = 0; r < GRID_ROWS; r++) for (let c = 0; c < GRID_COLS; c++) sum += outside(r, c) ? 0 : 1 + 0.15 * r + 0.1 * c + 0.2 * Math.sin(q);
      for (let r = 0; r < GRID_ROWS; r++) {
        for (let c = 0; c < GRID_COLS; c++) {
          const m = 1 + 0.15 * r + 0.1 * c + 0.2 * Math.sin(q);
          stat.push(s);
          quarter.push(at);
          row.push(BigInt(r));
          col.push(BigInt(c));
          rate.push(outside(r, c) ? Number.NaN : valueOf(s, m));
          total.push(valueOf(s, sum));
        }
      }
    }
  }
  const n = stat.length;
  return arrowStream(
    {
      stat: labels(stat, true),
      quarter: makeData({ type: new TimestampNanosecond(), length: n, nullCount: 0, data: BigInt64Array.from(quarter, (ms) => BigInt(ms) * 1_000_000n) }),
      row: makeData({ type: new Int64(), length: n, nullCount: 0, data: BigInt64Array.from(row) }),
      col: makeData({ type: new Int64(), length: n, nullCount: 0, data: BigInt64Array.from(col) }),
      rate: makeData({ type: new Float64(), length: n, nullCount: 0, data: Float64Array.from(rate) }),
      total: makeData({ type: new Float64(), length: n, nullCount: 0, data: Float64Array.from(total) }),
    },
    new Map([["mapi.dims", JSON.stringify(["stat", "quarter", "row", "col"])]]),
  );
}

// ------------------------------------------------------------------------------ summaries

const QUARTERS = Array.from({ length: GRID_QUARTERS }, (_, q) => new Date(Date.UTC(2022 + Math.floor(q / 4), (q % 4) * 3, 1)).toISOString());

export const gridSummary: ScoreSummary = {
  tables: [
    {
      variable: "rate",
      columns: ["quarter", "row", "col", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: QUARTERS.slice(0, 2).flatMap((q) => [[q, 1, 0, 1.2, 0.8, 1.6], [q, 1, 1, 1.3, 0.9, 1.7]]),
      dims: ["quarter", "row", "col"],
      time_dim: "quarter",
    },
    {
      variable: "total",
      columns: ["quarter", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: QUARTERS.map((q, i) => [q, 180 + 4 * i, 160 + 4 * i, 200 + 4 * i]),
      dims: ["quarter"],
      time_dim: "quarter",
    },
  ] as ScoreSummary["tables"],
  truncated: true,
};

export const CLASSIFICATION_PLOTS = ["core.calibration", "core.roc_pr", "core.confusion"];

export const classifierSummary: ScoreSummary = {
  tables: [
    {
      variable: "y_hat",
      columns: ["customer", "mean"],
      rows: Array.from({ length: 6 }, (_, i) => [`c${i + 1}`, [0.12, 0.81, 0.33, 0.67, 0.08, 0.92][i]!]),
      dims: ["customer"],
      plots: CLASSIFICATION_PLOTS,
    },
  ] as ScoreSummary["tables"],
};

const AREAS = ["north", "east", "south", "west", "centre"];

export const arealSummary: ScoreSummary = {
  tables: [
    {
      variable: "cases",
      columns: ["area", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: AREAS.map((a, i) => [a, 12 + 3 * i, 8 + 2 * i, 17 + 4 * i]),
      dims: ["area"],
      plots: ["sci.area_map"],
    },
    {
      variable: "rr",
      columns: ["area", "mean", "hdi_90_lower", "hdi_90_upper"],
      rows: AREAS.map((a, i) => [a, 0.9 + 0.05 * i, 0.7 + 0.05 * i, 1.1 + 0.05 * i]),
      dims: ["area"],
    },
  ] as ScoreSummary["tables"],
};

// ------------------------------------------------------------------------------ run plots

function plot(id: string, title: string, caption: string, spec: PlotSpec["spec"], datasets: PlotSpec["datasets"]): PlotSpec {
  return { id, title, kind: "vega-lite", spec, datasets, schema_version: "v5", persona: "both", caption_plain: caption, provenance: { recipe: `${id}@1.0.0`, generated_at: fx.T0 } };
}

const P = [0.05, 0.12, 0.2, 0.33, 0.41, 0.58, 0.67, 0.74, 0.81, 0.92];
const Y = [0, 0, 0, 1, 0, 1, 1, 0, 1, 1];

/** Minimal stand-ins for the classifier recipes (the real ones come with the regenerated examples). */
export const classifierRunPlots: PlotSpec[] = [
  plot(
    "core.calibration",
    "Calibration",
    "The points are near the diagonal: the predicted chances agree with how often the event occurred.",
    { data: { name: "bins" }, layer: [{ mark: { type: "line", point: true }, encoding: { x: { field: "predicted", type: "quantitative" }, y: { field: "observed", type: "quantitative" } } }] },
    { bins: inlineArrow({ predicted: [0.1, 0.3, 0.5, 0.7, 0.9], observed: [0.08, 0.27, 0.55, 0.66, 0.93] }) },
  ),
  plot(
    "core.roc_pr",
    "ROC and precision-recall curves",
    "The AUC is 0.88. The model separates the classes well. The base rate is 50%.",
    { data: { name: "roc" }, mark: { type: "line" }, encoding: { x: { field: "fpr", type: "quantitative", title: "False positive rate" }, y: { field: "tpr", type: "quantitative", title: "True positive rate" } } },
    { roc: inlineArrow({ fpr: [0, 0, 0.2, 0.2, 0.4, 1], tpr: [0, 0.4, 0.6, 0.8, 1, 1] }) },
  ),
  plot(
    "core.confusion",
    "Confusion matrix",
    "At the threshold 0.5, the accuracy is 80%.",
    {
      data: { name: "cells" },
      layer: [
        { mark: "rect", encoding: { x: { field: "predicted", type: "nominal" }, y: { field: "actual", type: "nominal" }, color: { field: "count", type: "quantitative" } } },
        { mark: "text", encoding: { x: { field: "predicted", type: "nominal" }, y: { field: "actual", type: "nominal" }, text: { field: "count", type: "quantitative" } } },
      ],
    },
    { cells: inlineArrow({ actual: ["0", "0", "1", "1"], predicted: ["0", "1", "0", "1"], count: [4, 1, 1, 4] }) },
  ),
  plot(
    "core.fit_vs_actual",
    "Predictions against actuals",
    "Each point is one customer.",
    { data: { name: "points" }, mark: "point", encoding: { x: { field: "actual", type: "quantitative" }, y: { field: "predicted", type: "quantitative" } } },
    { points: inlineArrow({ actual: Y, predicted: P }) },
  ),
];

/** A stand-in for `sci.area_map` when the examples do not have it. */
const arealFallback: PlotSpec = plot(
  "sci.area_map",
  "Map of areas",
  "The colour of each area is the mean of cases.",
  { data: { name: "areas" }, mark: "bar", encoding: { x: { field: "area", type: "nominal" }, y: { field: "cases", type: "quantitative" } } },
  { areas: inlineArrow({ area: AREAS, cases: AREAS.map((_, i) => 12 + 3 * i) }) },
);

/** The plot `id` from the generated examples (`<id>@1.0.0`), else `fallback`. */
async function example(id: string, fallback: PlotSpec): Promise<PlotSpec> {
  const { plots } = (await import("./plot-examples.gen.json")).default as unknown as { plots: Record<string, PlotSpec | undefined> };
  const spec = plots[`${id}@1.0.0`];
  return spec ? { ...spec, id } : fallback;
}

/** A plot whose data is a file of the run: the run detail page cannot fetch it (no signed links there). */
export const dataRefPlot: PlotSpec = { ...fx.plotSpec, id: "core.ppc_fan", title: "Posterior predictive fan" };

export async function shapeRunPlots(key: ShapeKey): Promise<PlotSpec[]> {
  if (key === "classifier") return Promise.all(classifierRunPlots.map((p) => (p.id === "core.calibration" ? example(p.id, p) : Promise.resolve(p))));
  if (key === "areal") return [await example("sci.area_map", arealFallback), dataRefPlot];
  return [];
}

// ------------------------------------------------------------------------------ runs + handlers

export function shapeRun(key: ShapeKey): Run {
  return {
    ...fx.runDone,
    id: SHAPE_RUN_IDS[key],
    kind: "score",
    state: "succeeded",
    model_version_id: SHAPE_MODELS[key].version.id,
    output_model_version_id: null,
    spec_id: null,
    spec_revision: null,
  };
}

/** The finished score runs, for the run store of `handlers.ts`. */
export const SHAPE_RUNS: Run[] = (Object.keys(SHAPE_RUN_IDS) as ShapeKey[]).map(shapeRun);

const keyOfModel = (id: unknown): ShapeKey | undefined => (Object.keys(SHAPE_MODEL_IDS) as ShapeKey[]).find((k) => SHAPE_MODEL_IDS[k] === id);
const keyOfRun = (id: unknown): ShapeKey | undefined => (Object.keys(SHAPE_RUN_IDS) as ShapeKey[]).find((k) => SHAPE_RUN_IDS[k] === id);

const SUMMARIES: Record<ShapeKey, ScoreSummary> = { grid: gridSummary, classifier: classifierSummary, areal: arealSummary };

/** The models' routes, their score calls and the plots of their score runs (before the generic routes). */
export function createShapeHandlers(shared: { artifacts: Map<string, { bytes: Uint8Array; type: string }> }): HttpHandler[] {
  const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });
  return [
    http.get(`${API}/v1/models/:id`, ({ params }) => {
      const k = keyOfModel(params.id);
      return k ? HttpResponse.json(SHAPE_MODELS[k].model) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions`, ({ params }) => {
      const k = keyOfModel(params.id);
      return k ? page([SHAPE_MODELS[k].version]) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions/:v`, ({ params }) => {
      const k = keyOfModel(params.id);
      return k && params.v === "1" ? HttpResponse.json(SHAPE_MODELS[k].version) : undefined;
    }),
    http.get(`${API}/v1/models/:id/versions/:v/plots`, ({ params }) => (keyOfModel(params.id) ? page([]) : undefined)),
    http.post(new RegExp(`/v1/models/[^/]+/versions/\\d+:score$`), ({ request }) => {
      const k = keyOfModel(new URL(request.url).pathname.split("/")[3]);
      if (!k) return undefined;
      const outputs = [];
      if (k === "grid") {
        const bytes = gridSummaryArrow();
        shared.artifacts.set("ui/summary.arrow", { bytes, type: "application/vnd.apache.arrow.stream" });
        outputs.push(fx.artifactLink("ui/summary.arrow", bytes, "application/vnd.apache.arrow.stream"));
      }
      const result = enc.encode(JSON.stringify({ format: "mapi.score-summary/v1", note: "mock" }));
      shared.artifacts.set("result.json", { bytes: result, type: "application/json" });
      outputs.push(fx.artifactLink("result.json", result, "application/json"));
      return HttpResponse.json({ done: true, run: shapeRun(k), outputs, summary: SUMMARIES[k] }, { headers: { "Preference-Applied": "wait=30" } });
    }),
    http.get(`${API}/v1/runs/:id/plots`, async ({ params }) => {
      const k = keyOfRun(params.id);
      return k ? page(await shapeRunPlots(k)) : undefined;
    }),
  ];
}
