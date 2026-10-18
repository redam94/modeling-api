/**
 * Plot outputs of workflow nodes (`plot.render`, the `plot` ports of `model.forecast` and
 * `evaluate.residuals`, `plot` sections of a report): a PlotSpec whose datasets are in the
 * document (`inline_arrow`), or rows. Decoded here for the plot sandbox; a dataset that
 * is a file (`data_ref`) cannot be drawn inline.
 */
import type { JsonObject, PlotDatasets, Row } from "@mapi/plot-sandbox/protocol";

import { MAX_PLOT_ROWS } from "@/features/plots/plot-data";
import { base64ToBytes, decodeArrow } from "@/lib/arrow";

export interface InlinePlot {
  kind: "vega-lite" | "plotly";
  spec: JsonObject;
  datasets: PlotDatasets;
  caption: string | null;
}

type Obj = Record<string, unknown>;

function obj(v: unknown): Obj | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
}

/** True when `value` looks like a plot payload (a PlotSpec or `{kind, spec, datasets}`). */
export function isPlotPayload(value: unknown): boolean {
  const o = obj(value);
  return Boolean(o && obj(o.spec) && (o.kind === "vega-lite" || o.kind === "plotly"));
}

/**
 * The plot ready for the sandbox, or `null` when a dataset is not in the document (a file).
 * Throws when an embedded dataset cannot be decoded.
 */
export function inlinePlot(value: unknown): InlinePlot | null {
  const o = obj(value);
  const spec = o ? obj(o.spec) : null;
  if (!o || !spec || (o.kind !== "vega-lite" && o.kind !== "plotly")) return null;
  const datasets: PlotDatasets = {};
  for (const [name, ds] of Object.entries(obj(o.datasets) ?? {})) {
    if (Array.isArray(ds)) {
      datasets[name] = ds as Row[];
      continue;
    }
    const d = obj(ds);
    if (d?.kind !== "inline_arrow" || typeof d.data !== "string") return null;
    datasets[name] = decodeArrow(base64ToBytes(d.data), MAX_PLOT_ROWS).rows;
  }
  return {
    kind: o.kind,
    spec: spec as JsonObject,
    datasets,
    caption: typeof o.caption_plain === "string" ? o.caption_plain : null,
  };
}
