/**
 * Builder › Preview, signed in only: the model version's plots behind the layout 2 picture
 * panels, drawn in the plot sandbox (PlotFrame). Clients and share links never load this: they
 * see the PNG picture the app makes at publish. Loaded lazily by `preview.tsx`.
 */
import { usePlot } from "@/api/queries";
import { ErrorNotice, LoadingRows } from "@/components/common";
import { PlotFrame } from "@/features/plots/plot-frame";
import { useModelPlotData } from "@/features/plots/plot-data";

import { Section } from "../readout/parts";

export interface RecipePreviewItem {
  panelId: string;
  title: string;
  plotId: string;
}

function OnePlot({ modelId, version, item }: { modelId: string; version: number; item: RecipePreviewItem }) {
  const spec = usePlot(modelId, version, item.plotId);
  const data = useModelPlotData(modelId, version, spec.data);
  return (
    <figure className="grid min-w-0 gap-2" data-recipe-preview={item.panelId}>
      <figcaption className="text-sm font-medium">{item.title}</figcaption>
      {spec.isError ? (
        <ErrorNotice error={spec.error} title="Cannot load the plot" />
      ) : data.isError ? (
        <ErrorNotice error={data.error} title="Cannot load the plot data" />
      ) : data.data ? (
        <PlotFrame kind={data.data.spec.kind} spec={data.data.body} datasets={data.data.datasets} title={item.title} exportName={item.plotId} toolbar={false} />
      ) : (
        <LoadingRows rows={3} />
      )}
    </figure>
  );
}

export default function RecipePreview({ modelId, version, items }: { modelId: string; version: number; items: readonly RecipePreviewItem[] }) {
  return (
    <Section title="Model plots" description="Only your team sees these live plots. Clients see a picture of each one, made when you publish.">
      <div className="grid gap-5 lg:grid-cols-2">
        {items.map((it) => (
          <OnePlot key={it.panelId} modelId={modelId} version={version} item={it} />
        ))}
      </div>
    </Section>
  );
}
