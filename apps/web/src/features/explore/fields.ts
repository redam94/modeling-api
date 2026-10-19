/**
 * Pure helpers of the field viewer (`fields` section): decoding the quantised rasters of a
 * `FieldsView` (`mapi_spec.views.FieldRaster`), colour maps, colour ranges and the pixel
 * readout. No React and no DOM here, so the decoding and colours are unit-tested.
 *
 * Encoding (see `FieldRaster`): `data` is base64 of little-endian uint16 codes, row-major.
 * Code 65535 is a missing cell; a code `q` in 0..65534 is `lo + (hi - lo) * q / 65534`, which
 * is log10 of the value when the field's `encoding` is `log10`.
 */
import type { FieldEntry, FieldFrame, FieldRaster } from "./views";

export const FIELD_NAN = 65535;
export const FIELD_LEVELS = 65534;

export type FieldEncoding = "linear" | "log10";
export type ColormapName = "viridis" | "magma" | "cividis" | "gray" | "diverging";
/** What the viewer draws: a posterior statistic, mean÷sd or one posterior draw. */
export type FieldStat = "mean" | "sd" | "ratio" | "q05" | "q95" | "draw";
export type Rgb = readonly [number, number, number];

// ------------------------------------------------------------------------------ decoding

function bytesOf(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** A raster's values in the field's units (`NaN` = missing), row-major. */
export function decodeRaster(r: Pick<FieldRaster, "lo" | "hi" | "data">, encoding: FieldEncoding = "linear"): Float32Array {
  const bytes = bytesOf(r.data);
  const n = bytes.length >> 1;
  const out = new Float32Array(n);
  const lo = r.lo ?? null;
  const hi = r.hi ?? null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i = 0; i < n; i++) {
    const q = view.getUint16(2 * i, true);
    if (q === FIELD_NAN || lo === null || hi === null) {
      out[i] = NaN;
      continue;
    }
    const v = lo + ((hi - lo) * Math.min(q, FIELD_LEVELS)) / FIELD_LEVELS;
    out[i] = encoding === "log10" ? 10 ** v : v;
  }
  return out;
}

/** The inverse of {@link decodeRaster} (for tests and fixtures). */
export function encodeRaster(values: ArrayLike<number>, encoding: FieldEncoding = "linear"): FieldRaster {
  const x = Array.from(values, (v) => (encoding === "log10" ? (v > 0 ? Math.log10(v) : NaN) : v));
  const ok = x.filter((v) => Number.isFinite(v));
  const lo = ok.length ? Math.min(...ok) : null;
  const hi = ok.length ? Math.max(...ok) : null;
  const bytes = new Uint8Array(2 * x.length);
  const view = new DataView(bytes.buffer);
  x.forEach((v, i) => {
    let q = FIELD_NAN;
    if (Number.isFinite(v) && lo !== null && hi !== null) q = hi > lo ? Math.round(((v - lo) / (hi - lo)) * FIELD_LEVELS) : 0;
    view.setUint16(2 * i, q, true);
  });
  let bin = "";
  bytes.forEach((b) => (bin += String.fromCharCode(b)));
  return { lo, hi, data: btoa(bin) };
}

/** The values of `stat` in one frame (`draw` picks `frame.draws[draw]`); null when absent. */
export function frameValues(field: Pick<FieldEntry, "encoding">, frame: FieldFrame, stat: FieldStat, draw = 0): Float32Array | null {
  const enc = (field.encoding ?? "linear");
  if (stat === "ratio") {
    if (!frame.sd) return null;
    const m = decodeRaster(frame.mean, enc);
    const s = decodeRaster(frame.sd, enc);
    return m.map((v, i) => (s[i]! > 0 ? v / s[i]! : NaN));
  }
  const r = stat === "draw" ? frame.draws?.[draw] : stat === "mean" ? frame.mean : frame[stat];
  return r ? decodeRaster(r, enc) : null;
}

/** The statistics a field can show (the order of the switch). */
export function availableStats(field: Pick<FieldEntry, "frames">): FieldStat[] {
  const f = field.frames[0];
  if (!f) return [];
  const out: FieldStat[] = ["mean"];
  if (f.sd) out.push("sd", "ratio");
  if (f.q05) out.push("q05");
  if (f.q95) out.push("q95");
  if (f.draws?.length) out.push("draw");
  return out;
}

// ------------------------------------------------------------------------------ colours

const STOPS: Record<ColormapName, readonly string[]> = {
  viridis: ["#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725"],
  magma: ["#000004", "#180f3d", "#440f76", "#721f81", "#9e2f7f", "#cd4071", "#f1605d", "#fd9668", "#feca8d", "#fcfdbf"],
  cividis: ["#00224e", "#123570", "#3b496c", "#575d6d", "#707173", "#8a8779", "#a69d75", "#c4b56c", "#e4cf5b", "#fee838"],
  gray: ["#000000", "#ffffff"],
  diverging: ["#2166ac", "#4393c3", "#92c5de", "#d1e5f0", "#f7f7f7", "#fddbc7", "#f4a582", "#d6604d", "#b2182b"],
};

function hex(h: string): Rgb {
  const n = Number.parseInt(h.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const LUTS = new Map<ColormapName, Uint8ClampedArray>();

/** A 256-entry RGB lookup table (`lut[3 * i + c]`), interpolated between the map's stops. */
export function colormapLut(name: ColormapName): Uint8ClampedArray {
  const cached = LUTS.get(name);
  if (cached) return cached;
  const stops = (STOPS[name] ?? STOPS.viridis).map(hex);
  const lut = new Uint8ClampedArray(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = (i / 255) * (stops.length - 1);
    const k = Math.min(stops.length - 2, Math.floor(t));
    const f = t - k;
    for (let c = 0; c < 3; c++) lut[3 * i + c] = Math.round(stops[k]![c]! * (1 - f) + stops[k + 1]![c]! * f);
  }
  LUTS.set(name, lut);
  return lut;
}

/** The colour of `t` in [0, 1] (clamped). */
export function colorAt(name: ColormapName, t: number): Rgb {
  const lut = colormapLut(name);
  const i = Math.round(Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0)) * 255);
  return [lut[3 * i]!, lut[3 * i + 1]!, lut[3 * i + 2]!];
}

/** CSS `linear-gradient` stops of a map (for the colour bar). */
export function gradientCss(name: ColormapName, steps = 12): string {
  const parts: string[] = [];
  for (let i = 0; i < steps; i++) {
    const t = i / (steps - 1);
    const [r, g, b] = colorAt(name, t);
    parts.push(`rgb(${r} ${g} ${b}) ${(t * 100).toFixed(1)}%`);
  }
  return `linear-gradient(to right, ${parts.join(", ")})`;
}

// ------------------------------------------------------------------------------ scales

export interface ColorScale {
  /** The display range (log10 of the value when `log`). */
  lo: number;
  hi: number;
  log: boolean;
  colormap: ColormapName;
}

/** The display value of a value: log10 for log scales (`NaN` for values <= 0). */
export function toDisplay(v: number, log: boolean): number {
  if (!Number.isFinite(v)) return NaN;
  if (!log) return v;
  return v > 0 ? Math.log10(v) : NaN;
}

/** The display range of some arrays (a symmetric range around 0 for a diverging map). */
export function displayRange(arrays: readonly (ArrayLike<number> | null)[], log: boolean, colormap: ColormapName): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const a of arrays) {
    if (!a) continue;
    for (let i = 0; i < a.length; i++) {
      const d = toDisplay(a[i]!, log);
      if (!Number.isFinite(d)) continue;
      if (d < lo) lo = d;
      if (d > hi) hi = d;
    }
  }
  if (!Number.isFinite(lo)) return null;
  if (colormap === "diverging" && !log && lo < 0 && hi > 0) {
    const m = Math.max(-lo, hi);
    return [-m, m];
  }
  return [lo, hi];
}

/**
 * The display range of the rasters of a statistic group from their `lo`/`hi` only (no
 * decoding): every frame and draw share one scale, so colours compare across them.
 */
export function rasterRange(rasters: readonly (FieldRaster | null | undefined)[], encoding: FieldEncoding, log: boolean, colormap: ColormapName): [number, number] | null {
  const ends: number[] = [];
  for (const r of rasters) {
    if (!r || r.lo == null || r.hi == null) continue;
    ends.push(encoding === "log10" ? 10 ** r.lo : r.lo, encoding === "log10" ? 10 ** r.hi : r.hi);
  }
  return displayRange([ends], log, colormap);
}

/**
 * RGBA pixels of a raster (`height` x `width`, row 0 at the top unless `origin` is `lower`).
 * Missing cells are transparent.
 */
export function paint(values: ArrayLike<number>, width: number, height: number, scale: ColorScale, origin: "upper" | "lower" = "upper"): Uint8ClampedArray<ArrayBuffer> {
  const lut = colormapLut(scale.colormap);
  const out = new Uint8ClampedArray(width * height * 4);
  const span = scale.hi - scale.lo;
  for (let r = 0; r < height; r++) {
    const row = origin === "lower" ? height - 1 - r : r;
    for (let c = 0; c < width; c++) {
      const d = toDisplay(values[r * width + c]!, scale.log);
      const o = 4 * (row * width + c);
      if (!Number.isFinite(d)) continue;
      const t = span > 0 ? (d - scale.lo) / span : 0.5;
      const i = 3 * Math.round(Math.max(0, Math.min(1, t)) * 255);
      out[o] = lut[i]!;
      out[o + 1] = lut[i + 1]!;
      out[o + 2] = lut[i + 2]!;
      out[o + 3] = 255;
    }
  }
  return out;
}

/** Evenly spaced tick values of a colour bar, in the field's units (powers of ten on a log scale). */
export function colorTicks(scale: Pick<ColorScale, "lo" | "hi" | "log">, n = 5): number[] {
  const { lo, hi, log } = scale;
  if (!(hi > lo)) return [log ? 10 ** lo : lo];
  const out: number[] = [];
  for (let i = 0; i < n; i++) {
    const d = lo + ((hi - lo) * i) / (n - 1);
    out.push(log ? 10 ** d : d);
  }
  return out;
}

/** The data cell (row, column) under a point of the drawn raster (`fx`, `fy` in [0, 1)). */
export function cellAt(fx: number, fy: number, width: number, height: number, origin: "upper" | "lower" = "upper"): { row: number; col: number } {
  const col = Math.max(0, Math.min(width - 1, Math.floor(fx * width)));
  const shown = Math.max(0, Math.min(height - 1, Math.floor(fy * height)));
  return { row: origin === "lower" ? height - 1 - shown : shown, col };
}

/**
 * RGBA pixels of a mask overlay (data mode): the cells outside the mask (value 0) are shaded
 * with a light/dark checker that shows on any colour map; the other cells stay transparent.
 */
export function paintMask(mask: ArrayLike<number>, width: number, height: number, origin: "upper" | "lower" = "upper"): Uint8ClampedArray<ArrayBuffer> {
  const out = new Uint8ClampedArray(width * height * 4);
  for (let r = 0; r < height; r++) {
    const row = origin === "lower" ? height - 1 - r : r;
    for (let c = 0; c < width; c++) {
      const v = mask[r * width + c]!;
      if (!Number.isFinite(v) || v !== 0) continue;
      const o = 4 * (row * width + c);
      const light = (r + c) % 2 === 0 ? 255 : 0;
      out[o] = light;
      out[o + 1] = light;
      out[o + 2] = light;
      out[o + 3] = 150;
    }
  }
  return out;
}

/** The index of a frame's label in the frame statistics (`-1` when it is not there). */
export function frameStatIndex(coords: readonly unknown[], label: unknown): number {
  const key = String(label);
  return coords.findIndex((c) => String(c) === key);
}
