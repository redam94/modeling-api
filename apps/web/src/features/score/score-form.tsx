/**
 * The score request, as guided steps: what to do (the modes the version supports, in plain
 * words), how far (a forecast's periods), the values to score - typed into a grid built from
 * the model's inputs (`GET …/inputs`), a dataset, or nothing new - and what-if changes. The
 * rarely changed settings (entry stage, side tables, output, seed) sit under Advanced.
 */
import { Link } from "@tanstack/react-router";
import { CheckCircle2, Gauge, Plus, Trash2, TriangleAlert } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, newIdempotencyKey } from "@/api/client";
import { scoreModel, useDatasets } from "@/api/queries";
import type { ModelInputSchema, ModelVersion, ScoreCall, ScoreCallResult } from "@/api/types";
import { ErrorNotice, Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { freqUnit, gridColumns, gridToInline, inputVars, plural, seedRows, summarize, targets } from "./inputs";
import {
  buildScoreRequest,
  defaultScoreForm,
  asRepresentation,
  REPRESENTATIONS,
  SUMMARY_OPTIONS,
  type FormProblem,
  type FutureCovariates,
  type InputSource,
  type InterventionOp,
  type ScoreForm,
  type ScoreMode,
  type SideTableOption,
} from "./request";
import { ValuesGrid } from "./values-grid";

const ALL_MODES: ScoreMode[] = ["predict", "forecast", "counterfactual", "simulate", "filter", "evaluate"];

/** Problems in fields under Advanced (it opens to show them). */
const ADVANCED_FIELDS = /^(inputsStage|scenario|sideTables\.|summary|draws|vars|seed)/;

/** Modes that read a dataset only (the server refuses inline data for them). */
const DATASET_ONLY: ScoreMode[] = ["filter", "evaluate"];

const FUTURE_OPTIONS: { value: FutureCovariates; label: string; hint: string }[] = [
  { value: "carry_last", label: "Repeat the last value", hint: "Blank future values repeat each series' latest value." },
  { value: "seasonal_naive", label: "Same period a year earlier", hint: "Blank future values copy the value one season before." },
  { value: "zero", label: "Zero (nothing planned)", hint: "Blank future values are zero, e.g. no spend planned." },
  { value: "supplied", label: "None: I give every value", hint: "Every future value must be typed or in the dataset; a gap is an error." },
];

function problemFor(problems: FormProblem[], field: string): string | undefined {
  return problems.find((p) => p.field === field)?.message;
}

function datasetRefs(items: { id: string; ref: string; latest_version?: number | null }[]): string[] {
  return items.map((d) => (d.latest_version ? `${d.ref}@v${d.latest_version}` : d.ref));
}

interface ModeText {
  title: string;
  detail: string;
}

function modeText(mode: ScoreMode, inputs: ModelInputSchema | null, periods: string): ModeText {
  const t = inputs ? targets(inputs.snapshot) : [];
  const what = t.length ? t.join(", ") : "the outputs";
  const hdim = inputs?.snapshot.horizon_dim ?? null;
  const unit = hdim ? freqUnit(inputs?.snapshot.fit_coords?.[hdim]?.freq ?? inputs?.snapshot.input_schema.dims?.[hdim]?.freq) : null;
  const n = Number(periods);
  switch (mode) {
    case "forecast":
      return {
        title: "Forecast",
        detail: `${what} for the next ${Number.isInteger(n) && n > 0 && unit ? plural(n, unit) : "periods"} after the fitted history.`,
      };
    case "predict":
      return { title: "Predict", detail: `${what} for the input values you give (in-sample when you give none).` };
    case "counterfactual":
      return { title: "What if", detail: `${what} with changed inputs, compared with the inputs as they are.` };
    case "simulate":
      return { title: "Simulate", detail: `Synthetic ${what} drawn from the model.` };
    case "filter":
      return { title: "Update with new data", detail: "Update the model's latest state with new observations (state-space models; a dataset)." };
    case "evaluate":
      return { title: "Evaluate", detail: "Score held-out data and compute accuracy metrics (a dataset)." };
  }
}

function Step({ n, title, children, hint }: { n: number; title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="grid gap-3" aria-label={title}>
      <div className="flex items-baseline gap-2">
        <span className="inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground" aria-hidden>
          {n}
        </span>
        <h3 className="text-[13px] font-semibold">{title}</h3>
      </div>
      {hint ? <p className="-mt-1 text-xs text-muted-foreground">{hint}</p> : null}
      <div className="grid gap-3 pl-7">{children}</div>
    </section>
  );
}

export function ScoreFormView({
  mv,
  inputs,
  inputsError,
  stages,
  sideTables,
  onResult,
}: {
  mv: ModelVersion;
  /** The version's inputs; null while loading or when the version has none stored. */
  inputs: ModelInputSchema | null;
  inputsError: unknown;
  stages: string[];
  sideTables: SideTableOption[];
  onResult: (r: { result: ScoreCallResult; key: string; body: ScoreCall }) => void;
}) {
  const [form, setForm] = useState<ScoreForm>(defaultScoreForm);
  const [problems, setProblems] = useState<FormProblem[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const [focusInvalid, setFocusInvalid] = useState(0);
  useEffect(() => {
    if (focusInvalid) formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [focusInvalid]);
  const datasets = useDatasets();
  const refs = useMemo(() => datasetRefs(datasets.data?.items ?? []), [datasets.data]);
  const set = <K extends keyof ScoreForm>(k: K, v: ScoreForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const setOut = <K extends keyof ScoreForm["output"]>(k: K, v: ScoreForm["output"][K]) => setForm((f) => ({ ...f, output: { ...f.output, [k]: v } }));

  // Supplied side tables default to the version the fit used (it usually covers the horizon).
  useEffect(() => {
    const defaults = Object.fromEntries(sideTables.filter((t) => t.policy !== "pinned").map((t) => [t.name, t.ref]));
    setForm((f) => ({ ...f, sideTables: { ...defaults, ...f.sideTables } }));
  }, [sideTables]);

  const modes = useMemo(() => {
    const declared = (inputs?.snapshot.score_modes ?? mv.capabilities?.score_modes ?? ALL_MODES) as ScoreMode[];
    return ALL_MODES.filter((m) => declared.includes(m));
  }, [inputs, mv.capabilities]);
  useEffect(() => {
    if (!modes.includes(form.mode) && modes[0]) setForm((f) => ({ ...f, mode: modes.includes("forecast") ? "forecast" : (modes[0] as ScoreMode) }));
  }, [modes, form.mode]);

  // Summaries and draws follow how the model represents its output (quantile models have no
  // mean; only draw-based models return draws).
  const representation = asRepresentation(inputs?.snapshot.representation ?? mv.capabilities?.representation);
  useEffect(() => {
    if (!representation) return;
    const rules = REPRESENTATIONS[representation];
    setForm((f) => ({
      ...f,
      output: {
        ...f.output,
        summary: f.output.summary.every((x) => rules.options.includes(x)) && f.output.summary.length ? f.output.summary : rules.defaults,
        draws: rules.draws ? f.output.draws : "0",
      },
    }));
  }, [representation]);

  // The forecast extends the model's own horizon dim.
  const hdim = inputs?.snapshot.horizon_dim ?? null;
  useEffect(() => {
    if (hdim) setForm((f) => ({ ...f, horizonDim: hdim }));
  }, [hdim]);

  // Typed values need the inputs; dataset-only modes need a dataset.
  const canType = Boolean(inputs?.input_table?.columns?.length) && !DATASET_ONLY.includes(form.mode) && form.inputsStage === (inputs?.snapshot.inputs_stage ?? "raw");
  useEffect(() => {
    // Fall back once the inputs are known (or failed to load).
    if (form.source === "typed" && !canType && (inputs !== null || inputsError)) setForm((f) => ({ ...f, source: DATASET_ONLY.includes(f.mode) ? "dataset" : f.mode === "forecast" ? "none" : "dataset" }));
    if (form.source === "none" && DATASET_ONLY.includes(form.mode)) setForm((f) => ({ ...f, source: "dataset" }));
  }, [canType, form.source, form.mode, inputs, inputsError]);

  // The grid: seeded from the inputs for the mode and periods until the user edits it.
  const columns = useMemo(() => (inputs ? gridColumns(inputs, form.mode) : []), [inputs, form.mode]);
  const periods = Number(form.horizonPeriods);
  const seed = useMemo(
    () => (inputs ? seedRows(inputs, columns, form.mode, Number.isInteger(periods) && periods > 0 ? Math.min(periods, 200) : 0) : []),
    [inputs, columns, form.mode, periods],
  );
  const [rows, setRows] = useState<string[][]>([]);
  const [edited, setEdited] = useState(false);
  useEffect(() => {
    if (!edited) setRows(seed);
  }, [seed, edited]);
  // A different set of columns (another mode) cannot keep the old cells.
  useEffect(() => setEdited(false), [columns]);
  const summary = summarize(columns, rows);

  const vars = inputs ? inputVars(inputs.snapshot) : [];
  const futureVars = vars.filter((v) => v.future && v.role !== "target").map((v) => v.name);
  const dims = Object.keys(inputs?.snapshot.input_schema.dims ?? {});
  const levelHint = dims
    .filter((d) => d !== hdim)
    .map((d) => {
      const values = inputs?.snapshot.fit_coords?.[d]?.values ?? [];
      return values.length ? `${d}: ${values.slice(0, 6).map(String).join(", ")}${values.length > 6 ? ", …" : ""}` : d;
    })
    .join("; ");
  const blankDrivers =
    form.mode !== "forecast" ? "" : FUTURE_OPTIONS.find((o) => o.value === form.futureCovariates)?.label.toLowerCase() ?? "";

  // What will be sent, in one line; and what is missing.
  const readiness = (() => {
    const notes: { ok: boolean; text: string }[] = [];
    if (form.source === "typed") {
      if (summary.filled) notes.push({ ok: true, text: `${summary.filled} typed value${summary.filled === 1 ? "" : "s"} over ${plural(summary.rows, "row")}` });
      else if (form.mode === "forecast") notes.push({ ok: true, text: "No values typed: the forecast fills future inputs as chosen below" });
      else notes.push({ ok: false, text: "Type at least one value, or score without new data" });
    } else if (form.source === "dataset") {
      notes.push(form.data.trim() ? { ok: true, text: `Data from ${form.data.trim()}` } : { ok: false, text: "Choose a dataset" });
    } else {
      notes.push({ ok: true, text: form.mode === "forecast" ? "No new data: forecast from the end of the fit" : "No new data: in-sample" });
    }
    if (form.mode === "forecast" && futureVars.length && form.futureCovariates === "supplied") {
      const missing = form.source === "typed" ? futureVars.filter((v) => summary.emptyColumns.includes(v) || summary.partialDrivers.includes(v)) : form.source === "none" ? futureVars : [];
      if (missing.length) notes.push({ ok: false, text: `Every future value of ${missing.join(", ")} is needed (or choose how to fill blanks)` });
    }
    if (form.mode === "counterfactual" && !form.interventions.length && !form.scenario.trim()) notes.push({ ok: false, text: "Add a change to compare against" });
    return notes;
  })();

  const submit = async () => {
    const built = buildScoreRequest(form, mv.ref, representation);
    const extra: FormProblem[] = [];
    let inline: ScoreCall["inline"] = null;
    if (form.source === "typed" && canType) {
      try {
        const data = gridToInline(columns, rows, hdim);
        if (data) inline = { data: data };
        else if (form.mode !== "forecast") extra.push({ field: "values", message: "Type at least one value, or choose “No new data”." });
      } catch (e) {
        extra.push({ field: "values", message: e instanceof Error ? e.message : String(e) });
      }
    }
    if (form.source === "dataset" && !form.data.trim()) extra.push({ field: "data", message: "Choose the dataset to score." });
    const all = [...("problems" in built ? built.problems : []), ...extra];
    if (all.length || "problems" in built) {
      setProblems(all);
      setFocusInvalid((n) => n + 1);
      return;
    }
    setProblems([]);
    setError(null);
    setSubmitting(true);
    const key = newIdempotencyKey();
    try {
      const body: ScoreCall = { request: built.request, ...(inline ? { inline } : {}) };
      const { result } = await scoreModel(mv.model_id, mv.version, body, { key, waitSeconds: 30 });
      onResult({ result, key, body });
    } catch (e) {
      setError(e);
    } finally {
      setSubmitting(false);
    }
  };

  const valuesProblem = problemFor(problems, "values");
  const inputsMissing = inputsError instanceof ApiError && inputsError.code === "CAPABILITY_UNSUPPORTED";

  return (
    <form
      ref={formRef}
      className="grid gap-6"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <Step n={1} title="What do you want to do?">
        <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Score mode">
          {modes.map((m) => {
            const text = modeText(m, inputs, form.horizonPeriods);
            const on = form.mode === m;
            return (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={on}
                data-mode={m}
                onClick={() => set("mode", m)}
                className={cn("grid gap-0.5 rounded-lg border p-3 text-left transition-colors hover:bg-accent/50", on && "border-primary bg-primary/6 ring-1 ring-primary/40")}
              >
                <span className="text-[13px] font-semibold">{text.title}</span>
                <span className="text-xs text-muted-foreground">{text.detail}</span>
              </button>
            );
          })}
        </div>
      </Step>

      {form.mode === "forecast" ? (
        <Step n={2} title="How far ahead?">
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Periods" htmlFor="h-periods" error={problemFor(problems, "horizonPeriods")}>
              <Input id="h-periods" inputMode="numeric" value={form.horizonPeriods} onChange={(e) => set("horizonPeriods", e.target.value)} className="w-24" />
            </Field>
            {hdim ? (
              <p className="pb-2 text-xs text-muted-foreground">
                {(() => {
                  const fit = inputs?.snapshot.fit_coords?.[hdim];
                  const unit = freqUnit(fit?.freq);
                  return (
                    <>
                      {unit ? `${unit}s` : "periods"} of <Mono>{hdim}</Mono>
                      {fit?.end !== undefined && fit.end !== null ? <> after the fitted end, {String(fit.end)}</> : null}
                    </>
                  );
                })()}
              </p>
            ) : (
              <Field label="Dim to extend" htmlFor="h-dim" error={problemFor(problems, "horizonDim")}>
                <Input id="h-dim" value={form.horizonDim} onChange={(e) => set("horizonDim", e.target.value)} className="w-40 font-mono" />
              </Field>
            )}
          </div>
        </Step>
      ) : null}

      <Step
        n={form.mode === "forecast" ? 3 : 2}
        title="Values to score"
        hint={
          form.source === "typed"
            ? "Type the inputs the model should use. Rows start with the periods and levels the model knows; edit, add or paste rows."
            : form.source === "dataset"
              ? "A dataset with the inputs, in the units of the entry stage."
              : undefined
        }
      >
        <SegmentedControl<InputSource>
          label="Where the values come from"
          value={form.source}
          onValueChange={(v) => set("source", v)}
          options={[
            ...(canType ? [{ value: "typed" as const, label: "Type values", title: "Enter the values here, no upload needed" }] : []),
            { value: "dataset" as const, label: "Use a dataset", title: "A dataset you uploaded under Datasets" },
            ...(!DATASET_ONLY.includes(form.mode) ? [{ value: "none" as const, label: "No new data", title: form.mode === "forecast" ? "Forecast from the end of the fit" : "Score the fitted data" }] : []),
          ]}
          className="w-fit"
        />
        {inputsMissing ? (
          <p className="text-xs text-muted-foreground">This version was fitted before the platform stored its inputs, so values cannot be typed: use a dataset, or refit it.</p>
        ) : null}

        {form.source === "typed" && canType ? (
          <div className="grid gap-1.5" aria-invalid={valuesProblem ? true : undefined}>
            <ValuesGrid
              columns={columns}
              rows={rows}
              onRows={(r) => {
                setEdited(true);
                setRows(r);
              }}
              onReset={() => {
                setEdited(false);
                setRows(seed);
              }}
              horizonDim={hdim}
              blankDrivers={blankDrivers}
            />
            {valuesProblem ? (
              <p role="alert" className="text-xs font-medium text-destructive">
                {valuesProblem}
              </p>
            ) : null}
          </div>
        ) : null}

        {form.source === "dataset" ? (
          <>
            <datalist id="score-datasets">
              {refs.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
            <Field label="Dataset" htmlFor="score-data" error={problemFor(problems, "data")} hint="Upload one under Datasets first.">
              <div className="flex gap-2">
                <Input id="score-data" value={form.data} onChange={(e) => set("data", e.target.value)} placeholder="dataset://default/eu_media_future@v1" className="font-mono" list="score-datasets" />
                <Button variant="outline" asChild>
                  <Link to="/datasets">Upload</Link>
                </Button>
              </div>
            </Field>
          </>
        ) : null}

        {form.mode === "forecast" && (futureVars.length || form.source !== "none") ? (
          <Field
            label={futureVars.length ? `Future values of ${futureVars.join(", ")} not given` : "Future inputs not given"}
            htmlFor="future-covariates"
            hint={FUTURE_OPTIONS.find((o) => o.value === form.futureCovariates)?.hint}
          >
            <NativeSelect id="future-covariates" value={form.futureCovariates} onChange={(e) => set("futureCovariates", e.target.value as FutureCovariates)} className="w-72">
              {FUTURE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </NativeSelect>
          </Field>
        ) : null}
      </Step>

      <Step
        n={form.mode === "forecast" ? 4 : 3}
        title={form.mode === "counterfactual" ? "What to change" : "Changes (optional)"}
        hint={form.mode === "counterfactual" ? "The result compares the model with these changes against the model without them." : "Change a variable on some levels before scoring, e.g. spend × 1.2 where channel = tv."}
      >
        <InterventionRows form={form} set={set} problems={problems} vars={vars.filter((v) => v.role !== "target").map((v) => v.name)} levelHint={levelHint} />
      </Step>

      <details className="rounded-lg border p-3" open={problems.some((p) => ADVANCED_FIELDS.test(p.field)) || undefined}>
        <summary className="cursor-pointer text-[13px] font-medium">Advanced</summary>
        <div className="mt-4 grid gap-5">
          <Field label="Inputs enter at stage" htmlFor="inputs-stage" error={problemFor(problems, "inputsStage")} hint="Send data in the units of this stage. Typed values use the raw stage.">
            <NativeSelect id="inputs-stage" value={form.inputsStage} onChange={(e) => set("inputsStage", e.target.value)} className="w-48 font-mono">
              {stages.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Scenario dataset (optional)" htmlFor="scenario" error={problemFor(problems, "scenario")} hint="A changed copy of the input data. What-if compares it against the input data.">
            <Input id="scenario" value={form.scenario} onChange={(e) => set("scenario", e.target.value)} placeholder="dataset://default/plan_2027h1@v1" className="font-mono" list="score-datasets" />
          </Field>
          {sideTables.length ? (
            <fieldset className="grid gap-3 rounded-lg border p-4">
              <legend className="px-1 text-[13px] font-medium">Side tables</legend>
              {sideTables.map((t) =>
                t.policy === "pinned" ? (
                  <p key={t.name} className="text-xs text-muted-foreground">
                    <Mono>{t.name}</Mono> is pinned: scoring replays <Mono>{t.ref}</Mono> from the fit.
                  </p>
                ) : (
                  <Field
                    key={t.name}
                    label={t.name}
                    htmlFor={`side-${t.name}`}
                    error={problemFor(problems, `sideTables.${t.name}`)}
                    hint={t.policy === "supplied" ? "Required when a step before the entry stage uses it; it must cover the scored dates." : "Optional: defaults to its latest version."}
                  >
                    <Input id={`side-${t.name}`} value={form.sideTables[t.name] ?? ""} onChange={(e) => set("sideTables", { ...form.sideTables, [t.name]: e.target.value })} className="font-mono" list="score-datasets" />
                  </Field>
                ),
              )}
            </fieldset>
          ) : null}
          <OutputSettings form={form} setOut={setOut} set={set} problems={problems} representation={representation} />
        </div>
      </details>

      <div className="grid gap-3 rounded-lg border bg-muted/30 p-3" aria-label="Ready to score">
        <ul className="grid gap-1 text-[13px]">
          {readiness.map((n) => (
            <li key={n.text} className={cn("flex items-start gap-2", !n.ok && "text-warning")}>
              {n.ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" /> : <TriangleAlert className="mt-0.5 size-4 shrink-0" />}
              {n.text}
            </li>
          ))}
        </ul>
        {error ? <ErrorNotice error={error} title="Scoring was refused" /> : null}
        <div>
          <Button type="submit" disabled={submitting}>
            <Gauge /> {submitting ? "Scoring…" : modeText(form.mode, inputs, form.horizonPeriods).title}
          </Button>
        </div>
      </div>
    </form>
  );
}

function InterventionRows({
  form,
  set,
  problems,
  vars,
  levelHint,
}: {
  form: ScoreForm;
  set: <K extends keyof ScoreForm>(k: K, v: ScoreForm[K]) => void;
  problems: FormProblem[];
  vars: string[];
  levelHint: string;
}) {
  return (
    <div className="grid gap-2">
      {form.interventions.length ? (
        <ul className="grid gap-2">
          {form.interventions.map((row, i) => {
            const update = (patch: Partial<typeof row>) => set("interventions", form.interventions.map((r, j) => (j === i ? { ...r, ...patch } : r)));
            const msg = problemFor(problems, `interventions.${i}`);
            const rowAria = msg ? ({ "aria-invalid": true, "aria-describedby": `iv-${i}-error` } as const) : {};
            return (
              <li key={i} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  {vars.length ? (
                    <NativeSelect aria-label="Variable" {...rowAria} value={row.var} onChange={(e) => update({ var: e.target.value })} className="h-8 w-36 font-mono">
                      {row.var && !vars.includes(row.var) ? <option value={row.var}>{row.var}</option> : null}
                      {vars.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </NativeSelect>
                  ) : (
                    <Input aria-label="Variable" {...rowAria} value={row.var} onChange={(e) => update({ var: e.target.value })} className="h-8 w-32 font-mono" />
                  )}
                  <NativeSelect aria-label="Operation" value={row.op} onChange={(e) => update({ op: e.target.value as InterventionOp })} className="h-8 w-28">
                    <option value="multiply">× multiply</option>
                    <option value="add">+ add</option>
                    <option value="set">= set to</option>
                  </NativeSelect>
                  <Input aria-label="Value" {...rowAria} inputMode="decimal" value={row.value} onChange={(e) => update({ value: e.target.value })} className="h-8 w-24" />
                  <span className="text-xs text-muted-foreground">where</span>
                  <Input aria-label="Selection" {...rowAria} value={row.sel} onChange={(e) => update({ sel: e.target.value })} placeholder="everywhere" className="h-8 w-48 font-mono" />
                  <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove change" onClick={() => set("interventions", form.interventions.filter((_, j) => j !== i))}>
                    <Trash2 />
                  </Button>
                </div>
                {msg ? (
                  <p id={`iv-${i}-error`} role="alert" className="text-xs font-medium text-destructive">
                    {msg}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {form.interventions.length && levelHint ? (
        <p className="text-xs text-muted-foreground">
          “where” takes <Mono>dim=level</Mono> pairs separated by <Mono>;</Mono> (blank: everywhere). Levels: {levelHint}
        </p>
      ) : null}
      <Button
        type="button"
        size="sm"
        variant="outline"
        className="w-fit"
        onClick={() => set("interventions", [...form.interventions, { var: vars[0] ?? "spend", sel: "", op: "multiply", value: "1.1" }])}
      >
        <Plus /> Add a change
      </Button>
    </div>
  );
}

function OutputSettings({
  form,
  set,
  setOut,
  problems,
  representation,
}: {
  form: ScoreForm;
  set: <K extends keyof ScoreForm>(k: K, v: ScoreForm[K]) => void;
  setOut: <K extends keyof ScoreForm["output"]>(k: K, v: ScoreForm["output"][K]) => void;
  problems: FormProblem[];
  representation: ReturnType<typeof asRepresentation>;
}) {
  const rules = representation ? REPRESENTATIONS[representation] : null;
  const options = rules?.options ?? SUMMARY_OPTIONS;
  const summaryProblem = problemFor(problems, "summary");
  return (
    <fieldset className="grid gap-4 rounded-lg border p-4">
      <legend className="px-1 text-[13px] font-medium">Output</legend>
      <div className="flex flex-wrap gap-4">
        <div className="grid gap-1.5">
          <span className="text-[13px] font-medium">Quantity</span>
          <SegmentedControl
            label="Output kind"
            value={form.output.kind}
            onValueChange={(v) => setOut("kind", v)}
            options={[
              { value: "expected", label: "Expected", title: "The mean response, without observation noise" },
              { value: "predictive", label: "Predictive", title: "Includes observation noise" },
            ]}
          />
        </div>
        <div className="grid gap-1.5">
          <span className="text-[13px] font-medium">Units</span>
          <SegmentedControl label="Units" value={form.output.units} onValueChange={(v) => setOut("units", v)} options={[{ value: "original", label: "Original" }, { value: "model", label: "Model space" }]} />
        </div>
        <div className="grid gap-1.5">
          <span className="text-[13px] font-medium">Names</span>
          <SegmentedControl label="Names" value={form.output.names} onValueChange={(v) => setOut("names", v)} options={[{ value: "dataset", label: "Dataset" }, { value: "canonical", label: "Canonical" }]} />
        </div>
      </div>
      <div className="grid gap-1.5">
        <span className="text-[13px] font-medium">Summaries</span>
        {representation ? <p className="text-xs text-muted-foreground">This model returns {representation}, so these are the summaries it can give.</p> : null}
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Summaries" aria-invalid={summaryProblem ? true : undefined}>
          {options.map((s) => {
            const on = form.output.summary.includes(s);
            return (
              <button
                key={s}
                type="button"
                aria-pressed={on}
                onClick={() => setOut("summary", on ? form.output.summary.filter((x) => x !== s) : [...form.output.summary, s])}
                className={cn("rounded-md border px-2 py-1 font-mono text-xs", on ? "border-primary bg-accent text-accent-foreground" : "text-muted-foreground hover:text-foreground")}
              >
                {s}
              </button>
            );
          })}
        </div>
        {summaryProblem ? (
          <p role="alert" className="text-xs font-medium text-destructive">
            {summaryProblem}
          </p>
        ) : null}
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field
          label="Draws"
          htmlFor="out-draws"
          error={problemFor(problems, "draws")}
          hint={rules && !rules.draws ? `Not available: this model returns ${representation}.` : "0 returns summaries only."}
        >
          <Input id="out-draws" inputMode="numeric" value={form.output.draws} disabled={Boolean(rules && !rules.draws)} onChange={(e) => setOut("draws", e.target.value)} />
        </Field>
        <Field label="Variables (optional)" htmlFor="out-vars" error={problemFor(problems, "vars")} hint="Comma-separated; empty means all outputs.">
          <Input id="out-vars" value={form.output.vars} onChange={(e) => setOut("vars", e.target.value)} className="font-mono" placeholder="y_hat" />
        </Field>
        <Field label="Seed (optional)" htmlFor="seed" error={problemFor(problems, "seed")}>
          <Input id="seed" inputMode="numeric" value={form.seed} onChange={(e) => set("seed", e.target.value)} />
        </Field>
      </div>
    </fieldset>
  );
}
