import { describe, expect, it } from "vitest";

import { codeStats, codeValue, paintCodes, topCode } from "./readout-charts-extra";

describe("field codes", () => {
  it("leaves missing cells out of the colours and the statistics", () => {
    const codes = new Uint8Array([0, 255, 254, 255]);
    const px = paintCodes(codes, "viridis", 255);
    expect(px[4 * 1 + 3]).toBe(0); // missing: transparent
    expect(px[4 * 3 + 3]).toBe(0);
    expect(px[3]).toBe(255);
    const s = codeStats(codes, 10, 20, false, 255);
    expect(s.min).toBe(10);
    expect(s.max).toBe(20); // code 254 is vmax when 255 marks missing cells
    expect(s.mean).toBe(15); // never pulled down by the missing cells
    expect(s.missing).toBe(2);
  });

  it("keeps the old scale for blocks without a missing code", () => {
    expect(topCode(null)).toBe(255);
    expect(codeValue(255, 0, 1, false)).toBe(1);
    const s = codeStats(new Uint8Array([0, 255]), 0, 1, false, null);
    expect(s.mean).toBe(0.5);
    expect(s.missing).toBe(0);
  });
});
