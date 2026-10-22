/**
 * "From your team" in Build (v0.18 D4, D9, D17): put a used library item into the spec being
 * edited. The server compiles the item (`POST /v1/library/{id}:use`); these pure functions
 * apply the result to the YAML text with the same structured edits the visual builder uses,
 * so comments survive and step ids stay unique.
 *
 * - a **prep preset** gives `pipeline` (its slots filled, ids prefixed): the steps go to the
 *   end of the pipeline, and `metadata.labels["mapi.library/prep"]` records the lineage;
 * - a **model recipe** gives a `spec_fragment` (`pipeline`, `model`, optional `outputs.plots`,
 *   `metadata.labels["mapi.library/model"]`): its steps go to the end, the model block replaces
 *   the current one and its plots are added.
 */
import { addToList, insertStep, setPath } from "@/features/editor/spec-doc";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type JsonObject = { [key: string]: Json };

function obj(v: unknown): JsonObject {
  return typeof v === "object" && v !== null && !Array.isArray(v) ? (v as JsonObject) : {};
}

/** The steps of a compiled pipeline that the builder can insert (`id` and `use` strings). */
export function stepsOf(pipeline: unknown): { id: string; use: string; with: JsonObject }[] {
  if (!Array.isArray(pipeline)) return [];
  return pipeline.map(obj).flatMap((s) => {
    const use = s.use;
    if (typeof use !== "string" || !use) return [];
    return [{ id: typeof s.id === "string" && s.id ? s.id : (use.split("@")[0] ?? use), use, with: obj(s.with) }];
  });
}

/** Append `steps` to the pipeline (ids made unique). `null` when the YAML does not parse. */
export function appendSteps(text: string, steps: readonly { id: string; use: string; with: JsonObject }[]): string | null {
  let out: string | null = text;
  for (const s of steps) {
    if (out === null) return null;
    out = insertStep(out, Number.POSITIVE_INFINITY, s.use, s.with, s.id);
  }
  return out;
}

/** Set the lineage labels (`mapi.library/*`) in `metadata.labels`. */
export function setLabels(text: string, labels: Readonly<Record<string, string>>): string | null {
  let out: string | null = text;
  for (const [k, v] of Object.entries(labels)) {
    if (out === null) return null;
    if (typeof v === "string" && v) out = setPath(out, ["metadata", "labels", k], v);
  }
  return out;
}

/** Apply a used prep preset (`pipeline` + `labels`). */
export function applyPrepUse(text: string, pipeline: unknown, labels: Readonly<Record<string, string>> = {}): string | null {
  const steps = appendSteps(text, stepsOf(pipeline));
  return steps === null ? null : setLabels(steps, labels);
}

/** Apply a used model recipe (`spec_fragment` + `labels`). */
export function applyModelUse(text: string, fragment: unknown, labels: Readonly<Record<string, string>> = {}): string | null {
  const f = obj(fragment);
  let out = appendSteps(text, stepsOf(f.pipeline));
  const model = obj(f.model);
  if (out !== null && typeof model.use === "string") {
    // The whole block: `setPath` drops the keys the recipe's block does not have, so the old
    // model's settings never mix with the recipe's (the block keeps its place in the file).
    out = setPath(out, ["model"], model);
  }
  const plots = obj(f.outputs).plots;
  if (Array.isArray(plots)) {
    for (const p of plots) {
      if (out === null) break;
      if (typeof p === "string") out = addToList(out, ["outputs", "plots"], p);
    }
  }
  const fragmentLabels = obj(obj(f.metadata).labels);
  const all: Record<string, string> = {};
  for (const [k, v] of Object.entries(fragmentLabels)) if (typeof v === "string") all[k] = v;
  return out === null ? null : setLabels(out, { ...all, ...labels });
}
