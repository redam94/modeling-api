/** Plain-language labels of About-card fields (difficulty, runtime, kinds). */
import type { AboutKind, Difficulty, Recommendation, TypicalRuntime } from "./types";

export const DIFFICULTY_META: Record<Difficulty, { label: string; description: string; variant: "success" | "info" | "warning" }> = {
  beginner: { label: "Beginner friendly", description: "You do not need knowledge of statistics.", variant: "success" },
  intermediate: { label: "Some experience", description: "It is easier if you know the basics of statistics.", variant: "info" },
  expert: { label: "For experts", description: "You need good knowledge of statistics to set it up and to read the results.", variant: "warning" },
};

export const RUNTIME_META: Record<TypicalRuntime, { label: string; description: string }> = {
  instant: { label: "Instant", description: "Less than one second." },
  seconds: { label: "Seconds", description: "From some seconds to one minute." },
  minutes: { label: "Minutes", description: "From one to fifteen minutes." },
  long: { label: "Longer", description: "More than fifteen minutes on usual data." },
};

export const KIND_META: Record<AboutKind, { label: string; plural: string }> = {
  model: { label: "Model", plural: "Models" },
  step: { label: "Preparation step", plural: "Preparation steps" },
  split: { label: "Train/test split", plural: "Train/test splits" },
  template: { label: "Guided analysis", plural: "Guided analyses" },
  workflow_template: { label: "Workflow template", plural: "Workflow templates" },
  node_kind: { label: "Workflow node kind", plural: "Workflow node kinds" },
  node: { label: "Workflow node", plural: "Workflow nodes" },
  check: { label: "Governance check", plural: "Governance checks" },
  dataset: { label: "Dataset", plural: "Datasets" },
  spec: { label: "Spec", plural: "Specs" },
};

/** The order the catalog browser lists kinds in (things people start from first). */
export const KIND_ORDER: readonly AboutKind[] = ["template", "model", "workflow_template", "step", "split", "check", "node", "node_kind"];

/**
 * The catalog id of a reference with a version range or pin (`mmm.bayesian@^1`,
 * `core.resample@1.0.0`), as About cards are keyed.
 */
export function bareId(ref: string): string {
  const at = ref.indexOf("@");
  return (at >= 0 ? ref.slice(0, at) : ref).trim();
}

/** `kind:id`, the key of a card across kinds (a step and a node may share an id). */
export function aboutKey(kind: AboutKind, id: string): string {
  return `${kind}:${id}`;
}

/** Parse `kind:id` (the `about` search parameter of the catalog). */
export function parseAboutKey(key: string | null | undefined): { kind: AboutKind; id: string } | null {
  if (!key) return null;
  const i = key.indexOf(":");
  if (i <= 0) return null;
  const kind = key.slice(0, i) as AboutKind;
  const id = key.slice(i + 1);
  return kind in KIND_META && /^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$/.test(id) ? { kind, id } : null;
}

/** Whether a recommendation may be called the best match: it fits more answers than it misses
 * (a 0% match is never the "best match", even when it is the first item). */
export function isGoodMatch(rec: Pick<Recommendation, "score" | "reasons_plain">): boolean {
  return rec.score > 0.5 && (rec.reasons_plain?.length ?? 0) > 0;
}
