/**
 * An uploaded chart's example (D20): the `PlotSpec` the describe run rendered on toy data,
 * re-sanitized by the control plane, drawn in the plot sandbox origin like every other plot
 * (never inline). Lazy: the plot data helpers bring Arrow, which the plugins chunk must not.
 */
import type { JsonObject } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";
import { ImageOff } from "lucide-react";

import { Skeleton } from "@/components/ui/primitives";
import { PlotFrame } from "@/features/plots/plot-frame";
import { resolvePlotDatasets } from "@/features/plots/plot-data";

import { usePluginExample } from "./api";

export default function ExamplePlot({ pluginId, versionId, plotId, title }: { pluginId: string; versionId: string; plotId: string; title: string }) {
  const spec = usePluginExample(pluginId, versionId, plotId);
  const resolved = useQuery({
    queryKey: ["plugin-example-data", versionId, plotId],
    enabled: Boolean(spec.data),
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const s = spec.data!;
      const datasets = await resolvePlotDatasets(s, () => Promise.reject(new Error("an example must embed its datasets")), signal);
      return { kind: s.kind, body: s.spec as JsonObject, datasets };
    },
  });
  if (spec.isError || resolved.isError) {
    return (
      <div className="flex h-40 flex-col items-center justify-center gap-1.5 rounded-xl border border-dashed text-center text-xs text-muted-foreground">
        <ImageOff aria-hidden className="size-5" />
        This chart has no example we can show.
      </div>
    );
  }
  if (!resolved.data) return <Skeleton className="h-72 w-full rounded-xl" />;
  return <PlotFrame kind={resolved.data.kind} spec={resolved.data.body} datasets={resolved.data.datasets} title={`Example: ${title}`} height={300} toolbar={false} />;
}
