/**
 * Small building blocks of the Guided screens, in the shadcn/ui new-york style of the rest of
 * the app: sliders, a side sheet, check badges, sparklines and an uncertainty band.
 */
import { AlertTriangle, CheckCircle2, X, XCircle } from "lucide-react";
import { Dialog as DialogPrimitive, Slider as SliderPrimitive } from "radix-ui";
import type * as React from "react";

import { cn } from "@/lib/utils";

// ----------------------------------------------------------------------------- sliders

const track = "relative h-1.5 w-full grow overflow-hidden rounded-full bg-muted";
const thumb =
  "block size-4 rounded-full border-2 border-primary bg-card shadow-sm transition-[box-shadow] focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/40 disabled:pointer-events-none";

/** One value on a scale (what-if multipliers, horizons). */
export function ValueSlider({
  value,
  onValueChange,
  min,
  max,
  step,
  label,
  valueText,
  className,
  disabled,
}: {
  value: number;
  onValueChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  valueText?: string;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <SliderPrimitive.Root
      className={cn("relative flex h-6 w-full touch-none items-center select-none", className)}
      value={[value]}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      onValueChange={(v) => onValueChange(v[0] ?? value)}
    >
      <SliderPrimitive.Track className={track}>
        <SliderPrimitive.Range className="absolute h-full bg-primary" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className={thumb} aria-label={label} {...(valueText ? { "aria-valuetext": valueText } : {})} />
    </SliderPrimitive.Root>
  );
}

/** A low-high range on a scale (ROI or share beliefs). */
export function RangeSlider({
  value,
  onValueChange,
  min,
  max,
  step,
  label,
  format,
  disabled,
}: {
  value: [number, number];
  onValueChange: (v: [number, number]) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  format: (v: number) => string;
  disabled?: boolean;
}) {
  return (
    <SliderPrimitive.Root
      className="relative flex h-6 w-full touch-none items-center select-none data-[disabled]:opacity-50"
      value={value}
      min={min}
      max={max}
      step={step}
      minStepsBetweenThumbs={1}
      disabled={disabled}
      onValueChange={(v) => onValueChange([v[0] ?? value[0], v[1] ?? value[1]])}
    >
      <SliderPrimitive.Track className={track}>
        <SliderPrimitive.Range className="absolute h-full bg-primary" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className={thumb} aria-label={`${label}: low end`} aria-valuetext={format(value[0])} />
      <SliderPrimitive.Thumb className={thumb} aria-label={`${label}: high end`} aria-valuetext={format(value[1])} />
    </SliderPrimitive.Root>
  );
}

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
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/30 backdrop-blur-[1px]" />
      <DialogPrimitive.Content
        className={cn(
          "fixed inset-y-0 right-0 z-50 flex w-full flex-col gap-4 overflow-y-auto border-l bg-popover p-5 text-popover-foreground shadow-2xl sm:max-w-2xl sm:p-6",
          className,
        )}
      >
        <div className="grid gap-1 pr-8">
          <DialogPrimitive.Title className="text-base font-semibold tracking-tight">{title}</DialogPrimitive.Title>
          {description ? <DialogPrimitive.Description className="text-[13px] text-muted-foreground">{description}</DialogPrimitive.Description> : null}
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

// ----------------------------------------------------------------------------- uncertainty band

/**
 * A value with its 90% band on a symmetric scale around zero (what-if changes): the band is
 * where 90% of plausible outcomes fall, the mark the average. `exact` adds the exact run's
 * band underneath for comparison.
 */
export function ChangeBand({
  lo,
  mid,
  hi,
  exact,
  label,
}: {
  lo: number;
  mid: number;
  hi: number;
  exact?: { lo: number; mid: number; hi: number } | null;
  label: string;
}) {
  const extent = Math.max(Math.abs(lo), Math.abs(hi), Math.abs(exact?.lo ?? 0), Math.abs(exact?.hi ?? 0), 1e-9) * 1.15;
  const x = (v: number) => `${((v + extent) / (2 * extent)) * 100}%`;
  const width = (a: number, b: number) => `${(Math.abs(b - a) / (2 * extent)) * 100}%`;
  const tone = mid >= 0 ? "bg-success" : "bg-destructive";
  return (
    <div role="img" aria-label={label} className="relative h-12 w-full">
      <div aria-hidden className="absolute inset-x-0 top-5 h-px bg-border" />
      <div aria-hidden className="absolute top-2 bottom-2 w-px bg-muted-foreground/50" style={{ left: "50%" }} />
      <div aria-hidden className={cn("absolute top-3.5 h-3 rounded-full opacity-30", tone)} style={{ left: x(Math.min(lo, hi)), width: width(lo, hi) }} />
      <div aria-hidden className={cn("absolute top-2.5 h-5 w-1 -translate-x-1/2 rounded-full", tone)} style={{ left: x(mid) }} />
      {exact ? (
        <>
          <div aria-hidden className="absolute top-8 h-1.5 rounded-full bg-foreground/35" style={{ left: x(Math.min(exact.lo, exact.hi)), width: width(exact.lo, exact.hi) }} />
          <div aria-hidden className="absolute top-7 h-3.5 w-0.5 -translate-x-1/2 rounded-full bg-foreground" style={{ left: x(exact.mid) }} />
        </>
      ) : null}
    </div>
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
