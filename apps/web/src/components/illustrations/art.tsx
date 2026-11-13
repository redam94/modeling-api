/**
 * What every inline SVG drawing in this folder shares: it is decorative by default
 * (`aria-hidden`, no text inside) or, given a `label`, one image with that accessible name.
 *
 * Art rules: built from chart parts only (rounded bars, a response curve, an operating-point
 * dot, a disc); colours by role token class only, never a hex; strokes 2 px or more; no text, no
 * gradients, no shadows, no external files. Never in readout, share or client markup.
 */
import type * as React from "react";

export interface ArtProps {
  /** Says what the art shows when it carries meaning; leave out for decorative art. */
  label?: string;
  className?: string;
}

/** Accessibility attributes for an SVG: one labelled image, or hidden from assistive tech. */
export function artA11y(label: string | undefined): React.SVGProps<SVGSVGElement> {
  return label ? { role: "img", "aria-label": label } : { "aria-hidden": true, focusable: "false" };
}

// Shared class strings: the illustration role tokens as fill utilities.
export const I = {
  /** Main bars (the ink). */
  main: "fill-illus-1",
  /** Second bars (the data orange). */
  second: "fill-illus-2",
  /** A disc, an operating point (the data blue). */
  disc: "fill-illus-3",
  /** The quiet dot (mid grey). */
  dot: "fill-illus-4",
} as const;
