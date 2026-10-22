/**
 * The readout charts of layout 2 dashboards (v0.17, plan D18 and §3.7): bars, a scatter, a
 * calibration chart, a confusion grid, a field map, a small table and a chart of several lines.
 * Like `readout-charts.tsx` they are dependency-free SVG, a canvas and plain HTML drawn with
 * token classes only (no Vega, Plotly or iframe on client and share pages), and the readout
 * loads them lazily, so a dashboard without these panels does not download them.
 *
 * Accessibility: every chart is `role="img"` with an accessible name that carries the numbers
 * (counts, extremes, the latest values), and the values are also in a visually hidden table.
 * The field map paints uint8 codes through a colour lookup table with `putImageData` (no
 * markup is built from text anywhere here).
 */
import { useEffect, useId, useRef, useState } from "react";

import { axisDate, dmy, fixed, grouped, MISSING } from "@/components/ods/format";
import { Legend, type LegendColor } from "@/components/ods/legend";
import { niceDomain, type ChartPeriod } from "@/components/readout-charts";
import { colormapLut, gradientCss, type ColormapName } from "@/features/explore/fields";
import { cn } from "@/lib/utils";

type Num = number | null | undefined;
const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const r2 = (v: number) => Math.round(v * 100) / 100;
const AXIS_TEXT = "tabular fill-hint text-2xs";
const plainFormat = (v: number) => (Math.abs(v) >= 100 || Number.isInteger(v) ? grouped(v, 0) : fixed(v, 2));

function useContainerWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(Math.round(el.getBoundingClientRect().width));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width > 0 ? width : fallback] as const;
}

function scale(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

// ----------------------------------------------------------------------- Bars

export interface BarItem {
  label: string;
  value: number | null;
  low?: number | null;
  high?: number | null;
  /** A short word after the value ("above 5.0"). */
  tag?: string | null;
}

/**
 * Horizontal bars, one row per item (label | bar | value), with the likely range as a thin
 * line over the bar. Negative values go left of zero. On phones the bar moves under its label.
 */
export function Bars({ label, rows, format = plainFormat, className }: { label: string; rows: readonly BarItem[]; format?: (v: number) => string; className?: string }) {
  const id = useId();
  const nums = rows.flatMap((r) => [r.value, r.low, r.high]).filter(isNum);
  const lo = Math.min(0, ...nums);
  const hi = Math.max(0, ...nums, 1e-12);
  const pos = (v: number) => clamp(((v - lo) / (hi - lo || 1)) * 100, 0, 100);
  const zero = pos(0);
  const top = rows.filter((r) => isNum(r.value)).reduce<BarItem | null>((a, r) => (a === null || (r.value as number) > (a.value as number) ? r : a), null);
  const summary = `${label}: ${rows.length} ${rows.length === 1 ? "bar" : "bars"}.${top ? ` Highest ${top.label} ${format(top.value as number)}.` : ""} ${rows
    .slice(0, 12)
    .map((r) => `${r.label} ${isNum(r.value) ? format(r.value) : MISSING}`)
    .join(", ")}${rows.length > 12 ? ", and more" : ""}.`;
  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <div role="img" aria-label={summary} aria-describedby={`${id}-table`} data-chart="bars" className="flex flex-col gap-2.5">
        {rows.map((r, i) => {
          const v = isNum(r.value) ? r.value : null;
          const left = v === null ? zero : Math.min(zero, pos(v));
          const width = v === null ? 0 : Math.abs(pos(v) - zero);
          return (
            <div key={`${r.label}-${i}`} aria-hidden className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,200px)_minmax(0,1fr)_auto]">
              <span className="truncate text-sm text-foreground">{r.label}</span>
              <span className="relative h-3 min-w-0 max-sm:order-last max-sm:col-span-2">
                <span className="absolute inset-y-0 w-px bg-chart-base" style={{ left: `${zero}%` }} />
                {v !== null ? <span className={cn("absolute inset-y-0.5 rounded-sm", v < 0 ? "bg-chart-loss" : "bg-chart-index")} style={{ left: `${left}%`, width: `${Math.max(0.5, width)}%` }} /> : null}
                {isNum(r.low) && isNum(r.high) ? <span className="absolute top-[5px] h-0.5 bg-chart-net" style={{ left: `${pos(r.low)}%`, width: `${Math.max(0.5, pos(r.high) - pos(r.low))}%` }} data-range /> : null}
              </span>
              <span className="text-sm font-semibold text-foreground tabular">
                {v === null ? MISSING : format(v)}
                {r.tag ? <span className="ml-1 font-normal text-hint">{r.tag}</span> : null}
              </span>
            </div>
          );
        })}
      </div>
      {/* a wrapper clips it: a table ignores the 1px box of sr-only and would scroll a phone sideways */}
      <div className="sr-only">
        <table id={`${id}-table`}>
          <caption>{label}</caption>
          <thead>
            <tr>
              <th scope="col">Item</th>
              <th scope="col">Value</th>
              <th scope="col">Likely range</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.label}-${i}`}>
                <th scope="row">{r.label}</th>
                <td>
                  {isNum(r.value) ? format(r.value) : MISSING}
                  {r.tag ? ` ${r.tag}` : ""}
                </td>
                <td>{isNum(r.low) && isNum(r.high) ? `${format(r.low)} to ${format(r.high)}` : MISSING}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

// ----------------------------------------------------------------------- Scatter

const SC = { H: 260, T: 12, R: 12, B: 228 } as const;

/** Points against two axes; `diagonal` draws the line where y equals x (predicted against actual). */
export function Scatter({
  label,
  points,
  xLabel = "x",
  yLabel = "y",
  diagonal = false,
  format = plainFormat,
  className,
}: {
  label: string;
  points: readonly { x: number; y: number }[];
  xLabel?: string;
  yLabel?: string;
  diagonal?: boolean;
  format?: (v: number) => string;
  className?: string;
}) {
  const id = useId();
  const [boxRef, W0] = useContainerWidth<HTMLDivElement>(480);
  const W = Math.max(260, W0);
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const dx = niceDomain(diagonal ? [...xs, ...ys] : xs);
  const dy = diagonal ? dx : niceDomain(ys);
  const L = clamp(Math.max(...dy.ticks.map((t) => format(t).length)) * 6.5 + 14, 40, 96);
  const x = scale(dx.lo, dx.hi, L, W - SC.R);
  const y = scale(dy.lo, dy.hi, SC.B, SC.T);
  const summary = points.length
    ? `${label}: ${points.length} points. ${xLabel} from ${format(Math.min(...xs))} to ${format(Math.max(...xs))}; ${yLabel} from ${format(Math.min(...ys))} to ${format(Math.max(...ys))}.${diagonal ? " Points on the line are exact." : ""}`
    : `${label}: no points.`;
  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <div ref={boxRef} className="min-w-0">
        <svg width={W} height={SC.H} viewBox={`0 0 ${W} ${SC.H}`} role="img" aria-label={summary} aria-describedby={`${id}-table`} data-chart="scatter" className="block max-w-full">
          {dy.ticks.map((v) => (
            <g key={`y${v}`}>
              <line x1={L} x2={W - SC.R} y1={r2(y(v))} y2={r2(y(v))} className="stroke-chart-grid" />
              <text x={L - 8} y={r2(y(v) + 4)} textAnchor="end" className={AXIS_TEXT}>
                {format(v)}
              </text>
            </g>
          ))}
          {dx.ticks.map((v) => (
            <text key={`x${v}`} x={r2(x(v))} y={SC.H - 14} textAnchor="middle" className={AXIS_TEXT}>
              {format(v)}
            </text>
          ))}
          {diagonal ? <line x1={r2(x(dx.lo))} y1={r2(y(dx.lo))} x2={r2(x(dx.hi))} y2={r2(y(dx.hi))} className="stroke-chart-ref" strokeDasharray="4 3" data-ref /> : null}
          {points.map((p, i) => (
            <circle key={i} cx={r2(x(p.x))} cy={r2(y(p.y))} r={3} className="fill-chart-index opacity-70" />
          ))}
        </svg>
      </div>
      <p aria-hidden className="text-2xs text-hint">
        Across: {xLabel}. Up: {yLabel}.
      </p>
      {/* a wrapper clips it: a table ignores the 1px box of sr-only and would scroll a phone sideways */}
      <div className="sr-only">
        <table id={`${id}-table`}>
          <caption>{label}</caption>
          <thead>
            <tr>
              <th scope="col">{xLabel}</th>
              <th scope="col">{yLabel}</th>
            </tr>
          </thead>
          <tbody>
            {points.slice(0, 200).map((p, i) => (
              <tr key={i}>
                <td>{format(p.x)}</td>
                <td>{format(p.y)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

// ----------------------------------------------------------------------- Calibration

/** Calibration: the mean probability of each bin against how often the result occurred. */
export function Calibration({ label, bins, className }: { label: string; bins: readonly { p_mean: number; observed: number; n: number }[]; className?: string }) {
  const id = useId();
  const S = 240;
  const P = 36;
  // the right margin holds half of the "100%" label centred on the last tick (never clipped)
  const R = 20;
  const x = scale(0, 1, P, S - R);
  const y = scale(0, 1, S - P, 8);
  const maxN = Math.max(1, ...bins.map((b) => b.n));
  const pct = (v: number) => `${fixed(v * 100, 0)}%`;
  const worst = bins.reduce<{ p_mean: number; observed: number; n: number } | null>((a, b) => (a === null || Math.abs(b.observed - b.p_mean) > Math.abs(a.observed - a.p_mean) ? b : a), null);
  const summary = bins.length
    ? `${label}: ${bins.length} groups of cases. ${worst ? `The largest gap: the model said ${pct(worst.p_mean)}, and the result occurred in ${pct(worst.observed)} of ${worst.n} cases.` : ""} On the dashed line the probabilities are exact.`
    : `${label}: no groups.`;
  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <svg width={S} height={S} viewBox={`0 0 ${S} ${S}`} role="img" aria-label={summary} aria-describedby={`${id}-table`} data-chart="calibration" className="block max-w-full">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={P} x2={S - R} y1={r2(y(t))} y2={r2(y(t))} className="stroke-chart-grid" />
            <text x={P - 6} y={r2(y(t) + 4)} textAnchor="end" className={AXIS_TEXT}>
              {pct(t)}
            </text>
            <text x={r2(x(t))} y={S - P + 16} textAnchor="middle" className={AXIS_TEXT}>
              {pct(t)}
            </text>
          </g>
        ))}
        <line x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} className="stroke-chart-ref" strokeDasharray="4 3" data-ref />
        {bins.map((b, i) => (
          <circle key={i} cx={r2(x(clamp(b.p_mean, 0, 1)))} cy={r2(y(clamp(b.observed, 0, 1)))} r={r2(3 + 5 * Math.sqrt(b.n / maxN))} className="fill-chart-index stroke-card opacity-80" strokeWidth={1} />
        ))}
      </svg>
      <p aria-hidden className="text-2xs text-hint">Across: the probability the model gave. Up: how often the result occurred.</p>
      {/* a wrapper clips it: a table ignores the 1px box of sr-only and would scroll a phone sideways */}
      <div className="sr-only">
        <table id={`${id}-table`}>
          <caption>{label}</caption>
          <thead>
            <tr>
              <th scope="col">Probability the model gave</th>
              <th scope="col">How often it occurred</th>
              <th scope="col">Cases</th>
            </tr>
          </thead>
          <tbody>
            {bins.map((b, i) => (
              <tr key={i}>
                <td>{pct(b.p_mean)}</td>
                <td>{pct(b.observed)}</td>
                <td>{b.n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

// ----------------------------------------------------------------------- ConfusionGrid

/**
 * The confusion grid: a real table (rows: the actual class, columns: the predicted class), the
 * cells shaded by their share of the row. The diagonal holds the right predictions.
 */
export function ConfusionGrid({ label, labels, counts, className }: { label: string; labels: readonly string[]; counts: readonly (readonly number[])[]; className?: string }) {
  const total = counts.reduce((a, r) => a + r.reduce((s, c) => s + c, 0), 0);
  const right = labels.reduce((a, _, i) => a + (counts[i]?.[i] ?? 0), 0);
  return (
    <figure className={cn("grid min-w-0 gap-2", className)} data-chart="confusion">
      <div className="relative max-w-full overflow-x-auto" data-slot="scroll-x">
        <table className="border-collapse text-sm">
          <caption className="sr-only">
            {label}: {right} of {total} cases were predicted right. Rows are the actual class, columns the predicted class.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="px-2 py-1 text-left text-xs font-medium text-hint">
                Actual ↓ / Predicted →
              </th>
              {labels.map((l) => (
                <th key={l} scope="col" className="px-2 py-1 text-xs font-medium text-hint">
                  {l}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {labels.map((row, i) => {
              const sum = (counts[i] ?? []).reduce((s, c) => s + c, 0) || 1;
              return (
                <tr key={row}>
                  <th scope="row" className="px-2 py-1 text-left text-xs font-medium text-hint">
                    {row}
                  </th>
                  {labels.map((col, j) => {
                    const c = counts[i]?.[j] ?? 0;
                    return (
                      <td key={col} className="relative min-w-12 px-2 py-1.5 text-center tabular" data-diagonal={i === j || undefined}>
                        <span aria-hidden className={cn("absolute inset-0.5 rounded-sm", i === j ? "bg-chart-gain" : "bg-chart-loss")} style={{ opacity: 0.08 + 0.5 * (c / sum) }} />
                        <span className="relative font-semibold text-foreground">{c}</span>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-hint">
        {right} of {total} cases were right ({total ? fixed((right / total) * 100, 1) : MISSING}%).
      </p>
    </figure>
  );
}

// ----------------------------------------------------------------------- FieldHeatmap

export interface FieldFrameCodes {
  label: string;
  codes_b64: string;
}

/** The uint8 codes of a frame (`null` for a frame that does not decode to width x height cells). */
export function decodeCodes(b64: string, cells: number): Uint8Array | null {
  try {
    const bin = atob(b64);
    if (bin.length !== cells) return null;
    const out = new Uint8Array(cells);
    for (let i = 0; i < cells; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return null;
  }
}

/**
 * Code `c` as a value: `vmin + c / top (vmax − vmin)`, back from log10 on a log map. `top` is 255 for
 * a block without a missing code and 254 for one with it (code 255 is then a cell with no value).
 */
export function codeValue(c: number, vmin: number, vmax: number, log: boolean, top = 255): number {
  const v = vmin + (c / top) * (vmax - vmin);
  return log ? 10 ** v : v;
}

/** The highest value code of a block: 254 when 255 marks a missing cell, else 255. */
export function topCode(missingCode: number | null | undefined): number {
  return missingCode === 255 ? 254 : 255;
}

/**
 * Paint codes to RGBA through the colour map's lookup table. A missing cell (`missingCode`) is transparent; value
 * codes are stretched over the whole colour map.
 */
export function paintCodes(codes: Uint8Array, colormap: ColormapName, missingCode: number | null = null): Uint8ClampedArray<ArrayBuffer> {
  const lut = colormapLut(colormap);
  const top = topCode(missingCode);
  const out = new Uint8ClampedArray(new ArrayBuffer(codes.length * 4));
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i] ?? 0;
    if (missingCode !== null && c === missingCode) continue; // transparent: no value here
    const k = 3 * Math.round((Math.min(c, top) / top) * 255);
    out[4 * i] = lut[k] ?? 0;
    out[4 * i + 1] = lut[k + 1] ?? 0;
    out[4 * i + 2] = lut[k + 2] ?? 0;
    out[4 * i + 3] = 255;
  }
  return out;
}

/** Lowest, average and highest value of a frame's codes, leaving missing cells out (`null` when none has a value). */
export function codeStats(
  codes: Uint8Array,
  vmin: number,
  vmax: number,
  log: boolean,
  missingCode: number | null = null,
): { min: number | null; mean: number | null; max: number | null; missing: number } {
  const top = topCode(missingCode);
  let mn = 255;
  let mx = -1;
  let sum = 0;
  let n = 0;
  for (const v of codes) {
    if (missingCode !== null && v === missingCode) continue;
    mn = Math.min(mn, v);
    mx = Math.max(mx, v);
    sum += codeValue(v, vmin, vmax, log, top);
    n++;
  }
  if (!n) return { min: null, mean: null, max: null, missing: codes.length };
  return { min: codeValue(mn, vmin, vmax, log, top), mean: sum / n, max: codeValue(mx, vmin, vmax, log, top), missing: codes.length - n };
}

/** A field map: ≤ 64 x 64 cells per frame, ≤ 8 frames behind a slider, and a colour legend. */
export function FieldHeatmap({
  label,
  width,
  height,
  frames,
  vmin,
  vmax,
  missingCode = null,
  log = false,
  colormap = "viridis",
  units,
  xLabel,
  yLabel,
  className,
}: {
  label: string;
  width: number;
  height: number;
  frames: readonly FieldFrameCodes[];
  vmin: number;
  vmax: number;
  /** The code of a cell with no value (255), or none (a block built before the missing code). */
  missingCode?: number | null;
  log?: boolean;
  colormap?: ColormapName;
  units?: string | null;
  xLabel?: string | null;
  yLabel?: string | null;
  className?: string;
}) {
  const id = useId();
  const [at, setAt] = useState(0);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const frame = frames[Math.min(at, frames.length - 1)];
  const cells = width * height;
  const codes = frame ? decodeCodes(frame.codes_b64, cells) : null;
  const fmt = (v: number) => `${plainFormat(v)}${units ? ` ${units}` : ""}`;
  const top = topCode(missingCode);
  const lo = codeValue(0, vmin, vmax, log, top);
  const hi = codeValue(top, vmin, vmax, log, top);
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || !codes) return;
    let ctx: CanvasRenderingContext2D | null = null;
    try {
      ctx = el.getContext("2d");
    } catch {
      ctx = null; // jsdom has no canvas
    }
    if (!ctx) return;
    ctx.putImageData(new ImageData(paintCodes(codes, colormap, missingCode), width, height), 0, 0);
  }, [codes, colormap, width, height, missingCode]);
  const stats = frames.map((f) => {
    const c = decodeCodes(f.codes_b64, cells);
    if (!c?.length) return { label: f.label, min: null, mean: null, max: null, missing: 0 };
    return { label: f.label, ...codeStats(c, vmin, vmax, log, missingCode) };
  });
  const cur = stats[Math.min(at, stats.length - 1)];
  const gaps = cur && cur.missing > 0 ? `; ${cur.missing} cells have no value` : "";
  const summary = `${label}${frame ? `, ${frame.label}` : ""}: a map of ${width} by ${height} cells. Values from ${fmt(lo)} to ${fmt(hi)}${cur && isNum(cur.mean) ? `; the average is ${fmt(cur.mean)}` : ""}${gaps}.`;
  return (
    <figure className={cn("grid min-w-0 gap-3", className)} data-chart="field">
      <canvas
        ref={canvasRef}
        width={width}
        height={height}
        role="img"
        aria-label={summary}
        aria-describedby={`${id}-table`}
        className="block aspect-square h-auto w-full max-w-[min(100%,24rem)] rounded-sm border [image-rendering:pixelated]"
        style={{ aspectRatio: `${width} / ${height}` }}
      />
      {xLabel || yLabel ? (
        <p aria-hidden className="text-2xs text-hint">
          {xLabel ? `Across: ${xLabel}.` : ""} {yLabel ? `Up: ${yLabel}.` : ""}
        </p>
      ) : null}
      <div aria-hidden className="grid max-w-sm gap-1">
        <span className="h-2.5 rounded-sm" style={{ background: gradientCss(colormap) }} />
        <span className="flex justify-between text-2xs text-hint tabular">
          <span>{fmt(lo)}</span>
          <span>{fmt(hi)}</span>
        </span>
      </div>
      {frames.length > 1 ? (
        <label className="grid max-w-sm gap-1 text-sm">
          <span className="text-hint">
            Frame: <span className="font-semibold text-foreground">{frame?.label}</span>
          </span>
          <input
            type="range"
            min={0}
            max={frames.length - 1}
            step={1}
            value={at}
            onChange={(e) => setAt(Number(e.target.value))}
            aria-valuetext={frame?.label ?? ""}
            className="accent-primary"
          />
        </label>
      ) : null}
      {/* a wrapper clips it: a table ignores the 1px box of sr-only and would scroll a phone sideways */}
      <div className="sr-only">
        <table id={`${id}-table`}>
          <caption>{label}</caption>
          <thead>
            <tr>
              <th scope="col">Frame</th>
              <th scope="col">Lowest</th>
              <th scope="col">Average</th>
              <th scope="col">Highest</th>
            </tr>
          </thead>
          <tbody>
            {stats.map((s, i) => (
              <tr key={`${s.label}-${i}`}>
                <th scope="row">{s.label}</th>
                <td>{isNum(s.min) ? fmt(s.min) : MISSING}</td>
                <td>{isNum(s.mean) ? fmt(s.mean) : MISSING}</td>
                <td>{isNum(s.max) ? fmt(s.max) : MISSING}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}

// ----------------------------------------------------------------------- SmallTable

/** A small table of text and numbers (≤ 12 columns, ≤ 50 rows), scrolling sideways on phones. */
export function SmallTable({
  label,
  columns,
  rows,
  format = plainFormat,
  className,
}: {
  label: string;
  columns: readonly string[];
  rows: readonly (readonly (string | number | boolean | null)[])[];
  format?: (v: number) => string;
  className?: string;
}) {
  const cell = (v: string | number | boolean | null | undefined) => (v === null || v === undefined || v === "" ? MISSING : typeof v === "number" ? format(v) : String(v));
  // a column of numbers is right-aligned, its header too (so the header sits over its digits)
  const numeric = columns.map((_, j) => rows.some((r) => typeof r[j] === "number"));
  return (
    <div className={cn("relative max-w-full overflow-x-auto rounded-lg border", className)} data-slot="scroll-x" data-chart="table">
      <table className="w-max min-w-full border-collapse text-sm">
        <caption className="sr-only">{label}</caption>
        <thead>
          <tr className="border-b bg-muted/50 text-left text-xs text-hint">
            {columns.map((c, j) => (
              <th key={c} scope="col" data-numeric={numeric[j] ? "" : undefined} className={cn("px-3 py-2 font-medium", numeric[j] && "text-right")}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b last:border-b-0">
              {columns.map((c, j) => (
                <td key={c} className={cn("px-3 py-1.5", typeof r[j] === "number" && "text-right tabular")}>
                  {cell(r[j])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ----------------------------------------------------------------------- MultiLine

export interface LineSeries {
  label: string;
  points: readonly { t: string; mean?: number | null; low?: number | null; high?: number | null; actual?: number | null }[];
}

const LINE_STROKE = ["stroke-series-1", "stroke-series-2", "stroke-series-3", "stroke-series-4", "stroke-series-5", "stroke-series-6", "stroke-series-7", "stroke-series-8"] as const;
const PLURAL: Record<string, string> = { week: "weeks", month: "months", period: "periods", day: "days", quarter: "quarters", year: "years", case: "cases" };

/** Several lines on one axis (≤ 8 coloured; more series are in the table only). */
export function MultiLine({
  label,
  series,
  format = plainFormat,
  period = "period",
  className,
}: {
  label: string;
  series: readonly LineSeries[];
  format?: (v: number) => string;
  period?: ChartPeriod;
  className?: string;
}) {
  const id = useId();
  const [boxRef, W0] = useContainerWidth<HTMLDivElement>(640);
  const W = Math.max(280, W0);
  const H = 240;
  const T = 12;
  const B = 214;
  const shown = series.slice(0, 8);
  const ts = [...new Set(shown.flatMap((s) => s.points.map((p) => p.t)))];
  const tAt = new Map(ts.map((t, i) => [t, i]));
  const dom = niceDomain(shown.flatMap((s) => s.points.map((p) => p.mean)));
  const L = clamp(Math.max(...dom.ticks.map((t) => format(t).length)) * 6.5 + 12, 44, 96);
  const step = (W - L - 12) / Math.max(1, ts.length);
  const x = (j: number) => L + step * (j + 0.5);
  const y = scale(dom.lo, dom.hi, B, T);
  const every = Math.max(1, Math.ceil((ts.length * 56) / Math.max(1, W - L - 12)));
  const tick = axisDate(ts);
  const path = (s: LineSeries) => {
    let d = "";
    let pen = false;
    for (const p of s.points) {
      if (!isNum(p.mean)) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${r2(x(tAt.get(p.t) ?? 0))} ${r2(y(p.mean))}`;
      pen = true;
    }
    return d;
  };
  const last = (s: LineSeries) => [...s.points].reverse().find((p) => isNum(p.mean));
  const summary = `${label}: ${series.length} lines over ${ts.length} ${ts.length === 1 ? period : (PLURAL[period] ?? `${period}s`)}${ts.length ? ` to ${dmy(ts[ts.length - 1] ?? "")}` : ""}. Latest: ${shown
    .map((s) => `${s.label} ${isNum(last(s)?.mean) ? format(last(s)!.mean as number) : MISSING}`)
    .join(", ")}.`;
  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <div ref={boxRef} className="min-w-0">
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} aria-describedby={`${id}-table`} data-chart="multi-line" className="block max-w-full">
          {dom.ticks.map((v) => (
            <g key={v}>
              <line x1={L} x2={W - 12} y1={r2(y(v))} y2={r2(y(v))} className="stroke-chart-grid" />
              <text x={L - 8} y={r2(y(v) + 4)} textAnchor="end" className={AXIS_TEXT}>
                {format(v)}
              </text>
            </g>
          ))}
          {shown.map((s, i) => (
            <path key={s.label} d={path(s)} className={cn("fill-none", LINE_STROKE[i])} strokeWidth={1.75} strokeLinejoin="round" />
          ))}
          {ts.map((t, j) =>
            (ts.length - 1 - j) % every === 0 ? (
              <text key={t} x={r2(x(j))} y={H - 6} textAnchor="middle" className={AXIS_TEXT}>
                {tick(t)}
              </text>
            ) : null,
          )}
        </svg>
      </div>
      <Legend items={shown.map((s, i) => ({ label: s.label, color: `series-${i + 1}` as LegendColor, swatch: "line" as const }))} />
      {/* a wrapper clips it: a table ignores the 1px box of sr-only and would scroll a phone sideways */}
      <div className="sr-only">
        <table id={`${id}-table`}>
          <caption>{label}</caption>
          <thead>
            <tr>
              <th scope="col">Period</th>
              {series.map((s) => (
                <th key={s.label} scope="col">
                  {s.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ts.map((t) => (
              <tr key={t}>
                <th scope="row">{dmy(t)}</th>
                {series.map((s) => {
                  const p = s.points.find((q) => q.t === t);
                  return <td key={s.label}>{isNum(p?.mean) ? format(p.mean) : MISSING}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  );
}
