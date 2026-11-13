/**
 * Tag words: a 24px soft pill (as `Badge`) that names a state in words. Zone colours never stand alone on a
 * readout; each zone comes with its tag word ("More room", "Within range", ...).
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

export type TagTone = "neutral" | "info" | "positive" | "warning" | "negative";

// The tint alphas match the Badge table in theme-contrast.test.ts, so the text inks pass AA.
const TONE_CLASS: Record<TagTone, string> = {
  neutral: "bg-muted text-muted-foreground",
  info: "bg-accent text-accent-foreground",
  positive: "bg-success/12 text-success",
  warning: "bg-warning/14 text-warning",
  negative: "bg-destructive/12 text-destructive",
};

export function TagWord({
  tone = "neutral",
  className,
  ...props
}: React.ComponentProps<"span"> & { tone?: TagTone }) {
  return (
    <span
      data-slot="tag-word"
      data-tone={tone}
      className={cn("inline-flex h-6 flex-none items-center gap-1 rounded-full px-2.5 text-xs font-medium whitespace-nowrap", TONE_CLASS[tone], className)}
      {...props}
    />
  );
}

/** Where a channel's spend sits on its response curve (mapi_spec.dashboards.Zone). */
export type Zone = "headroom" | "optimal" | "over" | "no_payback";

/** The plain tag words for each zone (plan §3.4); GLOSSARY in mapi_spec explains them. */
export const ZONE_WORDS: Record<Zone, string> = {
  headroom: "More room",
  optimal: "Within range",
  over: "Past its best",
  no_payback: "Not paying back",
};

export const ZONE_TONE: Record<Zone, TagTone> = {
  headroom: "info",
  optimal: "positive",
  over: "negative",
  no_payback: "neutral",
};

/** Strong zone fills (bars, legends), as literal classes so Tailwind generates them. */
export const ZONE_FILL: Record<Zone, string> = {
  headroom: "bg-zone-headroom",
  optimal: "bg-zone-optimal",
  over: "bg-zone-over",
  no_payback: "bg-zone-nopay",
};

/** Zone tints (cells, bands). */
export const ZONE_TINT: Record<Zone, string> = {
  headroom: "bg-zone-headroom-tint",
  optimal: "bg-zone-optimal-tint",
  over: "bg-zone-over-tint",
  no_payback: "bg-zone-nopay-tint",
};

export function ZoneTag({ zone, className }: { zone: Zone | null | undefined; className?: string }) {
  if (!zone) {
    return (
      <TagWord tone="neutral" className={className}>
        No zone
      </TagWord>
    );
  }
  return (
    <TagWord tone={ZONE_TONE[zone]} data-zone={zone} className={className}>
      {ZONE_WORDS[zone]}
    </TagWord>
  );
}
