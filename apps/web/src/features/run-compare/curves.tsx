/**
 * The Curves section of the compare page (v0.14): one chart per metric key with steps, a line
 * per run (in the run colours) and per series, dashed by the value of a label (`split` by
 * default), with a log scale switch. The key picker writes the keys to the URL (12 maximum);
 * without a choice the server picks the keys most of the runs logged. Keys without steps are
 * in the Metric bars section.
 */
import { useId, useMemo, useState } from "react";

import { MAX_COMPARE_KEYS } from "@/api/experiments";
import type { CompareMetric, Run, RunCompare } from "@/api/types";
import { Mono } from "@/components/common";
import { Checkbox, Switch } from "@/components/ui/overlays";
import { Card, CardContent, CardHeader, CardTitle, Label, NativeSelect } from "@/components/ui/primitives";
import { compareCurves } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";

import { defaultSplitLabel, metricKeys, type MetricKeyInfo } from "./compare-model";

/** Does the metric have a series with two or more stepped points (a curve)? */
export function hasCurve(metric: CompareMetric): boolean {
  return (metric.series ?? []).some((s) => s.steps.filter((st) => st !== null && st !== undefined).length > 1);
}

function KeyPicker({ infos, selected, onChange }: { infos: MetricKeyInfo[]; selected: readonly string[]; onChange: (keys: string[]) => void }) {
  const uid = useId();
  const full = selected.length >= MAX_COMPARE_KEYS;
  return (
    <details className="relative text-xs">
      <summary className="flex h-8 cursor-pointer list-none items-center rounded-md border bg-card px-2.5 [&::-webkit-details-marker]:hidden">
        Metrics: {selected.length} of {infos.length}
      </summary>
      <div role="group" aria-label="Metrics to draw" className="absolute right-0 z-20 mt-1 grid max-h-72 min-w-56 gap-1.5 overflow-auto rounded-md border bg-popover p-2 shadow-md">
        <p className="text-muted-foreground">Select {MAX_COMPARE_KEYS} metrics maximum.</p>
        {infos.map((k, i) => {
          const id = `${uid}-${i}`;
          const on = selected.includes(k.key);
          return (
            <span key={k.key} className="flex items-center gap-2">
              <Checkbox id={id} checked={on} disabled={!on && full} onCheckedChange={(v) => onChange(v ? [...selected, k.key] : selected.filter((x) => x !== k.key))} />
              <Label htmlFor={id} className="flex-1 font-mono text-xs font-normal [overflow-wrap:anywhere]">
                {k.key}
              </Label>
              <span className="text-muted-foreground tabular">{k.runs}</span>
            </span>
          );
        })}
      </div>
    </details>
  );
}

function CurveCard({ metric, runs, labels }: { metric: CompareMetric; runs: readonly Run[]; labels: readonly string[] }) {
  const all = useMemo(() => metric.series ?? [], [metric.series]);
  const labelKeys = useMemo(() => [...new Set(all.flatMap((s) => Object.keys(s.labels ?? {})))].sort(), [all]);
  const [splitBy, setSplitBy] = useState<string | null>(() => defaultSplitLabel(labelKeys));
  const [logY, setLogY] = useState(false);
  const plot = useMemo(
    () =>
      compareCurves(
        runs.map((r, i) => ({ runLabel: labels[i] ?? r.id, history: all.filter((s) => s.run_id === r.id) })),
        metric.key,
        { splitBy, logY },
      ),
    [runs, labels, all, metric.key, splitBy, logY],
  );
  const downsampled = all.some((s) => s.downsampled);
  const logId = `compare-log-${metric.key}`;
  return (
    <Card data-compare-metric={metric.key}>
      <CardHeader className="flex-wrap gap-y-2">
        <CardTitle>
          <Mono className="text-[13px]">{metric.key}</Mono>
        </CardTitle>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {labelKeys.length ? (
            <NativeSelect aria-label={`Line style of ${metric.key}`} value={splitBy ?? ""} onChange={(e) => setSplitBy(e.target.value || null)} className="h-8 w-auto max-w-60 text-xs">
              <option value="">Solid lines</option>
              {labelKeys.map((k) => (
                <option key={k} value={k}>
                  Dash by {k}
                </option>
              ))}
            </NativeSelect>
          ) : null}
          <span className="flex items-center gap-1.5">
            <Switch id={logId} checked={logY} onCheckedChange={setLogY} aria-label="Log scale" />
            <Label htmlFor={logId} className="text-xs font-normal">
              Log scale
            </Label>
          </span>
        </div>
      </CardHeader>
      <CardContent className="grid gap-2">
        <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={metric.key} height={280} exportName={`compare-${metric.key}`} />
        {downsampled ? <p className="text-xs text-muted-foreground">The chart shows a sample of the points of the long series.</p> : null}
      </CardContent>
    </Card>
  );
}

export function CurvesSection({ data, labels, keys, onKeys }: { data: RunCompare; labels: readonly string[]; keys: readonly string[] | undefined; onKeys: (keys: string[]) => void }) {
  const infos = useMemo(() => metricKeys(data.summaries ?? []).filter((k) => k.stepped), [data.summaries]);
  const metrics = data.metrics ?? [];
  // `keys` is the user's choice (`[]`: none); without one, the server's keys
  const curves = metrics.filter((m) => hasCurve(m) && (!keys || keys.includes(m.key)));
  const selected = keys ?? metrics.map((m) => m.key);
  return (
    <section className="grid gap-4" aria-label="Curves">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[13px] text-muted-foreground">Each chart shows one metric over the steps. The colour shows the run.</p>
        {infos.length ? (
          <div className="ml-auto">
            <KeyPicker infos={infos} selected={selected.filter((k) => infos.some((i) => i.key === k))} onChange={onKeys} />
          </div>
        ) : null}
      </div>
      {curves.length ? (
        <div className="grid gap-5 xl:grid-cols-2">
          {curves.map((m) => (
            <CurveCard key={m.key} metric={m} runs={data.runs} labels={labels} />
          ))}
        </div>
      ) : (
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-[13px] text-muted-foreground">
          {infos.length ? "Select the metrics to draw." : "The runs did not log metrics with steps. The Metric bars section shows their values."}
        </p>
      )}
      {data.truncated?.series ? <p className="text-xs text-muted-foreground">Some runs have more series of a metric than the charts show.</p> : null}
    </section>
  );
}
