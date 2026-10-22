/**
 * The Catalog's what-if playground (v0.18 D21 (i)), for the marketing-mix model card that
 * WP-W3's card grid mounts. This file is tiny on purpose: the playground and its demo grid
 * (about 40 KiB) live in the lazy `playground-*` chunk, which loads only when the card renders,
 * and Home shares the same chunk. Signed-in surfaces only (never in share or client chunks).
 */
import { lazy, Suspense } from "react";

import { cn } from "@/lib/utils";

const MiniPlayground = lazy(() => import("./playground").then((m) => ({ default: m.MiniPlayground })));

/** What the card shows while the playground chunk loads: its shape, no motion. */
export function PlaygroundSkeleton({ className }: { className?: string }) {
  return (
    <div aria-hidden data-slot="playground-skeleton" className={cn("grid gap-4 rounded-2xl border bg-card p-5", className)}>
      <div className="h-5 w-2/5 rounded-full bg-muted" />
      <div className="flex gap-1.5">
        <div className="h-8 w-16 rounded-full bg-muted" />
        <div className="h-8 w-24 rounded-full bg-muted" />
        <div className="h-8 w-16 rounded-full bg-muted" />
      </div>
      <div className="h-2 w-full rounded-full bg-muted" />
      <div className="aspect-[8/5] w-full rounded-lg bg-muted/60" />
    </div>
  );
}

/** "Try the curve": pick a channel and slide its spend on example data (no server). */
export function CatalogPlayground({ className }: { className?: string }) {
  return (
    <Suspense fallback={<PlaygroundSkeleton className={className} />}>
      <MiniPlayground {...(className ? { className } : {})} />
    </Suspense>
  );
}
