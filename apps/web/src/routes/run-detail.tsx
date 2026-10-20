import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  Flag,
  Gauge,
  HardDriveDownload,
  Info,
  Radio,
  Stethoscope,
  WifiOff,
  Workflow as WorkflowIcon,
} from "lucide-react";
import { useState } from "react";

import { useModelVersionById, useRun } from "@/api/queries";
import { runName, runTags } from "@/api/tracking";
import type { Run, RunEventView } from "@/api/types";
import {
  CopyButton,
  ErrorNotice,
  KeyValues,
  LoadingRows,
  Mono,
  PageHeader,
  ProblemNotice,
  StateBadge,
  UntrustedText,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { LogsViewer } from "@/features/runs/logs-viewer";
import { MetricsPanel } from "@/features/runs/metrics-panel";
import { ParamsPanel } from "@/features/runs/params-panel";
import { TagBadges } from "@/features/runs/tag-badges";
import { phaseLabel, ProgressPanel } from "@/features/runs/progress-panel";
import { RunActions } from "@/features/runs/run-actions";
import { hasUnsavedNotes, RunNotes } from "@/features/runs/run-notes";
import type { NotesEdit } from "@/features/runs/run-patch";
import { RunPlotsCard } from "@/features/runs/run-plots";
import { useRunEvents } from "@/hooks/use-run-events";
import { absoluteTime, elapsedSeconds, formatBytes, formatDuration, formatNumber, relativeTime, shortId } from "@/lib/format";
import { runCostView } from "@/lib/cost";
import { isTerminal, STATE_META } from "@/lib/run-state";
import { cn } from "@/lib/utils";
import type { RunTab } from "@/router";

const route = getRouteApi("/runs/$runId");

function RunCostValue({ run }: { run: Run }) {
  const c = runCostView(run.cost);
  if (!c) return <span className="text-muted-foreground">Not metered</span>;
  return (
    <span className="grid">
      <span className={cn("font-medium tabular", c.estimated && "text-muted-foreground")}>{c.text}</span>
      <span className="text-xs text-muted-foreground">{c.detail}</span>
    </span>
  );
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/** One line of the event timeline. Plugin-originated text is shown only as untrusted text. */
function EventRow({ event }: { event: RunEventView }) {
  const p = event.payload as Record<string, unknown>;
  let icon = <CircleDot className="size-3.5 text-muted-foreground" />;
  let title: string;
  let detail: string | null = null;
  let untrusted: string | null = null;
  switch (event.type) {
    case "state":
      title = phaseLabel(str(p.phase) ?? "state");
      detail = str(p.message);
      break;
    case "warning":
      icon = <AlertTriangle className="size-3.5 text-warning" />;
      title = `${str(p.code) ?? "WARNING"}: ${str(p.message) ?? ""}`;
      untrusted = str(p.untrusted_text);
      break;
    case "diagnostic":
      icon = <Stethoscope className="size-3.5 text-info" />;
      title = `${str(p.code) ?? "DIAGNOSTIC"}: ${str(p.message_expert) ?? str(p.message_plain) ?? ""}`;
      break;
    case "checkpoint":
      icon = <HardDriveDownload className="size-3.5 text-muted-foreground" />;
      title = `Checkpoint ${typeof p.segment === "number" ? p.segment : ""} saved`;
      detail = `${formatBytes(typeof p.size === "number" ? p.size : null)}${typeof p.draws_done === "number" ? ` · ${p.draws_done} draws` : ""}`;
      break;
    case "metric":
      icon = <Gauge className="size-3.5 text-primary" />;
      title = `${str(p.name) ?? "metric"} = ${formatNumber(typeof p.value === "number" ? p.value : null)}`;
      break;
    case "complete":
      icon = <Flag className="size-3.5 text-success" />;
      title = p.status === "partial" ? "Finished with a partial result" : "Finished. The run uploaded its outputs.";
      detail = `${typeof p.objects === "number" ? p.objects : "?"} objects, ${formatBytes(typeof p.total_bytes === "number" ? p.total_bytes : null)}`;
      break;
    case "error":
      icon = <AlertTriangle className="size-3.5 text-destructive" />;
      title = `${str(p.code) ?? "ERROR"}: ${str(p.message) ?? ""}`;
      untrusted = str(p.untrusted_text);
      break;
    default:
      title = event.type;
  }
  return (
    <li className="grid grid-cols-[1rem_1fr_auto] gap-x-3 py-1.5">
      <span className="pt-0.5">{icon}</span>
      <div className="min-w-0">
        <p className="text-[13px]">{title}</p>
        {detail ? <p className="text-xs text-muted-foreground">{detail}</p> : null}
        {untrusted ? (
          <details className="mt-1">
            <summary className="cursor-pointer text-xs text-muted-foreground">Details from the run (untrusted)</summary>
            <UntrustedText text={untrusted} className="mt-1" maxHeight="12rem" />
          </details>
        ) : null}
      </div>
      <time className="text-xs text-muted-foreground tabular" dateTime={event.ts} title={event.ts}>
        a{event.attempt} · {event.ts.slice(11, 19)}
      </time>
    </li>
  );
}

function StreamStatus({ status, terminal }: { status: string; terminal: boolean }) {
  if (terminal) return null;
  const live = status === "open";
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs", live ? "text-success" : "text-muted-foreground")}>
      {live ? <Radio className="size-3.5" /> : <WifiOff className="size-3.5" />}
      {live ? "Live" : status === "reconnecting" ? "Reconnecting…" : "Connecting…"}
    </span>
  );
}

function OutputLink({ mvId }: { mvId: string }) {
  const mv = useModelVersionById(mvId);
  if (mv.isPending) return <span className="text-muted-foreground">Looking up the model version…</span>;
  if (!mv.data) return <Mono>{shortId(mvId)}</Mono>;
  return (
    <Button asChild size="sm" className="h-auto min-h-8 max-w-full justify-start py-1.5 text-left whitespace-normal [overflow-wrap:anywhere]">
      <Link to="/models/$modelId/versions/$version" params={{ modelId: mv.data.model_id, version: String(mv.data.version) }}>
        <CheckCircle2 /> Inspect {mv.data.ref}
      </Link>
    </Button>
  );
}

/** M3.5: a workflow run opens in its run monitor; a step of a workflow links to its workflow run. */
function WorkflowRunLink({ run }: { run: Run }) {
  const target = run.kind === "workflow" ? run.id : run.parent_run_id && run.labels?.["workflow"] ? run.parent_run_id : null;
  if (!target) return null;
  return (
    <Button asChild variant="outline">
      <Link to="/workflow-runs/$runId" params={{ runId: target }}>
        <WorkflowIcon /> {run.kind === "workflow" ? "Open the run monitor" : "Workflow run"}
      </Link>
    </Button>
  );
}

export function RunDetailPage() {
  const { runId } = route.useParams();
  const { tab } = route.useSearch();
  const navigate = useNavigate({ from: "/runs/$runId" });
  const run = useRun(runId);
  const stream = useRunEvents(run.data);
  // The Notes edit lives here, above the tabs: switching tabs unmounts the tab content.
  const [notesEdit, setNotesEdit] = useState<NotesEdit | null>(null);

  if (run.isPending) return <LoadingRows rows={6} />;
  if (run.isError) return <ErrorNotice error={run.error} title="The app could not load this run" />;
  const r = run.data;
  const terminal = isTerminal(r.state);
  const meta = STATE_META[r.state];
  const name = runName(r);
  const tags = runTags(r);

  return (
    <>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            {name ? <span className="[overflow-wrap:anywhere]" data-testid="run-name">{name}</span> : <span className="capitalize">{r.kind}</span>}
            <Mono className="text-base text-muted-foreground">{r.id}</Mono>
            <CopyButton value={r.id} label="Copy run id" />
          </span>
        }
        meta={
          <>
            <StateBadge state={r.state} />
            {name ? <span>{r.kind}</span> : null}
            <span>attempt {r.attempt}</span>
            <span>class {r.resource_class}</span>
            <span>timeout {r.timeout}</span>
            {r.executor ? <span>{r.executor}</span> : null}
            {runCostView(r.cost) ? <span title={runCostView(r.cost)?.detail}>cost {runCostView(r.cost)?.text}</span> : null}
            <StreamStatus status={stream.status} terminal={terminal} />
            {Object.keys(tags).length ? <TagBadges tags={tags} max={5} /> : null}
          </>
        }
        actions={
          <>
            <WorkflowRunLink run={r} />
            <RunActions run={r} />
          </>
        }
      />

      {r.state === "stalled" ? (
        <div role="status" className="mb-5 flex items-start gap-3 rounded-lg border border-warning/40 bg-warning/8 p-4">
          <AlertTriangle className="animate-stall mt-0.5 size-4 text-warning" />
          <div>
            <p className="font-semibold">The run did not report progress recently</p>
            <p className="text-muted-foreground">{meta.description}</p>
          </div>
        </div>
      ) : null}

      {r.problem ? (
        <div className="mb-5">
          <ProblemNotice problem={r.problem} />
        </div>
      ) : null}

      <Tabs value={tab ?? "overview"} onValueChange={(t) => void navigate({ search: { tab: t === "overview" ? undefined : (t as RunTab) }, replace: true })}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="metrics">Metrics</TabsTrigger>
          <TabsTrigger value="params">Params</TabsTrigger>
          <TabsTrigger value="logs">Logs</TabsTrigger>
          <TabsTrigger value="notes">
            Notes
            {hasUnsavedNotes(notesEdit, r.id) ? <span className="ml-1 inline-block size-1.5 rounded-full bg-primary" role="img" aria-label="Changes not saved" title="Changes not saved" /> : null}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <div className="grid min-w-0 content-start gap-5">
              {!terminal || r.progress ? (
                <Card>
                  <CardHeader>
                    <CardTitle>Progress</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <ProgressPanel run={r} progress={stream.progress} />
                  </CardContent>
                </Card>
              ) : null}
              <RunPlotsCard run={r} />
            </div>
            <div className="grid min-w-0 content-start gap-5">
              {r.output_model_version_id ? (
                <Card>
                  <CardHeader>
                    <CardTitle>Result</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <OutputLink mvId={r.output_model_version_id} />
                  </CardContent>
                </Card>
              ) : null}
              <Card>
                <CardHeader>
                  <CardTitle>Details</CardTitle>
                </CardHeader>
                <CardContent>
                  <KeyValues
                    items={[
                      ["Created", <span key="c" title={absoluteTime(r.created_at)}>{relativeTime(r.created_at)}</span>],
                      ["Started", r.started_at ? relativeTime(r.started_at) : "—"],
                      ["Duration", formatDuration(elapsedSeconds(r.started_at, r.finished_at))],
                      [
                        "Spec",
                        r.spec_id ? (
                          <Link key="s" to="/build/$specId" params={{ specId: r.spec_id }} search={{ rev: r.spec_revision ?? undefined }} className="hover:underline">
                            <Mono>
                              {shortId(r.spec_id)} r{r.spec_revision}
                            </Mono>
                          </Link>
                        ) : (
                          "—"
                        ),
                      ],
                      ["Model version", r.model_version_id ? <Mono key="m">{shortId(r.model_version_id)}</Mono> : "—"],
                      ["Dataset", r.dataset_id ? <Link key="d" to="/datasets/$datasetId" params={{ datasetId: r.dataset_id }} className="hover:underline"><Mono>{shortId(r.dataset_id)}</Mono></Link> : "—"],
                      ["Retry of", r.retry_of ? <Link key="r" to="/runs/$runId" params={{ runId: r.retry_of }} className="hover:underline"><Mono>{shortId(r.retry_of)}</Mono></Link> : "—"],
                      ["Spec hash", r.spec_hash ? <Mono key="h" title={r.spec_hash}>{shortId(r.spec_hash)}</Mono> : "—"],
                      ["Cost", <RunCostValue key="cost" run={r} />],
                    ]}
                  />
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle>Events</CardTitle>
                  <Info className="size-4 text-muted-foreground" aria-label="State changes, warnings, diagnostics, checkpoints and metrics" />
                </CardHeader>
                <CardContent>
                  {stream.events.length ? (
                    <ol className="divide-y">{stream.events.map((e) => <EventRow key={e.id} event={e} />)}</ol>
                  ) : (
                    <p className="text-muted-foreground">The run has no events.</p>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        </TabsContent>
        <TabsContent value="metrics">
          <MetricsPanel runId={r.id} live={!terminal} />
        </TabsContent>
        <TabsContent value="params">
          <ParamsPanel run={r} />
        </TabsContent>
        <TabsContent value="logs">
          <Card>
            <CardHeader>
              <CardTitle>Logs</CardTitle>
              <Badge variant="outline">untrusted output</Badge>
            </CardHeader>
            <CardContent>
              <LogsViewer runId={r.id} live={!terminal} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="notes">
          <RunNotes run={r} edit={notesEdit} onEdit={setNotesEdit} />
        </TabsContent>
      </Tabs>
    </>
  );
}
