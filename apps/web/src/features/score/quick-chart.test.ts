import { describe, expect, it } from "vitest";

import { quickChart } from "@/features/plots/builtin-specs";
import { decodeArrow, type DecodedTable } from "@/lib/arrow";
import { pivotStats } from "@/lib/summary";
import { GRID_COLS, GRID_QUARTERS, GRID_ROWS, gridSummaryArrow } from "@/mocks/score-shapes";
import { isPlotDatasets } from "@mapi/plot-sandbox/protocol";

import { cellLevels, cellOf, facetsFor, MAX_CELLS, quickChartChoice, quickChartRows } from "./quick-chart";

/** The LGCP-shaped summary (stat × quarter × row × col) as the Score page decodes it. */
const grid = pivotStats(decodeArrow(gridSummaryArrow()));

describe("quick chart of a grid table (864 rows)", () => {
  it("decodes to one row per quarter and cell", () => {
    expect(grid.rows).toHaveLength(GRID_QUARTERS * GRID_ROWS * GRID_COLS);
    expect(grid.rows).toHaveLength(864);
  });

  it("draws time on x, the mean with its band, one cell at a time", () => {
    const choice = quickChartChoice(grid);
    expect(choice).toEqual({ x: "quarter", y: "rate_mean", band: "rate_hdi_90_lower|rate_hdi_90_upper", color: "", facets: ["row", "col"] });
    const levels = cellLevels(grid.rows, choice.facets, choice.y);
    expect(levels.total).toBe(GRID_ROWS * GRID_COLS);
    expect(levels.cells[0]).toBe("0 · 0");
    const rows = quickChartRows(grid.rows, choice, levels.first);
    expect(rows).toHaveLength(8);
    expect(new Set(rows.map((r) => r.quarter)).size).toBe(8);
    const plot = quickChart(rows, { x: choice.x, y: choice.y, lower: "rate_hdi_90_lower", upper: "rate_hdi_90_upper" }, true);
    expect(isPlotDatasets(plot.datasets)).toBe(true);
  });

  it("starts on the first cell with values (a cell outside the study area is empty)", () => {
    const table: DecodedTable = {
      columns: [{ name: "t", type: "Int64" }, { name: "cell", type: "Int64" }, { name: "y_mean", type: "Float64" }],
      rows: [
        { t: 1, cell: 0, y_mean: null },
        { t: 1, cell: 1, y_mean: 2 },
        { t: 2, cell: 0, y_mean: null },
        { t: 2, cell: 1, y_mean: 3 },
      ],
      totalRows: 4,
      truncated: false,
      dims: ["t", "cell"],
    };
    const choice = quickChartChoice(table);
    expect(choice.facets).toEqual(["cell"]);
    expect(cellLevels(table.rows, choice.facets, choice.y)).toEqual({ cells: ["0", "1"], first: "1", total: 2 });
    expect(quickChartRows(table.rows, choice, "1").map((r) => r.y_mean)).toEqual([2, 3]);
  });

  it("follows the x and colour the user picks, and has no cells without dims metadata", () => {
    expect(facetsFor(grid, "row", "")).toEqual(["quarter", "col"]);
    const { dims: _dims, ...plain } = grid;
    expect(quickChartChoice(plain).facets).toEqual([]);
    expect(quickChartRows(grid.rows, { ...quickChartChoice(plain), facets: [] }, null).length).toBeGreaterThan(8);
    expect(cellOf({ row: 3, col: 7 }, ["row", "col"])).toBe("3 · 7");
  });

  it("lists at most MAX_CELLS cells", () => {
    const rows = Array.from({ length: MAX_CELLS + 20 }, (_, i) => ({ t: 1, cell: i, y: i }));
    const levels = cellLevels(rows, ["cell"], "y");
    expect(levels.cells).toHaveLength(MAX_CELLS);
    expect(levels.total).toBe(MAX_CELLS + 20);
  });
});
