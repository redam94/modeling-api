/**
 * StatTile: one KPI on a readout (MEI "Statistic"): a label, a large tabular value, the change
 * against its basis and a plain caption. The change carries a word as well as colour and a
 * triangle, so it reads the same without colour and to a screen reader ("Up 4.2 % against the
 * plan").
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

/** mapi_spec.dashboards.KpiValue.delta_dir. */
export type DeltaDir = "up" | "down" | "flat" | "alert";

const DELTA_CLASS: Record<DeltaDir, string> = {
  up: "text-success",
  down: "text-destructive",
  flat: "text-hint",
  alert: "text-destructive",
};

/** The word a screen reader hears before the change; flat and alert texts speak for themselves. */
export const DELTA_WORD: Record<DeltaDir, string | null> = {
  up: "Up",
  down: "Down",
  flat: null,
  alert: null,
};

export function DeltaText({ dir, children, className }: { dir: DeltaDir; children: React.ReactNode; className?: string }) {
  const word = DELTA_WORD[dir];
  return (
    <span data-slot="delta" data-dir={dir} className={cn("inline-flex items-center gap-0.5 text-sm font-semibold tabular", DELTA_CLASS[dir], className)}>
      {dir === "up" || dir === "down" ? (
        <svg aria-hidden viewBox="0 0 10 10" className={cn("size-2.5 flex-none fill-current", dir === "down" && "rotate-180")}>
          <path d="M5 1 9 7H1z" />
        </svg>
      ) : null}
      {word ? <span className="sr-only">{word} </span> : null}
      {children}
    </span>
  );
}

export function StatTile({
  label,
  value,
  delta,
  deltaDir = "flat",
  caption,
  size = "m",
  bare = false,
  className,
}: {
  label: React.ReactNode;
  /** Already formatted ("12,400", "1.84", "—"). */
  value: React.ReactNode;
  /** Already formatted, without a sign word ("4.2 % against the plan"). */
  delta?: React.ReactNode;
  deltaDir?: DeltaDir;
  caption?: React.ReactNode;
  size?: "m" | "l";
  /** Without the card (inside a SummaryStrip or another card). */
  bare?: boolean;
  className?: string;
}) {
  return (
    <div
      data-slot="stat-tile"
      className={cn("flex min-w-0 flex-col gap-1", !bare && "rounded-xl border bg-card p-5 max-sm:p-4", className)}
    >
      <div className="text-sm text-muted-foreground">{label}</div>
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className={cn("font-semibold text-foreground tabular", size === "l" ? "text-3xl" : "text-2xl")}>{value}</span>
        {delta != null && delta !== "" ? <DeltaText dir={deltaDir}>{delta}</DeltaText> : null}
      </div>
      {caption ? <div className="text-xs text-hint">{caption}</div> : null}
    </div>
  );
}

/** Four tiles a row on wide screens, two on tablets, one on phones. */
export function StatGrid({ className, ...props }: React.ComponentProps<"div">) {
  return <div className={cn("grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4", className)} {...props} />;
}
