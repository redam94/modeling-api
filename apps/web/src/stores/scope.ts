/**
 * The selected tenant and project (header switcher). The preference persists per browser;
 * `active` is the validated selection the app works in, set by the workspace gate once
 * `/v1/me` has been checked (a stale preference falls back to the first project).
 *
 * Server data never lives here: every project-scoped query key starts with the active key
 * (see `qk.project` in `api/queries.ts`), and requests name it in `X-Mapi-Project`.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export interface ProjectRef {
  tenantId: string;
  projectId: string;
}

/** `mapi_spec._dto.TENANT_ID_RE` (tenant and project ids are slugs). */
const SLUG = "[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?";
const KEY_RE = new RegExp(`^(${SLUG})/(${SLUG})$`);

/** `<tenant>/<project>`: the value of `X-Mapi-Project` and the query-key prefix. */
export function scopeKey(ref: ProjectRef): string {
  return `${ref.tenantId}/${ref.projectId}`;
}

export function parseScopeKey(value: string): ProjectRef | null {
  const m = KEY_RE.exec(value);
  return m ? { tenantId: m[1] ?? "", projectId: m[2] ?? "" } : null;
}

export interface ScopeState {
  /** The persisted preference (may name a project the user lost access to). */
  preferred: ProjectRef | null;
  /** Most recently used projects, newest first (`tenant/project` keys). */
  recent: string[];
  /** The validated selection in use; `null` until the workspace gate resolved it. */
  active: ProjectRef | null;
  /** The user picked a project: remember it and work in it. */
  select: (ref: ProjectRef) => void;
  /** The gate resolved the selection (does not touch the preference). */
  setActive: (ref: ProjectRef | null) => void;
}

function isRef(v: unknown): v is ProjectRef {
  if (typeof v !== "object" || v === null) return false;
  const r = v as Record<string, unknown>;
  return typeof r.tenantId === "string" && typeof r.projectId === "string" && parseScopeKey(`${r.tenantId}/${r.projectId}`) !== null;
}

export const useScope = create<ScopeState>()(
  persist(
    (set, get) => ({
      preferred: null,
      recent: [],
      active: null,
      select: (ref) => {
        const key = scopeKey(ref);
        set({
          preferred: ref,
          active: ref,
          recent: [key, ...get().recent.filter((k) => k !== key)].slice(0, 5),
        });
      },
      setActive: (active) => set({ active }),
    }),
    {
      name: "mapi.scope",
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ preferred: s.preferred, recent: s.recent }),
      // Whatever is in storage is user-controlled: keep only well-formed values.
      merge: (persisted, current) => {
        const p = (persisted ?? {}) as Partial<ScopeState>;
        return {
          ...current,
          preferred: isRef(p.preferred) ? p.preferred : null,
          recent: Array.isArray(p.recent) ? p.recent.filter((k): k is string => typeof k === "string" && parseScopeKey(k) !== null).slice(0, 5) : [],
        };
      },
    },
  ),
);

/** The active `tenant/project` key, or `null` before the gate resolved it. */
export function activeScopeKey(): string | null {
  const a = useScope.getState().active;
  return a ? scopeKey(a) : null;
}
