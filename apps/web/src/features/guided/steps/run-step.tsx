/**
 * Step 4, "Fit the model": start the fit and follow it in plain words (live over SSE): what is
 * happening now, about how long is left, and a way to stop it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowRight, Check, CircleStop, Play, RotateCcw } from "lucide-react";
import { useRef, useState } from "react";

import { can } from "@/api/access";
import { errorMessage, newIdempotencyKey } from "@/api/client";
import { qk, useCancelRun, useCreateRun, useProjectScope, useRun } from "@/api/queries";
import type { Run } from "@/api/types";
import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, Progress } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { useRunEvents } from "@/hooks/use-run-events";
import { adviseProblem } from "@/lib/problems";
import { formatMicros } from "@/lib/cost";
import { isTerminal } from "@/lib/run-state";
import { cn } from "@/lib/utils";

import { StepActions } from "../frame";
import { etaText, PHASE_ORDER, phaseSlot, phaseText, stateText } from "../plain";
import { GUIDED_LABEL } from "../session";
import type { GuidedSession } from "../use-session";
import { IngestStatus } from "./data-summary";

function PhaseChecklist({ phase, done }: { phase: string | null; done: boolean }) {
  const slot = done ? PHASE_ORDER.length : phaseSlot(phase);
  return (
    <ol className="grid gap-2 sm:grid-cols-2" aria-label="What happens during the fit">
      {PHASE_ORDER.map((p, i) => {
        const state = i < slot ? "done" : i === slot ? "now" : "later";
        return (
          <li key={p} className={cn("flex items-center gap-2 text-[13px]", state === "later" && "text-muted-foreground")}>
            <span
              aria-hidden
              className={cn(
                "flex size-5 shrink-0 items-center justify-center rounded-full border",
                state === "done" && "border-success/40 bg-success/12 text-success",
                state === "now" && "border-primary text-primary",
              )}
            >
              {state === "done" ? <Check className="size-3" /> : state === "now" ? <Spinner className="size-3" /> : null}
            </span>
            <span>
              {phaseText(p).title}
              <span className="sr-only">{state === "done" ? " (done)" : state === "now" ? " (in progress)" : ""}</span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function LiveRun({ run, session }: { run: Run; session: GuidedSession }) {
  const live = useRun(run.id);
  const current = live.data ?? run;
  const stream = useRunEvents(current);
  const cancel = useCancelRun();
  const grants = useGrants();
  const navigate = useNavigate();
  const p = stream.progress ?? current.progress ?? null;
  const terminal = isTerminal(current.state);
  const text = phaseText(p?.phase);
  const eta = etaText(p?.eta_seconds);
  const advice = current.problem ? adviseProblem(current.problem) : null;

  if (current.state === "succeeded") {
    return (
      <Card>
        <CardContent className="grid gap-4 pt-5">
          <div className="flex items-start gap-3">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-success/12 text-success">
              <Check aria-hidden className="size-5" />
            </span>
            <div className="grid gap-1">
              <h2 className="text-[17px] font-semibold tracking-tight">Your model is ready</h2>
              <p className="text-[13px] text-muted-foreground">
                It learned from your data and checked itself on the weeks it held back.
                {current.cost?.amount_micros != null ? ` This fit cost ${formatMicros(current.cost.amount_micros, current.cost.currency)}.` : ""}
              </p>
            </div>
          </div>
          <PhaseChecklist phase={null} done />
          <Button className="justify-self-start" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "report" } })}>
            See the results <ArrowRight />
          </Button>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardContent className="grid gap-5 pt-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="grid gap-1" aria-live="polite">
            <p className="text-xs font-medium text-muted-foreground">{stateText(current.state)}</p>
            <h2 className="text-[17px] font-semibold tracking-tight">{terminal ? (current.state === "cancelled" ? "The fit was stopped" : "The fit did not finish") : text.title}</h2>
            <p className="max-w-[60ch] text-[13px] text-muted-foreground">
              {terminal ? (advice ? `${advice.title}. ${advice.message}` : (current.problem?.detail ?? current.problem?.title ?? "You can start it again.")) : text.body}
            </p>
          </div>
          {!terminal ? (
            <Button variant="outline" size="sm" disabled={current.state === "cancelling" || cancel.isPending || !can(grants, "cancel", "run")} onClick={() => cancel.mutate(current.id)}>
              <CircleStop /> {current.state === "cancelling" ? "Stopping…" : "Cancel"}
            </Button>
          ) : null}
        </div>
        {!terminal ? (
          <div className="grid gap-1.5">
            <Progress value={p?.fraction ?? null} label="Fit progress" className="h-2" />
            <p className="flex justify-between text-xs text-muted-foreground tabular">
              <span>{p?.fraction != null ? `${Math.round(p.fraction * 100)}% done` : "Starting…"}</span>
              <span>{eta ?? ""}</span>
            </p>
          </div>
        ) : null}
        {!terminal ? <PhaseChecklist phase={p?.phase ?? null} done={false} /> : null}
        <p className="text-xs text-muted-foreground">
          You can leave this page: the fit carries on and this step shows where it is when you come back.{" "}
          <Link to="/runs/$runId" params={{ runId: current.id }} className="underline-offset-2 hover:underline">
            Technical details
          </Link>
        </p>
      </CardContent>
    </Card>
  );
}

export function RunStep({ session }: { session: GuidedSession }) {
  const create = useCreateRun();
  const qc = useQueryClient();
  const scope = useProjectScope();
  const grants = useGrants();
  const navigate = useNavigate();
  const key = useRef(newIdempotencyKey());
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState<Run | null>(null);
  const run = started ?? session.currentFit;
  const dataReady = session.datasetVersion?.status === "ready";
  const canRun = can(grants, "create", "run");
  const finished = run && isTerminal(run.state) && run.state !== "succeeded";

  const start = async () => {
    setError(null);
    try {
      const r = await create.mutateAsync({
        key: key.current,
        body: { kind: "fit", spec_id: session.spec.id, revision: session.latest.revision, labels: { [GUIDED_LABEL]: session.template.id } },
      });
      key.current = newIdempotencyKey();
      setStarted(r);
      await qc.invalidateQueries({ queryKey: qk.runLists(scope.key) });
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <div className="grid gap-5">
      {!dataReady ? <IngestStatus version={session.datasetVersion} compact /> : null}
      {run ? <LiveRun key={run.id} run={run} session={session} /> : null}
      {!run || finished ? (
        <Card>
          <CardContent className="grid gap-3 pt-5">
            <h2 className="text-[15px] font-semibold tracking-tight">{run ? "Try again" : "Ready when you are"}</h2>
            <p className="max-w-[62ch] text-[13px] text-muted-foreground">
              Fitting runs in a secure sandbox and usually takes a few minutes. First the model learns without the latest weeks to measure how well it forecasts, then it learns again from everything.
              {session.lastGoodFit && session.lastGoodFit.spec_revision !== session.latest.revision ? " Your earlier results stay available until the new fit finishes." : ""}
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={() => void start()} disabled={!dataReady || create.isPending || !canRun}>
                {create.isPending ? <Spinner /> : run ? <RotateCcw /> : <Play />} {run ? "Fit again" : "Fit the model"}
              </Button>
              {!canRun ? <span className="text-xs text-muted-foreground">Starting a fit needs the runner role.</span> : null}
            </div>
            {error ? (
              <p role="alert" className="text-[13px] text-destructive">
                {error}
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}
      <StepActions>
        <Button variant="ghost" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: session.template.tasks?.includes("business_priors") ? "priors" : "data" } })}>
          Back
        </Button>
        {session.lastGoodFit ? (
          <Button variant="outline" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "report" } })}>
            {session.lastGoodFit.spec_revision === session.latest.revision ? "Results" : "Earlier results"} <ArrowRight />
          </Button>
        ) : null}
      </StepActions>
    </div>
  );
}
