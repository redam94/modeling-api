import { describe, expect, it } from "vitest";

import { noDataRule, type ScoreMode } from "./request";

const ALL: ScoreMode[] = ["predict", "forecast", "counterfactual", "simulate", "filter", "evaluate"];
const NO_FORECAST: ScoreMode[] = ["predict", "simulate"];

describe("noDataRule (the server's 'the score request has no data' check)", () => {
  it("allows no new data for a forecast, which always sends a horizon", () => {
    expect(noDataRule("forecast", ALL, "")).toEqual({ ok: true });
  });

  it("refuses no new data for predict, simulate and what if without a scenario", () => {
    for (const mode of ["predict", "simulate", "counterfactual"] as const) {
      const rule = noDataRule(mode, ALL, "  ");
      expect(rule.ok).toBe(false);
      if (!rule.ok) expect(rule.reason).toMatch(/select Forecast\.$/);
    }
  });

  it("says that a model without forecast mode always needs values", () => {
    const rule = noDataRule("predict", NO_FORECAST, "");
    expect(rule.ok).toBe(false);
    if (!rule.ok) {
      expect(rule.reason).toContain("This model cannot forecast");
      expect(rule.reason).not.toContain("select Forecast");
    }
  });

  it("allows no new data when a scenario dataset gives the values", () => {
    expect(noDataRule("predict", NO_FORECAST, "dataset://default/plan@v1")).toEqual({ ok: true });
  });

  it("refuses no new data for the dataset-only modes, also with a scenario", () => {
    expect(noDataRule("evaluate", ALL, "dataset://default/plan@v1").ok).toBe(false);
    expect(noDataRule("filter", ALL, "").ok).toBe(false);
  });
});
