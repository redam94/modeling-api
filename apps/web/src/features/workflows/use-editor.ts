/**
 * Editor state for one workflow document: the draft with undo/redo, live static validation by
 * the server (debounced; never evaluated in the browser), issues pinned to nodes by their JSON
 * pointers, and canvas positions (a deterministic layout plus the user's own arrangement, kept
 * per browser).
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import type { Issue } from "@/api/types";
import { useDebounced } from "@/hooks/use-debounced";

import { validateWorkflow } from "./api";
import { nodeOfPointer } from "./doc";
import type { NodeIssue } from "./inspector";
import { layoutWorkflow, type Point } from "./layout";
import type { WorkflowDoc, WorkflowValidation } from "./types";

const MAX_HISTORY = 100;

export function useDocHistory(initial: WorkflowDoc) {
  const [state, setState] = useState<{ stack: WorkflowDoc[]; index: number }>({ stack: [initial], index: 0 });
  const doc = state.stack[state.index]!;
  const set = useCallback((next: WorkflowDoc, opts: { merge?: boolean } = {}) => {
    setState((s) => {
      const cur = s.stack[s.index]!;
      if (JSON.stringify(cur) === JSON.stringify(next)) return s;
      const base = s.stack.slice(0, s.index + (opts.merge && s.index > 0 ? 0 : 1));
      const stack = [...base, next].slice(-MAX_HISTORY);
      return { stack, index: stack.length - 1 };
    });
  }, []);
  const reset = useCallback((next: WorkflowDoc) => setState({ stack: [next], index: 0 }), []);
  const undo = useCallback(() => setState((s) => ({ ...s, index: Math.max(0, s.index - 1) })), []);
  const redo = useCallback(() => setState((s) => ({ ...s, index: Math.min(s.stack.length - 1, s.index + 1) })), []);
  return { doc, set, reset, undo, redo, canUndo: state.index > 0, canRedo: state.index < state.stack.length - 1 };
}

export interface LiveValidation {
  validation: WorkflowValidation | null;
  /** A refusal of the request itself (unparseable document). */
  problem: ApiError | null;
  validating: boolean;
  /** The document the shown result belongs to (it lags behind while typing). */
  validatedJson: string | null;
}

export function useLiveValidation(doc: WorkflowDoc, enabled = true): LiveValidation {
  const scope = useProjectScope();
  const json = useMemo(() => JSON.stringify(doc), [doc]);
  const debounced = useDebounced(json, 400);
  const q = useQuery({
    queryKey: [...qk.project(scope.key), "workflow-validate", debounced],
    queryFn: async ({ signal }) => {
      try {
        return { validation: await validateWorkflow(scope, JSON.parse(debounced) as Record<string, unknown>, signal), problem: null, json: debounced };
      } catch (err) {
        if (err instanceof ApiError && err.status < 500) return { validation: null, problem: err, json: debounced };
        throw err;
      }
    },
    enabled: enabled && doc.nodes.length > 0,
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    retry: 1,
  });
  return {
    validation: q.data?.validation ?? null,
    problem: q.data?.problem ?? null,
    validating: q.isFetching || debounced !== json,
    validatedJson: q.data?.json ?? null,
  };
}

/** Issues by node id (with the pointer below the node), and the document-level rest. */
export function pinIssues(doc: WorkflowDoc, issues: readonly Issue[]): { byNode: Map<string, NodeIssue[]>; general: Issue[] } {
  const byNode = new Map<string, NodeIssue[]>();
  const general: Issue[] = [];
  for (const issue of issues) {
    const hit = nodeOfPointer(doc, issue.pointer);
    if (hit) {
      const list = byNode.get(hit.id) ?? [];
      list.push({ issue, rest: hit.rest });
      byNode.set(hit.id, list);
    } else general.push(issue);
  }
  return { byNode, general };
}

function storageKey(key: string): string {
  return `mapi.workflow.positions.${key}`;
}

function readPositions(key: string): Record<string, Point> {
  try {
    const raw = localStorage.getItem(storageKey(key));
    const v: unknown = raw ? JSON.parse(raw) : null;
    if (!v || typeof v !== "object") return {};
    const out: Record<string, Point> = {};
    for (const [id, p] of Object.entries(v as Record<string, unknown>)) {
      const pt = p as Partial<Point> | null;
      if (pt && typeof pt.x === "number" && typeof pt.y === "number" && Number.isFinite(pt.x) && Number.isFinite(pt.y)) out[id] = { x: pt.x, y: pt.y };
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * Positions for the canvas: the automatic layout, overridden by where the user dragged nodes
 * (remembered in this browser only). New nodes land where they were dropped.
 */
export function useCanvasPositions(key: string, doc: WorkflowDoc) {
  const [overrides, setOverrides] = useState<Record<string, Point>>(() => readPositions(key));
  const keyRef = useRef(key);
  useEffect(() => {
    if (keyRef.current !== key) {
      keyRef.current = key;
      setOverrides(readPositions(key));
    }
  }, [key]);
  const shape = doc.nodes.map((n) => `${n.id}:${JSON.stringify(n.inputs ?? {})}:${n.when ?? ""}`).join("|");
  // eslint-disable-next-line react-hooks/exhaustive-deps -- the layout depends on the graph's shape only
  const auto = useMemo(() => layoutWorkflow(doc), [shape]);
  const positions = useMemo(() => {
    const ids = new Set(["inputs", ...doc.nodes.map((n) => n.id)]);
    const out: Record<string, Point> = {};
    for (const id of ids) out[id] = overrides[id] ?? auto[id] ?? { x: 0, y: 0 };
    return out;
  }, [auto, overrides, doc.nodes]);
  const persist = (next: Record<string, Point>) => {
    try {
      localStorage.setItem(storageKey(key), JSON.stringify(next));
    } catch {
      // storage unavailable: the arrangement lasts for this page only
    }
  };
  const move = useCallback(
    (id: string, p: Point) =>
      setOverrides((o) => {
        const next = { ...o, [id]: { x: Math.round(p.x), y: Math.round(p.y) } };
        persist(next);
        return next;
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key],
  );
  const rename = useCallback((from: string, to: string) => setOverrides((o) => (o[from] ? { ...o, [to]: o[from] } : o)), []);
  const arrange = useCallback(() => {
    setOverrides({});
    persist({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return { positions, move, rename, arrange };
}
