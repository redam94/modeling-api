/**
 * "Start with this": where a recommended or browsed item leads.
 *
 * - a guided template opens a new Guided analysis;
 * - a model opens a new spec in the editor with that model. A Guided user instead starts the
 *   Guided template built on that model when there is one (`tabular.regressor` → `store_demand`);
 *   otherwise they are told the model is set up in Expert mode and can switch deliberately
 *   (never, in a project locked to Guided mode);
 * - a workflow template runs as a recipe on the user's data in Guided mode, or opens the
 *   "use this template" wizard in Expert mode;
 * - a governance check opens the governance check catalog;
 * - other building blocks (steps, splits, nodes) open their About card in the catalog.
 */
import type { Persona } from "@/stores/ui";

import { aboutKey } from "./labels";
import type { AboutKind } from "./types";

export type StartTarget =
  | { kind: "guided"; to: "/guided/new/$templateId"; params: { templateId: string }; label: string }
  | { kind: "spec"; to: "/build/new"; search: { model: string }; label: string }
  /** A model with no Guided path, for a Guided user: switching to Expert mode is explicit. */
  | { kind: "expert"; to: "/build/new"; search: { model: string }; label: string; hint: string; locked: boolean }
  | { kind: "workflow"; to: "/workflows/recipe" | "/workflows/new"; search: { template: string }; label: string }
  | { kind: "governance"; to: "/governance"; search: { tab: "checks" }; label: string }
  | { kind: "catalog"; to: "/catalog"; search: { about: string }; label: string };

/** The gallery id of a workflow template (About ids are `workflow.<template id>`). */
export function workflowTemplateId(aboutId: string): string {
  return aboutId.replace(/^workflow\./, "");
}

/** The plugin id of a `UseRef` (`tabular.regressor@^1` → `tabular.regressor`). */
function pluginId(useRef: string): string {
  return useRef.split("@", 1)[0] ?? useRef;
}

/** The first Guided template built on model `modelId`, or `null`. */
export function guidedTemplateForModel(templates: readonly { id: string; plugin: string }[], modelId: string): string | null {
  return templates.find((t) => pluginId(t.plugin) === modelId)?.id ?? null;
}

export const EXPERT_HINT = "Expert mode shows every setting of the model as text (YAML) for you to fill in.";

export interface StartOptions {
  /** For a model in Guided mode: the Guided template that uses it, `null` when none does. */
  templateForModel?: string | null;
  /** The project is locked to Guided mode for this user. */
  locked?: boolean;
}

/** The target for `kind:id`; Guided users run workflow templates as recipes. */
export function startTarget(kind: AboutKind, id: string, persona: Persona = "expert", opts: StartOptions = {}): StartTarget {
  switch (kind) {
    case "template":
      return { kind: "guided", to: "/guided/new/$templateId", params: { templateId: id }, label: "Start a guided analysis" };
    case "model":
      if (persona !== "guided") return { kind: "spec", to: "/build/new", search: { model: id }, label: "Start a spec with this model" };
      if (opts.templateForModel) {
        return { kind: "guided", to: "/guided/new/$templateId", params: { templateId: opts.templateForModel }, label: "Start a guided analysis" };
      }
      return opts.locked
        ? { kind: "expert", to: "/build/new", search: { model: id }, label: "Needs Expert mode", hint: "This model has no Guided setup yet, and this project is locked to Guided mode. A project admin can set it up.", locked: true }
        : { kind: "expert", to: "/build/new", search: { model: id }, label: "Set up in Expert mode", hint: `This model has no Guided setup yet. ${EXPERT_HINT}`, locked: false };
    case "workflow_template":
      return persona === "guided"
        ? { kind: "workflow", to: "/workflows/recipe", search: { template: workflowTemplateId(id) }, label: "Run this recipe on my data" }
        : { kind: "workflow", to: "/workflows/new", search: { template: workflowTemplateId(id) }, label: "Start a workflow from this template" };
    case "check":
      return { kind: "governance", to: "/governance", search: { tab: "checks" }, label: "See the governance checks" };
    default:
      return { kind: "catalog", to: "/catalog", search: { about: aboutKey(kind, id) }, label: "Open in the catalog" };
  }
}
