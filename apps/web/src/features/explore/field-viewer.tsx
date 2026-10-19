/**
 * The interactive viewer of one gridded output (`FieldEntry` of the `fields` view): a canvas
 * raster in the field's colour map, a switch between the posterior mean, the sd, mean÷sd, the
 * 5% and 95% quantiles and single posterior draws, a frame slider, a draw scrubber that plays
 * the draws one after the other (hypothetical outcome plots), arrows for vector fields, a
 * colour bar and a readout of the cell under the pointer (or under the keyboard cursor: focus
 * the map and use the arrow keys). Strings in the view come from the user's model: text only.
 *
 * v0.10, `mode="data"` (the Data Explorer's `images` section): the raster is the data itself,
 * so there is one statistic ("Value") and no draws; an optional mask is shaded over the image,
 * and a strip of the per-frame statistics follows the frame slider (and picks a frame).
 */
import { Pause, Play } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { availableStats, cellAt, colorTicks, decodeRaster, displayRange, frameStatIndex, frameValues, gradientCss, maskWord, paint, paintMask, rasterRange, ROBUST_FACTOR, robustRange, type ColorScale, type FieldStat } from "./fields";
import { num } from "./ui";
import type { FieldArrows, FieldEntry, FieldRaster, FrameStats } from "./views";

export const STAT_LABEL: Record<FieldStat, string> = {
  mean: "Mean",
  sd: "Std. dev.",
  ratio: "Mean ÷ sd",
  q05: "5% quantile",
  q95: "95% quantile",
  draw: "Draws",
};

const STAT_HINT: Record<FieldStat, string> = {
  mean: "The posterior mean of each cell.",
  sd: "The posterior standard deviation of each cell. High values show where the model is not sure.",
  ratio: "The mean divided by the standard deviation. Values near 0 show cells that the data does not fix.",
  q05: "The model expects 95% of the values in each cell to be above this value.",
  q95: "The model expects 95% of the values in each cell to be below this value.",
  draw: "Each draw is one possible field that agrees with the data. Play the draws to see how much the field changes.",
};

const PLAY_MS = 450;

/** What the viewer shows: posterior statistics of a model output, or the values of the data. */
export type FieldViewerMode = "model" | "data";

const DATA_HINT = "The value of each cell in the data.";

function label(v: string | number | boolean | null | undefined): string {
  return v === null || v === undefined ? "—" : String(v);
}

function Arrows({ arrows, width, height, origin }: { arrows: FieldArrows; width: number; height: number; origin: "upper" | "lower" }) {
  const xs = arrows.x;
  const ys = arrows.y;
  const step = Math.min(xs.length > 1 ? xs[1]! - xs[0]! : width, ys.length > 1 ? ys[1]! - ys[0]! : height);
  let max = 0;
  arrows.u.forEach((row, i) =>
    row.forEach((u, j) => {
      const v = arrows.v[i]?.[j];
      if (u != null && v != null) max = Math.max(max, Math.hypot(u, v));
    }),
  );
  if (!(max > 0)) return null;
  const scale = (0.9 * Math.max(step, 1)) / max;
  const lines: { x1: number; y1: number; x2: number; y2: number; key: string }[] = [];
  arrows.u.forEach((row, i) =>
    row.forEach((u, j) => {
      const v = arrows.v[i]?.[j];
      if (u == null || v == null) return;
      const cx = xs[j]! + 0.5;
      const cy = origin === "lower" ? height - (ys[i]! + 0.5) : ys[i]! + 0.5;
      const dx = u * scale;
      const dy = (origin === "lower" ? -v : v) * scale;
      lines.push({ x1: cx - dx / 2, y1: cy - dy / 2, x2: cx + dx / 2, y2: cy + dy / 2, key: `${i}-${j}` });
    }),
  );
  const head = Math.max(step, 1) * 0.28;
  const path = (l: (typeof lines)[number]) => {
    const a = Math.atan2(l.y2 - l.y1, l.x2 - l.x1);
    const len = Math.hypot(l.x2 - l.x1, l.y2 - l.y1);
    const h = Math.min(head, len * 0.6);
    const p1 = `${l.x2 - h * Math.cos(a - 0.45)},${l.y2 - h * Math.sin(a - 0.45)}`;
    const p2 = `${l.x2 - h * Math.cos(a + 0.45)},${l.y2 - h * Math.sin(a + 0.45)}`;
    return `M${l.x1},${l.y1}L${l.x2},${l.y2}M${p1}L${l.x2},${l.y2}L${p2}`;
  };
  return (
    <svg className="pointer-events-none absolute inset-0 size-full" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden data-testid="field-arrows">
      {lines.map((l) => (
        <g key={l.key}>
          <path d={path(l)} fill="none" stroke="rgb(0 0 0 / 0.55)" strokeWidth={3} strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          <path d={path(l)} fill="none" stroke="white" strokeWidth={1.4} strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        </g>
      ))}
    </svg>
  );
}

function ColorBar({ scale, units, title, clipped = false }: { scale: ColorScale; units?: string | null | undefined; title: string; clipped?: boolean }) {
  const ticks = colorTicks(scale);
  return (
    <figure className="grid gap-1" aria-label={`Colour scale: ${title}`}>
      <div className="h-3 rounded-sm border" style={{ backgroundImage: gradientCss(scale.colormap) }} aria-hidden />
      <div className="flex justify-between text-[11px] text-muted-foreground tabular">
        {ticks.map((t, i) => (
          <span key={i}>{num(t, 3)}</span>
        ))}
      </div>
      <figcaption className="text-[11px] text-muted-foreground">
        {title}
        {units ? ` (${units})` : ""}
        {scale.log ? ", log scale" : ""}
        {clipped ? ". The colours cover 98% of the values. The lowest 1% and the highest 1% show the colours at the two ends." : ""}
      </figcaption>
    </figure>
  );
}

/**
 * The per-frame statistics as a small strip (mean, with the min–max band), with a marker at
 * the frame shown. A click picks the shown frame nearest to that position. Numbers only.
 */
function FrameStrip({ stats, at, frameDim, shownLabels, onPick }: { stats: FrameStats; at: number; frameDim: string; shownLabels: readonly unknown[]; onPick: (frame: number) => void }) {
  const n = stats.coords.length;
  const W = 400;
  const H = 48;
  const vals = [...stats.mean, ...(stats.min ?? []), ...(stats.max ?? [])].filter((v): v is number => v !== null && Number.isFinite(v));
  if (n < 2 || !vals.length) return null;
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => (hi > lo ? H - 2 - ((v - lo) / (hi - lo)) * (H - 4) : H / 2);
  const line = (arr: readonly (number | null)[] | undefined) =>
    (arr ?? [])
      .map((v, i) => (v !== null && Number.isFinite(v) ? `${x(i).toFixed(1)},${y(v).toFixed(1)}` : null))
      .filter((p) => p !== null)
      .join(" ");
  const band =
    stats.min && stats.max
      ? [...stats.max.map((v, i) => (v !== null ? `${x(i).toFixed(1)},${y(v).toFixed(1)}` : null)), ...stats.min.map((v, i) => (v !== null ? `${x(i).toFixed(1)},${y(v).toFixed(1)}` : null)).reverse()].filter((p) => p !== null).join(" ")
      : "";
  const shownIdx = shownLabels.map((l) => frameStatIndex(stats.coords, l));
  const pick = (frac: number) => {
    const target = frac * (n - 1);
    let best = 0;
    let dist = Infinity;
    shownIdx.forEach((i, f) => {
      const d = Math.abs((i >= 0 ? i : (f / Math.max(1, shownIdx.length - 1)) * (n - 1)) - target);
      if (d < dist) {
        dist = d;
        best = f;
      }
    });
    onPick(best);
  };
  return (
    <figure className="grid gap-1" data-testid="frame-strip">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-12 w-full cursor-pointer rounded-sm border bg-muted/30"
        role="img"
        aria-label={`The mean of every ${frameDim} (${n}), with the minimum and maximum`}
        onClick={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          if (box.width) pick((e.clientX - box.left) / box.width);
        }}
      >
        {band ? <polygon points={band} className="fill-primary/15" /> : null}
        <polyline points={line(stats.mean)} fill="none" className="stroke-primary" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        {at >= 0 ? <line x1={x(at)} x2={x(at)} y1={0} y2={H} className="stroke-foreground" strokeWidth={1.5} vectorEffect="non-scaling-stroke" data-testid="frame-strip-marker" /> : null}
      </svg>
      <figcaption className="text-[11px] text-muted-foreground">
        Mean of each {frameDim} ({num(n)}{stats.downsampled ? ", thinned" : ""}), with the range from the minimum to the maximum. Click to go to a {frameDim}.
      </figcaption>
    </figure>
  );
}

export interface FieldViewerProps {
  field: FieldEntry;
  /** `data`: the Data Explorer's images (one statistic, "Value"). Default `model`. */
  mode?: FieldViewerMode;
  /** Data mode: a 0/1 raster of the same size; the cells at 0 are shaded. */
  mask?: FieldRaster | null | undefined;
  /** Data mode: the name of the mask variable. */
  maskLabel?: string | null | undefined;
  /** Data mode: the statistics of every frame, drawn as a strip that follows the slider. */
  frameStats?: FrameStats | null | undefined;
}

/** One field: the canvas, its controls, the colour bar and the cell readout. */
export function FieldViewer({ field, mode = "model", mask = null, maskLabel = null, frameStats = null }: FieldViewerProps) {
  const ids = useId();
  const data = mode === "data";
  const stats = useMemo((): FieldStat[] => (data ? ["mean"] : availableStats(field)), [field, data]);
  const [stat, setStat] = useState<FieldStat>("mean");
  const [frame, setFrame] = useState(0);
  const [draw, setDraw] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [arrowsOn, setArrowsOn] = useState(true);
  const [cell, setCell] = useState<{ row: number; col: number } | null>(null);
  const [maskOn, setMaskOn] = useState(true);
  const [fullRange, setFullRange] = useState(false);
  const canvas = useRef<HTMLCanvasElement>(null);
  const maskCanvas = useRef<HTMLCanvasElement>(null);

  const { width, height } = field;
  const frames = field.frames;
  const fr = frames[Math.min(frame, frames.length - 1)]!;
  const nDraws = fr.draws?.length ?? 0;
  const enc = field.encoding ?? "linear";
  const log = Boolean(field.log);
  const colormap = field.colormap ?? "viridis";
  const origin = field.origin ?? "upper";
  const current = stats.includes(stat) ? stat : "mean";

  // Decoded rasters, cached per (frame, statistic, draw).
  // (the parent remounts the viewer with a new key for another field)
  const [cache] = useState(() => new Map<string, Float32Array | null>());
  const values = (f: number, s: FieldStat, d = 0): Float32Array | null => {
    const key = `${f}:${s}:${d}`;
    if (!cache.has(key)) cache.set(key, frameValues(field, frames[f]!, s, d));
    return cache.get(key) ?? null;
  };
  const shown = values(frame, current, draw);

  // One colour scale per statistic group, shared by every frame and draw.
  const fullScale = useMemo((): ColorScale | null => {
    if (current === "ratio") {
      const r = displayRange([frameValues(field, fr, "ratio")], false, colormap);
      return r ? { lo: r[0], hi: r[1], log: false, colormap } : null;
    }
    if (current === "sd") {
      const r = rasterRange(frames.map((f) => f.sd), enc, log, "viridis");
      return r ? { lo: r[0], hi: r[1], log, colormap: colormap === "diverging" ? "viridis" : colormap } : null;
    }
    const group = frames.flatMap((f) => [f.mean, f.q05, f.q95, ...(f.draws ?? [])]);
    const r = rasterRange(group, enc, log, colormap);
    return r ? { lo: r[0], hi: r[1], log, colormap } : null;
  }, [current, field, fr, frames, enc, log, colormap]);
  // Data mode: the 1% to 99% range of the values of every frame when the full range is much
  // wider (a few bright cells); a check box shows the full range.
  const robust = useMemo((): ColorScale | null => {
    if (!data || !fullScale) return null;
    const r = robustRange(frames.map((f) => frameValues(field, f, "mean")), log, colormap);
    if (!r || fullScale.hi - fullScale.lo <= ROBUST_FACTOR * (r[1] - r[0])) return null;
    return { ...fullScale, lo: r[0], hi: r[1] };
  }, [data, fullScale, frames, field, log, colormap]);
  const scale = robust && !fullRange ? robust : fullScale;

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    ctx.clearRect(0, 0, width, height);
    if (!shown || !scale) return;
    ctx.putImageData(new ImageData(paint(shown, width, height, scale, origin), width, height), 0, 0);
  }, [shown, scale, width, height, origin]);

  // Data mode: the mask, decoded once (only when it has the image's size).
  const maskValues = useMemo(() => {
    if (!data || !mask) return null;
    const m = decodeRaster(mask);
    return m.length === width * height ? m : null;
  }, [data, mask, width, height]);
  useEffect(() => {
    const el = maskCanvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    ctx.clearRect(0, 0, width, height);
    if (maskValues && maskOn) ctx.putImageData(new ImageData(paintMask(maskValues, width, height, origin), width, height), 0, 0);
  }, [maskValues, maskOn, width, height, origin]);

  useEffect(() => {
    if (!playing || current !== "draw" || nDraws < 2) return;
    const t = window.setInterval(() => setDraw((d) => (d + 1) % nDraws), PLAY_MS);
    return () => window.clearInterval(t);
  }, [playing, current, nDraws]);

  const onPointer = (e: PointerEvent<HTMLDivElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    if (!box.width || !box.height) return;
    setCell(cellAt((e.clientX - box.left) / box.width, (e.clientY - box.top) / box.height, width, height, origin));
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step: Record<string, [number, number]> = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [origin === "lower" ? 1 : -1, 0], ArrowDown: [origin === "lower" ? -1 : 1, 0] };
    const s = step[e.key];
    if (!s) return;
    e.preventDefault();
    const big = e.shiftKey ? 10 : 1;
    setCell((c) => {
      const base = c ?? { row: Math.floor(height / 2), col: Math.floor(width / 2) };
      return { row: Math.max(0, Math.min(height - 1, base.row + s[0] * big)), col: Math.max(0, Math.min(width - 1, base.col + s[1] * big)) };
    });
  };

  const at = cell ? cell.row * width + cell.col : -1;
  const read = (s: FieldStat, d = 0) => (at >= 0 ? (values(frame, s, d)?.[at] ?? NaN) : NaN);
  const frameDim = field.frame_dim ?? "frame";
  const vector = field.kind === "vector";
  const statLabel = data ? "Value" : STAT_LABEL[current];
  const title = `${statLabel}${current === "draw" ? ` ${draw + 1}` : ""} of ${vector ? `the size of ${field.output}` : field.output}`;
  const statAt = frameStats ? frameStatIndex(frameStats.coords, fr.label) : -1;
  const cursorY = cell ? (origin === "lower" ? height - 1 - cell.row : cell.row) : 0;

  return (
    <div className="grid gap-4" data-testid="field-viewer">
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        {data ? null : (
        <div className="grid gap-1">
          <span className="text-xs font-medium text-muted-foreground" id={`${ids}-stat`}>
            Statistic
          </span>
          <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`${ids}-stat`}>
            {stats.map((s) => (
              <Button
                key={s}
                size="sm"
                variant={s === current ? "secondary" : "ghost"}
                className={cn("h-7 px-2.5", s === current && "ring-1 ring-border")}
                aria-pressed={s === current}
                title={STAT_HINT[s]}
                onClick={() => {
                  setStat(s);
                  if (s !== "draw") setPlaying(false);
                }}
              >
                {STAT_LABEL[s]}
              </Button>
            ))}
          </div>
        </div>
        )}
        {vector ? (
          <label className="flex items-center gap-2 pb-1 text-[13px]">
            <input type="checkbox" className="accent-primary" checked={arrowsOn} onChange={(e) => setArrowsOn(e.target.checked)} />
            Show the arrows
          </label>
        ) : null}
        {robust ? (
          <label className="flex items-center gap-2 pb-1 text-[13px]">
            <input type="checkbox" className="accent-primary" checked={fullRange} onChange={(e) => setFullRange(e.target.checked)} />
            Colours for the full range
          </label>
        ) : null}
        {maskValues ? (
          <label className="flex items-center gap-2 pb-1 text-[13px]">
            <input type="checkbox" className="accent-primary" checked={maskOn} onChange={(e) => setMaskOn(e.target.checked)} />
            Shade the cells outside the mask{maskLabel ? ` (${maskLabel})` : ""}
          </label>
        ) : null}
      </div>

      {frames.length > 1 ? (
        <div className="grid gap-1">
          <label htmlFor={`${ids}-frame`} className="flex justify-between text-xs font-medium text-muted-foreground">
            <span>{frameDim}</span>
            <span className="tabular text-foreground">{label(fr.label)}</span>
          </label>
          <input
            id={`${ids}-frame`}
            type="range"
            min={0}
            max={frames.length - 1}
            value={frame}
            onChange={(e) => setFrame(Number(e.target.value))}
            aria-valuetext={`${frameDim} ${label(fr.label)}`}
            className="w-full accent-primary"
          />
          {frameStats ? <FrameStrip stats={frameStats} at={statAt} frameDim={frameDim} shownLabels={frames.map((f) => f.label)} onPick={setFrame} /> : null}
        </div>
      ) : null}

      {current === "draw" && nDraws > 0 ? (
        <div className="flex items-end gap-3">
          <Button size="sm" variant="outline" onClick={() => setPlaying((p) => !p)} aria-pressed={playing} disabled={nDraws < 2}>
            {playing ? <Pause /> : <Play />} {playing ? "Stop" : "Play the draws"}
          </Button>
          <div className="grid flex-1 gap-1">
            <label htmlFor={`${ids}-draw`} className="flex justify-between text-xs font-medium text-muted-foreground">
              <span>Draw</span>
              <span className="tabular text-foreground">
                {draw + 1} of {nDraws}
              </span>
            </label>
            <input
              id={`${ids}-draw`}
              type="range"
              min={0}
              max={nDraws - 1}
              value={Math.min(draw, nDraws - 1)}
              onChange={(e) => {
                setPlaying(false);
                setDraw(Number(e.target.value));
              }}
              className="w-full accent-primary"
            />
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_14rem]">
        <div className="grid min-w-0 gap-2">
          <div
            className="relative mx-auto w-full overflow-hidden rounded-md border bg-muted/40 outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
            style={{ aspectRatio: `${width} / ${height}`, maxWidth: `min(100%, ${(30 * width) / height}rem)` }}
            role="img"
            tabIndex={0}
            aria-label={`${title}. Use the arrow keys to move the cursor to a cell.`}
            aria-describedby={`${ids}-readout`}
            onPointerMove={onPointer}
            onKeyDown={onKey}
          >
            <canvas ref={canvas} width={width} height={height} className="size-full" style={{ imageRendering: "pixelated" }} />
            {maskValues ? <canvas ref={maskCanvas} width={width} height={height} className="pointer-events-none absolute inset-0 size-full" style={{ imageRendering: "pixelated" }} aria-hidden data-testid="field-mask" /> : null}
            {vector && arrowsOn && fr.arrows ? <Arrows arrows={fr.arrows} width={width} height={height} origin={origin} /> : null}
            {cell ? (
              <svg className="pointer-events-none absolute inset-0 size-full" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden>
                <rect x={cell.col} y={cursorY} width={1} height={1} fill="none" stroke="white" strokeWidth={2} vectorEffect="non-scaling-stroke" />
                <rect x={cell.col} y={cursorY} width={1} height={1} fill="none" stroke="black" strokeWidth={1} vectorEffect="non-scaling-stroke" />
              </svg>
            ) : null}
          </div>
          {scale ? <ColorBar scale={scale} units={current === "ratio" ? null : field.units} title={title} clipped={scale === robust} /> : <p className="text-[13px] text-muted-foreground">This raster has no values.</p>}
        </div>
        <dl id={`${ids}-readout`} className="grid h-fit grid-cols-[auto_1fr] gap-x-3 gap-y-1 rounded-lg border bg-card p-3 text-[13px]" aria-live="polite" data-testid="field-readout">
          {cell ? (
            <>
              <dt className="text-muted-foreground">{field.y_dim}</dt>
              <dd className="tabular">{label(field.y_coords[cell.row])}</dd>
              <dt className="text-muted-foreground">{field.x_dim}</dt>
              <dd className="tabular">{label(field.x_coords[cell.col])}</dd>
              <dt className="text-muted-foreground">{data ? "Value" : "Mean"}</dt>
              <dd className="tabular">{num(read("mean"))}</dd>
              {maskValues ? (
                <>
                  <dt className="text-muted-foreground">{maskLabel ?? "Mask"}</dt>
                  <dd>{maskWord(maskValues[at] ?? NaN)}</dd>
                </>
              ) : null}
              {fr.sd ? (
                <>
                  <dt className="text-muted-foreground">Std. dev.</dt>
                  <dd className="tabular">{num(read("sd"))}</dd>
                </>
              ) : null}
              {fr.q05 && fr.q95 ? (
                <>
                  <dt className="text-muted-foreground">90% interval</dt>
                  <dd className="tabular">
                    {num(read("q05"))} to {num(read("q95"))}
                  </dd>
                </>
              ) : null}
              {current === "draw" ? (
                <>
                  <dt className="text-muted-foreground">Draw {draw + 1}</dt>
                  <dd className="tabular">{num(read("draw", draw))}</dd>
                </>
              ) : null}
              {current === "ratio" ? (
                <>
                  <dt className="text-muted-foreground">Mean ÷ sd</dt>
                  <dd className="tabular">{num(read("ratio"))}</dd>
                </>
              ) : null}
            </>
          ) : (
            <p className="col-span-2 text-muted-foreground">Move the pointer over the map to see the values of a cell.</p>
          )}
        </dl>
      </div>
      <p className="text-xs text-muted-foreground">{data ? DATA_HINT : STAT_HINT[current]}</p>
    </div>
  );
}
