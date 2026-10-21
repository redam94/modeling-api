import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { KpiBlock } from "../types";
import { asClientV2, pageSections, panelTile, sectionOfTab, splitBlocks, summaryCsvV2, unitText } from "./panel-model";
import { accuracyDetailLines, accuracyMeasures, bandNote, batchLines, historyMetric, predictionLines } from "./panels/results-model";
import { classifierDashboard, CLASSIFIER_BLOCKS, fakeClient, readout } from "./panels/test-fixtures";

describe("sections", () => {
  it("overview first, no duplicates, the inputs' list when the server sends none", () => {
    expect(pageSections({ sections: ["history", "overview", "try", "try"] })).toEqual(["overview", "history", "try"]);
    expect(pageSections({ inputs: { scenario_rows: true, actuals: false, batch: true } })).toEqual(["overview", "try", "batch", "history"]);
    expect([sectionOfTab(undefined), sectionOfTab("this_period"), sectionOfTab("update"), sectionOfTab("scenarios"), sectionOfTab("history")]).toEqual([undefined, "overview", "accuracy", "try", "history"]);
  });
});

describe("tiles and numbers", () => {
  const d = classifierDashboard();
  it("a probability tile with its likely range and its change", () => {
    const t = panelTile(CLASSIFIER_BLOCKS[0] as KpiBlock, d);
    expect(t).toMatchObject({ label: "Chance of churn", value: "30.0%", delta: "10% more than the readout before", deltaDir: "up", approximate: false });
    expect(t.caption).toBe("Likely between 20.0% and 40.0%.");
  });

  it("an approximate tile says so", () => {
    const t = panelTile(CLASSIFIER_BLOCKS[1] as KpiBlock, d);
    expect(t.approximate).toBe(true);
    expect(t.caption).toBe("Approximate.");
    expect(t.value).toBe("12.00");
  });

  it("currency uses the subject's currency and decimals", () => {
    const money = classifierDashboard({ subject: { variable: "rev", label: "Revenue", unit: "currency", currency: "EUR", decimals: 0 } });
    expect(unitText(1234.4, "currency", money)).toBe("€1,234");
    expect(unitText(-5, "currency", money)).toBe("−€5");
  });

  it("splits KPI blocks from the rest, in order", () => {
    const { tiles, rest } = splitBlocks(CLASSIFIER_BLOCKS);
    expect(tiles.map((b) => b.panel_id)).toEqual(["chance", "expected"]);
    expect(rest[0]?.panel_id).toBe("probs");
  });

  it("the summary CSV holds the lead and the tiles, formula cells escaped", () => {
    const r = readout(3);
    r.snapshot!.lead = "=HYPERLINK(1)";
    const csv = summaryCsvV2(classifierDashboard(), r, "UTC");
    expect(csv.split("\r\n").slice(0, 6)).toEqual(["Churn risk", "Updated 20/10/2026 09.40.", "'=HYPERLINK(1)", "", "Measure,Value,Change,Note", "Chance of churn,30.0%,10% more than the readout before,Likely between 20.0% and 40.0%."]);
  });

  it("knows a layout 2 client", () => {
    const c = fakeClient(classifierDashboard());
    expect(asClientV2(c)).toBe(c);
    const { predict: _p, submitBatch: _b, ...v1 } = c;
    expect(asClientV2(v1 as never)).toBeNull();
  });
});

describe("results in words (the server's numbers)", () => {
  const d = classifierDashboard();
  it("predictions", () => {
    const lines = predictionLines({ band: "outcome", rows: [{ outputs: { churn: { centre: 0.42, low: 0.3, high: 0.55 } } }] }, 0, d);
    expect(lines).toEqual([{ output: "churn", centre: "42.0%", range: "Likely between 30.0% and 55.0%", label: null, probability: null }]);
    expect(bandNote("outcome")).toMatch(/^The likely range shows where one new result will probably be\./);
    expect(bandNote("none")).toBeNull();
  });

  it("batch: the spread of predictions, never an interval", () => {
    const lines = batchLines({ n_rows: 3, n_scored: 2, share_positive: null, stats: { churn: { p50: 0.5, p10: 0.1, p90: 0.9 } } }, d);
    expect(lines).toEqual(["The model scored 2 of 3 cases.", "churn: the middle prediction is 50.0%; most are between 10.0% and 90.0%. This is the spread of the predictions, not a likely range."]);
  });

  it("accuracy of each kind", () => {
    const forecast = classifierDashboard({ period_label: "month", subject: { variable: "y", label: "Sales", unit: "count", decimals: 0 } });
    expect(accuracyDetailLines({ kind: "error", n: 3, mae: 12, mape: 0.05, bias: -2, band_hit_rate: 2 / 3 }, forecast)).toEqual([
      "We compared 3 months with the predictions.",
      "The typical error is 12.",
      "On average the prediction was 5.0% from the actual number.",
      "The predictions were 2 too low on average.",
      "The actual number was in the likely range 66.7% of the time.",
    ]);
    expect(accuracyDetailLines({ kind: "classification", n: 4, accuracy: 0.75, rate_pred: 0.5, rate_obs: 0.25, positive: "yes", threshold: 0.5 }, d)).toEqual([
      "We compared 4 cases with the predictions.",
      "The model was right 75.0% of the time.",
      'It predicted 50.0% "yes"; the actual share was 25.0%.',
      'A case counts as "yes" when its probability is 50.0% or more.',
    ]);
    expect(accuracyDetailLines({ kind: "effect", n_periods: 4, total: 120, low: 80, high: 160, relative: 0.1, note: "The estimate updates after the analyst refits." }, forecast)).toEqual([
      "The change added about 120 (likely 80 to 160) over 4 months.",
      "That is 10.0% more than without it.",
      "The estimate updates after the analyst refits.",
    ]);
    expect(accuracyMeasures({ kind: "classification", accuracy: 0.75, brier: 0.1234, auc: null }, d)).toEqual([
      { label: "Accuracy", value: "75.0%" },
      { label: "Brier score", value: "0.123" },
    ]);
    expect(historyMetric([{ seq: 1, created_at: "x", values: { auc: 0.7, accuracy: null } }])).toBe("auc");
  });
});

/** Every source file of the readout page and its charts. */
function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) return f === "__snapshots__" ? [] : sources(p);
    return /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f) ? [p] : [];
  });
}

describe("what client and share pages may hold (plan D4, §3.7)", () => {
  const root = resolve(__dirname, "../../..");
  const files = [...sources(resolve(__dirname)), resolve(root, "components/readout-charts.tsx"), resolve(root, "components/readout-charts-extra.tsx")];

  it("no HTML from strings anywhere in readout/** and the readout charts", () => {
    expect(files.length).toBeGreaterThan(20);
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      expect([f, /innerHTML|outerHTML|dangerouslySetInnerHTML|insertAdjacentHTML|document\.write/.test(text)]).toEqual([f, false]);
    }
  });

  it("no Vega, Plotly, plot sandbox or iframe", () => {
    for (const f of files) {
      const text = readFileSync(f, "utf8");
      expect([f, /from "(?:vega|vega-lite|vega-embed|plotly\.js|react-plotly\.js)|@mapi\/plot-sandbox|<iframe|features\/plots|PlotFrame/.test(text)]).toEqual([f, false]);
    }
  });
});
