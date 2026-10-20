import { describe, expect, it } from "vitest";

import type { CompareParam, CompareSeries, CompareSummary, PlotSpec, Run } from "@/api/types";
import { compareBars, compareCurves } from "@/features/plots/builtin-specs";

import { bestRun, defaultSplitLabel, itemAtStep, lowerIsBetter, metricKeys, paramDiffers, paramRows, runColor, runLabels, summarySeries, summaryValue, unionSteps } from "./compare-model";
import { hasCurve } from "./curves";
import { sharedPlots } from "./plots";

const summary = (run_id: string, key: string, labels: Record<string, string>, patch: Partial<CompareSummary> = {}): CompareSummary => ({
  run_id,
  key,
  labels,
  attempt: 1,
  count: 10,
  last: 1,
  last_step: 9,
  min: 0.5,
  max: 2,
  first_ts: "2026-10-15T09:30:00Z",
  last_ts: "2026-10-15T09:31:00Z",
  ...patch,
});

const series = (run_id: string, labels: Record<string, string>, steps: (number | null)[], values: number[]): CompareSeries => ({
  run_id,
  labels,
  attempt: 1,
  count: values.length,
  downsampled: false,
  steps,
  values,
});

describe("compare model", () => {
  it("labels runs by name, then short id, and keeps labels unique", () => {
    const runs = [
      { id: "run_01aaaaaaaaaaaaaaaaaaaaaa11", name: "Fit" },
      { id: "run_01bbbbbbbbbbbbbbbbbbbbbb22", name: "Fit" },
      { id: "run_01cccccccccccccccccccccc33", name: null },
      { id: "run_01dddddddddddddddddddddd44", name: "  " },
    ] as Pick<Run, "id" | "name">[];
    const labels = runLabels(runs);
    expect(labels[0]).toMatch(/^Fit \(.+\)$/);
    expect(labels[0]).not.toBe(labels[1]);
    expect(labels[2]).not.toBe("");
    expect(new Set(labels).size).toBe(4);
  });

  it("gives run colours from the chart palette, in run order", () => {
    expect(runColor(0)).toBe("var(--series-1)");
    expect(runColor(7)).toBe("var(--series-8)");
    expect(runColor(8)).toBe("var(--series-1)");
  });

  it("marks the smallest value best for loss-like keys and the largest for others", () => {
    expect(lowerIsBetter("val_loss")).toBe(true);
    expect(lowerIsBetter("rmse")).toBe(true);
    expect(lowerIsBetter("crps")).toBe(true);
    expect(lowerIsBetter("accuracy")).toBe(false);
    expect(bestRun("rmse", [3, null, 2, 5])).toEqual({ index: 2, direction: "min" });
    expect(bestRun("elpd_loo", [-1241.6, -1228.1])).toEqual({ index: 1, direction: "max" });
    // one value, or every value the same: no best run
    expect(bestRun("rmse", [3, null])).toBeNull();
    expect(bestRun("rmse", [3, 3])).toBeNull();
    expect(bestRun("rmse", [NaN, 3, 4])).toEqual({ index: 1, direction: "min" });
  });

  it("finds the stepped metric keys, most runs first", () => {
    const keys = metricKeys([
      summary("a", "loss", { split: "train" }),
      summary("a", "loss", { split: "val" }),
      summary("b", "loss", { split: "val" }),
      summary("a", "rmse", {}, { last_step: null, count: 1 }),
      summary("b", "rmse", {}, { last_step: null, count: 1 }),
      summary("b", "aux", {}),
    ]);
    expect(keys.map((k) => [k.key, k.runs, k.stepped])).toEqual([
      ["loss", 2, true],
      ["rmse", 2, false],
      ["aux", 1, true],
    ]);
    expect(keys[0]!.labelKeys).toEqual(["split"]);
    expect(defaultSplitLabel(["fold", "split"])).toBe("split");
    expect(defaultSplitLabel(["chain"])).toBeNull();
  });

  it("reads a summary value per run and series (newest attempt)", () => {
    const s = [summary("a", "loss", { split: "val" }, { last: 0.3, min: 0.25 }), summary("a", "loss", { split: "val" }, { attempt: 2, last: 0.28, min: 0.2 }), summary("b", "loss", {}, { last: 9 })];
    const refs = summarySeries(s);
    expect(refs.map((r) => `${r.key}|${r.id}`)).toEqual(["loss|", "loss|split=val"]);
    expect(summaryValue(s, "a", { key: "loss", id: "split=val" }, "last")).toBe(0.28);
    expect(summaryValue(s, "a", { key: "loss", id: "split=val" }, "min")).toBe(0.2);
    expect(summaryValue(s, "b", { key: "loss", id: "split=val" }, "last")).toBeNull();
  });

  it("filters the params diff", () => {
    const params: CompareParam[] = [
      { key: "fit.draws", values: { a: "1000", b: "1000" }, differs: false },
      { key: "settings.adstock.l_max", values: { a: "8", b: "4" }, differs: true },
      { key: "settings.note", values: { a: null, b: "<b>short</b>" }, differs: true },
    ];
    expect(paramRows(params, { onlyDiffs: true }).map((p) => p.key)).toEqual(["settings.adstock.l_max", "settings.note"]);
    expect(paramRows(params, { onlyDiffs: false }).length).toBe(3);
    expect(paramRows(params, { onlyDiffs: false, text: "SHORT" }).map((p) => p.key)).toEqual(["settings.note"]);
    expect(paramDiffers(["1", "1"])).toBe(false);
    expect(paramDiffers(["1", null])).toBe(true);
    expect(paramDiffers(["1", undefined, null])).toBe(true);
  });

  it("picks the item at the greatest step at or below the slider", () => {
    const a = [{ step: 0 }, { step: 2 }, { step: 4 }];
    const b = [{ step: 1 }, { step: null }];
    expect(unionSteps([a, b])).toEqual([0, 1, 2, 4]);
    expect(itemAtStep(a, 3)).toEqual({ step: 2 });
    expect(itemAtStep(a, 4)).toEqual({ step: 4 });
    expect(itemAtStep([{ step: 2 }], 1)).toBeNull();
    // an item without a step counts as step 0
    expect(itemAtStep(b, 0)).toEqual({ step: null });
  });

  it("knows which metrics draw as curves", () => {
    expect(hasCurve({ key: "loss", series: [series("a", {}, [0, 1, 2], [3, 2, 1])] })).toBe(true);
    expect(hasCurve({ key: "rmse", series: [series("a", {}, [null], [3]), series("b", {}, [5], [2])] })).toBe(false);
  });

  it("keeps the plots that two or more runs have", () => {
    const plot = (id: string) => ({ id, title: id.toUpperCase() }) as PlotSpec;
    const shared = sharedPlots([[plot("core.roc_pr"), plot("core.fan")], [plot("core.fan")], undefined]);
    expect(shared.map((p) => p.id)).toEqual(["core.fan"]);
    expect(shared[0]!.byRun.map((p) => p?.id ?? null)).toEqual(["core.fan", "core.fan", null]);
  });
});

describe("compareCurves", () => {
  const runs = [
    { runLabel: "Adstock 8 weeks", history: [series("a", { split: "train" }, [0, 1, 2], [3, 2, 1]), series("a", { split: "val" }, [0, 1, 2], [3.2, 2.3, 1.4])] },
    { runLabel: "<b>B</b>", history: [series("b", { split: "val" }, [0, 1], [4, 3]), series("b", { split: "test" }, [7], [2.5])] },
  ];

  it("colours by run in run order and dashes by the split label", () => {
    const p = compareCurves(runs, "loss", { splitBy: "split" });
    const layers = p.spec.layer as { data: { name: string }; mark: { type: string }; encoding: Record<string, Record<string, unknown>> }[];
    expect(layers.map((l) => l.mark.type)).toEqual(["line", "point"]);
    expect(layers[0]!.encoding.color).toMatchObject({ field: "run", type: "nominal", sort: { field: "order", op: "min" } });
    expect(layers[0]!.encoding.strokeDash).toMatchObject({ field: "split", title: "split" });
    expect(layers[0]!.encoding.detail).toMatchObject({ field: "series" });
    expect(p.datasets.lines).toHaveLength(8);
    expect(p.datasets.lines![0]).toMatchObject({ step: 0, value: 3, run: "Adstock 8 weeks", order: 0, split: "train" });
    expect(p.datasets.points).toEqual([expect.objectContaining({ step: 7, run: "<b>B</b>", order: 1, split: "test" })]);
    // untrusted run names stay in the data, never in the spec
    expect(JSON.stringify(p.spec)).not.toContain("<b>");
  });

  it("draws solid lines without splitBy and leaves out points without a step", () => {
    const p = compareCurves([{ runLabel: "a", history: [series("a", {}, [null, 1, 2], [9, 2, 1])] }], "loss", { logY: true });
    const enc = (p.spec.layer as { encoding: Record<string, { scale?: unknown }> }[])[0]!.encoding;
    expect(enc.strokeDash).toBeUndefined();
    expect(enc.y!.scale).toEqual({ type: "log" });
    expect(p.datasets.lines).toHaveLength(2);
  });
});

describe("compareBars", () => {
  it("draws a bar per run with a value, in run order", () => {
    const p = compareBars(
      [
        { run: "a", value: 412.5 },
        { run: "b", value: null },
        { run: "c", value: 398.2 },
      ],
      "rmse (last)",
    );
    expect(p.datasets.bars).toEqual([
      { run: "a", value: 412.5, order: 0 },
      { run: "c", value: 398.2, order: 2 },
    ]);
    const enc = p.spec.encoding as Record<string, Record<string, unknown>>;
    expect(enc.y).toMatchObject({ field: "run", sort: { field: "order", op: "min" } });
    expect(enc.x).toMatchObject({ field: "value", title: "rmse (last)" });
    expect(enc.color).toMatchObject({ field: "run", legend: null });
  });
});
