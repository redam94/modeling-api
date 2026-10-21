/**
 * Builder › Setup › Panels: the editor of one layout 2 panel, by kind. Sources are picked from
 * the source catalog (outputs, fit metrics, scorecard checks, views, accuracy numbers), and
 * each choice offers only what that source allows (its values, summaries and chart types).
 * Without a catalog (it loads, or the app could not read it) names are typed in.
 */
import type * as React from "react";

import { Checkbox } from "@/components/ui/overlays";
import { Field, Input, NativeSelect, Textarea } from "@/components/ui/primitives";

import { RECIPE_INTENTS, type AccuracyKind, type CatalogOutput, type ChartIntent, type DashboardSpecV2, type Panel, type PresetKey, type SourceCatalog } from "../types";
import { ListInput } from "./list-input";
import { issueAt, type FormIssue } from "./spec-form";
import {
  bandOf,
  bandReason,
  bindOutput,
  chartIntentsOf,
  defaultReduce,
  INTENT_WORD,
  MAX_CAPTION,
  MAX_CHART_ITEMS,
  MAX_NOTE_TEXT,
  MAX_PANEL_TITLE,
  MAX_TABLE_ROWS,
  MMM_PART_WORD,
  outputLabel,
  outputOf,
  PRESET_RULES,
  type Band,
  type ChartPanel,
  type KpiPanel,
  type MmmPart,
  type PanelSource,
  type ReduceHow,
} from "./spec-form-v2";

type AccuracyMetric = Extract<PanelSource, { kind: "accuracy" }>["metric"];
type ViewName = Extract<PanelSource, { kind: "view" }>["view"];
type Stat = NonNullable<KpiPanel["stat"]>;

const STAT_WORD: Record<Stat, string> = { centre: "The middle value", low: "The low end of the range", high: "The high end of the range", value: "The value" };
const REDUCE_WORD: Record<ReduceHow, string> = {
  none: "As it is",
  select: "Chosen cells only",
  last: "The last period",
  next: "The next period",
  mean: "The average over…",
  sum: "The total over…",
  share_above: "The share above a value",
  top_class: "The most likely class",
};
const BAND_WORD: Record<Band, string> = { outcome: "Where one new result will probably be", mean: "Where the average will probably be", none: "No range" };
const VIEW_WORD: Record<ViewName, string> = { forecast_total: "The total over the forecast", counterfactual_delta: "The difference to the result without the change", conditioning: "The forecast updated with the sent numbers" };
const SOURCE_WORD: Record<PanelSource["kind"], string> = { output: "A model output", metric: "A metric of the fit", scorecard: "A check of the scorecard", view: "A forecast or effect total", accuracy: "Accuracy of the sent numbers" };
export const ACCURACY_WORD: Record<AccuracyMetric, string> = {
  n: "Number of compared values",
  mae: "Typical error (MAE)",
  rmse: "Typical error (RMSE)",
  mape: "Typical error in percent (MAPE)",
  wape: "Error as a share of the total (WAPE)",
  smape: "Symmetric error in percent (sMAPE)",
  bias: "Bias (predicted minus actual)",
  r2: "Share of variation explained (R²)",
  band_hit_rate: "Results inside the range",
  accuracy: "How often the model is right",
  brier: "Probability error (Brier)",
  log_score: "Log score",
  auc: "How well the model ranks cases (AUC)",
  effect_total: "Total effect",
  effect_relative: "Effect in percent",
};
const ERROR_METRICS: AccuracyMetric[] = ["n", "mae", "rmse", "mape", "wape", "smape", "bias", "r2", "band_hit_rate"];
const CLASS_METRICS: AccuracyMetric[] = ["n", "accuracy", "brier", "log_score", "auc"];
const EFFECT_METRICS: AccuracyMetric[] = ["effect_total", "effect_relative"];

/** The accuracy numbers a preset's uploads give (`accuracy_kind`). */
export function accuracyKindOf(preset: PresetKey): AccuracyKind | null {
  if (preset === "classifier" || preset === "image") return "classification";
  if (preset === "causal_impact") return "effect";
  return preset === "generic" ? null : "error";
}

function accuracyMetrics(preset: PresetKey): AccuracyMetric[] {
  const k = accuracyKindOf(preset);
  if (k === "classification") return CLASS_METRICS;
  if (k === "effect") return EFFECT_METRICS;
  if (k === "error") return ERROR_METRICS;
  return [...ERROR_METRICS, ...CLASS_METRICS.slice(1), ...EFFECT_METRICS];
}

function viewsOf(catalog: SourceCatalog | null | undefined): ViewName[] {
  if (!catalog) return ["forecast_total", "counterfactual_delta", "conditioning"];
  const out: ViewName[] = [];
  if ((catalog.score_modes ?? []).includes("forecast") || (catalog.views ?? []).includes("forecast")) out.push("forecast_total");
  if ((catalog.views ?? []).includes("counterfactual")) out.push("counterfactual_delta");
  if (catalog.conditions_on_actuals) out.push("conditioning");
  return out;
}

export interface PanelEditorProps {
  spec: DashboardSpecV2;
  index: number;
  panel: Panel;
  onChange: (p: Panel) => void;
  issues: readonly FormIssue[];
  catalog: SourceCatalog | null | undefined;
}

export function PanelEditor({ spec, index, panel: p, onChange, issues, catalog }: PanelEditorProps) {
  const at = `/panels/${index}`;
  const fid = (k: string) => `pe-${index}-${k}`;
  const err = (k: string, deep = false) => issueAt(issues, `${at}/${k}`, deep);
  return (
    <div className="grid gap-4" data-panel-editor={p.id}>
      <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Field label="Title" htmlFor={fid("title")} error={err("title")}>
          <Input id={fid("title")} maxLength={MAX_PANEL_TITLE} value={p.title} onChange={(e) => onChange({ ...p, title: e.target.value })} />
        </Field>
        <Field label="Id" htmlFor={fid("id")} error={err("id")} hint="Only your team sees it. Lowercase letters, digits and _.">
          <Input id={fid("id")} className="font-mono" maxLength={32} value={p.id} onChange={(e) => onChange({ ...p, id: e.target.value.trim() })} />
        </Field>
      </div>
      <Field label="Caption (optional)" htmlFor={fid("caption")} error={err("caption")} hint="A short line under the title.">
        <Input id={fid("caption")} maxLength={MAX_CAPTION} value={p.caption ?? ""} onChange={(e) => onChange({ ...p, caption: e.target.value })} />
      </Field>
      {p.kind === "kpi" ? <KpiFields spec={spec} p={p} onChange={onChange} catalog={catalog} fid={fid} err={err} /> : null}
      {p.kind === "chart" ? <ChartFields spec={spec} p={p} onChange={onChange} catalog={catalog} fid={fid} err={err} /> : null}
      {p.kind === "table" ? (
        <>
          <OutputPicker id={fid("output")} label="Output" value={p.source.variable} catalog={catalog} error={err("source/variable")} onChange={(v) => onChange({ ...p, source: { ...p.source, variable: v } })} />
          <div className="grid gap-4 sm:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <Field
              label="Columns (optional)"
              htmlFor={fid("columns")}
              error={err("columns")}
              hint={`Dims and values, in order, separated by commas.${outputOf(catalog, p.source.variable)?.dims?.length ? ` Dims: ${outputOf(catalog, p.source.variable)!.dims!.join(", ")}.` : ""} Values: centre, low, high.`}
            >
              <ListInput id={fid("columns")} className="font-mono" value={p.columns ?? []} onChange={(columns) => onChange({ ...p, columns })} />
            </Field>
            <Field label="Rows" htmlFor={fid("rows")} error={err("max_rows")}>
              <Input id={fid("rows")} type="number" min={1} max={MAX_TABLE_ROWS} step={1} value={p.max_rows ?? 20} onChange={(e) => onChange({ ...p, max_rows: Number(e.target.value) })} />
            </Field>
          </div>
        </>
      ) : null}
      {p.kind === "note" ? (
        <Field label="Text" htmlFor={fid("text")} error={err("text")} hint={`Plain text, at most ${MAX_NOTE_TEXT} characters. The client sees it as you type it.`}>
          <Textarea id={fid("text")} maxLength={MAX_NOTE_TEXT} value={p.text} onChange={(e) => onChange({ ...p, text: e.target.value })} />
        </Field>
      ) : null}
      {p.kind === "recipe" ? <RecipeFields p={p} onChange={onChange} catalog={catalog} fid={fid} err={err} /> : null}
      {p.kind === "mmm" ? (
        <Field label="Planner part" htmlFor={fid("part")} error={err("part")}>
          <NativeSelect id={fid("part")} value={p.part} onChange={(e) => onChange({ ...p, part: e.target.value as MmmPart })}>
            {(Object.keys(MMM_PART_WORD) as MmmPart[]).map((k) => (
              <option key={k} value={k}>
                {MMM_PART_WORD[k]}
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : null}
    </div>
  );
}

interface FieldsProps<P> {
  p: P;
  onChange: (p: Panel) => void;
  catalog: SourceCatalog | null | undefined;
  fid: (k: string) => string;
  err: (k: string, deep?: boolean) => string | undefined;
}

function OutputPicker({ id, label, value, catalog, error, onChange, filter }: { id: string; label: string; value: string; catalog: SourceCatalog | null | undefined; error?: string | undefined; onChange: (v: string, o: CatalogOutput | null) => void; filter?: (o: CatalogOutput) => boolean }) {
  const outs = (catalog?.outputs ?? []).filter((o) => !filter || filter(o) || o.name === value);
  if (!catalog?.outputs?.length)
    return (
      <Field label={label} htmlFor={id} error={error} hint="The model output's name, as the score summary names it.">
        <Input id={id} className="font-mono" value={value} onChange={(e) => onChange(e.target.value.trim(), null)} />
      </Field>
    );
  return (
    <Field label={label} htmlFor={id} error={error}>
      <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value, outputOf(catalog, e.target.value))}>
        {outs.some((o) => o.name === value) ? null : <option value={value}>{value || "Choose an output"}</option>}
        {outs.map((o) => (
          <option key={o.name} value={o.name}>
            {outputLabel(o.name)} ({o.name})
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}

function SummaryFields({ p, o, onChange, fid, err }: { p: KpiPanel | ChartPanel; o: CatalogOutput | null; onChange: (p: Panel) => void; fid: (k: string) => string; err: (k: string) => string | undefined }) {
  const r = p.reduce ?? {};
  const how = r.how ?? "none";
  const reduces = (o?.reduces?.length ? o.reduces : (Object.keys(REDUCE_WORD) as ReduceHow[])) as ReduceHow[];
  const set = (patch: Partial<NonNullable<KpiPanel["reduce"]>>) => onChange({ ...p, reduce: { ...r, ...patch } });
  const dims = o?.dims ?? [];
  return (
    <>
      <Field label="Summary" htmlFor={fid("reduce")} error={err("reduce/how")}>
        <NativeSelect
          id={fid("reduce")}
          value={how}
          onChange={(e) => {
            const next = e.target.value as ReduceHow;
            set({ how: next, over: next === "mean" || next === "sum" ? (r.over?.length ? r.over : dims.slice(0, 1)) : [], threshold: next === "share_above" ? (r.threshold ?? 0) : null });
          }}
        >
          {(reduces.includes(how) ? reduces : [how, ...reduces]).map((k) => (
            <option key={k} value={k}>
              {REDUCE_WORD[k]}
            </option>
          ))}
        </NativeSelect>
      </Field>
      {how === "mean" || how === "sum" ? (
        <fieldset className="grid gap-1.5">
          <legend className="text-sm font-medium">{how === "sum" ? "Total over" : "Average over"}</legend>
          {dims.length ? (
            dims.map((dim) => {
              const on = (r.over ?? []).includes(dim);
              return (
                <label key={dim} className="flex items-center gap-2 text-sm">
                  <Checkbox checked={on} onCheckedChange={() => set({ over: on ? (r.over ?? []).filter((x) => x !== dim) : [...(r.over ?? []), dim].slice(0, 4) })} />
                  <span className="font-mono">{dim}</span>
                </label>
              );
            })
          ) : (
            <ListInput aria-label="Dims to reduce over" className="font-mono" value={r.over ?? []} onChange={(over) => set({ over })} />
          )}
          {err("reduce/over") ? (
            <p role="alert" className="text-xs font-medium text-destructive">
              {err("reduce/over")}
            </p>
          ) : null}
        </fieldset>
      ) : null}
      {how === "share_above" ? (
        <Field label="Count the share above" htmlFor={fid("threshold")} error={err("reduce/threshold")}>
          <Input id={fid("threshold")} type="number" step="any" value={r.threshold ?? ""} onChange={(e) => set({ threshold: e.target.value === "" ? null : Number(e.target.value) })} />
        </Field>
      ) : null}
      <BandField p={p} o={o} onChange={onChange} fid={fid} err={err} />
    </>
  );
}

function BandField({ p, o, onChange, fid, err }: { p: KpiPanel | ChartPanel; o: CatalogOutput | null; onChange: (p: Panel) => void; fid: (k: string) => string; err: (k: string) => string | undefined }) {
  const band = p.band ?? "outcome";
  const reasons = (Object.keys(BAND_WORD) as Band[]).map((b) => [b, bandReason(o, b)] as const);
  return (
    <Field label="Range" htmlFor={fid("band")} error={err("band")} hint={reasons.find(([b]) => b === "outcome")?.[1] ?? "The likely range of one new result is wider than the range of the average."}>
      <NativeSelect id={fid("band")} value={band} onChange={(e) => onChange({ ...p, band: e.target.value as Band })}>
        {reasons.map(([b, why]) => (
          <option key={b} value={b} disabled={Boolean(why) && b !== band}>
            {BAND_WORD[b]}
            {why ? " (not for this output)" : ""}
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}

function defaultSource(kind: PanelSource["kind"], spec: DashboardSpecV2, catalog: SourceCatalog | null | undefined): PanelSource {
  switch (kind) {
    case "output":
      return { kind, variable: outputOf(catalog, spec.subject.variable)?.name ?? catalog?.outputs?.[0]?.name ?? spec.subject.variable };
    case "metric": {
      const m = catalog?.metrics?.[0];
      return { kind, key: m?.key ?? "rmse", labels: { ...(m?.labels ?? {}) } };
    }
    case "scorecard":
      return { kind, check: catalog?.scorecard?.[0] ?? "" };
    case "view":
      return { kind, view: viewsOf(catalog)[0] ?? "forecast_total" };
    case "accuracy":
      return { kind, metric: accuracyMetrics(spec.preset)[1] ?? "mae" };
  }
}

function KpiFields({ spec, p, onChange, catalog, fid, err }: FieldsProps<KpiPanel> & { spec: DashboardSpecV2 }) {
  const src = p.source;
  const o = src.kind === "output" ? outputOf(catalog, src.variable) : null;
  const kinds = (Object.keys(SOURCE_WORD) as PanelSource["kind"][]).filter(
    (k) => k === src.kind || !catalog || (k === "output" && catalog.outputs?.length) || (k === "metric" && catalog.metrics?.length) || (k === "scorecard" && catalog.scorecard?.length) || (k === "view" && viewsOf(catalog).length) || k === "accuracy",
  );
  const setKind = (k: PanelSource["kind"]) => {
    const source = defaultSource(k, spec, catalog);
    if (source.kind === "output") {
      const bound = outputOf(catalog, source.variable);
      onChange(bound ? bindOutput({ ...p, stat: "centre" }, bound) : { ...p, source, stat: "centre", reduce: { how: "none", over: [] } });
    } else onChange({ ...p, source, stat: "value", reduce: { how: "none", over: [] }, band: k === "view" ? bandOf(outputOf(catalog, spec.subject.variable)) : "none", compare: k === "accuracy" ? "previous" : p.compare });
  };
  const metricIndex = src.kind === "metric" ? (catalog?.metrics ?? []).findIndex((m) => m.key === src.key && JSON.stringify(m.labels ?? {}) === JSON.stringify(src.labels ?? {})) : -1;
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Shows" htmlFor={fid("source")}>
        <NativeSelect id={fid("source")} value={src.kind} onChange={(e) => setKind(e.target.value as PanelSource["kind"])}>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {SOURCE_WORD[k]}
            </option>
          ))}
        </NativeSelect>
      </Field>
      {src.kind === "output" ? (
        <>
          <OutputPicker id={fid("output")} label="Output" value={src.variable} catalog={catalog} error={err("source/variable")} onChange={(v, bound) => onChange(bound ? bindOutput(p, bound) : { ...p, source: { ...src, variable: v } })} />
          <Field label="Value" htmlFor={fid("stat")} error={err("stat")}>
            <NativeSelect id={fid("stat")} value={p.stat ?? "centre"} onChange={(e) => onChange({ ...p, stat: e.target.value as Stat })}>
              {((o?.stats?.length ? o.stats : ["centre", "low", "high"]) as Stat[]).filter((s) => s !== "value").map((s) => (
                <option key={s} value={s}>
                  {STAT_WORD[s]}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <SummaryFields p={p} o={o} onChange={onChange} fid={fid} err={err} />
        </>
      ) : null}
      {src.kind === "metric" ? (
        catalog?.metrics?.length ? (
          <Field label="Metric" htmlFor={fid("metric")} error={err("source/key")} hint="The value that the fit run logged.">
            <NativeSelect
              id={fid("metric")}
              value={metricIndex}
              onChange={(e) => {
                const m = catalog.metrics![Number(e.target.value)];
                if (m) onChange({ ...p, source: { kind: "metric", key: m.key, labels: { ...(m.labels ?? {}) } } });
              }}
            >
              {metricIndex < 0 ? <option value={-1}>{src.key}</option> : null}
              {catalog.metrics.map((m, i) => (
                <option key={i} value={i}>
                  {m.key}
                  {Object.keys(m.labels ?? {}).length ? ` (${Object.entries(m.labels ?? {}).map(([k, v]) => `${k} ${v}`).join(", ")})` : ""}
                  {m.last != null ? ` = ${Number(m.last.toPrecision(4))}` : ""}
                </option>
              ))}
            </NativeSelect>
          </Field>
        ) : (
          <Field label="Metric" htmlFor={fid("metric")} error={err("source/key")}>
            <Input id={fid("metric")} className="font-mono" value={src.key} onChange={(e) => onChange({ ...p, source: { ...src, key: e.target.value.trim() } })} />
          </Field>
        )
      ) : null}
      {src.kind === "scorecard" ? (
        <Field label="Check" htmlFor={fid("check")} error={err("source/check")}>
          <NativeSelect id={fid("check")} value={src.check} onChange={(e) => onChange({ ...p, source: { kind: "scorecard", check: e.target.value } })}>
            {(catalog?.scorecard ?? []).includes(src.check) ? null : <option value={src.check}>{src.check || "Choose a check"}</option>}
            {(catalog?.scorecard ?? []).map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : null}
      {src.kind === "view" ? (
        <Field label="Total" htmlFor={fid("view")} error={err("source/view")}>
          <NativeSelect id={fid("view")} value={src.view} onChange={(e) => onChange({ ...p, source: { kind: "view", view: e.target.value as ViewName } })}>
            {[...new Set([src.view, ...viewsOf(catalog)])].map((v) => (
              <option key={v} value={v}>
                {VIEW_WORD[v]}
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : null}
      {src.kind === "accuracy" ? <AccuracyPicker id={fid("accuracy")} preset={spec.preset} value={src.metric} error={err("source/metric")} onChange={(m) => onChange({ ...p, source: { kind: "accuracy", metric: m } })} /> : null}
      <Field label="Compare with the previous readout" htmlFor={fid("compare")}>
        <NativeSelect id={fid("compare")} value={p.compare ?? "previous"} onChange={(e) => onChange({ ...p, compare: e.target.value as NonNullable<KpiPanel["compare"]> })}>
          <option value="previous">Show the change</option>
          <option value="none">Do not compare</option>
        </NativeSelect>
      </Field>
      <Field label="Good news is" htmlFor={fid("good")}>
        <NativeSelect id={fid("good")} value={p.good ?? "up"} onChange={(e) => onChange({ ...p, good: e.target.value as NonNullable<KpiPanel["good"]> })}>
          <option value="up">A higher value</option>
          <option value="down">A lower value</option>
          <option value="none">Neither</option>
        </NativeSelect>
      </Field>
    </div>
  );
}

function AccuracyPicker({ id, preset, value, error, onChange }: { id: string; preset: PresetKey; value: AccuracyMetric; error?: string | undefined; onChange: (m: AccuracyMetric) => void }) {
  const metrics = accuracyMetrics(preset);
  return (
    <Field label="Accuracy number" htmlFor={id} error={error} hint="It shows after the client sends the actual numbers.">
      <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value as AccuracyMetric)}>
        {(metrics.includes(value) ? metrics : [value, ...metrics]).map((m) => (
          <option key={m} value={m}>
            {ACCURACY_WORD[m]}
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
}

function ChartFields({ spec, p, onChange, catalog, fid, err }: FieldsProps<ChartPanel> & { spec: DashboardSpecV2 }) {
  const src = p.source;
  const o = src.kind === "output" ? outputOf(catalog, src.variable) : null;
  const history = PRESET_RULES[spec.preset].intents.includes("accuracy_history");
  const intents: ChartIntent[] = [...chartIntentsOf(spec.preset, o), ...(history ? (["accuracy_history"] as const) : [])];
  const setIntent = (intent: ChartIntent) => {
    if (intent === "accuracy_history") return onChange({ ...p, intent, source: { kind: "accuracy", metric: accuracyMetrics(spec.preset)[1] ?? "mae" }, reduce: { how: "none", over: [] }, band: "none" });
    if (src.kind === "accuracy") {
      const first = (catalog?.outputs ?? []).find((x) => (x.intents ?? []).includes(intent)) ?? null;
      return onChange(first ? bindOutput({ ...p, intent }, first) : { ...p, intent, source: { kind: "output", variable: spec.subject.variable }, reduce: { how: defaultReduce(null), over: [] } });
    }
    onChange({ ...p, intent });
  };
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Chart" htmlFor={fid("intent")} error={err("intent")}>
        <NativeSelect id={fid("intent")} value={p.intent} onChange={(e) => setIntent(e.target.value as ChartIntent)}>
          {(intents.includes(p.intent) ? intents : [p.intent, ...intents]).map((i) => (
            <option key={i} value={i}>
              {INTENT_WORD[i]}
            </option>
          ))}
        </NativeSelect>
      </Field>
      {src.kind === "accuracy" ? (
        <AccuracyPicker id={fid("accuracy")} preset={spec.preset} value={src.metric} error={err("source/metric")} onChange={(m) => onChange({ ...p, source: { kind: "accuracy", metric: m } })} />
      ) : (
        <>
          <OutputPicker
            id={fid("output")}
            label="Output"
            value={src.variable}
            catalog={catalog}
            error={err("source/variable")}
            filter={(x) => (x.intents ?? []).includes(p.intent)}
            onChange={(v, bound) => onChange(bound ? bindOutput(p, bound) : { ...p, source: { ...src, variable: v } })}
          />
          <SummaryFields p={p} o={o} onChange={onChange} fid={fid} err={err} />
          <Field label="Items at most" htmlFor={fid("items")} error={err("max_items")} hint="Bars, lines or classes that the chart shows.">
            <Input id={fid("items")} type="number" min={1} max={MAX_CHART_ITEMS} step={1} value={p.max_items ?? 20} onChange={(e) => onChange({ ...p, max_items: Number(e.target.value) })} />
          </Field>
        </>
      )}
    </div>
  );
}

function RecipeFields({ p, onChange, catalog, fid, err }: FieldsProps<Extract<Panel, { kind: "recipe" }>>) {
  const pictures = catalog?.pictures ?? [];
  const native = p.recipe in RECIPE_INTENTS;
  const value = native ? `native:${p.recipe}` : (p.plot_id ?? "");
  const known = native || pictures.some((x) => x.plot_id === p.plot_id);
  const choose = (v: string): void => {
    if (v.startsWith("native:")) return void onChange({ ...p, recipe: v.slice(7), plot_id: null });
    const pic = pictures.find((x) => x.plot_id === v);
    if (pic) onChange({ ...p, recipe: pic.recipe, plot_id: pic.plot_id, title: p.title || pic.title.slice(0, MAX_PANEL_TITLE) });
  };
  const hint: React.ReactNode = native
    ? "The dashboard draws this plot itself from each readout."
    : "A picture of the model version's plot. The app makes it once, when you publish. The Preview tab shows the plot to your team.";
  return (
    <Field label="Plot" htmlFor={fid("plot")} error={err("plot_id") ?? err("recipe")} hint={hint}>
      <NativeSelect id={fid("plot")} value={value} onChange={(e) => choose(e.target.value)}>
        {known ? null : <option value={value}>{p.plot_id ?? p.recipe}</option>}
        {pictures.length ? (
          <optgroup label="Plots of the model version">
            {pictures.map((x) => (
              <option key={x.plot_id} value={x.plot_id}>
                {x.title || x.recipe}
              </option>
            ))}
          </optgroup>
        ) : null}
        <optgroup label="Drawn from each readout">
          {Object.entries(RECIPE_INTENTS).map(([r, intent]) => (
            <option key={r} value={`native:${r}`}>
              {INTENT_WORD[intent]} ({r})
            </option>
          ))}
        </optgroup>
      </NativeSelect>
    </Field>
  );
}
