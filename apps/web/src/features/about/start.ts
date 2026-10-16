/**
 * "Start with this": where a recommended or browsed item leads.
 *
 * - a guided template opens a new Guided analysis;
 * - a model opens a new spec in the editor with that model;
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
  | { kind: "workflow"; to: "/workflows/recipe" | "/workflows/new"; search: { template: string }; label: string }
  | { kind: "governance"; to: "/governance"; search: { tab: "checks" }; label: string }
  | { kind: "catalog"; to: "/catalog"; search: { about: string }; label: string };

/** The gallery id of a workflow template (About ids are `workflow.<template id>`). */
export function workflowTemplateId(aboutId: string): string {
  return aboutId.replace(/^workflow\./, "");
}

/** The target for `kind:id`; Guided users run workflow templates as recipes. */
export function startTarget(kind: AboutKind, id: string, persona: Persona = "expert"): StartTarget {
  switch (kind) {
    case "template":
      return { kind: "guided", to: "/guided/new/$templateId", params: { templateId: id }, label: "Start a guided analysis" };
    case "model":
      return { kind: "spec", to: "/build/new", search: { model: id }, label: "Start a spec with this model" };
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
