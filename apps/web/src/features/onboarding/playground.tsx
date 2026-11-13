/**
 * The what-if playgrounds (v0.18 D21 (i)): budget sliders answered in the browser over a static
 * demo grid, so a new analyst can feel what a fitted marketing mix model does before they fit
 * one. Two faces share one model:
 *
 * - {@link Playground}: the Home "What if you moved the budget?" card (one slider per channel,
 *   predicted weekly sales with a delta pill, the 90% band of the change, the response curves
 *   with each channel's operating point, and what the next £1 would return);
 * - {@link MiniPlayground}: the Catalog's compact "try the curve" card on the marketing-mix
 *   model (one channel at a time, the 90% range and the "pays back" zone as toggles).
 *
 * The grid (`demo-grid.json`, `mapi.response-grid/v1`, made by `scripts/make_demo_grid.py`
 * from a real fit of the synthetic sample) is answered with `evaluateScenario`, the same code
 * as the Guided what-if, so the numbers behave like the real thing. Everything here is
 * labelled "Example data"; "Open the real model" opens the tenant's sample once it is ready.
 * Nothing is sent to the server. This module is a lazy chunk (`playground-*`, demo grid
 * included); signed-in surfaces only (never in share or client chunks).
 */
import { useNavigate } from "@tanstack/react-router";
import { ArrowRight, RotateCcw } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";

import { ChangeBand } from "@/components/interactive/change-band";
import { type SliderColor, ValueSlider } from "@/components/interactive/sliders";
import { CountUp } from "@/components/ods/count-up";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/overlays";
import { evaluateScenario, interp, parseResponseGrid, quantile, type ResponseGrid } from "@/features/guided/whatif";
import { emitOnboarding } from "@/lib/onboarding-events";
import { cn } from "@/lib/utils";
import { useScope } from "@/stores/scope";

import { type SampleInfo, useOnboardingTenant, useSamples } from "./api";
import rawDemoGrid from "./demo-grid.json";

// ----------------------------------------------------------------------------- the demo model

/** The slider step, as a share of the planned spend (the grid's multipliers run 0 to 2.5). */
export const PLAYGROUND_STEP = 0.05;
/** A weekly change smaller than this (in £) reads "same as plan". */
const FLAT_BELOW = 50;

/** Plain names for the demo grid's channels (any other channel keeps a capitalised id). */
export const CHANNEL_LABEL: Readonly<Record<string, string>> = { tv: "TV", search: "Paid search", social: "Social" };
/** The order the sliders show (the mockup's: the big channel first). */
const CHANNEL_ORDER = ["tv", "search", "social"];

const COLORS = ["series-1", "series-2", "series-3", "series-4", "series-5", "series-6"] as const satisfies readonly SliderColor[];
type SeriesColor = (typeof COLORS)[number];
// Literal class names so Tailwind finds every one of them in the source.
const SWATCH: Record<SeriesColor, string> = {
  "series-1": "bg-series-1",
  "series-2": "bg-series-2",
  "series-3": "bg-series-3",
  "series-4": "bg-series-4",
  "series-5": "bg-series-5",
  "series-6": "bg-series-6",
};
const STROKE: Record<SeriesColor, string> = {
  "series-1": "stroke-series-1",
  "series-2": "stroke-series-2",
  "series-3": "stroke-series-3",
  "series-4": "stroke-series-4",
  "series-5": "stroke-series-5",
  "series-6": "stroke-series-6",
};
const FILL: Record<SeriesColor, string> = {
  "series-1": "fill-series-1",
  "series-2": "fill-series-2",
  "series-3": "fill-series-3",
  "series-4": "fill-series-4",
  "series-5": "fill-series-5",
  "series-6": "fill-series-6",
};

export function channelLabel(channel: string): string {
  return CHANNEL_LABEL[channel] ?? channel.charAt(0).toUpperCase() + channel.slice(1).replace(/_/g, " ");
}

let parsed: ResponseGrid | null = null;

/** The demo grid, parsed and validated once (throws `WhatIfError` if the file is broken). */
export function demoGrid(): ResponseGrid {
  parsed ??= parseResponseGrid(rawDemoGrid);
  return parsed;
}

/** The grid's channels in the order the playground shows them. */
export function orderedChannels(grid: Pick<ResponseGrid, "channels">): string[] {
  return [...CHANNEL_ORDER.filter((c) => grid.channels.includes(c)), ...grid.channels.filter((c) => !CHANNEL_ORDER.includes(c))];
}

/** Every channel at its planned spend (multiplier 1). */
export function planMultipliers(grid: Pick<ResponseGrid, "channels">): Record<string, number> {
  return Object.fromEntries(grid.channels.map((c) => [c, 1]));
}

/** One channel's response curve per week: spend, the mean and the 90% range of what it adds. */
export interface ChannelCurve {
  channel: string;
  /** Weekly spend at each grid multiplier. */
  spend: number[];
  /** Weekly sales added, mean over draws, at each grid multiplier. */
  mean: number[];
  /** 5% and 95% quantiles over draws. */
  lo: number[];
  hi: number[];
}

/** The response curves of every channel, per week (the grid holds totals over its periods). */
export function channelCurves(grid: ResponseGrid): ChannelCurve[] {
  const per = 1 / grid.periods;
  return grid.channels.map((channel, c) => {
    const mean: number[] = [];
    const lo: number[] = [];
    const hi: number[] = [];
    grid.multipliers.forEach((_, i) => {
      const values = grid.contribution.map((perC) => ((perC[c] as number[])[i] as number) * per);
      const sorted = [...values].sort((a, b) => a - b);
      mean.push(values.reduce((s, v) => s + v, 0) / values.length);
      lo.push(quantile(sorted, 0.05));
      hi.push(quantile(sorted, 0.95));
    });
    return { channel, spend: (grid.spend[c] as number[]).map((s) => s * per), mean, lo, hi };
  });
}

/** What the next £1 on a channel returns at multiplier `m` (slope of the mean curve). */
export function nextPound(grid: Pick<ResponseGrid, "multipliers">, curve: ChannelCurve, m: number): number {
  const xs = grid.multipliers;
  const a = Math.max(xs[0] as number, m - PLAYGROUND_STEP);
  const b = Math.min(xs[xs.length - 1] as number, m + PLAYGROUND_STEP);
  const ds = interp(xs, curve.spend, b) - interp(xs, curve.spend, a);
  return ds > 0 ? (interp(xs, curve.mean, b) - interp(xs, curve.mean, a)) / ds : 0;
}

/**
 * The spend ranges (weekly £) where the next £1 returns at least £1 on `curve`, scanned at the
 * slider step. Concave channels pay back from zero up to a point; an S-shaped one only in a band.
 */
export function paysBackRanges(grid: Pick<ResponseGrid, "multipliers">, curve: ChannelCurve): [number, number][] {
  const xs = grid.multipliers;
  const max = xs[xs.length - 1] as number;
  const out: [number, number][] = [];
  let start: number | null = null;
  let last = 0;
  for (let i = 0; i * PLAYGROUND_STEP <= max + 1e-9; i++) {
    const m = Math.min(max, i * PLAYGROUND_STEP);
    const spend = interp(xs, curve.spend, m);
    if (nextPound(grid, curve, m) >= 1) {
      start ??= spend;
      last = spend;
    } else if (start !== null) {
      out.push([start, last]);
      start = null;
    }
  }
  if (start !== null) out.push([start, last]);
  return out;
}

export interface ChannelState {
  channel: string;
  multiplier: number;
  /** Weekly spend. */
  spend: number;
  /** Weekly sales the channel adds (mean). */
  added: number;
  /** What the next £1 returns. */
  nextPound: number;
}

/** The playground's answer for a set of multipliers, per week. */
export interface PlaygroundView {
  /** Predicted weekly sales (baseline + media, mean over draws); `null` without a baseline. */
  weeklySales: number | null;
  /** The change in weekly sales against the plan: mean and 90% range. */
  change: { lo: number; mid: number; hi: number };
  weeklySpend: number;
  spendChange: number;
  /** Sales gained per extra £1 of the change (`null` when the spend did not change). */
  returnOnChange: number | null;
  channels: ChannelState[];
}

export function playgroundView(grid: ResponseGrid, curves: readonly ChannelCurve[], multipliers: Record<string, number>): PlaygroundView {
  const r = evaluateScenario(grid, { multipliers });
  const per = 1 / grid.periods;
  const q = r.kpi_delta.quantiles;
  const spendChange = r.spend_delta * per;
  const change = { lo: (q[0] as number) * per, mid: r.kpi_delta.mean * per, hi: (q[2] as number) * per };
  return {
    weeklySales: r.kpi ? r.kpi.mean * per : null,
    change,
    weeklySpend: r.total_spend * per,
    spendChange,
    returnOnChange: Math.abs(spendChange) >= 1 ? change.mid / spendChange : null,
    channels: grid.channels.map((channel, c) => {
      const out = r.channels[channel];
      const m = out?.multiplier ?? 1;
      return {
        channel,
        multiplier: m,
        spend: (out?.spend ?? 0) * per,
        added: (out?.contribution.mean ?? 0) * per,
        nextPound: nextPound(grid, curves[c] as ChannelCurve, m),
      };
    }),
  };
}

export type DeltaTone = "up" | "down" | "flat";

export function deltaTone(change: number): DeltaTone {
  return Math.abs(change) < FLAT_BELOW ? "flat" : change > 0 ? "up" : "down";
}

const TONE_CLASS: Record<DeltaTone, string> = {
  up: "bg-highlight text-highlight-foreground",
  down: "bg-attention-soft text-attention",
  flat: "bg-muted text-muted-foreground",
};

/** £ amounts the way an analyst says them: £729k, £1.24M, £950, −£12k. */
export function money(v: number): string {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  const sign = v < 0 && a >= 0.5 ? "−" : "";
  if (a >= 1e6) return `${sign}£${(a / 1e6).toFixed(a >= 1e8 ? 0 : 2)}M`;
  if (a >= 9_999.5) return `${sign}£${Math.round(a / 1e3)}k`;
  if (a >= 1e3) return `${sign}£${(a / 1e3).toFixed(1)}k`;
  return `${sign}£${Math.round(a)}`;
}

export function signedMoney(v: number): string {
  return Math.abs(v) < 0.5 ? money(0) : v > 0 ? `+${money(v)}` : money(v);
}

/** The pill text: "+£12k vs plan", "−£3.2k vs plan" or "same as plan". */
export function deltaText(change: number): string {
  return deltaTone(change) === "flat" ? "same as plan" : `${signedMoney(change)} vs plan`;
}

function pounds(v: number): string {
  return Number.isFinite(v) ? `£${v.toFixed(2)}` : "—";
}

/** How a slider's spend compares with the plan: "as planned", "+20% vs plan", "switched off". */
export function spendWords(m: number): string {
  if (Math.abs(m - 1) < 1e-9) return "as planned";
  if (m <= 0) return "switched off";
  const pct = Math.round((m - 1) * 100);
  return `${pct > 0 ? "+" : "−"}${Math.abs(pct)}% vs plan`;
}

function roundStep(v: number): number {
  return Math.round(v / PLAYGROUND_STEP) * PLAYGROUND_STEP;
}

/** Tell the checklist once per card that a budget slider moved (D25). */
function useFirstMove(source: string): () => void {
  const told = useRef(false);
  return () => {
    if (told.current) return;
    told.current = true;
    emitOnboarding("whatif_moved", { source });
  };
}

// ----------------------------------------------------------------------------- the chart

/** A smooth path through `pts` that never overshoots (monotone cubic, Fritsch-Carlson). */
export function smoothPath(pts: readonly (readonly [number, number])[]): string {
  const n = pts.length;
  if (n === 0) return "";
  const f = (v: number) => (Math.round(v * 10) / 10).toString();
  if (n === 1) return `M${f(pts[0]![0])} ${f(pts[0]![1])}`;
  const dx: number[] = [];
  const m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const d = pts[i + 1]![0] - pts[i]![0];
    dx.push(d);
    m.push(d === 0 ? 0 : (pts[i + 1]![1] - pts[i]![1]) / d);
  }
  const t: number[] = [m[0]!];
  for (let i = 1; i < n - 1; i++) t.push(m[i - 1]! * m[i]! <= 0 ? 0 : (m[i - 1]! + m[i]!) / 2);
  t.push(m[n - 2]!);
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) {
      t[i] = 0;
      t[i + 1] = 0;
      continue;
    }
    const a = t[i]! / m[i]!;
    const b = t[i + 1]! / m[i]!;
    const s = a * a + b * b;
    if (s > 9) {
      const tau = 3 / Math.sqrt(s);
      t[i] = tau * a * m[i]!;
      t[i + 1] = tau * b * m[i]!;
    }
  }
  let d = `M${f(pts[0]![0])} ${f(pts[0]![1])}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i]!;
    const [x1, y1] = pts[i + 1]!;
    const h = dx[i]! / 3;
    d += `C${f(x0 + h)} ${f(y0 + t[i]! * h)} ${f(x1 - h)} ${f(y1 - t[i + 1]! * h)} ${f(x1)} ${f(y1)}`;
  }
  return d;
}

/** 0 and evenly spaced round ticks up to at least `max`. */
export function niceTicks(max: number, count = 4): number[] {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const p = 10 ** Math.floor(Math.log10(raw));
  const r = raw / p;
  const step = (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * p;
  const ticks: number[] = [];
  for (let v = 0; v <= max + step * 0.999; v += step) ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

interface ChartSeries {
  curve: ChannelCurve;
  color: SeriesColor;
  /** The operating point: weekly spend and sales added. */
  at: { spend: number; added: number };
}

const W = 400;
const H = 250;
const PAD = { l: 48, r: 14, t: 16, b: 32 };

function CurvesChart({
  grid,
  series,
  band = false,
  zones = [],
  label,
}: {
  grid: ResponseGrid;
  series: ChartSeries[];
  band?: boolean;
  zones?: [number, number][];
  label: string;
}) {
  const xMax = Math.max(...series.map((s) => s.curve.spend[s.curve.spend.length - 1] ?? 0), 1);
  const yMax = Math.max(...series.map((s) => Math.max(...(band ? s.curve.hi : s.curve.mean))), 1);
  const xt = niceTicks(xMax);
  const yt = niceTicks(yMax);
  const xTop = xt[xt.length - 1] as number;
  const yTop = yt[yt.length - 1] as number;
  const sx = (v: number) => PAD.l + ((W - PAD.l - PAD.r) * v) / xTop;
  const sy = (v: number) => H - PAD.b - ((H - PAD.t - PAD.b) * v) / yTop;
  const fine = (curve: ChannelCurve, ys: number[]) => {
    // Sample the interpolated curve finely so the smooth path follows the grid closely.
    const xs = grid.multipliers;
    const max = xs[xs.length - 1] as number;
    const pts: [number, number][] = [];
    for (let m = 0; m <= max + 1e-9; m += 0.1) {
      const mm = Math.min(max, m);
      pts.push([sx(interp(xs, curve.spend, mm)), sy(interp(xs, ys, mm))]);
    }
    return pts;
  };
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="block h-auto w-full overflow-visible" data-slot="response-curves">
      <g aria-hidden>
        {yt.map((v) => (
          <g key={`y${v}`}>
            <line x1={PAD.l} x2={W - PAD.r} y1={sy(v)} y2={sy(v)} className="stroke-chart-grid" strokeWidth={1} />
            <text x={PAD.l - 8} y={sy(v) + 4} textAnchor="end" className="fill-hint text-[11px] font-medium tabular">
              {money(v)}
            </text>
          </g>
        ))}
        {xt.map((v) => (
          <text key={`x${v}`} x={sx(v)} y={H - 10} textAnchor="middle" className="fill-hint text-[11px] font-medium tabular">
            {money(v)}
          </text>
        ))}
        <text x={W - PAD.r} y={H - PAD.b - 8} textAnchor="end" className="fill-hint text-[11px] font-medium">
          weekly spend
        </text>
        <text x={PAD.l + 4} y={PAD.t - 4} className="fill-hint text-[11px] font-medium">
          sales added a week
        </text>
        {zones.map(([a, b]) => (
          <rect
            key={`z${a}`}
            data-slot="pays-back"
            x={sx(a)}
            y={PAD.t}
            width={Math.max(2, sx(b) - sx(a))}
            height={H - PAD.t - PAD.b}
            rx={6}
            className="fill-zone-optimal-tint stroke-zone-optimal"
            strokeWidth={1.5}
          />
        ))}
        {band
          ? series.map((s) => {
              const upper = fine(s.curve, s.curve.hi);
              const lower = fine(s.curve, s.curve.lo).reverse();
              return (
                <path
                  key={`b${s.curve.channel}`}
                  data-slot="band"
                  d={`${smoothPath(upper)}L${smoothPath(lower).slice(1)}Z`}
                  className={cn(FILL[s.color], "opacity-20")}
                />
              );
            })
          : null}
        {series.map((s) => (
          <path key={`c${s.curve.channel}`} d={smoothPath(fine(s.curve, s.curve.mean))} fill="none" strokeWidth={3} strokeLinecap="round" className={STROKE[s.color]} />
        ))}
        {series.map((s) => (
          <g key={`d${s.curve.channel}`} data-slot="operating-point" data-channel={s.curve.channel}>
            <line
              x1={sx(s.at.spend)}
              x2={sx(s.at.spend)}
              y1={sy(0)}
              y2={sy(s.at.added)}
              strokeWidth={1.5}
              strokeDasharray="3 4"
              className={STROKE[s.color]}
            />
            <circle cx={sx(s.at.spend)} cy={sy(s.at.added)} r={7} strokeWidth={3} className={cn("fill-card", STROKE[s.color])} />
          </g>
        ))}
      </g>
    </svg>
  );
}

// ----------------------------------------------------------------------------- shared bits

function ExampleChip({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex h-6 items-center rounded-full bg-highlight-soft px-2.5 text-xs font-semibold text-highlight-ink", className)}>Example data</span>
  );
}

function ResetButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  return (
    <Button type="button" variant="ghost" size="sm" className="rounded-full bg-muted text-foreground hover:bg-surface-hover" onClick={onClick} disabled={disabled}>
      <RotateCcw /> Reset to plan
    </Button>
  );
}

/** "Open the real model": the tenant's sample once its fit is ready (switches to its project). */
function OpenRealModel() {
  const tenant = useOnboardingTenant();
  const samples = useSamples({ enabled: tenant !== null });
  const ready = samples.data?.find((s) => s.state === "ready" && s.project_id && s.model_id && s.model_version != null);
  if (!tenant || !ready) return null;
  return <OpenRealModelButton tenant={tenant} sample={ready} />;
}

function OpenRealModelButton({ tenant, sample }: { tenant: string; sample: SampleInfo }) {
  const navigate = useNavigate();
  return (
    <Button
      type="button"
      size="sm"
      className="rounded-full"
      onClick={() => {
        useScope.getState().select({ tenantId: tenant, projectId: sample.project_id ?? "" });
        emitOnboarding("sample_opened", { sample: sample.id, source: "playground" });
        void navigate({ to: "/models/$modelId/versions/$version", params: { modelId: sample.model_id ?? "", version: String(sample.model_version ?? 1) } });
      }}
    >
      Open the real model <ArrowRight />
    </Button>
  );
}

// ----------------------------------------------------------------------------- Home playground

/**
 * Home's "What if you moved the budget?" card: one slider per channel (weekly spend), the
 * predicted weekly sales with a delta pill, the 90% band of the change, the response curves
 * with each channel's operating point and what the next £1 returns. Instant, in the browser.
 */
export function Playground({ className, anchor = "home.playground" }: { className?: string; anchor?: string }) {
  const grid = useMemo(demoGrid, []);
  const curves = useMemo(() => channelCurves(grid), [grid]);
  const order = useMemo(() => orderedChannels(grid), [grid]);
  const [mult, setMult] = useState(() => planMultipliers(grid));
  const view = useMemo(() => playgroundView(grid, curves, mult), [grid, curves, mult]);
  const moved = useFirstMove("home");
  const titleId = useId();
  const npId = useId();
  const changed = Object.values(mult).some((m) => Math.abs(m - 1) > 1e-9);
  const tone = deltaTone(view.change.mid);
  const colorOf = (ch: string): SeriesColor => COLORS[order.indexOf(ch) % COLORS.length] as SeriesColor;
  const stateOf = (ch: string) => view.channels.find((c) => c.channel === ch) as ChannelState;
  const min = grid.multipliers[0] as number;
  const max = grid.multipliers[grid.multipliers.length - 1] as number;

  let sub = `Predicted weekly sales. You spend ${money(view.weeklySpend)} a week`;
  if (Math.abs(view.spendChange) >= 1) sub += `, ${signedMoney(view.spendChange)} vs plan`;
  sub += ".";
  if (view.returnOnChange !== null) sub += ` Each extra £1 returns ${pounds(view.returnOnChange)}.`;

  return (
    <section
      data-testid="playground"
      data-tour={anchor}
      aria-labelledby={titleId}
      className={cn("grid gap-7 rounded-2xl border bg-card p-7 text-card-foreground max-sm:gap-6 max-sm:p-5 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]", className)}
    >
      <header className="flex flex-wrap items-start justify-between gap-4 lg:col-span-2">
        <div className="grid max-w-[52ch] gap-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id={titleId} className="text-2xl leading-tight font-semibold">
              What if you moved the budget?
            </h2>
            <ExampleChip />
          </div>
          <p className="text-muted-foreground">
            Slide the weekly spend on each channel and watch sales respond. These curves come from a real fit on our sample data. The dot shows where each
            channel sits on its curve. Try pushing TV past its sweet spot.
          </p>
        </div>
        <ResetButton disabled={!changed} onClick={() => setMult(planMultipliers(grid))} />
      </header>

      <div className="grid content-start gap-6">
        <ul className="grid gap-5" aria-label="Weekly spend per channel">
          {order.map((ch) => {
            const s = stateOf(ch);
            const color = colorOf(ch);
            const words = spendWords(s.multiplier);
            return (
              <li key={ch} className="grid gap-1.5" data-channel={ch}>
                {/* label above value on every row: rows keep one height whatever wraps */}
                <div className="grid gap-0.5">
                  <span className="flex min-w-0 items-center gap-2 font-semibold">
                    <span aria-hidden className={cn("size-2.5 rounded-[3px]", SWATCH[color])} />
                    {channelLabel(ch)}
                  </span>
                  <span className="text-xs font-semibold text-muted-foreground tabular">
                    <b className="text-sm text-foreground">{money(s.spend)}</b> a week, {words}
                  </span>
                </div>
                <ValueSlider
                  label={`Weekly spend on ${channelLabel(ch)}`}
                  valueText={`${money(s.spend)} a week, ${words}`}
                  value={s.multiplier}
                  min={min}
                  max={max}
                  step={PLAYGROUND_STEP}
                  color={color}
                  onValueChange={(v) => {
                    setMult((x) => ({ ...x, [ch]: roundStep(v) }));
                    moved();
                  }}
                />
              </li>
            );
          })}
        </ul>
        <div className="grid gap-2">
          <p id={npId} className="text-xs text-muted-foreground">
            What the next £1 would return
          </p>
          <div role="group" aria-labelledby={npId} className="grid grid-cols-3 gap-2">
            {order.map((ch) => {
              const s = stateOf(ch);
              return (
                <div key={ch} className="min-w-0 truncate rounded-lg bg-muted px-3 py-2.5 text-xs text-muted-foreground" title={channelLabel(ch)}>
                  <b className={cn("block font-display text-lg leading-tight font-semibold tabular", s.nextPound >= 1 ? "text-highlight-ink" : "text-foreground")}>
                    {pounds(s.nextPound)}
                  </b>
                  {channelLabel(ch)}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="grid content-start gap-4">
        <div data-testid="playground-result" className="grid gap-3">
          <div aria-live="polite" aria-atomic="true" className="grid gap-1">
            <div className="flex flex-wrap items-baseline gap-3">
              {view.weeklySales !== null ? (
                <CountUp value={view.weeklySales} format={money} className="font-display text-4xl leading-none font-[750] tracking-[-0.03em]" />
              ) : null}
              <span data-testid="playground-delta" data-tone={tone} className={cn("rounded-full px-2.5 py-1 text-sm font-semibold tabular", TONE_CLASS[tone])}>
                {deltaText(view.change.mid)}
              </span>
            </div>
            <p className="text-xs text-muted-foreground tabular">{sub}</p>
          </div>
          <ChangeBand
            lo={view.change.lo}
            mid={view.change.mid}
            hi={view.change.hi}
            label={`Change in weekly sales: ${signedMoney(view.change.mid)}, 90% likely between ${signedMoney(view.change.lo)} and ${signedMoney(view.change.hi)}`}
          />
        </div>
        <CurvesChart
          grid={grid}
          series={order.map((ch) => {
            const s = stateOf(ch);
            return { curve: curves[grid.channels.indexOf(ch)] as ChannelCurve, color: colorOf(ch), at: { spend: s.spend, added: s.added } };
          })}
          label={`Response curves for ${order.map(channelLabel).join(", ")}: sales added a week against weekly spend, with a dot at the spend you picked`}
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-hint">Example data: {grid.draws} posterior draws over {grid.periods} weeks of a synthetic retailer.</p>
          <OpenRealModel />
        </div>
      </div>
    </section>
  );
}

// ----------------------------------------------------------------------------- Catalog mini playground

/**
 * The Catalog's compact playground on the marketing-mix model card: pick a channel, slide its
 * weekly spend along its response curve, show the 90% range and shade where the next £1
 * still pays back. Same demo grid and maths as {@link Playground}.
 */
export function MiniPlayground({ className, anchor = "catalog.playground" }: { className?: string; anchor?: string }) {
  const grid = useMemo(demoGrid, []);
  const curves = useMemo(() => channelCurves(grid), [grid]);
  const order = useMemo(() => orderedChannels(grid), [grid]);
  const [channel, setChannel] = useState(order[0] ?? grid.channels[0] ?? "");
  const [mult, setMult] = useState(() => planMultipliers(grid));
  const [band, setBand] = useState(true);
  const [zone, setZone] = useState(false);
  const moved = useFirstMove("catalog");
  const titleId = useId();
  const bandId = useId();
  const zoneId = useId();
  const view = useMemo(() => playgroundView(grid, curves, mult), [grid, curves, mult]);
  const c = grid.channels.indexOf(channel);
  const curve = curves[c] as ChannelCurve;
  const s = view.channels.find((x) => x.channel === channel) as ChannelState;
  const color = COLORS[order.indexOf(channel) % COLORS.length] as SeriesColor;
  const xs = grid.multipliers;
  const lo = interp(xs, curve.lo, s.multiplier);
  const hi = interp(xs, curve.hi, s.multiplier);
  const zones = useMemo(() => paysBackRanges(grid, curve), [grid, curve]);
  const tone = deltaTone(view.change.mid);
  const name = channelLabel(channel);
  const words = spendWords(s.multiplier);
  const changed = Math.abs(s.multiplier - 1) > 1e-9;

  let zoneText = "";
  if (zone) {
    zoneText = zones.length
      ? ` The next £1 pays back between ${zones.map(([a, b]) => `${money(a)} and ${money(b)}`).join(", and between ")} a week.`
      : ` On this example, ${name} never returns £1 for the next £1.`;
  }

  return (
    <section
      data-testid="playground"
      data-variant="catalog"
      data-tour={anchor}
      aria-labelledby={titleId}
      className={cn("grid gap-4 rounded-2xl border bg-card p-5 text-card-foreground", className)}
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="grid gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 id={titleId} className="text-xl leading-tight font-semibold">
              Try the curve
            </h3>
            <ExampleChip />
          </div>
          <p className="text-xs text-muted-foreground">Pick a channel and slide its spend. This is the kind of answer the model gives you.</p>
        </div>
        <ResetButton
          disabled={!changed}
          onClick={() => setMult((x) => ({ ...x, [channel]: 1 }))}
        />
      </header>

      <div role="group" aria-label="Channel" className="flex flex-wrap gap-1.5">
        {order.map((ch) => (
          <button
            key={ch}
            type="button"
            aria-pressed={ch === channel}
            onClick={() => setChannel(ch)}
            className={cn(
              "focus-ring inline-flex h-8 items-center gap-1.5 rounded-full border-[1.5px] border-border-strong px-3 text-xs font-semibold transition-colors hover:bg-muted",
              ch === channel && "border-foreground bg-foreground text-background hover:bg-foreground",
            )}
          >
            <span aria-hidden className={cn("size-2 rounded-full", SWATCH[COLORS[order.indexOf(ch) % COLORS.length] as SeriesColor])} />
            {channelLabel(ch)}
          </button>
        ))}
      </div>

      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs font-semibold text-muted-foreground tabular">
          <span>Weekly spend on {name}</span>
          <span>
            <b className="text-sm text-foreground">{money(s.spend)}</b> a week, {words}
          </span>
        </div>
        <ValueSlider
          label={`Weekly spend on ${name}`}
          valueText={`${money(s.spend)} a week, ${words}`}
          value={s.multiplier}
          min={xs[0] as number}
          max={xs[xs.length - 1] as number}
          step={PLAYGROUND_STEP}
          color={color}
          onValueChange={(v) => {
            setMult((x) => ({ ...x, [channel]: roundStep(v) }));
            moved();
          }}
        />
      </div>

      <CurvesChart
        grid={grid}
        series={[{ curve, color, at: { spend: s.spend, added: s.added } }]}
        band={band}
        zones={zone ? zones : []}
        label={`Response curve for ${name}: sales added a week against weekly spend${band ? ", with its 90% range" : ""}${zone ? ", with the spend where it pays back shaded" : ""}`}
      />

      <div className="flex flex-wrap gap-x-5 gap-y-2">
        <label htmlFor={bandId} className="flex items-center gap-2 text-xs font-semibold">
          <Switch id={bandId} checked={band} onCheckedChange={setBand} /> Show 90% range
        </label>
        <label htmlFor={zoneId} className="flex items-center gap-2 text-xs font-semibold">
          <Switch id={zoneId} checked={zone} onCheckedChange={setZone} /> Shade where it pays back
        </label>
      </div>

      <div data-testid="playground-result" aria-live="polite" aria-atomic="true" className="grid gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-display text-2xl leading-none font-semibold tabular">{money(s.added)}</span>
          <span className="text-xs text-muted-foreground">of sales a week from {name}</span>
          <span data-testid="playground-delta" data-tone={tone} className={cn("rounded-full px-2.5 py-0.5 text-xs font-semibold tabular", TONE_CLASS[tone])}>
            {deltaText(view.change.mid)}
          </span>
        </div>
        <p className="text-xs text-muted-foreground tabular">
          90% likely between {money(lo)} and {money(hi)}. The next £1 returns {pounds(s.nextPound)}.{zoneText}
        </p>
      </div>
      <div className="flex justify-end empty:hidden">
        <OpenRealModel />
      </div>
    </section>
  );
}

export default Playground;
