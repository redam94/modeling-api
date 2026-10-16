/**
 * Pure operations on a `WorkflowSpec` document: reading bindings and dependencies, editing
 * nodes and edges, and the YAML/JSON text view. The document stays the single source of truth:
 * the canvas, the inspector and the YAML view all edit it through these functions, and the
 * server validates every result (`POST /v1/workflows:validate`). Nothing here evaluates an
 * expression: conditions and `$expr` values are only scanned for the node ids they name.
 */
import { parse, stringify } from "yaml";

import { parsePointer } from "@/lib/pointer";

import type { Binding, Json, JsonObject, WorkflowDoc, WorkflowNodeDoc } from "./types";
import { BRANCH_NODE, INPUTS_NODE } from "./types";

/** `mapi_spec.workflow._ID_RE`. */
export const NODE_ID_RE = /^[a-z][a-z0-9_]{0,62}$/;
const BIND_RE = /^(?:inputs\.([A-Za-z_][A-Za-z0-9_.-]{0,127})|([a-z][a-z0-9_]{0,62})\.([a-z][a-z0-9_]{0,62}))$/;
const WHEN_RE = /^([a-z][a-z0-9_]{0,62})\.(then|else)$/;
const RESERVED_IDS = new Set(["inputs", "params", "nodes", "run"]);
/** `nodes.<id>` inside a condition or a `$expr` (scanned, never evaluated). */
const NODE_REF_RE = /\bnodes\.([a-z][a-z0-9_]{0,62})\b/g;

export type BindingRef = { kind: "input"; name: string } | { kind: "node"; node: string; port: string };

export function parseBinding(binding: string): BindingRef | null {
  const m = BIND_RE.exec(binding);
  if (!m) return null;
  if (m[1] !== undefined) return { kind: "input", name: m[1] };
  return { kind: "node", node: m[2] ?? "", port: m[3] ?? "" };
}

export function formatBinding(source: string, port: string): string {
  return source === INPUTS_NODE ? `inputs.${port}` : `${source}.${port}`;
}

export function parseWhen(when: string | null | undefined): { branch: string; arm: "then" | "else" } | null {
  const m = when ? WHEN_RE.exec(when) : null;
  return m ? { branch: m[1] ?? "", arm: m[2] as "then" | "else" } : null;
}

/** The implementation id of a `use` (`test.adf@^1` → `test.adf`). */
export function implId(use: string): string {
  return use.split("@", 1)[0] ?? use;
}

export function bindingList(binding: Binding | undefined): string[] {
  if (binding === undefined) return [];
  return typeof binding === "string" ? [binding] : [...binding];
}

/** Every string that may name upstream nodes: the branch condition and `$expr` values. */
export function expressionTexts(node: WorkflowNodeDoc): { path: (string | number)[]; text: string }[] {
  const out: { path: (string | number)[]; text: string }[] = [];
  const walk = (v: Json | undefined, path: (string | number)[]) => {
    if (v === null || v === undefined) return;
    if (Array.isArray(v)) {
      v.forEach((x, i) => walk(x, [...path, i]));
      return;
    }
    if (typeof v === "object") {
      const keys = Object.keys(v);
      if (keys.length === 1 && keys[0] === "$expr" && typeof v.$expr === "string") {
        out.push({ path, text: v.$expr });
        return;
      }
      for (const k of keys) walk(v[k], [...path, k]);
    }
  };
  const w = node.with ?? {};
  if (implId(node.use) === BRANCH_NODE && typeof w.condition === "string") out.push({ path: ["condition"], text: w.condition });
  for (const [k, v] of Object.entries(w)) {
    if (implId(node.use) === BRANCH_NODE && k === "condition") continue;
    walk(v, [k]);
  }
  return out;
}

/** Node ids named by `nodes.<id>` in a text. */
export function referencedNodes(text: string): string[] {
  const out = new Set<string>();
  for (const m of text.matchAll(NODE_REF_RE)) if (m[1]) out.add(m[1]);
  return [...out];
}

export interface Dependencies {
  /** node id → the node ids it waits for (bindings, `when`, references in expressions). */
  deps: Map<string, Set<string>>;
}

/** What each node waits for (`mapi_spec.workflow.node_dependencies`). Unknown ids are kept out. */
export function nodeDependencies(doc: WorkflowDoc): Map<string, Set<string>> {
  const ids = new Set(doc.nodes.map((n) => n.id));
  const deps = new Map<string, Set<string>>();
  for (const n of doc.nodes) {
    const d = new Set<string>();
    for (const b of Object.values(n.inputs ?? {})) {
      for (const s of bindingList(b)) {
        const ref = parseBinding(s);
        if (ref?.kind === "node" && ids.has(ref.node)) d.add(ref.node);
      }
    }
    const when = parseWhen(n.when);
    if (when && ids.has(when.branch)) d.add(when.branch);
    for (const { text } of expressionTexts(n)) for (const id of referencedNodes(text)) if (ids.has(id)) d.add(id);
    d.delete(n.id);
    deps.set(n.id, d);
  }
  return deps;
}

/** Every node `id` depends on, directly or not. */
export function ancestors(deps: Map<string, Set<string>>, id: string): Set<string> {
  const seen = new Set<string>();
  const stack = [...(deps.get(id) ?? [])];
  while (stack.length) {
    const x = stack.pop()!;
    if (seen.has(x)) continue;
    seen.add(x);
    for (const y of deps.get(x) ?? []) stack.push(y);
  }
  return seen;
}

/** A path `from` → … → `to` along dependants, if one exists (for "this would create a cycle"). */
export function dependencyPath(deps: Map<string, Set<string>>, from: string, to: string): string[] | null {
  // Walk "x depends on y" backwards: is `from` reachable from `to` through dependencies?
  const prev = new Map<string, string>();
  const queue = [to];
  const seen = new Set([to]);
  while (queue.length) {
    const x = queue.shift()!;
    if (x === from) {
      const path = [from];
      let cur = from;
      while (cur !== to) {
        cur = prev.get(cur)!;
        path.push(cur);
      }
      return path;
    }
    for (const y of deps.get(x) ?? []) {
      if (!seen.has(y)) {
        seen.add(y);
        prev.set(y, x);
        queue.push(y);
      }
    }
  }
  return null;
}

/** Topological order (Kahn, ties in document order); `null` when the graph has a cycle. */
export function topologicalOrder(doc: WorkflowDoc): string[] | null {
  const deps = nodeDependencies(doc);
  const indeg = new Map<string, number>();
  const users = new Map<string, string[]>();
  for (const n of doc.nodes) {
    indeg.set(n.id, deps.get(n.id)?.size ?? 0);
    users.set(n.id, []);
  }
  for (const [id, d] of deps) for (const x of d) users.get(x)?.push(id);
  const ready = doc.nodes.filter((n) => (indeg.get(n.id) ?? 0) === 0).map((n) => n.id);
  const out: string[] = [];
  while (ready.length) {
    const id = ready.shift()!;
    out.push(id);
    for (const u of users.get(id) ?? []) {
      const left = (indeg.get(u) ?? 0) - 1;
      indeg.set(u, left);
      if (left === 0) ready.push(u);
    }
  }
  return out.length === doc.nodes.length ? out : null;
}

// ------------------------------------------------------------------------------- editing

function cloneDoc(doc: WorkflowDoc): WorkflowDoc {
  return structuredClone(doc);
}

function nodeOf(doc: WorkflowDoc, id: string): WorkflowNodeDoc {
  const n = doc.nodes.find((x) => x.id === id);
  if (!n) throw new Error(`no node ${id}`);
  return n;
}

/** A fresh id for a node of implementation `impl` (`adf`, `adf_2`, …). */
export function freshNodeId(doc: WorkflowDoc, impl: string): string {
  const base = (impl.split(".")[1] ?? impl).replace(/[^a-z0-9_]/g, "_").replace(/^[^a-z]+/, "") || "node";
  const short = base.slice(0, 40);
  const taken = new Set(doc.nodes.map((n) => n.id));
  if (!taken.has(short) && !RESERVED_IDS.has(short)) return short;
  for (let i = 2; ; i++) {
    const id = `${short}_${i}`;
    if (!taken.has(id)) return id;
  }
}

export function nodeIdProblem(doc: WorkflowDoc, id: string, current?: string): string | null {
  if (!NODE_ID_RE.test(id)) return "Use lowercase letters, digits and _, starting with a letter (up to 63).";
  if (RESERVED_IDS.has(id)) return `"${id}" is reserved.`;
  if (id !== current && doc.nodes.some((n) => n.id === id)) return `Another node is called "${id}".`;
  return null;
}

/** Add a node using `impl` (with an empty parameter block, or the given one). */
export function addNode(doc: WorkflowDoc, impl: string, opts: { id?: string; with?: JsonObject; label?: string } = {}): { doc: WorkflowDoc; id: string } {
  const next = cloneDoc(doc);
  const id = opts.id ?? freshNodeId(doc, impl);
  const node: WorkflowNodeDoc = { id, use: impl };
  const w = opts.with ?? (impl === BRANCH_NODE ? { condition: "" } : undefined);
  if (w) node.with = w;
  if (opts.label) node.label = opts.label;
  next.nodes.push(node);
  return { doc: next, id };
}

function prune(bindings: Record<string, Binding>, keep: (b: string) => boolean): Record<string, Binding> {
  const out: Record<string, Binding> = {};
  for (const [port, b] of Object.entries(bindings)) {
    const list = bindingList(b).filter(keep);
    if (list.length === 1) out[port] = list[0]!;
    else if (list.length) out[port] = list;
  }
  return out;
}

/** Remove a node and every binding, `when` and output that names it. */
export function removeNode(doc: WorkflowDoc, id: string): WorkflowDoc {
  const next = cloneDoc(doc);
  next.nodes = next.nodes.filter((n) => n.id !== id);
  for (const n of next.nodes) {
    if (n.inputs) {
      n.inputs = prune(n.inputs, (b) => {
        const r = parseBinding(b);
        return !(r?.kind === "node" && r.node === id);
      });
      if (!Object.keys(n.inputs).length) delete n.inputs;
    }
    if (parseWhen(n.when)?.branch === id) delete n.when;
  }
  if (next.outputs) {
    next.outputs = Object.fromEntries(
      Object.entries(next.outputs).filter(([, b]) => {
        const r = parseBinding(b);
        return !(r?.kind === "node" && r.node === id);
      }),
    );
    if (!Object.keys(next.outputs).length) delete next.outputs;
  }
  return next;
}

function renameInText(text: string, from: string, to: string): string {
  return text.replace(new RegExp(`\\bnodes\\.${from}\\b`, "g"), `nodes.${to}`);
}

function renameInJson(v: Json, from: string, to: string): Json {
  if (Array.isArray(v)) return v.map((x) => renameInJson(x, from, to));
  if (v && typeof v === "object") {
    const keys = Object.keys(v);
    if (keys.length === 1 && keys[0] === "$expr" && typeof v.$expr === "string") return { $expr: renameInText(v.$expr, from, to) };
    return Object.fromEntries(keys.map((k) => [k, renameInJson(v[k] as Json, from, to)]));
  }
  return v;
}

/** Rename a node and every reference to it (bindings, `when`, expressions, outputs). */
export function renameNode(doc: WorkflowDoc, from: string, to: string): WorkflowDoc {
  if (from === to) return doc;
  const next = cloneDoc(doc);
  const rebind = (b: string) => {
    const r = parseBinding(b);
    return r?.kind === "node" && r.node === from ? `${to}.${r.port}` : b;
  };
  for (const n of next.nodes) {
    if (n.id === from) n.id = to;
    if (n.inputs) {
      for (const [port, b] of Object.entries(n.inputs)) n.inputs[port] = typeof b === "string" ? rebind(b) : b.map(rebind);
    }
    const w = parseWhen(n.when);
    if (w?.branch === from) n.when = `${to}.${w.arm}`;
    if (n.with) {
      const isBranch = implId(n.use) === BRANCH_NODE;
      n.with = Object.fromEntries(
        Object.entries(n.with).map(([k, v]) => [k, isBranch && k === "condition" && typeof v === "string" ? renameInText(v, from, to) : renameInJson(v, from, to)]),
      );
    }
  }
  if (next.outputs) for (const [k, b] of Object.entries(next.outputs)) next.outputs[k] = rebind(b);
  return next;
}

/**
 * Bind `binding` to `target.port`. `replace` sets it as the only source; `append` adds it as the
 * next alternative (single-value port) or the next collected source (multiple port).
 */
export function connect(doc: WorkflowDoc, binding: string, target: string, port: string, mode: "replace" | "append"): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, target);
  const current = bindingList(n.inputs?.[port]);
  const list = mode === "replace" ? [binding] : current.includes(binding) ? current : [...current, binding];
  n.inputs = { ...n.inputs, [port]: list.length === 1 ? list[0]! : list };
  return next;
}

/** Remove one source from `target.port` (the whole binding when `binding` is omitted). */
export function disconnect(doc: WorkflowDoc, target: string, port: string, binding?: string): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, target);
  if (!n.inputs) return next;
  const list = binding === undefined ? [] : bindingList(n.inputs[port]).filter((b) => b !== binding);
  if (list.length === 0) delete n.inputs[port];
  else n.inputs[port] = list.length === 1 ? list[0]! : list;
  if (!Object.keys(n.inputs).length) delete n.inputs;
  return next;
}

/** Move one alternative up or down in `target.port`'s list. */
export function moveAlternative(doc: WorkflowDoc, target: string, port: string, index: number, delta: -1 | 1): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, target);
  const list = bindingList(n.inputs?.[port]);
  const j = index + delta;
  if (j < 0 || j >= list.length) return next;
  [list[index], list[j]] = [list[j]!, list[index]!];
  n.inputs = { ...n.inputs, [port]: list };
  return next;
}

export function setWhen(doc: WorkflowDoc, id: string, when: string | null): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, id);
  if (when) n.when = when;
  else delete n.when;
  return next;
}

export function setWith(doc: WorkflowDoc, id: string, w: JsonObject): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, id);
  if (Object.keys(w).length) n.with = w;
  else delete n.with;
  return next;
}

type NodeSettings = Pick<WorkflowNodeDoc, "label" | "description" | "retries" | "timeout" | "cache" | "on_failure" | "use">;

/** Set (or, with `undefined`/`null`/defaults, drop) a node's own fields. */
export function setNodeFields(doc: WorkflowDoc, id: string, fields: Partial<NodeSettings>): WorkflowDoc {
  const next = cloneDoc(doc);
  const n = nodeOf(next, id) as unknown as Record<string, unknown>;
  const defaults: Record<string, unknown> = { retries: 0, cache: true, on_failure: "fail" };
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null || v === "" || defaults[k] === v) delete n[k];
    else n[k] = v;
  }
  return next;
}

export function setOutputs(doc: WorkflowDoc, outputs: Record<string, string>): WorkflowDoc {
  const next = cloneDoc(doc);
  if (Object.keys(outputs).length) next.outputs = outputs;
  else delete next.outputs;
  return next;
}

// ------------------------------------------------------------------------------- issues

/** The node a JSON pointer lands in (`/nodes/3/with/alpha` → the 4th node), else `null`. */
export function nodeOfPointer(doc: WorkflowDoc, pointer: string | null | undefined): { id: string; rest: string[] } | null {
  if (!pointer) return null;
  let seg: string[];
  try {
    seg = parsePointer(pointer);
  } catch {
    return null;
  }
  if (seg[0] !== "nodes" || seg.length < 2 || !/^\d+$/.test(seg[1] ?? "")) return null;
  const n = doc.nodes[Number(seg[1])];
  return n ? { id: n.id, rest: seg.slice(2) } : null;
}

// ------------------------------------------------------------------------------- text

/** The document as YAML (block style, no line folding: conditions stay on one line). */
export function toYaml(doc: WorkflowDoc): string {
  return stringify(doc, { lineWidth: 0, aliasDuplicateObjects: false, indent: 2 });
}

export function toJson(doc: WorkflowDoc): string {
  return `${JSON.stringify(doc, null, 2)}\n`;
}

export type ParsedDoc = { ok: true; doc: WorkflowDoc } | { ok: false; error: string };

/**
 * Read YAML or JSON text into a document the canvas can draw. Only the shape the canvas needs
 * is checked here (a mapping with `kind: Workflow` and a list of nodes with ids and `use`);
 * everything else is the server's static validation.
 */
export function parseDocText(text: string): ParsedDoc {
  let value: unknown;
  try {
    value = parse(text, { uniqueKeys: true, maxAliasCount: 0 });
  } catch (e) {
    return { ok: false, error: e instanceof Error ? e.message.split("\n")[0]! : String(e) };
  }
  return asWorkflowDoc(value);
}

export function asWorkflowDoc(value: unknown): ParsedDoc {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ok: false, error: "The document must be a mapping (kind: Workflow)." };
  const v = value as Record<string, unknown>;
  if (v.kind !== "Workflow") return { ok: false, error: "kind must be Workflow." };
  if (!v.metadata || typeof v.metadata !== "object") return { ok: false, error: "metadata (name, project) is missing." };
  if (!Array.isArray(v.nodes)) return { ok: false, error: "nodes must be a list." };
  const seen = new Set<string>();
  for (const [i, n] of v.nodes.entries()) {
    if (!n || typeof n !== "object" || Array.isArray(n)) return { ok: false, error: `nodes[${i}] must be a mapping.` };
    const node = n as Record<string, unknown>;
    if (typeof node.id !== "string" || !node.id) return { ok: false, error: `nodes[${i}] needs an id.` };
    if (typeof node.use !== "string" || !node.use) return { ok: false, error: `node ${node.id} needs use.` };
    if (seen.has(node.id)) return { ok: false, error: `Two nodes are called ${node.id}.` };
    seen.add(node.id);
    if (node.inputs !== undefined && (typeof node.inputs !== "object" || node.inputs === null || Array.isArray(node.inputs))) {
      return { ok: false, error: `node ${node.id}: inputs must be a mapping.` };
    }
    if (node.with !== undefined && (typeof node.with !== "object" || node.with === null || Array.isArray(node.with))) {
      return { ok: false, error: `node ${node.id}: with must be a mapping.` };
    }
  }
  return { ok: true, doc: value as WorkflowDoc };
}

/** A new, empty workflow for a project. */
export function emptyWorkflow(name: string, project: string): WorkflowDoc {
  return {
    apiVersion: "mapi/v1",
    kind: "Workflow",
    metadata: { name, project },
    inputs: { data: { type: "dataset" } },
    nodes: [],
  };
}
