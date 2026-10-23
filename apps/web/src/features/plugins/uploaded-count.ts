/**
 * Whether this workspace can see uploads at all, and how many plugins Catalog › Yours lists
 * (the tab's count). Small on purpose: the Yours tab imports it statically and loads the
 * Uploaded section itself lazily.
 */
import { trustTierOf, useWorkspace, type Workspace } from "@/features/auth/workspace";

import { pluginFlags, usePlugins } from "./api";

/** Uploads are on, the caller has a session, and the tenant is not on the public tier (D1). */
export function uploadsAvailable(ws: Workspace): boolean {
  return pluginFlags(ws.me).enabled && ws.mode === "session" && Boolean(ws.current) && trustTierOf(ws.current?.tenant) !== "public";
}

/** How many uploaded plugins Yours lists (0 while loading or when uploads are off). */
export function useUploadedCount(): { count: number; isPending: boolean } {
  const ws = useWorkspace();
  const on = uploadsAvailable(ws);
  const q = usePlugins({}, on);
  return { count: on ? (q.data?.length ?? 0) : 0, isPending: on && q.isPending };
}
