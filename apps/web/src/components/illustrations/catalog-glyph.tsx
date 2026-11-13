/**
 * Catalog glyphs: a small chart-parts drawing in a tinted tile, one tint per kind (model ink tint,
 * preparation step orange tint, chart blue tint, workflow muted). Each kind has a few
 * drawings; `seed` (an entry id) picks one so a card grid does not repeat one picture, and the
 * same entry always gets the same drawing. 34 px art in a 56 px tile (`md`), 26 in 44 (`sm`, list
 * rows).
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

import { artA11y, I } from "./art";

export type GlyphKind = "model" | "step" | "plot" | "workflow";

/**
 * The glyph kind for a catalog or library kind word: `model`/`model_recipe` → model,
 * `step`/`prep_preset` → step, `plot`/`recipe`/`chart_template` → plot,
 * `workflow`/`node`/`template`/`workflow_template` → workflow. Unknown words draw a model.
 */
export function glyphKind(kind: string): GlyphKind {
  const k = kind.toLowerCase();
  if (k.includes("workflow") || k === "node" || k === "workflow_node" || k === "template") return "workflow";
  if (k.includes("chart") || k.includes("plot") || k === "recipe") return "plot";
  if (k.includes("step") || k.includes("prep")) return "step";
  return "model";
}

const TILE: Record<GlyphKind, string> = {
  model: "bg-accent",
  step: "bg-highlight-soft",
  plot: "bg-info/10",
  workflow: "bg-muted",
};

const FG = "fill-none stroke-foreground";
const BASE = "fill-none stroke-border-strong";

/** The drawings for each kind (viewBox 0 0 34 34). */
const DRAWINGS: Record<GlyphKind, readonly React.ReactNode[]> = {
  model: [
    <>
      <path className="fill-none stroke-primary" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" d="M3 24 L10 18 L16 21 L23 11 L31 14" />
      <path className={I.second} d="M23 11 L31 6 V 20 Z" />
    </>,
    <>
      <circle className={I.disc} cx="17" cy="17" r="11" />
      <path className={FG} strokeWidth={3} strokeLinecap="round" d="M8 24 L26 10" />
    </>,
  ],
  step: [
    <>
      <rect className={I.main} x="3" y="6" width="6" height="22" rx="2" />
      <rect className={I.second} x="14" y="12" width="6" height="16" rx="2" />
      <rect className={I.second} x="25" y="16" width="6" height="12" rx="2" />
      <path className={FG} strokeWidth={2.5} strokeDasharray="3 3" d="M6 4 C 14 4, 20 8, 28 14" />
    </>,
    <>
      <path className="fill-none stroke-highlight-ink" strokeWidth={3} strokeLinecap="round" d="M3 26 C 10 26, 14 8, 31 8" />
      <path className={BASE} strokeWidth={2} d="M3 30 H 31" />
    </>,
    <>
      <path className={BASE} strokeWidth={2} d="M3 28 H31" />
      <rect className={I.second} x="5" y="16" width="4" height="12" rx="1" />
      <rect className={I.second} x="12" y="18" width="4" height="10" rx="1" />
      <rect className={I.dot} x="19" y="5" width="5" height="23" rx="1.5" />
      <rect className={I.second} x="27" y="17" width="4" height="11" rx="1" />
    </>,
    <>
      <rect className={I.main} x="5" y="5" width="24" height="7" rx="3" />
      <rect className={I.second} x="5" y="14" width="24" height="7" rx="3" />
      <rect className={I.disc} x="5" y="23" width="24" height="7" rx="3" />
    </>,
  ],
  plot: [
    <>
      <path className="fill-none stroke-illus-4" strokeWidth={3} strokeLinecap="round" d="M4 30 C 10 28, 14 10, 22 8 S 30 6, 31 6" />
      <circle className={I.dot} cx="20" cy="9" r="3.5" />
    </>,
    <>
      <rect className={I.main} x="3" y="20" width="6" height="10" rx="1.5" />
      <rect className={I.second} x="11" y="12" width="6" height="8" rx="1.5" />
      <rect className={I.dot} x="19" y="6" width="6" height="6" rx="1.5" />
      <rect className={I.disc} x="27" y="6" width="5" height="24" rx="1.5" />
    </>,
    <>
      <rect className={I.dot} x="4" y="18" width="6" height="12" rx="1.5" />
      <rect className={I.second} x="14" y="10" width="6" height="20" rx="1.5" />
      <rect className={I.main} x="24" y="4" width="6" height="26" rx="1.5" />
    </>,
  ],
  workflow: [
    <>
      <path className={FG} strokeWidth={2.5} d="M10 11 L15 22 M24 11 L19 22" />
      <circle className={I.main} cx="7" cy="9" r="4" />
      <circle className={I.second} cx="27" cy="9" r="4" />
      <circle className={I.disc} cx="17" cy="26" r="5" />
    </>,
  ],
};

/** A small stable number for a string (FNV-1a), so a seed always picks the same drawing. */
function hash(seed: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < seed.length; i++) {
    h ^= seed.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** How many drawings a kind has (`variant` is taken modulo this). */
export function glyphVariants(kind: GlyphKind): number {
  return DRAWINGS[kind].length;
}

export function CatalogGlyph({
  kind,
  seed,
  variant,
  size = "md",
  label,
  className,
}: {
  kind: GlyphKind;
  /** An entry id or name: picks one of the kind's drawings, the same one every time. */
  seed?: string;
  /** Picks a drawing directly (wins over `seed`). */
  variant?: number;
  size?: "md" | "sm";
  /** Says what the glyph stands for when nothing next to it does; decorative otherwise. */
  label?: string;
  className?: string;
}) {
  const drawings = DRAWINGS[kind];
  const index = variant != null ? Math.abs(Math.trunc(variant)) % drawings.length : seed ? hash(seed) % drawings.length : 0;
  return (
    <span
      data-slot="catalog-glyph"
      data-kind={kind}
      className={cn(
        "grid flex-none place-items-center",
        size === "md" ? "size-14 rounded-xl" : "size-11 rounded-lg",
        TILE[kind],
        className,
      )}
    >
      <svg viewBox="0 0 34 34" className={size === "md" ? "size-[34px]" : "size-[26px]"} {...artA11y(label)}>
        {drawings[index]}
      </svg>
    </span>
  );
}
