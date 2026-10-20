/**
 * Editor hints: the server's ModelSpec JSON Schema, enriched in the browser with what this
 * tenant actually has (catalog models, steps and plots; datasets and their columns; the
 * names each pipeline stage sees), so monaco-yaml can complete and describe real values.
 *
 * Values are offered as `defaultSnippets` (label + description), never as `enum`: the
 * schema stays exactly as strict as the server's, and a value we do not know is still valid.
 * Each step's `with` and the model's `settings` get that plugin's own settings schema.
 */
import type { CatalogResponse, DataSchema, Dataset, ModelCatalogEntry, StageSchema, StepCatalogEntry } from "@/api/types";
import { flatSchema, nodeKey, nodesWithVars, ROOT, type NodeSelection } from "@/features/datasets/tree";

type ModelManifest = ModelCatalogEntry["manifest"];

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

export interface Snippet {
  label: string;
  body: string;
  description?: string;
  markdownDescription?: string;
}

/** What the spec being edited references (best effort, from the parsed YAML). */
export interface SpecShape {
  modelUse: string | null;
  steps: { id: string; use: string }[];
  datasetRef: string | null;
  bind: { dims: Record<string, string>; vars: Record<string, string> };
  /** M4: `data.nodes` (tree datasets): key → selected node. */
  nodes: Record<string, NodeSelection>;
}

export interface HintSources {
  catalog: CatalogResponse | null;
  datasets: Dataset[];
  /** Data schema of the dataset version `data.dataset` points at (dataset names, unbound). */
  columns: DataSchema | null;
  /** Per-stage schemas from the last validation (`raw`, then one per step id). */
  stages: StageSchema[];
  /** M4: the node schemas of that version when it is a tree (`tree_schema.nodes`). */
  tree?: Record<string, DataSchema> | null;
}

// ----------------------------------------------------------------------------- parsing

/** `t/acme/core.resample@^1` → `t/acme/core.resample`. */
export function pluginId(use: string): string {
  const at = use.indexOf("@");
  return at < 0 ? use : use.slice(0, at);
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function strMap(v: unknown): Record<string, string> {
  if (!isObj(v)) return {};
  return Object.fromEntries(Object.entries(v).filter((e): e is [string, string] => typeof e[1] === "string"));
}

export function specShape(document: unknown): SpecShape {
  const doc = isObj(document) ? document : {};
  const data = isObj(doc.data) ? doc.data : {};
  const bind = isObj(data.bind) ? data.bind : {};
  const model = isObj(doc.model) ? doc.model : {};
  const pipeline = Array.isArray(doc.pipeline) ? doc.pipeline : [];
  return {
    modelUse: str(model.use),
    steps: pipeline.map((s, i) => {
      const e = isObj(s) ? s : {};
      return { id: str(e.id) ?? `step_${i + 1}`, use: str(e.use) ?? "" };
    }),
    datasetRef: str(data.dataset),
    bind: { dims: strMap(bind.dims), vars: strMap(bind.vars) },
    nodes: nodeSelections(data.nodes),
  };
}

function nodeSelections(v: unknown): Record<string, NodeSelection> {
  if (!isObj(v)) return {};
  const out: Record<string, NodeSelection> = {};
  for (const [key, sel] of Object.entries(v)) {
    if (!isObj(sel) || typeof sel.path !== "string") continue;
    const vars = Array.isArray(sel.vars) ? sel.vars.filter((x): x is string => typeof x === "string") : null;
    out[key] = { path: sel.path, vars, prefix: str(sel.prefix) };
  }
  return out;
}

/**
 * The names the spec reads from its dataset: a plain version's schema, or (M4) for a tree
 * version the merge of the nodes in `data.nodes` (the root node when there is none).
 */
export function inputColumns(shape: SpecShape, sources: Pick<HintSources, "columns" | "tree">): DataSchema | null {
  if (!sources.tree) return sources.columns;
  if (Object.keys(shape.nodes).length) return flatSchema(sources.tree, shape.nodes);
  return sources.tree[ROOT] ?? sources.columns;
}

/** `dataset://p/name@v3` → `{ref: "dataset://p/name", version: 3}`; `@latest`/none → `version: null`. */
export function parseDatasetRef(ref: string): { ref: string; version: number | null } {
  const m = /^(dataset:\/\/[^@]+)(?:@(.*))?$/.exec(ref.trim());
  if (!m) return { ref, version: null };
  const v = m[2] ? /^v?(\d+)$/.exec(m[2]) : null;
  return { ref: m[1]!, version: v ? Number(v[1]) : null };
}

/** Newest non-yanked catalog step for a `use` (version ranges are left to the server). */
export function findStep(catalog: CatalogResponse | null, use: string): StepCatalogEntry | null {
  const id = pluginId(use);
  const list = (catalog?.steps ?? []).filter((s) => s.id === id && !s.yanked);
  return list.sort((a, b) => compareVersions(b.version, a.version))[0] ?? null;
}

export function findModel(catalog: CatalogResponse | null, use: string): ModelManifest | null {
  const id = pluginId(use);
  const list = (catalog?.models ?? []).filter((m) => m.manifest.id === id && !m.yanked).map((m) => m.manifest);
  return list.sort((a, b) => compareVersions(b.version, a.version))[0] ?? null;
}

export function compareVersions(a: string, b: string): number {
  const pa = a.split(/[.+-]/).map((x) => Number(x) || 0);
  const pb = b.split(/[.+-]/).map((x) => Number(x) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d) return d;
  }
  return 0;
}

/** `id@^major` — what a spec should pin to for a catalog entry. */
export function pinnedUse(id: string, version: string): string {
  return `${id}@^${version.split(".")[0] ?? version}`;
}

// ----------------------------------------------------------------------------- names

export interface NameHint {
  name: string;
  detail: string;
  description?: string | undefined;
}

export interface Names {
  dims: NameHint[];
  vars: NameHint[];
}

export function namesOf(schema: DataSchema | null | undefined): Names {
  return {
    dims: Object.entries(schema?.dims ?? {}).map(([name, d]) => ({
      name,
      detail: [d.kind, d.freq].filter(Boolean).join(" · "),
      description: d.description ?? undefined,
    })),
    vars: Object.entries(schema?.vars ?? {}).map(([name, v]) => ({
      name,
      detail: `${v.dtype}${v.dims.length ? ` [${v.dims.join(", ")}]` : ""}${v.role ? ` · ${v.role}` : ""}`,
      description: v.description ?? undefined,
    })),
  };
}

/** Dataset names under the spec's binding (canonical name for bound ones). */
export function boundNames(columns: DataSchema | null, bind: SpecShape["bind"]): Names {
  const raw = namesOf(columns);
  const rev = (m: Record<string, string>) => new Map(Object.entries(m).map(([canon, ds]) => [ds, canon]));
  const dims = rev(bind.dims);
  const vars = rev(bind.vars);
  return {
    dims: raw.dims.map((h) => ({ ...h, name: dims.get(h.name) ?? h.name })),
    vars: raw.vars.map((h) => ({ ...h, name: vars.get(h.name) ?? h.name })),
  };
}

/** The names each step reads: the stage before it (`raw` for the first step). */
export function namesBeforeStep(index: number, shape: SpecShape, sources: HintSources): Names {
  const stages = new Map(sources.stages.map((s) => [s.name, s]));
  for (let i = index - 1; i >= -1; i--) {
    const name = i < 0 ? "raw" : shape.steps[i]?.id;
    const stage = name ? stages.get(name) : undefined;
    if (stage?.data_schema) return namesOf(stage.data_schema);
  }
  return boundNames(inputColumns(shape, sources), shape.bind);
}

/** The names the model sees: the last stage. */
export function namesForModel(shape: SpecShape, sources: HintSources): Names {
  return namesBeforeStep(shape.steps.length, shape, sources);
}

// ----------------------------------------------------------------------------- snippets

function md(title: string, ...lines: (string | null | undefined)[]): string {
  return [`**${title}**`, ...lines.filter(Boolean)].join("\n\n");
}

export function stepSnippets(catalog: CatalogResponse | null): Snippet[] {
  return latestSteps(catalog).map((s) => {
    const use = pinnedUse(s.id, s.version);
    return {
      label: use,
      body: use,
      description: s.summary ?? s.about?.title ?? s.kind,
      markdownDescription: md(s.about?.title ?? s.id, s.summary_plain ?? s.summary, `\`${s.kind}\` step · v${s.version}`),
    };
  });
}

export function modelSnippets(catalog: CatalogResponse | null): Snippet[] {
  return latestModels(catalog).map((m) => {
    const use = pinnedUse(m.id, m.version);
    return {
      label: use,
      body: use,
      description: m.about?.title ?? m.description ?? m.family ?? "",
      markdownDescription: md(m.about?.title ?? m.id, m.about?.summary_plain ?? m.description, m.family ? `${m.family} · v${m.version}` : `v${m.version}`),
    };
  });
}

export function plotSnippets(catalog: CatalogResponse | null, model: ModelManifest | null): Snippet[] {
  const seen = new Set<string>();
  const out: Snippet[] = [];
  for (const id of model?.plots ?? []) {
    seen.add(id);
    out.push({ label: id, body: id, description: `${model!.id} recommends this plot` });
  }
  for (const p of catalog?.plots ?? []) {
    if (p.yanked || seen.has(p.id)) continue;
    seen.add(p.id);
    out.push({ label: p.id, body: p.id, description: p.title, markdownDescription: md(p.title, p.about?.summary_plain) });
  }
  return out;
}

export function datasetSnippets(datasets: Dataset[]): Snippet[] {
  return datasets.map((d) => ({
    label: `${d.ref}@latest`,
    body: `${d.ref}@latest`,
    description: d.description ?? d.name,
    markdownDescription: md(d.name, d.description ?? d.about?.summary_generated, d.latest_version ? `Latest version: v${d.latest_version}` : "This dataset has no versions."),
  }));
}

function nameSnippets(names: NameHint[]): Snippet[] {
  return names.map((n) => ({
    label: n.name,
    body: n.name,
    description: n.detail,
    ...(n.description ? { markdownDescription: md(n.name, n.description, n.detail) } : {}),
  }));
}

export function latestSteps(catalog: CatalogResponse | null): StepCatalogEntry[] {
  const by = new Map<string, StepCatalogEntry>();
  for (const s of catalog?.steps ?? []) {
    if (s.yanked) continue;
    const cur = by.get(s.id);
    if (!cur || compareVersions(s.version, cur.version) > 0) by.set(s.id, s);
  }
  return [...by.values()].sort((a, b) => a.id.localeCompare(b.id));
}

export function latestModels(catalog: CatalogResponse | null): ModelManifest[] {
  const by = new Map<string, ModelManifest>();
  for (const e of catalog?.models ?? []) {
    if (e.yanked) continue;
    const cur = by.get(e.manifest.id);
    if (!cur || compareVersions(e.manifest.version, cur.version) > 0) by.set(e.manifest.id, e.manifest);
  }
  return [...by.values()].sort((a, b) => a.id.localeCompare(b.id));
}

/** Canonical dim/var names a model's data template names literally (not `{{param}}` ones). */
export function templateNames(model: ModelManifest | null): { dims: string[]; vars: string[] } {
  const t = model?.schema_template?.template;
  const keys = (v: unknown) => (isObj(v) ? Object.keys(v).filter((k) => !k.includes("{{")) : []);
  return { dims: keys(isObj(t) ? t.dims : null), vars: keys(isObj(t) ? t.vars : null) };
}

// ----------------------------------------------------------------------------- schema surgery

/** Add value snippets to a property schema: to strings, array items and union members. */
function addSnippets(schema: unknown, snippets: Snippet[]): void {
  if (!isObj(schema) || snippets.length === 0) return;
  for (const key of ["anyOf", "oneOf", "allOf"] as const) {
    const list = schema[key];
    if (Array.isArray(list)) list.forEach((s) => addSnippets(s, snippets));
  }
  if (schema.type === "array" || isObj(schema.items)) {
    addSnippets(schema.items, snippets);
    return;
  }
  if (schema.type === "string" || (schema.type === undefined && !schema.anyOf && !schema.oneOf && !schema.$ref)) {
    // Replace the generic examples (`core.resample@^1`) with real values.
    delete schema.examples;
    schema.defaultSnippets = snippets;
  }
}

const safe = (s: string) => s.replace(/[^A-Za-z0-9_]/g, "_");

/**
 * Embed a plugin's settings schema into `defs`, rewriting its local `$ref`s, and return a
 * `$ref` to it. Plugin schemas are self-contained documents with their own `$defs`.
 */
function embed(defs: Json, name: string, schema: Json): Json {
  const key = `x-${safe(name)}`;
  const inner = isObj(schema.$defs) ? schema.$defs : {};
  const rewrite = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(rewrite);
    if (!isObj(v)) return v;
    const out: Json = {};
    for (const [k, val] of Object.entries(v)) {
      if (k === "$defs" || k === "$schema" || k === "$id") continue;
      if (k === "$ref" && typeof val === "string") {
        out[k] = val === "#" ? `#/$defs/${key}` : val.startsWith("#/$defs/") ? `#/$defs/${key}.${safe(val.slice(8))}` : val;
      } else {
        out[k] = rewrite(val);
      }
    }
    return out;
  };
  for (const [n, d] of Object.entries(inner)) defs[`${key}.${safe(n)}`] = rewrite(d);
  defs[key] = rewrite(schema);
  return { $ref: `#/$defs/${key}` };
}

/** Name hints on a step's dim/var parameters, on a copy of its settings schema. */
function withSchemaFor(step: StepCatalogEntry, names: Names): Json | null {
  if (!isObj(step.settings_schema)) return null;
  const schema = JSON.parse(JSON.stringify(step.settings_schema)) as Json;
  const props = isObj(schema.properties) ? schema.properties : {};
  const varParams = new Set([...(step.var_params ?? []), ...(step.target_vars_param ? [step.target_vars_param] : [])]);
  const dimParams = new Set(step.dim_params ?? []);
  for (const [p, s] of Object.entries(props)) {
    if (varParams.has(p)) addSnippets(s, nameSnippets(names.vars));
    else if (dimParams.has(p)) addSnippets(s, nameSnippets(names.dims));
  }
  return schema;
}

function prop(schema: unknown, ...path: string[]): Json | null {
  let cur: unknown = schema;
  for (const p of path) cur = isObj(cur) && isObj(cur.properties) ? cur.properties[p] : undefined;
  return isObj(cur) ? cur : null;
}

/**
 * The ModelSpec schema with this tenant's values as completions. Pure; `base` is not mutated.
 */
export function enrichSpecSchema(base: unknown, shape: SpecShape, sources: HintSources): unknown {
  if (!isObj(base)) return base;
  // A JSON round trip, not structuredClone: shared sub-schemas must become separate copies.
  const schema = JSON.parse(JSON.stringify(base)) as Json;
  const defs = isObj(schema.$defs) ? schema.$defs : (schema.$defs = {});
  const { catalog } = sources;
  const model = shape.modelUse ? findModel(catalog, shape.modelUse) : null;

  // data.dataset and data.bind
  const data = defs.DataBlock;
  addSnippets(prop(data, "dataset"), datasetSnippets(sources.datasets));
  if (sources.tree) addNodeHints(defs, data, sources.tree);
  const columns = namesOf(inputColumns(shape, sources));
  const binding = defs.Binding;
  const wanted = templateNames(model);
  for (const [kind, names, canon] of [
    ["dims", columns.dims, wanted.dims],
    ["vars", columns.vars, wanted.vars],
  ] as const) {
    const block = prop(binding, kind);
    if (!block) continue;
    const values = nameSnippets(names);
    if (isObj(block.patternProperties)) Object.values(block.patternProperties).forEach((s) => addSnippets(s, values));
    if (canon.length) {
      // Suggest the canonical names the model expects as keys.
      block.properties = Object.fromEntries(
        canon.map((c) => [c, { type: "string", description: `The dataset name for the canonical ${kind === "dims" ? "dim" : "variable"} \`${c}\`. The model ${model!.id} needs this name.`, defaultSnippets: values }]),
      );
    }
  }

  // split dims (raw stage names); v0.14: the `column` and `stratified` splits also name a
  // variable (or coordinate) along their dim. Sweep documents are retired (v0.14): no hints.
  const raw = namesBeforeStep(0, shape, sources);
  const rawDims = nameSnippets(raw.dims);
  const rawColumns = nameSnippets([...raw.vars, ...raw.dims]);
  for (const def of ["TimeHoldoutSplit", "LastPerGroupSplit", "AsOfSplit", "LeaveGroupOutSplit", "MaskBlockSplit", "ColumnSplit", "StratifiedSplit"]) {
    for (const p of ["dim", "dims", "within"]) addSnippets(prop(defs[def], p), rawDims);
    for (const p of ["column", "by"]) addSnippets(prop(defs[def], p), rawColumns);
    addSnippets(prop(defs[def], "after_step"), shape.steps.map((s) => ({ label: s.id, body: s.id, description: s.use })));
  }

  // pipeline[].use, and each known step's `with`
  const stepCall = defs.StepCall;
  addSnippets(prop(stepCall, "use"), stepSnippets(catalog));
  const pipeline = prop(schema, "pipeline");
  if (pipeline && shape.steps.length) {
    pipeline.prefixItems = shape.steps.map((s, i) => {
      const entry = s.use ? findStep(catalog, s.use) : null;
      const withSchema = entry ? withSchemaFor(entry, namesBeforeStep(i, shape, sources)) : null;
      if (!entry || !withSchema) return { $ref: "#/$defs/StepCall" };
      return {
        allOf: [{ $ref: "#/$defs/StepCall" }],
        properties: { with: { ...embed(defs, `step.${i}.${entry.id}`, withSchema), description: entry.summary_plain ?? entry.summary ?? undefined } },
      };
    });
  }

  // model.use, model.settings, outputs
  const modelBlock = defs.ModelBlock;
  addSnippets(prop(modelBlock, "use"), modelSnippets(catalog));
  if (model && isObj(model.settings_schema) && isObj(modelBlock) && isObj(modelBlock.properties)) {
    modelBlock.properties.settings = { ...embed(defs, `model.${model.id}`, model.settings_schema), description: `The settings for ${model.id}` };
  }
  const outputs = defs.OutputsBlock;
  addSnippets(prop(outputs, "plots"), plotSnippets(catalog, model));
  if (model?.derived?.length) {
    addSnippets(
      prop(outputs, "derived"),
      model.derived.map((d) => ({ label: d.name, body: d.name, description: d.description ?? `[${d.dims.join(", ")}]` })),
    );
  }
  return schema;
}

/** Node paths of a tree dataset as snippets (the nodes that have variables). */
export function nodeSnippets(tree: Record<string, DataSchema>): Snippet[] {
  return nodesWithVars(tree).map((p) => {
    const vars = Object.keys(tree[p]?.vars ?? {});
    return { label: p, body: p, description: `${vars.length} ${vars.length === 1 ? "variable" : "variables"}: ${vars.slice(0, 6).join(", ")}${vars.length > 6 ? "…" : ""}` };
  });
}

/**
 * M4: `data.nodes` hints for a tree dataset: a suggested key per node (with its `path`
 * filled in), node paths for `path`, and each node's variables for `vars`.
 */
function addNodeHints(defs: Json, data: unknown, tree: Record<string, DataSchema>): void {
  const paths = nodeSnippets(tree);
  if (!paths.length) return;
  addSnippets(prop(defs.NodeSelect, "path"), paths);
  const allVars: Snippet[] = [];
  for (const p of nodesWithVars(tree)) {
    for (const [name, v] of Object.entries(tree[p]?.vars ?? {})) allVars.push({ label: name, body: name, description: `${p} · ${v.dtype}${v.dims.length ? ` [${v.dims.join(", ")}]` : ""}` });
  }
  addSnippets(prop(defs.NodeSelect, "vars"), allVars);
  const nodes = prop(data, "nodes");
  const block = (Array.isArray(nodes?.anyOf) ? nodes.anyOf : [nodes]).find((s): s is Json => isObj(s) && s.type === "object");
  if (!nodes || !block) return;
  const taken: string[] = [];
  const keyed = nodesWithVars(tree).map((p) => {
    const key = nodeKey(p, taken);
    taken.push(key);
    return [key, p] as const;
  });
  block.properties = Object.fromEntries(
    keyed.map(([key, p]) => [
      key,
      {
        allOf: [{ $ref: "#/$defs/NodeSelect" }],
        description: `The node \`${p}\` of the dataset (${Object.keys(tree[p]?.vars ?? {}).length} variables)`,
        defaultSnippets: [{ label: `path: ${p}`, body: { path: p } }],
        properties: {
          path: { type: "string", defaultSnippets: [{ label: p, body: p }] },
          vars: { type: "array", items: { type: "string", defaultSnippets: nameSnippets(namesOf(tree[p]).vars) } },
        },
      },
    ]),
  );
  nodes.defaultSnippets = keyed.map(([key, p]) => ({ label: `${key}: ${p}`, description: `Read the node ${p}`, body: { [key]: { path: p } } }));
}

/** A cheap key of everything `enrichSpecSchema` depends on in the spec, to re-bind only on change. */
export function hintKey(shape: SpecShape, sources: HintSources): string {
  return JSON.stringify([
    shape.modelUse && pluginId(shape.modelUse),
    shape.steps.map((s) => [s.id, pluginId(s.use)]),
    shape.datasetRef,
    shape.bind,
    shape.nodes,
    sources.tree ? Object.keys(sources.tree) : null,
    sources.catalog?.digest ?? null,
    sources.datasets.map((d) => d.ref),
    sources.columns ? Object.keys(sources.columns.dims ?? {}).concat(Object.keys(sources.columns.vars ?? {})) : null,
    sources.stages.map((s) => [s.name, Object.keys(s.data_schema?.dims ?? {}), Object.keys(s.data_schema?.vars ?? {})]),
  ]);
}
