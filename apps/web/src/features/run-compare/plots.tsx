/**
 * The Plots section of the compare page (v0.14): the plots that two or more of the runs
 * rendered (`GET /v1/runs/{id}/plots`), side by side. One row per plot, one column per run; up
 * to four columns share the width, more scroll sideways. A plot whose data is a file of the run
 * says so (this page has no signed links to run files).
 */
import { useQueries } from "@tanstack/react-query";

import { runPlotsQuery, useProjectScope } from "@/api/queries";
import type { PlotSpec, Run } from "@/api/types";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { refuseRunFiles, RunPlot } from "@/features/runs/run-plots";

import { RunSwatch } from "./run-chips";

export interface SharedPlot {
  id: string;
  title: string;
  /** The run's plot of this id, per run (`null`: the run has none). */
  byRun: (PlotSpec | null)[];
}

/** The plot ids that `min` or more runs have, in the order of the first run that has them. */
export function sharedPlots(perRun: readonly (readonly PlotSpec[] | undefined)[], min = 2): SharedPlot[] {
  const order: string[] = [];
  const titles = new Map<string, string>();
  for (const list of perRun) {
    for (const p of list ?? []) {
      if (!titles.has(p.id)) {
        titles.set(p.id, p.title);
        order.push(p.id);
      }
    }
  }
  return order
    .map((id) => ({ id, title: titles.get(id) ?? id, byRun: perRun.map((list) => list?.find((p) => p.id === id) ?? null) }))
    .filter((p) => p.byRun.filter(Boolean).length >= min);
}

export function PlotsSection({ runs, labels }: { runs: readonly Run[]; labels: readonly string[] }) {
  const s = useProjectScope();
  const results = useQueries({ queries: runs.map((r) => runPlotsQuery(s, r.id)) });
  const pending = results.some((r) => r.isPending);
  const failed = results.find((r) => r.isError);
  const lists = results.map((r) => r.data?.items);
  const plots = sharedPlots(lists);

  // up to four columns share the width (two runs get half each: wide plots such as ROC + PR fit)
  const cols = Math.min(Math.max(runs.length, 1), 4);
  const autoCols = `minmax(16rem, calc((100% - ${cols - 1}rem) / ${cols}))`;

  if (pending) return <LoadingRows rows={4} />;
  return (
    <section className="grid gap-5" aria-label="Plots">
      {failed ? <ErrorNotice error={failed.error} title="The app could not load the plots of a run" /> : null}
      {plots.length ? (
        plots.map((p) => (
          <div key={p.id} className="grid min-w-0 gap-2" data-compare-plot={p.id}>
            <h3 className="text-[13px] font-semibold">{p.title}</h3>
            <div className="grid grid-flow-col gap-4 overflow-x-auto pb-2 [contain:inline-size]" style={{ gridAutoColumns: autoCols }}>
              {p.byRun.map((plot, i) => (
                <div key={runs[i]!.id} className="grid min-w-0 content-start gap-2 rounded-lg border bg-card p-3">
                  <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                    <RunSwatch index={i} />
                    <span className="truncate">{labels[i]}</span>
                  </span>
                  {plot ? <RunPlot runId={runs[i]!.id} plot={plot} resolveLink={refuseRunFiles} linksKey="none" /> : <p className="text-xs text-muted-foreground">The run has no plot of this kind.</p>}
                </div>
              ))}
            </div>
          </div>
        ))
      ) : (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-[13px] text-muted-foreground">
          No plot is in two or more of the runs. Fits show their plots on the page of the model version.
        </p>
      )}
    </section>
  );
}
