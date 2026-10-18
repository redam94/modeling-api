/**
 * `/datasets/$datasetId/versions/$version/explore`: the Data Explorer of one dataset version
 * (M3.5), with the dataset's About on top. `?section=` picks the view, `?run=` the explore
 * run whose views the page shows (so a link keeps both). M4: `?node=` picks the node of a
 * tree dataset; each node has its own views.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Workflow } from "lucide-react";

import { useDataset, useDatasetVersion } from "@/api/queries";
import { ErrorNotice, LoadingRows, Mono, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Field, NativeSelect } from "@/components/ui/primitives";
import { DatasetAboutCard } from "@/features/about/dataset-about";
import { explorerNode, isTree, nodesWithVars } from "@/features/datasets/tree";
import { DataExplorer } from "@/features/explore/data-explorer";
import { useEffectivePersona } from "@/features/guided/persona";

const route = getRouteApi("/datasets/$datasetId/versions/$version/explore");

/** The gallery's time-series readiness → ARIMAX workflow, offered for data over time. */
const READINESS_TEMPLATE = "ts_readiness_arimax";

function hasTimeDim(schema: { dims?: Record<string, { kind: string }> } | null | undefined): boolean {
  return Object.values(schema?.dims ?? {}).some((d) => d.kind === "time");
}

export function DataExplorerPage() {
  const { datasetId, version } = route.useParams();
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/datasets/$datasetId/versions/$version/explore" });
  const v = Number(version);
  const valid = Number.isInteger(v) && v > 0;
  const dataset = useDataset(datasetId);
  const dv = useDatasetVersion(datasetId, valid ? v : undefined);
  const persona = useEffectivePersona();
  const expert = persona === "expert";
  const tree = isTree(dv.data) ? dv.data.tree_schema.nodes : null;
  const nodeOptions = tree ? nodesWithVars(tree) : [];
  // Only a node with variables can be explored (the picker lists only those): a group node or
  // an unknown one in `?node=` falls back to the default node, with a note.
  const picked = tree ? explorerNode(tree, search.node) : null;
  const node = picked?.node ?? null;
  const schema = tree && node ? tree[node] : dv.data?.data_schema;
  if (!valid) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (dataset.isError) return <ErrorNotice error={dataset.error} title="The app could not load this dataset" />;
  if (dv.isError) return <ErrorNotice error={dv.error} title="The app could not load this dataset version" />;
  return (
    <div className="grid gap-6">
      <PageHeader
        title={
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            Explore {dataset.data?.name ?? "the data"}
            <Mono className="text-[15px] text-muted-foreground">v{v}</Mono>
          </span>
        }
        description={expert ? "All the views of the data, with their statistics. The sandbox calculates the views. The control plane does not read your data." : "This page tells you, in plain words, what is in your data and what to correct before you make a model. Open “Show the statistics” to see the numbers."}
        meta={
          <>
            {dv.data ? <Mono>{dv.data.ref}</Mono> : null}
            <Badge variant="outline">{expert ? "Expert view" : "Guided view"}</Badge>
          </>
        }
        actions={
          <>
            <Button asChild variant="ghost">
              <Link to="/datasets/$datasetId" params={{ datasetId }} search={{ version: v }}>
                <ArrowLeft /> Back to the dataset
              </Link>
            </Button>
            {dv.data?.status === "ready" && hasTimeDim(schema) && !tree ? (
              // M3.5: the readiness workflow (unit roots, differencing, order search, backtest, ARIMAX).
              <Button asChild variant="outline">
                <Link to={expert ? "/workflows/new" : "/workflows/recipe"} search={{ template: READINESS_TEMPLATE, data: dv.data.ref }}>
                  <Workflow /> {expert ? "Readiness workflow" : "Test if it is ready to forecast"}
                </Link>
              </Button>
            ) : null}
          </>
        }
      />
      <DatasetAboutCard datasetId={datasetId} />
      {dv.isPending ? (
        <LoadingRows rows={6} />
      ) : dv.data.status !== "ready" ? (
        <ErrorNotice error={new Error(dv.data.status === "ingesting" ? "The ingest of this version is not complete. The explorer opens when the version is ready." : "The ingest of this version failed. There is no data to explore. Upload the data again.")} />
      ) : (
        <>
        {tree && node ? (
          // M4: one explorer per node of a tree dataset.
          <div className="flex flex-wrap items-end gap-3 rounded-xl border bg-card px-4 py-3">
            <Field label="Node" htmlFor="explore-node" hint={expert ? undefined : "This dataset is a tree. Each node has its own variables and views."}>
              <NativeSelect
                id="explore-node"
                value={node}
                onChange={(e) => void navigate({ search: (prev) => ({ ...prev, node: e.target.value, run: undefined }), replace: true })}
                className="w-80 max-w-full font-mono"
              >
                {nodeOptions.map((p) => (
                  <option key={p} value={p}>
                    {p} ({Object.keys(tree[p]?.vars ?? {}).length} {Object.keys(tree[p]?.vars ?? {}).length === 1 ? "variable" : "variables"})
                  </option>
                ))}
              </NativeSelect>
            </Field>
            {picked?.note ? (
              <p role="status" className="text-xs text-warning">
                {picked.note}
              </p>
            ) : null}
          </div>
        ) : null}
        <DataExplorer
          key={node ?? "/"}
          datasetId={datasetId}
          version={v}
          node={node}
          schema={schema}
          section={search.section ?? "overview"}
          onSection={(s) => void navigate({ search: (prev) => ({ ...prev, section: s }), replace: true })}
          runId={search.run ?? null}
          onRun={(run) => void navigate({ search: (prev) => ({ ...prev, run }) })}
          expert={expert}
        />
        </>
      )}
    </div>
  );
}
