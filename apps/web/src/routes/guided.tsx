/**
 * Guided mode (plan §11, M3): task flows for analysts, built on the same specs Expert mode edits.
 *
 * - `/guided`: "What do you want to learn?" (templates) and the analyses in progress;
 * - `/guided/new/$templateId`: the data step of a new analysis (nothing is stored until upload);
 * - `/guided/$specId`: resume a saved analysis at the step it has reached;
 * - `/guided/$specId/$step`: one step of a saved analysis (`data`, `priors`, `run`, `report`,
 *   `explore`, `share`), rebuilt from server state so every step is resumable and linkable.
 */
import { getRouteApi, Link, Navigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { EmptyState, ErrorNotice, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ChooserTeaser } from "@/features/about/chooser-teaser";
import { useGuidedTemplate } from "@/features/guided/api";
import { GuidedFrame, type StepLink } from "@/features/guided/frame";
import { useEnterGuided } from "@/features/guided/persona";
import { hasTask, isStepId, stepsFor, valueAt, type GuidedStepId } from "@/features/guided/session";
import { DataSummaryStep } from "@/features/guided/steps/data-summary";
import { NewDataStep } from "@/features/guided/steps/data-step";
import { ExploreStep } from "@/features/guided/steps/explore-step";
import { PriorsStep } from "@/features/guided/steps/priors-step";
import { ReportStep } from "@/features/guided/steps/report-step";
import { RunStep } from "@/features/guided/steps/run-step";
import { ShareStep } from "@/features/guided/steps/share-step";
import { SessionList, TemplatePicker } from "@/features/guided/steps/template-step";
import { useGuidedSession, type GuidedSession } from "@/features/guided/use-session";
import { isTerminal } from "@/lib/run-state";

const newRoute = getRouteApi("/guided/new/$templateId");
const resumeRoute = getRouteApi("/guided/$specId");
const stepRoute = getRouteApi("/guided/$specId/$step");

export function GuidedPage() {
  useEnterGuided();
  return (
    <div className="mx-auto grid w-full max-w-6xl gap-8">
      <PageHeader
        title="What do you want to learn?"
        description="Pick the question closest to yours. We'll ask a few plain questions about your spreadsheet, fit a model in a secure sandbox and explain what it found - no statistics needed."
      />
      <ChooserTeaser title="Not sure which one fits? Help me choose" />
      <TemplatePicker />
      <section aria-labelledby="sessions-h" className="grid gap-3">
        <h2 id="sessions-h" className="text-[15px] font-semibold tracking-tight">
          Continue where you left off
        </h2>
        <SessionList />
      </section>
    </div>
  );
}

function steps(templateLike: Parameters<typeof stepsFor>[0], session: GuidedSession | null, current: GuidedStepId): StepLink[] {
  const defs = stepsFor(templateLike);
  const at = defs.findIndex((d) => d.id === current);
  return defs.map((def, i): StepLink => {
    if (!session) return { def, to: null, done: false };
    const fitted = Boolean(session.lastGoodFit);
    const reachable = ["data", "priors", "run"].includes(def.id) || fitted;
    const done =
      def.id === "data"
        ? session.datasetVersion?.status === "ready"
        : def.id === "priors"
          ? typeof valueAt(session.document, "/calibration") === "object" || session.fits.length > 0
          : def.id === "run"
            ? fitted
            : reachable && i < at;
    return { def, to: reachable ? { specId: session.spec.id, step: def.id } : null, done: Boolean(done) };
  });
}

export function GuidedNewPage() {
  const { templateId } = newRoute.useParams();
  const template = useGuidedTemplate(templateId);
  const [yaml, setYaml] = useState<string | null>(null);
  if (template.isPending) return <LoadingRows rows={6} />;
  if (template.isError) {
    return (
      <EmptyState
        title="This template does not exist"
        action={
          <Button asChild variant="outline">
            <Link to="/guided">See the templates</Link>
          </Button>
        }
      >
        The link may be out of date.
      </EmptyState>
    );
  }
  return (
    <GuidedFrame templateTitle={template.data.title} sessionName={null} steps={steps(template.data, null, "data")} current="data" yaml={yaml} specId={null}>
      <NewDataStep template={template.data} onYaml={setYaml} />
    </GuidedFrame>
  );
}

/** Where a saved analysis stands: the step to open when it is resumed. */
export function resumeStep(session: GuidedSession): GuidedStepId {
  const active = session.fits.find((r) => !isTerminal(r.state));
  if (active) return "run";
  if (session.lastGoodFit) return "report";
  if (hasTask(session.template, "business_priors") && valueAt(session.document, "/calibration") === undefined) return "priors";
  return "run";
}

function SessionGate({ specId, children }: { specId: string; children: (s: GuidedSession) => ReactNode }) {
  const state = useGuidedSession(specId);
  if (state.notGuided) {
    return (
      <EmptyState
        title="This analysis was not made in Guided mode"
        action={
          <Button asChild variant="outline">
            <Link to="/build/$specId" params={{ specId }}>
              Open it in Expert mode
            </Link>
          </Button>
        }
      >
        Its specification was written by hand, so there are no Guided steps for it.
      </EmptyState>
    );
  }
  if (state.error) return <ErrorNotice error={state.error} title="This analysis could not be loaded" />;
  if (!state.session) return <LoadingRows rows={6} />;
  return <>{children(state.session)}</>;
}

export function GuidedResumePage() {
  const { specId } = resumeRoute.useParams();
  return <SessionGate specId={specId}>{(s) => <Navigate to="/guided/$specId/$step" params={{ specId, step: resumeStep(s) }} replace />}</SessionGate>;
}

function StepBody({ session, step }: { session: GuidedSession; step: GuidedStepId }) {
  switch (step) {
    case "data":
      return <DataSummaryStep session={session} />;
    case "priors":
      return <PriorsStep session={session} />;
    case "run":
      return <RunStep session={session} />;
    case "report":
      return <ReportStep session={session} />;
    case "explore":
      return <ExploreStep session={session} />;
    case "share":
      return <ShareStep session={session} />;
  }
}

export function GuidedSessionPage() {
  const { specId, step } = stepRoute.useParams();
  if (!isStepId(step)) return <Navigate to="/guided/$specId" params={{ specId }} replace />;
  return (
    <SessionGate specId={specId}>
      {(s) => {
        const known = stepsFor(s.template).some((d) => d.id === step);
        if (!known) return <Navigate to="/guided/$specId" params={{ specId }} replace />;
        return (
          <GuidedFrame templateTitle={s.template.title} sessionName={s.spec.name} steps={steps(s.template, s, step)} current={step} yaml={s.latest.yaml} specId={s.spec.id} advanced={s.advanced}>
            <StepBody session={s} step={step} />
          </GuidedFrame>
        );
      }}
    </SessionGate>
  );
}
