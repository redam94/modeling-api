import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { MINUS } from "@/components/ods/format";
import { parseResponseGrid, type WhatIfResult, type WhatIfScenario } from "@/features/guided/whatif";

import type { ActualsTemplate, ClientDashboard, KpiDef, KpiValue, Readout, ReadoutHeader, ReadoutSnapshot } from "../types";
import {
  accuracyLines,
  accuracyOf,
  actualsChecks,
  changeRows,
  channelRows,
  checksPass,
  csvCell,
  deltaWords,
  gridSlope,
  formatValue,
  historyRow,
  metaLine,
  pageTabs,
  scenarioModel,
  SCORECARD_COLUMNS,
  SCORECARD_REFLOW_PX,
  scorecardCells,
  sliderBounds,
  summaryCsv,
  tabLabel,
  tileViews,
  trendIndex,
  zoneOf,
  zonesIn,
} from "./readout-model";

const KPI: KpiDef = { variable: "revenue", label: "Revenue", unit: "currency", currency: "EUR", decimals: 0 };

function tile(p: Partial<KpiValue> & Pick<KpiValue, "key">): KpiValue {
  return { label: p.key, unit: "percent", value: null, delta: null, delta_basis: null, delta_dir: "flat", low: null, high: null, caption: "", ...p };
}

const SNAP: ReadoutSnapshot = {
  lead: "Media brought 210,000 revenue this week.",
  tiles: [
    tile({ key: "return_on_media", label: "Return on media", unit: "ratio", value: 1.843 }),
    tile({ key: "expected", label: "Expected revenue", unit: "currency", value: 412_300, delta: 0.032, delta_basis: "the week before", delta_dir: "up", caption: "Likely range" }),
    tile({ key: "vs_expected", label: "Compared with the model", unit: "percent", value: -4.12, delta: -0.0412, delta_basis: "the model's expectation", delta_dir: "alert" }),
    tile({ key: "in_range_spend", label: "Spend in a good range", unit: "percent", value: 72 }),
    tile({ key: "index", label: "Index", unit: "index", value: 96 }),
  ],
  channels: [
    { name: "search", label: "Paid search", spend: 18_000, contribution: 48_600, share: 0.25, roi: 2.7, roi_low: 2.1, roi_high: 3.3, mroi: 1.9, zone: "headroom" },
    { name: "tv", label: "TV", spend: 42_000, contribution: 79_800, share: 0.4, roi: 1.9, roi_low: 1.4, roi_high: 2.4, mroi: 0.8, zone: "over" },
    { name: "display", label: "Display", spend: 8_000, contribution: 6_800, share: 0.035, roi: 0.85, roi_low: 0.5, roi_high: 1.2, mroi: 0.4, zone: "no_payback" },
    { name: "price", label: "Average price", spend: null, contribution: null, share: null, roi: null, roi_low: null, roi_high: null, mroi: null, zone: null },
  ],
  series: [
    {
      name: "kpi",
      label: "Revenue",
      points: [
        { t: "2026-10-04", mean: 100, low: 90, high: 110, actual: 105 },
        { t: "2026-10-11", mean: 100, low: 90, high: 110, actual: 120 },
        { t: "2026-10-18", mean: 100, low: 95, high: 105, actual: 100 },
      ],
    },
  ],
};

const DASH: Pick<ClientDashboard, "title" | "kpi" | "channels" | "tiles" | "period_label" | "tabs"> = {
  title: "Weekly media readout",
  kpi: KPI,
  channels: [
    { name: "tv", label: "TV" },
    { name: "search", label: "Paid search" },
    { name: "display", label: "Display" },
  ],
  tiles: ["expected", "vs_expected", "return_on_media", "in_range_spend"],
  period_label: "week",
  tabs: ["history", "this_period", "scenarios"],
};

function header(p: Partial<ReadoutHeader> = {}): ReadoutHeader {
  return {
    seq: 3,
    kind: "refresh",
    trigger: "workflow",
    visibility: "published",
    status: "ready",
    title: "Week ending 18/10/2026",
    period: ["2026-10-12", "2026-10-18"],
    model_version_id: "mv_1",
    model_ref: "geo-mmm@3",
    created_at: "2026-10-20T09:40:00Z",
    ready_at: "2026-10-20T09:40:00Z",
    refit_status: null,
    ...p,
  };
}

describe("tiles", () => {
  it("keeps the dashboard's order, at most four, with values in the KPI's unit", () => {
    const tiles = tileViews(SNAP, KPI, DASH.tiles ?? []);
    expect(tiles.map((t) => t.key)).toEqual(["expected", "vs_expected", "return_on_media", "in_range_spend"]);
    expect(tiles[0]?.value).toBe("€412,300");
    expect(tiles[1]?.value).toBe(`${MINUS}4.1%`);
    expect(tiles[2]?.value).toBe("1.84");
    expect(tiles[3]?.value).toBe("72.0%");
  });

  it("says the change in plain words", () => {
    expect(deltaWords({ delta: 0.032, delta_basis: "the week before", delta_dir: "up" })).toBe("3.2% more than the week before");
    expect(deltaWords({ delta: -0.0412, delta_basis: "the model's expectation", delta_dir: "alert" })).toBe("4.1% less than the model's expectation, outside the likely range");
    expect(deltaWords({ delta: 0.002, delta_basis: "the plan", delta_dir: "flat" })).toBe("About the same as the plan");
    expect(deltaWords({ delta: 0.25, delta_basis: "the plan", delta_dir: "up" })).toBe("25% more than the plan");
    expect(deltaWords({ delta: null, delta_basis: "the plan", delta_dir: "flat" })).toBeNull();
  });

  it("formats each unit", () => {
    expect(formatValue(-1200.4, "currency", { currency: "EUR", decimals: 0 })).toBe(`${MINUS}€1,200`);
    expect(formatValue(4210, "count")).toBe("4,210");
    expect(formatValue(99.6, "index")).toBe("100");
    expect(formatValue(null, "ratio")).toBe("—");
  });
});

describe("zones", () => {
  it("follows zone_of (break-even 1, band 20%)", () => {
    expect(zoneOf(0.9, 2, 1)).toBe("no_payback");
    expect(zoneOf(1.5, 0.9, 1)).toBe("over");
    expect(zoneOf(1.5, 1.21, 1)).toBe("headroom");
    expect(zoneOf(1.5, 1.1, 1)).toBe("optimal");
    expect(zoneOf(1.5, null, 1)).toBeNull();
  });

  it("gives each channel its zone word and leaves out channels without numbers", () => {
    const rows = channelRows(SNAP, DASH);
    expect(rows.map((r) => [r.name, r.zoneWord])).toEqual([
      ["tv", "Past its best"],
      ["search", "More room"],
      ["display", "Not paying back"],
    ]);
    expect(zonesIn(rows)).toEqual(["headroom", "over", "no_payback"]);
    // The colour follows the channel's place in the dashboard, not its rank.
    expect(rows.map((r) => r.color)).toEqual([1, 2, 3]);
  });
});

describe("scorecard reflow data", () => {
  it("labels every value with its column, so the card layout reads like the table", () => {
    const [tv] = channelRows(SNAP, DASH);
    const cells = scorecardCells(tv!);
    expect(cells.map((c) => c.label)).toEqual(SCORECARD_COLUMNS.map((c) => c.label));
    expect(cells.map((c) => c.text)).toEqual(["TV", "Past its best", "1.90 (1.40 to 2.40)", "€79,800", "40%"]);
    expect(SCORECARD_REFLOW_PX).toBe(1099);
  });
});

describe("page words", () => {
  it("puts This period first and names the tabs", () => {
    expect(pageTabs(DASH)).toEqual(["this_period", "history", "scenarios"]);
    expect(tabLabel("this_period", "month")).toBe("This month");
    expect(tabLabel("update", "week")).toBe("Update numbers");
  });

  it("writes the meta line in DD/MM/YYYY and HH.MM", () => {
    expect(metaLine(header(), "week", "UTC")).toBe("Updated 20/10/2026 09.40 with results to the week ending 18/10/2026.");
    expect(metaLine(null, "week")).toMatch(/no results yet/);
  });

  it("tags uploads that wait for review and their refit request", () => {
    const row = historyRow(header({ kind: "actuals", visibility: "pending_review", refit_status: "open" }), "UTC");
    expect(row.kind).toBe("Your numbers");
    expect(row.tags.map((t) => t.label)).toEqual(["Waiting for review", "Refit requested"]);
    expect(historyRow(header({ status: "running" })).tags.map((t) => t.label)).toEqual(["Calculating"]);
  });
});

describe("accuracy and change", () => {
  it("computes accuracy from the series when the snapshot has none", () => {
    const acc = accuracyOf(SNAP)!;
    expect(acc.n).toBe(3);
    expect(acc.band_hit_rate).toBeCloseTo((2 / 3) * 100);
    const lines = accuracyLines(acc, KPI, "week");
    expect(lines[0]).toBe("The actual number was in the likely range in 2 of 3 weeks.");
    expect(lines.at(-1)).toMatch(/^The model expected €8 less than the actual number each week/);
  });

  it("diffs channel contributions against the readout before", () => {
    const prev: ReadoutSnapshot = { ...SNAP, channels: (SNAP.channels ?? []).map((c) => ({ ...c, contribution: c.contribution == null ? null : c.contribution - 1000 })) };
    const ch = changeRows(SNAP, prev)!;
    expect(ch.rows.map((r) => r.value)).toEqual([1000, 1000, 1000]);
    expect(ch.net.value).toBe(3000);
  });

  it("builds the trend index and marks a model change", () => {
    const pts = [
      { readout: header({ seq: 1, period: ["2026-10-05", "2026-10-11"] }), snapshot: { ...SNAP, tiles: [tile({ key: "vs_expected", value: 2 })] } },
      { readout: header({ seq: 2, model_version_id: "mv_2", created_at: "2026-10-20T09:00:00Z" }), snapshot: SNAP },
    ];
    const t = trendIndex(pts, "UTC");
    expect(t.points.map((p) => p.value)).toEqual([1.02, 1 - 0.0412]);
    expect(t.points[1]).toMatchObject({ t: "2026-10-18", low: 0.95, high: 1.05 });
    expect(t.versions).toEqual([{ at: 1, label: "Model updated on 20/10/2026" }]);
  });
});

describe("Update numbers checks", () => {
  const template: ActualsTemplate = {
    mode: "compared",
    kpi_column: "revenue",
    max_rows: 3,
    example_csv: "",
    input_table: {
      horizon_dim: "date",
      columns: [
        { name: "date", role: "coord", dims: [], freq: "W-SUN" },
        { name: "revenue", role: "target", dims: ["date"] },
        { name: "spend_tv", role: "input", dims: ["date"] },
      ],
    },
  };

  it("passes good rows and says what to fix otherwise", () => {
    expect(checksPass(actualsChecks(template, [["2026-10-11", 410000, 42000], ["2026-10-18", 400000, 41000]]))).toBe(true);
    const texts = (rows: (string | number | null)[][]) => actualsChecks(template, rows).filter((c) => !c.ok).map((c) => c.text);
    expect(texts([])).toEqual(["Add at least one row.", "Enter at least one number in the column revenue."]);
    expect(texts([["2026-10-11", "lots", 1]])).toEqual(["Enter at least one number in the column revenue.", "Use numbers only. Check row 1, revenue."]);
    expect(texts([["2026-10-11", -1, 1]])).toEqual(["Numbers must be 0 or more. Check row 1, revenue."]);
    expect(texts([["2026-10-11", 1, 1], ["2026-10-11", 2, 1]])).toEqual(["The date 2026-10-11 occurs two times."]);
    expect(texts([["11/10/2026", 1, 1]])).toEqual(["Row 1: write the date as yyyy-MM-dd."]);
    expect(texts([["2026-10-04", 1, 1], ["2026-10-11", 1, 1], ["2026-10-18", 1, 1], ["2026-10-25", 1, 1]])).toEqual(["Use 3 rows or fewer. You have 4."]);
  });

  it("names the rows as the editor numbers them, blank rows included", () => {
    const rows = [["2026-10-04", 1, 1], [null, null, null], [null, null, null], ["2026-10-18", 1, "12k"]];
    const texts = actualsChecks(template, rows).filter((c) => !c.ok).map((c) => c.text);
    expect(texts).toEqual(["Use numbers only. Check row 4, spend_tv."]);
    const noDate = actualsChecks(template, [[null, null, null], [null, 5, 1]]).filter((c) => !c.ok).map((c) => c.text);
    expect(noDate).toEqual(["Row 2 has no date."]);
  });
});

describe("summary CSV", () => {
  it("has the tiles and the scorecard, and neutralises formula cells", () => {
    const r = { ...header(), snapshot: { ...SNAP, lead: "=HYPERLINK(1)" }, problem: null } as Readout;
    const csv = summaryCsv(DASH, r, "UTC");
    expect(csv).toContain("Expected revenue,\"€412,300\",3.2% more than the week before,Likely range");
    expect(csv).toContain("'=HYPERLINK(1)");
    expect(csv).toContain("Channel,Where the spend sits,Return on media,Expected result,Share");
  });

  it("keeps plain signed numbers as they are (no literal apostrophe) and still guards formulas", () => {
    expect(csvCell("+3.2%")).toBe("+3.2%");
    expect(csvCell("-1,200")).toBe('"-1,200"');
    expect(csvCell("+0.4 pts")).toBe("+0.4 pts");
    expect(csvCell("+cmd|' /C calc'!A0")).toBe("'+cmd|' /C calc'!A0");
    expect(csvCell("-2+3")).toBe("'-2+3");
    expect(csvCell("@SUM(A1)")).toBe("'@SUM(A1)");
  });
});

describe("live zones and slider steps", () => {
  // one channel, three draws equal, multipliers 0.5/1/1.5, contribution flattening out
  const grid = parseResponseGrid({
    format: "mapi.response-grid/v1", kpi: "revenue", periods: 52, channels: ["tv"], multipliers: [0.5, 1, 1.5], draws: 1,
    contribution: [[[1400, 2000, 2200]]], spend: [[500, 1000, 1500]], current_spend: [1000], baseline: [2000],
  });
  const d = { kpi: KPI, channels: [{ name: "tv", label: "TV", min_mult: 0.5, max_mult: 1.5 }] };

  it("uses the slope of the grid segment that holds the multiplier, the last one at the end (as the server)", () => {
    expect(gridSlope(grid, 0, 0.5)).toBeCloseTo(1.2); // (2000-1400)/(1000-500)
    expect(gridSlope(grid, 0, 0.75)).toBeCloseTo(1.2); // inside the first segment
    expect(gridSlope(grid, 0, 1)).toBeCloseTo(0.4); // the segment to the right at a knot
    expect(gridSlope(grid, 0, 1.5)).toBeCloseTo(0.4); // the end: the last segment
    // at the top of the slider the zone word stays (it vanished with a +0.1 secant)
    const top = scenarioModel(grid, d, { tv: 1.5 }).channels[0]!;
    expect(top.zone).toBe("over");
    expect(top.zoneWord).not.toBe("No zone");
  });

  it("starts the steps so that the current plan (1) is one of them", () => {
    expect(sliderBounds({ min_mult: 0.75, max_mult: 1.5, step: 0.1 }, grid)).toEqual({ min: 0.8, max: 1.5, step: 0.1 });
    expect(sliderBounds({ min_mult: 0.5, max_mult: 1.5, step: 0.05 }, grid).min).toBe(0.5);
    const b = sliderBounds({ min_mult: 0.75, max_mult: 1.5, step: 0.1 }, grid);
    const k = (1 - b.min) / b.step;
    expect(Math.abs(k - Math.round(k))).toBeLessThan(1e-9);
  });
});

/** Exported by packages/spec/tests/test_m3_whatif_golden.py (mapi_spec.whatif.evaluate_scenario). */
const GOLDEN = JSON.parse(readFileSync(resolve(__dirname, "../../../../../../packages/spec/tests/snapshots/whatif_golden.json"), "utf-8")) as {
  grids: Record<string, unknown>;
  cases: { name: string; grid: string; scenario: WhatIfScenario; result?: WhatIfResult }[];
};

describe("scenario numbers equal the what-if golden cases", () => {
  const cases = GOLDEN.cases.filter((c) => c.result && c.scenario.geo == null);
  expect(cases.length).toBeGreaterThan(3);
  for (const c of cases) {
    it(c.name, () => {
      const grid = parseResponseGrid(GOLDEN.grids[c.grid]);
      const d = { kpi: KPI, channels: grid.channels.map((name) => ({ name, label: name, min_mult: 0, max_mult: 5 })) };
      const m = scenarioModel(grid, d, c.scenario.multipliers ?? {});
      const want = c.result!;
      const close = (a: number, b: number) => expect(Math.abs(a - b)).toBeLessThanOrEqual(1e-9 * Math.max(1, Math.abs(b)));
      close(m.kpiDelta, want.kpi_delta.mean);
      close(m.spendDelta, want.spend_delta);
      if (want.kpi) close(m.kpi!.mean, want.kpi.mean);
      for (const ch of m.channels) close(ch.contribution, want.channels[ch.name]!.contribution.mean);
      for (const ch of m.channels) {
        const b = sliderBounds(d.channels.find((x) => x.name === ch.name)!, grid);
        expect(ch.multiplier).toBeGreaterThanOrEqual(b.min);
        expect(ch.multiplier).toBeLessThanOrEqual(b.max);
      }
    });
  }
});
