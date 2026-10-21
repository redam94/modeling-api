/**
 * Readout charts for client dashboards (MEI planner look): dependency-free SVG and plain divs
 * drawn with token classes only. Each chart is drawn at its container's pixel width (a
 * ResizeObserver, 1:1 viewBox), so 11px labels stay 11px instead of scaling with the page.
 *
 * Accessibility: every chart is `role="img"` with an accessible name that carries the numbers
 * (latest value, range, extremes, model changes), and the larger charts add the values in a
 * visually hidden table. Colour follows the entity (a channel, the index, the actuals), never its
 * rank, and zone colours always come with a tag word.
 *
 * Model plots in the expert app still go through the plot sandbox; these exist because client
 * and share pages load no sandbox iframe (plan D2).
 */
import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";

import { dm, dmy, fixed, grouped, MISSING } from "@/components/ods/format";
import { Legend } from "@/components/ods/legend";
import { ZONE_FILL, ZONE_WORDS, type Zone } from "@/components/ods/tag-word";
import { cn } from "@/lib/utils";

// ----------------------------------------------------------------------- geometry helpers

type Num = number | null | undefined;

const isNum = (v: Num): v is number => typeof v === "number" && Number.isFinite(v);
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const r2 = (v: number) => Math.round(v * 100) / 100;

/** 11px tabular axis text in the hint ink. */
const AXIS_TEXT = "tabular fill-hint text-2xs";

/** The container's content width in px, or `fallback` until it is measured (and in jsdom). */
function useContainerWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(Math.round(el.getBoundingClientRect().width));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width > 0 ? width : fallback] as const;
}

const INDEX_STEPS = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100];

export interface IndexDomain {
  lo: number;
  hi: number;
  /** Round ticks, symmetric about the centre: at most three a side. */
  ticks: number[];
  step: number;
  half: number;
}

/**
 * A y domain symmetric about `centre` (the reference) that holds every value: at least
 * ±`min`, widened in 0.05 steps up to ±0.20 and in 0.10 steps beyond, with ticks at a round step
 * out from the centre, at most three a side (MEI indexDomain).
 */
export function indexDomain(values: readonly Num[], { centre = 1, min = 0.1 }: { centre?: number; min?: number } = {}): IndexDomain {
  let half = min;
  for (const v of values) if (isNum(v)) half = Math.max(half, Math.abs(v - centre));
  const unit = half <= 0.2 + 1e-9 ? 0.05 : 0.1;
  half = +(Math.ceil(half / unit - 1e-9) * unit).toFixed(10);
  const whole = (s: number) => {
    const k = half / s;
    return Math.abs(k - Math.round(k)) < 1e-9 && Math.round(k) <= 3;
  };
  let step = INDEX_STEPS.find(whole);
  let k: number;
  if (step) k = Math.round(half / step);
  else {
    step = INDEX_STEPS.find((s) => Math.floor(half / s + 1e-9) <= 3) ?? half;
    k = Math.max(1, Math.floor(half / step + 1e-9));
  }
  const ticks: number[] = [];
  for (let j = -k; j <= k; j++) ticks.push(+(centre + j * step).toFixed(10));
  return { lo: +(centre - half).toFixed(10), hi: +(centre + half).toFixed(10), ticks, step, half };
}

function niceStep(raw: number): number {
  if (!(raw > 0)) return 1;
  const exp = 10 ** Math.floor(Math.log10(raw));
  const f = raw / exp;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * exp;
}

/** A plain domain widened to round ticks (about four intervals). */
export function niceDomain(values: readonly Num[]): { lo: number; hi: number; ticks: number[] } {
  const v = values.filter(isNum);
  let lo = v.length ? Math.min(...v) : 0;
  let hi = v.length ? Math.max(...v) : 1;
  if (hi - lo < 1e-9) {
    const pad = Math.abs(hi) > 1e-9 ? Math.abs(hi) * 0.1 : 1;
    lo -= pad;
    hi += pad;
  }
  const step = niceStep((hi - lo) / 4);
  lo = Math.floor(lo / step + 1e-9) * step;
  hi = Math.ceil(hi / step - 1e-9) * step;
  const ticks: number[] = [];
  for (let t = lo; t <= hi + step / 2; t += step) ticks.push(+t.toFixed(10));
  return { lo, hi, ticks };
}

/** Points in slots: n equal columns between left and right, each point at its column's centre. */
function slots(n: number, left: number, right: number) {
  const step = (right - left) / Math.max(1, n);
  return { x: (j: number) => left + step * (j + 0.5), half: step / 2 };
}

function scale(d0: number, d1: number, r0: number, r1: number) {
  const k = d1 === d0 ? 0 : (r1 - r0) / (d1 - d0);
  return (v: number) => r0 + (v - d0) * k;
}

/** A polyline that breaks at missing values. */
function linePath(xs: readonly number[], ys: readonly Num[]): string {
  let d = "";
  let pen = false;
  ys.forEach((y, j) => {
    if (!isNum(y)) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${r2(xs[j] ?? 0)} ${r2(y)}`;
    pen = true;
  });
  return d;
}

/** A band between two lines, one closed shape per run of points that have both ends. */
function areaPath(xs: readonly number[], lo: readonly Num[], hi: readonly Num[]): string {
  const runs: number[][] = [];
  let run: number[] = [];
  xs.forEach((_, j) => {
    if (isNum(lo[j]) && isNum(hi[j])) run.push(j);
    else if (run.length) {
      runs.push(run);
      run = [];
    }
  });
  if (run.length) runs.push(run);
  return runs
    .map((r) => {
      const top = r.map((j, i) => `${i ? "L" : "M"}${r2(xs[j] ?? 0)} ${r2(hi[j] as number)}`).join("");
      const bottom = [...r].reverse().map((j) => `L${r2(xs[j] ?? 0)} ${r2(lo[j] as number)}`).join("");
      return `${top}${bottom}Z`;
    })
    .join("");
}

/** Which x labels to draw: at least `gap` px apart, counted back from the last point. */
function labelIndices(n: number, plotW: number, gap = 56): number[] {
  if (n <= 0) return [];
  const every = Math.max(1, Math.ceil((n * gap) / Math.max(1, plotW)));
  const out: number[] = [];
  for (let j = n - 1; j >= 0; j -= every) out.unshift(j);
  return out;
}

const PERIOD_PLURAL: Record<string, string> = { week: "weeks", month: "months", period: "periods" };
const plural = (period: string, n: number) => (n === 1 ? period : (PERIOD_PLURAL[period] ?? `${period}s`));

function extremes(ts: readonly string[], vs: readonly Num[]) {
  let lo: number | null = null;
  let hi: number | null = null;
  vs.forEach((v, j) => {
    if (!isNum(v)) return;
    if (lo === null || v < (vs[lo] as number)) lo = j;
    if (hi === null || v > (vs[hi] as number)) hi = j;
  });
  return { lo, hi, at: (j: number | null) => (j === null ? "" : dmy(ts[j] ?? "")) };
}

function lastIndex(vs: readonly Num[]): number | null {
  for (let j = vs.length - 1; j >= 0; j--) if (isNum(vs[j])) return j;
  return null;
}

/** The chart's figure: a measured box, the SVG, and the visually hidden table. */
function ChartFrame({
  boxRef,
  className,
  children,
}: {
  boxRef: RefObject<HTMLDivElement | null>;
  className?: string | undefined;
  children: ReactNode;
}) {
  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <div ref={boxRef} className="relative min-w-0">
        {children}
      </div>
    </figure>
  );
}

// ----------------------------------------------------------------------- IndexLine

export interface IndexPoint {
  /** ISO date of the period end. */
  t: string;
  value: number | null;
  low?: number | null;
  high?: number | null;
}

/** A shaded run of periods, by point index (inclusive), with a short label above it. */
export interface ChartRegion {
  from: number;
  to: number;
  label: string;
}

/** The model version changed from the point at `at` onwards ("Model v4"). */
export interface VersionMark {
  at: number;
  label: string;
}

const IDX = { H: 240, L: 44, R: 12, T: 30, B: 214 } as const;

/**
 * The index line: the reading per period with its likely band, the reference line (1.00 or
 * 100), an end dot labelled "0.99 this week", shaded regions, a now line and model-change
 * markers. The y domain is symmetric about the reference (at least ±`minHalf`, ≤ 3 ticks a side).
 */
export function IndexLine({
  label,
  points,
  reference = 1,
  minHalf = 0.1,
  format = (v: number) => fixed(v, 2),
  period = "week",
  regions = [],
  nowAt,
  versions = [],
  className,
}: {
  /** The chart's name, which starts its accessible name ("Media effectiveness index"). */
  label: string;
  points: readonly IndexPoint[];
  reference?: number;
  minHalf?: number;
  format?: (v: number) => string;
  period?: "week" | "month" | "period";
  regions?: readonly ChartRegion[];
  nowAt?: number | undefined;
  versions?: readonly VersionMark[];
  className?: string;
}) {
  const id = useId();
  const [boxRef, W0] = useContainerWidth<HTMLDivElement>(640);
  const W = Math.max(280, W0);
  const { H, L, R, T, B } = IDX;
  const n = points.length;
  const ts = points.map((p) => p.t);
  const vs = points.map((p) => p.value);
  const lov = points.map((p) => (isNum(p.low) ? p.low : null));
  const hiv = points.map((p) => (isNum(p.high) ? p.high : null));
  const dom = indexDomain([...vs, ...lov, ...hiv], { centre: reference, min: minHalf });
  const { x, half } = slots(n, L, W - R);
  const y = scale(dom.lo, dom.hi, B, T);
  const xs = points.map((_, j) => x(j));
  const last = lastIndex(vs);
  const lastV = last === null ? null : (vs[last] as number);
  const hasBand = points.some((_, j) => isNum(lov[j]) && isNum(hiv[j]) && hiv[j] - lov[j] > 1e-9);
  const ext = extremes(ts, vs);
  const marks = versions.filter((v) => v.at > 0 && v.at < n);

  const summary =
    `${label}, ${n} ${plural(period, n)}${n ? ` to ${dmy(ts[n - 1] ?? "")}` : ""}` +
    (lastV !== null && last !== null
      ? `: ${format(lastV)} this ${period}` +
        (isNum(lov[last]) && isNum(hiv[last]) ? `, likely range ${format(lov[last])} to ${format(hiv[last])}` : "") +
        "."
      : ". There are no values.") +
    ` The reference is ${format(reference)}.` +
    (ext.lo !== null && ext.hi !== null && ext.lo !== ext.hi
      ? ` Lowest ${format(vs[ext.lo] as number)} on ${ext.at(ext.lo)}, highest ${format(vs[ext.hi] as number)} on ${ext.at(ext.hi)}.`
      : "") +
    marks.map((m) => ` ${m.label} from ${dmy(ts[m.at] ?? "")}.`).join("") +
    regions.map((r) => ` ${r.label}: ${dmy(ts[r.from] ?? "")} to ${dmy(ts[r.to] ?? "")}.`).join("");

  // End label: below the dot when the reading is under the reference, else above; kept inside the plot.
  let endLabel: { y: number; text: string } | null = null;
  if (lastV !== null) {
    const py = y(lastV);
    const below = py + 22;
    const above = py - 12;
    const fits = (v: number) => v >= T + 12 && v <= B - 22;
    const ly = lastV < reference ? (fits(below) ? below : above) : fits(above) ? above : below;
    endLabel = { y: clamp(ly, T + 12, B - 4), text: `${format(lastV)} this ${period}` };
  }

  return (
    <ChartFrame boxRef={boxRef} className={className}>
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={summary}
        aria-describedby={`${id}-table`}
        data-chart="index-line"
        className="block max-w-full"
      >
        {regions.map((r) => {
          const x0 = x(clamp(r.from, 0, n - 1)) - half;
          const x1 = x(clamp(r.to, 0, n - 1)) + half;
          return (
            <g key={`${r.from}-${r.label}`} data-region={r.label}>
              <rect x={r2(x0)} y={T} width={r2(Math.max(0, x1 - x0))} height={B - T} className="fill-muted" />
              <text x={r2(x0 + 4)} y={12} className={AXIS_TEXT}>
                {r.label}
              </text>
            </g>
          );
        })}
        {dom.ticks.map((v) => {
          const isRef = Math.abs(v - reference) < 1e-9;
          return (
            <g key={v}>
              {!isRef && Math.abs(v - dom.lo) > 1e-9 ? <line x1={L} x2={W - R} y1={r2(y(v))} y2={r2(y(v))} className="stroke-chart-grid" /> : null}
              <text x={L - 8} y={r2(y(v) + 4)} textAnchor="end" className={cn(AXIS_TEXT, isRef && "fill-muted-foreground font-semibold")}>
                {format(v)}
              </text>
            </g>
          );
        })}
        <line x1={L} x2={W - R} y1={B} y2={B} className="stroke-chart-base" />
        {hasBand ? <path d={areaPath(xs, lov.map((v) => (isNum(v) ? y(v) : null)), hiv.map((v) => (isNum(v) ? y(v) : null)))} className="fill-chart-band" /> : null}
        <line x1={L} x2={W - R} y1={r2(y(reference))} y2={r2(y(reference))} className="stroke-chart-ref" data-ref />
        {isNum(nowAt) && nowAt >= 0 && nowAt < n ? (
          <g data-marker="now">
            <line x1={r2(x(nowAt))} x2={r2(x(nowAt))} y1={T} y2={B} className="stroke-chart-now" strokeWidth={1.5} />
            <text x={r2(x(nowAt) - 4)} y={24} textAnchor="end" className={AXIS_TEXT}>
              Now
            </text>
          </g>
        ) : null}
        {marks.map((m) => {
          const mx = r2(x(m.at) - half);
          return (
            <g key={`${m.at}-${m.label}`} data-marker="version" data-at={m.at}>
              <line x1={mx} x2={mx} y1={T - 2} y2={B} className="stroke-chart-plain" strokeDasharray="3 3" />
              <text x={mx + 3} y={24} className={AXIS_TEXT}>
                {m.label}
              </text>
            </g>
          );
        })}
        <path d={linePath(xs, vs.map((v) => (isNum(v) ? y(v) : null)))} className="fill-none stroke-chart-index" strokeWidth={2} strokeLinejoin="round" />
        {lastV !== null && last !== null ? (
          <circle cx={r2(x(last))} cy={r2(y(lastV))} r={4.5} className="fill-chart-index stroke-card" strokeWidth={2} data-end-dot />
        ) : null}
        {endLabel ? (
          <text x={W - R - 6} y={r2(endLabel.y)} textAnchor="end" className="tabular fill-foreground text-xs font-semibold" data-end-label>
            {endLabel.text}
          </text>
        ) : null}
        {labelIndices(n, W - L - R).map((j) => (
          <text key={j} x={r2(x(j))} y={H - 6} textAnchor={j === n - 1 && n > 1 ? "end" : "middle"} className={AXIS_TEXT}>
            {dm(ts[j] ?? "")}
          </text>
        ))}
      </svg>
      <table id={`${id}-table`} className="sr-only">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th scope="col">Period to</th>
            <th scope="col">Value</th>
            <th scope="col">Likely range</th>
            <th scope="col">Note</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p, j) => (
            <tr key={p.t}>
              <th scope="row">{dmy(p.t)}</th>
              <td>{isNum(p.value) ? format(p.value) : MISSING}</td>
              <td>{isNum(p.low) && isNum(p.high) ? `${format(p.low)} to ${format(p.high)}` : MISSING}</td>
              <td>{marks.filter((m) => m.at === j).map((m) => m.label).join(", ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </ChartFrame>
  );
}

// ----------------------------------------------------------------------- BandLine

/** mapi_spec.dashboards.SeriesPoint. */
export interface BandPoint {
  t: string;
  mean: number | null;
  low?: number | null;
  high?: number | null;
  actual?: number | null;
}

/**
 * Forecast against actuals: the expected line with its likely band, and the actual numbers as a
 * second line with dots. The accessible name gives the latest pair and how often the actual
 * number was in the likely range.
 */
export function BandLine({
  label,
  points,
  format = (v: number) => grouped(v, 0),
  period = "week",
  nowAt,
  expectedLabel = "Expected",
  actualLabel = "Actual",
  legend = true,
  className,
}: {
  label: string;
  points: readonly BandPoint[];
  format?: (v: number) => string;
  period?: "week" | "month" | "period";
  nowAt?: number | undefined;
  expectedLabel?: string;
  actualLabel?: string;
  legend?: boolean;
  className?: string;
}) {
  const id = useId();
  const [boxRef, W0] = useContainerWidth<HTMLDivElement>(640);
  const W = Math.max(280, W0);
  const H = IDX.H;
  const T = 14;
  const B = IDX.B;
  const n = points.length;
  const ts = points.map((p) => p.t);
  const mean = points.map((p) => p.mean);
  const lov = points.map((p) => (isNum(p.low) ? p.low : null));
  const hiv = points.map((p) => (isNum(p.high) ? p.high : null));
  const act = points.map((p) => (isNum(p.actual) ? p.actual : null));
  const dom = niceDomain([...mean, ...lov, ...hiv, ...act]);
  const tickW = Math.max(...dom.ticks.map((t) => format(t).length)) * 6.5 + 12;
  const L = clamp(Math.round(tickW), 44, 96);
  const R = IDX.R;
  const { x } = slots(n, L, W - R);
  const y = scale(dom.lo, dom.hi, B, T);
  const xs = points.map((_, j) => x(j));
  const hasBand = points.some((_, j) => isNum(lov[j]) && isNum(hiv[j]));
  const hasActual = act.some(isNum);

  const lastA = lastIndex(act);
  const lastM = lastIndex(mean);
  const withRange = points.filter((p) => isNum(p.actual) && isNum(p.low) && isNum(p.high));
  const inside = withRange.filter((p) => (p.actual as number) >= (p.low as number) && (p.actual as number) <= (p.high as number)).length;
  const range = (j: number) => (isNum(lov[j]) && isNum(hiv[j]) ? ` (likely range ${format(lov[j])} to ${format(hiv[j])})` : "");
  const summary =
    `${label}, ${n} ${plural(period, n)}${n ? ` to ${dmy(ts[n - 1] ?? "")}` : ""}.` +
    (lastA !== null
      ? ` Latest ${actualLabel.toLowerCase()} ${format(act[lastA] as number)} on ${dmy(ts[lastA] ?? "")}, against ${expectedLabel.toLowerCase()} ${isNum(mean[lastA]) ? format(mean[lastA]) : MISSING}${range(lastA)}.`
      : lastM !== null
        ? ` Latest ${expectedLabel.toLowerCase()} ${format(mean[lastM] as number)} on ${dmy(ts[lastM] ?? "")}${range(lastM)}.`
        : " There are no values.") +
    (withRange.length ? ` The ${actualLabel.toLowerCase()} number was in the likely range in ${inside} of ${withRange.length} ${plural(period, withRange.length)}.` : "");

  return (
    <ChartFrame boxRef={boxRef} className={className}>
      <svg
        width={W}
        height={H}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={summary}
        aria-describedby={`${id}-table`}
        data-chart="band-line"
        className="block max-w-full"
      >
        {dom.ticks.map((v) => (
          <g key={v}>
            {Math.abs(v - dom.lo) > 1e-9 ? <line x1={L} x2={W - R} y1={r2(y(v))} y2={r2(y(v))} className="stroke-chart-grid" /> : null}
            <text x={L - 8} y={r2(y(v) + 4)} textAnchor="end" className={AXIS_TEXT}>
              {format(v)}
            </text>
          </g>
        ))}
        <line x1={L} x2={W - R} y1={B} y2={B} className="stroke-chart-base" />
        {hasBand ? <path d={areaPath(xs, lov.map((v) => (isNum(v) ? y(v) : null)), hiv.map((v) => (isNum(v) ? y(v) : null)))} className="fill-chart-band" /> : null}
        {isNum(nowAt) && nowAt >= 0 && nowAt < n ? (
          <g data-marker="now">
            <line x1={r2(x(nowAt))} x2={r2(x(nowAt))} y1={T} y2={B} className="stroke-chart-now" strokeWidth={1.5} />
          </g>
        ) : null}
        <path d={linePath(xs, mean.map((v) => (isNum(v) ? y(v) : null)))} className="fill-none stroke-chart-index" strokeWidth={2} strokeLinejoin="round" />
        {hasActual ? (
          <>
            <path d={linePath(xs, act.map((v) => (isNum(v) ? y(v) : null)))} className="fill-none stroke-chart-net" strokeWidth={1.5} strokeLinejoin="round" />
            {act.map((v, j) => (isNum(v) ? <circle key={j} cx={r2(x(j))} cy={r2(y(v))} r={3} className="fill-chart-net stroke-card" strokeWidth={1.5} /> : null))}
          </>
        ) : null}
        {labelIndices(n, W - L - R).map((j) => (
          <text key={j} x={r2(x(j))} y={H - 6} textAnchor={j === n - 1 && n > 1 ? "end" : "middle"} className={AXIS_TEXT}>
            {dm(ts[j] ?? "")}
          </text>
        ))}
      </svg>
      {legend ? (
        <Legend
          items={[
            { label: expectedLabel, color: "index", swatch: "line" },
            ...(hasBand ? [{ label: "Likely range", color: "band" as const }] : []),
            ...(hasActual ? [{ label: actualLabel, color: "net" as const, swatch: "dot" as const }] : []),
          ]}
        />
      ) : null}
      <table id={`${id}-table`} className="sr-only">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th scope="col">Period to</th>
            <th scope="col">{expectedLabel}</th>
            <th scope="col">Likely range</th>
            <th scope="col">{actualLabel}</th>
          </tr>
        </thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.t}>
              <th scope="row">{dmy(p.t)}</th>
              <td>{isNum(p.mean) ? format(p.mean) : MISSING}</td>
              <td>{isNum(p.low) && isNum(p.high) ? `${format(p.low)} to ${format(p.high)}` : MISSING}</td>
              <td>{isNum(p.actual) ? format(p.actual) : MISSING}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </ChartFrame>
  );
}

// ----------------------------------------------------------------------- DivergingBars

export interface DivergingRow {
  key: string;
  label: string;
  value: number;
}

function DivergingRowView({
  row,
  isNet,
  max,
  format,
}: {
  row: { label: string; value: number };
  isNet: boolean;
  max: number;
  format: (v: number) => string;
}) {
  const w = `${(Math.abs(row.value) / max) * 50}%`;
  const pos = row.value >= 0;
  return (
    <div
      data-row={isNet ? "net" : "item"}
      className={cn(
        "grid grid-cols-[minmax(0,1fr)_64px] items-center gap-x-4 gap-y-1 sm:grid-cols-[200px_minmax(0,1fr)_64px]",
        isNet && "border-t pt-3.5",
      )}
    >
      <span className={cn("min-w-0 truncate text-sm", isNet ? "font-semibold text-foreground" : "text-muted-foreground")}>{row.label}</span>
      <span className="relative h-5 max-sm:col-span-2 max-sm:row-start-2 sm:col-start-2">
        <span className="absolute -top-1 -bottom-1 left-1/2 w-px bg-chart-ref" />
        {Math.abs(row.value) > 0 ? (
          <span
            data-sign={pos ? "gain" : "loss"}
            className={cn(
              "absolute top-0 h-5",
              pos ? "left-1/2 rounded-r-sm" : "right-1/2 rounded-l-sm",
              isNet ? "bg-chart-net" : pos ? "bg-chart-gain" : "bg-chart-loss",
            )}
            style={{ width: w }}
          />
        ) : null}
      </span>
      <span className="text-right text-sm font-semibold text-foreground tabular max-sm:col-start-2 max-sm:row-start-1">{format(row.value)}</span>
    </div>
  );
}

const signedGrouped = (v: number) => (Number(v.toFixed(0)) > 0 ? `+${grouped(v, 0)}` : grouped(v, 0));

/**
 * Diverging bars ("Why the result differs from the plan"): label 200 | bar 1fr | value 64, zero
 * in the middle, a symmetric scale; gains to the right, losses to the left, the net row in the
 * net ink under a rule. On phones the bar moves under its label and value.
 */
export function DivergingBars({
  label,
  rows,
  net,
  format = signedGrouped,
  className,
}: {
  label: string;
  rows: readonly DivergingRow[];
  net?: { label: string; value: number } | undefined;
  format?: (v: number) => string;
  className?: string;
}) {
  const id = useId();
  const max = Math.max(1e-12, ...rows.map((r) => Math.abs(r.value)), net ? Math.abs(net.value) : 0);
  const summary = `${label}: ${rows.map((r) => `${r.label} ${format(r.value)}`).join(", ")}${net ? `; ${net.label} ${format(net.value)}` : ""}.`;

  return (
    <figure className={cn("grid min-w-0 gap-2", className)}>
      <div role="img" aria-label={summary} aria-describedby={`${id}-table`} data-chart="diverging-bars" className="flex flex-col gap-3.5">
        {rows.map((r) => (
          <DivergingRowView key={r.key} row={r} isNet={false} max={max} format={format} />
        ))}
        {net ? <DivergingRowView row={net} isNet max={max} format={format} /> : null}
        <div aria-hidden className="grid grid-cols-[minmax(0,1fr)_64px] gap-x-4 sm:grid-cols-[200px_minmax(0,1fr)_64px]">
          <span className="flex justify-between text-2xs text-hint tabular max-sm:col-span-2 sm:col-start-2">
            <span>{format(-max)}</span>
            <span>0</span>
            <span>{format(max)}</span>
          </span>
        </div>
      </div>
      <table id={`${id}-table`} className="sr-only">
        <caption>{label}</caption>
        <tbody>
          {[...rows, ...(net ? [{ key: "net", ...net }] : [])].map((r) => (
            <tr key={r.key}>
              <th scope="row">{r.label}</th>
              <td>{format(r.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

// ----------------------------------------------------------------------- ZoneBar

/**
 * Where the spend sits on the response curve: an 8px track split into the zones (more room up to
 * `low`, within range up to `high`, past its best beyond), a 1px tick at the usual level and a
 * dot at this period's spend. Values are multiples of the usual spend on a 0 to `scaleMax` scale.
 */
export function ZoneBar({
  label,
  zone,
  now,
  low,
  high,
  usual = 1,
  scaleMax = 2.4,
  period = "week",
  className,
}: {
  label: string;
  zone: Zone | null | undefined;
  now: number | null | undefined;
  low?: number | null | undefined;
  high?: number | null | undefined;
  usual?: number;
  scaleMax?: number;
  period?: "week" | "month" | "period";
  className?: string;
}) {
  const pct = (v: number) => `${clamp((v / scaleMax) * 100, 0, 100)}%`;
  const split = zone !== "no_payback" && isNum(low) && isNum(high) && high >= low;
  const word = zone ? ZONE_WORDS[zone] : "No zone";
  const aria =
    `${label}: ${word}.` +
    (isNum(now) ? ` Spend this ${period} is ${fixed(now, 2)} times the usual level.` : "") +
    (split ? ` The best range is ${fixed(low, 2)} to ${fixed(high, 2)} times.` : "");
  return (
    <div role="img" aria-label={aria} data-chart="zone-bar" data-zone={zone ?? "none"} className={cn("relative h-3.5 min-w-0", className)}>
      <div className={cn("absolute inset-x-0 top-[3px] flex h-2 overflow-hidden rounded-sm", !split && ZONE_FILL.no_payback)}>
        {split ? (
          <>
            <span className={ZONE_FILL.headroom} style={{ width: pct(low) }} />
            <span className={ZONE_FILL.optimal} style={{ width: `calc(${pct(high)} - ${pct(low)})` }} />
            <span className={cn("flex-1", ZONE_FILL.over)} />
          </>
        ) : null}
      </div>
      <span className="absolute top-0 h-3.5 w-px bg-muted-foreground" style={{ left: pct(usual) }} data-usual />
      {isNum(now) ? (
        <span className="absolute top-0 -ml-[7px] size-3.5 rounded-full border-2 border-card bg-foreground" style={{ left: pct(now) }} data-now />
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------- RangeBar

/**
 * A value and its likely range against a reference in the middle (return on media against
 * break-even, expected against the plan). The range is green when all of it is above the
 * reference, red when all of it is below, grey when it spans the reference.
 */
export function RangeBar({
  label,
  value,
  low,
  high,
  reference = 1,
  referenceLabel = "break-even",
  format = (v: number) => fixed(v, 2),
  minSpread = 0.12,
  className,
}: {
  label: string;
  value: number | null | undefined;
  low?: number | null | undefined;
  high?: number | null | undefined;
  reference?: number;
  referenceLabel?: string;
  format?: (v: number) => string;
  /** The smallest half-width of the scale, as a fraction of the reference. */
  minSpread?: number;
  className?: string;
}) {
  const half =
    Math.max(Math.abs(reference) * minSpread, ...[value, low, high].filter(isNum).map((v) => Math.abs(v - reference)), 1e-9) * 1.1;
  const pos = (v: number) => clamp(50 + ((v - reference) / half) * 50, 0, 100);
  const hasRange = isNum(low) && isNum(high);
  const dir = hasRange ? (low > reference ? "up" : high < reference ? "down" : "flat") : isNum(value) ? (value > reference ? "up" : value < reference ? "down" : "flat") : "flat";
  const relation = dir === "up" ? "above" : dir === "down" ? "below" : "around";
  const aria = isNum(value)
    ? `${label}: ${format(value)}${hasRange ? `, likely range ${format(low)} to ${format(high)}` : ""}, ${relation} the ${referenceLabel} of ${format(reference)}.`
    : `${label}: no value.`;
  return (
    <div role="img" aria-label={aria} data-chart="range-bar" data-dir={dir} className={cn("relative h-3 min-w-0", className)}>
      <span className="absolute inset-x-0 top-[5px] h-0.5 bg-chart-grid" />
      {hasRange ? (
        <span
          className={cn("absolute top-0.5 h-2 rounded-sm", dir === "up" ? "bg-zone-optimal" : dir === "down" ? "bg-zone-over" : "bg-zone-nopay")}
          style={{ left: `${pos(low)}%`, width: `${Math.max(0.5, pos(high) - pos(low))}%` }}
        />
      ) : null}
      <span className="absolute top-0 left-1/2 -ml-px h-3 w-0.5 bg-chart-net" data-ref />
      {isNum(value) ? (
        <span
          className={cn("absolute top-px -ml-[5px] size-2.5 rounded-full", dir === "up" ? "bg-chart-gain" : dir === "down" ? "bg-chart-loss" : "bg-chart-plain")}
          style={{ left: `${pos(value)}%` }}
          data-dot
        />
      ) : null}
    </div>
  );
}

// ----------------------------------------------------------------------- Sparkline

const SPARK_STROKE = {
  index: "stroke-chart-index",
  "series-1": "stroke-series-1",
  "series-2": "stroke-series-2",
  "series-3": "stroke-series-3",
  "series-4": "stroke-series-4",
  "series-5": "stroke-series-5",
  "series-6": "stroke-series-6",
  "series-7": "stroke-series-7",
  "series-8": "stroke-series-8",
} as const;
const SPARK_FILL: Record<keyof typeof SPARK_STROKE, string> = {
  index: "fill-chart-index",
  "series-1": "fill-series-1",
  "series-2": "fill-series-2",
  "series-3": "fill-series-3",
  "series-4": "fill-series-4",
  "series-5": "fill-series-5",
  "series-6": "fill-series-6",
  "series-7": "fill-series-7",
  "series-8": "fill-series-8",
};

/**
 * A 300x48 trend for one entity: the line, a dashed reference line and a dot at the last value.
 * With a reference the scale is symmetric about it (at least ±0.15, in 0.05 steps), so rows of
 * sparklines that share `half` stay comparable.
 */
export function Sparkline({
  label,
  values,
  reference,
  half,
  format = (v: number) => fixed(v, 2),
  color = "index",
  width = 300,
  className,
}: {
  label: string;
  values: readonly Num[];
  reference?: number | undefined;
  /** A shared half-height about the reference; defaults to what these values need. */
  half?: number | undefined;
  format?: (v: number) => string;
  /** The entity's colour (a channel keeps its series colour on every chart). */
  color?: keyof typeof SPARK_STROKE;
  width?: number;
  className?: string;
}) {
  const [boxRef, W0] = useContainerWidth<HTMLDivElement>(width);
  const W = Math.max(80, Math.min(width, W0));
  const H = 48;
  const n = values.length;
  const nums = values.filter(isNum);
  let lo: number;
  let hi: number;
  if (isNum(reference)) {
    let h = half ?? 0.15;
    if (half === undefined) for (const v of nums) h = Math.max(h, Math.abs(v - reference));
    h = +(Math.ceil(h / 0.05 - 1e-9) * 0.05).toFixed(10);
    lo = reference - h;
    hi = reference + h;
  } else {
    lo = nums.length ? Math.min(...nums) : 0;
    hi = nums.length ? Math.max(...nums) : 1;
    if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    }
  }
  const x = (j: number) => (n > 1 ? 2 + (j / (n - 1)) * (W - 4) : W / 2);
  const y = (v: number) => 4 + ((hi - clamp(v, lo, hi)) / (hi - lo)) * (H - 8);
  const last = lastIndex(values);
  const desc = !nums.length
    ? "no values"
    : nums.every((v) => Math.abs(v - (nums[0] as number)) < 1e-9)
      ? `${format(nums[0] as number)} throughout`
      : `from ${format(nums[0] as number)} to ${format(nums[nums.length - 1] as number)}, lowest ${format(Math.min(...nums))}, highest ${format(Math.max(...nums))}`;
  const aria = `${label}, ${n} values: ${desc}.${isNum(reference) ? ` The reference is ${format(reference)}.` : ""}`;
  return (
    <div ref={boxRef} className={cn("min-w-0", className)} style={{ maxWidth: width }}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={aria} data-chart="sparkline" className="block">
        {isNum(reference) ? <line x1={2} x2={W - 2} y1={r2(y(reference))} y2={r2(y(reference))} className="stroke-chart-ref" strokeDasharray="3 3" data-ref /> : null}
        <path d={linePath(values.map((_, j) => x(j)), values.map((v) => (isNum(v) ? y(v) : null)))} className={cn("fill-none", SPARK_STROKE[color])} strokeWidth={1.5} strokeLinejoin="round" />
        {last !== null ? <circle cx={r2(x(last))} cy={r2(y(values[last] as number))} r={3.5} className={SPARK_FILL[color]} /> : null}
      </svg>
    </div>
  );
}
