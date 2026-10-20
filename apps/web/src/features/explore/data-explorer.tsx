/**
 * The Data Explorer of one dataset version (M3.5): every section of `mapi_spec.views` in one
 * place. The quick sections come from the ingest; the rest (and any section with other
 * options) from an explore run started here (`POST …/explore`, polled with
 * `GET /v1/explorations/{run_id}`). Guided mode shows each section's plain reading first;
 * Expert mode shows every statistic and the run options.
 *
 * v0.10: the runner detects the shapes of the data (images, maps, networks, trees, directions,
 * points, series) and orders the sections by relevance; the sections that do not apply leave
 * the list for a closed "Not for this data" list (they still open from a link, `shapes.ts`).
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
import { GallerySection } from "./gallery-section";
import { CircularSection, ImagesSection, MapSection, NetworkSection, PointsSection, ProfilesSection, StructureSection, TreeSection } from "./science-sections";
import { DATA_GROUPS, DATA_ORDER, DATA_SECTION_META, periodUnit } from "./sections-meta";
import { missingSections, navPlan, readyText, runIndex } from "./shapes";
import { SectionCard } from "./ui";
import { DATA_SECTIONS, type DataSection, type SectionViews, type StructureHints } from "./views";

export interface DataExplorerProps {
  datasetId: string;
  version: number;
  schema: DataSchema | null | undefined;
  section: DataSection;
  onSection: (s: DataSection) => void;
  runId: string | null;
  onRun: (runId: string) => void;
  expert: boolean;
  /** M4: the node of a tree dataset (its schema is `schema`); `null` for a plain dataset. */
  node?: string | null;
}

/** The request of the "compute the rest" button: every section the page does not have yet. */
export function defaultDataRequest(schema: DataSchema | null | undefined, missing: readonly DataSection[]): DataExploreRequest {
  const kpi = Object.entries(schema?.vars ?? {}).find(([, v]) => v.role === "target")?.[0];
  return { sections: missing.length ? [...missing] : [...DATA_SECTIONS], ...(kpi ? { kpi } : {}) };
}

function allDims(schema: DataSchema | null | undefined): string[] {
  return Object.keys(schema?.dims ?? {});
}

type AngleUnits = NonNullable<StructureHints["angle_vars"]>[string];

const ANGLE_UNITS: readonly { value: AngleUnits; label: string }[] = [
  { value: "deg", label: "degrees" },
  { value: "rad", label: "radians" },
  { value: "h", label: "hours (time of day)" },
  { value: "d", label: "days (day of the year)" },
  { value: "month", label: "months" },
];

/** The hint problems in plain words (the server refuses them too). */
export function hintProblem(h: StructureHints | null | undefined): string | null {
  if (!h) return null;
  if (Boolean(h.y_dim) !== Boolean(h.x_dim)) return "Select the rows and the columns of the image, or neither.";
  if (h.y_dim && h.y_dim === h.x_dim) return "The rows and the columns of the image must be different dimensions.";
  return null;
}

/** The hints without empty fields (`null` when no hint is set). */
export function cleanHints(h: StructureHints | null | undefined): StructureHints | null {
  if (!h) return null;
  const out: StructureHints = {};
  for (const k of ["y_dim", "x_dim", "frame_dim", "node_dim", "time_dim"] as const) if (h[k]) out[k] = h[k];
  const angles = Object.entries(h.angle_vars ?? {}).filter(([v, u]) => v && u);
  if (angles.length) out.angle_vars = Object.fromEntries(angles);
  return Object.keys(out).length ? out : null;
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
  const anyDims = allDims(schema);
  const sections = req.sections ?? [...DATA_SECTIONS];
  const set = (patch: Partial<DataExploreRequest>) => setReq((r) => ({ ...r, ...patch }));
  const hints = req.hints ?? {};
  const setHint = (patch: Partial<StructureHints>) => setReq((r) => ({ ...r, hints: { ...(r.hints ?? {}), ...patch } }));
  const [angleVar, angleUnits] = Object.entries(hints.angle_vars ?? {})[0] ?? ["", "deg"];
  const problem = hintProblem(cleanHints(req.hints));
  const dimSelect = (id: string, key: "y_dim" | "x_dim" | "frame_dim" | "node_dim" | "time_dim", label: string, hint?: string) => (
    <Field label={label} htmlFor={id} {...(hint ? { hint } : {})}>
      <NativeSelect id={id} value={hints[key] ?? ""} onChange={(e) => setHint({ [key]: e.target.value || null })}>
        <option value="">Find it automatically</option>
        {anyDims.map((d) => (
          <option key={d} value={d}>
            {d}
          </option>
        ))}
      </NativeSelect>
    </Field>
  );
  const valid = sections.length > 0 && (req.facet_by ?? []).length <= 2 && hintProblem(cleanHints(req.hints)) === null;
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent wide>
        <DialogTitle>Explore with options</DialogTitle>
        <DialogDescription>An explore run computes the views that you select. It runs in the sandbox. If you ask again with the same options, you get the result immediately.</DialogDescription>
        <form
          className="grid gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!valid) return;
            const { hints: raw, ...rest } = req;
            const clean = cleanHints(raw);
            onRun({ ...rest, sections, ...(clean ? { hints: clean } : {}) });
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
          <fieldset className="grid gap-3 rounded-lg border p-4" data-testid="explore-hints">
            <legend className="px-1 text-[13px] font-medium">Shape of the data (hints)</legend>
            <p className="text-xs text-muted-foreground">The explorer finds images, maps, networks, trees, directions and series automatically. If it finds the wrong shape, tell it which dimension has which role. A hint changes only that dimension or variable.</p>
            <div className="grid gap-4 sm:grid-cols-2">
              {dimSelect("hint-y", "y_dim", "Rows of an image (y)")}
              {dimSelect("hint-x", "x_dim", "Columns of an image (x)")}
              {dimSelect("hint-frame", "frame_dim", "Frames of an image or a movie")}
              {dimSelect("hint-node", "node_dim", "Nodes of a network")}
              {dimSelect("hint-time", "time_dim", "Time", "Select the dimension that is time. Only a time dimension gives the time views (memory, trend and season).")}
              <Field label="Angle or time of day" htmlFor="hint-angle" hint="Select a variable that is a direction or a time of day, and its units.">
                <div className="flex gap-2">
                  <NativeSelect id="hint-angle" value={angleVar} onChange={(e) => setHint({ angle_vars: e.target.value ? { [e.target.value]: angleUnits } : {} })}>
                    <option value="">Find them automatically</option>
                    {vars.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </NativeSelect>
                  <NativeSelect aria-label="Units of the angle" value={angleUnits} disabled={!angleVar} onChange={(e) => setHint({ angle_vars: angleVar ? { [angleVar]: e.target.value as AngleUnits } : {} })} className="w-44">
                    {ANGLE_UNITS.map((u) => (
                      <option key={u.value} value={u.value}>
                        {u.label}
                      </option>
                    ))}
                  </NativeSelect>
                </div>
              </Field>
            </div>
            {problem ? (
              <p role="alert" className="text-xs text-destructive">
                {problem}
              </p>
            ) : null}
          </fieldset>
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

function SectionContent({ section, view, expert, unit, onSection }: { section: DataSection; view: SectionViews[DataSection]; expert: boolean; unit: string; onSection: (s: DataSection) => void }): ReactNode {
  switch (section) {
    case "overview":
      return <OverviewSection view={view as SectionViews["overview"]} expert={expert} onSection={onSection} />;
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
    case "structure":
      return <StructureSection view={view as SectionViews["structure"]} expert={expert} onSection={onSection} />;
    case "images":
      return <ImagesSection view={view as SectionViews["images"]} expert={expert} />;
    case "gallery":
      return <GallerySection view={view as SectionViews["gallery"]} expert={expert} />;
    case "profiles":
      return <ProfilesSection view={view as SectionViews["profiles"]} expert={expert} />;
    case "map":
      return <MapSection view={view as SectionViews["map"]} expert={expert} />;
    case "network":
      return <NetworkSection view={view as SectionViews["network"]} expert={expert} />;
    case "tree":
      return <TreeSection view={view as SectionViews["tree"]} expert={expert} />;
    case "circular":
      return <CircularSection view={view as SectionViews["circular"]} expert={expert} />;
    case "points":
      return <PointsSection view={view as SectionViews["points"]} expert={expert} />;
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
  onSection,
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
  onSection: (s: DataSection) => void;
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
        {view.data ? <SectionContent section={section} view={view.data} expert={expert} unit={unit} onSection={onSection} /> : null}
        <RecipePlots target={target} state={state} runId={runId} expert={expert} />
      </SectionGate>
    </SectionCard>
  );
}

// ------------------------------------------------------------------------------ the explorer

export function DataExplorer({ datasetId, version, schema, section, onSection, runId, onRun, expert, node = null }: DataExplorerProps) {
  const target = useMemo((): ExplorerTarget => ({ kind: "dataset", id: datasetId, version, node }), [datasetId, version, node]);
  const base = useBaseIndex(target);
  const run = useExploration(runId);
  const start = useStartExploration(target);
  const grants = useGrants();
  const canRun = can(grants, "create", "run");
  const [optionsOpen, setOptionsOpen] = useState(false);
  const states = useMemo(() => mergeSections(DATA_ORDER, base.data, run.data), [base.data, run.data]);
  const byId = new Map(states.map((s) => [s.section, s]));
  const plan = useMemo(() => navPlan(states, [runIndex(run.data), base.data]), [states, run.data, base.data]);
  const missing = missingSections(states, plan);
  const listed = plan.shown.length;
  const ready = plan.shown.filter((s) => (byId.get(s)?.status ?? "missing") !== "missing").length;
  const running = Boolean(run.data && !run.data.done) || start.isPending;
  const unit = periodUnit(timeFreq(schema));

  const explore = (req: DataExploreRequest) =>
    start.mutate(node ? { ...req, node } : req, {
      onSuccess: (r) => {
        setOptionsOpen(false);
        onRun(r.exploration.run.id);
        if (r.cached) toast.success("Already computed", "The sandbox computed these views before with the same options.");
      },
      onError: (e) => toast.error("The explore run did not start", errorMessage(e)),
    });

  const entries: NavEntry[] = plan.shown.map((s) => ({ id: s, title: DATA_SECTION_META[s].title, group: DATA_SECTION_META[s].group, state: byId.get(s) }));
  const hidden = plan.hidden.map((h) => ({ id: h.section, title: h.title, message: h.message }));

  return (
    <div className="grid gap-5">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-card px-4 py-3">
        <p className="text-[13px] text-muted-foreground">
          {base.isPending
            ? "Loading the views…"
            : missing.length
              ? readyText(ready, listed, missing, plan.shown)
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
        <ExplorerNav entries={entries} hidden={hidden} groupOrder={DATA_GROUPS} current={section} onSelect={(id) => onSection(id as DataSection)} label="Data Explorer sections" />
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
          onSection={onSection}
          onCompute={() => explore({ ...defaultDataRequest(schema, missing), sections: [...new Set([section, ...missing])] })}
        />
      </div>
      {optionsOpen ? <OptionsDialog open onOpenChange={setOptionsOpen} schema={schema} initial={defaultDataRequest(schema, missing)} onRun={explore} pending={start.isPending} /> : null}
    </div>
  );
}
