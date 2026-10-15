/**
 * Small, dependency-free SVG charts for usage and admin views. Every chart carries a text
 * summary (`aria-label`) and the exact values in a visually hidden table, so screen readers
 * and keyboard users get the numbers, not a picture. Plots of *model* data still go through
 * the isolated plot sandbox; these only show the platform's own counters.
 */
import { useId, useMemo, useState } from "react";

import { cn } from "@/lib/utils";

export interface BarDatum {
  key: string;
  /** Axis label (short). */
  label: string;
  value: number;
  /** Tooltip / table label (long); defaults to `label`. */
  title?: string;
}

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(v));
  const f = v / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return nice * exp;
}

/** Vertical bars over a category axis (days, classes). */
export function BarChart({
  data,
  format,
  axisFormat,
  label,
  height = 180,
  className,
}: {
  data: BarDatum[];
  format: (v: number) => string;
  axisFormat?: (v: number) => string;
  label: string;
  height?: number;
  className?: string;
}) {
  const id = useId();
  const [hover, setHover] = useState<number | null>(null);
  const max = useMemo(() => niceMax(Math.max(0, ...data.map((d) => d.value))), [data]);
  const ticks = [0, max / 2, max];
  const fmtAxis = axisFormat ?? format;
  const W = 640;
  const H = height;
  const left = 56;
  const bottom = 22;
  const top = 8;
  const plotW = W - left - 8;
  const plotH = H - top - bottom;
  const n = Math.max(1, data.length);
  const band = plotW / n;
  const barW = Math.max(2, Math.min(28, band * 0.7));
  const labelEvery = Math.max(1, Math.ceil(n / 12));
  const total = data.reduce((a, d) => a + d.value, 0);
  const peak = data.reduce<BarDatum | null>((a, d) => (a === null || d.value > a.value ? d : a), null);
  const summary = `${label}: ${data.length} bars, total ${format(total)}${peak && peak.value > 0 ? `, highest ${format(peak.value)} on ${peak.title ?? peak.label}` : ""}.`;
  const hovered = hover !== null ? data[hover] : undefined;

  return (
    <figure className={cn("grid gap-2", className)}>
      <div className="relative">
        <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={summary} aria-describedby={`${id}-table`}>
          {ticks.map((t) => {
            const y = top + plotH - (t / max) * plotH;
            return (
              <g key={t}>
                <line x1={left} x2={W - 8} y1={y} y2={y} className="stroke-border" strokeDasharray={t === 0 ? undefined : "3 4"} />
                <text x={left - 8} y={y + 4} textAnchor="end" className="fill-muted-foreground text-[11px]">
                  {fmtAxis(t)}
                </text>
              </g>
            );
          })}
          {data.map((d, i) => {
            const h = (d.value / max) * plotH;
            const x = left + i * band + (band - barW) / 2;
            return (
              <g key={d.key} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
                <rect x={left + i * band} y={top} width={band} height={plotH} className="fill-transparent" />
                <rect
                  x={x}
                  y={top + plotH - h}
                  width={barW}
                  height={Math.max(d.value > 0 ? 1.5 : 0, h)}
                  rx={Math.min(3, barW / 3)}
                  className={cn("fill-primary transition-opacity", hover !== null && hover !== i && "opacity-40")}
                />
                {i % labelEvery === 0 ? (
                  <text x={left + i * band + band / 2} y={H - 6} textAnchor="middle" className="fill-muted-foreground text-[11px]">
                    {d.label}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>
        {hovered ? (
          <div
            aria-hidden
            className="pointer-events-none absolute top-1 right-2 rounded-md border bg-popover px-2 py-1 text-xs shadow-sm"
          >
            <span className="text-muted-foreground">{hovered.title ?? hovered.label}</span>{" "}
            <span className="font-medium tabular">{format(hovered.value)}</span>
          </div>
        ) : null}
      </div>
      <table id={`${id}-table`} className="sr-only">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th scope="col">Period</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map((d) => (
            <tr key={d.key}>
              <th scope="row">{d.title ?? d.label}</th>
              <td>{format(d.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** Ranked horizontal bars (cost by project, token or class). */
export function BarList({
  items,
  format,
  label,
  empty = "Nothing recorded in this period.",
}: {
  items: { key: string; label: React.ReactNode; value: number; sub?: React.ReactNode }[];
  format: (v: number) => string;
  label: string;
  empty?: string;
}) {
  const sorted = [...items].sort((a, b) => b.value - a.value);
  const max = Math.max(0, ...sorted.map((i) => i.value));
  if (!sorted.length) return <p className="text-muted-foreground">{empty}</p>;
  return (
    <ul aria-label={label} className="grid gap-2.5">
      {sorted.map((i) => (
        <li key={i.key} className="grid gap-1">
          <div className="flex items-baseline justify-between gap-3 text-[13px]">
            <span className="min-w-0 truncate">{i.label}</span>
            <span className="shrink-0 font-medium tabular">{format(i.value)}</span>
          </div>
          <div aria-hidden className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary/80" style={{ width: `${max > 0 ? Math.max(1.5, (i.value / max) * 100) : 0}%` }} />
          </div>
          {i.sub ? <div className="text-xs text-muted-foreground">{i.sub}</div> : null}
        </li>
      ))}
    </ul>
  );
}

export type MeterTone = "ok" | "warn" | "critical";

/** 70 % warns, 90 % is critical (plan §8.8 alerts at 70 %). */
export function meterTone(fraction: number | null | undefined): MeterTone {
  if (fraction == null) return "ok";
  if (fraction >= 0.9) return "critical";
  if (fraction >= 0.7) return "warn";
  return "ok";
}

const TONE_BAR: Record<MeterTone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  critical: "bg-destructive",
};

/** A quota gauge: used of limit, coloured by {@link meterTone}. */
export function Meter({
  label,
  fraction,
  valueText,
  className,
}: {
  label: string;
  fraction: number | null;
  valueText: string;
  className?: string;
}) {
  const pct = fraction == null ? 0 : Math.round(Math.min(1, Math.max(0, fraction)) * 100);
  const tone = meterTone(fraction);
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      aria-valuetext={valueText}
      data-tone={tone}
      className={cn("relative h-2 w-full overflow-hidden rounded-full bg-muted", className)}
    >
      <div className={cn("h-full rounded-full transition-[width]", TONE_BAR[tone])} style={{ width: `${fraction == null ? 0 : Math.max(pct, fraction > 0 ? 1 : 0)}%` }} />
      {/* threshold ticks */}
      <span aria-hidden className="absolute top-0 bottom-0 left-[70%] w-px bg-background/80" />
      <span aria-hidden className="absolute top-0 bottom-0 left-[90%] w-px bg-background/80" />
    </div>
  );
}
