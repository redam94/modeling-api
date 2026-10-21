/**
 * SummaryStrip: one sheet with N cells separated by hairlines (MEI summary strip). Each cell is a
 * label, a value, an optional sub line and an optional tag. Two cells a row on phones.
 */
import type * as React from "react";

import { cn } from "@/lib/utils";

export interface SummaryItem {
  key: string;
  label: React.ReactNode;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tag?: React.ReactNode;
}

export function SummaryStrip({ items, label, className }: { items: readonly SummaryItem[]; label?: string; className?: string }) {
  return (
    <dl
      aria-label={label}
      data-slot="summary-strip"
      style={{ "--cols": Math.max(1, items.length) } as React.CSSProperties}
      className={cn(
        "grid grid-cols-2 gap-y-4 rounded-xl border bg-card p-5 max-sm:p-4 md:grid-cols-[repeat(var(--cols),minmax(0,1fr))] md:gap-y-0",
        className,
      )}
    >
      {items.map((i) => (
        <div
          key={i.key}
          className="flex min-w-0 flex-col gap-1 px-4 max-md:odd:pl-0 max-md:even:border-l md:border-l md:first:border-l-0 md:first:pl-0 md:last:pr-0"
        >
          <dt className="text-xs text-hint">{i.label}</dt>
          <dd className="text-lg font-semibold text-foreground tabular">{i.value}</dd>
          {i.sub ? <dd className="text-xs text-muted-foreground">{i.sub}</dd> : null}
          {i.tag ? <dd className="flex h-7 items-center">{i.tag}</dd> : null}
        </div>
      ))}
    </dl>
  );
}
