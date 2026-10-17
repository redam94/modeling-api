import { Link } from "@tanstack/react-router";
import { Database, FileCode2, Library, Plus, Sparkles } from "lucide-react";

import { useModels, useReady, useRuns } from "@/api/queries";
import type { Run } from "@/api/types";
import { ErrorNotice, LoadingRows, Mono, StateBadge } from "@/components/common";
import { ChooserTeaser } from "@/features/about/chooser-teaser";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, Progress } from "@/components/ui/primitives";
import { phaseLabel } from "@/features/runs/progress-panel";
import { relativeTime, shortId } from "@/lib/format";
import { isTerminal, runSubject } from "@/lib/run-state";


function ActiveRun({ run }: { run: Run }) {
  const p = run.progress;
  return (
    <Link
      to="/runs/$runId"
      params={{ runId: run.id }}
      className="grid grid-cols-[7.5rem_1fr] items-center gap-x-4 gap-y-1 rounded-lg px-3 py-2.5 hover:bg-accent/60 sm:grid-cols-[7.5rem_9rem_1fr_6rem]"
    >
      <StateBadge state={run.state} className="w-fit" />
      <span className="truncate text-[13px]">
        <span className="font-medium capitalize">{run.kind}</span> <Mono className="text-muted-foreground">{runSubject(run)}</Mono>
      </span>
      <span className="col-span-2 grid gap-1 sm:col-span-1">
        <Progress value={p?.fraction ?? null} label={`Progress of ${run.id}`} />
        <span className="truncate text-xs text-muted-foreground">{p ? phaseLabel(p.phase) : "not started"}</span>
      </span>
      <span className="hidden text-right text-xs text-muted-foreground sm:block">{relativeTime(run.started_at ?? run.created_at)}</span>
    </Link>
  );
}

export function HomePage() {
  const runs = useRuns({ limit: 50 });
  const models = useModels();
  const ready = useReady();
  const items = runs.data?.items ?? [];
  const active = items.filter((r) => !isTerminal(r.state));
  const attention = items.filter((r) => r.state === "failed" || r.state === "stalled" || r.state === "partial").slice(0, 6);
  const recent = items.filter((r) => isTerminal(r.state)).slice(0, 8);

  return (
    <div className="grid gap-8">
      <section aria-labelledby="active-heading" className="grid gap-3">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 id="active-heading" className="text-[26px] leading-tight font-semibold tracking-[-0.015em]">
              {active.length ? `${active.length} run${active.length === 1 ? "" : "s"} in flight` : "Nothing running"}
            </h1>
            <p className="text-muted-foreground">
              {ready.isError
                ? "The app cannot connect to the API. Start the server, then reload the page."
                : active.length
                  ? "The progress updates live. Open a run to see its chains, logs and events."
                  : "Write a spec and fit it. Or upload a dataset to start an ingest run."}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <Link to="/datasets"><Database /> Upload data</Link>
            </Button>
            <Button asChild>
              <Link to="/build/new"><Plus /> New spec</Link>
            </Button>
          </div>
        </div>
        {runs.isPending ? (
          <LoadingRows rows={3} />
        ) : runs.isError ? (
          <ErrorNotice error={runs.error} title="The app could not load the runs" />
        ) : active.length ? (
          <div className="grid divide-y rounded-lg border bg-card p-1">{active.map((r) => <ActiveRun key={r.id} run={r} />)}</div>
        ) : null}
      </section>

      <ChooserTeaser />

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Needs attention</CardTitle>
            <Link to="/runs" search={{ state: "failed" }} className="text-xs text-muted-foreground hover:text-foreground">Failed runs</Link>
          </CardHeader>
          <CardContent>
            {attention.length ? (
              <ul className="grid gap-1">
                {attention.map((r) => (
                  <li key={r.id}>
                    <Link to="/runs/$runId" params={{ runId: r.id }} className="grid grid-cols-[6.5rem_1fr_auto] items-center gap-3 rounded-md px-2 py-1.5 text-[13px] hover:bg-accent/60">
                      <StateBadge state={r.state} className="w-fit" />
                      <span className="truncate">{r.problem?.title ?? `${r.kind} ${shortId(r.id)}`}</span>
                      <span className="text-xs text-muted-foreground">{relativeTime(r.finished_at ?? r.updated_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">The latest 50 runs have no failed, stalled or partial runs.</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Recently finished</CardTitle>
            <Link to="/runs" className="text-xs text-muted-foreground hover:text-foreground">All runs</Link>
          </CardHeader>
          <CardContent>
            {recent.length ? (
              <ul className="grid gap-1">
                {recent.map((r) => (
                  <li key={r.id}>
                    <Link to="/runs/$runId" params={{ runId: r.id }} className="grid grid-cols-[6.5rem_4rem_1fr_auto] items-center gap-3 rounded-md px-2 py-1.5 text-[13px] hover:bg-accent/60">
                      <StateBadge state={r.state} className="w-fit" />
                      <span className="capitalize">{r.kind}</span>
                      <Mono className="truncate text-muted-foreground">{runSubject(r)}</Mono>
                      <span className="text-xs text-muted-foreground">{relativeTime(r.finished_at ?? r.updated_at)}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">There are no finished runs.</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Models</CardTitle>
            <Link to="/models" className="text-xs text-muted-foreground hover:text-foreground">All models</Link>
          </CardHeader>
          <CardContent>
            {(models.data?.items ?? []).length ? (
              <ul className="grid gap-1">
                {(models.data?.items ?? []).slice(0, 6).map((m) => (
                  <li key={m.id}>
                    <Link
                      to={m.latest_version ? "/models/$modelId/versions/$version" : "/models/$modelId"}
                      params={{ modelId: m.id, version: String(m.latest_version ?? 1) }}
                      className="flex items-center justify-between rounded-md px-2 py-1.5 text-[13px] hover:bg-accent/60"
                    >
                      <span className="font-medium">{m.name}</span>
                      <Mono className="text-muted-foreground">{m.latest_version ? `v${m.latest_version}` : "no versions"}</Mono>
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-muted-foreground">When a fit is successful, its model version shows here.</p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Start something</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-2">
            <Link to="/guided" className="flex items-start gap-3 rounded-md p-2 hover:bg-accent/60">
              <Sparkles className="mt-0.5 size-4 text-primary" />
              <span><span className="font-medium">Start a guided analysis</span><span className="block text-muted-foreground">Answer questions in business terms about a marketing mix, a forecast, demand or customer choice.</span></span>
            </Link>
            <Link to="/build/new" className="flex items-start gap-3 rounded-md p-2 hover:bg-accent/60">
              <FileCode2 className="mt-0.5 size-4 text-primary" />
              <span><span className="font-medium">Write a spec</span><span className="block text-muted-foreground">Start from the geo MMM template. The app does a check of the spec while you type.</span></span>
            </Link>
            <Link to="/datasets" className="flex items-start gap-3 rounded-md p-2 hover:bg-accent/60">
              <Database className="mt-0.5 size-4 text-primary" />
              <span><span className="font-medium">Upload a dataset</span><span className="block text-muted-foreground">Upload a CSV file with a column mapping. Or upload a zarr or netCDF dataset as it is.</span></span>
            </Link>
            <Link to="/catalog" className="flex items-start gap-3 rounded-md p-2 hover:bg-accent/60">
              <Library className="mt-0.5 size-4 text-primary" />
              <span><span className="font-medium">Browse the catalog</span><span className="block text-muted-foreground">Models, preprocessing steps and plot recipes.</span></span>
            </Link>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
