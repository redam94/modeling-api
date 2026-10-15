/**
 * Pure spec preparation (no DOM, no renderer imports) so it is unit-testable: dataset
 * injection, Plotly `$ref` resolution, theme merging.
 */
import type { JsonObject, JsonValue, PlotDatasets, Row, Scalar, Theme } from "./protocol";
import { themeRecipeColours } from "./recipe-palette";
import { plotlyLayout, vegaConfig } from "./theme";

export class SpecError extends Error {
  override name = "SpecError";
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Recursive merge where `override` wins; arrays and scalars are replaced, not merged. */
export function deepMerge(base: Record<string, unknown>, override: unknown): Record<string, unknown> {
  if (!isObject(override)) return { ...base };
  const out: Record<string, unknown> = { ...base };
  for (const [k, v] of Object.entries(override)) {
    out[k] = isObject(v) && isObject(out[k]) ? deepMerge(out[k], v) : v;
  }
  return out;
}

/** Column `column` of dataset `name` as an array (Plotly `{"$ref": "name.column"}`). */
export function column(datasets: PlotDatasets, ref: string): Scalar[] {
  const dot = ref.indexOf(".");
  if (dot <= 0 || dot === ref.length - 1) throw new SpecError(`invalid $ref ${JSON.stringify(ref)}`);
  const name = ref.slice(0, dot);
  const col = ref.slice(dot + 1);
  const rows: Row[] | undefined = Object.prototype.hasOwnProperty.call(datasets, name)
    ? datasets[name]
    : undefined;
  if (!rows) throw new SpecError(`unknown dataset ${JSON.stringify(name)} in $ref`);
  if (rows.length && !Object.prototype.hasOwnProperty.call(rows[0], col)) {
    throw new SpecError(`dataset ${JSON.stringify(name)} has no column ${JSON.stringify(col)}`);
  }
  return rows.map((r) => (Object.prototype.hasOwnProperty.call(r, col) ? (r[col] ?? null) : null));
}

/** Replace every `{"$ref": "name.column"}` node with the referenced column. */
export function resolvePlotlyRefs(node: JsonValue, datasets: PlotDatasets): JsonValue {
  if (Array.isArray(node)) return node.map((x) => resolvePlotlyRefs(x, datasets));
  if (!isObject(node)) return node;
  const keys = Object.keys(node);
  if (keys.length === 1 && keys[0] === "$ref") {
    const ref = node.$ref;
    if (typeof ref !== "string") throw new SpecError("$ref must be a string");
    return column(datasets, ref);
  }
  const out: JsonObject = {};
  for (const [k, v] of Object.entries(node)) out[k] = resolvePlotlyRefs(v, datasets);
  return out;
}

export interface PreparedPlotly {
  data: JsonValue[];
  layout: Record<string, unknown>;
  frames: JsonValue[];
}

export function preparePlotly(spec: JsonObject, datasets: PlotDatasets, theme: Theme): PreparedPlotly {
  const resolved = resolvePlotlyRefs(spec, datasets) as JsonObject;
  const data = resolved.data;
  if (!Array.isArray(data)) throw new SpecError("a Plotly spec needs a 'data' array");
  const layout = deepMerge(plotlyLayout(theme), resolved.layout);
  layout.autosize = true;
  const frames = Array.isArray(resolved.frames) ? resolved.frames : [];
  return { data, layout, frames };
}

const UNIT_KEYS = ["mark", "layer"];

/** Inject named datasets and theme config (and theme the recipe palette) into a Vega-Lite spec. */
export function prepareVegaLite(spec: JsonObject, datasets: PlotDatasets, theme: Theme): JsonObject {
  // explicit recipe colours beat any config, so they are swapped for their theme variants
  spec = themeRecipeColours(spec, theme) as JsonObject;
  const out: JsonObject = { ...spec };
  const inline = isObject(spec.datasets) ? (spec.datasets) : {};
  out.datasets = { ...inline, ...(datasets as unknown as JsonObject) };
  out.config = deepMerge(vegaConfig(theme), spec.config) as JsonObject;
  const isUnit = UNIT_KEYS.some((k) => k in spec);
  if (isUnit && spec.width === undefined) out.width = "container";
  // Vega-Lite only supports "fit" autosizing for single and layered views. On a composite
  // (concat/facet/repeat) spec it computes a zero-width view, so those keep their own sizes.
  if (spec.autosize === undefined) out.autosize = isUnit ? { type: "fit-x", contains: "padding" } : { type: "pad" };
  delete out.$schema;
  return out;
}
