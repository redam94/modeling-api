/**
 * First-party Vega-Lite specs the app builds from view data (missingness heatmap,
 * preprocessing-state heatmap, quick result charts). They still render only inside the plot
 * sandbox; data travels as named datasets, never inline URLs.
 */
import type { JsonObject, PlotDatasets, Row } from "@mapi/plot-sandbox/protocol";

import type { MissingnessGrid, StateArray } from "@/lib/views";

export interface BuiltPlot {
  spec: JsonObject;
  datasets: PlotDatasets;
}

export function missingnessHeatmap(grid: MissingnessGrid): BuiltPlot {
  const rows: Row[] = [];
  grid.labels.forEach((label, i) => {
    grid.coords.forEach((coord, j) => {
      rows.push({ label, coord, missing: grid.fraction[i]?.[j] ?? 0 });
    });
  });
  return {
    datasets: { missing: rows },
    spec: {
      data: { name: "missing" },
      mark: { type: "rect" },
      height: { step: 18 },
      encoding: {
        x: { field: "coord", type: "ordinal", title: grid.along, axis: { labelOverlap: true, labelAngle: 0 } },
        y: { field: "label", type: "nominal", title: null, sort: grid.labels },
        color: {
          field: "missing",
          type: "quantitative",
          title: "Missing",
          scale: { domain: [0, 1] },
          legend: { format: ".0%" },
        },
        tooltip: [
          { field: "label", title: "Series" },
          { field: "coord", title: grid.along },
          { field: "missing", title: "Missing", format: ".0%" },
        ],
      },
    },
  };
}

export function stateArrayPlot(array: StateArray): BuiltPlot {
  const [rowDim, colDim] = array.dims;
  const rows: Row[] = [];
  if (array.dims.length === 2 && rowDim && colDim) {
    const rc = array.coords[rowDim] ?? [];
    const cc = array.coords[colDim] ?? [];
    array.values.forEach((vals, i) => {
      vals.forEach((v, j) => {
        rows.push({ [rowDim]: String(rc[i] ?? i), [colDim]: String(cc[j] ?? j), value: v });
      });
    });
    return {
      datasets: { state: rows },
      spec: {
        data: { name: "state" },
        mark: { type: "rect" },
        height: { step: 28 },
        encoding: {
          x: { field: colDim, type: "nominal", axis: { labelAngle: 0 } },
          y: { field: rowDim, type: "nominal" },
          color: { field: "value", type: "quantitative", title: array.name },
          tooltip: [{ field: rowDim }, { field: colDim }, { field: "value", format: ".4~g" }],
        },
      },
    };
  }
  const dim = rowDim ?? "index";
  const coords = rowDim ? (array.coords[rowDim] ?? []) : [];
  (array.values[0] ?? []).forEach((v, i) => rows.push({ [dim]: String(coords[i] ?? i), value: v }));
  return {
    datasets: { state: rows },
    spec: {
      data: { name: "state" },
      mark: { type: "bar" },
      encoding: {
        x: { field: dim, type: "nominal", axis: { labelAngle: 0 } },
        y: { field: "value", type: "quantitative", title: array.name },
        tooltip: [{ field: dim }, { field: "value", format: ".4~g" }],
      },
    },
  };
}

export interface QuickChart {
  x: string;
  y: string;
  lower?: string | undefined;
  upper?: string | undefined;
  color?: string | undefined;
}

/** A line (plus optional band) over decoded result rows. */
export function quickChart(rows: Row[], c: QuickChart, xIsTime: boolean): BuiltPlot {
  const xType = xIsTime ? "temporal" : "quantitative";
  const base = {
    x: { field: c.x, type: rows.length && typeof rows[0]?.[c.x] === "string" && !xIsTime ? "ordinal" : xType },
    ...(c.color ? { color: { field: c.color, type: "nominal" } } : {}),
  };
  const layers: JsonObject[] = [];
  if (c.lower && c.upper) {
    layers.push({
      mark: { type: "area", opacity: 0.22 },
      encoding: { ...base, y: { field: c.lower, type: "quantitative", title: c.y }, y2: { field: c.upper } },
    });
  }
  layers.push({
    mark: { type: "line", strokeWidth: 1.6 },
    encoding: {
      ...base,
      y: { field: c.y, type: "quantitative" },
      tooltip: [{ field: c.x }, { field: c.y, format: ".4~g" }],
    },
  });
  return { datasets: { result: rows }, spec: { data: { name: "result" }, layer: layers } };
}
