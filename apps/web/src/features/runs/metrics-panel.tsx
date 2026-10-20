/**
 * The Metrics tab of the run page (v0.11): the run's metric series grouped by key. Stepped keys
 * get a chart card each (colour by a label, show or hide label values, log scale); keys logged
 * only without a step are listed in the "Values" table. A live run refetches what its SSE
 * `tracking` frames changed (see `useRunEvents`). Label values are untrusted text.
 */
import { Gauge } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { useMetricHistory, useRunMetrics } from "@/api/queries";
import type { MetricSeries } from "@/api/tracking";
import { EmptyState, ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Checkbox, Switch } from "@/components/ui/overlays";
import { Card, CardContent, CardHeader, CardTitle, Label, NativeSelect, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { metricCurves } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { formatNumber, relativeTime } from "@/lib/format";

import { defaultColorLabel, filterSeries, groupSeries, groupStats, labelText, labelValues, type MetricGroup } from "./metrics-model";

/** Points per series the charts ask for (the server downsamples with LTTB). */
export const CHART_POINTS = 1000;

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className="text-[13px] font-medium tabular">{value}</span>
    </div>
  );
}

/** Show or hide the values of one label (every value shown at first). */
function LabelFilter({ label, values, hidden, onChange }: { label: string; values: string[]; hidden: readonly string[]; onChange: (hidden: string[]) => void }) {
  const shown = values.length - hidden.length;
  // unique on the page: several cards can filter the same label and value
  const uid = useId();
  return (
    <details className="relative text-xs">
      <summary className="flex h-8 cursor-pointer list-none items-center rounded-md border bg-card px-2.5 [&::-webkit-details-marker]:hidden">
        Show {label}: {shown === values.length ? "all" : `${shown} of ${values.length}`}
      </summary>
      <div role="group" aria-label={`Values of ${label}`} className="absolute z-20 mt-1 grid max-h-64 min-w-40 gap-1.5 overflow-auto rounded-md border bg-popover p-2 shadow-md">
        {values.map((v, i) => {
          const id = `${uid}-${i}`;
          return (
            <span key={v} className="flex items-center gap-2">
              <Checkbox
                id={id}
                checked={!hidden.includes(v)}
                onCheckedChange={(on) => onChange(on ? hidden.filter((h) => h !== v) : [...hidden, v])}
              />
              <Label htmlFor={id} className="text-xs font-normal [overflow-wrap:anywhere]">
                {v}
              </Label>
            </span>
          );
        })}
      </div>
    </details>
  );
}

function MetricCard({ runId, group }: { runId: string; group: MetricGroup }) {
  const [colorBy, setColorBy] = useState<string | null>(() => defaultColorLabel(group.labelKeys));
  const [logY, setLogY] = useState(false);
  const [hidden, setHidden] = useState<Record<string, string[]>>({});
  const history = useMetricHistory(runId, group.key, { maxPoints: CHART_POINTS });
  const values = useMemo(() => labelValues(group.series), [group.series]);
  const filterLabels = group.labelKeys.filter((k) => k !== colorBy && (values[k]?.length ?? 0) > 1);
  const visible = useMemo(() => {
    if (!history.data) return null;
    const active = Object.fromEntries(Object.entries(hidden).filter(([k]) => k !== colorBy));
    return { ...history.data, series: filterSeries(history.data.series ?? [], active) };
  }, [history.data, hidden, colorBy]);
  const plot = useMemo(() => (visible?.series?.length ? metricCurves(visible, { colorBy, logY }) : null), [visible, colorBy, logY]);
  const stats = groupStats(filterSeries(group.series, hidden));
  const downsampled = history.data?.series?.some((s) => s.downsampled) ?? false;
  const logId = `log-${group.key}`;

  return (
    <Card data-metric={group.key}>
      <CardHeader className="flex-wrap gap-y-2">
        <CardTitle>
          <Mono className="text-[13px]">{group.key}</Mono>
        </CardTitle>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {group.labelKeys.length ? (
            <NativeSelect aria-label="Colour by" value={colorBy ?? ""} onChange={(e) => setColorBy(e.target.value || null)} className="h-8 w-auto max-w-60 text-xs">
              <option value="">One colour</option>
              {group.labelKeys.map((k) => (
                <option key={k} value={k}>
                  Colour by {k}
                </option>
              ))}
            </NativeSelect>
          ) : null}
          {filterLabels.map((k) => (
            <LabelFilter key={k} label={k} values={values[k] ?? []} hidden={hidden[k] ?? []} onChange={(h) => setHidden((prev) => ({ ...prev, [k]: h }))} />
          ))}
          <span className="flex items-center gap-1.5">
            <Switch id={logId} checked={logY} onCheckedChange={setLogY} aria-label="Log scale" />
            <Label htmlFor={logId} className="text-xs font-normal">
              Log scale
            </Label>
          </span>
        </div>
      </CardHeader>
      <CardContent className="grid gap-3">
        {stats ? (
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            <Stat label="Latest" value={formatNumber(stats.latest)} />
            <Stat label="Minimum" value={formatNumber(stats.min)} />
            <Stat label="Maximum" value={formatNumber(stats.max)} />
            <Stat label="Series" value={String(stats.series)} />
          </div>
        ) : null}
        {history.isError ? (
          <ErrorNotice error={history.error} title="Cannot load the metric history" />
        ) : !history.data ? (
          <LoadingRows rows={4} />
        ) : plot ? (
          <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={group.key} height={260} exportName={`metric-${group.key}`} />
        ) : (
          <p className="text-[13px] text-muted-foreground">The filters hide all the series. Show more values.</p>
        )}
        {downsampled || history.data?.truncated ? (
          <p className="text-xs text-muted-foreground">
            {downsampled ? `The chart shows a sample of at most ${CHART_POINTS.toLocaleString("en")} points for each series. ` : ""}
            {history.data?.truncated ? `The chart shows the first ${history.data.series?.length ?? 0} series.` : ""}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function ValuesTable({ series }: { series: MetricSeries[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Values</CardTitle>
      </CardHeader>
      <CardContent>
        <Table aria-label="Metric values">
          <THead>
            <TR>
              <TH>Metric</TH>
              <TH>Labels</TH>
              <TH className="text-right">Value</TH>
              <TH>Logged</TH>
            </TR>
          </THead>
          <TBody>
            {series.map((s) => (
              <TR key={`${s.key}|${labelText(s.labels)}`}>
                <TD>
                  <Mono>{s.key}</Mono>
                </TD>
                <TD className="text-muted-foreground [overflow-wrap:anywhere]">{labelText(s.labels) || "—"}</TD>
                <TD className="text-right tabular">{formatNumber(s.last)}</TD>
                <TD className="text-muted-foreground" title={s.last_ts}>
                  {relativeTime(s.last_ts)}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function MetricsPanel({ runId, live }: { runId: string; live: boolean }) {
  const metrics = useRunMetrics(runId);
  const groups = useMemo(() => groupSeries(metrics.data?.items ?? []), [metrics.data]);
  if (metrics.isError) return <ErrorNotice error={metrics.error} title="Cannot load the metrics of the run" />;
  if (metrics.isPending) return <LoadingRows rows={4} />;
  if (!groups.length) {
    return (
      <EmptyState icon={<Gauge />} title={live ? "No metrics yet" : "No metrics"}>
        {live
          ? "The page updates when the run logs metrics."
          : "The run did not log metrics. Model code and workflow nodes can log metrics during a run. You can also log metrics after the run with the API."}
      </EmptyState>
    );
  }
  const scalars = groups.filter((g) => g.scalar).flatMap((g) => g.series);
  const charts = groups.filter((g) => !g.scalar);
  return (
    <div className="grid gap-5">
      {scalars.length ? <ValuesTable series={scalars} /> : null}
      {charts.length ? (
        <div className="grid gap-5 xl:grid-cols-2">
          {charts.map((g) => (
            <MetricCard key={g.key} runId={runId} group={g} />
          ))}
        </div>
      ) : null}
      {metrics.data?.more ? <p className="text-xs text-muted-foreground">The run has more series than the page shows.</p> : null}
    </div>
  );
}
