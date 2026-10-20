/**
 * v0.15 deep learning in the mock API: a finished `dl.cnn_classifier` fit on the GPU class with
 * what the `mapi_jax` trainer logs (I-9): `loss` and `accuracy` by `split` (train at every step,
 * val at each epoch end), `lr` and `epoch`, `dl.*` params, the media `val_predictions` and
 * `confusion` (label `split: val`) at 5 evaluation steps, and the plots of the three training
 * recipes (`dl-plots.json`, rendered by `mapi_plots` from `examples.training_tree`).
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { PlotSpec, Run, RunParam } from "@/api/types";
import { env } from "@/env";

import * as fx from "./fixtures";
import type { MockSeries, MockTracking } from "./fixtures";
import { addItem, type MediaState } from "./media";
import { encodeRgbPng } from "./png";

const API = env.apiBase;

export const DL_RUN_ID = "run_01jaf3q2x8m9v7k5h4g3f2e1k8";
export const DL_EPOCHS = 10;
export const DL_STEPS_PER_EPOCH = 40;
/** The evaluation steps with media (every second epoch end). */
export const DL_MEDIA_STEPS = [2, 4, 6, 8, 10].map((e) => e * DL_STEPS_PER_EPOCH);
export const DL_CLASSES = ["circle", "square", "cross"];

const T0 = "2026-10-16T08:00:00Z";
const at = (seconds: number) => new Date(Date.parse(T0) + seconds * 1000).toISOString();

export const dlRun: Run = {
  ...fx.runDone,
  id: DL_RUN_ID,
  kind: "fit",
  state: "succeeded",
  resource_class: "GPU",
  output_model_version_id: null,
  created_at: T0,
  updated_at: at(400),
  started_at: at(5),
  finished_at: at(400),
  name: "Shapes CNN",
  tags: { model: "dl.cnn_classifier", data: "shapes" },
  experiment_id: null,
};

/** A noise value in [-1, 1] that depends only on `i` (the mock has no RNG). */
const wobble = (i: number) => Math.sin(i * 12.9898) * 0.5 + Math.sin(i * 4.1414) * 0.5;

export function dlTracking(): MockTracking {
  const series: MockSeries[] = [];
  const steps = Array.from({ length: DL_EPOCHS * DL_STEPS_PER_EPOCH }, (_, i) => i + 1);
  const tOf = (step: number) => step / DL_STEPS_PER_EPOCH;
  const round = (v: number) => Number(v.toFixed(4));
  const train = (key: string, f: (t: number, i: number) => number): MockSeries => ({
    key,
    labels: { split: "train" },
    attempt: 1,
    points: steps.map((s, i) => ({ step: s, value: round(f(tOf(s), i)), ts: at(5 + s) })),
  });
  series.push(train("loss", (t, i) => 1.1 * Math.exp(-t / 2.5) + 0.05 + 0.06 * wobble(i)));
  series.push(train("accuracy", (t, i) => Math.min(1, Math.max(0, 1 - 0.67 * Math.exp(-t / 2) + 0.03 * wobble(i + 7)))));
  const ends = Array.from({ length: DL_EPOCHS }, (_, e) => (e + 1) * DL_STEPS_PER_EPOCH);
  const val = (key: string, f: (e: number) => number): MockSeries => ({
    key,
    labels: { split: "val" },
    attempt: 1,
    points: ends.map((s, e) => ({ step: s, value: round(f(e + 1)), ts: at(5 + s) })),
  });
  // the validation loss goes up after epoch 7 (the best epoch): early signs of overfitting
  series.push(val("loss", (e) => 1.15 * Math.exp(-e / 2.2) + 0.18 + 0.06 * Math.max(0, e - 7) ** 1.3));
  series.push(val("accuracy", (e) => 1 - 0.66 * Math.exp(-e / 2) - 0.02 * Math.max(0, e - 7)));
  series.push({ key: "lr", labels: {}, attempt: 1, points: steps.map((s) => ({ step: s, value: Number((1e-3 * 0.5 * (1 + Math.cos((Math.PI * (s - 1)) / steps.length))).toExponential(4)), ts: at(5 + s) })) });
  series.push({ key: "epoch", labels: {}, attempt: 1, points: ends.map((s, e) => ({ step: s, value: e + 1, ts: at(5 + s) })) });
  const param = (key: string, value: string, value_num: number | null = null): RunParam => ({ key, value, value_num, source: "runner", logged_at: at(5) });
  return {
    series,
    params: [
      param("model", "dl.cnn_classifier@1.0.0"),
      param("dl.backend", "gpu"),
      param("dl.device", "NVIDIA L4"),
      param("dl.parameters", "24675", 24675),
      param("dl.arch.kind", "resnet"),
      param("dl.arch.widths", "[16, 32, 64]"),
      param("dl.train.epochs", String(DL_EPOCHS), DL_EPOCHS),
      param("dl.train.batch_size", "64", 64),
      param("dl.train.optimizer", "adamw"),
      param("dl.train.learning_rate", "0.001", 0.001),
      param("dl.train.schedule", "warmup_cosine"),
      param("fit.seed", "7", 7),
    ],
  };
}

/** A 48 x 48 grid of 4 x 4 validation images (12 px each) with a green or red frame per prediction. */
function predictionGrid(epoch: number): Uint8Array {
  const size = 48;
  const rgb = new Uint8Array(size * size * 3).fill(245);
  for (let k = 0; k < 16; k++) {
    const ox = (k % 4) * 12;
    const oy = Math.floor(k / 4) * 12;
    const shape = k % 3;
    const right = (k * 7 + epoch) % 10 < 4 + Math.floor(epoch / 2);
    for (let y = 0; y < 12; y++) {
      for (let x = 0; x < 12; x++) {
        const o = ((oy + y) * size + ox + x) * 3;
        const frame = x === 0 || y === 0 || x === 11 || y === 11;
        const cx = x - 5.5;
        const cy = y - 5.5;
        const on = shape === 0 ? Math.abs(Math.hypot(cx, cy) - 3.5) < 1 : shape === 1 ? Math.max(Math.abs(cx), Math.abs(cy)) < 4.5 && Math.max(Math.abs(cx), Math.abs(cy)) > 3 : Math.abs(cx) < 1 || Math.abs(cy) < 1;
        const c = frame ? (right ? [46, 125, 50] : [198, 40, 40]) : on ? [40, 60, 120] : [235, 238, 244];
        rgb.set(c, o);
      }
    }
  }
  return encodeRgbPng(size, size, rgb);
}

/** A 3 x 3 confusion heatmap (12 px cells): darker blue on the diagonal as the training goes on. */
function confusionPng(epoch: number): Uint8Array {
  const size = 36;
  const rgb = new Uint8Array(size * size * 3);
  const acc = 1 - 0.66 * Math.exp(-epoch / 2);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = Math.floor(y / 12);
      const j = Math.floor(x / 12);
      const share = i === j ? acc : (1 - acc) / 2;
      const o = (y * size + x) * 3;
      rgb[o] = Math.round(235 - 200 * share);
      rgb[o + 1] = Math.round(240 - 160 * share);
      rgb[o + 2] = Math.round(248 - 60 * share);
    }
  }
  return encodeRgbPng(size, size, rgb);
}

/** The media of the DL fit: `val_predictions` and `confusion` at the 5 media steps. */
export function addDlMedia(state: MediaState, artifacts: Map<string, { bytes: Uint8Array; type: string }>): void {
  DL_MEDIA_STEPS.forEach((step) => {
    const epoch = step / DL_STEPS_PER_EPOCH;
    const t = at(5 + step);
    addItem(state, artifacts, DL_RUN_ID, { key: "val_predictions", kind: "image", contentType: "image/png", bytes: predictionGrid(epoch), step, labels: { split: "val" }, caption: `Validation images at epoch ${epoch}: true and predicted class`, width: 48, height: 48, at: t });
    addItem(state, artifacts, DL_RUN_ID, { key: "confusion", kind: "image", contentType: "image/png", bytes: confusionPng(epoch), step, labels: { split: "val" }, caption: `Validation confusion matrix at epoch ${epoch}`, width: 36, height: 36, at: t });
  });
}

/** The plots of the DL fit, in the order the runner lists them. */
export const DL_PLOT_IDS = ["ml.training_curves", "ml.confusion_epochs", "ml.misclassified"] as const;

/** The rendered plots (`dl-plots.json`, loaded on first use like the plot examples). */
export async function dlPlotSpecs(): Promise<PlotSpec[]> {
  const { plots } = (await import("./dl-plots.json")).default as unknown as { plots: Record<string, PlotSpec | undefined> };
  return DL_PLOT_IDS.map((id) => plots[id]).filter((p): p is PlotSpec => Boolean(p));
}

/** The plots route of the DL fit (before the generic run routes). */
export function createDlHandlers(): HttpHandler[] {
  return [http.get(`${API}/v1/runs/${DL_RUN_ID}/plots`, async () => HttpResponse.json({ items: await dlPlotSpecs(), next_cursor: null }))];
}
