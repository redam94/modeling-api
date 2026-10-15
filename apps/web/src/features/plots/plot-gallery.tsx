import { BarChart3 } from "lucide-react";
import { useState } from "react";

import { usePlot, usePlots } from "@/api/queries";
import type { PlotListItem } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, QueryView } from "@/components/common";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { PlotFrame } from "./plot-frame";
import { useModelPlotData } from "./plot-data";

function PlotPanel({ modelId, version, item }: { modelId: string; version: number; item: PlotListItem }) {
  const spec = usePlot(modelId, version, item.id);
  const data = useModelPlotData(modelId, version, spec.data);
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{item.title}</CardTitle>
          {item.caption_plain ? <CardDescription>{item.caption_plain}</CardDescription> : null}
        </div>
        <div className="flex gap-1">
          <Badge variant="outline">{item.kind}</Badge>
          {item.recipe ? <Badge variant="secondary">{item.recipe}</Badge> : null}
        </div>
      </CardHeader>
      <CardContent>
        {spec.isError ? (
          <ErrorNotice error={spec.error} title="The plot could not be loaded" />
        ) : data.isError ? (
          <ErrorNotice error={data.error} title="The plot data could not be loaded" />
        ) : data.data ? (
          <PlotFrame
            kind={data.data.spec.kind}
            spec={data.data.body}
            datasets={data.data.datasets}
            title={item.title}
            exportName={item.id}
          />
        ) : (
          <LoadingRows rows={3} />
        )}
      </CardContent>
    </Card>
  );
}

/** Plots of a model version: a list to pick from and the selected plot rendered in the sandbox. */
export function PlotGallery({ modelId, version }: { modelId: string; version: number }) {
  const plots = usePlots(modelId, version);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <QueryView query={plots}>
      {(page) => {
        if (!page.items.length) {
          return (
            <EmptyState icon={<BarChart3 />} title="No plots for this version">
              Plots are produced by the fit run from the recipes listed in the spec&apos;s outputs.
            </EmptyState>
          );
        }
        const current = page.items.find((p) => p.id === selected) ?? page.items[0];
        return (
          <div className="grid gap-5 lg:grid-cols-[14rem_1fr]">
            <nav aria-label="Plots" className="grid content-start gap-0.5">
              {page.items.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setSelected(p.id)}
                  aria-current={current?.id === p.id ? "true" : undefined}
                  className={cn(
                    "rounded-md px-3 py-2 text-left text-[13px] hover:bg-accent",
                    current?.id === p.id && "bg-accent font-medium text-accent-foreground",
                  )}
                >
                  {p.title}
                  <span className="block font-mono text-[11px] text-muted-foreground">{p.id}</span>
                </button>
              ))}
            </nav>
            {current ? <PlotPanel key={current.id} modelId={modelId} version={version} item={current} /> : null}
          </div>
        );
      }}
    </QueryView>
  );
}
