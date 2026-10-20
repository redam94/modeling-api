/**
 * The field viewer's pure helpers: decoding the quantised rasters the runner writes (and the
 * demo fixture `fields.gen.json`), colour maps, colour ranges, painting and the cell lookup.
 */
import { describe, expect, it } from "vitest";

import { FIELDS_FIXTURE } from "@/mocks/explore";

import { availableStats, cellAt, colorAt, colormapLut, colorTicks, decodeRaster, displayRange, encodeRaster, FIELD_NAN, frameValues, gradientCss, paint, rasterRange, robustRange } from "./fields";
import { parseView } from "./views";

function b64(codes: number[]): string {
  const bytes = new Uint8Array(codes.length * 2);
  const v = new DataView(bytes.buffer);
  codes.forEach((c, i) => v.setUint16(2 * i, c, true));
  return btoa(String.fromCharCode(...bytes));
}

describe("raster decoding", () => {
  it("maps uint16 codes linearly onto [lo, hi] and the sentinel to NaN", () => {
    const out = decodeRaster({ lo: -2, hi: 2, data: b64([0, 32767, 65534, FIELD_NAN]) });
    expect(out[0]).toBe(-2);
    expect(out[1]).toBeCloseTo(0, 3);
    expect(out[2]).toBe(2);
    expect(Number.isNaN(out[3])).toBe(true);
  });

  it("decodes log10 rasters to values and keeps a flat raster flat", () => {
    const out = decodeRaster({ lo: 0, hi: 3, data: b64([0, 65534]) }, "log10");
    expect(out[0]).toBeCloseTo(1);
    expect(out[1]).toBeCloseTo(1000, 0);
    expect(Array.from(decodeRaster({ lo: 5, hi: 5, data: b64([0, 0]) }))).toEqual([5, 5]);
    expect(Number.isNaN(decodeRaster({ lo: null, hi: null, data: b64([FIELD_NAN]) })[0])).toBe(true);
  });

  it("round-trips through encodeRaster to about five significant digits", () => {
    const values = [0.5, 12.25, NaN, 3, 7.75, 100];
    const back = decodeRaster(encodeRaster(values, "log10"), "log10");
    values.forEach((v, i) => (Number.isNaN(v) ? expect(Number.isNaN(back[i])).toBe(true) : expect(back[i]! / v).toBeCloseTo(1, 3)));
  });

  it("reads the demo fixture: every raster has height x width cells", () => {
    const view = parseView("fields", FIELDS_FIXTURE.fit.views.fields);
    expect(view.fields.map((f) => f.output)).toEqual(["intensity", "traction", "density"]);
    for (const f of view.fields) {
      for (const fr of f.frames) {
        for (const s of availableStats(f)) {
          const v = frameValues(f, fr, s);
          expect(v?.length).toBe(f.height * f.width);
        }
      }
    }
    const intensity = view.fields[0]!;
    expect(availableStats(intensity)).toEqual(["mean", "sd", "ratio", "q05", "q95", "draw"]);
    const fr = intensity.frames[0]!;
    const mean = frameValues(intensity, fr, "mean")!;
    const q05 = frameValues(intensity, fr, "q05")!;
    const q95 = frameValues(intensity, fr, "q95")!;
    expect(mean.every((m, i) => q05[i]! <= m * 1.001 && m <= q95[i]! * 1.001)).toBe(true);
    expect(view.fields[1]!.frames[0]!.arrows).toBeTruthy();
  });
});

describe("colour maps", () => {
  it("interpolates 256 entries between the stops", () => {
    expect(colormapLut("viridis")).toHaveLength(768);
    expect(colorAt("viridis", 0)).toEqual([0x44, 0x01, 0x54]);
    expect(colorAt("viridis", 1)).toEqual([0xfd, 0xe7, 0x25]);
    expect(colorAt("gray", 0.5)).toEqual([128, 128, 128]);
    for (const c of colorAt("diverging", 0.5)) expect(Math.abs(c - 0xf7)).toBeLessThanOrEqual(2);
    expect(colorAt("magma", 2)).toEqual(colorAt("magma", 1));
    expect(gradientCss("cividis", 3)).toMatch(/^linear-gradient\(to right, rgb\(0 34 78\) 0\.0%/);
  });

  it("paints rows top down, or bottom up for origin lower, and leaves NaN transparent", () => {
    const scale = { lo: 0, hi: 1, log: false, colormap: "gray" as const };
    const up = paint([0, 1, NaN, 0], 2, 2, scale);
    expect(Array.from(up.slice(0, 8))).toEqual([0, 0, 0, 255, 255, 255, 255, 255]);
    expect(up[11]).toBe(0);
    const low = paint([0, 1, NaN, 0], 2, 2, scale, "lower");
    expect(Array.from(low.slice(8, 16))).toEqual([0, 0, 0, 255, 255, 255, 255, 255]);
  });

  it("finds display ranges: log10, symmetric for diverging maps, from lo/hi only", () => {
    expect(displayRange([[1, 10, 100, 0]], true, "viridis")).toEqual([0, 2]);
    expect(displayRange([[-1, 3]], false, "diverging")).toEqual([-3, 3]);
    expect(displayRange([[NaN]], false, "viridis")).toBeNull();
    // a camera frame: a background near 1000 and a few bright spots
    const frame = Array.from({ length: 1600 }, (_, i) => (i % 400 === 0 ? 40_000 : 1000 + (i % 100)));
    const [lo, hi] = robustRange([frame], false, "viridis")!;
    expect(lo).toBeGreaterThanOrEqual(1000);
    expect(hi).toBeLessThan(1100);
    expect(robustRange([[1, 1, 1]], false, "viridis")).toBeNull();
    expect(robustRange([Array.from({ length: 101 }, (_, i) => i - 50)], false, "diverging")).toEqual([-49, 49]);
    const r = rasterRange([{ lo: 0, hi: 2, data: "" }, { lo: -1, hi: 1, data: "" }, null], "log10", true, "viridis");
    expect(r).toEqual([-1, 2]);
    expect(colorTicks({ lo: 0, hi: 2, log: true }, 3)).toEqual([1, 10, 100]);
  });

  it("maps a pointer position to a data cell", () => {
    expect(cellAt(0.99, 0.01, 4, 3)).toEqual({ row: 0, col: 3 });
    expect(cellAt(0.99, 0.01, 4, 3, "lower")).toEqual({ row: 2, col: 3 });
    expect(cellAt(1.5, -1, 4, 3)).toEqual({ row: 0, col: 3 });
  });
});
