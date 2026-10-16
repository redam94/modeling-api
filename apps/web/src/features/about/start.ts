/**
 * "Start with this": where a recommended or browsed item leads.
 *
 * - a guided template opens a new Guided analysis;
 * - a model opens a new spec in the editor with that model;
 * - a workflow template opens the workflow editor from the template (when the app has the
 *   workflows screens), else its About card in the catalog;
 * - building blocks (steps, splits, nodes, checks) open their About card in the catalog.
 */
import { aboutKey } from "./labels";
import type { AboutKind } from "./types";

/** The workflows screens' path for a new workflow from a template (see `cross_package_changes`). */
export const WORKFLOW_FROM_TEMPLATE_PATH = "/workflows/new";

export type StartTarget =
  | { kind: "guided"; to: "/guided/new/$templateId"; params: { templateId: string }; label: string }
  | { kind: "spec"; to: "/build/new"; search: { model: string }; label: string }
  | { kind: "href"; href: string; label: string }
  | { kind: "catalog"; to: "/catalog"; search: { about: string }; label: string };

/**
 * The target for `kind:id`. `hasRoute(path)` says whether the app has a route (the workflow
 * screens are optional), so the catalog is the fallback.
 */
export function startTarget(kind: AboutKind, id: string, hasRoute: (path: string) => boolean = () => false): StartTarget {
  switch (kind) {
    case "template":
      return { kind: "guided", to: "/guided/new/$templateId", params: { templateId: id }, label: "Start a guided analysis" };
    case "model":
      return { kind: "spec", to: "/build/new", search: { model: id }, label: "Start a spec with this model" };
    case "workflow_template":
      if (hasRoute(WORKFLOW_FROM_TEMPLATE_PATH)) {
        return { kind: "href", href: `${WORKFLOW_FROM_TEMPLATE_PATH}?template=${encodeURIComponent(id)}`, label: "Start a workflow from this template" };
      }
      return { kind: "catalog", to: "/catalog", search: { about: aboutKey(kind, id) }, label: "See the workflow template" };
    default:
      return { kind: "catalog", to: "/catalog", search: { about: aboutKey(kind, id) }, label: "Open in the catalog" };
  }
}
