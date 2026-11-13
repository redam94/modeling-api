/**
 * The frame every Studio illustration draws in: an inline SVG that is decorative by default
 * (`aria-hidden`, no text inside) or, given a `label`, one image with that accessible name.
 *
 * Art rules (plans/v018-direction.md §6): built from MMM chart parts only (rounded bars, a
 * response curve, an operating-point dot, a sun disc, a spend wedge, card outlines, dashed
 * placeholders); colours by role token class only, never a hex; strokes 2 px or more; no text,
 * no gradients, no shadows, no external files. Never in readout, share or client markup.
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

export interface ArtProps {
  /** Says what the art shows when it carries meaning; leave out for decorative art. */
  label?: string;
  className?: string;
}

/** Accessibility attributes for an SVG: one labelled image, or hidden from assistive tech. */
export function artA11y(label: string | undefined): React.SVGProps<SVGSVGElement> {
  return label ? { role: "img", "aria-label": label } : { "aria-hidden": true, focusable: "false" };
}

export function Art({ viewBox, label, className, children }: ArtProps & { viewBox: string; children: React.ReactNode }) {
  return (
    <svg viewBox={viewBox} data-slot="illustration" className={cn("block h-auto w-full", className)} {...artA11y(label)}>
      {children}
    </svg>
  );
}

// Shared class strings: the role tokens of §6 as fill and stroke utilities.
export const I = {
  /** Main bars. */
  indigo: "fill-illus-1",
  /** Second bars. */
  lav: "fill-illus-2",
  /** The sun, an operating point. */
  lime: "fill-illus-3",
  /** The accent dot. */
  coral: "fill-illus-4",
  /** Pale shapes (the spend wedge, title bars). */
  pale: "fill-grey-300",
  /** Ink shapes. */
  ink: "fill-grey-900",
  /** White shapes on a block. */
  white: "fill-block-foreground",
  /** The one raised bar on a block. */
  raised: "fill-block-raised",
  /** A card shape with its outline. */
  card: "fill-card stroke-border-strong",
  /** A placeholder panel or line. */
  muted: "fill-muted",
  /** A dashed placeholder outline (`strokeDasharray="6 5"`). */
  dashed: "fill-none stroke-input",
  /** The response curve. */
  curve: "fill-none stroke-illus-4",
  /** A mark drawn on the sun (plus, arrow, tick). */
  onSun: "fill-none stroke-highlight-foreground",
} as const;

/** Empty-state art is 420 × 320 and never animates (one entrance per page: the hero's). */
export const EMPTY_VIEWBOX = "0 0 420 320";

/** The card most empty-state art is drawn on: an outlined card with a pale title and a muted line. */
export function CardFrame({ x = 40, y = 40, width = 300, height = 220 }: { x?: number; y?: number; width?: number; height?: number }) {
  return (
    <>
      <rect className={I.card} strokeWidth={2} x={x} y={y} width={width} height={height} rx={22} />
      <rect className={I.pale} x={x + 24} y={y + 24} width={120} height={16} rx={8} />
      <rect className={I.muted} x={x + 24} y={y + 50} width={76} height={10} rx={5} />
    </>
  );
}

/** The pale quarter-circle spend wedge in the lower right corner. */
export function SpendWedge() {
  return <path className={I.pale} d="M330 300 a 60 60 0 0 1 60 -60 v 60 z" />;
}
