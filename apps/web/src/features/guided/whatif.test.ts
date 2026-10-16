import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  baselineShare,
  channelHeadlines,
  evaluateScenario,
  interp,
  parseResponseGrid,
  quantile,
  WhatIfError,
  type SummaryStats,
  type WhatIfResult,
  type WhatIfScenario,
} from "./whatif";

/** Exported by packages/spec/tests/test_m3_whatif_golden.py (mapi_spec.whatif.evaluate_scenario). */
const GOLDEN = JSON.parse(
  readFileSync(resolve(__dirname, "../../../../../packages/spec/tests/snapshots/whatif_golden.json"), "utf-8"),
) as {
  levels: number[];
  grids: Record<string, unknown>;
  cases: { name: string; grid: string; scenario: WhatIfScenario; result?: WhatIfResult; error?: { code: string; pointer: string | null } }[];
};

function close(actual: number, expected: number) {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(1e-9 * Math.max(1, Math.abs(expected)));
}

function sameStats(actual: SummaryStats | null, expected: SummaryStats | null) {
  if (expected === null) {
    expect(actual).toBeNull();
    return;
  }
  expect(actual).not.toBeNull();
  close(actual!.mean, expected.mean);
  expect(actual!.levels).toEqual(expected.levels);
  expected.quantiles.forEach((q, i) => close(actual!.quantiles[i]!, q));
}

describe("what-if interpolation matches mapi_spec.whatif (golden cases)", () => {
  expect(GOLDEN.cases.length).toBeGreaterThanOrEqual(8);
  for (const c of GOLDEN.cases) {
    it(c.name, () => {
      const grid = parseResponseGrid(GOLDEN.grids[c.grid]);
      if (c.error) {
        let err: unknown = null;
        try {
          evaluateScenario(grid, c.scenario, GOLDEN.levels);
        } catch (e) {
          err = e;
        }
        expect(err).toBeInstanceOf(WhatIfError);
        expect((err as WhatIfError).code).toBe(c.error.code);
        expect((err as WhatIfError).pointer).toBe(c.error.pointer);
        return;
      }
      const res = evaluateScenario(grid, c.scenario, GOLDEN.levels);
      const want = c.result!;
      expect(res.draws).toBe(want.draws);
      close(res.total_spend, want.total_spend);
      close(res.spend_delta, want.spend_delta);
      sameStats(res.kpi, want.kpi);
      sameStats(res.kpi_delta, want.kpi_delta);
      sameStats(res.roi_delta, want.roi_delta);
      expect(Object.keys(res.channels).sort()).toEqual(Object.keys(want.channels).sort());
      for (const [ch, out] of Object.entries(want.channels)) {
        close(res.channels[ch]!.multiplier, out.multiplier);
        close(res.channels[ch]!.spend, out.spend);
        sameStats(res.channels[ch]!.contribution, out.contribution);
      }
    });
  }
});

describe("response grid parsing", () => {
  const good = GOLDEN.grids.hand as Record<string, unknown>;
  it.each([
    [{ multipliers: [0, 0.5, 2] }, "include 1"],
    [{ multipliers: [1, 0, 2] }, "ascending"],
    [{ draws: 3 }, "contribution"],
    [{ channels: ["tv", "tv"] }, "unique"],
    [{ format: "mapi.response-grid/v9" }, "unknown format"],
    [{ baseline: [1] }, "baseline"],
    [{ geos: null }, "per-geo"],
  ])("refuses %j", (over, message) => {
    expect(() => parseResponseGrid({ ...good, ...over })).toThrow(message);
  });
  it("ignores extra producer fields", () => {
    expect(parseResponseGrid({ ...good, produced_by: "mapi-runner" }).draws).toBe(2);
  });
});

describe("helpers", () => {
  it("interpolates and takes numpy's linear quantiles", () => {
    expect(interp([0, 1, 2], [0, 10, 15], 0.5)).toBe(5);
    expect(interp([0, 1, 2], [0, 10, 15], 2)).toBe(15);
    expect(quantile([1, 2, 3, 4], 0.5)).toBe(2.5);
    expect(quantile([1, 2, 3, 4], 0.05)).toBeCloseTo(1.15);
  });

  it("summarises ROI and shares per draw at the actual spend", () => {
    const grid = parseResponseGrid(GOLDEN.grids.hand);
    const [tv, search] = channelHeadlines(grid);
    expect(tv!.roi!.mean).toBeCloseTo((10 / 100 + 20 / 100) / 2);
    expect(search!.roi!.mean).toBeCloseTo((4 / 50 + 2 / 50) / 2);
    // draw 0: 10 / (100 + 14); draw 1: 20 / (200 + 22)
    expect(tv!.share!.mean).toBeCloseTo((10 / 114 + 20 / 222) / 2);
    expect(baselineShare(grid)!.mean).toBeCloseTo((100 / 114 + 200 / 222) / 2);
  });
});
