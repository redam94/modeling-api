/**
 * The Model Explorer of one model version (M3.5): what the model learned, whether it can be
 * trusted and what it expects next. The fit wrote the parameter, diagnostic, fit, residual,
 * contribution, response-curve and IRF/FEVD views; forecasts and backtests come from an
 * explore run started here. Governance evidence and lineage complete the picture.
 */
import { Link } from "@tanstack/react-router";
import { FlaskConical, GitCompare, Settings2 } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import type { ModelVersion } from "@/api/types";
import { KeyValues, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { useAbout } from "@/features/about/api";
import { bareId } from "@/features/about/labels";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { mergeSections, useBaseIndex, useExploration, useSectionView, useStartExploration, type ExplorerTarget, type ModelExploreRequest, type SectionState, type SplitPlan } from "./api";
import { ExplorerNav, RecipePlots, RunBanner, SectionGate, type NavEntry } from "./explorer-shell";
import { GovernancePanel } from "./governance-panel";
import { LineagePanel } from "./lineage-panel";
import { BacktestSection, DecompositionSection, DiagnosticsSection, FevdSection, FitSection, ForecastSection, IrfSection, ParamsSection, ResidualsSection, ResponseCurvesSection } from "./model-sections";
import { diagnosticsReading, fitReading } from "./plain";
import { MODEL_SECTION_META } from "./sections-meta";
import { num, PlainSummary, SectionCard } from "./ui";
import type { ModelSection, SectionViews } from "./views";

export type ModelTab = Exclude<ModelSection, "comparison"> | "summary" | "governance" | "lineage";

/** The view sections the explorer lists (comparison has its own page). */
export const MODEL_ORDER: readonly Exclude<ModelSection, "comparison">[] = ["params", "decomposition", "response_curves", "irf", "fevd", "diagnostics", "fit", "residuals", "backtest", "forecast"];

const ON_DEMAND: readonly ModelSection[] = ["forecast", "backtest"];

export function defaultBacktest(): SplitPlan {
  return { kind: "expanding_window", horizons: [1, 4], n_folds: 4 };
}

/** The request of "compute the rest": the on-demand sections still missing. */
export function defaultModelRequest(missing: readonly ModelSection[]): ModelExploreRequest {
  const sections = missing.filter((s) => ON_DEMAND.includes(s));
  const want = sections.length ? sections : [...ON_DEMAND];
  return { sections: want, horizon: 12, ...(want.includes("backtest") ? { backtest: defaultBacktest() } : {}) };
}

// ------------------------------------------------------------------------------ options

function parseHorizons(text: string): number[] {
  return [...new Set(text.split(/[\s,]+/).map(Number).filter((n) => Number.isInteger(n) && n > 0))].sort((a, b) => a - b);
}

function ModelOptionsDialog({ open, onOpenChange, initial, onRun, pending }: { open: boolean; onOpenChange: (o: boolean) => void; initial: ModelExploreRequest; onRun: (r: ModelExploreRequest) => void; pending: boolean }) {
  const [sections, setSections] = useState<ModelSection[]>([...(initial.sections ?? ON_DEMAND)] as ModelSection[]);
  const [horizon, setHorizon] = useState(initial.horizon ?? 12);
  const [interval, setIntervalKind] = useState<NonNullable<ModelExploreRequest["interval"]>>(initial.interval ?? "hdi_90");
  const [kind, setKind] = useState<"expanding_window" | "rolling_origin">("expanding_window");
  const [horizons, setHorizons] = useState("1, 4");
  const [folds, setFolds] = useState(4);
  const [minTrain, setMinTrain] = useState(24);
  const [gap, setGap] = useState(0);
  const [trainWindow, setTrainWindow] = useState(52);
  const hs = parseHorizons(horizons);
  const needsPlan = sections.includes("backtest");
  const valid = sections.length > 0 && (!needsPlan || hs.length > 0) && horizon >= 1 && horizon <= 520;
  const choices: ModelSection[] = ["forecast", "backtest", "params", "diagnostics", "fit", "residuals", "decomposition", "response_curves", "irf", "fevd"];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide>
        <DialogTitle>Explore with options</DialogTitle>
        <DialogDescription>An explore run computes these views in the sandbox. A backtest fits the model again on each fold (maximum 10 folds).</DialogDescription>
        <form
          className="grid gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!valid) return;
            const backtest = needsPlan
              ? ({ kind, horizons: hs, n_folds: folds, min_train: minTrain, gap, ...(kind === "rolling_origin" ? { window: trainWindow } : {}) } as SplitPlan)
              : undefined;
            onRun({ sections, horizon, interval, ...(backtest ? { backtest } : {}) });
          }}
        >
          <fieldset className="grid gap-2">
            <legend className="text-[13px] font-medium">Views</legend>
            <div className="grid gap-1.5 sm:grid-cols-3">
              {choices.map((s) => (
                <label key={s} className="flex items-center gap-2 text-[13px]">
                  <Checkbox checked={sections.includes(s)} onCheckedChange={(c) => setSections((x) => (c ? [...x, s] : x.filter((y) => y !== s)))} aria-label={MODEL_SECTION_META[s].title} />
                  {MODEL_SECTION_META[s].title}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Forecast horizon (periods)" htmlFor="mx-h">
              <Input id="mx-h" type="number" min={1} max={520} value={horizon} onChange={(e) => setHorizon(Number(e.target.value))} />
            </Field>
            <Field label="Parameter intervals" htmlFor="mx-int">
              <NativeSelect id="mx-int" value={interval} onChange={(e) => setIntervalKind(e.target.value as typeof interval)}>
                <option value="hdi_90">90% (highest density / confidence)</option>
                <option value="hdi_95">95% highest density</option>
                <option value="ci_90">90% confidence</option>
                <option value="ci_95">95% confidence</option>
              </NativeSelect>
            </Field>
          </div>
          {needsPlan ? (
            <fieldset className="grid gap-3 rounded-lg border p-4">
              <legend className="px-1 text-[13px] font-medium">Backtest plan</legend>
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label="Plan" htmlFor="mx-kind">
                  <NativeSelect id="mx-kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
                    <option value="expanding_window">Expanding window</option>
                    <option value="rolling_origin">Rolling origin (fixed window)</option>
                  </NativeSelect>
                </Field>
                <Field label="Horizons" htmlFor="mx-hs" hint="Type the periods ahead, for example 1, 4, 8." error={hs.length ? undefined : "There is no horizon. Type one or more horizons."}>
                  <Input id="mx-hs" value={horizons} onChange={(e) => setHorizons(e.target.value)} />
                </Field>
                <Field label="Folds" htmlFor="mx-folds">
                  <Input id="mx-folds" type="number" min={1} max={10} value={folds} onChange={(e) => setFolds(Number(e.target.value))} />
                </Field>
                <Field label="Fewest training periods" htmlFor="mx-min">
                  <Input id="mx-min" type="number" min={1} value={minTrain} onChange={(e) => setMinTrain(Number(e.target.value))} />
                </Field>
                <Field label="Gap (periods not scored)" htmlFor="mx-gap" hint="Use a gap for data that arrives late.">
                  <Input id="mx-gap" type="number" min={0} value={gap} onChange={(e) => setGap(Number(e.target.value))} />
                </Field>
                {kind === "rolling_origin" ? (
                  <Field label="Training window" htmlFor="mx-win">
                    <Input id="mx-win" type="number" min={1} value={trainWindow} onChange={(e) => setTrainWindow(Number(e.target.value))} />
                  </Field>
                ) : null}
              </div>
            </fieldset>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid || pending}>
              {pending ? <Spinner /> : <FlaskConical />} Explore
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

// ------------------------------------------------------------------------------ sections

function SectionContent({ section, view, expert }: { section: ModelSection; view: SectionViews[ModelSection]; expert: boolean }): ReactNode {
  switch (section) {
    case "params":
      return <ParamsSection view={view as SectionViews["params"]} expert={expert} />;
    case "diagnostics":
      return <DiagnosticsSection view={view as SectionViews["diagnostics"]} expert={expert} />;
    case "fit":
      return <FitSection view={view as SectionViews["fit"]} expert={expert} />;
    case "residuals":
      return <ResidualsSection view={view as SectionViews["residuals"]} expert={expert} />;
    case "decomposition":
      return <DecompositionSection view={view as SectionViews["decomposition"]} expert={expert} />;
    case "response_curves":
      return <ResponseCurvesSection view={view as SectionViews["response_curves"]} expert={expert} />;
    case "irf":
      return <IrfSection view={view as SectionViews["irf"]} expert={expert} />;
    case "fevd":
      return <FevdSection view={view as SectionViews["fevd"]} expert={expert} />;
    case "forecast":
      return <ForecastSection view={view as SectionViews["forecast"]} expert={expert} />;
    case "backtest":
      return <BacktestSection view={view as SectionViews["backtest"]} expert={expert} />;
    case "comparison":
      return null;
  }
}

function SectionPanel({ target, section, state, runId, expert, onCompute, canRun, running }: { target: ExplorerTarget; section: ModelSection; state: SectionState | undefined; runId: string | null; expert: boolean; onCompute: () => void; canRun: boolean; running: boolean }) {
  const view = useSectionView(target, state, runId, section);
  const meta = MODEL_SECTION_META[section];
  return (
    <SectionCard title={meta.title} description={meta.question} id={`section-${section}`}>
      <SectionGate
        state={state}
        isPending={view.isPending}
        error={view.error}
        notComputed={
          <div className="grid justify-items-start gap-3 rounded-lg border border-dashed p-5 text-[13px]" data-testid="section-missing">
            <p className="text-muted-foreground">
              {section === "backtest" ? "A backtest fits the model again on earlier parts of the history. Then it scores each fit on the periods that came next. It runs in the sandbox and can take some minutes." : "The sandbox computes this view when you ask for it."}
            </p>
            <Button size="sm" onClick={onCompute} disabled={!canRun || running}>
              {running ? <Spinner /> : <FlaskConical />} Compute it now
            </Button>
          </div>
        }
      >
        {view.data ? <SectionContent section={section} view={view.data} expert={expert} /> : null}
        <RecipePlots target={target} state={state} runId={runId} expert={expert} />
      </SectionGate>
    </SectionCard>
  );
}

// ------------------------------------------------------------------------------ summary

function SummaryPanel({ mv, target, states, runId, expert, onSection }: { mv: ModelVersion; target: ExplorerTarget; states: SectionState<Exclude<ModelSection, "comparison">>[]; runId: string | null; expert: boolean; onSection: (t: ModelTab) => void }) {
  const byId = new Map(states.map((s) => [s.section, s]));
  const diag = useSectionView(target, byId.get("diagnostics"), runId, "diagnostics");
  const fit = useSectionView(target, byId.get("fit"), runId, "fit");
  const modelId = mv.lineage.plugin ? bareId(mv.lineage.plugin) : null;
  const { about, entry } = useAbout("model", modelId);
  const readings = [diag.data ? diagnosticsReading(diag.data) : null, fit.data ? fitReading(fit.data) : null].filter((r) => r !== null);
  const worst = readings.some((r) => r.tone === "bad") ? "bad" : readings.some((r) => r.tone === "warn") ? "warn" : readings.length ? "good" : "neutral";
  const ready = states.filter((s) => s.status === "ok");
  return (
    <div className="grid gap-5">
      <SectionCard title="At a glance" description="The short answer: did the fit work, and how close is the model to what happened?">
        <PlainSummary tone={worst}>
          {readings.length ? readings.map((r) => <p key={r.text}>{r.text}</p>) : <p>This version has no fit-time views.</p>}
        </PlainSummary>
        {Object.keys(mv.metrics ?? {}).length ? (
          <KeyValues items={Object.entries(mv.metrics ?? {}).map(([k, v]) => [<span key={k} className="font-mono text-xs">{k}</span>, <span key="v" className="tabular">{num(v)}</span>])} />
        ) : null}
        <ul className="flex flex-wrap gap-1.5" aria-label="Views">
          {ready.map((s) => (
            <li key={s.section}>
              <Button variant="outline" size="sm" onClick={() => onSection(s.section)}>
                {MODEL_SECTION_META[s.section].title}
              </Button>
            </li>
          ))}
        </ul>
      </SectionCard>
      {about && entry ? (
        <SectionCard title="About this kind of model" description="What it is for and what it assumes.">
          <AboutCard about={about} kind="model" id={entry.id} expertOpen={expert} />
        </SectionCard>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------ the explorer

export interface ModelExplorerProps {
  mv: ModelVersion;
  modelId: string;
  version: number;
  tab: ModelTab;
  onTab: (t: ModelTab) => void;
  runId: string | null;
  onRun: (runId: string) => void;
  expert: boolean;
}

export function ModelExplorer({ mv, modelId, version, tab, onTab, runId, onRun, expert }: ModelExplorerProps) {
  const target = useMemo((): ExplorerTarget => ({ kind: "model", id: modelId, version }), [modelId, version]);
  const base = useBaseIndex(target);
  const run = useExploration(runId);
  const start = useStartExploration(target);
  const grants = useGrants();
  const canRun = can(grants, "create", "run");
  const [optionsOpen, setOptionsOpen] = useState(false);
  const states = useMemo(() => mergeSections(MODEL_ORDER, base.data, run.data), [base.data, run.data]);
  const byId = new Map(states.map((s) => [s.section, s]));
  const missing = states.filter((s) => s.status === "missing").map((s) => s.section);
  const running = Boolean(run.data && !run.data.done) || start.isPending;
  const explore = (req: ModelExploreRequest) =>
    start.mutate(req, {
      onSuccess: (r) => {
        setOptionsOpen(false);
        onRun(r.exploration.run.id);
        if (r.cached) toast.success("Already computed", "The sandbox computed these views before with the same options.");
      },
      onError: (e) => toast.error("The explore run did not start", errorMessage(e)),
    });

  // Sections that do not apply to this model (an MMM has no IRF) go to the end, greyed.
  const applicable = states.filter((s) => s.status !== "skipped");
  const notApplicable = states.filter((s) => s.status === "skipped");
  const entries: NavEntry[] = [
    { id: "summary", title: "At a glance", group: "Overview" },
    ...[...applicable, ...notApplicable].map((s) => ({ id: s.section, title: MODEL_SECTION_META[s.section].title, group: s.status === "skipped" ? "Does not apply" : MODEL_SECTION_META[s.section].group, state: s })),
    { id: "governance", title: "Governance", group: "Trust and origin" },
    { id: "lineage", title: "Lineage", group: "Trust and origin" },
  ];
  const groupOrder = ["Overview", "What it learned", "Can you trust it?", "Looking ahead", "Trust and origin", "Does not apply"];
  entries.sort((a, b) => groupOrder.indexOf(a.group) - groupOrder.indexOf(b.group));

  let body: ReactNode;
  if (tab === "summary") body = <SummaryPanel mv={mv} target={target} states={states} runId={runId} expert={expert} onSection={onTab} />;
  else if (tab === "governance")
    body = (
      <SectionCard title="Governance" description="Did this version pass the checks that your policies need?">
        <GovernancePanel modelId={modelId} version={version} expert={expert} />
      </SectionCard>
    );
  else if (tab === "lineage")
    body = (
      <SectionCard title="Lineage" description="The data, the spec and the runs that made this version.">
        <LineagePanel mv={mv} expert={expert} />
      </SectionCard>
    );
  else
    body = (
      <SectionPanel
        key={tab}
        target={target}
        section={tab}
        state={byId.get(tab)}
        runId={runId}
        expert={expert}
        canRun={canRun}
        running={running}
        onCompute={() => explore({ ...defaultModelRequest(missing), sections: [...new Set([tab, ...missing.filter((s) => ON_DEMAND.includes(s))])] })}
      />
    );

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card px-4 py-3">
        <p className="text-[13px] text-muted-foreground">
          {base.isPending ? "Loading the views…" : missing.length ? `The sandbox computes forecasts and backtests when you ask for them (${missing.map((s) => MODEL_SECTION_META[s].title.toLowerCase()).join(", ")}).` : "Every view is ready."}
        </p>
        <div className="flex flex-wrap gap-2">
          {missing.length ? (
            <Button onClick={() => explore(defaultModelRequest(missing))} disabled={!canRun || running}>
              {running ? <Spinner /> : <FlaskConical />} {expert ? "Compute forecast and backtest" : "Check how it forecasts"}
            </Button>
          ) : null}
          {expert ? (
            <Button variant="outline" onClick={() => setOptionsOpen(true)} disabled={!canRun || running}>
              <Settings2 /> Explore with options
            </Button>
          ) : null}
          <Button asChild variant="outline">
            <Link to="/models/compare" search={{ refs: [mv.ref] }}>
              <GitCompare /> Compare
            </Link>
          </Button>
        </div>
      </div>
      <RunBanner exploration={run.data} error={run.error} title="Computing the views" />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <ExplorerNav entries={entries} current={tab} onSelect={(id) => onTab(id as ModelTab)} label="Model Explorer sections" />
        <div className="min-w-0">{body}</div>
      </div>
      {optionsOpen ? <ModelOptionsDialog open onOpenChange={setOptionsOpen} initial={defaultModelRequest(missing)} onRun={explore} pending={start.isPending} /> : null}
    </div>
  );
}
