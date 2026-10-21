/**
 * Query keys of the v0.16 dashboards.
 *
 * Analyst keys live under `qk.dashboards(p)` (`["p", "<tenant>/<project>", "dashboards"]`) so a
 * project switch never mixes caches; `keys.test.ts` checks the prefixes equal `qk`'s. The
 * readout page keys its queries by the {@link DashboardClient.cacheKey} of its client
 * (`view`), so a member preview, a client user and a share link never share entries. This
 * module imports nothing at runtime (the share app and the readout page use it).
 */
import type { ReadoutKind, ReadoutQuery, ReadoutVisibility } from "./types";

export interface DashboardListFilters {
  status?: "draft" | "published" | "archived" | undefined;
}

export interface ReadoutFilters {
  kind?: ReadoutKind | undefined;
  visibility?: ReadoutVisibility | undefined;
}

export const dk = {
  /** Every dashboard query of project `p` (= `qk.dashboards(p)`). */
  all: (p: string) => ["p", p, "dashboards"] as const,
  list: (p: string, f: DashboardListFilters = {}) => ["p", p, "dashboards", "list", f] as const,
  /** One dashboard and everything under it (= `qk.dashboard(p, id)`). */
  one: (p: string, id: string) => ["p", p, "dashboards", id] as const,
  revisions: (p: string, id: string) => ["p", p, "dashboards", id, "revisions"] as const,
  validation: (p: string, id: string, etag: string) => ["p", p, "dashboards", id, "validation", etag] as const,
  grants: (p: string, id: string) => ["p", p, "dashboards", id, "grants"] as const,
  shares: (p: string, id: string) => ["p", p, "dashboards", id, "shares"] as const,
  readouts: (p: string, id: string) => ["p", p, "dashboards", id, "readouts"] as const,
  readoutList: (p: string, id: string, f: ReadoutFilters) => ["p", p, "dashboards", id, "readouts", "list", f] as const,
  refitRequests: (p: string, id: string) => ["p", p, "dashboards", id, "refit-requests"] as const,

  // ---------------------------------------------------------------- the readout page (any client)
  /** Everything one {@link DashboardClient} read. */
  view: (cacheKey: readonly string[]) => [...cacheKey, "view"] as const,
  viewDashboard: (cacheKey: readonly string[]) => [...cacheKey, "view", "dashboard"] as const,
  viewReadouts: (cacheKey: readonly string[], q: ReadoutQuery = {}) => [...cacheKey, "view", "readouts", q] as const,
  viewReadout: (cacheKey: readonly string[], seq: number) => [...cacheKey, "view", "readout", seq] as const,
  viewGrid: (cacheKey: readonly string[]) => [...cacheKey, "view", "grid"] as const,
  viewTemplate: (cacheKey: readonly string[]) => [...cacheKey, "view", "actuals-template"] as const,
  viewWhatIf: (cacheKey: readonly string[], multipliers: Record<string, number>) => [...cacheKey, "view", "whatif", multipliers] as const,
};

/** The cache key of a signed-in caller's client (under the project's dashboard keys). */
export function memberCacheKey(scopeKey: string, dashboardId: string, kind: "member" | "client"): readonly string[] {
  return [...dk.one(scopeKey, dashboardId), kind];
}

/** The cache key of a share-link client (never under a project: the share app has none). */
export function shareCacheKey(dashboardId: string): readonly string[] {
  return ["share", dashboardId];
}
