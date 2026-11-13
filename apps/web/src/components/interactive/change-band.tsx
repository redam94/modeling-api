/**
 * A change with its 90% band on a symmetric scale around zero (Guided explore, the playgrounds).
 * Signed-in surfaces only (never in share or client chunks).
 */
import { cn } from "@/lib/utils";

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
    <div className="grid gap-1">
      <ChangeBandTrack lo={lo} mid={mid} hi={hi} exact={exact} label={label} x={x} width={width} tone={tone} />
      <div aria-hidden className="flex flex-wrap items-center justify-between gap-x-3 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className={cn("h-2 w-4 rounded-full opacity-40", tone)} /> instant estimate
        </span>
        <span>| no change</span>
        {exact ? (
          <span className="flex items-center gap-1.5">
            <span className="h-1.5 w-4 rounded-full bg-foreground/40" /> exact run
          </span>
        ) : (
          <span />
        )}
      </div>
    </div>
  );
}

function ChangeBandTrack({
  lo,
  mid,
  hi,
  exact,
  label,
  x,
  width,
  tone,
}: {
  lo: number;
  mid: number;
  hi: number;
  exact?: { lo: number; mid: number; hi: number } | null | undefined;
  label: string;
  x: (v: number) => string;
  width: (a: number, b: number) => string;
  tone: string;
}) {
  return (
    <div role="img" aria-label={label} className="relative h-12 w-full">
      <div aria-hidden className="absolute inset-x-0 top-5 h-px bg-border" />
      <div aria-hidden className="absolute top-2 bottom-2 w-px bg-muted-foreground/50" style={{ left: "50%" }} />
      <div
        aria-hidden
        className={cn("absolute top-3.5 h-3 rounded-full opacity-30 transition-[left,width] duration-240", tone)}
        style={{ left: x(Math.min(lo, hi)), width: width(lo, hi) }}
      />
      <div aria-hidden className={cn("absolute top-2.5 h-5 w-1 -translate-x-1/2 rounded-full transition-[left] duration-240", tone)} style={{ left: x(mid) }} />
      {exact ? (
        <>
          <div aria-hidden className="absolute top-8 h-1.5 rounded-full bg-foreground/35" style={{ left: x(Math.min(exact.lo, exact.hi)), width: width(exact.lo, exact.hi) }} />
          <div aria-hidden className="absolute top-7 h-3.5 w-0.5 -translate-x-1/2 rounded-full bg-foreground" style={{ left: x(exact.mid) }} />
        </>
      ) : null}
    </div>
  );
}
