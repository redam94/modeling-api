/**
 * "Predictions against your actuals": for a forecast sent actuals, the one-step-ahead
 * predictions on the actual periods next to the actuals (how far off, how often inside the
 * band), then the forecast after them - as a chart in the plot sandbox and a table.
 */
import { CalendarCheck } from "lucide-react";
import { useMemo, useState } from "react";

import type { ScoreSummary } from "@/api/types";
import { Mono } from "@/components/common";
import { Field, NativeSelect, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { PlotFrame } from "@/features/plots/plot-frame";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

import { compareActuals, comparisonChart, metricsOf, type Comparison } from "./compare";

function pct(v: number | null): string {
  return v === null ? "—" : `${v.toFixed(v >= 10 ? 0 : 1)}%`;
}

export function ActualsComparison({ summary }: { summary: ScoreSummary }) {
  const comparisons = useMemo(
    () => (summary.actuals ?? []).map((a) => compareActuals(summary, a, summary.conditioning?.dim ?? null)).filter((c): c is Comparison => c !== null),
    [summary],
  );
  if (!comparisons.length) return null;
  return (
    <div className="grid gap-4">
      {comparisons.map((c) => (
        <ComparisonView key={c.variable} c={c} />
      ))}
    </div>
  );
}

function ComparisonView({ c }: { c: Comparison }) {
  const [series, setSeries] = useState<string | null>(c.series.length > 1 ? null : (c.series[0] ?? null));
  const shown = series === null ? c.points : c.points.filter((p) => p.series === series);
  const m = series === null ? c.metrics : metricsOf(shown);
  const chart = useMemo(() => comparisonChart(c, series), [c, series]);
  const rows = shown.filter((p) => p.actual !== null || p.forecast);
  const fmt = (v: number | null) => (v === null ? "—" : formatNumber(v));
  const direction = m.bias === null ? "" : m.bias > 0 ? "over" : m.bias < 0 ? "under" : "";

  return (
    <section aria-label={`Predictions against your actuals: ${c.variable}`} className="grid gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <CalendarCheck className="size-4 text-primary" aria-hidden />
        <h3 className="text-[13px] font-semibold">Predictions against your actuals</h3>
        <Mono className="text-xs text-muted-foreground">{c.variable}</Mono>
      </div>
      <p className="text-[13px] text-muted-foreground">
        For the periods that have actuals (◆), the predictions are one-step-ahead. Each prediction is the value that the model expected before it saw that actual.
        After the last actual (dashed line), the model forecasts.
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="How close the one-step-ahead predictions were">
        <Stat label="Actuals compared" value={String(m.n)} />
        <Stat label="Typical error (MAE)" value={fmt(m.mae)} />
        <Stat label="Average % error" value={pct(m.mape)} />
        <Stat
          label={c.band ? `Inside the ${c.band.label} band` : "Inside the band"}
          value={pct(m.coverage)}
          hint={m.bias !== null && direction ? `on average ${formatNumber(Math.abs(m.bias))} ${direction}-predicted` : undefined}
        />
      </dl>
      {c.series.length > 1 ? (
        <Field label={c.seriesColumns.join(" · ") || "Series"} htmlFor="compare-series">
          <NativeSelect id="compare-series" className="w-56" value={series ?? ""} onChange={(e) => setSeries(e.target.value || null)}>
            <option value="">All ({c.series.length})</option>
            {c.series.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : null}
      <PlotFrame kind="vega-lite" spec={chart.spec} datasets={chart.datasets} title={`${c.variable}: predictions against actuals`} exportName={`${c.variable}-vs-actuals`} height={280} />
      <div className="max-h-72 overflow-auto rounded-md border">
        <Table aria-label="Actuals and predictions" className="text-xs">
          <THead className="sticky top-0 bg-card">
            <TR>
              {c.seriesColumns.length ? <TH>{c.seriesColumns.join(" · ")}</TH> : null}
              <TH>{c.periodColumn}</TH>
              <TH className="text-right">actual</TH>
              <TH className="text-right">predicted ({c.center})</TH>
              {c.band ? <TH className="text-right">{c.band.label}</TH> : null}
              <TH className="text-right">error</TH>
            </TR>
          </THead>
          <TBody>
            {rows.map((p) => {
              const err = p.actual !== null && p.predicted !== null ? p.predicted - p.actual : null;
              const outside = p.actual !== null && p.lower !== null && p.upper !== null && (p.actual < p.lower || p.actual > p.upper);
              return (
                <TR key={`${p.series}|${p.period}`} className={cn(p.forecast && "text-muted-foreground")}>
                  {c.seriesColumns.length ? <TD>{p.series}</TD> : null}
                  <TD className="font-mono whitespace-nowrap">
                    {p.period}
                    {p.forecast ? <span className="ml-1.5 text-[10px] uppercase">forecast</span> : null}
                  </TD>
                  <TD className="text-right tabular">{fmt(p.actual)}</TD>
                  <TD className="text-right tabular">{fmt(p.predicted)}</TD>
                  {c.band ? (
                    <TD className={cn("text-right tabular", outside && "text-warning")} title={outside ? "The actual is outside this band." : undefined}>
                      {fmt(p.lower)} – {fmt(p.upper)}
                    </TD>
                  ) : null}
                  <TD className="text-right tabular">{err === null ? "" : `${err > 0 ? "+" : ""}${formatNumber(err)}`}</TD>
                </TR>
              );
            })}
          </TBody>
        </Table>
      </div>
    </section>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string | undefined }) {
  return (
    <div className="grid gap-0.5 rounded-md border bg-muted/30 px-3 py-2">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-[15px] font-semibold tabular">{value}</dd>
      {hint ? <dd className="text-xs text-muted-foreground">{hint}</dd> : null}
    </div>
  );
}
