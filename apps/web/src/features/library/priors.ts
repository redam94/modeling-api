/**
 * The prior picker's maths: closed-form densities of the six priors a declarative model may use
 * (`tabular.declarative_glm`, v0.18 D3), the shape a sign gives them, and plain words for what a
 * prior says. Pure TypeScript: the sparkline next to each driver is drawn from these numbers,
 * live, with no request.
 *
 * Parameters follow the model (`mapi_plugin_tabular.declarative`): `normal`, `student_t` (`nu`
 * default 3) and `laplace` take `mu` and `sigma`; `half_normal` and `exponential` take `sigma`
 * as the scale; `lognormal` takes `mu` and `sigma` on the log scale. With a sign, a symmetric
 * prior is truncated at 0 (its size is the free variable); a positive prior is positive by
 * nature and `negative` mirrors it.
 */
import { POSITIVE_DISTS, type PriorDist, type PriorSpec, type Sign } from "./types";

export const DEFAULT_NU = 3;

const SQRT_2PI = Math.sqrt(2 * Math.PI);

/** ln Γ(x) (Lanczos, g = 7): enough for the Student-t constant. */
function lnGamma(x: number): number {
  const c = [0.9999999999998099, 676.5203681218851, -1259.1392167224028, 771.3234287776531, -176.6150291621406, 12.507343278686905, -0.13857109526572012, 9.984369578019572e-6, 1.5056327351493116e-7];
  if (x < 0.5) return Math.log(Math.PI / Math.sin(Math.PI * x)) - lnGamma(1 - x);
  const z = x - 1;
  let a = c[0]!;
  const t = z + 7.5;
  for (let i = 1; i < 9; i++) a += c[i]! / (z + i);
  return 0.5 * Math.log(2 * Math.PI) + (z + 0.5) * Math.log(t) - t + Math.log(a);
}

export interface Prior {
  dist: PriorDist;
  mu: number;
  sigma: number;
  nu: number;
}

/** A prior spec with the model's defaults filled in. */
export function resolvePrior(p: PriorSpec | undefined, defaults: PriorSpec = {}): Prior {
  const q = { ...defaults, ...(p ?? {}) };
  const sigma = typeof q.sigma === "number" && q.sigma > 0 ? q.sigma : 1;
  return { dist: q.dist ?? "normal", mu: typeof q.mu === "number" && Number.isFinite(q.mu) ? q.mu : 0, sigma, nu: typeof q.nu === "number" && q.nu > 0 ? q.nu : DEFAULT_NU };
}

/** The density of the untruncated prior at `x`. */
export function priorPdf(p: Prior, x: number): number {
  const { mu, sigma, nu } = p;
  switch (p.dist) {
    case "normal": {
      const z = (x - mu) / sigma;
      return Math.exp(-0.5 * z * z) / (sigma * SQRT_2PI);
    }
    case "student_t": {
      const z = (x - mu) / sigma;
      const logC = lnGamma((nu + 1) / 2) - lnGamma(nu / 2) - 0.5 * Math.log(nu * Math.PI) - Math.log(sigma);
      return Math.exp(logC - ((nu + 1) / 2) * Math.log(1 + (z * z) / nu));
    }
    case "laplace":
      return Math.exp(-Math.abs(x - mu) / sigma) / (2 * sigma);
    case "half_normal": {
      if (x < 0) return 0;
      const z = x / sigma;
      return (2 * Math.exp(-0.5 * z * z)) / (sigma * SQRT_2PI);
    }
    case "exponential":
      return x < 0 ? 0 : Math.exp(-x / sigma) / sigma;
    case "lognormal": {
      if (x <= 0) return 0;
      const z = (Math.log(x) - mu) / sigma;
      return Math.exp(-0.5 * z * z) / (x * sigma * SQRT_2PI);
    }
  }
}

/** Is the effect's sign set by this prior and sign (positive by nature, or truncated)? */
export function signOf(dist: PriorDist, sign: Sign | undefined): Sign {
  if (POSITIVE_DISTS.has(dist)) return sign === "negative" ? "negative" : "positive";
  return sign ?? "any";
}

/** The x range worth drawing: most of the mass, and 0 when the effect can only be one sign. */
export function priorDomain(p: Prior, sign: Sign = "any"): [number, number] {
  const s = signOf(p.dist, sign);
  let lo: number;
  let hi: number;
  switch (p.dist) {
    case "half_normal":
      [lo, hi] = [0, 3.2 * p.sigma];
      break;
    case "exponential":
      [lo, hi] = [0, 4.5 * p.sigma];
      break;
    case "lognormal":
      [lo, hi] = [0, Math.min(Math.exp(p.mu + 2.6 * p.sigma), Math.exp(p.mu) * 40)];
      break;
    case "student_t": {
      const w = (p.nu <= 2 ? 6 : 4.5) * p.sigma;
      [lo, hi] = [p.mu - w, p.mu + w];
      break;
    }
    case "laplace":
      [lo, hi] = [p.mu - 5 * p.sigma, p.mu + 5 * p.sigma];
      break;
    default:
      [lo, hi] = [p.mu - 3.5 * p.sigma, p.mu + 3.5 * p.sigma];
  }
  if (POSITIVE_DISTS.has(p.dist)) return s === "negative" ? [0 - hi, 0 - lo] : [lo, hi];
  if (s === "positive") return [0, Math.max(hi, p.sigma)];
  if (s === "negative") return [Math.min(lo, -p.sigma), 0];
  return [lo, hi];
}

export interface PriorCurve {
  xs: number[];
  /** The density of the signed effect (normalised over the drawn range when truncated). */
  ys: number[];
  domain: [number, number];
  sign: Sign;
}

/**
 * The density of the effect a driver may have, on `n` points: the prior, truncated at 0 by a
 * positive or negative sign (renormalised), or mirrored for a positive prior with `negative`.
 */
export function priorCurve(spec: PriorSpec | undefined, sign: Sign = "any", n = 64): PriorCurve {
  const p = resolvePrior(spec);
  const s = signOf(p.dist, sign);
  const [lo, hi] = priorDomain(p, s);
  const xs: number[] = [];
  const ys: number[] = [];
  const mirrored = POSITIVE_DISTS.has(p.dist) && s === "negative";
  for (let i = 0; i < n; i++) {
    const x = lo + ((hi - lo) * i) / (n - 1);
    let y = priorPdf(p, mirrored ? -x : x);
    if (!POSITIVE_DISTS.has(p.dist) && ((s === "positive" && x < 0) || (s === "negative" && x > 0))) y = 0;
    xs.push(x);
    ys.push(Number.isFinite(y) ? y : 0);
  }
  if (!POSITIVE_DISTS.has(p.dist) && s !== "any") {
    // Truncation: the kept half integrates to P(side); scale it back to a density.
    const step = (hi - lo) / (n - 1);
    let mass = 0;
    for (let i = 1; i < n; i++) mass += ((ys[i - 1]! + ys[i]!) / 2) * step;
    if (mass > 0) for (let i = 0; i < n; i++) ys[i] = ys[i]! / mass;
  }
  return { xs, ys, domain: [lo, hi], sign: s };
}

/** The central interval holding about 90 % of the (signed) prior's mass, from its curve. */
export function priorInterval(spec: PriorSpec | undefined, sign: Sign = "any"): [number, number] {
  const c = priorCurve(spec, sign, 401);
  const cdf: number[] = [0];
  for (let i = 1; i < c.xs.length; i++) cdf.push(cdf[i - 1]! + ((c.ys[i - 1]! + c.ys[i]!) / 2) * (c.xs[i]! - c.xs[i - 1]!));
  const total = cdf.at(-1) || 1;
  const at = (q: number) => {
    const target = q * total;
    const i = cdf.findIndex((v) => v >= target);
    if (i <= 0) return c.xs[0] ?? 0;
    // linear interpolation inside the step that crosses the target
    const [c0, c1, x0, x1] = [cdf[i - 1]!, cdf[i]!, c.xs[i - 1]!, c.xs[i]!];
    return c1 > c0 ? x0 + ((target - c0) / (c1 - c0)) * (x1 - x0) : x1;
  };
  return [at(0.05), at(0.95)];
}

/** A short number for prior words: 0.5, 12, 1.2k, 3e-4. */
export function shortNumber(v: number): string {
  if (!Number.isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 10_000) return `${(v / 1000).toFixed(0)}k`;
  if (a >= 1000) return `${(v / 1000).toFixed(1)}k`;
  if (a >= 10) return v.toFixed(0);
  if (a >= 0.1) return String(Number(v.toFixed(2)));
  if (a >= 0.001) return String(Number(v.toFixed(3)));
  return v.toExponential(0);
}

export const PRIOR_LABEL: Record<PriorDist, string> = {
  normal: "Normal",
  student_t: "Student-t (heavier tails)",
  laplace: "Laplace (pulls to the centre)",
  half_normal: "Half-normal (positive)",
  exponential: "Exponential (positive, small is likely)",
  lognormal: "Log-normal (positive, skewed)",
};

/** What the prior says, in words: "most likely between 0.03 and 0.98 (only lifts it)". */
export function describePrior(spec: PriorSpec | undefined, sign: Sign = "any"): string {
  const s = signOf(resolvePrior(spec).dist, sign);
  const [lo, hi] = priorInterval(spec, sign);
  const dir = s === "positive" ? " It can only lift the outcome." : s === "negative" ? " It can only lower the outcome." : "";
  return `Before seeing the data, the effect is most likely between ${shortNumber(lo)} and ${shortNumber(hi)}.${dir}`;
}

/** The SVG path of a curve scaled into `width` × `height` (the y axis starts at 0). */
export function curvePath(curve: PriorCurve, width: number, height: number, pad = 2): { line: string; area: string; zeroX: number | null } {
  const [lo, hi] = curve.domain;
  const span = hi - lo || 1;
  const maxY = Math.max(...curve.ys, 1e-12);
  const x = (v: number) => pad + ((v - lo) / span) * (width - 2 * pad);
  const y = (v: number) => height - pad - (v / maxY) * (height - 2 * pad);
  const pts = curve.xs.map((v, i) => `${x(v).toFixed(2)},${y(curve.ys[i] ?? 0).toFixed(2)}`);
  const line = `M${pts.join(" L")}`;
  const area = `${line} L${x(hi).toFixed(2)},${(height - pad).toFixed(2)} L${x(lo).toFixed(2)},${(height - pad).toFixed(2)} Z`;
  const zeroX = lo <= 0 && hi >= 0 ? x(0) : null;
  return { line, area, zeroX };
}
