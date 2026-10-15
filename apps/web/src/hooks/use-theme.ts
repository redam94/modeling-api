import { useEffect, useSyncExternalStore } from "react";

import { resolveTheme, useUi } from "@/stores/ui";

const QUERY = "(prefers-color-scheme: dark)";

function subscribe(cb: () => void): () => void {
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", cb);
  return () => mql.removeEventListener("change", cb);
}

function prefersDark(): boolean {
  return window.matchMedia(QUERY).matches;
}

/** The effective theme ("light" | "dark"), following the OS when the preference is "system". */
export function useResolvedTheme(): "light" | "dark" {
  const pref = useUi((s) => s.theme);
  const dark = useSyncExternalStore(subscribe, prefersDark, () => false);
  return resolveTheme(pref, dark);
}

/** Apply the theme class to <html>. Mount once at the root. */
export function useApplyTheme(): "light" | "dark" {
  const theme = useResolvedTheme();
  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    root.style.colorScheme = theme;
  }, [theme]);
  return theme;
}
