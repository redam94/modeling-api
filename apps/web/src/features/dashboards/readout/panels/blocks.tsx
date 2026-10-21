/**
 * The panel blocks of a layout 2 readout (v0.17, plan D18): the light kinds are drawn here
 * (KPI tiles, one-line series, notes, unavailable panels); the heavy kinds (bars, scatter,
 * calibration, confusion, field, table, picture and several-line series) load lazily from
 * `readout-extra` with their charts, so a dashboard without them does not download them.
 *
 * Every block keeps its own card: the panel title, the analyst's caption and an "Approximate"
 * tag when a total was added up from single values (it has no likely range).
 */
import { lazy } from "react";
import type * as React from "react";

import { InlineMessage, StatGrid, StatTile } from "@/components/ods";
import { BandLine } from "@/components/readout-charts";

import type { ClientDashboard, DashboardClient, KpiBlock, NoteBlock, PanelBlock, ReadoutSnapshot, SeriesBlock, UnavailableBlock } from "../../types";
import { LazyPart } from "../load-boundary";
import { APPROXIMATE_TEXT, blockTags, isPlannerDuplicate, panelTile, splitBlocks, unitText } from "../panel-model";
import { Section, Tags, Working } from "../parts";
import { periodOf } from "../readout-model";

const HeavyBlock = lazy(() => import("./readout-extra").then((m) => ({ default: m.HeavyBlock })));

/** The KPI blocks as tiles (four a row, two on tablets, one on phones). */
export function PanelTiles({ dashboard, blocks, label = "Key numbers" }: { dashboard: ClientDashboard; blocks: readonly KpiBlock[]; label?: string }) {
  if (!blocks.length) return null;
  return (
    <StatGrid role="list" aria-label={label}>
      {blocks.map((b) => {
        const t = panelTile(b, dashboard);
        return (
          <div role="listitem" key={t.id} className="min-w-0" data-panel={t.id}>
            <StatTile label={t.label} value={t.value} delta={t.delta} deltaDir={t.deltaDir} caption={t.caption} className="h-full" />
          </div>
        );
      })}
    </StatGrid>
  );
}

/** The card of one panel: its title, the analyst's caption and the approximate tag. */
export function BlockCard({ block, children }: { block: PanelBlock; children: React.ReactNode }) {
  const tags = blockTags(block);
  return (
    <Section title={block.title} description={block.caption ?? undefined} action={tags.length ? <Tags tags={tags} /> : undefined} data-panel={block.panel_id} data-block={block.kind}>
      {children}
      {block.approximate ? <p className="text-xs text-hint">{APPROXIMATE_TEXT}</p> : null}
    </Section>
  );
}

const SERIES_LABELS: Partial<Record<SeriesBlock["intent"], { expected: string; actual: string }>> = {
  effect: { expected: "Without the change", actual: "Actual" },
};

/** The accuracy history's line, by its metric (the series name): its words and how to show it. */
const ACCURACY_LINE: Record<string, { label: string; format: "percent" | "share" | "number" | "subject" }> = {
  mae: { label: "Typical error", format: "subject" },
  rmse: { label: "RMSE", format: "subject" },
  bias: { label: "Bias", format: "subject" },
  mape: { label: "Average error", format: "percent" },
  wape: { label: "WAPE", format: "percent" },
  smape: { label: "SMAPE", format: "percent" },
  band_hit_rate: { label: "Inside the likely range", format: "percent" },
  r2: { label: "R²", format: "number" },
  accuracy: { label: "Right predictions", format: "share" },
  brier: { label: "Brier score", format: "number" },
  log_score: { label: "Log score", format: "number" },
  auc: { label: "AUC", format: "number" },
  effect_total: { label: "Effect", format: "subject" },
  effect_relative: { label: "Relative effect", format: "percent" },
};

function SeriesView({ dashboard, block }: { dashboard: ClientDashboard; block: SeriesBlock }) {
  const s = block.series?.[0];
  const points = (s?.points ?? []).map((p) => ({ t: p.t, mean: p.mean ?? null, low: p.low ?? null, high: p.high ?? null, actual: p.actual ?? null }));
  if (!points.length) return <p className="text-sm text-muted-foreground">This panel has no values yet.</p>;
  const split = block.split_at ? points.findIndex((p) => p.t === block.split_at) : -1;
  const line = block.intent === "accuracy_history" ? ACCURACY_LINE[s?.name ?? ""] : undefined;
  const words = line ? { expected: line.label, actual: "Actual" } : (SERIES_LABELS[block.intent] ?? { expected: block.intent === "accuracy_history" ? s?.label || "Accuracy" : "Prediction", actual: "Actual" });
  const unit = dashboard.subject?.unit ?? dashboard.kpi.unit;
  const subjectFormat = (v: number) => unitText(v, unit === "probability" ? "number" : unit, dashboard);
  const format =
    line?.format === "percent"
      ? (v: number) => `${v.toFixed(1)}%`
      : line?.format === "share"
        ? (v: number) => `${(v * 100).toFixed(1)}%`
        : line?.format === "number"
          ? (v: number) => v.toFixed(3)
          : subjectFormat;
  return (
    <BandLine
      label={`${s?.label || block.title} each ${periodOf(dashboard)}`}
      points={points}
      period={periodOf(dashboard)}
      format={format}
      nowAt={split >= 0 ? split : undefined}
      expectedLabel={words.expected}
      actualLabel={words.actual}
    />
  );
}

function NoteView({ block }: { block: NoteBlock }) {
  return <p className="max-w-[72ch] text-sm whitespace-pre-line text-foreground">{block.text}</p>;
}

function UnavailableView({ block }: { block: UnavailableBlock }) {
  return (
    <InlineMessage tone="info" size="s" role="none">
      {block.reason}
    </InlineMessage>
  );
}

/** One non-KPI block in its card. */
export function PanelBlockView({ client, dashboard, block }: { client: DashboardClient; dashboard: ClientDashboard; block: PanelBlock }) {
  let body: React.ReactNode;
  if (block.kind === "series" && (block.series?.length ?? 0) <= 1) body = <SeriesView dashboard={dashboard} block={block} />;
  else if (block.kind === "note") body = <NoteView block={block} />;
  else if (block.kind === "unavailable") body = <UnavailableView block={block} />;
  else if (block.kind === "kpi") body = <PanelTiles dashboard={dashboard} blocks={[block]} label={block.title} />;
  else
    body = (
      <LazyPart fallback={<Working>Loading the panel…</Working>} label={block.title}>
        <HeavyBlock client={client} dashboard={dashboard} block={block} />
      </LazyPart>
    );
  return <BlockCard block={block}>{body}</BlockCard>;
}

/**
 * The panels of a snapshot in the analyst's order: the KPI tiles first, then a card per other
 * block. `planner`: leave out the subject line the media planner already draws.
 */
export function PanelBlocks({
  client,
  dashboard,
  snapshot,
  label,
  planner = false,
}: {
  client: DashboardClient;
  dashboard: ClientDashboard;
  snapshot: Pick<ReadoutSnapshot, "panels">;
  label?: string;
  planner?: boolean;
}) {
  const { tiles, rest } = splitBlocks(snapshot.panels ?? []);
  const shown = planner ? rest.filter((b) => !isPlannerDuplicate(b)) : rest;
  return (
    <>
      <PanelTiles dashboard={dashboard} blocks={tiles} {...(label ? { label } : {})} />
      {shown.map((b) => (
        <PanelBlockView key={b.panel_id} client={client} dashboard={dashboard} block={b} />
      ))}
    </>
  );
}
