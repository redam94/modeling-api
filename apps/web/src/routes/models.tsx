import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Columns2, Gauge, ShieldCheck, Telescope, Wallet } from "lucide-react";
import { lazy, Suspense } from "react";

import { useModel, useModels, useModelVersion, useModelVersions } from "@/api/queries";
import type { Model, ModelVersion } from "@/api/types";
import { CopyButton, EmptyState, ErrorNotice, KeyValues, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { EmptyModelsArt } from "@/components/illustrations";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { ValidationStatusBadge } from "@/features/explore/governance-panel";
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
      <header className="pb-8">
        <div className="grid min-w-0 gap-3">
          <p className="eyebrow">Fitted and signed</p>
          <h1 className="display-title text-[clamp(2.25rem,5vw,3rem)] leading-[1.02] text-balance">Models</h1>
          <p className="max-w-[60ch] text-lg text-muted-foreground">Each successful fit makes a verified, signed model version. Open one to read its scorecard, plots and diagnostics.</p>
        </div>
      </header>
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
            <div className="grid gap-4">
              <EmptyState
                title="No models yet"
                illustration={<EmptyModelsArt />}
                primaryAction={
                  <Button asChild className="rounded-full">
                    <Link to="/build">Fit a spec</Link>
                  </Button>
                }
                action={
                  <Button asChild variant="outline" className="rounded-full">
                    <Link to="/guided">Start a guided analysis</Link>
                  </Button>
                }
              >
                Your fitted models land here. Fit a spec on the Build page: when the fit is successful and the server verifies its outputs, the model version shows here.
              </EmptyState>
              <EmptyHints
                hints={[
                  ["Signed", "Each version has a signature, so you know it is the one that was fitted."],
                  ["Scored", "A sanity scorecard checks the fit, the forecast and the data."],
                  ["Ready to use", "Score it, explore what-ifs or plan a budget from the version page."],
                ]}
              />
            </div>
          )
        }
      </QueryView>
    </>
  );
}

/** Three small cards under a Studio empty state: what the page gives you once it has something in it. */
function EmptyHints({ hints }: { hints: [string, string][] }) {
  return (
    <ul className="grid gap-4 md:grid-cols-3">
      {hints.map(([title, body]) => (
        <li key={title} className="rounded-2xl border bg-card p-5">
          <p className="font-display text-lg font-bold tracking-[-0.02em]">{title}</p>
          <p className="mt-1 text-xs text-muted-foreground">{body}</p>
        </li>
      ))}
    </ul>
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
  if (model.isError) return <ErrorNotice error={model.error} title="The app could not load this model" />;
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
            empty="This model has no versions."
          />
        )}
      </QueryView>
    </>
  );
}

const versionRoute = getRouteApi("/models/$modelId/versions/$version");

/** v0.18: the team's chart templates on this version (lazy; signed-in pages only). */
const YourCharts = lazy(() => import("@/features/library/your-charts").then((m) => ({ default: m.YourCharts })));

export function ModelVersionPage() {
  const { modelId, version } = versionRoute.useParams();
  const { tab } = versionRoute.useSearch();
  const navigate = useNavigate({ from: "/models/$modelId/versions/$version" });
  const v = Number(version);
  const mv = useModelVersion(modelId, Number.isInteger(v) && v > 0 ? v : undefined);
  if (!Number.isInteger(v) || v < 1) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (mv.isPending) return <LoadingRows rows={6} />;
  if (mv.isError) return <ErrorNotice error={mv.error} title="The app could not load this model version" />;
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
            <ValidationStatusBadge modelId={modelId} version={v} />
            {caps ? <span>{caps.inference} · {caps.representation}</span> : null}
            <Link to="/runs/$runId" params={{ runId: d.run_id }} className="hover:underline">fit run {shortId(d.run_id)}</Link>
            <span>{relativeTime(d.created_at)}</span>
          </>
        }
        actions={
          <>
            {/* M3.5: the Model Explorer, comparison and budget optimisation. */}
            <Button asChild variant="outline">
              <Link to="/models/$modelId/versions/$version/explore" params={{ modelId, version: String(v) }}>
                <Telescope /> Explore
              </Link>
            </Button>
            <Button asChild variant="outline">
              <Link to="/models/compare" search={{ refs: [d.ref] }}>
                <Columns2 /> Compare
              </Link>
            </Button>
            {caps?.decision ? (
              <Button asChild variant="outline">
                <Link to="/models/$modelId/versions/$version/optimize" params={{ modelId, version: String(v) }}>
                  <Wallet /> Optimise budget
                </Link>
              </Button>
            ) : null}
            {/* M3.5: governance validation of this version (policies, checks, approval). */}
            <Button variant="outline" asChild>
              <Link to="/models/$modelId/versions/$version/validations" params={{ modelId, version: String(v) }}>
                <ShieldCheck /> Validation
              </Link>
            </Button>
            <Button asChild>
              <Link to="/score" search={{ model: modelId, version: v }}>
                <Gauge /> Score
              </Link>
            </Button>
          </>
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
                    <p className="text-muted-foreground">This version has no holdout metrics.</p>
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
          <div className="grid gap-5">
            <PlotGallery modelId={modelId} version={v} />
            <Suspense fallback={null}>
              <YourCharts modelId={modelId} version={v} />
            </Suspense>
          </div>
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
