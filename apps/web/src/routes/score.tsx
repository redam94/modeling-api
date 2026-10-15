import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { Gauge, Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { newIdempotencyKey } from "@/api/client";
import {
  scoreModel,
  useDatasets,
  useModels,
  useModelVersion,
  useModelVersions,
  useRun,
  useSpecRevision,
} from "@/api/queries";
import type { ModelVersion, ScoreCall, ScoreCallResult } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Mono, PageHeader, ProblemNotice, StateBadge } from "@/components/common";
import { Button } from "@/components/ui/button";
import { SegmentedControl } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { ProgressPanel } from "@/features/runs/progress-panel";
import {
  buildScoreRequest,
  defaultScoreForm,
  sideTableOptions,
  SUMMARY_OPTIONS,
  type FormProblem,
  type InterventionOp,
  type ScoreForm,
  type ScoreMode,
  type SideTableOption,
} from "@/features/score/request";
import { ScoreResults } from "@/features/score/results";
import { useRunEvents } from "@/hooks/use-run-events";
import { isTerminal } from "@/lib/run-state";
import { cn } from "@/lib/utils";

const route = getRouteApi("/score");

const MODE_HELP: Record<ScoreMode, string> = {
  predict: "In-sample or new-data predictions with replayed preprocessing.",
  forecast: "Extend a time-like dim past the fitted range.",
  counterfactual: "Compare against interventions with common random numbers.",
  simulate: "Draw synthetic outcomes from the model.",
  filter: "Update the terminal state with new observations (state-space models).",
  evaluate: "Score held-out data and compute metrics.",
};

function ModelPicker({ modelId, version }: { modelId: string | undefined; version: number | undefined }) {
  const navigate = useNavigate({ from: "/score" });
  const models = useModels();
  const versions = useModelVersions(modelId);
  return (
    <div className="flex flex-wrap items-end gap-3">
      <Field label="Model" htmlFor="score-model">
        <NativeSelect
          id="score-model"
          value={modelId ?? ""}
          onChange={(e) => void navigate({ search: { model: e.target.value || undefined } })}
          className="w-64"
        >
          <option value="">Choose a model</option>
          {(models.data?.items ?? []).map((m) => (
            <option key={m.id} value={m.id}>{m.name}</option>
          ))}
        </NativeSelect>
      </Field>
      <Field label="Version" htmlFor="score-version">
        <NativeSelect
          id="score-version"
          value={version ?? ""}
          disabled={!modelId}
          onChange={(e) => void navigate({ search: (s) => ({ ...s, version: e.target.value ? Number(e.target.value) : undefined }) })}
          className="w-32"
        >
          <option value="">Version</option>
          {[...(versions.data?.items ?? [])].sort((a, b) => b.version - a.version).map((v) => (
            <option key={v.version} value={v.version}>v{v.version}</option>
          ))}
        </NativeSelect>
      </Field>
    </div>
  );
}

/**
 * What the spec that produced this version allows a score request to name: its pipeline step
 * ids (the valid `inputs_stage` values) and its side tables.
 */
function useSpecOptions(mv: ModelVersion | undefined): { stages: string[]; sideTables: SideTableOption[] } {
  const run = useRun(mv?.lineage.run_id, { poll: false });
  const rev = useSpecRevision(run.data?.spec_id ?? undefined, run.data?.spec_revision ?? undefined);
  return useMemo(() => {
    const doc = rev.data?.document as { pipeline?: unknown } | undefined;
    const pipeline = doc?.pipeline;
    const ids = Array.isArray(pipeline)
      ? pipeline.map((s) => (typeof s === "object" && s !== null && typeof (s as { id?: unknown }).id === "string" ? (s as { id: string }).id : null)).filter((x): x is string => x !== null)
      : [];
    return { stages: ["raw", ...ids], sideTables: sideTableOptions(doc) };
  }, [rev.data]);
}

function datasetRefs(items: { id: string; ref: string; latest_version?: number | null }[]): string[] {
  return items.map((d) => (d.latest_version ? `${d.ref}@v${d.latest_version}` : d.ref));
}

function problemFor(problems: FormProblem[], field: string): string | undefined {
  return problems.find((p) => p.field === field)?.message;
}

function ScoreFormView({ mv, onResult }: { mv: ModelVersion; onResult: (r: { result: ScoreCallResult; key: string; body: ScoreCall }) => void }) {
  const [form, setForm] = useState<ScoreForm>(defaultScoreForm);
  const [problems, setProblems] = useState<FormProblem[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  /** Bumped by a submit that found problems: focus the first invalid control once rendered. */
  const [focusInvalid, setFocusInvalid] = useState(0);
  useEffect(() => {
    if (focusInvalid) formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [focusInvalid]);
  const [error, setError] = useState<unknown>(null);
  const { stages, sideTables } = useSpecOptions(mv);
  const datasets = useDatasets();
  const refs = useMemo(() => datasetRefs(datasets.data?.items ?? []), [datasets.data]);
  // Supplied side tables default to the version the fit used (it usually covers the horizon).
  useEffect(() => {
    const defaults = Object.fromEntries(sideTables.filter((t) => t.policy !== "pinned").map((t) => [t.name, t.ref]));
    setForm((f) => ({ ...f, sideTables: { ...defaults, ...f.sideTables } }));
  }, [sideTables]);
  const modes = useMemo(
    () => (mv.capabilities?.score_modes ?? ["predict", "forecast", "counterfactual", "simulate", "filter", "evaluate"]) as ScoreMode[],
    [mv.capabilities],
  );
  useEffect(() => {
    if (!modes.includes(form.mode) && modes[0]) setForm((f) => ({ ...f, mode: modes[0] as ScoreMode }));
  }, [modes, form.mode]);
  const set = <K extends keyof ScoreForm>(k: K, v: ScoreForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const setOut = <K extends keyof ScoreForm["output"]>(k: K, v: ScoreForm["output"][K]) => setForm((f) => ({ ...f, output: { ...f.output, [k]: v } }));

  const submit = async () => {
    const built = buildScoreRequest(form, mv.ref);
    if ("problems" in built) {
      setProblems(built.problems);
      setFocusInvalid((n) => n + 1);
      return;
    }
    setProblems([]);
    setError(null);
    setSubmitting(true);
    const key = newIdempotencyKey();
    try {
      const body: ScoreCall = { request: built.request };
      const { result } = await scoreModel(mv.model_id, mv.version, body, { key, waitSeconds: 30 });
      onResult({ result, key, body });
    } catch (e) {
      setError(e);
    } finally {
      setSubmitting(false);
    }
  };

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
      <div className="grid gap-2">
        <span className="text-[13px] font-medium">Mode</span>
        <SegmentedControl<ScoreMode>
          label="Score mode"
          value={form.mode}
          onValueChange={(m) => set("mode", m)}
          options={modes.map((m) => ({ value: m, label: m }))}
          className="w-fit"
        />
        <p className="text-xs text-muted-foreground">{MODE_HELP[form.mode]}</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        {form.mode === "forecast" ? (
          <>
            <Field label="Horizon dim" htmlFor="h-dim" error={problemFor(problems, "horizonDim")}>
              <Input id="h-dim" value={form.horizonDim} onChange={(e) => set("horizonDim", e.target.value)} className="font-mono" />
            </Field>
            <Field label="Periods" htmlFor="h-periods" error={problemFor(problems, "horizonPeriods")}>
              <Input id="h-periods" inputMode="numeric" value={form.horizonPeriods} onChange={(e) => set("horizonPeriods", e.target.value)} />
            </Field>
          </>
        ) : null}
        <Field label="Inputs enter at stage" htmlFor="inputs-stage" error={problemFor(problems, "inputsStage")} hint="Send data in the units of this stage, e.g. weekly EUR spend.">
          <NativeSelect id="inputs-stage" value={form.inputsStage} onChange={(e) => set("inputsStage", e.target.value)} className="font-mono">
            {stages.map((s) => <option key={s} value={s}>{s}</option>)}
          </NativeSelect>
        </Field>
      </div>

      <datalist id="score-datasets">
        {refs.map((r) => (
          <option key={r} value={r} />
        ))}
      </datalist>
      <Field
        label="Input data"
        htmlFor="score-data"
        error={problemFor(problems, "data")}
        hint="The inputs to score, in the units of the entry stage: future spend over the horizon, new markets, or a window of past data. Upload it under Datasets first."
      >
        <div className="flex gap-2">
          <Input id="score-data" value={form.data} onChange={(e) => set("data", e.target.value)} placeholder="dataset://default/eu_media_future@v1" className="font-mono" list="score-datasets" />
          <Button variant="outline" asChild>
            <Link to="/datasets">Upload</Link>
          </Button>
        </div>
      </Field>

      <Field label="Scenario data (optional)" htmlFor="scenario" error={problemFor(problems, "scenario")} hint="A changed copy of the input data. Counterfactuals compare it against the input data.">
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
                hint={
                  t.policy === "supplied"
                    ? "Required when a step before the entry stage uses it; it must cover the scored dates."
                    : "Optional: defaults to its latest version."
                }
              >
                <Input
                  id={`side-${t.name}`}
                  value={form.sideTables[t.name] ?? ""}
                  onChange={(e) => set("sideTables", { ...form.sideTables, [t.name]: e.target.value })}
                  className="font-mono"
                  list="score-datasets"
                />
              </Field>
            ),
          )}
        </fieldset>
      ) : null}

      <div className="grid gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[13px] font-medium">Interventions</span>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => set("interventions", [...form.interventions, { var: "spend", sel: "", op: "multiply", value: "1.1" }])}
          >
            <Plus /> Add
          </Button>
        </div>
        {form.interventions.length === 0 ? (
          <p className="text-xs text-muted-foreground">None. Add one to change a variable on selected coordinates, e.g. spend × 1.2 where channel=meta.</p>
        ) : (
          <ul className="grid gap-2">
            {form.interventions.map((row, i) => {
              const update = (patch: Partial<typeof row>) =>
                set("interventions", form.interventions.map((r, j) => (j === i ? { ...r, ...patch } : r)));
              const msg = problemFor(problems, `interventions.${i}`);
              const rowAria = msg ? ({ "aria-invalid": true, "aria-describedby": `iv-${i}-error` } as const) : {};
              return (
                <li key={i} className="grid gap-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <Input aria-label="Variable" {...rowAria} value={row.var} onChange={(e) => update({ var: e.target.value })} className="h-8 w-32 font-mono" />
                    <span className="text-xs text-muted-foreground">where</span>
                    <Input aria-label="Selection" {...rowAria} value={row.sel} onChange={(e) => update({ sel: e.target.value })} placeholder="channel=meta; geo=de,fr" className="h-8 w-56 font-mono" />
                    <NativeSelect aria-label="Operation" value={row.op} onChange={(e) => update({ op: e.target.value as InterventionOp })} className="h-8 w-28">
                      <option value="multiply">multiply</option>
                      <option value="add">add</option>
                      <option value="set">set to</option>
                    </NativeSelect>
                    <Input aria-label="Value" {...rowAria} inputMode="decimal" value={row.value} onChange={(e) => update({ value: e.target.value })} className="h-8 w-24" />
                    <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove intervention" onClick={() => set("interventions", form.interventions.filter((_, j) => j !== i))}>
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
        )}
      </div>

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
          <div className="flex flex-wrap gap-1.5" role="group" aria-label="Summaries">
            {SUMMARY_OPTIONS.map((s) => {
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
        </div>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Draws" htmlFor="out-draws" error={problemFor(problems, "draws")} hint="0 returns summaries only.">
            <Input id="out-draws" inputMode="numeric" value={form.output.draws} onChange={(e) => setOut("draws", e.target.value)} />
          </Field>
          <Field label="Variables (optional)" htmlFor="out-vars" error={problemFor(problems, "vars")} hint="Comma-separated; empty means all outputs.">
            <Input id="out-vars" value={form.output.vars} onChange={(e) => setOut("vars", e.target.value)} className="font-mono" placeholder="y_hat" />
          </Field>
          <Field label="Seed (optional)" htmlFor="seed" error={problemFor(problems, "seed")}>
            <Input id="seed" inputMode="numeric" value={form.seed} onChange={(e) => set("seed", e.target.value)} />
          </Field>
        </div>
      </fieldset>

      {error ? <ErrorNotice error={error} title="Scoring was refused" /> : null}
      <div>
        <Button type="submit" disabled={submitting}>
          <Gauge /> {submitting ? "Scoring…" : "Score"}
        </Button>
      </div>
    </form>
  );
}

/** Follows a score run that did not finish within the `Prefer: wait` budget. */
function PendingScore({
  mv,
  result,
  scoreKey,
  body,
  onDone,
}: {
  mv: ModelVersion;
  result: ScoreCallResult;
  scoreKey: string;
  body: ScoreCall;
  onDone: (r: ScoreCallResult) => void;
}) {
  const run = useRun(result.run.id);
  const stream = useRunEvents(run.data ?? result.run);
  const current = run.data ?? result.run;
  const fetched = useRef(false);
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!isTerminal(current.state) || fetched.current) return;
    fetched.current = true;
    if (current.state !== "succeeded") {
      onDone({ ...result, run: current, done: true, outputs: [] });
      return;
    }
    // Replaying the same call with the same Idempotency-Key returns the finished result.
    scoreModel(mv.model_id, mv.version, body, { key: scoreKey, waitSeconds: 0 })
      .then(({ result: r }) => onDone(r))
      .catch(setError);
  }, [current, mv, onDone, result, scoreKey, body]);
  return (
    <div className="grid gap-4">
      <div className="flex items-center gap-3">
        <StateBadge state={current.state} />
        <Link to="/runs/$runId" params={{ runId: current.id }} className="text-xs text-muted-foreground hover:underline">
          <Mono>{current.id}</Mono>
        </Link>
      </div>
      <ProgressPanel run={current} progress={stream.progress} />
      {error ? <ErrorNotice error={error} title="The finished result could not be fetched" /> : null}
    </div>
  );
}

export function ScorePage() {
  const { model, version } = route.useSearch();
  const mv = useModelVersion(model, version);

  return (
    <>
      <PageHeader title="Score" description="Forecasts, scenarios and counterfactuals with the fitted preprocessing replayed exactly." actions={<ModelPicker modelId={model} version={version} />} />
      {!model || !version ? (
        <EmptyState icon={<Gauge />} title="Choose a model version">
          Scoring needs a fitted model version. Pick one above, or open a version under Models and press Score.
        </EmptyState>
      ) : mv.isError ? (
        <ErrorNotice error={mv.error} />
      ) : mv.data ? (
        // Keyed by version: switching model or version starts a fresh form and empty results.
        <ScoreWorkspace key={`${model}@${version}`} mv={mv.data} />
      ) : (
        <LoadingRows rows={6} />
      )}
    </>
  );
}

/** The request form and its results, for one model version. */
function ScoreWorkspace({ mv }: { mv: ModelVersion }) {
  const [pending, setPending] = useState<{ result: ScoreCallResult; key: string; body: ScoreCall } | null>(null);
  const [done, setDone] = useState<ScoreCallResult | null>(null);
  const resultsRef = useRef<HTMLElement>(null);
  // Below 2xl the results stack under the form: bring them into view when a score starts.
  useEffect(() => {
    if (pending || done) resultsRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [pending, done]);

  return (
        <div className="grid gap-5 2xl:grid-cols-[minmax(0,34rem)_minmax(0,1fr)]">
          <Card>
            <CardHeader>
              <div>
                <CardTitle>Request</CardTitle>
                <CardDescription><Mono>{mv.ref}</Mono></CardDescription>
              </div>
            </CardHeader>
            <CardContent>
              <ScoreFormView
                mv={mv}
                onResult={({ result, key, body }) => {
                  setDone(null);
                  setPending(null);
                  if (result.done) setDone(result);
                  else setPending({ result, key, body });
                }}
              />
            </CardContent>
          </Card>
          <Card ref={resultsRef} aria-label="Results">
            <CardHeader>
              <CardTitle>Results</CardTitle>
              {done ? <StateBadge state={done.run.state} /> : null}
            </CardHeader>
            <CardContent>
              {done ? (
                done.run.problem ? <ProblemNotice problem={done.run.problem} /> : <ScoreResults outputs={done.outputs ?? []} runId={done.run.id} />
              ) : pending ? (
                <PendingScore
                  mv={mv}
                  result={pending.result}
                  scoreKey={pending.key}
                  body={pending.body}
                  onDone={(r) => {
                    setPending(null);
                    setDone(r);
                  }}
                />
              ) : (
                <p className="text-muted-foreground">Results appear here. Short requests return within 30 seconds; longer ones keep running and show progress.</p>
              )}
            </CardContent>
          </Card>
        </div>
  );
}

