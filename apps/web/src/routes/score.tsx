import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { Gauge } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { scoreModel, useModelInputs, useModels, useModelVersion, useModelVersions, useRun, useSpecRevision } from "@/api/queries";
import type { ModelVersion, ScoreCall, ScoreCallResult } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Mono, PageHeader, ProblemNotice, StateBadge } from "@/components/common";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Field, NativeSelect } from "@/components/ui/primitives";
import { ConditioningNote } from "@/features/integrations/actuals-editor";
import { ProgressPanel } from "@/features/runs/progress-panel";
import { ActualsComparison } from "@/features/score/actuals-comparison";
import { ModelInputsPanel } from "@/features/score/model-inputs";
import { sideTableOptions, type SideTableOption } from "@/features/score/request";
import { ScoreResults } from "@/features/score/results";
import { ScoreFormView } from "@/features/score/score-form";
import { useRunEvents } from "@/hooks/use-run-events";
import { isTerminal } from "@/lib/run-state";

const route = getRouteApi("/score");

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
      {error ? <ErrorNotice error={error} title="The app could not get the finished result" /> : null}
    </div>
  );
}

export function ScorePage() {
  const { model, version } = route.useSearch();
  const mv = useModelVersion(model, version);

  return (
    <>
      <PageHeader title="Score" description="Make forecasts, scenarios and counterfactuals. The server uses the same preprocessing as the fit." actions={<ModelPicker modelId={model} version={version} />} />
      {!model || !version ? (
        <EmptyState icon={<Gauge />} title="Choose a model version">
          To score, you need a fitted model version. Select one above. Or open a version on the Models page and click Score.
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
  const inputs = useModelInputs(mv.model_id, mv.version);
  const { stages, sideTables } = useSpecOptions(mv);
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
            <CardContent className="grid gap-6">
              {inputs.data ? <ModelInputsPanel inputs={inputs.data} /> : inputs.isPending ? <LoadingRows rows={3} /> : null}
              <ScoreFormView
                mv={mv}
                inputs={inputs.data ?? null}
                inputsError={inputs.error}
                stages={stages}
                sideTables={sideTables}
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
                done.run.problem ? (
                  <ProblemNotice problem={done.run.problem} />
                ) : (
                  <div className="grid gap-3">
                    {done.summary?.conditioning ? <ConditioningNote conditioning={done.summary.conditioning} /> : null}
                    {done.summary?.actuals?.length ? <ActualsComparison summary={done.summary} /> : null}
                    <ScoreResults outputs={done.outputs ?? []} runId={done.run.id} />
                  </div>
                )
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
                <p className="text-muted-foreground">The results show here. A short request gives its results in 30 seconds or less. A longer request continues to run and shows its progress.</p>
              )}
            </CardContent>
          </Card>
        </div>
  );
}

