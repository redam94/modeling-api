/** Revisions of a workflow: each with its static validation, and a side-by-side diff of two. */
import { Link } from "@tanstack/react-router";
import { CheckCircle2, GitCompare, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { LoadingRows, QueryView } from "@/components/common";
import { Button } from "@/components/ui/button";
import { NativeSelect, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { DiffView } from "@/features/editor/diff-view";
import { relativeTime } from "@/lib/format";

import { useWorkflowRevision, useWorkflowRevisions } from "./api";
import { toYaml } from "./doc";
import type { WorkflowDoc } from "./types";

export function RevisionDiff({ workflowId, a, b }: { workflowId: string; a: number; b: number }) {
  const ra = useWorkflowRevision(workflowId, a);
  const rb = useWorkflowRevision(workflowId, b);
  const texts = useMemo(
    () => (ra.data && rb.data ? [toYaml(ra.data.document as unknown as WorkflowDoc), toYaml(rb.data.document as unknown as WorkflowDoc)] : null),
    [ra.data, rb.data],
  );
  if (!texts) return <LoadingRows rows={4} />;
  return <DiffView original={texts[0]!} modified={texts[1]!} height="55vh" />;
}

export function RevisionsPanel({ workflowId, latest }: { workflowId: string; latest: number }) {
  const revisions = useWorkflowRevisions(workflowId);
  const [a, setA] = useState(Math.max(1, latest - 1));
  const [b, setB] = useState(latest);
  const [compare, setCompare] = useState(false);
  return (
    <div className="grid gap-5">
      <QueryView query={revisions}>
        {(page) => (
          <Table aria-label="Revisions">
            <THead>
              <TR>
                <TH>Revision</TH>
                <TH>What changed</TH>
                <TH>Checks</TH>
                <TH>Saved</TH>
                <TH />
              </TR>
            </THead>
            <TBody>
              {page.items.map((r) => {
                const errors = (r.validation.issues ?? []).filter((i) => (i.severity ?? "error") === "error").length;
                return (
                  <TR key={r.revision}>
                    <TD className="font-mono">r{r.revision}</TD>
                    <TD>{r.message ?? <span className="text-muted-foreground">—</span>}</TD>
                    <TD>
                      {r.validation.valid ? (
                        <span className="inline-flex items-center gap-1 text-success">
                          <CheckCircle2 className="size-3.5" aria-hidden /> Runnable
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-destructive">
                          <XCircle className="size-3.5" aria-hidden /> {errors} problem{errors === 1 ? "" : "s"}
                        </span>
                      )}
                    </TD>
                    <TD title={r.created_at}>{relativeTime(r.created_at)}</TD>
                    <TD>
                      <Button variant="ghost" size="sm" asChild>
                        <Link to="/workflows/$workflowId" params={{ workflowId }} search={{ tab: "editor", rev: r.revision }}>
                          Open
                        </Link>
                      </Button>
                    </TD>
                  </TR>
                );
              })}
            </TBody>
          </Table>
        )}
      </QueryView>
      {latest > 1 ? (
        <section className="grid gap-3" aria-label="Compare revisions">
          <div className="flex flex-wrap items-end gap-2">
            <NativeSelect aria-label="Older revision" className="w-28" value={a} onChange={(e) => setA(Number(e.target.value))}>
              {Array.from({ length: latest }, (_, i) => i + 1).map((n) => (
                <option key={n} value={n}>
                  r{n}
                </option>
              ))}
            </NativeSelect>
            <span className="pb-2 text-muted-foreground">→</span>
            <NativeSelect aria-label="Newer revision" className="w-28" value={b} onChange={(e) => setB(Number(e.target.value))}>
              {Array.from({ length: latest }, (_, i) => i + 1).map((n) => (
                <option key={n} value={n}>
                  r{n}
                </option>
              ))}
            </NativeSelect>
            <Button variant="outline" onClick={() => setCompare(true)} disabled={a === b}>
              <GitCompare /> Compare
            </Button>
          </div>
          {compare && a !== b ? <RevisionDiff workflowId={workflowId} a={a} b={b} /> : null}
        </section>
      ) : null}
    </div>
  );
}
