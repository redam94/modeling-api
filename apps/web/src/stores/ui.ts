/**
 * UI-only state (theme, persona, layout). Server data never lives here: it belongs to
 * TanStack Query. Persisted per browser; M2 moves persona to the user profile.
 */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type ThemePreference = "system" | "light" | "dark";
export type Persona = "expert" | "guided";

export interface UiState {
  theme: ThemePreference;
  persona: Persona;
  sidebarCollapsed: boolean;
  paletteOpen: boolean;
  shortcutsOpen: boolean;
  setTheme: (theme: ThemePreference) => void;
  setPersona: (persona: Persona) => void;
  toggleSidebar: () => void;
  setPaletteOpen: (open: boolean) => void;
  setShortcutsOpen: (open: boolean) => void;
}

export const useUi = create<UiState>()(
  persist(
    (set) => ({
      theme: "system",
      persona: "expert",
      sidebarCollapsed: false,
      paletteOpen: false,
      shortcutsOpen: false,
      setTheme: (theme) => set({ theme }),
      setPersona: (persona) => set({ persona }),
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
      setPaletteOpen: (paletteOpen) => set({ paletteOpen }),
      setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
    }),
    {
      name: "mapi.ui",
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({ theme: s.theme, persona: s.persona, sidebarCollapsed: s.sidebarCollapsed }),
    },
  ),
);

/** Resolve the effective theme for a preference and the OS setting. */
export function resolveTheme(pref: ThemePreference, prefersDark: boolean): "light" | "dark" {
  if (pref === "system") return prefersDark ? "dark" : "light";
  return pref;
}
