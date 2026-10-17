/**
 * A starter spec for a model picked from "Help me choose" or the catalog: the model with its
 * major version, placeholders for the data and its About (what it needs) as comments, so live
 * validation can guide the rest.
 */
import { STARTER_SPEC } from "@/features/editor/starter";

import type { About } from "./types";

/** One line of catalog text, safe inside a YAML comment. */
function commentLine(text: string): string {
  return text.replace(/[\r\n\u2028\u2029]+/g, " ").replace(/[\u0000-\u001f\u007f]/g, "").slice(0, 200);
}

/** `1.4.2` → `^1` (any 1.x). */
export function majorRange(version: string | null | undefined): string {
  const major = /^(\d+)\./.exec(version ?? "")?.[1];
  return `^${major ?? "1"}`;
}

export function specStarterFor(modelId: string, opts: { version?: string | null; about?: About | null } = {}): string {
  if (modelId === "mmm.bayesian") return STARTER_SPEC;
  const short = modelId.replace(/^[a-z]+\./, "").replace(/_/g, "-").slice(0, 40);
  const about = opts.about;
  const notes = about
    ? [
        `# ${commentLine(about.title)}: ${commentLine(about.summary_plain)}`,
        ...about.data_needs.slice(0, 4).map((n) => `#   needs: ${commentLine(n)}`),
      ]
    : [];
  return [
    "apiVersion: mapi/v1",
    "kind: ModelSpec",
    "metadata:",
    `  name: new-${short}`,
    "  project: default",
    "data:",
    "  # Set your dataset here. Map its columns to the roles of the model.",
    "  dataset: dataset://default/my-dataset@latest",
    "  bind:",
    "    dims: {time: date}",
    "    vars: {y: my_target_column}",
    "  split: {kind: time_holdout, dim: time, last: 8}",
    "model:",
    ...notes.map((n) => `  ${n}`),
    `  use: ${modelId}@${majorRange(opts.version)}`,
    "  settings: {}",
    "",
  ].join("\n");
}
