/**
 * The heavy panel blocks (plan D18), loaded lazily through `readout-extra`: bars, scatter,
 * calibration, confusion, field map, table, picture and series with several lines.
 *
 * A picture is the model version's own plot, rendered to PNG at publish: it is fetched through
 * the viewer's dashboard client (the share session header for a share link, never a cookie),
 * shown from a Blob URL (`img-src blob:`), and the URL is revoked when the panel goes.
 */
import { useEffect, useState } from "react";

import { InlineMessage } from "@/components/ods";
import { Bars, Calibration, ConfusionGrid, FieldHeatmap, MultiLine, Scatter, SmallTable } from "@/components/readout-charts-extra";

import { dashboardErrorMessage, useDashboardAsset } from "../../client";
import type { ClientDashboard, DashboardClient, DashboardClientV2, PanelBlock, PictureBlock } from "../../types";
import { asClientV2, plainNumber, unitText } from "../panel-model";
import { Working } from "../parts";
import { periodOf } from "../readout-model";

function PictureImage({ client, block }: { client: DashboardClientV2; block: PictureBlock }) {
  const q = useDashboardAsset(client, block.asset_id);
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!q.data) return;
    const u = URL.createObjectURL(q.data);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [q.data]);
  if (q.isError) return <InlineMessage tone="info" size="s" role="none">{`The picture is not available. ${dashboardErrorMessage(q.error)}`}</InlineMessage>;
  if (!url) return <Working>Loading the picture…</Working>;
  return <img src={url} alt={block.alt} width={block.width} height={block.height} className="h-auto max-w-full rounded-sm border" data-picture={block.asset_id} />;
}

export function PictureView({ client, block }: { client: DashboardClient; block: PictureBlock }) {
  const v2 = asClientV2(client);
  if (!v2) return <p className="text-sm text-muted-foreground">{block.alt}</p>;
  return <PictureImage client={v2} block={block} />;
}

/** One heavy block's chart (the card around it is the caller's). */
export function HeavyBlock({ client, dashboard, block }: { client: DashboardClient; dashboard: ClientDashboard; block: PanelBlock }) {
  const unit = dashboard.subject?.unit ?? dashboard.kpi.unit;
  const fmt = (v: number) => unitText(v, unit === "probability" ? "number" : unit, dashboard);
  switch (block.kind) {
    case "bars": {
      const probability = block.intent === "probability_bars";
      return (
        <Bars
          label={block.title}
          rows={(block.rows ?? []).map((r) => ({ label: r.label, value: r.value ?? null, low: r.low ?? null, high: r.high ?? null, tag: r.tag ?? null }))}
          format={probability ? (v) => unitText(v, "probability", dashboard) : fmt}
        />
      );
    }
    case "scatter":
      return (
        <Scatter
          label={block.title}
          points={block.points ?? []}
          xLabel={block.x_label ?? (block.intent === "residuals" ? "Prediction" : "Actual")}
          yLabel={block.y_label ?? (block.intent === "residuals" ? "Difference" : "Prediction")}
          diagonal={block.intent !== "residuals"}
          format={plainNumber}
        />
      );
    case "calibration":
      return <Calibration label={block.title} bins={block.bins ?? []} />;
    case "confusion":
      return <ConfusionGrid label={block.title} labels={block.labels ?? []} counts={block.counts ?? []} />;
    case "field":
      return (
        <FieldHeatmap
          label={block.title}
          width={block.width}
          height={block.height}
          frames={block.frames}
          vmin={block.vmin}
          vmax={block.vmax}
          log={block.log ?? false}
          colormap={block.colormap ?? "viridis"}
          units={block.units ?? null}
          xLabel={block.x_label ?? null}
          yLabel={block.y_label ?? null}
        />
      );
    case "table":
      return <SmallTable label={block.title} columns={block.columns ?? []} rows={block.rows ?? []} format={plainNumber} />;
    case "picture":
      return <PictureView client={client} block={block} />;
    case "series":
      return <MultiLine label={block.title} series={(block.series ?? []).map((s) => ({ label: s.label || s.name, points: s.points ?? [] }))} format={fmt} period={periodOf(dashboard)} />;
    default:
      return null;
  }
}
