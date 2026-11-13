import { describe, expect, it } from "vitest";

import { curvePath, describePrior, priorCurve, priorDomain, priorInterval, priorPdf, resolvePrior, shortNumber, signOf } from "./priors";
import { PRIOR_DISTS, type PriorDist } from "./types";

/** ∫ pdf over [lo, hi] by the trapezoid rule. */
function integrate(f: (x: number) => number, lo: number, hi: number, n = 20_000): number {
  const h = (hi - lo) / n;
  let s = (f(lo) + f(hi)) / 2;
  for (let i = 1; i < n; i++) s += f(lo + i * h);
  return s * h;
}

describe("prior densities (closed forms)", () => {
  it("match known values", () => {
    expect(priorPdf(resolvePrior({ dist: "normal", mu: 0, sigma: 1 }), 0)).toBeCloseTo(0.398942, 5);
    expect(priorPdf(resolvePrior({ dist: "normal", mu: 1, sigma: 2 }), 1)).toBeCloseTo(0.199471, 5);
    expect(priorPdf(resolvePrior({ dist: "laplace", mu: 0, sigma: 1 }), 0)).toBeCloseTo(0.5, 6);
    expect(priorPdf(resolvePrior({ dist: "half_normal", sigma: 1 }), 0)).toBeCloseTo(0.797885, 5);
    expect(priorPdf(resolvePrior({ dist: "half_normal", sigma: 1 }), -0.1)).toBe(0);
    expect(priorPdf(resolvePrior({ dist: "exponential", sigma: 2 }), 0)).toBeCloseTo(0.5, 6); // scale 2 → rate 0.5
    expect(priorPdf(resolvePrior({ dist: "lognormal", mu: 0, sigma: 1 }), 1)).toBeCloseTo(0.398942, 5);
    // Student-t with ν = 1 is the Cauchy: 1 / π at its centre; ν = 3 (the default) at 0 is 0.367553.
    expect(priorPdf(resolvePrior({ dist: "student_t", mu: 0, sigma: 1, nu: 1 }), 0)).toBeCloseTo(1 / Math.PI, 6);
    expect(priorPdf(resolvePrior({ dist: "student_t", mu: 0, sigma: 1 }), 0)).toBeCloseTo(0.367553, 5);
  });

  it.each(PRIOR_DISTS.map((d) => [d] as [PriorDist]))("%s integrates to 1", (dist) => {
    const p = resolvePrior({ dist, mu: dist === "lognormal" ? 0 : 0.5, sigma: 0.8 });
    const [lo, hi] = dist === "half_normal" || dist === "exponential" || dist === "lognormal" ? [0, 200] : [-400, 400];
    expect(integrate((x) => priorPdf(p, x), lo, hi, 400_000)).toBeCloseTo(1, 2);
  });

  it("fills the model's defaults", () => {
    expect(resolvePrior(undefined)).toEqual({ dist: "normal", mu: 0, sigma: 1, nu: 3 });
    expect(resolvePrior({ sigma: -1 }).sigma).toBe(1);
  });
});

describe("signed curves", () => {
  it("truncates a symmetric prior at 0 and renormalises it", () => {
    const c = priorCurve({ dist: "normal", mu: 0, sigma: 1 }, "positive", 401);
    expect(c.domain[0]).toBe(0);
    expect(c.ys.every((y, i) => (c.xs[i]! < 0 ? y === 0 : y >= 0))).toBe(true);
    // half of a standard normal, doubled: the density at 0 is 2 φ(0)
    expect(c.ys[0]).toBeCloseTo(2 * 0.398942, 2);
    const neg = priorCurve({ dist: "laplace", sigma: 1 }, "negative");
    expect(neg.domain[1]).toBe(0);
  });

  it("mirrors a positive prior for a negative sign and treats 'any' as positive", () => {
    expect(signOf("half_normal", "any")).toBe("positive");
    expect(signOf("exponential", "negative")).toBe("negative");
    expect(signOf("normal", undefined)).toBe("any");
    const c = priorCurve({ dist: "half_normal", sigma: 1 }, "negative");
    expect(c.domain[1]).toBe(0);
    expect(c.ys.at(-1)).toBeGreaterThan(c.ys[0]!); // the mode sits at 0, on the right
    expect(priorDomain(resolvePrior({ dist: "exponential", sigma: 1 }), "negative")[0]).toBeLessThan(0);
  });

  it("describes the 90% interval in words", () => {
    const [lo, hi] = priorInterval({ dist: "normal", mu: 0, sigma: 1 });
    expect(lo).toBeCloseTo(-1.645, 1);
    expect(hi).toBeCloseTo(1.645, 1);
    expect(describePrior({ dist: "half_normal", sigma: 0.5 }, "positive")).toMatch(/^Before seeing the data, the effect is most likely between 0\.\d+ and 0\.\d+\. It can only lift the outcome\.$/);
    expect(describePrior({ dist: "normal", sigma: 1 }, "negative")).toContain("It can only lower the outcome.");
  });

  it("draws a path inside the box and marks 0", () => {
    const { line, area, zeroX } = curvePath(priorCurve({ dist: "normal", sigma: 1 }), 100, 40);
    expect(line.startsWith("M")).toBe(true);
    expect(area.endsWith("Z")).toBe(true);
    expect(zeroX).toBeCloseTo(50, 0);
    expect(curvePath(priorCurve({ dist: "exponential", sigma: 1 }), 100, 40).zeroX).toBeCloseTo(2, 5);
  });

  it("shortens numbers", () => {
    expect(shortNumber(0)).toBe("0");
    expect(shortNumber(0.98765)).toBe("0.99");
    expect(shortNumber(12.4)).toBe("12");
    expect(shortNumber(1234)).toBe("1.2k");
    expect(shortNumber(0.0004)).toBe("4e-4");
  });
});
