/**
 * CountUp: a number that eases to its new value (the playground total, KPI tiles on signed-in
 * pages). The final, formatted value is always in the DOM: while the number moves, a hidden-from-
 * assistive-tech copy shows the in-between values over it and the final text is only made
 * transparent. Screen readers, tests and copy-paste therefore always get the final value. Under
 * reduced motion (or without requestAnimationFrame) the number jumps straight to its end value.
 */
import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Ease out (cubic): fast at first, settling at the end. */
const easeOut = (p: number) => 1 - (1 - p) ** 3;

export function CountUp({
  value,
  format = String,
  from,
  duration = 600,
  className,
}: {
  value: number;
  /** Formats every shown value (the final one and those in between). */
  format?: (v: number) => string;
  /** Count from this value when the number first appears; by default it appears at its value. */
  from?: number;
  duration?: number;
  className?: string;
}) {
  // The value on screen right now: where the next animation starts when the value changes again.
  const shown = useRef(from ?? value);
  const [tween, setTween] = useState<{ target: number; at: number } | null>(null);

  useEffect(() => {
    const start = shown.current;
    if (start === value || !Number.isFinite(start) || !Number.isFinite(value) || duration <= 0 || prefersReducedMotion() || typeof requestAnimationFrame !== "function") {
      shown.current = value;
      return undefined;
    }
    let frame = 0;
    const t0 = performance.now();
    const step = (now: number) => {
      const p = Math.min(1, (now - t0) / duration);
      const at = start + (value - start) * easeOut(p);
      shown.current = at;
      setTween(p < 1 ? { target: value, at } : null);
      if (p < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);

  // A tween left over from an earlier target (interrupted, then a jump) is never shown.
  const moving = tween !== null && tween.target === value;
  return (
    <span data-slot="count-up" className={cn("inline-grid tabular", className)}>
      <span className={cn("[grid-area:1/1]", moving && "opacity-0")}>{format(value)}</span>
      {moving ? (
        <span aria-hidden className="[grid-area:1/1]">
          {format(tween.at)}
        </span>
      ) : null}
    </span>
  );
}
