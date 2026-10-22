/**
 * The tour spotlight (v0.18 D18): a fixed box over the element a step points at, drawn with
 * the Studio tour ring (lime inside ink, `tour-target` in app.css, which also pulses the
 * beacon three times) and a soft scrim over the rest of the page.
 *
 * It never blocks the page: `pointer-events: none`, so the highlighted control (and
 * everything else) stays clickable while a tour plays. Moving to the next step glides the
 * box on the Studio curve (a CSS transition: no animation library in this chunk, see the
 * tour host); scrolling and resizing follow at once. Under reduced motion the box jumps, and
 * app.css switches the beacon off.
 *
 * Also here: {@link useAnchor}, which waits for an anchor to appear, and {@link useAnchorBox},
 * which follows its box on screen.
 */
import { useEffect, useRef, useState, useSyncExternalStore } from "react";

import { cn } from "@/lib/utils";

import { findAnchor, type TourAnchor } from "./anchors";

/** How long a step waits for its anchor before it shows the "not on screen" card. */
export const ANCHOR_WAIT_MS = 8000;
/** Room between the element and the ring. */
const PAD = 4;

export interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
  /** The element's own corner radius in px. */
  radius: number;
}

// ------------------------------------------------------------------------------ media

function subscribeMedia(query: string) {
  return (cb: () => void) => {
    if (typeof window === "undefined" || !window.matchMedia) return () => undefined;
    const mq = window.matchMedia(query);
    mq.addEventListener?.("change", cb);
    return () => mq.removeEventListener?.("change", cb);
  };
}

/** A media query as React state (false where `matchMedia` is missing). */
export function useMedia(query: string): boolean {
  return useSyncExternalStore(
    subscribeMedia(query),
    () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false),
    () => false,
  );
}

export const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";
/** Phones get the coach-mark as a bottom sheet. */
export const PHONE = "(max-width: 639px)";

// ------------------------------------------------------------------------------ anchors

export interface AnchorState {
  el: HTMLElement | null;
  /** The anchor did not appear within {@link ANCHOR_WAIT_MS} (or the step cannot find it at all). */
  missing: boolean;
}

/**
 * The element of `anchor`, watched with a `MutationObserver`: it is picked up as soon as the
 * page renders it, and dropped when it leaves. After {@link ANCHOR_WAIT_MS} without it the
 * state says `missing` (the host then offers "Take me there" / "Skip"). `waitKey` restarts
 * the wait (a new step); `unreachable` reports `missing` at once (no page to open).
 */
export function useAnchor(anchor: TourAnchor, waitKey: string, unreachable = false): AnchorState {
  const [state, setState] = useState<AnchorState & { key: string }>(() => ({ el: null, missing: false, key: waitKey }));

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let missing = unreachable;
    const publish = (el: HTMLElement | null) => {
      const next = { el, missing: el ? false : missing, key: waitKey };
      setState((prev) => (prev.el === next.el && prev.missing === next.missing && prev.key === next.key ? prev : next));
    };
    const check = () => {
      const el = findAnchor(anchor);
      if (el) {
        if (timer !== null) clearTimeout(timer);
        timer = null;
        missing = unreachable;
      } else if (!missing && timer === null) {
        // not there yet, or gone (the user went elsewhere): wait for it afresh
        timer = setTimeout(() => {
          timer = null;
          missing = true;
          publish(findAnchor(anchor));
        }, ANCHOR_WAIT_MS);
      }
      publish(el);
    };
    check();
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-tour", "hidden"] });
    // a copy that only shows at some widths (the phone nav bar) comes and goes with the window size
    window.addEventListener("resize", check);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", check);
      if (timer !== null) clearTimeout(timer);
    };
  }, [anchor, waitKey, unreachable]);

  return state.key === waitKey ? { el: state.el, missing: state.missing } : { el: null, missing: unreachable };
}

function boxOf(el: HTMLElement): Box {
  const r = el.getBoundingClientRect();
  const radius = Number.parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0;
  return { top: r.top, left: r.left, width: r.width, height: r.height, radius };
}

function sameBox(a: Box | null, b: Box | null): boolean {
  return Boolean(a && b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height && a.radius === b.radius);
}

/** The on-screen box of `el`, kept current through scrolling, resizing and layout changes. */
export function useAnchorBox(el: HTMLElement | null): Box | null {
  const [box, setBox] = useState<Box | null>(null);
  useEffect(() => {
    if (!el) {
      setBox(null);
      return undefined;
    }
    let frame = 0;
    const measure = () => {
      frame = 0;
      const next = boxOf(el);
      setBox((prev) => (sameBox(prev, next) ? prev : next));
    };
    const schedule = () => {
      if (frame) return;
      frame = typeof requestAnimationFrame === "function" ? requestAnimationFrame(measure) : (setTimeout(measure, 16) as unknown as number);
    };
    measure();
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(schedule) : null;
    ro?.observe(el);
    ro?.observe(document.body);
    return () => {
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      ro?.disconnect();
      if (frame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
    };
  }, [el]);
  return box;
}

/** Scroll `el` into view when it is (partly) off screen; instant under reduced motion. */
export function revealAnchor(el: HTMLElement, opts: { reduced: boolean; phone: boolean }): void {
  const r = el.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight;
  // the phone sheet covers the lower part of the screen: keep the target in the top half
  const bottomLimit = opts.phone ? vh * 0.5 : vh;
  if (r.top >= 56 && r.bottom <= bottomLimit) return;
  // a tall anchor (taller than half the window) shows its top: the coach-mark points there
  const tall = r.height > bottomLimit * 0.5;
  if (typeof el.scrollIntoView === "function") el.scrollIntoView({ block: opts.phone || tall ? "start" : "center", behavior: opts.reduced ? "auto" : "smooth" });
}

// ------------------------------------------------------------------------------ spotlight

export interface SpotlightProps {
  box: Box;
  /** Changes with the step: the box glides to a new step and follows scrolling instantly. */
  stepKey: string;
  reduced: boolean;
}

/** How long the box glides to the next step (the coach-mark's Studio curve, with its small overshoot). */
export const GLIDE_MS = 320;

export function Spotlight({ box, stepKey, reduced }: SpotlightProps) {
  // Glide only when the step changes; while scrolling the box must stick to its element.
  const [glideKey, setGlideKey] = useState<string | null>(null);
  const lastKey = useRef(stepKey);
  useEffect(() => {
    if (lastKey.current === stepKey) return undefined;
    lastKey.current = stepKey;
    setGlideKey(stepKey);
    const t = setTimeout(() => setGlideKey(null), GLIDE_MS + 40);
    return () => clearTimeout(t);
  }, [stepKey]);
  const glide = glideKey === stepKey && !reduced;
  const radius = box.radius + PAD;
  return (
    <div
      aria-hidden
      data-testid="tour-spotlight"
      data-glide={glide ? "true" : undefined}
      className={cn(
        "tour-target pointer-events-none z-[60]",
        glide && "transition-[top,left,width,height,border-radius] duration-[320ms] ease-[cubic-bezier(0.2,0.9,0.3,1.2)]",
      )}
      style={{
        position: "fixed",
        top: box.top - PAD,
        left: box.left - PAD,
        width: box.width + PAD * 2,
        height: box.height + PAD * 2,
        borderRadius: radius,
        ["--tour-radius" as string]: `${radius}px`,
        boxShadow:
          "0 0 0 3px var(--highlight), 0 0 0 6px var(--block), 0 0 0 200vmax color-mix(in srgb, var(--overlay) 38%, transparent)",
      }}
    />
  );
}
