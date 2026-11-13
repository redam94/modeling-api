/**
 * The glyphs of the empty states: one 480 px file for each kind of thing, shown at about
 * 120 px. Signed-in pages only: the share link page and the client portal never load this
 * module (the bundle budget forbids `src/assets/brand/empty-*` there).
 */
import dashboards from "@/assets/brand/empty-dashboards.webp";
import datasets from "@/assets/brand/empty-datasets.webp";
import library from "@/assets/brand/empty-library.webp";
import models from "@/assets/brand/empty-models.webp";
import runs from "@/assets/brand/empty-runs.webp";
import search from "@/assets/brand/empty-search.webp";
import workflows from "@/assets/brand/empty-workflows.webp";
import { cn } from "@/lib/utils";

import { BrandArt } from "./brand-art";

const GLYPH = { dashboards, datasets, library, models, runs, search, workflows } as const;

export type EmptyArtKind = keyof typeof GLYPH;

/** Pass it to `EmptyState`'s `illustration` slot. `onBlock`: the glyph sits on a dark block. */
export function EmptyArt({ kind, className, onBlock = false }: { kind: EmptyArtKind; className?: string; onBlock?: boolean }) {
  return <BrandArt src={GLYPH[kind]} width={480} height={480} blend={onBlock ? "screen" : true} className={cn("size-[120px]", className)} />;
}
