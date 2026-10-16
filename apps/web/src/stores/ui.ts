/**
 * UI-only state (theme, persona, layout). Server data never lives here: it belongs to
 * TanStack Query. Persisted per browser; the Guided/Expert choice is remembered per signed-in
 * user (`personas`, keyed by user id), with `persona` as this browser's last choice. Until
 * either is set, the workspace decides (`defaultPersona` in features/guided/persona).
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type ThemePreference = "system" | "light" | "dark";
export type Persona = "expert" | "guided";

export interface UiState {
  theme: ThemePreference;
  /** The last choice made on this browser (the default for users without their own); `null` until one is made. */
  persona: Persona | null;
  /** Guided or Expert, per user id. */
  personas: Record<string, Persona>;
  sidebarCollapsed: boolean;
  paletteOpen: boolean;
  shortcutsOpen: boolean;
  setTheme: (theme: ThemePreference) => void;
  /** Remember `persona` (for `userId` when given). */
  setPersona: (persona: Persona, userId?: string | null) => void;
  toggleSidebar: () => void;
  setPaletteOpen: (open: boolean) => void;
  setShortcutsOpen: (open: boolean) => void;
}

const MAX_REMEMBERED_USERS = 50;

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      theme: "system",
      persona: null,
      personas: {},
      sidebarCollapsed: false,
      paletteOpen: false,
      shortcutsOpen: false,
      setTheme: (theme) => set({ theme }),
      setPersona: (persona, userId) =>
        set((s) => {
          if (!userId) return { persona };
          const rest = Object.entries(s.personas).filter(([k]) => k !== userId).slice(-(MAX_REMEMBERED_USERS - 1));
          return { persona, personas: { ...Object.fromEntries(rest), [userId]: persona } };
        }),
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
      setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
    }),
    {
      name: "mapi.ui",
      version: 3,
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ theme: s.theme, persona: s.persona, personas: s.personas, sidebarCollapsed: s.sidebarCollapsed }),
      migrate: (persisted, version) => {
        const v = (persisted ?? {}) as Partial<UiState>;
        // Before v3 `persona` started as "expert", so a stored "expert" may never have been
        // chosen; the per-user choices (`personas`) always were.
        return { ...v, persona: version < 3 ? null : (v.persona ?? null), personas: v.personas ?? {} };
      },
    },
  ),
);

/**
 * The persona of `userId`: their own choice, else this browser's last choice, else `fallback`
 * (the workspace's default).
 */
export function personaOf(
  state: Pick<UiState, "persona" | "personas">,
  userId: string | null | undefined,
  fallback: Persona = "expert",
): Persona {
  return (userId ? state.personas[userId] : undefined) ?? state.persona ?? fallback;
}

/** Resolve the effective theme for a preference and the OS setting. */
export function resolveTheme(pref: ThemePreference, prefersDark: boolean): "light" | "dark" {
  if (pref === "system") return prefersDark ? "dark" : "light";
  return pref;
}
