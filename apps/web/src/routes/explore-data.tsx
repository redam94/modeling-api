/**
 * `/datasets/$datasetId/versions/$version/explore`: the Data Explorer of one dataset version
 * (M3.5), with the dataset's About on top. `?section=` picks the view, `?run=` the explore
 * run whose views the page shows (so a link keeps both).
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Workflow } from "lucide-react";

import { useDataset, useDatasetVersion } from "@/api/queries";
import { ErrorNotice, LoadingRows, Mono, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { DatasetAboutCard } from "@/features/about/dataset-about";
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
  if (!valid) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (dataset.isError) return <ErrorNotice error={dataset.error} title="This dataset could not be loaded" />;
  if (dv.isError) return <ErrorNotice error={dv.error} title="This dataset version could not be loaded" />;
  return (
    <div className="grid gap-6">
      <PageHeader
        title={
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            Explore {dataset.data?.name ?? "the data"}
            <Mono className="text-[15px] text-muted-foreground">v{v}</Mono>
          </span>
        }
        description={expert ? "Every view of the data, with its statistics. Views are computed in the sandbox; the control plane never reads your data." : "What is in your data and what to fix before modelling, in plain words. Open “Show the statistics” for the numbers."}
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
            {dv.data?.status === "ready" && hasTimeDim(dv.data.data_schema) ? (
              // M3.5: the readiness workflow (unit roots, differencing, order search, backtest, ARIMAX).
              <Button asChild variant="outline">
                <Link to={expert ? "/workflows/new" : "/workflows/recipe"} search={{ template: READINESS_TEMPLATE, data: dv.data.ref }}>
                  <Workflow /> {expert ? "Readiness workflow" : "Check it is ready to forecast"}
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
        <ErrorNotice error={new Error(dv.data.status === "ingesting" ? "This version is still being ingested. The explorer opens once it is ready." : "This version failed to ingest, so there is nothing to explore.")} />
      ) : (
        <DataExplorer
          datasetId={datasetId}
          version={v}
          schema={dv.data.data_schema}
          section={search.section ?? "overview"}
          onSection={(s) => void navigate({ search: (prev) => ({ ...prev, section: s }), replace: true })}
          runId={search.run ?? null}
          onRun={(run) => void navigate({ search: (prev) => ({ ...prev, run }) })}
          expert={expert}
        />
      )}
    </div>
  );
}
