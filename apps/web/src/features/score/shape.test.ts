import { describe, expect, it } from "vitest";

import type { FieldEntry } from "@/features/explore/views";
import { gridScoreSummary, scoreFieldsView } from "@/mocks/score-fields";

import { classify, gridOutputsOf, intervalRows, shapesOf, statsOf, tableDims, type Table } from "./shape";

const NO_GRIDS = new Map<string, FieldEntry[]>();

describe("tableDims", () => {
  it("uses the server's dims, else the leading columns that are not statistics", () => {
    expect(tableDims({ variable: "y", columns: ["geo", "time", "mean"], rows: [], dims: ["geo", "time"] })).toEqual(["geo", "time"]);
    expect(tableDims({ variable: "y", columns: ["geo", "time", "mean", "hdi_90_lower", "hdi_90_upper"], rows: [] })).toEqual(["geo", "time"]);
    expect(tableDims({ variable: "y", columns: ["month", "median", "q5", "q95"], rows: [] })).toEqual(["month"]);
    expect(tableDims({ variable: "y", columns: ["mean", "sd"], rows: [] })).toEqual([]);
  });
});

describe("classify", () => {
  it("makes an output of ui/fields.json a grid, even when its table is there", () => {
    const grids = gridOutputsOf(scoreFieldsView);
    const shape = classify(gridScoreSummary.tables![0]!, grids);
    expect(shape).toMatchObject({ kind: "grid", variable: "intensity" });
    expect(shape.kind === "grid" && shape.fields[0]?.frame_dim).toBe("period");
  });

  it("finds the time dim from time_dim, ISO dates or the dim's name", () => {
    const named: Table = { variable: "y", columns: ["quarter", "mean"], rows: [["2019Q1", 1]] };
    expect(classify(named, NO_GRIDS)).toEqual({ kind: "time", variable: "y", time: "quarter" });
    const dated: Table = { variable: "y", columns: ["geo", "ts", "mean"], rows: [["de", "2026-10-01", 1], ["fr", "2026-10-01", 2]] };
    expect(classify(dated, NO_GRIDS)).toEqual({ kind: "time", variable: "y", time: "ts", series: "geo" });
    const declared: Table = { variable: "y", columns: ["k", "mean"], rows: [[1, 2]], dims: ["k"], time_dim: "k" };
    expect(classify(declared, NO_GRIDS)).toEqual({ kind: "time", variable: "y", time: "k" });
  });

  it("colours by a second dim only when it has few levels", () => {
    const rows = Array.from({ length: 20 }, (_, i) => [`c${i}`, "2026-10-01", i]);
    expect(classify({ variable: "y", columns: ["cell", "time", "mean"], rows }, NO_GRIDS)).toEqual({ kind: "time", variable: "y", time: "time" });
  });

  it("makes other dims categorical, a circular table categorical and no dims a scalar", () => {
    expect(classify({ variable: "roi", columns: ["channel", "mean"], rows: [["tv", 1.2]] }, NO_GRIDS)).toEqual({ kind: "categorical", variable: "roi", dims: ["channel"] });
    expect(classify({ variable: "y", columns: ["hour", "mean"], rows: [[1, 2]], period: 24, dims: ["hour"], time_dim: "hour" }, NO_GRIDS)).toEqual({ kind: "categorical", variable: "y", dims: ["hour"] });
    expect(classify({ variable: "peak", columns: ["mean", "sd"], rows: [[1, 0.1]] }, NO_GRIDS)).toEqual({ kind: "scalar", variable: "peak" });
  });
});

describe("shapesOf", () => {
  it("keeps the tables' order and adds the grid outputs a truncated summary left out", () => {
    const shapes = shapesOf(gridScoreSummary, scoreFieldsView);
    expect(shapes.map((s) => [s.variable, s.kind])).toEqual([
      ["intensity", "grid"],
      ["total", "time"],
      ["peak", "scalar"],
      ...scoreFieldsView.fields.filter((f) => f.output !== "intensity").map((f) => [f.output, "grid"]),
    ]);
    expect(shapesOf(undefined, null)).toEqual([]);
  });
});

describe("intervalRows", () => {
  it("labels each row by its coordinates and takes the widest band", () => {
    const t: Table = {
      variable: "roi",
      columns: ["geo", "channel", "mean", "hdi_50_lower", "hdi_50_upper", "hdi_90_lower", "hdi_90_upper"],
      rows: [["de", "tv", 1.2, 1, 1.4, 0.8, 1.6]],
    };
    expect(statsOf(t)).toEqual({ center: "mean", band: { lower: "hdi_90_lower", upper: "hdi_90_upper", label: "hdi 90" } });
    expect(intervalRows(t, ["geo", "channel"])).toEqual([{ label: "de · tv", mean: 1.2, lower: 0.8, upper: 1.6 }]);
  });
});
