/**
 * Structured edits of ModelSpec YAML text (the `yaml` Document API): comments, key order and
 * formatting of untouched parts survive. Every function returns the new text, or `null` when
 * the text does not parse (the caller then leaves the editor alone).
 */
import { isMap, isScalar, isSeq, parseDocument, type Document, type YAMLMap, YAMLSeq } from "yaml";

import type { StepCatalogEntry } from "@/api/types";

import type { Names } from "./spec-hints";

type Json = Record<string, unknown>;
const isObj = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);

function edit(text: string, fn: (doc: Document.Parsed) => void): string | null {
  const doc = parseDocument(text, { keepSourceTokens: false });
  if (doc.errors.length) return null;
  if (doc.contents === null) doc.contents = doc.createNode({}) as never;
  if (!isMap(doc.contents)) return null;
  fn(doc);
  // No padding inside flow collections: `{a: 1}` stays as written, not `{ a: 1 }`.
  return doc.toString({ lineWidth: 0, flowCollectionPadding: false });
}

/** A flow-style node (`{a: 1}`, `[x]`), the style the starter spec uses for small blocks. */
function flow(doc: Document, value: unknown) {
  const node = doc.createNode(value);
  if (isMap(node) || isSeq(node)) node.flow = true;
  return node;
}

function seqAt(doc: Document, path: string[]): YAMLSeq {
  const cur = doc.getIn(path, true);
  if (isSeq(cur)) return cur;
  const seq = new YAMLSeq();
  doc.setIn(path, seq);
  return seq;
}

export function stepIds(text: string): string[] {
  const doc = parseDocument(text);
  const p = doc.getIn(["pipeline"], true);
  return isSeq(p) ? p.items.map((s) => (isMap(s) ? s.get("id") : null)).filter((id): id is string => typeof id === "string" && id !== "") : [];
}

/** `core.mean_scale` → `mean_scale`, made unique among `taken`. */
export function newStepId(stepId: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = (stepId.split(".").at(-1) ?? "step").toLowerCase().replace(/[^a-z0-9_]/g, "_").replace(/^[^a-z_]/, "s_").slice(0, 56) || "step";
  if (!used.has(base)) return base;
  for (let i = 2; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`;
}

function pickName(list: Names["dims"], prefer?: (detail: string) => boolean) {
  return (prefer && list.find((n) => prefer(n.detail))?.name) ?? list[0]?.name;
}

/**
 * A starting `with` for a step: every required setting without a default, filled with a
 * plausible value (an enum's first member, a dim or variable name the step can see).
 */
export function starterWith(step: StepCatalogEntry, names: Names): Json {
  const schema = isObj(step.settings_schema) ? step.settings_schema : {};
  const props = isObj(schema.properties) ? schema.properties : {};
  const required = Array.isArray(schema.required) ? (schema.required as string[]) : [];
  const vars = new Set([...(step.var_params ?? []), ...(step.target_vars_param ? [step.target_vars_param] : [])]);
  const dims = new Set(step.dim_params ?? []);
  const out: Json = {};
  for (const p of required) {
    const s = isObj(props[p]) ? props[p] : {};
    if ("default" in s) continue;
    const isArray = s.type === "array";
    let v: unknown;
    if (dims.has(p)) v = pickName(names.dims, (d) => d.startsWith("time"));
    else if (vars.has(p)) v = names.vars[0]?.name;
    else if (Array.isArray(s.enum)) v = s.enum[0];
    else if (s.type === "integer" || s.type === "number") v = typeof s.minimum === "number" ? s.minimum : 1;
    else if (s.type === "boolean") v = false;
    else if (isArray) v = [];
    else v = "";
    out[p] = isArray && v !== undefined && !Array.isArray(v) ? [v] : (v ?? (isArray ? [] : ""));
  }
  return out;
}

/** Append a pipeline step (flow style) with an id derived from `stepId`, unique in the pipeline. */
export function addStep(text: string, use: string, withValue: Json, stepId: string): string | null {
  return edit(text, (doc) => {
    const step: Json = { id: newStepId(stepId, stepIds(text)), use };
    if (Object.keys(withValue).length) step.with = withValue;
    seqAt(doc, ["pipeline"]).items.push(flow(doc, step));
  });
}

/** Set `model.use`. A different model's settings are dropped (they belong to the old model). */
export function setModel(text: string, use: string, settings?: Json): string | null {
  return edit(text, (doc) => {
    const prev = doc.getIn(["model", "use"]);
    const sameModel = typeof prev === "string" && prev.split("@")[0] === use.split("@")[0];
    doc.setIn(["model", "use"], use);
    if (!sameModel) {
      if (settings && Object.keys(settings).length) doc.setIn(["model", "settings"], doc.createNode(settings));
      else doc.deleteIn(["model", "settings"]);
    }
  });
}

export function setDataset(text: string, ref: string): string | null {
  return edit(text, (doc) => doc.setIn(["data", "dataset"], ref));
}

/** Add a value to a string list (`outputs.plots`, `outputs.derived`) unless it is there. */
export function addToList(text: string, path: string[], value: string): string | null {
  return edit(text, (doc) => {
    const existing = doc.getIn(path, true);
    if (isSeq(existing) && existing.items.some((i) => isScalar(i) && i.value === value)) return;
    if (isSeq(existing)) {
      existing.items.push(doc.createNode(value));
      return;
    }
    doc.setIn(path, flow(doc, [value]));
  });
}

/** Bind a canonical name to a dataset name (`data.bind.dims.geo: market`). */
export function setBinding(text: string, kind: "dims" | "vars", canonical: string, column: string): string | null {
  return edit(text, (doc) => {
    const block = doc.getIn(["data", "bind", kind], true) as YAMLMap | undefined;
    if (isMap(block)) block.set(canonical, column);
    else doc.setIn(["data", "bind", kind], flow(doc, { [canonical]: column }));
  });
}
