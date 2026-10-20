import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { classifierRunPlots, dataRefPlot } from "@/mocks/score-shapes";

import { loadRunPlotData, outputLinks, refuseRunFiles, runPlotKey } from "./run-plots";

describe("run plot data", () => {
  it("decodes inline datasets on a page without signed links", async () => {
    const data = await loadRunPlotData(classifierRunPlots[1]!, refuseRunFiles);
    expect(data.kind).toBe("ready");
    expect(data.kind === "ready" && data.datasets.roc?.[1]).toEqual({ fpr: 0, tpr: 0.4 });
  });

  it("says where the plot is when its data is a file of the run", async () => {
    expect(await loadRunPlotData(dataRefPlot, refuseRunFiles)).toEqual({ kind: "run-file" });
  });

  it("reports a missing output of the score call as an error", async () => {
    await expect(loadRunPlotData(dataRefPlot, outputLinks([]))).rejects.toThrow("the run has no output named ui/plotdata/ppc_fan.arrow");
  });
});

describe("run plot cache key", () => {
  it("keeps the data of each run's plot apart, though the recipe id is the same", async () => {
    const a = classifierRunPlots[1]!;
    const b = { ...a, datasets: {} }; // run B's core.roc_pr: other data
    expect(a.id).toBe(b.id);
    const qc = new QueryClient();
    const load = (runId: string, plot: typeof a) =>
      qc.fetchQuery({ queryKey: runPlotKey("t/p", runId, plot.id, "none"), staleTime: Infinity, queryFn: () => loadRunPlotData(plot, refuseRunFiles) });
    const fromA = await load("run-a", a);
    const fromB = await load("run-b", b);
    expect(fromA.kind === "ready" && Object.keys(fromA.datasets)).toEqual(["roc"]);
    expect(fromB).toEqual({ kind: "ready", datasets: {} }); // not run A's ROC curve
  });

  it("holds the project scope, the run, the plot and the links", () => {
    const key = runPlotKey("t/p", "run-a", "core.roc_pr", "none");
    expect(key).not.toEqual(runPlotKey("t/q", "run-a", "core.roc_pr", "none"));
    expect(key).not.toEqual(runPlotKey("t/p", "run-b", "core.roc_pr", "none"));
    expect(key).not.toEqual(runPlotKey("t/p", "run-a", "core.calibration", "none"));
    expect(key).not.toEqual(runPlotKey("t/p", "run-a", "core.roc_pr", "https://x/1"));
  });
});
