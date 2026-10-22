/**
 * Small building blocks of the Guided screens, in the shadcn/ui new-york style of the rest of
 * the app: a side sheet, check badges, sparklines and headline figures. The sliders and the
 * uncertainty band moved to `components/interactive` (the playgrounds use them too); they are
 * re-exported here so the Guided steps keep their imports.
 */
import { AlertTriangle, CheckCircle2, X, XCircle } from "lucide-react";
import { Dialog as DialogPrimitive } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

export { ChangeBand } from "@/components/interactive/change-band";
export { RangeSlider, ValueSlider, type SliderColor } from "@/components/interactive/sliders";

// ----------------------------------------------------------------------------- sheet

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;

/** A panel sliding in from the right (full width on a phone). */
export function SheetContent({
  title,
  description,
  children,
  className,
}: {
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay/60 backdrop-blur-[1px]" />
      <DialogPrimitive.Content
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex w-full flex-col gap-4 overflow-y-auto border-l bg-popover p-5 text-popover-foreground shadow-2xl sm:max-w-2xl sm:p-6",
          className,
        )}
      >
        <div className="grid gap-1 pr-8">
          <DialogPrimitive.Title className="text-base font-semibold tracking-tight">{title}</DialogPrimitive.Title>
          {description ? <DialogPrimitive.Description className="text-sm text-muted-foreground">{description}</DialogPrimitive.Description> : null}
        </div>
        {children}
        <DialogPrimitive.Close className="absolute top-4 right-4 rounded-sm p-1 text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
          <X className="size-4" />
          <span className="sr-only">Close</span>
        </DialogPrimitive.Close>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

// ----------------------------------------------------------------------------- badges

export type CheckStatus = "pass" | "warn" | "fail";

const STATUS_META: Record<CheckStatus, { label: string; className: string; icon: typeof CheckCircle2 }> = {
  pass: { label: "Pass", className: "border-success/30 bg-success/12 text-success", icon: CheckCircle2 },
  warn: { label: "Check", className: "border-warning/35 bg-warning/14 text-warning", icon: AlertTriangle },
  fail: { label: "Fail", className: "border-destructive/30 bg-destructive/12 text-destructive", icon: XCircle },
};

/** Pass / check / fail with an icon and a word, never colour alone. */
export function StatusBadge({ status, className }: { status: CheckStatus; className?: string }) {
  const meta = STATUS_META[status];
  const Icon = meta.icon;
  return (
    <span data-status={status} className={cn("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap", meta.className, className)}>
      <Icon aria-hidden className="size-3.5" />
      {meta.label}
    </span>
  );
}

// ----------------------------------------------------------------------------- sparkline

/** A tiny line of a series (decorative; `label` describes it for screen readers). */
export function Sparkline({ values, label, className }: { values: readonly number[]; label: string; className?: string }) {
  const w = 120;
  const h = 28;
  if (values.length < 2) return <span className="text-xs text-muted-foreground">—</span>;
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const span = hi - lo || 1;
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * w).toFixed(1)},${(h - 2 - ((v - lo) / span) * (h - 4)).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label} className={cn("h-7 w-[7.5rem] text-primary", className)} preserveAspectRatio="none">
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth={1.5} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

/** A labelled number in a card (headline figures). */
export function Figure({ label, value, detail, className }: { label: string; value: React.ReactNode; detail?: React.ReactNode; className?: string }) {
  return (
    <div className={cn("grid gap-1 rounded-lg border bg-card p-4", className)}>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="text-2xl leading-tight font-semibold tracking-tight tabular">{value}</p>
      {detail ? <p className="text-xs text-muted-foreground">{detail}</p> : null}
    </div>
  );
}
