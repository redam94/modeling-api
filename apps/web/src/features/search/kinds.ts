/**
 * The kinds of things the global search finds (v0.21): the heading of each result group, the
 * list page it belongs to, and the permission its list needs. This module imports nothing at
 * run time.
 */
import type { ResourceKind } from "@/api/types";

/** The groups the search loads from the API, in the order of the results. */
export const REMOTE_GROUP_IDS = ["datasets", "models", "specs", "runs", "experiments", "dashboards", "workflows", "endpoints", "catalog", "library"] as const;
export type RemoteGroupId = (typeof REMOTE_GROUP_IDS)[number];

/** The groups whose list the server filters by text (`name`, `name`, `q`); the search filters the others. */
export const SERVER_FILTERED: ReadonlySet<RemoteGroupId> = new Set(["runs", "experiments", "library"]);

/** Kinds that have a page but no result group: the search opens them by id only. */
export const ID_ONLY_KIND_IDS = ["validations", "policies", "webhooks", "plugins"] as const;
export type KindId = RemoteGroupId | (typeof ID_ONLY_KIND_IDS)[number];

export interface KindDef {
  id: KindId;
  /** The heading of the result group. */
  heading: string;
  /** One thing of this kind, in the hint at the right of a row. */
  kind: string;
  /** The nav entry that leads to these things: without it in the sidebar, the search hides them. */
  nav: string;
  /** The list page that reads `?q=`, when it is not `nav`. */
  list?: string;
  /** `can(grants, "read", resource)` guards the request. */
  resource: ResourceKind;
}

export const KINDS: Readonly<Record<KindId, KindDef>> = {
  datasets: { id: "datasets", heading: "Datasets", kind: "Dataset", nav: "/datasets", resource: "dataset" },
  models: { id: "models", heading: "Models", kind: "Model", nav: "/models", resource: "model" },
  specs: { id: "specs", heading: "Specs", kind: "Spec", nav: "/build", resource: "spec" },
  runs: { id: "runs", heading: "Runs", kind: "Run", nav: "/runs", resource: "run" },
  experiments: { id: "experiments", heading: "Experiments", kind: "Experiment", nav: "/experiments", resource: "run" },
  dashboards: { id: "dashboards", heading: "Dashboards", kind: "Dashboard", nav: "/dashboards", resource: "dashboard" },
  workflows: { id: "workflows", heading: "Workflows", kind: "Workflow", nav: "/workflows", resource: "workflow" },
  endpoints: { id: "endpoints", heading: "Endpoints", kind: "Endpoint", nav: "/integrations", resource: "endpoint" },
  catalog: { id: "catalog", heading: "Catalog", kind: "Catalog", nav: "/catalog", resource: "catalog" },
  library: { id: "library", heading: "Make your own", kind: "Make your own", nav: "/library", resource: "library" },
  validations: { id: "validations", heading: "Validations", kind: "Validation", nav: "/governance", list: "/validations", resource: "policy" },
  policies: { id: "policies", heading: "Policies", kind: "Policy", nav: "/governance", resource: "policy" },
  webhooks: { id: "webhooks", heading: "Webhooks", kind: "Webhook", nav: "/integrations", resource: "webhook" },
  plugins: { id: "plugins", heading: "Uploaded code", kind: "Uploaded code", nav: "/catalog", resource: "plugin" },
};

export function isKindId(v: unknown): v is KindId {
  return typeof v === "string" && Object.hasOwn(KINDS, v);
}

/** One thing a source found, before the ranking. */
export interface Candidate {
  id: string;
  title: string;
  /** One muted line: the id, a status, a section. */
  secondary?: string | undefined;
  /** An in-app path (it can have a query string). */
  href: string;
  at?: string | null | undefined;
  /** Other text that also finds the row (a slug, a catalog id). */
  alt?: string | undefined;
}

/**
 * What a remote source knows now. `loading` can have the rows of the text before (they stay
 * until the answer comes). An error gives no rows: the other groups still work.
 */
export interface SourceState {
  status: "loading" | "ready" | "error";
  items: Candidate[];
}
