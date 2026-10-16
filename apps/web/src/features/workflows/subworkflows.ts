/** The ports of the saved workflows that `subworkflow.run` nodes of a document name. */
import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";

import { useProjectScope } from "@/api/queries";

import { revisionQuery, useWorkflows } from "./api";
import { implId } from "./doc";
import { parseWorkflowRef } from "./pickers";
import { subworkflowPortsOf, subworkflowRef, type Catalog, type SubworkflowIndex } from "./ports";
import type { WorkflowDoc } from "./types";
import { SUBWORKFLOW_NODE } from "./types";

export function useSubworkflowIndex(doc: WorkflowDoc | null, catalog: Catalog): SubworkflowIndex {
  const scope = useProjectScope();
  const refs = useMemo(
    () => [...new Set((doc?.nodes ?? []).filter((n) => implId(n.use) === SUBWORKFLOW_NODE).map(subworkflowRef).filter((r): r is string => Boolean(r)))],
    [doc],
  );
  const workflows = useWorkflows();
  const wanted = refs
    .map((ref) => {
      const p = parseWorkflowRef(ref);
      const wf = p ? workflows.data?.items.find((w) => w.name === p.name) : undefined;
      if (!p || !wf) return null;
      return { ref, id: wf.id, revision: typeof p.revision === "number" ? p.revision : wf.latest_revision };
    })
    .filter((x): x is { ref: string; id: string; revision: number } => x !== null);
  const results = useQueries({ queries: wanted.map((w) => revisionQuery(scope, w.id, w.revision)) });
  const docs = results.map((r) => r.data?.document);
  const key = JSON.stringify(wanted) + docs.map((d) => (d ? "1" : "0")).join("");
  return useMemo(() => {
    const out = new Map();
    wanted.forEach((w, i) => {
      const child = docs[i];
      if (child) out.set(w.ref, subworkflowPortsOf(child as unknown as WorkflowDoc, catalog));
    });
    return out as SubworkflowIndex;
    // `key` captures `wanted` and which documents have loaded.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, catalog]);
}
