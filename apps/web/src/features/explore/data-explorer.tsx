/**
 * The Data Explorer of one dataset version (M3.5): every section of `mapi_spec.views` in one
 * place. The quick sections come from the ingest; the rest (and any section with other
 * options) from an explore run started here (`POST …/explore`, polled with
 * `GET /v1/explorations/{run_id}`). Guided mode shows each section's plain reading first;
 * Expert mode shows every statistic and the run options.
 */
import { FlaskConical, Settings2 } from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";

import { can } from "@/api/access";
import { errorMessage } from "@/api/client";
import type { DataSchema } from "@/api/types";
import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Switch } from "@/components/ui/overlays";
import { Field, Input, NativeSelect } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { toast } from "@/stores/toast";

import { mergeSections, useBaseIndex, useExploration, useSectionView, useStartExploration, type DataExploreRequest, type ExplorerTarget, type SectionState } from "./api";
import {
  AcfPacfSection,
  CcfSection,
  CorrelationSection,
  DistributionsSection,
  GroupsSection,
  MissingnessSection,
  OutliersSection,
  OverviewSection,
  QualitySection,
  StationaritySection,
  StlSection,
  TimeSeriesSection,
  VifSection,
} from "./data-sections";
import { ExplorerNav, RecipePlots, RunBanner, SectionGate, type NavEntry } from "./explorer-shell";
import { DATA_ORDER, DATA_SECTION_META, periodUnit } from "./sections-meta";
import { SectionCard } from "./ui";
import { DATA_SECTIONS, type DataSection, type SectionViews } from "./views";

export interface DataExplorerProps {
  datasetId: string;
  version: number;
  schema: DataSchema | null | undefined;
  section: DataSection;
  onSection: (s: DataSection) => void;
  runId: string | null;
  onRun: (runId: string) => void;
  expert: boolean;
}

/** The request of the "compute the rest" button: every section the page does not have yet. */
export function defaultDataRequest(schema: DataSchema | null | undefined, missing: readonly DataSection[]): DataExploreRequest {
  const kpi = Object.entries(schema?.vars ?? {}).find(([, v]) => v.role === "target")?.[0];
  return { sections: missing.length ? [...missing] : [...DATA_SECTIONS], ...(kpi ? { kpi } : {}) };
}

function categoricalDims(schema: DataSchema | null | undefined): string[] {
  return Object.entries(schema?.dims ?? {})
    .filter(([, d]) => d.kind === "categorical")
    .map(([k]) => k);
}

function timeFreq(schema: DataSchema | null | undefined): string | null {
  return Object.values(schema?.dims ?? {}).find((d) => d.kind === "time")?.freq ?? null;
}

// ------------------------------------------------------------------------------ options

function OptionsDialog({
  open,
  onOpenChange,
  schema,
  initial,
  onRun,
  pending,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  schema: DataSchema | null | undefined;
  initial: DataExploreRequest;
  onRun: (req: DataExploreRequest) => void;
  pending: boolean;
}) {
  const [req, setReq] = useState<DataExploreRequest>(initial);
  const vars = Object.keys(schema?.vars ?? {});
  const dims = categoricalDims(schema);
  const sections = req.sections ?? [...DATA_SECTIONS];
  const set = (patch: Partial<DataExploreRequest>) => setReq((r) => ({ ...r, ...patch }));
  const valid = sections.length > 0 && (req.facet_by ?? []).length <= 2;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide>
        <DialogTitle>Explore with options</DialogTitle>
        <DialogDescription>An explore run computes the views that you select. It runs in the sandbox. If you ask again with the same options, you get the result immediately.</DialogDescription>
        <form
          className="grid gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (valid) onRun({ ...req, sections });
          }}
        >
          <fieldset className="grid gap-2">
            <legend className="text-[13px] font-medium">Views</legend>
            <div className="grid gap-1.5 sm:grid-cols-3">
              {DATA_ORDER.map((s) => (
                <label key={s} className="flex items-center gap-2 text-[13px]">
                  <Checkbox checked={sections.includes(s)} onCheckedChange={(c) => set({ sections: c ? [...sections, s] : sections.filter((x) => x !== s) })} aria-label={DATA_SECTION_META[s].title} />
                  {DATA_SECTION_META[s].title}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Result (KPI) for leads and lags" htmlFor="ex-kpi">
              <NativeSelect id="ex-kpi" value={req.kpi ?? ""} onChange={(e) => set({ kpi: e.target.value || null })}>
                <option value="">The dataset&apos;s target</option>
                {vars.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Split series by" htmlFor="ex-facet" hint="Select a maximum of two dimensions, for example markets and products. The server adds or averages the other dimensions.">
              <NativeSelect
                id="ex-facet"
                value={(req.facet_by ?? [])[0] ?? ""}
                onChange={(e) => set({ facet_by: e.target.value ? [e.target.value, ...(req.facet_by ?? []).slice(1).filter((d) => d !== e.target.value)] : [] })}
              >
                <option value="">Do not split (totals)</option>
                {dims.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Compare groups along" htmlFor="ex-group">
              <NativeSelect id="ex-group" value={req.group_by ?? ""} onChange={(e) => set({ group_by: e.target.value || null })}>
                <option value="">The first category dimension</option>
                {dims.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <Field label="Lags to look at" htmlFor="ex-lag" hint="Type the number of lags for ACF/PACF and for leads and lags. The range is 1 to 200.">
              <Input id="ex-lag" type="number" min={1} max={200} value={req.max_lag ?? 26} onChange={(e) => set({ max_lag: Number(e.target.value) })} />
            </Field>
            <Field label="Season length (periods)" htmlFor="ex-period" hint="Type the number of periods in one season. If empty, the server uses the frequency: 52 for weekly, 12 for monthly, 7 for daily data.">
              <Input id="ex-period" type="number" min={2} max={400} value={req.period ?? ""} onChange={(e) => set({ period: e.target.value ? Number(e.target.value) : null })} />
            </Field>
            <Field label="Significance level" htmlFor="ex-alpha">
              <Input id="ex-alpha" type="number" step={0.01} min={0.001} max={0.49} value={req.alpha ?? 0.05} onChange={(e) => set({ alpha: Number(e.target.value) })} />
            </Field>
            <Field label="Correlation" htmlFor="ex-corr">
              <NativeSelect id="ex-corr" value={req.correlation ?? "pearson"} onChange={(e) => set({ correlation: e.target.value as "pearson" | "spearman" })}>
                <option value="pearson">Pearson (linear)</option>
                <option value="spearman">Spearman (ranks)</option>
              </NativeSelect>
            </Field>
            <Field label="Unusual values" htmlFor="ex-out" hint="Select a robust score. Then type its threshold (on a standard-normal scale).">
              <div className="flex gap-2">
                <NativeSelect id="ex-out" value={req.outliers ?? "mad"} onChange={(e) => set({ outliers: e.target.value as "mad" | "iqr" | "zscore" })} className="w-32">
                  <option value="mad">MAD</option>
                  <option value="iqr">IQR</option>
                  <option value="zscore">z-score</option>
                </NativeSelect>
                <Input aria-label="Threshold" type="number" step={0.5} min={0.5} max={20} value={req.outlier_threshold ?? 3.5} onChange={(e) => set({ outlier_threshold: Number(e.target.value) })} />
              </div>
            </Field>
            <Field label="Series per view" htmlFor="ex-max">
              <Input id="ex-max" type="number" min={1} max={200} value={req.max_series ?? 50} onChange={(e) => set({ max_series: Number(e.target.value) })} />
            </Field>
            <label className="flex items-center gap-3 self-end pb-2 text-[13px]">
              <Switch checked={req.prewhiten ?? true} onCheckedChange={(c) => set({ prewhiten: c })} aria-label="Prewhiten before cross-correlating" />
              Prewhiten before the cross-correlation (this prevents false peaks)
            </label>
          </div>
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

// ------------------------------------------------------------------------------ one section

function SectionContent({ section, view, expert, unit }: { section: DataSection; view: SectionViews[DataSection]; expert: boolean; unit: string }): ReactNode {
  switch (section) {
    case "overview":
      return <OverviewSection view={view as SectionViews["overview"]} expert={expert} />;
    case "distributions":
      return <DistributionsSection view={view as SectionViews["distributions"]} expert={expert} />;
    case "timeseries":
      return <TimeSeriesSection view={view as SectionViews["timeseries"]} expert={expert} />;
    case "missingness":
      return <MissingnessSection view={view as SectionViews["missingness"]} expert={expert} />;
    case "outliers":
      return <OutliersSection view={view as SectionViews["outliers"]} expert={expert} />;
    case "correlation":
      return <CorrelationSection view={view as SectionViews["correlation"]} expert={expert} />;
    case "ccf":
      return <CcfSection view={view as SectionViews["ccf"]} expert={expert} unit={unit} />;
    case "acf_pacf":
      return <AcfPacfSection view={view as SectionViews["acf_pacf"]} expert={expert} />;
    case "stationarity":
      return <StationaritySection view={view as SectionViews["stationarity"]} expert={expert} />;
    case "stl":
      return <StlSection view={view as SectionViews["stl"]} expert={expert} />;
    case "vif":
      return <VifSection view={view as SectionViews["vif"]} expert={expert} />;
    case "groups":
      return <GroupsSection view={view as SectionViews["groups"]} expert={expert} />;
    case "quality":
      return <QualitySection view={view as SectionViews["quality"]} expert={expert} />;
  }
}

function SectionPanel({
  target,
  section,
  state,
  runId,
  expert,
  unit,
  onCompute,
  canRun,
  running,
}: {
  target: ExplorerTarget;
  section: DataSection;
  state: SectionState | undefined;
  runId: string | null;
  expert: boolean;
  unit: string;
  onCompute: () => void;
  canRun: boolean;
  running: boolean;
}) {
  const view = useSectionView(target, state, runId, section);
  const meta = DATA_SECTION_META[section];
  return (
    <SectionCard title={meta.title} description={meta.question} id={`section-${section}`}>
      <SectionGate
        state={state}
        isPending={view.isPending}
        error={view.error}
        notComputed={
          <div className="grid justify-items-start gap-3 rounded-lg border border-dashed p-5 text-[13px]" data-testid="section-missing">
            <p className="text-muted-foreground">The sandbox computes this view when you ask for it. Usually, this takes less than one minute.</p>
            <Button size="sm" onClick={onCompute} disabled={!canRun || running}>
              {running ? <Spinner /> : <FlaskConical />} Compute it now
            </Button>
            {!canRun ? <p className="text-xs text-muted-foreground">Your role cannot start runs in this project.</p> : null}
          </div>
        }
      >
        {view.data ? <SectionContent section={section} view={view.data} expert={expert} unit={unit} /> : null}
        <RecipePlots target={target} state={state} runId={runId} expert={expert} />
      </SectionGate>
    </SectionCard>
  );
}

// ------------------------------------------------------------------------------ the explorer

export function DataExplorer({ datasetId, version, schema, section, onSection, runId, onRun, expert }: DataExplorerProps) {
  const target = useMemo((): ExplorerTarget => ({ kind: "dataset", id: datasetId, version }), [datasetId, version]);
  const base = useBaseIndex(target);
  const run = useExploration(runId);
  const start = useStartExploration(target);
  const grants = useGrants();
  const canRun = can(grants, "create", "run");
  const [optionsOpen, setOptionsOpen] = useState(false);
  const states = useMemo(() => mergeSections(DATA_ORDER, base.data, run.data), [base.data, run.data]);
  const byId = new Map(states.map((s) => [s.section, s]));
  const missing = states.filter((s) => s.status === "missing").map((s) => s.section);
  const running = Boolean(run.data && !run.data.done) || start.isPending;
  const unit = periodUnit(timeFreq(schema));

  const explore = (req: DataExploreRequest) =>
    start.mutate(req, {
      onSuccess: (r) => {
        setOptionsOpen(false);
        onRun(r.exploration.run.id);
        if (r.cached) toast.success("Already computed", "The sandbox computed these views before with the same options.");
      },
      onError: (e) => toast.error("The explore run did not start", errorMessage(e)),
    });

  const entries: NavEntry[] = DATA_ORDER.map((s) => ({ id: s, title: DATA_SECTION_META[s].title, group: DATA_SECTION_META[s].group, state: byId.get(s) }));

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card px-4 py-3">
        <p className="text-[13px] text-muted-foreground">
          {base.isPending
            ? "Loading the views…"
            : missing.length
              ? `${states.length - missing.length} of ${states.length} views are ready. The sandbox computes the others (${missing.map((s) => DATA_SECTION_META[s].title.toLowerCase()).join(", ")}) when you ask for them.`
              : "Every view is ready."}
        </p>
        <div className="flex flex-wrap gap-2">
          {missing.length ? (
            <Button onClick={() => explore(defaultDataRequest(schema, missing))} disabled={!canRun || running}>
              {running ? <Spinner /> : <FlaskConical />} {expert ? "Compute the remaining views" : "Run the deeper checks"}
            </Button>
          ) : null}
          {expert ? (
            <Button variant="outline" onClick={() => setOptionsOpen(true)} disabled={!canRun || running}>
              <Settings2 /> Explore with options
            </Button>
          ) : null}
        </div>
      </div>
      <RunBanner exploration={run.data} error={run.error} title="Computing the views" />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <ExplorerNav entries={entries} current={section} onSelect={(id) => onSection(id as DataSection)} label="Data Explorer sections" />
        <SectionPanel
          key={section}
          target={target}
          section={section}
          state={byId.get(section)}
          runId={runId}
          expert={expert}
          unit={unit}
          canRun={canRun}
          running={running}
          onCompute={() => explore({ ...defaultDataRequest(schema, missing), sections: [...new Set([section, ...missing])] })}
        />
      </div>
      {optionsOpen ? <OptionsDialog open onOpenChange={setOptionsOpen} schema={schema} initial={defaultDataRequest(schema, missing)} onRun={explore} pending={start.isPending} /> : null}
    </div>
  );
}
