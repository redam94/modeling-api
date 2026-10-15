/**
 * Dark-mode variants of the platform recipe palette (`mapi_plots.palette`).
 *
 * Vega-Lite applies an explicit mark colour over any theme config, so the recipes draw with a
 * fixed light palette and the sandbox swaps each of those colours for its dark variant when it
 * renders in dark mode. A Python test (plots-core `test_the_plot_sandbox_mirrors_the_dark_variants`)
 * keeps this table equal to `mapi_plots.palette.dark_variants()`.
 */
import type { JsonValue, Theme } from "./protocol";

/** `{light: dark}`; keys and values are lowercase `#rrggbb`. */
export const DARK_VARIANTS: Readonly<Record<string, string>> = {
  "#3366cc": "#6fa8f0", // band
  "#1f4e99": "#8ec5ff", // estimate
  "#1b2333": "#e3e7ee", // observed
  "#c62828": "#ff7b72", // alert
  "#2e7d32": "#5cc46a", // ok
  "#767e8c": "#8b95a5", // reference
  "#5b6577": "#9aa3b2", // muted
  "#333b48": "#c8cfdb", // strong
  "#6b7685": "#97a6b6", // baseline
  "#c45a00": "#f0a35e", // scenario
  "#1f6fb4": "#6fb0f2", // positive
  "#e3e6ec": "#2a3140", // empty
  "#ffffff": "#1b212d", // surface
};

/** Fills that need no text-level contrast (empty cells, separators in the card colour). */
export const NON_TEXT_FILLS: ReadonlySet<string> = new Set(["#e3e6ec", "#ffffff"]);

/** Data and inline datasets are values, not styling: never rewritten. */
const DATA_KEYS = new Set(["data", "datasets"]);

/** The spec with every recipe-palette colour replaced by its `theme` variant. */
export function themeRecipeColours(node: JsonValue, theme: Theme): JsonValue {
  if (theme !== "dark") return node;
  if (typeof node === "string") return DARK_VARIANTS[node.toLowerCase()] ?? node;
  if (Array.isArray(node)) return node.map((v) => themeRecipeColours(v, theme));
  if (node === null || typeof node !== "object") return node;
  const out: Record<string, JsonValue> = {};
  for (const [k, v] of Object.entries(node)) out[k] = DATA_KEYS.has(k) ? v : themeRecipeColours(v, theme);
  return out;
}

function luminance(hex: string): number {
  const h = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => {
    const c = parseInt(h.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio of two `#rrggbb` colours. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}
