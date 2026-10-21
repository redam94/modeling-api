/**
 * This period (MEI planner view): the lead sentence, the inline messages, four KPI tiles, the
 * KPI chart with its likely range, the channel scorecard with the zones key, then "What changed
 * since the last readout" beside the accuracy card (7-5 on wide screens).
 */
import type * as React from "react";

import { InlineMessage, ZoneLegend, ZoneTag } from "@/components/ods";
import { ZONE_FILL, ZONE_TINT } from "@/components/ods/tag-word";
import { DivergingBars, RangeBar } from "@/components/readout-charts";
import { cn } from "@/lib/utils";

import type { ClientDashboard, Readout, ReadoutHeader, Zone } from "../../types";
import { KpiChart, ReadoutState, ReadoutTiles, Section } from "../parts";
import {
  accuracyLines,
  accuracyOf,
  breakevenOf,
  changeRows,
  channelRows,
  dateOf,
  kpiWord,
  periodOf,
  SCORECARD_COLUMNS,
  scorecardCells,
  zonesIn,
  zoneWord,
  type ChannelRow,
} from "../readout-model";
import { TechnicalToggle } from "./technical-toggle";

const ZONE_ORDER: readonly Zone[] = ["headroom", "optimal", "over", "no_payback"];

/** Four cells, the channel's zone filled and the others tinted; the tag word says the same. */
function ZoneStrip({ label, zone }: { label: string; zone: Zone | null }) {
  return (
    <span role="img" aria-label={`${label}: ${zoneWord(zone)}`} data-zone={zone ?? "none"} className="grid h-2 w-full max-w-40 grid-cols-4 gap-0.5">
      {ZONE_ORDER.map((z) => (
        <span key={z} className={cn("rounded-xs", z === zone ? ZONE_FILL[z] : ZONE_TINT[z])} />
      ))}
    </span>
  );
}

// Wide: one row a channel in five columns. Narrow (≤ 1099 px): one card a channel, each value
// with its column label. One DOM for both, so a screen reader hears the same table.
const GRID = "min-[1100px]:grid min-[1100px]:grid-cols-[minmax(120px,1fr)_minmax(170px,1.3fr)_minmax(170px,1.3fr)_minmax(110px,0.9fr)_72px] min-[1100px]:items-center min-[1100px]:gap-x-6";

function Cell({ label, className, children }: { label: string; className?: string; children: React.ReactNode }) {
  return (
    <div role="cell" className={cn("grid min-w-0 grid-cols-[9rem_minmax(0,1fr)] items-center gap-x-3 max-[1099px]:py-1 min-[1100px]:block", className)}>
      <span aria-hidden className="text-xs text-hint min-[1100px]:hidden">
        {label}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function Scorecard({ dashboard, rows }: { dashboard: ClientDashboard; rows: readonly ChannelRow[] }) {
  const be = breakevenOf(dashboard.kpi);
  const zones = zonesIn(rows);
  const period = periodOf(dashboard);
  return (
    <Section
      title="Channels"
      description={`Where the spend of each channel sits, what each 1 unit of spend brought this ${period}, and its part of the result.`}
      data-readout="scorecard"
    >
      <div role="table" aria-label="Channel scorecard" className="grid min-w-0 gap-3 min-[1100px]:gap-0">
        {/* Narrow: visually hidden but kept in the accessibility tree (the cells' own labels are
            aria-hidden), so a screen reader still hears each value with its column header. */}
        <div role="row" data-readout="scorecard-head" className={cn("sr-only border-b pb-2 text-xs text-hint min-[1100px]:not-sr-only", GRID)}>
          {SCORECARD_COLUMNS.map((c) => (
            <span role="columnheader" key={c.key} className={c.key === "share" ? "text-right" : undefined}>
              {c.label}
            </span>
          ))}
        </div>
        {rows.map((row) => {
          const cells = Object.fromEntries(scorecardCells(row).map((c) => [c.key, c]));
          return (
            <div
              role="row"
              key={row.name}
              data-channel={row.name}
              className={cn("grid min-w-0 gap-1 rounded-lg border p-3 min-[1100px]:rounded-none min-[1100px]:border-0 min-[1100px]:border-b min-[1100px]:px-0 min-[1100px]:py-3", GRID)}
            >
              <div role="rowheader" className="flex min-w-0 items-center gap-2 text-sm font-semibold text-foreground">
                {row.label}
              </div>
              <Cell label="Where the spend sits">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <ZoneStrip label={row.label} zone={row.zone} />
                  <ZoneTag zone={row.zone} />
                </div>
              </Cell>
              <Cell label="Return on media">
                <div className="grid min-w-0 gap-1">
                  <span className="text-sm font-semibold text-foreground tabular">{cells.roi?.text}</span>
                  {be !== null && row.roi !== null ? <RangeBar label={`${row.label} return on media`} value={row.roi} low={row.roiLow} high={row.roiHigh} reference={be} /> : null}
                </div>
              </Cell>
              <Cell label="Expected result">
                <span className="text-sm text-foreground tabular">{cells.contribution?.text}</span>
              </Cell>
              <Cell label="Share" className="min-[1100px]:text-right">
                <span className="text-sm text-foreground tabular">{cells.share?.text}</span>
              </Cell>
            </div>
          );
        })}
      </div>
      {zones.length ? <ZoneLegend zones={zones} /> : null}
      {be !== null ? (
        <p className="text-xs text-hint">
          The thin mark on each bar is break-even ({be === 1 ? "1.00" : be.toFixed(2)}). The coloured part is the likely range.
        </p>
      ) : null}
    </Section>
  );
}

function AccuracyCard({ dashboard, readout, canUpload }: { dashboard: ClientDashboard; readout: Readout; canUpload: boolean }) {
  const acc = accuracyOf(readout.snapshot);
  const lines = accuracyLines(acc, dashboard.kpi, periodOf(dashboard));
  return (
    <Section title="How near the model was" description="The model's expectation against the actual numbers." data-readout="accuracy">
      {lines.length ? (
        <ul className="grid gap-2 text-sm text-foreground">
          {lines.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">
          There are no actual numbers to compare yet.{canUpload ? " Send them in Update numbers." : ""}
        </p>
      )}
    </Section>
  );
}

export function ThisPeriod({
  dashboard,
  readout,
  previous,
  pending = [],
  preview = false,
}: {
  dashboard: ClientDashboard;
  /** The newest published readout (`null`: none yet). */
  readout: Readout | null;
  /** The published readout before it, for "What changed". */
  previous?: Readout | null | undefined;
  /** Numbers that wait for the analyst's review (visible to the uploader and the analysts). */
  pending?: readonly ReadoutHeader[];
  preview?: boolean;
}) {
  const period = periodOf(dashboard);
  const snap = readout?.status === "ready" ? readout.snapshot : null;
  const rows = channelRows(snap, dashboard);
  const change = changeRows(snap, previous?.status === "ready" ? previous.snapshot : null);
  const canUpload = Boolean(dashboard.inputs?.actuals);
  return (
    <div className="grid min-w-0 gap-6" data-tab="this_period">
      {snap ? <p className="max-w-[68ch] text-xl text-foreground" data-readout="lead">{snap.lead}</p> : null}
      <div className="grid gap-2 empty:hidden">
        {dashboard.live_inputs === "paused" ? (
          <InlineMessage tone="warning">New results are paused. The readouts here stay available. Your analyst is checking the dashboard.</InlineMessage>
        ) : null}
        {preview ? <InlineMessage tone="info">This is a preview. You see what your client sees. Inputs are off in the preview.</InlineMessage> : null}
        {pending.map((p) => (
          <InlineMessage key={p.seq} tone="info">
            The numbers sent on {dateOf(p.created_at)} wait for the analyst&apos;s review. Until then, only you and the analysts see the result in History.
          </InlineMessage>
        ))}
        {(snap?.notes_plain ?? []).map((n) => (
          <InlineMessage key={n} tone="info">
            {n}
          </InlineMessage>
        ))}
      </div>
      {!readout ? (
        <InlineMessage tone="info">There are no results yet. Your analyst publishes the first readout soon.</InlineMessage>
      ) : !snap ? (
        <ReadoutState readout={readout} />
      ) : (
        <>
          <ReadoutTiles dashboard={dashboard} readout={readout} />
          <KpiChart dashboard={dashboard} readout={readout} />
          {rows.length ? <Scorecard dashboard={dashboard} rows={rows} /> : null}
          <div className="grid min-w-0 gap-6 lg:grid-cols-12">
            {change ? (
              <Section
                className="lg:col-span-7"
                title={`What changed since the ${period} before`}
                description={`The change of each channel's part of the ${kpiWord(dashboard.kpi)} against the readout before.`}
                data-readout="change"
              >
                <DivergingBars label={`Change in ${kpiWord(dashboard.kpi)} by channel`} rows={change.rows} net={change.net} />
              </Section>
            ) : null}
            <div className={change ? "min-w-0 lg:col-span-5" : "min-w-0 lg:col-span-12"}>
              <AccuracyCard dashboard={dashboard} readout={readout} canUpload={canUpload} />
            </div>
          </div>
          {dashboard.note ? (
            <Section title="Note from your analyst">
              <p className="text-sm whitespace-pre-line text-foreground">{dashboard.note}</p>
            </Section>
          ) : null}
          {dashboard.show_technical ? <TechnicalToggle readout={readout} /> : null}
        </>
      )}
    </div>
  );
}
