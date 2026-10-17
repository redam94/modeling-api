/**
 * The run monitor: live per-node status over SSE (resumed with Last-Event-ID), the canvas
 * coloured by status (Expert), the steps in plain words with their verdicts (both audiences),
 * the decisions taken and why, the run's outputs and report, the event log, cancel and cost.
 * Selecting a node opens its detail drawer.
 */
import { Link } from "@tanstack/react-router";
import { Ban, CircleDot, Radio, RotateCcw, WifiOff } from "lucide-react";
import { Suspense, useMemo, useState } from "react";

import { can } from "@/api/access";
import { ErrorNotice, KeyValues, LoadingRows, Mono, PageHeader, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/overlays";
import { Badge, Card, CardContent, CardHeader, CardTitle, Progress, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { useWorkspace } from "@/features/auth/workspace";
import { runCostView } from "@/lib/cost";
import { absoluteTime, elapsedSeconds, formatDuration, relativeTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { TERMINAL_RUN, useCancelWorkflowRun, useNodeCatalog, useWorkflow, useWorkflowRevision, useWorkflowRun } from "./api";
import { implId, parseWhen } from "./doc";
import { CanvasFallback, LazyWorkflowCanvas } from "./editor";
import { decisionSentence, DecisionView, labelOfFor, NodeDrawer, type LabelOf } from "./node-drawer";
import { ModelExplorerLinks, modelVersionRefs } from "./model-link";
import { OutputView, PayloadView } from "./outputs";
import { useWorkflowRunEvents } from "./run-events";
import { KIND_LABEL, NODE_STATUS_META, RUN_STATUS_META, TONE_BADGE, TRIGGER_LABEL } from "./status";
import { useCanvasPositions } from "./use-editor";
import type { Catalog } from "./ports";
import type { BranchDecision, NodeRun, NodeRunStatus, WorkflowDoc, WorkflowRun } from "./types";

const DONE: ReadonlySet<NodeRunStatus> = new Set(["succeeded", "cached", "skipped", "failed", "cancelled"]);

function lowerFirst(text: string): string {
  return /^[A-Z][a-z]/.test(text) ? text[0]!.toLowerCase() + text.slice(1) : text;
}

export interface SummaryContext {
  doc?: WorkflowDoc | null;
  decisions?: readonly BranchDecision[];
  labelOf?: LabelOf;
}

/**
 * A one-line plain summary of a node's outcome: a branch says what its path means (its
 * explanation), a step it skipped says why it was not needed, and a comparison names its
 * winner by its label.
 */
export function nodeSummary(n: NodeRun, ctx: SummaryContext = {}): string {
  const r = n.result ?? {};
  const labelOf = ctx.labelOf ?? ((id: string) => id);
  for (const key of ["verdict_plain", "reason_plain", "message_plain", "summary_plain"] as const) {
    const v = r[key];
    if (typeof v === "string" && v) return v;
  }
  if (n.status === "skipped") {
    const when = parseWhen(ctx.doc?.nodes.find((x) => x.id === n.node_id)?.when);
    const d = when ? ctx.decisions?.find((x) => x.node_id === when.branch) : undefined;
    if (when && d?.explain_plain && d.arm !== when.arm) return `Not needed: ${lowerFirst(d.explain_plain)}`;
    return n.skipped_reason ?? "Skipped";
  }
  if (n.error) return `${n.error.code}: ${n.error.message}`;
  if (typeof r.arm === "string") {
    const d = ctx.decisions?.find((x) => x.node_id === n.node_id);
    if (d) return d.explain_plain ?? decisionSentence(d, ctx.doc ?? null, labelOf);
    return r.value ? "The condition was met." : "The condition was not met.";
  }
  if (typeof r.best === "string") return `Best: ${labelOf(r.best)}`;
  if (typeof r.n_folds === "number") return `${r.n_folds} folds`;
  return NODE_STATUS_META[n.status].plain;
}

function StreamBadge({ status, terminal }: { status: string; terminal: boolean }) {
  if (terminal) return null;
  if (status === "open")
    return (
      <Badge variant="info">
        <Radio aria-hidden /> Live
      </Badge>
    );
  if (status === "error")
    return (
      <Badge variant="warning">
        <WifiOff aria-hidden /> Not live
      </Badge>
    );
  return (
    <Badge variant="outline">
      <Spinner className="size-3" /> Connecting
    </Badge>
  );
}

function StepsTable({ run, doc, catalog, onOpen }: { run: WorkflowRun; doc: WorkflowDoc | null; catalog: Catalog; onOpen: (id: string) => void }) {
  const ctx: SummaryContext = { doc, decisions: run.decisions ?? [], labelOf: labelOfFor(doc, catalog) };
  return (
    <Table aria-label="Steps">
      <THead>
        <TR>
          <TH>Step</TH>
          <TH>Status</TH>
          <TH className="w-1/2">What happened</TH>
          <TH>Took</TH>
        </TR>
      </THead>
      <TBody>
        {(run.nodes ?? []).map((n) => {
          const meta = NODE_STATUS_META[n.status];
          const label = doc?.nodes.find((x) => x.id === n.node_id)?.label ?? catalog.get(implId(n.use))?.about.title;
          return (
            <TR key={n.node_id} data-node={n.node_id} data-status={n.status}>
              <TD>
                <button type="button" className="text-left hover:underline" onClick={() => onOpen(n.node_id)}>
                  <span className="block font-medium">{label ?? n.node_id}</span>
                  <span className="block text-xs text-muted-foreground">
                    {KIND_LABEL[n.kind]} · <Mono>{n.node_id}</Mono>
                  </span>
                </button>
              </TD>
              <TD>
                <Badge variant={TONE_BADGE[meta.tone]}>{meta.label}</Badge>
              </TD>
              <TD className={cn("text-[13px]", n.status === "failed" && "text-destructive")}>{nodeSummary(n, ctx)}</TD>
              <TD className="tabular text-muted-foreground">{n.status === "cached" ? "reused" : formatDuration(elapsedSeconds(n.started_at, n.finished_at))}</TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}

function EventLog({ events }: { events: { id: number; type: string; node_id?: string | null; ts: string; data?: Record<string, unknown> }[] }) {
  if (!events.length) return <p className="text-[13px] text-muted-foreground">No events received yet.</p>;
  return (
    <ol className="grid max-h-96 gap-0.5 overflow-y-auto" aria-label="Events" aria-live="polite">
      {events.map((e) => (
        <li key={e.id} className="grid grid-cols-[1rem_1fr_auto] items-start gap-2 py-0.5 text-[13px]">
          <CircleDot className="mt-1 size-3 text-muted-foreground" aria-hidden />
          <span>
            <Mono>{e.type.replace("workflow.", "")}</Mono>
            {e.node_id ? <span className="ml-2 text-muted-foreground">{e.node_id}</span> : null}
            {typeof e.data?.reason === "string" ? <span className="block text-xs text-muted-foreground">{e.data.reason}</span> : null}
            {typeof e.data?.message === "string" ? <span className="block text-xs text-destructive">{e.data.message}</span> : null}
          </span>
          <time className="text-xs text-muted-foreground tabular" dateTime={e.ts}>
            {e.ts.slice(11, 19)}
          </time>
        </li>
      ))}
    </ol>
  );
}

export function WorkflowRunMonitor({ runId, plain = false }: { runId: string; plain?: boolean }) {
  const ws = useWorkspace();
  const q = useWorkflowRun(runId);
  const run = q.data;
  const stream = useWorkflowRunEvents(run);
  const catalog = useNodeCatalog();
  const wf = useWorkflow(run?.workflow_id);
  const rev = useWorkflowRevision(run?.workflow_id, run?.revision);
  const doc = (rev.data?.document as unknown as WorkflowDoc | undefined) ?? null;
  const cancel = useCancelWorkflowRun();
  const [openNode, setOpenNode] = useState<string | null>(null);
  const [tab, setTab] = useState(plain ? "steps" : "graph");
  const statuses = useMemo(() => new Map((run?.nodes ?? []).map((n) => [n.node_id, n.status])), [run?.nodes]);
  const positions = useCanvasPositions(run?.workflow_id ?? "run", doc ?? { apiVersion: "mapi/v1", kind: "Workflow", metadata: { name: "", project: "" }, nodes: [] });

  if (q.isPending) return <LoadingRows rows={6} />;
  if (q.isError || !run) return <ErrorNotice error={q.error} title="The workflow run could not be loaded" />;

  const terminal = TERMINAL_RUN.has(run.status);
  const meta = RUN_STATUS_META[run.status];
  const nodes = run.nodes ?? [];
  const done = nodes.filter((n) => DONE.has(n.status)).length;
  const cost = runCostView(run.cost);
  const report = Object.values(run.outputs ?? {}).find((o) => o.type === "report" && o.inline);
  const modelRefs = run.status === "succeeded" ? modelVersionRefs(run.outputs) : [];
  const node = nodes.find((n) => n.node_id === openNode) ?? null;
  const mayCancel = can(ws.grants, "cancel", "workflow") && (run.status === "queued" || run.status === "running");

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5" data-testid="run-monitor" data-status={run.status}>
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            {wf.data ? (
              <Link to="/workflows/$workflowId" params={{ workflowId: run.workflow_id }} className="hover:underline">
                {wf.data.name}
              </Link>
            ) : (
              "Workflow run"
            )}
            <Badge variant={TONE_BADGE[meta.tone]} className="text-sm" data-testid="run-status">
              {meta.label}
            </Badge>
            <StreamBadge status={stream.status} terminal={terminal} />
          </span>
        }
        description={plain ? "The steps of the recipe, in sequence, with the result of each step." : `Run ${shortId(run.run_id)} of revision ${run.revision}.`}
        meta={
          <>
            <span>{TRIGGER_LABEL[run.trigger ?? "manual"]}</span>
            <span>·</span>
            <span title={run.created_at}>started {relativeTime(run.started_at ?? run.created_at)}</span>
            {run.parent_run_id ? (
              <>
                <span>·</span>
                <Link to="/workflow-runs/$runId" params={{ runId: run.parent_run_id }} className="text-primary hover:underline">
                  parent run
                </Link>
              </>
            ) : null}
          </>
        }
        actions={
          <>
            {mayCancel ? (
              <Button
                variant="outline"
                onClick={() =>
                  cancel.mutate(run.run_id, {
                    onSuccess: () => toast.info("The run stops", "The server stops the steps that run now. No other step starts."),
                  })
                }
                disabled={cancel.isPending}
              >
                <Ban /> Cancel run
              </Button>
            ) : null}
            {terminal ? (
              <Button variant="outline" asChild>
                <Link to="/workflows/$workflowId" params={{ workflowId: run.workflow_id }} search={{ tab: "runs" }}>
                  <RotateCcw /> Run again
                </Link>
              </Button>
            ) : null}
          </>
        }
      />
      {cancel.isError ? <ErrorNotice error={cancel.error} title="The run was not cancelled" /> : null}
      {run.error ? (
        <div role="alert" className="rounded-lg border border-destructive/35 bg-destructive/6 p-3 text-[13px]">
          <p className="font-medium text-destructive">
            {run.error.code}: {run.error.message}
          </p>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card>
          <CardContent className="grid gap-2 pt-4">
            <div className="flex items-center justify-between text-[13px]">
              <span>
                {done} of {nodes.length} steps finished
              </span>
              <span className="text-muted-foreground">{formatDuration(elapsedSeconds(run.started_at, run.finished_at))}</span>
            </div>
            <Progress value={nodes.length ? done / nodes.length : null} label="Steps finished" />
            {report?.inline && typeof (report.inline as { summary_plain?: unknown }).summary_plain === "string" ? (
              <p className="text-[13px]" data-testid="run-summary">
                {(report.inline as { summary_plain: string }).summary_plain}
              </p>
            ) : null}
            {modelRefs.length ? (
              <div className="flex flex-wrap gap-2 pt-1" data-testid="run-models">
                {modelRefs.map((ref) => (
                  <ModelExplorerLinks key={ref} modelRef={ref} />
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="pt-4">
            <KeyValues
              items={[
                ["Data", Object.entries(run.inputs ?? {}).map(([k, v]) => <Mono key={k}>{v}</Mono>)],
                ["Cost", cost ? <span title={cost.detail}>{cost.text}</span> : terminal ? "Not metered" : "Known when the run finishes"],
                ["Finished", run.finished_at ? absoluteTime(run.finished_at) : null],
              ]}
            />
          </CardContent>
        </Card>
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="graph">Graph</TabsTrigger>
          <TabsTrigger value="steps">Steps</TabsTrigger>
          <TabsTrigger value="decisions">Decisions ({(run.decisions ?? []).length})</TabsTrigger>
          <TabsTrigger value="outputs">Outputs</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
        </TabsList>
        <TabsContent value="graph">
          {doc && catalog.map.size ? (
            <Suspense fallback={<CanvasFallback height="60vh" />}>
              <LazyWorkflowCanvas
                label="Workflow run graph"
                doc={doc}
                catalog={catalog.map}
                positions={positions.positions}
                onMove={positions.move}
                statuses={statuses}
                selected={openNode}
                readOnly
                onSelect={setOpenNode}
                height="60vh"
              />
            </Suspense>
          ) : (
            <LoadingRows rows={3} />
          )}
          <p className="mt-2 text-xs text-muted-foreground">Select a step to see its result, outputs, runs and logs.</p>
        </TabsContent>
        <TabsContent value="steps">
          <StepsTable run={run} doc={doc} catalog={catalog.map} onOpen={setOpenNode} />
        </TabsContent>
        <TabsContent value="decisions">
          {(run.decisions ?? []).length ? (
            <div className="grid gap-3">
              {(run.decisions ?? []).map((d) => (
                <Card key={d.node_id}>
                  <CardHeader>
                    <CardTitle>{doc?.nodes.find((n) => n.id === d.node_id)?.label ?? d.node_id}</CardTitle>
                  </CardHeader>
                  <CardContent>
                    <DecisionView decision={d} doc={doc} run={run} labelOf={labelOfFor(doc, catalog.map)} expert={!plain} />
                  </CardContent>
                </Card>
              ))}
            </div>
          ) : (
            <p className="text-[13px] text-muted-foreground">There is no decision yet.</p>
          )}
        </TabsContent>
        <TabsContent value="outputs">
          {Object.keys(run.outputs ?? {}).length ? (
            <div className="grid gap-3">
              {Object.entries(run.outputs ?? {}).map(([name, out]) =>
                out.type === "report" && out.inline ? (
                  <Card key={name}>
                    <CardContent className="pt-4">
                      <PayloadView value={out.inline} title={name} />
                    </CardContent>
                  </Card>
                ) : (
                  <OutputView key={name} runId={run.run_id} nodeId={name} port={name} out={out} />
                ),
              )}
            </div>
          ) : (
            <p className="text-[13px] text-muted-foreground">{terminal ? "This run gave no outputs." : "The outputs show when the run finishes."}</p>
          )}
        </TabsContent>
        <TabsContent value="events">
          <EventLog events={stream.events} />
        </TabsContent>
      </Tabs>

      <NodeDrawer run={run} node={node} doc={doc} catalog={catalog.map} onClose={() => setOpenNode(null)} expert={!plain} />
    </div>
  );
}
