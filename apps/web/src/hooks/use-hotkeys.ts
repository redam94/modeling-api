import { useEffect, useRef } from "react";

export type HotkeyMap = Record<string, (e: KeyboardEvent) => void>;

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || Boolean(target.closest(".monaco-editor"));
}

/** Normalise a key event to "mod+k", "shift+?", "g d" chords use {@link useChords}. */
export function keyName(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.metaKey || e.ctrlKey) parts.push("mod");
  if (e.altKey) parts.push("alt");
  if (e.shiftKey && e.key.length > 1) parts.push("shift");
  parts.push(e.key.length === 1 ? e.key.toLowerCase() : e.key);
  return parts.join("+");
}

/**
 * Global shortcuts. Plain keys are ignored while typing in inputs or the editor; `mod+…`
 * combos always fire. Two-key chords ("g d") are supported with a 1.2 s window.
 */
export function useHotkeys(map: HotkeyMap, enabled = true): void {
  const ref = useRef(map);
  ref.current = map;
  useEffect(() => {
    if (!enabled) return;
    let pending: string | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const onKey = (e: KeyboardEvent) => {
      const name = keyName(e);
      const typing = isEditable(e.target);
      if (typing && !name.startsWith("mod+") && name !== "Escape") return;
      const handlers = ref.current;
      if (pending) {
        const chord = `${pending} ${name}`;
        pending = null;
        clearTimeout(timer);
        const h = handlers[chord];
        if (h) {
          e.preventDefault();
          h(e);
          return;
        }
      }
      if (Object.keys(handlers).some((k) => k.startsWith(`${name} `))) {
        pending = name;
        timer = setTimeout(() => (pending = null), 1200);
        return;
      }
      const h = handlers[name];
      if (h) {
        e.preventDefault();
        h(e);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(timer);
    };
  }, [enabled]);
}
