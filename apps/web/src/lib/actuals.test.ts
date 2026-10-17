import { describe, expect, it } from "vitest";

import type { ForecastConditioning, InputTable } from "@/api/types";

import { cellText, conditioningHeadline, forecastPeriods, nextLabel, parseCell, parsePasted, tableForecastCall, tableToXr } from "./actuals";

const TABLE: InputTable = {
  horizon_dim: "t",
  conditions_on_actuals: true,
  columns: [
    { name: "t", role: "coord", dims: [], freq: "MS", description: "The period (yyyy-MM-dd)" },
    { name: "y", role: "target", dims: ["t"], description: "The actual" },
    { name: "price", role: "driver", dims: ["t"], description: "Needed on every period" },
  ],
  rows: [
    ["1961-01-01", null, null],
    ["1961-02-01", null, null],
  ],
};

describe("forecasts with actuals", () => {
  it("turns actual rows and rows to forecast into the call", () => {
    const rows = [
      ["1961-01-01", 417, 10.5],
      ["1961-02-01", 391, 10.5],
      ["1961-03-01", null, 11],
      ["1961-04-01", null, null],
      [null, null, null], // a blank editor row
    ];
    const call = tableForecastCall(TABLE, rows, { output: { summary: ["median", "q5", "q95"] } });
    expect(call).toEqual({
      mode: "forecast",
      horizon: { dim: "t", periods: 2 },
      future_covariates: "carry_last",
      output: { summary: ["median", "q5", "q95"] },
      data: {
        coords: { t: { dims: ["t"], data: ["1961-01-01", "1961-02-01", "1961-03-01", "1961-04-01"] } },
        data_vars: {
          y: { dims: ["t"], data: [417, 391, null, null] },
          price: { dims: ["t"], data: [10.5, 10.5, 11, null] },
        },
      },
    });
  });

  it("appends the horizon after a table that ends with an actual, or forecasts every listed date", () => {
    expect(tableForecastCall(TABLE, [["1961-01-01", 417, 1]], {}, 6).horizon).toEqual({ dim: "t", periods: 6 });
    const dates = tableToXr(TABLE, [["1961-01-01", null, null], ["1961-02-01", null, null]]);
    expect(forecastPeriods(TABLE, dates)).toBe(2);
  });

  it("explains a malformed table", () => {
    expect(() => tableToXr(TABLE, [[null, 417, null], ["1961-01-01", 1, 2]])).toThrow('row 1 has no "t"');
    expect(() => tableToXr(TABLE, [["1961-01-01", 1, null], ["1961-01-01", 2, null]])).toThrow("two different values");
    expect(() => tableForecastCall({ ...TABLE, horizon_dim: null }, [], {})).toThrow("no dim a forecast extends");
  });

  it("parses cells and pasted spreadsheet ranges", () => {
    const [t, y] = TABLE.columns!;
    expect(parseCell(" 1,520.5 ", y!)).toBe(1520.5);
    expect(parseCell("", y!)).toBeNull();
    expect(parseCell("n/a", y!)).toBe("n/a");
    expect(parseCell("1961-01-01", t!)).toBe("1961-01-01");
    expect(parseCell("3", { name: "store", role: "coord" })).toBe(3);
    expect(cellText(null)).toBe("");
    expect(parsePasted(TABLE, "price\tt\ty\n10.5\t1961-01-01\t417\n\t1961-02-01\t\n")).toEqual([
      ["1961-01-01", 417, 10.5],
      ["1961-02-01", null, null],
    ]);
    expect(parsePasted(TABLE, "1961-01-01,417,1")).toEqual([["1961-01-01", 417, 1]]);
  });

  it("steps dates by the frequency", () => {
    expect(nextLabel("1961-12-01", "MS")).toBe("1962-01-01");
    expect(nextLabel("2026-01-04", "W-SUN")).toBe("2026-01-11");
    expect(nextLabel("2026-01-31", "ME")).toBe("2026-02-28");
    expect(nextLabel("2026-01-01", "QS")).toBe("2026-04-01");
    expect(nextLabel("2026-02-28", "D")).toBe("2026-03-01");
    expect(nextLabel("7", null)).toBe("8");
    expect(nextLabel("x", "MS")).toBe("");
  });

  it("says what a forecast was conditioned on", () => {
    const base = { dim: "t", fit_end: "1960-12-01", horizon_source: "rows", periods: 2, message: "" } as const;
    const yes = { ...base, conditioned: true, n_actuals: 3, actuals_through: "1961-03-01" } as ForecastConditioning;
    expect(conditioningHeadline(yes)).toBe("Conditioned on 3 actuals through 1961-03-01");
    const no = { ...base, conditioned: false, n_actuals: 0 } as ForecastConditioning;
    expect(conditioningHeadline(no)).toBe("From the end of the fitted history (1960-12-01)");
  });
});
