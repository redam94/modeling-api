/**
 * What the global search may offer a caller (v0.21): the pages of the sidebar, the result
 * groups and the jumps by id. The rules are those of the sidebar (the nav list of the mode)
 * plus `can()`, so the search never offers a page the sidebar hides and never asks for a list
 * the caller cannot read. The server still decides every request.
 */
import { can, type Grants } from "@/api/access";
import type { ResourceKind } from "@/api/types";

import { KINDS, REMOTE_GROUP_IDS, type KindDef, type KindId } from "./kinds";

/** The data a page lists: a caller who cannot read it does not get the page as a result. */
const PAGE_RESOURCE: Readonly<Record<string, ResourceKind>> = {
  "/datasets": "dataset",
  "/build": "spec",
  "/runs": "run",
  "/experiments": "run",
  "/models": "model",
  "/score": "model",
  "/workflows": "workflow",
  "/governance": "policy",
  "/integrations": "endpoint",
  "/dashboards": "dashboard",
  "/catalog": "catalog",
  "/library": "library",
  "/usage": "usage",
  "/members": "member",
  "/tokens": "token",
};

/** The nav entries of the sidebar that the caller can read. */
export function visiblePages<T extends { to: string }>(pages: readonly T[], grants: Grants): T[] {
  return pages.filter((p) => {
    const resource = PAGE_RESOURCE[p.to];
    return !resource || can(grants, "read", resource);
  });
}

function available(kind: KindDef, paths: ReadonlySet<string>, grants: Grants): boolean {
  return paths.has(kind.nav) && can(grants, "read", kind.resource);
}

/** May the search show things of `kind`? (Its page is in the sidebar and the caller can read it.) */
export function kindAvailable(kind: KindId, paths: ReadonlySet<string>, grants: Grants): boolean {
  return available(KINDS[kind], paths, grants);
}

/** The result groups to load, in the order of the results. `paths` are the nav entries shown. */
export function remoteGroups(paths: ReadonlySet<string>, grants: Grants): KindDef[] {
  return REMOTE_GROUP_IDS.map((id) => KINDS[id]).filter((k) => available(k, paths, grants));
}

/** `<prefix>_<ULID>` as `mapi_spec._dto.new_id` mints it (the mock ids are shorter). */
const ID_TAIL = "[0-9a-z]{6,}";

const ID_ROUTES: readonly { prefix: string; kind: KindId; label: string; tail?: string; href: (id: string) => string }[] = [
  { prefix: "run", kind: "runs", label: "Open run", href: (id) => `/runs/${id}` },
  { prefix: "ds", kind: "datasets", label: "Open dataset", href: (id) => `/datasets/${id}` },
  { prefix: "spc", kind: "specs", label: "Open spec", href: (id) => `/build/${id}` },
  { prefix: "mdl", kind: "models", label: "Open model", href: (id) => `/models/${id}` },
  { prefix: "dsh", kind: "dashboards", label: "Open dashboard", href: (id) => `/dashboards/${id}` },
  { prefix: "exp", kind: "experiments", label: "Open experiment", href: (id) => `/experiments/${id}` },
  { prefix: "wf", kind: "workflows", label: "Open workflow", href: (id) => `/workflows/${id}` },
  { prefix: "val", kind: "validations", label: "Open validation", href: (id) => `/validations/${id}` },
  { prefix: "pol", kind: "policies", label: "Open policy", href: (id) => `/governance/policies/${id}` },
  { prefix: "whk", kind: "webhooks", label: "Open webhook", href: (id) => `/integrations/webhooks/${id}` },
  { prefix: "plg", kind: "plugins", label: "Open uploaded code", href: (id) => `/plugins/${id}` },
  // the library route accepts a full id only (`router.tsx`)
  { prefix: "lib", kind: "library", label: "Open library item", tail: "[0-9a-z]{26}", href: (id) => `/library?item=${id}` },
];

const ID_PATTERNS = ID_ROUTES.map((r) => ({ ...r, re: new RegExp(`^${r.prefix}_${r.tail ?? ID_TAIL}$`) }));

export interface IdJump {
  id: string;
  kind: KindId;
  href: string;
  label: string;
}

/** The page of the thing with the pasted id `query`, when the caller may open it. */
export function idJump(query: string, paths: ReadonlySet<string>, grants: Grants): IdJump | null {
  const id = query.trim();
  const route = ID_PATTERNS.find((r) => r.re.test(id));
  if (!route || !kindAvailable(route.kind, paths, grants)) return null;
  return { id, kind: route.kind, href: route.href(id), label: `${route.label} ${id}` };
}
