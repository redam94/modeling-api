import { getRouteApi, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { useState } from "react";

import { useCatalog } from "@/api/queries";
import type { ModelCatalogEntry, StepCatalogEntry } from "@/api/types";
import { JsonBlock, KeyValues, Mono, QueryView } from "@/components/common";
import { AddYourOwn } from "@/components/extend/add-your-own";
import { DataTable } from "@/components/data-table";
import { Dialog, DialogContent, DialogDescription, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { CatalogBrowser } from "@/features/about/catalog-browser";
import { StartButton } from "@/features/about/chooser";
import { YoursTab, useTeamCount } from "@/features/extend/yours-tab";
import { GpuBadge } from "@/features/models/gpu-badge";
import { PlotCatalogGrid } from "@/features/plots/plot-catalog";

const route = getRouteApi("/catalog");

const modelColumns: ColumnDef<ModelCatalogEntry, unknown>[] = [
  {
    id: "id",
    header: "Model",
    accessorFn: (m) => m.manifest.id,
    cell: ({ getValue, row }) => (
      <span className="inline-flex items-center gap-1.5">
        <Mono className="font-medium">{String(getValue())}</Mono>
        <GpuBadge manifest={row.original.manifest} />
      </span>
    ),
  },
  { id: "version", header: "Version", accessorFn: (m) => m.manifest.version },
  { id: "about", header: "What it does", accessorFn: (m) => m.manifest.about?.summary_plain ?? "", cell: ({ getValue }) => <span className="line-clamp-1 max-w-[28rem] text-muted-foreground">{String(getValue())}</span> },
  { id: "inference", header: "Inference", accessorFn: (m) => m.manifest.capabilities.inference },
  { id: "repr", header: "Representation", accessorFn: (m) => m.manifest.capabilities.representation, cell: ({ getValue }) => <Badge variant="outline">{String(getValue())}</Badge> },
  { id: "modes", header: "Score modes", accessorFn: (m) => (m.manifest.capabilities.score_modes ?? []).join(", ") },
  { id: "image", header: "Base image", accessorFn: (m) => m.base_image ?? "—", cell: ({ getValue }) => <Mono className="text-muted-foreground">{String(getValue())}</Mono> },
];

const stepColumns: ColumnDef<StepCatalogEntry, unknown>[] = [
  { accessorKey: "id", header: "Step", cell: ({ getValue }) => <Mono className="font-medium">{String(getValue())}</Mono> },
  { accessorKey: "version", header: "Version" },
  { accessorKey: "kind", header: "Kind", cell: ({ getValue }) => <Badge variant="outline">{String(getValue())}</Badge> },
  { accessorKey: "stateful", header: "State", cell: ({ getValue }) => (getValue() ? "learns state" : "stateless") },
  { accessorKey: "invertible", header: "Invertible", cell: ({ getValue }) => (getValue() ? "yes" : "no") },
  { id: "summary", header: "What it does", accessorFn: (s) => s.about?.summary_plain ?? s.summary ?? "", cell: ({ getValue }) => <span className="line-clamp-1 text-muted-foreground">{String(getValue())}</span> },
];

type Selected = { kind: "model"; entry: ModelCatalogEntry } | { kind: "step"; entry: StepCatalogEntry };

function Detail({ selected, onClose }: { selected: Selected | null; onClose: () => void }) {
  if (!selected) return null;
  let title: string;
  let body: React.ReactNode;
  if (selected.kind === "model") {
    const m = selected.entry.manifest;
    title = `${m.id}@${m.version}`;
    body = (
      <div className="grid gap-4">
        {m.about ? <AboutCard about={m.about} kind="model" id={m.id} actions={<StartButton kind="model" id={m.id} />} /> : null}
        <h3 className="text-sm font-semibold">Technical details</h3>
        <KeyValues
          items={[
            ["Inference", m.capabilities.inference],
            ["Representation", m.capabilities.representation],
            ["Owns", (m.capabilities.owns ?? []).join(", ") || "—"],
            ["Plots", (m.plots ?? []).join(", ") || "—"],
            // v0.15: the resource classes of the model, and whether it can use the GPU class.
            ["Resource classes", <span key="rc" className="inline-flex items-center gap-1.5">{`${m.resources?.min_class ?? "S"} or larger, default ${m.resources?.default_class ?? "S"}`}<GpuBadge manifest={m} /></span>],
          ]}
        />
        <h3 className="text-sm font-semibold">Settings schema</h3>
        <JsonBlock value={m.settings_schema} maxHeight="18rem" />
        <h3 className="text-sm font-semibold">Fit schema</h3>
        <JsonBlock value={m.fit_schema} maxHeight="14rem" />
      </div>
    );
  } else {
    const s = selected.entry;
    title = `${s.id}@${s.version}`;
    body = (
      <div className="grid gap-4">
        {s.about ? <AboutCard about={s.about} kind="step" id={s.id} /> : s.summary ? <p>{s.summary}</p> : null}
        {s.about ? <h3 className="text-sm font-semibold">Technical details</h3> : null}
        <KeyValues items={[["Kind", s.kind], ["Stateful", s.stateful ? "yes" : "no"], ["Invertible", s.invertible ? "yes" : "no"], ["Context", s.context ? JSON.stringify(s.context) : "—"]]} />
        <h3 className="text-sm font-semibold">Schema effect</h3>
        <JsonBlock value={s.effect} maxHeight="14rem" />
        <h3 className="text-sm font-semibold">Settings schema</h3>
        <JsonBlock value={s.settings_schema} maxHeight="18rem" />
      </div>
    );
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle className="font-mono">{title}</DialogTitle>
        <DialogDescription>This is static catalog metadata. The control plane uses it to do a check of specs. It does not run plugin code.</DialogDescription>
        {body}
      </DialogContent>
    </Dialog>
  );
}

type CatalogTab = "yours" | "browse" | "models" | "steps" | "plots";

/**
 * The tab to show: the URL's (`?tab=`), or "yours" when it was picked here. The router's
 * search schema may not know "yours" yet, so the pick is also kept in the page state.
 */
export function activeCatalogTab(urlTab: string | undefined, pickedYours: boolean): CatalogTab {
  if (urlTab === "yours" || (pickedYours && urlTab === undefined)) return "yours";
  return urlTab === "models" || urlTab === "steps" || urlTab === "plots" ? urlTab : "browse";
}

export function CatalogPage() {
  const catalog = useCatalog();
  const { tab, about } = route.useSearch();
  const navigate = useNavigate({ from: "/catalog" });
  const [selected, setSelected] = useState<Selected | null>(null);
  const [pickedYours, setPickedYours] = useState(false);
  const team = useTeamCount();
  const active = activeCatalogTab(tab, pickedYours);
  const go = (t: string) => {
    setPickedYours(t === "yours");
    void navigate({ search: { tab: t === "browse" ? undefined : (t as never) } });
  };
  const yoursFirst = team.count > 0;
  const yoursTrigger = (
    <TabsTrigger value="yours">
      Yours {team.count ? <Badge variant="secondary">{team.count}</Badge> : null}
    </TabsTrigger>
  );
  return (
    <>
      <header className="grid gap-3 pb-8">
        <h1 className="display-title text-[clamp(2.25rem,5vw,3rem)] leading-[1.02] text-balance">Catalog</h1>
        <p className="max-w-[60ch] text-lg leading-relaxed text-muted-foreground">
          Every model you can fit, every preparation step, every chart, each with a card that says what it is good for. Plus whatever your team has added.
        </p>
        {catalog.data ? (
          <p className="text-xs text-muted-foreground">
            <Mono title={catalog.data.digest}>digest {catalog.data.digest.slice(0, 19)}</Mono>
          </p>
        ) : null}
      </header>
      <QueryView query={catalog}>
        {(c) => (
          <Tabs value={active} onValueChange={go}>
            <TabsList className="max-w-full flex-wrap">
              {yoursFirst ? yoursTrigger : null}
              <TabsTrigger value="browse">Browse</TabsTrigger>
              <TabsTrigger value="plots" data-tour="catalog.tab.plots">
                Chart types <Badge variant="secondary">{c.plots?.length ?? 0}</Badge>
              </TabsTrigger>
              {yoursFirst ? null : yoursTrigger}
              <span aria-hidden className="mx-1 hidden h-5 w-px bg-border-strong sm:inline-block" />
              <span aria-hidden className="hidden px-1 text-[10px] font-bold tracking-wide text-hint uppercase sm:inline">
                Technical view
              </span>
              <TabsTrigger value="models" title="Technical view: every model plugin, as a table">
                Models <Badge variant="secondary">{c.models?.length ?? 0}</Badge>
              </TabsTrigger>
              <TabsTrigger value="steps" title="Technical view: every preparation step, as a table">
                Steps <Badge variant="secondary">{c.steps?.length ?? 0}</Badge>
              </TabsTrigger>
            </TabsList>
            <TabsContent value="yours" className="pt-4">
              <YoursTab />
            </TabsContent>
            <TabsContent value="browse" className="grid gap-12 pt-4">
              <CatalogBrowser selected={about ?? null} onSelect={(key) => void navigate({ search: { about: key ?? undefined }, replace: true })} teamCount={team.count} onShowTeam={() => go("yours")} />
              <AddYourOwn kind="any" />
            </TabsContent>
            <TabsContent value="models" className="pt-4">
              <DataTable label="Model plugins" data={c.models ?? []} columns={modelColumns} filterPlaceholder="Filter models" onRowClick={(entry) => setSelected({ kind: "model", entry })} />
            </TabsContent>
            <TabsContent value="steps" className="pt-4">
              <DataTable label="Steps" data={c.steps ?? []} columns={stepColumns} filterPlaceholder="Filter steps" onRowClick={(entry) => setSelected({ kind: "step", entry })} />
            </TabsContent>
            <TabsContent value="plots" className="pt-4">
              <PlotCatalogGrid plots={c.plots ?? []} />
            </TabsContent>
          </Tabs>
        )}
      </QueryView>
      <Detail selected={selected} onClose={() => setSelected(null)} />
    </>
  );
}
