import { describe, expect, it } from "vitest";

import type { ModelInputSchema } from "@/api/types";
import gen from "@/mocks/editor.gen.json";

import { fillColumn, fittedRange, freqUnit, futureLabels, gridColumns, gridToInline, inputVars, seedRows, summarize } from "./inputs";

const inputs = gen.model_inputs as unknown as ModelInputSchema;

describe("model inputs", () => {
  it("describes frequencies and fitted ranges in words", () => {
    expect([freqUnit("D"), freqUnit("W-SUN"), freqUnit("MS"), freqUnit("QE"), freqUnit("weird")]).toEqual(["day", "week", "month", "quarter", null]);
    expect(fittedRange(inputs.snapshot.fit_coords?.time)).toBe("2024-10-01 to 2026-09-30 (730 days)");
    expect(fittedRange(inputs.snapshot.fit_coords?.geo)).toBe("3 levels: de, fr, it");
    expect(inputVars(inputs.snapshot).map((v) => [v.name, v.future])).toEqual([
      ["y", false],
      ["spend", true],
    ]);
  });

  it("steps the periods after the fit by its frequency", () => {
    expect(futureLabels({ kind: "time", size: 1, end: "2026-09-27", freq: "W-SUN" }, 2)).toEqual(["2026-10-04", "2026-10-11"]);
    expect(futureLabels({ kind: "time", size: 1, end: "2026-12-01", freq: "MS" }, 2)).toEqual(["2027-01-01", "2027-02-01"]);
    expect(futureLabels({ kind: "integer", size: 1, end: 9 }, 2)).toEqual(["10", "11"]);
    expect(futureLabels({ kind: "time", size: 1, end: "2026-09-30", freq: null }, 2)).toEqual([]);
  });

  it("seeds every fitted level, and the target only where a forecast takes actuals", () => {
    const forecast = gridColumns(inputs, "forecast");
    expect(forecast.map((c) => c.name)).toEqual(["time", "geo", "channel", "spend"]);
    const rows = seedRows(inputs, forecast, "forecast", 2);
    expect(rows).toHaveLength(2 * 3 * 2);
    expect(rows[0]).toEqual(["2026-10-01", "de", "search", ""]);
    expect(rows.at(-1)).toEqual(["2026-10-02", "it", "tv", ""]);
    // Other modes start from the last fitted period.
    expect(seedRows(inputs, gridColumns(inputs, "predict"), "predict", 8).map((r) => r[0])).toEqual(Array(6).fill("2026-09-30"));
    const actuals = { ...inputs, snapshot: { ...inputs.snapshot, conditions_on_actuals: true } };
    expect(gridColumns(actuals, "forecast").map((c) => c.name)).toContain("y");
  });

  it("sends only columns with values, as XrJSON", () => {
    const columns = gridColumns(inputs, "forecast");
    const rows = fillColumn(seedRows(inputs, columns, "forecast", 1), 3, "5");
    expect(summarize(columns, rows)).toMatchObject({ rows: 6, filled: 6, emptyColumns: [] });
    const data = gridToInline(columns, rows, "time")!;
    expect(data.coords.time!.data).toEqual(["2026-10-01"]);
    expect(data.data_vars.spend).toEqual({ dims: ["geo", "channel", "time"], data: [[[5], [5]], [[5], [5]], [[5], [5]]] });
    expect(gridToInline(columns, seedRows(inputs, columns, "forecast", 1), "time")).toBeNull();
    // fill only touches blanks unless asked to overwrite
    expect(fillColumn([["a", "1"], ["b", ""]], 1, "9")).toEqual([["a", "1"], ["b", "9"]]);
    expect(fillColumn([["a", "1"]], 1, "9", true)).toEqual([["a", "9"]]);
  });
});
