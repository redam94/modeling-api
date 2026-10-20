/**
 * The compare page (v0.14): 2..10 runs side by side (`GET /v1/run-compare`). Sections: Curves
 * (metric histories by step, a colour per run), Metric bars (summary values and the best run),
 * Params (the keys whose values differ), Plots (the plots of two or more runs) and Media (the
 * images and tables of the runs at one step). The URL holds the runs, the curve keys and the
 * media keys; the run colours follow the run order.
 */
import { useQueries } from "@tanstack/react-query";
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, GitCompareArrows } from "lucide-react";
import { useMemo, useState } from "react";

import { ApiError } from "@/api/client";
import { MAX_COMPARE_MEDIA_KEYS, MIN_COMPARE_RUNS } from "@/api/experiments";
import { runMediaKeysQuery, useProjectScope, useRunCompare } from "@/api/queries";
import type { MediaKind } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { BarsSection } from "@/features/run-compare/bars";
import { runLabels } from "@/features/run-compare/compare-model";
import { CurvesSection } from "@/features/run-compare/curves";
import { MediaSection, type MediaKeyOption } from "@/features/run-compare/media";
import { ParamsSection } from "@/features/run-compare/params";
import { PlotsSection } from "@/features/run-compare/plots";
import { RunChips } from "@/features/run-compare/run-chips";

const route = getRouteApi("/runs/compare");

type Section = "curves" | "bars" | "params" | "plots" | "media";

export function RunComparePage() {
  const search = route.useSearch();
  const navigate = useNavigate({ from: "/runs/compare" });
  const s = useProjectScope();
  const [section, setSection] = useState<Section>("curves");
  const runIds = search.runs;

  // The media keys of every run: the Media section offers their union.
  const keyQueries = useQueries({ queries: runIds.map((id) => runMediaKeysQuery(s, id)) });
  const keysReady = keyQueries.every((q) => !q.isPending);
  const options = useMemo((): MediaKeyOption[] => {
    const byKey = new Map<string, { kind: MediaKind; runs: Set<string> }>();
    keyQueries.forEach((q, i) => {
      for (const k of q.data?.items ?? []) {
        const e = byKey.get(k.key) ?? { kind: k.kind, runs: new Set<string>() };
        e.runs.add(runIds[i]!);
        byKey.set(k.key, e);
      }
    });
    return [...byKey.entries()].map(([key, e]) => ({ key, kind: e.kind, runs: e.runs.size })).sort((a, b) => Number(a.kind !== "image") - Number(b.kind !== "image") || b.runs - a.runs || a.key.localeCompare(b.key));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the query results are new arrays on every render
  }, [runIds, keyQueries.map((q) => q.dataUpdatedAt).join(",")]);
  // without a choice: the image key most runs logged (images before tables); `[]` is a choice
  const mediaKeys = search.media ?? (options[0] ? [options[0].key] : []);
  // Wait for the media keys: one request asks for the metrics and the media together.
  const compare = useRunCompare({ runs: runIds, keys: search.keys, mediaKeys: mediaKeys.slice(0, MAX_COMPARE_MEDIA_KEYS) }, keysReady);
  const labels = useMemo(() => runLabels(compare.data?.runs ?? []), [compare.data?.runs]);

  const setRuns = (runs: string[]) => void navigate({ search: (prev) => ({ ...prev, runs }), replace: true });

  if (runIds.length < MIN_COMPARE_RUNS) {
    return (
      <>
        <PageHeader title="Compare runs" />
        <EmptyState
          icon={<GitCompareArrows />}
          title="Select two or more runs"
          action={
            <Link to="/runs" className="text-primary hover:underline">
              Go to Runs
            </Link>
          }
        >
          Select 2 to 10 runs in the runs table. Then click Compare.
        </EmptyState>
      </>
    );
  }

  const notFound = compare.error instanceof ApiError && compare.error.status === 404;
  return (
    <>
      <PageHeader
        title="Compare runs"
        description={`${runIds.length} runs. The colour of a run is the same in every chart.`}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link to="/runs">
              <ArrowLeft /> Runs
            </Link>
          </Button>
        }
      />
      {compare.data ? (
        <div className="mb-5">
          <RunChips runs={compare.data.runs} labels={labels} onRemove={(id) => setRuns(runIds.filter((r) => r !== id))} onAdd={(id) => setRuns([...runIds, id])} />
        </div>
      ) : null}
      {compare.isError ? (
        <ErrorNotice error={compare.error} title={notFound ? "A run of the comparison does not exist, or you cannot read it" : "The app could not compare the runs"} />
      ) : !compare.data ? (
        <LoadingRows rows={6} />
      ) : (
        <Tabs value={section} onValueChange={(v) => setSection(v as Section)}>
          <TabsList>
            <TabsTrigger value="curves">Curves</TabsTrigger>
            <TabsTrigger value="bars">Metric bars</TabsTrigger>
            <TabsTrigger value="params">Params</TabsTrigger>
            <TabsTrigger value="plots">Plots</TabsTrigger>
            <TabsTrigger value="media">Media</TabsTrigger>
          </TabsList>
          <TabsContent value="curves">
            <CurvesSection data={compare.data} labels={labels} keys={search.keys} onKeys={(keys) => void navigate({ search: (prev) => ({ ...prev, keys }), replace: true })} />
          </TabsContent>
          <TabsContent value="bars">
            <BarsSection data={compare.data} labels={labels} />
          </TabsContent>
          <TabsContent value="params">
            <ParamsSection data={compare.data} labels={labels} />
          </TabsContent>
          <TabsContent value="plots">
            <PlotsSection runs={compare.data.runs} labels={labels} />
          </TabsContent>
          <TabsContent value="media">
            <MediaSection
              data={compare.data}
              labels={labels}
              options={options}
              selected={mediaKeys}
              loading={!keysReady}
              onSelect={(media) => void navigate({ search: (prev) => ({ ...prev, media }), replace: true })}
            />
          </TabsContent>
        </Tabs>
      )}
      {compare.isFetching && compare.data ? <p className="mt-3 text-xs text-muted-foreground" aria-live="polite">Updating the comparison…</p> : null}
    </>
  );
}
