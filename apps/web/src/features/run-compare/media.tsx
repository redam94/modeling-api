/**
 * The Media section of the compare page (v0.14): for each chosen media key (4 maximum, in the
 * URL), a step slider over the steps of every run and a grid with the item of each run at the
 * greatest step at or below the slider. Images download through checked object URLs (see
 * `media-view.tsx`). Captions and table cells are untrusted text.
 */
import { useMemo, useState } from "react";

import { MAX_COMPARE_MEDIA_ITEMS, MAX_COMPARE_MEDIA_KEYS } from "@/api/experiments";
import type { MediaKind, RunCompare } from "@/api/types";
import { Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, NativeSelect } from "@/components/ui/primitives";
import { StepSlider } from "@/features/runs/media-panel";
import { MediaItemView } from "@/features/runs/media-view";
import { labelsKey } from "@/features/runs/run-query";

import { itemAtStep, unionSteps } from "./compare-model";
import { RunSwatch } from "./run-chips";

export interface MediaKeyOption {
  key: string;
  kind: MediaKind;
  /** Runs that logged the key. */
  runs: number;
}

function MediaKeyBlock({ data, labels, mediaKey }: { data: RunCompare; labels: readonly string[]; mediaKey: string }) {
  const series = useMemo(() => (data.media ?? []).filter((m) => m.key === mediaKey), [data.media, mediaKey]);
  const labelSets = useMemo(() => [...new Set(series.map((m) => labelsKey(m.labels ?? {})))].sort(), [series]);
  const [labelsId, setLabelsId] = useState<string | null>(null);
  const active = labelsId !== null && labelSets.includes(labelsId) ? labelsId : (labelSets[0] ?? "");
  const perRun = useMemo(
    () =>
      data.runs.map((r) =>
        [...(series.find((m) => m.run_id === r.id && labelsKey(m.labels ?? {}) === active)?.items ?? [])].sort((a, b) => (a.step ?? 0) - (b.step ?? 0)),
      ),
    [data.runs, series, active],
  );
  const steps = useMemo(() => unionSteps(perRun), [perRun]);
  const [pos, setPos] = useState<number | null>(null);
  const index = pos === null ? Math.max(0, steps.length - 1) : Math.min(pos, Math.max(0, steps.length - 1));
  const step = steps[index] ?? 0;

  return (
    <Card data-compare-media={mediaKey}>
      <CardHeader className="flex-wrap gap-y-2">
        <CardTitle>
          <Mono className="text-[13px]">{mediaKey}</Mono>
        </CardTitle>
        {labelSets.length > 1 ? (
          <NativeSelect aria-label={`Labels of ${mediaKey}`} value={active} onChange={(e) => setLabelsId(e.target.value)} className="ml-auto h-8 w-auto max-w-72 text-xs">
            {labelSets.map((l) => (
              <option key={l} value={l}>
                {l ? l.replace(/,/g, ", ") : "No labels"}
              </option>
            ))}
          </NativeSelect>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-4">
        {steps.length ? (
          <StepSlider label={`Step of ${mediaKey}`} steps={steps} index={index} onChange={(i) => setPos(i === steps.length - 1 ? null : i)} />
        ) : (
          <p className="text-[13px] text-muted-foreground">The runs did not log items for this key.</p>
        )}
        <div className="grid grid-cols-[repeat(auto-fill,minmax(12rem,1fr))] gap-4">
          {data.runs.map((r, i) => {
            const item = itemAtStep(perRun[i] ?? [], step);
            return (
              <div key={r.id} className="grid min-w-0 content-start gap-2" data-run={r.id}>
                <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <RunSwatch index={i} />
                  <span className="truncate">{labels[i]}</span>
                  {item && item.step !== step ? <span>(step {item.step ?? "—"})</span> : null}
                </span>
                {item ? <MediaItemView media={item} compact /> : <p className="rounded-md border border-dashed px-2 py-6 text-center text-xs text-muted-foreground">No item at this step</p>}
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}

export function MediaSection({
  data,
  labels,
  options,
  selected,
  onSelect,
  loading,
}: {
  data: RunCompare;
  labels: readonly string[];
  /** The media keys of the runs (union). */
  options: readonly MediaKeyOption[];
  selected: readonly string[];
  onSelect: (keys: string[]) => void;
  loading: boolean;
}) {
  if (!loading && !options.length) {
    return <p className="rounded-md border border-dashed px-3 py-6 text-center text-[13px] text-muted-foreground">The runs did not log images or tables.</p>;
  }
  const full = selected.length >= MAX_COMPARE_MEDIA_KEYS;
  return (
    <section className="grid gap-4" aria-label="Media">
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Media keys">
        <span className="text-xs text-muted-foreground">Keys ({MAX_COMPARE_MEDIA_KEYS} maximum):</span>
        {options.map((o) => {
          const on = selected.includes(o.key);
          return (
            <Button
              key={o.key}
              size="sm"
              variant={on ? "default" : "outline"}
              aria-pressed={on}
              disabled={!on && full}
              onClick={() => onSelect(on ? selected.filter((k) => k !== o.key) : [...selected, o.key])}
            >
              <span className="font-mono">{o.key}</span>
              <span className="text-[11px] opacity-70">
                {o.kind === "image" ? "images" : "tables"} · {o.runs} {o.runs === 1 ? "run" : "runs"}
              </span>
            </Button>
          );
        })}
      </div>
      {selected.map((k) => (
        <MediaKeyBlock key={k} data={data} labels={labels} mediaKey={k} />
      ))}
      {data.truncated?.media ? <p className="text-xs text-muted-foreground">The server sends {MAX_COMPARE_MEDIA_ITEMS} items maximum for each run and key: an even sample of the steps, with the last one.</p> : null}
    </section>
  );
}
