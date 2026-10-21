/**
 * A chart key: a swatch and a word per mark. Swatch colours are token names mapped to literal
 * classes (Tailwind only generates classes it can see in the source).
 */
import { cn } from "@/lib/utils";

import { ZONE_FILL, ZONE_WORDS, type Zone } from "./tag-word";

export type LegendColor =
  | "index"
  | "band"
  | "band-50"
  | "ref"
  | "gain"
  | "loss"
  | "net"
  | "now"
  | "plain"
  | "foreground"
  | Zone
  | `series-${1 | 2 | 3 | 4 | 5 | 6 | 7 | 8}`;

/** fill: a 14x10 block; line: a 2px rule; dash: a dashed rule; dot: a 10px circle. */
export type LegendSwatch = "fill" | "line" | "dash" | "dot";

export interface LegendItem {
  label: string;
  color: LegendColor;
  swatch?: LegendSwatch;
}

const BG: Record<LegendColor, string> = {
  index: "bg-chart-index",
  band: "bg-chart-band",
  "band-50": "bg-chart-band-50",
  ref: "bg-chart-ref",
  gain: "bg-chart-gain",
  loss: "bg-chart-loss",
  net: "bg-chart-net",
  now: "bg-chart-now",
  plain: "bg-chart-plain",
  foreground: "bg-foreground",
  ...ZONE_FILL,
  "series-1": "bg-series-1",
  "series-2": "bg-series-2",
  "series-3": "bg-series-3",
  "series-4": "bg-series-4",
  "series-5": "bg-series-5",
  "series-6": "bg-series-6",
  "series-7": "bg-series-7",
  "series-8": "bg-series-8",
};

const BORDER: Record<LegendColor, string> = {
  index: "border-chart-index",
  band: "border-chart-band",
  "band-50": "border-chart-band-50",
  ref: "border-chart-ref",
  gain: "border-chart-gain",
  loss: "border-chart-loss",
  net: "border-chart-net",
  now: "border-chart-now",
  plain: "border-chart-plain",
  foreground: "border-foreground",
  headroom: "border-zone-headroom",
  optimal: "border-zone-optimal",
  over: "border-zone-over",
  no_payback: "border-zone-nopay",
  "series-1": "border-series-1",
  "series-2": "border-series-2",
  "series-3": "border-series-3",
  "series-4": "border-series-4",
  "series-5": "border-series-5",
  "series-6": "border-series-6",
  "series-7": "border-series-7",
  "series-8": "border-series-8",
};

function Swatch({ color, swatch = "fill" }: { color: LegendColor; swatch?: LegendSwatch | undefined }) {
  if (swatch === "line") return <i aria-hidden className={cn("inline-block h-0 w-3.5 border-t-2", BORDER[color])} />;
  if (swatch === "dash") return <i aria-hidden className={cn("inline-block h-0 w-3.5 border-t-2 border-dashed", BORDER[color])} />;
  if (swatch === "dot") return <i aria-hidden className={cn("inline-block size-2.5 rounded-full", BG[color])} />;
  return <i aria-hidden className={cn("inline-block h-2.5 w-3.5 rounded-xs", BG[color])} />;
}

export function Legend({ items, label = "Key", className }: { items: readonly LegendItem[]; label?: string; className?: string }) {
  return (
    <ul aria-label={label} className={cn("flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground", className)}>
      {items.map((i) => (
        <li key={`${i.color}-${i.label}`} className="inline-flex items-center gap-1.5">
          <Swatch color={i.color} swatch={i.swatch} />
          {i.label}
        </li>
      ))}
    </ul>
  );
}

const ZONES: readonly Zone[] = ["headroom", "optimal", "over", "no_payback"];

/** The four zones with their tag words. */
export function ZoneLegend({ zones = ZONES, className }: { zones?: readonly Zone[]; className?: string }) {
  return <Legend label="Zones" className={className} items={zones.map((z) => ({ label: ZONE_WORDS[z], color: z }))} />;
}
