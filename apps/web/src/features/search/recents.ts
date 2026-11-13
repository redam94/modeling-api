/**
 * The things a user opened from the global search (v0.21): the last eight for each project,
 * newest first, kept in this browser. As in `stores/scope.ts`, whatever is in storage is
 * user-controlled, so only well-formed in-app links are kept.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { parseScopeKey } from "@/stores/scope";

import { isKindId, type KindId } from "./kinds";

export const RECENT_LIMIT = 8;
/** Projects with a list; the least recently used list goes first. */
const SCOPE_LIMIT = 20;
/** The key of a server without projects (v0.2 profile). */
export const NO_SCOPE = "-";

export interface RecentItem {
  group: KindId;
  title: string;
  secondary?: string | undefined;
  /** An in-app path (it can have a query string). */
  href: string;
}

export interface RecentsState {
  /** `tenant/project` → items, newest first. */
  byScope: Record<string, RecentItem[]>;
  list: (scope: string) => RecentItem[];
  add: (scope: string, item: RecentItem) => void;
  clear: (scope: string) => void;
}

const EMPTY: RecentItem[] = [];
/** A path of this app: one leading slash, no scheme, no host. */
const HREF_RE = /^\/(?!\/)[\w\-./?=&%:~]{0,500}$/;

function isScope(key: string): boolean {
  return key === NO_SCOPE || parseScopeKey(key) !== null;
}

function isItem(v: unknown): v is RecentItem {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return (
    isKindId(r.group) &&
    typeof r.title === "string" &&
    r.title.length > 0 &&
    r.title.length <= 300 &&
    (r.secondary === undefined || (typeof r.secondary === "string" && r.secondary.length <= 300)) &&
    typeof r.href === "string" &&
    HREF_RE.test(r.href)
  );
}

export const useRecents = create<RecentsState>()(
  persist(
    (set, get) => ({
      byScope: {},
      list: (scope) => get().byScope[scope] ?? EMPTY,
      add: (scope, item) => {
        if (!isScope(scope) || !isItem(item)) return;
        const { [scope]: before = EMPTY, ...others } = get().byScope;
        const list = [item, ...before.filter((r) => r.href !== item.href)].slice(0, RECENT_LIMIT);
        const kept = Object.entries(others).slice(-(SCOPE_LIMIT - 1));
        set({ byScope: { ...Object.fromEntries(kept), [scope]: list } });
      },
      clear: (scope) => {
        const { [scope]: _dropped, ...others } = get().byScope;
        set({ byScope: others });
      },
    }),
    {
      name: "mapi.search.recent",
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ byScope: s.byScope }),
      merge: (persisted, current) => {
        const stored = (persisted ?? {}) as { byScope?: unknown };
        const byScope: Record<string, RecentItem[]> = {};
        if (typeof stored.byScope === "object" && stored.byScope !== null) {
          for (const [key, list] of Object.entries(stored.byScope).slice(-SCOPE_LIMIT)) {
            if (!isScope(key) || !Array.isArray(list)) continue;
            const items = list.filter(isItem).slice(0, RECENT_LIMIT);
            if (items.length) byScope[key] = items;
          }
        }
        return { ...current, byScope };
      },
    },
  ),
);
