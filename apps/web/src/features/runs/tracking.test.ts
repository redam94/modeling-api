import { describe, expect, it } from "vitest";

import { atLeastLevel, filterByLevel } from "@/api/logs";
import { applyRunPatch } from "@/api/queries";
import { parseTrackingFrame, type MetricHistory, type MetricSeries } from "@/api/tracking";
import type { Run } from "@/api/types";
import { metricCurves } from "@/features/plots/builtin-specs";
import { mergeTracking, runStreamReducer, trackingKeys, type RunStreamState } from "@/hooks/use-run-events";

import { cleanLogLines } from "./logs-viewer";
import { defaultColorLabel, filterSeries, groupSeries, groupStats, labelText, labelValues } from "./metrics-model";
import { formatStepSize } from "./progress-panel";
import { draftErrors, draftFromRun, runPatchDiff, tagRowError } from "./run-patch";

const series = (key: string, labels: Record<string, string>, patch: Partial<MetricSeries> = {}): MetricSeries => ({
  key,
  labels,
  attempt: 1,
  count: 10,
  last: 1,
  last_step: 9,
  min: 0,
  max: 2,
  first_ts: "2026-10-15T09:30:00Z",
  last_ts: "2026-10-15T09:31:00Z",
  ...patch,
});

const history = (s: MetricHistory["series"]): MetricHistory => ({ run_id: "run_1", key: "step_size", series: s });
const hs = (labels: Record<string, string>, steps: (number | null)[], values: number[]) => ({
  labels,
  attempt: 1,
  count: values.length,
  downsampled: false,
  steps,
  values,
  ts: values.map((_, i) => `2026-10-15T09:30:0${i}Z`),
});

describe("metricCurves", () => {
  it("draws a line per series over step, coloured by a label", () => {
    const p = metricCurves(history([hs({ chain: "0" }, [1, 2], [0.5, 0.4]), hs({ chain: "1" }, [1, 2], [0.6, 0.3])]), { colorBy: "chain" });
    const layers = p.spec.layer as { data: { name: string }; mark: { type: string }; encoding: Record<string, { field: string; type?: string; scale?: unknown }> }[];
    expect(layers).toHaveLength(1);
    expect(layers[0]!.mark.type).toBe("line");
    expect(layers[0]!.encoding.x).toMatchObject({ field: "step", type: "quantitative" });
    expect(layers[0]!.encoding.color).toMatchObject({ field: "group", type: "nominal", title: "chain" });
    expect(layers[0]!.encoding.detail).toMatchObject({ field: "series" });
    expect(p.datasets.lines).toHaveLength(4);
    expect(p.datasets.lines![0]).toMatchObject({ step: 1, value: 0.5, group: "0", labels: "chain=0" });
    expect(p.datasets.points).toEqual([]);
  });

  it("uses a log scale (symlog with values at or below zero) and one colour without colorBy", () => {
    const pos = metricCurves(history([hs({}, [1, 2], [1, 10])]), { logY: true });
    const enc = (pos.spec.layer as { encoding: Record<string, { scale?: { type?: string } }> }[])[0]!.encoding;
    expect(enc.y!.scale).toEqual({ type: "log" });
    expect(enc.color).toBeUndefined();
    const zero = metricCurves(history([hs({}, [1, 2], [0, 10])]), { logY: true });
    expect((zero.spec.layer as { encoding: { y: { scale: unknown } } }[])[0]!.encoding.y.scale).toEqual({ type: "symlog" });
  });

  it("falls back to time when no point has a step, and draws a one-point series as a point", () => {
    const p = metricCurves(history([hs({ fold: "0" }, [null], [3]), hs({ fold: "1" }, [null, null], [2, 1])]));
    const layers = p.spec.layer as { mark: { type: string }; encoding: { x: { field: string; type: string } } }[];
    expect(layers.map((l) => l.mark.type)).toEqual(["line", "point"]);
    expect(layers[0]!.encoding.x).toMatchObject({ field: "ts", type: "temporal" });
    expect(p.datasets.points).toHaveLength(1);
  });
});

describe("metrics model", () => {
  const all = [
    series("step_size", { chain: "1" }),
    series("step_size", { chain: "0" }, { last: 0.12, last_ts: "2026-10-15T09:32:00Z", min: -1 }),
    series("rmse", {}, { last_step: null }),
    series("step_size", { chain: "10" }),
    series("loss", { fit: "evaluation", chain: "0" }),
  ];

  it("groups series by key, sorts them and marks scalar-only keys", () => {
    const groups = groupSeries(all);
    expect(groups.map((g) => g.key)).toEqual(["loss", "rmse", "step_size"]);
    expect(groups[2]!.series.map((s) => s.labels?.chain)).toEqual(["0", "1", "10"]);
    expect(groups[1]!.scalar).toBe(true);
    expect(groups[2]!.scalar).toBe(false);
    expect(groups[0]!.labelKeys).toEqual(["chain", "fit"]);
  });

  it("colours by chain when present, else by the first label, else not at all", () => {
    expect(defaultColorLabel(["chain", "fit"])).toBe("chain");
    expect(defaultColorLabel(["fold", "stage"])).toBe("fold");
    expect(defaultColorLabel([])).toBeNull();
  });

  it("filters hidden label values and keeps series without the label", () => {
    const kept = filterSeries(all, { chain: ["1", "10"] });
    expect(kept.map((s) => `${s.key}:${labelText(s.labels)}`)).toEqual(["step_size:chain=0", "rmse:", "loss:chain=0, fit=evaluation"]);
    expect(labelValues(all)).toEqual({ chain: ["0", "1", "10"], fit: ["evaluation"] });
  });

  it("summarises a group: latest of the newest series, min and max over all", () => {
    expect(groupStats(all.filter((s) => s.key === "step_size"))).toEqual({ latest: 0.12, min: -1, max: 2, series: 3 });
    expect(groupStats([])).toBeNull();
  });
});

describe("run stream tracking frames", () => {
  const initial: RunStreamState = { status: "open", detail: null, events: [], progress: null, trackingTick: 0, changedKeys: [] };
  const frame = (keys: string[], extra: { params?: boolean; tags?: boolean; media?: boolean } = {}) => ({
    run_id: "run_1",
    series: keys.map((k) => series(k, {})),
    params: extra.params ?? false,
    tags: extra.tags ?? false,
    media: extra.media ?? false,
  });

  it("counts frames and exposes the changed keys, never storing them as events", () => {
    let s = runStreamReducer(initial, { type: "tracking", frame: frame(["step_size", "divergences", "step_size"]) });
    expect(s.trackingTick).toBe(1);
    expect(s.changedKeys).toEqual(["divergences", "step_size"]);
    expect(s.events).toEqual([]);
    s = runStreamReducer(s, { type: "tracking", frame: frame([], { params: true }) });
    expect(s.trackingTick).toBe(2);
    expect(s.changedKeys).toEqual([]);
  });

  it("merges the frames of one refetch window", () => {
    let c = mergeTracking(null, frame(["loss"]));
    c = mergeTracking(c, frame(["rmse"], { tags: true }));
    expect([...c.keys].sort()).toEqual(["loss", "rmse"]);
    expect(c).toMatchObject({ params: false, tags: true, media: false });
    // v0.14: a media frame in the window refetches the media of the run.
    c = mergeTracking(c, frame([], { media: true }));
    c = mergeTracking(c, frame(["loss"]));
    expect(c.media).toBe(true);
    expect(trackingKeys(frame(["b", "a", "b"]))).toEqual(["a", "b"]);
  });

  it("validates the frame JSON", () => {
    expect(parseTrackingFrame({ run_id: "run_1", series: [series("loss", {}), { key: 3 }], params: true })).toMatchObject({ params: true, tags: false, media: false });
    expect(parseTrackingFrame({ run_id: "run_1", series: [], media: true })).toMatchObject({ media: true });
    expect(parseTrackingFrame({ run_id: "run_1", series: [], media: "yes" })).toMatchObject({ media: false });
    expect(parseTrackingFrame({ run_id: "run_1", series: [series("loss", {}), { key: 3 }] })!.series).toHaveLength(1);
    expect(parseTrackingFrame({ series: [] })).toBeNull();
    expect(parseTrackingFrame([])).toBeNull();
  });
});

describe("log levels", () => {
  const line = (seq: number, stream: "stdout" | "user", level: "debug" | "info" | "warning" | "error" | null, fields?: Record<string, string | number | null>) => ({
    run_id: "run_1",
    attempt: 1,
    seq,
    ts: "2026-10-15T09:30:00Z",
    stream,
    line: `line ${seq}`,
    level,
    fields: fields ?? null,
  });

  it("filters by the minimum level, lines without a level counting as info", () => {
    const lines = cleanLogLines([line(1, "stdout", null), line(2, "user", "debug"), line(3, "user", "warning"), line(4, "user", "error"), line(5, "user", "info")]);
    expect(filterByLevel(lines, null).map((l) => l.text)).toHaveLength(5);
    expect(filterByLevel(lines, "info").map((l) => l.key)).toEqual(["1:1", "1:3", "1:4", "1:5"]);
    expect(filterByLevel(lines, "warning").map((l) => l.key)).toEqual(["1:3", "1:4"]);
    expect(filterByLevel(lines, "error").map((l) => l.key)).toEqual(["1:4"]);
    expect(atLeastLevel(undefined, "info")).toBe(true);
    expect(atLeastLevel("debug", "info")).toBe(false);
  });

  it("keeps the fields of user lines as sorted text", () => {
    const [l] = cleanLogLines([line(1, "user", "warning", { step_size: 0.012, chain: 1, note: "\u001b[31m<b>x</b>", none: null })]);
    expect(l!.level).toBe("warning");
    expect(l!.fields).toEqual([
      ["chain", "1"],
      ["none", "null"],
      ["note", "<b>x</b>"],
      ["step_size", "0.012"],
    ]);
  });
});

describe("run patch", () => {
  const run = {
    id: "run_1",
    name: "baseline",
    note: "first try",
    tags: { team: "growth", stage: "dev", "mapi.workflow": "wf_1" },
  } as unknown as Run;

  it("sends nothing when nothing changed", () => {
    expect(runPatchDiff(draftFromRun(run), draftFromRun(run))).toBeNull();
  });

  it("sends only the changed fields: a new name, a changed and a removed tag", () => {
    const d = draftFromRun(run);
    expect(d.tags.map((t) => t.key)).toEqual(["stage", "team"]); // platform tags stay out of the form
    d.name = "  tuned priors ";
    d.tags = [{ id: "a", key: "stage", value: "prod" }, { id: "b", key: "owner", value: "ana" }];
    expect(runPatchDiff(draftFromRun(run), d)).toEqual({ name: "tuned priors", tags: { stage: "prod", owner: "ana", team: null } });
  });

  it("clears the name and the note with null", () => {
    const d = { ...draftFromRun(run), name: " ", note: "" };
    expect(runPatchDiff(draftFromRun(run), d)).toEqual({ name: null, note: null });
  });

  it("compares with the run as it was when the edit started, not with a newer run", () => {
    const start = draftFromRun(run);
    const draft = { ...start, note: "second try" };
    // while the user types, the runner sets a tag and another user renames the run
    const newer = { ...run, name: "renamed", tags: { ...run.tags, "grid.best_point": "3" } } as unknown as Run;
    expect(draftFromRun(newer).tags.map((t) => t.key)).toContain("grid.best_point");
    expect(runPatchDiff(start, draft)).toEqual({ note: "second try" });
  });

  it("ignores a blank tag row, and a row with a value but no key is an error", () => {
    const start = draftFromRun(run);
    const blank = { id: "n:1", key: "", value: "" };
    const draft = { ...start, note: "edited", tags: [...start.tags, blank] };
    expect(tagRowError(blank, draft.tags)).toBe("");
    expect(draftErrors(draft)).toEqual([]);
    expect(runPatchDiff(start, draft)).toEqual({ note: "edited" });
    const keyless = { id: "n:2", key: " ", value: "x" };
    expect(tagRowError(keyless, [keyless])).toMatch(/key/);
    expect(draftErrors({ ...draft, tags: [keyless] })).toEqual(["Correct the tags."]);
  });

  it("checks tag keys", () => {
    const rows = [{ id: "a", key: "mapi.x", value: "" }, { id: "b", key: "ok", value: "" }, { id: "c", key: "ok", value: "" }, { id: "d", key: "bad key", value: "" }];
    expect(tagRowError(rows[0]!, rows)).toMatch(/platform/);
    expect(tagRowError(rows[1]!, rows)).toMatch(/one time/);
    expect(tagRowError(rows[3]!, rows)).toMatch(/letters/);
  });

  it("applies a patch to the cached run (optimistic update)", () => {
    const next = applyRunPatch(run, { note: null, tags: { team: null, owner: "ana" } });
    expect(next).toMatchObject({ name: "baseline", note: null, tags: { stage: "dev", owner: "ana", "mapi.workflow": "wf_1" } });
    expect(next.tags).not.toHaveProperty("team");
  });
});

describe("ChainLanes step size", () => {
  it("shows two significant digits", () => {
    expect(formatStepSize(0.1234)).toBe("0.12");
    expect(formatStepSize(0.01234)).toBe("0.012");
    expect(formatStepSize(1.5)).toBe("1.5");
    expect(formatStepSize(0.00001234)).toBe("1.2e-5");
  });
});
