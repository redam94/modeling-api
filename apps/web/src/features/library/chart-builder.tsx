/**
 * Make your own › Charts: the chart template builder (v0.18 D5, §3.4).
 *
 * A chart template restyles the data of a platform chart: pick the chart type it starts from
 * (its example data shows which datasets and columns there are), pick a starting point, choose
 * what goes on each axis, give it a title and a caption. "Advanced: Vega-Lite" opens the spec
 * as JSON. The preview is drawn on the chart type's example data by
 * `POST /v1/library/charts:preview` (JSON only, no run) 250 ms after you stop editing, in the
 * plot sandbox like every other chart, and the sanitizer's findings are shown in plain words.
 * Saved templates show on run and model pages under "Your charts" (signed-in only; never in
 * share links or client portals).
 */
import type { JsonObject as PlotJson } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";
import { Save, Wand2 } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, IssueList, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Field, NativeSelect, Textarea } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { PlotFrame } from "@/features/plots/plot-frame";
import { plainIssue } from "@/lib/plain-issue";
import { cn } from "@/lib/utils";

import { decodeInline, useChartExample } from "./chart-data";
import { useSaveDoc, type Saved } from "./actions";
import { useChartTypes } from "./catalog";
import { docKey, isStaleRevision, useChartPreview, useLibraryItem, useLibraryValidation } from "./api";
import { chartStarters, columnsOf, EMPTY_CHART, MARKS, simpleSpec, type DatasetColumns, type SimpleChart } from "./chart-starters";
import { tidyDoc } from "./doc";
import { BuilderSection, Chip, focusPointer, IssueLines, issueText, MetadataFields, PILL, PILL_SM, ScopeChoice, ToggleChip, useDebounced } from "./ui";
import { LazyYamlTab as YamlTab } from "./yaml-tab-lazy";
import { chartPreviewable, isChartTemplateDoc, PLOT_SPEC_MAX_BYTES, jsonBytes, splitUseRef, type ChartTemplateDoc, type JsonObject, type LibraryDoc, type LibraryScope } from "./types";

const PREVIEW_DEBOUNCE_MS = 250;

function ColumnSelect({ label, value, onChange, ds, optional = false, pointer }: { label: string; value: string; onChange: (v: string) => void; ds: DatasetColumns | undefined; optional?: boolean; pointer?: string }) {
  const id = useId();
  return (
    <Field label={label} htmlFor={id}>
      <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value)} {...(pointer ? { "data-pointer": pointer } : {})}>
        <option value="">{optional ? "None" : "Pick a column"}</option>
        {(ds?.columns ?? []).map((c) => (
          <option key={c.name} value={c.name}>
            {c.name} ({c.type === "temporal" ? "date" : c.type === "quantitative" ? "number" : "category"})
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}

/** The live preview: the server draws the template on the example, the sandbox renders it. */
function ChartPreviewPanel({ doc: raw, enabled }: { doc: ChartTemplateDoc; enabled: boolean }) {
  // An untitled template still previews (the title is checked when you save).
  const doc = useMemo(() => (raw.metadata.title.trim() && raw.metadata.name ? raw : { ...raw, metadata: { ...raw.metadata, title: raw.metadata.title.trim() || "Untitled chart", name: raw.metadata.name || "untitled-chart" } }), [raw]);
  const debounced = useDebounced(doc, PREVIEW_DEBOUNCE_MS);
  // v0.18 live: `enabled` follows the doc at once, the debounced copy lags behind it; right
  // after a chart type is picked the copy still has no type (or no spec), and previewing it
  // only earned a 422 `/source_recipe`. Ask only for a copy that has both.
  const ready = enabled && chartPreviewable(debounced);
  const preview = useChartPreview(ready ? (debounced as unknown as JsonObject) : null, ready);
  const plot = preview.data?.plot ?? null;
  const data = useQuery({
    queryKey: ["library", "chart-preview-data", plot ? docKey(plot.datasets ?? {}) : ""],
    enabled: Boolean(plot),
    staleTime: Infinity,
    queryFn: ({ signal }) => decodeInline(plot!, signal),
  });
  const pending = enabled && (preview.isFetching || docKey(debounced) !== docKey(doc));
  const issues = preview.data?.issues ?? [];
  return (
    <section aria-label="Preview" className="grid gap-3 rounded-2xl border bg-card p-4" data-testid="chart-preview" data-tour="library.chart-preview">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">Preview on example data</h2>
        <span role="status" className="flex items-center gap-1.5 text-xs text-muted-foreground">
          {pending ? (
            <>
              <Spinner className="size-3" /> Drawing…
            </>
          ) : plot ? (
            "Up to date"
          ) : null}
        </span>
      </div>
      {!enabled ? (
        <p className="rounded-xl border border-dashed border-border-strong p-6 text-center text-muted-foreground">Pick a chart type and a starting point to see your chart here.</p>
      ) : preview.isError ? (
        <ErrorNotice error={preview.error} title="The preview did not draw" />
      ) : plot && data.data ? (
        <PlotFrame kind={plot.kind} spec={plot.spec as PlotJson} datasets={data.data} title={plot.title || "Chart preview"} height={320} exportName={plot.id} />
      ) : (
        <div className="grid h-80 place-items-center rounded-xl bg-muted text-sm text-muted-foreground" aria-hidden={pending ? undefined : true}>
          {pending ? "Drawing your chart…" : "Fix the problems below to see the chart."}
        </div>
      )}
      {issues.length ? (
        <ul className="grid gap-1" aria-label="What the safety check says">
          {issues.map((i, k) => (
            <li key={k} className={cn("text-xs", (i.severity ?? "error") === "error" ? "font-medium text-destructive" : "text-muted-foreground")}>
              {plainIssue(i)}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

export interface ChartBuilderProps {
  itemId: string | null;
  initial: ChartTemplateDoc;
  onCreated?: (saved: Saved) => void;
}

export function ChartBuilder({ itemId, initial, onCreated }: ChartBuilderProps) {
  const id = useId();
  const grants = useGrants();
  const item = useLibraryItem(itemId ?? undefined);
  const types = useChartTypes();
  const [doc, setDoc] = useState<ChartTemplateDoc>(initial);
  /** The simple editor's choices; `null` once the spec was edited as JSON (or when editing a saved one). */
  const [simple, setSimple] = useState<SimpleChart | null>(itemId || Object.keys(initial.plot).length ? null : EMPTY_CHART);
  const [json, setJson] = useState(() => JSON.stringify(initial.plot, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [view, setView] = useState<"builder" | "yaml">("builder");
  const [scope, setScope] = useState<LibraryScope>("project");
  const [savedKey, setSavedKey] = useState<string | null>(itemId ? docKey(tidyDoc(initial)) : null);
  const root = useRef<HTMLDivElement>(null);
  const save = useSaveDoc(item.data);

  const [sourceId] = splitUseRef(doc.source_recipe);
  const entry = types.plots.find((p) => p.id === sourceId);
  const example = useChartExample(sourceId, entry?.version ?? "1.0.0", Boolean(entry));
  const datasets = useMemo(() => (example.data ? columnsOf(example.data.datasets) : []), [example.data]);
  const starters = useMemo(() => chartStarters(datasets), [datasets]);
  const ds = datasets.find((d) => d.name === simple?.dataset);

  // A new template takes the first starting point once the example is known.
  useEffect(() => {
    if (simple && !simple.dataset && starters[0]) setSimple(starters[0].chart);
  }, [simple, starters]);
  // The simple editor writes the spec.
  useEffect(() => {
    if (!simple || !simple.dataset) return;
    const plot = simpleSpec(simple, datasets);
    setDoc((d) => (docKey(d.plot) === docKey(plot) ? d : { ...d, plot }));
    setJson(JSON.stringify(plot, null, 2));
  }, [simple, datasets]);

  const tidy = useMemo(() => tidyDoc(doc), [doc]);
  const debounced = useDebounced(tidy, 300);
  const validation = useLibraryValidation(debounced as unknown as JsonObject);
  const issues = validation.data?.issues;
  const dirty = docKey(tidy) !== savedKey;
  const errors = (issues ?? []).filter((i) => (i.severity ?? "error") === "error").length;
  const hasPlot = Object.keys(doc.plot).length > 0;
  const set = (patch: Partial<SimpleChart>) => simple && setSimple({ ...simple, ...patch });

  const doSave = async () => {
    try {
      const saved = await save.save(doc, { scope });
      setSavedKey(docKey(tidy));
      if (!itemId) onCreated?.(saved);
    } catch (e) {
      if (isStaleRevision(e)) void item.refetch();
    }
  };

  return (
    <div ref={root} className="grid gap-6">
      <div className="flex flex-wrap gap-2" role="group" aria-label="How to edit">
        <ToggleChip pressed={view === "builder"} onClick={() => setView("builder")}>
          Builder
        </ToggleChip>
        <ToggleChip pressed={view === "yaml"} onClick={() => setView("yaml")}>
          YAML
        </ToggleChip>
      </div>
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(22rem,34rem)]">
        <div className="grid min-w-0 grid-cols-[minmax(0,1fr)] content-start gap-5">
          {view === "yaml" ? (
            <YamlTab
              kind="chart_template"
              doc={doc}
              issues={issues}
              onDoc={(d: LibraryDoc) => {
                if (!isChartTemplateDoc(d)) return;
                setSimple(null);
                setDoc(d);
                setJson(JSON.stringify(d.plot, null, 2));
              }}
            />
          ) : (
            <>
              <BuilderSection n={1} title="Start from a chart type" hint="Your template restyles this chart's data. It works on every run and model that has this chart." done={Boolean(entry)}>
                <Field label="Chart type" htmlFor={`${id}-source`} error={issueText(issues, "/source_recipe")} className="max-w-lg">
                  <NativeSelect
                    id={`${id}-source`}
                    data-pointer="/source_recipe"
                    value={sourceId}
                    disabled={types.isPending}
                    onChange={(e) => {
                      const next = types.plots.find((p) => p.id === e.target.value);
                      setDoc({ ...doc, source_recipe: next ? `${next.id}@^${(next.version ?? "1").split(".")[0]}` : "", plot: {} });
                      setSimple(EMPTY_CHART);
                    }}
                  >
                    <option value="">{types.isPending ? "Loading the catalog…" : "Pick a chart type"}</option>
                    {sourceId && !entry && !types.isPending ? <option value={sourceId}>{sourceId} (not in this catalog)</option> : null}
                    {types.plots.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.title} ({p.id})
                      </option>
                    ))}
                  </NativeSelect>
                </Field>
                {entry?.about?.summary_plain ?? entry?.summary ? <p className="mt-3 max-w-[70ch] text-muted-foreground">{entry?.about?.summary_plain ?? entry?.summary}</p> : null}
                {entry && example.data === null ? <p className="mt-2 text-sm text-warning">This chart type has no example data, so there is nothing to preview on. Pick another one.</p> : null}
                {datasets.length ? (
                  <p className="mt-3 text-xs text-muted-foreground">
                    Its data: {datasets.map((d) => `${d.name} (${d.columns.map((c) => c.name).join(", ")})`).join("; ")}
                  </p>
                ) : null}
              </BuilderSection>

              {entry && starters.length ? (
                <BuilderSection n={2} title="Pick a starting point" hint="You can change everything after.">
                  <div className="grid gap-2 sm:grid-cols-2" role="group" aria-label="Starting points">
                    {starters.map((s) => {
                      const on = simple !== null && docKey(simple) === docKey(s.chart);
                      return (
                        <button
                          key={s.id}
                          type="button"
                          aria-pressed={on}
                          onClick={() => {
                            setSimple(s.chart);
                            setJsonError(null);
                          }}
                          className={cn("focus-ring grid gap-1 rounded-xl border-[1.5px] p-3 text-left transition-colors", on ? "border-primary bg-accent" : "border-transparent bg-background hover:border-border-strong")}
                        >
                          <span className="display flex items-center gap-2 text-base font-semibold">
                            <Wand2 aria-hidden className="size-4 text-attention" />
                            {s.label}
                          </span>
                          <span className="text-xs text-muted-foreground">{s.hint}</span>
                        </button>
                      );
                    })}
                  </div>
                </BuilderSection>
              ) : null}

              {entry ? (
                <BuilderSection n={3} title="Choose what goes where" hint={simple ? "Each choice redraws the preview." : "You edited the Vega-Lite spec by hand. Pick a starting point above to use these pickers again."}>
                  <fieldset disabled={!simple} className="grid gap-4 sm:grid-cols-2 disabled:opacity-60">
                    <Field label="Data" htmlFor={`${id}-ds`}>
                      <NativeSelect id={`${id}-ds`} value={simple?.dataset ?? ""} onChange={(e) => set({ ...EMPTY_CHART, mark: simple?.mark ?? "line", dataset: e.target.value })}>
                        {datasets.map((d) => (
                          <option key={d.name} value={d.name}>
                            {d.name}
                          </option>
                        ))}
                      </NativeSelect>
                    </Field>
                    <div className="grid gap-1.5">
                      <span className="text-sm font-medium">Draw it as</span>
                      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Mark">
                        {MARKS.map((m) => (
                          <ToggleChip key={m.value} pressed={simple?.mark === m.value} onClick={() => set({ mark: m.value })}>
                            {m.label}
                          </ToggleChip>
                        ))}
                      </div>
                    </div>
                    <ColumnSelect label="Across (x)" value={simple?.x ?? ""} ds={ds} onChange={(x) => set({ x })} />
                    <ColumnSelect label="Up (y)" value={simple?.y ?? ""} ds={ds} onChange={(y) => set({ y })} />
                    <ColumnSelect label="Colour by" value={simple?.color ?? ""} ds={ds} optional onChange={(color) => set({ color })} />
                    <ColumnSelect label="One small chart for each" value={simple?.facet ?? ""} ds={ds} optional onChange={(facet) => set({ facet })} />
                    <ColumnSelect label="Band from" value={simple?.bandLo ?? ""} ds={ds} optional onChange={(bandLo) => set({ bandLo })} />
                    <ColumnSelect label="Band to" value={simple?.bandHi ?? ""} ds={ds} optional onChange={(bandHi) => set({ bandHi })} />
                    <ColumnSelect label="Dots on top" value={simple?.dots ?? ""} ds={ds} optional onChange={(dots) => set({ dots })} />
                  </fieldset>
                  <details className="mt-5 rounded-xl border p-3" open={!simple && !itemId ? true : undefined}>
                    <summary className="cursor-pointer text-sm font-medium">Advanced: Vega-Lite</summary>
                    <div className="mt-3 grid gap-2">
                      <p className="text-xs text-muted-foreground">The spec may read only the chart type's datasets (data: name). Links, images from the web and custom code are removed by the safety check.</p>
                      <Textarea
                        aria-label="Vega-Lite spec as JSON"
                        data-pointer="/plot"
                        className="min-h-64 font-mono text-xs"
                        spellCheck={false}
                        value={json}
                        onChange={(e) => {
                          setJson(e.target.value);
                          try {
                            const parsed = JSON.parse(e.target.value) as unknown;
                            if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
                            if (jsonBytes(parsed) > PLOT_SPEC_MAX_BYTES) {
                              setJsonError("The spec is larger than 64 KiB. Make it smaller.");
                              return;
                            }
                            setJsonError(null);
                            setSimple(null);
                            setDoc({ ...doc, plot: parsed as JsonObject });
                          } catch {
                            setJsonError("The JSON is not correct. The preview shows your last correct spec.");
                          }
                        }}
                      />
                      {jsonError ? (
                        <p role="alert" className="text-xs font-medium text-destructive">
                          {jsonError}
                        </p>
                      ) : null}
                      <IssueLines issues={issues} pointer="/plot" />
                    </div>
                  </details>
                </BuilderSection>
              ) : null}

              <BuilderSection n={4} title="Name it and say what it shows" done={Boolean(doc.metadata.title.trim())}>
                <div className="grid gap-5">
                  <MetadataFields value={doc.metadata} onChange={(metadata) => setDoc({ ...doc, metadata })} issues={issues} locked={Boolean(itemId)} titleLabel="Chart title" titlePlaceholder="Fit with a 90% band" />
                  <Field label="Caption (optional)" htmlFor={`${id}-caption`} hint="One plain sentence under the chart: what to look at." error={issueText(issues, "/caption_plain")}>
                    <Textarea id={`${id}-caption`} data-pointer="/caption_plain" className="min-h-16" maxLength={300} value={doc.caption_plain ?? ""} onChange={(e) => setDoc({ ...doc, caption_plain: e.target.value })} />
                  </Field>
                  <ScopeChoice value={scope} onChange={setScope} tenantAllowed={!itemId && can(grants, "create", "tenant_library")} />
                </div>
              </BuilderSection>
            </>
          )}
          {issues?.length ? (
            <section aria-label="What the check found" className="rounded-2xl border bg-card p-4 [overflow-wrap:anywhere]">
              <h2 className="mb-2 text-base font-semibold">What the check found</h2>
              <IssueList
                issues={issues}
                onSelect={(i) => {
                  if (view === "yaml") setView("builder");
                  requestAnimationFrame(() => focusPointer(root.current, i.pointer));
                }}
              />
            </section>
          ) : null}
          {save.error ? <ErrorNotice error={save.error} title={isStaleRevision(save.error) ? "Someone saved this item after you opened it" : "The chart template was not saved"} /> : null}
        </div>

        <div className="grid min-w-0 content-start gap-4 xl:sticky xl:top-6">
          <ChartPreviewPanel doc={doc} enabled={Boolean(entry) && hasPlot} />
          <aside aria-label="Save the chart template" className="grid gap-3 rounded-3xl bg-block p-5 text-block-foreground">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="display text-lg font-semibold [overflow-wrap:anywhere]">{doc.metadata.title.trim() || "Your chart"}</p>
              <Chip tone="block">{errors ? `${errors} ${errors === 1 ? "thing" : "things"} to fix` : "Looks good"}</Chip>
            </div>
            <Button className={cn(PILL, "w-full")} disabled={save.isPending || (!dirty && savedKey !== null)} onClick={() => void doSave()}>
              {save.isPending ? <Spinner /> : <Save />}
              {savedKey !== null && !dirty ? "Saved" : savedKey !== null ? "Save a new version" : "Save chart template"}
            </Button>
            <p className="text-xs text-block-muted">Saved charts show on run and model pages under "Your charts", on the real data. They never appear in share links.</p>
            {simple === null && entry && starters.length ? (
              <Button variant="ghost" className={cn(PILL_SM, "justify-self-start text-block-foreground hover:bg-block-raised hover:text-block-foreground")} onClick={() => setSimple(starters[0]!.chart)}>
                <Wand2 /> Start over from a starting point
              </Button>
            ) : null}
          </aside>
        </div>
      </div>
    </div>
  );
}
