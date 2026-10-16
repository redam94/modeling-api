/**
 * The node detail drawer of a workflow run: what happened in plain words (ran, reused, skipped
 * and why, failed and why), the test verdict or the branch decision with the values it read, the
 * node's result and outputs, its child runs (fits, sandbox runs, child workflows) with their
 * logs, and its cost.
 */
import { Link } from "@tanstack/react-router";
import { ExternalLink, GitBranch, Recycle, X } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";

import { JsonBlock, KeyValues, Mono, UntrustedText } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import { AboutCard } from "@/features/about/about-card";
import { LogsViewer } from "@/features/runs/logs-viewer";
import { runCostView } from "@/lib/cost";
import { absoluteTime, elapsedSeconds, formatDuration, shortId } from "@/lib/format";

import { implId, parseWhen } from "./doc";
import { OutputView, TestVerdict } from "./outputs";
import type { Catalog } from "./ports";
import { NODE_STATUS_META, TONE_BADGE } from "./status";
import type { BranchDecision, NodeRun, WorkflowDoc, WorkflowRun } from "./types";
import { BRANCH_NODE, SUBWORKFLOW_NODE } from "./types";

function show(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** "diff ran; sdiff was skipped" for the arm a branch took. */
export function decisionSentence(d: BranchDecision, doc: WorkflowDoc | null): string {
  const taken = (doc?.nodes ?? []).filter((n) => parseWhen(n.when)?.branch === d.node_id && parseWhen(n.when)?.arm === d.arm).map((n) => n.id);
  const other = (doc?.nodes ?? []).filter((n) => parseWhen(n.when)?.branch === d.node_id && parseWhen(n.when)?.arm !== d.arm).map((n) => n.id);
  const parts = [`The condition was ${d.value ? "true" : "false"}, so the ${d.arm} arm was taken`];
  if (taken.length) parts.push(`${taken.join(", ")} ran`);
  if (other.length) parts.push(`${other.join(", ")} ${other.length === 1 ? "was" : "were"} skipped`);
  return `${parts.join("; ")}.`;
}

export function DecisionView({ decision, doc }: { decision: BranchDecision; doc: WorkflowDoc | null }) {
  const values = Object.entries(decision.inputs ?? {});
  return (
    <section className="grid gap-2" aria-label="Decision" data-testid="branch-decision">
      <p className="flex items-center gap-2 text-[14px] font-medium">
        <GitBranch className="size-4 text-muted-foreground" aria-hidden />
        Took the <Badge variant={decision.arm === "then" ? "success" : "secondary"}>{decision.arm}</Badge> arm
      </p>
      <p className="text-[13px]">{decisionSentence(decision, doc)}</p>
      <pre className="overflow-x-auto rounded-md border bg-muted/50 p-2 font-mono text-xs whitespace-pre-wrap">{decision.condition}</pre>
      {values.length ? (
        <div>
          <p className="text-xs font-medium text-muted-foreground">Values it read</p>
          <KeyValues items={values.map(([k, v]) => [<Mono key={k}>{k}</Mono>, <Mono key={`${k}-v`}>{show(v)}</Mono>])} />
        </div>
      ) : null}
    </section>
  );
}

function ChildRunLink({ id, workflow }: { id: string; workflow: boolean }) {
  return workflow ? (
    <Link to="/workflow-runs/$runId" params={{ runId: id }} className="inline-flex items-center gap-1 text-primary hover:underline">
      <Mono>{shortId(id)}</Mono> <ExternalLink className="size-3" aria-hidden />
    </Link>
  ) : (
    <Link to="/runs/$runId" params={{ runId: id }} className="inline-flex items-center gap-1 text-primary hover:underline">
      <Mono>{shortId(id)}</Mono> <ExternalLink className="size-3" aria-hidden />
    </Link>
  );
}

export function NodeDrawer({
  run,
  node,
  doc,
  catalog,
  onClose,
}: {
  run: WorkflowRun;
  node: NodeRun | null;
  doc: WorkflowDoc | null;
  catalog: Catalog;
  onClose: () => void;
}) {
  const open = Boolean(node);
  const impl = node ? catalog.get(implId(node.use)) : undefined;
  const meta = node ? NODE_STATUS_META[node.status] : null;
  const decision = node ? (run.decisions ?? []).find((d) => d.node_id === node.node_id) : undefined;
  const docNode = node ? doc?.nodes.find((n) => n.id === node.node_id) : undefined;
  const isTest = node?.kind === "test" && node.result && typeof node.result.verdict_plain === "string";
  const isSub = node ? implId(node.use) === SUBWORKFLOW_NODE : false;
  const cost = runCostView(node?.cost);
  const logsRun = node?.run_id ?? node?.child_runs?.at(-1) ?? null;
  const live = node?.status === "running" || node?.status === "queued";
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/30" />
        <DialogPrimitive.Content
          className="fixed top-0 right-0 z-50 grid h-dvh w-full max-w-xl content-start gap-4 overflow-y-auto border-l bg-popover p-5 text-popover-foreground shadow-2xl"
          aria-describedby={undefined}
          data-testid="node-drawer"
        >
          {node && meta ? (
            <>
              <header className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <DialogPrimitive.Title className="truncate text-lg font-semibold">{docNode?.label ?? impl?.about.title ?? node.node_id}</DialogPrimitive.Title>
                  <p className="font-mono text-xs text-muted-foreground">
                    {node.node_id} · {node.use}
                  </p>
                </div>
                <DialogPrimitive.Close className="rounded-sm p-1 text-muted-foreground hover:text-foreground" aria-label="Close">
                  <X className="size-4" />
                </DialogPrimitive.Close>
              </header>

              <section className="grid gap-2 rounded-lg border p-3" aria-label="What happened">
                <p className="flex flex-wrap items-center gap-2">
                  <Badge variant={TONE_BADGE[meta.tone]}>{meta.label}</Badge>
                  {node.status === "cached" ? (
                    <span className="inline-flex items-center gap-1 text-[13px]">
                      <Recycle className="size-3.5 text-success" aria-hidden /> Reused, not run again
                    </span>
                  ) : node.status === "succeeded" && implId(node.use) !== BRANCH_NODE ? (
                    <span className="text-[13px] text-muted-foreground">Ran in this run{(node.attempt ?? 0) > 1 ? ` (attempt ${node.attempt})` : ""}</span>
                  ) : null}
                </p>
                <p className="text-[13px] text-muted-foreground">
                  {node.status === "cached" && node.cached_from ? (
                    <>
                      Nothing it depends on changed, so the result of run <ChildRunLink id={node.cached_from} workflow /> was reused at no cost.
                    </>
                  ) : node.status === "skipped" ? (
                    (node.skipped_reason ?? meta.plain)
                  ) : (
                    meta.plain
                  )}
                </p>
                {node.error ? (
                  <div role="alert" className="grid gap-1 rounded-md border border-destructive/35 bg-destructive/6 p-2">
                    <p className="text-[13px] font-medium text-destructive">
                      {node.error.code}: {node.error.message}
                    </p>
                    {node.error.untrusted_text ? (
                      <details>
                        <summary className="cursor-pointer text-xs text-muted-foreground">Output from the run (untrusted)</summary>
                        <UntrustedText text={node.error.untrusted_text} className="mt-1" maxHeight="12rem" />
                      </details>
                    ) : null}
                  </div>
                ) : null}
                <KeyValues
                  items={[
                    ["Started", node.started_at ? <span title={node.started_at}>{absoluteTime(node.started_at)}</span> : null],
                    ["Took", formatDuration(elapsedSeconds(node.started_at, node.finished_at))],
                    ["Cost", cost ? <span title={cost.detail}>{cost.text}</span> : node.status === "cached" || implId(node.use) === BRANCH_NODE ? "Free" : null],
                  ]}
                />
              </section>

              {decision ? <DecisionView decision={decision} doc={doc} /> : null}
              {isTest && node.result ? (
                <section aria-label="Test result" className="grid gap-2">
                  <h3 className="text-sm font-semibold">Verdict</h3>
                  <TestVerdict result={node.result} />
                </section>
              ) : null}

              {Object.keys(node.outputs ?? {}).length ? (
                <section className="grid gap-2" aria-label="Outputs">
                  <h3 className="text-sm font-semibold">Outputs</h3>
                  {Object.entries(node.outputs ?? {})
                    .filter(([, o]) => !(isTest && o.type === "test_result"))
                    .map(([port, out]) => (
                      <OutputView key={port} runId={run.run_id} nodeId={node.node_id} port={port} out={out} />
                    ))}
                </section>
              ) : null}

              {node.result && !isTest && !decision ? (
                <details className="rounded-md border p-2">
                  <summary className="cursor-pointer text-sm font-semibold">Result fields</summary>
                  <p className="mt-1 text-xs text-muted-foreground">What conditions downstream can read as nodes.{node.node_id}.&lt;field&gt;.</p>
                  <JsonBlock value={node.result} className="mt-2" maxHeight="18rem" />
                </details>
              ) : null}

              {Object.keys(node.params ?? {}).length ? (
                <details className="rounded-md border p-2">
                  <summary className="cursor-pointer text-sm font-semibold">Settings it ran with</summary>
                  <JsonBlock value={node.params} className="mt-2" maxHeight="14rem" />
                </details>
              ) : null}

              {node.child_runs?.length || node.run_id ? (
                <section className="grid gap-1" aria-label="Runs">
                  <h3 className="text-sm font-semibold">{isSub ? "Child workflow runs" : "Runs"}</h3>
                  <ul className="grid gap-1 text-[13px]">
                    {node.run_id ? (
                      <li>
                        Sandbox run <ChildRunLink id={node.run_id} workflow={false} />
                      </li>
                    ) : null}
                    {(node.child_runs ?? []).map((id, i) => (
                      <li key={id}>
                        {isSub ? "Workflow run" : node.kind === "model" ? "Fit" : "Child run"}
                        {node.child_runs!.length > 1 ? ` (attempt ${i + 1})` : ""} <ChildRunLink id={id} workflow={isSub} />
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}

              {logsRun && !isSub ? (
                <details className="rounded-md border p-2">
                  <summary className="cursor-pointer text-sm font-semibold">Logs</summary>
                  <div className="mt-2">
                    <LogsViewer runId={logsRun} live={live} />
                  </div>
                </details>
              ) : null}

              {impl ? <AboutCard about={impl.about} compact headingLevel={3} heading={<span className="text-sm">About this node</span>} /> : null}
            </>
          ) : (
            <DialogPrimitive.Title className="sr-only">Node</DialogPrimitive.Title>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
