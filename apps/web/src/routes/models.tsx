import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Boxes, Gauge } from "lucide-react";

import { useModel, useModels, useModelVersion, useModelVersions } from "@/api/queries";
import type { Model, ModelVersion } from "@/api/types";
import { CopyButton, EmptyState, ErrorNotice, KeyValues, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { DiagnosticsTable, PreprocessViewer, ProvenancePanel, ScorecardView, TreeBrowser } from "@/features/models/inspect";
import { PlotGallery } from "@/features/plots/plot-gallery";
import { formatNumber, relativeTime, shortId } from "@/lib/format";

const modelColumns: ColumnDef<Model, unknown>[] = [
  {
    accessorKey: "name",
    header: "Model",
    cell: ({ row }) => (
      <span className="grid">
        <span className="font-medium">{row.original.name}</span>
        <Mono className="text-muted-foreground">{row.original.ref}</Mono>
      </span>
    ),
  },
  { accessorKey: "latest_version", header: "Latest", cell: ({ getValue }) => (getValue() == null ? "—" : <Mono>v{String(getValue())}</Mono>) },
  { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => relativeTime(row.original.updated_at) },
];

export function ModelsPage() {
  const models = useModels();
  const navigate = useNavigate();
  return (
    <>
      <PageHeader title="Models" description="Each successful fit registers a verified, signed model version." />
      <QueryView query={models}>
        {(page) =>
          page.items.length ? (
            <DataTable
              label="Models"
              data={page.items}
              columns={modelColumns}
              getRowId={(m) => m.id}
              filterPlaceholder="Filter models"
              onRowClick={(m) => void navigate({ to: "/models/$modelId", params: { modelId: m.id } })}
            />
          ) : (
            <EmptyState icon={<Boxes />} title="No models yet">
              Fit a spec from Build. When the fit succeeds and its outputs verify, the model version appears here.
            </EmptyState>
          )
        }
      </QueryView>
    </>
  );
}

const OVERALL = { pass: "success", warn: "warning", fail: "danger" } as const;

const versionColumns: ColumnDef<ModelVersion, unknown>[] = [
  { accessorKey: "version", header: "Version", cell: ({ getValue }) => <Mono className="font-semibold">v{String(getValue())}</Mono> },
  { accessorKey: "status", header: "Status", cell: ({ getValue }) => <Badge variant={getValue() === "ready" ? "success" : "warning"}>{String(getValue())}</Badge> },
  {
    id: "scorecard",
    header: "Scorecard",
    accessorFn: (v) => v.scorecard?.overall ?? null,
    cell: ({ row }) => (row.original.scorecard ? <Badge variant={OVERALL[row.original.scorecard.overall]}>{row.original.scorecard.overall}</Badge> : "—"),
  },
  {
    id: "metrics",
    header: "Metrics",
    accessorFn: (v) => Object.entries(v.metrics ?? {}).map(([k, x]) => `${k} ${formatNumber(x, 3)}`).join("  "),
    cell: ({ getValue }) => <span className="font-mono text-xs text-muted-foreground">{String(getValue()) || "—"}</span>,
  },
  { accessorKey: "created_at", header: "Created", cell: ({ row }) => relativeTime(row.original.created_at) },
];

const modelRoute = getRouteApi("/models/$modelId");

export function ModelPage() {
  const { modelId } = modelRoute.useParams();
  const model = useModel(modelId);
  const versions = useModelVersions(modelId);
  const navigate = useNavigate();
  if (model.isError) return <ErrorNotice error={model.error} title="This model could not be loaded" />;
  return (
    <>
      <PageHeader title={model.data?.name ?? "Model"} meta={model.data ? <Mono>{model.data.ref}</Mono> : null} />
      <QueryView query={versions}>
        {(page) => (
          <DataTable
            label="Model versions"
            data={page.items}
            columns={versionColumns}
            getRowId={(v) => v.id}
            initialSorting={[{ id: "version", desc: true }]}
            onRowClick={(v) => void navigate({ to: "/models/$modelId/versions/$version", params: { modelId, version: String(v.version) } })}
            empty="No versions yet."
          />
        )}
      </QueryView>
    </>
  );
}

const versionRoute = getRouteApi("/models/$modelId/versions/$version");

export function ModelVersionPage() {
  const { modelId, version } = versionRoute.useParams();
  const { tab } = versionRoute.useSearch();
  const navigate = useNavigate({ from: "/models/$modelId/versions/$version" });
  const v = Number(version);
  const mv = useModelVersion(modelId, Number.isInteger(v) && v > 0 ? v : undefined);
  if (!Number.isInteger(v) || v < 1) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (mv.isPending) return <LoadingRows rows={6} />;
  if (mv.isError) return <ErrorNotice error={mv.error} title="This model version could not be loaded" />;
  const d = mv.data;
  const caps = d.capabilities;
  return (
    <>
      <PageHeader
        title={
          <span className="flex items-center gap-2">
            <Mono className="text-[22px]">{d.ref}</Mono>
            <CopyButton value={d.ref} label="Copy reference" />
          </span>
        }
        meta={
          <>
            <Badge variant={d.status === "ready" ? "success" : "warning"}>{d.status}</Badge>
            {d.scorecard ? <Badge variant={OVERALL[d.scorecard.overall]}>scorecard {d.scorecard.overall}</Badge> : null}
            {caps ? <span>{caps.inference} · {caps.representation}</span> : null}
            <Link to="/runs/$runId" params={{ runId: d.run_id }} className="hover:underline">fit run {shortId(d.run_id)}</Link>
            <span>{relativeTime(d.created_at)}</span>
          </>
        }
        actions={
          <Button asChild>
            <Link to="/score" search={{ model: modelId, version: v }}>
              <Gauge /> Score
            </Link>
          </Button>
        }
      />
      <Tabs value={tab ?? "overview"} onValueChange={(t) => void navigate({ search: { tab: t === "overview" ? undefined : (t as typeof tab) } })}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="diagnostics">Diagnostics {d.diagnostics?.length ? <Badge variant="secondary">{d.diagnostics.length}</Badge> : null}</TabsTrigger>
          <TabsTrigger value="plots">Plots</TabsTrigger>
          <TabsTrigger value="tree">Data tree</TabsTrigger>
          <TabsTrigger value="preprocess">Preprocessing</TabsTrigger>
          <TabsTrigger value="provenance">Provenance</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <Card>
              <CardHeader>
                <CardTitle>Scorecard</CardTitle>
              </CardHeader>
              <CardContent>
                <ScorecardView scorecard={d.scorecard} />
              </CardContent>
            </Card>
            <div className="grid content-start gap-5">
              <Card>
                <CardHeader>
                  <CardTitle>Metrics</CardTitle>
                </CardHeader>
                <CardContent>
                  {Object.keys(d.metrics ?? {}).length ? (
                    <KeyValues items={Object.entries(d.metrics ?? {}).map(([k, x]) => [<Mono key={k}>{k}</Mono>, <span key="v" className="tabular">{formatNumber(x)}</span>])} />
                  ) : (
                    <p className="text-muted-foreground">No holdout metrics recorded.</p>
                  )}
                </CardContent>
              </Card>
              {caps ? (
                <Card>
                  <CardHeader>
                    <CardTitle>Capabilities</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <KeyValues
                      items={[
                        ["Score modes", (caps.score_modes ?? []).join(", ")],
                        ["Representation", caps.representation],
                        ["Contributions", caps.contribution_scale ?? "none"],
                        ["Owns", (caps.owns ?? []).join(", ") || "—"],
                        ["Deterministic", caps.deterministic_given_seed === false ? "no" : "given a seed"],
                      ]}
                    />
                  </CardContent>
                </Card>
              ) : null}
            </div>
          </div>
        </TabsContent>
        <TabsContent value="diagnostics">
          <DiagnosticsTable diagnostics={d.diagnostics ?? []} capabilities={caps} />
        </TabsContent>
        <TabsContent value="plots">
          <PlotGallery modelId={modelId} version={v} />
        </TabsContent>
        <TabsContent value="tree">
          <TreeBrowser mv={d} />
        </TabsContent>
        <TabsContent value="preprocess">
          <PreprocessViewer mv={d} />
        </TabsContent>
        <TabsContent value="provenance">
          <ProvenancePanel mv={d} />
        </TabsContent>
      </Tabs>
    </>
  );
}
