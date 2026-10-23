/**
 * Query keys of uploaded code (`/v1/plugins…`, v0.19).
 *
 * The plugin routes act in the selected project (the request names it in `X-Mapi-Project`;
 * the server reads the tenant from it), and what a caller sees depends on who they are
 * (private versions), so every key starts with `["p", "<tenant>/<project>", "plugins"]` like
 * `qk`'s project keys: a project or tenant switch never mixes caches. The tenant settings
 * routes name the tenant in the path: `["tenants", t, …]`. This module imports nothing at
 * runtime.
 */
import type { PluginListFilters } from "./types";

export const pk = {
  /** Every plugin query of project `p`. */
  all: (p: string) => ["p", p, "plugins"] as const,
  lists: (p: string) => ["p", p, "plugins", "list"] as const,
  list: (p: string, f: PluginListFilters = {}) => ["p", p, "plugins", "list", f] as const,
  /** One plugin and everything under it. */
  plugin: (p: string, id: string) => ["p", p, "plugins", "item", id] as const,
  versions: (p: string, id: string) => ["p", p, "plugins", "item", id, "versions"] as const,
  version: (p: string, id: string, versionId: string) => ["p", p, "plugins", "item", id, "versions", versionId] as const,
  usage: (p: string, id: string, versionId: string) => ["p", p, "plugins", "item", id, "versions", versionId, "usage"] as const,
  example: (p: string, id: string, versionId: string, plotId: string) =>
    ["p", p, "plugins", "item", id, "versions", versionId, "examples", plotId] as const,
  /** Tenant settings (tenant admins). */
  settings: (tenant: string) => ["tenants", tenant, "plugin-settings"] as const,
  uploaders: (tenant: string) => ["tenants", tenant, "plugin-uploaders"] as const,
};
