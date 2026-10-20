/**
 * The Media tab of the run page (v0.14): the images and tables the run logged
 * (`ctx.log_image`, `log_figure`, `log_table`, or the API), one card per key. A card has a
 * select of the key's label sets and a step slider over the logged steps (the newest at first:
 * a live run moves it on as items arrive). A live run refetches its media when a `tracking`
 * frame says media changed (see `useRunEvents`). Captions and table cells are untrusted text.
 */
import { Images } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { useRunMedia, useRunMediaKeys } from "@/api/queries";
import { EmptyState, ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Badge, Card, CardContent, CardHeader, CardTitle, NativeSelect } from "@/components/ui/primitives";

import { MediaItemView } from "./media-view";
import { groupMediaKeys, labelSetText, seriesItems, sliderIndex, type MediaGroup } from "./media-model";
import { labelsKey } from "./run-query";

/** A step slider over `steps` (positions, not values: steps can have gaps). */
export function StepSlider({ label, steps, index, onChange }: { label: string; steps: readonly (number | null | undefined)[]; index: number; onChange: (index: number) => void }) {
  const id = useId();
  if (steps.length < 2) return null;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-3">
      <label htmlFor={id} className="text-xs text-muted-foreground">
        Step
      </label>
      <input
        id={id}
        type="range"
        min={0}
        max={steps.length - 1}
        step={1}
        value={index}
        aria-label={label}
        aria-valuetext={`step ${steps[index] ?? "—"}`}
        onChange={(e) => onChange(Number(e.target.value))}
        className="min-w-32 flex-1 accent-[var(--primary)]"
      />
      <span className="text-xs tabular">
        {steps[index] ?? "—"} <span className="text-muted-foreground">of {steps[0] ?? "—"}…{steps.at(-1) ?? "—"}</span>
      </span>
    </div>
  );
}

function MediaCard({ runId, group }: { runId: string; group: MediaGroup }) {
  const [labelsId, setLabelsId] = useState(() => labelsKey(group.series[0]?.labels ?? {}));
  const [pos, setPos] = useState<number | null>(null);
  const media = useRunMedia(runId, { key: group.key });
  const items = useMemo(() => seriesItems(media.data?.items ?? [], labelsId), [media.data, labelsId]);
  const index = sliderIndex(pos, items.length);
  const current = items[index] ?? null;

  return (
    <Card data-media-key={group.key}>
      <CardHeader className="flex-wrap gap-y-2">
        <CardTitle className="flex items-center gap-2">
          <Mono className="text-[13px]">{group.key}</Mono>
          <Badge variant="outline">{group.kind === "image" ? "images" : "tables"}</Badge>
        </CardTitle>
        {group.series.length > 1 ? (
          <NativeSelect
            aria-label={`Labels of ${group.key}`}
            value={labelsId}
            onChange={(e) => {
              setLabelsId(e.target.value);
              setPos(null);
            }}
            className="ml-auto h-8 w-auto max-w-72 text-xs"
          >
            {group.series.map((s) => {
              const id = labelsKey(s.labels ?? {});
              return (
                <option key={id} value={id}>
                  {labelSetText(s.labels) || "No labels"} ({s.count})
                </option>
              );
            })}
          </NativeSelect>
        ) : group.series[0] && labelSetText(group.series[0].labels) ? (
          <span className="ml-auto text-xs text-muted-foreground [overflow-wrap:anywhere]">{labelSetText(group.series[0].labels)}</span>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-3">
        {media.isError ? (
          <ErrorNotice error={media.error} title="The app could not load the media of this key" />
        ) : !media.data ? (
          <LoadingRows rows={3} />
        ) : current ? (
          <>
            <StepSlider label={`Step of ${group.key}`} steps={items.map((m) => m.step)} index={index} onChange={(i) => setPos(i === items.length - 1 ? null : i)} />
            <MediaItemView media={current} />
          </>
        ) : (
          <p className="text-[13px] text-muted-foreground">This label set has no items.</p>
        )}
        {media.data?.more ? <p className="text-xs text-muted-foreground">The key has more items than the tab shows.</p> : null}
      </CardContent>
    </Card>
  );
}

export function MediaPanel({ runId, live }: { runId: string; live: boolean }) {
  const keys = useRunMediaKeys(runId);
  const groups = useMemo(() => groupMediaKeys(keys.data?.items ?? []), [keys.data]);
  if (keys.isError) return <ErrorNotice error={keys.error} title="The app could not load the media of the run" />;
  if (keys.isPending) return <LoadingRows rows={4} />;
  if (!groups.length) {
    return (
      <EmptyState icon={<Images />} title={live ? "No media yet" : "No media"}>
        {live
          ? "The run did not log images or tables yet. The page updates when it logs them."
          : "The run did not log images or tables. Model code can log them with log_image, log_figure and log_table. You can also log them after the run with the API."}
      </EmptyState>
    );
  }
  return (
    <div className="grid gap-5 xl:grid-cols-2">
      {groups.map((g) => (
        <MediaCard key={g.key} runId={runId} group={g} />
      ))}
    </div>
  );
}
