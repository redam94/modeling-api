/**
 * Small pieces the readout tabs share: a section card, a loading line, a file save, and the
 * readout body (lead, tiles, the KPI chart) that This period and History both show.
 *
 * The readout page and its tabs import only `components/{ui,ods,readout-charts}`, the dashboard
 * client and pure modules, so the share app can load them without the signed-in app.
 */
import { Loader2 } from "lucide-react";
import type * as React from "react";

import { InlineMessage, StatGrid, StatTile, TagWord } from "@/components/ods";
import { BandLine } from "@/components/readout-charts";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { ClientDashboard, Readout } from "../types";
import { kpiNumber, kpiSeries, periodOf, refitTag, STATUS_WORD, tileViews, type Tag } from "./readout-model";

export function Section({
  title,
  description,
  action,
  className,
  children,
  ...props
}: Omit<React.ComponentProps<"section">, "title"> & { title: string; description?: React.ReactNode; action?: React.ReactNode }) {
  return (
    <Card className={cn("min-w-0", className)} {...props}>
      <CardHeader>
        <div className="grid min-w-0 gap-1">
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription className="text-sm text-muted-foreground">{description}</CardDescription> : null}
        </div>
        {action ? <div className="flex-none">{action}</div> : null}
      </CardHeader>
      <CardContent className="grid min-w-0 gap-4">{children}</CardContent>
    </Card>
  );
}

export function Working({ children }: { children: React.ReactNode }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 text-sm text-muted-foreground">
      <Loader2 aria-hidden className="size-4 animate-spin" />
      <span>{children}</span>
    </div>
  );
}

export function Tags({ tags, className }: { tags: readonly Tag[]; className?: string }) {
  if (!tags.length) return null;
  return (
    <span className={cn("flex flex-wrap gap-1.5", className)}>
      {tags.map((t) => (
        <TagWord key={t.label} tone={t.tone}>
          {t.label}
        </TagWord>
      ))}
    </span>
  );
}

/** Save `text` as a file (a Blob URL; nothing is sent anywhere). */
export function saveText(name: string, text: string, type = "text/csv;charset=utf-8"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.rel = "noopener";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

/** The KPI tiles of a readout (four a row, two on tablets, one on phones). */
export function ReadoutTiles({ dashboard, readout, label = "Key numbers" }: { dashboard: ClientDashboard; readout: Readout; label?: string }) {
  const tiles = tileViews(readout.snapshot, dashboard.kpi, dashboard.tiles ?? []);
  if (!tiles.length) return null;
  return (
    <StatGrid role="list" aria-label={label}>
      {tiles.map((t) => (
        <div role="listitem" key={t.key} className="min-w-0">
          <StatTile label={t.label} value={t.value} delta={t.delta} deltaDir={t.deltaDir} caption={t.caption} className="h-full" />
        </div>
      ))}
    </StatGrid>
  );
}

/** The KPI over time: the expected line, its likely range and the actual numbers. */
export function KpiChart({ dashboard, readout, title }: { dashboard: ClientDashboard; readout: Readout; title?: string }) {
  const series = kpiSeries(readout.snapshot);
  const points = (series?.points ?? []).map((p) => ({ t: p.t, mean: p.mean ?? null, low: p.low ?? null, high: p.high ?? null, actual: p.actual ?? null }));
  if (!points.length) return null;
  const period = periodOf(dashboard);
  const hasActual = points.some((p) => p.actual !== null);
  return (
    <Section
      title={title ?? `${dashboard.kpi.label}: expected and actual`}
      description={`The line is what the model expects each ${period}. The shaded band is the likely range. ${hasActual ? "The dots are the actual numbers." : ""}`.trim()}
    >
      <BandLine label={`${dashboard.kpi.label} each ${period}`} points={points} period={period} format={kpiNumber(dashboard.kpi)} />
    </Section>
  );
}

/** A readout that is not ready: what is happening, in words. */
export function ReadoutState({ readout }: { readout: Pick<Readout, "status" | "problem" | "refit_status"> }) {
  const refit = refitTag(readout.refit_status);
  return (
    <div className="grid gap-2">
      {readout.status === "failed" ? (
        <InlineMessage tone="error">
          This readout did not finish.{readout.problem?.title ? ` ${readout.problem.title}.` : ""} Your analyst can see what happened.
        </InlineMessage>
      ) : readout.status !== "ready" ? (
        <Working>{STATUS_WORD[readout.status]}. The readout appears here when it is ready.</Working>
      ) : null}
      {refit ? <InlineMessage tone={refit.tone === "negative" ? "error" : refit.tone === "positive" ? "success" : "info"}>{refit.text}</InlineMessage> : null}
    </div>
  );
}
