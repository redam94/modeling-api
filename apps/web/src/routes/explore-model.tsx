/**
 * The Model Explorer (`/models/$modelId/versions/$version/explore`) and the side-by-side
 * comparison of several model versions (`/models/compare`), M3.5.
 */
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, GitCompare, Wallet } from "lucide-react";

import { errorMessage } from "@/api/client";
import { useModel, useModelVersion } from "@/api/queries";
import { CopyButton, ErrorNotice, LoadingRows, Mono, PageHeader, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { useCompareModels, useExploration } from "@/features/explore/api";
import { ComparePicker, ComparisonResult } from "@/features/explore/compare";
import { RunBanner } from "@/features/explore/explorer-shell";
import { ValidationStatusBadge } from "@/features/explore/governance-panel";
import { ModelExplorer } from "@/features/explore/model-explorer";
import { SectionCard } from "@/features/explore/ui";
import { useEffectivePersona } from "@/features/guided/persona";
import { relativeTime } from "@/lib/format";
import { toast } from "@/stores/toast";

const exploreRoute = getRouteApi("/models/$modelId/versions/$version/explore");

export function ModelExplorerPage() {
  const { modelId, version } = exploreRoute.useParams();
  const search = exploreRoute.useSearch();
  const navigate = useNavigate({ from: "/models/$modelId/versions/$version/explore" });
  const v = Number(version);
  const valid = Number.isInteger(v) && v > 0;
  const model = useModel(modelId);
  const mv = useModelVersion(modelId, valid ? v : undefined);
  const expert = useEffectivePersona() === "expert";
  if (!valid) return <ErrorNotice error={new Error("The version must be a positive integer.")} />;
  if (mv.isError) return <ErrorNotice error={mv.error} title="This model version could not be loaded" />;
  if (mv.isPending) return <LoadingRows rows={8} />;
  const d = mv.data;
  return (
    <div className="grid gap-6">
      <PageHeader
        title={
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            Explore {model.data?.name ?? "the model"}
            <Mono className="text-[15px] text-muted-foreground">v{v}</Mono>
          </span>
        }
        description={expert ? "Parameters, diagnostics, fit, residuals, contributions, forecasts and backtests of this version, with governance evidence and lineage." : "What the model learned, whether you can trust it and what it expects next, in plain words."}
        meta={
          <>
            <Mono>{d.ref}</Mono>
            <CopyButton value={d.ref} label="Copy reference" />
            <ValidationStatusBadge modelId={modelId} version={v} />
            {d.capabilities ? <span>{d.capabilities.inference}</span> : null}
            <span>{relativeTime(d.created_at)}</span>
            <Badge variant="outline">{expert ? "Expert view" : "Guided view"}</Badge>
          </>
        }
        actions={
          <>
            {d.capabilities?.decision ? (
              <Button asChild variant="outline">
                <Link to="/models/$modelId/versions/$version/optimize" params={{ modelId, version: String(v) }}>
                  <Wallet /> Optimise a budget
                </Link>
              </Button>
            ) : null}
            <Button asChild variant="ghost">
              <Link to="/models/$modelId/versions/$version" params={{ modelId, version: String(v) }}>
                <ArrowLeft /> Back to the version
              </Link>
            </Button>
          </>
        }
      />
      <ModelExplorer
        mv={d}
        modelId={modelId}
        version={v}
        tab={(search.section ?? "summary")}
        onTab={(t) => void navigate({ search: (prev) => ({ ...prev, section: t }), replace: true })}
        runId={search.run ?? null}
        onRun={(run) => void navigate({ search: (prev) => ({ ...prev, run }) })}
        expert={expert}
      />
    </div>
  );
}

const compareRoute = getRouteApi("/models/compare");

export function ComparePage() {
  const search = compareRoute.useSearch();
  const navigate = useNavigate({ from: "/models/compare" });
  const refs = search.refs ?? [];
  const expert = useEffectivePersona() === "expert";
  const compare = useCompareModels();
  const run = useExploration(search.run ?? null);
  const start = () =>
    compare.mutate(
      { models: refs, sections: ["comparison"] },
      {
        onSuccess: (r) => void navigate({ search: (prev) => ({ ...prev, run: r.exploration.run.id }) }),
        onError: (e) => toast.error("The comparison did not start", errorMessage(e)),
      },
    );
  return (
    <div className="grid gap-6">
      <PageHeader
        title="Compare model versions"
        description="Put several versions side by side on held-out data: their errors, a ranking per metric, and tests of whether the differences are more than chance."
      />
      <SectionCard
        title="Versions"
        actions={
          <Button onClick={start} disabled={refs.length < 2 || compare.isPending || Boolean(run.data && !run.data.done)}>
            {compare.isPending ? <Spinner /> : <GitCompare />} Compare {refs.length >= 2 ? `${refs.length} versions` : ""}
          </Button>
        }
      >
        <ComparePicker refs={refs} onChange={(next) => void navigate({ search: { refs: next } })} />
      </SectionCard>
      <RunBanner exploration={run.data} error={run.error} title="Comparing" />
      {run.data?.done && run.data.run.state === "succeeded" ? <ComparisonResult exploration={run.data} refs={refs.length ? refs : []} expert={expert} /> : null}
    </div>
  );
}
