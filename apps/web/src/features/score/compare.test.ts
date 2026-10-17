import { describe, expect, it } from "vitest";

import type { ScoreSummary } from "@/api/types";

import { compareActuals, comparisonChart } from "./compare";

/** A quantile model's forecast of `sales` per region, sent actuals for Jan and Feb 2023. */
const SUMMARY: ScoreSummary = {
  tables: [
    {
      variable: "sales_hat",
      columns: ["region", "month", "median", "q5", "q95"],
      rows: [
        ["n", "2023-01-01", 128, 120, 136],
        ["n", "2023-02-01", 135, 127, 143],
        ["n", "2023-03-01", 133, 121, 145],
        ["s", "2023-01-01", 50, 45, 55],
        ["s", "2023-02-01", 52, 47, 57],
        ["s", "2023-03-01", 53, 46, 60],
      ],
    },
  ],
  truncated: false,
  actuals: [
    {
      variable: "sales_hat",
      columns: ["region", "month", "actual"],
      rows: [
        ["n", "2023-01-01", 130],
        ["n", "2023-02-01", 131],
        ["s", "2023-01-01", 50],
      ],
    },
  ],
};

describe("predictions against actuals", () => {
  const c = compareActuals(SUMMARY, SUMMARY.actuals![0]!, "time")!;

  it("joins actuals to the predictions on their coordinates", () => {
    expect(c).not.toBeNull();
    expect([c.periodColumn, c.seriesColumns, c.center, c.band?.label]).toEqual(["month", ["region"], "median", "q5–q95"]);
    expect(c.series).toEqual(["n", "s"]);
    const n = c.points.filter((p) => p.series === "n");
    expect(n.map((p) => [p.period, p.actual, p.predicted, p.forecast])).toEqual([
      ["2023-01-01", 130, 128, false],
      ["2023-02-01", 131, 135, false],
      ["2023-03-01", null, 133, true],
    ]);
    // each series forecasts after its own last actual
    expect(c.points.filter((p) => p.series === "s" && p.forecast).map((p) => p.period)).toEqual(["2023-02-01", "2023-03-01"]);
  });

  it("measures how far off the one-step-ahead predictions were", () => {
    expect(c.metrics.n).toBe(3);
    expect(c.metrics.mae).toBeCloseTo((2 + 4 + 0) / 3);
    expect(c.metrics.bias).toBeCloseTo((-2 + 4 + 0) / 3);
    expect(c.metrics.mape).toBeCloseTo(((2 / 130 + 4 / 131 + 0) / 3) * 100);
    expect(c.metrics.coverage).toBe(100);
  });

  it("refuses tables it cannot join", () => {
    const wider = { ...SUMMARY, tables: [{ ...SUMMARY.tables![0]!, columns: ["region", "month", "channel", "median"], rows: [] }] };
    expect(compareActuals(wider, SUMMARY.actuals![0]!)).toBeNull();
    expect(compareActuals({ ...SUMMARY, tables: [] }, SUMMARY.actuals![0]!)).toBeNull();
  });

  it("charts the band, the predictions (dashed when forecast) and the actuals", () => {
    const chart = comparisonChart(c, "n");
    // 3 periods, plus the forecast line's start at the last one-step-ahead point
    expect(chart.datasets.comparison).toHaveLength(4);
    expect(chart.datasets.comparison![2]).toMatchObject({ period: "2023-03-01", kind: "forecast", actual: null });
    expect(chart.datasets.comparison![3]).toMatchObject({ period: "2023-02-01", kind: "forecast", predicted: 135, actual: null });
    const layers = chart.spec.layer as { mark: { type: string } }[];
    expect(layers.map((l) => l.mark.type)).toEqual(["area", "line", "point"]);
    // all series at once are coloured by series
    expect(JSON.stringify(comparisonChart(c, null).spec)).toContain('"field":"series"');
  });
});
