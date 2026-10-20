/**
 * The Metric bars section of the compare page (v0.14): a table of every metric series (key and
 * labels) by run with one summary value (last, min or max), the best run marked, and a bar
 * chart per series. The best run has the smallest value for keys like loss, error, rmse, mae,
 * mape, smape, wape, crps and brier, else the largest.
 */
import { Trophy } from "lucide-react";
import { useMemo, useState } from "react";

import type { RunCompare } from "@/api/types";
import { Mono } from "@/components/common";
import { Card, CardContent, CardHeader, CardTitle, NativeSelect, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { compareBars } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import type { MetricAgg } from "@/features/runs/run-query";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

import { bestRun, lowerIsBetter, summarySeries, summaryValue, type SeriesRef } from "./compare-model";
import { RunSwatch } from "./run-chips";

/** Bar charts the section draws (the table has every series). */
export const MAX_BAR_CHARTS = 12;

const AGGS: { value: Exclude<MetricAgg, "count">; label: string }[] = [
  { value: "last", label: "Last value" },
  { value: "min", label: "Minimum" },
  { value: "max", label: "Maximum" },
];

function seriesName(s: SeriesRef): string {
  return s.id ? `${s.key}{${s.id}}` : s.key;
}

export function BarsSection({ data, labels }: { data: RunCompare; labels: readonly string[] }) {
  const [agg, setAgg] = useState<Exclude<MetricAgg, "count">>("last");
  const summaries = useMemo(() => data.summaries ?? [], [data.summaries]);
  const series = useMemo(() => summarySeries(summaries), [summaries]);
  const rows = useMemo(
    () =>
      series.map((s) => {
        const values = data.runs.map((r) => summaryValue(summaries, r.id, s, agg));
        return { series: s, values, best: bestRun(s.key, values) };
      }),
    [series, summaries, data.runs, agg],
  );
  if (!series.length) {
    return <p className="rounded-md border border-dashed px-3 py-6 text-center text-[13px] text-muted-foreground">The runs did not log metrics.</p>;
  }
  return (
    <section className="grid gap-4" aria-label="Metric bars">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[13px] text-muted-foreground">
          The best run has the smallest value for metrics such as loss, error and RMSE. For other metrics it has the largest value.
        </p>
        <NativeSelect aria-label="Value of each metric" value={agg} onChange={(e) => setAgg(e.target.value as typeof agg)} className="ml-auto h-8 w-auto text-xs">
          {AGGS.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </NativeSelect>
      </div>
      <Card>
        <CardContent className="pt-4">
          <Table aria-label="Metric values by run">
            <THead>
              <TR>
                <TH>Metric</TH>
                {data.runs.map((r, i) => (
                  <TH key={r.id} className="text-right">
                    <span className="inline-flex max-w-40 items-center gap-1.5">
                      <RunSwatch index={i} />
                      <span className="truncate" title={labels[i]}>
                        {labels[i]}
                      </span>
                    </span>
                  </TH>
                ))}
                <TH>Best</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map(({ series: s, values, best }) => (
                <TR key={`${s.key}|${s.id}`}>
                  <TD>
                    <Mono className="[overflow-wrap:anywhere]">{seriesName(s)}</Mono>
                  </TD>
                  {values.map((v, i) => (
                    <TD key={data.runs[i]!.id} className={cn("text-right tabular", best?.index === i && "font-semibold text-success")}>
                      {v === null ? <span className="text-muted-foreground">—</span> : formatNumber(v)}
                      {best?.index === i ? <Trophy className="ml-1 inline size-3.5" aria-label="Best run" /> : null}
                    </TD>
                  ))}
                  <TD className="text-xs text-muted-foreground">{best ? (best.direction === "min" ? "smallest" : "largest") : "—"}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
      <div className="grid gap-5 lg:grid-cols-2 2xl:grid-cols-3">
        {rows.slice(0, MAX_BAR_CHARTS).map(({ series: s, values }) => {
          const plot = compareBars(
            data.runs.map((r, i) => ({ run: labels[i] ?? r.id, value: values[i] ?? null })),
            `${s.key} (${agg})`,
          );
          return (
            <Card key={`${s.key}|${s.id}`} data-compare-bars={seriesName(s)}>
              <CardHeader>
                <CardTitle>
                  <Mono className="text-[13px] [overflow-wrap:anywhere]">{seriesName(s)}</Mono>
                </CardTitle>
                <span className="text-xs text-muted-foreground">{lowerIsBetter(s.key) ? "Smaller is better" : "Larger is better"}</span>
              </CardHeader>
              <CardContent>
                <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={`${seriesName(s)} by run`} height={Math.max(120, 40 + 26 * data.runs.length)} exportName={`compare-bars-${s.key}`} />
              </CardContent>
            </Card>
          );
        })}
      </div>
      {rows.length > MAX_BAR_CHARTS ? <p className="text-xs text-muted-foreground">The section draws the first {MAX_BAR_CHARTS} metrics. The table shows all of them.</p> : null}
      {data.truncated?.summaries ? <p className="text-xs text-muted-foreground">The runs have more metric series than the server sends. The table shows a part of them.</p> : null}
    </section>
  );
}
