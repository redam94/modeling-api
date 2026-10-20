import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { Database, Plus, Telescope, Upload } from "lucide-react";
import { useState, type ReactNode } from "react";

import { errorMessage } from "@/api/client";
import { useCreateDataset, useDataset, useDatasets, useDatasetVersion, useDatasetVersions } from "@/api/queries";
import type { Dataset, DatasetVersion } from "@/api/types";
import { CopyButton, EmptyState, ErrorNotice, IssueList, KeyValues, LoadingRows, Mono, PageHeader, QueryView } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogTitle, Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardHeader, CardTitle, Field, Input, Textarea } from "@/components/ui/primitives";
import { DatasetAboutCard } from "@/features/about/dataset-about";
import { collectionOf, isCollectionVersion } from "@/features/datasets/collection-upload";
import { UploadWizard } from "@/features/datasets/upload-wizard";
import { isTree } from "@/features/datasets/tree";
import { TreeBrowser } from "@/features/datasets/tree-view";
import { CollectionSummary, CoordBrowser, MissingnessPanel, PreviewPanel, SchemaTable } from "@/features/datasets/version-views";
import { formatBytes, relativeTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

const columns: ColumnDef<Dataset, unknown>[] = [
  {
    accessorKey: "name",
    header: "Dataset",
    cell: ({ row }) => (
      <span className="grid">
        <span className="font-medium">{row.original.name}</span>
        <Mono className="text-muted-foreground">{row.original.ref}</Mono>
      </span>
    ),
  },
  { accessorKey: "latest_version", header: "Latest", cell: ({ getValue }) => (getValue() == null ? "—" : <Mono>v{String(getValue())}</Mono>) },
  { accessorKey: "description", header: "Description", cell: ({ getValue }) => <span className="line-clamp-1 text-muted-foreground">{(getValue() as string | null) ?? ""}</span> },
  { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => relativeTime(row.original.updated_at) },
];

function CreateDatasetDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const create = useCreateDataset();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const nameOk = /^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/.test(name);
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>New dataset</DialogTitle>
        <DialogDescription>A dataset contains versions that you cannot change. In the next step, you upload the first version.</DialogDescription>
        <form
          className="grid gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            if (!nameOk) return;
            create.mutate(
              { name, ...(description.trim() ? { description: description.trim() } : {}) },
              {
                onSuccess: (ds) => {
                  onOpenChange(false);
                  void navigate({ to: "/datasets/$datasetId", params: { datasetId: ds.id }, search: { upload: true } });
                },
                onError: (err) => toast.error("The app could not create the dataset", errorMessage(err)),
              },
            );
          }}
        >
          <Field label="Name" htmlFor="ds-name" hint={name && !nameOk ? "Use only lowercase letters, digits, - and _." : "References use this name: dataset://default/<name>"}>
            <Input id="ds-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="eu_media_daily" aria-invalid={Boolean(name) && !nameOk} autoFocus />
          </Field>
          <Field label="Description" htmlFor="ds-desc">
            <Textarea id="ds-desc" value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <DialogFooter>
            <Button type="submit" disabled={!nameOk || create.isPending}>
              Create dataset
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function DatasetsPage() {
  const datasets = useDatasets();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  return (
    <>
      <PageHeader
        title="Datasets"
        description="Labelled data with a schema. Only a sandboxed ingest run reads the files."
        actions={
          <Button onClick={() => setOpen(true)}>
            <Plus /> New dataset
          </Button>
        }
      />
      <QueryView query={datasets}>
        {(page) =>
          page.items.length ? (
            <DataTable
              label="Datasets"
              data={page.items}
              columns={columns}
              getRowId={(d) => d.id}
              filterPlaceholder="Filter datasets"
              onRowClick={(d) => void navigate({ to: "/datasets/$datasetId", params: { datasetId: d.id } })}
            />
          ) : (
            <EmptyState icon={<Database />} title="No datasets yet" action={<Button onClick={() => setOpen(true)}><Plus /> New dataset</Button>}>
              Create a dataset. Then upload a CSV, Parquet, zarr zip, netCDF or HDF5 file (or several files), or a folder of images, as its first version.
            </EmptyState>
          )
        }
      </QueryView>
      <CreateDatasetDialog open={open} onOpenChange={setOpen} />
    </>
  );
}

const detailRoute = getRouteApi("/datasets/$datasetId");

const VERSION_STATUS = { ready: "success", ingesting: "info", failed: "danger" } as const;

function VersionAbout({ version }: { version: DatasetVersion }) {
  const items: [string, ReactNode][] = [
    ["Content hash", version.sha256 ? <Mono key="h">{version.sha256}</Mono> : "—"],
    ["Size", formatBytes(version.size_bytes)],
    ["Created", relativeTime(version.created_at)],
    ["Ready", relativeTime(version.ready_at)],
    ["Message", version.message ?? "—"],
  ];
  // M4: tree versions and versions made from several files.
  if (version.layout === "tree") items.push(["Layout", `tree of ${Object.keys(version.tree_schema?.nodes ?? {}).length} nodes`]);
  if ((version.sources ?? []).length > 1) items.push(["Made from", `${version.sources!.length} uploaded files`]);
  // v0.15: an image collection.
  const collection = collectionOf(version);
  if (collection) items.push(["Layout", `image collection of ${collection.n_images.toLocaleString("en-US")} images`]);
  return <KeyValues items={items} />;
}

function VersionDetail({ version }: { version: DatasetVersion }) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Mono className="min-w-0 text-[15px] font-semibold break-all">{version.ref}</Mono>
        <CopyButton value={version.ref} label="Copy reference" />
        <Badge variant={VERSION_STATUS[version.status]}>{version.status}</Badge>
        {version.layout === "tree" ? <Badge variant="outline">tree</Badge> : null}
        {isCollectionVersion(version) ? <Badge variant="outline">Image collection</Badge> : null}
        {version.status === "ready" ? (
          // M3.5: the Data Explorer of this version.
          <Button asChild size="sm" className="ml-auto">
            <Link to="/datasets/$datasetId/versions/$version/explore" params={{ datasetId: version.dataset_id, version: String(version.version) }}>
              <Telescope /> Explore this version
            </Link>
          </Button>
        ) : null}
        {version.ingest_run_id ? (
          <Link to="/runs/$runId" params={{ runId: version.ingest_run_id }} className="text-xs text-muted-foreground hover:underline">
            ingest run {shortId(version.ingest_run_id)}
          </Link>
        ) : null}
      </div>
      {version.issues?.length ? (
        <Card>
          <CardHeader>
            <CardTitle>Issues that the ingest found</CardTitle>
          </CardHeader>
          <CardContent>
            <IssueList issues={version.issues} />
          </CardContent>
        </Card>
      ) : null}
      {version.status === "ingesting" ? (
        <p className="text-muted-foreground">The ingest run reads the upload and does a check of it. This page refreshes automatically.</p>
      ) : null}
      {version.status === "ready" && isTree(version) ? (
        // M4: a tree version shows its nodes first.
        <Tabs defaultValue="nodes">
          <TabsList>
            <TabsTrigger value="nodes">Nodes</TabsTrigger>
            <TabsTrigger value="missing">Missingness</TabsTrigger>
            <TabsTrigger value="about">About</TabsTrigger>
          </TabsList>
          <TabsContent value="nodes"><TreeBrowser version={version} nodes={version.tree_schema.nodes} /></TabsContent>
          <TabsContent value="missing"><MissingnessPanel version={version} /></TabsContent>
          <TabsContent value="about"><VersionAbout version={version} /></TabsContent>
        </Tabs>
      ) : version.status === "ready" && isCollectionVersion(version) ? (
        // v0.15: an image collection shows what it holds first (the manifest has the coordinates).
        <Tabs defaultValue="collection">
          <TabsList>
            <TabsTrigger value="collection">Collection</TabsTrigger>
            <TabsTrigger value="schema">Schema</TabsTrigger>
            <TabsTrigger value="coords">Coordinates</TabsTrigger>
            <TabsTrigger value="preview">Preview</TabsTrigger>
            <TabsTrigger value="about">About</TabsTrigger>
          </TabsList>
          <TabsContent value="collection"><CollectionSummary version={version} /></TabsContent>
          <TabsContent value="schema">{version.data_schema ? <SchemaTable schema={version.data_schema} /> : <p className="text-muted-foreground">This version has no schema.</p>}</TabsContent>
          <TabsContent value="coords"><CoordBrowser version={version} /></TabsContent>
          <TabsContent value="preview"><PreviewPanel version={version} /></TabsContent>
          <TabsContent value="about"><VersionAbout version={version} /></TabsContent>
        </Tabs>
      ) : version.status === "ready" ? (
        <Tabs defaultValue="schema">
          <TabsList>
            <TabsTrigger value="schema">Schema</TabsTrigger>
            <TabsTrigger value="coords">Coordinates</TabsTrigger>
            <TabsTrigger value="missing">Missingness</TabsTrigger>
            <TabsTrigger value="preview">Preview</TabsTrigger>
            <TabsTrigger value="about">About</TabsTrigger>
          </TabsList>
          <TabsContent value="schema">{version.data_schema ? <SchemaTable schema={version.data_schema} /> : <p className="text-muted-foreground">This version has no schema.</p>}</TabsContent>
          <TabsContent value="coords"><CoordBrowser version={version} /></TabsContent>
          <TabsContent value="missing"><MissingnessPanel version={version} /></TabsContent>
          <TabsContent value="preview"><PreviewPanel version={version} /></TabsContent>
          <TabsContent value="about"><VersionAbout version={version} /></TabsContent>
        </Tabs>
      ) : null}
    </div>
  );
}

export function DatasetDetailPage() {
  const { datasetId } = detailRoute.useParams();
  const search = detailRoute.useSearch();
  const navigate = useNavigate({ from: "/datasets/$datasetId" });
  const dataset = useDataset(datasetId);
  const versions = useDatasetVersions(datasetId);
  const sorted = [...(versions.data?.items ?? [])].sort((a, b) => b.version - a.version);
  const selected = search.version ?? sorted[0]?.version;
  const version = useDatasetVersion(datasetId, selected);
  const uploading = search.upload === true;

  if (dataset.isError) return <ErrorNotice error={dataset.error} title="The app could not load this dataset" />;
  return (
    <>
      <PageHeader
        title={dataset.data?.name ?? "Dataset"}
        description={dataset.data?.description ?? undefined}
        meta={dataset.data ? <Mono>{dataset.data.ref}</Mono> : null}
        actions={
          <Button onClick={() => void navigate({ search: (s) => ({ ...s, upload: uploading ? undefined : true }) })} variant={uploading ? "outline" : "default"}>
            <Upload /> {uploading ? "Close upload" : "Upload new version"}
          </Button>
        }
      />
      {/* M3.5: the dataset's About (owner description + generated summary). */}
      {dataset.data && dataset.data.latest_version ? (
        <div className="mb-6">
          <DatasetAboutCard datasetId={datasetId} />
        </div>
      ) : null}
      {uploading ? (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Upload a new version</CardTitle>
          </CardHeader>
          <CardContent>
            <UploadWizard
              datasetId={datasetId}
              onDone={(v) => {
                toast.success(`The ingest of v${v.version} started`, "A sandboxed ingest run does a check of the file.");
                void navigate({ search: { version: v.version } });
              }}
            />
          </CardContent>
        </Card>
      ) : null}
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <nav aria-label="Versions" className="grid content-start gap-0.5">
          <h2 className="px-2 pb-1 text-xs font-medium text-muted-foreground">Versions</h2>
          {versions.isPending ? <LoadingRows rows={3} /> : null}
          {sorted.map((v) => (
            <button
              key={v.version}
              type="button"
              aria-current={v.version === selected ? "true" : undefined}
              onClick={() => void navigate({ search: (s) => ({ ...s, version: v.version }) })}
              className={cn("flex items-center justify-between rounded-md px-2 py-1.5 text-[13px] hover:bg-accent", v.version === selected && "bg-accent font-medium")}
            >
              <Mono>v{v.version}</Mono>
              <span className="text-xs text-muted-foreground">{v.status === "ready" ? relativeTime(v.created_at) : v.status}</span>
            </button>
          ))}
          {versions.data && !sorted.length ? <p className="px-2 text-muted-foreground">This dataset has no versions.</p> : null}
        </nav>
        <div className="min-w-0">
          {selected === undefined ? (
            versions.isPending ? null : (
              <EmptyState icon={<Upload />} title="Upload the first version" action={<Button onClick={() => void navigate({ search: { upload: true } })}><Upload /> Upload</Button>}>
                Map the columns to dims and variables. Then the ingest run makes the labelled dataset.
              </EmptyState>
            )
          ) : version.isPending ? (
            <LoadingRows rows={6} />
          ) : version.isError ? (
            <ErrorNotice error={version.error} />
          ) : (
            <VersionDetail version={version.data} />
          )}
        </div>
      </div>
    </>
  );
}
