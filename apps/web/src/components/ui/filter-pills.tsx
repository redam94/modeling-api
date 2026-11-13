/**
 * The filter pills (as the Catalog's filters): a single choice among
 * a few page-level options, each a pill with a 1.5 px `border-strong` edge; the chosen one is
 * filled with the foreground (`filter-pill` in app.css). `SegmentedControl` stays the compact
 * grey strip for dense toolbars.
 *
 * Same roles, states and keys as `SegmentedControl` (a Radix single toggle group): a
 * `radiogroup` of `radio`s with `aria-checked` and `data-state="on"`, one tab stop, the arrow
 * keys (and Home/End) move focus, Enter or Space chooses. Written by hand, without Radix, so
 * the share and client chunks (which share `overlays.tsx`) do not grow.
 */
import { useRef, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "@/lib/utils";

const MOVES: Record<string, (i: number, n: number) => number> = {
  ArrowRight: (i, n) => (i + 1) % n,
  ArrowDown: (i, n) => (i + 1) % n,
  ArrowLeft: (i, n) => (i - 1 + n) % n,
  ArrowUp: (i, n) => (i - 1 + n) % n,
  Home: () => 0,
  End: (_, n) => n - 1,
};

export function FilterPills<T extends string>({
  value,
  onValueChange,
  options,
  label,
  className,
}: {
  value: T;
  onValueChange: (v: T) => void;
  options: { value: T; label: ReactNode; title?: string }[];
  label: string;
  className?: string;
}) {
  const root = useRef<HTMLDivElement>(null);
  const current = Math.max(0, options.findIndex((o) => o.value === value));
  const onKeyDown = (e: KeyboardEvent<HTMLButtonElement>, i: number) => {
    const move = MOVES[e.key];
    if (!move) return;
    e.preventDefault();
    root.current?.querySelectorAll<HTMLButtonElement>("[role='radio']")[move(i, options.length)]?.focus();
  };
  return (
    <div ref={root} role="radiogroup" aria-label={label} className={cn("flex max-w-full flex-wrap items-center gap-2", className)}>
      {options.map((o, i) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            data-state={on ? "on" : "off"}
            title={o.title}
            tabIndex={i === current ? 0 : -1}
            onClick={() => {
              if (!on) onValueChange(o.value);
            }}
            onKeyDown={(e) => onKeyDown(e, i)}
            className="focus-ring filter-pill"
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
