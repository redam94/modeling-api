/**
 * Typed ports: what each node takes and gives, and whether an edge may connect two ports.
 * Mirrors the port checks of `mapi_spec.workflow.validate_workflow` so the canvas can refuse an
 * incompatible connection with a reason before the server sees it (the server still validates
 * every saved document).
 */
import { ancestors, bindingList, dependencyPath, formatBinding, implId, nodeDependencies, parseBinding } from "./doc";
import type { NodeImplementation, PortSpec, PortType, WorkflowDoc, WorkflowNodeDoc } from "./types";
import { BRANCH_NODE, INPUTS_NODE, SUBWORKFLOW_NODE } from "./types";

export type Catalog = ReadonlyMap<string, NodeImplementation>;

/** The ports of a saved workflow used as a sub-workflow: its inputs and typed outputs. */
export interface SubworkflowPorts {
  inputs: Record<string, PortSpec>;
  outputs: Record<string, PortType>;
}

/** Sub-workflow ports by the `workflow://…` reference a `subworkflow.run` node names. */
export type SubworkflowIndex = ReadonlyMap<string, SubworkflowPorts>;

export const PORT_TYPE_META: Record<PortType, { label: string; plain: string; color: string }> = {
  dataset: { label: "dataset", plain: "Data", color: "var(--series-5)" },
  test_result: { label: "test result", plain: "Test result", color: "var(--series-3)" },
  split_plan: { label: "split plan", plain: "Train/test plan", color: "var(--series-6)" },
  model_version: { label: "model version", plain: "Fitted model", color: "var(--series-1)" },
  metrics: { label: "metrics", plain: "Scores", color: "var(--series-2)" },
  table: { label: "table", plain: "Table", color: "var(--series-8)" },
  plot: { label: "plot", plain: "Chart", color: "var(--series-4)" },
  report: { label: "report", plain: "Report", color: "var(--series-7)" },
  evidence: { label: "evidence", plain: "Governance evidence", color: "var(--warning)" },
};

export function subworkflowRef(node: WorkflowNodeDoc): string | null {
  const ref = node.with?.workflow;
  return typeof ref === "string" ? ref : null;
}

/** The input ports of a node (a sub-workflow takes its child's declared inputs). */
export function inputPorts(node: WorkflowNodeDoc, catalog: Catalog, subs: SubworkflowIndex = new Map()): Record<string, PortSpec> {
  const id = implId(node.use);
  if (id === SUBWORKFLOW_NODE) {
    const ref = subworkflowRef(node);
    return (ref && subs.get(ref)?.inputs) || {};
  }
  return catalog.get(id)?.inputs ?? {};
}

/** The output ports of a node (a sub-workflow gives its child's named outputs). */
export function outputPorts(node: WorkflowNodeDoc, catalog: Catalog, subs: SubworkflowIndex = new Map()): Record<string, PortType> {
  const id = implId(node.use);
  if (id === SUBWORKFLOW_NODE) {
    const ref = subworkflowRef(node);
    return (ref && subs.get(ref)?.outputs) || {};
  }
  return catalog.get(id)?.outputs ?? {};
}

/** The type a binding carries, or `null` when it names nothing known. */
export function bindingType(doc: WorkflowDoc, binding: string, catalog: Catalog, subs: SubworkflowIndex = new Map()): PortType | null {
  const ref = parseBinding(binding);
  if (!ref) return null;
  if (ref.kind === "input") return doc.inputs?.[ref.name]?.type ?? null;
  const src = doc.nodes.find((n) => n.id === ref.node);
  return src ? (outputPorts(src, catalog, subs)[ref.port] ?? null) : null;
}

/** Ports of a child workflow document: its inputs and the types of its outputs. */
export function subworkflowPortsOf(child: WorkflowDoc, catalog: Catalog): SubworkflowPorts {
  const inputs: Record<string, PortSpec> = {};
  for (const [name, decl] of Object.entries(child.inputs ?? {})) {
    inputs[name] = { type: decl.type, required: (decl.required ?? true) && !decl.default, multiple: false, description: decl.description ?? "" };
  }
  const outputs: Record<string, PortType> = {};
  for (const [name, b] of Object.entries(child.outputs ?? {})) {
    const t = bindingType(child, b, catalog);
    if (t) outputs[name] = t;
  }
  return { inputs, outputs };
}

export type ConnectionCheck =
  | { ok: true; mode: "replace" | "append"; note: string | null }
  | { ok: false; reason: string };

/**
 * May `source.sourcePort` feed `target.targetPort`? A refusal says why in plain words: wrong
 * type, a node feeding itself, a cycle, or an unknown port. A single-value port that is already
 * bound is replaced (`append` would add an alternative); a multiple port collects sources.
 */
export function checkConnection(
  doc: WorkflowDoc,
  catalog: Catalog,
  source: { node: string; port: string },
  target: { node: string; port: string },
  subs: SubworkflowIndex = new Map(),
): ConnectionCheck {
  if (source.node === target.node) return { ok: false, reason: "A node cannot send data to its own input." };
  const tNode = doc.nodes.find((n) => n.id === target.node);
  if (!tNode) return { ok: false, reason: `There is no node "${target.node}".` };
  const spec = inputPorts(tNode, catalog, subs)[target.port];
  if (!spec) return { ok: false, reason: `${target.node} has no input "${target.port}".` };
  const binding = formatBinding(source.node, source.port);
  const type = bindingType(doc, binding, catalog, subs);
  if (!type) {
    return { ok: false, reason: source.node === INPUTS_NODE ? `The workflow has no input "${source.port}".` : `${source.node} has no output "${source.port}".` };
  }
  if (type !== spec.type) {
    return {
      ok: false,
      reason: `${target.node}.${target.port} takes ${PORT_TYPE_META[spec.type].label}, but ${binding} gives ${PORT_TYPE_META[type].label}.`,
    };
  }
  if (source.node !== INPUTS_NODE) {
    const deps = nodeDependencies(doc);
    if (ancestors(deps, source.node).has(target.node)) {
      const path = dependencyPath(deps, source.node, target.node) ?? [source.node, target.node];
      return { ok: false, reason: `The connection makes a cycle: ${[...path].reverse().join(" → ")} → ${source.node}.` };
    }
  }
  const current = bindingList(tNode.inputs?.[target.port]);
  if (current.includes(binding)) return { ok: false, reason: `${binding} is already connected to ${target.node}.${target.port}.` };
  if (spec.multiple) return { ok: true, mode: "append", note: current.length ? `Adds a source to the ${current.length} sources that the input has.` : null };
  return { ok: true, mode: "replace", note: current.length ? `Replaces ${current.join(", ")}. To keep the current source, add the new source as an alternative in the node inspector.` : null };
}

/** The sources that could feed `target.port`: workflow inputs and outputs of upstream-safe nodes. */
export function compatibleSources(
  doc: WorkflowDoc,
  catalog: Catalog,
  target: { node: string; port: string },
  subs: SubworkflowIndex = new Map(),
): { binding: string; label: string }[] {
  const out: { binding: string; label: string }[] = [];
  for (const name of Object.keys(doc.inputs ?? {})) {
    if (checkConnection(doc, catalog, { node: INPUTS_NODE, port: name }, target, subs).ok) out.push({ binding: `inputs.${name}`, label: `Workflow input ${name}` });
  }
  for (const n of doc.nodes) {
    if (n.id === target.node) continue;
    for (const port of Object.keys(outputPorts(n, catalog, subs))) {
      if (checkConnection(doc, catalog, { node: n.id, port }, target, subs).ok) out.push({ binding: `${n.id}.${port}`, label: `${n.label || n.id} → ${port}` });
    }
  }
  return out;
}

/** Branch nodes whose arms may gate `node` (no cycle). */
export function gateCandidates(doc: WorkflowDoc, node: string): string[] {
  const deps = nodeDependencies(doc);
  return doc.nodes
    .filter((n) => implId(n.use) === BRANCH_NODE && n.id !== node && !ancestors(deps, n.id).has(node))
    .map((n) => n.id);
}

/** Required input ports of a node that nothing feeds. */
export function unboundRequired(node: WorkflowNodeDoc, catalog: Catalog, subs: SubworkflowIndex = new Map()): string[] {
  return Object.entries(inputPorts(node, catalog, subs))
    .filter(([port, spec]) => spec.required && !bindingList(node.inputs?.[port]).length)
    .map(([port]) => port);
}
