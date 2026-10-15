import { getRouteApi, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { useState } from "react";

import { useCatalog } from "@/api/queries";
import type { ModelCatalogEntry, PlotCatalogEntry, StepCatalogEntry } from "@/api/types";
import { JsonBlock, KeyValues, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Dialog, DialogContent, DialogDescription, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge } from "@/components/ui/primitives";

const route = getRouteApi("/catalog");

const modelColumns: ColumnDef<ModelCatalogEntry, unknown>[] = [
  { id: "id", header: "Model", accessorFn: (m) => m.manifest.id, cell: ({ getValue }) => <Mono className="font-medium">{String(getValue())}</Mono> },
  { id: "version", header: "Version", accessorFn: (m) => m.manifest.version },
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
  { accessorKey: "summary", header: "Summary", cell: ({ getValue }) => <span className="line-clamp-1 text-muted-foreground">{(getValue() as string | null) ?? ""}</span> },
];

const plotColumns: ColumnDef<PlotCatalogEntry, unknown>[] = [
  { accessorKey: "id", header: "Recipe", cell: ({ getValue }) => <Mono className="font-medium">{String(getValue())}</Mono> },
  { accessorKey: "title", header: "Title" },
  { accessorKey: "version", header: "Version", cell: ({ getValue }) => (getValue() as string | null) ?? "—" },
  { accessorKey: "persona", header: "Audience", cell: ({ getValue }) => (getValue() as string | null) ?? "both" },
  { id: "groups", header: "Needs groups", accessorFn: (p) => (p.requires_groups ?? []).join(", ") || "—" },
];

type Selected = { kind: "model"; entry: ModelCatalogEntry } | { kind: "step"; entry: StepCatalogEntry } | { kind: "plot"; entry: PlotCatalogEntry };

function Detail({ selected, onClose }: { selected: Selected | null; onClose: () => void }) {
  if (!selected) return null;
  let title: string;
  let body: React.ReactNode;
  if (selected.kind === "model") {
    const m = selected.entry.manifest;
    title = `${m.id}@${m.version}`;
    body = (
      <div className="grid gap-4">
        <KeyValues items={[["Inference", m.capabilities.inference], ["Representation", m.capabilities.representation], ["Owns", (m.capabilities.owns ?? []).join(", ") || "—"], ["Plots", (m.plots ?? []).join(", ") || "—"]]} />
        <h3 className="text-[13px] font-semibold">Settings schema</h3>
        <JsonBlock value={m.settings_schema} maxHeight="18rem" />
        <h3 className="text-[13px] font-semibold">Fit schema</h3>
        <JsonBlock value={m.fit_schema} maxHeight="14rem" />
      </div>
    );
  } else if (selected.kind === "step") {
    const s = selected.entry;
    title = `${s.id}@${s.version}`;
    body = (
      <div className="grid gap-4">
        {s.summary ? <p>{s.summary}</p> : null}
        <KeyValues items={[["Kind", s.kind], ["Stateful", s.stateful ? "yes" : "no"], ["Invertible", s.invertible ? "yes" : "no"], ["Context", s.context ? JSON.stringify(s.context) : "—"]]} />
        <h3 className="text-[13px] font-semibold">Schema effect</h3>
        <JsonBlock value={s.effect} maxHeight="14rem" />
        <h3 className="text-[13px] font-semibold">Settings schema</h3>
        <JsonBlock value={s.settings_schema} maxHeight="18rem" />
      </div>
    );
  } else {
    title = selected.entry.id;
    body = <JsonBlock value={selected.entry} />;
  }
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent wide>
        <DialogTitle className="font-mono">{title}</DialogTitle>
        <DialogDescription>Static catalog metadata; the control plane validates specs against it without running plugin code.</DialogDescription>
        {body}
      </DialogContent>
    </Dialog>
  );
}

export function CatalogPage() {
  const catalog = useCatalog();
  const { tab } = route.useSearch();
  const navigate = useNavigate({ from: "/catalog" });
  const [selected, setSelected] = useState<Selected | null>(null);
  return (
    <>
      <PageHeader
        title="Catalog"
        description="Model plugins, preprocessing steps and plot recipes available to specs."
        meta={catalog.data ? <Mono title={catalog.data.digest}>digest {catalog.data.digest.slice(0, 19)}</Mono> : null}
      />
      <QueryView query={catalog}>
        {(c) => (
          <Tabs value={tab ?? "models"} onValueChange={(t) => void navigate({ search: { tab: t === "models" ? undefined : (t as "steps" | "plots") } })}>
            <TabsList>
              <TabsTrigger value="models">Models <Badge variant="secondary">{c.models?.length ?? 0}</Badge></TabsTrigger>
              <TabsTrigger value="steps">Steps <Badge variant="secondary">{c.steps?.length ?? 0}</Badge></TabsTrigger>
              <TabsTrigger value="plots">Plots <Badge variant="secondary">{c.plots?.length ?? 0}</Badge></TabsTrigger>
            </TabsList>
            <TabsContent value="models">
              <DataTable label="Model plugins" data={c.models ?? []} columns={modelColumns} filterPlaceholder="Filter models" onRowClick={(entry) => setSelected({ kind: "model", entry })} />
            </TabsContent>
            <TabsContent value="steps">
              <DataTable label="Steps" data={c.steps ?? []} columns={stepColumns} filterPlaceholder="Filter steps" onRowClick={(entry) => setSelected({ kind: "step", entry })} />
            </TabsContent>
            <TabsContent value="plots">
              <DataTable label="Plot recipes" data={c.plots ?? []} columns={plotColumns} filterPlaceholder="Filter recipes" onRowClick={(entry) => setSelected({ kind: "plot", entry })} />
            </TabsContent>
          </Tabs>
        )}
      </QueryView>
      <Detail selected={selected} onClose={() => setSelected(null)} />
    </>
  );
}
