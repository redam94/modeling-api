/**
 * Tree datasets (M4): node paths and the tree schema of a dataset version, mirroring
 * `mapi_spec.tree` (the same `NodePath` pattern and helpers), so the app can check a path
 * before the server does and lay out a version's nodes.
 */
import type { DataSchema, DatasetVersion } from "@/api/types";

export const MAX_TREE_DEPTH = 8;
export const MAX_TREE_NODES = 256;
export const ROOT = "/";

const SEGMENT = "[A-Za-z_][A-Za-z0-9_.\\-]{0,63}";
/** `mapi_spec.tree.NODE_PATH_RE`: `/` or `/a/b` with 1-8 segments of up to 64 characters. */
export const NODE_PATH_RE = new RegExp(`^/(?:${SEGMENT}(?:/${SEGMENT}){0,${MAX_TREE_DEPTH - 1}})?$`);
const SEGMENT_RE = new RegExp(`^${SEGMENT}$`);

export function isNodePath(path: string): boolean {
  return NODE_PATH_RE.test(path);
}

/** Why `path` is not a node path, in plain words (`null` when it is one). */
export function nodePathProblem(path: string): string | null {
  if (isNodePath(path)) return null;
  if (!path.startsWith("/")) return `The node "${path}" must start with /. Use / for the top of the tree.`;
  if (path.length > 1 && path.endsWith("/")) return `The node "${path}" must not end with /.`;
  const segments = path.slice(1).split("/");
  if (segments.length > MAX_TREE_DEPTH) return `The node "${path}" has ${segments.length} levels. The maximum is ${MAX_TREE_DEPTH}.`;
  const bad = segments.find((s) => !SEGMENT_RE.test(s));
  if (bad !== undefined) {
    return bad === ""
      ? `The node "${path}" has an empty name between two / characters.`
      : `"${bad}" is not a valid node name. Start with a letter or _. Then use letters, digits, _, . and - (a maximum of 64 characters).`;
  }
  return `"${path}" is not a valid node path.`;
}

/** The parent of `path` (`null` for the root). */
export function nodeParent(path: string): string | null {
  if (path === ROOT) return null;
  const head = path.slice(0, path.lastIndexOf("/"));
  return head || ROOT;
}

/** The last segment of `path` (`""` for the root). */
export function nodeName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function nodeDepth(path: string): number {
  return path === ROOT ? 0 : path.split("/").length - 1;
}

/** True if `ancestor` is a strict ancestor of `path`. */
export function isAncestor(ancestor: string, path: string): boolean {
  if (ancestor === path) return false;
  return ancestor === ROOT || path.startsWith(`${ancestor}/`);
}

/** A node and its descendants as a depth-first list (each node after its parent). */
export interface TreeRow {
  path: string;
  depth: number;
  schema: DataSchema;
}

/** The nodes of a tree schema, depth first (children in declaration order). */
export function treeRows(nodes: Record<string, DataSchema>): TreeRow[] {
  const paths = Object.keys(nodes);
  const out: TreeRow[] = [];
  const visit = (p: string) => {
    out.push({ path: p, depth: nodeDepth(p), schema: nodes[p]! });
    for (const c of paths.filter((x) => x !== p && nodeParent(x) === p)) visit(c);
  };
  if (nodes[ROOT]) visit(ROOT);
  // Nodes without their parent in the map (not valid, but never hide them).
  for (const p of paths) if (!out.some((r) => r.path === p)) out.push({ path: p, depth: nodeDepth(p), schema: nodes[p]! });
  return out;
}

/** `TreeSchema.default_node`: the root if it has variables, else the first node with some. */
export function defaultNode(nodes: Record<string, DataSchema>): string {
  if (Object.keys(nodes[ROOT]?.vars ?? {}).length) return ROOT;
  return Object.entries(nodes).find(([, s]) => Object.keys(s.vars ?? {}).length)?.[0] ?? ROOT;
}

/** The nodes that have variables (the ones a Data Explorer or `data.nodes` can read). */
export function nodesWithVars(nodes: Record<string, DataSchema>): string[] {
  return Object.entries(nodes)
    .filter(([, s]) => Object.keys(s.vars ?? {}).length > 0)
    .map(([p]) => p);
}

export function isTree(version: DatasetVersion | null | undefined): version is DatasetVersion & { tree_schema: { nodes: Record<string, DataSchema> } } {
  return version?.layout === "tree" && Boolean(version.tree_schema?.nodes);
}

/** A `data.nodes` key for a node: its last segment as a spec name (`root` for `/`). */
export function nodeKey(path: string, taken: Iterable<string> = []): string {
  const used = new Set(taken);
  const base = (path === ROOT ? "root" : nodeName(path)).replace(/[^A-Za-z0-9_.-]/g, "_").replace(/^([^A-Za-z_])/, "_$1") || "node";
  let key = base;
  for (let i = 2; used.has(key); i++) key = `${base}_${i}`;
  return key;
}

export interface NodeSelection {
  path: string;
  vars?: string[] | null | undefined;
  prefix?: string | null | undefined;
}

/**
 * The dims and variables a `data.nodes` selection gives the pipeline (`TreeSchema.flat`,
 * best effort: conflicts are left for the server's validation to report).
 */
export function flatSchema(nodes: Record<string, DataSchema>, selection: Record<string, NodeSelection>): DataSchema {
  const dims: NonNullable<DataSchema["dims"]> = {};
  const vars: NonNullable<DataSchema["vars"]> = {};
  for (const sel of Object.values(selection)) {
    const node = nodes[sel.path];
    if (!node) continue;
    const wanted = sel.vars?.length ? sel.vars : Object.keys(node.vars ?? {});
    for (const name of wanted) {
      const v = node.vars?.[name];
      if (!v) continue;
      vars[`${sel.prefix ?? ""}${name}`] = v;
      for (const d of v.dims ?? []) {
        // A node sees its ancestors' dims too.
        let at: string | null = sel.path;
        while (at !== null && !nodes[at]?.dims?.[d]) at = nodeParent(at);
        const spec = at !== null ? nodes[at]?.dims?.[d] : undefined;
        if (spec && !dims[d]) dims[d] = spec;
      }
    }
  }
  return { layout: "dense", dims, vars };
}
