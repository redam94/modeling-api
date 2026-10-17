/**
 * The canvas's graph, computed from the document (no React Flow import, so it is cheap to test):
 * one box per node plus the workflow inputs, data edges per binding (alternatives numbered),
 * control edges from a branch arm to the nodes it gates, and dotted "reads" edges for nodes a
 * condition or `$expr` reads without a binding. Handle ids encode what a connection means:
 * `out:<port>` → `in:<port>` binds data, `arm:then|else` → `gate` sets `when`.
 */
import type { Issue } from "@/api/types";

import { bindingList, expressionTexts, implId, parseBinding, parseWhen, referencedNodes } from "./doc";
import { checkConnection, gateCandidates, inputPorts, outputPorts, type Catalog, type ConnectionCheck, type SubworkflowIndex } from "./ports";
import type { NodeKind, NodeRunStatus, PortSpec, PortType, WorkflowDoc } from "./types";
import { BRANCH_NODE, INPUTS_NODE } from "./types";

export interface CanvasNodeData {
  [key: string]: unknown;
  nodeId: string;
  /** `inputs` is the workflow-inputs box. */
  role: "node" | "inputs";
  title: string;
  label: string | null;
  implId: string;
  kind: NodeKind | null;
  known: boolean;
  inputs: [string, PortSpec][];
  outputs: [string, PortType][];
  isBranch: boolean;
  gatedBy: { branch: string; arm: "then" | "else" } | null;
  issues: Issue[];
  status: NodeRunStatus | null;
  readOnly: boolean;
}

export interface CanvasEdgeModel {
  id: string;
  source: string;
  target: string;
  sourceHandle: string;
  targetHandle: string;
  kind: "data" | "control" | "reads";
  label: string | null;
  portType: PortType | null;
  /** The binding the edge stands for (data edges). */
  binding: string | null;
  port: string | null;
}

export const GATE_HANDLE = "gate";
export const REF_OUT = "ref-out";
export const REF_IN = "ref-in";

export function buildCanvasNodes(
  doc: WorkflowDoc,
  catalog: Catalog,
  opts: { subs?: SubworkflowIndex; issues?: Map<string, Issue[]>; statuses?: Map<string, NodeRunStatus>; readOnly?: boolean } = {},
): CanvasNodeData[] {
  const subs = opts.subs ?? new Map();
  const out: CanvasNodeData[] = [
    {
      nodeId: INPUTS_NODE,
      role: "inputs",
      title: "Workflow inputs",
      label: null,
      implId: "",
      kind: null,
      known: true,
      inputs: [],
      outputs: Object.entries(doc.inputs ?? {}).map(([name, d]) => [name, d.type] as [string, PortType]),
      isBranch: false,
      gatedBy: null,
      issues: opts.issues?.get(INPUTS_NODE) ?? [],
      status: null,
      readOnly: Boolean(opts.readOnly),
    },
  ];
  for (const n of doc.nodes) {
    const id = implId(n.use);
    const impl = catalog.get(id);
    const ins = Object.entries(inputPorts(n, catalog, subs));
    // A port bound in the document but unknown to the catalog still gets a handle (its issue says why).
    for (const port of Object.keys(n.inputs ?? {})) if (!ins.some(([p]) => p === port)) ins.push([port, { type: "dataset", required: false, multiple: false, description: "Not an input of this node" }]);
    out.push({
      nodeId: n.id,
      role: "node",
      title: impl?.about.title ?? id,
      label: n.label ?? null,
      implId: id,
      kind: impl?.kind ?? n.kind ?? null,
      known: Boolean(impl),
      inputs: ins,
      outputs: Object.entries(outputPorts(n, catalog, subs)),
      isBranch: id === BRANCH_NODE,
      gatedBy: parseWhen(n.when),
      issues: opts.issues?.get(n.id) ?? [],
      status: opts.statuses?.get(n.id) ?? null,
      readOnly: Boolean(opts.readOnly),
    });
  }
  return out;
}

export function buildCanvasEdges(doc: WorkflowDoc, catalog: Catalog, subs: SubworkflowIndex = new Map()): CanvasEdgeModel[] {
  const edges: CanvasEdgeModel[] = [];
  const ids = new Set(doc.nodes.map((n) => n.id));
  for (const n of doc.nodes) {
    const ports = inputPorts(n, catalog, subs);
    const linked = new Set<string>();
    for (const [port, b] of Object.entries(n.inputs ?? {})) {
      const list = bindingList(b);
      const multiple = ports[port]?.multiple ?? false;
      list.forEach((s, k) => {
        const ref = parseBinding(s);
        if (!ref) return;
        const source = ref.kind === "input" ? INPUTS_NODE : ref.node;
        if (ref.kind === "node" && !ids.has(ref.node)) return;
        const sourcePort = ref.kind === "input" ? ref.name : ref.port;
        linked.add(source);
        const src = source === INPUTS_NODE ? null : doc.nodes.find((x) => x.id === source)!;
        const portType = source === INPUTS_NODE ? (doc.inputs?.[sourcePort]?.type ?? null) : (outputPorts(src!, catalog, subs)[sourcePort] ?? null);
        edges.push({
          id: `d:${n.id}:${port}:${k}`,
          source,
          target: n.id,
          sourceHandle: `out:${sourcePort}`,
          targetHandle: `in:${port}`,
          kind: "data",
          label: list.length > 1 && !multiple ? `alt ${k + 1}` : null,
          portType,
          binding: s,
          port,
        });
      });
    }
    const when = parseWhen(n.when);
    if (when && ids.has(when.branch)) {
      linked.add(when.branch);
      edges.push({ id: `w:${n.id}`, source: when.branch, target: n.id, sourceHandle: `arm:${when.arm}`, targetHandle: GATE_HANDLE, kind: "control", label: when.arm, portType: null, binding: null, port: null });
    }
    const reads = new Set<string>();
    for (const { text } of expressionTexts(n)) for (const r of referencedNodes(text)) if (ids.has(r) && r !== n.id && !linked.has(r)) reads.add(r);
    for (const r of reads) edges.push({ id: `r:${r}:${n.id}`, source: r, target: n.id, sourceHandle: REF_OUT, targetHandle: REF_IN, kind: "reads", label: null, portType: null, binding: null, port: null });
  }
  return edges;
}

/** What a drag from `sourceHandle` to `targetHandle` would do, or why it is refused. */
export function checkCanvasConnection(
  doc: WorkflowDoc,
  catalog: Catalog,
  conn: { source: string; sourceHandle: string | null; target: string; targetHandle: string | null },
  subs: SubworkflowIndex = new Map(),
): ({ kind: "data"; port: string; sourcePort: string } & ConnectionCheck) | ({ kind: "control"; arm: "then" | "else" } & ({ ok: true } | { ok: false; reason: string })) {
  const sh = conn.sourceHandle ?? "";
  const th = conn.targetHandle ?? "";
  if (sh.startsWith("arm:")) {
    const arm = sh.slice(4) as "then" | "else";
    if (th !== GATE_HANDLE) return { kind: "control", arm, ok: false, reason: `A branch arm does not carry data: drop it on the top edge of a node to run that node only on the ${arm} arm.` };
    if (conn.target === conn.source) return { kind: "control", arm, ok: false, reason: "A branch cannot gate itself." };
    if (!gateCandidates(doc, conn.target).includes(conn.source)) return { kind: "control", arm, ok: false, reason: `${conn.source} runs after ${conn.target}, so it cannot gate it (that would be a cycle).` };
    return { kind: "control", arm, ok: true };
  }
  const sourcePort = sh.startsWith("out:") ? sh.slice(4) : "";
  if (th === GATE_HANDLE) {
    return { kind: "data", port: "", sourcePort, ok: false, reason: "The top edge of a node takes a branch arm (then or else), not data." };
  }
  const port = th.startsWith("in:") ? th.slice(3) : "";
  return { kind: "data", port, sourcePort, ...checkConnection(doc, catalog, { node: conn.source, port: sourcePort }, { node: conn.target, port }, subs) };
}
