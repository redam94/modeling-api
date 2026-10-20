/**
 * The plots a run rendered (`GET /v1/runs/{id}/plots`, sanitized by the server) and the one
 * component that draws such a plot in the sandbox, shared by the run detail page and the Score
 * page. Plot data comes inline (`inline_arrow`) or as a file of the run (`data_ref`); a page
 * resolves files through its own signed links, or has none and says where the plot is.
 */
import type { JsonObject, PlotDatasets } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { qk, useModelVersionById, usePlots, useProjectScope, useRunPlots } from "@/api/queries";
import type { ArtifactLink, PlotSpec, Run } from "@/api/types";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { PlotGallery } from "@/features/plots/plot-gallery";
import { resolvePlotDatasets, type LinkResolver } from "@/features/plots/plot-data";
import { PlotFrame } from "@/features/plots/plot-frame";
import { isTerminal } from "@/lib/run-state";

/** What the run detail page says for a plot whose data is a file of the run. */
export const RUN_FILE_NOTE = "The data of this plot is in a file of the run. Open the model version to see the plot.";

/** A dataset at a path, on a page that has no signed links to the run's files. */
export class RunFileError extends Error {
  override name = "RunFileError";
}

/** A resolver for pages without signed links: every `data_ref` dataset is refused. */
export const refuseRunFiles: LinkResolver = (path) => Promise.reject(new RunFileError(`the data of this plot is in the run file ${path}`));

/** A resolver over the signed links of a run's outputs (the score call hands them out). */
export function outputLinks(outputs: readonly ArtifactLink[]): LinkResolver {
  return (path) => {
    const link = outputs.find((o) => o.name === path);
    return link ? Promise.resolve(link) : Promise.reject(new Error(`the run has no output named ${path}`));
  };
}

export type RunPlotData = { kind: "ready"; datasets: PlotDatasets } | { kind: "run-file" };

/** The datasets of a run plot, or `run-file` when its data is a file the page cannot open. */
export async function loadRunPlotData(plot: Pick<PlotSpec, "datasets">, resolveLink: LinkResolver, signal?: AbortSignal): Promise<RunPlotData> {
  try {
    return { kind: "ready", datasets: await resolvePlotDatasets(plot, resolveLink, signal) };
  } catch (err) {
    if (err instanceof RunFileError) return { kind: "run-file" };
    throw err;
  }
}

/**
 * The cache key of a run plot's data. A plot id is a recipe id (`core.roc_pr`), the same in
 * every run, so the key holds the project scope and the run: without them, a run would draw
 * its spec with the data of another run's plot of the same recipe.
 */
export function runPlotKey(scopeKey: string, runId: string, plotId: string, linksKey: string) {
  return [...qk.runPlots(scopeKey, runId), plotId, linksKey] as const;
}

/**
 * One plot of run `runId` with its title and caption. `linksKey` names the resolver's links in
 * the query cache (the output URLs on the Score page, "none" where files are refused).
 */
export function RunPlot({ runId, plot, resolveLink, linksKey }: { runId: string; plot: PlotSpec; resolveLink: LinkResolver; linksKey: string }) {
  const s = useProjectScope();
  const data = useQuery({
    queryKey: runPlotKey(s.key, runId, plot.id, linksKey),
    staleTime: Infinity,
    queryFn: ({ signal }) => loadRunPlotData(plot, resolveLink, signal),
  });
  return (
    <figure className="grid gap-2">
      <figcaption>
        <span className="text-[13px] font-medium">{plot.title}</span>
        {plot.caption_plain ? <span className="block text-xs text-muted-foreground">{plot.caption_plain}</span> : null}
      </figcaption>
      {data.isError ? (
        <ErrorNotice error={data.error} title="Cannot load the plot data" />
      ) : data.data?.kind === "run-file" ? (
        <p className="rounded-md border border-dashed px-3 py-2 text-[13px] text-muted-foreground">{RUN_FILE_NOTE}</p>
      ) : data.data ? (
        <PlotFrame kind={plot.kind} spec={plot.spec as JsonObject} datasets={data.data.datasets} title={plot.title} exportName={plot.id} />
      ) : (
        <LoadingRows rows={3} />
      )}
    </figure>
  );
}

function PlotsCard({ children }: { children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Plots</CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

/** A fit's plots are the plots of the version it made: the gallery opens their data files. */
function VersionPlots({ mvId }: { mvId: string }) {
  const mv = useModelVersionById(mvId);
  const plots = usePlots(mv.data?.model_id ?? "", mv.data?.version ?? 0);
  if (mv.isError) return <PlotsCard><ErrorNotice error={mv.error} title="Cannot find the model version of the run" /></PlotsCard>;
  if (mv.isPending || (mv.data && plots.isPending)) return <PlotsCard><LoadingRows rows={3} /></PlotsCard>;
  if (!mv.data || (plots.data && !plots.data.items.length)) return null;
  return (
    <PlotsCard>
      <PlotGallery modelId={mv.data.model_id} version={mv.data.version} />
    </PlotsCard>
  );
}

/** Every other run: the plots it rendered, with inline data only (this page has no signed links). */
function RenderedPlots({ runId }: { runId: string }) {
  const plots = useRunPlots(runId);
  if (plots.isError) return <PlotsCard><ErrorNotice error={plots.error} title="Cannot load the plots of the run" /></PlotsCard>;
  if (plots.isPending) return <PlotsCard><LoadingRows rows={3} /></PlotsCard>;
  if (!plots.data.items.length) return null;
  return (
    <PlotsCard>
      <section aria-label="Run plots" className="grid gap-5">
        {plots.data.items.map((p) => (
          <RunPlot key={p.id} runId={runId} plot={p} resolveLink={refuseRunFiles} linksKey="none" />
        ))}
      </section>
    </PlotsCard>
  );
}

/**
 * The "Plots" card of the run detail page, for finished runs (a running run has no plots yet).
 * Hidden when the run has no plots.
 */
export function RunPlotsCard({ run }: { run: Pick<Run, "id" | "kind" | "state" | "output_model_version_id"> }) {
  if (!isTerminal(run.state)) return null;
  if (run.kind === "fit" && run.output_model_version_id) return <VersionPlots mvId={run.output_model_version_id} />;
  return <RenderedPlots runId={run.id} />;
}
