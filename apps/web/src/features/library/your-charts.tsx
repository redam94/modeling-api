/**
 * "Your charts" on run and model pages (v0.18 D5): the team's chart templates drawn on this
 * model version's real data. `POST /v1/library/{id}:use` swaps the template's spec onto the
 * version's stored plot of the source chart type (its datasets are kept, `data_ref` resolved
 * through the version's signed links) and sanitizes it; the sandbox draws it.
 *
 * Signed-in pages only: an authored chart is never embeddable (`provenance.author`), so share
 * links, client portals and dashboard pictures never show it. The templates whose chart type
 * this version has are drawn; the others can be added with a chip.
 */
import type { JsonObject as PlotJson } from "@mapi/plot-sandbox/protocol";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";

import { api, unwrap } from "@/api/client";
import { modelArtifactLink, useModelVersionById, usePlots, useProjectScope } from "@/api/queries";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { CatalogGlyph } from "@/components/illustrations";
import { PlotFrame } from "@/features/plots/plot-frame";

import { requestItemUse } from "./actions";
import { useLibraryItems } from "./api";
import { lk } from "./keys";
import { Chip, LibraryLink, ToggleChip } from "./ui";
import { splitUseRef, type LibraryItem } from "./types";

/** At most this many charts are drawn without asking (each is a compile and a data load). */
const AUTO_MAX = 4;

function YourChart({ item, modelId, version }: { item: LibraryItem; modelId: string; version: number }) {
  const s = useProjectScope();
  const chart = useQuery({
    queryKey: ["p", s.key, "library", "your-chart", item.id, item.latest_revision, modelId, version],
    staleTime: 10 * 60_000,
    retry: false,
    queryFn: async ({ signal }) => {
      const used = await requestItemUse(s, item.id, { revision: item.latest_revision, model_id: modelId, model_version: version, slots: {}, exposed: {} });
      const plot = used.plot;
      if (!plot) return { plot: null, datasets: null, issues: used.issues ?? [] };
      // Arrow decoding loads with the first chart, not with the page.
      const { resolvePlotDatasets } = await import("@/features/plots/plot-data");
      const datasets = await resolvePlotDatasets(plot, (path) => modelArtifactLink(modelId, version, path), signal);
      return { plot, datasets, issues: used.issues ?? [] };
    },
  });
  return (
    <figure className="grid min-w-0 gap-2">
      <figcaption>
        <span className="text-sm font-semibold">{chart.data?.plot?.title ?? item.title}</span>
        {chart.data?.plot?.caption_plain ? <span className="block text-xs text-muted-foreground">{chart.data.plot.caption_plain}</span> : null}
      </figcaption>
      {chart.isPending ? (
        <LoadingRows rows={3} />
      ) : chart.isError ? (
        <ErrorNotice error={chart.error} title="This chart could not be drawn on this version" />
      ) : chart.data.plot && chart.data.datasets ? (
        <PlotFrame kind={chart.data.plot.kind} spec={chart.data.plot.spec as PlotJson} datasets={chart.data.datasets} title={chart.data.plot.title} exportName={chart.data.plot.id} />
      ) : (
        <p className="rounded-md border border-dashed px-3 py-2 text-sm text-muted-foreground">{chart.data.issues[0]?.message ?? "This version has no data for this chart."}</p>
      )}
    </figure>
  );
}

/** The source chart type of each template (its latest revision; revisions are immutable and cached). */
function useTemplateSources(items: readonly LibraryItem[]): Map<string, string> | null {
  const s = useProjectScope();
  const docs = useQueries({
    queries: items.map((i) => ({
      queryKey: lk.revision(s.key, i.id, i.latest_revision),
      staleTime: Infinity,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        unwrap(api.GET("/v1/library/{item_id}/revisions/{revision}", { params: { path: { item_id: i.id, revision: i.latest_revision } }, headers: s.headers, signal })),
    })),
  });
  if (docs.some((d) => d.isPending)) return null;
  const out = new Map<string, string>();
  docs.forEach((d, k) => {
    const doc = d.data?.document as { source_recipe?: unknown } | undefined;
    const item = items[k];
    if (item && typeof doc?.source_recipe === "string") out.set(item.id, splitUseRef(doc.source_recipe)[0]);
  });
  return out;
}

/** The team's chart templates for one model version. Renders nothing when the library is not readable. */
export function YourCharts({ modelId, version }: { modelId: string; version: number }) {
  const templates = useLibraryItems({ kind: "chart_template" });
  const plots = usePlots(modelId, version);
  const [extra, setExtra] = useState<string[]>([]);
  const items = useMemo(() => templates.data ?? [], [templates.data]);
  const sources = useTemplateSources(items);
  const recipes = useMemo(() => new Set((plots.data?.items ?? []).map((p) => splitUseRef(p.recipe ?? p.id)[0])), [plots.data]);
  if (templates.isError) return null;
  // Templates whose chart type this version has are drawn (up to AUTO_MAX); the rest wait for a click.
  const fits = sources ? items.filter((i) => recipes.has(sources.get(i.id) ?? "")) : [];
  const auto = fits.slice(0, AUTO_MAX).map((i) => i.id);
  const shown = items.filter((i) => auto.includes(i.id) || extra.includes(i.id));
  const hidden = items.filter((i) => !shown.includes(i));
  const loading = templates.isPending || (items.length > 0 && (sources === null || plots.isPending));
  return (
    <Card aria-label="Your charts">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CatalogGlyph kind="plot" seed="your-charts" size="sm" />
          Your charts
        </CardTitle>
        <Chip>From your team</Chip>
      </CardHeader>
      <CardContent className="grid gap-5">
        {loading ? (
          <LoadingRows rows={2} />
        ) : items.length ? (
          <>
            {shown.length ? (
              <div className="grid gap-6 lg:grid-cols-2">
                {shown.map((i) => (
                  <YourChart key={i.id} item={i} modelId={modelId} version={version} />
                ))}
              </div>
            ) : (
              <p className="text-muted-foreground">None of your team's charts starts from a chart this version has. Pick one below to try it anyway.</p>
            )}
            {hidden.length ? (
              <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="More of your charts">
                <span className="text-xs text-muted-foreground">{shown.length ? "Also draw:" : "Draw:"}</span>
                {hidden.map((i) => (
                  <ToggleChip key={i.id} pressed={false} onClick={() => setExtra([...extra, i.id])}>
                    <Plus className="size-3" /> {i.title}
                  </ToggleChip>
                ))}
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-muted-foreground">
            Your team has no chart templates yet. Restyle any chart once and it shows here on every run.{" "}
            <LibraryLink search={{ tab: "charts", new: "chart" }} className="link font-semibold">
              Make a chart template
            </LibraryLink>
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** "Your charts" for the model version a fit run made (the run detail page). */
export function YourChartsForVersion({ mvId }: { mvId: string }) {
  const mv = useModelVersionById(mvId);
  if (!mv.data) return null;
  return <YourCharts modelId={mv.data.model_id} version={mv.data.version} />;
}
